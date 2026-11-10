"""Local, read-only collectors for AI coding-agent sessions.

Supported sources:
  * Claude Code  (~/.claude/projects/**/*.jsonl)
  * Codex CLI    (~/.codex/sessions/**/rollout-*.jsonl)
  * OpenCode     (~/.local/share/opencode/opencode.db)

Everything here mirrors the data abtop displays: token usage, context fill,
status, recent tools/chat, child processes, listening ports, git state and
account rate limits.  No network calls and no credentials are read.
"""

from __future__ import annotations

import os
import time

import psutil

from ..model import AgentSession, ChildProc, ProcInfo, RateLimit
from .agent_claude import ClaudeState
from .agent_codex import CodexState
from .agent_common import (
    CLAUDE_DIRS,
    HOME,
    GitCache,
    _scan_files,
    read_rate_limits,
    read_summaries,
)
from .agent_opencode import OpenCodeCollector


class AgentCollector:
    def __init__(self) -> None:
        self._claude: dict[str, ClaudeState] = {}
        self._codex: dict[str, CodexState] = {}
        self._opencode = OpenCodeCollector()
        self._git = GitCache()
        self._summaries_at = 0.0
        self._summaries: dict[str, str] = {}
        self._last_cutoff = 0.0
        self._last_all = False
        self._selected_id = ""
        self._last_open_fd_scan = 0.0
        self._fd_map: dict[str, int] = {}

    # -- discovery -------------------------------------------------------

    def _get_summaries(self) -> dict[str, str]:
        now = time.monotonic()
        if now - self._summaries_at > 30:
            self._summaries_at = now
            self._summaries = read_summaries()
        return self._summaries

    def _scan_open_transcripts(self, procs: dict[int, ProcInfo], force: bool = False) -> dict[str, int]:
        now = time.monotonic()
        if not force and now - self._last_open_fd_scan < 10:
            return self._fd_map
        self._last_open_fd_scan = now
        mapping: dict[str, int] = {}
        pids = []
        for proc in procs.values():
            cls = classify_agent_process(proc)
            if cls in ("claude", "codex"):
                pids.append(proc.pid)
        for pid in pids[:30]:
            try:
                fd_dir = f"/proc/{pid}/fd"
                for name in os.listdir(fd_dir):
                    try:
                        target = os.readlink(os.path.join(fd_dir, name))
                    except OSError:
                        continue
                    if target.endswith(".jsonl") and ("/.claude/" in target or "/.codex/" in target):
                        mapping[target] = pid
            except OSError:
                continue
        self._fd_map = mapping
        return mapping

    def collect(
        self,
        procs: dict[int, ProcInfo],
        scope: str = "recent",
        selected_id: str = "",
    ) -> tuple[list[AgentSession], list[RateLimit]]:
        now = time.time()
        self._selected_id = selected_id
        cutoff = now - (1800 if scope == "recent" else 86400)
        summaries = self._get_summaries()
        fd_map = self._scan_open_transcripts(procs)

        sessions: list[AgentSession] = []

        # Claude Code transcript scan.
        claude_files = _scan_files([d / "projects" for d in CLAUDE_DIRS], cutoff, 40)
        if not claude_files and scope == "all":
            claude_files = _scan_files([d / "projects" for d in CLAUDE_DIRS], now - 7 * 86400, 20)
        for path, _mtime in claude_files:
            state = self._claude.get(path)
            if state is None:
                state = ClaudeState(path=path)
                self._claude[path] = state
            for obj in state.tailer.read_new(path):
                state.feed(obj)
            if state.updated_at and state.updated_at < cutoff - 3600:
                continue
            session = state.to_session(summaries)
            session.pid = self._match_pid(session, procs, fd_map)
            sessions.append(session)

        # Codex rollout scan.
        codex_files = _scan_files([HOME / ".codex" / "sessions"], cutoff, 50)
        if not codex_files and scope == "all":
            codex_files = _scan_files([HOME / ".codex" / "sessions"], now - 7 * 86400, 25)
        for path, _mtime in codex_files:
            state = self._codex.get(path)
            if state is None:
                state = CodexState(path=path)
                self._codex[path] = state
            for obj in state.tailer.read_new(path):
                state.feed(obj)
            if state.updated_at and state.updated_at < cutoff - 3600:
                continue
            session = state.to_session(summaries)
            session.pid = self._match_pid(session, procs, fd_map)
            sessions.append(session)

        # OpenCode.
        if self._opencode.db_path:
            sessions.extend(
                self._opencode.sessions(
                    cutoff,
                    30 if scope == "all" else 18,
                    selected_id=selected_id,
                )
            )

        # Drop duplicates (same agent + session id), keeping the newest.
        dedup: dict[tuple[str, str], AgentSession] = {}
        for session in sessions:
            key = (session.agent, session.session_id)
            old = dedup.get(key)
            if old is None or session.updated_at > old.updated_at:
                dedup[key] = session
        sessions = list(dedup.values())

        # Attach process memory/tree, and rich detail for the selected session.
        children_map = build_children_map(procs)
        self._attach_process_metrics(sessions, procs, children_map)
        self._attach_git_and_details(sessions, procs)

        sessions.sort(
            key=lambda s: (
                0 if s.is_active else 1,
                -(s.updated_at or 0),
            )
        )

        quotas = read_rate_limits()
        for state in self._codex.values():
            if state.rate_limit:
                q = state.rate_limit
                quotas = [old for old in quotas if old.source != "codex"] + [q]
                break

        # Trim very old collector states.
        self._prune(now)
        return sessions, quotas

    def _match_pid(self, session: AgentSession, procs: dict[int, ProcInfo], fd_map: dict[str, int]) -> int:
        if session.source_path and session.source_path in fd_map:
            return fd_map[session.source_path]
        candidates = [p for p in procs.values() if classify_agent_process(p) == session.agent]
        if not candidates:
            return 0
        cwd = os.path.realpath(session.cwd) if session.cwd else ""
        if cwd:
            for proc in candidates:
                pcwd = proc.cwd
                if not pcwd:
                    try:
                        pcwd = psutil.Process(proc.pid).cwd()
                    except Exception:
                        pcwd = ""
                if pcwd and os.path.realpath(pcwd) == cwd:
                    return proc.pid
        if session.agent == "opencode":
            servers = [p for p in candidates if "serve" in p.cmdline]
            if servers:
                return max(servers, key=lambda p: p.create_time).pid
        if session.started_at:
            return min(candidates, key=lambda p: abs(p.create_time - session.started_at)).pid
        return max(candidates, key=lambda p: p.create_time).pid

    def _attach_process_metrics(
        self,
        sessions: list[AgentSession],
        procs: dict[int, ProcInfo],
        children_map: dict[int, list[int]],
    ) -> None:
        for session in sessions:
            if not session.pid:
                continue
            total_rss = 0
            stack = [session.pid]
            seen: set[int] = set()
            while stack:
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                proc = procs.get(pid)
                if proc:
                    total_rss += proc.rss
                stack.extend(children_map.get(pid, []))
            session.mem_mb = int(total_rss / (1024 * 1024))

    def _attach_git_and_details(self, sessions: list[AgentSession], procs: dict[int, ProcInfo]) -> None:
        selected = next((s for s in sessions if s.session_id == self._selected_id), None)
        for session in sessions:
            if not session.pid and session.agent != "opencode":
                continue
            if session is selected:
                if not session.git_branch and session.cwd:
                    branch, added, modified = self._git.get(session.cwd)
                    session.git_branch = branch
                    session.git_added = added
                    session.git_modified = modified
                elif session.cwd and not session.git_added and not session.git_modified:
                    branch, added, modified = self._git.get(session.cwd)
                    if not session.git_branch:
                        session.git_branch = branch
                    session.git_added = added
                    session.git_modified = modified
                session.children = self._children(session, procs)
                if session.agent == "opencode" and not session.chat:
                    chat, tools, current = self._opencode.details(session.session_id)
                    if chat:
                        session.chat = chat
                    if tools:
                        session.tools = tools
                    if current:
                        session.current_task = current

    def _children(
        self, session: AgentSession, procs: dict[int, ProcInfo], limit: int = 10
    ) -> list[ChildProc]:
        if not session.pid:
            return []
        children_map = build_children_map(procs)
        ports = read_listening_ports()
        stack = list(children_map.get(session.pid, []))
        seen: set[int] = set()
        out: list[ChildProc] = []
        while stack and len(out) < limit:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            proc = procs.get(pid)
            if not proc:
                continue
            port = ports.get(pid)
            out.append(
                ChildProc(
                    pid=pid,
                    command=proc.cmd or proc.name,
                    mem_kb=int(proc.rss / 1024),
                    port=port,
                )
            )
            stack.extend(children_map.get(pid, []))
        out.sort(key=lambda c: (c.port is None, c.command))
        return out[:limit]

    def _prune(self, now: float) -> None:
        if len(self._claude) > 120:
            for path, state in list(self._claude.items()):
                if now - state.updated_at > 7200:
                    del self._claude[path]
        if len(self._codex) > 120:
            for path, state in list(self._codex.items()):
                if now - state.updated_at > 7200:
                    del self._codex[path]


def classify_agent_process(proc: ProcInfo) -> str:
    name = (proc.name or "").lower()
    argv = proc.cmdline or []
    joined = " ".join(argv).lower()
    base = os.path.basename(argv[0]).lower() if argv else name
    if base in ("claude", "claude-code") or "claude-code" in joined or "/.claude/" in joined:
        if "claude" in base or "claude-code" in joined:
            return "claude"
    if base == "codex" or (base.endswith("/codex")) or (base == "codex" and "app-server" not in joined):
        if "mcp-server" in joined or "exec-server" in joined:
            return ""
        return "codex"
    if base == "opencode" or "opencode" in base:
        return "opencode"
    if base == "node" and "opencode" in joined:
        return "opencode"
    return ""


def build_children_map(procs: dict[int, ProcInfo]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for proc in procs.values():
        if proc.ppid:
            out.setdefault(proc.ppid, []).append(proc.pid)
    return out


def read_listening_ports() -> dict[int, int]:
    ports: dict[int, int] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError, OSError):
        return ports
    for conn in conns:
        if conn.status == psutil.CONN_LISTEN and conn.pid and conn.laddr:
            ports.setdefault(conn.pid, int(conn.laddr.port))
    return ports

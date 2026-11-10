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

import json
import os
import sqlite3
import subprocess
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import psutil

from ..model import AgentSession, ChildProc, ProcInfo, RateLimit

HOME = Path.home()
SUMMARIES_FILE = HOME / ".cache" / "abtop" / "summaries.json"
CODEX_CACHE = HOME / ".cache" / "abtop" / "codex-rate-limits.json"
CLAUDE_DIRS = [HOME / ".claude"]
if os.environ.get("CLAUDE_CONFIG_DIR"):
    CLAUDE_DIRS.insert(0, Path(os.environ["CLAUDE_CONFIG_DIR"]))
with suppress(Exception):
    CLAUDE_DIRS.extend(sorted(HOME.glob(".claude-*")))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def parse_iso(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def redact(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").replace("\x00", "").split())
    # Avoid printing anything that looks like a secret.
    for marker in ("sk-", "ghp_", "xoxb-", "AKIA"):
        if marker in text:
            text = text[: text.index(marker)] + "<redacted>"
    return text[:limit] + ("…" if len(text) > limit else "")


def safe_json(line: bytes) -> Optional[dict]:
    try:
        value = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


class Tailer:
    """Incremental JSONL reader.  Only new lines are parsed on each poll."""

    def __init__(self) -> None:
        self.offset = 0
        self.partial = b""
        self.mtime = 0.0
        self.size = 0

    def read_new(self, path: str) -> list[dict]:
        try:
            stat = os.stat(path)
        except OSError:
            return []
        if stat.st_size < self.offset:
            self.offset = 0
            self.partial = b""
        if stat.st_mtime == self.mtime and stat.st_size == self.size:
            return []
        self.mtime = stat.st_mtime
        self.size = stat.st_size
        try:
            with open(path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return []
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()
        out = []
        for line in lines:
            if not line.strip():
                continue
            obj = safe_json(line)
            if obj is not None:
                out.append(obj)
        return out


def read_summaries() -> dict[str, str]:
    try:
        data = json.loads(SUMMARIES_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


def _scan_files(roots: Iterable[Path], cutoff: float, limit: int = 80) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith(".jsonl"):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        mtime = os.stat(path).st_mtime
                    except OSError:
                        continue
                    if mtime >= cutoff:
                        found.append((path, mtime))
        except OSError:
            continue
    found.sort(key=lambda item: item[1], reverse=True)
    return found[:limit]


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


@dataclass
class ClaudeState:
    path: str
    tailer: Tailer = field(default_factory=Tailer)
    session_id: str = ""
    cwd: str = ""
    project: str = ""
    model: str = ""
    version: str = ""
    git_branch: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    turn_count: int = 0
    current_turn_tokens: int = 0
    context_tokens: int = 0
    context_window: int = 200_000
    last_role: str = ""
    last_stop: str = ""
    pending_tool: bool = False
    last_tool: tuple[str, str] = ("", "")
    first_prompt: str = ""
    chat: deque = field(default_factory=lambda: deque(maxlen=10))
    tools: deque = field(default_factory=lambda: deque(maxlen=12))
    token_history: deque = field(default_factory=lambda: deque(maxlen=48))
    seen_summary: bool = False

    def feed(self, obj: dict) -> None:
        ts = parse_iso(obj.get("timestamp"))
        if ts:
            self.updated_at = max(self.updated_at, ts)
            if not self.started_at:
                self.started_at = ts
        for key in ("sessionId", "cwd", "version", "gitBranch"):
            value = obj.get(key)
            if value:
                if key == "sessionId" and not self.session_id:
                    self.session_id = str(value)
                elif key == "cwd" and not self.cwd:
                    self.cwd = str(value)
                elif key == "version" and not self.version:
                    self.version = str(value)
                elif key == "gitBranch":
                    self.git_branch = str(value)
        kind = obj.get("type")
        msg = obj.get("message") or {}
        if kind == "user":
            content = msg.get("content")
            texts: list[str] = []
            has_tool_result = False
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_result":
                        has_tool_result = True
                    elif item.get("type") == "text" and item.get("text"):
                        texts.append(str(item["text"]))
            if has_tool_result:
                self.pending_tool = False
                self.last_role = "tool_result"
            elif texts and not obj.get("isMeta"):
                text = redact(texts[0], 1000)
                if text and "conversation title generator" not in text.lower():
                    if not self.first_prompt:
                        self.first_prompt = redact(texts[0], 90)
                    self.chat.append(("you", redact(texts[0], 180)))
                self.last_role = "user"
                self.pending_tool = False
        elif kind == "assistant":
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                inp = int(usage.get("input_tokens") or 0)
                out = int(usage.get("output_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                self.input_tokens += inp
                self.output_tokens += out
                self.cache_read += cr
                self.cache_write += cw
                if inp or out or cr or cw:
                    self.context_tokens = inp + cr + cw
                    self.current_turn_tokens += inp + out + cr + cw
            model = msg.get("model")
            if model and model != "<synthetic>":
                self.model = str(model)
                self.context_window = 1_000_000 if "1m" in self.model.lower() else 200_000
            content = msg.get("content")
            has_tool = False
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype == "text" and item.get("text"):
                        text = str(item["text"])
                        if text.strip() and not self.seen_summary:
                            self.chat.append(("ai", redact(text, 180)))
                    elif itype == "tool_use":
                        has_tool = True
                        name = str(item.get("name") or "tool")
                        inp = item.get("input") or {}
                        arg = ""
                        if isinstance(inp, dict):
                            for key in ("file_path", "path", "command", "pattern", "description", "prompt"):
                                if inp.get(key):
                                    arg = redact(str(inp[key]), 90)
                                    break
                            if not arg and inp:
                                arg = redact(str(next(iter(inp.values()))), 90)
                        self.last_tool = (name, arg)
                        self.tools.append((name, arg))
            self.pending_tool = has_tool
            self.last_role = "assistant"
            self.last_stop = str(msg.get("stop_reason") or "")
            completed = self.last_stop in ("end_turn", "stop_sequence", "max_tokens")
            if not self.last_stop and not has_tool:
                completed = True
            if completed:
                self.turn_count += 1
                if self.current_turn_tokens:
                    self.token_history.append(self.current_turn_tokens)
                self.current_turn_tokens = 0

    def status(self, now: float) -> str:
        age = now - self.updated_at if self.updated_at else 1e9
        if age > 900:
            return "done"
        if self.pending_tool:
            return "executing"
        if self.last_role in ("user", "tool_result"):
            return "thinking"
        if age > 90:
            return "done"
        return "waiting"

    def to_session(self, summaries: dict[str, str]) -> AgentSession:
        project = self.project or (os.path.basename(self.cwd.rstrip("/")) if self.cwd else "?")
        summary = summaries.get(self.session_id) or self.first_prompt or project
        current = ""
        if self.pending_tool and self.last_tool[0]:
            current = f"{self.last_tool[0]} {self.last_tool[1]}".strip()
        elif self.last_role in ("user", "tool_result"):
            current = "thinking…"
        context_pct = None
        if self.context_window and self.context_tokens:
            context_pct = min(100.0, self.context_tokens / self.context_window * 100.0)
        return AgentSession(
            agent="claude",
            session_id=self.session_id or os.path.basename(self.path).replace(".jsonl", "")[:12],
            project=project,
            cwd=self.cwd,
            status=self.status(time.time()),
            model=self.model,
            context_pct=context_pct,
            context_tokens=self.context_tokens,
            context_window=self.context_window,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            total_tokens=self.input_tokens + self.output_tokens + self.cache_read + self.cache_write,
            turn_count=self.turn_count,
            started_at=self.started_at,
            updated_at=self.updated_at,
            version=self.version,
            git_branch=self.git_branch,
            current_task=current,
            summary=summary,
            chat=list(self.chat)[-6:],
            tools=list(self.tools)[-8:],
            token_history=list(self.token_history)[-32:],
            source_path=self.path,
        )


# ---------------------------------------------------------------------------
# Codex CLI
# ---------------------------------------------------------------------------


@dataclass
class CodexState:
    path: str
    tailer: Tailer = field(default_factory=Tailer)
    session_id: str = ""
    cwd: str = ""
    project: str = ""
    model: str = ""
    effort: str = ""
    version: str = ""
    git_branch: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    context_tokens: int = 0
    context_window: int = 0
    turn_count: int = 0
    current_turn_tokens: int = 0
    last_event: str = ""
    pending_tool: bool = False
    last_tool: tuple[str, str] = ("", "")
    first_prompt: str = ""
    chat: deque = field(default_factory=lambda: deque(maxlen=10))
    tools: deque = field(default_factory=lambda: deque(maxlen=12))
    token_history: deque = field(default_factory=lambda: deque(maxlen=48))
    rate_limit: Optional[RateLimit] = None

    def feed(self, obj: dict) -> None:
        ts = parse_iso(obj.get("timestamp"))
        if ts:
            self.updated_at = max(self.updated_at, ts)
            if not self.started_at:
                self.started_at = ts
        typ = obj.get("type")
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            return
        if typ == "session_meta":
            self.session_id = str(payload.get("session_id") or payload.get("id") or self.session_id)
            self.cwd = str(payload.get("cwd") or self.cwd)
            self.version = str(payload.get("cli_version") or self.version)
            started = parse_iso(payload.get("timestamp"))
            if started:
                self.started_at = started
        elif typ == "turn_context":
            self.cwd = str(payload.get("cwd") or self.cwd)
            self.model = str(payload.get("model") or self.model)
            self.effort = str(payload.get("effort") or self.effort)
            cw = payload.get("model_context_window")
            if cw:
                self.context_window = int(cw)
        elif typ == "event_msg":
            etype = payload.get("type") or ""
            self.last_event = etype
            if etype == "task_started":
                started = payload.get("started_at")
                if started:
                    self.started_at = float(started)
                cw = payload.get("model_context_window")
                if cw:
                    self.context_window = int(cw)
                self.pending_tool = False
            elif etype == "user_message":
                text = payload.get("message") or payload.get("text") or ""
                if text:
                    if not self.first_prompt:
                        self.first_prompt = redact(str(text), 90)
                    self.chat.append(("you", redact(str(text), 180)))
            elif etype == "agent_message":
                text = payload.get("message") or payload.get("text") or ""
                if text:
                    self.chat.append(("ai", redact(str(text), 180)))
                self.pending_tool = False
            elif etype == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage") or {}
                if isinstance(total, dict):
                    inp = int(total.get("input_tokens") or 0)
                    out = int(total.get("output_tokens") or 0)
                    cache = int(total.get("cached_input_tokens") or total.get("cache_read_input_tokens") or 0)
                    self.input_tokens = max(0, inp - cache)
                    self.output_tokens = out
                    self.cache_read = cache
                    self.cache_write = int(total.get("cache_write_input_tokens") or 0)
                last = info.get("last_token_usage") or {}
                if isinstance(last, dict):
                    inp = int(last.get("input_tokens") or 0)
                    out = int(last.get("output_tokens") or 0)
                    self.context_tokens = inp
                    if inp or out:
                        self.current_turn_tokens += inp + out
                cw = info.get("model_context_window")
                if cw:
                    self.context_window = int(cw)
                rl = payload.get("rate_limits") or {}
                if isinstance(rl, dict) and str(rl.get("limit_id") or "codex") in ("codex", ""):
                    quota = RateLimit(source="codex", updated_at=ts, live=True)
                    for slot in ("primary", "secondary"):
                        win = rl.get(slot) or {}
                        if not isinstance(win, dict):
                            continue
                        mins = int(win.get("window_minutes") or 0)
                        pct = win.get("used_percent")
                        reset = win.get("resets_at")
                        if pct is None:
                            continue
                        if mins and mins > 300:
                            quota.seven_pct = float(pct)
                            quota.seven_reset = float(reset) if reset else None
                            quota.seven_window = mins
                        else:
                            quota.five_pct = float(pct)
                            quota.five_reset = float(reset) if reset else None
                            quota.five_window = mins or None
                    if quota.five_pct is not None or quota.seven_pct is not None:
                        self.rate_limit = quota
            elif etype == "task_complete":
                self.pending_tool = False
                self.last_event = "task_complete"
                self.turn_count += 1
                if self.current_turn_tokens:
                    self.token_history.append(self.current_turn_tokens)
                self.current_turn_tokens = 0
            elif etype == "function_call":
                self.pending_tool = True
        elif typ == "response_item":
            rtype = payload.get("type")
            if rtype == "function_call":
                name = payload.get("name") or payload.get("tool_name") or "tool"
                args = payload.get("arguments") or payload.get("input") or ""
                arg = redact(str(args), 90)
                self.last_tool = (str(name), arg)
                self.tools.append((str(name), arg))
                self.pending_tool = True
                self.last_event = "function_call"
            elif rtype == "function_call_output":
                self.pending_tool = False
            elif rtype == "message":
                role = payload.get("role")
                content = payload.get("content")
                text = ""
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") in ("output_text", "text"):
                            text += str(item.get("text") or "")
                elif isinstance(content, str):
                    text = content
                if text and role in ("user", "assistant"):
                    self.chat.append(("you" if role == "user" else "ai", redact(text, 180)))
                    if role == "user" and not self.first_prompt:
                        self.first_prompt = redact(text, 90)

    def status(self, now: float) -> str:
        age = now - self.updated_at if self.updated_at else 1e9
        if age > 900:
            return "done"
        if self.pending_tool:
            return "executing"
        if self.last_event in ("task_started", "user_message"):
            return "thinking"
        if self.last_event in ("agent_message", "task_complete"):
            return "done" if age > 90 else "waiting"
        if age > 90:
            return "done"
        return "waiting"

    def to_session(self, summaries: dict[str, str]) -> AgentSession:
        project = self.project or (os.path.basename(self.cwd.rstrip("/")) if self.cwd else "?")
        summary = summaries.get(self.session_id) or self.first_prompt or project
        current = ""
        if self.pending_tool and self.last_tool[0]:
            current = f"{self.last_tool[0]} {self.last_tool[1]}".strip()
        elif self.last_event in ("task_started", "user_message"):
            current = "thinking…"
        context_pct = None
        if self.context_window and self.context_tokens:
            context_pct = min(100.0, self.context_tokens / self.context_window * 100.0)
        return AgentSession(
            agent="codex",
            session_id=self.session_id or os.path.basename(self.path)[:12],
            project=project,
            cwd=self.cwd,
            status=self.status(time.time()),
            model=self.model,
            effort=self.effort,
            context_pct=context_pct,
            context_tokens=self.context_tokens,
            context_window=self.context_window,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read=self.cache_read,
            cache_write=self.cache_write,
            total_tokens=self.input_tokens + self.output_tokens + self.cache_read + self.cache_write,
            turn_count=self.turn_count,
            started_at=self.started_at,
            updated_at=self.updated_at,
            version=self.version,
            git_branch=self.git_branch,
            current_task=current,
            summary=summary,
            chat=list(self.chat)[-6:],
            tools=list(self.tools)[-8:],
            token_history=list(self.token_history)[-32:],
            source_path=self.path,
        )


# ---------------------------------------------------------------------------
# OpenCode (SQLite)
# ---------------------------------------------------------------------------


OPENCODE_DBS = [
    HOME / ".local" / "share" / "opencode" / "opencode.db",
    HOME / ".local" / "share" / "opencode" / "db.sqlite",
]


def find_opencode_db() -> Optional[Path]:
    for path in OPENCODE_DBS:
        if path.exists():
            return path
    return None


class OpenCodeCollector:
    def __init__(self) -> None:
        self.db_path = find_opencode_db()
        self._conn: Optional[sqlite3.Connection] = None
        self._failed_at = 0.0

    def _connect(self) -> Optional[sqlite3.Connection]:
        if not self.db_path:
            return None
        if self._conn is not None:
            return self._conn
        if time.monotonic() - self._failed_at < 30:
            return None
        try:
            uri = f"file:{self.db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            self._conn = conn
            return conn
        except Exception:
            self._failed_at = time.monotonic()
            return None

    def _reset(self) -> None:
        try:
            if self._conn:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def sessions(self, cutoff: float, limit: int = 30, selected_id: str = "") -> list[AgentSession]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                """
                SELECT id, title, directory, parent_id, model, cost,
                       tokens_input, tokens_output, tokens_reasoning,
                       tokens_cache_read, tokens_cache_write,
                       time_created, time_updated, version
                FROM session
                WHERE time_updated >= ?
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (int(cutoff * 1000), limit),
            ).fetchall()
        except sqlite3.Error:
            self._reset()
            return []

        sessions: list[AgentSession] = []
        now = time.time()
        for row in rows:
            if row["parent_id"]:
                continue
            sid = str(row["id"])
            title = str(row["title"] or sid)
            directory = str(row["directory"] or "")
            model = self._model_name(str(row["model"] or ""))
            updated = int(row["time_updated"] or 0) / 1000.0
            created = int(row["time_created"] or 0) / 1000.0
            _last_tokens, context_tokens, last_text, turns, turn_history = self._last_assistant(
                conn, sid, include_turn_history=(sid == selected_id)
            )
            context_window = self._guess_context_window(model)
            context_pct = None
            if context_window and context_tokens:
                context_pct = min(100.0, context_tokens / context_window * 100.0)
            age = now - updated
            if age < 15:
                status = "thinking"
            elif age < 180:
                status = "waiting"
            else:
                status = "idle"
            sessions.append(
                AgentSession(
                    agent="opencode",
                    session_id=sid,
                    project=os.path.basename(directory.rstrip("/")) or title,
                    cwd=directory,
                    status=status,
                    model=model,
                    context_pct=context_pct,
                    context_tokens=context_tokens,
                    context_window=context_window,
                    input_tokens=int(row["tokens_input"] or 0),
                    output_tokens=int(row["tokens_output"] or 0),
                    cache_read=int(row["tokens_cache_read"] or 0),
                    cache_write=int(row["tokens_cache_write"] or 0),
                    total_tokens=(
                        int(row["tokens_input"] or 0)
                        + int(row["tokens_output"] or 0)
                        + int(row["tokens_reasoning"] or 0)
                        + int(row["tokens_cache_read"] or 0)
                        + int(row["tokens_cache_write"] or 0)
                    ),
                    turn_count=turns,
                    started_at=created,
                    updated_at=updated,
                    version=str(row["version"] or ""),
                    current_task=title,
                    summary=title,
                    chat=[("ai", last_text)] if last_text else [],
                    token_history=turn_history,
                    source_path=str(self.db_path),
                )
            )
        return sessions

    def _last_assistant(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        *,
        include_turn_history: bool = False,
    ) -> tuple[int, int, str, int, list[int]]:
        # Deliberately avoid a LIKE scan over the (often multi-GB) message
        # table: walk the last few rows through the session/time index instead.
        try:
            limit = 256 if include_turn_history else 24
            rows = conn.execute(
                "SELECT data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        except sqlite3.Error:
            return 0, 0, "", 0, []
        parsed: list[dict] = []
        for row in reversed(rows):
            try:
                data = json.loads(row["data"])
            except Exception:
                continue
            if isinstance(data, dict):
                parsed.append(data)
        last_tokens = 0
        context_tokens = 0
        for data in reversed(parsed):
            if data.get("role") != "assistant":
                continue
            tokens = data.get("tokens") or {}
            cache = tokens.get("cache") or {}
            last_tokens = _opencode_message_tokens(tokens)
            context_tokens = (
                int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
            )
            break

        turn_history: list[int] = []
        turn_count = 0
        if include_turn_history:
            turn_tokens = 0
            in_turn = False
            has_assistant = False
            for data in parsed:
                role = data.get("role")
                if role == "user":
                    if in_turn and has_assistant:
                        turn_history.append(turn_tokens)
                    in_turn = True
                    has_assistant = False
                    turn_tokens = 0
                elif role == "assistant" and in_turn:
                    has_assistant = True
                    turn_tokens += _opencode_message_tokens(data.get("tokens") or {})
            if in_turn and has_assistant:
                turn_history.append(turn_tokens)
            try:
                turn_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM message "
                        "WHERE session_id = ? AND json_extract(data, '$.role') = 'user'",
                        (session_id,),
                    ).fetchone()[0]
                )
            except sqlite3.Error:
                turn_count = sum(1 for data in parsed if data.get("role") == "user")
            turn_history = turn_history[-32:]
        return last_tokens, context_tokens, "", turn_count, turn_history

    def details(self, session_id: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
        """Return (chat, tools, current task) for one session."""
        conn = self._connect()
        if conn is None:
            return [], [], ""
        try:
            rows = conn.execute(
                """
                SELECT m.data AS mdata, p.data AS pdata
                FROM part p JOIN message m ON m.id = p.message_id
                WHERE p.session_id = ?
                ORDER BY p.time_created DESC LIMIT 60
                """,
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            self._reset()
            return [], [], ""
        chat: list[tuple[str, str]] = []
        tools: list[tuple[str, str]] = []
        current = ""
        for row in reversed(rows):
            try:
                mdata = json.loads(row["mdata"])
                pdata = json.loads(row["pdata"])
            except Exception:
                continue
            role = mdata.get("role")
            ptype = pdata.get("type")
            if ptype == "text" and pdata.get("text"):
                text = redact(str(pdata["text"]), 180)
                if role in ("user", "assistant") and text:
                    chat.append(("you" if role == "user" else "ai", text))
            elif ptype == "tool":
                name = str(pdata.get("tool") or pdata.get("name") or "tool")
                state = pdata.get("state") or {}
                inp = state.get("input") if isinstance(state, dict) else None
                arg = redact(str(inp), 90) if inp else ""
                tools.append((name, arg))
                status = state.get("status") if isinstance(state, dict) else ""
                if status in ("running", "pending"):
                    current = f"{name} {arg}".strip()
        return chat[-6:], tools[-8:], current

    @staticmethod
    def _model_name(raw: str) -> str:
        if not raw:
            return ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                model = str(data.get("id") or "")
                provider = str(data.get("providerID") or "")
                if provider and model and provider not in model:
                    return model
                return model
        except Exception:
            pass
        return raw[:40]

    @staticmethod
    def _guess_context_window(model: str) -> int:
        low = model.lower()
        for marker, size in (
            ("1m", 1_000_000),
            ("1.0m", 1_000_000),
            ("200k", 200_000),
            ("128k", 128_000),
            ("96k", 96_000),
            ("64k", 64_000),
            ("32k", 32_000),
        ):
            if marker in low:
                return size
        return 0


def _opencode_message_tokens(tokens: dict) -> int:
    cache = tokens.get("cache") or {}
    return sum(
        int(value or 0)
        for value in (
            tokens.get("input"),
            tokens.get("output"),
            tokens.get("reasoning"),
            cache.get("read"),
            cache.get("write"),
        )
    )


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------


def _read_rate_file(path: Path, source: str) -> Optional[RateLimit]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    five = data.get("five_hour") or {}
    seven = data.get("seven_day") or {}
    if not five and not seven:
        return None
    return RateLimit(
        source=str(data.get("source") or source),
        five_pct=five.get("used_percentage"),
        five_reset=five.get("resets_at") or None,
        five_window=five.get("window_minutes"),
        seven_pct=seven.get("used_percentage"),
        seven_reset=seven.get("resets_at") or None,
        seven_window=seven.get("window_minutes"),
        updated_at=data.get("updated_at"),
        live=False,
    )


def read_rate_limits() -> list[RateLimit]:
    out: list[RateLimit] = []
    for directory in CLAUDE_DIRS:
        quota = _read_rate_file(directory / "abtop-rate-limits.json", "claude")
        if quota:
            out.append(quota)
            break
    codex = _read_rate_file(CODEX_CACHE, "codex")
    if codex:
        out.append(codex)
    return out


# ---------------------------------------------------------------------------
# Process / port / git helpers
# ---------------------------------------------------------------------------


class GitCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str, int, int]] = {}

    def get(self, cwd: str, timeout: float = 0.7) -> tuple[str, int, int]:
        if not cwd or not os.path.isdir(cwd):
            return "", 0, 0
        now = time.monotonic()
        hit = self._cache.get(cwd)
        if hit and now - hit[0] < 45:
            return hit[1], hit[2], hit[3]
        branch = ""
        added = 0
        modified = 0
        try:
            res = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                text=True,
            )
            if res.returncode == 0:
                branch = res.stdout.strip()
                status = subprocess.run(
                    ["git", "-C", cwd, "status", "--porcelain"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=max(0.4, timeout),
                    text=True,
                )
                if status.returncode == 0:
                    for line in status.stdout.splitlines():
                        if not line:
                            continue
                        code = line[:2]
                        if "?" in code or "A" in code:
                            added += 1
                        else:
                            modified += 1
        except (OSError, subprocess.TimeoutExpired):
            pass
        self._cache[cwd] = (now, branch, added, modified)
        return branch, added, modified


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


# ---------------------------------------------------------------------------
# shared process utilities
# ---------------------------------------------------------------------------


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

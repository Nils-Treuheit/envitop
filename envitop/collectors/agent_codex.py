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
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from ..model import AgentSession, RateLimit
from .agent_common import (
    Tailer,
    parse_iso,
    redact,
)


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

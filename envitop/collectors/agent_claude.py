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

from ..model import AgentSession
from .agent_common import (
    Tailer,
    parse_iso,
    redact,
)


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

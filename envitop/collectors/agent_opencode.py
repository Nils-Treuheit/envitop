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
import time
from typing import Optional

from ..model import AgentSession
from .agent_common import (
    _opencode_message_tokens,
    find_opencode_db,
    redact,
)


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

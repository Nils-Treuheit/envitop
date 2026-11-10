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
import subprocess
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ..model import RateLimit

HOME = Path.home()
SUMMARIES_FILE = HOME / ".cache" / "abtop" / "summaries.json"
CODEX_CACHE = HOME / ".cache" / "abtop" / "codex-rate-limits.json"
OPENCODE_DBS = [
    HOME / ".local" / "share" / "opencode" / "opencode.db",
    HOME / ".local" / "share" / "opencode" / "db.sqlite",
]
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


def find_opencode_db() -> Optional[Path]:
    for path in OPENCODE_DBS:
        if path.exists():
            return path
    return None


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

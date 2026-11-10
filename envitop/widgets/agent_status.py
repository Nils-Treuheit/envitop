"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import time
from typing import Optional, Sequence

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    clip,
    get_palette,
    human_ago,
    human_duration,
    human_tokens,
    pct_bar,
    status_glyph,
    truncate_middle,
)
from ..model import (
    AgentSession,
    RateLimit,
)
from .common import (
    _join,
    _line,
)


class AgentPortsPanel(Widget):
    """Ports attributed to agent child processes in the selected session set."""

    DEFAULT_CSS = """
    AgentPortsPanel { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sessions: list[AgentSession] = []

    def update_data(self, sessions: Sequence[AgentSession]) -> None:
        self.sessions = list(sessions)
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        rows = [
            (session, child)
            for session in self.sessions
            for child in session.children
            if child.port is not None
        ]
        if not rows:
            return _join([Text("No agent child ports detected.", style=palette.dim)], w, h)
        port_width = 5
        project_width = max(8, w // 4)
        pid_width = 9
        gap = 2
        command_width = max(1, w - port_width - project_width - pid_width - gap * 3)
        lines = [
            _line(
                [
                    (f"{'PORT':>{port_width}}", palette.dim),
                    ("  ", ""),
                    (f"{'PROJECT':<{project_width}}", palette.dim),
                    ("  ", ""),
                    (f"{'PID':>{pid_width}}", palette.dim),
                    ("  ", ""),
                    (f"{'PROCESS':<{command_width}}", palette.dim),
                ],
                w,
            )
        ]
        for session, child in rows[: max(0, h - 1)]:
            lines.append(
                _line(
                    [
                        (f"{child.port:>{port_width}}", palette.cyan),
                        ("  ", ""),
                        (f"{clip(session.project, project_width):<{project_width}}", palette.text),
                        ("  ", ""),
                        (f"{child.pid:>{pid_width}}", palette.faint),
                        ("  ", ""),
                        (clip(child.command, command_width), palette.dim),
                    ],
                    w,
                )
            )
        hidden = len(rows) - min(len(rows), max(0, h - 1))
        if hidden and len(lines) < h:
            lines.append(Text(f"+ {hidden} more ports", style=palette.faint))
        return _join(lines, w, h)


class QuotaPanel(Widget):
    DEFAULT_CSS = """
    QuotaPanel { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.quotas: list[RateLimit] = []

    def update_data(self, quotas: list[RateLimit]) -> None:
        self.quotas = quotas
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        lines: list[Text] = []
        if not self.quotas:
            lines.append(Text("No quota data", style=palette.dim))
            lines.append(Text("abtop statusline hook not installed", style=palette.faint))
            return _join(lines, w, h)
        for quota in self.quotas:
            label = "Claude" if quota.source == "claude" else "Codex"
            for window, pct, reset in (
                ("5h", quota.five_pct, quota.five_reset),
                ("7d", quota.seven_pct, quota.seven_reset),
            ):
                if pct is None:
                    continue
                bar_w = max(5, min(18, w - 26))
                if w < 34:
                    bar_w = max(4, w - 20)
                reset_txt = ""
                if reset:
                    remaining = reset - time.time()
                    if remaining > 0:
                        reset_txt = f" {human_duration(remaining, short=True)}"
                    else:
                        reset_txt = " now"
                parts: list[tuple[str, str]] = [
                    (f"{label:<6} ", palette.accent),
                    (f"{window:>2} ", palette.dim),
                ]
                line = Text()
                for val, style in parts:
                    line.append(val, style=style)
                line.append_text(pct_bar(pct, bar_w, palette))
                line.append(f" {pct:3.0f}%", style=palette.dim)
                line.append(reset_txt, style=palette.faint)
                if time.time() - (quota.updated_at or 0) > 120:
                    line.append(" stale", style=palette.faint)
                line.truncate(w, overflow="crop")
                if line.cell_len < w:
                    line.append(" " * (w - line.cell_len))
                lines.append(line)
        return _join(lines, w, h)


class SessionDetail(Widget):
    DEFAULT_CSS = """
    SessionDetail { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session: Optional[AgentSession] = None

    def update_data(self, session: Optional[AgentSession]) -> None:
        self.session = session
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        s = self.session
        if not s:
            return _join(
                [Text("select a session with j/k", style=palette.dim)],
                w,
                h,
            )
        glyph, colour_name = status_glyph(s.status)
        colour = getattr(palette, colour_name, palette.dim)
        agent_colour = {
            "claude": palette.orange,
            "codex": palette.cyan,
            "opencode": palette.green,
        }.get(s.agent, palette.dim)
        lines: list[Text] = []
        lines.append(
            _line(
                [
                    (glyph + " ", colour),
                    (s.agent_label + " ", f"bold {agent_colour}"),
                    (clip(s.summary or s.project, w - 20), f"bold {palette.text}"),
                    (" ", ""),
                    (human_ago(s.updated_at), palette.faint),
                ],
                w,
            )
        )
        if h > 2:
            lines.append(
                _line(
                    [
                        (clip(truncate_middle(s.cwd or "-", max(8, w - 12)), w - 10), palette.dim),
                        (" ", ""),
                        (s.git_branch or "", palette.faint),
                    ],
                    w,
                )
            )
        if h > 3:
            model = s.model or "unknown model"
            lines.append(
                _line(
                    [
                        (f"PID {s.pid or '—'}", palette.faint),
                        (f"   MEM {s.mem_mb}M", palette.text),
                        (f"   TURNS {s.turn_count}", palette.dim),
                        (f"   SUBAGENTS {s.subagent_count}", palette.dim),
                        (f"   {clip(model, max(8, w // 3))}", palette.magenta),
                    ],
                    w,
                )
            )
        if h > 3:
            ctx = f"{s.context_pct:.0f}%" if s.context_pct is not None else "—"
            used = human_tokens(s.context_tokens)
            win = human_tokens(s.context_window) if s.context_window else "?"
            lines.append(
                _line(
                    [
                        ("ctx ", palette.dim),
                        (f"{ctx:>4}", palette.text),
                        (f" {used}/{win}  ", palette.faint),
                        ("in ", palette.dim),
                        (human_tokens(s.input_tokens), palette.text),
                        ("  out ", palette.dim),
                        (human_tokens(s.output_tokens), palette.text),
                        ("  cache ", palette.dim),
                        (human_tokens(s.cache_read), palette.text),
                    ],
                    w,
                )
            )
        if h > 4:
            task = s.current_task or s.model or "idle"
            lines.append(_line([("▸ ", palette.accent), (clip(task, w - 2), palette.cyan)], w))
        # Ports and children.
        if h > 5 and s.children:
            for child in s.children[: max(1, min(3, h - 6))]:
                port = f":{child.port}" if child.port else ""
                lines.append(
                    _line(
                        [
                            ("└ ", palette.faint),
                            (f"{child.pid:>6} ", palette.dim),
                            (clip(child.command, max(8, w - 20)), palette.dim),
                            (port, palette.green if child.port else palette.faint),
                        ],
                        w,
                    )
                )
        # Recent tool calls.
        if h > len(lines) + 1 and s.tools:
            for name, arg in s.tools[-2:]:
                lines.append(
                    _line(
                        [
                            ("⚙ ", palette.magenta),
                            (clip(name, 12), palette.magenta),
                            (" ", ""),
                            (clip(arg, max(8, w - 16)), palette.dim),
                        ],
                        w,
                    )
                )
        # Chat tail, if there is still room.
        if h > len(lines) and s.chat:
            for role, text in s.chat[-3:]:
                if len(lines) >= h:
                    break
                label = "you" if role == "you" else " ai"
                lines.append(
                    _line(
                        [
                            (label + " ", palette.accent if role == "you" else palette.green),
                            (clip(text, max(8, w - 5)), palette.text),
                        ],
                        w,
                    )
                )
        return _join(lines, w, h)

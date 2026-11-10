"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import time
from typing import Optional

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    braille_graph,
    clip,
    get_palette,
    human_ago,
    human_tokens,
    status_glyph,
)
from ..model import (
    AgentSession,
)
from .common import (
    _context_bar,
    _join,
    _line,
    _pad_text,
)


class AgentTable(Widget):
    DEFAULT_CSS = """
    AgentTable { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sessions: list[AgentSession] = []
        self.selected = 0
        self.token_rate = 0.0
        self.scope = "recent"

    def update_data(self, sessions: list[AgentSession], selected: int, token_rate: float, scope: str) -> None:
        self.sessions = sessions
        self.selected = selected
        self.token_rate = token_rate
        self.scope = scope
        self.refresh()

    def _summary(self, width: int) -> Text:
        palette = get_palette()
        total = len(self.sessions)
        active = sum(1 for s in self.sessions if s.is_active)
        tokens = sum(s.total_tokens for s in self.sessions)
        text = (
            f"{total} session{'s' if total != 1 else ''} · {active} active · "
            f"{human_tokens(tokens)} tokens · {human_tokens(self.token_rate)} tok/s"
        )
        if self.scope != "recent":
            text += " · all"
        return _line([(clip(text, width), palette.dim)], width)

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        lines: list[Text] = [self._summary(w)]
        if h <= 1:
            return _join(lines, w, h)
        if not self.sessions:
            lines.append(Text("No agent sessions in the last 30 min.", style=palette.dim))
            lines.append(Text("press a to include older sessions", style=palette.faint))
            return _join(lines, w, h)
        if w >= 116:
            return self._render_wide(w, h, palette, lines)

        show_header = h >= 3
        visible_h = max(0, h - 1 - int(show_header))
        selected = min(max(0, self.selected), len(self.sessions) - 1)
        start = 0
        if selected >= visible_h:
            start = selected - visible_h + 1
        if start > max(0, len(self.sessions) - visible_h):
            start = max(0, len(self.sessions) - visible_h)
        rows = self.sessions[start : start + visible_h]

        show_model = w >= 58
        prefix_w = 6
        pct_w, tokens_w, age_w = 4, 7, 7
        tail_w = 1 + pct_w + 1 + tokens_w + 1 + age_w
        if show_model:
            project_w = max(8, min(20, int(w * 0.22)))
            model_w = max(8, min(18, int(w * 0.18)))
        else:
            project_w = max(8, min(24, w - prefix_w - tail_w - 1 - 4))
            model_w = 0
        bar_w = max(1, w - prefix_w - project_w - 1 - (model_w + 1 if show_model else 0) - tail_w)
        if show_header:
            heading = Text(style=palette.faint)
            heading.append("  AG  ")
            heading.append(f"{'PROJECT':<{project_w}}")
            heading.append(" ")
            if show_model:
                heading.append(f"{'MODEL':<{model_w}}")
                heading.append(" ")
            heading.append(f"{clip('CONTEXT', bar_w):<{bar_w}}")
            heading.append(f" {'USE%':>{pct_w}} {'TOKENS':>{tokens_w}} {'AGE':>{age_w}}")
            lines.append(_pad_text(heading, w))
        for offset, session in enumerate(rows):
            idx = start + offset
            is_sel = idx == selected
            base = f"on {palette.selected_bg} {palette.selected_fg}" if is_sel else ""
            glyph, colour_name = status_glyph(session.status)
            colour = getattr(palette, colour_name, palette.dim)
            agent_colour = {
                "claude": palette.orange,
                "codex": palette.cyan,
                "opencode": palette.green,
            }.get(session.agent, palette.dim)
            model = session.model or "-"
            if session.effort:
                model = f"{model}·{session.effort}"
            age = human_ago(session.updated_at, time.time())
            tokens = human_tokens(session.total_tokens)
            row = Text(style=base)
            row.append(glyph, style=colour)
            row.append(" ")
            row.append(f"{session.agent_label:<3}", style=f"bold {agent_colour}")
            row.append(" ")
            project = clip(session.project or "?", project_w)
            row.append(f"{project:<{project_w}}", style=f"bold {palette.text}" if is_sel else palette.text)
            row.append(" ")
            if show_model:
                row.append(f"{clip(model, model_w):<{model_w}}", style=palette.magenta)
                row.append(" ")
            context_pct = session.context_pct
            if session.context_window > 0:
                context_pct = max(
                    0.0,
                    min(100.0, session.context_tokens / session.context_window * 100.0),
                )
            elif context_pct is not None:
                context_pct = max(0.0, min(100.0, context_pct))
            row.append_text(_context_bar(context_pct, bar_w, palette, selected=is_sel))
            pct_text = f"{context_pct:3.0f}%" if context_pct is not None else "  —"
            row.append(f" {pct_text:>{pct_w}}", style=palette.dim)
            row.append(f" {tokens:>{tokens_w}}", style=palette.dim)
            row.append(" ")
            row.append(f"{age:>{age_w}}", style=palette.faint if age != "now" else palette.green)
            if row.cell_len < w:
                row.append(" " * (w - row.cell_len))
            row.truncate(w, overflow="crop")
            lines.append(row)
        return _join(lines, w, h)

    def _render_wide(self, w: int, h: int, palette, lines: list[Text]) -> Text:
        columns = {
            "agent": 3,
            "pid": 9,
            "project": 12,
            "session": 8,
            "summary": 20,
            "status": 7,
            "model": 12,
            "pct": 4,
            "tokens": 7,
            "mem": 6,
            "turn": 4,
        }
        separators = 11
        fixed = sum(columns.values()) + separators
        context_width = max(5, w - fixed)
        headings = [
            (f"{'AI':<{columns['agent']}}", palette.dim),
            (" ", ""),
            (f"{'PID':>{columns['pid']}}", palette.dim),
            (" ", ""),
            (f"{'PROJECT':<{columns['project']}}", palette.dim),
            (" ", ""),
            (f"{'SESSION':<{columns['session']}}", palette.dim),
            (" ", ""),
            (f"{'SUMMARY':<{columns['summary']}}", palette.dim),
            (" ", ""),
            (f"{'STATE':<{columns['status']}}", palette.dim),
            (" ", ""),
            (f"{'MODEL':<{columns['model']}}", palette.dim),
            (" ", ""),
            (f"{'CONTEXT':<{context_width}}", palette.dim),
            (" ", ""),
            (f"{'USE%':>{columns['pct']}}", palette.dim),
            (" ", ""),
            (f"{'TOKENS':>{columns['tokens']}}", palette.dim),
            (" ", ""),
            (f"{'MEM':>{columns['mem']}}", palette.dim),
            (" ", ""),
            (f"{'TURN':>{columns['turn']}}", palette.dim),
        ]
        lines.append(_line(headings, w))
        visible = max(0, h - len(lines))
        selected = min(max(0, self.selected), len(self.sessions) - 1)
        start = max(0, selected - visible + 1)
        start = min(start, max(0, len(self.sessions) - visible))
        for offset, session in enumerate(self.sessions[start : start + visible]):
            idx = start + offset
            chosen = idx == selected
            base = f"on {palette.selected_bg} {palette.selected_fg}" if chosen else ""
            _, color_name = status_glyph(session.status)
            status_color = getattr(palette, color_name, palette.dim)
            status = {"thinking": "WORK", "executing": "WORK", "waiting": "WAIT"}.get(
                session.status, session.status[:7].upper()
            )
            context_pct = session.context_pct
            if session.context_window > 0:
                context_pct = session.context_tokens / session.context_window * 100.0
            if context_pct is not None:
                context_pct = max(0.0, min(100.0, context_pct))
            model = session.model or "—"
            if session.effort:
                model = f"{model}·{session.effort}"
            project = clip(session.project or "?", columns["project"])
            summary = session.summary or session.current_task or "—"
            pct = f"{context_pct:3.0f}%" if context_pct is not None else "—"
            mem = f"{session.mem_mb}M" if session.mem_mb else "—"
            agent_color = {"claude": palette.orange, "codex": palette.cyan, "opencode": palette.green}.get(
                session.agent, palette.dim
            )
            row = Text(style=base)
            for value, style in [
                (f"{session.agent_label:<{columns['agent']}}", f"bold {agent_color}"),
                (" ", ""),
                (f"{session.pid:>{columns['pid']}}", palette.faint),
                (" ", ""),
                (f"{project:<{columns['project']}}", palette.text),
                (" ", ""),
                (f"{session.session_id[: columns['session']]:<{columns['session']}}", palette.accent),
                (" ", ""),
                (f"{clip(summary, columns['summary']):<{columns['summary']}}", palette.text),
                (" ", ""),
                (f"{status:<{columns['status']}}", status_color),
                (" ", ""),
                (f"{clip(model, columns['model']):<{columns['model']}}", palette.magenta),
                (" ", ""),
            ]:
                row.append(value, style=style or None)
            bar = _context_bar(context_pct, context_width, palette, selected=chosen)
            row.append_text(bar)
            for value, style in [
                (" ", ""),
                (f"{pct:>{columns['pct']}}", palette.dim),
                (" ", ""),
                (f"{human_tokens(session.total_tokens):>{columns['tokens']}}", palette.text),
                (" ", ""),
                (f"{mem:>{columns['mem']}}", palette.dim),
                (" ", ""),
                (f"{session.turn_count:>{columns['turn']}}", palette.dim),
            ]:
                row.append(value, style=style or None)
            lines.append(_pad_text(row, w))
        return _join(lines, w, h)


class TokenOverview(Widget):
    """abtop-style token usage for the selected agent session."""

    DEFAULT_CSS = """
    TokenOverview { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session: Optional[AgentSession] = None
        self.token_rate = 0.0
        self.rate_history: list[float] = []

    def update_data(self, session: Optional[AgentSession], token_rate: float) -> None:
        self.session = session
        self.token_rate = token_rate
        self.rate_history.append(max(0.0, token_rate * 60))
        self.rate_history = self.rate_history[-120:]
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        session = self.session
        if session is None:
            return _join([Text("select an agent session to inspect tokens", style=palette.dim)], w, h)
        total = session.total_tokens
        lines = [
            _line(
                [
                    (f"Total: {human_tokens(total)}", f"bold {palette.text}"),
                    (f"    {human_tokens(self.token_rate * 60)}/min", f"bold {palette.green}"),
                ],
                w,
            )
        ]
        rows = (
            ("Input", session.input_tokens),
            ("Output", session.output_tokens),
            ("CacheR", session.cache_read),
            ("CacheW", session.cache_write),
        )
        value_labels = [human_tokens(value) for _, value in rows]
        value_w = max(1, *(len(value) for value in value_labels))
        bar_w = max(1, w - 10 - 1 - value_w)
        metric_rows = min(len(rows), max(0, h - 6)) if h >= 6 else 0
        for label, value in rows[:metric_rows]:
            row = Text()
            row.append(f"{label:<8}: ", style=palette.dim)
            relative = value / total * 100 if total else 0.0
            row.append_text(_context_bar(relative, bar_w, palette, fill_pct=relative * 0.88))
            row.append(f" {human_tokens(value):>{value_w}}", style=palette.text)
            lines.append(_pad_text(row, w))
        if h >= 6:
            lines.append(
                _line(
                    [
                        ("Token rate · ", palette.dim),
                        (f"{human_tokens(self.token_rate * 60)}/min", f"bold {palette.green}"),
                    ],
                    w,
                )
            )
            graph_rows = h - len(lines) - 2  # divider and footer
            rate_graph_h = (graph_rows + 1) // 2
            turn_graph_h = graph_rows - rate_graph_h
            rate_values = self.rate_history or [0.0]
            lines.extend(
                braille_graph(
                    rate_values,
                    w,
                    rate_graph_h,
                    palette,
                    maximum=max(rate_values) * 1.1 or 1.0,
                    color_by="value",
                )
            )
            lines.append(Text("─" * w, style=palette.faint))
            turn_values = session.token_history or [0]
            lines.extend(
                braille_graph(
                    turn_values,
                    w,
                    turn_graph_h,
                    palette,
                    maximum=max(turn_values) * 1.1 or 1.0,
                    color_by="value",
                )
            )
            avg = total / session.turn_count if session.turn_count else 0
            lines.append(
                _line(
                    [
                        ("Tokens/Turn · ", palette.dim),
                        (f"Turns: {session.turn_count}", palette.dim),
                        (f"    Avg: {human_tokens(avg)}/t", palette.dim),
                    ],
                    w,
                )
            )
        return _join(lines, w, h)

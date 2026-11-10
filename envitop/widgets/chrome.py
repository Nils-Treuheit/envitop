"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import time
from typing import Optional

from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget

from ..draw import (
    clip,
    get_palette,
    human_bytes,
    human_duration,
    human_tokens,
)
from ..model import (
    Snapshot,
)
from .common import (
    _line,
)


class Header(Widget):
    DEFAULT_CSS = """
    Header { height: 2; }
    """

    LOGO = (
        " ⣏⡉ ⡷⣸ ⡇⢸ ⡇ ⢹⠁ ⡎⢱ ⣏⡱",
        " ⠧⠤ ⠇⠹ ⠸⠃ ⠇ ⠸  ⠣⠜ ⠇",
    )

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.snapshot: Optional[Snapshot] = None
        self.view = "overview"

    def update_data(self, snapshot: Snapshot, view: str = "overview") -> None:
        self.snapshot = snapshot
        self.view = view
        self.refresh()

    def _logo(self) -> Text:
        palette = get_palette()
        text = Text()
        total = len(self.LOGO[0])
        stops = [palette.green, palette.cyan, palette.blue, palette.magenta]
        for row, line in enumerate(self.LOGO):
            if row:
                text.append("\n")
            for i, char in enumerate(line):
                p = (i / max(1, total - 1)) * (len(stops) - 1)
                lo = int(p)
                hi = min(len(stops) - 1, lo + 1)
                from ..draw import blend

                colour = blend(stops[lo], stops[hi], p - lo)
                text.append(char, style=f"bold {colour}")
        return text

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        s = self.snapshot
        if s is None:
            return Text("envitop", style=f"bold {palette.accent}")
        gpu_fields = self._gpu_summary(s)
        if w < 88:
            if self.view == "gpu":
                line = (
                    f"◆ envitop  {s.hostname} · up {human_duration(s.uptime, short=True)} · "
                    f"{s.task_count} Tasks · {s.thread_count} Threads"
                )
                return _line([(clip(line, w), palette.text)], w)
            line = (
                f"◆ envitop  {s.hostname} · up {human_duration(s.uptime, short=True)} · "
                f"CPU {s.cpu.percent:3.1f}% · RAM {s.mem.percent:3.1f}% · "
                f"{s.task_count} Tasks · {s.thread_count} Threads · {gpu_fields}"
            )
            line += f" · agents {sum(1 for a in s.agents if a.is_active)}/{len(s.agents)}"
            return _line([(clip(line, w), palette.text)], w)
        info1 = f"{s.hostname}  ·  {s.username}  ·  up {human_duration(s.uptime, short=True)}  ·  {s.os_name}"
        if self.view == "gpu":
            info2 = f"{s.task_count} Tasks  ·  {s.thread_count} Threads  ·  {time.strftime('%H:%M:%S')}"
        else:
            info2 = (
                f"CPU {s.cpu.percent:4.1f}%  ·  RAM {s.mem.percent:4.1f}%  ·  "
                f"{s.task_count} Tasks  ·  {s.thread_count} Threads  ·  {gpu_fields}  ·  "
                f"AGENTS {sum(1 for a in s.agents if a.is_active)}/{len(s.agents)}  ·  "
                f"{human_tokens(s.token_rate)} tok/s  ·  {time.strftime('%H:%M:%S')}"
            )
        logo = self._logo()
        lines = logo.plain.split("\n")
        out = Text()
        logow = max(cell_len(x) for x in lines) + 2
        info_start = max(0, (len(lines) - 2) // 2)
        for i, line in enumerate(lines):
            out.append_text(self._logo_line(i, lines))
            out.append(" " * max(0, logow - cell_len(line)))
            if i == info_start:
                out.append(clip(info1, max(0, w - logow)), style=palette.dim)
            elif i == info_start + 1:
                out.append(clip(info2, max(0, w - logow)), style=palette.text)
            if i < len(lines) - 1:
                out.append("\n")
        return out

    @staticmethod
    def _gpu_summary(snapshot: Snapshot) -> str:
        gpu_state = snapshot.gpu
        if not gpu_state.gpus:
            return "GPU n/a · VRAM n/a"
        if len(gpu_state.gpus) == 1:
            gpu = gpu_state.gpus[0]
            return f"GPU {gpu.util:3.1f}% · VRAM {gpu.mem_percent:3.1f}%"
        return (
            f"GPU cluster {len(gpu_state.gpus)} · avg {gpu_state.aggregate_util:3.1f}% · "
            f"VRAM {human_bytes(gpu_state.aggregate_mem_used)}/"
            f"{human_bytes(gpu_state.aggregate_mem_total)} "
            f"({gpu_state.aggregate_mem_percent:3.1f}%)"
        )

    def _logo_line(self, row: int, lines: list[str]) -> Text:
        palette = get_palette()
        from ..draw import blend

        stops = [palette.green, palette.cyan, palette.blue, palette.magenta]
        text = Text()
        total = max(1, len(lines[row]) - 1)
        for i, char in enumerate(lines[row]):
            p = (i / total) * (len(stops) - 1)
            lo = int(p)
            hi = min(len(stops) - 1, lo + 1)
            text.append(char, style=f"bold {blend(stops[lo], stops[hi], p - lo)}")
        return text


class Footer(Widget):
    DEFAULT_CSS = """
    Footer { height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.message = ""
        self.message_until = 0.0
        self.confirm = ""
        self.narrow = False
        self.paused = False
        self.view = "system"
        self.insight_summary = ""

    def set_state(
        self,
        *,
        message: str = "",
        confirm: str = "",
        narrow: bool = False,
        paused: bool = False,
        view: str = "system",
        insight_summary: str = "",
    ) -> None:
        self.message = message
        self.message_until = time.time() + 6 if message else 0
        self.confirm = confirm
        self.narrow = narrow
        self.paused = paused
        self.view = view
        self.insight_summary = insight_summary
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        view_names = [
            ("0", "Overview", "overview"),
            ("1", "CPU", "cpu"),
            ("2", "GPU", "gpu"),
            ("3", "Net", "net"),
            ("4", "Memory", "memory"),
            ("5", "Procs", "procs"),
            ("6", "Agents", "agents"),
        ]
        navigation = Text()
        for index, (key, name, view) in enumerate(view_names):
            if index:
                navigation.append("  ", style=palette.faint)
            style = f"bold {palette.accent}" if self.view == view else palette.dim
            navigation.append(f"{key} {name}", style=style)

        left = Text()
        if self.confirm:
            left.append(clip(self.confirm, w), style=f"bold {palette.red}")
        elif self.message and time.time() < self.message_until:
            left.append(clip(self.message, w), style=f"bold {palette.yellow}")
        else:
            if self.paused:
                left.append("PAUSED  ", style=f"bold {palette.yellow}")
            if self.narrow:
                left.append("Tab focus  j/k move  x kill", style=palette.dim)
                if self.view == "procs":
                    left.append("  f search  / column regex", style=palette.dim)
            else:
                keys = [
                    ("q", f"bold {palette.accent}"),
                    (" quit  ", palette.dim),
                    ("?", f"bold {palette.accent}"),
                    (" help  ", palette.dim),
                    ("j/k", f"bold {palette.accent}"),
                    (" move  ", palette.dim),
                    ("Tab", f"bold {palette.accent}"),
                    (" focus  ", palette.dim),
                    ("x", f"bold {palette.accent}"),
                    (" kill  ", palette.dim),
                    ("a", f"bold {palette.accent}"),
                    (" scope  ", palette.dim),
                    ("t", f"bold {palette.accent}"),
                    (" theme  ", palette.dim),
                    ("p", f"bold {palette.accent}"),
                    (" pause  ", palette.dim),
                ]
                if self.view == "procs":
                    keys.extend(
                        [
                            ("f", f"bold {palette.accent}"),
                            (" search  ", palette.dim),
                            ("/", f"bold {palette.accent}"),
                            (" column regex  ", palette.dim),
                        ]
                    )
                for value, style in keys:
                    left.append(value, style=style)
            if self.insight_summary:
                left.append("  " + clip(self.insight_summary, 26), style=palette.yellow)

        if w < 64:
            navigation = Text()
            compact_views = [
                ("0", "O", "overview"),
                ("1", "C", "cpu"),
                ("2", "G", "gpu"),
                ("3", "N", "net"),
                ("4", "M", "memory"),
                ("5", "P", "procs"),
                ("6", "A", "agents"),
            ]
            for index, (key, label, view) in enumerate(compact_views):
                if index:
                    navigation.append(" ", style=palette.faint)
                style = f"bold {palette.accent}" if self.view == view else palette.dim
                navigation.append(f"{key}{label}", style=style)
        nav_width = navigation.cell_len
        available_left = max(0, w - nav_width - 1)
        if left.cell_len > available_left:
            left.truncate(available_left, overflow="ellipsis")
        out = Text()
        out.append_text(left)
        out.append(" " * max(0, w - left.cell_len - nav_width))
        out.append_text(navigation)
        if out.cell_len > w:
            out.truncate(w, overflow="crop")
        return out

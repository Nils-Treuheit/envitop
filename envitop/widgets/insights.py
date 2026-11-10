"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import time
from typing import Sequence

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    braille_graph,
    get_palette,
    human_bytes,
    human_rate,
)
from ..insights import Insight
from ..model import (
    Snapshot,
)
from .common import (
    _join,
    _line,
)


class TimelinePanel(Widget):
    """Readable summary of the bounded snapshot buffer and scrub position."""

    DEFAULT_CSS = """
    TimelinePanel { height: 1fr; min-height: 4; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.snapshots: list[Snapshot] = []
        self.selected = 0

    def update_data(self, snapshots: Sequence[Snapshot], selected: int) -> None:
        self.snapshots = list(snapshots)
        self.selected = min(max(0, selected), max(0, len(self.snapshots) - 1))
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        if not self.snapshots:
            return _join([Text("Collecting timeline samples…", style=palette.dim)], w, h)
        snap = self.snapshots[self.selected]
        live = self.selected == len(self.snapshots) - 1
        when = time.strftime("%H:%M:%S", time.localtime(snap.ts)) if snap.ts else "unknown"
        span = max(0.0, self.snapshots[-1].ts - self.snapshots[0].ts)
        chart_h = max(2, min(8, h // 6))
        lines = [
            _line(
                [
                    ("LIVE" if live else "SCRUBBED", palette.green if live else palette.yellow),
                    (f"  sample {self.selected + 1}/{len(self.snapshots)}  {when}  ", palette.text),
                    (f"window {span:.0f}s", palette.dim),
                ],
                w,
            ),
            _line(
                [
                    ("CPU ", palette.dim),
                    (f"{snap.cpu.percent:.1f}%", palette.text),
                    ("   RAM ", palette.dim),
                    (f"{snap.mem.percent:.1f}%", palette.text),
                    ("   load ", palette.dim),
                    (f"{snap.cpu.load1:.2f}", palette.text),
                ],
                w,
            ),
        ]
        lines.append(Text("CPU HISTORY · whole-machine utilization", style=palette.accent))
        lines.extend(
            braille_graph(
                [item.cpu.percent for item in self.snapshots],
                w,
                chart_h,
                palette,
                maximum=100,
                color_by="value",
            )
        )
        lines.append(Text("MEMORY HISTORY · RAM used", style=palette.magenta))
        lines.extend(
            braille_graph(
                [item.mem.percent for item in self.snapshots],
                w,
                chart_h,
                palette,
                maximum=100,
                color_by="value",
            )
        )
        if snap.gpu.gpus:
            gpu = snap.gpu.gpus[0]
            lines.append(
                _line(
                    [
                        ("SELECTED GPU ", palette.dim),
                        (f"GPU{gpu.index} {gpu.util:.0f}%", palette.text),
                        ("   VRAM ", palette.dim),
                        (f"{human_bytes(gpu.mem_used)}/{human_bytes(gpu.mem_total)}", palette.text),
                    ],
                    w,
                )
            )
        lines.append(Text("SAMPLE LOG · ← older  → newer  Home oldest  End live", style=palette.faint))
        rows = max(0, h - len(lines))
        start = max(0, self.selected - rows // 2)
        end = min(len(self.snapshots), start + rows)
        start = max(0, end - rows)
        for index in range(start, end):
            item = self.snapshots[index]
            selected = index == self.selected
            ts = time.strftime("%H:%M:%S", time.localtime(item.ts)) if item.ts else "--:--:--"
            glyph = "▶" if selected else " "
            line = _line(
                [
                    (glyph + " ", palette.accent if selected else palette.faint),
                    (f"{ts}  CPU {item.cpu.percent:5.1f}%  RAM {item.mem.percent:5.1f}%  ", palette.text),
                    (f"RX {human_rate(item.net.rx_rate)}  TX {human_rate(item.net.tx_rate)}", palette.dim),
                ],
                w,
            )
            lines.append(line)
        return _join(lines, w, h)


class InsightsPanel(Widget):
    """Plain-language diagnostic cards with their measurement and tab."""

    DEFAULT_CSS = """
    InsightsPanel { height: 1fr; min-height: 4; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.insights: list[Insight] = []

    def update_data(self, insights: Sequence[Insight]) -> None:
        self.insights = list(insights)
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        if not self.insights:
            return _join(
                [
                    Text("No current conditions crossed the insight thresholds.", style=palette.green),
                    Text("Checks use only measurements available in this snapshot.", style=palette.dim),
                ],
                w,
                h,
            )
        lines: list[Text] = []
        for item in self.insights:
            color = {"high": palette.red, "warn": palette.yellow, "info": palette.cyan}.get(
                item.severity, palette.dim
            )
            lines.append(_line([("● ", color), (item.title, f"bold {palette.text}")], w))
            lines.append(
                _line(
                    [
                        ("  ", palette.faint),
                        (item.evidence, palette.dim),
                        ("   → ", palette.faint),
                        (item.tab, palette.accent),
                    ],
                    w,
                )
            )
            lines.append(Text(""))
        return _join(lines, w, h)

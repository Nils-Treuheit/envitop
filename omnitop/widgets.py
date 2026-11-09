"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import time
from typing import Iterable, Optional, Sequence

from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget

from .draw import (
    braille_graph,
    braille_line_graph,
    clip,
    get_palette,
    human_ago,
    human_bytes,
    human_duration,
    human_rate,
    human_tokens,
    pct_bar,
    smooth_series,
    sparkline,
    status_glyph,
    tiny_bar,
    truncate_middle,
)
from .model import AgentSession, DiskInfo, GpuState, ProcInfo, RateLimit, Snapshot


def _line(parts: Sequence[tuple[str, str]], width: int, base: str = "") -> Text:
    text = Text(style=base)
    for value, style in parts:
        if value:
            text.append(value, style=style or None)
    plain_width = cell_len(text.plain)
    if plain_width < width:
        text.append(" " * (width - plain_width))
    text.truncate(width, overflow="crop")
    return text


def _join(lines: Sequence[Text], width: int, height: int) -> Text:
    out = Text()
    for i in range(height):
        if i:
            out.append("\n")
        if i < len(lines):
            out.append_text(lines[i])
        else:
            out.append(" " * width)
    return out


def _pad_text(text: Text, width: int) -> Text:
    if text.cell_len < width:
        text.append(" " * (width - text.cell_len))
    text.truncate(width, overflow="crop")
    return text


def _bar_text(pct: Optional[float], width: int, palette, selected: bool = False) -> Text:
    style = "on " + palette.selected_bg if selected else ""
    if pct is None:
        return Text("—" * width, style=palette.faint)
    return pct_bar(pct, width, palette, style=style)


class OneLine(Widget):
    """A single-line text area that never wraps or overflows its panel."""

    DEFAULT_CSS = """
    OneLine { height: 1; }
    """

    def __init__(self, text: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.content = text

    def update_text(self, content) -> None:
        self.content = content
        self.refresh()

    def render(self) -> Text:
        w = max(1, self.size.width)
        if isinstance(self.content, Text):
            text = self.content.copy()
            if text.cell_len < w:
                text.append(" " * (w - text.cell_len))
            text.truncate(w, overflow="crop")
            return text
        return _line([(clip(str(self.content), w), get_palette().text)], w)


class HistoryGraph(Widget):
    """Braille history graph with filled-area and connected-trace styles.

    When a secondary series is supplied (and the widget is tall enough) the
    graph is split in two: the primary series on top and the secondary one
    below.  This is used for network RX/TX and disk read/write so both
    directions stay visible at a glance.
    """

    DEFAULT_CSS = """
    HistoryGraph { height: 1fr; min-height: 1; }
    """

    def __init__(
        self,
        maximum: Optional[float] = None,
        color_mode: str = "value",
        primary_role: str = "accent",
        secondary_role: str = "",
        graph_style: str = "filled",
        primary_label: str = "",
        secondary_label: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.values: list[float] = []
        self.secondary: list[float] = []
        self.maximum = maximum
        self.secondary_maximum: Optional[float] = None
        self.color_mode = color_mode
        self.primary_role = primary_role
        self.secondary_role = secondary_role
        self.graph_style = graph_style
        self.primary_label = primary_label
        self.secondary_label = secondary_label

    def update_data(
        self,
        values: Iterable[float],
        maximum: Optional[float] = None,
        secondary: Optional[Iterable[float]] = None,
        secondary_maximum: Optional[float] = None,
    ) -> None:
        self.values = smooth_series(list(values))
        if secondary is not None:
            self.secondary = smooth_series(list(secondary))
        if maximum is not None:
            self.maximum = maximum
        self.secondary_maximum = secondary_maximum
        self.refresh()

    def _combined_max(self) -> Optional[float]:
        if self.maximum is not None:
            return self.maximum
        values = list(self.values) + list(self.secondary)
        if not values:
            return None
        return max(values) * 1.1 or 1.0

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        if self.secondary and h >= 2:
            maximum = self._combined_max()
            show_labels = h >= 4 and (self.primary_label or self.secondary_label)
            graph_height = h - (2 if show_labels else 0)
            top_h = max(1, graph_height // 2)
            bottom_h = max(1, graph_height - top_h)
            primary_colour = getattr(palette, self.primary_role, palette.accent)
            secondary_colour = getattr(palette, self.secondary_role, palette.magenta)
            graph_fn = braille_line_graph if self.graph_style == "line" else braille_graph
            lines: list[Text] = []
            if show_labels and self.primary_label:
                lines.append(Text(self.primary_label, style=primary_colour))
            lines.extend(graph_fn(
                self.values,
                w,
                top_h,
                palette,
                maximum=maximum,
                color_by=self.color_mode,
                solid_color=primary_colour if self.color_mode != "value" else None,
            ))
            if show_labels and self.secondary_label:
                lines.append(Text(self.secondary_label, style=secondary_colour))
            lines += graph_fn(
                self.secondary,
                w,
                bottom_h,
                palette,
                maximum=self.secondary_maximum or maximum,
                color_by=self.color_mode,
                solid_color=secondary_colour if self.color_mode != "value" else None,
            )
            return _join(lines, w, h)
        graph_fn = braille_line_graph if self.graph_style == "line" else braille_graph
        lines = graph_fn(self.values, w, h, palette, maximum=self.maximum, color_by=self.color_mode)
        return _join(lines, w, h)


class CoreBars(Widget):
    """Compact per-core utilisation grid."""

    DEFAULT_CSS = """
    CoreBars {
        height: 1fr;
        min-height: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.cores: list[float] = []
        self.temperatures: list[Optional[float]] = []
        self.temp_source = ""

    def update_data(
        self,
        cores: Iterable[float],
        temperatures: Optional[Iterable[Optional[float]]] = None,
        temp_source: str = "",
    ) -> None:
        self.cores = list(cores)
        self.temperatures = list(temperatures or [])
        self.temp_source = temp_source
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        cores = self.cores
        if not cores:
            return Text("waiting for samples…", style=palette.dim)
        cell = 14
        cols = max(1, w // cell)
        rows_needed = (len(cores) + cols - 1) // cols
        temp_label = self.temp_source.upper() if self.temperatures and self.temp_source else "TEMP"
        header = Text()
        for _ in range(cols):
            header.append(f"{'CORE % ' + temp_label:<{cell}}", style=palette.dim)
        lines: list[Text] = [header]
        for row in range(min(max(0, h - 1), rows_needed)):
            line = Text()
            for col in range(cols):
                idx = row * cols + col
                if idx >= len(cores):
                    line.append(" " * cell)
                    continue
                pct = cores[idx]
                temp = self.temperatures[idx] if idx < len(self.temperatures) else None
                temp_text = f" {temp:.0f}°" if temp is not None else ""
                line.append(f"C{idx:<2} ", style=palette.dim)
                line.append(f"{pct:3.0f}%", style=f"bold {palette.gradient(pct)}")
                line.append(temp_text, style=palette.orange if temp is not None else palette.dim)
                padding = cell - 4 - len(f"{pct:3.0f}%") - len(temp_text)
                if padding > 0:
                    line.append(" " * padding)
            lines.append(_pad_text(line, w))
        return _join(lines, w, h)


class GpuPanel(Widget):
    DEFAULT_CSS = """
    GpuPanel { height: 1fr; min-height: 3; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = GpuState()

    def update_data(self, state: GpuState) -> None:
        self.state = state
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        state = self.state
        lines: list[Text] = []
        if not state.available or not state.gpus:
            lines.append(Text("No NVIDIA GPU detected", style=f"bold {palette.yellow}"))
            if state.error:
                lines.append(Text(clip(state.error, w), style=palette.dim))
            lines.append(Text("Install/verify nvidia-smi for GPU telemetry.", style=palette.faint))
            return _join(lines, w, h)

        n = len(state.gpus)
        band = max(4, h // n) if n else h
        for gi, gpu in enumerate(state.gpus):
            this_band = band if gi < n - 1 else max(2, h - band * (n - 1))
            stats_lines = 2 if this_band >= 5 else 1
            show_graph_labels = this_band >= 8
            graph_h = max(1, this_band - stats_lines - (2 if show_graph_labels else 0))
            util_colour = palette.gradient(gpu.util)
            title = Text()
            title.append(f"GPU{gpu.index} ", style=f"bold {palette.accent}")
            name_w = max(8, min(w // 3, 22))
            title.append(
                clip(gpu.name.replace("NVIDIA ", ""), name_w),
                style=f"bold {palette.text}",
            )
            title.append("  ")
            title.append(f"{gpu.util:3.0f}%", style=f"bold {util_colour}")

            def add(part: str, style: str) -> None:
                if title.cell_len + cell_len(part) <= w:
                    title.append(part, style=style)

            if gpu.temp_c is not None:
                add(f"  {gpu.temp_c:.0f}°C", palette.text)
            if gpu.power_w is not None:
                pwr = f"{gpu.power_w:.0f}W"
                if gpu.power_limit_w:
                    pwr += f"/{gpu.power_limit_w:.0f}W"
                add(f"  {pwr}", palette.dim)
            if gpu.fan_pct is not None and gpu.fan_pct > 0:
                add(f"  FAN {gpu.fan_pct:.0f}%", palette.dim)
            if gpu.clock_sm:
                add(f"  {gpu.clock_sm:.0f}MHz", palette.dim)
            if gpu.pstate:
                add(f"  {gpu.pstate}", palette.faint)
            lines.append(_pad_text(title, w))

            if stats_lines == 2:
                vram = f"{human_bytes(gpu.mem_used)}/{human_bytes(gpu.mem_total)}"
                second: list[tuple[str, str]] = [
                    ("VRAM ", palette.dim),
                ]
                second_text = tiny_bar(gpu.mem_percent, max(4, min(14, w // 4)), palette)
                # Compose the bar as a separate Text so styles survive.
                prefix = _line(second, 5)
                bar = second_text
                suffix = _line(
                    [
                        (f" {gpu.mem_percent:3.0f}%  {vram}", palette.dim),
                    ],
                    max(0, w - 5 - bar.cell_len),
                )
                row = Text()
                row.append_text(prefix)
                row.append_text(bar)
                row.append_text(suffix)
                row.truncate(w, overflow="crop")
                lines.append(row)

            # Match nvitop's paired utilization and memory history regions.
            perf_h = max(1, graph_h // 2)
            mem_h = max(1, graph_h - perf_h)
            if show_graph_labels:
                lines.append(Text("GPU UTIL", style=palette.dim))
            graph = braille_graph(
                smooth_series(gpu.history),
                w,
                perf_h,
                palette,
                maximum=100,
                color_by="value",
            )
            lines.extend(graph)
            if show_graph_labels:
                lines.append(Text("VRAM", style=palette.dim))
            lines.extend(
                braille_graph(
                    smooth_series(gpu.mem_history),
                    w,
                    mem_h,
                    palette,
                    maximum=100,
                    color_by="value",
                )
            )
            if gi < n - 1:
                # Replace the last line with a separator if the band is tiny.
                if this_band <= 2:
                    lines.append(Text("─" * w, style=palette.faint))
        return _join(lines, w, h)


class DiskPanel(Widget):
    """Per-partition capacity and read/write activity histories."""

    DEFAULT_CSS = """
    DiskPanel { height: 1fr; min-height: 2; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.disk = DiskInfo()

    def update_data(self, disk: DiskInfo) -> None:
        self.disk = disk
        self.refresh()

    @staticmethod
    def _shared_io_graph(read_values, write_values, width: int, scale: float) -> Text:
        """Draw recent reads and writes side by side on one shared scale."""
        width = max(1, width)
        count = max(1, width // 2)
        glyphs = "▁▂▃▄▅▆▇█"

        def sample(values):
            values = list(values)[-60:]
            if len(values) > count:
                length = len(values)
                values = [
                    max(values[int(i * length / count): max(int(i * length / count) + 1, int((i + 1) * length / count))])
                    for i in range(count)
                ]
            if len(values) < count:
                values = [0.0] * (count - len(values)) + values
            return values

        reads = sample(read_values)
        writes = sample(write_values)
        graph = Text()
        for read_value, write_value in zip(reads, writes):
            read_level = min(7, round(max(0.0, read_value) / scale * 7))
            write_level = min(7, round(max(0.0, write_value) / scale * 7))
            graph.append(" " if read_value <= 0 else glyphs[read_level], style=get_palette().yellow)
            graph.append(" " if write_value <= 0 else glyphs[write_level], style=get_palette().magenta)
        if graph.cell_len < width:
            graph.append(" " * (width - graph.cell_len))
        graph.truncate(width, overflow="crop")
        return graph

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        disk = self.disk
        compact = w < 104
        if compact:
            summary_parts = [
                ("R ", palette.yellow),
                (human_rate(disk.read_rate), palette.text),
                ("  W ", palette.magenta),
                (human_rate(disk.write_rate), palette.text),
                (f"  {len(disk.mounts)} partitions", palette.dim),
                (" · shared scale", palette.faint),
            ]
        else:
            summary_parts = [
                ("R ", palette.yellow),
                (human_rate(disk.read_rate), palette.text),
                ("  W ", palette.magenta),
                (human_rate(disk.write_rate), palette.text),
                (f"  {len(disk.mounts)} partitions", palette.dim),
                (" · peak is last 60s", palette.faint),
            ]
        lines = [
            _line(
                summary_parts,
                w,
            )
        ]
        if not disk.mounts:
            lines.append(Text("No mounted partitions found", style=palette.dim))
        mount_w = max(10, min(16, max((len(m.mount) for m in disk.mounts), default=9)))
        capacity_w = max(
            13,
            min(18, max((len(f"{human_bytes(m.used)}/{human_bytes(m.total)}") for m in disk.mounts), default=10)),
        )
        free_w = max(8, min(10, max((len(human_bytes(m.free)) for m in disk.mounts), default=4)))
        if compact:
            mount_w = max(8, min(12, max((len(m.mount) for m in disk.mounts), default=8)))
            capacity_w = max(11, min(15, max((len(f"{human_bytes(m.used)}/{human_bytes(m.total)}") for m in disk.mounts), default=11)))
            free_w = max(6, min(9, max((len(human_bytes(m.free)) for m in disk.mounts), default=4)))
            rate_w = min(10, max(6, (w - mount_w - capacity_w - free_w - 12) // 2))
            graph_w = max(1, w - mount_w - capacity_w - free_w - 4 - rate_w * 2)
            header = Text()
            header.append(f"{'PARTITION':<{mount_w}} ", style=palette.dim)
            header.append(f"{'USED/TOTAL':<{capacity_w}} ", style=palette.dim)
            header.append(f"{'FREE':<{free_w}} ", style=palette.dim)
            header.append(f"{'READ/WRITE':<{graph_w}} ", style=palette.accent)
            header.append(f"{'READ/s':>{rate_w}} ", style=palette.yellow)
            header.append(f"{'WRITE/s':>{rate_w}}", style=palette.magenta)
            lines.append(_pad_text(header, w))
            scale = max(
                (
                    max(m.read_rate, m.write_rate, *(m.read_history[-60:] or [0.0]), *(m.write_history[-60:] or [0.0]))
                    for m in disk.mounts
                ),
                default=0.0,
            ) * 1.05 or 1.0
            for mount in disk.mounts[: max(0, h - 2)]:
                row = Text()
                row.append(f"{clip(mount.mount, mount_w - 1):<{mount_w}} ", style=f"bold {palette.text}")
                usage = f"{human_bytes(mount.used)}/{human_bytes(mount.total)}"
                row.append(f"{usage:<{capacity_w}} ", style=palette.text)
                row.append(f"{human_bytes(mount.free):<{free_w}} ", style=palette.faint)
                row.append_text(
                    self._shared_io_graph(mount.read_history[-60:], mount.write_history[-60:], graph_w, scale)
                )
                row.append(" ")
                row.append(f"{human_rate(mount.read_rate):>{rate_w}}", style=palette.yellow)
                row.append(f"{human_rate(mount.write_rate):>{rate_w}}", style=palette.magenta)
                lines.append(_pad_text(row, w))
            return _join(lines, w, h)
        rate_w = max(16, (w - mount_w - capacity_w - free_w - 1) // 2)
        # Give the history traces most of each rate column while preserving
        # enough room for the aligned current/peak figures.
        spark_w = max(5, min(14, rate_w - 11))
        header = Text()
        header.append(f"{'PARTITION':<{mount_w}}", style=palette.dim)
        header.append(f"{'USED/TOTAL':<{capacity_w}}", style=palette.dim)
        header.append(f"{'FREE':<{free_w}}", style=palette.dim)
        header.append(f"{'READ/s NOW/PEAK':<{rate_w}}", style=palette.yellow)
        header.append(" ")
        header.append("WRITE/s NOW/PEAK", style=palette.magenta)
        lines.append(_pad_text(header, w))
        for mount in disk.mounts[: max(0, h - 2)]:
            row = Text()
            row.append(f"{clip(mount.mount, mount_w - 1):<{mount_w}}", style=f"bold {palette.text}")
            usage = f"{human_bytes(mount.used)}/{human_bytes(mount.total)}"
            row.append(f"{usage:<{capacity_w}}", style=palette.text)
            row.append(f"{human_bytes(mount.free):<{free_w}}", style=palette.faint)
            read_history = mount.read_history[-60:] or [0.0]
            write_history = mount.write_history[-60:] or [0.0]
            read_peak = max(read_history, default=0.0)
            write_peak = max(write_history, default=0.0)
            read = f"{human_bytes(mount.read_rate)}/{human_bytes(read_peak)}"
            write = f"{human_bytes(mount.write_rate)}/{human_bytes(write_peak)}"
            row.append(sparkline(smooth_series(read_history), spark_w), style=palette.yellow)
            row.append(f"{read:>{rate_w - spark_w}}", style=palette.text)
            row.append(" ")
            row.append(sparkline(smooth_series(write_history), spark_w), style=palette.magenta)
            row.append(f"{write:>{rate_w - spark_w}}", style=palette.text)
            lines.append(_pad_text(row, w))
        return _join(lines, w, h)


class GpuProcTable(Widget):
    DEFAULT_CSS = """
    GpuProcTable { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.processes: list = []
        self.selected = 0

    def update_data(self, processes, selected: int) -> None:
        next_processes = list(processes)
        if self.processes == next_processes and self.selected == selected:
            return
        self.processes = next_processes
        self.selected = selected
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        if h <= 1:
            return Text("GPU processes", style=palette.dim)
        lines: list[Text] = []
        header = [
            ("PID", palette.dim), ("  G", palette.dim), (" T", palette.dim),
            ("  SM%", palette.dim), (" MEM%", palette.dim), ("    VRAM", palette.dim),
            ("  PROCESS", palette.dim),
        ]
        lines.append(_line(header, w))
        visible = max(1, h - 1)
        selected = min(max(0, self.selected), max(0, len(self.processes) - 1))
        start = max(0, selected - visible + 1)
        if start > max(0, len(self.processes) - visible):
            start = max(0, len(self.processes) - visible)
        rows = self.processes[start : start + visible]
        for offset, proc in enumerate(rows):
            i = start + offset
            selected_row = i == selected
            base = f"on {palette.selected_bg} {palette.selected_fg}" if selected_row else ""
            sm = "  -" if proc.sm is None else f"{proc.sm:3.0f}"
            mem = "  -" if proc.mem_pct is None else f"{proc.mem_pct:3.0f}"
            name_w = max(6, w - 7 - 3 - 2 - 5 - 5 - 8 - 2)
            parts = [
                (f"{proc.pid:>6}", palette.dim if not selected_row else ""),
                (f" {proc.gpu_index:>2}", palette.accent if not selected_row else ""),
                (f" {proc.kind:>1}", palette.magenta if proc.kind == "G" else palette.green),
                (f" {sm:>4}", palette.gradient(proc.sm or 0) if proc.sm else palette.faint),
                (f" {mem:>4}", palette.gradient(proc.mem_pct or 0) if proc.mem_pct else palette.faint),
                (f" {human_bytes(proc.vram):>7}", palette.text),
                ("  ", ""),
                (clip(proc.name, name_w), palette.text),
            ]
            lines.append(_line(parts, w, base))
        return _join(lines, w, h)


class ProcTable(Widget):
    DEFAULT_CSS = """
    ProcTable { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.processes: list[ProcInfo] = []
        self.selected = 0
        self.sort_mode = "cpu"

    def update_data(self, processes: list[ProcInfo], selected: int, sort_mode: str = "cpu") -> None:
        self.processes = processes
        self.selected = selected
        self.sort_mode = sort_mode
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        if h <= 1:
            return Text("processes", style=palette.dim)
        lines: list[Text] = []
        lines.append(
            _line(
                [
                    ("PID", palette.dim),
                    (" USER", palette.dim),
                    ("  CPU%", palette.dim),
                    (" MEM%", palette.dim),
                    ("    RSS", palette.dim),
                    ("  COMMAND", palette.dim),
                ],
                w,
            )
        )
        visible = max(1, h - 1)
        selected = min(max(0, self.selected), max(0, len(self.processes) - 1))
        start = max(0, selected - visible + 1)
        if start > max(0, len(self.processes) - visible):
            start = max(0, len(self.processes) - visible)
        rows = self.processes[start : start + visible]
        for offset, proc in enumerate(rows):
            i = start + offset
            selected_row = i == selected
            base = f"on {palette.selected_bg} {palette.selected_fg}" if selected_row else ""
            cpu_colour = palette.gradient(min(100, proc.cpu))
            mem_colour = palette.gradient(min(100, proc.mem_pct * 2))
            cmd_w = max(8, w - 7 - 9 - 6 - 6 - 8 - 2)
            parts = [
                (f"{proc.pid:>6}", palette.dim),
                (f" {clip(proc.user, 8):<8}", palette.text),
                (f" {proc.cpu:>5.1f}", cpu_colour),
                (f" {proc.mem_pct:>5.1f}", mem_colour),
                (f" {human_bytes(proc.rss):>7}", palette.dim),
                ("  ", ""),
                (clip(proc.cmd or proc.name, cmd_w), palette.text if selected_row else palette.dim),
            ]
            lines.append(_line(parts, w, base))
        return _join(lines, w, h)


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

        visible_h = h - 1
        selected = min(max(0, self.selected), len(self.sessions) - 1)
        start = 0
        if selected >= visible_h:
            start = selected - visible_h + 1
        if start > max(0, len(self.sessions) - visible_h):
            start = max(0, len(self.sessions) - visible_h)
        rows = self.sessions[start : start + visible_h]

        show_model = w >= 58
        show_ctx = w >= 44
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
            # Reserve: glyph(1) agent(3) spaces + age(7) tokens(7) ctx(12)
            fixed = 1 + 1 + 3 + 1 + 8 + 7 + (12 if show_ctx else 0)
            project_w = max(8, int((w - fixed) * 0.48)) if show_model else max(8, w - fixed)
            model_w = max(6, w - fixed - project_w - 1) if show_model else 0
            row = Text(style=base)
            row.append(glyph, style=colour)
            row.append(" ")
            row.append(session.agent_label, style=f"bold {agent_colour}")
            row.append(" ")
            row.append(clip(session.project or "?", project_w), style=f"bold {palette.text}" if is_sel else palette.text)
            row.append(" ")
            if show_model:
                row.append(clip(model, model_w), style=palette.magenta)
                row.append(" ")
            if show_ctx:
                if session.context_pct is not None:
                    row.append("ctx ", style=palette.dim)
                    row.append_text(tiny_bar(session.context_pct, 5, palette, style=base))
                    row.append(f" {session.context_pct:3.0f}% ", style=palette.dim)
                else:
                    row.append("ctx   —   ", style=palette.faint)
            row.append(f"{tokens:>6}", style=palette.dim)
            row.append(" ")
            row.append(f"{age:>7}", style=palette.faint if age != "now" else palette.green)
            if row.cell_len < w:
                row.append(" " * (w - row.cell_len))
            row.truncate(w, overflow="crop")
            lines.append(row)
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

    def update_data(self, session: Optional[AgentSession], token_rate: float) -> None:
        self.session = session
        self.token_rate = token_rate
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        session = self.session
        if session is None:
            return _join([Text("select an agent session to inspect tokens", style=palette.dim)], w, h)
        total = session.total_tokens
        lines = [_line([
            (f"Total: {human_tokens(total)}", f"bold {palette.text}"),
            (f"    {human_tokens(self.token_rate * 60)}/min", f"bold {palette.green}"),
        ], w)]
        rows = (
            ("Input", session.input_tokens),
            ("Output", session.output_tokens),
            ("CacheR", session.cache_read),
            ("CacheW", session.cache_write),
        )
        bar_w = max(4, min(20, w - 20))
        for label, value in rows:
            if len(lines) >= h:
                break
            row = Text()
            row.append(f"{label:<8}: ", style=palette.dim)
            pct = value / total * 100 if total else 0.0
            row.append_text(pct_bar(pct, bar_w, palette))
            row.append(f" {human_tokens(value)}", style=palette.text)
            lines.append(_pad_text(row, w))
        if h >= 7 and len(lines) < h:
            values = session.token_history or [0]
            graph = braille_graph(
                values,
                max(1, w - 14),
                2,
                palette,
                maximum=max(values) * 1.1 or 1.0,
                color_by="value",
            )
            trend = Text("tokens/turn ", style=palette.dim)
            trend.append_text(graph[0])
            lines.append(_pad_text(trend, w))
            lines.append(_pad_text(graph[1], w))
        if len(lines) < h:
            avg = total / session.turn_count if session.turn_count else 0
            lines.append(_line([
                (f"Turns: {session.turn_count}", palette.dim),
                (f"   Avg: {human_tokens(avg)}/t", palette.dim),
            ], w))
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
                    ]
                , w)
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


class Header(Widget):
    DEFAULT_CSS = """
    Header { height: 2; }
    """

    LOGO = (
        " █▀█ █▀▄▀█ █▄ █ █ ▀█▀ █▀█ █▀█",
        " █▄█ █░▀░█ █░▀█ █ ░█░ █▄█ █▀▀",
    )

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.snapshot: Optional[Snapshot] = None

    def update_data(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
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
                from .draw import blend

                colour = blend(stops[lo], stops[hi], p - lo)
                text.append(char, style=f"bold {colour}")
        return text

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        s = self.snapshot
        if s is None:
            return Text("omnitop", style=f"bold {palette.accent}")
        if w < 88:
            line = (
                f"◆ omnitop  {s.hostname} · up {human_duration(s.uptime, short=True)} · "
                f"cpu {s.cpu.percent:.0f}% · mem {s.mem.percent:.0f}%"
            )
            if s.gpu.gpus:
                line += f" · gpu {s.gpu.gpus[0].util:.0f}%"
            line += f" · agents {sum(1 for a in s.agents if a.is_active)}/{len(s.agents)}"
            return _line([(clip(line, w), palette.text)], w)
        info1 = (
            f"{s.hostname}  ·  {s.username}  ·  up {human_duration(s.uptime, short=True)}  ·  "
            f"{s.os_name}"
        )
        gpu = f"GPU {s.gpu.gpus[0].util:.0f}%" if s.gpu.gpus else "GPU n/a"
        info2 = (
            f"CPU {s.cpu.percent:4.1f}%  ·  MEM {s.mem.percent:4.1f}%  ·  {gpu}  ·  "
            f"AGENTS {sum(1 for a in s.agents if a.is_active)}/{len(s.agents)}  ·  "
            f"{human_tokens(s.token_rate)} tok/s  ·  {time.strftime('%H:%M:%S')}"
        )
        logo = self._logo()
        lines = logo.plain.split("\n")
        out = Text()
        logow = max(cell_len(x) for x in lines) + 2
        for i, line in enumerate(lines):
            out.append_text(self._logo_line(i, lines))
            out.append(" " * max(0, logow - cell_len(line)))
            info = info1 if i == 0 else info2
            out.append(clip(info, max(0, w - logow)), style=palette.dim if i == 0 else palette.text)
            if i == 0:
                out.append("\n")
        return out

    def _logo_line(self, row: int, lines: list[str]) -> Text:
        palette = get_palette()
        from .draw import blend

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

    def set_state(self, *, message: str = "", confirm: str = "", narrow: bool = False, paused: bool = False, view: str = "system") -> None:
        self.message = message
        self.message_until = time.time() + 6 if message else 0
        self.confirm = confirm
        self.narrow = narrow
        self.paused = paused
        self.view = view
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        if self.confirm:
            return _line([(clip(self.confirm, w), f"bold {palette.red}")], w)
        if self.message and time.time() < self.message_until:
            return _line([(clip(self.message, w), f"bold {palette.yellow}")], w)
        keys: list[tuple[str, str]] = []
        if self.narrow:
            keys.append(("1/2/3", f"bold {palette.accent}"))
            keys.append((" views  ", palette.dim))
        keys.extend(
            [
                ("q", f"bold {palette.accent}"),
                (" quit  ", palette.dim),
                ("?", f"bold {palette.accent}"),
                (" help  ", palette.dim),
                ("j/k", f"bold {palette.accent}"),
                (" select  ", palette.dim),
                ("tab", f"bold {palette.accent}"),
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
        )
        if self.paused:
            keys.insert(0, ("PAUSED  ", f"bold {palette.yellow}"))
        return _line(keys, w)

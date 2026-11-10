"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

import shlex
import time
from typing import Iterable, Optional, Sequence

from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget

from .draw import (
    blend,
    braille_graph,
    braille_line_graph,
    braille_stacked_graph,
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
from .insights import Insight
from .model import (
    AgentSession,
    DiskInfo,
    GpuProcess,
    GpuState,
    HostedSiteInfo,
    MountInfo,
    NetInfo,
    NetListenerInfo,
    ProcInfo,
    RateLimit,
    Snapshot,
)


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


def _context_bar(
    pct: Optional[float],
    width: int,
    palette,
    *,
    selected: bool = False,
    fill_pct: Optional[float] = None,
) -> Text:
    """Render a full-width meter with a green-to-red usage color."""
    width = max(0, width)
    text = Text()
    background = f"on {palette.selected_bg}" if selected else ""
    if not width:
        return text
    if pct is None:
        text.append("·" * width, style=f"{background} {palette.faint}".strip())
        return text
    pct = max(0.0, min(100.0, pct))
    stops = ("#5ee08a", "#a3e635", "#f5d76e", "#ffa657", "#ff6b6b")
    position = pct / 100 * (len(stops) - 1)
    index = min(len(stops) - 2, int(position))
    color = blend(stops[index], stops[index + 1], position - index)
    filled = max(0.0, min(100.0, pct if fill_pct is None else fill_pct)) / 100 * width
    full = min(width, int(filled))
    text.append("█" * full, style=f"{background} {color}".strip())
    remainder = width - full
    fraction = int((filled - full) * 8)
    partial = " ▏▎▍▌▋▊▉"
    if remainder and fraction:
        text.append(partial[fraction], style=f"{background} {color}".strip())
        remainder -= 1
    if remainder:
        text.append("░" * remainder, style=f"{background} {palette.faint}".strip())
    return text


def _process_usage_color(pct: Optional[float], palette) -> str:
    """Use green below 30%, warning colors from 30–49%, and red at 50%."""
    if pct is None:
        return palette.faint
    pct = max(0.0, min(100.0, pct))
    if pct >= 50:
        return palette.red
    if pct >= 30:
        return blend(palette.yellow, palette.orange, (pct - 30) / 20)
    return palette.green


def _gpu_memory(value: int) -> str:
    """Format framebuffer memory with nvitop-like precision."""
    gib = 1024**3
    mib = 1024**2
    if value >= gib:
        return f"{value / gib:.2f}GiB"
    if value >= mib:
        return f"{value / mib:.1f}MiB"
    return f"{value / 1024:.1f}KiB" if value >= 1024 else f"{value}B"


def _llama_model(command: str) -> str:
    try:
        args = shlex.split(command)
    except ValueError:
        args = command.split()
    model = ""
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            model = args[index + 1]
            break
        if arg.startswith("--model="):
            model = arg.partition("=")[2]
            break
    if not model:
        return "?"
    model = model.removeprefix("hf://")
    marker = "huggingface.co/"
    if marker in model:
        model = model.split(marker, 1)[1]
        components = model.split("/")
        if len(components) >= 2:
            model = "/".join(components[:2])
    for component in model.replace("\\", "/").split("/"):
        if component.startswith("models--"):
            repo = component.removeprefix("models--")
            organization, separator, name = repo.partition("--")
            if separator and organization and name:
                return f"{organization}/{name}"
    model = model.rstrip("/")
    if "/" in model and (model.startswith("/") or model.startswith(".")):
        model = model.rsplit("/", 1)[-1]
    for extension in (".gguf", ".safetensors", ".bin"):
        if model.endswith(extension):
            model = model[: -len(extension)]
            break
    return model or "?"


def _gpu_process_details(proc, active_projects: Sequence[str]) -> str:
    command = proc.cmd or proc.name
    lower = f"{proc.name} {command}".lower()
    if "llama-server" in lower or "llama_server" in lower:
        return f"M:{_llama_model(command)}"
    ai_app = any(
        name in lower
        for name in ("chatgpt", "claude", "codex", "opencode", "aider", "cursor", "copilot", "gemini")
    )
    if not ai_app:
        return "-"
    projects = list(dict.fromkeys(project.strip() for project in active_projects if project.strip()))
    return "P:[" + ",".join(projects) + "]" if projects else "-"


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
        secondary_invert: bool = False,
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
        self.secondary_invert = secondary_invert

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
            show_labels = h >= 5 and (self.primary_label or self.secondary_label)
            show_divider = h >= 3
            label_rows = int(show_labels and bool(self.primary_label)) + int(
                show_labels and bool(self.secondary_label)
            )
            graph_height = h - label_rows - int(show_divider)
            top_h = max(1, graph_height // 2)
            bottom_h = max(1, graph_height - top_h)
            primary_colour = getattr(palette, self.primary_role, palette.accent)
            secondary_colour = getattr(palette, self.secondary_role, palette.magenta)
            graph_fn = braille_line_graph if self.graph_style == "line" else braille_graph
            lines: list[Text] = []
            if show_labels and self.primary_label:
                lines.append(Text(self.primary_label, style=primary_colour))
            lines.extend(
                graph_fn(
                    self.values,
                    w,
                    top_h,
                    palette,
                    maximum=maximum,
                    color_by=self.color_mode,
                    solid_color=primary_colour if self.color_mode != "value" else None,
                )
            )
            if show_divider:
                lines.append(Text("─" * w, style=palette.faint))
            lines.extend(
                graph_fn(
                    self.secondary,
                    w,
                    bottom_h,
                    palette,
                    maximum=self.secondary_maximum or maximum,
                    color_by=self.color_mode,
                    solid_color=secondary_colour if self.color_mode != "value" else None,
                    invert=self.secondary_invert,
                )
            )
            if show_labels and self.secondary_label:
                lines.append(Text(self.secondary_label, style=secondary_colour))
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
        self.histories: list[list[float]] = []
        self.temp_source = ""
        self.detailed = False

    def update_data(
        self,
        cores: Iterable[float],
        temperatures: Optional[Iterable[Optional[float]]] = None,
        temp_source: str = "",
        histories: Optional[Iterable[Iterable[float]]] = None,
        detailed: bool = False,
    ) -> None:
        self.cores = list(cores)
        self.temperatures = list(temperatures or [])
        self.temp_source = temp_source
        self.histories = [list(history) for history in histories or []]
        self.detailed = detailed
        self.styles.height = len(self.cores) + 1 if detailed and self.cores else "1fr"
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        cores = self.cores
        if not cores:
            return Text("waiting for samples…", style=palette.dim)
        if self.detailed:
            core_width, temp_width, value_width = 6, 8, 7
            bar_width = max(1, w - core_width - temp_width - value_width - 8)
            header = Text()
            header.append(f"{'CORE':<{core_width}}  {'TEMP':>{temp_width}}  ", style=f"bold {palette.dim}")
            header.append("UTILIZATION".ljust(bar_width), style=f"bold {palette.dim}")
            header.append(f"  {'CPU%':>{value_width}}", style=f"bold {palette.dim}")
            header.append("  ")
            lines = [_pad_text(header, w)]
            for idx, pct in enumerate(cores):
                temp = self.temperatures[idx] if idx < len(self.temperatures) else None
                temp_text = f"{temp:.0f}°C" if temp is not None else "—"
                line = Text()
                line.append(f"C{idx:02d}".ljust(core_width), style=f"bold {palette.text}")
                line.append("  ")
                line.append(
                    f"{temp_text:>{temp_width}}",
                    style=f"bold {palette.orange if temp is not None else palette.dim}",
                )
                line.append("  ")
                line.append_text(_context_bar(pct, bar_width, palette))
                line.append("  ")
                line.append(f"{pct:6.1f}%", style=f"bold {palette.gradient(pct)}")
                line.append("  ")
                lines.append(_pad_text(line, w))
            return _join(lines, w, h)
        cell = 14
        cols = max(1, w // cell)
        rows_needed = (len(cores) + cols - 1) // cols
        temp_label = "CCD" if self.temp_source.upper() == "CCD" else "TMP"
        header = Text()
        for _ in range(cols):
            header.append(f"{'CORE':<4} {'%':>4} {temp_label:<3} ", style=palette.dim)
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
                temp_text = f"{temp:.0f}°" if temp is not None else ""
                line.append(f"C{idx:<3}", style=palette.dim)
                line.append(" ")
                line.append(f"{pct:3.0f}%", style=f"bold {palette.gradient(pct)}")
                line.append(" ")
                line.append(temp_text, style=palette.orange if temp is not None else palette.dim)
                padding = cell - 4 - 1 - 4 - 1 - len(temp_text)
                if padding > 0:
                    line.append(" " * padding)
            lines.append(_pad_text(line, w))
        return _join(lines, w, h)


class CpuTopProcesses(Widget):
    """Top host processes by whole-machine CPU capacity."""

    DEFAULT_CSS = """
    CpuTopProcesses { height: 1fr; min-height: 3; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.processes: list[ProcInfo] = []
        self._source_processes: list[ProcInfo] = []
        self.sort_column: Optional[str] = "cpu"
        self.sort_direction = -1
        self._default_sort = True
        self.row_offset = 0

    def update_data(self, processes: Sequence[ProcInfo]) -> None:
        self._source_processes = list(processes)
        self._sort_processes()
        self.row_offset = min(
            self.row_offset,
            max(0, len(self.processes) - max(1, self.size.height - 1)),
        )
        self.refresh()

    def scroll_by(self, amount: int) -> None:
        visible = max(1, self.size.height - 1)
        maximum = max(0, len(self.processes) - visible)
        next_offset = min(max(0, self.row_offset + amount), maximum)
        if next_offset != self.row_offset:
            self.row_offset = next_offset
            self.refresh()

    def _sort_processes(self) -> None:
        if self.sort_column is None or self.sort_direction == 0:
            self.processes = list(self._source_processes)
            return
        key_functions = {
            "pid": lambda item: item.pid,
            "cpu": lambda item: (item.cpu, item.mem_pct),
            "mem": lambda item: (item.mem_pct, item.cpu),
            "process": lambda item: item.name.casefold(),
            "command": lambda item: (item.cmd or item.name).casefold(),
        }
        self.processes = sorted(
            self._source_processes,
            key=key_functions[self.sort_column],
            reverse=self.sort_direction < 0,
        )

    def _column_specs(self, width: int) -> list[tuple[str, str, int, str]]:
        pid_width, cpu_width, ram_width, process_width, command_width, _gap = self._column_widths(width)
        return [
            ("pid", "PID", pid_width, "left"),
            ("cpu", "CPU%", cpu_width, "right"),
            ("mem", "RAM%", ram_width, "right"),
            ("process", "PROCESS", process_width, "left"),
            ("command", "COMMAND", command_width, "left"),
        ]

    def on_click(self, event) -> None:
        if event.y != 0:
            return
        _pid, _cpu, _ram, _process, _command, gap = self._column_widths(max(1, self.size.width))
        x = 0
        for column, _label, width, _align in self._column_specs(max(1, self.size.width)):
            if x <= event.x < x + width:
                if self.sort_column != column:
                    self.sort_column = column
                    self.sort_direction = 1
                    self._default_sort = False
                elif self._default_sort:
                    self.sort_direction = 1
                    self._default_sort = False
                elif self.sort_direction < 0:
                    self.sort_direction = 1
                elif self.sort_direction > 0:
                    self.sort_direction = -1
                else:
                    self.sort_column = None
                    self.sort_direction = 0
                self.row_offset = 0
                self._sort_processes()
                self.refresh()
                event.stop()
                return
            x += width + gap

    @staticmethod
    def _column_widths(width: int) -> tuple[int, int, int, int, int, int]:
        gap = 2
        pid_width = min(11, max(6, width // 20 + 3))
        cpu_width = min(8, max(5, width // 24))
        ram_width = cpu_width
        process_width = min(24, max(10, width // 6))
        command_width = max(1, width - pid_width - cpu_width - ram_width - process_width - gap * 4)
        return pid_width, cpu_width, ram_width, process_width, command_width, gap

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        pid_width, cpu_width, ram_width, process_width, command_width, gap = self._column_widths(w)
        columns = self._column_specs(w)
        header = Text()
        for index, (column, label, column_width, align) in enumerate(columns):
            if index:
                header.append(" " * gap)
            selected = self.sort_column == column
            if selected and column_width >= len(label) + 2:
                arrow = "↓" if self.sort_direction < 0 else "↑"
                label = f"{label} {arrow}"
            value = clip(label, column_width)
            header.append(
                f"{value:<{column_width}}" if align == "left" else f"{value:>{column_width}}",
                style=f"bold {palette.accent if selected else palette.dim}",
            )
        lines = [_pad_text(header, w)]
        visible = max(0, h - 1)
        start = min(self.row_offset, max(0, len(self.processes) - visible))
        for proc in self.processes[start : start + visible]:
            fields = [
                (str(proc.pid), pid_width, "left", palette.faint),
                (f"{proc.cpu:.1f}", cpu_width, "right", f"bold {_process_usage_color(proc.cpu, palette)}"),
                (f"{proc.mem_pct:.1f}", ram_width, "right", _process_usage_color(proc.mem_pct, palette)),
                (proc.name, process_width, "left", palette.text),
                (proc.cmd or proc.name, command_width, "left", palette.dim),
            ]
            line = Text()
            for index, (value, column_width, align, style) in enumerate(fields):
                if index:
                    line.append(" " * gap)
                value = clip(value, column_width)
                line.append(
                    f"{value:<{column_width}}" if align == "left" else f"{value:>{column_width}}", style=style
                )
            lines.append(_pad_text(line, w))
        if not self.processes:
            lines.append(_pad_text(Text("No process samples available.", style=palette.dim), w))
        return _join(lines, w, h)


class GpuPanel(Widget):
    DEFAULT_CSS = """
    GpuPanel { height: 1fr; min-height: 3; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = GpuState()
        self.focused = False

    def update_data(self, state: GpuState, focused: Optional[bool] = None) -> None:
        if focused is not None:
            self.focused = focused
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

        if len(state.gpus) > 1:
            if self.focused:
                return self._render_focused_cluster(state, w, h, palette)
            return self._render_cluster(state, w, h, palette)

        if self.focused:
            gpu = state.gpus[0]
            chart_height = max(1, (h - 3) // 2)
            remainder = max(1, h - chart_height - 3)
            lines = [Text("GPU UTILIZATION", style=palette.dim)]
            lines.extend(
                braille_graph(
                    smooth_series(gpu.history or [gpu.util]),
                    w,
                    chart_height,
                    palette,
                    maximum=100,
                    color_by="value",
                )
            )
            lines.append(Text("VRAM USAGE", style=palette.dim))
            lines.extend(
                braille_graph(
                    smooth_series(gpu.mem_history or [gpu.mem_percent]),
                    w,
                    remainder,
                    palette,
                    maximum=100,
                    color_by="value",
                )
            )
            return _join(lines, w, h)

        n = len(state.gpus)
        band = max(4, h // n) if n else h
        for gi, gpu in enumerate(state.gpus):
            this_band = band if gi < n - 1 else max(2, h - band * (n - 1))
            stats_lines = 2 if this_band >= 5 else 1
            show_graph_labels = this_band >= 8
            show_divider = this_band - stats_lines - (2 if show_graph_labels else 0) >= 3
            graph_h = max(
                1,
                this_band - stats_lines - (2 if show_graph_labels else 0) - int(show_divider),
            )
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

            if state.driver or state.cuda:
                version = "  Driver " + (state.driver or "—")
                if state.cuda:
                    version += " | CUDA " + state.cuda
                add(version, palette.dim)
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
                prefix = _line([("VRAM ", palette.dim)], 5)
                suffix_text = f" {gpu.mem_percent:3.0f}%  {vram}"
                suffix_width = min(cell_len(suffix_text), max(0, w - prefix.cell_len))
                bar_width = max(0, w - prefix.cell_len - suffix_width)
                bar = tiny_bar(gpu.mem_percent, max(1, bar_width), palette)
                suffix = _line([(suffix_text, palette.dim)], suffix_width)
                row = Text()
                row.append_text(prefix)
                if bar_width:
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
            if show_divider:
                lines.append(Text("─" * w, style=palette.faint))
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
            if show_graph_labels:
                lines.append(Text("VRAM", style=palette.dim))
            if gi < n - 1:
                # Replace the last line with a separator if the band is tiny.
                if this_band <= 2:
                    lines.append(Text("─" * w, style=palette.faint))
        return _join(lines, w, h)

    @staticmethod
    def _render_cluster(state: GpuState, width: int, height: int, palette) -> Text:
        """Pool multi-GPU capacity while retaining per-device color identity."""
        gpus = sorted(state.gpus, key=lambda gpu: gpu.index)
        count = len(gpus)
        colors = (
            palette.green,
            palette.cyan,
            palette.yellow,
            palette.magenta,
            palette.blue,
            palette.orange,
            palette.red,
        )
        mem_total = state.aggregate_mem_total
        power = state.aggregate_power_w
        power_limit = state.aggregate_power_limit_w
        hottest = state.hottest_temp_c
        summary = Text()
        summary.append(f"GPU CLUSTER · {count} devices", style=f"bold {palette.accent}")
        summary.append(
            f"   avg util {state.aggregate_util:.0f}%", style=f"bold {palette.gradient(state.aggregate_util)}"
        )
        if power is not None:
            limit = f"/{power_limit:.0f}W" if power_limit is not None else ""
            summary.append(f"   power {power:.0f}W{limit}", style=palette.orange)
        summary = _pad_text(summary, width)
        capacity = Text()
        capacity.append(
            f"VRAM {human_bytes(state.aggregate_mem_used)}/{human_bytes(mem_total)} "
            f"({state.aggregate_mem_percent:.1f}%)",
            style=palette.text,
        )
        if hottest is not None:
            capacity.append(f"   max temp {hottest:.0f}°C", style=palette.orange)
        versions = "   ".join(
            part
            for part in (
                f"Driver {state.driver}" if state.driver else "",
                f"CUDA {state.cuda}" if state.cuda else "",
            )
            if part
        )
        if versions and capacity.cell_len + len(versions) + 3 < width:
            capacity.append(f"   {versions}", style=palette.dim)
        capacity = _pad_text(capacity, width)

        utilization_layers: list[list[float]] = []
        memory_layers: list[list[float]] = []
        for gpu in gpus:
            util_history = gpu.history or [gpu.util]
            utilization_layers.append([value / count for value in util_history])
            mem_history = gpu.mem_history or [gpu.mem_percent]
            weight = (gpu.mem_total / mem_total) if mem_total else 0.0
            memory_layers.append([value * weight for value in mem_history])

        hidden = max(0, count - max(0, height - 8))
        device_rows = count - hidden
        chart_height = max(1, (height - 4 - device_rows - int(hidden > 0)) // 2)
        lines: list[Text] = [summary, capacity]
        lines.append(Text("GPU UTIL · per-device contributions to cluster average", style=palette.dim))
        lines.extend(
            braille_stacked_graph(
                utilization_layers,
                width,
                chart_height,
                palette,
                colors=colors,
                maximum=100,
            )
        )
        lines.append(Text("VRAM · used memory pooled against total device capacity", style=palette.dim))
        lines.extend(
            braille_stacked_graph(
                memory_layers,
                width,
                chart_height,
                palette,
                colors=colors,
                maximum=100,
            )
        )
        for gpu, color in zip(gpus[:device_rows], (colors[i % len(colors)] for i in range(count))):
            name = clip(gpu.name.replace("NVIDIA ", ""), max(8, min(22, width // 4)))
            details = Text()
            details.append(f"GPU{gpu.index} ", style=f"bold {color}")
            details.append(name, style=palette.text)
            details.append(f"   {gpu.util:.0f}%", style=f"bold {palette.gradient(gpu.util)}")
            details.append(
                f"   VRAM {human_bytes(gpu.mem_used)}/{human_bytes(gpu.mem_total)}", style=palette.dim
            )
            if gpu.temp_c is not None:
                details.append(f"   {gpu.temp_c:.0f}°C", style=palette.orange)
            if gpu.power_w is not None:
                power_limit_text = f"/{gpu.power_limit_w:.0f}W" if gpu.power_limit_w is not None else ""
                details.append(f"   {gpu.power_w:.0f}W{power_limit_text}", style=palette.dim)
            if gpu.fan_pct is not None and gpu.fan_pct > 0:
                details.append(f"   FAN {gpu.fan_pct:.0f}%", style=palette.faint)
            if gpu.clock_sm is not None:
                details.append(f"   {gpu.clock_sm:.0f}MHz", style=palette.dim)
            if gpu.pstate:
                details.append(f"   {gpu.pstate}", style=palette.faint)
            lines.append(_pad_text(details, width))
        if hidden:
            lines.append(
                Text(
                    f"+ {hidden} more device(s); switch to GPU for the full device list", style=palette.faint
                )
            )
        return _join(lines, width, height)

    @staticmethod
    def _render_focused_cluster(state: GpuState, width: int, height: int, palette) -> Text:
        gpus = sorted(state.gpus, key=lambda gpu: gpu.index)
        chart_height = max(1, (height - 3) // 2)
        remainder = max(1, height - chart_height - 3)
        colors = (
            palette.green,
            palette.cyan,
            palette.yellow,
            palette.magenta,
            palette.blue,
            palette.orange,
            palette.red,
        )
        count = len(gpus)
        util_layers = [[value / count for value in (gpu.history or [gpu.util])] for gpu in gpus]
        mem_total = state.aggregate_mem_total
        mem_layers = [
            [
                value * (gpu.mem_total / mem_total) if mem_total else 0.0
                for value in (gpu.mem_history or [gpu.mem_percent])
            ]
            for gpu in gpus
        ]
        lines = [Text("GPU UTILIZATION · CLUSTER MEAN", style=palette.dim)]
        lines.extend(
            braille_stacked_graph(
                util_layers,
                width,
                chart_height,
                palette,
                colors=colors,
                maximum=100,
            )
        )
        lines.append(Text("VRAM USAGE · POOLED CAPACITY", style=palette.dim))
        lines.extend(
            braille_stacked_graph(
                mem_layers,
                width,
                remainder,
                palette,
                colors=colors,
                maximum=100,
            )
        )
        return _join(lines, width, height)


class GpuStatsPanel(Widget):
    """Detailed right-hand GPU telemetry for the focused GPU view."""

    DEFAULT_CSS = """
    GpuStatsPanel { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = GpuState()

    def update_data(self, state: GpuState) -> None:
        if self.state == state:
            return
        self.state = state
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        state = self.state
        if not state.available or not state.gpus:
            return _join(
                [
                    Text("GPU telemetry unavailable", style=f"bold {palette.yellow}"),
                    Text(clip(state.error, w), style=palette.dim) if state.error else Text(""),
                ],
                w,
                h,
            )

        lines: list[Text] = []
        metric_label_width = len("MEM BANDWIDTH   ")
        metric_value_width = max(7, min(12, w // 5))
        meter_width = max(1, w - metric_label_width - metric_value_width - 2)

        def meter_row(label: str, value: Optional[float], formatted: str, color: str) -> Text:
            row = Text()
            row.append(f"{label:<{metric_label_width}}", style=f"bold {palette.text}")
            row.append_text(_context_bar(value, meter_width, palette))
            row.append("  ")
            row.append(f"{formatted:>{metric_value_width}}", style=f"bold {color}")
            return _pad_text(row, w)

        if len(state.gpus) > 1:
            lines.append(Text(f"CLUSTER · {len(state.gpus)} GPUs", style=f"bold {palette.accent}"))
            lines.append(
                meter_row(
                    "AVG UTIL",
                    state.aggregate_util,
                    f"{state.aggregate_util:.1f}%",
                    palette.gradient(state.aggregate_util),
                )
            )
            lines.append(
                meter_row(
                    "POOLED VRAM",
                    state.aggregate_mem_percent,
                    f"{state.aggregate_mem_percent:.1f}%",
                    palette.cyan,
                )
            )
            lines.append(
                _line(
                    [
                        ("USED ", palette.dim),
                        (human_bytes(state.aggregate_mem_used), f"bold {palette.text}"),
                        ("   TOTAL ", palette.dim),
                        (human_bytes(state.aggregate_mem_total), f"bold {palette.text}"),
                    ],
                    w,
                )
            )
            if state.aggregate_mem_util is not None:
                lines.append(
                    meter_row(
                        "AVG MBW",
                        state.aggregate_mem_util,
                        f"{state.aggregate_mem_util:.1f}%",
                        palette.cyan,
                    )
                )
            if state.aggregate_power_w is not None:
                limit = (
                    f" / {state.aggregate_power_limit_w:.0f}W"
                    if state.aggregate_power_limit_w is not None
                    else ""
                )
                lines.append(
                    _line(
                        [("POWER ", palette.dim), (f"{state.aggregate_power_w:.0f}W{limit}", palette.orange)],
                        w,
                    )
                )
            if state.hottest_temp_c is not None:
                lines.append(
                    _line([("MAX TEMP ", palette.dim), (f"{state.hottest_temp_c:.0f}°C", palette.orange)], w)
                )

        for gpu in sorted(state.gpus, key=lambda item: item.index):
            if len(state.gpus) > 1:
                lines.append(Text(""))
            name = clip(gpu.name.replace("NVIDIA ", ""), max(1, w - 14))
            lines.append(Text(f"GPU {gpu.index}  ·  {name}", style=f"bold {palette.accent}"))
            lines.append(
                meter_row(
                    "UTILIZATION",
                    gpu.util,
                    f"{gpu.util:.1f}%",
                    palette.gradient(gpu.util),
                )
            )
            lines.append(
                meter_row(
                    "MEM BANDWIDTH",
                    gpu.mem_util,
                    f"{gpu.mem_util:.1f}%" if gpu.mem_util is not None else "—",
                    palette.cyan,
                )
            )
            lines.append(meter_row("VRAM", gpu.mem_percent, f"{gpu.mem_percent:.1f}%", palette.cyan))
            lines.append(
                _line(
                    [
                        ("USED ", palette.dim),
                        (human_bytes(gpu.mem_used), f"bold {palette.text}"),
                        ("   TOTAL ", palette.dim),
                        (human_bytes(gpu.mem_total), f"bold {palette.text}"),
                    ],
                    w,
                )
            )
            environment: list[tuple[str, str]] = []
            if gpu.temp_c is not None:
                environment.append((f"{gpu.temp_c:.0f}°C", palette.orange))
            if gpu.power_w is not None:
                limit = f"/{gpu.power_limit_w:.0f}W" if gpu.power_limit_w is not None else ""
                environment.append((f"{gpu.power_w:.0f}W{limit}", palette.orange))
            if gpu.fan_pct is not None:
                environment.append((f"FAN {gpu.fan_pct:.0f}%", palette.dim))
            if environment:
                parts: list[tuple[str, str]] = [(environment[0][0], environment[0][1])]
                for value, style in environment[1:]:
                    parts.extend([("   ", palette.dim), (value, style)])
                lines.append(_line(parts, w))
            clocks = []
            if gpu.clock_sm is not None:
                clocks.append((f"SM {gpu.clock_sm:.0f}MHz", palette.dim))
            if gpu.clock_mem is not None:
                clocks.append((f"MEM {gpu.clock_mem:.0f}MHz", palette.dim))
            if gpu.pstate:
                clocks.append((gpu.pstate, palette.faint))
            if clocks:
                parts = []
                for value, style in clocks:
                    if parts:
                        parts.append(("   ", palette.dim))
                    parts.append((value, style))
                lines.append(_line(parts, w))
            if gpu.encoder is not None or gpu.decoder is not None:
                engines = []
                if gpu.encoder is not None:
                    engines.append((f"ENC {gpu.encoder:.0f}%", palette.dim))
                if gpu.decoder is not None:
                    engines.append((f"DEC {gpu.decoder:.0f}%", palette.dim))
                parts = []
                for value, style in engines:
                    if parts:
                        parts.append(("   ", palette.dim))
                    parts.append((value, style))
                lines.append(_line(parts, w))
        if state.driver or state.cuda:
            lines.append(Text(""))
            versions = "  ".join(
                part
                for part in (
                    f"Driver {state.driver}" if state.driver else "",
                    f"CUDA {state.cuda}" if state.cuda else "",
                )
                if part
            )
            lines.append(Text(clip(versions, w), style=palette.faint))
        return _join(lines, w, h)


class DiskPanel(Widget):
    """Per-partition capacity and read/write activity histories."""

    DEFAULT_CSS = """
    DiskPanel { height: 1fr; min-height: 2; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.disk = DiskInfo()
        self.detailed = False

    def update_data(self, disk: DiskInfo, detailed: bool = False) -> None:
        self.disk = disk
        self.detailed = detailed
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
                    max(
                        values[
                            int(i * length / count) : max(
                                int(i * length / count) + 1, int((i + 1) * length / count)
                            )
                        ]
                    )
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
        if self.detailed:
            read_peak = max(disk.read_history[-60:] or [disk.read_rate])
            write_peak = max(disk.write_history[-60:] or [disk.write_rate])
            graph_h = min(5, max(1, (h - 16) // 5))
            lines = [
                _line(
                    [
                        ("SYSTEM READ  ", f"bold {palette.yellow}"),
                        (f"now {human_rate(disk.read_rate)}  peak {human_rate(read_peak)}", palette.text),
                    ],
                    w,
                ),
            ]
            lines.extend(
                braille_graph(
                    disk.read_history or [disk.read_rate],
                    w,
                    graph_h,
                    palette,
                    maximum=max(read_peak * 1.1, 1.0),
                    solid_color=palette.yellow,
                )
            )
            lines.append(Text("─" * w, style=palette.faint))
            lines.append(
                _line(
                    [
                        ("SYSTEM WRITE ", f"bold {palette.magenta}"),
                        (f"now {human_rate(disk.write_rate)}  peak {human_rate(write_peak)}", palette.text),
                    ],
                    w,
                )
            )
            lines.extend(
                braille_graph(
                    disk.write_history or [disk.write_rate],
                    w,
                    graph_h,
                    palette,
                    maximum=max(write_peak * 1.1, 1.0),
                    solid_color=palette.magenta,
                )
            )
            lines.append(Text("", style=palette.faint))
            devices: dict[str, MountInfo] = {}
            for mount in disk.mounts:
                devices.setdefault(mount.device or mount.mount, mount)
            ordered = sorted(
                devices.values(), key=lambda item: item.read_rate + item.write_rate, reverse=True
            )
            device_width = min(16, max(8, w // 7))
            mount_width = max(12, min(24, w // 5))
            direction_width = 5
            ratio_width = min(22, max(12, w // 4))
            graph_width = max(1, w - device_width - mount_width - direction_width - ratio_width - 7)
            table_header = Text()
            table_header.append(f"{'DEVICE':<{device_width}} ", style=f"bold {palette.dim}")
            table_header.append(f"{'MOUNT':<{mount_width}} ", style=f"bold {palette.dim}")
            table_header.append(f"{'DIR':<{direction_width}} ", style=f"bold {palette.dim}")
            table_header.append(f"{'HISTORY':<{graph_width}}  ", style=f"bold {palette.dim}")
            table_header.append(f"{'NOW / PEAK':>{ratio_width}}", style=f"bold {palette.dim}")
            lines.append(_pad_text(table_header, w))
            row_budget = max(0, (h - len(lines) - 1) // 3)
            for mount in ordered[:row_budget]:
                read_history = smooth_series(mount.read_history[-60:] or [0.0])
                write_history = smooth_series(mount.write_history[-60:] or [0.0])
                read_peak = max([mount.read_rate, *read_history])
                write_peak = max([mount.write_rate, *write_history])
                device = clip(mount.device or "?", device_width)
                mount_name = clip(mount.mount, mount_width)
                for direction, history, current, peak, color in (
                    ("READ", read_history, mount.read_rate, read_peak, palette.yellow),
                    ("WRITE", write_history, mount.write_rate, write_peak, palette.magenta),
                ):
                    row = Text()
                    row.append(
                        f"{device:<{device_width}} ",
                        style=f"bold {palette.accent}" if direction == "READ" else palette.dim,
                    )
                    row.append(
                        f"{mount_name:<{mount_width}} ",
                        style=palette.text if direction == "READ" else palette.dim,
                    )
                    row.append(f"{direction:<{direction_width}} ", style=f"bold {color}")
                    row.append(sparkline(history, graph_width), style=color)
                    ratio = f"{human_rate(current)} / {human_rate(peak)}"
                    row.append(f"  {truncate_middle(ratio, ratio_width):>{ratio_width}}", style=palette.text)
                    lines.append(_pad_text(row, w))
                lines.append(Text("", style=palette.faint))
            if len(ordered) > row_budget and len(lines) < h:
                lines.append(Text(f"+ {len(ordered) - row_budget} devices hidden", style=palette.faint))
            return _join(lines, w, h)
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
            min(
                18,
                max((len(f"{human_bytes(m.used)}/{human_bytes(m.total)}") for m in disk.mounts), default=10),
            ),
        )
        free_w = max(8, min(10, max((len(human_bytes(m.free)) for m in disk.mounts), default=4)))
        if compact:
            mount_w = max(8, min(12, max((len(m.mount) for m in disk.mounts), default=8)))
            capacity_w = max(
                11,
                min(
                    15,
                    max(
                        (len(f"{human_bytes(m.used)}/{human_bytes(m.total)}") for m in disk.mounts),
                        default=11,
                    ),
                ),
            )
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
            scale = (
                max(
                    (
                        max(
                            m.read_rate,
                            m.write_rate,
                            *(m.read_history[-60:] or [0.0]),
                            *(m.write_history[-60:] or [0.0]),
                        )
                        for m in disk.mounts
                    ),
                    default=0.0,
                )
                * 1.05
                or 1.0
            )
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


class FilesystemPanel(Widget):
    """Filesystem capacity and inode usage for mounted volumes."""

    DEFAULT_CSS = """
    FilesystemPanel { height: 1fr; min-height: 2; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.mounts: list[MountInfo] = []

    def update_data(self, disk: DiskInfo) -> None:
        self.mounts = list(disk.mounts)
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        if not self.mounts:
            return _join([Text("No filesystem capacity data", style=palette.dim)], w, h)
        ordered = sorted(self.mounts, key=lambda item: item.percent, reverse=True)
        space_rows = max(0, h - 1)
        density = 3 if space_rows >= 3 * len(ordered) else 2 if space_rows >= 2 * len(ordered) else 1
        limit = min(len(ordered), space_rows // density)
        mount_width = min(30, max(12, w // 3))
        type_width = 8
        lines = [Text("FILESYSTEM SPACE AND INODE USE", style=f"bold {palette.accent}")]

        for mount in ordered[:limit]:
            inode_percent = mount.inodes_percent
            if density == 3:
                details = Text()
                details.append(
                    f"{clip(mount.mount, mount_width):<{mount_width}}", style=f"bold {palette.text}"
                )
                details.append(
                    f"  {clip(mount.filesystem or '?', type_width):<{type_width}}", style=palette.dim
                )
                details.append("  USED ", style=palette.dim)
                details.append(human_bytes(mount.used), style=f"bold {palette.text}")
                details.append(" / ", style=palette.faint)
                details.append(human_bytes(mount.total), style=palette.text)
                details.append("  FREE ", style=palette.dim)
                details.append(human_bytes(mount.free), style=palette.green)
                lines.append(_pad_text(details, w))

                space_bar_width = max(1, w - 15)
                space_line = Text("SPACE  ", style=f"bold {palette.dim}")
                space_line.append_text(_context_bar(mount.percent, space_bar_width, palette))
                space_line.append("  ")
                space_line.append(f"{mount.percent:5.1f}%", style=f"bold {palette.gradient(mount.percent)}")
                lines.append(_pad_text(space_line, w))

                if inode_percent is not None:
                    inode_counts = f"{mount.inodes_used}/{mount.inodes_total}"
                    count_width = min(24, max(9, len(inode_counts)))
                    inode_bar_width = max(1, w - 7 - count_width - 2 - 2 - 6)
                    inode_line = Text("INODES ", style=f"bold {palette.dim}")
                    inode_line.append(
                        f"{clip(inode_counts, count_width):>{count_width}}", style=palette.faint
                    )
                    inode_line.append("  ")
                    inode_line.append_text(_context_bar(inode_percent, inode_bar_width, palette))
                    inode_line.append("  ")
                    inode_color = palette.orange if inode_percent >= 85 else palette.text
                    inode_line.append(f"{inode_percent:5.1f}%", style=f"bold {inode_color}")
                    lines.append(_pad_text(inode_line, w))
                else:
                    lines.append(Text("INODES unavailable", style=palette.faint))
            elif density == 2:
                details = Text()
                details.append(
                    f"{clip(mount.mount, mount_width):<{mount_width}}", style=f"bold {palette.text}"
                )
                details.append(
                    f"  {clip(mount.filesystem or '?', type_width):<{type_width}}", style=palette.dim
                )
                details.append(
                    f"  {human_bytes(mount.used)} / {human_bytes(mount.total)}", style=palette.text
                )
                details.append(f"  FREE {human_bytes(mount.free)}", style=palette.green)
                if inode_percent is not None:
                    details.append(
                        f"  INODE {inode_percent:.1f}%",
                        style=palette.orange if inode_percent >= 85 else palette.dim,
                    )
                lines.append(_pad_text(details, w))
                space_bar_width = max(1, w - 18)
                space_line = Text("SPACE  ", style=f"bold {palette.dim}")
                space_line.append_text(_context_bar(mount.percent, space_bar_width, palette))
                space_line.append("  ")
                space_line.append(f"{mount.percent:5.1f}%", style=f"bold {palette.gradient(mount.percent)}")
                lines.append(_pad_text(space_line, w))
            else:
                inode = f"{inode_percent:.0f}%" if inode_percent is not None else "—"
                line = Text()
                line.append(f"{clip(mount.mount, mount_width):<{mount_width}}", style=palette.accent)
                line.append(f" {clip(mount.filesystem or '?', type_width):<{type_width}} ", style=palette.dim)
                line.append(
                    f"{human_bytes(mount.used):>9}/{human_bytes(mount.total):<9} ", style=palette.text
                )
                line.append(f"{mount.percent:5.1f}% ", style=f"bold {palette.gradient(mount.percent)}")
                line.append(
                    f"inode {inode}",
                    style=palette.orange
                    if inode_percent is not None and inode_percent >= 85
                    else palette.dim,
                )
                lines.append(_pad_text(line, w))

        hidden = len(ordered) - limit
        if hidden and len(lines) < h:
            lines.append(Text(f"+ {hidden} more filesystems", style=palette.faint))
        return _join(lines, w, h)


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


class NetDetailPanel(Widget):
    """Per-interface rates, reported link limits, and socket-owner tables."""

    DEFAULT_CSS = """
    NetDetailPanel { height: 1fr; min-height: 2; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.net = NetInfo()

    def update_data(self, net: NetInfo) -> None:
        self.net = net
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        net = self.net
        peak_rx = max(net.rx_history[-60:] or [net.rx_rate])
        peak_tx = max(net.tx_history[-60:] or [net.tx_rate])
        lines = [
            _line(
                [
                    ("OBSERVED PEAK  ", palette.dim),
                    (f"RX {human_rate(peak_rx)}", palette.green),
                    ("   TX ", palette.dim),
                    (human_rate(peak_tx), palette.cyan),
                ],
                w,
            ),
        ]
        interfaces = sorted(
            net.interfaces,
            key=lambda item: (item.rx_rate + item.tx_rate > 0, item.rx_rate + item.tx_rate),
            reverse=True,
        )
        lines.append(
            Text(
                "Interface counters · per-process rates use Linux TCP_INFO when available",
                style=palette.faint,
            )
        )
        hidden = 0
        interface_width = 16
        process_width = 22
        pid_width = 10
        connections_width = 5
        rate_width = 10
        gap = "     "
        for interface in interfaces:
            required = 2 + max(1, len(interface.processes))
            if len(lines) + required > h:
                hidden += 1
                continue
            maximum = human_rate(interface.max_rate) if interface.max_rate is not None else "—"
            interface_line = Text()
            interface_line.append(
                f"{clip(interface.name, interface_width):<{interface_width}}", style=f"bold {palette.accent}"
            )
            interface_line.append(gap)
            interface_line.append(f"{'RX ' + human_rate(interface.rx_rate):>12}", style=palette.green)
            interface_line.append(gap)
            interface_line.append(f"{'TX ' + human_rate(interface.tx_rate):>12}", style=palette.cyan)
            interface_line.append(gap)
            interface_line.append(f"{'LINK MAX ' + maximum:>20}", style=palette.dim)
            lines.append(_pad_text(interface_line, w))

            header = Text("  ")
            header.append(f"{'PID':>{pid_width}}", style=palette.dim)
            header.append(gap)
            header.append(f"{'PROCESS':<{process_width}}", style=palette.dim)
            header.append(gap)
            header.append(f"{'CONN':>{connections_width}}", style=palette.dim)
            header.append(gap)
            header.append(f"{'RX/s':>{rate_width}}", style=palette.dim)
            header.append(gap)
            header.append(f"{'TX/s':>{rate_width}}", style=palette.dim)
            lines.append(_pad_text(header, w))
            if interface.processes:
                for process in interface.processes:
                    rx = human_rate(process.rx_rate) if process.rx_rate is not None else "—"
                    tx = human_rate(process.tx_rate) if process.tx_rate is not None else "—"
                    row = Text("  ")
                    row.append(f"{process.pid:>{pid_width}}", style=palette.faint)
                    row.append(gap)
                    row.append(f"{clip(process.name, process_width):<{process_width}}", style=palette.text)
                    row.append(gap)
                    row.append(f"{process.connections:>{connections_width}}", style=palette.dim)
                    row.append(gap)
                    row.append(f"{rx:>{rate_width}}", style=palette.green)
                    row.append(gap)
                    row.append(f"{tx:>{rate_width}}", style=palette.cyan)
                    lines.append(_pad_text(row, w))
            else:
                lines.append(Text("      no active process-owned connections", style=palette.faint))
        if hidden and len(lines) < h:
            lines.append(
                Text(f"+ {hidden} interfaces hidden; resize the terminal to see more", style=palette.faint)
            )
        elif not interfaces:
            lines.append(Text("Per-interface counters are unavailable.", style=palette.faint))
        return _join(lines, w, h)


class HostedSitesPanel(Widget):
    """Locally hosted HTTP services and their discovered entry files."""

    DEFAULT_CSS = """
    HostedSitesPanel { height: 1fr; min-height: 2; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.websites: list[HostedSiteInfo] = []

    def update_data(self, websites: Sequence[HostedSiteInfo]) -> None:
        self.websites = list(websites)
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        url_width = max(16, w // 4)
        file_width = max(12, w // 3)
        status_width, pid_width, process_width = 6, 9, max(8, w - url_width - file_width - 6 - 9 - 8)
        columns = [
            ("LINK", url_width, "left"),
            ("MAIN FILE", file_width, "left"),
            ("HTTP", status_width, "right"),
            ("PID", pid_width, "right"),
            ("PROCESS", process_width, "left"),
        ]
        header = Text()
        for index, (label, width, align) in enumerate(columns):
            if index:
                header.append("  ")
            value = clip(label, width)
            header.append(
                f"{value:<{width}}" if align == "left" else f"{value:>{width}}", style=f"bold {palette.dim}"
            )
        lines = [_pad_text(header, w)]
        if not self.websites:
            lines.append(_pad_text(Text("No local HTTP services detected.", style=palette.faint), w))
        for site in sorted(self.websites, key=lambda item: item.url)[: max(0, h - 1)]:
            status = str(site.status_code) if site.status_code is not None else "-"
            status_color = (
                palette.green
                if site.status_code is not None and 200 <= site.status_code < 400
                else (palette.red if site.status_code is not None else palette.dim)
            )
            values = [
                (site.url, url_width, "left", palette.cyan),
                (site.main_file or "-", file_width, "left", palette.text),
                (status, status_width, "right", status_color),
                (str(site.pid) if site.pid is not None else "-", pid_width, "right", palette.faint),
                (site.process or "-", process_width, "left", palette.text),
            ]
            row = Text()
            for index, (value, width, align, style) in enumerate(values):
                if index:
                    row.append("  ")
                clipped = clip(truncate_middle(value, width) if index == 1 else value, width)
                start = row.cell_len
                row.append(f"{clipped:<{width}}" if align == "left" else f"{clipped:>{width}}", style=style)
                if index == 0:
                    row.stylize(f"link {site.url}", start, row.cell_len)
                    row.stylize("underline", start, row.cell_len)
            lines.append(_pad_text(row, w))
        return _join(lines, w, h)


class ListeningPortsPanel(Widget):
    """TCP/UDP listeners and the process that owns each bound port."""

    DEFAULT_CSS = """
    ListeningPortsPanel { height: 1fr; min-height: 2; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.listeners: list[NetListenerInfo] = []

    def update_data(self, listeners: Sequence[NetListenerInfo]) -> None:
        self.listeners = list(listeners)
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        port_width, protocol_width, address_width, pid_width = 7, 6, max(12, w // 4), 9
        process_width = max(8, w - port_width - protocol_width - address_width - pid_width - 8)
        columns = [
            ("PORT", port_width, "right"),
            ("PROTO", protocol_width, "left"),
            ("ADDRESS", address_width, "left"),
            ("PID", pid_width, "right"),
            ("APPLICATION", process_width, "left"),
        ]
        header = Text()
        for index, (label, width, align) in enumerate(columns):
            if index:
                header.append("  ")
            value = clip(label, width)
            header.append(
                f"{value:<{width}}" if align == "left" else f"{value:>{width}}", style=f"bold {palette.dim}"
            )
        lines = [_pad_text(header, w)]
        if not self.listeners:
            lines.append(_pad_text(Text("No listening ports detected.", style=palette.faint), w))
        for listener in sorted(self.listeners, key=lambda item: (item.port, item.protocol, item.pid or 0))[
            : max(0, h - 1)
        ]:
            values = [
                (str(listener.port), port_width, "right", palette.cyan),
                (listener.protocol, protocol_width, "left", palette.accent),
                (listener.address or "*", address_width, "left", palette.dim),
                (str(listener.pid) if listener.pid is not None else "-", pid_width, "right", palette.faint),
                (listener.process or "-", process_width, "left", palette.text),
            ]
            row = Text()
            for index, (value, width, align, style) in enumerate(values):
                if index:
                    row.append("  ")
                clipped = clip(value, width)
                row.append(f"{clipped:<{width}}" if align == "left" else f"{clipped:>{width}}", style=style)
            lines.append(_pad_text(row, w))
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


class GpuProcTable(Widget):
    DEFAULT_CSS = """
    GpuProcTable { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.processes: list = []
        self.selected = 0
        self.gpu_totals: dict[int, int] = {}
        self.active_projects: list[str] = []

    def update_data(self, processes, selected: int, gpus=(), active_projects=()) -> None:
        next_processes = list(processes)
        next_totals = {gpu.index: gpu.mem_total for gpu in gpus}
        next_projects = list(active_projects)
        if (
            self.processes == next_processes
            and self.selected == selected
            and self.gpu_totals == next_totals
            and self.active_projects == next_projects
        ):
            return
        self.processes = next_processes
        self.selected = selected
        self.gpu_totals = next_totals
        self.active_projects = next_projects
        self.refresh()

    def render(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        if h <= 1:
            return Text("GPU processes", style=palette.dim)
        lines: list[Text] = []
        remaining = max(0, w - 56)
        natural_process_width = max(
            8,
            max((cell_len(proc.name or "?") for proc in self.processes), default=8),
        )
        show_command = remaining >= 8 + 1 + 18
        details_width = 26
        show_details = show_command and remaining >= 8 + 1 + 18 + 1 + details_width
        if show_command:
            details_column_width = details_width if show_details else 0
            process_width = min(
                natural_process_width,
                16,
                remaining - 1 - 18 - (1 + details_column_width if show_details else 0),
            )
            process_width = max(8, process_width)
            command_width = remaining - process_width - 1 - (details_column_width + 1 if show_details else 0)
        else:
            process_width = min(natural_process_width, remaining)
            command_width = details_width = 0
        header = [
            (f"{'PID':<9}", palette.dim),
            (" ", palette.dim),
            (f"{'G':>1}", palette.dim),
            (" ", palette.dim),
            (f"{'T':>1}", palette.dim),
            (" ", palette.dim),
            (f"{'CPU%':>5}", palette.dim),
            (" ", palette.dim),
            (f"{'RAM%':>5}", palette.dim),
            (" ", palette.dim),
            (f"{'SM%':>3}", palette.dim),
            (" ", palette.dim),
            (f"{'GMBW%':>5}", palette.dim),
            (" ", palette.dim),
            (f"{'VRAM':>9}", palette.dim),
            (" ", palette.dim),
            (f"{'TIME':>8}", palette.dim),
            (" ", palette.dim),
            (f"{'PROCESS':<{process_width}}", palette.dim),
        ]
        if show_details:
            header.extend(
                [
                    (" ", palette.dim),
                    (f"{'DETAILS':<{details_width}}", palette.dim),
                    (" ", palette.dim),
                    (f"{'COMMAND':<{command_width}}", palette.dim),
                ]
            )
        elif show_command:
            header.extend([(" ", palette.dim), (f"{'COMMAND':<{command_width}}", palette.dim)])
        lines.append(_line(header, w))
        visible = max(1, h - 1)
        selected = self.selected if 0 <= self.selected < len(self.processes) else None
        start = max(0, selected - visible + 1) if selected is not None else 0
        if selected is not None and start > max(0, len(self.processes) - visible):
            start = max(0, len(self.processes) - visible)
        rows = self.processes[start : start + visible]
        for offset, proc in enumerate(rows):
            i = start + offset
            selected_row = i == selected
            base = f"on {palette.selected_bg} {palette.selected_fg}" if selected_row else ""
            sm = "-" if proc.sm is None else f"{proc.sm:3.0f}"
            gmbw = "-" if proc.gmbw_pct is None else f"{proc.gmbw_pct:5.0f}"
            cpu = "-" if proc.cpu_pct is None else f"{proc.cpu_pct:.1f}"
            ram = "-" if proc.host_mem_pct is None else f"{proc.host_mem_pct:.1f}"
            vram = _gpu_memory(proc.vram) if proc.vram is not None else "-"
            gpu_total = self.gpu_totals.get(proc.gpu_index, 0)
            vram_use = None
            if proc.vram is not None and gpu_total > 0:
                free_pct = max(0.0, gpu_total - proc.vram) / gpu_total * 100.0
                vram_use = 100.0 - free_pct
            runtime = "-"
            if proc.runtime_s is not None:
                seconds = int(proc.runtime_s)
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                runtime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                if hours >= 100:
                    runtime = f"{hours // 24}d{hours % 24:02d}:{minutes:02d}"
            parts = [
                (f"{proc.pid:<9}", palette.dim if not selected_row else ""),
                (" ", ""),
                (f"{proc.gpu_index:>1}", palette.accent if not selected_row else ""),
                (" ", ""),
                (f"{proc.kind:>1}", palette.magenta if proc.kind == "G" else palette.green),
                (" ", ""),
                (f"{cpu:>5}", _process_usage_color(proc.cpu_pct, palette)),
                (" ", ""),
                (f"{ram:>5}", _process_usage_color(proc.host_mem_pct, palette)),
                (" ", ""),
                (f"{sm:>3}", _process_usage_color(proc.sm, palette)),
                (" ", ""),
                (f"{gmbw:>5}", _process_usage_color(proc.gmbw_pct, palette)),
                (" ", ""),
                (f"{vram:>9}", _process_usage_color(vram_use, palette)),
                (" ", ""),
                (f"{runtime:>8}", palette.dim),
                (" ", ""),
                (f"{clip(proc.name, process_width):<{process_width}}", palette.text),
            ]
            if show_details:
                details = _gpu_process_details(proc, self.active_projects)
                parts.extend(
                    [
                        (" ", ""),
                        (f"{clip(details or '-', details_width):<{details_width}}", palette.text),
                        (" ", ""),
                        (clip(proc.cmd or proc.name, command_width), palette.dim),
                    ]
                )
            elif show_command:
                parts.extend([(" ", ""), (clip(proc.cmd or proc.name, command_width), palette.dim)])
            lines.append(_line(parts, w, base))
        return _join(lines, w, h)


class ProcTable(Widget):
    DEFAULT_CSS = """
    ProcTable { height: 1fr; min-height: 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.processes: list[ProcInfo] = []
        self.gpu_processes: list[GpuProcess] = []
        self.selected = 0
        self.sort_column: Optional[str] = None
        self.sort_direction = 0
        self.show_gpu_columns = True

    def _column_specs(
        self, width: int, show_gpu_columns: Optional[bool] = None
    ) -> list[tuple[str, str, int]]:
        compact = width < 112
        specs = [
            ("pid", "PID", 9),
            ("user", "USER", 6 if compact else 10),
            ("cpu", "CPU%", 5),
            ("mem", "MEM%", 5),
            ("rss", "RSS", 5 if compact else 7),
        ]
        include_gpu = self.show_gpu_columns if show_gpu_columns is None else show_gpu_columns
        if include_gpu:
            specs.extend(
                [
                    ("gpu", "GPU", 4 if compact else 6),
                    ("type", "T", 1),
                    ("sm", "SM%", 4),
                    ("gmbw", "GMBW%", 5),
                    ("vram", "VRAM", 8 if compact else 10),
                    ("time", "TIME", 8),
                ]
            )
        command_width = max(1, width - sum(spec[2] for spec in specs) - len(specs))
        specs.append(("process", "PROCESS", command_width))
        return specs

    def update_data(
        self,
        processes: list[ProcInfo],
        selected: int,
        sort_column: Optional[str] = None,
        sort_direction: int = 0,
        gpu_processes: Optional[list[GpuProcess]] = None,
        show_gpu_columns: bool = True,
    ) -> None:
        self.processes = processes
        self.gpu_processes = list(gpu_processes or [])
        self.selected = selected
        self.sort_column = sort_column
        self.sort_direction = sort_direction
        self.show_gpu_columns = show_gpu_columns
        self.refresh()

    def on_click(self, event) -> None:
        if event.y != 0:
            return
        x = 0
        for column, _label, width in self._column_specs(max(1, self.size.width)):
            if x <= event.x < x + width:
                self.app.cycle_proc_sort(column)
                event.stop()
                return
            x += width + 1

    def render(self) -> Text:
        if self.show_gpu_columns:
            return self._render_extended()
        return self._render_cpu_only()

    def _render_cpu_only(self) -> Text:
        """Render Overview's CPU process list without the separate GPU fields."""
        palette = get_palette()
        w, h = max(1, self.size.width), max(1, self.size.height)
        if h <= 1:
            return Text("CPU processes", style=palette.dim)
        specs = self._column_specs(w, show_gpu_columns=False)
        header = Text()
        left_aligned = {"pid", "user", "process"}
        for index, (column, label, width) in enumerate(specs):
            if index:
                header.append(" ")
            if self.sort_column == column and width >= 6:
                label = f"{clip(label, width - 2)} {'↑' if self.sort_direction > 0 else '↓'}"
            label = clip(label, width)
            align = "left" if column in left_aligned else "right"
            style = (
                f"bold underline {palette.accent}"
                if column == self.sort_column
                else f"underline {palette.dim}"
            )
            header.append(f"{label:<{width}}" if align == "left" else f"{label:>{width}}", style=style)
        lines = [_pad_text(header, w)]
        visible = max(1, h - 1)
        selected = min(max(0, self.selected), max(0, len(self.processes) - 1))
        start = max(0, selected - visible + 1)
        start = min(start, max(0, len(self.processes) - visible))
        for index, proc in enumerate(self.processes[start : start + visible], start):
            selected_row = index == selected
            base = f"on {palette.selected_bg} {palette.selected_fg}" if selected_row else ""
            values = {
                "pid": (str(proc.pid), palette.dim),
                "user": (proc.user, palette.text),
                "cpu": (f"{proc.cpu:.1f}", _process_usage_color(proc.cpu, palette)),
                "mem": (f"{proc.mem_pct:.1f}", _process_usage_color(proc.mem_pct, palette)),
                "rss": (human_bytes(proc.rss), palette.dim),
                "process": (proc.cmd or proc.name, palette.text if selected_row else palette.dim),
            }
            row = Text(style=base)
            for column_index, (column, _label, width) in enumerate(specs):
                if column_index:
                    row.append(" ")
                value, color = values[column]
                value = clip(value, width)
                align = "left" if column in left_aligned else "right"
                row.append(f"{value:<{width}}" if align == "left" else f"{value:>{width}}", style=color)
            lines.append(_pad_text(row, w))
        return _join(lines, w, h)

    def _render_extended(self) -> Text:
        palette = get_palette()
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        if h <= 1:
            return Text("processes", style=palette.dim)
        gpu_by_pid: dict[int, list[GpuProcess]] = {}
        for gpu_proc in self.gpu_processes:
            gpu_by_pid.setdefault(gpu_proc.pid, []).append(gpu_proc)
        for entries in gpu_by_pid.values():
            entries.sort(key=lambda item: item.gpu_index)
        specs = self._column_specs(w, show_gpu_columns=True)
        widths = {name: width for name, _label, width in specs}
        user_width = widths["user"]
        rss_width = widths["rss"]
        gpu_width = widths.get("gpu", 0)
        kind_width = widths.get("type", 0)
        sm_width = widths.get("sm", 0)
        gmbw_width = widths.get("gmbw", 0)
        vram_width = widths.get("vram", 0)
        time_width = widths.get("time", 0)
        cmd_w = widths["process"]
        lines: list[Text] = []
        header = Text()
        left_aligned = {"pid", "user", "process"}
        for index, (column, label, width) in enumerate(specs):
            if index:
                header.append(" ")
            if self.sort_column == column and width >= 6:
                arrow = "↑" if self.sort_direction > 0 else "↓"
                label = f"{clip(label, width - 2)} {arrow}"
            label = clip(label, width)
            cell = f"{label:<{width}}" if column in left_aligned else f"{label:>{width}}"
            style = (
                f"bold underline {palette.accent}"
                if column == self.sort_column
                else f"underline {palette.dim}"
            )
            header.append(cell, style=style)
        lines.append(_pad_text(header, w))
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
            cpu_colour = _process_usage_color(proc.cpu, palette)
            mem_colour = _process_usage_color(proc.mem_pct, palette)
            gpu_entries = gpu_by_pid.get(proc.pid, []) if self.show_gpu_columns else []
            gpu_ids = ",".join(str(item.gpu_index) for item in gpu_entries) or "—"
            kinds = list(dict.fromkeys(item.kind for item in gpu_entries))
            gpu_kind = kinds[0] if len(kinds) == 1 else ("*" if kinds else "—")
            sm = "/".join(f"{item.sm:.0f}" if item.sm is not None else "-" for item in gpu_entries) or "—"
            gmbw = (
                "/".join(f"{item.gmbw_pct:.0f}" if item.gmbw_pct is not None else "-" for item in gpu_entries)
                or "—"
            )
            allocated = [item.vram for item in gpu_entries if item.vram is not None]
            vram = _gpu_memory(sum(allocated)) if allocated else "—"
            runtimes = [item.runtime_s for item in gpu_entries if item.runtime_s is not None]
            runtime = "—"
            if runtimes:
                seconds = int(max(runtimes))
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                runtime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                if hours >= 100:
                    runtime = f"{hours // 24}d{hours % 24:02d}:{minutes:02d}"
            parts = [
                (f"{proc.pid:<{widths['pid']}}", palette.dim),
                (" ", ""),
                (f"{clip(proc.user, user_width):<{user_width}}", palette.text),
                (" ", ""),
                (f"{proc.cpu:5.1f}", cpu_colour),
                (" ", ""),
                (f"{proc.mem_pct:5.1f}", mem_colour),
                (" ", ""),
                (f"{human_bytes(proc.rss):>{rss_width}}", palette.dim),
            ]
            if self.show_gpu_columns:
                parts.extend(
                    [
                        (" ", ""),
                        (
                            f"{clip(gpu_ids, gpu_width):>{gpu_width}}",
                            palette.accent if gpu_entries else palette.faint,
                        ),
                        (" ", ""),
                        (
                            f"{clip(gpu_kind, kind_width):>{kind_width}}",
                            palette.magenta if gpu_entries else palette.faint,
                        ),
                        (" ", ""),
                        (
                            f"{clip(sm, sm_width):>{sm_width}}",
                            palette.green if gpu_entries else palette.faint,
                        ),
                        (" ", ""),
                        (
                            f"{clip(gmbw, gmbw_width):>{gmbw_width}}",
                            palette.cyan if gpu_entries else palette.faint,
                        ),
                        (" ", ""),
                        (
                            f"{clip(vram, vram_width):>{vram_width}}",
                            palette.yellow if gpu_entries else palette.faint,
                        ),
                        (" ", ""),
                        (f"{clip(runtime, time_width):>{time_width}}", palette.dim),
                    ]
                )
            parts.extend(
                [
                    (" ", ""),
                    (clip(proc.cmd or proc.name, cmd_w), palette.text if selected_row else palette.dim),
                ]
            )
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
                from .draw import blend

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

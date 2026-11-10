"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

from typing import Optional, Sequence

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    clip,
    get_palette,
    human_bytes,
)
from ..model import (
    GpuProcess,
    ProcInfo,
)
from .common import (
    _gpu_memory,
    _join,
    _line,
    _pad_text,
    _process_usage_color,
)


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

"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget

from ..draw import (
    clip,
    get_palette,
)
from .common import (
    _gpu_memory,
    _gpu_process_details,
    _join,
    _line,
    _process_usage_color,
)


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

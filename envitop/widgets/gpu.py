"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

from typing import Optional

from rich.cells import cell_len
from rich.text import Text
from textual.widget import Widget

from ..draw import (
    braille_graph,
    braille_stacked_graph,
    clip,
    get_palette,
    human_bytes,
    smooth_series,
    tiny_bar,
)
from ..model import (
    GpuState,
)
from .common import (
    _context_bar,
    _join,
    _line,
    _pad_text,
)


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
            if gi < n - 1 and this_band <= 2:
                # Replace the last line with a separator if the band is tiny.
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

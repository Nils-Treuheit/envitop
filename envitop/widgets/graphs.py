"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

from typing import Iterable, Optional

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    braille_graph,
    braille_line_graph,
    clip,
    get_palette,
    smooth_series,
)
from .common import (
    _context_bar,
    _join,
    _line,
    _pad_text,
)


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
        label_min_height: int = 5,
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
        self.label_min_height = label_min_height
        self.primary_value: Optional[float] = None
        self.secondary_value: Optional[float] = None

    def update_data(
        self,
        values: Iterable[float],
        maximum: Optional[float] = None,
        secondary: Optional[Iterable[float]] = None,
        secondary_maximum: Optional[float] = None,
        primary_value: Optional[float] = None,
        secondary_value: Optional[float] = None,
    ) -> None:
        self.values = smooth_series(list(values))
        if secondary is not None:
            self.secondary = smooth_series(list(secondary))
        if maximum is not None:
            self.maximum = maximum
        self.secondary_maximum = secondary_maximum
        self.primary_value = primary_value
        self.secondary_value = secondary_value
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
            show_labels = h >= self.label_min_height and (self.primary_label or self.secondary_label)
            label_rows = int(show_labels and bool(self.primary_label)) + int(
                show_labels and bool(self.secondary_label)
            )
            show_divider = h - label_rows >= 3
            graph_height = h - label_rows - int(show_divider)
            top_h = max(1, graph_height // 2)
            bottom_h = max(1, graph_height - top_h)
            primary_colour = getattr(palette, self.primary_role, palette.accent)
            secondary_colour = getattr(palette, self.secondary_role, palette.magenta)
            graph_fn = braille_line_graph if self.graph_style == "line" else braille_graph
            lines: list[Text] = []
            if show_labels and self.primary_label:
                label = Text(self.primary_label, style=primary_colour)
                if self.primary_value is not None:
                    label.append(
                        f" {self.primary_value:.0f}%", style=f"bold {palette.gradient(self.primary_value)}"
                    )
                lines.append(label)
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
                label = Text(self.secondary_label, style=secondary_colour)
                if self.secondary_value is not None:
                    label.append(
                        f" {self.secondary_value:.0f}%",
                        style=f"bold {palette.gradient(self.secondary_value)}",
                    )
                lines.append(label)
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

    def _overview_groups(self, width: int, height: int):
        """Aggregate only as much as needed to fit every core in the grid."""
        count = len(self.cores)
        rows = max(1, height - 1)
        label_width = len(f"C{max(0, count - 4)}-{count - 1}")
        cell_width = max(14, label_width + 9)
        columns = max(1, width // (cell_width + 1))
        group_size = max(1, (count + columns * rows - 1) // (columns * rows))
        groups = []
        for start in range(0, count, group_size):
            end = min(count, start + group_size)
            values = self.cores[start:end]
            temps = [value for value in self.temperatures[start:end] if value is not None]
            groups.append((start, end, sum(values) / len(values), max(temps) if temps else None))
        return groups, columns, cell_width

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
        groups, cols, cell = self._overview_groups(w, h)
        temp_label = "CCD" if self.temp_source.upper() == "CCD" else "TMP"
        header = Text()
        cell_core_width = cell - 9
        for _ in range(cols):
            header.append(f"{'CORE':<{cell_core_width}} {'%':>4} {temp_label:<3} ", style=palette.dim)
        lines: list[Text] = [header]
        for row in range(min(max(0, h - 1), (len(groups) + cols - 1) // cols)):
            line = Text()
            for col in range(cols):
                group_idx = row * cols + col
                if group_idx >= len(groups):
                    line.append(" " * (cell + 1))
                    continue
                start, end, pct, temp = groups[group_idx]
                temp_text = f"{temp:.0f}°" if temp is not None else ""
                core_label = f"C{start}" if end == start + 1 else f"C{start}-{end - 1}"
                line.append(f"{core_label:<{cell_core_width}} ", style=palette.dim)
                line.append(f"{pct:3.0f}%", style=f"bold {palette.gradient(pct)}")
                line.append(f" {temp_text:>3} ", style=palette.orange if temp is not None else palette.dim)
            lines.append(_pad_text(line, w))
        return _join(lines, w, h)

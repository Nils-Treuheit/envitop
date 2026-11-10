"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    braille_graph,
    clip,
    get_palette,
    human_bytes,
    human_rate,
    smooth_series,
    sparkline,
    truncate_middle,
)
from ..model import (
    DiskInfo,
    MountInfo,
)
from .common import (
    _context_bar,
    _join,
    _line,
    _pad_text,
)


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

"""Custom btop-flavoured widgets (all render with Rich/Textual)."""

from __future__ import annotations

from typing import Sequence

from rich.text import Text
from textual.widget import Widget

from ..draw import (
    clip,
    get_palette,
    human_rate,
    truncate_middle,
)
from ..model import (
    HostedSiteInfo,
    NetInfo,
    NetListenerInfo,
)
from .common import (
    _join,
    _line,
    _pad_text,
)


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

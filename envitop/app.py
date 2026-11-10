"""Textual application: the btop-like unified dashboard."""

from __future__ import annotations

import copy
import os
import re
import time
from collections import deque
from contextlib import suppress
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .draw import (
    clip,
    cycle_palette,
    get_palette,
    human_bytes,
    human_rate,
    set_palette,
    smooth_series,
    truncate_middle,
)
from .insights import diagnose
from .model import AgentSession, ProcInfo, Snapshot
from .sampler import Sampler
from .widgets import (
    AgentPortsPanel,
    AgentTable,
    CoreBars,
    CpuTopProcesses,
    DiskPanel,
    FilesystemPanel,
    Footer,
    GpuPanel,
    GpuProcTable,
    GpuStatsPanel,
    Header,
    HistoryGraph,
    HostedSitesPanel,
    ListeningPortsPanel,
    NetDetailPanel,
    OneLine,
    ProcTable,
    QuotaPanel,
    SessionDetail,
    TokenOverview,
)

HELP_TEXT = """\
[b]envitop[/b] — btop + nvitop + abtop in one view

[b]Navigation[/b]
  [cyan]j[/cyan] / [cyan]k[/cyan], [cyan]↑[/cyan] / [cyan]↓[/cyan] move selection
  [cyan]tab[/cyan] / [cyan]shift+tab[/cyan]    cycle focus fields
  [cyan]0[/cyan]–[cyan]6[/cyan]              Overview / CPU / GPU / Net / Memory / Procs / Agents
  [cyan]←[/cyan] / [cyan]→[/cyan], [cyan]Home[/cyan] / [cyan]End[/cyan] scrub session history
  [cyan]f[/cyan]                  search all Procs fields
  [cyan]/[/cyan]                  column regex, e.g. PID/150 or T/C; Enter accept, Esc clear
  Click Procs headers    cycle ascending / descending / unsorted
  [cyan]a[/cyan]                  toggle 30 min / 24 h agent scope
  [cyan]p[/cyan]                  pause sampling
  [cyan]r[/cyan]                  force refresh
  [cyan]t[/cyan]                  cycle colour theme
  [cyan]?[/cyan]                  close this help
  [cyan]q[/cyan]                  quit

[b]Kill[/b]
  [cyan]x[/cyan]                  kill selected process or agent
  [cyan]y[/cyan] / [cyan]n[/cyan]              confirm / cancel
  OpenCode's shared server and envitop itself are protected.

[b]Data sources[/b]
  CPU / memory / network / disk / processes ... psutil
  GPU stats and per-process SM% ... nvidia-smi + pmon
  Claude Code / Codex / OpenCode ... local transcripts + sqlite
  Rate limits ... abtop statusline cache (if installed)

Monitoring is read-only. Local HTTP services receive a bounded HEAD probe for status codes.
No API keys or external network requests are used.
Use ↑/↓ to scroll if the help does not fit.
Press any key to close."""


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]
    SCROLL_KEYS = {
        "up": "scroll_up",
        "down": "scroll_down",
        "left": "scroll_left",
        "right": "scroll_right",
        "pageup": "page_up",
        "pagedown": "page_down",
        "home": "scroll_home",
        "end": "scroll_end",
        "space": "page_down",
    }

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(Text.from_markup(HELP_TEXT))

    def on_mount(self) -> None:
        palette = get_palette()
        box = self.query_one("#help-box", VerticalScroll)
        box.styles.border = ("round", palette.border_focus)
        box.styles.background = palette.panel
        box.focus()

    def on_key(self, event) -> None:
        # The screen is opened by a deferred callback, so this cannot swallow
        # the very key press that opened it.  Navigation keys scroll the help
        # text; every other key closes it.
        event.stop()
        action = self.SCROLL_KEYS.get(event.key)
        if action:
            box = self.query_one("#help-box", VerticalScroll)
            with suppress(Exception):
                getattr(box, f"action_{action}")()
            return
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class EnviTop(App):
    CSS_PATH = "app.tcss"

    def __init__(self, scope: str = "recent", theme: str = "btop", demo: bool = False) -> None:
        super().__init__()
        set_palette(theme)
        self.demo = demo
        self.source = None
        self.narrow = False
        self.view = "overview"
        self.timeline = deque(maxlen=120)
        self.timeline_offset = 0
        self.focus_area = "agents"
        self.agent_index = 0
        self.gpu_proc_index = 0
        self.proc_index = 0
        self._displayed_gpu_processes = []
        self._displayed_procs: list[ProcInfo] = []
        self.proc_sort_column: Optional[str] = "cpu"
        self.proc_sort_direction = -1
        self.proc_sort_default = True
        self.proc_filter = ""
        self.proc_filter_active = False
        self.proc_filter_mode: Optional[str] = None
        self.current_insights = []
        self.scope = scope
        self.paused = False
        self.confirm_kill: Optional[tuple[str, int, str]] = None
        self.status_message = ""
        self.status_until = 0.0
        self._last_agent_key: Optional[tuple[str, str]] = None
        self._last_snapshot: Optional[Snapshot] = None

    # -- construction ----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(id="header")
        with Horizontal(id="main"):
            with Vertical(id="col-left", classes="col"):
                with Vertical(id="panel-cpu", classes="panel"):
                    yield OneLine(id="cpu-stats")
                    with Vertical(id="cpu-content"):
                        with Horizontal(id="cpu-top-row"):
                            with VerticalScroll(id="cpu-core-scroll"):
                                yield CoreBars(id="cpu-cores")
                            with Vertical(id="panel-cpu-aggregate", classes="cpu-aggregate"):
                                yield HistoryGraph(
                                    maximum=100,
                                    color_mode="value",
                                    primary_role="green",
                                    secondary_role="cyan",
                                    primary_label="USER CPU",
                                    secondary_label="TOTAL CPU",
                                    id="cpu-graph",
                                )
                        with Vertical(id="panel-cpu-top-procs", classes="panel"):
                            yield CpuTopProcesses(id="cpu-top-procs")
                with Vertical(id="memory-content"):
                    with Vertical(id="panel-mem", classes="panel"):
                        yield OneLine(id="mem-stats")
                        yield OneLine(id="mem-extra")
                        yield HistoryGraph(
                            maximum=100,
                            color_mode="value",
                            primary_role="magenta",
                            secondary_role="cyan",
                            primary_label="RAM USED",
                            secondary_label="SWAP USED",
                            id="mem-graph",
                        )
                    with Horizontal(id="memory-lower"):
                        with Vertical(id="panel-disk", classes="panel"):
                            yield DiskPanel(id="disk-panel")
                        with Vertical(id="panel-filesystems", classes="panel"):
                            yield FilesystemPanel(id="filesystem-panel")
                with Vertical(id="panel-net", classes="panel"):
                    yield OneLine(id="net-stats")
                    yield HistoryGraph(
                        id="net-graph",
                        color_mode="value",
                        primary_role="green",
                        secondary_role="cyan",
                        primary_label="TX ↑",
                        secondary_label="RX ↓",
                        secondary_invert=True,
                    )
                    with Horizontal(id="net-lower"):
                        with Vertical(id="panel-net-interfaces", classes="panel"):
                            yield NetDetailPanel(id="net-details")
                        with Vertical(id="panel-websites", classes="panel"):
                            yield HostedSitesPanel(id="hosted-sites")
                        with Vertical(id="panel-listeners", classes="panel"):
                            yield ListeningPortsPanel(id="listening-ports")
            with Vertical(id="col-mid", classes="col"):
                with Horizontal(id="gpu-top"):
                    with Vertical(id="panel-gpu", classes="panel"):
                        yield GpuPanel(id="gpu-panel")
                    with Vertical(id="panel-gpu-stats", classes="panel"):
                        yield GpuStatsPanel(id="gpu-stats")
                with Vertical(id="panel-gpuprocs", classes="panel"):
                    yield GpuProcTable(id="gpu-procs")
                with Vertical(id="panel-procs", classes="panel"):
                    yield OneLine(id="proc-summary")
                    yield ProcTable(id="procs")
            with Vertical(id="col-right", classes="col"):
                with Vertical(id="agent-content"):
                    with Vertical(id="agents-row-top"):
                        with Vertical(id="panel-agents", classes="panel"):
                            yield AgentTable(id="agents")
                        with Vertical(id="panel-tokens", classes="panel"):
                            yield TokenOverview(id="tokens")
                    with Vertical(id="agents-row-bottom"):
                        with Vertical(id="panel-detail", classes="panel"):
                            yield SessionDetail(id="detail")
                        with Vertical(id="panel-quota", classes="panel"):
                            yield QuotaPanel(id="quota")
                        with Vertical(id="panel-ports", classes="panel"):
                            yield AgentPortsPanel(id="agent-ports")
        yield Footer(id="footer")

    def on_mount(self) -> None:
        titles = {
            "panel-cpu": " CPU ",
            "panel-cpu-top-procs": " CPU PROCESSES ",
            "panel-cpu-aggregate": " AGGREGATED CPU MONITOR ",
            "panel-mem": " MEMORY ",
            "panel-net": " NETWORK ",
            "panel-net-interfaces": " INTERFACES / PROCESS I/O ",
            "panel-websites": " LOCAL WEBSITES ",
            "panel-listeners": " OPEN PORTS ",
            "panel-disk": " DISK I/O ",
            "panel-filesystems": " FILESYSTEMS ",
            "panel-gpu": " GPU ",
            "panel-gpu-stats": " GPU TELEMETRY ",
            "panel-gpuprocs": " GPU PROCESSES ",
            "panel-procs": " PROCESSES + GPU ",
            "panel-agents": " AI AGENTS ",
            "panel-detail": " SESSION ",
            "panel-tokens": " TOKENS ",
            "panel-quota": " RATE LIMITS ",
            "panel-ports": " CHILD PORTS ",
        }
        for panel_id, title in titles.items():
            self.query_one(f"#{panel_id}").border_title = title
        if self.demo:
            from .demo import DemoSource

            self.source = DemoSource(scope=self.scope)
        else:
            self.source = Sampler(scope=self.scope)
            self.source.start()
        self._apply_theme()
        self.set_interval(0.5, self.refresh_ui)
        self._apply_layout(self.size.width)
        self.refresh_ui()

    def on_unmount(self) -> None:
        if self.source is not None and hasattr(self.source, "stop"):
            self.source.stop()

    def on_resize(self, event) -> None:
        self._apply_layout(event.size.width)

    # -- layout / focus --------------------------------------------------

    def _columns(self) -> dict[str, Vertical]:
        return {
            "system": self.query_one("#col-left", Vertical),
            "gpu": self.query_one("#col-mid", Vertical),
            "agents": self.query_one("#col-right", Vertical),
        }

    def _apply_layout(self, width: int) -> None:
        self.narrow = width < 116
        cols = self._columns()
        panels = {
            "system": [
                "panel-cpu",
                "panel-mem",
                "panel-net",
                "panel-net-interfaces",
                "panel-websites",
                "panel-listeners",
                "panel-disk",
                "panel-filesystems",
            ],
            "gpu": ["panel-gpu", "panel-gpu-stats", "panel-gpuprocs", "panel-procs"],
            "agents": ["panel-agents", "panel-tokens", "panel-detail", "panel-quota", "panel-ports"],
        }
        tab_panels = {
            "overview": [
                "panel-cpu",
                "panel-cpu-aggregate",
                "panel-mem",
                "panel-net",
                "panel-disk",
                *panels["gpu"],
                "panel-agents",
                "panel-tokens",
                "panel-detail",
                "panel-quota",
            ],
            "cpu": ["panel-cpu", "panel-cpu-aggregate", "panel-cpu-top-procs"],
            "gpu": ["panel-gpu", "panel-gpu-stats", "panel-gpuprocs"],
            "net": ["panel-net", "panel-net-interfaces", "panel-websites", "panel-listeners"],
            "memory": ["panel-mem", "panel-disk", "panel-filesystems"],
            "procs": ["panel-procs"],
            "agents": panels["agents"],
        }
        visible = set(tab_panels.get(self.view, tab_panels["overview"]))
        aggregate_panel = self.query_one("#panel-cpu-aggregate")
        if self.view == "cpu":
            aggregate_panel.add_class("panel")
        else:
            aggregate_panel.remove_class("panel")
        self.query_one("#panel-procs").border_title = (
            " CPU PROCESSES " if self.view == "overview" else " PROCESSES + GPU "
        )
        # Keep Overview legible at small widths; every subsystem remains one
        # key away in its dedicated tab.
        if self.view == "overview" and self.narrow:
            visible = {"panel-cpu", "panel-cpu-aggregate", "panel-mem", "panel-net", "panel-disk"}
            if self.focus_area == "agents":
                visible.update(("panel-agents", "panel-tokens", "panel-detail", "panel-quota"))
            elif self.focus_area == "gpuprocs":
                visible.update(panel_id for panel_id in panels["gpu"] if panel_id != "panel-procs")
            elif self.focus_area == "procs":
                visible.add("panel-procs")
        for name, col in cols.items():
            col.styles.display = "block" if any(panel_id in visible for panel_id in panels[name]) else "none"
            col.styles.width = "1fr"
            col.styles.height = "1fr"
        for panel_id in (
            "panel-cpu",
            "panel-cpu-aggregate",
            "panel-cpu-top-procs",
            "panel-mem",
            "panel-net",
            "panel-net-interfaces",
            "panel-websites",
            "panel-listeners",
            "panel-disk",
            "panel-filesystems",
            "panel-gpu",
            "panel-gpu-stats",
            "panel-gpuprocs",
            "panel-procs",
            "panel-agents",
            "panel-tokens",
            "panel-detail",
            "panel-quota",
            "panel-ports",
        ):
            panel = self.query_one(f"#{panel_id}")
            panel.styles.display = "block" if panel_id in visible else "none"
            if self.view != "overview":
                panel.styles.height = "1fr"
            panel.styles.width = "1fr"
        self.query_one("#net-lower").styles.display = "block" if self.view == "net" else "none"
        memory_visible = bool(visible.intersection({"panel-mem", "panel-disk", "panel-filesystems"}))
        self.query_one("#memory-content").styles.display = "block" if memory_visible else "none"
        self.query_one("#memory-content").styles.layout = "vertical"
        self.query_one("#memory-lower").styles.display = (
            "block" if self.view in ("overview", "memory") else "none"
        )
        self.query_one("#panel-filesystems").styles.display = "block" if self.view == "memory" else "none"
        self.query_one("#mem-extra").styles.display = "block" if self.view == "memory" else "none"
        self.query_one("#cpu-top-row").styles.layout = "horizontal"
        self.query_one("#cpu-top-row").styles.height = "1fr"
        self.query_one("#panel-cpu-aggregate").styles.layout = "vertical"
        self.query_one("#panel-cpu-aggregate").styles.display = "block"
        self.query_one("#panel-cpu-aggregate").styles.height = "1fr"
        self.query_one("#panel-cpu-aggregate").styles.margin_left = 0
        self.query_one("#cpu-core-scroll").styles.width = "1fr"
        self.query_one("#panel-cpu-aggregate").styles.width = "1fr"
        self.query_one("#cpu-graph").styles.display = "block"
        self.query_one("#cpu-graph").styles.height = "1.5fr"
        self.query_one("#cpu-top-procs").styles.display = "block" if self.view == "cpu" else "none"
        self.query_one("#gpu-top").styles.display = "block" if "panel-gpu" in visible else "none"
        self.query_one("#gpu-top").styles.layout = "horizontal"
        self.query_one("#panel-gpu-stats").styles.display = "block" if self.view == "gpu" else "none"
        self.query_one("#net-graph").styles.height = "1fr"
        self.query_one("#net-details").styles.height = "1fr"
        self.query_one("#proc-summary").styles.display = "block" if self.view == "procs" else "none"
        if self.view == "overview":
            for panel_id, height in (
                ("panel-cpu", "2.6fr"),
                ("memory-content", "3.05fr"),
                ("panel-mem", "1.4fr"),
                ("memory-lower", "1.65fr"),
                ("panel-disk", "1fr"),
                ("panel-net", "1.15fr"),
                ("panel-gpu", "2.5fr"),
                ("panel-gpuprocs", "1.3fr"),
                ("panel-procs", "1.45fr"),
                ("panel-agents", "1.85fr"),
                ("panel-tokens", "2fr"),
                ("panel-detail", "1.3fr"),
                ("panel-quota", "1fr"),
            ):
                self.query_one(f"#{panel_id}").styles.height = height
        # Focused pages rearrange their subsystem panels around the available
        # width instead of stretching the Overview's vertical card stacks.
        for col in cols.values():
            col.styles.layout = "vertical"
        self.query_one("#cpu-content").styles.layout = "vertical"
        self.query_one("#cpu-top-row").styles.layout = "vertical"
        self.query_one("#cpu-cores").styles.width = "1fr"
        self.query_one("#cpu-graph").styles.width = "1fr"
        self.query_one("#panel-cpu-aggregate").styles.width = "1fr"
        self.query_one("#cpu-cores").styles.height = "1fr"
        self.query_one("#memory-lower").styles.layout = "vertical"
        self.query_one("#panel-mem").styles.height = "1.4fr"
        self.query_one("#memory-lower").styles.height = "1.65fr"
        self.query_one("#panel-disk").styles.width = "1fr"
        self.query_one("#panel-filesystems").styles.width = "1fr"
        self.query_one("#agent-content").styles.layout = "vertical"
        self.query_one("#agent-content").styles.display = (
            "block"
            if any(
                panel_id in visible
                for panel_id in ("panel-agents", "panel-tokens", "panel-detail", "panel-quota", "panel-ports")
            )
            else "none"
        )
        self.query_one("#agents-row-top").styles.layout = "vertical"
        self.query_one("#agents-row-bottom").styles.layout = "vertical"
        self.query_one("#agents-row-top").styles.height = "3.85fr"
        self.query_one("#agents-row-bottom").styles.height = "2.3fr"
        for panel_id in ("panel-agents", "panel-tokens", "panel-detail", "panel-quota", "panel-ports"):
            self.query_one(f"#{panel_id}").styles.width = "1fr"
        if self.view == "overview" and width >= 116:
            self.query_one("#cpu-top-row").styles.layout = "horizontal"
            self.query_one("#cpu-core-scroll").styles.width = "1.1fr"
            self.query_one("#panel-cpu-aggregate").styles.width = "1fr"
        if self.view == "cpu":
            self.query_one("#cpu-content").styles.layout = "vertical"
            self.query_one("#cpu-top-row").styles.layout = "horizontal"
            core_count = (
                len(self._last_snapshot.cpu.per_core)
                if self._last_snapshot is not None and self._last_snapshot.cpu.per_core
                else 24
            )
            self.query_one("#cpu-top-row").styles.height = core_count + 1
            self.query_one("#cpu-core-scroll").styles.height = "1fr"
            self.query_one("#cpu-core-scroll").styles.width = "1fr"
            self.query_one("#cpu-cores").styles.width = "1fr"
            self.query_one("#panel-cpu-aggregate").styles.height = "1fr"
            self.query_one("#panel-cpu-aggregate").styles.width = "0.43fr"
            self.query_one("#panel-cpu-aggregate").styles.margin_left = 1
            self.query_one("#cpu-graph").styles.height = "1fr"
            self.query_one("#panel-cpu-top-procs").styles.height = "1fr"
            self.query_one("#cpu-top-procs").styles.height = "1fr"
        if self.view == "memory":
            self.query_one("#memory-content").styles.height = "1fr"
            self.query_one("#panel-mem").styles.height = "1.15fr"
            self.query_one("#memory-lower").styles.height = "2fr"
            if width >= 130:
                self.query_one("#memory-lower").styles.layout = "horizontal"
                self.query_one("#panel-disk").styles.width = "1fr"
                self.query_one("#panel-filesystems").styles.width = "1.35fr"
            else:
                self.query_one("#memory-lower").styles.layout = "vertical"
                self.query_one("#panel-disk").styles.height = "1fr"
                self.query_one("#panel-filesystems").styles.height = "1.35fr"
        if self.view == "net":
            self.query_one("#net-graph").styles.height = "1.15fr"
            self.query_one("#net-lower").styles.height = "2.35fr"
            if width >= 150:
                self.query_one("#net-lower").styles.layout = "horizontal"
                self.query_one("#panel-net-interfaces").styles.width = "1.35fr"
                self.query_one("#panel-websites").styles.width = "1.25fr"
                self.query_one("#panel-listeners").styles.width = "1fr"
            else:
                self.query_one("#net-lower").styles.layout = "vertical"
        elif self.view == "gpu":
            self.query_one("#gpu-top").styles.height = "1.45fr"
            self.query_one("#panel-gpu").styles.width = "1.7fr"
            self.query_one("#panel-gpu-stats").styles.width = "1.3fr"
            self.query_one("#panel-gpuprocs").styles.height = "1.55fr"
        elif self.view == "agents":
            if width >= 120:
                self.query_one("#agents-row-top").styles.layout = "horizontal"
                self.query_one("#agents-row-bottom").styles.layout = "horizontal"
                self.query_one("#agents-row-top").styles.height = "1fr"
                self.query_one("#agents-row-bottom").styles.height = "1fr"
                self.query_one("#panel-agents").styles.width = "1.2fr"
                self.query_one("#panel-tokens").styles.width = "1fr"
                self.query_one("#panel-detail").styles.width = "2fr"
                self.query_one("#panel-quota").styles.width = "1fr"
                self.query_one("#panel-ports").styles.width = "1.2fr"
        self._update_active_panel()
        self._update_footer()

    def _update_active_panel(self) -> None:
        palette = get_palette()
        for panel in self.query(".panel"):
            panel.remove_class("active")
            with suppress(Exception):
                panel.styles.border = ("round", palette.border)
        ids: list[str] = []
        ids = {
            "overview": {
                "agents": ["panel-agents"],
                "gpuprocs": ["panel-gpuprocs"],
                "procs": ["panel-procs"],
            }.get(self.focus_area, []),
            "cpu": ["panel-cpu"],
            "gpu": ["panel-gpuprocs"],
            "net": ["panel-net"],
            "memory": ["panel-mem", "panel-disk", "panel-filesystems"],
            "procs": ["panel-procs"],
            "agents": ["panel-agents"],
        }.get(self.view, [])
        for panel_id in ids:
            try:
                panel = self.query_one(f"#{panel_id}")
                panel.add_class("active")
                panel.styles.border = ("round", palette.border_focus)
            except Exception:
                pass

    def _apply_theme(self) -> None:
        """Push the active palette into the pieces defined by static CSS."""
        palette = get_palette()
        with suppress(Exception):
            self.screen.styles.background = palette.bg
        for selector in ("#header", "#footer"):
            with suppress(Exception):
                self.query_one(selector).styles.background = palette.bg
        for panel in self.query(".panel"):
            with suppress(Exception):
                panel.styles.background = palette.panel
        self._update_active_panel()

    def _set_view(self, view: str) -> None:
        self.view = view
        self.narrow = self.size.width < 116
        self.focus_area = {
            "overview": "agents",
            "cpu": "none",
            "gpu": "gpuprocs",
            "procs": "procs",
            "agents": "agents",
        }.get(view, "none")
        self._apply_layout(self.size.width)

    def _cycle_focus(self, backwards: bool = False) -> None:
        order = {
            "overview": ["agents", "gpuprocs", "procs"],
            "gpu": ["gpuprocs"],
            "procs": ["procs"],
            "agents": ["agents"],
        }.get(self.view, [])
        if not order:
            return
        idx = order.index(self.focus_area) if self.focus_area in order else 0
        self.focus_area = order[(idx + (-1 if backwards else 1)) % len(order)]
        self._apply_layout(self.size.width)

    def cycle_proc_sort(self, column: str) -> None:
        """Cycle one Procs column through ascending, descending, and unsorted."""
        if self.proc_sort_column != column:
            self.proc_sort_column = column
            self.proc_sort_direction = 1
            self.proc_sort_default = False
        elif self.proc_sort_default:
            # The initial CPU-descending sort is a default. The first click on
            # CPU changes it to ascending instead of immediately clearing it.
            self.proc_sort_direction = 1
            self.proc_sort_default = False
        elif self.proc_sort_direction > 0:
            self.proc_sort_direction = -1
        else:
            self.proc_sort_column = None
            self.proc_sort_direction = 0
            self.proc_sort_default = False
        self.refresh_ui()

    # -- selection -------------------------------------------------------

    def _snapshot(self) -> Snapshot:
        return self.source.snapshot() if self.source else Snapshot()

    def _selected_session(self, snap: Snapshot) -> Optional[AgentSession]:
        if not snap.agents:
            return None
        self.agent_index = min(max(0, self.agent_index), len(snap.agents) - 1)
        return snap.agents[self.agent_index]

    def _move(self, delta: int) -> None:
        snap = self._snapshot()
        if self.view == "cpu":
            self.query_one("#cpu-top-procs", CpuTopProcesses).scroll_by(delta)
            return
        if self.view in ("net", "memory"):
            return
        area = self.focus_area
        if area == "agents" and snap.agents:
            self.agent_index = (self.agent_index + delta) % len(snap.agents)
            session = snap.agents[self.agent_index]
            if self.source:
                self.source.set_selected(session.session_id)
            self._last_agent_key = (session.agent, session.session_id)
        elif area == "gpuprocs" and self._displayed_gpu_processes:
            self.gpu_proc_index = (self.gpu_proc_index + delta) % len(self._displayed_gpu_processes)
            pid = self._displayed_gpu_processes[self.gpu_proc_index].pid
            self.proc_index = next(
                (i for i, proc in enumerate(self._displayed_procs) if proc.pid == pid),
                self.proc_index,
            )
        elif area == "procs" and self._displayed_procs:
            self.proc_index = (self.proc_index + delta) % len(self._displayed_procs)
            pid = self._displayed_procs[self.proc_index].pid
            self.gpu_proc_index = next(
                (i for i, proc in enumerate(self._displayed_gpu_processes) if proc.pid == pid),
                -1,
            )
        self.refresh_ui()

    # -- actions ---------------------------------------------------------

    def _kill_target(self) -> Optional[tuple[str, int, str]]:
        if self.demo:
            self._message("Demo mode: process killing is disabled")
            return None
        if self.timeline_offset:
            self._message("Return to the live sample before killing a process")
            return None
        snap = self._snapshot()
        if self.view in ("cpu", "net", "memory"):
            return None
        if self.focus_area == "agents" and snap.agents:
            s = snap.agents[self.agent_index]
            if s.agent == "opencode" or not s.pid:
                self._message("Refusing to kill the shared OpenCode server; kill a child port instead")
                return None
            if s.pid in (1, os.getpid()):
                return None
            return (f"agent {s.agent_label}", s.pid, f"{s.agent} {s.project}")
        if self.focus_area == "gpuprocs" and 0 <= self.gpu_proc_index < len(self._displayed_gpu_processes):
            p = self._displayed_gpu_processes[self.gpu_proc_index]
            if p.pid in (1, os.getpid()):
                return None
            return ("gpu process", p.pid, p.name)
        if self.focus_area == "procs" and 0 <= self.proc_index < len(self._displayed_procs):
            p = self._displayed_procs[self.proc_index]
            if p.pid in (1, os.getpid()):
                return None
            return ("process", p.pid, p.name)
        return None

    def _request_kill(self) -> None:
        target = self._kill_target()
        if not target:
            return
        kind, pid, name = target
        self.confirm_kill = (kind, pid, name)
        self._update_footer()

    def _do_kill(self) -> None:
        if self.demo:
            self.confirm_kill = None
            self._message("Demo mode: process killing is disabled")
            return
        if not self.confirm_kill:
            return
        _kind, pid, name = self.confirm_kill
        self.confirm_kill = None
        try:
            import psutil

            proc = psutil.Process(pid)
            proc.terminate()
            self._message(f"Sent SIGTERM to {name} (pid {pid})")
        except Exception as exc:
            self._message(f"Kill failed: {exc}")
        if self.source:
            self.source.force_refresh()
        self._update_footer()

    def _message(self, text: str) -> None:
        self.status_message = text
        self.status_until = time.time() + 6
        self._update_footer()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def _toggle_scope(self) -> None:
        self.scope = "all" if self.scope == "recent" else "recent"
        if self.source:
            self.source.set_scope(self.scope)
        self._message(f"Agent scope: {'last 24 h' if self.scope == 'all' else 'last 30 min'}")
        self.refresh_ui()

    def _cycle_theme(self) -> None:
        palette = cycle_palette()
        self._apply_theme()
        self._message(f"Theme: {palette.name}")
        self.refresh_ui()

    def _update_footer(self) -> None:
        try:
            footer = self.query_one("#footer", Footer)
        except Exception:
            return
        confirm = ""
        if self.confirm_kill:
            kind, pid, name = self.confirm_kill
            confirm = f"Kill {kind} '{clip(name, 32)}' pid {pid}?  y / n"
        message = self.status_message if time.time() < self.status_until else ""
        if not message:
            self.status_message = ""
        footer.set_state(
            message=message,
            confirm=confirm,
            narrow=self.narrow,
            paused=self.paused,
            view=self.view,
            insight_summary=(
                f"⚠ {len(self.current_insights)} · {clip(self.current_insights[0].title, 20)}"
                if self.current_insights
                else ""
            ),
        )

    # -- key handling ----------------------------------------------------

    async def on_key(self, event) -> None:
        key = event.key
        # A modal screen (help) owns the keyboard while it is open.
        if len(self.screen_stack) > 1:
            return
        if self.confirm_kill:
            if key in ("y", "Y"):
                self._do_kill()
            elif key in ("n", "N", "escape"):
                self.confirm_kill = None
                self._message("Cancelled")
            event.stop()
            return
        if self.proc_filter_active:
            character = getattr(event, "character", None)
            if key == "escape":
                self.proc_filter = ""
                self.proc_filter_active = False
            elif key == "enter":
                self.proc_filter_active = False
            elif key == "backspace":
                self.proc_filter = self.proc_filter[:-1]
            elif key == "ctrl+u":
                self.proc_filter = ""
            elif character and character.isprintable():
                self.proc_filter += character
            self.refresh_ui()
            event.stop()
            return
        if key in ("q", "ctrl+c"):
            self.exit()
            event.stop()
            return
        if key in ("?", "question_mark"):
            # Defer the push so the same key press isn't delivered to the
            # freshly opened help screen (which closes on any key).
            self.call_after_refresh(self.action_help)
            event.stop()
            return
        if key == "tab":
            self._cycle_focus(False)
            event.stop()
            return
        if key == "shift+tab":
            self._cycle_focus(True)
            event.stop()
            return
        if key in ("/", "slash") and self.view == "procs":
            if self.proc_filter_mode != "column":
                self.proc_filter = ""
            self.proc_filter_mode = "column"
            self.proc_filter_active = True
            self.refresh_ui()
            event.stop()
            return
        if key in ("f", "F") and self.view == "procs":
            if self.proc_filter_mode != "global":
                self.proc_filter = ""
            self.proc_filter_mode = "global"
            self.proc_filter_active = True
            self.refresh_ui()
            event.stop()
            return
        if key in ("0", "1", "2", "3", "4", "5", "6"):
            self._set_view(
                {
                    "0": "overview",
                    "1": "cpu",
                    "2": "gpu",
                    "3": "net",
                    "4": "memory",
                    "5": "procs",
                    "6": "agents",
                }[key]
            )
            event.stop()
            return
        if key in ("left", "right", "home", "end") and self.timeline:
            if key == "left":
                self.timeline_offset = min(max(0, len(self.timeline) - 1), self.timeline_offset + 1)
            elif key == "right":
                self.timeline_offset = max(0, self.timeline_offset - 1)
            elif key == "home":
                self.timeline_offset = max(0, len(self.timeline) - 1)
            else:
                self.timeline_offset = 0
            self.refresh_ui()
            event.stop()
            return
        if key in ("j", "down"):
            self._move(1)
            event.stop()
            return
        if key in ("k", "up"):
            self._move(-1)
            event.stop()
            return
        if key in ("a", "A"):
            self._toggle_scope()
            event.stop()
            return
        if key in ("p", "P"):
            self.paused = not self.paused
            self.source.paused = self.paused
            self._message("Paused" if self.paused else "Resumed")
            self._update_footer()
            event.stop()
            return
        if key in ("r", "R"):
            if self.source:
                self.source.force_refresh()
            self._message("Refreshing…")
            event.stop()
            return
        if key in ("t", "T"):
            self._cycle_theme()
            event.stop()
            return
        if key in ("x", "X"):
            self._request_kill()
            event.stop()
            return
        if key == "escape":
            self.confirm_kill = None
            self._update_footer()
            event.stop()

    # -- refresh ---------------------------------------------------------

    def refresh_ui(self) -> None:
        if not self.source:
            return
        live_snapshot = self._snapshot()
        if not self.timeline or live_snapshot.ts != self.timeline[-1].ts:
            self.timeline.append(copy.deepcopy(live_snapshot))
        self.timeline_offset = min(self.timeline_offset, max(0, len(self.timeline) - 1))
        selected_index = max(0, len(self.timeline) - 1 - self.timeline_offset)
        snap = self.timeline[selected_index] if self.timeline else live_snapshot
        self._last_snapshot = live_snapshot
        palette = get_palette()
        previous_gpu_pid = (
            self._displayed_gpu_processes[self.gpu_proc_index].pid
            if 0 <= self.gpu_proc_index < len(self._displayed_gpu_processes)
            else None
        )
        previous_proc_pid = (
            self._displayed_procs[self.proc_index].pid
            if 0 <= self.proc_index < len(self._displayed_procs)
            else None
        )

        # Keep the selected session stable across refreshes.
        if snap.agents:
            if self._last_agent_key:
                for idx, session in enumerate(snap.agents):
                    if (session.agent, session.session_id) == self._last_agent_key:
                        self.agent_index = idx
                        break
                else:
                    self.agent_index = min(self.agent_index, len(snap.agents) - 1)
            else:
                self.agent_index = min(self.agent_index, len(snap.agents) - 1)
                s = snap.agents[self.agent_index]
                self._last_agent_key = (s.agent, s.session_id)
            if self.source and self.timeline_offset == 0:
                self.source.set_selected(snap.agents[self.agent_index].session_id)
        else:
            self.agent_index = 0

        # Header / footer.
        with suppress(Exception):
            self.query_one("#header", Header).update_data(live_snapshot, self.view)
        self.current_insights = diagnose(live_snapshot, list(self.timeline))
        self._update_footer()

        # CPU.
        cpu = snap.cpu
        cpu_widget = self.query_one("#cpu-stats", OneLine)
        cpu_right = Text()
        cpu_right.append(f"CPU {cpu.percent:4.0f}%", style=f"bold {palette.gradient(cpu.percent)}")
        cpu_right.append(f"  USER {cpu.user_percent:4.0f}%", style=f"bold {palette.cyan}")
        if cpu.temp_c is not None:
            cpu_right.append(f"  {cpu.temp_c:.0f}°C", style=palette.orange)
        cpu_right.append(
            f"  load {cpu.load1:.2f} {cpu.load5:.2f} {cpu.load15:.2f}",
            style=palette.dim,
        )
        stats_width = cpu_widget.size.width or 72
        cpu_name = Text(
            clip(cpu.model_name, max(1, stats_width - cpu_right.cell_len - 1)), style=f"bold {palette.text}"
        )
        cpu_stats = Text()
        cpu_stats.append_text(cpu_name)
        cpu_stats.append(" " * max(1, stats_width - cpu_name.cell_len - cpu_right.cell_len))
        cpu_stats.append_text(cpu_right)
        cpu_widget.update_text(cpu_stats)
        self.query_one("#cpu-graph", HistoryGraph).update_data(
            cpu.user_history, maximum=100, secondary=cpu.history
        )
        self.query_one("#cpu-cores", CoreBars).update_data(
            cpu.per_core,
            cpu.core_temps,
            cpu.core_temp_source,
            cpu.core_history,
            detailed=self.view == "cpu",
        )
        self.query_one("#cpu-top-procs", CpuTopProcesses).update_data(snap.procs)
        self.query_one("#panel-cpu").border_subtitle = f" {cpu.percent:.0f}% "

        # Memory.
        mem = snap.mem
        mem_stats = Text()
        mem_stats.append(f"{mem.percent:5.1f}%", style=f"bold {palette.gradient(mem.percent)}")
        mem_stats.append(f"  {human_bytes(mem.used)} / {human_bytes(mem.total)}", style=palette.text)
        swap_text = (
            f"SWAP {human_bytes(mem.swap_used)}/{human_bytes(mem.swap_total)}"
            if mem.swap_total
            else "SWAP off"
        )
        mem_stats.append(f"  {swap_text}", style=palette.magenta)
        mem_stats.append(f"  avail {human_bytes(mem.available)}", style=palette.dim)
        mem_stats.append(f"  cached {human_bytes(mem.cached)}", style=palette.blue)
        self.query_one("#mem-stats", OneLine).update_text(mem_stats)
        mem_extra = Text()
        mem_extra.append(f"USED {human_bytes(mem.used)}", style=palette.text)
        mem_extra.append(f"   FREE {human_bytes(mem.available)}", style=palette.green)
        mem_extra.append(f"   CACHE {human_bytes(mem.cached)}", style=palette.blue)
        mem_extra.append(f"   BUFFERS {human_bytes(mem.buffers)}", style=palette.dim)
        swap_percent = f"{mem.swap_percent:.1f}%" if mem.swap_total else "off"
        mem_extra.append(
            f"   SWAP {human_bytes(mem.swap_used)}/{human_bytes(mem.swap_total)} {swap_percent}",
            style=palette.magenta,
        )
        self.query_one("#mem-extra", OneLine).update_text(mem_extra)
        self.query_one("#mem-graph", HistoryGraph).update_data(
            mem.history, maximum=100, secondary=mem.swap_history
        )
        self.query_one("#panel-mem").border_subtitle = f" {mem.percent:.0f}% "

        # Network.
        net = snap.net
        net_stats = Text()
        net_stats.append("▼ ", style=palette.green)
        net_stats.append(human_rate(net.rx_rate), style=palette.text)
        net_stats.append("  ▲ ", style=palette.cyan)
        net_stats.append(human_rate(net.tx_rate), style=palette.text)
        net_stats.append(
            f"  total {human_bytes(net.total_rx)} / {human_bytes(net.total_tx)}", style=palette.faint
        )
        self.query_one("#net-stats", OneLine).update_text(net_stats)
        recent_rx = smooth_series(net.rx_history[-60:] or [1])
        recent_tx = smooth_series(net.tx_history[-60:] or [1])
        max_rx = max(max(recent_rx), 1) * 1.1
        max_tx = max(max(recent_tx), 1) * 1.1
        self.query_one("#net-graph", HistoryGraph).update_data(
            net.tx_history,
            maximum=max_tx,
            secondary=net.rx_history,
            secondary_maximum=max_rx,
        )
        self.query_one("#net-details", NetDetailPanel).update_data(net)
        self.query_one("#hosted-sites", HostedSitesPanel).update_data(net.websites)
        self.query_one("#listening-ports", ListeningPortsPanel).update_data(net.listeners)
        self.query_one("#panel-net").border_subtitle = f" ↓{human_bytes(net.rx_rate)}/s "

        # Disk.
        disk = snap.disk
        self.query_one("#disk-panel", DiskPanel).update_data(disk, detailed=self.view == "memory")
        self.query_one("#filesystem-panel", FilesystemPanel).update_data(disk)
        self.query_one("#panel-disk").border_subtitle = f" {human_bytes(disk.read_rate + disk.write_rate)}/s "

        # GPU.
        self.query_one("#gpu-panel", GpuPanel).update_data(snap.gpu, focused=self.view == "gpu")
        self.query_one("#gpu-stats", GpuStatsPanel).update_data(snap.gpu)
        if snap.gpu.gpus:
            if self.view == "gpu":
                self.query_one("#panel-gpu").border_subtitle = ""
            elif len(snap.gpu.gpus) == 1:
                g = snap.gpu.gpus[0]
                self.query_one("#panel-gpu").border_subtitle = f" {g.util:.0f}% {g.temp_c or 0:.0f}°C "
            else:
                hottest = snap.gpu.hottest_temp_c
                temp_text = f" max {hottest:.0f}°C" if hottest is not None else ""
                self.query_one(
                    "#panel-gpu"
                ).border_subtitle = f" avg {snap.gpu.aggregate_util:.0f}% · VRAM {snap.gpu.aggregate_mem_percent:.0f}%{temp_text} "
        else:
            self.query_one("#panel-gpu").border_subtitle = " n/a "

        # Keep every GPU process selectable in both tables, even when it falls
        # outside the regular process collector's top-N display slice.
        gpu_processes = list(snap.gpu.processes)
        procs = list(snap.procs)
        proc_pids = {proc.pid for proc in procs}
        for gpu_proc in gpu_processes:
            if gpu_proc.pid in proc_pids:
                continue
            procs.append(
                ProcInfo(
                    pid=gpu_proc.pid,
                    name=gpu_proc.name,
                    user=gpu_proc.user,
                    cpu=gpu_proc.cpu_pct or 0.0,
                    mem_pct=gpu_proc.host_mem_pct or 0.0,
                    rss=gpu_proc.rss,
                    cmd=gpu_proc.cmd or gpu_proc.name,
                    create_time=(time.time() - gpu_proc.runtime_s) if gpu_proc.runtime_s is not None else 0.0,
                )
            )
            proc_pids.add(gpu_proc.pid)
        gpu_by_pid: dict[int, list] = {}
        for gpu_proc in gpu_processes:
            gpu_by_pid.setdefault(gpu_proc.pid, []).append(gpu_proc)
        for entries in gpu_by_pid.values():
            entries.sort(key=lambda item: item.gpu_index)

        def sort_value(proc: ProcInfo):
            entries = gpu_by_pid.get(proc.pid, [])
            if self.proc_sort_column == "pid":
                return proc.pid
            if self.proc_sort_column == "user":
                return proc.user.casefold()
            if self.proc_sort_column == "cpu":
                return proc.cpu, proc.mem_pct
            if self.proc_sort_column == "mem":
                return proc.mem_pct
            if self.proc_sort_column == "rss":
                return proc.rss
            if self.proc_sort_column == "gpu":
                return min((item.gpu_index for item in entries), default=-1)
            if self.proc_sort_column == "type":
                return ",".join(dict.fromkeys(item.kind for item in entries)).casefold()
            if self.proc_sort_column == "sm":
                return max((item.sm for item in entries if item.sm is not None), default=-1.0)
            if self.proc_sort_column == "gmbw":
                return max((item.gmbw_pct for item in entries if item.gmbw_pct is not None), default=-1.0)
            if self.proc_sort_column == "vram":
                return sum(item.vram or 0 for item in entries)
            if self.proc_sort_column == "time":
                return max((item.runtime_s for item in entries if item.runtime_s is not None), default=-1.0)
            return f"{proc.name} {proc.cmd}".casefold()

        if self.proc_sort_column and self.proc_sort_direction:
            procs.sort(key=sort_value, reverse=self.proc_sort_direction < 0)
        total_proc_rows = len(procs)

        def column_values(proc: ProcInfo) -> dict[str, str]:
            entries = gpu_by_pid.get(proc.pid, [])
            vram_bytes = sum(item.vram or 0 for item in entries)
            runtime_values = [item.runtime_s for item in entries if item.runtime_s is not None]
            return {
                "pid": str(proc.pid),
                "user": proc.user,
                "cpu": f"{proc.cpu:.1f}",
                "mem": f"{proc.mem_pct:.1f}",
                "rss": f"{human_bytes(proc.rss)} {proc.rss}",
                "gpu": ",".join(str(item.gpu_index) for item in entries) or "—",
                "type": ",".join(dict.fromkeys(item.kind for item in entries)) or "—",
                "sm": "/".join(str(item.sm) for item in entries if item.sm is not None) or "—",
                "gmbw": "/".join(str(item.gmbw_pct) for item in entries if item.gmbw_pct is not None) or "—",
                "vram": f"{human_bytes(vram_bytes)} {vram_bytes}" if vram_bytes else "—",
                "time": " ".join(str(int(value)) for value in runtime_values) or "—",
                "process": f"{proc.name} {proc.cmd}",
            }

        if self.proc_filter and self.proc_filter_mode == "global":
            query = self.proc_filter.casefold()
            procs = [proc for proc in procs if query in " ".join(column_values(proc).values()).casefold()]
        elif self.proc_filter and self.proc_filter_mode == "column":
            if "/" not in self.proc_filter:
                procs = []
            else:
                column_name, pattern = self.proc_filter.split("/", 1)
                aliases = {
                    "pid": "pid",
                    "user": "user",
                    "cpu": "cpu",
                    "cpu%": "cpu",
                    "mem": "mem",
                    "mem%": "mem",
                    "rss": "rss",
                    "gpu": "gpu",
                    "t": "type",
                    "type": "type",
                    "sm": "sm",
                    "sm%": "sm",
                    "gmbw": "gmbw",
                    "gmbw%": "gmbw",
                    "vram": "vram",
                    "time": "time",
                    "process": "process",
                    "name": "process",
                    "cmd": "process",
                    "command": "process",
                }
                column = aliases.get(column_name.strip().casefold())
                try:
                    expression = re.compile(pattern, re.IGNORECASE)
                except re.error:
                    expression = None
                if column is None or expression is None:
                    procs = []
                else:
                    procs = [proc for proc in procs if expression.search(column_values(proc)[column])]

        def index_for_pid(items, pid):
            if pid is None:
                return -1
            return next((i for i, item in enumerate(items) if item.pid == pid), -1)

        if self.focus_area == "gpuprocs" and previous_gpu_pid is not None:
            preferred_pid = previous_gpu_pid
        elif self.focus_area == "procs" and previous_proc_pid is not None:
            preferred_pid = previous_proc_pid
        else:
            preferred_pid = previous_proc_pid or previous_gpu_pid
        proc_match = index_for_pid(procs, preferred_pid)
        gpu_match = index_for_pid(gpu_processes, preferred_pid)
        if preferred_pid is None and procs:
            proc_match = min(self.proc_index, len(procs) - 1)
            preferred_pid = procs[proc_match].pid
            gpu_match = index_for_pid(gpu_processes, preferred_pid)
        self.proc_index = (
            proc_match if proc_match >= 0 else min(max(self.proc_index, 0), max(0, len(procs) - 1))
        )
        self.gpu_proc_index = gpu_match
        self._displayed_gpu_processes = gpu_processes
        self._displayed_procs = procs
        self.query_one("#gpu-procs", GpuProcTable).update_data(
            gpu_processes,
            self.gpu_proc_index,
            snap.gpu.gpus,
            [
                session.project
                for session in snap.agents
                if session.status in ("thinking", "executing", "waiting")
            ],
        )
        self.query_one("#procs", ProcTable).update_data(
            procs,
            self.proc_index,
            self.proc_sort_column,
            self.proc_sort_direction,
            gpu_processes,
            show_gpu_columns=self.view != "overview",
        )
        proc_summary = Text()
        proc_summary.append(f"{len(procs)} matches", style=f"bold {palette.accent}")
        proc_summary.append(f" / {total_proc_rows} rows   ", style=palette.dim)
        proc_summary.append(f"{snap.thread_count} threads   ", style=palette.dim)
        proc_summary.append(f"{len(gpu_processes)} GPU process entries   ", style=palette.cyan)
        if self.proc_sort_column:
            direction = "ASC" if self.proc_sort_direction > 0 else "DESC"
            proc_summary.append(f"sorted {self.proc_sort_column.upper()} {direction} · ", style=palette.faint)
        else:
            proc_summary.append("unsorted · ", style=palette.faint)
        proc_summary.append("CPU% is whole-host capacity", style=palette.faint)
        if self.proc_filter_active:
            prefix = "f:" if self.proc_filter_mode == "global" else "/"
            proc_summary.append(
                f"   {prefix}{self.proc_filter}▏ Enter apply · Esc clear", style=palette.yellow
            )
        elif self.proc_filter:
            prefix = "f " if self.proc_filter_mode == "global" else "/"
            proc_summary.append(
                f"   {prefix}{self.proc_filter} · f search / regex edit · Esc clear", style=palette.cyan
            )
        else:
            proc_summary.append("   f search · / COLUMN/REGEX · click headers to sort", style=palette.faint)
        self.query_one("#proc-summary", OneLine).update_text(proc_summary)
        sort_subtitle = (
            f" {self.proc_sort_column.upper()} {'ASC' if self.proc_sort_direction > 0 else 'DESC'} "
            if self.proc_sort_column
            else " UNSORTED "
        )
        self.query_one("#panel-procs").border_subtitle = sort_subtitle

        # Agents.
        self.query_one("#agents", AgentTable).update_data(
            snap.agents, self.agent_index, snap.token_rate, snap.agent_scope
        )
        self.query_one(
            "#panel-agents"
        ).border_subtitle = f" {sum(1 for s in snap.agents if s.is_active)}/{len(snap.agents)} "
        selected = self._selected_session(snap)
        self.query_one("#tokens", TokenOverview).update_data(selected, snap.token_rate)
        token_title = " TOKENS "
        if selected:
            token_id = f"{selected.project}/{selected.session_id[:7]}"
            token_title = f" TOKENS ({truncate_middle(token_id, 28)}) "
        self.query_one("#panel-tokens").border_title = token_title
        self.query_one("#detail", SessionDetail).update_data(selected)
        if selected:
            self.query_one("#panel-detail").border_subtitle = f" {selected.agent_label} "
        self.query_one("#quota", QuotaPanel).update_data(snap.quotas)
        self.query_one("#agent-ports", AgentPortsPanel).update_data(snap.agents)

        # Errors are surfaced in the footer message area, briefly.
        if snap.errors and not self.confirm_kill and time.time() >= self.status_until:
            self._message(snap.errors[-1])

"""Textual application: the btop-like unified dashboard."""

from __future__ import annotations

import os
import time
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
from .model import AgentSession, ProcInfo, Snapshot
from .sampler import Sampler
from .widgets import (
    AgentTable,
    CoreBars,
    DiskPanel,
    Footer,
    GpuPanel,
    GpuProcTable,
    Header,
    HistoryGraph,
    OneLine,
    ProcTable,
    QuotaPanel,
    SessionDetail,
    TokenOverview,
)

HELP_TEXT = """\
[b]omnitop[/b] — btop + nvitop + abtop in one view

[b]Navigation[/b]
  [cyan]j[/cyan] / [cyan]k[/cyan], arrows      move selection
  [cyan]tab[/cyan] / [cyan]shift+tab[/cyan]    cycle focused panel
  [cyan]1[/cyan] [cyan]2[/cyan] [cyan]3[/cyan]            system / gpu / agents view (narrow)
  [cyan]a[/cyan]                  toggle 30 min / 24 h agent scope
  [cyan]s[/cyan]                  sort processes by cpu / memory
  [cyan]p[/cyan]                  pause sampling
  [cyan]r[/cyan]                  force refresh
  [cyan]t[/cyan]                  cycle colour theme
  [cyan]?[/cyan]                  close this help
  [cyan]q[/cyan]                  quit

[b]Kill[/b]
  [cyan]x[/cyan]                  kill selected process or agent
  [cyan]y[/cyan] / [cyan]n[/cyan]              confirm / cancel
  OpenCode's shared server and omnitop itself are protected.

[b]Data sources[/b]
  CPU / memory / network / disk / processes ... psutil
  GPU stats and per-process SM% ... nvidia-smi + pmon
  Claude Code / Codex / OpenCode ... local transcripts + sqlite
  Rate limits ... abtop statusline cache (if installed)

All monitoring is read-only.  No API keys, no network calls.
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
            try:
                getattr(box, f"action_{action}")()
            except Exception:
                pass
            return
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class OmniTop(App):
    CSS = """
    Screen {
        background: #070b10;
        color: #d6e2ea;
    }
    #header {
        height: 2;
        padding: 0 1;
        background: #070b10;
    }
    #main {
        height: 1fr;
    }
    .col {
        height: 1fr;
        width: 1fr;
    }
    .panel {
        border: round #26414f;
        background: #0b1219;
        padding: 0 1;
        width: 1fr;
        height: 1fr;
    }
    .panel.active {
        border: round #3fd0c9;
    }
    .stat {
        height: 1;
    }
    #panel-cpu { height: 2.6fr; }
    #panel-mem { height: 1.4fr; }
    #panel-net { height: 1.15fr; }
    #panel-disk { height: 1.65fr; }
    #panel-gpu { height: 2.5fr; }
    #panel-gpuprocs { height: 1.3fr; }
    #panel-procs { height: 1.45fr; }
    #panel-agents { height: 1.85fr; }
    #panel-tokens { height: 2.0fr; }
    #panel-detail { height: 1.3fr; }
    #panel-quota { height: 1.0fr; }
    #cpu-content { height: 1fr; }
    #cpu-graph { width: 1fr; height: 1.5fr; min-height: 2; }
    #cpu-cores { width: 1fr; height: 1fr; min-height: 3; }
    #footer {
        height: 1;
        padding: 0 1;
        background: #0b1219;
        color: #7d93a3;
    }
    HelpScreen {
        align: center middle;
        background: #070b10 70%;
    }
    #help-box {
        width: 90%;
        max-width: 88;
        height: 90%;
        max-height: 40;
        border: round #3fd0c9;
        background: #0b1219;
        padding: 1 2;
        color: #d6e2ea;
        overflow-y: auto;
    }
    """

    def __init__(self, scope: str = "recent", theme: str = "btop", demo: bool = False) -> None:
        super().__init__()
        set_palette(theme)
        self.demo = demo
        self.source = None
        self.narrow = False
        self.view = "system"
        self.focus_area = "agents"
        self.agent_index = 0
        self.gpu_proc_index = 0
        self.proc_index = 0
        self._displayed_gpu_processes = []
        self._displayed_procs: list[ProcInfo] = []
        self.proc_sort = "cpu"
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
                        yield CoreBars(id="cpu-cores")
                        yield HistoryGraph(
                            maximum=100,
                            color_mode="value",
                            primary_role="green",
                            secondary_role="cyan",
                            primary_label="USER CPU",
                            secondary_label="TOTAL CPU",
                            id="cpu-graph",
                        )
                with Vertical(id="panel-mem", classes="panel"):
                    yield OneLine(id="mem-stats")
                    yield HistoryGraph(
                        maximum=100,
                        color_mode="value",
                        primary_role="magenta",
                        secondary_role="cyan",
                        primary_label="RAM USED",
                        secondary_label="SWAP USED",
                        id="mem-graph",
                    )
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
                with Vertical(id="panel-disk", classes="panel"):
                    yield DiskPanel(id="disk-panel")
            with Vertical(id="col-mid", classes="col"):
                with Vertical(id="panel-gpu", classes="panel"):
                    yield GpuPanel(id="gpu-panel")
                with Vertical(id="panel-gpuprocs", classes="panel"):
                    yield GpuProcTable(id="gpu-procs")
                with Vertical(id="panel-procs", classes="panel"):
                    yield ProcTable(id="procs")
            with Vertical(id="col-right", classes="col"):
                with Vertical(id="panel-agents", classes="panel"):
                    yield AgentTable(id="agents")
                with Vertical(id="panel-tokens", classes="panel"):
                    yield TokenOverview(id="tokens")
                with Vertical(id="panel-detail", classes="panel"):
                    yield SessionDetail(id="detail")
                with Vertical(id="panel-quota", classes="panel"):
                    yield QuotaPanel(id="quota")
        yield Footer(id="footer")

    def on_mount(self) -> None:
        titles = {
            "panel-cpu": " CPU ",
            "panel-mem": " MEMORY ",
            "panel-net": " NETWORK ",
            "panel-disk": " DISK I/O ",
            "panel-gpu": " GPU ",
            "panel-gpuprocs": " GPU PROCESSES ",
            "panel-procs": " PROCESSES ",
            "panel-agents": " AI AGENTS ",
            "panel-detail": " SESSION ",
            "panel-tokens": " TOKENS ",
            "panel-quota": " RATE LIMITS ",
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
        if self.narrow:
            for name, col in cols.items():
                col.styles.display = "block" if name == self.view else "none"
            if self.view == "system":
                self.focus_area = "none"
            elif self.view == "gpu":
                self.focus_area = "gpuprocs"
            else:
                self.focus_area = "agents"
        else:
            for col in cols.values():
                col.styles.display = "block"
        self._update_active_panel()
        self._update_footer()

    def _update_active_panel(self) -> None:
        palette = get_palette()
        for panel in self.query(".panel"):
            panel.remove_class("active")
            try:
                panel.styles.border = ("round", palette.border)
            except Exception:
                pass
        ids: list[str] = []
        if self.narrow:
            ids = {
                "system": ["panel-cpu", "panel-mem", "panel-net", "panel-disk"],
                "gpu": ["panel-gpu", "panel-gpuprocs", "panel-procs"],
                "agents": ["panel-agents", "panel-tokens", "panel-detail", "panel-quota"],
            }.get(self.view, [])
        else:
            ids = {
                "agents": ["panel-agents"],
                "gpuprocs": ["panel-gpuprocs"],
                "procs": ["panel-procs"],
            }.get(self.focus_area, [])
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
        try:
            self.screen.styles.background = palette.bg
        except Exception:
            pass
        for selector in ("#header", "#footer"):
            try:
                self.query_one(selector).styles.background = palette.bg
            except Exception:
                pass
        for panel in self.query(".panel"):
            try:
                panel.styles.background = palette.panel
            except Exception:
                pass
        self._update_active_panel()

    def _set_view(self, view: str) -> None:
        self.view = view
        if self.narrow:
            self._apply_layout(self.size.width)
        else:
            self.focus_area = {"system": "none", "gpu": "gpuprocs", "agents": "agents"}.get(view, self.focus_area)
            self._update_active_panel()
            self._update_footer()

    def _cycle_focus(self, backwards: bool = False) -> None:
        if self.narrow:
            order = ["system", "gpu", "agents"]
            idx = order.index(self.view)
            self._set_view(order[(idx + (-1 if backwards else 1)) % len(order)])
            return
        order = ["agents", "gpuprocs", "procs"]
        idx = order.index(self.focus_area) if self.focus_area in order else 0
        self.focus_area = order[(idx + (-1 if backwards else 1)) % len(order)]
        self._update_active_panel()
        self._update_footer()

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
        if self.narrow and self.view == "system":
            return
        area = self.focus_area
        if self.narrow and self.view == "gpu":
            area = "gpuprocs"
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
        snap = self._snapshot()
        if self.narrow and self.view == "system":
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
        if key in ("1", "2", "3") and self.narrow:
            self._set_view({"1": "system", "2": "gpu", "3": "agents"}[key])
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
        if key in ("s", "S"):
            self.proc_sort = "mem" if self.proc_sort == "cpu" else "cpu"
            self._message(f"Process sort: {self.proc_sort.upper()}")
            self.refresh_ui()
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
        snap = self._snapshot()
        self._last_snapshot = snap
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
            if self.source:
                self.source.set_selected(snap.agents[self.agent_index].session_id)
        else:
            self.agent_index = 0

        # Header / footer.
        try:
            self.query_one("#header", Header).update_data(snap)
        except Exception:
            pass
        self._update_footer()

        # CPU.
        cpu = snap.cpu
        cpu_widget = self.query_one("#cpu-stats", OneLine)
        cpu_right = Text()
        cpu_right.append(f"CPU {cpu.percent:4.0f}%", style=f"bold {palette.gradient(cpu.percent)}")
        if cpu.temp_c is not None:
            cpu_right.append(f"  {cpu.temp_c:.0f}°C", style=palette.orange)
        cpu_right.append(
            f"  load {cpu.load1:.2f} {cpu.load5:.2f} {cpu.load15:.2f}",
            style=palette.dim,
        )
        stats_width = cpu_widget.size.width or 72
        cpu_name = Text(clip(cpu.model_name, max(1, stats_width - cpu_right.cell_len - 1)),
                        style=f"bold {palette.text}")
        cpu_stats = Text()
        cpu_stats.append_text(cpu_name)
        cpu_stats.append(" " * max(1, stats_width - cpu_name.cell_len - cpu_right.cell_len))
        cpu_stats.append_text(cpu_right)
        cpu_widget.update_text(cpu_stats)
        self.query_one("#cpu-graph", HistoryGraph).update_data(
            cpu.user_history, maximum=100, secondary=cpu.history
        )
        self.query_one("#cpu-cores", CoreBars).update_data(
            cpu.per_core, cpu.core_temps, cpu.core_temp_source
        )
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
        net_stats.append(f"  total {human_bytes(net.total_rx)} / {human_bytes(net.total_tx)}", style=palette.faint)
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
        self.query_one("#panel-net").border_subtitle = f" ↓{human_bytes(net.rx_rate)}/s "

        # Disk.
        disk = snap.disk
        self.query_one("#disk-panel", DiskPanel).update_data(disk)
        self.query_one("#panel-disk").border_subtitle = f" {human_bytes(disk.read_rate + disk.write_rate)}/s "

        # GPU.
        self.query_one("#gpu-panel", GpuPanel).update_data(snap.gpu)
        if snap.gpu.gpus:
            g = snap.gpu.gpus[0]
            self.query_one("#panel-gpu").border_subtitle = f" {g.util:.0f}% {g.temp_c or 0:.0f}°C "
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
                    create_time=(time.time() - gpu_proc.runtime_s)
                    if gpu_proc.runtime_s is not None
                    else 0.0,
                )
            )
            proc_pids.add(gpu_proc.pid)
        if self.proc_sort == "mem":
            procs.sort(key=lambda p: (p.mem_pct, p.rss), reverse=True)
        else:
            procs.sort(key=lambda p: (p.cpu, p.rss), reverse=True)

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
        self.proc_index = proc_match if proc_match >= 0 else min(max(self.proc_index, 0), max(0, len(procs) - 1))
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
        self.query_one("#procs", ProcTable).update_data(procs, self.proc_index, self.proc_sort)
        self.query_one("#panel-procs").border_subtitle = f" {self.proc_sort.upper()} "

        # Agents.
        self.query_one("#agents", AgentTable).update_data(
            snap.agents, self.agent_index, snap.token_rate, snap.agent_scope
        )
        self.query_one("#panel-agents").border_subtitle = (
            f" {sum(1 for s in snap.agents if s.is_active)}/{len(snap.agents)} "
        )
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

        # Errors are surfaced in the footer message area, briefly.
        if snap.errors and not self.confirm_kill and time.time() >= self.status_until:
            self._message(snap.errors[-1])

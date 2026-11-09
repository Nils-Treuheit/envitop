"""Background sampling loop used by the TUI (and the one-shot CLI)."""

from __future__ import annotations

import threading
import time
from typing import Optional

from .collectors.agents import AgentCollector
from .collectors.gpu import GpuCollector
from .collectors.system import SystemCollector
from .model import AgentSession, GpuState, ProcInfo, RateLimit, Snapshot


class Sampler(threading.Thread):
    def __init__(self, scope: str = "recent") -> None:
        super().__init__(name="envitop-sampler", daemon=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force = threading.Event()
        self.system = SystemCollector()
        self.gpu = GpuCollector()
        self._gpu_state = GpuState()
        self._gpu_lock = threading.Lock()
        self._gpu_ready = threading.Event()
        self._gpu_empty_process_samples = 0
        self._gpu_unavailable_samples = 0
        self._latest_proc_index: dict[int, ProcInfo] = {}
        self.agents = AgentCollector()
        self.scope = scope
        self.selected_id = ""
        self.paused = False
        self._snapshot = Snapshot(
            hostname=self.system.hostname,
            username=self.system.username,
            os_name=self.system.os_name,
            kernel=self.system.kernel,
        )
        self._prev_tokens: Optional[int] = None
        self._prev_token_time = 0.0
        self._sessions: list[AgentSession] = []
        self._quotas: list[RateLimit] = []
        self._last_agents = 0.0
        self._token_rate = 0.0

    # -- control ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        self._gpu_ready.set()

    def force_refresh(self) -> None:
        self._force.set()

    def set_scope(self, scope: str) -> None:
        self.scope = scope
        self.force_refresh()

    def set_selected(self, session_id: str) -> None:
        if session_id != self.selected_id:
            self.selected_id = session_id
            self._force.set()

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def session_by_id(self, session_id: str) -> Optional[AgentSession]:
        for session in self._sessions:
            if session.session_id == session_id:
                return session
        return None

    # -- thread ----------------------------------------------------------

    def run(self) -> None:
        gpu_thread = threading.Thread(target=self._gpu_loop, name="envitop-gpu-sampler", daemon=True)
        gpu_thread.start()
        while not self._stop.is_set():
            if self.paused:
                time.sleep(0.4)
                continue
            started = time.monotonic()
            errors: list[str] = []
            try:
                cpu, mem, net, disk, procs, index = self.system.sample()
            except Exception as exc:  # keep the TUI alive on transient /proc races
                errors.append(f"system: {exc}")
                time.sleep(1.0)
                continue
            with self._gpu_lock:
                self._latest_proc_index = index
            self._gpu_ready.set()

            now = time.monotonic()
            with self._lock:
                gpu_state = self._gpu_state
                # Publish system readings before slow agent history parsing.
                self._snapshot = Snapshot(
                    ts=time.time(),
                    hostname=self.system.hostname,
                    username=self.system.username,
                    uptime=self.system.uptime(),
                    os_name=self.system.os_name,
                    kernel=self.system.kernel,
                    cpu=cpu,
                    mem=mem,
                    net=net,
                    disk=disk,
                    gpu=gpu_state,
                    procs=procs,
                    task_count=len(index),
                    thread_count=sum(p.threads for p in index.values()),
                    agents=list(self._sessions),
                    quotas=list(self._quotas),
                    token_rate=self._token_rate,
                    agent_scope=self.scope,
                    errors=errors,
                )

            force = self._force.is_set()
            agents_refreshed = False
            if force or now - self._last_agents >= 2.5:
                self._force.clear()
                self._last_agents = now
                try:
                    self._sessions, self._quotas = self.agents.collect(
                        index, self.scope, self.selected_id
                    )
                    agents_refreshed = True
                except Exception as exc:
                    errors.append(f"agents: {exc}")
                    self._sessions = self._sessions or []
            # Token rate is based on non-cache active tokens, like abtop.
            total = sum(s.active_tokens for s in self._sessions)
            token_rate = self._token_rate
            if agents_refreshed and self._prev_tokens is not None:
                dt = now - self._prev_token_time
                delta = total - self._prev_tokens
                if dt > 0 and delta >= 0:
                    token_rate = delta / dt
                elif delta < 0:
                    token_rate = 0.0
            if agents_refreshed:
                self._prev_tokens = total
                self._prev_token_time = now
                self._token_rate = token_rate

            if agents_refreshed or errors:
                with self._lock:
                    self._snapshot.agents = list(self._sessions)
                    self._snapshot.quotas = list(self._quotas)
                    self._snapshot.token_rate = self._token_rate
                    self._snapshot.errors = errors

            elapsed = time.monotonic() - started
            self._stop.wait(max(0.15, 1.0 - elapsed))

    def _gpu_loop(self) -> None:
        self._gpu_ready.wait()
        while not self._stop.is_set():
            if self.paused:
                self._stop.wait(0.4)
                continue
            started = time.monotonic()
            with self._gpu_lock:
                proc_index = self._latest_proc_index
            try:
                state = self.gpu.sample(proc_index, include_processes=False)
            except Exception as exc:
                state = GpuState(available=False, error=str(exc))
            with self._lock:
                previous_state = self._gpu_state
                # Keep the last complete process table while pmon is sampling.
                # Publishing an empty intermediate list makes the panel flash.
                if state.available:
                    state.processes = list(previous_state.processes)
                    self._gpu_unavailable_samples = 0
                elif previous_state.available:
                    self._gpu_unavailable_samples += 1
                    if self._gpu_unavailable_samples < 2:
                        state = previous_state
                    else:
                        self._gpu_unavailable_samples = 0
                self._gpu_state = state
                self._snapshot.gpu = state
            if state.available:
                try:
                    processes = self.gpu.sample_processes(proc_index, state.gpus)
                    with self._lock:
                        previous_processes = list(self._gpu_state.processes)
                        if processes is None:
                            processes = previous_processes
                        elif not processes and previous_processes:
                            self._gpu_empty_process_samples += 1
                            if self._gpu_empty_process_samples < 2:
                                processes = previous_processes
                            else:
                                self._gpu_empty_process_samples = 0
                        else:
                            self._gpu_empty_process_samples = 0
                    driver = state.driver or self.gpu.refresh_driver()
                    cuda = state.cuda or self.gpu.refresh_cuda()
                    detailed = GpuState(
                        available=state.available,
                        error=state.error,
                        gpus=state.gpus,
                        processes=processes,
                        driver=driver,
                        cuda=cuda,
                    )
                    with self._lock:
                        self._gpu_state = detailed
                        self._snapshot.gpu = detailed
                except Exception:
                    pass
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.15, 1.0 - elapsed))

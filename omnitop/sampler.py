"""Background sampling loop used by the TUI (and the one-shot CLI)."""

from __future__ import annotations

import threading
import time
from typing import Optional

from .collectors.agents import AgentCollector
from .collectors.gpu import GpuCollector
from .collectors.system import SystemCollector
from .model import AgentSession, RateLimit, Snapshot


class Sampler(threading.Thread):
    def __init__(self, scope: str = "recent") -> None:
        super().__init__(name="omnitop-sampler", daemon=True)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._force = threading.Event()
        self.system = SystemCollector()
        self.gpu = GpuCollector()
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
            try:
                gpu_state = self.gpu.sample(index)
            except Exception as exc:
                errors.append(f"gpu: {exc}")
                from .model import GpuState

                gpu_state = GpuState(available=False, error=str(exc))

            now = time.monotonic()
            force = self._force.is_set()
            if force or now - self._last_agents >= 2.5:
                self._force.clear()
                self._last_agents = now
                try:
                    self._sessions, self._quotas = self.agents.collect(
                        index, self.scope, self.selected_id
                    )
                except Exception as exc:
                    errors.append(f"agents: {exc}")
                    self._sessions = self._sessions or []
            # Token rate is based on non-cache active tokens, like abtop.
            total = sum(s.active_tokens for s in self._sessions)
            token_rate = 0.0
            if self._prev_tokens is not None:
                dt = now - self._prev_token_time
                delta = total - self._prev_tokens
                if dt > 0 and delta >= 0:
                    token_rate = delta / dt
                elif delta < 0:
                    token_rate = 0.0
            self._prev_tokens = total
            self._prev_token_time = now
            self._token_rate = token_rate

            snap = Snapshot(
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
                agents=list(self._sessions),
                quotas=list(self._quotas),
                token_rate=token_rate,
                agent_scope=self.scope,
                errors=errors,
            )
            with self._lock:
                self._snapshot = snap

            elapsed = time.monotonic() - started
            self._stop.wait(max(0.15, 1.0 - elapsed))

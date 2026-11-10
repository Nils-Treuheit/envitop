"""Small, evidence-backed read-only diagnostics derived from snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .model import Snapshot


@dataclass(frozen=True)
class Insight:
    severity: str
    title: str
    evidence: str
    tab: str


def diagnose(snap: Snapshot, history: Sequence[Snapshot] = ()) -> list[Insight]:
    """Return current conditions with explicit measurements and destinations."""
    found: list[Insight] = []
    cpu_count = max(1, len(snap.cpu.per_core))
    if snap.cpu.percent >= 90:
        found.append(Insight("high", "CPU is heavily loaded", f"CPU {snap.cpu.percent:.0f}%", "CPU"))
    if snap.cpu.load1 >= cpu_count * 1.5:
        found.append(
            Insight(
                "high",
                "Runnable load is high",
                f"1 min load {snap.cpu.load1:.2f} across {cpu_count} cores",
                "CPU",
            )
        )
    if snap.mem.percent >= 90:
        found.append(Insight("high", "Memory is nearly full", f"RAM {snap.mem.percent:.0f}%", "Memory"))
    if snap.mem.swap_total and snap.mem.swap_percent >= 75:
        found.append(
            Insight("high", "Swap is heavily allocated", f"Swap {snap.mem.swap_percent:.0f}%", "Memory")
        )
    recent = list(history[-10:])
    if snap.mem.swap_total and len(recent) >= 5:
        swap_growth = snap.mem.swap_percent - recent[0].mem.swap_percent
        if swap_growth >= 10 and snap.mem.swap_percent >= 20:
            found.append(
                Insight(
                    "high",
                    "Swap use is rising quickly",
                    f"Swap increased {swap_growth:.0f} points across {len(recent)} samples to {snap.mem.swap_percent:.0f}%",
                    "Memory",
                )
            )
    for mount in snap.disk.mounts:
        if mount.percent >= 90:
            found.append(
                Insight(
                    "high", "Filesystem is nearly full", f"{mount.mount}: {mount.percent:.0f}% used", "Memory"
                )
            )
    zombies = sum(1 for proc in snap.procs if proc.status.lower() in ("zombie", "z"))
    if zombies:
        found.append(
            Insight("warn", "Zombie processes are present", f"{zombies} visible zombie process(es)", "Procs")
        )
    busiest = max(snap.procs, key=lambda proc: proc.cpu, default=None)
    # ProcInfo.cpu is whole-machine capacity, not psutil's per-core scale.
    # A fully occupied core on a 24-logical-CPU host is about 4.2% here.
    if busiest and busiest.cpu >= 80:
        found.append(
            Insight(
                "warn",
                "One process is using substantial CPU",
                f"{busiest.name} (PID {busiest.pid}) at {busiest.cpu:.0f}%",
                "Procs",
            )
        )
    if snap.gpu.available and snap.gpu.gpus:
        busiest_gpu = max(snap.gpu.gpus, key=lambda gpu: gpu.util)
        if busiest_gpu.util >= 95:
            found.append(
                Insight(
                    "info",
                    "GPU is near full utilization",
                    f"GPU{busiest_gpu.index} at {busiest_gpu.util:.0f}%",
                    "GPU",
                )
            )
    return found

"""Command line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from typing import Optional

from . import __version__


def collect_once(scope: str = "recent"):
    from .collectors.agents import AgentCollector
    from .collectors.gpu import GpuCollector
    from .collectors.system import SystemCollector
    from .model import Snapshot

    system = SystemCollector()
    gpu = GpuCollector()
    agents = AgentCollector()
    # Prime rate counters, then take a real sample.
    system.sample()
    time.sleep(0.35)
    cpu, mem, net, disk, procs, index = system.sample()
    gpu_state = gpu.sample(index)
    sessions, quotas = agents.collect(index, scope, sessions_id())
    return Snapshot(
        ts=time.time(),
        hostname=system.hostname,
        username=system.username,
        uptime=system.uptime(),
        os_name=system.os_name,
        kernel=system.kernel,
        cpu=cpu,
        mem=mem,
        net=net,
        disk=disk,
        gpu=gpu_state,
        procs=procs,
        task_count=len(index),
        thread_count=sum(p.threads for p in index.values()),
        agents=sessions,
        quotas=quotas,
        token_rate=0.0,
        agent_scope=scope,
    )


def sessions_id() -> str:
    return ""


def _print_snapshot(snapshot) -> None:
    from .draw import human_ago, human_bytes, human_rate, human_tokens

    print(
        f"omnitop {__version__} — {snapshot.hostname} · {snapshot.username} · "
        f"up {snapshot.uptime / 3600:.1f}h · {snapshot.os_name}"
    )
    cpu = snapshot.cpu
    freq = f"{cpu.freq_mhz / 1000:.2f}GHz" if cpu.freq_mhz else "—"
    print(
        f"CPU  {cpu.percent:5.1f}%  {freq}"
        + (f"  {cpu.temp_c:.0f}°C" if cpu.temp_c is not None else "")
        + f"  load {cpu.load1:.2f} {cpu.load5:.2f} {cpu.load15:.2f}"
    )
    mem = snapshot.mem
    print(
        f"MEM  {mem.percent:5.1f}%  {human_bytes(mem.used)}/{human_bytes(mem.total)}"
        f"  swap {human_bytes(mem.swap_used)}/{human_bytes(mem.swap_total)}"
    )
    net = snapshot.net
    print(f"NET  down {human_rate(net.rx_rate)}  up {human_rate(net.tx_rate)}")
    disk = snapshot.disk
    print(f"DISK read {human_rate(disk.read_rate)}  write {human_rate(disk.write_rate)}")
    if snapshot.gpu.available:
        for gpu in snapshot.gpu.gpus:
            print(
                f"GPU{gpu.index} {gpu.name}  {gpu.util:.0f}%  "
                f"{human_bytes(gpu.mem_used)}/{human_bytes(gpu.mem_total)}  "
                f"{gpu.temp_c or 0:.0f}°C  {gpu.power_w or 0:.0f}W"
            )
        for proc in snapshot.gpu.processes[:8]:
            runtime = "—"
            if proc.runtime_s is not None:
                seconds = int(proc.runtime_s)
                hours, remainder = divmod(seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                runtime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            print(
                f"  gpuproc pid={proc.pid:<7} sm={proc.sm_display}% "
                f"gmbw={proc.gmbw_display}% "
                f"vram={human_bytes(proc.vram) if proc.vram is not None else '—'} "
                f"time={runtime} {proc.name}"
            )
    else:
        print(f"GPU  unavailable ({snapshot.gpu.error})")
    print(f"\nAI AGENTS ({len(snapshot.agents)})")
    for s in snapshot.agents:
        ctx = f"{s.context_pct:.0f}%" if s.context_pct is not None else "—"
        print(
            f"  {s.agent_label} pid={s.pid:<7} {s.status:<10} {s.project:<22} "
            f"{s.model:<22} ctx={ctx:<5} tok={human_tokens(s.total_tokens):<8} "
            f"{human_ago(s.updated_at)}"
        )
        if s.current_task:
            print(f"       └─ {s.current_task}")
        for child in s.children[:3]:
            port = f" :{child.port}" if child.port else ""
            print(f"       └─ {child.pid} {child.command[:70]}{port}")
    if snapshot.quotas:
        print("\nRATE LIMITS")
        for q in snapshot.quotas:
            for name, pct, reset in (("5h", q.five_pct, q.five_reset), ("7d", q.seven_pct, q.seven_reset)):
                if pct is not None:
                    remain = max(0, (reset or 0) - time.time())
                    print(f"  {q.source:<8} {name} {pct:5.1f}%  resets in {remain / 60:.0f}m")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omnitop",
        description="btop-style TUI unifying system, NVIDIA GPU and AI-agent monitoring.",
    )
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--json", action="store_true", help="print one JSON snapshot and exit")
    parser.add_argument("--demo", action="store_true", help="run with synthetic demo data")
    parser.add_argument(
        "--scope",
        choices=["recent", "all"],
        default="recent",
        help="agent sessions: last 30 minutes (default) or last day",
    )
    parser.add_argument(
        "--theme",
        choices=["btop", "dracula", "gruvbox", "nord", "adapta"],
        default="btop",
        help="colour theme",
    )
    parser.add_argument("--version", action="version", version=f"omnitop {__version__}")
    args = parser.parse_args(argv)

    if args.once or args.json:
        if args.demo:
            from .demo import DemoSource

            snapshot = DemoSource(scope=args.scope).snapshot()
        else:
            snapshot = collect_once(args.scope)
        if args.json:
            data = dataclasses.asdict(snapshot)
            json.dump(data, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            _print_snapshot(snapshot)
        return 0

    from .app import OmniTop

    app = OmniTop(scope=args.scope, theme=args.theme, demo=args.demo)
    try:
        app.run()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

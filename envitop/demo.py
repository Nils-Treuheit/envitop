"""Synthetic data source used by `envitop --demo` and screenshots/tests."""

from __future__ import annotations

import math
import random
import time
from collections import deque

from .model import (
    AgentSession,
    ChildProc,
    CpuInfo,
    DiskInfo,
    GpuInfo,
    GpuProcess,
    GpuState,
    MemInfo,
    MountInfo,
    NetInfo,
    ProcInfo,
    RateLimit,
    Snapshot,
)

GB = 1024**3
MB = 1024**2


class DemoSource:
    def __init__(self, scope: str = "recent") -> None:
        self.t = 0.0
        self.paused = False
        self.scope = scope
        self.selected_id = ""
        self._cpu_hist: deque[float] = deque(maxlen=240)
        self._gpu_hist: deque[float] = deque(maxlen=240)
        self._vram_hist: deque[float] = deque(maxlen=240)
        self._mem_hist: deque[float] = deque(maxlen=240)
        self._rx_hist: deque[float] = deque(maxlen=240)
        self._tx_hist: deque[float] = deque(maxlen=240)
        self._read_hist: deque[float] = deque(maxlen=240)
        self._write_hist: deque[float] = deque(maxlen=240)
        self._seed_history()

    def _seed_history(self) -> None:
        """Pre-fill graphs so the first frame already looks like btop."""
        for i in range(120):
            t = -0.5 * (120 - i)
            self._cpu_hist.append(34 + 20 * math.sin(t / 9) + 6 * math.sin(t / 2.3))
            self._mem_hist.append(42 + 5 * math.sin(t / 17))
            self._gpu_hist.append(max(0, min(100, 78 + 18 * math.sin(t / 7))))
            self._vram_hist.append(58 + 6 * math.sin(t / 13))
            self._rx_hist.append(max(0, 5.4 * MB + 3.5 * MB * math.sin(t / 5)))
            self._tx_hist.append(max(0, 900_000 + 450_000 * math.sin(t / 4 + 1)))
            self._read_hist.append(max(0, 41 * MB + 32 * MB * math.sin(t / 6)))
            self._write_hist.append(max(0, 12 * MB + 9 * MB * math.sin(t / 4 + 1)))

    # API used by the app -------------------------------------------------
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def force_refresh(self) -> None:
        pass

    def set_scope(self, scope: str) -> None:
        self.scope = scope

    def set_selected(self, session_id: str) -> None:
        self.selected_id = session_id

    def snapshot(self) -> Snapshot:
        if not self.paused:
            self.t += 0.5
        t = self.t
        now = time.time()
        cpu_total = 34 + 20 * math.sin(t / 9) + 6 * math.sin(t / 2.3)
        cores = []
        for i in range(24):
            cores.append(max(1, min(100, cpu_total + 24 * math.sin(t / 5 + i * 0.7) + random.uniform(-6, 6))))
        if not self.paused:
            self._cpu_hist.append(cpu_total)
        mem_pct = 42 + 5 * math.sin(t / 17)
        if not self.paused:
            self._mem_hist.append(mem_pct)
        gpu_util = 78 + 18 * math.sin(t / 7) + random.uniform(-4, 4)
        gpu_util = max(0, min(100, gpu_util))
        vram_pct = 58 + 6 * math.sin(t / 13)
        if not self.paused:
            self._gpu_hist.append(gpu_util)
            self._vram_hist.append(vram_pct)
        rx = max(0, 5.4 * MB + 3.5 * MB * math.sin(t / 5) + random.uniform(0, 1.5 * MB))
        tx = max(0, 900_000 + 450_000 * math.sin(t / 4 + 1) + random.uniform(0, 200_000))
        if not self.paused:
            self._rx_hist.append(rx)
            self._tx_hist.append(tx)
            self._read_hist.append(max(0, 41 * MB + 32 * MB * math.sin(t / 6)))
            self._write_hist.append(max(0, 12 * MB + 9 * MB * math.sin(t / 4 + 1)))

        cpu = CpuInfo(
            percent=cpu_total,
            per_core=cores,
            freq_mhz=4820,
            temp_c=63,
            load1=4.12,
            load5=3.71,
            load15=3.22,
            history=list(self._cpu_hist),
        )
        mem = MemInfo(
            total=64 * GB,
            used=int(64 * GB * mem_pct / 100),
            available=int(64 * GB * (100 - mem_pct) / 100),
            cached=21 * GB,
            buffers=2 * GB,
            percent=mem_pct,
            swap_total=16 * GB,
            swap_used=int(1.8 * GB),
            swap_percent=11,
            history=list(self._mem_hist),
        )
        net = NetInfo(
            rx_rate=rx,
            tx_rate=tx,
            total_rx=812 * GB,
            total_tx=94 * GB,
            rx_history=list(self._rx_hist),
            tx_history=list(self._tx_hist),
        )
        disk = DiskInfo(
            read_rate=self._read_hist[-1],
            write_rate=self._write_hist[-1],
            read_history=list(self._read_hist),
            write_history=list(self._write_hist),
            mounts=[
                MountInfo("/", int(412 * GB), 931 * GB, 44),
                MountInfo("/mnt/data", int(3.2e12), int(4.0e12), 80),
            ],
            temp_c=47,
        )
        gpu = GpuState(
            available=True,
            driver="595.99.02",
            gpus=[
                GpuInfo(
                    index=0,
                    name="NVIDIA GeForce RTX 5090",
                    util=gpu_util,
                    mem_used=int(32 * GB * vram_pct / 100),
                    mem_total=32 * GB,
                    mem_percent=vram_pct,
                    temp_c=58 + 4 * math.sin(t / 11),
                    power_w=310 + 80 * math.sin(t / 8),
                    power_limit_w=575,
                    fan_pct=42,
                    clock_sm=2790,
                    clock_mem=14001,
                    pstate="P2",
                    history=list(self._gpu_hist),
                    mem_history=list(self._vram_hist),
                )
            ],
            processes=[
                GpuProcess(0, 61204, "python3", "C", 82, 54, int(17.2 * GB), "nils"),
                GpuProcess(0, 73912, "ollama", "C", 47, 32, int(10.4 * GB), "nils"),
                GpuProcess(0, 6147, "Xorg", "G", 4, 2, int(0.7 * GB), "nils"),
                GpuProcess(0, 6668, "gnome-shell", "G", 2, 1, int(0.4 * GB), "nils"),
            ],
        )
        agents = [
            AgentSession(
                agent="claude",
                session_id="demo-claude-1",
                project="vector-db",
                cwd="/home/nils/code/vector-db",
                pid=61204,
                status="executing",
                model="claude-opus-4-6",
                context_pct=63.0,
                context_tokens=126_000,
                context_window=200_000,
                input_tokens=18_400,
                output_tokens=9_100,
                cache_read=98_700,
                cache_write=2_100,
                total_tokens=128_300,
                turn_count=14,
                started_at=now - 2460,
                updated_at=now - 2,
                mem_mb=412,
                git_branch="feat/hybrid-search",
                git_added=3,
                git_modified=7,
                current_task="Bash pytest -q tests/test_index.py",
                summary="Add HNSW recall regression test",
                children=[
                    ChildProc(61310, "pytest -q tests/test_index.py", 184_000, None),
                    ChildProc(61344, "uvicorn api.main:app --port 8080", 96_000, 8080),
                ],
                tools=[
                    ("Bash", "pytest -q tests/test_index.py"),
                    ("Edit", "src/index/hnsw.py"),
                    ("Read", "tests/test_index.py"),
                ],
                chat=[
                    ("ai", "The recall@10 drop comes from the new ef_search default."),
                    ("you", "Patch it and rerun the regression test."),
                ],
                token_history=[2100, 3300, 1800, 4200, 2700, 5100, 3900],
            ),
            AgentSession(
                agent="codex",
                session_id="demo-codex-1",
                project="vlm-benchmark",
                cwd="/mnt/data/vlm-benchmark",
                pid=73912,
                status="thinking",
                model="gpt-6-luna",
                effort="high",
                context_pct=41.0,
                context_tokens=106_000,
                context_window=258_400,
                input_tokens=61_200,
                output_tokens=14_600,
                cache_read=510_000,
                cache_write=0,
                total_tokens=585_800,
                turn_count=22,
                started_at=now - 8_400,
                updated_at=now - 18,
                mem_mb=286,
                git_branch="bench/sgg-v2",
                git_added=1,
                git_modified=2,
                current_task="thinking…",
                summary="Benchmark SGG on VG-150",
                children=[
                    ChildProc(74110, "python tools/eval_sgg.py --split val", 2_400_000, None),
                ],
                tools=[("exec", "python tools/eval_sgg.py --split val")],
                chat=[("you", "Run the VG-150 evaluation and compare with the baseline.")],
                token_history=[8200, 6100, 9400, 7800, 10200, 5700],
            ),
            AgentSession(
                agent="opencode",
                session_id="demo-opencode-1",
                project="envitop",
                cwd="/home/nils/envitop",
                pid=81900,
                status="waiting",
                model="big-pickle",
                context_pct=None,
                context_tokens=28_400,
                input_tokens=12_900,
                output_tokens=4_300,
                cache_read=33_100,
                total_tokens=50_300,
                turn_count=9,
                started_at=now - 1_320,
                updated_at=now - 74,
                mem_mb=198,
                git_branch="main",
                git_added=2,
                git_modified=5,
                current_task="Build unified btop + nvitop TUI",
                summary="Build unified btop + nvitop TUI",
                tools=[("edit", "envitop/widgets.py"), ("bash", "pytest -q")],
                chat=[("you", "Make the GPU panel look like nvitop.")],
                token_history=[1400, 2200, 3100, 2600, 3400],
            ),
            AgentSession(
                agent="claude",
                session_id="demo-claude-2",
                project="infra",
                cwd="/home/nils/code/infra",
                pid=0,
                status="done",
                model="claude-sonnet-4-5",
                context_pct=22.0,
                context_tokens=44_000,
                context_window=200_000,
                input_tokens=8_000,
                output_tokens=2_200,
                cache_read=36_000,
                total_tokens=46_200,
                turn_count=5,
                started_at=now - 9_600,
                updated_at=now - 3_600,
                mem_mb=0,
                git_branch="main",
                current_task="",
                summary="Rotate staging deploy keys",
            ),
        ]
        quotas = [
            RateLimit(
                source="claude",
                five_pct=21,
                five_reset=now + 1_800,
                five_window=300,
                seven_pct=47,
                seven_reset=now + 360_000,
                seven_window=10_080,
                updated_at=now - 12,
                live=True,
            ),
            RateLimit(
                source="codex",
                five_pct=30,
                five_reset=now + 13_200,
                five_window=300,
                seven_pct=27,
                seven_reset=now + 420_000,
                seven_window=10_080,
                updated_at=now - 4,
                live=True,
            ),
        ]
        procs = [
            ProcInfo(61204, "python3", "nils", 17.2, 14.2, int(4.1 * GB), "python -m train.ddp --config configs/large.toml", ppid=1, gpu_index=0, gpu_sm=82, gpu_mem_pct=54),
            ProcInfo(73912, "codex", "nils", 3.7, 3.1, int(1.2 * GB), "codex exec --full-auto", ppid=4211, gpu_index=0, gpu_sm=47, gpu_mem_pct=32),
            ProcInfo(81900, "opencode", "nils", 31.2, 2.0, int(0.9 * GB), "opencode", ppid=1),
            ProcInfo(4120, "firefox", "nils", 22.8, 6.4, int(2.8 * GB), "firefox --new-window", ppid=1),
            ProcInfo(9412, "gnome-shell", "nils", 18.4, 2.1, int(0.8 * GB), "gnome-shell --mode=user", ppid=1),
            ProcInfo(61344, "uvicorn", "nils", 12.6, 2.6, int(1.0 * GB), "uvicorn api.main:app --port 8080", ppid=61204),
            ProcInfo(6668, "Xorg", "nils", 9.1, 1.2, int(0.6 * GB), "/usr/lib/Xorg vt2 -displayfd 3", ppid=1),
            ProcInfo(311, "systemd", "root", 0.9, 0.1, int(42 * MB), "/sbin/init splash", ppid=0),
            ProcInfo(2055, "opencode", "nils", 0.4, 0.4, int(180 * MB), "opencode serve --hostname 127.0.0.1", ppid=1),
        ]
        token_rate = 1850 + 900 * math.sin(t / 6)
        return Snapshot(
            ts=now,
            hostname="workstation",
            username="nils",
            uptime=2 * 86400 + 4 * 3600 + 37 * 60,
            os_name="Linux 6.8.0-envitop",
            kernel="#1 SMP",
            cpu=cpu,
            mem=mem,
            net=net,
            disk=disk,
            gpu=gpu,
            procs=procs,
            task_count=len(procs),
            thread_count=sum(max(1, p.threads) for p in procs),
            agents=agents,
            quotas=quotas,
            token_rate=max(0, token_rate),
            agent_scope=self.scope,
            errors=[],
        )

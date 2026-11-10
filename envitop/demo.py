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
    def __init__(self, scope: str = "recent", *, step_seconds: float = 0.5, smooth: bool = False) -> None:
        self.t = 0.0
        self.paused = False
        self.scope = scope
        self.step_seconds = step_seconds
        self.smooth = smooth
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

    def _variation(self, amplitude: float, phase: float = 0.0, *, centered: bool = False) -> float:
        if not self.smooth:
            low = -amplitude if centered else 0.0
            return random.uniform(low, amplitude)
        wave = math.sin(self.t * 2.7 + phase)
        return amplitude * (wave if centered else (wave + 1) / 2)

    def _seed_history(self) -> None:
        """Pre-fill graphs so the first frame already looks like btop."""
        for i in range(120):
            t = -0.5 * (120 - i)
            cpu, memory, gpu, vram, rx, tx, read, write = self._signals(t)
            self._cpu_hist.append(cpu)
            self._mem_hist.append(memory)
            self._gpu_hist.append(max(0, min(100, gpu)))
            self._vram_hist.append(vram)
            self._rx_hist.append(rx * MB)
            self._tx_hist.append(tx * MB)
            self._read_hist.append(read * MB)
            self._write_hist.append(write * MB)

    @staticmethod
    def _signals(t: float) -> tuple[float, ...]:
        pulse = lambda divisor, power, phase=0: max(0.0, math.sin(t / divisor + phase)) ** power
        cpu = 42 + 22 * math.sin(t / 6.8) + 12 * math.sin(t / 2.1) + 8 * math.sin(t / 0.95)
        gpu = 54 + 27 * math.sin(t / 8) + 17 * math.sin(t / 2.3) + 16 * pulse(11, 10)
        rx = 0.6 + 6 * pulse(4.1, 1.6) + 3 * pulse(1.5, 4, 0.8)
        tx = 0.2 + 2.2 * pulse(3.3, 2.4, 1.1) + 1.6 * pulse(1.25, 5)
        read = 4 + 34 * pulse(5.1, 6) + 16 * pulse(1.7, 8, 1)
        write = 2 + 21 * pulse(4.2, 7, 1.4) + 12 * pulse(1.35, 8, 0.5)
        memory = 46 + 8 * math.sin(t / 18) + 3 * math.sin(t / 4.2)
        vram = 66 + 5 * math.sin(t / 14) + 3 * math.sin(t / 3.1)
        return cpu, memory, gpu, vram, rx, tx, read, write

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
            self.t += self.step_seconds
        t = self.t
        now = time.time()
        cpu_total, mem_pct, gpu_util, vram_pct, rx, tx, read, write = self._signals(t)
        cores = []
        for i in range(24):
            cores.append(
                max(
                    1,
                    min(
                        100, cpu_total + 24 * math.sin(t / 5 + i * 0.7) + self._variation(6, i, centered=True)
                    ),
                )
            )
        if not self.paused:
            self._cpu_hist.append(cpu_total)
        if not self.paused:
            self._mem_hist.append(mem_pct)
        gpu_util += self._variation(3, 1.3, centered=True)
        gpu_util = max(0, min(100, gpu_util))
        if not self.paused:
            self._gpu_hist.append(gpu_util)
            self._vram_hist.append(vram_pct)
        rx = max(0, rx * MB + self._variation(0.8 * MB, 0.5 * math.pi))
        tx = max(0, tx * MB + self._variation(0.3 * MB, math.pi))
        if not self.paused:
            self._rx_hist.append(rx)
            self._tx_hist.append(tx)
            self._read_hist.append(max(0, read * MB + self._variation(1.2 * MB, 2 * math.pi / 3)))
            self._write_hist.append(max(0, write * MB + self._variation(0.8 * MB, 5 * math.pi / 3)))

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
                MountInfo(
                    "/",
                    int(412 * GB),
                    931 * GB,
                    44,
                    free=519 * GB,
                    read_rate=self._read_hist[-1] * 0.28,
                    write_rate=self._write_hist[-1] * 0.16,
                    read_history=[value * 0.28 for value in self._read_hist],
                    write_history=[value * 0.16 for value in self._write_hist],
                ),
                MountInfo(
                    "/mnt/data",
                    int(3.2e12),
                    int(4.0e12),
                    80,
                    free=int(0.8e12),
                    read_rate=self._read_hist[-1] * 0.72,
                    write_rate=self._write_hist[-1] * 0.84,
                    read_history=[value * 0.72 for value in self._read_hist],
                    write_history=[value * 0.84 for value in self._write_hist],
                ),
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

        def turn_samples(seed: float, mean: float, amplitude: float) -> list[int]:
            return [
                max(
                    100,
                    int(
                        mean
                        + amplitude * math.sin(i * 0.72 + seed)
                        + amplitude * 0.28 * math.sin(i * 1.91 + seed * 0.5)
                    ),
                )
                for i in range(36)
            ]

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
                token_history=turn_samples(0.2, 3_400, 1_700),
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
                token_history=turn_samples(1.3, 7_800, 2_600),
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
                children=[
                    ChildProc(81914, "cargo build --release", 482_000, None),
                    ChildProc(81928, "python -m ruff check envitop", 96_000, None),
                ],
                tools=[("edit", "envitop/widgets.py"), ("bash", "pytest -q")],
                chat=[("you", "Make the GPU panel look like nvitop.")],
                token_history=turn_samples(2.1, 2_500, 1_400),
            ),
            AgentSession(
                agent="codex",
                session_id="demo-codex-2",
                project="envitop",
                cwd="/home/nils/envitop",
                pid=82416,
                status="executing",
                model="gpt-6.1-terra",
                effort="high",
                context_pct=57.0,
                context_tokens=147_700,
                context_window=258_400,
                input_tokens=42_800,
                output_tokens=8_600,
                cache_read=214_000,
                cache_write=1_200,
                total_tokens=266_600,
                turn_count=11,
                started_at=now - 1_780,
                updated_at=now - 5,
                mem_mb=334,
                git_branch="feat/demo-dashboard",
                git_added=5,
                git_modified=3,
                current_task="Increase graph variation and polish the demo export",
                summary="Refresh demo graphs and agent process examples",
                children=[
                    ChildProc(82431, "python -m compileall -q envitop", 124_000, None),
                    ChildProc(82457, "cargo check --release", 662_000, None),
                ],
                tools=[
                    ("Read", "envitop/demo.py"),
                    ("Edit", "docs/envitop-demo.svg"),
                    ("Bash", "cargo check --release"),
                ],
                chat=[("ai", "The demo now includes bursty IO, CPU and GPU histories.")],
                token_history=turn_samples(2.8, 4_600, 2_100),
            ),
            AgentSession(
                agent="claude",
                session_id="demo-claude-3",
                project="docs-site",
                cwd="/home/nils/code/docs-site",
                pid=82642,
                status="executing",
                model="claude-sonnet-4-5",
                context_pct=34.0,
                context_tokens=68_000,
                context_window=200_000,
                input_tokens=24_600,
                output_tokens=7_800,
                cache_read=88_200,
                cache_write=3_400,
                total_tokens=124_000,
                turn_count=8,
                started_at=now - 1_120,
                updated_at=now - 11,
                mem_mb=258,
                git_branch="docs/quickstart",
                git_added=4,
                git_modified=6,
                current_task="Check install steps and validate the SVG overview",
                summary="Update quickstart and check command examples",
                children=[
                    ChildProc(82655, "npm run build", 326_000, None),
                    ChildProc(82682, "node scripts/check_links.js", 112_000, None),
                ],
                tools=[("Read", "README.md"), ("Edit", "docs/quickstart.md"), ("Bash", "npm run build")],
                chat=[("you", "Make the setup steps match the current launcher.")],
                token_history=turn_samples(3.4, 3_200, 1_300),
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
            ProcInfo(
                61204,
                "python3",
                "nils",
                17.2,
                14.2,
                int(4.1 * GB),
                "python -m train.ddp --config configs/large.toml",
                ppid=1,
                gpu_index=0,
                gpu_sm=82,
                gpu_mem_pct=54,
            ),
            ProcInfo(
                73912,
                "codex",
                "nils",
                3.7,
                3.1,
                int(1.2 * GB),
                "codex exec --full-auto",
                ppid=4211,
                gpu_index=0,
                gpu_sm=47,
                gpu_mem_pct=32,
            ),
            ProcInfo(81900, "opencode", "nils", 31.2, 2.0, int(0.9 * GB), "opencode", ppid=1),
            ProcInfo(
                82416,
                "codex",
                "nils",
                24.6,
                2.8,
                int(1.1 * GB),
                "codex exec --full-auto --title envitop-demo",
                ppid=1,
            ),
            ProcInfo(82642, "claude", "nils", 15.2, 1.9, int(0.7 * GB), "claude --project docs-site", ppid=1),
            ProcInfo(4120, "firefox", "nils", 22.8, 6.4, int(2.8 * GB), "firefox --new-window", ppid=1),
            ProcInfo(
                9412, "gnome-shell", "nils", 18.4, 2.1, int(0.8 * GB), "gnome-shell --mode=user", ppid=1
            ),
            ProcInfo(
                61344,
                "uvicorn",
                "nils",
                12.6,
                2.6,
                int(1.0 * GB),
                "uvicorn api.main:app --port 8080",
                ppid=61204,
            ),
            ProcInfo(
                82431,
                "python",
                "nils",
                7.2,
                0.8,
                int(124 * MB),
                "python -m compileall -q envitop",
                ppid=82416,
            ),
            ProcInfo(82457, "rustc", "nils", 43.1, 4.6, int(1.8 * GB), "cargo check --release", ppid=82416),
            ProcInfo(82655, "node", "nils", 8.6, 0.7, int(326 * MB), "npm run build", ppid=82642),
            ProcInfo(
                82682, "node", "nils", 2.4, 0.2, int(112 * MB), "node scripts/check_links.js", ppid=82642
            ),
            ProcInfo(6668, "Xorg", "nils", 9.1, 1.2, int(0.6 * GB), "/usr/lib/Xorg vt2 -displayfd 3", ppid=1),
            ProcInfo(311, "systemd", "root", 0.9, 0.1, int(42 * MB), "/sbin/init splash", ppid=0),
            ProcInfo(
                2055,
                "opencode",
                "nils",
                0.4,
                0.4,
                int(180 * MB),
                "opencode serve --hostname 127.0.0.1",
                ppid=1,
            ),
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

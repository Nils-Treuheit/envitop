"""NVIDIA GPU collector built on nvidia-smi (no extra Python dependencies)."""

from __future__ import annotations

import csv
import shutil
import subprocess
import time
from typing import Optional

from ..model import GpuInfo, GpuProcess, GpuState, ProcInfo


def _num(value: str) -> Optional[float]:
    value = (value or "").strip()
    if not value or value in ("N/A", "[N/A]", "[Not Supported]", "N/A "):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: str) -> int:
    num = _num(value)
    return int(num) if num is not None else 0


class GpuCollector:
    HISTORY = 240
    QUERY = (
        "index,name,utilization.gpu,memory.used,memory.total,"
        "temperature.gpu,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem,"
        "pstate,utilization.memory"
    )

    def __init__(self) -> None:
        self._hist: dict[int, list[float]] = {}
        self._mem_hist: dict[int, list[float]] = {}
        self._last_error = ""
        self._last_command_ok = True
        self._driver = ""
        self._cuda = ""
        self._last_header_check = 0.0

    def _run(self, args: list[str], timeout: float = 3.0) -> str:
        self._last_command_ok = False
        try:
            out = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._last_error = str(exc)
            return ""
        if out.returncode != 0:
            self._last_error = f"nvidia-smi exited {out.returncode}"
            return ""
        self._last_command_ok = True
        return out.stdout

    def _read_driver(self) -> str:
        now = time.monotonic()
        if self._driver and now - self._last_header_check < 60:
            return self._driver
        self._last_header_check = now
        out = self._run(["nvidia-smi"], timeout=3)
        for line in out.splitlines():
            if "Driver Version:" in line:
                self._driver = line.split("Driver Version:", 1)[1].strip().split()[0]
            if "CUDA Version:" in line:
                self._cuda = line.split("CUDA Version:", 1)[1].strip().split()[0]
        return self._driver

    def refresh_driver(self) -> str:
        return self._read_driver()

    def refresh_cuda(self) -> str:
        self._read_driver()
        return self._cuda

    def sample(self, proc_index: dict[int, ProcInfo], include_processes: bool = True) -> GpuState:
        if not shutil.which("nvidia-smi"):
            return GpuState(available=False, error="nvidia-smi not found")
        out = self._run(
            ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
            timeout=3,
        )
        if not out.strip():
            return GpuState(available=False, error=self._last_error or "no NVIDIA GPU detected")

        gpus: list[GpuInfo] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                row = next(csv.reader([line]))
            except Exception:
                continue
            if len(row) < 13:
                continue
            row = [c.strip() for c in row]
            idx = _int(row[0])
            util = _num(row[2]) or 0.0
            mem_used = _int(row[3]) * 1024 * 1024
            mem_total = _int(row[4]) * 1024 * 1024
            mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0
            gpu = GpuInfo(
                index=idx,
                name=row[1] or "NVIDIA GPU",
                util=util,
                mem_used=mem_used,
                mem_total=mem_total,
                mem_percent=float(mem_pct),
                temp_c=_num(row[5]),
                power_w=_num(row[6]),
                power_limit_w=_num(row[7]),
                fan_pct=_num(row[8]),
                clock_sm=_num(row[9]),
                clock_mem=_num(row[10]),
                pstate=row[11] or "",
                mem_util=_num(row[12]),
            )
            hist = self._hist.setdefault(idx, [])
            hist.append(util)
            if len(hist) > self.HISTORY:
                del hist[: len(hist) - self.HISTORY]
            mhist = self._mem_hist.setdefault(idx, [])
            mhist.append(gpu.mem_percent)
            if len(mhist) > self.HISTORY:
                del mhist[: len(mhist) - self.HISTORY]
            gpu.history = list(hist)
            gpu.mem_history = list(mhist)
            gpus.append(gpu)

        if not gpus:
            return GpuState(available=False, error="no NVIDIA GPU detected")

        processes = (self._processes(proc_index, gpus) or []) if include_processes else []
        return GpuState(
            available=True,
            gpus=gpus,
            processes=processes,
            driver=self._read_driver() if include_processes else self._driver,
            cuda=self._cuda,
        )

    def sample_processes(
        self, proc_index: dict[int, ProcInfo], gpus: list[GpuInfo]
    ) -> Optional[list[GpuProcess]]:
        return self._processes(proc_index, gpus)

    def _processes(self, proc_index: dict[int, ProcInfo], gpus: list[GpuInfo]) -> Optional[list[GpuProcess]]:
        apps: dict[int, int] = {}
        app_out = self._run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=3,
        )
        apps_ok = self._last_command_ok
        for line in app_out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit():
                apps[int(parts[0])] = _int(parts[1]) * 1024 * 1024

        # `u` reports per-process SM and memory-bandwidth activity. `m` adds
        # instantaneous framebuffer allocation, including graphics processes.
        pmon = self._run(["nvidia-smi", "pmon", "-s", "um", "-c", "1"], timeout=4)
        pmon_ok = self._last_command_ok
        rows: list[GpuProcess] = []
        seen: set[tuple[int, int]] = set()
        columns: list[str] = []
        for line in pmon.splitlines():
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                header = line.lstrip()[1:].split()
                if "pid" in header and "gpu" in header:
                    columns = header
                continue
            parts = line.split(None, max(0, len(columns) - 1)) if columns else line.split()
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            gpu_idx, pid = int(parts[0]), int(parts[1])
            if pid <= 0:
                continue
            key = (gpu_idx, pid)
            if key in seen:
                continue
            seen.add(key)
            kind = parts[2] if len(parts) > 2 else "C"
            positions = {column: i for i, column in enumerate(columns)}

            def metric(column: str) -> Optional[float]:
                pos = positions.get(column)
                return _num(parts[pos]) if pos is not None and pos < len(parts) else None

            sm = metric("sm") if columns else (_num(parts[3]) if len(parts) > 3 else None)
            gmbw_pct = metric("mem") if columns else (_num(parts[4]) if len(parts) > 4 else None)
            command_pos = positions.get("command")
            command = (
                parts[command_pos].strip()
                if command_pos is not None and command_pos < len(parts)
                else (parts[-1].strip() if len(parts) > 9 else "")
            )
            proc = proc_index.get(pid)
            name = proc.name if proc else command
            if proc and proc.cmd:
                name = proc.name
            vram = apps.get(pid)
            # Header-driven parsing matters: newer drivers insert JPG/OFA
            # utilization columns before framebuffer memory.
            pmon_vram_mb = metric("fb") if columns else None
            if pmon_vram_mb is not None and pmon_vram_mb >= 0:
                vram = int(pmon_vram_mb * 1024 * 1024)
            rows.append(
                GpuProcess(
                    gpu_index=gpu_idx,
                    pid=pid,
                    name=name or command or "?",
                    kind=kind,
                    sm=sm,
                    gmbw_pct=gmbw_pct,
                    vram=vram,
                    user=proc.user if proc else "",
                    cpu_pct=proc.cpu if proc else None,
                    host_mem_pct=proc.mem_pct if proc else None,
                    rss=proc.rss if proc else 0,
                    cmd=proc.cmd if proc else name or command or "?",
                    runtime_s=max(0.0, time.time() - proc.create_time)
                    if proc and proc.create_time > 0
                    else None,
                )
            )

        # Fallback: compute-apps are visible even when pmon is unsupported.
        if not rows and not pmon_ok and not apps_ok:
            return None
        if not rows and apps:
            for pid, vram in apps.items():
                proc = proc_index.get(pid)
                rows.append(
                    GpuProcess(
                        gpu_index=gpus[0].index,
                        pid=pid,
                        name=proc.name if proc else "?",
                        kind="C",
                        sm=None,
                        gmbw_pct=None,
                        vram=vram,
                        user=proc.user if proc else "",
                        cpu_pct=proc.cpu if proc else None,
                        host_mem_pct=proc.mem_pct if proc else None,
                        rss=proc.rss if proc else 0,
                        cmd=proc.cmd if proc else "?",
                        runtime_s=max(0.0, time.time() - proc.create_time)
                        if proc and proc.create_time > 0
                        else None,
                    )
                )
        rows.sort(key=lambda p: (p.gpu_index, -(p.vram or 0), p.pid))
        return rows

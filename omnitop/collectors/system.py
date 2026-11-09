"""System-wide collectors (CPU, memory, network, disk, processes)."""

from __future__ import annotations

import os
import platform
import socket
import time
from typing import Optional

import psutil

from ..model import CpuInfo, DiskInfo, MemInfo, MountInfo, NetInfo, ProcInfo


def _read_first(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


class SystemCollector:
    HISTORY = 240

    def __init__(self) -> None:
        self._hist_cpu: list[float] = []
        self._hist_mem: list[float] = []
        self._hist_rx: list[float] = []
        self._hist_tx: list[float] = []
        self._hist_read: list[float] = []
        self._hist_write: list[float] = []
        self._last_net: Optional[tuple[int, int, float]] = None
        self._last_disk: Optional[tuple[int, int, float]] = None
        self._mounts: list[MountInfo] = []
        self._mounts_at = 0.0
        self._temps_at = 0.0
        self._temp_c: Optional[float] = None
        self._disk_temp_c: Optional[float] = None
        self._freq_at = 0.0
        self._freq = 0.0
        self._proc_cache: list[ProcInfo] = []
        self._proc_index: dict[int, ProcInfo] = {}
        self.hostname = socket.gethostname()
        self.username = _read_first("/proc/self/loginuid") == "4294967295" and "" or os.environ.get("USER", "")
        if not self.username:
            try:
                self.username = psutil.Process().username()
            except Exception:
                self.username = os.environ.get("USER", "user")
        self.os_name = f"{platform.system()} {platform.release()}"
        try:
            self.kernel = platform.version()
        except Exception:
            self.kernel = ""
        # Prime psutil's per-core counters so the first visible sample is real.
        try:
            psutil.cpu_percent(interval=0.05, percpu=True)
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------

    def _cpu_temp(self) -> Optional[float]:
        try:
            sensors = psutil.sensors_temperatures()
        except Exception:
            return None
        candidates: list[float] = []
        for name in ("k10temp", "coretemp", "zenpower", "cpu_thermal", "acpitz"):
            for entry in sensors.get(name, []):
                label = (entry.label or "").lower()
                if name == "k10temp" and label not in ("tctl", "tdie", ""):
                    continue
                if entry.current and entry.current < 150:
                    candidates.append(float(entry.current))
        if candidates:
            return max(candidates)
        for name, entries in sensors.items():
            if name in ("nvme", "amdgpu", "nouveau"):
                continue
            for entry in entries:
                if entry.current and 0 < entry.current < 150:
                    return float(entry.current)
        return None

    def _disk_temp(self) -> Optional[float]:
        try:
            sensors = psutil.sensors_temperatures()
        except Exception:
            return None
        values = []
        for entry in sensors.get("nvme", []):
            if entry.current and 0 < entry.current < 150:
                values.append(float(entry.current))
        return max(values) if values else None

    def _read_mounts(self) -> list[MountInfo]:
        now = time.monotonic()
        if now - self._mounts_at < 20 and self._mounts:
            return self._mounts
        self._mounts_at = now
        seen: set[str] = set()
        out: list[MountInfo] = []
        try:
            parts = psutil.disk_partitions(all=False)
        except Exception:
            parts = []
        skip_fs = {
            "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup",
            "cgroup2", "devpts", "ramfs", "iso9660", "autofs", "efivarfs", "fusectl",
        }
        for part in parts:
            mount = part.mountpoint
            if mount in seen or part.fstype in skip_fs:
                continue
            if mount.startswith(("/snap", "/var/lib/docker", "/var/lib/snapd")):
                continue
            try:
                usage = psutil.disk_usage(mount)
            except Exception:
                continue
            if usage.total <= 0:
                continue
            seen.add(mount)
            out.append(
                MountInfo(
                    mount=mount,
                    used=usage.used,
                    total=usage.total,
                    percent=usage.percent,
                )
            )
        out.sort(key=lambda m: (m.mount != "/", m.mount))
        self._mounts = out[:6]
        return self._mounts

    # -- processes -------------------------------------------------------

    def processes(self, limit: int = 90) -> tuple[list[ProcInfo], dict[int, ProcInfo]]:
        result: list[ProcInfo] = []
        attrs = [
            "pid", "name", "username", "cpu_percent", "memory_percent",
            "memory_info", "cmdline", "status", "num_threads", "ppid", "create_time",
        ]
        for proc in psutil.process_iter(attrs=attrs, ad_value=None):
            info = proc.info
            try:
                cmdline = info.get("cmdline") or []
                if isinstance(cmdline, str):
                    cmdline = [cmdline]
                cmd = " ".join(cmdline)
                rss = int(getattr(info.get("memory_info"), "rss", 0) or 0)
                result.append(
                    ProcInfo(
                        pid=int(info.get("pid") or 0),
                        name=str(info.get("name") or "?"),
                        user=str(info.get("username") or ""),
                        cpu=float(info.get("cpu_percent") or 0.0),
                        mem_pct=float(info.get("memory_percent") or 0.0),
                        rss=rss,
                        cmd=cmd[:400],
                        status=str(info.get("status") or ""),
                        threads=int(info.get("num_threads") or 0),
                        ppid=int(info.get("ppid") or 0),
                        create_time=float(info.get("create_time") or 0.0),
                        cmdline=cmdline,
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        result.sort(key=lambda p: (p.cpu, p.rss), reverse=True)
        index = {p.pid: p for p in result}
        self._proc_index = index
        self._proc_cache = result
        return result[:limit], index

    # -- main sample -----------------------------------------------------

    def sample(self) -> tuple[CpuInfo, MemInfo, NetInfo, DiskInfo, list[ProcInfo], dict[int, ProcInfo]]:
        now = time.monotonic()
        try:
            total = float(psutil.cpu_percent(interval=None))
            cores = [float(v) for v in psutil.cpu_percent(interval=None, percpu=True)]
        except Exception:
            total, cores = 0.0, []
        try:
            load = os.getloadavg()
        except (OSError, AttributeError):
            load = (0.0, 0.0, 0.0)
        if now - self._freq_at > 2:
            self._freq_at = now
            try:
                freq = psutil.cpu_freq()
                self._freq = float(freq.current) if freq and freq.current >= 100 else 0.0
            except Exception:
                self._freq = 0.0
        if now - self._temps_at > 5:
            self._temps_at = now
            self._temp_c = self._cpu_temp()
            self._disk_temp_c = self._disk_temp()
        self._hist_cpu.append(total)
        self._hist_cpu = self._hist_cpu[-self.HISTORY:]

        cpu = CpuInfo(
            percent=total,
            per_core=cores,
            freq_mhz=self._freq,
            temp_c=self._temp_c,
            load1=float(load[0]),
            load5=float(load[1]),
            load15=float(load[2]),
            history=list(self._hist_cpu),
        )

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        self._hist_mem.append(float(mem.percent))
        self._hist_mem = self._hist_mem[-self.HISTORY:]
        mem_info = MemInfo(
            total=int(mem.total),
            used=int(mem.used),
            available=int(mem.available),
            cached=int(getattr(mem, "cached", 0) or 0),
            buffers=int(getattr(mem, "buffers", 0) or 0),
            percent=float(mem.percent),
            swap_total=int(swap.total),
            swap_used=int(swap.used),
            swap_percent=float(swap.percent),
            history=list(self._hist_mem),
        )

        # Network rates.
        try:
            net = psutil.net_io_counters()
            rx, tx = int(net.bytes_recv), int(net.bytes_sent)
        except Exception:
            rx = tx = 0
        rx_rate = tx_rate = 0.0
        if self._last_net:
            last_rx, last_tx, last_t = self._last_net
            dt = max(1e-6, now - last_t)
            rx_rate = max(0.0, (rx - last_rx) / dt)
            tx_rate = max(0.0, (tx - last_tx) / dt)
        self._last_net = (rx, tx, now)
        self._hist_rx.append(rx_rate)
        self._hist_tx.append(tx_rate)
        self._hist_rx = self._hist_rx[-self.HISTORY:]
        self._hist_tx = self._hist_tx[-self.HISTORY:]
        net_info = NetInfo(
            rx_rate=rx_rate,
            tx_rate=tx_rate,
            total_rx=rx,
            total_tx=tx,
            rx_history=list(self._hist_rx),
            tx_history=list(self._hist_tx),
        )

        # Disk rates + usage.
        read_rate = write_rate = 0.0
        try:
            io = psutil.disk_io_counters()
            read, write = int(io.read_bytes), int(io.write_bytes)
        except Exception:
            read = write = 0
        if self._last_disk:
            last_read, last_write, last_t = self._last_disk
            dt = max(1e-6, now - last_t)
            read_rate = max(0.0, (read - last_read) / dt)
            write_rate = max(0.0, (write - last_write) / dt)
        self._last_disk = (read, write, now)
        self._hist_read.append(read_rate)
        self._hist_write.append(write_rate)
        self._hist_read = self._hist_read[-self.HISTORY:]
        self._hist_write = self._hist_write[-self.HISTORY:]
        disk_info = DiskInfo(
            read_rate=read_rate,
            write_rate=write_rate,
            read_history=list(self._hist_read),
            write_history=list(self._hist_write),
            mounts=self._read_mounts(),
            temp_c=self._disk_temp_c,
        )

        procs, index = self.processes()
        return cpu, mem_info, net_info, disk_info, procs, index

    def uptime(self) -> float:
        try:
            return max(0.0, time.time() - psutil.boot_time())
        except Exception:
            return 0.0

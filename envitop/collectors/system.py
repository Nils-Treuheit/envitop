"""System-wide collectors (CPU, memory, network, disk, processes)."""

from __future__ import annotations

import http.client
import ipaddress
import os
import platform
import re
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Optional

import psutil

from ..model import (
    CpuInfo,
    DiskInfo,
    HostedSiteInfo,
    MemInfo,
    MountInfo,
    NetInfo,
    NetInterfaceInfo,
    NetListenerInfo,
    NetProcessInfo,
    ProcInfo,
)


def _read_first(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


def _endpoint_ip(endpoint: str) -> str:
    if endpoint.startswith("[") and "]" in endpoint:
        return endpoint[1:endpoint.index("]")].split("%", 1)[0]
    host, separator, _port = endpoint.rpartition(":")
    return (host if separator else endpoint).split("%", 1)[0]


class SystemCollector:
    HISTORY = 240

    def __init__(self) -> None:
        self._hist_cpu: list[float] = []
        self._hist_cpu_user: list[float] = []
        self._hist_core: list[list[float]] = []
        self._hist_mem: list[float] = []
        self._hist_swap: list[float] = []
        self._hist_rx: list[float] = []
        self._hist_tx: list[float] = []
        self._hist_read: list[float] = []
        self._hist_write: list[float] = []
        self._last_net: Optional[tuple[int, int, float]] = None
        self._last_net_if: dict[str, tuple[int, int, float]] = {}
        self._last_socket_bytes: dict[tuple[int, str, str], tuple[int, int, float]] = {}
        self._website_status_cache: dict[tuple[int, int, str], Optional[int]] = {}
        self._website_probe_at = 0.0
        self._last_disk: Optional[tuple[int, int, float]] = None
        self._mounts: list[MountInfo] = []
        self._mount_prev: dict[str, tuple[int, int, float]] = {}
        self._mount_history: dict[str, tuple[list[float], list[float]]] = {}
        self._mounts_at = 0.0
        self._temps_at = 0.0
        self._temp_c: Optional[float] = None
        self._core_temps: list[Optional[float]] = []
        self._core_temp_source = ""
        self._disk_temp_c: Optional[float] = None
        self._freq_at = 0.0
        self._freq = 0.0
        self._proc_cache: list[ProcInfo] = []
        self._proc_index: dict[int, ProcInfo] = {}
        self._logical_cpus = max(1, psutil.cpu_count(logical=True) or 1)
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
        self.cpu_model = self._cpu_model()
        # Prime psutil's per-core counters so the first visible sample is real.
        try:
            psutil.cpu_percent(interval=0.05, percpu=True)
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _cpu_model() -> str:
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, sep, value = line.partition(":")
                    if sep and key.strip().lower() in ("model name", "hardware"):
                        return value.strip()
        except OSError:
            pass
        return platform.processor() or "CPU"

    def _cpu_core_temps(self) -> list[Optional[float]]:
        try:
            sensors = psutil.sensors_temperatures()
        except Exception:
            return []
        values: dict[int, float] = {}
        ccds: dict[int, float] = {}
        for entries in sensors.values():
            for entry in entries:
                label = (entry.label or "").lower()
                match = re.search(r"(?:core|cpu)\s*(\d+)", label)
                if match and entry.current and 0 < entry.current < 150:
                    index = int(match.group(1))
                    values[index] = max(values.get(index, 0.0), float(entry.current))
                ccd = re.search(r"tccd\s*(\d+)", label)
                if ccd and entry.current and 0 < entry.current < 150:
                    ccds[int(ccd.group(1))] = float(entry.current)
        if values:
            self._core_temp_source = "core"
            return [values.get(index) for index in range(max(values) + 1)]
        if ccds:
            # Ryzen exposes chiplet temperature rather than a sensor per core.
            # Map each CCD reading to its contiguous logical-CPU group.
            count = psutil.cpu_count(logical=True) or max(ccds) * 6
            ordered = sorted(ccds.items())
            self._core_temp_source = "CCD"
            return [ordered[min(len(ordered) - 1, index * len(ordered) // count)][1] for index in range(count)]
        self._core_temp_source = ""
        return []

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
        if now - self._mounts_at < 5 and self._mounts:
            return self._mounts
        self._mounts_at = now
        seen_devices: set[str] = set()
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
            device = self._device_name(part.device)
            if device in seen_devices or part.fstype in skip_fs:
                continue
            if mount.startswith(("/snap", "/var/lib/docker", "/var/lib/snapd")):
                continue
            try:
                usage = psutil.disk_usage(mount)
            except Exception:
                continue
            if usage.total <= 0:
                continue
            try:
                vfs = os.statvfs(mount)
                inode_total = int(vfs.f_files)
                inode_used = max(0, inode_total - int(vfs.f_ffree))
                inode_percent = inode_used / inode_total * 100.0 if inode_total else None
            except OSError:
                inode_total = inode_used = 0
                inode_percent = None
            seen_devices.add(device)
            out.append(
                MountInfo(
                    mount=mount,
                    used=usage.used,
                    total=usage.total,
                    percent=usage.percent,
                    free=usage.free,
                    device=device,
                    filesystem=part.fstype,
                    inodes_total=inode_total,
                    inodes_used=inode_used,
                    inodes_percent=inode_percent,
                )
            )
        out.sort(key=lambda m: (-m.total, m.mount))
        self._mounts = out[:32]
        return self._mounts

    @staticmethod
    def _device_name(device: str) -> str:
        """Resolve a mounted block-device node to its kernel diskstats name."""
        try:
            st = os.stat(device)
            if not os.path.exists(device) or not os.path.exists("/sys/dev/block"):
                return device.rsplit("/", 1)[-1]
            devno = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
            return os.path.basename(os.path.realpath(f"/sys/dev/block/{devno}"))
        except (OSError, AttributeError):
            return device.rsplit("/", 1)[-1]

    def _sample_mount_io(self, mounts: list[MountInfo], now: float) -> list[MountInfo]:
        try:
            counters = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            counters = {}
        sampled: list[MountInfo] = []
        for mount in mounts:
            io = counters.get(mount.device)
            read_rate = write_rate = 0.0
            if io is not None:
                read, write = int(io.read_bytes), int(io.write_bytes)
                previous = self._mount_prev.get(mount.device)
                if previous:
                    last_read, last_write, last_at = previous
                    dt = max(1e-6, now - last_at)
                    read_rate = max(0.0, (read - last_read) / dt)
                    write_rate = max(0.0, (write - last_write) / dt)
                self._mount_prev[mount.device] = (read, write, now)
            read_history, write_history = self._mount_history.setdefault(mount.mount, ([], []))
            read_history.append(read_rate)
            write_history.append(write_rate)
            del read_history[:-self.HISTORY]
            del write_history[:-self.HISTORY]
            sampled.append(
                MountInfo(
                    mount=mount.mount,
                    used=mount.used,
                    total=mount.total,
                    percent=mount.percent,
                    free=mount.free,
                    device=mount.device,
                    read_rate=read_rate,
                    write_rate=write_rate,
                    read_history=list(read_history),
                    write_history=list(write_history),
                    filesystem=mount.filesystem,
                    inodes_total=mount.inodes_total,
                    inodes_used=mount.inodes_used,
                    inodes_percent=mount.inodes_percent,
                )
            )
        return sampled

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
                # psutil reports a process using one fully busy logical CPU as
                # 100%, and multithreaded processes can exceed 100%. Envitop
                # uses whole-machine capacity instead: divide by logical CPUs
                # so process CPU stays on the same 0–100% scale as host CPU.
                cpu_pct = float(info.get("cpu_percent") or 0.0) / self._logical_cpus
                result.append(
                    ProcInfo(
                        pid=int(info.get("pid") or 0),
                        name=str(info.get("name") or "?"),
                        user=str(info.get("username") or ""),
                        cpu=max(0.0, min(100.0, cpu_pct)),
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
        index = {p.pid: p for p in result}
        self._proc_index = index
        result.sort(key=lambda proc: (proc.cpu, proc.mem_pct), reverse=True)
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
            self._core_temps = self._cpu_core_temps()
            self._disk_temp_c = self._disk_temp()
        self._hist_cpu.append(total)
        self._hist_cpu = self._hist_cpu[-self.HISTORY:]
        try:
            cpu_times = psutil.cpu_times_percent(interval=None)
            user_percent = float(cpu_times.user + getattr(cpu_times, "nice", 0.0))
        except Exception:
            user_percent = 0.0
        self._hist_cpu_user.append(user_percent)
        self._hist_cpu_user = self._hist_cpu_user[-self.HISTORY:]
        if len(self._hist_core) != len(cores):
            self._hist_core = [[] for _ in cores]
        for history, value in zip(self._hist_core, cores):
            history.append(value)
            del history[:-self.HISTORY]

        cpu = CpuInfo(
            model_name=self.cpu_model,
            percent=total,
            user_percent=user_percent,
            per_core=cores,
            core_temps=list(self._core_temps),
            core_temp_source=self._core_temp_source,
            freq_mhz=self._freq,
            temp_c=self._temp_c,
            load1=float(load[0]),
            load5=float(load[1]),
            load15=float(load[2]),
            history=list(self._hist_cpu),
            user_history=list(self._hist_cpu_user),
            core_history=[list(history) for history in self._hist_core],
        )

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        # Linux btop-style used memory is total minus MemAvailable. psutil's
        # `used` can use a different accounting definition across platforms.
        used = max(0, int(mem.total) - int(mem.available))
        used_percent = (used / int(mem.total) * 100.0) if mem.total else 0.0
        self._hist_mem.append(used_percent)
        self._hist_mem = self._hist_mem[-self.HISTORY:]
        self._hist_swap.append(float(swap.percent))
        self._hist_swap = self._hist_swap[-self.HISTORY:]
        mem_info = MemInfo(
            total=int(mem.total),
            used=used,
            available=int(mem.available),
            cached=int(getattr(mem, "cached", 0) or 0),
            buffers=int(getattr(mem, "buffers", 0) or 0),
            percent=used_percent,
            swap_total=int(swap.total),
            swap_used=int(swap.used),
            swap_percent=float(swap.percent),
            history=list(self._hist_mem),
            swap_history=list(self._hist_swap),
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
            mounts=self._sample_mount_io(self._read_mounts(), now),
            temp_c=self._disk_temp_c,
        )

        procs, index = self.processes()
        (
            net_info.interfaces,
            net_info.listeners,
            net_info.websites,
        ) = self._network_interfaces(index, now)
        return cpu, mem_info, net_info, disk_info, procs, index

    @staticmethod
    def _route_source_ip(remote_ip: str) -> str:
        """Ask the kernel which source address it would route from, without sending data."""
        try:
            address = ipaddress.ip_address(remote_ip.split("%", 1)[0])
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect((remote_ip, 9))
                return str(probe.getsockname()[0]).split("%", 1)[0]
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _website_main_file(proc: Optional[ProcInfo]) -> str:
        if proc is None:
            return "-"
        try:
            cwd = Path(psutil.Process(proc.pid).cwd())
        except (psutil.Error, OSError):
            return "-"

        arguments = proc.cmdline or []
        root: Optional[Path] = None
        root_flags = ("--directory", "--root", "--root-dir", "--dir", "-d", "-r")
        for index, argument in enumerate(arguments):
            for flag in root_flags:
                if argument == flag and index + 1 < len(arguments):
                    root = Path(arguments[index + 1]).expanduser()
                    break
                if argument.startswith(flag + "="):
                    root = Path(argument.split("=", 1)[1]).expanduser()
                    break
            if root is not None:
                break
        if root is not None and not root.is_absolute():
            root = cwd / root
        root = root or cwd

        if root.is_file():
            return str(root)
        index_files = (
            "index.html", "public/index.html", "dist/index.html", "build/index.html",
            "site/index.html", "www/index.html",
        )
        for relative in index_files:
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)

        for argument in arguments:
            match = re.match(r"^([A-Za-z_][\w.]*)\s*:\s*(?:app|application)$", argument)
            if match:
                module = root / (match.group(1).replace(".", "/") + ".py")
                if module.is_file():
                    return str(module)
        for relative in (
            "app.py", "main.py", "server.py", "src/main.py", "src/app.py",
            "server.js", "index.js", "src/index.js",
        ):
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)
        return "-"

    @staticmethod
    def _probe_local_http_status(host: str, port: int, secure: bool) -> Optional[int]:
        connection = None
        try:
            if secure:
                context = ssl._create_unverified_context()
                connection = http.client.HTTPSConnection(host, port, timeout=0.12, context=context)
            else:
                connection = http.client.HTTPConnection(host, port, timeout=0.12)
            connection.request("HEAD", "/", headers={"Host": "localhost", "Connection": "close"})
            response = connection.getresponse()
            status = int(response.status)
            if status in (405, 501):
                response.close()
                connection.request(
                    "GET", "/",
                    headers={"Host": "localhost", "Connection": "close", "Range": "bytes=0-0"},
                )
                response = connection.getresponse()
                status = int(response.status)
            return status
        except (OSError, http.client.HTTPException, ssl.SSLError):
            return None
        finally:
            if connection is not None:
                connection.close()

    def _network_interfaces(
        self, proc_index: dict[int, ProcInfo], now: float
    ) -> tuple[list[NetInterfaceInfo], list[NetListenerInfo], list[HostedSiteInfo]]:
        try:
            counters = psutil.net_io_counters(pernic=True) or {}
            link_stats = psutil.net_if_stats()
            addresses = psutil.net_if_addrs()
        except Exception:
            return [], [], []

        interfaces: list[NetInterfaceInfo] = []
        address_map: dict[str, str] = {}
        for name, entries in addresses.items():
            for entry in entries:
                if entry.family in (socket.AF_INET, socket.AF_INET6):
                    address_map[entry.address.split("%", 1)[0]] = name
        for name, item in counters.items():
            rx, tx = int(item.bytes_recv), int(item.bytes_sent)
            previous = self._last_net_if.get(name)
            rx_rate = tx_rate = 0.0
            if previous:
                old_rx, old_tx, old_time = previous
                elapsed = max(1e-6, now - old_time)
                rx_rate = max(0.0, (rx - old_rx) / elapsed)
                tx_rate = max(0.0, (tx - old_tx) / elapsed)
            self._last_net_if[name] = (rx, tx, now)
            stats = link_stats.get(name)
            speed = float(stats.speed) * 1_000_000 / 8 if stats and stats.speed > 0 else None
            interfaces.append(NetInterfaceInfo(
                name=name,
                rx_rate=rx_rate,
                tx_rate=tx_rate,
                total_rx=rx,
                total_tx=tx,
                max_rate=speed,
            ))

        by_name = {item.name: item for item in interfaces}
        process_connections: dict[str, dict[int, int]] = {}
        process_rates = self._tcp_process_rates(address_map, now)
        try:
            connections = psutil.net_connections(kind="inet")
        except (psutil.Error, OSError, NotImplementedError):
            connections = []
        listeners_by_key: dict[tuple[int, int, str, str], NetListenerInfo] = {}
        website_candidates: dict[tuple[int, int, str], tuple[ProcInfo, str, str]] = {}
        common_web_ports = {80, 443, 3000, 3001, 4000, 5000, 5173, 5174, 8000, 8001, 8080, 8081, 8443, 8888}
        web_process_names = (
            "nginx", "apache", "httpd", "caddy", "traefik", "uvicorn", "gunicorn", "hypercorn",
            "flask", "django", "vite", "next", "webpack", "http.server", "php -s", "tomcat",
        )
        generic_web_runtime_pattern = re.compile(
            r"(?:node(?:js)?|python(?:3(?:\.\d+)?)?|ruby(?:\d+(?:\.\d+)?)?|"
            r"php(?:\d+(?:\.\d+)?)?|java|bun|deno)"
        )
        for connection in connections:
            pid = connection.pid
            remote = getattr(connection.raddr, "ip", "") or ""
            if not remote and connection.raddr:
                remote = str(connection.raddr[0])
            local = getattr(connection.laddr, "ip", "") or ""
            local_port = getattr(connection.laddr, "port", 0) or 0
            if not local and connection.laddr:
                local = str(connection.laddr[0])
                if len(connection.laddr) > 1:
                    local_port = int(connection.laddr[1])
            local = local.split("%", 1)[0]
            is_tcp_listener = (
                connection.type == socket.SOCK_STREAM and connection.status == psutil.CONN_LISTEN
            )
            is_udp_listener = connection.type == socket.SOCK_DGRAM and not remote
            if local_port > 0 and (is_tcp_listener or is_udp_listener):
                protocol = "TCP" if is_tcp_listener else "UDP"
                process = proc_index.get(pid) if pid is not None else None
                process_name = process.name if process else "-"
                command = process.cmd if process else ""
                display_address = local or "*"
                key = (pid or 0, local_port, protocol, display_address)
                listeners_by_key[key] = NetListenerInfo(
                    port=local_port,
                    protocol=protocol,
                    address=display_address,
                    pid=pid,
                    process=process_name,
                    command=command,
                )
                app_signature = f"{process_name} {command}".lower()
                runtime_name = process_name.lower().split("/", 1)[-1]
                is_web = is_tcp_listener and (
                    local_port in common_web_ports
                    or any(name in app_signature for name in web_process_names)
                    or generic_web_runtime_pattern.fullmatch(runtime_name) is not None
                )
                if is_web and pid is not None:
                    secure = local_port in (443, 8443) or any(name in app_signature for name in ("https", "ssl", "tls"))
                    scheme = "https" if secure else "http"
                    try:
                        address_ip = ipaddress.ip_address(local) if local else None
                    except ValueError:
                        address_ip = None
                    if address_ip is None or address_ip.is_unspecified:
                        host = "::1" if local and ":" in local else "127.0.0.1"
                    else:
                        host = str(address_ip)
                    shown_host = f"[{host}]" if ":" in host else host
                    website_candidates[(pid, local_port, scheme)] = (process, host, f"{scheme}://{shown_host}:{local_port}/")
                continue

            if pid is None:
                continue
            if connection.type == socket.SOCK_STREAM:
                if connection.status not in (
                    psutil.CONN_ESTABLISHED,
                    psutil.CONN_SYN_SENT,
                    psutil.CONN_SYN_RECV,
                ):
                    continue
            elif connection.type == socket.SOCK_DGRAM:
                if not remote:
                    continue
            else:
                continue
            try:
                if not local or ipaddress.ip_address(local).is_unspecified:
                    if remote:
                        local = self._route_source_ip(remote)
            except ValueError:
                continue
            interface = address_map.get(local)
            if interface not in by_name:
                continue
            counts = process_connections.setdefault(interface, {})
            counts[pid] = counts.get(pid, 0) + 1

        for name, counts in process_connections.items():
            by_name[name].processes = [
                NetProcessInfo(
                    pid,
                    proc_index[pid].name,
                    count,
                    *(process_rates.get((name, pid), (None, None))),
                )
                for pid, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
                if pid in proc_index
            ]

        listeners = sorted(
            listeners_by_key.values(), key=lambda item: (item.port, item.protocol, item.pid or 0, item.address)
        )
        if now >= self._website_probe_at:
            self._website_probe_at = now + 5.0
            self._website_status_cache = {
                key: self._probe_local_http_status(host, key[1], key[2] == "https")
                for key, (_process, host, _url) in sorted(
                    website_candidates.items(), key=lambda item: (item[0][1], item[0][0])
                )[:6]
            }
        websites = [
            HostedSiteInfo(
                url=url,
                main_file=self._website_main_file(process),
                status_code=self._website_status_cache.get(key),
                pid=key[0],
                process=process.name,
            )
            for key, (process, _host, url) in sorted(
                website_candidates.items(), key=lambda item: (item[0][1], item[0][0])
            )
        ]
        return interfaces, listeners, websites

    def _tcp_process_rates(
        self, address_map: dict[str, str], now: float
    ) -> dict[tuple[str, int], tuple[float, float]]:
        """Read Linux TCP_INFO byte counters when `ss` exposes them.

        Interface counters remain authoritative. Per-process figures are
        available only for TCP sockets reported by iproute2; other platforms,
        UDP sockets, permissions, or older `ss` builds display as unavailable.
        """
        if platform.system() != "Linux":
            self._last_socket_bytes.clear()
            return {}
        try:
            result = subprocess.run(
                ["ss", "-tinp"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=0.4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._last_socket_bytes.clear()
            return {}
        if result.returncode != 0:
            self._last_socket_bytes.clear()
            return {}

        blocks = re.split(r"(?m)^(?=ESTAB\s)", result.stdout)
        current: dict[tuple[int, str, str], tuple[int, int, float]] = {}
        rates: dict[tuple[str, int], list[float]] = {}
        for block in blocks:
            if not block.startswith("ESTAB"):
                continue
            pid_match = re.search(r"pid=(\d+)", block)
            ack_match = re.search(r"\bbytes_acked:(\d+)", block)
            recv_match = re.search(r"\bbytes_received:(\d+)", block)
            if not (pid_match and ack_match and recv_match):
                continue
            fields = block.splitlines()[0].split()
            if len(fields) < 5:
                continue
            pid = int(pid_match.group(1))
            local_endpoint, remote_endpoint = fields[3], fields[4]
            local_ip = _endpoint_ip(local_endpoint)
            interface = address_map.get(local_ip)
            if interface is None:
                continue
            key = (pid, local_endpoint, remote_endpoint)
            sent, received = int(ack_match.group(1)), int(recv_match.group(1))
            previous = self._last_socket_bytes.get(key)
            current[key] = (sent, received, now)
            if previous:
                old_sent, old_received, old_time = previous
                elapsed = max(1e-6, now - old_time)
                value = rates.setdefault((interface, pid), [0.0, 0.0])
                value[0] += max(0, received - old_received) / elapsed
                value[1] += max(0, sent - old_sent) / elapsed
        self._last_socket_bytes = current
        return {key: (value[0], value[1]) for key, value in rates.items()}

    def uptime(self) -> float:
        try:
            return max(0.0, time.time() - psutil.boot_time())
        except Exception:
            return 0.0

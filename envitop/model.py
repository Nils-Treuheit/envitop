"""Immutable-ish data structures shared by collectors, widgets and the app."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# System metrics
# ---------------------------------------------------------------------------


@dataclass
class CpuInfo:
    model_name: str = ""
    percent: float = 0.0
    user_percent: float = 0.0
    per_core: list[float] = field(default_factory=list)
    core_temps: list[Optional[float]] = field(default_factory=list)
    core_temp_source: str = ""
    freq_mhz: float = 0.0
    temp_c: Optional[float] = None
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0
    history: list[float] = field(default_factory=list)
    user_history: list[float] = field(default_factory=list)
    core_history: list[list[float]] = field(default_factory=list)


@dataclass
class MemInfo:
    total: int = 0
    used: int = 0
    available: int = 0
    cached: int = 0
    buffers: int = 0
    percent: float = 0.0
    swap_total: int = 0
    swap_used: int = 0
    swap_percent: float = 0.0
    history: list[float] = field(default_factory=list)
    swap_history: list[float] = field(default_factory=list)


@dataclass
class NetProcessInfo:
    pid: int
    name: str
    connections: int = 1
    rx_rate: Optional[float] = None
    tx_rate: Optional[float] = None


@dataclass
class NetInterfaceInfo:
    name: str
    rx_rate: float = 0.0
    tx_rate: float = 0.0
    total_rx: int = 0
    total_tx: int = 0
    max_rate: Optional[float] = None  # bytes/s, OS-reported link speed
    processes: list[NetProcessInfo] = field(default_factory=list)


@dataclass
class NetListenerInfo:
    port: int
    protocol: str
    address: str = ""
    pid: Optional[int] = None
    process: str = "-"
    command: str = ""


@dataclass
class HostedSiteInfo:
    url: str
    main_file: str = "-"
    status_code: Optional[int] = None
    pid: Optional[int] = None
    process: str = "-"


@dataclass
class NetInfo:
    rx_rate: float = 0.0
    tx_rate: float = 0.0
    total_rx: int = 0
    total_tx: int = 0
    rx_history: list[float] = field(default_factory=list)
    tx_history: list[float] = field(default_factory=list)
    interfaces: list[NetInterfaceInfo] = field(default_factory=list)
    listeners: list[NetListenerInfo] = field(default_factory=list)
    websites: list[HostedSiteInfo] = field(default_factory=list)


@dataclass
class MountInfo:
    mount: str
    used: int
    total: int
    percent: float
    free: int = 0
    device: str = ""
    read_rate: float = 0.0
    write_rate: float = 0.0
    read_history: list[float] = field(default_factory=list)
    write_history: list[float] = field(default_factory=list)
    filesystem: str = ""
    inodes_total: int = 0
    inodes_used: int = 0
    inodes_percent: Optional[float] = None


@dataclass
class DiskInfo:
    read_rate: float = 0.0
    write_rate: float = 0.0
    read_history: list[float] = field(default_factory=list)
    write_history: list[float] = field(default_factory=list)
    mounts: list[MountInfo] = field(default_factory=list)
    temp_c: Optional[float] = None


# ---------------------------------------------------------------------------
# GPU metrics
# ---------------------------------------------------------------------------


@dataclass
class GpuProcess:
    gpu_index: int
    pid: int
    name: str
    kind: str = "C"  # C = compute, G = graphics
    sm: Optional[float] = None
    gmbw_pct: Optional[float] = None  # per-process GPU memory-bandwidth activity
    vram: Optional[int] = None  # bytes; None when the per-process allocation is unavailable
    user: str = ""
    runtime_s: Optional[float] = None
    cpu_pct: Optional[float] = None  # host CPU share, normalized to total machine capacity
    host_mem_pct: Optional[float] = None  # process share of host RAM
    rss: int = 0  # host resident memory, bytes
    cmd: str = ""

    @property
    def sm_display(self) -> str:
        return "  -" if self.sm is None else f"{self.sm:3.0f}"

    @property
    def gmbw_display(self) -> str:
        return "  -" if self.gmbw_pct is None else f"{self.gmbw_pct:3.0f}"


@dataclass
class GpuInfo:
    index: int = 0
    name: str = "GPU"
    util: float = 0.0
    mem_used: int = 0
    mem_total: int = 0
    mem_percent: float = 0.0
    temp_c: Optional[float] = None
    power_w: Optional[float] = None
    power_limit_w: Optional[float] = None
    fan_pct: Optional[float] = None
    clock_sm: Optional[float] = None
    clock_mem: Optional[float] = None
    pstate: str = ""
    encoder: Optional[float] = None
    decoder: Optional[float] = None
    mem_util: Optional[float] = None  # memory-bandwidth utilization, percent
    history: list[float] = field(default_factory=list)
    mem_history: list[float] = field(default_factory=list)


@dataclass
class GpuState:
    available: bool = False
    error: str = ""
    gpus: list[GpuInfo] = field(default_factory=list)
    processes: list[GpuProcess] = field(default_factory=list)
    driver: str = ""
    cuda: str = ""

    @property
    def aggregate_util(self) -> float:
        """Mean device utilization, treating each visible GPU as a cluster member."""
        return sum(gpu.util for gpu in self.gpus) / len(self.gpus) if self.gpus else 0.0

    @property
    def aggregate_mem_util(self) -> Optional[float]:
        values = [gpu.mem_util for gpu in self.gpus if gpu.mem_util is not None]
        return sum(values) / len(values) if values else None

    @property
    def aggregate_mem_used(self) -> int:
        return sum(gpu.mem_used for gpu in self.gpus)

    @property
    def aggregate_mem_total(self) -> int:
        return sum(gpu.mem_total for gpu in self.gpus)

    @property
    def aggregate_mem_percent(self) -> float:
        total = self.aggregate_mem_total
        return self.aggregate_mem_used / total * 100.0 if total else 0.0

    @property
    def aggregate_power_w(self) -> Optional[float]:
        values = [gpu.power_w for gpu in self.gpus if gpu.power_w is not None]
        return sum(values) if values else None

    @property
    def aggregate_power_limit_w(self) -> Optional[float]:
        values = [gpu.power_limit_w for gpu in self.gpus if gpu.power_limit_w is not None]
        return sum(values) if values else None

    @property
    def hottest_temp_c(self) -> Optional[float]:
        values = [gpu.temp_c for gpu in self.gpus if gpu.temp_c is not None]
        return max(values) if values else None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


@dataclass
class ProcInfo:
    pid: int
    name: str
    user: str = ""
    cpu: float = 0.0  # percent of total host capacity, normalized across logical CPUs
    mem_pct: float = 0.0
    rss: int = 0
    cmd: str = ""
    status: str = ""
    threads: int = 0
    ppid: int = 0
    cwd: str = ""
    create_time: float = 0.0
    cmdline: list[str] = field(default_factory=list)
    gpu_index: Optional[int] = None
    gpu_sm: Optional[float] = None
    gpu_mem_pct: Optional[float] = None
    gpu_vram: int = 0


# ---------------------------------------------------------------------------
# AI agents
# ---------------------------------------------------------------------------


@dataclass
class ChildProc:
    pid: int
    command: str
    mem_kb: int = 0
    port: Optional[int] = None


@dataclass
class AgentSession:
    agent: str
    session_id: str
    project: str
    cwd: str = ""
    pid: int = 0
    status: str = "idle"
    model: str = ""
    effort: str = ""
    context_pct: Optional[float] = None
    context_tokens: int = 0
    context_window: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0
    turn_count: int = 0
    started_at: float = 0.0
    updated_at: float = 0.0
    mem_mb: int = 0
    git_branch: str = ""
    git_added: int = 0
    git_modified: int = 0
    version: str = ""
    current_task: str = ""
    summary: str = ""
    children: list[ChildProc] = field(default_factory=list)
    token_history: list[int] = field(default_factory=list)
    chat: list[tuple[str, str]] = field(default_factory=list)
    tools: list[tuple[str, str]] = field(default_factory=list)
    source_path: str = ""
    subagent_count: int = 0

    @property
    def agent_label(self) -> str:
        return {"claude": "CC", "codex": "CX", "opencode": "OC"}.get(self.agent, self.agent[:2].upper())

    @property
    def active_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write

    @property
    def is_active(self) -> bool:
        return self.status in ("thinking", "executing")


@dataclass
class RateLimit:
    source: str
    five_pct: Optional[float] = None
    five_reset: Optional[float] = None
    five_window: Optional[int] = None
    seven_pct: Optional[float] = None
    seven_reset: Optional[float] = None
    seven_window: Optional[int] = None
    updated_at: Optional[float] = None
    live: bool = False


# ---------------------------------------------------------------------------
# Aggregated view
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    ts: float = 0.0
    hostname: str = ""
    username: str = ""
    uptime: float = 0.0
    os_name: str = ""
    kernel: str = ""
    cpu: CpuInfo = field(default_factory=CpuInfo)
    mem: MemInfo = field(default_factory=MemInfo)
    net: NetInfo = field(default_factory=NetInfo)
    disk: DiskInfo = field(default_factory=DiskInfo)
    gpu: GpuState = field(default_factory=GpuState)
    procs: list[ProcInfo] = field(default_factory=list)
    task_count: int = 0
    thread_count: int = 0
    agents: list[AgentSession] = field(default_factory=list)
    quotas: list[RateLimit] = field(default_factory=list)
    token_rate: float = 0.0
    agent_scope: str = "recent"
    errors: list[str] = field(default_factory=list)

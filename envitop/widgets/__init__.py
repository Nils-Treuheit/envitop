"""Custom btop-flavoured widgets, grouped by dashboard component."""

from .agent_status import AgentPortsPanel, QuotaPanel, SessionDetail
from .agents import AgentTable, TokenOverview
from .chrome import Footer, Header
from .gpu import GpuPanel, GpuStatsPanel
from .gpu_processes import GpuProcTable
from .graphs import CoreBars, HistoryGraph, OneLine
from .insights import InsightsPanel, TimelinePanel
from .network import HostedSitesPanel, ListeningPortsPanel, NetDetailPanel
from .processes import CpuTopProcesses, ProcTable
from .storage import DiskPanel, FilesystemPanel

__all__ = [
    "OneLine",
    "HistoryGraph",
    "CoreBars",
    "GpuPanel",
    "GpuStatsPanel",
    "DiskPanel",
    "FilesystemPanel",
    "NetDetailPanel",
    "HostedSitesPanel",
    "ListeningPortsPanel",
    "CpuTopProcesses",
    "ProcTable",
    "GpuProcTable",
    "AgentTable",
    "TokenOverview",
    "AgentPortsPanel",
    "QuotaPanel",
    "SessionDetail",
    "TimelinePanel",
    "InsightsPanel",
    "Header",
    "Footer",
]

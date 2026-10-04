from __future__ import annotations

from dataclasses import dataclass

from custom_types import NetTrafficStats, ServerMetricsObj

__all__ = [
    "CPUInfo", "LoadAverage", "RamInfo", "SwapInfo", "SystemMemory",
    "IPList", "ConnCount", "AppMemory", "GCGenStats", "GCStats",
    "ThreadInfo", "HealthStatus", "FullSystemInfo", "PollingSystemInfo",
    "StateSnapshot",
]


@dataclass(slots=True, frozen=True, kw_only=True)
class CPUInfo:
    cores: int | None
    name: str
    mhz_max: float | int

@dataclass(slots=True, frozen=True, kw_only=True)
class LoadAverage:
    load_1m: float
    load_5m: float
    load_15m: float

@dataclass(slots=True, frozen=True, kw_only=True)
class RamInfo:
    total: int
    available: int
    used: int

@dataclass(slots=True, frozen=True, kw_only=True)
class SwapInfo:
    total: int
    free: int
    used: int

@dataclass(slots=True, frozen=True, kw_only=True)
class SystemMemory:
    ram: RamInfo
    swap: SwapInfo

@dataclass(slots=True, frozen=True, kw_only=True)
class IPList:
    ipv4: tuple[str, ...] | None
    ipv6: tuple[str, ...] | None

@dataclass(slots=True, frozen=True, kw_only=True)
class ConnCount:
    tcp: int
    udp: int

@dataclass(slots=True, frozen=True, kw_only=True)
class AppMemory:
    ram: float
    swap: float

@dataclass(frozen=True, slots=True, kw_only=True)
class GCGenStats:
    collections: int
    collected: int
    uncollectable: int

@dataclass(frozen=True, slots=True, kw_only=True)
class GCStats:
    gc_counts: tuple[int, int, int]
    gc_thresholds: tuple[int, int, int]
    gc_stats: tuple[GCGenStats, ...]

@dataclass(frozen=True, slots=True, kw_only=True)
class ThreadInfo:
    """One OS thread of this process.

    `cpu` is user + system seconds. `ctx_switches` is voluntary plus
    involuntary. `stack` is the thread stack reservation in bytes.
    Missing OS fields stay None so older snapshots still hydrate.
    """
    tid: int
    name: str
    state: str | None = None
    cpu: float | None = None
    ctx_switches: int | None = None
    stack: int | None = None

@dataclass(slots=True, frozen=True, kw_only=True)
class HealthStatus:
    uptime: float
    memory: AppMemory
    threads: int

@dataclass(slots=True, frozen=True, kw_only=True)
class FullSystemInfo:
    cpu: float
    process_count: int
    uptime: float
    cpu_info: CPUInfo
    loadavg: LoadAverage
    network: NetTrafficStats
    memory: SystemMemory
    ip: IPList
    connections: ConnCount

    app_memory: AppMemory
    app_uptime: float
    app_thread_amount: int
    app_threads: tuple[ThreadInfo, ...]
    app_gc_stats: GCStats


@dataclass(slots=True, frozen=True)
class StateSnapshot:
    """Host plus panel status captured together.

    Lives here because `host` is a `FullSystemInfo`. Importing that type
    from `custom_types` would cycle through `NetTrafficStats`.
    """
    ts: int
    host: FullSystemInfo
    panels: dict[str, ServerMetricsObj]

@dataclass(slots=True, frozen=True, kw_only=True)
class PollingSystemInfo:
    """Dynamic host fields for frequent polling.

    Static identity (`cpu_info`, `ip`, GC) stays on `full_info`.
    `uptime` and `app_uptime` are seconds and change every poll.
    """
    cpu: float
    process_count: int
    uptime: float
    loadavg: LoadAverage
    network: NetTrafficStats
    memory: SystemMemory
    connections: ConnCount
    app_memory: AppMemory
    app_uptime: float
    app_thread_amount: int
    app_threads: tuple[ThreadInfo, ...]

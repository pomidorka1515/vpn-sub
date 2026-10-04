from __future__ import annotations

import gc
import ipaddress
import os
import socket
import threading
import time

from pathlib import Path

import psutil
from dacite import from_dict

from custom_types import NetTrafficStats

from .types import (
    AppMemory,
    ConnCount,
    CPUInfo,
    FullSystemInfo,
    GCGenStats,
    GCStats,
    HealthStatus,
    IPList,
    LoadAverage,
    PollingSystemInfo,
    RamInfo,
    SwapInfo,
    SystemMemory,
    ThreadInfo,
)

__all__ = ["SysUtil"]


class SysUtil:
    """
    Fetch info about current state of the app and system. Linux-only.
    Not meant to be instantiated.
    """
    _cpu_lock = threading.Lock()
    _cpu_prev: tuple[int, int] | None = None

    def __init__(self) -> None:
        raise NotImplementedError("SysUtil is a namespace, not an instance")
    
    @staticmethod
    def _cpu_sample() -> tuple[int, int]:
        """Total and idle jiffies from the aggregate `/proc/stat` line."""
        with open('/proc/stat') as f:
            cpu_line = f.readline()
        # user, nice, system, idle, iowait, irq, softirq, steal
        parts = cpu_line.split()
        return sum(map(int, parts[1:])), int(parts[4])

    @staticmethod
    def _cpu_percent(previous: tuple[int, int], current: tuple[int, int]) -> float:
        total = current[0] - previous[0]
        idle = current[1] - previous[1]
        if total <= 0:
            return 0.0
        return round((total - idle) / total * 100, 1)

    @staticmethod
    def cpu(sleep: int | float = 0.3) -> float:
        """CPU % over interval. Needs two reads
        
        Args:
            sleep: amount of time to wait between calls, defaults to 0.3"""
        previous = SysUtil._cpu_sample()
        time.sleep(sleep)
        return SysUtil._cpu_percent(previous, SysUtil._cpu_sample())

    @staticmethod
    def cpu_since_last() -> float:
        """CPU % since the previous call. Does not sleep.

        The first call has no baseline and returns 0.0 after storing one.
        Later calls use the jiffies elapsed since that baseline, which is
        the interval the poller actually waited.
        """
        current = SysUtil._cpu_sample()
        with SysUtil._cpu_lock:
            previous = SysUtil._cpu_prev
            SysUtil._cpu_prev = current
        if previous is None:
            return 0.0
        return SysUtil._cpu_percent(previous, current)

    @staticmethod
    def cpu_info() -> CPUInfo:
        cores = os.cpu_count()
        
        with open('/proc/cpuinfo') as f:
            cpu_name = ""
            max_mhz: float | int = 0
            for line in f:
                if line.startswith('model name'):
                    cpu_name = line.split(':', 1)[1].strip()
                elif line.startswith('cpu MHz'):
                    mhz = float(line.split(':', 1)[1].strip())
                    max_mhz = max(max_mhz, mhz)
        
        return CPUInfo(
            cores=cores,
            name=cpu_name,
            mhz_max=max_mhz
        )

    @staticmethod
    def loadavg() -> LoadAverage:
        with open('/proc/loadavg') as f:
            one, five, fifteen = f.read().split()[:3]
        
        return LoadAverage(
            load_1m=float(one),
            load_5m=float(five),
            load_15m=float(fifteen)
        )

    @staticmethod
    def process_count() -> int:
        return len(list(Path('/proc').glob('[0-9]*')))

    @staticmethod
    def network() -> NetTrafficStats:
        tx, rx = 0, 0
        with open('/proc/net/dev') as f:
            f.readline()  # skip headers
            f.readline()
            for line in f:
                parts = line.split()
                if len(parts) >= 10:
                    iface = parts[0].rstrip(':')
                    if iface != 'lo':
                        rx += int(parts[1])
                        tx += int(parts[9])
        return NetTrafficStats(sent=tx, recv=rx)

    @staticmethod
    def uptime() -> float:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])

    @staticmethod
    def memory() -> SystemMemory:
        mem_data: dict[str, int] = {}
        
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    try:
                        mem_data[key] = int(parts[1]) * 1024
                    except ValueError:
                        continue
        
        ram_total = mem_data.get("MemTotal", 0)
        ram_available = mem_data.get("MemAvailable", ram_total)
        ram_used = ram_total - ram_available

        swap_total = mem_data.get("SwapTotal", 0)
        swap_free = mem_data.get("SwapFree", 0)
        swap_used = swap_total - swap_free

        return SystemMemory(
            ram=RamInfo(
                total=ram_total, 
                available=ram_available, 
                used=ram_used
            ),
            swap=SwapInfo(
                total=swap_total, 
                free=swap_free, 
                used=swap_used
            )
        )
    
    @staticmethod
    def _ip_is_reportable(ip: str, family: int) -> bool:
        """
        Keep globally reachable addresses. Loopback, link-local, and
        unspecified addresses are not a public IP; a hosts-file mapping of
        the hostname to 127.0.0.1 must not win over the real interface.
        """
        try:
            addr = ipaddress.ip_address(ip.split("%", 1)[0])
        except ValueError:
            return False
        if family == socket.AF_INET and not isinstance(addr, ipaddress.IPv4Address):
            return False
        if family == socket.AF_INET6 and not isinstance(addr, ipaddress.IPv6Address):
            return False
        return not (
            addr.is_loopback
            or addr.is_link_local
            or addr.is_unspecified
            or addr.is_multicast
        )

    @classmethod
    def _interface_ips(cls, family: int) -> tuple[str, ...]:
        found: list[str] = []
        seen: set[str] = set()
        for addrs in psutil.net_if_addrs().values():
            for addr in addrs:
                if addr.family != family:
                    continue
                ip = addr.address.split("%", 1)[0]
                if ip in seen or not cls._ip_is_reportable(ip, family):
                    continue
                seen.add(ip)
                found.append(ip)
        return tuple(found)

    @classmethod
    def ipaddr(cls) -> IPList:
        """Assigned interface addresses, excluding loopback and link-local."""
        ipv4 = cls._interface_ips(socket.AF_INET)
        ipv6 = cls._interface_ips(socket.AF_INET6)
        return IPList(
            ipv4=ipv4 or None,
            ipv6=ipv6 or None,
        )
    
    @staticmethod
    def connections() -> ConnCount:
        """TCP/UDP connection counts"""
        tcp_count = 0
        udp_count = 0
        
        for state_file in Path('/proc/net').glob('tcp*'):
            with open(state_file) as f:
                f.readline()  # skip header
                tcp_count += sum(1 for _ in f)
        
        for udp_file in Path('/proc/net').glob('udp*'):
            with open(udp_file) as f:
                f.readline()
                udp_count += sum(1 for _ in f)
        
        return ConnCount(
            tcp=tcp_count - 2,
            udp=udp_count - 2
        )


    @staticmethod
    def app_memory() -> AppMemory:
        """RSS and swap of this process and its children, in bytes.

        Callers format the values with `fmt_bytes`. Health uses the same
        object and labels the unit itself.
        """
        current_proc = psutil.Process()
        children = current_proc.children(recursive=True)
    
        total_rss: int = 0
        total_swap: int = 0
    
        for proc in [current_proc] + children:
            try:
                mem = proc.memory_full_info()
                total_rss += mem.rss
                total_swap += mem.swap
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    
        return AppMemory(
            ram=float(total_rss),
            swap=float(total_swap),
        )
    
    @staticmethod
    def app_uptime() -> float:
        with open('/proc/uptime') as f:
            system_uptime = float(f.read().split()[0])
    
        with open(f'/proc/{os.getpid()}/stat') as f:
            starttime_ticks = int(f.read().split()[21])
    
        clk_tck = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
        return system_uptime - (starttime_ticks / clk_tck)

    @staticmethod
    def app_thread_amount() -> int:
        return psutil.Process().num_threads()

    @staticmethod
    def _thread_stack(pid: int, tid: int) -> int | None:
        """Stack reservation from the thread status file, in bytes.

        psutil has no per-thread stack field. `VmStk` is the only size
        that belongs to the thread rather than the shared process maps.
        """
        path = f"{psutil.PROCFS_PATH}/{pid}/task/{tid}/status"
        try:
            with open(path, encoding="utf-8", errors="replace") as status:
                for line in status:
                    if not line.startswith("VmStk:"):
                        continue
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

    @staticmethod
    def app_threads() -> tuple[ThreadInfo, ...]:
        """OS threads of this process, ordered by tid.

        Name, state, CPU, and context switches come from psutil. Stack
        size is the one field psutil does not expose, so it is read from
        the thread status file. A thread that exits mid-read is skipped.
        """
        current = psutil.Process()
        found: list[ThreadInfo] = []
        for thread in current.threads():
            try:
                native = psutil.Process(thread.id)
                switches = native.num_ctx_switches()
                found.append(ThreadInfo(
                    tid=thread.id,
                    name=native.name(),
                    state=native.status(),
                    cpu=round(thread.user_time + thread.system_time, 1),
                    ctx_switches=switches.voluntary + switches.involuntary,
                    stack=SysUtil._thread_stack(current.pid, thread.id),
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return tuple(found)
    
    @staticmethod
    def health() -> HealthStatus:
        """Cheap process status. Does not sleep or touch the network."""
        return HealthStatus(
            uptime=SysUtil.app_uptime(),
            memory=SysUtil.app_memory(),
            threads=SysUtil.app_thread_amount(),
        )

    @staticmethod
    def app_gc_stats() -> GCStats:
        return GCStats(
            gc_counts=gc.get_count(), # objects in each generation (0, 1, 2)
            gc_thresholds=gc.get_threshold(), # collection thresholds
            gc_stats=tuple(from_dict(GCGenStats, stats) for stats in gc.get_stats()) # detailed per-generation stats
        )
    
    @classmethod
    def full_info(cls) -> FullSystemInfo:
        return FullSystemInfo(
            cpu=cls.cpu(0.2),
            process_count=cls.process_count(),
            uptime=cls.uptime(),
            cpu_info=cls.cpu_info(),
            loadavg=cls.loadavg(),
            network=cls.network(),
            memory=cls.memory(),
            ip=cls.ipaddr(),
            connections=cls.connections(),

            app_memory=cls.app_memory(),
            app_uptime=cls.app_uptime(),
            app_thread_amount=cls.app_thread_amount(),
            app_threads=cls.app_threads(),
            app_gc_stats=cls.app_gc_stats()
        )

    @classmethod
    def polling_info(cls) -> PollingSystemInfo:
        """Dynamic host fields only. CPU is since the last sample, no sleep."""
        return PollingSystemInfo(
            cpu=cls.cpu_since_last(),
            process_count=cls.process_count(),
            uptime=cls.uptime(),
            loadavg=cls.loadavg(),
            network=cls.network(),
            memory=cls.memory(),
            connections=cls.connections(),
            app_memory=cls.app_memory(),
            app_uptime=cls.app_uptime(),
            app_thread_amount=cls.app_thread_amount(),
            app_threads=cls.app_threads(),
        )

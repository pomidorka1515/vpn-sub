import psutil
import gc
import socket
import sys
import os
import time
import threading

from pathlib import Path
from dacite import from_dict

from custom_types import (
    CPUInfo, LoadAverage, NetTrafficStats, SystemMemory,
    RamInfo, SwapInfo, IPList, ConnCount, AppMemory,
    GCStats, GCGenStats, ThreadInfo, FullSystemInfo
)

def tuple_hook(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value

def parse_bool(value: object) -> bool | None:
    """Convert boolean-like values to actual bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.lower().strip()
        if value in ('true', 'yes', '1', 'on', 'y'):
            return True
        if value in ('false', 'no', '0', 'off', 'n'):
            return False
    if isinstance(value, int):
        return bool(value)
    return None

def fmt_bytes_tuple(value: int | float) -> tuple[str, str]:
    """
    Format bytes into a tuple.
    Returns:
        tuple[amount, label]  
        Example: ("193", "MB")
    """
    for unit, div in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if value >= div:
            return str(round(value / div, 2)), unit
    return str(round(value / 10**6, 2)), "MB"

def fmt_bytes(b: float) -> str:
    """Format bytes as short human-readable string. Does NOT return a tuple."""
    for unit, div in (('TB', 10**12), ('GB', 10**9), ('MB', 10**6), ('KB', 10**3)):
        if b >= div:
            return f'{b / div:.1f} {unit}'
    return f'{int(b)} B'


def fmt_time(seconds: int, lang: str = "ru") -> str:
    if lang not in ("ru", "en"):
        lang = "ru"
    t_s = "с" if lang == "ru" else "s"
    t_d = "д" if lang == "ru" else "d"
    t_m = "м" if lang == "ru" else "m"
    t_h = "ч" if lang == "ru" else "h"

    if seconds < 60:
        return f"{seconds}{t_s}"
    m, _ = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    
    if d > 0:
        return f"{d}{t_d} {h}{t_h} {m}{t_m}"
    if h > 0:
        return f"{h}{t_h} {m}{t_m}"
    return f"{m}{t_m}"

class SysUtil:
    """
    Fetch info about current state of the app and system. Linux-onlyю
    Most methods are static and should be called like `SysUtil.method()`.
    """
    def __init__(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("Linux-only utility class")

    @staticmethod
    def cpu(sleep: int | float = 0.3) -> float:
        """CPU % over interval. Needs two reads
        
        Args:
            sleep: amount of time to wait between calls, defaults to 0.3"""
        with open('/proc/stat') as f:
            cpu_line = f.readline()
        # user, nice, system, idle, iowait, irq, softirq, steal
        cpu1 = sum(map(int, cpu_line.split()[1:]))
        idle1 = int(cpu_line.split()[4])
        
        time.sleep(sleep)
        
        with open('/proc/stat') as f:
            cpu_line = f.readline()
        cpu2 = sum(map(int, cpu_line.split()[1:]))
        idle2 = int(cpu_line.split()[4])
        
        total = cpu2 - cpu1
        idle = idle2 - idle1
        return round((total - idle) / total * 100, 1)

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
    def _get_ip_family(family: int) -> tuple[str, ...] | None:
        """
        Resolves the local hostname to a list of unique IP addresses
        for the given address family, excluding wildcard/unspecified addresses.
        """
        try:
            hostname = socket.gethostname()
            addr_info = socket.getaddrinfo(hostname, None, family=family)
        except (socket.gaierror, OSError):
            return None

        unique_ips: set[str] = set()
        for item in addr_info:
            # item[4] is the sockaddr tuple, first element is always the IP string.
            ip = item[4][0]
            
            if not isinstance(ip, str):
                continue

            if ip not in ("0.0.0.0", "::"):
                unique_ips.add(ip)

        return tuple(unique_ips)
    
    @classmethod # get ip family needs to be called
    def ipaddr(cls) -> IPList:
        ipv4 = cls._get_ip_family(socket.AF_INET)
        ipv6 = cls._get_ip_family(socket.AF_INET6)
        
        return IPList(ipv4=ipv4, ipv6=ipv6)
    
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
        current_proc = psutil.Process()
        children = current_proc.children(recursive=True)
    
        total_rss: int = 0
        total_swap: int = 0
    
        for proc in [current_proc] + children:
            try:
                mem = proc.memory_info()
                total_rss += mem.rss
                total_swap += getattr(mem, 'swap', 0)
            except psutil.NoSuchProcess:
                pass
    
        return AppMemory(
            ram=round(total_rss / 1024 / 1024, 2),
            swap=round(total_swap / 1024 / 1024, 2)
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
        with open(f'/proc/{os.getpid()}/stat') as f:
            return int(f.read().split()[19])

    @staticmethod
    def app_threads() -> tuple[ThreadInfo, ...]:
        return tuple(
            ThreadInfo(
                name=t.name,
                ident=t.ident,
                daemon=t.daemon
            ) for t in threading.enumerate()
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

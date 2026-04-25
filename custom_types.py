from typing import TypedDict

### 3x-ui status object ###
class MemoryStats(TypedDict):
    current: int
    total: int
class SwapStats(TypedDict):
    current: int
    total: int
class DiskStats(TypedDict):
    current: int
    total: int
class XrayStats(TypedDict):
    state: str
    errorMsg: str
    version: str
class NetIOStats(TypedDict):
    up: int
    down: int
class NetTrafficStats(TypedDict):
    sent: int
    recv: int
class PublicIPStats(TypedDict):
    ipv4: str
    ipv6: str
class AppStats(TypedDict):
    threads: int
    mem: int
    uptime: int

class ServerMetricsObj(TypedDict):
    cpu: int
    cpuCores: int
    logicalPro: int
    cpuSpeedMhz: int
    mem: MemoryStats
    swap: SwapStats
    disk: DiskStats
    xray: XrayStats
    uptime: int
    loads: list[float]
    tcpCount: int
    udpCount: int
    netIO: NetIOStats
    netTraffic: NetTrafficStats
    publicIP: PublicIPStats
    appStats: AppStats

class ServerMetricsResponse(TypedDict):
    success: bool
    msg: str
    obj: ServerMetricsObj
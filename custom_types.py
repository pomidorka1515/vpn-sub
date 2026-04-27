from typing import (
    TypedDict, Protocol,
    runtime_checkable, Any, Iterable
)

### Bots ###

@runtime_checkable
class AdminBotLike(Protocol):
    """Stub protocol for AdminBot class to avoid circular imports."""

    def msg(self, text: str, parse_mode: str = "HTML") -> None: ...

@runtime_checkable
class PublicBotLike(Protocol):
    """Stub protocol for PublicBot."""

    def msg(
        self, 
        tgid: int | str | None,
        key: str,
        **kwargs: Any
    ) -> None: ... 

@runtime_checkable
class LinesConfigLike(Protocol):
    """Sub protocol for LinesConfig."""

    def append(
        self,
        record: dict[str, Any]
    ) -> None: ...

    def append_many(
        self, 
        records: Iterable[dict[str, Any]]
    ) -> None: ...

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

### 3x-ui inbound list object ###
class ClientStats(TypedDict):
    id: int
    inboundId: int
    enable: bool
    email: str
    uuid: str
    subId: str
    up: int
    down: int
    allTime: int
    expiryTime: int
    total: int
    reset: int
    lastOnline: int

class Inbound(TypedDict):
    id: int
    up: int
    down: int
    total: int
    allTime: int
    remark: str
    enable: bool
    expiryTime: int
    trafficReset: str
    lastTrafficResetTime: int
    clientStats: list[ClientStats]
    listen: str
    port: int
    protocol: str
    settings: str
    streamSettings: str
    tag: str
    sniffing: str

class InboundListResponse(TypedDict):
    success: bool
    msg: str
    obj: list[Inbound]

### 3x-ui client settings object ###
class SettingsClient(TypedDict):
    id: str
    flow: str
    email: str
    limitIp: int
    totalGB: int
    expiryTime: int
    enable: bool
    tgId: str | int
    subId: str
    comment: str
    reset: int

class InboundSettings(TypedDict):
    clients: list[SettingsClient]

### add_new_user() ###

class NewUserInfo(TypedDict):
    username: str
    token: str
    uuid: str
    fingerprint: str
    displayname: str

### register_with_code() ###

class RegisterWithCodeInfo(TypedDict):
    username: str
    token: str
    uuid: str
    fingerprint: str
    limit: int
    wl_limit: int
    time: int

### Code Object ###

class CodeObject(TypedDict):
    action: str
    perma: bool
    days: int
    gb: int
    wl_gb: int

### reset_user() ###
class ResetUserObject(TypedDict):
    uuid: str
    token: str

### get_info() ###
class UserInfoBandwidthTotal(TypedDict):
    upload: int | float
    download: int | float
    total: int | float

### apply_bonus_code() ###

class ApplyBonusCodeObject(TypedDict):
    days: int
    gb: int
    wl_gb: int
    perma: bool
    time: int
    limit: int
    wl_limit: int

### User Info ###

class UserInfoBandwidth(TypedDict):
    total: UserInfoBandwidthTotal
    wl_total: UserInfoBandwidthTotal
    monthly: int | float
    wl_monthly: int | float
    limit: int
    wl_limit: int

class UserInfo(TypedDict):
    _: str
    token: str
    link: str
    displayname: str
    uuid: str
    fingerprint: str
    enabled: bool
    wl_enabled: bool
    time: int
    online: bool
    bandwidth: UserInfoBandwidth

### Helper functions ###
def client_stats_to_settings(stats: ClientStats, flow: str = '', limit_ip: int = 0, tg_id: str | int = '', comment: str = '') -> SettingsClient:
    """Convert ClientStats to SettingsClient format.

    Args:
        stats: ClientStats object from inbound list response
        flow: Optional flow value (default: '')
        limit_ip: Optional IP limit (default: 0)
        tg_id: Optional Telegram ID (default: '')
        comment: Optional comment (default: '')
    """
    return {
        'id': stats['uuid'],
        'flow': flow,
        'email': stats['email'],
        'limitIp': limit_ip,
        'totalGB': stats['total'],
        'expiryTime': stats['expiryTime'],
        'enable': stats['enable'],
        'tgId': tg_id,
        'subId': stats['subId'],
        'comment': comment,
        'reset': stats['reset']
    }
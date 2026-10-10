from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol, TypedDict

from requests import Response
from requests.auth import AuthBase
from requests.cookies import RequestsCookieJar
from requests.models import PreparedRequest

from config.constants import JsonValue

# Dacite does not resolve PEP 695 recursive aliases when checking fields.
# Keep JSON checking for callers while its runtime check accepts raw objects.
if TYPE_CHECKING:
    _InboundValue = JsonValue
else:
    _InboundValue = object

__all__ = [

    'AppStats',
    'ApplyBonusCodeObject',
    'BandwidthInfo',
    'BandwidthSnapshot',
    'BandwidthUpdate',
    'BonusResult',
    'ClientListResponse',
    'ClientPayload',
    'ClientTraffic',
    'ClientTrafficResponse',
    'CodeObject',
    'CodeRecord',
    'DiskStats',
    'HTTPMethod',
    'Inbound',
    'InboundListResponse',
    'JsonifyValue',
    'MemoryStats',
    'MigrationReport',
    'NetIOStats',
    'NetTrafficStats',
    'NewUserInfo',
    'PanelClient',
    'PollingPanelInfo',
    'PublicIPStats',
    'RegisterCodeResult',
    'RegisterWithCodeInfo',
    'RequestKwargs',
    'ResetUserObject',
    'ServerMetricsObj',
    'ServerMetricsResponse',
    'SwapStats',
    'UserInfo',
    'UserInfoBandwidth',
    'UserInfoBandwidthTotal',
    'UserRecord',
    'XrayStats'
]


type JsonifyValue = str | int | float | bool | Mapping[str, 'JsonifyValue'] | Sequence['JsonifyValue'] | tuple['JsonifyValue', ...] | None
type HTTPMethod   = Literal['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']

class BandwidthInfo(NamedTuple):
    """
    Every value is in bytes.
    ' | float' is present because get_info formats these values into floats.
    bandwidth() itself returns int, always.
    """
    upload: int | float
    download: int | float
    total: int | float

    @staticmethod
    def format_bytes_mb(b: int) -> float:
        return round(b / 10**6, 2)

    def format_all_mb(self) -> BandwidthInfo:
        """Format everything into MB."""
        return BandwidthInfo(
            self.format_bytes_mb(int(self.upload)),
            self.format_bytes_mb(int(self.download)),
            self.format_bytes_mb(int(self.total)),
        )


class BandwidthUpdate(NamedTuple):
    delta: int
    current: BandwidthInfo

class OnlineStatus(NamedTuple):
    users: list[str] | dict[str, str | None]
    panel_health: dict[str, Literal["ok", "unavailable", "invalid"]]

# pyright: reportPrivateUsage=false
# pyright: reportUnnecessaryIsInstance=false
# pylint: disable=protected-access

### Stub Protocols ###


class SupportsKeysAndGetItem[KT, VT](Protocol):
    def keys(self) -> Iterable[KT]: ...
    def __getitem__(self, __k: KT) -> VT: ...
class SupportsRead[T_C](Protocol):
    def read(self, __length: int = ...) -> T_C: ...
class SupportsItems[K, V](Protocol):
    def items(self) -> Iterable[tuple[K, V]]: ...

### requests-stubs/session.pyi ###
_FileSpec = (
    SupportsRead[str | bytes] | str | bytes
    | tuple[str | None, SupportsRead[str | bytes] | str | bytes]
    | tuple[str | None, SupportsRead[str | bytes] | str | bytes, str]
    | tuple[str | None, SupportsRead[str | bytes] | str | bytes, str, Mapping[str, str]]
)

class RequestKwargs(TypedDict, total=False):
    # method: str | bytes
    # url: str | bytes
    params: (
        Mapping[str | bytes | int | float, str | bytes | int | float | Iterable[str | bytes | int | float] | None]
        | tuple[str | bytes | int | float, str | bytes | int | float | Iterable[str | bytes | int | float] | None]
        | Iterable[tuple[str | bytes | int | float, str | bytes | int | float | Iterable[str | bytes | int | float] | None]]
        | str
        | bytes
    ) | None
    data: Iterable[bytes] | str | bytes | SupportsRead[str | bytes] | list[tuple[str, str | bytes | int | float | bool | None]] | tuple[tuple[str, str | bytes | int | float | bool | None], ...] | Mapping[str, str | bytes | int | float | bool | None] | None
    headers: Mapping[str, str | bytes | None] | None
    cookies: RequestsCookieJar | MutableMapping[str, str] | None
    files: Mapping[str, _FileSpec] | Iterable[tuple[str, _FileSpec]] | None
    auth: tuple[str, str] | AuthBase | Callable[[PreparedRequest], PreparedRequest] | None
    timeout: float | tuple[float | None, float | None] | None
    allow_redirects: bool
    proxies: MutableMapping[str, str] | None
    hooks: Mapping[str, Iterable[Callable[[Response], object]] | Callable[[Response], object]] | None
    stream: bool | None
    verify: bool | str | None
    cert: str | tuple[str, str] | None
    json: JsonValue

### db.py ###

class UserRecord(TypedDict):
    username: str
    uuid: str
    token: str
    auth_token: str | None
    fingerprint: str
    displayname: str
    enabled: int
    enabled_time: int
    enabled_wl: int
    expires_at: int
    bw_limit_gb: int
    bw_used: int
    wl_limit_gb: int
    wl_used: int
    ext_username: str | None
    ext_password_hash: str | None
    created_at: int


class UserFields(TypedDict, total=False):
    userid: str
    uuid: str
    token: str
    fingerprint: str
    displayname: str
    status: bool | int
    status_time: bool | int
    status_wl: bool | int
    bw_limit: int
    bw_used: int
    wl_bw_limit: int
    wl_bw_used: int
    expiry_time: int
    ext_username: str | None
    ext_password: str | None
    tgid: str | int | None


class BandwidthSnapshotPayload(TypedDict):
    ts: int
    up: int
    down: int
    wl_up: int
    wl_down: int


class CodeRecord(TypedDict):
    code: str
    action: str
    perma: bool
    uses: int
    days: int
    gb: int
    wl_gb: int
    created_at: int


class RegisterCodeResult(TypedDict):
    days: int
    gb: int
    wl_gb: int
    uses: int
    perma: bool
    time: int


class BonusResult(RegisterCodeResult):
    limit: int
    wl_limit: int



@dataclass(frozen=True, slots=True)
class MigrationReport:
    users: int
    codes: int
    telegram_mappings: int
    bandwidth_snapshots: int
    state_snapshots: int
    backup_paths: tuple[str, ...]
    skipped_orphans: int = 0
    already_migrated: bool = False

### 3x-ui status object ###
@dataclass(slots=True, frozen=True, kw_only=True)
class MemoryStats:
    current: int
    total: int

@dataclass(slots=True, frozen=True, kw_only=True)
class SwapStats:
    current: int
    total: int

@dataclass(slots=True, frozen=True, kw_only=True)
class DiskStats:
    current: int
    total: int

@dataclass(slots=True, frozen=True, kw_only=True)
class XrayStats:
    state: str
    errorMsg: str
    version: str

@dataclass(slots=True, frozen=True, kw_only=True)
class NetIOStats:
    up: int
    down: int

@dataclass(slots=True, frozen=True, kw_only=True)
class NetTrafficStats:
    sent: int
    recv: int

@dataclass(slots=True, frozen=True, kw_only=True)
class PublicIPStats:
    ipv4: str
    ipv6: str

@dataclass(slots=True, frozen=True, kw_only=True)
class AppStats:
    threads: int
    mem: int
    uptime: int

@dataclass(slots=True, kw_only=True)
class ServerMetricsObj:
    cpu: int | float
    cpuCores: int
    logicalPro: int
    cpuSpeedMhz: int | float
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

    def format(self) -> None:
        """Format ugly values from 3x-ui's API. (e.g. 2999.98MHz, etc)"""
        self.cpu = round(self.cpu, 2)
        self.cpuSpeedMhz = int(self.cpuSpeedMhz)

@dataclass(slots=True, frozen=True, kw_only=True)
class ServerMetricsResponse:
    success: bool
    msg: str
    obj: ServerMetricsObj


### 3x-ui inbound list object ###
@dataclass(slots=True, frozen=True, kw_only=True)
class ClientTraffic:
    id: int
    inboundId: int
    enable: bool
    email: str
    uuid: str
    subId: str
    up: int
    down: int
    expiryTime: int
    total: int
    reset: int
    resetDay: int = 0
    resetMax: int = 0
    resetCount: int = 0
    lastOnline: int = 0
    lastSubFetch: int = 0


@dataclass(slots=True, frozen=True, kw_only=True)
class Inbound:
    id: int
    up: int
    down: int
    total: int
    remark: str
    enable: bool
    expiryTime: int
    trafficReset: str
    lastTrafficResetTime: int
    clientStats: list[ClientTraffic]
    listen: str
    port: int
    protocol: str
    settings: dict[str, _InboundValue]
    streamSettings: dict[str, _InboundValue]
    tag: str
    sniffing: dict[str, _InboundValue]
    subSortIndex: int = 0
    trafficResetDay: int = 0
    nodeId: int | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class InboundListResponse:
    success: bool
    msg: str
    obj: list[Inbound]

### 3x-ui clients-first API objects ###
@dataclass(slots=True, kw_only=True)
class ClientPayload: # NOTE: this must NOT be frozen
    email: str
    id: str
    flow: str
    limitIp: int
    totalGB: int
    expiryTime: int
    enable: bool
    tgId: str | int
    subId: str
    comment: str
    reset: int


@dataclass(slots=True, frozen=True, kw_only=True)
class PanelClient:
    email: str
    uuid: str
    subId: str
    enable: bool
    flow: str
    limitIp: int
    totalGB: int
    expiryTime: int
    tgId: str | int
    comment: str
    reset: int
    resetDay: int = 0
    resetMax: int = 0
    resetCount: int = 0
    inboundIds: list[int] = field(default_factory=list[int])
    traffic: ClientTraffic | None = None


@dataclass(slots=True, frozen=True, kw_only=True)
class ClientListResponse:
    success: bool
    msg: str
    obj: list[PanelClient]


@dataclass(slots=True, frozen=True, kw_only=True)
class ClientTrafficResponse:
    success: bool
    msg: str
    obj: ClientTraffic

### add_new_user() ###

@dataclass(slots=True, frozen=True, kw_only=True)
class NewUserInfo:
    username: str
    token: str
    uuid: str
    fingerprint: str
    displayname: str

### register_with_code() ###

@dataclass(slots=True, frozen=True, kw_only=True)
class RegisterWithCodeInfo:
    username: str
    token: str
    uuid: str
    fingerprint: str
    limit: int
    wl_limit: int
    time: int

### Code Object ###
@dataclass(slots=True, frozen=True, kw_only=True)
class CodeObject:
    code: str
    action: str
    perma: bool
    uses: int
    days: int
    gb: int
    wl_gb: int

### reset_user() ###
@dataclass(slots=True, frozen=True, kw_only=True)
class ResetUserObject:
    uuid: str
    token: str

### get_info() ###
@dataclass(slots=True, frozen=True, kw_only=True)
class UserInfoBandwidthTotal:
    upload: int | float
    download: int | float
    total: int | float

### apply_bonus_code() ###
@dataclass(slots=True, frozen=True, kw_only=True)
class ApplyBonusCodeObject:
    days: int
    gb: int
    wl_gb: int
    perma: bool
    uses: int
    time: int
    limit: int
    wl_limit: int

### User Info ###
@dataclass(slots=True, frozen=True, kw_only=True)
class UserInfoBandwidth:
    total: UserInfoBandwidthTotal
    wl_total: UserInfoBandwidthTotal
    monthly: int | float
    wl_monthly: int | float
    limit: int
    wl_limit: int

@dataclass(slots=True, frozen=True, kw_only=True)
class UserInfo:
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

@dataclass(slots=True, frozen=True, kw_only=True)
class PollingPanelInfo:
    """Dynamic panel fields for frequent polling.

    Static identity (`cpuCores`, `xray`, `publicIP`) stays on `getstatus`.
    `cpu` is the panel host percentage, not this process. `uptime` is the
    panel host seconds; `app_stats.uptime` is the panel app seconds.
    `netIO` is the instantaneous up/down rate; `netTraffic` is totals.
    """
    app_stats: AppStats
    cpu: int | float
    disk: DiskStats
    loads: list[float]
    mem: MemoryStats
    netIO: NetIOStats
    netTraffic: NetTrafficStats
    swap: SwapStats
    tcpCount: int
    udpCount: int
    uptime: int

### Snapshots ###
@dataclass(slots=True, frozen=True)
class BandwidthSnapshot:
    ts: int
    up: int = 0
    down: int = 0
    wl_up: int = 0
    wl_down: int = 0

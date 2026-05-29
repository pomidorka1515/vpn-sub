from __future__ import annotations

from typing import (
    Protocol,
    runtime_checkable, overload,
    Any, Iterable,
    Callable, Iterator, Self, Final,
    Literal, TypeVar, TypedDict
)
from types import TracebackType
from collections.abc import MutableMapping, Mapping, Sequence

from dataclasses import dataclass

from requests import Response
from requests.cookies import RequestsCookieJar
from requests.auth import AuthBase
from requests.models import PreparedRequest

__all__ = [
    'BaseValidator',
    
    'AdminBotLike', 'PublicBotLike', 
    'ConfigLike', 'LinesConfigLike',

    'MemoryStats', 'SwapStats', 'DiskStats', 'XrayStats',
    'NetIOStats', 'NetTrafficStats', 'PublicIPStats', 'AppStats',

    'ServerMetricsObj', 'ServerMetricsResponse', 'ClientStats',
    'Inbound', 'InboundListResponse', 'InboundSettings',

    'SettingsClient', 'NewUserInfo', 'RegisterWithCodeInfo',
    'CodeObject', 'ResetUserObject', 'ApplyBonusCodeObject',

    'UserInfoBandwidthTotal', 'UserInfoBandwidth', 'UserInfo',

    'client_stats_to_settings',

    'JsonValue', 'JsonDict', 'MISSING_TYPE',

    'RequestKwargs'
]

type JsonValue = int | float | Mapping[str, 'JsonValue'] | Sequence['JsonValue'] | str | bool | None
type JsonDict = dict[str, JsonValue]

class ValidatorMeta(type):
    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, object],
        base_type: type | tuple[type, ...] | None = None,
        validator_name: str = "",
    ) -> type:
        if base_type is not None:
            base_types = base_type if isinstance(base_type, tuple) else (base_type,)
            new_bases = tuple(bt for bt in base_types if bt not in bases)
            bases = new_bases + bases
        cls = super().__new__(mcs, name, bases, namespace)
        cls.field_name = validator_name
        cls.__allowed_types__ = base_type or ()
        cls.has_value_base = base_type is not None
        return cls

    field_name: str = ""
    has_value_base: bool
    __allowed_types__: type | tuple[type, ...] = ()

class BaseValidator(metaclass=ValidatorMeta):
    field_name: str = ""
    __allowed_types__: type | tuple[type, ...] = ()
    has_value_base: bool = False
    _value: object

    def __new__(cls, value: object) -> Self:
        if cls.has_value_base:
            try:
                instance = super().__new__(cls, value) # type: ignore[call-arg]
            except TypeError:
                instance = super().__new__(cls)
        else:
            instance = super().__new__(cls)
        instance._value = value
        # SONbrero what is this error (i have no idea)
        return instance # pyright: ignore[reportUnknownVariableType]

    def __init__(self, value: object) -> None:
        self._validate()

    def _validate(self) -> None:
        if not hasattr(self, "_value"):
            return
        allowed = self.__class__.__allowed_types__
        if allowed and not isinstance(self._value, allowed):
            raise ValueError(
                f"expected {allowed}, got {type(self._value)}"
            )
        self.validate()

    def validate(self) -> None:
        raise NotImplementedError

class MISSING_TYPE:
    """Sentinel for missing default values."""
    def __repr__(self) -> str:
        return "<MISSING>"

MISSING: Final[MISSING_TYPE] = MISSING_TYPE()
_T = TypeVar("_T")
_TJ = TypeVar("_TJ", bound=JsonValue)
_T_C = TypeVar("_T_C", covariant=True)
_K = TypeVar("_K", covariant=True)
_V = TypeVar("_V", covariant=True)
_KT = TypeVar("_KT", covariant=False)
_VT = TypeVar("_VT", covariant=True)

### Stub Protocols ###

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
class ConfigLike(Protocol):
    """Stub protocol for Config."""

    @property
    def path(self) -> str: ...

    @property
    def size(self) -> int: ...

    def close(self) -> None: ...

    def backup_now(self) -> None: ...

    def reload(self) -> bool: ...

    def edit(self) -> _ConfigTransactionLike: ...

    def mutate(self, callback: Callable[[MutableMapping[str, JsonValue]], _T]) -> _T: ...

    def __enter__(self) -> _ConfigTransactionLike: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None
    ) -> Literal[False] | None: ...

    def __getitem__(self, key: str) -> Any: ...

    def __setitem__(self, key: str, value: JsonValue) -> None: ...

    def __delitem__(self, key: str) -> None: ...

    def __contains__(self, key: str) -> bool: ...

    def __len__(self) -> int: ...

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get(self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def get(self, key: str, *, as_type: type[_T]) -> _T: ...

    @overload
    def get(self, key: str, default: MISSING_TYPE, *, as_type: type[_T]) -> _T: ...

    @overload
    def get(self, key: str, default: _TJ, *, as_type: type[_T]) -> _TJ | _T: ...

    @overload
    def get(
        self,
        key: str,
        default: _TJ | MISSING_TYPE = MISSING,
        *,
        as_type: type[_T] | None = None
    ) -> _TJ | _T: ...

    def get(
        self,
        key: str,
        default: JsonValue | MISSING_TYPE = MISSING,
        *,
        as_type: type[_T] | None = None
    ) -> Any: ...
    
    def __iter__(self) -> Iterator[str]: ...

    def keys(self) -> tuple[str, ...]: ...

    def values(self) -> tuple[JsonValue, ...]: ...

    def items(self) -> tuple[tuple[str, JsonValue], ...]: ...

    def copy(self) -> dict[str, Any]: ...
    
    def clear(self) -> None: ...

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop(self, key: str, default: _TJ) -> _TJ: ...

    def pop(self, key: str, default: _TJ | MISSING_TYPE = MISSING) -> JsonValue | _TJ: ...

    def popitem(self) -> tuple[str, JsonValue]: ...

    @overload
    def setdefault(self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue: ...

    @overload
    def update(self, **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Mapping[str, JsonValue], **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Iterable[tuple[str, JsonValue]], **kwargs: JsonValue) -> None: ...

    def update( # pyright: ignore[reportInconsistentOverload]
        self,
        __m: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] | None = None,
        **kwargs: JsonValue
    ) -> None: ...
    

@runtime_checkable
class _ConfigTransactionLike(Protocol):
    
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None
    ) -> Literal[False] | None: ...

    def __getitem__(self, key: str) -> Any: ...

    def __setitem__(self, key: str, value: JsonValue) -> None: ...

    def __delitem__(self, key: str) -> None: ...

    def __iter__(self) -> Iterator[str]: ...

    def __len__(self) -> int: ...

    def __contains__(self, key: str) -> bool: ...

    @overload
    def get(self, key: str) -> JsonValue: ...

    @overload
    def get(self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def get(self, key: str, *, as_type: type[_T]) -> _T: ...

    @overload
    def get(self, key: str, default: MISSING_TYPE, *, as_type: type[_T]) -> _T: ...

    @overload
    def get(self, key: str, default: _TJ, *, as_type: type[_T]) -> _TJ | _T: ...

    @overload
    def get(self, key: str, default: _TJ | MISSING_TYPE = MISSING, *, as_type: type[_T] | None = None) -> _TJ | _T: ...

    def get(
        self,
        key: str,
        default: JsonValue | MISSING_TYPE = MISSING,
        *,
        as_type: type[_T] | None = None
    ) -> Any: ...

    def copy(self) -> dict[str, Any]: ...

    def clear(self) -> None: ...

    @overload
    def pop(self, key: str) -> JsonValue: ...

    @overload
    def pop(self, key: str, default: _TJ) -> _TJ: ...

    def pop(self, key: str, default: _TJ | MISSING_TYPE = MISSING) -> JsonValue | _TJ: ...

    def popitem(self) -> tuple[str, JsonValue]: ...

    @overload
    def setdefault(self, key: str, default: _TJ) -> _TJ: ...

    @overload
    def setdefault(self, key: str, default: None = None) -> JsonValue: ...

    def setdefault(self, key: str, default: JsonValue = None) -> JsonValue: ...

    @overload
    def update(self, **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Mapping[str, JsonValue], **kwargs: JsonValue) -> None: ...

    @overload
    def update(self, __m: Iterable[tuple[str, JsonValue]], **kwargs: JsonValue) -> None: ...

    def update( # pyright: ignore[reportInconsistentOverload]
        self,
        __m: Mapping[str, JsonValue] | Iterable[tuple[str, JsonValue]] | None = None,
        **kwargs: JsonValue
    ) -> None: ...

@runtime_checkable
class LinesConfigLike(Protocol):
    """Stub protocol for LinesConfig."""

    @property
    def path(self) -> str: ...

    @property
    def size(self) -> int: ...

    def append(
        self,
        record: Mapping[str, JsonValue]
    ) -> None: ...

    def append_many(
        self, 
        records: Sequence[Mapping[str, JsonValue]]
    ) -> None: ...

    def __iter__(self) -> Iterator[JsonDict]: ...

    def tail(self, n: int = 100) -> Iterator[JsonDict]: ...

    def read_all(self) -> list[JsonDict]: ...

    def first(self, n: int = 1) -> list[JsonDict]: ...

    def count(self) -> int: ...

    def clear(self) -> None: ...

    def compact(self, keep: Callable[[Mapping[str, JsonValue]], bool] | None = None) -> tuple[int, int]: ...

    def backup_now(self) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False] | None: ...

class SupportsKeysAndGetItem(Protocol[_KT, _VT]):
    def keys(self) -> Iterable[_KT]: ...
    def __getitem__(self, __k: _KT) -> _VT: ...
class SupportsRead(Protocol[_T_C]):
    def read(self, __length: int = ...) -> _T_C: ...
class SupportsItems(Protocol[_K, _V]):
    def items(self) -> Iterator[tuple[_K, _V]]: ...

### requests-stubs/session.pyi ###
_FileSpec = SupportsRead[str | bytes] | str | bytes \
            | tuple[str | None, SupportsRead[str | bytes] | str | bytes] \
            | tuple[str | None, SupportsRead[str | bytes] | str | bytes, str] \
            | tuple[str | None, SupportsRead[str | bytes] | str | bytes, str, Mapping[str, str]]
_Params = (
    SupportsItems[str | bytes | int | float, str | bytes | int | float | Iterable[str | bytes | int | float] | None]
    | tuple[str | bytes | int | float, str | bytes | int | float | Iterable[str | bytes | int | float] | None]
    | Iterable[tuple[str | bytes | int | float, str | bytes | int | float | Iterable[str | bytes | int | float] | None]]
    | str
    | bytes
)
class RequestKwargs(TypedDict, total=False):
    # method: str | bytes
    # url: str | bytes
    params: _Params | None
    data: Iterable[bytes] | str | bytes | SupportsRead[str |  bytes] \
          | list[tuple[Any, Any]] | tuple[tuple[Any, Any], ...] \
          | Mapping[Any, Any] | None
    headers: Mapping[str, str | bytes | None] | SupportsKeysAndGetItem[str, str | bytes | None] | None
    cookies: None | RequestsCookieJar | MutableMapping[str, str]
    files: Mapping[str, _FileSpec] | Iterable[tuple[str, _FileSpec]] | None
    auth: tuple[str, str] | AuthBase | Callable[[PreparedRequest], PreparedRequest] | None 
    timeout: float | tuple[float | None, float | None] | None 
    allow_redirects: bool
    proxies: MutableMapping[str, str] | None 
    hooks: Mapping[str, Iterable[Callable[[Response], Any]] | Callable[[Response], Any]] | None
    stream: bool | None
    verify: bool | str | None
    cert: str | tuple[str, str] | None
    json: Any | None 

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
class ClientStats:
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


@dataclass(slots=True, frozen=True, kw_only=True)
class Inbound:
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


@dataclass(slots=True, frozen=True, kw_only=True)
class InboundListResponse:
    success: bool
    msg: str
    obj: list[Inbound]

### 3x-ui client settings object ###
@dataclass(slots=True, kw_only=True)
class SettingsClient: # NOTE: this must NOT be frozen
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

@dataclass(slots=True, frozen=True, kw_only=True)
class InboundSettings:
    clients: list[SettingsClient]

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

### Helper functions ###
def client_stats_to_settings(
    stats: ClientStats, 
    flow: str = '', 
    limit_ip: int = 0, 
    tg_id: str | int = '', 
    comment: str = ''
) -> SettingsClient:
    """Convert ClientStats to SettingsClient format.

    Args:
        stats: ClientStats object from inbound list response
        flow: Optional flow value (default: '')
        limit_ip: Optional IP limit (default: 0)
        tg_id: Optional Telegram ID (default: '')
        comment: Optional comment (default: '')
    """

    return SettingsClient(
        id=stats.uuid,
        flow=flow,
        email=stats.email,
        limitIp=limit_ip,
        totalGB=stats.total,
        expiryTime=stats.expiryTime,
        enable=stats.enable,
        tgId=tg_id,
        subId=stats.subId,
        comment=comment,
        reset=stats.reset
    )

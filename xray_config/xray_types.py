from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
import dacite
from typing import (
    Literal, Any, TypeVar, Callable
)
from functools import partial
from custom_types import BaseValidator

### XRAY CONFIG DATACLASSES ###
# Unused.. for now?
# NOTE: also incomplete, dont touch for now

QUERY_STRAT = Literal["UseIP", "UseIPv4", "UseIPv6", "UseSystem"]

class Port(int, BaseValidator, base_type=int):
    def validate(self) -> None:
        if not 1 <= self <= 65535:
            raise ValueError(f"{self} is not a valid port number")
class RewritePort(Port):
    def validate(self) -> None:
        if not 0 <= self <= 65535:
            raise ValueError(f"{self} is not a valid port number")
class PortString(str, BaseValidator, base_type=str):
    def validate(self) -> None:
        try:
            i = int(self)
        except ValueError:
            raise ValueError("port must be an integer inside a str object")
        
        if not 1 <= i <= 65535:
            raise ValueError(f"{self} is not a valid port number")
class PortRange(str, BaseValidator, base_type=str):
    def validate(self) -> None:
        if not self.strip():
            raise ValueError("Port range string cannot be empty")
        
        # if self.startswith("env:"):
        #     return
        
        parts = [p.strip() for p in self.split(",")]
        if any(not p for p in parts):
            raise ValueError(f"'{self}' contains an empty port specification")
        
        for part in parts:
            if '-' in part:
                range_parts = part.split('-')
                if len(range_parts) != 2:
                    raise ValueError(
                        f"Invalid range '{part}': expected format 'start-end'"
                    )
                try:
                    start = int(range_parts[0])
                    end = int(range_parts[1])
                except ValueError:
                    raise ValueError(
                        f"Range '{part}' contains non-numeric values"
                    )
                if not (1 <= start <= 65535 and 1 <= end <= 65535):
                    raise ValueError(
                        f"Ports in range '{part}' must be between 1 and 65535"
                    )
                if start > end:
                    raise ValueError(
                        f"Invalid range '{part}': start port {start} > end port {end}"
                    )
            else:
                try:
                    port = int(part)
                except ValueError:
                    raise ValueError(
                        f"Invalid port '{part}': must be an integer"
                    )
                if not (1 <= port <= 65535):
                    raise ValueError(
                        f"Port '{port}' must be between 1 and 65535"
                    )
class RegexOrEmail(str, BaseValidator, base_type=str):
    def validate(self) -> None:
        if not self.strip():
            raise ValueError("Value cannot be empty")

        if self.startswith("regexp:"):
            return
        else:
            if not re.match(r"^[^@]+@[^@]+\.[^@]+$", self):
                raise ValueError(
                    f"'{self}' is neither a valid email nor a regex pattern (use 'regex:...')"
                )
class Email(str, BaseValidator, base_type=str):
    def validate(self) -> None:
        if not self.strip():
            raise ValueError("value cannot be empty")
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", self):
            raise ValueError(
                f"'{self}' is not a valid email" 
            )
class CronExpression(str, BaseValidator, base_type=str, validator_name='cron'):
    _FIELD_RE = re.compile(r'^(\*|(\d+(-\d+)?)(/\d+)?|(\d+(,\d+)*))$')

    def validate(self) -> None:
        fields = self.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression requires exactly 5 fields, got {len(fields)}: {self!r}"
            )
        for i, field in enumerate(fields):
            if not self._FIELD_RE.match(field):
                raise ValueError(
                    f"cron field {i} is invalid: {field!r}"
                )
class Ciphers(str, BaseValidator, base_type=str):

    _CIPHERS: tuple[str, ...] = (
        "TLS_AES_128_GCM_SHA256",
        "TLS_AES_256_GCM_SHA384",
        "TLS_CHACHA20_POLY1305_SHA256",
        "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA",
        "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA",
        "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA",
        "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA",
        "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256",
        "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384",
        "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",
        "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384",
        "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
        "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256"
    )

    def validate(self) -> None:
        for cipher in self.split(':', -1):
            if cipher not in self._CIPHERS:
                raise ValueError(f'invalid cipher {cipher}')
class X25519Key(str, BaseValidator, base_type=str):
    _X25519_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
    
    def validate(self) -> None:
        if not self._X25519_RE.match(self):
            raise ValueError("x25519 key is not valid")
class MLDSA65Seed(X25519Key): pass
class MLDSA65Verify(str, BaseValidator, base_type=str):

    _RE = re.compile(r'^[A-Za-z0-9_-]+$')

    def validate(self) -> None:
        if not self:
            raise ValueError("verify key cannot be empty")
        if not self._RE.match(self):
            raise ValueError("verify key must contain only base64url characters")
        if len(self) != 2603:
            raise ValueError(f"verify key must be exactly 2603 chars, got {len(self)}")
class Version(str, BaseValidator, base_type=str):
    _VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')

    def validate(self) -> None:
        if not self._VERSION_RE.match(self):
            raise ValueError("version must be like 'x.y.z' with non-negative integers")
class ShortId(str, BaseValidator, base_type=str):
    _HEX_RE = re.compile(r'^[0-9a-fA-F]*$')

    def validate(self) -> None:
        if not self._HEX_RE.match(self):
            raise ValueError("shortId must contain only hex characters (0-9, a-f)")
        if len(self) > 16:
            raise ValueError("shortId must be at most 16 hex characters")
        if len(self) % 2 != 0:
            raise ValueError("shortId length must be even")
class WireguardKey(str, BaseValidator, base_type=str):

    _WG_RE = re.compile('[A-Za-z0-9+/=]+')
    
    def validate(self) -> None:
        if len(self) != 44:
            raise ValueError(f"wg ky length should be 44 chars, got {len(self)}")
        if not self[-1] == "=":
            raise ValueError("wg key must end with =")
        if not self._WG_RE.fullmatch(self):
            raise ValueError("wg key must contain valid characters")
class BrutalBandwidth(str, BaseValidator, base_type=str):
    _RE = re.compile(r'^(\d+)\s*([a-zA-Z]*)$')
    _MULTIPLIERS = {
        'k': 1_000,
        'm': 1_000_000,
        'g': 1_000_000_000,
        't': 1_000_000_000_000,
    }

    def validate(self) -> None:
        s = self.strip()
        if s == '0':
            return

        m = self._RE.match(s)
        if not m:
            raise ValueError(
                "Invalid format. Expected a number followed by optional unit (e.g. 100kb, 20 mb, 1g, 1 tbps)"
            )

        number_str, unit_str = m.groups()
        number = int(number_str)

        unit = unit_str.lower()
        if unit == '' or unit == 'bps':
            multiplier = 1
        else:
            if unit.endswith('bps'):
                prefix = unit[:-3]
            elif unit.endswith('b'):
                prefix = unit[:-1]
            else:
                prefix = unit
            
            if prefix not in self._MULTIPLIERS:
                raise ValueError(
                    f"Unknown unit prefix: '{prefix}'. Use k, m, g, t (optionally with 'b' or 'bps')"
                )
            multiplier = self._MULTIPLIERS[prefix]

        bps = number * multiplier

        if bps < 65_535:
            raise ValueError(
                f"Bandwidth must be at least 65535 bps, got {bps} bps (from '{self}')"
            )
class QuicMaxIdleTimeout(int, BaseValidator, base_type=int):
    def validate(self) -> None:
        if not 4 <= self <= 120:
            raise ValueError(f"maxIdleTimeout must be 4–120, got {self}")
class QuicKeepAlivePeriod(int, BaseValidator, base_type=int):
    def validate(self) -> None:
        if not 2 <= self <= 60:
            raise ValueError(f"keepAlivePeriod must be 2-60, got {self}")
class QuicMaxIncomingStreams(int, BaseValidator, base_type=int):
    def validate(self) -> None:
        if self < 8:
            raise ValueError(f"maxIncomingStreams must be > 8, got {self}")
class SimpleRange(str, BaseValidator, base_type=str):
    _RE = re.compile(r'^\d+-\d+$')

    def validate(self) -> None:
        if not self._RE.match(self):
            raise ValueError("range must be like '100-300' with non-negative integers separated by '-'")
class VLESSDecryption(str, BaseValidator, base_type=str):
    def validate(self) -> None:
        if self == "none":
            return
        
        if not self:
            raise ValueError("decryption cannot be empty. Use 'none' to disable.")
            
        blocks = self.split('.')
        
        if len(blocks) < 4:
            raise ValueError("decryption string must have at least 4 blocks (handshake, method, lifetime, auth) or be 'none'.")
        
        if blocks[0] != "mlkem768x25519plus":
            raise ValueError(f"first block (handshake) must be 'mlkem768x25519plus', got '{blocks[0]}'.")
            
        if blocks[1] not in ("native", "xorpub", "random"):
            raise ValueError(f"second block (method) must be 'native', 'xorpub', or 'random', got '{blocks[1]}'.")
            
        if not re.match(r'^(\d+-\d+|\d+)s$', blocks[2]):
            raise ValueError(f"third block (lifetime) must be in format 'Xs' or 'X-Ys', got '{blocks[2]}'.")
            
        auth_block = blocks[-1]
        if not re.match(r'^[A-Za-z0-9_-]+$', auth_block):
            raise ValueError("last block (auth) must be a valid base64url string.")
            
        middle_blocks = blocks[3:-1]
        if middle_blocks:
            for i, block in enumerate(middle_blocks):
                parts = block.split('-')
                if len(parts) != 3 or not all(p.isdigit() for p in parts):
                    raise ValueError(f"Padding/Delay block '{block}' must be in format 'probability-min-max' (all integers).")
                
                prob, min_val, max_val = map(int, parts)
                
                if i % 2 == 0:
                    if i == 0:
                        if prob != 100:
                            raise ValueError(f"First padding block probability must be 100, got {prob}.")
                        if min_val <= 0:
                            raise ValueError(f"First padding block minimum length must be > 0, got {min_val}.")
                else:
                    # delay logic (probability-min-max applies, no strict min>0 enforced by docs)
                    pass 
class VLESSEncryption(str, BaseValidator, base_type=str):
    def validate(self) -> None:
        if self == "none":
            return
            
        if not self:
            raise ValueError("encryption cannot be empty. Use 'none' to disable.")
            
        blocks = self.split('.')
        
        if len(blocks) < 4:
            raise ValueError("encryption string must have at least 4 blocks (handshake, method, session, auth) or be 'none'.")
        
        if blocks[0] != "mlkem768x25519plus":
            raise ValueError(f"first block (handshake) must be 'mlkem768x25519plus', got '{blocks[0]}'.")
            
        if blocks[1] not in ("native", "xorpub", "random"):
            raise ValueError(f"second block (method) must be 'native', 'xorpub', or 'random', got '{blocks[1]}'.")
            
        if blocks[2] not in ("0rtt", "1rtt"):
            raise ValueError(f"third block (session) must be '0rtt' or '1rtt', got '{blocks[2]}'.")
            
        auth_block = blocks[-1]
        if not re.match(r'^[A-Za-z0-9_-]+$', auth_block):
            raise ValueError("last block (auth) must be a valid base64url string.")
            
        middle_blocks = blocks[3:-1]
        if middle_blocks:
            for i, block in enumerate(middle_blocks):
                parts = block.split('-')
                if len(parts) != 3 or not all(p.isdigit() for p in parts):
                    raise ValueError(f"Padding/Delay block '{block}' must be in format 'probability-min-max' (all integers).")
                
                prob, min_val, max_val = map(int, parts)
                
                if i % 2 == 0:
                    if i == 0:
                        if prob != 100:
                            raise ValueError(f"first padding block probability must be 100, got {prob}.")
                        if min_val <= 0:
                            raise ValueError(f"first padding block minimum length must be > 0, got {min_val}.")
                else:
                    pass
__type_classes: tuple[type, ...] = ( 
    Port,
    RewritePort,
    PortString,
    PortRange,
    RegexOrEmail,
    Email,
    CronExpression,
    Ciphers,
    X25519Key,
    MLDSA65Verify,
    Version,
    ShortId,
    BrutalBandwidth,
    QuicMaxIdleTimeout,
    QuicKeepAlivePeriod,
    QuicMaxIncomingStreams,
    SimpleRange,
    VLESSDecryption,
    VLESSEncryption,
    WireguardKey
)

TYPE_HOOKS: dict[type, Callable[..., Any]] = {cls: partial(cls) for cls in __type_classes}
DACITE_CONFIG = dacite.Config(
    type_hooks=TYPE_HOOKS,
    strict=True
)


def _map_pass_keys(data: dict[str, object]) -> dict[str, object]:
    """Recursively map 'pass' -> '_pass' for dataclass JSON serialization."""
    result: dict[str, object] = {}
    for k, v in data.items():
        if k == "pass":
            result["_pass"] = _map_pass_keys(v) if isinstance(v, dict) else v
        elif isinstance(v, dict):
            result[k] = _map_pass_keys(v)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            result[k] = [_map_pass_keys(item) for item in v]
        else:
            result[k] = v
    return result


def _reverse_pass_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively map '_pass' -> 'pass' after dataclass serialization."""
    result: dict[str, Any] = {}
    for k, v in data.items():
        if k == "_pass":
            result["pass"] = _reverse_pass_keys(v) if isinstance(v, dict) else v
        elif isinstance(v, dict):
            result[k] = _reverse_pass_keys(v)
        elif isinstance(v, list):
            result[k] = [_reverse_pass_keys(item) for item in v]
        else:
            result[k] = v
    return result


T = TypeVar("T")


def from_dict(data_class: type[T], data: dict[str, Any]) -> T:
    return dacite.from_dict(data_class, _map_pass_keys(data), DACITE_CONFIG)


def to_dict(obj: Any) -> dict[str, Any]:
    return _reverse_pass_keys(asdict(obj))

NETWORK_MAP: dict[str, str] = {
    "raw": "rawSettings",
    "xhttp": "xhttpSettings",
    "mkcp": "kcpSettings",
    "grpc": "grpcSettings",
    "websocket": "wsSettings",
    "httpupgrade": "httpupgradeSettings",
    "hysteria": "hysteriaSettings",
    "reality": "realitySettings",
    "tls": "tlsSettings"
}
INBOUND_MAP: dict[str, str] = {
    "dokodemo-door": "TunnelInboundConfigurationObject",
    "http": "HTTPInboundConfigurationObject",
    "shadowsocks": "ShadowSocksInboundConfigurationObject",
    "socks": "SocksInboundConfigurationObject",
    "mixed": "SocksInboundConfigurationObject",
    "vless": "VLESSInboundConfigurationObject",
    "vmess": "VMessInboundConfigurationObject",
    "trojan": "TrojanInboundConfigurationObject",
    "wireguard": "WireGuardInboundConfigurationObject",
    "hysteria": "HysteriaInboundConfigurationObject",
    "tun": "TUNInboundConfigurationObject"
}
TLS_VER = Literal['1.0', '1.1', '1.2', '1.3']

@dataclass(kw_only=True, slots=True)
class LogObject:
    access: str = ""
    error: str = ""
    loglevel: Literal["debug", "info", "warning", "error", "none"] = "none"
    dnsLog: bool = False
    maskAddress: Literal['quarter', 'half', 'full'] | None = None

@dataclass(kw_only=True, slots=True)
class VersionObject:
    min: Version | None = None
    max: Version | None = None

@dataclass(kw_only=True, slots=True)
class ApiObject:
    tag: str
    listen: str | None = None
    services: list[str] | None = None

@dataclass(kw_only=True, slots=True)
class DnsServerObject:
    address: str
    port: Port = Port(53)
    domains: list[str] | None = None
    expectedIPs: list[str] | None = None
    unexpectedIPs: list[str] | None = None
    skipFallback: bool = False
    timeoutMs: int = 4000
    finalQuery: bool = False
    queryStrategy: QUERY_STRAT | None = None

    # Not present -> use global config
    serveExpiredTTL: int  | None = None
    serveStale:  bool | None = None
    disableCache: bool | None = None
    clientIp: str | None = None
    tag: str | None = None
@dataclass(kw_only=True, slots=True)
class DnsObject:
    tag: str
    hosts: dict[str, str | list[str]] | None = None
    servers: list[str | DnsServerObject] | None = None
    clientIp: str | None = None
    queryStrategy: QUERY_STRAT | None = None
    disableCache: bool = False
    serveStale:  bool = False
    serveExpiredTTL: int = 0
    disableFallback: bool = False
    disableFallbackIfMatch: bool = False
    enableParallelQuery: bool = False
    useSystemHosts: bool = False

@dataclass(kw_only=True, slots=True)
class WebhookObject:
    url: str
    deduplication: int | None = None
    headers: dict[str, str] | None = None
@dataclass(kw_only=True, slots=True)
class RuleObject:
    domain: list[str] | None = None
    ip: list[str] | None = None
    port: Port | PortRange | None = None
    sourcePort: Port | PortRange | None = None
    localPort: Port | PortRange | None = None
    network: Literal["tcp", "udp", "tcp,udp"] | None = None
    sourceIP: list[str] | None = None
    localIP: list[str] | None = None
    user: list[RegexOrEmail] | None = None
    vlessRoute: int | str | None = None
    inboundTag: list[str] | None = None
    protocol: list[Literal["http", "tls", "quic", "bittorrent"]] | None = None
    attrs: dict[str, str] | None = None
    process: list[str] | None = None
    outboundTag: str | None = None
    balancerTag: str | None = None
    ruleTag: str | None = None
    webhook: WebhookObject | None = None

@dataclass(kw_only=True, slots=True)
class CostObject:
    match: str
    value: float | None = None
    regexp: bool = False
@dataclass(kw_only=True, slots=True)
class StrategySettingsObject:
    expected: int | None = None
    maxRTT: str | None = None
    tolerance: float | None = None
    baselines: list[str] | None = None
    costs: list[CostObject] | None = None

@dataclass(kw_only=True, slots=True)
class StrategyObject:
    settings: StrategyObject
    type: Literal["random", "roundRobin", "leastPing", "leastLoad"] = "random"
    
@dataclass(kw_only=True, slots=True)
class BalancerObject:
    tag: str
    selector: list[str] | None = None
    fallbackTag: str | None = None
    strategy: StrategyObject | None = None

@dataclass(kw_only=True, slots=True)
class RoutingObject:
    domainStrategy: Literal["AsIs", "IPIfNonMatch", "IPOnDemand"] = "AsIs"
    rules: list[RuleObject] | None = None
    balancers: list[BalancerObject] | None = None

@dataclass(kw_only=True, slots=True)
class StatsObject:
    pass

@dataclass(kw_only=True, slots=True)
class MetricsObject:
    tag: str | None
    listen: str | None

    def __post_init__(self) -> None:
        if self.tag is None and self.listen is None:
            raise ValueError("metrics: either tag or listen field must be present")

@dataclass(kw_only=True, slots=True)
class ObservatoryObject:
    subjectSelector: list[str]
    probeUrl: str
    probeInterval: str | None = None
    enableConcurrency: bool | None = None

@dataclass(kw_only=True, slots=True)
class PingConfigObject:
    destination: str
    connectivity: str = ""
    interval: str = "1m"
    sampling: int = 10
    timeout: str = "5s"
    httpMethod: str = "HEAD"

@dataclass(kw_only=True, slots=True)
class BurstObservatoryObject:
    subjectSelector: list[str]
    pingConfig: PingConfigObject

@dataclass(kw_only=True, slots=True)
class AssetObject:
    url: str
    file: str

@dataclass(kw_only=True, slots=True)
class GeodataObject:
    cron: str | None = None
    outbound: str | None = None
    assets: list[AssetObject] | None = None

@dataclass(kw_only=True, slots=True)
class LevelPolicyObject:
    handshake: int = 4
    connIdle: int = 300
    uplinkOnly: int = 2
    downlinkOnly: int = 5
    statsUserUplink: bool = False
    statsUserDownlink: bool = False
    statsUserOnline: bool = True
    bufferSize: int = 10240
@dataclass(kw_only=True, slots=True)
class SystemPolicyObject:
    statsInboundUplink: bool = False
    statsInboundDownlink: bool = False
    statsOutboundUplink: bool = False
    statsOutboundDownlink: bool = False

@dataclass(kw_only=True, slots=True)
class PolicyObject:
    system: SystemPolicyObject
    level: dict[str, LevelPolicyObject] | None = None

@dataclass(kw_only=True, slots=True)
class FakeDnsObject:
    ipPool: str
    poolSize: int

@dataclass(kw_only=True, slots=True)
class SniffingObject:
    enabled: bool = False
    destOverride: list[Literal['https', 'tls', 'quic', 'fakedns']] | None = None
    metadataOnly: bool = False
    domainsExcluded: list[str] | None = None
    ipsExcluded: list[str] | None = None
    routeOnly: bool = False

@dataclass(kw_only=True, slots=True)
class TLSObject:
    serverName: str = ""
    verifyPeerCertByName: str | None = None
    rejectUnknownSni: bool = False
    alpn: list[Literal['http/1.1', 'h2', 'h3', 'FromMitM']] = field(default_factory=lambda: ["h2", "http/1.1"])
    minVersion: TLS_VER | None = None
    maxVersion: TLS_VER | None = None
    cipherSuites: Ciphers | None = None
    allowInsecure: bool = False
    disableSystemRoot: bool = False
    enableSessionResumption: bool = False

@dataclass(kw_only=True, slots=True)
class LimitFallbackObject:
    afterBytes: int = 0
    bytesPerSec: int = 0
    burstBytesPerSec: int | None = None

@dataclass(kw_only=True, slots=True)
class InboundRealityObject:
    target: str
    shortIds: list[ShortId]
    serverNames: list[str]
    privateKey: X25519Key
    minClientVer: Version | None = None
    maxClientVer: Version | None = None
    maxTimeDiff: int | None = None
    mldsa65Seed: MLDSA65Seed | None = None
    limitFallbackUpload: LimitFallbackObject | None = None
    limitFallbackDownload: LimitFallbackObject | None = None
    xver: int = 0
    show: bool = False

@dataclass(kw_only=True, slots=True)
class OutboundRealityObject:
    fingerprint: str
    shortId: ShortId
    password: X25519Key
    mldsa65Verify: MLDSA65Verify | None = None
    spiderX: str | None = None
    serverName: str = ""
    show: bool = False

@dataclass(kw_only=True, slots=True)
class FinalMaskTcpHeaderFakePacketRuleObject:
    delay: int = 0
    rand: int = 0
    randRange: SimpleRange = SimpleRange("0-255")
    type: Literal["array", "str", "hex", "base64"] = "array"
    packet: list[int] = field(default_factory=lambda: []) # will hold the raw bytes when type="array"

@dataclass(kw_only=True, slots=True)
class FinalMaskTcpHeaderCustomObject:
    clients: list[list[FinalMaskTcpHeaderFakePacketRuleObject]] = field(default_factory=lambda: [])
    servers: list[list[FinalMaskTcpHeaderFakePacketRuleObject]] = field(default_factory=lambda: [])
    errors: list[list[FinalMaskTcpHeaderFakePacketRuleObject]] = field(default_factory=lambda: [])


@dataclass(kw_only=True, slots=True)
class FinalMaskTcpFragmentObject:
    packets: str # 'tlshello' | '1-3', may vary, no literal
    length: SimpleRange = SimpleRange("100-200")
    delay: SimpleRange = SimpleRange("10-20")
    maxSplit: SimpleRange = SimpleRange("3-6")

@dataclass(kw_only=True, slots=True)
class FinalMaskSudokuObject:
    # NOTE: i fr have no idea of the precise types.
    # NOTE: as broad and permissive as it gets!
    password: str | None = None
    ascii: str | None = None
    customTable: str | None = None
    customTables: list[str] | None = None
    paddingMin: int | None = None
    paddingMax: int | None = None

@dataclass(kw_only=True, slots=True)
class FinalMaskTcpSettingsObject:
    type: Literal['header-custom', 'fragment', 'sudoku']
    settings: FinalMaskTcpFragmentObject | FinalMaskTcpHeaderCustomObject | FinalMaskSudokuObject

    def __post_init__(self) -> None:
        match self.type:
            case 'fragment':
                if not isinstance(self.settings, FinalMaskTcpFragmentObject):
                    raise ValueError(f"type is 'fragment', but settings is a different type")
            case 'header-custom':
                if not isinstance(self.settings, FinalMaskTcpHeaderCustomObject):
                    raise ValueError(f"type is 'header-custom', but settings is a different type")
            case 'sudoku':
                if not isinstance(self.settings, FinalMaskSudokuObject):
                    raise ValueError(f"type is 'sudoku', but settings is a different type")

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpHeaderCustomValueObject:
    rand: int = 0
    randRange: SimpleRange = SimpleRange("0-255")
    type: Literal['array', 'str', 'hex', 'base64'] = "array"
    packet: list[object] | None = None

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpHeaderCustomObject:
    client: list[FinalMaskUdpHeaderCustomValueObject] = field(default_factory=lambda: [])
    server: list[FinalMaskUdpHeaderCustomValueObject] = field(default_factory=lambda: [])

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpHeaderDnsObject:
    domain: str

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpMkcpAesObject:
    password: str

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpNoiseValueObject:
    rand: SimpleRange | None = None
    randRange: SimpleRange | None = None
    type: Literal['array', 'str', 'hex', 'base64'] = "array"
    packet: list[object] | None = None
    delay: str | None = None

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpNoiseObject:
    reset: int = 0
    noise: list[FinalMaskUdpNoiseValueObject] = field(default_factory=lambda: [])

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpSalamanderObject:
    password: str

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpXDnsObject:
    domain: str

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpXIcmpObject:
    listenIp: str
    id: int = 0

@dataclass(kw_only=True, slots=True)
class FinalMaskUdpSettingsObject:
    type: Literal[
        "header-custom",
        "header-dns",
        "header-dtls", # no settings
        "header-srtp", # no settings
        "header-utp", # no settings
        "header-wechat", # no settings
        "header-wireguard", # no settings
        "mkcp-original", # no settings
        "mkcp-aes128gcm",
        "noise",
        "salamander",
        "sudoku",
        "xdns",
        "xicmp",
    ]
    settings:   FinalMaskUdpHeaderCustomObject | FinalMaskUdpMkcpAesObject | FinalMaskUdpNoiseObject | FinalMaskUdpSalamanderObject \
              | FinalMaskUdpHeaderDnsObject    | FinalMaskUdpXDnsObject    | FinalMaskUdpXIcmpObject | FinalMaskSudokuObject \
              | None = None
    
    __SETTINGS_MAP = {
        'header-custom': FinalMaskUdpHeaderCustomObject,
        'header-dns': FinalMaskUdpHeaderDnsObject,
        'mkcp-aes128gcm': FinalMaskUdpMkcpAesObject,
        'noise': FinalMaskUdpNoiseObject,
        'salamander': FinalMaskUdpSalamanderObject,
        'sudoku': FinalMaskSudokuObject,
        'xdns': FinalMaskUdpXDnsObject,
        'xicmp': FinalMaskUdpXIcmpObject,
    }

    def __post_init__(self) -> None:
        expected_type = self.__SETTINGS_MAP.get(self.type, None)

        if expected_type is not None:
            if not isinstance(self.settings, expected_type):
                raise ValueError(
                    f"type '{self.type}' requires settings of type {expected_type.__name__}, "
                    f"got {type(self.settings).__name__}"
                )
        else:
            if self.settings is not None:
                raise ValueError(
                    f"type '{self.type}' does not accept settings, got {self.settings!r}"
                )

@dataclass(kw_only=True, slots=True)
class QuicParamsUdpHopObject:
    ports: PortRange
    interval: int = 30

    def __post_init__(self) -> None:
        if self.interval < 5:
            raise ValueError("interval must be less than 5")

@dataclass(kw_only=True, slots=True)
class QuicParamsObject:
    congestion: Literal['reno', 'bbr', 'brutal', 'force-brutal']
    debug: bool = False
    brutalUp: BrutalBandwidth | int | None = None
    brutalDown: BrutalBandwidth | int = 0
    udpHop: QuicParamsUdpHopObject | None = None
    initStreamReceiveWindow: int | None = None
    maxStreamReceiveWindow: int | None = None
    initConnectionReceiveWindow: int | None = None
    maxConnectionReceiveWindow: int | None = None
    maxIdleTimeout: QuicMaxIdleTimeout = field(default_factory=lambda: QuicMaxIdleTimeout(30))
    keepAlivePeriod: QuicKeepAlivePeriod | None = None
    maxIncomingStreams: QuicMaxIncomingStreams | None = None
    disablePathMTUDiscovery: bool | None = None
    
@dataclass(kw_only=True, slots=True)
class FinalMaskObject:
    tcp: list[FinalMaskTcpSettingsObject] | None = None
    udp: list[FinalMaskUdpSettingsObject] | None = None
    quicParams: QuicParamsObject | None = None

@dataclass(kw_only=True, slots=True)
class CustomSockoptObject:
    type: Literal["str", "int"]
    value: str | int
    level: str | int | None = None
    opt: str | int | None = None
    system: Literal["linux", "windows", "darwin"] | None = None

@dataclass(kw_only=True, slots=True)
class HappyEyeballsObject:
    tryDelayMs: int = 0
    prioritizeIPv6: bool = False
    interleave: int = 1
    maxConcurrentTry: int = 4

@dataclass(kw_only=True, slots=True)
class SockoptObject:
    mark: int | None = None
    tcpMaxSeg: int | None = None
    tcpFastOpen: bool | int | None = None
    tproxy: Literal["redirect", "tproxy", "off"] | None = None
    domainStrategy: Literal["AsIs", "UseIP",  "UseIPv6v4", "UseIPv6", "UseIPv4v6", "UseIPv4", 
                            "ForceIP", "ForceIPv6v4", "ForceIPv6", "ForceIPv4v6", "ForceIPv4"] = "AsIs"
    dialerProxy: str | None = None
    acceptProxyProtocol: bool = False
    trustedXForwardedFor: list[str] = field(default_factory=lambda: [])
    tcpKeepAliveIdle: int | None = None
    tcpKeepAliveInterval: int | None = None
    tcpUserTimeout: int | None = None
    tcpcongestion: str | None = None # NOTE: Literal['bbr', 'cubic', 'reno'] but accept arbitrary values
    interface: str | None = None
    V6Only: bool | None = None
    tcpWindowClamp: int | None = None
    tcpMptcp: bool = False
    addressPortStrategy: Literal["none", "SrvPortOnly", "SrvAddressOnly", "SrvPortAndAddress", "TxtPortOnly", "TxtAddressOnly", "TxtPortAndAddress"] = "none"
    customSockopt: list[CustomSockoptObject] | None = None
    happyEyeballs: HappyEyeballsObject | None = None

@dataclass(kw_only=True, slots=True)
class HTTPRequestObject:
    headers: dict[str, list[str] | str]
    version: str = "1.1"
    method: str = "GET"
    path: list[str] = field(default_factory=lambda: ["/"])
    

@dataclass(kw_only=True, slots=True)
class HTTPResponseObject:
    headers: dict[str, list[str] | str]
    version: str = "1.1"
    status: str = "200"
    reason: str = "OK"

@dataclass(kw_only=True, slots=True)
class HTTPHeaderObject:
    type: Literal["http"] = "http"
    request: HTTPRequestObject
    response: HTTPResponseObject

@dataclass(kw_only=True, slots=True)
class NoneHeaderObject:
    type: Literal["none"] = "none"

@dataclass(kw_only=True, slots=True)
class RawObject:
    acceptProxyProtocol: bool = False
    header: NoneHeaderObject | HTTPHeaderObject = field(default_factory=lambda: NoneHeaderObject())



@dataclass(kw_only=True, slots=True)
class XMUXObject:
    maxConcurrency: int | SimpleRange = 0
    maxConnections: int | SimpleRange = 0
    cMaxReuseTimes: int | SimpleRange = 0
    hMaxRequestTimes: int | SimpleRange = 0
    hMaxReusableSecs: int | SimpleRange = 0
    hKeepAlivePeriod: int = 0 

@dataclass(kw_only=True, slots=True)
class DownloadSettingsObject:
    address: str = ""
    port: int = 443
    network: Literal["xhttp"] = "xhttp"
    security: Literal["none", "tls", "reality"] = "none"
    tlsSettings: TLSObject | None = None
    realitySettings: InboundRealityObject | None = None
    xhttpSettings: XHTTPObject | None = None
    sockopt: SockoptObject | None = None

@dataclass(kw_only=True, slots=True)
class XHTTPExtraObject:
    headers: dict[str, str] = field(default_factory=lambda: {})
    xPaddingBytes: SimpleRange = field(default_factory=lambda: SimpleRange("100-1000"))
    noGRPCHeader: bool = False # client only
    noSSEHeader: bool = False  # server only
    scMaxEachPostBytes: int | SimpleRange = 1000000 # packet-up only
    scMinPostsIntervalMs: int | SimpleRange = 30    # packet-up, client only
    scMaxBufferedPosts: int = 30                    # packet-up, server only
    scStreamUpServerSecs: int | SimpleRange = field(default_factory=lambda: SimpleRange("20-80")) # stream-up, server only
    xmux: XMUXObject | None = None
    downloadSettings: DownloadSettingsObject | None = None

@dataclass(kw_only=True, slots=True)
class XHTTPObject:
    path: str
    host: str = ""
    mode: Literal["auto", "packet-up", "stream-up", "stream-one"] = "auto"
    extra: XHTTPExtraObject | None = None

@dataclass(kw_only=True, slots=True)
class KcpObject:
    mtu: int = 1350
    tti: int = 50
    uplinkCapacity: int = 5
    downlinkCapacity: int = 20
    congestion: bool = False
    readBufferSize: int = 2
    writeBufferSize: int = 2

@dataclass(kw_only=True, slots=True)
class GRPCBaseObject:
    authority: str = ""
    serviceName: str = ""

class InboundGRPCObject(GRPCBaseObject): pass

class OutboundGRPCObject(GRPCBaseObject):
    user_agent: str = ""
    multiMode: bool = False
    idle_timeout: int | None = None
    health_check_timeout: int = 20
    permit_without_stream: bool = False
    initial_windows_size: int = 0

@dataclass(kw_only=True, slots=True)
class WebSocketObject:
    acceptProxyProtocol: bool | None = None
    path: str = "/"
    host: str = ""
    headers: dict[str, str] = field(default_factory=lambda: {})
    heartbeatPeriod: int | None = None

@dataclass(kw_only=True, slots=True)
class HTTPUpgradeObject:
    acceptProxyProtocol: bool | None = None
    path: str = "/"
    host: str = ""
    headers: dict[str, str] = field(default_factory=lambda: {})

@dataclass(kw_only=True, slots=True)
class HysteriaMasqObject:
    type: Literal["file", "proxy", "string", ""] = ""
    dir: str | None = None
    url: str | None = None
    rewriteHost: bool | None = None
    insecure: bool | None = None
    content: str | None = None
    headers: dict[str, str] | None = None
    statusCode: int | None = None

@dataclass(kw_only=True, slots=True)
class HysteriaObject:
    version: Literal[2] = 2
    auth: str
    udpIdleTimeout: int = 60
    masquerade: HysteriaMasqObject | None = None

@dataclass(kw_only=True, slots=True)
class InboundStreamSettingsObject:
    network: Literal["raw", "xhttp", "mkcp", "grpc", "websocket", "httpupgrade", "hysteria"]
    security: Literal['none', 'reality', 'tls']
    rawSettings: RawObject | None = None
    xhttpSettings: XHTTPObject | None = None
    kcpSettings: KcpObject | None = None
    grpcSettings: InboundGRPCObject | None = None
    wsSettings: WebSocketObject | None = None
    httpupgradeSettings: HTTPUpgradeObject | None = None
    hysteriaSettings: HysteriaObject | None = None

    realitySettings: InboundRealityObject | None = None
    tlsSettings: TLSObject | None = None

    finalmask: FinalMaskObject | None = None
    sockopt: SockoptObject | None = None

    def __post_init__(self) -> None:
        for value, settings_field in NETWORK_MAP.items():
            if self.network == value and getattr(self, settings_field, None) is None:
                raise ValueError(
                    f"Network is '{value}' but {settings_field} is missing"
                )
            if self.security == value and getattr(self, settings_field, None) is None:
                raise ValueError(
                    f"Security is '{value}' but {settings_field} is missing"
            )

@dataclass(kw_only=True, slots=True)
class InboundObject:
    tag: str
    listen: str
    port: Port | PortRange # No support for env:... because who the fuck uses it
    protocol: Literal["dokodemo-door", "http", "shadowsocks", "mixed", "vless", "vmess", "trojan", "wireguard", "hysteria"]
    settings:   TunnelInboundConfigurationObject    | HTTPInboundConfigurationObject     | ShadowSocksInboundConfigurationObject \
              | SocksInboundConfigurationObject     | VLESSInboundConfigurationObject    | VMessInboundConfigurationObject       \
              | WireGuardInboundConfigurationObject | HysteriaInboundConfigurationObject | TUNInboundConfigurationObject
    streamSettings: InboundStreamSettingsObject
    sniffing: SniffingObject | None = None

    def __post_init__(self) -> None:
        for value, field in INBOUND_MAP.items():
            if self.protocol == value and getattr(self, field, None) is None:
                raise ValueError(f"protocol is {value} but {field} is missing")

@dataclass(kw_only=True, slots=True)
class FallbackObject:
    name: str = ""
    alpn: str = ""
    path: str = ""
    port: str | int
    xver: int = 0


@dataclass(kw_only=True, slots=True)
class TunnelInboundConfigurationObject:
    allowedNetwork: Literal["tcp", "udp", "tcp,udp"] = "tcp"
    rewriteAddress: str = "localhost"
    rewritePort: RewritePort | None = None
    port_map: dict[str, str] | None = None
    followRedirect: bool | None = None
    userLevel: int = 0

    def __post_init__(self) -> None:
        if self.port_map is not None:
            self.port_map = {PortString(k): v for k, v in self.port_map.items()}

@dataclass(kw_only=True, slots=True)   
class UserObject:
    user: str
    pass_: str

@dataclass(kw_only=True, slots=True)
class HTTPInboundConfigurationObject:
    users: list[UserObject] = field(default_factory=lambda: [])
    allowTransparent: bool = False
    userLevel: int = 0

@dataclass(kw_only=True, slots=True)
class ShadowSocksUserObject:
    password: str
    method: str  | None = None
    level: int = 0
    email: str | None = None
    

@dataclass(kw_only=True, slots=True)
class ShadowSocksInboundConfigurationObject:
    network: Literal["tcp", "udp", "tcp,udp"] = "tcp"
    method: Literal[
        "2022-blake3-aes-128-gcm",
        "2022-blake3-aes-256-gcm", 
        "2022-blake3-chacha20-poly1305",
        "aes-256-gcm",
        "aes-128-gcm",
        "chacha20-poly1305",
        "chacha20-ietf-poly1305",
        "xchacha20-poly1305",
        "xchacha20-ietf-poly1305",
        "none",
        "plain",
    ] | None = None
    password: str
    level: int = 0
    email: str | None = None
    users: list[ShadowSocksUserObject] | None = None
    
@dataclass(kw_only=True, slots=True)
class SocksInboundConfigurationObject:
    auth: Literal["noauth", "password"] = "noauth"
    users: list[UserObject] | None = None
    udp: bool = False
    ip: str | None = None
    userLevel: int = 0

@dataclass(kw_only=True, slots=True)
class TrojanUserObject:
    password: str
    email: str | None = None
    level: int = 0

@dataclass(kw_only=True, slots=True)
class TrojanInboundConfigurationObject:
    users: list[TrojanUserObject] | None = None
    fallbacks: list[FallbackObject] | None = None

@dataclass(kw_only=True, slots=True)
class VLESSReverseObject:
    tag: str

@dataclass(kw_only=True, slots=True)
class VLESSUserObject:
    id: str
    level: int = 0
    email: str | None = None
    flow: str | None = None
    reverse: VLESSReverseObject | None = None

@dataclass(kw_only=True, slots=True)
class VLESSInboundConfigurationObject:
    flow: str | None = None
    decryption: VLESSDecryption
    fallbacks: list[FallbackObject] | None = None
    users: list[VLESSUserObject] | None = None

@dataclass(kw_only=True, slots=True)
class VMessUserObject:
    id: str
    level: int = 0
    email: str | None = None

@dataclass(kw_only=True, slots=True)
class VMessDefaultObject:
    level: int = 0
    
@dataclass(kw_only=True, slots=True)
class VMessInboundConfigurationObject:
    users: list[VMessUserObject] | None = None
    default: VMessDefaultObject | None = None

@dataclass(kw_only=True, slots=True)
class WireGuardPeerObject:
    publicKey: WireguardKey
    allowedIps: list[str] = field(default_factory=lambda: ["0.0.0.0/0", "::/0"])

@dataclass(kw_only=True, slots=True)
class WireGuardInboundConfigurationObject:
    secretKey: WireguardKey
    mtu: int = 1420
    peers: WireGuardPeerObject | None = None

@dataclass(kw_only=True, slots=True)
class HysteriaUserObject:
    auth: str
    level: int = 0
    email: str | None = None

@dataclass(kw_only=True, slots=True)
class HysteriaInboundConfigurationObject:
    version: Literal[2] = 2
    users: list[UserObject] = field(default_factory=lambda: [])

@dataclass(kw_only=True, slots=True)
class TUNInboundConfigurationObject:
    name: str = "xray0"
    mtu: int = 1500
    gateway: list[str] | None = None # ip
    dns: list[str] | None = None 
    userLevel: int = 0
    autoSystemRoutingTable: list[str] | None = None
    autoOutboundsInterface: str | None = None
    




@dataclass(kw_only=True, slots=True)
class XrayConfig:
    log: LogObject | None = None
    api: ApiObject | None = None
    dns: DnsObject | None = None
    routing: RoutingObject | None = None
    policy: PolicyObject | None = None
    inbounds: list[InboundObject] | None = None
    outbounds: list[None] | None = None # TODO
    stats: StatsObject | None = None
    fakedns: list[FakeDnsObject] | FakeDnsObject | None = None 
    metrics: MetricsObject | None = None
    observatory: ObservatoryObject | None = None
    burstObservatory: BurstObservatoryObject | None = None
    geodata: GeodataObject | None = None
    version: VersionObject | None = None

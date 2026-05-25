from dataclasses import dataclass
from typing import Literal

from .validators import *
from .stream import (
    OutboundStreamSettingsObject,
    InboundStreamSettingsObject
)
from .protocols import *

__all__ = [
    # config root
    "XrayConfig",
    # objects
    "ApiObject",
    "AssetObject",
    "BalancerObject",
    "BurstObservatoryObject",
    "CostObject",
    "DnsObject",
    "DnsServerObject",
    "FakeDnsObject",
    "GeodataObject",
    "InboundObject",
    "LevelPolicyObject",
    "LogObject",
    "MetricsObject",
    "MuxObject",
    "ObservatoryObject",
    "OutboundObject",
    "OutboundProxySettingsObject",
    "PingConfigObject",
    "PolicyObject",
    "RoutingObject",
    "RuleObject",
    "SniffingObject",
    "StatsObject",
    "StrategyObject",
    "StrategySettingsObject",
    "SystemPolicyObject",
    "VersionObject",
    "WebhookObject",
    # maps
    "INBOUND_MAP",
    "OUTBOUND_MAP",
    # types
    "QUERY_STRAT",
]

OUTBOUND_MAP: dict[str, type] = {
    "http": HTTPOutboundConfigurationObject,
    "shadowsocks": ShadowSocksOutboundConfigurationObject,
    "socks": SocksOutboundConfigurationObject,
    "vless": VLESSOutboundConfigurationObject,
    "vmess": VMessOutboundConfigurationObject,
    "trojan": TrojanOutboundConfigurationObject,
    "wireguard": WireGuardOutboundConfigurationObject,
    "hysteria": HysteriaOutboundConfigurationObject,
    "blackhole": BlackholeOutboundConfigurationObject,
    "dns": DNSOutboundConfigurationObject,
    "freedom": FreedomOutboundConfigurationObject,
    "loopback": LoopbackOutboundConfigurationObject
}

INBOUND_MAP: dict[str, type] = {
    "dokodemo-door": TunnelInboundConfigurationObject,
    "http": HTTPInboundConfigurationObject,
    "shadowsocks": ShadowSocksInboundConfigurationObject,
    "socks": SocksInboundConfigurationObject,
    "mixed": SocksInboundConfigurationObject,
    "vless": VLESSInboundConfigurationObject,
    "vmess": VMessInboundConfigurationObject,
    "trojan": TrojanInboundConfigurationObject,
    "wireguard": WireGuardInboundConfigurationObject,
    "hysteria": HysteriaInboundConfigurationObject,
    "tun": TUNInboundConfigurationObject
}

QUERY_STRAT = Literal["UseIP", "UseIPv4", "UseIPv6", "UseSystem"]


@dataclass(kw_only=True, slots=True)
class SniffingObject:
    enabled: bool = False
    destOverride: list[Literal['https', 'tls', 'quic', 'fakedns']] | None = None
    metadataOnly: bool = False
    domainsExcluded: list[str] | None = None
    ipsExcluded: list[str] | None = None
    routeOnly: bool = False

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
    type: str
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
    tag: str | None = None
    listen: str | None = None

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
class OutboundProxySettingsObject:
    tag: str | None = None
    transportLayer: bool = False

@dataclass(kw_only=True, slots=True)
class MuxObject:
    enabled: bool = False
    concurrency: int | None = None
    xudpConcurrency: int | None = None
    xudpProxyUDP443: Literal["reject", "allow", "skip"] = "reject"
    
@dataclass(kw_only=True, slots=True)
class InboundObject:
    tag: str
    listen: str
    port: Port | PortRange # No support for env:... because who the fuck uses it
    protocol: Literal["dokodemo-door", "http", "shadowsocks", "mixed", "vless", "vmess", "trojan", "wireguard", "hysteria"]
    settings: (
        TunnelInboundConfigurationObject
    |   HTTPInboundConfigurationObject
    |   ShadowSocksInboundConfigurationObject
    |   SocksInboundConfigurationObject
    |   VLESSInboundConfigurationObject
    |   VMessInboundConfigurationObject
    |   WireGuardInboundConfigurationObject
    |   HysteriaInboundConfigurationObject
    |   TUNInboundConfigurationObject
    |   None
    ) = None
    streamSettings: InboundStreamSettingsObject | None = None
    sniffing: SniffingObject | None = None

    # def __post_init__(self) -> None:
    #     for value, field in INBOUND_MAP.items():
    #         if self.protocol == value and not isinstance(self.settings, field):
    #             raise ValueError(f"protocol is {value} but {field} is missing")

@dataclass(kw_only=True, slots=True)
class OutboundObject:
    sendThrough: str | None = None
    protocol: Literal["blackhole", "dns", "freedom", "http", "loopback", "shadowsocks", "socks", "trojan", "vless", "vmess", "hysteria", "wireguard"]
    settings: (
        HTTPOutboundConfigurationObject
    |   ShadowSocksOutboundConfigurationObject
    |   SocksOutboundConfigurationObject
    |   VLESSOutboundConfigurationObject
    |   VMessOutboundConfigurationObject
    |   TrojanOutboundConfigurationObject
    |   WireGuardOutboundConfigurationObject
    |   HysteriaOutboundConfigurationObject
    |   BlackholeOutboundConfigurationObject
    |   DNSOutboundConfigurationObject
    |   FreedomOutboundConfigurationObject
    |   LoopbackOutboundConfigurationObject
    |   None
    ) = None
    tag: str = ""
    streamSettings: OutboundStreamSettingsObject | None = None
    proxySettings: OutboundProxySettingsObject | None = None
    mux: MuxObject | None = None
    targetStrategy: Literal["AsIs", "UseIP", "UseIPv6v4", "UseIPv6", "UseIPv4v6", "UseIPv4", "ForceIP", "ForceIPv6v4", "ForceIPv6", "ForceIPv4v6", "ForceIPv4"] = "AsIs"
    # def __post_init__(self) -> None:
    #     for value, field in OUTBOUND_MAP.items():
    #         if self.protocol == value and not isinstance(self.settings, field):
    #             raise ValueError(f"protocol is {value} but {field} is missing")


@dataclass(kw_only=True, slots=True)
class XrayConfig:
    log: LogObject | None = None
    api: ApiObject | None = None
    dns: DnsObject | None = None
    routing: RoutingObject | None = None
    policy: PolicyObject | None = None
    inbounds: list[InboundObject] | None = None
    outbounds: list[OutboundObject] | None = None # TODO-
    stats: StatsObject | None = None
    fakedns: list[FakeDnsObject] | FakeDnsObject | None = None 
    metrics: MetricsObject | None = None
    observatory: ObservatoryObject | None = None
    burstObservatory: BurstObservatoryObject | None = None
    geodata: GeodataObject | None = None
    version: VersionObject | None = None

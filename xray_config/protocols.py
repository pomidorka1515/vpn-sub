from dataclasses import dataclass, field
from typing import Any, Literal

from .validators import *

__all__ = [
    "FallbackObject",
    "BlackholeResponseObject",
    "BlackholeOutboundConfigurationObject",
    "DNSRuleObject",
    "DNSOutboundConfigurationObject",
    "FreedomFinalRuleObject",
    "FreedomOutboundConfigurationObject",
    "HTTPOutboundConfigurationObject",
    "LoopbackOutboundConfigurationObject",
    "ShadowSocksOutboundConfigurationObject",
    "SocksOutboundConfigurationObject",
    "TrojanOutboundConfigurationObject",
    "VLESSOutboundReverseObject",
    "VLESSOutboundConfigurationObject",
    "VMessOutboundUserObject",
    "VMessOutboundConfigurationObject",
    "WireGuardOutboundPeerObject",
    "WireGuardOutboundConfigurationObject",
    "HysteriaOutboundConfigurationObject",
    "TunnelInboundConfigurationObject",
    "UserObject",
    "HTTPInboundConfigurationObject",
    "ShadowSocksUserObject",
    "ShadowSocksInboundConfigurationObject",
    "SocksInboundConfigurationObject",
    "TrojanUserObject",
    "TrojanInboundConfigurationObject",
    "VLESSReverseObject",
    "VLESSUserObject",
    "VLESSInboundConfigurationObject",
    "VMessUserObject",
    "VMessDefaultObject",
    "VMessInboundConfigurationObject",
    "WireGuardInboundPeerObject",
    "WireGuardInboundConfigurationObject",
    "HysteriaUserObject",
    "HysteriaInboundConfigurationObject",
    "TUNInboundConfigurationObject",
]

@dataclass(kw_only=True, slots=True)
class FallbackObject:
    name: str = ""
    alpn: str = ""
    path: str = ""
    port: str | int
    xver: int = 0

@dataclass(kw_only=True, slots=True)
class BlackholeResponseObject:
    type: Literal["http", "none"] = "none"

@dataclass(kw_only=True, slots=True)
class BlackholeOutboundConfigurationObject:
    response: BlackholeResponseObject | None = None

@dataclass(kw_only=True, slots=True)
class DNSRuleObject:
    action: Literal["direct", "hijack", "drop", "reject"]
    qtype: str | int | None = None
    domain: list[str] | None = None
     
@dataclass(kw_only=True, slots=True)
class DNSOutboundConfigurationObject:
    rewriteNetwork: Literal["tcp", "udp"] | None = None
    rewriteAddress: str | None = None # ip
    rewritePort: Port | None = None
    userLevel: int = 0
    rules: list[DNSRuleObject] | None = None

@dataclass(kw_only=True, slots=True)
class FreedomFinalRuleObject:
    action: Literal["block", "allow"] | None = None
    network: str | list[str] | None = None
    port: Port | PortRange | None = None
    ip: list[str] | None = None
    blockDelay: str | None = None

@dataclass(kw_only=True, slots=True)
class FreedomOutboundConfigurationObject:
    domainStrategy: Literal["AsIs", "UseIP",  "UseIPv6v4", "UseIPv6", "UseIPv4v6", "UseIPv4", "ForceIP", "ForceIPv6v4", "ForceIPv6", "ForceIPv4v6", "ForceIPv4"] = "AsIs"
    redirect: str | None = None
    userLevel: int = 0
    fragment: dict[str, str] | None = None # TODO
    noises: list[dict[str, str]] | None = None
    proxyProtocol: int = 0
    finalRules: list[FreedomFinalRuleObject] | None = None
    
@dataclass(kw_only=True, slots=True)
class HTTPOutboundConfigurationObject:
    address: str
    port: Port
    user: str | None = None
    pass_: str | None = None
    level: int = 0
    email: str | None = None
    headers: dict[str, str] | None = None

@dataclass(kw_only=True, slots=True)
class LoopbackOutboundConfigurationObject:
    inboundTag: str

@dataclass(kw_only=True, slots=True)
class ShadowSocksOutboundConfigurationObject:
    email: str | None = None
    address: str
    port: Port
    method: Literal[
        "2022-blake3-aes-128-gcm",
        "2022-blake3-aes-256-gcm",
        "2022-blake3-chacha20-poly1305",
        "aes-256-gcm",
        "aes-128-gcm",
        "chacha20-poly1305 или chacha20-ietf-poly1305",
        "xchacha20-poly1305 или xchacha20-ietf-poly1305",
        "none",
        "plain"
    ]
    uot: bool | None = None
    UoTVersion: Literal[1, 2] | None = None
    level: int = 0
    

@dataclass(kw_only=True, slots=True)
class SocksOutboundConfigurationObject:
    address: str
    port: Port
    user: str
    pass_: str
    level: int = 0
    email: str | None = None

@dataclass(kw_only=True, slots=True)
class TrojanOutboundConfigurationObject:
    address: str
    port: Port
    password: str
    email: str | None = None
    level: int = 0
    
@dataclass(kw_only=True, slots=True)
class VLESSOutboundReverseObject:
    tag: str
    sniffing: dict[str, Any] | None = None

@dataclass(kw_only=True, slots=True)
class VLESSVnextUserObject:
    id: str
    email: str | None = None
    security: str | None = None
    encryption: VLESSEncryption

@dataclass(kw_only=True, slots=True)
class VLESSVnextConfigurationObject:
    address: str
    port: Port
    users: list[VLESSVnextUserObject]

@dataclass(kw_only=True, slots=True)
class VLESSOutboundConfigurationObject:
    address: str | None = None
    port: Port | None = None
    id: str | None = None
    encryption: VLESSEncryption | None = None
    flow: str | None = None 
    level: int | None = None
    vnext: list[VLESSVnextConfigurationObject] | None = None
    reverse: VLESSOutboundReverseObject | None = None

@dataclass(kw_only=True, slots=True)
class VMessOutboundUserObject:
    id: str
    level: int = 0
    security: Literal["aes-128-gcm", "chacha20-poly1305", "auto", "none", "zero"]

@dataclass(kw_only=True, slots=True)
class VMessOutboundConfigurationObject:
    address: str
    port: Port
    users: list[VMessOutboundUserObject]

@dataclass(kw_only=True, slots=True)
class WireGuardOutboundPeerObject:
    endpoint: str
    publicKey: WireguardKey
    preSharedKey: WireguardKey | None = None
    keepAlive: int = 0
    allowedIPs: list[str] | None = None

@dataclass(kw_only=True, slots=True)
class WireGuardOutboundConfigurationObject:
    secretKey: WireguardKey
    address: list[str] | None = None
    noKernelTun: bool | None = None
    mtu: int | None = None
    reserved: list[int] | None = None
    workers: int | None = None
    peers: list[WireGuardOutboundPeerObject]
    domainStrategy: Literal["ForceIPv6v4", "ForceIPv6", "ForceIPv4v6", "ForceIPv4", "ForceIP"] = "ForceIP"
    
@dataclass(kw_only=True, slots=True)
class HysteriaOutboundConfigurationObject:
    version: Literal[2] = 2
    address: str
    port: Port
    
@dataclass(kw_only=True, slots=True)
class TunnelInboundConfigurationObject:
    allowedNetwork: Literal["tcp", "udp", "tcp,udp"] = "tcp"
    rewriteAddress: str = "localhost"
    address: str | None = None
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
    allowTransparent: bool = False
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
class WireGuardInboundPeerObject:
    publicKey: WireguardKey
    allowedIps: list[str] = field(default_factory=lambda: ["0.0.0.0/0", "::/0"])

@dataclass(kw_only=True, slots=True)
class WireGuardInboundConfigurationObject:
    secretKey: WireguardKey
    mtu: int = 1420
    peers: WireGuardInboundPeerObject | None = None

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


from typing import Literal
from dataclasses import dataclass, field

from .validators import *

__all__ = [
    "NETWORK_MAP",
    "SECURITY_MAP",
    "TLS_VER",
    "TLSObject",
    "LimitFallbackObject",
    "InboundRealityObject",
    "OutboundRealityObject",
    "FinalMaskTcpHeaderFakePacketRuleObject",
    "FinalMaskTcpHeaderCustomObject",
    "FinalMaskTcpFragmentObject",
    "FinalMaskSudokuObject",
    "FinalMaskTcpSettingsObject",
    "FinalMaskUdpHeaderCustomValueObject",
    "FinalMaskUdpHeaderCustomObject",
    "FinalMaskUdpHeaderDnsObject",
    "FinalMaskUdpMkcpAesObject",
    "FinalMaskUdpNoiseValueObject",
    "FinalMaskUdpNoiseObject",
    "FinalMaskUdpSalamanderObject",
    "FinalMaskUdpXDnsObject",
    "FinalMaskUdpXIcmpObject",
    "FinalMaskUdpSettingsObject",
    "QuicParamsUdpHopObject",
    "QuicParamsObject",
    "FinalMaskObject",
    "CustomSockoptObject",
    "HappyEyeballsObject",
    "SockoptObject",
    "HTTPRequestObject",
    "HTTPResponseObject",
    "HTTPHeaderObject",
    "NoneHeaderObject",
    "RawObject",
    "XMUXObject",
    "DownloadSettingsObject",
    "XHTTPExtraObject",
    "XHTTPObject",
    "KcpObject",
    "GRPCBaseObject",
    "InboundGRPCObject",
    "OutboundGRPCObject",
    "WebSocketObject",
    "HTTPUpgradeObject",
    "HysteriaMasqObject",
    "HysteriaObject",
    "BaseStreamSettingsObject",
    "InboundStreamSettingsObject",
    "OutboundStreamSettingsObject",
]

NETWORK_MAP: dict[str, str] = {
    "raw": "rawSettings",
    "xhttp": "xhttpSettings",
    "mkcp": "kcpSettings",
    "grpc": "grpcSettings",
    "websocket": "wsSettings",
    "httpupgrade": "httpupgradeSettings",
    "hysteria": "hysteriaSettings",
}
SECURITY_MAP: dict[str, str] = {
    "reality": "realitySettings",
    "tls": "tlsSettings"
}
TLS_VER = Literal['1.0', '1.1', '1.2', '1.3']


@dataclass(kw_only=True, slots=True)
class TLSObject:
    serverName: str = ""
    verifyPeerCertByName: str | None = None
    rejectUnknownSni: bool = False
    fingerprint: str | None = None
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
    domainStrategy: Literal["AsIs", "UseIP",  "UseIPv6v4", "UseIPv6", "UseIPv4v6", "UseIPv4", "ForceIP", "ForceIPv6v4", "ForceIPv6", "ForceIPv4v6", "ForceIPv4"] = "AsIs"
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

@dataclass(kw_only=True, slots=True)
class InboundGRPCObject(GRPCBaseObject): pass

@dataclass(kw_only=True, slots=True)
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
class BaseStreamSettingsObject:
    network: Literal["raw", "tcp", "xhttp", "mkcp", "grpc", "websocket", "httpupgrade", "hysteria"]
    security: Literal['none', 'reality', 'tls']
    rawSettings: RawObject | None = None
    tcpSettings: None = None  # tcp has no specific settings, but field must exist for validation
    xhttpSettings: XHTTPObject | None = None
    kcpSettings: KcpObject | None = None
    # grpcSettings: InboundGRPCObject | None = None
    wsSettings: WebSocketObject | None = None
    httpupgradeSettings: HTTPUpgradeObject | None = None
    hysteriaSettings: HysteriaObject | None = None

    # realitySettings: InboundRealityObject | None = None
    tlsSettings: TLSObject | None = None

    finalmask: FinalMaskObject | None = None
    sockopt: SockoptObject | None = None

    def __post_init__(self) -> None:
        settings_field = NETWORK_MAP.get(self.network)
        if settings_field and self.network != "tcp":
            # tcp has no settings field, just has tcpSettings = None
            if getattr(self, settings_field, None) is None:
                raise ValueError(
                    f"network is '{self.network}' but '{settings_field}' is missing"
                )
        
        security_field = SECURITY_MAP.get(self.security)
        if security_field and getattr(self, security_field, None) is None:
            raise ValueError(
                f"security is '{self.security}' but '{security_field}' is missing"
            )
        
@dataclass(kw_only=True, slots=True)
class InboundStreamSettingsObject(BaseStreamSettingsObject):
    grpcSettings: InboundGRPCObject | None = None
    realitySettings: InboundRealityObject | None = None

@dataclass(kw_only=True, slots=True)
class OutboundStreamSettingsObject(BaseStreamSettingsObject):
    grpcSettings: OutboundGRPCObject | None = None
    realitySettings: OutboundRealityObject | None = None


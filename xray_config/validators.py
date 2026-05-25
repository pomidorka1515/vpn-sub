import re

from functools import partial
from typing import Self, Callable, Any

__all__ = [
    "BaseValidator",
    "Port",
    "PortString",
    "RewritePort",
    "PortRange",
    "RegexOrEmail",
    "Email",
    "CronExpression",
    "Ciphers",
    "X25519Key",
    "MLDSA65Seed",
    "MLDSA65Verify",
    "Version",
    "ShortId",
    "BrutalBandwidth",
    "QuicMaxIdleTimeout",
    "QuicKeepAlivePeriod",
    "QuicMaxIncomingStreams",
    "SimpleRange",
    "VLESSDecryption",
    "VLESSEncryption",
    "WireguardKey"
]

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
            new_bases = tuple(
                bt for bt in base_types if bt not in bases
            )
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
                
                prob, min_val = map(int, parts)
                
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
                
                prob, min_val = map(int, parts)
                
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
    MLDSA65Seed,
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

TYPE_HOOKS: dict[type[Any], Callable[..., Any]] = {cls: partial(cls) for cls in __type_classes}

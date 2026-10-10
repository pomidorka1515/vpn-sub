import hmac
import io
import re
import secrets
import uuid
from typing import TYPE_CHECKING, Literal, cast

import qrcode

if TYPE_CHECKING:
    from flask import Response

    from custom_types import JsonifyValue

__all__ = [
    "compare",
    "fmt_bytes",
    "fmt_bytes_tuple",
    "fmt_time",
    "format",
    "format_usage",
    "generate_token",
    "is_cancel_command",
    "isbrowser",
    "isusername",
    "isuuid",
    "make_qr",
    "parse_bool",
    "sanitize",
    "truncate_utf8",
    "tuple_hook",
]

_BROWSER_UA = re.compile(r'(MSIE|Trident|(?!Gecko.+)Firefox|(?!AppleWebKit.+Chrome.+)Safari(?!.+Edge)|(?!AppleWebKit.+)Chrome(?!.+Edge)|(?!AppleWebKit.+Chrome.+Safari.+)Edge|AppleWebKit(?!.+Chrome|.+Safari)|Gecko(?!.+Firefox))(?: |\/)([\d\.apre]+)')

def sanitize(s: str, kind: Literal["external", "display"], /) -> str:
    """
    Args:
        kind:
            "external" -> whitelist: A-Z, a-z, 0-9, and _, - chars allowed, max length 32
            "display"  -> blacklist: filters special chars, max length 16
    """
    match kind:
        case "external":
            return re.sub(r'[^A-Za-z0-9_\-]', '', s[:32])
        case "display":
            return s[:16].translate(str.maketrans('', '', r''':;"'?/<>{}[]*&^%$#@\|`'''))

def generate_token(kind: Literal["sub", "auth"], /) -> str:
    """
    One central function to generate a token.
    Args:
        kind:
            "sub"  -> url-safe, for subscription links
            "auth" -> for web-ui auth, longer
    """
    match kind:
        case "sub":
            return secrets.token_urlsafe(40)
        case "auth":
            return secrets.token_hex(50)

def isbrowser(ua: str) -> bool:
    return bool(_BROWSER_UA.search(ua))

def compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)

def isuuid(s: str) -> bool:
    """Validate a UUID."""
    try:
        val = uuid.UUID(s, version=4)
        return str(val) == s.lower()
    except ValueError:
        return False

_USERNAME_RE = re.compile(r'[A-Za-z0-9_-]+')

def isusername(s: str) -> bool:
    """Validate a username for panel use.

    The username becomes the 3x-ui client email (the clients-first API join
    key), so it must survive the panel's forbidden-character check for client
    emails (no whitespace, slashes, etc.) and URL path segments.
    """
    return bool(_USERNAME_RE.fullmatch(s))

def ok(
    msg: str | None = None,
    code: int = 200,
    obj: JsonifyValue = None
) -> tuple[Response, int]:
    """Internal helper function to return a successful Response."""
    from flask import jsonify

    return jsonify({"success": True, "msg": msg, "obj": obj}), code

def err(
    msg: str | None = None,
    code: int = 400,
    obj: JsonifyValue = None
) -> tuple[Response, int]:
    """Internal helper function to return an error Response."""
    from flask import jsonify

    return jsonify({"success": False, "msg": msg, "obj": obj}), code



class _PartialFormatter(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"

# intentionally shadows `builtins.format`
def format(template: str, **values: object) -> str: # noqa: A001
    return template.format_map(
        _PartialFormatter(values)
    )


def tuple_hook(value: object) -> object:
    if isinstance(value, list):
        return tuple(cast(list[object], value))
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

def fmt_bytes_tuple(value: float) -> tuple[str, str]:
    """
    Format bytes into a tuple.
    Returns:
        tuple[amount, label]
        Example: ("193", "MB")
    """
    for unit, div in (
        ("TB", 10**12), ("GB", 10**9),
        ("MB", 10**6), ("KB", 10**3)
    ):
        if value >= div:
            return str(round(value / div, 2)), unit
    return str(round(value / 10**6, 2)), "MB"


def fmt_bytes(value: float) -> str:
    """Format bytes as short human-readable string."""
    for unit, div in (
        ('TB', 10**12), ('GB', 10**9),
        ('MB', 10**6), ('KB', 10**3)
    ):
        if value >= div:
            return f'{value / div:.2f} {unit}'
    return f'{int(value)} B'


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


def format_usage(used: float, limit: float,
                 unlimited: str = "Безлимит") -> tuple[str, str, str]:
    """Format used bytes, a GB limit, and the corresponding percentage."""
    if limit == 0:
        return unlimited, unlimited, "N/A"
    used_text = fmt_bytes(used)
    limit_text = f"{limit} GB"
    percent = f"{int((used / (limit * 10**9)) * 100)}%" if used > 0 else "0%"
    return used_text, limit_text, percent


def is_cancel_command(text: str | None) -> bool:
    """Return whether input is a slash command used to cancel a flow."""
    return bool(text and text.startswith("/"))


def truncate_utf8(text: str, max_bytes: int, suffix: str = "...") -> str:
    """Truncate text to a byte limit without splitting UTF-8 characters."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= max_bytes:
        return suffix.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    raw = text.encode("utf-8")[:max_bytes - suffix_bytes]
    return raw.decode("utf-8", errors="ignore") + suffix


def make_qr(text: str) -> io.BytesIO:
    img = qrcode.make(text)
    bio = io.BytesIO()
    img.save(bio, 'PNG')
    bio.seek(0)
    bio.name = "qr.png"
    return bio

"""Extract operator scripts and the packed schema next to a binary.

``vpn-sub --load-scripts`` and ``vpn-sub-discord --load-scripts`` both call
:func:`load_scripts`. A compiled binary already carries its own
``config.schema.json`` in the payload. The scripts are not packed: they are
downloaded from the GitHub tag this binary was built from, into ``scripts/``
beside the executable. A checkout already has both and refuses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

    from requests import Response

    from loggers import Logger

from paths import bundled_root, compiled, program_dir
from version import VERSION

__all__ = ["load_scripts"]

REPO = "pomidorka1515/vpn-sub"
SCHEMA = "config.schema.json"
TIMEOUT = 30.0
_API = f"https://api.github.com/repos/{REPO}"


def load_scripts(log: Logger) -> int:
    """Write the packed schema beside the binary and download ``scripts/``.

    Requires a compiled binary. Does not ask: the files are the ones this
    binary was built with, not a newer release. An existing file is replaced.
    Returns a process exit code.
    """
    if not compiled():
        log.error("load-scripts only runs from the vpn-sub binary")
        return 1
    if VERSION == "0.0.0":
        log.error(f"binary has no release version ({VERSION})")
        return 1

    install = program_dir()
    schema = bundled_root() / SCHEMA
    if not schema.is_file():
        log.error(f"schema file not found: {schema}")
        return 1
    try:
        _write(install / SCHEMA, schema.read_bytes())
    except OSError as exc:
        log.error(f"could not write schema: {exc}")
        return 1
    log.info(f"wrote {install / SCHEMA}")

    scripts = install / "scripts"
    try:
        scripts.mkdir(parents=True, exist_ok=True)
        listed = _listing()
        if not listed:
            raise ValueError(f"v{VERSION} has no scripts")
        for name, digest in listed:
            target = scripts / name
            log.info(f"downloading {name}")
            _write(target, _script(name, digest))
            log.info(f"wrote {target}")
    except (OSError, RequestError, ValueError) as exc:
        log.error(f"download failed: {exc}")
        return 1
    return 0


def _listing() -> tuple[tuple[str, str], ...]:
    """Files in ``scripts/`` at the tag this binary was built from.

    A directory listing is an array. A missing path is an object, which is not
    a listing. Only blobs are files; a nested directory is not downloaded. The
    sha is the git blob id the file response must match, so a later response
    cannot substitute a different object.
    """
    response = _get(f"{_API}/contents/scripts?ref=v{VERSION}", TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("scripts response is not a listing")
    found: list[tuple[str, str]] = []
    for item in cast(list[object], payload):
        if not isinstance(item, dict):
            continue
        entry = cast(dict[object, object], item)
        name = entry.get("name")
        sha = entry.get("sha")
        kind = entry.get("type")
        if kind != "file" or not isinstance(name, str) or not isinstance(sha, str):
            continue
        if _safe_name(name):
            found.append((name, sha))
    return tuple(sorted(found))


def _safe_name(name: str) -> bool:
    return name not in (".", "..") and "/" not in name and "\\" not in name


def _script(name: str, digest: str) -> bytes:
    """Download one script from the tag this binary was built from.

    The contents API returns the file and the git blob sha in one response.
    The body is accepted only when ``sha1("blob <size>\\0" + body)`` matches
    that sha and the sha from the directory listing, so a truncated or
    substituted payload is not written. GitHub wraps the base64 at 60 columns;
    the alphabet check ignores that whitespace.
    """
    if not _safe_name(name):
        raise ValueError(f"refusing script name {name!r}")
    response = _get(f"{_API}/contents/scripts/{name}?ref=v{VERSION}", TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{name} response is not an object")
    body = cast(dict[object, object], payload)
    encoded = body.get("content")
    sha = body.get("sha")
    encoding = body.get("encoding")
    if encoding != "base64" or not isinstance(encoded, str) or not isinstance(sha, str):
        raise ValueError(f"{name} response has no base64 content")
    try:
        content = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"{name} content is not base64") from exc
    check = hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content, usedforsecurity=False)
    if not hmac.compare_digest(check.hexdigest(), sha) or not hmac.compare_digest(sha, digest):
        raise ValueError(f"{name} blob digest does not match the release tag")
    return content


def _write(target: Path, body: bytes) -> None:
    temporary = target.with_name(f".{target.name}.part")
    try:
        with temporary.open("wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class RequestError(OSError):
    """HTTP failure before ``requests`` has been imported, or after it has."""


class _Body:
    def __init__(self, response: Response) -> None:
        self._response = response

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    def json(self) -> object:
        payload: object = self._response.json()
        return payload


def _get(url: str, timeout: float) -> _Body:
    import requests

    try:
        response = requests.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RequestError(str(exc)) from exc
    return _Body(response)

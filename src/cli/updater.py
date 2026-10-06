"""Replace the Nuitka onefile binaries from a GitHub release.

Runs only inside a compiled binary. A checkout has no onefile stub to swap,
and ``python -m`` is not an install. ``vpn-sub --update`` and
``vpn-sub-discord --update`` both call :func:`update`. Boot calls
:func:`notice`, which only asks whether a newer release exists.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, cast
from types import TracebackType

if TYPE_CHECKING:
    from requests import Response

from loggers import Logger
from paths import compiled, program_dir
from version import VERSION

__all__ = ["notice", "update"]

REPO = "pomidorka1515/vpn-sub"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
BINARIES = ("vpn-sub", "vpn-sub-discord")
UNITS = ("vpn-sub.service", "vpn-sub-discord.service")
NOTICE_TIMEOUT = 3.0
UPDATE_TIMEOUT = 30.0
_DOWNLOAD_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    version: tuple[int, ...]
    notes: str
    page: str
    assets: dict[str, Asset]


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    url: str
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class Running:
    name: str
    pid: int
    unit: str | None


def notice(log: Logger) -> None:
    """Log one line when a newer GitHub release exists.

    A timeout or a bad response is not an error. Boot must not wait on GitHub
    and must not ask a question: a systemd unit has no TTY to answer it.
    """
    if not compiled():
        return
    try:
        release = _latest(NOTICE_TIMEOUT)
    except (OSError, RequestError, ValueError):
        return
    current = _parse(VERSION)
    if current is None or not _newer(release.version, current):
        return
    log.info(f"update available: {VERSION} -> {release.tag} (vpn-sub --update)")


def update(log: Logger) -> int:
    """Download the latest release and replace the installed binaries.

    Requires a compiled binary and a TTY. Asks before downloading. If either
    service is running, asks the operator to stop both and continues only
    after they are gone. Does not stop or start anything itself. Returns a
    process exit code.
    """
    if not compiled():
        log.error("updater only runs from the vpn-sub binary")
        return 1
    if not _interactive():
        log.error("updater needs a terminal")
        return 2
    current = _parse(VERSION)
    if current is None:
        log.error(f"binary has no release version ({VERSION})")
        return 1
    try:
        release = _latest(UPDATE_TIMEOUT)
    except (OSError, RequestError, ValueError) as exc:
        log.error(f"could not read the latest release: {exc}")
        return 1
    if not _newer(release.version, current):
        log.info(f"already at {VERSION}; latest release is {release.tag}")
        return 0
    log.info(f"update available: {VERSION} -> {release.tag}")
    log.info(release.page)
    if release.notes:
        log.info(release.notes)
    if not _confirm(log, f"update to {release.tag}?"):
        log.info("update cancelled")
        return 0

    install = program_dir()
    paths = {name: install / name for name in BINARIES}
    present = {name: path for name, path in paths.items() if path.is_file()}
    linked = [name for name, path in present.items() if path.is_symlink()]
    if linked:
        log.error(f"refusing to replace a symlink: {', '.join(linked)}")
        return 1
    if not present:
        log.error(f"no vpn-sub binary in {install}")
        return 1
    missing = [asset for asset in present if asset not in release.assets]
    if missing:
        log.error(f"release {release.tag} has no asset for {', '.join(missing)}")
        return 1

    running = _running(log, paths)
    if running is None:
        return 1
    if running:
        for item in running:
            unit = f" ({item.unit})" if item.unit else ""
            log.info(f"running: {item.name} pid {item.pid}{unit}")
        log.info("make sure both services are stopped before continuing")
        if not _confirm(log, "both services are stopped?"):
            log.info("update cancelled")
            return 0
        running = _running(log, paths)
        if running is None:
            return 1
        if running:
            listed = ", ".join(f"{item.name} pid {item.pid}" for item in running)
            log.error(f"still running: {listed}")
            return 1

    files = _download(log, release, present)
    if files is None:
        return 1
    replaced = _swap(log, present, files)
    if replaced is None:
        return 1
    log.info(f"updated to {release.tag}")
    return 0


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm(log: Logger, question: str) -> bool:
    log.info(f"{question} [y/N]")
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _parse(value: str) -> tuple[int, ...] | None:
    text = value[1:] if value.startswith("v") else value
    text = text.split("-", 1)[0].split("+", 1)[0]
    parts = text.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _newer(release: tuple[int, ...], current: tuple[int, ...]) -> bool:
    width = max(len(release), len(current))
    return release + (0,) * (width - len(release)) > current + (0,) * (width - len(current))


def _latest(timeout: float) -> Release:
    response = _get(RELEASES_URL, timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("release response is not an object")
    body = _object(cast(dict[object, object], payload))
    tag = body.get("tag_name")
    version = _parse(tag) if isinstance(tag, str) else None
    if not isinstance(tag, str) or version is None:
        raise ValueError(f"release tag is not a version: {tag!r}")
    if body.get("draft") is True or body.get("prerelease") is True:
        raise ValueError(f"{tag} is not a published release")
    raw_assets = body.get("assets")
    if not isinstance(raw_assets, list):
        raise ValueError("release has no assets")
    listed = cast(list[object], raw_assets)
    assets: dict[str, Asset] = {}
    for raw in listed:
        if not isinstance(raw, dict):
            continue
        asset = _asset(cast(dict[object, object], raw))
        if asset is not None and asset.name in BINARIES:
            assets[asset.name] = asset
    notes = body.get("body")
    page = body.get("html_url")
    return Release(
        tag=tag,
        version=version,
        notes=notes if isinstance(notes, str) else "",
        page=page if isinstance(page, str) else f"https://github.com/{REPO}/releases/latest",
        assets=assets,
    )


def _object(value: dict[object, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}


def _asset(item: object) -> Asset | None:
    if not isinstance(item, dict):
        return None
    fields = _object(cast(dict[object, object], item))
    name = fields.get("name")
    url = fields.get("browser_download_url")
    size = fields.get("size")
    digest = fields.get("digest")
    if not isinstance(name, str) or not isinstance(url, str) or not isinstance(size, int):
        return None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError(f"{name} has no sha256 digest")
    return Asset(name=name, url=url, size=size, digest=digest.removeprefix("sha256:"))


def _running(log: Logger, paths: dict[str, Path]) -> list[Running] | None:
    wanted = {path.resolve(): name for name, path in paths.items()}
    found: dict[int, Running] = {}
    units = _units(log, wanted)
    if units is None:
        return None
    for unit, pid, name in units:
        found[pid] = Running(name=name, pid=pid, unit=unit)
    for pid, name in _processes(wanted, _self_pids(wanted)):
        found.setdefault(pid, Running(name=name, pid=pid, unit=None))
    return [found[pid] for pid in sorted(found)]


def _units(
    log: Logger,
    wanted: dict[Path, str],
) -> list[tuple[str, int, str]] | None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return []
    listed = subprocess.run(
        [systemctl, "list-units", "--type=service", "--all", "--no-legend", "--plain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        log.error(listed.stderr.strip() or "systemctl list-units failed")
        return None
    names = {line.split(maxsplit=1)[0] for line in listed.stdout.splitlines() if line.strip()}
    names.update(UNITS)
    found: list[tuple[str, int, str]] = []
    for unit in sorted(names):
        if not unit.endswith(".service"):
            continue
        shown = subprocess.run(
            [systemctl, "show", unit, "--property=Id,MainPID,FragmentPath,ExecStart,ActiveState"],
            check=False,
            capture_output=True,
            text=True,
        )
        if shown.returncode != 0:
            continue
        props = _properties(shown.stdout)
        if props.get("ActiveState") not in ("active", "activating", "deactivating"):
            continue
        pid = _pid(props.get("MainPID", ""))
        if pid is None or pid == os.getpid():
            continue
        binary = _unit_binary(props, wanted)
        if binary is None:
            continue
        found.append((props.get("Id", unit), pid, binary))
    return found


def _properties(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


def _pid(value: str) -> int | None:
    if not value.isdigit():
        return None
    pid = int(value)
    return pid if pid > 0 else None


def _unit_binary(props: dict[str, str], wanted: dict[Path, str]) -> str | None:
    """Return the installed binary a unit starts, if it is one of ours.

    ``ExecStart`` is ``{ path=<exe> ; argv[]=... }``. The path is not quoted,
    so a raw substring match treats ``/opt/vpn`` as ``/opt/vpn-sub``. The
    executable is the value of ``path=`` up to the field separator. A unit
    file path is not an executable and is not used.
    """
    executable = _exec_path(props.get("ExecStart", ""))
    if executable is None:
        return None
    try:
        resolved = Path(executable).resolve()
    except (OSError, ValueError):
        return None
    return wanted.get(resolved)


def _exec_path(text: str) -> str | None:
    for part in text.split(";"):
        field = part.strip().removeprefix("{").strip()
        if field.startswith("path="):
            value = field.removeprefix("path=").strip()
            return value or None
    return None


def _processes(wanted: dict[Path, str], own: set[int]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        pid = _pid(entry.name)
        if pid is None or pid in own:
            continue
        name = _process_name(entry, wanted)
        if name is not None:
            found.append((pid, name))
    return found


def _process_name(entry: Path, wanted: dict[Path, str]) -> str | None:
    """Match a live process to an installed binary.

    ``/proc/<pid>/exe`` is the file the process executed. A Nuitka onefile
    parent executes the stub, so that link is the install path. Its child
    executes the unpacked payload; the parent's path is then only in
    ``argv[0]``, which the bootstrap rewrites to an absolute path. A deleted
    binary keeps a `` (deleted)`` suffix on the link text. Resolution follows
    symlinks, so a link is compared as the path systemd and the operator see.
    """
    exe = entry / "exe"
    try:
        text = os.readlink(exe)
    except OSError:
        text = ""
    if text.endswith(" (deleted)"):
        text = text.removesuffix(" (deleted)")
    for candidate in (text, _argv0(entry)):
        if not candidate or candidate == "-":
            continue
        try:
            resolved = Path(candidate).resolve()
        except (OSError, ValueError):
            continue
        name = wanted.get(resolved)
        if name is not None:
            return name
    return None


def _argv0(entry: Path) -> str:
    try:
        raw = (entry / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _self_pids(wanted: dict[Path, str]) -> set[int]:
    """PIDs of this update command, including the onefile bootstrap.

    ``os.getpid()`` is the unpacked child. The parent that executed the
    installed stub stays alive until this process exits, and ``/proc/<pid>/exe``
    is the binary being replaced. Counting it makes the second check always
    fail after the services are stopped. ``NUITKA_ONEFILE_PARENT`` is that
    parent; ``getppid`` covers a bootstrap that did not export it. A parent
    counts only when it is one of the installed binaries, so a service that
    happens to be this process's parent is still reported.
    """
    pids = {os.getpid()}
    for pid in (os.getppid(), _pid(os.environ.get("NUITKA_ONEFILE_PARENT", ""))):
        if pid is None or pid <= 1 or pid in pids:
            continue
        if _process_name(Path("/proc") / str(pid), wanted) is not None:
            pids.add(pid)
    return pids


def _download(log: Logger, release: Release, present: dict[str, Path]) -> dict[str, Path] | None:
    files: dict[str, Path] = {}
    current: Path | None = None
    try:
        for name, path in present.items():
            target = path.with_name(f".{name}.download")
            current = target.with_name(f"{target.name}.part")
            log.info(f"downloading {name}")
            _fetch(release.assets[name], target)
            files[name] = target
            current = None
    except (OSError, RequestError, ValueError) as exc:
        log.error(f"download failed: {exc}")
        if current is not None:
            files[current.name] = current
        _cleanup(files)
        return None
    return files


def _fetch(asset: Asset, target: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    temporary = target.with_name(f"{target.name}.part")
    try:
        with _get(asset.url, UPDATE_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(_DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if size != asset.size:
            raise ValueError(f"size {size} != {asset.size}")
        if not hmac.compare_digest(digest.hexdigest(), asset.digest):
            raise ValueError("sha256 digest does not match the release")
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _swap(log: Logger, present: dict[str, Path], files: dict[str, Path]) -> list[str] | None:
    replaced: list[str] = []
    try:
        for name, path in present.items():
            backup = path.with_name(f"{name}.bak")
            staged = path.with_name(f"{name}.new")
            shutil.copy2(path, backup)
            os.chmod(files[name], 0o755)
            files[name].replace(staged)
            staged.replace(path)
            replaced.append(name)
            log.info(f"replaced {path}")
    except OSError as exc:
        log.error(f"replace failed: {exc}")
        _restore(log, present, replaced)
        _cleanup(files)
        return None
    _cleanup(files)
    return replaced


def _restore(log: Logger, present: dict[str, Path], replaced: list[str]) -> None:
    for name in reversed(replaced):
        path = present[name]
        backup = path.with_name(f"{name}.bak")
        try:
            if backup.is_file():
                backup.replace(path)
                log.warning(f"restored {path}")
        except OSError as exc:
            log.error(f"could not restore {path}: {exc}")


def _cleanup(files: dict[str, Path]) -> None:
    for path in files.values():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class RequestError(OSError):
    """HTTP failure before ``requests`` has been imported, or after it has."""


class _Body:
    def __init__(self, response: Response) -> None:
        self._response = response

    def __enter__(self) -> _Body:
        self._response.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._response.__exit__(exc_type, exc, traceback)

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    def json(self) -> object:
        payload: object = self._response.json()
        return payload

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for chunk in self._response.iter_content(chunk_size=chunk_size):
            if not isinstance(chunk, bytes):
                raise ValueError("download chunk is not bytes")
            yield chunk


def _get(url: str, timeout: float, stream: bool = False) -> _Body:
    import requests

    try:
        response = requests.get(
            url,
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
            stream=stream,
        )
    except requests.RequestException as exc:
        raise RequestError(str(exc)) from exc
    return _Body(response)

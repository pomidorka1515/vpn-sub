"""Shared runtime path resolution.

Locks and generation stamps must live on a directory this process can create.
``/run/lock`` is sticky and world-writable on Debian, but mode 0755 root:root
on RHEL, so a non-root service cannot create a file there. There is no FHS
directory that is sticky on every distro, and a predictable name under ``/tmp``
can be squatted. The data directory is already owned by the service user.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["bundled_root", "compiled", "program_dir", "runtime_dir"]


def program_dir() -> Path:
    """Directory that holds ``data/`` when ``DIR_DATA`` is unset.

    A checkout uses the working directory. ``python -m`` puts the module file
    in ``sys.argv[0]``, which is not the install directory.

    A Nuitka onefile binary is the directory of ``sys.argv[0]``. Included data
    is extracted next to the compiled module, so ``__file__`` is inside the
    payload and must not be used for configs the operator keeps beside the
    executable. Nuitka does not set ``sys.frozen``. ``__compiled__`` is how a
    compiled module is detected.

    The bootstrap gives the child an absolute, resolved ``sys.argv[0]``, so a
    PATH invocation still lands on the install directory.
    ``__compiled__.original_argv0`` is the operator's argv as typed and must
    not be used: a bare name resolves against the working directory. When argv
    is missing or stdin, ``containing_dir`` is the binary's directory. Do not
    use ``sys.executable``: onefile points it at the unpacked payload binary.

    ``DIR_DATA`` still overrides this.
    """
    compiled = sys.modules["__main__"].__dict__.get("__compiled__")
    if compiled is not None:
        argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
        if argv0 and argv0 != "-":
            return Path(argv0).resolve().parent
        containing = getattr(compiled, "containing_dir", None)
        if isinstance(containing, str) and containing:
            return Path(containing)
    return Path.cwd()


def compiled() -> bool:
    """True when this process was started from a Nuitka binary.

    Nuitka injects ``__compiled__`` on the entry module. A checkout does not
    have it. Imported modules must not look at their own globals: ``paths`` is
    compiled into both binaries, but tests and ``python -m`` import it as a
    normal module. The entry module is ``__main__``.
    """
    return sys.modules["__main__"].__dict__.get("__compiled__") is not None


def bundled_root() -> Path:
    """Directory that contains files shipped with this program.

    A normal checkout is the repository root, two levels above this module.
    Nuitka onefile extracts included data next to the compiled entry module,
    whose ``__file__`` is inside the payload rather than the checkout. Walk
    up from this module until a shipped marker is found.

    The main binary ships both ``res/`` and ``lang.jsonc``. The Discord binary
    ships only its own ``lang.jsonc``, so either marker is enough. Requiring
    both would miss the Discord payload and fall through to a parent of the
    unpack tree.

    ``DIR_DATA`` and ``PATH_*`` still override mutable runtime files. This
    only locates files that ship with the program.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "lang.jsonc").is_file() or (parent / "res").is_dir():
            return parent
    return here.parents[1]


def runtime_dir(data: Path | None = None) -> Path:
    """Directory for flock files and cross-process generation stamps.

    ``DIR_RUNTIME`` wins. Otherwise the directory is ``<DIR_DATA>/run``, and
    ``DIR_DATA`` itself defaults to ``<repo>/data``. ``data`` is the already
    resolved data directory; pass it so a caller that overrode ``DIR_DATA``
    does not read the environment a second time.

    The directory must be local. ``fcntl.flock`` is not reliable on NFS.
    """
    override = os.getenv("DIR_RUNTIME")
    if override:
        return Path(override)
    if data is None:
        data = Path(os.getenv("DIR_DATA", program_dir() / "data"))
    return data / "run"

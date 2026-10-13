"""Release version baked into the Nuitka binaries.

``__compiled__`` carries Nuitka's own version, not ``--product-version``.
The release workflow rewrites ``VERSION`` from the tag before compiling, the
complete suffix included. Nuitka's numeric ``--product-version`` is separate.
A checkout keeps ``0.0.0``.
"""

from __future__ import annotations

__all__ = ["VERSION"]

VERSION = "0.0.0"

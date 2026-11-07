from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from api import common

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "name",
    [
        "assets/admin/main.js",
        "assets/common.css",
        "templates/auth.html",
        "fonts/DejaVuSans.ttf",
        "vendor/chart.umd.min.js",
        "vendor/LICENSE_CODEMIRROR",
    ],
)
def test_asset_version_covers_generated_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    path = tmp_path / name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"before")
    monkeypatch.setattr(common, "RES_DIR", tmp_path)
    common.asset_version.cache_clear()
    try:
        before = common.asset_version()
        metadata = path.stat()
        path.write_bytes(b"after!")
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        common.asset_version.cache_clear()
        assert common.asset_version() != before
    finally:
        common.asset_version.cache_clear()

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from fonts import FONT_FILES, FONTS_MARKER, RES_DIR, embed_font_faces, font_version

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("name", sorted(FONT_FILES))
def test_embedded_font_versions_follow_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    (tmp_path / "fonts").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "fonts.css").write_bytes((RES_DIR / "assets" / "fonts.css").read_bytes())
    for filename in FONT_FILES:
        (tmp_path / "fonts" / filename).write_bytes(b"original font")
    monkeypatch.setattr("fonts.RES_DIR", tmp_path)
    font_version.cache_clear()
    try:
        version = font_version()
        source = f"before {FONTS_MARKER} after"
        original = embed_font_faces(source, "/custom/")
        assert original == embed_font_faces(source, "/custom")
        assert original.startswith("before ")
        assert original.endswith(" after")
        for filename in FONT_FILES:
            assert f"url('/custom/fonts/{filename}?v={version}')" in original
            assert f"url('/fonts/{filename}?v={version}')" in embed_font_faces(source, "/")
        assert "__FONT_" not in original

        path = tmp_path / "fonts" / name
        stat = path.stat()
        path.write_bytes(b"modified font")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        # A new release/process recomputes the digest, even for equal size/mtime.
        font_version.cache_clear()
        assert font_version() != version
        updated = embed_font_faces(source, "/custom")
        assert updated != original
        assert f"?v={version}" not in updated
        for filename in FONT_FILES:
            assert f"url('/custom/fonts/{filename}?v={font_version()}')" in updated
    finally:
        font_version.cache_clear()

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest
from jinja2 import Environment, FileSystemLoader

from api import common

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("prefix", ["", "/nested/sub", "</script><script>alert('x')</script>"])
@pytest.mark.parametrize("language", ["en", "ru"])
def test_shared_bootstrap_is_json_not_executable_script(prefix: str, language: str) -> None:
    hostile = "</script><script>alert('x')</script>\u2028\u2029&\""
    template = Environment(
        loader=FileSystemLoader(common.RES_DIR / "templates"),
        autoescape=True,
    ).get_template("shared-bootstrap.html")
    rendered = template.render(
        prefix=prefix,
        lang=language,
        L={"message": hostile},
        L_en={"message": "fallback"},
        asset_version="abc123",
    )
    opening = '<script id="frontend-bootstrap" type="application/json">'
    assert rendered.startswith(opening)
    assert rendered.count("</script>") == 1
    assert "<script>alert" not in rendered
    payload = json.loads(rendered.removeprefix(opening).removesuffix("</script>"))
    assert payload == {
        "prefix": prefix,
        "language": language,
        "translations": {"message": hostile},
        "fallback": {"message": "fallback"},
        "page": {
            "chartUrl": f"{prefix}/chart.umd.min.js?v=abc123",
            "debounceMs": 1000,
        },
    }


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

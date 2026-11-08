from __future__ import annotations

import json
import os
import shutil
from typing import TYPE_CHECKING

import pytest
from jinja2 import Environment, FileSystemLoader

from api import AsyncWebApi, common
from api.web import static
from fonts import FONT_FILES, font_version
from tests.support.api import api_app, config_snapshot, make_subscription, make_watch
from tests.support.config import subscription_config

if TYPE_CHECKING:
    from pathlib import Path

    from config import Config, LangConfig
    from db import Database


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


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/nested/sub"])
async def test_page_script_routes_and_error_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    language: Config[LangConfig],
    prefix: str,
) -> None:
    templates = common.RES_DIR / "templates"
    shutil.copytree(templates, tmp_path / "templates")
    assets = tmp_path / "assets"
    (assets / "admin").mkdir(parents=True)
    names = (
        "auth.js",
        "admin-auth.js",
        "dashboard.js",
        "history.js",
        "admin/main.js",
        "error.js",
        "redirect.js",
    )
    for name in names:
        (assets / name).write_text(f"/* {name} */", encoding="utf-8")
    monkeypatch.setattr(static, "RES_DIR", tmp_path)
    monkeypatch.setattr(common, "RES_DIR", tmp_path)
    common.asset_version.cache_clear()
    try:
        async with api_app() as app:
            app.jinja_env.loader = FileSystemLoader(tmp_path / "templates")
            sub = await make_subscription(
                database, app=app, lang_cfg=language, uri=prefix.strip("/")
            )
            AsyncWebApi(
                app=app,
                cfg=config_snapshot(subscription_config(uri=prefix.strip("/"))),
                sub=sub,
                bw=make_watch(database, sub),
                limiter=app.extensions["limiter"],
            )
            client = app.test_client()
            version = common.asset_version()
            for name in names:
                query = "" if name == "redirect.js" else f"?v={version}"
                response = await client.get(f"{prefix}/{name}{query}")
                assert response.status_code == 200
                assert response.mimetype == "text/javascript"
                assert await response.get_data(as_text=True) == f"/* {name} */"
                cache = response.headers["Cache-Control"]
                assert ("no-cache" in cache) if name == "redirect.js" else ("immutable" in cache)
                if name == "redirect.js":
                    cached = await client.get(
                        f"{prefix}/redirect.js", headers={"If-None-Match": response.headers["ETag"]}
                    )
                    assert cached.status_code == 304
                    assert "no-cache" in cached.headers["Cache-Control"]

            redirect = await client.get(f"{prefix}/redirect?prefix=happ://test")
            assert redirect.status_code == 200
            assert 'src="./redirect.js"' in await redirect.get_data(as_text=True)

            response = await client.get(
                "/missing/deep/page",
                headers={"User-Agent": "Mozilla/5.0 Chrome/130.0 Safari/537.36"},
            )
            assert response.status_code == 404
            assert response.mimetype == "text/html"
            html = await response.get_data(as_text=True)
            assert f'<script src="{prefix}/error.js?v={version}"></script>' in html
            assert html.index(f"{prefix}/error.js") < html.index("<style>")
    finally:
        common.asset_version.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/nested/sub"])
async def test_generated_resources_served_at_configured_prefix(
    database: Database,
    language: Config[LangConfig],
    prefix: str,
) -> None:
    async with api_app() as app:
        sub = await make_subscription(database, app=app, lang_cfg=language, uri=prefix.strip("/"))
        AsyncWebApi(
            app=app,
            cfg=config_snapshot(subscription_config(uri=prefix.strip("/"))),
            sub=sub,
            bw=make_watch(database, sub),
            limiter=app.extensions["limiter"],
        )
        client = app.test_client()
        response = await client.get(f"{prefix}/auth")
        html = await response.get_data(as_text=True)
        version = common.asset_version()
        assert response.status_code == 200
        for name, content_type in (
            ("common.js", "text/javascript"),
            ("auth.js", "text/javascript"),
            ("common.css", "text/css"),
            ("auth.css", "text/css"),
        ):
            url = f"{prefix}/{name}?v={version}"
            assert url in html
            asset = await client.get(url)
            assert asset.status_code == 200
            assert asset.mimetype == content_type
            assert await asset.get_data()
            assert "immutable" in asset.headers["Cache-Control"]

        css = await (await client.get(f"{prefix}/common.css?v={version}")).get_data(as_text=True)
        for name in FONT_FILES:
            url = f"{prefix}/fonts/{name}?v={font_version()}"
            assert url in css
            font = await client.get(url)
            assert font.mimetype == "font/woff2"
            body = await font.get_data()
            assert isinstance(body, bytes)
            assert body.startswith(b"wOF2")

        for url in (
            f"{prefix}/admin/config/state.js",
            f"{prefix}/frontend/src/templates/auth.html",
            f"{prefix}/vendor/LICENSE_CODEMIRROR",
            "/static/frontend/src/templates/auth.html",
        ):
            assert (await client.get(url)).status_code == 404

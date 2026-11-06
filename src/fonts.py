"""Self-hosted webfonts. Kept out of api/ so builders can use it without a cycle."""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha1

from paths import bundled_root

RES_DIR = bundled_root() / "res"

FONT_FILES: frozenset[str] = frozenset(
    {
        "outfit-latin.woff2",
        "jetbrains-mono-latin.woff2",
        "jetbrains-mono-cyrillic.woff2",
    },
)
_FONT_PLACEHOLDER = "__FONT_BASE__"
_VERSION_PLACEHOLDER = "__FONT_VERSION__"
FONTS_MARKER = "/* __FONTS__ */"


@lru_cache(maxsize=1)
def font_version() -> str:
    """Content version of the bundled webfonts, fixed for this process's release."""
    digest = sha1(usedforsecurity=False)
    for name in sorted(FONT_FILES):
        digest.update((RES_DIR / "fonts" / name).read_bytes())
    return digest.hexdigest()[:12]


def embed_font_faces(source: str, prefix: str) -> str:
    """Replace the fonts marker with @font-face rules under the URI prefix.

    CSS is served as a file, so a relative url() would resolve against the page
    path (/panel) instead of the asset root. The placeholder is substituted here.
    """
    if FONTS_MARKER not in source:
        raise ValueError("fonts marker is missing")
    css = (RES_DIR / "fonts.css").read_text(encoding="utf-8")
    if _FONT_PLACEHOLDER not in css:
        raise ValueError("fonts.css is missing the font base placeholder")
    if _VERSION_PLACEHOLDER not in css:
        raise ValueError("fonts.css is missing the font version placeholder")
    faces = css.replace(_FONT_PLACEHOLDER, prefix.rstrip("/")).replace(
        _VERSION_PLACEHOLDER,
        font_version(),
    )
    return source.replace(FONTS_MARKER, faces, 1)

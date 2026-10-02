"""Self-hosted webfonts. Kept out of api/ so builders can use it without a cycle."""
from pathlib import Path

RES_DIR = Path(__file__).resolve().parent.parent / 'res'

FONT_FILES: frozenset[str] = frozenset({
    'outfit-latin.woff2',
    'jetbrains-mono-latin.woff2',
    'jetbrains-mono-cyrillic.woff2',
})
_FONT_PLACEHOLDER = '__FONT_BASE__'
FONTS_MARKER = '/* __FONTS__ */'


def embed_font_faces(source: str, prefix: str) -> str:
    """Replace the fonts marker with @font-face rules under the URI prefix.

    CSS is served as a file, so a relative url() would resolve against the page
    path (/panel) instead of the asset root. The placeholder is substituted here.
    """
    if FONTS_MARKER not in source:
        raise ValueError('fonts marker is missing')
    css = (RES_DIR / 'fonts.css').read_text(encoding='utf-8')
    if _FONT_PLACEHOLDER not in css:
        raise ValueError('fonts.css is missing the font base placeholder')
    faces = css.replace(_FONT_PLACEHOLDER, prefix.rstrip('/'))
    return source.replace(FONTS_MARKER, faces, 1)

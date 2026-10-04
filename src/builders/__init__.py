"""Methods to build links and JSON arrays for clients.

Public names stay importable from this package so callers do not change.
``isbrowser``, ``render_template``, and ``embed_font_faces`` are re-exported
because subscription generation (and its tests) patch them on this package.
"""

from __future__ import annotations

from flask import render_template

from fonts import embed_font_faces
from util import isbrowser

from .browser import _browser_strings
from .description import build_description
from .json_profiles import build_json
from .links import build_link_array
from .subscription import get_subscription

__all__ = [
    "build_description",
    "build_link_array",
    "build_json",
    "get_subscription",
    "_browser_strings",
    "isbrowser",
    "render_template",
    "embed_font_faces",
]

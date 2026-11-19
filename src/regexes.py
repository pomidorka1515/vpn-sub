from __future__ import annotations

import re

# the root of all evil
# currently has 1 regex

__all__ = ["BROWSER_UA"]

BROWSER_UA = re.compile(
    r"(MSIE|Trident|(?!Gecko.+)Firefox|(?!AppleWebKit.+Chrome.+)Safari(?!.+Edge)|(?!AppleWebKit.+)Chrome(?!.+Edge)|(?!AppleWebKit.+Chrome.+Safari.+)Edge|AppleWebKit(?!.+Chrome|.+Safari)|Gecko(?!.+Firefox))(?: |\/)([\d\.apre]+)",
)

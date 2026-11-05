"""Bounded, independently formatted messages for administrator lists."""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


def _size(text: str) -> int:
    # Counting UTF-16 units (including markup) is conservative for both APIs.
    return len(text.encode("utf-16-le")) // 2


def list_messages(
    header: str,
    entries: Iterable[tuple[str, str | None]],
    *,
    limit: int,
    html: bool = False,
) -> Iterator[str]:
    """Split on rows, or raw field characters for an oversized row.

    Escape before measuring, but never split an entity or formatting wrapper.
    Every message repeats the heading and is valid on its own.
    """
    budget = limit - _size(header)
    opening, closing = ("<code>", "</code>") if html else ("`", "`")
    message = header
    for name, login in entries:
        line = "- "
        fields = [(opening, name, closing)]
        if login:
            fields.append((", логин: ", login, ""))
        for prefix, value, suffix in fields:
            part = prefix
            for char in value:
                token = escape(char) if html else char
                if _size(line + part + token + suffix) > budget:
                    if part != prefix:
                        line += part + suffix
                        part = prefix
                    if message != header and _size(message + "\n" + line) > limit:
                        yield message
                        message = header
                    message += ("" if message == header else "\n") + line
                    line = "- "
                part += token
            line += part + suffix
        if message != header and _size(message + "\n" + line) > limit:
            yield message
            message = header
        message += ("" if message == header else "\n") + line
    if message != header:
        yield message

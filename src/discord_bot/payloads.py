"""Discord response options."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import discord


class ResponseOptions(TypedDict, total=False):
    ephemeral: bool
    content: str
    view: discord.ui.View
    file: discord.File

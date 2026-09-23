from __future__ import annotations

from typing import Any, Mapping, cast

import discord

from composition import PublicFeatureMixin
from webapi import ApiResult

__all__ = ["DiscordIOMixin"]


class DiscordIOMixin(PublicFeatureMixin):
    """Shared send, edit, and ephemeral helpers for Discord interactions."""

    def _ephemeral(self, interaction: discord.Interaction, ephemeral: bool | None) -> bool:
        if ephemeral is not None:
            return ephemeral
        return not self.in_dm(interaction)

    async def _respond(
        self,
        interaction: discord.Interaction,
        content: str | None,
        *,
        ephemeral: bool | None = None,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
    ) -> None:
        if not content and view is None and file is None:
            return
        ephemeral = self._ephemeral(interaction, ephemeral)
        kwargs: dict[str, Any] = {"ephemeral": ephemeral}
        if content is not None:
            kwargs["content"] = content
        if view is not None:
            kwargs["view"] = view
        if file is not None:
            kwargs["file"] = file
        try:
            if interaction.response.is_done():
                kwargs.pop("ephemeral", None)
                if ephemeral:
                    kwargs["ephemeral"] = True
                await interaction.followup.send(**kwargs)
            else:
                await interaction.response.send_message(**kwargs)
        except Exception:
            self.log.error("failed to respond to interaction", exc_info=True)

    async def _reply_key(
        self,
        interaction: discord.Interaction,
        key: str,
        *,
        ephemeral: bool | None = None,
        view: discord.ui.View | None = None,
        file: discord.File | None = None,
        **fmt: object,
    ) -> None:
        lang = self.get_lang(interaction.user.id)
        text = self.text(lang, key, **fmt)
        if text is None and view is None and file is None:
            return
        await self._respond(
            interaction,
            text,
            ephemeral=ephemeral,
            view=view,
            file=file,
        )

    async def _defer(self, interaction: discord.Interaction, *, ephemeral: bool | None = None) -> None:
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer(ephemeral=self._ephemeral(interaction, ephemeral))
        except Exception:
            self.log.error("failed to defer interaction", exc_info=True)

    def modal_values(self, interaction: discord.Interaction) -> dict[str, str]:
        out: dict[str, str] = {}
        data = cast(Mapping[str, Any] | None, interaction.data)
        if not data:
            return out
        rows_raw = data.get("components")
        if not isinstance(rows_raw, list):
            return out
        rows = cast(list[dict[str, Any]], rows_raw)
        for row in rows:
            items_raw = row.get("components")
            if not isinstance(items_raw, list):
                continue
            items = cast(list[dict[str, Any]], items_raw)
            for item in items:
                cid = item.get("custom_id")
                if isinstance(cid, str):
                    value = item.get("value")
                    out[cid] = value if isinstance(value, str) else str(value or "")
        return out

    def apply_api_status(self, user_id: int, result: ApiResult) -> str | None:
        if result.status == 401:
            self.sessions.clear_token(user_id)
            return "session_expired"
        if result.status == 429:
            return "rate_limited"
        if result.status == 0 or result.msg == "http_unavailable":
            return "http_unavailable"
        return None

    async def consume_result(
        self,
        interaction: discord.Interaction,
        result: ApiResult,
    ) -> bool:
        key = self.apply_api_status(interaction.user.id, result)
        if key is not None:
            await self._reply_key(interaction, key)
            return False
        if not result.ok:
            if result.msg:
                await self._respond(interaction, result.msg)
            else:
                await self._reply_key(interaction, "bad_response")
            return False
        return True

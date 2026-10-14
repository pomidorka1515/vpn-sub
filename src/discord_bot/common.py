from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from discord_bot.composition import PublicFeatureMixin

if TYPE_CHECKING:
    import discord
    from discord_bot.payloads import ResponseOptions

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
        kwargs: ResponseOptions = {"ephemeral": ephemeral}
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
            self.log.exception("failed to respond to interaction")

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

    async def _defer(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool | None = None,
    ) -> None:
        if interaction.response.is_done():
            return
        try:
            await interaction.response.defer(ephemeral=self._ephemeral(interaction, ephemeral))
        except Exception:
            self.log.exception("failed to defer interaction")

    def modal_values(self, interaction: discord.Interaction) -> dict[str, str]:
        out: dict[str, str] = {}
        data = cast(Mapping[str, object] | None, interaction.data)
        if not data:
            return out
        rows_raw = data.get("components")
        if not isinstance(rows_raw, list):
            return out
        rows = cast(list[dict[str, object]], rows_raw)
        for row in rows:
            items_raw = row.get("components")
            if not isinstance(items_raw, list):
                continue
            items = cast(list[dict[str, object]], items_raw)
            for item in items:
                cid = item.get("custom_id")
                if isinstance(cid, str):
                    value = item.get("value")
                    out[cid] = value if isinstance(value, str) else str(value or "")
        return out

    async def username(self, interaction: discord.Interaction) -> str:
        from discord_bot.exceptions import SessionExpiredError

        token = self.sessions.token(interaction.user.id)
        username = await self.sub.user_svc.auth_token_to_user(token) if token else None
        if username is None:
            self.sessions.clear_token(interaction.user.id)
            raise SessionExpiredError
        return username

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Literal, Self

import discord

from bots.discord.composition import PublicFeatureMixin
from util import compare

__all__ = ["PublicAccountMixin"]

_CONFIRM_TTL = 300.0


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    nonce: str
    action: Literal["logout", "reset"]
    token: str
    generation: object
    expires: float


class BonusModal(discord.ui.Modal):
    def __init__(self, title: str, label: str) -> None:
        super().__init__(title=title, custom_id="bonus_modal")
        self.code: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=label,
            custom_id="code",
            min_length=1,
            max_length=64,
        )
        self.add_item(self.code)


class DeleteModal(discord.ui.Modal):
    def __init__(self, title: str, label: str) -> None:
        super().__init__(title=title, custom_id="delete_modal")
        self.password: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=label,
            custom_id="current_password",
            min_length=1,
            max_length=128,
        )
        self.add_item(self.password)


class PublicAccountMixin(PublicFeatureMixin):
    def _new_confirmation(self, user_id: int, action: Literal["logout", "reset"]) -> str | None:
        session = self.sessions.get(user_id)
        if not session.token or user_id in self._confirmation_busy:
            return None
        now = time.monotonic()
        self._pending_confirmations = {
            uid: pending
            for uid, pending in self._pending_confirmations.items()
            if pending.expires > now
        }
        nonce = secrets.token_urlsafe(24)
        self._pending_confirmations[user_id] = PendingConfirmation(
            nonce,
            action,
            session.token,
            session.generation,
            now + _CONFIRM_TTL,
        )
        return nonce

    def cancel_confirmation(self, user_id: int, nonce: str) -> None:
        pending = self._pending_confirmations.get(user_id)
        if pending is not None and compare(pending.nonce, nonce):
            self._pending_confirmations.pop(user_id)

    async def cmd_bonus(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = BonusModal(title=t.get("btn_bonus", "Bonus"), label=t.get("enter_bonus", "Code"))
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.exception("failed to open bonus modal")

    async def handle_bonus_modal(self, interaction: discord.Interaction) -> None:
        if not await self.require_login(interaction):
            return
        values = self.modal_values(interaction)
        code = values.get("code", "").strip()
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        await self.sub.code_svc.apply_bonus_code(
            username=await self.username(interaction),
            code=code,
        )
        await self._reply_key(interaction, "bonus_success")
        await self.send_info(interaction)

    async def cmd_logout(self, interaction: discord.Interaction) -> None:
        nonce = self._new_confirmation(interaction.user.id, "logout")
        if nonce is None:
            await self._reply_key(
                interaction,
                "cancelled" if interaction.user.id in self._confirmation_busy else "not_logged_in",
            )
            return
        lang = self.get_lang(interaction.user.id)
        warning = self.text(lang, "logout_web_warning")
        await self._respond(
            interaction,
            warning,
            view=self.confirm_view(lang, "logout", nonce),
        )

    async def cmd_reset(self, interaction: discord.Interaction) -> None:
        nonce = self._new_confirmation(interaction.user.id, "reset")
        if nonce is None:
            await self._reply_key(
                interaction,
                "cancelled" if interaction.user.id in self._confirmation_busy else "not_logged_in",
            )
            return
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(
            interaction,
            "confirm_reset",
            view=self.confirm_view(lang, "reset", nonce),
        )

    async def cmd_delete(self, interaction: discord.Interaction) -> None:
        lang = self.get_lang(interaction.user.id)
        t = self.TEXTS[lang]
        modal = DeleteModal(
            title=t.get("btn_delete", "Delete"),
            label=t.get("delete_need_password", t.get("enter_pass", "Password")),
        )
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            self.log.exception("failed to open delete modal")

    async def confirm_operation(
        self,
        interaction: discord.Interaction,
        action: str,
        nonce: str,
    ) -> None:
        uid = interaction.user.id
        pending = self._pending_confirmations.get(uid)
        if pending is None or pending.action != action or not compare(pending.nonce, nonce):
            await self._reply_key(interaction, "cancelled")
            return
        # Consume before any await: concurrent clicks cannot both proceed.
        self._pending_confirmations.pop(uid)
        session = self.sessions.get(uid)
        if (
            pending.expires <= time.monotonic()
            or session.generation is not pending.generation
            or session.token != pending.token
        ):
            await self._reply_key(interaction, "cancelled")
            return
        self._confirmation_busy.add(uid)
        try:
            await self._defer(interaction)
            username = await self.sub.user_svc.auth_token_to_user(pending.token)
            session = self.sessions.get(uid)
            if (
                username is None
                or pending.expires <= time.monotonic()
                or session.generation is not pending.generation
                or session.token != pending.token
            ):
                await self._reply_key(interaction, "cancelled")
                return
            if pending.action == "logout":
                await self.sub.account_svc.logout(username)
            else:
                await self.sub.business_svc.reset_user(username)
            if self.sessions.get(uid).generation is pending.generation:
                self.sessions.clear_token(uid)
            lang = self.get_lang(uid)
            await self._reply_key(
                interaction,
                f"{pending.action}_success",
                view=self.main_menu_view(lang, logged_in=self.sessions.token(uid) is not None),
            )
        finally:
            self._confirmation_busy.discard(uid)

    async def handle_delete_modal(self, interaction: discord.Interaction) -> None:
        values = self.modal_values(interaction)
        password = values.get("current_password", "")
        token = self.sessions.token(interaction.user.id)
        if not token:
            await self._reply_key(interaction, "not_logged_in")
            return
        await self._defer(interaction)
        await self.sub.account_svc.delete(await self.username(interaction), password)
        self.sessions.clear_token(interaction.user.id)
        lang = self.get_lang(interaction.user.id)
        await self._reply_key(
            interaction,
            "delete_success",
            view=self.main_menu_view(lang, logged_in=False),
        )

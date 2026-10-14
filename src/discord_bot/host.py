from __future__ import annotations

# The host owns the feature interaction boundary.
# ruff: file-ignore[private-member-access]
from typing import TYPE_CHECKING

import discord
from app_runtime.tasks import AdmissionClosedError, TaskRegistry
from discord import app_commands
from discord_bot.exceptions import SessionExpiredError
from errors import AppError, PanelUnavailableError
from loggers import Logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord_bot.admin.bot import AdminBot
    from discord_bot.public.bot import PublicBot

__all__ = ["SharedDiscordClient"]

log = Logger("discord.host")


class SharedDiscordClient(discord.Client):
    """One Discord client that syncs the shared tree and fans out interactions."""

    def __init__(
        self,
        *,
        intents: discord.Intents,
        tree: app_commands.CommandTree | None = None,
    ) -> None:
        super().__init__(intents=intents)
        self.tree = tree or app_commands.CommandTree(self)
        self._public: PublicBot | None = None
        self._admin: AdminBot | None = None
        self.handlers = TaskRegistry()
        self._wire_events()

    def attach(self, public: PublicBot, admin: AdminBot) -> None:
        self._public = public
        self._admin = admin

    async def process(self, user_id: str, callback: Callable[[], Awaitable[None]]) -> None:
        del user_id

        async def run() -> None:
            await callback()
        try:
            await self.handlers.create(run, name="discord:interaction")
        except AdmissionClosedError:
            return

    async def interact(
        self, interaction: discord.Interaction, callback: Callable[[], Awaitable[None]],
        *, operation: str, admin: bool = False,
    ) -> None:
        async def invoke() -> None:
            feature = self._admin if admin else self._public
            if feature is None:
                return
            if admin and self._admin is not None and not await self._admin.require_admin(interaction):
                return
            # Normalize IDs containing user names, pages or selected values so
            # changing a component's arguments cannot create a fresh bucket.
            scope, limit = interaction_limit(operation)
            try:
                allowed = await feature.limiter.allow(f"discord:{scope}", str(interaction.user.id), limit)
            except Exception:
                log.error("Discord rate limit store failed")  # ruff: ignore[error-instead-of-exception] avoid logging Redis credentials
                allowed = False
            if not allowed:
                if admin:
                    await feature._respond(interaction, "⏳ Слишком много запросов. Подождите минуту.")
                elif self._public is not None:
                    await self._public._reply_key(interaction, "rate_limited")
                return
            try:
                await callback()
            except SessionExpiredError:
                if self._public is not None:
                    await self._public._reply_key(interaction, "session_expired")
            except PanelUnavailableError:
                await feature._respond(interaction, "⚠ Сервис временно недоступен." if admin else "Service temporarily unavailable.")
            except AppError as error:
                await feature._respond(interaction, error.message)
            except Exception:
                log.exception("Discord interaction failed")
                await feature._respond(interaction, "❌ Внутренняя ошибка" if admin else "Service temporarily unavailable.")
        await self.process(str(interaction.user.id), invoke)

    async def close(self) -> None:
        self.handlers.stop_admission()
        await super().close()
        await self.handlers.drain(cancel=True)

    def _wire_events(self) -> None:
        @self.event
        async def on_ready() -> None:
            log.info("discord bot is ready")

        @self.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            if interaction.type not in {discord.InteractionType.component, discord.InteractionType.modal_submit}:
                return
            custom_id = _custom_id(interaction)
            public = self._public
            admin = self._admin
            if public is None or admin is None:
                return

            async def dispatch() -> None:
                if interaction.type is discord.InteractionType.component:
                    await _dispatch(custom_id, admin.dispatch_component, public.dispatch_component, interaction)
                elif interaction.type is discord.InteractionType.modal_submit:
                    await _dispatch(custom_id, admin.dispatch_modal, public.dispatch_modal, interaction)
            await self.interact(interaction, dispatch, operation=custom_id, admin=custom_id.startswith("admin"))

    async def setup_hook(self) -> None:
        try:
            await self.tree.sync()
        except Exception:
            log.exception("failed to sync slash commands")


def _custom_id(interaction: discord.Interaction) -> str:
    data = interaction.data
    if not isinstance(data, dict):
        return ""
    custom_id = data.get("custom_id")
    return custom_id if isinstance(custom_id, str) else ""


async def _dispatch(
    custom_id: str,
    admin: Callable[[discord.Interaction], Awaitable[None]],
    public: Callable[[discord.Interaction], Awaitable[None]],
    interaction: discord.Interaction,
) -> None:
    if custom_id.startswith("admin"):
        await admin(interaction)
        return
    await public(interaction)


def interaction_limit(operation: str) -> tuple[str, int]:
    if operation.startswith("admin:"):
        parts = operation.split(":")
        return ":".join(parts[:3] if parts[1] in {"modal", "edit"} else parts[:2]), 20
    aliases = {
        "login_modal": "login", "register_modal": "register", "confirm_logout": "logout",
        "confirm_reset": "reset", "delete_modal": "delete", "menu_info": "stats",
        "info": "stats", "menu_get_sub": "subscription", "sub": "subscription",
        "bonus_modal": "bonus", "chart_select": "history", "settings_name_modal": "settings",
        "settings_login_modal": "settings", "settings_pass_modal": "settings",
        "fp_select": "settings", "menu_help": "profiles", "help": "profiles",
        "set_fp": "fingerprints",
    }
    scope = aliases.get(operation, operation)
    limits = {"login": 10, "register": 5, "logout": 20, "reset": 3, "delete": 3,
              "bonus": 15, "settings": 15, "history": 30, "profiles": 60}
    return scope, limits.get(scope, 20)

"""Administrator user creation and reset workflows."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast

from errors import AppError

from ..composition import AdminFeatureMixin

if TYPE_CHECKING:
    from telebot import types

__all__ = ["AdminUserWorkflowsMixin"]


class AdminUserWorkflowsMixin(AdminFeatureMixin):
    """User creation and reset workflows."""

    async def _step_reset_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        username = text.strip()

        try:
            obj = await self.sub.business_svc.reset_user(username)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return

        await self.bot.send_message(
            message.chat.id,
            f"✅ Пользователь был сброшен.\n\nToken: <code>{obj.token}</code>\nUUID: <code>{obj.uuid}</code>",
            parse_mode="HTML",
            reply_markup=self.get_main_menu(),
        )

    async def _step_add_user_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        username = text.strip()
        if await self.sub.user_svc.isuser(username):
            await self.bot.send_message(
                message.chat.id,
                "❌ Этот username уже существует.",
                reply_markup=self.get_main_menu(),
            )
            return

        msg = await self.bot.send_message(message.chat.id, "Введите отображаемое имя:")
        self._expect(msg, self._step_add_user_display, username)  # pyright: ignore[reportUnknownMemberType]

    async def _step_add_user_display(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        displayname = text.strip()

        msg = await self.bot.send_message(
            message.chat.id,
            "Введите лимит в гигабайтах (или 0 для безлимита):",
        )
        self._expect(msg, self._step_add_user_limit, username, displayname)  # pyright: ignore[reportUnknownMemberType]

    async def _step_add_user_limit(
        self,
        message: types.Message,
        username: str,
        displayname: str,
    ) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        try:
            limit = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Ошибка: Лимит должен быть числом.",
                reply_markup=self.get_main_menu(),
            )
            return
        msg = await self.bot.send_message(
            message.chat.id,
            "Введите кол-во дней подписки (0 для безлимита):",
        )
        self._expect(  # pyright: ignore[reportUnknownMemberType]
            msg,
            self._step_add_user_time,
            username,
            displayname,
            limit,
        )

    async def _step_add_user_time(
        self,
        message: types.Message,
        username: str,
        displayname: str,
        limit: int,
    ) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return

        try:
            timee = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Ошибка: Лимит времени должен быть числом.",
                reply_markup=self.get_main_menu(),
            )
            return

        timee = int(time.time()) + timee * 86400 if timee else 0
        try:
            await self.sub.business_svc.add_new_user(
                username=username,
                displayname=displayname,
                limit=limit,
                timee=timee,
            )
            await self._send_message(
                message.chat.id,
                f"✅ Пользователь <b>{username}</b> успешно добавлен!",
                parse_mode="HTML",
                reply_markup=self.get_main_menu(),
            )
        except AppError as error:
            await self._send_message(
                message.chat.id,
                f"❌ Ошибка: {error.message}",
                reply_markup=self.get_main_menu(),
            )
        except Exception:
            self.log.exception("Telegram handler failed")
            await self._send_message(
                message.chat.id,
                "❌ Внутренняя ошибка",
                reply_markup=self.get_main_menu(),
            )

"""Public login workflows."""

from __future__ import annotations

from typing import cast

from telebot import types

from ..composition import PublicFeatureMixin

__all__ = ["PublicLoginMixin"]


class PublicLoginMixin(PublicFeatureMixin):
    """Username and password login workflows."""

    async def login_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if await self.sub.telegram_svc.is_registered(uid):
            return
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data

        await self._answer_callback(call.id)

        if action == "login_credentials":
            msg = await self.bot.send_message(
                message.chat.id,
                t["enter_email"],
                reply_markup=types.ReplyKeyboardRemove(),
            )
            self._expect(msg, self.step_login_email)  # pyright: ignore[reportUnknownMemberType]

    async def step_login_email(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)

        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        email = text.strip()

        msg = await self.bot.send_message(message.chat.id, t["enter_pass"])
        self._expect(msg, self.step_login_pass, email)  # pyright: ignore[reportUnknownMemberType]
        return None

    async def step_login_pass(self, message: types.Message, email: str) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)

        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        password = text.strip()

        await self._delete_message(message.chat.id, message.message_id, secret=True)

        internal_username = await self.sub.password_svc.validate_credentials(email, password)
        if internal_username:
            await self.sub.telegram_svc.set_telegram_user(uid, internal_username)
            await self.bot.send_message(
                message.chat.id,
                t["login_success"],
                reply_markup=await self.get_menu(uid),
            )
            await self.send_info(message.chat.id, uid, lang)
            return None

        await self.bot.send_message(
            message.chat.id,
            t["login_fail"],
            reply_markup=await self.get_menu(uid),
        )
        return None

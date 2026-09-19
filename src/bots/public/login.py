"""Public login workflows."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

from typing import cast

from telebot import types
__all__ = ["PublicLoginMixin"]


class PublicLoginMixin(PublicFeatureMixin):
    """Username and password login workflows."""

    def login_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if self.sub.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data

        self.bot.answer_callback_query(call.id)

        if action == "login_credentials":
            msg = self.bot.send_message(message.chat.id, t['enter_email'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_login_email)  # pyright: ignore[reportUnknownMemberType]
    def step_login_email(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)

        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        email = text.strip()

        msg = self.bot.send_message(message.chat.id, t['enter_pass'])
        self.bot.register_next_step_handler(msg, self.step_login_pass, email)  # pyright: ignore[reportUnknownMemberType]

    def step_login_pass(self, message: types.Message, email: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)

        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        password = text.strip()

        self._delete_message(message.chat.id, message.message_id, secret=True)

        internal_username = self.sub.validate_credentials(email, password)
        if internal_username:
            self.sub.set_telegram_user(uid, internal_username)
            self.bot.send_message(message.chat.id, t['login_success'], reply_markup=self.get_menu(uid))
            self.send_info(message.chat.id, uid, lang)
            return

        self.bot.send_message(message.chat.id, t['login_fail'], reply_markup=self.get_menu(uid))



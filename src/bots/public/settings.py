"""Public account settings workflows."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

from typing import cast

from telebot import types

from errors import AppError
__all__ = ["PublicSettingsMixin"]


class PublicSettingsMixin(PublicFeatureMixin):
    """Display-name, fingerprint, external-login, and password settings."""

    def settings_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not self.sub.telegram_svc.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data  # set_name, set_fp, set_pass, set_login

        self.bot.answer_callback_query(call.id)
        self._delete_message(message.chat.id, message.message_id)

        if action == "set_name":
            msg = self.bot.send_message(message.chat.id, t['settings_name_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_name)  # pyright: ignore[reportUnknownMemberType]
        elif action == "set_fp":
            markup = types.InlineKeyboardMarkup(row_width=2)
            username = self.sub.telegram_svc.get_username_telegram(uid)
            current_fp = self.sub.user_svc.get_fingerprint(username) if isinstance(username, str) else ''
            for fp in self.cfg['fingerprints']:
                label = f"✅ {fp}" if fp == current_fp else fp
                markup.add(types.InlineKeyboardButton(label, callback_data=f"fp_{fp}"))  # pyright: ignore[reportUnknownMemberType]
            self.bot.send_message(message.chat.id, t['settings_fp_prompt'], reply_markup=markup)
        elif action == "set_login":
            msg = self.bot.send_message(message.chat.id, t['settings_login_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_login)  # pyright: ignore[reportUnknownMemberType]
        elif action == "set_pass":
            msg = self.bot.send_message(message.chat.id, t['settings_pass_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_pass)  # pyright: ignore[reportUnknownMemberType]

    def fp_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not self.sub.telegram_svc.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        fp = data[3:]  # strip "fp_"

        username = self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str): return

        self.bot.answer_callback_query(call.id)
        try:
            self.sub.business_svc.update_params(username=username, fingerprint=fp)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_fp_success'], reply_markup=self.get_menu(uid))
        self._delete_message(message.chat.id, message.message_id)

    def step_settings_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        username = self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str): return

        new_name = text.strip()
        if len(new_name) > 16:
            self.bot.send_message(message.chat.id, t['length_displayname'].format(ln=16), reply_markup=self.get_menu(uid))
            return

        try:
            self.sub.business_svc.update_params(username=username, displayname=new_name)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_name_success'], reply_markup=self.get_menu(uid))

    def step_settings_login(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        username = self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str): return

        new_login = text.strip()
        if len(new_login) > 32:
            self.bot.send_message(message.chat.id, t['length_username'].format(ln=16), reply_markup=self.get_menu(uid))
            return

        try:
            self.sub.business_svc.update_params(username=username, ext_username=new_login)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_login_success'], reply_markup=self.get_menu(uid))

    def step_settings_pass(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        username = self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str): return
        if not self.sub.user_svc.get_external_username(username):
            self.bot.send_message(message.chat.id, t['no_account'], reply_markup=self.get_menu(uid))
            return
        new_pass = text.strip()
        self._delete_message(message.chat.id, message.message_id, secret=True)

        # Need current ext_username to update password (update_params requires both)
        ext_username = self.sub.user_svc.get_external_username(username)
        if not ext_username:
            self.bot.send_message(message.chat.id, "❌ No login found", reply_markup=self.get_menu(uid))
            return

        try:
            self.sub.business_svc.update_params(username=username, ext_username=ext_username, ext_password=new_pass)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_pass_success'], reply_markup=self.get_menu(uid))



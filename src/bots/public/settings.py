"""Public account settings workflows."""

from __future__ import annotations

from typing import cast

from telebot import types

from errors import AppError

from ..composition import PublicFeatureMixin

__all__ = ["PublicSettingsMixin"]


class PublicSettingsMixin(PublicFeatureMixin):
    """Display-name, fingerprint, external-login, and password settings."""

    async def settings_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not await self.sub.telegram_svc.is_registered(uid):
            return
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data  # set_name, set_fp, set_pass, set_login

        await self._answer_callback(call.id)
        await self._delete_message(message.chat.id, message.message_id)

        if action == "set_name":
            msg = await self.bot.send_message(
                message.chat.id,
                t["settings_name_prompt"],
                reply_markup=types.ReplyKeyboardRemove(),
            )
            self._expect(msg, self.step_settings_name)  # pyright: ignore[reportUnknownMemberType]
        elif action == "set_fp":
            markup = types.InlineKeyboardMarkup(row_width=2)
            username = await self.sub.telegram_svc.get_username_telegram(uid)
            current_fp = (
                await self.sub.user_svc.get_fingerprint(username)
                if isinstance(username, str)
                else ""
            )
            conf = self.cfg.view()
            for fp in conf["fingerprints"]:
                label = f"✅ {fp}" if fp == current_fp else fp
                markup.add(types.InlineKeyboardButton(label, callback_data=f"fp_{fp}"))  # pyright: ignore[reportUnknownMemberType]
            await self.bot.send_message(
                message.chat.id, t["settings_fp_prompt"], reply_markup=markup
            )
        elif action == "set_login":
            msg = await self.bot.send_message(
                message.chat.id,
                t["settings_login_prompt"],
                reply_markup=types.ReplyKeyboardRemove(),
            )
            self._expect(msg, self.step_settings_login)  # pyright: ignore[reportUnknownMemberType]
        elif action == "set_pass":
            msg = await self.bot.send_message(
                message.chat.id,
                t["settings_pass_prompt"],
                reply_markup=types.ReplyKeyboardRemove(),
            )
            self._expect(msg, self.step_settings_pass)  # pyright: ignore[reportUnknownMemberType]

    async def fp_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not await self.sub.telegram_svc.is_registered(uid):
            return
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        fp = data[3:]  # strip "fp_"

        username = await self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str):
            return

        await self._answer_callback(call.id)
        try:
            await self.sub.business_svc.update_params(username=username, fingerprint=fp)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ {error.message}",
                reply_markup=await self.get_menu(uid),
            )
        else:
            await self.bot.send_message(
                message.chat.id,
                t["settings_fp_success"],
                reply_markup=await self.get_menu(uid),
            )
        await self._delete_message(message.chat.id, message.message_id)

    async def step_settings_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        username = await self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str):
            return None

        new_name = text.strip()
        if len(new_name) > 16:
            await self.bot.send_message(
                message.chat.id,
                t["length_displayname"].format(ln=16),
                reply_markup=await self.get_menu(uid),
            )
            return None

        try:
            await self.sub.business_svc.update_params(username=username, displayname=new_name)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ {error.message}",
                reply_markup=await self.get_menu(uid),
            )
        else:
            await self.bot.send_message(
                message.chat.id,
                t["settings_name_success"],
                reply_markup=await self.get_menu(uid),
            )

    async def step_settings_login(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        username = await self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str):
            return None

        new_login = text.strip()
        if len(new_login) > 32:
            await self.bot.send_message(
                message.chat.id,
                t["length_username"].format(ln=16),
                reply_markup=await self.get_menu(uid),
            )
            return None

        try:
            await self.sub.business_svc.update_params(username=username, ext_username=new_login)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ {error.message}",
                reply_markup=await self.get_menu(uid),
            )
        else:
            await self.bot.send_message(
                message.chat.id,
                t["settings_login_success"],
                reply_markup=await self.get_menu(uid),
            )

    async def step_settings_pass(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        username = await self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str):
            return None
        if not await self.sub.user_svc.get_external_username(username):
            await self.bot.send_message(
                message.chat.id, t["no_account"], reply_markup=await self.get_menu(uid)
            )
            return None
        new_pass = text.strip()
        await self._delete_message(message.chat.id, message.message_id, secret=True)

        # Need current ext_username to update password (update_params requires both)
        ext_username = await self.sub.user_svc.get_external_username(username)
        if not ext_username:
            await self.bot.send_message(
                message.chat.id,
                "❌ No login found",
                reply_markup=await self.get_menu(uid),
            )
            return None

        try:
            await self.sub.business_svc.update_params(
                username=username,
                ext_username=ext_username,
                ext_password=new_pass,
            )
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ {error.message}",
                reply_markup=await self.get_menu(uid),
            )
        else:
            await self.bot.send_message(
                message.chat.id,
                t["settings_pass_success"],
                reply_markup=await self.get_menu(uid),
            )

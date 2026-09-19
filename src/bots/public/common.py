"""Public localization, menus, text routing, and lifecycle glue."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

from typing import cast

from telebot import types

from ..common import PublicStateMixin, TelegramIOMixin
from ..polling import TelegramPollingMixin
__all__ = ["PublicCommonMixin"]


class PublicCommonMixin(TelegramPollingMixin, TelegramIOMixin, PublicStateMixin, PublicFeatureMixin):
    """Localization, menus, start handling, text routing, and lifecycle glue."""

    def get_lang(self, uid: int) -> str:
        return self.sub.get_telegram_language(uid)

    def set_lang(self, uid: int, lang: str) -> None:
        self.sub.set_telegram_language(uid, lang)

    def msg(self, tgid: int | str | None, key: str, **kwargs: str | int | float | bool) -> None:
        if tgid is None or isinstance(tgid, str):
            return
        lang = self.get_lang(tgid)
        t = self.TEXTS[lang]
        text = t.get(key, None)
        if not text:
            return
        if kwargs:
            text = text.format(**kwargs)
        try:
            self.bot.send_message(tgid, text, parse_mode="HTML")
        except Exception as error:
            self.log.error(
                f"failed to send notification {key!r} to user {tgid}: {error}",
                exc_info=True,
            )

    def get_menu(self, uid: int) -> types.ReplyKeyboardMarkup:
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        is_reg = self.sub.is_registered(uid)

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        if not is_reg:
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t['btn_login'])
            )
        else:
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t['btn_main_account']),
                types.KeyboardButton(t['btn_main_sub'])
            )
        markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
        )
        return markup

    def cmd_start(self, message: types.Message) -> None:
        uid = cast(types.User, message.from_user).id
        self.bot.clear_step_handler_by_chat_id(message.chat.id)

        if not self.sub.has_telegram_language(uid):
            markup = types.InlineKeyboardMarkup()
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
            )
            self.bot.send_message(message.chat.id, "Welcome! Please choose your language:\nДобро пожаловать! Выберите язык:", reply_markup=markup)
        else:
            lang = self.get_lang(uid)
            t = self.TEXTS[lang]
            msg_text = t['welcome_reg'] if self.sub.is_registered(uid) else t['welcome_new']
            self.bot.send_message(message.chat.id, msg_text, reply_markup=self.get_menu(uid))

    def set_lang_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        new_lang = data.split('_', 1)[1]
        self.set_lang(uid, new_lang)

        self.bot.answer_callback_query(call.id)
        t = self.TEXTS[new_lang]
        self.bot.send_message(message.chat.id, t['lang_set'], reply_markup=self.get_menu(uid))

        self._delete_message(message.chat.id, message.message_id)

    def handle_text(self, message: types.Message) -> None:

        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        text = message.text


        if text in (self.TEXTS['ru']['btn_info'], self.TEXTS['en']['btn_info']):
            if not self.sub.is_registered(uid): return
            self.send_info(message.chat.id, uid, lang)

        elif text in (self.TEXTS['ru']['btn_bonus'], self.TEXTS['en']['btn_bonus']):
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['enter_bonus'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_bonus)  # pyright: ignore[reportUnknownMemberType]

        elif text in (self.TEXTS['ru']['btn_lang'], self.TEXTS['en']['btn_lang']):
            markup = types.InlineKeyboardMarkup()
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
            )
            self.bot.send_message(message.chat.id, t['choose_lang'], reply_markup=markup)
        elif text in (self.TEXTS['ru']['btn_login'], self.TEXTS['en']['btn_login']):
            if self.sub.is_registered(uid): return
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(t['btn_login_credentials'], callback_data="login_credentials")
            )
            self.bot.send_message(message.chat.id, t['choose_login'], reply_markup=markup)
            # msg = self.bot.send_message(message.chat.id, t['enter_email'], reply_markup=types.ReplyKeyboardRemove())
            # self.bot.register_next_step_handler(msg, self.step_login_email)
        elif text in (self.TEXTS['ru']['btn_main_sub'], self.TEXTS['en']['btn_main_sub']):
            if not self.sub.is_registered(uid): return
            reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            reply_markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t['btn_main_back']),

                types.KeyboardButton(t['btn_info']),
                types.KeyboardButton(t['btn_get_sub']),
                types.KeyboardButton(t['btn_bonus']),
                types.KeyboardButton(t['btn_reset']),
                types.KeyboardButton(t['btn_chart']),

                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
            )

            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=reply_markup)

        elif text in (self.TEXTS['ru']['btn_main_account'], self.TEXTS['en']['btn_main_account']):
            if not self.sub.is_registered(uid): return
            reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            reply_markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t['btn_main_back']),

                types.KeyboardButton(t['btn_settings']),
                types.KeyboardButton(t['btn_logout']),
                types.KeyboardButton(t['btn_help']),
                types.KeyboardButton(t['btn_delete']),

                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
            )

            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=reply_markup)

        elif text in (self.TEXTS['ru']['btn_main_back'], self.TEXTS['en']['btn_main_back']):
            if not self.sub.is_registered(uid): return
            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=self.get_menu(uid))

        elif text in (self.TEXTS['ru']['btn_reset'], self.TEXTS['en']['btn_reset']):
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['confirm_reset'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_reset)  # pyright: ignore[reportUnknownMemberType]

        elif text in (self.TEXTS['ru']['btn_support'], self.TEXTS['en']['btn_support']):
            self.bot.send_message(message.chat.id, t['support_text'], parse_mode="HTML")

        elif text in (self.TEXTS['ru']['btn_logout'], self.TEXTS['en']['btn_logout']):
            if not self.sub.is_registered(uid): return
            self.sub.set_telegram_user(uid, None)
            self.bot.send_message(message.chat.id, t['logout_success'], reply_markup=self.get_menu(uid))

        elif text in (self.TEXTS['ru']['btn_help'], self.TEXTS['en']['btn_help']):
            if not self.sub.is_registered(uid): return
            t = self.TEXTS[lang]
            text = ""
            for profile, desc in self.cfg['profileDescriptions'].items():
                profile_name = self.cfg['profiles'][profile][0 if lang == "en" else 1]
                profile_desc = desc[0 if lang == "en" else 1]
                text = text + f"<code>{profile_name}</code> — {profile_desc}\n"
            final_text = t['help_text'].format(
                text=text
            )
            self.bot.send_message(message.chat.id, final_text, parse_mode="HTML")

        elif text in (self.TEXTS['ru']['btn_settings'], self.TEXTS['en']['btn_settings']):
            if not self.sub.is_registered(uid): return
            t = self.TEXTS[lang]
            markup = types.InlineKeyboardMarkup(row_width=2)
            name_label = t['name_label']
            fp_label = t['fp_label']
            pass_label = t['pass_label']
            login_label = t['login_label']
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(name_label, callback_data="set_name"),
                types.InlineKeyboardButton(fp_label, callback_data="set_fp"),
                types.InlineKeyboardButton(login_label, callback_data="set_login"),
                types.InlineKeyboardButton(pass_label, callback_data="set_pass")
            )
            self.bot.send_message(message.chat.id, t['settings_menu'], parse_mode="HTML", reply_markup=markup)

        elif text in (self.TEXTS['ru']['btn_delete'], self.TEXTS['en']['btn_delete']):
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['confirm_delete'], parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_delete)  # pyright: ignore[reportUnknownMemberType]

        elif text in (self.TEXTS['ru']['btn_get_sub'], self.TEXTS['en']['btn_get_sub']):
            if not self.sub.is_registered(uid): return
            self.send_link(message.chat.id, uid, lang)

        elif text in (self.TEXTS['ru']['btn_chart'], self.TEXTS['en']['btn_chart']):
            if not self.sub.is_registered(uid): return
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=3), callback_data="chart_3"),
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=14), callback_data="chart_14"),
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=30), callback_data="chart_30"),
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=90), callback_data="chart_90")
            )
            self.bot.send_message(message.chat.id, t['choose_chart_days'], reply_markup=markup)

    def start(self) -> None:
        self.start_polling()

    def stop(self) -> None:
        self.stop_polling()
        self._executor.shutdown(wait=False, cancel_futures=True)



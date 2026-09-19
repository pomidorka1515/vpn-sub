"""Public localization, menus, and lifecycle glue."""
from __future__ import annotations

from typing import cast

from telebot import types

from ..common import PublicStateMixin, TelegramIOMixin
from ..composition import PublicFeatureMixin
from ..polling import TelegramPollingMixin
from .text_routing import PublicTextRoutingMixin

__all__ = ["PublicCommonMixin"]


class PublicCommonMixin(
    TelegramPollingMixin,
    TelegramIOMixin,
    PublicStateMixin,
    PublicTextRoutingMixin,
    PublicFeatureMixin,
):
    """Localization, menus, start handling, and lifecycle glue."""

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

    def start(self) -> None:
        self.start_polling()

    def stop(self) -> None:
        self.stop_polling()
        self._executor.shutdown(wait=False, cancel_futures=True)



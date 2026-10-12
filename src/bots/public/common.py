"""Public localization, menus, and lifecycle glue."""

from __future__ import annotations

from typing import cast

from telebot import types

from ..common import PublicStateMixin, TelegramIOMixin
from ..composition import PublicFeatureMixin
from ..conversations import ConversationMixin
from ..polling import TelegramPollingMixin
from .text_routing import PublicTextRoutingMixin

__all__ = ["PublicCommonMixin"]


class PublicCommonMixin(
    TelegramPollingMixin,
    TelegramIOMixin,
    PublicStateMixin,
    PublicTextRoutingMixin,
    ConversationMixin,
    PublicFeatureMixin,
):
    """Localization, menus, start handling, and lifecycle glue."""

    async def get_lang(self, uid: int) -> str:
        return await self.sub.telegram_svc.get_telegram_language(uid)

    async def send_notice(self, chat_id: int, text: str) -> None:
        # Notice markers require acceptance, so delivery errors must propagate.
        await self.bot.send_message(chat_id, text, parse_mode="HTML")

    async def set_lang(self, uid: int, lang: str) -> None:
        await self.sub.telegram_svc.set_telegram_language(uid, lang)

    async def get_menu(self, uid: int) -> types.ReplyKeyboardMarkup:
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]
        is_reg = await self.sub.telegram_svc.is_registered(uid)

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        if not is_reg:
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t["btn_login"]),
            )
        else:
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.KeyboardButton(t["btn_main_account"]),
                types.KeyboardButton(t["btn_main_sub"]),
            )
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.KeyboardButton(t["btn_lang"]),
            types.KeyboardButton(t["btn_support"]),
        )
        return markup

    async def cmd_start(self, message: types.Message) -> None:
        uid = cast(types.User, message.from_user).id
        self._clear_steps(message.chat.id)

        if not await self.sub.telegram_svc.has_telegram_language(uid):
            markup = types.InlineKeyboardMarkup()
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
            )
            await self.bot.send_message(
                message.chat.id,
                "Welcome! Please choose your language:\nДобро пожаловать! Выберите язык:",
                reply_markup=markup,
            )
        else:
            lang = await self.get_lang(uid)
            t = self.TEXTS[lang]
            msg_text = (
                t["welcome_reg"]
                if await self.sub.telegram_svc.is_registered(uid)
                else t["welcome_new"]
            )
            await self.bot.send_message(
                message.chat.id, msg_text, reply_markup=await self.get_menu(uid),
            )

    async def set_lang_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        new_lang = data.split("_", 1)[1]
        await self.set_lang(uid, new_lang)

        await self._answer_callback(call.id)
        t = self.TEXTS[new_lang]
        await self.bot.send_message(
            message.chat.id, t["lang_set"], reply_markup=await self.get_menu(uid),
        )

        await self._delete_message(message.chat.id, message.message_id)

    def _menu_message(self, text: str) -> bool:
        return any(
            text == texts.get(button)
            for texts in self.TEXTS.values()
            for button, _, _ in self.ROUTES
        )

    async def handle_callbacks(self, call: types.CallbackQuery) -> None:
        data = call.data or ""
        for prefix, handler in (
            ("lang_", self.set_lang_callback),
            ("set_", self.settings_callback),
            ("fp_", self.fp_callback),
            ("login_", self.login_callback),
            ("chart_", self.chart_callback),
        ):
            if data.startswith(prefix):
                await handler(call)
                return

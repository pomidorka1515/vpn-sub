"""Public text-message routing and button handlers."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from telebot import types

from ..composition import PublicFeatureMixin

__all__ = ["PublicTextRoutingMixin"]


class PublicTextRoutingMixin(PublicFeatureMixin):
    """Route localized reply-keyboard buttons to focused handlers."""

    ROUTES: tuple[tuple[str, str, bool], ...] = (
        ("btn_info", "_handle_info", True),
        ("btn_bonus", "_handle_bonus", True),
        ("btn_lang", "_handle_language", False),
        ("btn_login", "_handle_login", False),
        ("btn_main_sub", "_handle_subscription_menu", True),
        ("btn_main_account", "_handle_account_menu", True),
        ("btn_main_back", "_handle_main_menu", True),
        ("btn_reset", "_handle_reset", True),
        ("btn_support", "_handle_support", False),
        ("btn_logout", "_handle_logout", True),
        ("btn_help", "_handle_help", True),
        ("btn_settings", "_handle_settings", True),
        ("btn_delete", "_handle_delete", True),
        ("btn_get_sub", "_handle_get_subscription", True),
        ("btn_chart", "_handle_chart", True),
    )

    def handle_text(self, message: types.Message) -> None:
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        text = message.text

        for button_name, handler_name, requires_reg in self.ROUTES:
            if text not in (
                self.TEXTS["ru"][button_name],
                self.TEXTS["en"][button_name],
            ):
                continue
            if requires_reg and not self.sub.telegram_svc.is_registered(uid):
                return
            handler = cast(
                Callable[[types.Message, int, str], None],
                getattr(self, handler_name),
            )
            handler(message, uid, lang)
            return

    def _handle_info(self, message: types.Message, uid: int, lang: str) -> None:
        self.send_info(message.chat.id, uid, lang)

    def _handle_bonus(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        msg = self.bot.send_message(
            message.chat.id,
            t["enter_bonus"],
            reply_markup=types.ReplyKeyboardRemove(),
        )
        self.bot.register_next_step_handler(msg, self.step_bonus)  # pyright: ignore[reportUnknownMemberType]

    def _handle_language(self, message: types.Message, uid: int, lang: str) -> None:
        del uid
        t = self.TEXTS[lang]
        markup = types.InlineKeyboardMarkup()
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
            types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
        )
        self.bot.send_message(message.chat.id, t["choose_lang"], reply_markup=markup)

    def _handle_login(self, message: types.Message, uid: int, lang: str) -> None:
        if self.sub.telegram_svc.is_registered(uid):
            return
        t = self.TEXTS[lang]
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton(
                t["btn_login_credentials"],
                callback_data="login_credentials",
            )
        )
        self.bot.send_message(message.chat.id, t["choose_login"], reply_markup=markup)

    def _handle_subscription_menu(
        self, message: types.Message, uid: int, lang: str
    ) -> None:
        t = self.TEXTS[lang]
        reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        reply_markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.KeyboardButton(t["btn_main_back"]),
            types.KeyboardButton(t["btn_info"]),
            types.KeyboardButton(t["btn_get_sub"]),
            types.KeyboardButton(t["btn_bonus"]),
            types.KeyboardButton(t["btn_reset"]),
            types.KeyboardButton(t["btn_chart"]),
            types.KeyboardButton(t["btn_lang"]),
            types.KeyboardButton(t["btn_support"]),
        )
        self.bot.send_message(
            message.chat.id, t["welcome_reg"], reply_markup=reply_markup
        )

    def _handle_account_menu(
        self, message: types.Message, uid: int, lang: str
    ) -> None:
        t = self.TEXTS[lang]
        reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        reply_markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.KeyboardButton(t["btn_main_back"]),
            types.KeyboardButton(t["btn_settings"]),
            types.KeyboardButton(t["btn_logout"]),
            types.KeyboardButton(t["btn_help"]),
            types.KeyboardButton(t["btn_delete"]),
            types.KeyboardButton(t["btn_lang"]),
            types.KeyboardButton(t["btn_support"]),
        )
        self.bot.send_message(
            message.chat.id, t["welcome_reg"], reply_markup=reply_markup
        )

    def _handle_main_menu(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        self.bot.send_message(
            message.chat.id, t["welcome_reg"], reply_markup=self.get_menu(uid)
        )

    def _handle_reset(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        msg = self.bot.send_message(
            message.chat.id,
            t["confirm_reset"],
            reply_markup=types.ReplyKeyboardRemove(),
        )
        self.bot.register_next_step_handler(msg, self.step_reset)  # pyright: ignore[reportUnknownMemberType]

    def _handle_support(self, message: types.Message, uid: int, lang: str) -> None:
        del uid
        t = self.TEXTS[lang]
        self.bot.send_message(message.chat.id, t["support_text"], parse_mode="HTML")

    def _handle_logout(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        self.sub.telegram_svc.set_telegram_user(uid, None)
        self.bot.send_message(
            message.chat.id, t["logout_success"], reply_markup=self.get_menu(uid)
        )

    def _handle_help(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        descriptions = ""
        for profile, desc in self.cfg["profileDescriptions"].items():
            index = 0 if lang == "en" else 1
            profile_name = self.cfg["profiles"][profile][index]
            profile_desc = desc[index]
            descriptions += f"<code>{profile_name}</code> — {profile_desc}\n"
        self.bot.send_message(
            message.chat.id,
            t["help_text"].format(text=descriptions),
            parse_mode="HTML",
        )

    def _handle_settings(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton(t["name_label"], callback_data="set_name"),
            types.InlineKeyboardButton(t["fp_label"], callback_data="set_fp"),
            types.InlineKeyboardButton(t["login_label"], callback_data="set_login"),
            types.InlineKeyboardButton(t["pass_label"], callback_data="set_pass"),
        )
        self.bot.send_message(
            message.chat.id,
            t["settings_menu"],
            parse_mode="HTML",
            reply_markup=markup,
        )

    def _handle_delete(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        msg = self.bot.send_message(
            message.chat.id,
            t["confirm_delete"],
            parse_mode="HTML",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        self.bot.register_next_step_handler(msg, self.step_delete)  # pyright: ignore[reportUnknownMemberType]

    def _handle_get_subscription(
        self, message: types.Message, uid: int, lang: str
    ) -> None:
        self.send_link(message.chat.id, uid, lang)

    def _handle_chart(self, message: types.Message, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons: list[types.InlineKeyboardButton] = []
        for days in (3, 14, 30, 90):
            buttons.append(
                types.InlineKeyboardButton(
                    t["btn_chart_days"].format(days=days),
                    callback_data=f"chart_{days}",
                )
            )
        markup.add(*buttons)  # pyright: ignore[reportUnknownMemberType]
        self.bot.send_message(
            message.chat.id, t["choose_chart_days"], reply_markup=markup
        )

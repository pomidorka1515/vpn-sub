"""Public account lifecycle workflows."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

from typing import cast

from telebot import types

from errors import AppError
__all__ = ["PublicAccountMixin"]


class PublicAccountMixin(PublicFeatureMixin):
    """Reset, deletion, logout, and bonus workflows."""

    def step_delete(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        confirm = t['delete_confirm_input']
        if text.strip().lower() != confirm.lower():
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return

        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str):
            self.bot.send_message(message.chat.id, "❌ Error", reply_markup=self.get_menu(uid))
            return
        try:
            self.sub.delete_user(username=username, perma=True)
            self.bot.send_message(message.chat.id, t['delete_success'], reply_markup=self.get_menu(uid))

        except AppError as error:
            self._send_message(message.chat.id, error.message, reply_markup=self.get_menu(uid))
            return
        except Exception as error:
            self.log.error(f"Delete error for uid {uid}: {error}", exc_info=True)
            self._send_message(message.chat.id, "⚠️ Error", reply_markup=self.get_menu(uid))

    def step_reset(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        confirm = t['reset_confirm_input'] # string they need to say
        userinput = text.strip().lower()
        if userinput == confirm.lower():
            try:
                username = self.sub.get_username_telegram(uid)
                if not isinstance(username, str):
                    return
                self.sub.reset_user(username)
                self.bot.send_message(message.chat.id, t['reset_success'], reply_markup=self.get_menu(uid))
            except AppError as error:
                self._send_message(message.chat.id, error.message, reply_markup=self.get_menu(uid))
            except Exception:
                self.log.critical("reset_user failed", exc_info=True)
                self._send_message(message.chat.id, t.get("error_generic", "⚠️ Error"), reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return

    def step_bonus(self, message: types.Message) -> None:
        text = cast(str, message.text)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        if text.startswith('/'):
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return

        code = text.strip()
        try:
            self.sub.bonus_code(value=uid, code=code)
            self.bot.send_message(message.chat.id, t['bonus_success'], reply_markup=self.get_menu(uid))
            self.send_info(message.chat.id, uid, lang)
        except AppError:
            self._send_message(message.chat.id, t['invalid_code'], reply_markup=self.get_menu(uid))
        except Exception:
            self.log.error(f"Bonus error for uid {uid}", exc_info=True)
            self._send_message(message.chat.id, "⚠️ Error occurred", reply_markup=self.get_menu(uid))



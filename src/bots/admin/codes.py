"""Administrator invitation-code workflows."""
from __future__ import annotations

from ..composition import AdminFeatureMixin

from typing import Literal, cast

from telebot import types

from errors import AppError
__all__ = ["AdminCodesMixin"]


class AdminCodesMixin(AdminFeatureMixin):
    """Invitation-code workflows."""

    def _cb_list_codes(self, chat_id: int) -> None:
        try:
            codes = self.sub.code_svc.list_code()
            if not codes:
                self._send_message(chat_id, "Список кодов пуст.", reply_markup=self.get_codes_menu())
                return
            text = "🎟 <b>Список кодов:</b>\n\n" + "\n".join([f"- <code>{c}</code>" for c in codes])
            self._send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_codes_menu())
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(chat_id, "❌ Внутренняя ошибка", reply_markup=self.get_codes_menu())

    def _step_info_code(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        code = text.strip()
        try:
            info = self.sub.code_svc.get_code(code)
            text = (
                f"ℹ️ <b>Код: <code>{code}</code></b>\n\n"
                f"Тип: <code>{info.action}</code>\n"
                f"Перманентный: <b>{"Да" if info.perma else "Нет"}</b>\n"
                f"Использований: <code>{info.uses}</code>\n"
                f"Дней: <code>{info.days}</code>\n"
                f"Гигабайт: <code>{info.gb}</code>\n"
                f"ВЛ Гигабайт: <code>{info.wl_gb}</code>\n"
            )
            self._send_message(message.chat.id, text, parse_mode="HTML", reply_markup=self.get_codes_menu())
        except AppError as error:
            self._send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_codes_menu())
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(message.chat.id, "❌ Внутренняя ошибка", reply_markup=self.get_codes_menu())

    def _step_del_code(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        code = text.strip()
        try:
            self.sub.code_svc.delete_code(code)
        except AppError as error:
            self._send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_codes_menu())
            return
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(message.chat.id, "❌ Внутренняя ошибка", reply_markup=self.get_codes_menu())
            return
        self._send_message(message.chat.id, f"✅ Код <code>{code}</code> удалён.", parse_mode="HTML", reply_markup=self.get_codes_menu())

    def _step_add_code_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        code_name = text.strip()
        self._pending_codes[message.chat.id] = code_name
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("📝 register", callback_data="codetype_register"),
            types.InlineKeyboardButton("🎁 bonus", callback_data="codetype_bonus"),
            types.InlineKeyboardButton("🔙 Отмена", callback_data="codes_menu")
        )
        self.bot.send_message(message.chat.id, f"Код: <b>{code_name}</b>\nВыберите тип:", parse_mode="HTML", reply_markup=markup)

    def _step_add_code_days(self, message: types.Message, code_type: str, code_name: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            days = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_codes_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Введите количество гигабайтов (в гб, или 0 для безлимита):")
        self.bot.register_next_step_handler(msg, self._step_add_code_time, code_type, code_name, days)  # pyright: ignore[reportUnknownMemberType]

    def _step_add_code_time(self, message: types.Message, code_type: str, code_name: str, days: int) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            gb = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_codes_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Введите количество гигабайтов для ВЛ локаций (в гб, или 0 для безлимита)")
        self.bot.register_next_step_handler(msg, self._step_add_code_wl_time, code_type, code_name, days, gb)  # pyright: ignore[reportUnknownMemberType]

    def _step_add_code_wl_time(
        self,
        message: types.Message,
        code_type: str,
        code_name: str,
        days: int,
        gb: int
    ) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            wl_gb = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_codes_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Перманентный код? Да/Нет:")
        self.bot.register_next_step_handler(msg, self._step_add_code_perma, code_type, code_name, days, gb, wl_gb)  # pyright: ignore[reportUnknownMemberType]

    def _step_add_code_perma(
        self,
        message: types.Message,
        code_type: str,
        code_name: str,
        days: int,
        gb: int,
        wl_gb: int
    ) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        content = text.strip().lower()

        if content == "да":
            perma = True
        elif content == "нет":
            perma = False
        else:
            self.bot.send_message(message.chat.id, "❌ Неизвестное значение (только да/нет)", reply_markup=self.get_codes_menu())
            return

        if not perma:
            msg = self.bot.send_message(message.chat.id, "Кол-во использований? (>= 1)")
            self.bot.register_next_step_handler(msg, self._step_add_code_uses, code_type, code_name, days, gb, wl_gb, perma)  # pyright: ignore[reportUnknownMemberType]
        else:
            self._step_add_code_uses(
                message=message.chat.id,
                code_type=code_type,
                code_name=code_name,
                days=days,
                gb=gb,
                wl_gb=wl_gb,
                perma=perma
            )

    def _step_add_code_uses(
        self,
        message: types.Message | int,
        code_type: str,
        code_name: str,
        days: int,
        gb: int,
        wl_gb: int,
        perma: Literal[True, False]
    ) -> None:
        """
        flow:
        message = int -> perma = True, generate code directly without uses
        message = actual message -> perma = False, msg text has uses param
        """
        if isinstance(message, types.Message):
            text = cast(str, message.text)
            if text.startswith('/'): return
            try:
                uses = int(text.strip())
                if uses < 1:
                    raise ValueError("aeruioycxvvbseyporfdlhgnvcxhskl;dlkfdlaeeyroufhgkjhgfkgkfjhl")
            except ValueError:
                self.bot.send_message(message.chat.id, "❌ Ошибка: кол-во должно быть числом больше 0.")
                return
            chat_id = message.chat.id
        else:
            uses = -1
            chat_id = message

        try:
            self.sub.code_svc.add_code(
                code=code_name, action=code_type, permanent=perma,
                days=days, gb=gb, wl_gb=wl_gb, uses=uses
            )
            self._send_message(
                chat_id,
                f"""✅ Код создан!

Код: <code>{code_name}</code>
Тип: <code>{code_type}</code>
Перманентный: <b>{"Да" if perma else "Нет"}</b>
Дней: <code>{days}</code>
Кол-во использований: <code>{uses}</code>
Гб: <code>{gb}</code>
ВЛ Гб: <code>{wl_gb}</code>""",
                parse_mode="HTML",
                reply_markup=self.get_codes_menu()
            )
        except AppError as error:
            self._send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_codes_menu())
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(chat_id, "❌ Внутренняя ошибка", reply_markup=self.get_codes_menu())



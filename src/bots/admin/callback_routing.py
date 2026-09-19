"""Administrator callback-query routing and action dispatch."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from telebot import types

from errors import AppError

from ..composition import AdminFeatureMixin

__all__ = ["AdminCallbackRoutingMixin"]


class AdminCallbackRoutingMixin(AdminFeatureMixin):
    """Dispatch administrator callback data to focused handlers."""

    ROUTES: tuple[tuple[str, str, bool], ...] = (
        ("cancel", "_handle_cancel", False),
        ("noop", "_handle_noop", False),
        ("online_users", "_handle_online_users", False),
        ("list_users", "_handle_list_users", False),
        ("page_list_users_", "_handle_list_users_page", True),
        ("refresh_all", "_handle_refresh", False),
        ("reset_user", "_handle_reset_user", False),
        ("add_user", "_handle_add_user", False),
        ("info_user", "_handle_info_menu", False),
        ("page_info_", "_handle_info_page", True),
        ("info_code", "_handle_info_code", False),
        ("info_", "_handle_info_user", True),
        ("action_del", "_handle_delete_menu", False),
        ("page_dodel_", "_handle_delete_page", True),
        ("dodel_", "_handle_delete_user", True),
        ("codes_menu", "_handle_codes_menu", False),
        ("list_codes", "_handle_list_codes", False),
        ("del_code", "_handle_delete_code", False),
        ("add_code", "_handle_add_code", False),
        ("status_panels", "_handle_panel_status", False),
        ("codetype_", "_handle_code_type", True),
        ("chart", "_handle_chart", False),
        ("leaderboard", "_handle_leaderboard", False),
        ("edit_user_", "_handle_edit_user", True),
        ("edit_", "_handle_edit", True),
        ("fp_save_", "_handle_fingerprint_save", True),
        ("lbt_", "_handle_leaderboard_type", True),
        ("lbo_", "_handle_leaderboard_order", True),
    )

    def handle_callbacks(self, call: types.CallbackQuery) -> None:
        if not self.is_admin(call.from_user.id):
            return

        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        chat_id = message.chat.id
        self.bot.answer_callback_query(call.id)

        try:
            for route, handler_name, is_prefix in self.ROUTES:
                if (data.startswith(route) if is_prefix else data == route):
                    handler = cast(
                        Callable[[str, int, types.Message], None],
                        getattr(self, handler_name),
                    )
                    handler(data, chat_id, message)
                    return
        except Exception as error:
            self.log.error(f"Ошибка в боте: {error}", exc_info=True)
            self._send_message(chat_id, "⚠️ Внутренняя ошибка")

    def _handle_cancel(self, data: str, chat_id: int, message: types.Message) -> None:
        del data
        self._pending_edits.pop(chat_id, None)
        self.bot.edit_message_text(
            "Действие отменено. Главное меню:", chat_id, message.message_id,
            reply_markup=self.get_main_menu(),
        )
        self.bot.clear_step_handler_by_chat_id(chat_id)

    def _handle_noop(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, chat_id, message

    def _handle_online_users(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        self._cb_online_users(chat_id)

    def _handle_list_users(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        self._cb_list_users(chat_id)

    def _handle_list_users_page(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        self._cb_list_users(chat_id, int(data.rsplit("_", 1)[1]))

    def _handle_refresh(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        self._cb_refresh(chat_id)

    def _handle_reset_user(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        msg = self.bot.send_message(chat_id, "Введите username пользователя:")
        self.bot.register_next_step_handler(msg, self._step_reset_user)  # pyright: ignore[reportUnknownMemberType]

    def _handle_add_user(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        msg = self.bot.send_message(chat_id, "Введите username нового пользователя (или /start для отмены):")
        self.bot.register_next_step_handler(msg, self._step_add_user_name)  # pyright: ignore[reportUnknownMemberType]

    def _handle_info_menu(self, data: str, chat_id: int, message: types.Message) -> None:
        del data
        if not self.sub.list_users():
            self.bot.send_message(chat_id, "Список пользователей пуст.", reply_markup=self.get_main_menu())
            return
        page = self._pagination_state.get(chat_id, {}).get("info_page", 0)
        self._pagination_state.setdefault(chat_id, {})["info_page"] = page
        self.bot.edit_message_text("👤 Выберите пользователя для просмотра информации:", chat_id, message.message_id, reply_markup=self.get_users_menu("info", page))

    def _handle_info_page(self, data: str, chat_id: int, message: types.Message) -> None:
        page = int(data.rsplit("_", 1)[1])
        self._pagination_state.setdefault(chat_id, {})["info_page"] = page
        self.bot.edit_message_text("👤 Выберите пользователя для просмотра информации:", chat_id, message.message_id, reply_markup=self.get_users_menu("info", page))

    def _handle_info_user(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        self._cb_info_user(chat_id, data.split("info_", 1)[1])

    def _handle_delete_menu(self, data: str, chat_id: int, message: types.Message) -> None:
        del data
        if not self.sub.list_users():
            self.bot.send_message(chat_id, "Список пуст.", reply_markup=self.get_main_menu())
            return
        page = self._pagination_state.get(chat_id, {}).get("del_page", 0)
        self.bot.edit_message_text("Выберите пользователя для УДАЛЕНИЯ ⚠️:", chat_id, message.message_id, reply_markup=self.get_users_menu("dodel", page))

    def _handle_delete_page(self, data: str, chat_id: int, message: types.Message) -> None:
        page = int(data.rsplit("_", 1)[1])
        self._pagination_state.setdefault(chat_id, {})["del_page"] = page
        self.bot.edit_message_text("Выберите пользователя для УДАЛЕНИЯ ⚠️:", chat_id, message.message_id, reply_markup=self.get_users_menu("dodel", page))

    def _handle_delete_user(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        self._cb_del_user(chat_id, data.split("dodel_", 1)[1])

    def _handle_codes_menu(self, data: str, chat_id: int, message: types.Message) -> None:
        del data
        self.bot.edit_message_text("🎟 Управление кодами:", chat_id, message.message_id, reply_markup=self.get_codes_menu())

    def _handle_list_codes(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        self._cb_list_codes(chat_id)

    def _handle_info_code(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        msg = self.bot.send_message(chat_id, "Введите код (или /start для отмены):")
        self.bot.register_next_step_handler(msg, self._step_info_code)  # pyright: ignore[reportUnknownMemberType]

    def _handle_delete_code(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        msg = self.bot.send_message(chat_id, "Введите код для удаления (или /start для отмены):")
        self.bot.register_next_step_handler(msg, self._step_del_code)  # pyright: ignore[reportUnknownMemberType]

    def _handle_add_code(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        msg = self.bot.send_message(chat_id, "Введите название кода (или /start для отмены):")
        self.bot.register_next_step_handler(msg, self._step_add_code_name)  # pyright: ignore[reportUnknownMemberType]

    def _handle_panel_status(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        self._cb_all_panels_status(chat_id)

    def _handle_code_type(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        code_name = self._pending_codes.pop(chat_id, None)
        if not code_name:
            self.bot.send_message(chat_id, "❌ Сессия истекла, начните заново.")
            return
        code_type = data.split("codetype_", 1)[1]
        msg = self.bot.send_message(chat_id, f"Тип: <b>{code_type}</b>\nВведите количество дней:", parse_mode="HTML")
        self.bot.register_next_step_handler(msg, self._step_add_code_days, code_type, code_name)  # pyright: ignore[reportUnknownMemberType]

    def _handle_chart(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        msg = self.bot.send_message(chat_id, "Введите username пользователя (или /start для отмены):")
        self.bot.register_next_step_handler(msg, self._step_chart_username)  # pyright: ignore[reportUnknownMemberType]

    def _handle_leaderboard(self, data: str, chat_id: int, message: types.Message) -> None:
        del data, message
        self._cb_leaderboard_type(chat_id)

    def _handle_edit_user(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        self._cb_edit_user_options(chat_id, data.split("edit_user_", 1)[1])

    def _handle_edit(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        pending = self._pending_edits.get(chat_id)
        if not pending:
            self.bot.send_message(chat_id, "❌ Сессия истекла, начните с /info", reply_markup=self.get_main_menu())
            return
        handlers = {
            "fp": self._cb_edit_fingerprint, "limit": self._cb_edit_limit,
            "wl_limit": self._cb_edit_wl_limit, "time": self._cb_edit_time,
            "name": self._cb_edit_name,
        }
        handler = handlers.get(data.split("_", 1)[1])
        if handler is not None:
            handler(chat_id, pending["username"])

    def _handle_fingerprint_save(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        pending = self._pending_edits.get(chat_id)
        if not pending:
            self.bot.send_message(chat_id, "❌ Сессия истекла, начните с /info", reply_markup=self.get_main_menu())
            return
        fp = data.split("fp_save_", 1)[1]
        try:
            self.sub.update_params(username=pending["username"], fingerprint=fp)
        except AppError as error:
            self._send_message(chat_id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
        else:
            self._send_message(chat_id, f"✅ fingerprint обновлён: <code>{fp}</code>", parse_mode="HTML", reply_markup=self.get_main_menu())
        self._pending_edits.pop(chat_id, None)

    def _handle_leaderboard_type(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        self._pending_leaderboard[chat_id] = {"type": data.split("lbt_", 1)[1]}
        self._cb_leaderboard_order(chat_id)

    def _handle_leaderboard_order(self, data: str, chat_id: int, message: types.Message) -> None:
        del message
        if chat_id in self._pending_leaderboard:
            self._pending_leaderboard[chat_id]["order"] = data.split("lbo_", 1)[1]
        self._cb_leaderboard_window(chat_id)

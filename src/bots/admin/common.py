"""Administrator authentication, menus, routing, and lifecycle glue."""
from __future__ import annotations

from ..composition import AdminFeatureMixin

from telebot import types

from errors import AppError
from typing import cast

from ..common import AdminStateMixin, TelegramIOMixin
from ..polling import TelegramPollingMixin
__all__ = ["AdminCommonMixin"]


class AdminCommonMixin(TelegramPollingMixin, TelegramIOMixin, AdminStateMixin, AdminFeatureMixin):
    USERS_PER_PAGE = 10

    """Authentication, menus, callback routing, and shared lifecycle glue."""

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_uids

    def msg(self, text: str, parse_mode: str = "HTML") -> None:
        for uid in self.admin_uids:
            try:
                self.bot.send_message(uid, text, parse_mode=parse_mode)
            except Exception as e:
                self.log.error(f"failed to send message to admin ID {uid}: {e}")

    def get_main_menu(self) -> types.InlineKeyboardMarkup:
        return types.InlineKeyboardMarkup(row_width=2).add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("👥 Список юзеров", callback_data="list_users"),
            types.InlineKeyboardButton("ℹ️ Инфо о юзере", callback_data="info_user"),
            types.InlineKeyboardButton("➕ Добавить", callback_data="add_user"),
            types.InlineKeyboardButton("❌ Удалить", callback_data="action_del"),
            types.InlineKeyboardButton("🔄 Обновить всех", callback_data="refresh_all"),
            types.InlineKeyboardButton("🎟 Коды", callback_data="codes_menu"),
            types.InlineKeyboardButton("🟢 Пользователи онлайн", callback_data="online_users"),
            types.InlineKeyboardButton("⚠ Сбросить пользователя", callback_data="reset_user"),
            types.InlineKeyboardButton("📈 История трафика", callback_data="chart"),
            types.InlineKeyboardButton("🏆 Таблица лидеров", callback_data="leaderboard"),
            types.InlineKeyboardButton("ℹ️ Статус панелей", callback_data="status_panels")
        )

    def get_codes_menu(self) -> types.InlineKeyboardMarkup:
        return types.InlineKeyboardMarkup(row_width=2).add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("➕ Добавить код", callback_data="add_code"),
            types.InlineKeyboardButton("❌ Удалить код", callback_data="del_code"),
            types.InlineKeyboardButton("📋 Список кодов", callback_data="list_codes"),
            types.InlineKeyboardButton("ℹ️ Инфо о коде", callback_data="info_code"),
            types.InlineKeyboardButton("🔙 В меню", callback_data="cancel")
        )

    def get_users_menu(self, prefix: str, page: int = 0) -> types.InlineKeyboardMarkup:
        """Get paginated user list with navigation buttons."""
        all_users = self.sub.list_users()
        total_users = len(all_users)
        total_pages = max(1, (total_users - 1) // self.USERS_PER_PAGE + 1)

        page = max(0, min(page, total_pages - 1))
        start_idx = page * self.USERS_PER_PAGE
        end_idx = min(start_idx + self.USERS_PER_PAGE, total_users)
        page_users = all_users[start_idx:end_idx]

        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons: list[types.InlineKeyboardButton] = []
        for user in page_users:
            buttons.append(types.InlineKeyboardButton(user, callback_data=f"{prefix}_{user}"))
        markup.add(*buttons)  # pyright: ignore[reportUnknownMemberType]

        nav_buttons: list[types.InlineKeyboardButton] = []
        if page > 0:
            nav_buttons.append(types.InlineKeyboardButton("◀️", callback_data=f"page_{prefix}_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(types.InlineKeyboardButton("▶️", callback_data=f"page_{prefix}_{page + 1}"))

        if nav_buttons:
            markup.add(*nav_buttons)  # pyright: ignore[reportUnknownMemberType]
            markup.add(types.InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))  # pyright: ignore[reportUnknownMemberType]

        markup.add(types.InlineKeyboardButton("🔙 Отмена / В меню", callback_data="cancel"))  # pyright: ignore[reportUnknownMemberType]
        return markup

    def cmd_start(self, message: types.Message) -> None:
        if not self.is_admin(cast(types.User, message.from_user).id):
            return
        self.bot.send_message(
            message.chat.id,
            "👋 Привет! Панель управления VPN запущена.",
            reply_markup=self.get_main_menu()
        )

    def handle_callbacks(self, call: types.CallbackQuery) -> None:
        if not self.is_admin(call.from_user.id):
            return

        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        chat_id = message.chat.id
        self.bot.answer_callback_query(call.id)


        try:
            if data == "cancel":
                self._pending_edits.pop(chat_id, None)
                self.bot.edit_message_text("Действие отменено. Главное меню:", chat_id, message.message_id, reply_markup=self.get_main_menu())
                self.bot.clear_step_handler_by_chat_id(chat_id)

            elif data == "noop":
                pass  # page indicator button

            elif data == "online_users":
                self._cb_online_users(chat_id)

            elif data == "list_users":
                self._cb_list_users(chat_id)

            elif data.startswith("page_list_users_"):
                page = int(data.split("_")[-1])
                self._cb_list_users(chat_id, page)

            elif data == "refresh_all":
                self._cb_refresh(chat_id)

            elif data == "reset_user":
                msg = self.bot.send_message(chat_id, "Введите username пользователя:")
                self.bot.register_next_step_handler(msg, self._step_reset_user)  # pyright: ignore[reportUnknownMemberType]

            elif data == "add_user":
                msg = self.bot.send_message(chat_id, "Введите username нового пользователя (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_add_user_name)  # pyright: ignore[reportUnknownMemberType]

            elif data == "info_user":
                if not self.sub.list_users():
                    self.bot.send_message(chat_id, "Список пользователей пуст.", reply_markup=self.get_main_menu())
                    return
                page = self._pagination_state.get(chat_id, {}).get('info_page', 0)
                self._pagination_state.setdefault(chat_id, {})['info_page'] = page
                self.bot.edit_message_text("👤 Выберите пользователя для просмотра информации:", chat_id, message.message_id, reply_markup=self.get_users_menu("info", page))

            elif data.startswith("page_info_"):
                page = int(data.split("_")[-1])
                self._pagination_state.setdefault(chat_id, {})['info_page'] = page
                self.bot.edit_message_text("👤 Выберите пользователя для просмотра информации:", chat_id, message.message_id, reply_markup=self.get_users_menu("info", page))

            elif data.startswith("info_") and not data.startswith("info_code"):
                username = data.split("info_", 1)[1]
                self._cb_info_user(chat_id, username)

            elif data == "action_del":
                if not self.sub.list_users():
                    self.bot.send_message(chat_id, "Список пуст.", reply_markup=self.get_main_menu())
                    return
                page = self._pagination_state.get(chat_id, {}).get('del_page', 0)
                self.bot.edit_message_text("Выберите пользователя для УДАЛЕНИЯ ⚠️:", chat_id, message.message_id, reply_markup=self.get_users_menu("dodel", page))

            elif data.startswith("page_dodel_"):
                page = int(data.split("_")[-1])
                self._pagination_state.setdefault(chat_id, {})['del_page'] = page
                self.bot.edit_message_text("Выберите пользователя для УДАЛЕНИЯ ⚠️:", chat_id, message.message_id, reply_markup=self.get_users_menu("dodel", page))

            elif data.startswith("dodel_"):
                username = data.split("dodel_", 1)[1]
                self._cb_del_user(chat_id, username)

            elif data == "codes_menu":
                self.bot.edit_message_text("🎟 Управление кодами:", chat_id, message.message_id, reply_markup=self.get_codes_menu())

            elif data == "list_codes":
                self._cb_list_codes(chat_id)

            elif data == "info_code":
                msg = self.bot.send_message(chat_id, "Введите код (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_info_code)  # pyright: ignore[reportUnknownMemberType]

            elif data == "del_code":
                msg = self.bot.send_message(chat_id, "Введите код для удаления (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_del_code)  # pyright: ignore[reportUnknownMemberType]

            elif data == "add_code":
                msg = self.bot.send_message(chat_id, "Введите название кода (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_add_code_name)  # pyright: ignore[reportUnknownMemberType]

            elif data == "status_panels":
                self._cb_all_panels_status(chat_id)
            elif data.startswith("codetype_"):
                code_type = data.split("codetype_", 1)[1]
                code_name = self._pending_codes.pop(chat_id, None)
                if not code_name:
                    self.bot.send_message(chat_id, "❌ Сессия истекла, начните заново.")
                    return
                msg = self.bot.send_message(chat_id, f"Тип: <b>{code_type}</b>\nВведите количество дней:", parse_mode="HTML")
                self.bot.register_next_step_handler(msg, self._step_add_code_days, code_type, code_name)  # pyright: ignore[reportUnknownMemberType]

            elif data == "chart":
                msg = self.bot.send_message(chat_id, "Введите username пользователя (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_chart_username)  # pyright: ignore[reportUnknownMemberType]

            elif data == "leaderboard":
                self._cb_leaderboard_type(chat_id)

            elif data.startswith("edit_user_"):
                username = data.split("edit_user_", 1)[1]
                self._cb_edit_user_options(chat_id, username)

            elif data.startswith("edit_"):
                # edit_<action> without username — pulled from _pending_edits
                action = data.split("_", 1)[1]
                pending = self._pending_edits.get(chat_id)
                if not pending:
                    self.bot.send_message(chat_id, "❌ Сессия истекла, начните с /info", reply_markup=self.get_main_menu())
                    return
                if action == "fp":
                    self._cb_edit_fingerprint(chat_id, pending['username'])
                elif action == "limit":
                    self._cb_edit_limit(chat_id, pending['username'])
                elif action == "wl_limit":
                    self._cb_edit_wl_limit(chat_id, pending['username'])
                elif action == "time":
                    self._cb_edit_time(chat_id, pending['username'])
                elif action == "name":
                    self._cb_edit_name(chat_id, pending['username'])

            elif data.startswith("fp_save_"):
                # format: fp_save_<fingerprint> — username from _pending_edits
                fp = data.split("fp_save_", 1)[1]
                pending = self._pending_edits.get(chat_id)
                if not pending:
                    self.bot.send_message(chat_id, "❌ Сессия истекла, начните с /info", reply_markup=self.get_main_menu())
                    return
                username = pending['username']
                try:
                    self.sub.update_params(username=username, fingerprint=fp)
                except AppError as error:
                    self._send_message(chat_id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
                else:
                    self._send_message(chat_id, f"✅ fingerprint обновлён: <code>{fp}</code>", parse_mode="HTML", reply_markup=self.get_main_menu())
                self._pending_edits.pop(chat_id, None)

            elif data.startswith("lbt_"):
                # leaderboard type selection: lbt_total | lbt_monthly | lbt_wl_monthly
                bw_type = data.split("lbt_", 1)[1]
                self._pending_leaderboard[chat_id] = {'type': bw_type}
                self._cb_leaderboard_order(chat_id)

            elif data.startswith("lbo_"):
                # leaderboard order selection: lbo_asc | lbo_desc
                order = data.split("lbo_", 1)[1]
                if chat_id in self._pending_leaderboard:
                    self._pending_leaderboard[chat_id]['order'] = order
                self._cb_leaderboard_window(chat_id)

        except Exception as e:
            self.log.error(f"Ошибка в боте: {e}", exc_info=True)
            self._send_message(chat_id, "⚠️ Внутренняя ошибка")

    def start(self) -> None:
        self.start_polling()

    def stop(self) -> None:
        self.stop_polling()



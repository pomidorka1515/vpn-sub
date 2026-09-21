"""Administrator authentication, menus, and lifecycle glue."""
from __future__ import annotations

from telebot import types

from typing import cast

from ..common import AdminStateMixin, TelegramIOMixin
from ..composition import AdminFeatureMixin
from ..polling import TelegramPollingMixin
from .callback_routing import AdminCallbackRoutingMixin

__all__ = ["AdminCommonMixin"]


class AdminCommonMixin(
    TelegramPollingMixin,
    TelegramIOMixin,
    AdminStateMixin,
    AdminCallbackRoutingMixin,
    AdminFeatureMixin,
):
    USERS_PER_PAGE = 10

    """Authentication, menus, and shared lifecycle glue."""

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
        all_users = self.sub.user_svc.list_users()
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

    def start(self) -> None:
        self.start_polling()

    def stop(self) -> None:
        self.stop_polling()


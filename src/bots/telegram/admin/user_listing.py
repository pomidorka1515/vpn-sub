"""Administrator user listing and refresh workflows."""

from __future__ import annotations

from telebot import types

from bots.list_messages import list_messages
from errors import PanelUnavailableError

from ..composition import AdminFeatureMixin

__all__ = ["AdminUserListingMixin"]


class AdminUserListingMixin(AdminFeatureMixin):
    """User listing and panel refresh workflows."""

    async def _cb_list_users(self, chat_id: int, page: int = 0) -> None:
        all_users = await self.sub.user_svc.list_users()
        total_users = len(all_users)

        if not all_users:
            await self.bot.send_message(
                chat_id,
                "Список пользователей пуст.",
                reply_markup=self.get_main_menu(),
            )
            return

        total_pages = max(1, (total_users - 1) // self.USERS_PER_PAGE + 1)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * self.USERS_PER_PAGE
        end_idx = min(start_idx + self.USERS_PER_PAGE, total_users)
        page_users = all_users[start_idx:end_idx]

        header = f"👥 <b>Список пользователей</b> ({total_users} всего)\n\n"

        markup = types.InlineKeyboardMarkup(row_width=2)
        nav_buttons: list[types.InlineKeyboardButton] = []
        if page > 0:
            nav_buttons.append(
                types.InlineKeyboardButton("◀️", callback_data="page_list_users_" + str(page - 1)),
            )
        if page < total_pages - 1:
            nav_buttons.append(
                types.InlineKeyboardButton("▶️", callback_data="page_list_users_" + str(page + 1)),
            )
        if nav_buttons:
            markup.add(*nav_buttons)  # pyright: ignore[reportUnknownMemberType]
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"),
            )

        markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="cancel"))  # pyright: ignore[reportUnknownMemberType]
        for text in list_messages(
            header, ((username, None) for username in page_users), limit=4096, html=True
        ):
            await self._send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

    async def _cb_online_users(self, chat_id: int) -> None:
        online_users = await self.sub.panel_svc.get_online_users(new=True)
        if not isinstance(online_users, dict):
            online_users = dict.fromkeys(online_users)
        if not online_users:
            await self._send_message(
                chat_id,
                "Нет пользователей в сети.",
                reply_markup=self.get_main_menu(),
            )
            return
        for text in list_messages(
            "👥 <b>Список пользователей онлайн:</b>\n\n",
            online_users.items(),
            limit=4096,
            html=True,
        ):
            await self._send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=self.get_main_menu(),
            )

    async def _cb_refresh(self, chat_id: int) -> None:
        failures: list[str] = []
        successes = 0
        users = await self.sub.user_svc.list_users()
        for index, cc in enumerate(users):
            try:
                await self.sub.business_svc.add_users(cc)
            except PanelUnavailableError:
                failures.append(cc)
                self.log.exception("user refresh failed for %s", cc)
            except Exception:
                self.log.critical("bulk user refresh aborted for %s", cc, exc_info=True)
                await self._send_message(
                    chat_id,
                    f"❌ Обновление прервано. Успешно: {successes}; "
                    f"панель недоступна: {len(failures)}; другая ошибка: 1; "
                    f"не обработано: {len(users) - index - 1}; всего: {len(users)}.",
                    reply_markup=self.get_main_menu(),
                )
                return
            else:
                successes += 1
        if failures:
            await self._send_message(
                chat_id,
                "⚠️ Обновление завершено с ошибками: " + ", ".join(failures),
                reply_markup=self.get_main_menu(),
            )
        else:
            await self._send_message(
                chat_id,
                "✅ Все пользователи успешно обновлены.",
                reply_markup=self.get_main_menu(),
            )

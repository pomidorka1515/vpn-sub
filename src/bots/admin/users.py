"""Administrator user management workflows."""
from __future__ import annotations

from ..composition import AdminFeatureMixin

import time
from datetime import datetime, timezone
from typing import cast

from telebot import types

from errors import AppError, PanelUnavailableError
__all__ = ["AdminUsersMixin"]


class AdminUsersMixin(AdminFeatureMixin):
    """User listing, editing, and account workflows."""

    def _cb_list_users(self, chat_id: int, page: int = 0) -> None:
        all_users = self.sub.list_users()
        total_users = len(all_users)

        if not all_users:
            self.bot.send_message(chat_id, "Список пользователей пуст.", reply_markup=self.get_main_menu())
            return

        total_pages = max(1, (total_users - 1) // self.USERS_PER_PAGE + 1)
        page = max(0, min(page, total_pages - 1))
        start_idx = page * self.USERS_PER_PAGE
        end_idx = min(start_idx + self.USERS_PER_PAGE, total_users)
        page_users = all_users[start_idx:end_idx]

        text = f"👥 <b>Список пользователей</b> ({total_users} всего)\n\n"
        text += "\n".join([f"- <code>{u}</code>" for u in page_users])

        markup = types.InlineKeyboardMarkup(row_width=2)
        nav_buttons: list[types.InlineKeyboardButton] = []
        if page > 0:
            nav_buttons.append(types.InlineKeyboardButton("◀️", callback_data="page_list_users_" + str(page - 1)))
        if page < total_pages - 1:
            nav_buttons.append(types.InlineKeyboardButton("▶️", callback_data="page_list_users_" + str(page + 1)))
        if nav_buttons:
            markup.add(*nav_buttons)  # pyright: ignore[reportUnknownMemberType]
            markup.add(types.InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="noop"))  # pyright: ignore[reportUnknownMemberType]

        markup.add(types.InlineKeyboardButton("🔙 В меню", callback_data="cancel"))  # pyright: ignore[reportUnknownMemberType]
        self._send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

    def _cb_online_users(self, chat_id: int) -> None:
        online_users = self.sub.get_online_users(new=True)
        if not online_users:
            self._send_message(chat_id, "Нет пользователей в сети.", reply_markup=self.get_main_menu())
            return
        text = "👥 <b>Список пользователей онлайн:</b>\n\n" + "\n".join([f"- <code>{u}</code>{", логин: "+v if v else ""}" for u, v in online_users.items()])
        self._send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_main_menu())

    def _cb_refresh(self, chat_id: int) -> None:
        failures: list[str] = []
        users = self.sub.list_users()
        for cc in users:
            try:
                self.sub.add_users(cc)
            except PanelUnavailableError:
                failures.append(cc)
                self.log.error("user refresh failed for %s", cc, exc_info=True)
            except Exception:
                self.log.critical("bulk user refresh aborted for %s", cc, exc_info=True)
                self._send_message(
                    chat_id,
                    f"❌ Обновление прервано. Успешно: {len(users) - len(failures)}; "
                    f"панель недоступна: {len(failures)}; всего: {len(users)}.",
                    reply_markup=self.get_main_menu(),
                )
                return
        if failures:
            self._send_message(
                chat_id,
                "⚠️ Обновление завершено с ошибками: " + ", ".join(failures),
                reply_markup=self.get_main_menu(),
            )
        else:
            self._send_message(chat_id, "✅ Все пользователи успешно обновлены.", reply_markup=self.get_main_menu())

    def _cb_info_user(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=True)

            bw = info.bandwidth
            up = bw.total.upload
            down = bw.total.download
            wl_up = bw.wl_total.upload
            wl_down = bw.wl_total.download
            monthly = bw.monthly
            wl_monthly = bw.wl_monthly
            limit = bw.limit
            wl_limit = bw.wl_limit
            times = info.time
            token = info.token
            displayname = info.displayname
            status = "🟢 Включен" if info.enabled else "🔴 Отключен"
            wl_status = "🟢 Включен" if info.wl_enabled else "🔴 Отключен"
            online = "🟢 Да" if info.online else "🔴 Нет"
            fingerprint = info.fingerprint
            domain: str = self.cfg['domain']
            if times:
                days_left = str((times - int(time.time())) // 86400)
                date = datetime.fromtimestamp(times, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)")
            else:
                days_left = "N/A"
                date = "N/A"
            text = (
                f"ℹ️ <b>Информация о <code>{username}</code></b>\n\n"
                f"Имя: <code>{displayname}</code>\n"
                f"Статус: {status}\n"
                f"Статус WL: {wl_status}\n"
                f"В сети: {online}\n"
                f"Трафик в этом месяце: {monthly if monthly else "0"} MB / {limit} GB\n"
                f"Трафик WL в этом месяце: {wl_monthly if wl_monthly else "0"} MB / {wl_limit} GB\n"
                f"Дата окончания: {date}\n"
                f"Дней осталось: {days_left}\n"
                f"Upload: {up} MB | Download: {down} MB\n"
                f"WL Upload: {wl_up} MB | Download: {wl_down} MB\n"
                f"Отпечаток: <code>{fingerprint}</code>\n"
                f"Ссылка: <code>{domain}/sub?token={token}&lang=ru</code>\n"
            )
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton("✏️ Изменить пользователя", callback_data=f"edit_user_{username}"),
                types.InlineKeyboardButton("🔙 В меню", callback_data="cancel")
            )
            self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        except AppError as error:
            self._send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(chat_id, "❌ Внутренняя ошибка", reply_markup=self.get_main_menu())

    def _cb_del_user(self, chat_id: int, username: str) -> None:
        try:
            self.sub.delete_user(username=username, perma=True)
        except AppError as error:
            self._send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(chat_id, "❌ Внутренняя ошибка", reply_markup=self.get_main_menu())
            return
        self._send_message(chat_id, f"✅ Пользователь <b>{username}</b> удален.", parse_mode="HTML", reply_markup=self.get_main_menu())

    def _cb_edit_user_options(self, chat_id: int, username: str) -> None:
        self._pending_edits[chat_id] = {"username": username}
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("🔐 Отпечаток", callback_data="edit_fp"),
            types.InlineKeyboardButton("📊 Месячный лимит", callback_data="edit_limit"),
            types.InlineKeyboardButton("🌍 Мес. лимит WL", callback_data="edit_wl_limit"),
            types.InlineKeyboardButton("⏰ Срок", callback_data="edit_time"),
            types.InlineKeyboardButton("🏷 Отображаемое имя", callback_data="edit_name"),
            types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel")
        )
        self.bot.send_message(chat_id, f"✏️ Что изменить для <b>{username}</b>?", parse_mode="HTML", reply_markup=markup)

    def _cb_edit_fingerprint(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=False)
        except AppError as error:
            self.bot.send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return
        current = info.fingerprint
        markup = types.InlineKeyboardMarkup(row_width=2)
        for fp in self.cfg['fingerprints']:
            label = f"✅ {fp}" if fp == current else fp
            markup.add(types.InlineKeyboardButton(label, callback_data=f"fp_save_{fp}"))  # pyright: ignore[reportUnknownMemberType]
        markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel"))  # pyright: ignore[reportUnknownMemberType]
        self.bot.send_message(chat_id, f"🔐 Выберите отпечаток для <b>{username}</b> (текущий: <code>{current}</code>):", parse_mode="HTML", reply_markup=markup)

    def _cb_edit_limit(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=False)
        except AppError as error:
            self.bot.send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return
        current = info.bandwidth.limit
        msg = self.bot.send_message(chat_id, f"📊 Введите новый лимит в GB для <b>{username}</b> (текущий: <code>{current}</code> GB, 0 = безлимит):", parse_mode="HTML")
        self.bot.register_next_step_handler(msg, self._step_edit_limit, username)  # pyright: ignore[reportUnknownMemberType]

    def _step_edit_limit(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            limit = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_main_menu())
            return
        try:
            self.sub.update_params(username=username, limit=limit)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
            return
        self._pending_edits.pop(message.chat.id, None)
        self.bot.send_message(message.chat.id, f"✅ Месячный лимит обновлён: <code>{limit}</code> GB", parse_mode="HTML", reply_markup=self.get_main_menu())

    def _cb_edit_wl_limit(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=False)
        except AppError as error:
            self.bot.send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return
        current = info.bandwidth.wl_limit
        msg = self.bot.send_message(chat_id, f"🌍 Введите новый лимит ВЛ в GB для <b>{username}</b> (текущий: <code>{current}</code> GB, 0 = безлимит):", parse_mode="HTML")
        self.bot.register_next_step_handler(msg, self._step_edit_wl_limit, username)  # pyright: ignore[reportUnknownMemberType]

    def _step_edit_wl_limit(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            wl_limit = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_main_menu())
            return
        try:
            self.sub.update_params(username=username, wl_limit=wl_limit)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
            return
        self._pending_edits.pop(message.chat.id, None)
        self.bot.send_message(message.chat.id, f"✅ Лимит обновлён: <code>{wl_limit}</code> GB", parse_mode="HTML", reply_markup=self.get_main_menu())

    def _cb_edit_time(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=False)
        except AppError as error:
            self.bot.send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return
        current_time = info.time
        if current_time:
                date = datetime.fromtimestamp(current_time, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)")
        else:
            date = "N/A"
        msg = self.bot.send_message(chat_id, f"⏰ Введите новое кол-во дней для <b>{username}</b> (текущая дата: <code>{date}</code>, 0 = безлимит):", parse_mode="HTML")
        self.bot.register_next_step_handler(msg, self._step_edit_time, username)  # pyright: ignore[reportUnknownMemberType]

    def _step_edit_time(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            days = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_main_menu())
            return
        timee = int(time.time() + (days * 86400)) if days else 0
        try:
            self.sub.update_params(username=username, timee=timee)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
            return
        self._pending_edits.pop(message.chat.id, None)
        if days:
            new_date = datetime.fromtimestamp(timee, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)")
            self.bot.send_message(message.chat.id, f"✅ Срок продлён на <code>{days}</code> дней, новая дата: <code>{new_date}</code>", parse_mode="HTML", reply_markup=self.get_main_menu())
        else:
            self.bot.send_message(message.chat.id, "✅ Срок установлен в безлимит.", parse_mode="HTML", reply_markup=self.get_main_menu())

    def _cb_edit_name(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=False)
        except AppError as error:
            self.bot.send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return
        current = info.displayname
        msg = self.bot.send_message(chat_id, f"🏷 Введите новое отображаемое для <b>{username}</b> (текущий: <code>{current}</code>):", parse_mode="HTML")
        self.bot.register_next_step_handler(msg, self._step_edit_name, username)  # pyright: ignore[reportUnknownMemberType]

    def _step_edit_name(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        new_name = text.strip()
        if len(new_name) > 16:
            self.bot.send_message(message.chat.id, "❌ Имя слишком длинное (макс. 16 символов).", reply_markup=self.get_main_menu())
            return
        try:
            self.sub.update_params(username=username, displayname=new_name)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
            return
        self._pending_edits.pop(message.chat.id, None)
        self.bot.send_message(message.chat.id, f"✅ Имя обновлено: <code>{new_name}</code>", parse_mode="HTML", reply_markup=self.get_main_menu())

    def _step_reset_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        username = text.strip()

        try:
            obj = self.sub.reset_user(username)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return

        self.bot.send_message(message.chat.id, f"✅ Пользователь был сброшен.\n\nToken: <code>{obj.token}</code>\nUUID: <code>{obj.uuid}</code>", parse_mode="HTML", reply_markup=self.get_main_menu())

    def _step_add_user_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        username = text.strip()
        if self.sub.isuser(username):
            self.bot.send_message(message.chat.id, "❌ Этот username уже существует.", reply_markup=self.get_main_menu())
            return

        msg = self.bot.send_message(message.chat.id, "Введите отображаемое имя:")
        self.bot.register_next_step_handler(msg, self._step_add_user_display, username)  # pyright: ignore[reportUnknownMemberType]

    def _step_add_user_display(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        displayname = text.strip()

        msg = self.bot.send_message(message.chat.id, "Введите лимит в гигабайтах (или 0 для безлимита):")
        self.bot.register_next_step_handler(msg, self._step_add_user_limit, username, displayname)  # pyright: ignore[reportUnknownMemberType]

    def _step_add_user_limit(self, message: types.Message, username: str, displayname: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            limit = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Ошибка: Лимит должен быть числом.", reply_markup=self.get_main_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Введите кол-во дней подписки (0 для безлимита):")
        self.bot.register_next_step_handler(msg, self._step_add_user_time, username, displayname, limit)  # pyright: ignore[reportUnknownMemberType]

    def _step_add_user_time(self, message: types.Message, username: str, displayname: str, limit: int) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return

        try:
            timee = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Ошибка: Лимит времени должен быть числом.", reply_markup=self.get_main_menu())
            return

        timee = int(time.time() + (timee * 86400)) if timee else 0
        try:
            self.sub.add_new_user(username=username, displayname=displayname, limit=limit, timee=timee)
            self._send_message(message.chat.id, f"✅ Пользователь <b>{username}</b> успешно добавлен!", parse_mode="HTML", reply_markup=self.get_main_menu())
        except AppError as error:
            self._send_message(message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu())
        except Exception:
            self.log.error("Telegram handler failed", exc_info=True)
            self._send_message(message.chat.id, "❌ Внутренняя ошибка", reply_markup=self.get_main_menu())

    def _step_info_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        username = text.strip()
        self._cb_info_user(message.chat.id, username)



"""Administrator user management workflows."""

from __future__ import annotations

import time
from secrets import token_hex
from typing import cast

from telebot import types

from bots.edit_expiry import parse_edit_expiry
from bots.list_messages import list_messages
from errors import AppError, CommittedAuditError, PanelUnavailableError
from util import fmt_timestamp

from ..composition import AdminFeatureMixin
from .user_callbacks import user_callback

__all__ = ["AdminUsersMixin"]


class AdminUsersMixin(AdminFeatureMixin):
    """User listing, editing, and account workflows."""

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

    async def _cb_info_user(self, chat_id: int, username: str) -> None:
        try:
            info = await self.sub.business_svc.get_info(username, pretty=True)

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
            displayname = info.displayname
            status = "🟢 Включен" if info.enabled else "🔴 Отключен"
            wl_status = "🟢 Включен" if info.wl_enabled else "🔴 Отключен"
            online = "🟢 Да" if info.online else "🔴 Нет"
            fingerprint = info.fingerprint
            if times:
                days_left = str((times - int(time.time())) // 86400)
                date = fmt_timestamp(times)
            else:
                days_left = "N/A"
                date = "N/A"
            text = (
                f"ℹ️ <b>Информация о <code>{username}</code></b>\n\n"
                f"Имя: <code>{displayname}</code>\n"
                f"Статус: {status}\n"
                f"Статус WL: {wl_status}\n"
                f"В сети: {online}\n"
                f"Трафик в этом месяце: {monthly or '0'} MB / {limit} GB\n"
                f"Трафик WL в этом месяце: {wl_monthly or '0'} MB / {wl_limit} GB\n"
                f"Дата окончания: {date}\n"
                f"Дней осталось: {days_left}\n"
                f"Upload: {up} MB | Download: {down} MB\n"
                f"WL Upload: {wl_up} MB | Download: {wl_down} MB\n"
                f"Отпечаток: <code>{fingerprint}</code>\n"
                f"Ссылка: <code>{info.link}&lang=ru</code>\n"
            )
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(
                    "✏️ Изменить пользователя",
                    callback_data=user_callback("edit_user", username),
                ),
                types.InlineKeyboardButton("🔙 В меню", callback_data="cancel"),
            )
            await self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        except AppError as error:
            await self._send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
        except Exception:
            self.log.exception("Telegram handler failed")
            await self._send_message(
                chat_id,
                "❌ Внутренняя ошибка",
                reply_markup=self.get_main_menu(),
            )

    async def _cb_del_user(self, chat_id: int, username: str) -> None:
        try:
            await self.sub.business_svc.delete_user(username=username)
        except AppError as error:
            await self._send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        except Exception:
            self.log.exception("Telegram handler failed")
            await self._send_message(
                chat_id,
                "❌ Внутренняя ошибка",
                reply_markup=self.get_main_menu(),
            )
            return
        await self._send_message(
            chat_id,
            f"✅ Пользователь <b>{username}</b> удален.",
            parse_mode="HTML",
            reply_markup=self.get_main_menu(),
        )

    async def _cb_edit_user_options(self, chat_id: int, username: str) -> None:
        has_web_account = bool(await self.sub.user_svc.get_external_username(username))
        # The opaque token binds every downstream action to this target and menu,
        # without putting potentially long usernames in Telegram callback data.
        generation = token_hex(8)
        self._pending_edits[chat_id] = {"username": username, "generation": generation}
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("🔐 Отпечаток", callback_data=f"edit_fp:{generation}"),
            types.InlineKeyboardButton(
                "📊 Использовано за месяц", callback_data=f"edit_used:{generation}"
            ),
            types.InlineKeyboardButton(
                "📊 Месячный лимит", callback_data=f"edit_limit:{generation}"
            ),
            types.InlineKeyboardButton(
                "🌍 Использовано WL", callback_data=f"edit_wl_used:{generation}"
            ),
            types.InlineKeyboardButton(
                "🌍 Мес. лимит WL", callback_data=f"edit_wl_limit:{generation}"
            ),
            types.InlineKeyboardButton("⏰ Срок", callback_data=f"edit_time:{generation}"),
            types.InlineKeyboardButton(
                "🏷 Отображаемое имя", callback_data=f"edit_name:{generation}"
            ),
            types.InlineKeyboardButton("👤 Веб-логин", callback_data=f"edit_login:{generation}"),
            types.InlineKeyboardButton(
                "📅 Дата окончания", callback_data=f"edit_date:{generation}"
            ),
            types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel"),
        )
        if has_web_account:
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(
                    "🔑 Сброс пароля", callback_data=f"edit_password:{generation}"
                )
            )
        await self.bot.send_message(
            chat_id,
            f"✏️ Что изменить для <b>{username}</b>?",
            parse_mode="HTML",
            reply_markup=markup,
        )

    async def _cb_password_reset(self, chat_id: int, username: str) -> None:
        try:
            link = await self.sub.account_svc.issue_password_reset(username)
        except AppError as error:
            await self._send_message(chat_id, f"❌ {error.message}")
            return
        await self.bot.send_message(
            chat_id,
            "🔑 Ссылка для сброса пароля (одноразовая, действует 1 час или до перезапуска). "
            "Передайте её только этому пользователю:\n\n" + link,
            disable_web_page_preview=True,
            reply_markup=self.get_main_menu(),
        )

    async def _cb_edit_fingerprint(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        try:
            info = await self.sub.business_svc.get_info(username, pretty=False)
        except AppError as error:
            await self.bot.send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        current = info.fingerprint
        markup = types.InlineKeyboardMarkup(row_width=2)
        conf = self.cfg.view()
        for fp in conf["fingerprints"]:
            label = f"✅ {fp}" if fp == current else fp
            markup.add(  # pyright: ignore[reportUnknownMemberType]
                types.InlineKeyboardButton(label, callback_data=f"fp_save_{fp}:{generation}")
            )
        markup.add(types.InlineKeyboardButton("🔙 Отмена", callback_data="cancel"))  # pyright: ignore[reportUnknownMemberType]
        await self.bot.send_message(
            chat_id,
            f"🔐 Выберите отпечаток для <b>{username}</b> (текущий: <code>{current}</code>):",
            parse_mode="HTML",
            reply_markup=markup,
        )

    async def _cb_edit_used(self, chat_id: int, username: str, *, whitelist: bool = False) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        try:
            info = await self.sub.business_svc.get_info(username, pretty=False)
        except AppError as error:
            await self.bot.send_message(
                chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu()
            )
            return
        current = info.bandwidth.wl_monthly if whitelist else info.bandwidth.monthly
        msg = await self.bot.send_message(
            chat_id,
            f"📊 Введите использованный трафик{' WL' if whitelist else ''} в байтах для <b>{username}</b> (текущий: <code>{current}</code>):",
            parse_mode="HTML",
        )
        self._expect(msg, self._step_edit_used, username, generation, whitelist)  # pyright: ignore[reportUnknownMemberType]

    async def _cb_edit_wl_used(self, chat_id: int, username: str) -> None:
        await self._cb_edit_used(chat_id, username, whitelist=True)

    async def _step_edit_used(
        self, message: types.Message, username: str, generation: str, whitelist: bool
    ) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text).strip()
        if text.startswith("/"):
            return
        try:
            if not text.isascii() or not text.isdecimal() or not 0 <= int(text) <= 2**63 - 1:
                raise ValueError
            used = int(text)
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Введите целое число байт от 0 до 9223372036854775807.",
                reply_markup=self.get_main_menu(),
            )
            return
        try:
            if whitelist:
                await self.sub.business_svc.edit_user(username=username, wl_used=used)
            else:
                await self.sub.business_svc.edit_user(username=username, used=used)
        except CommittedAuditError:
            self.log.exception("user edit committed but audit failed")
            if self._pending_edits.get(message.chat.id) is pending:
                self._pending_edits.pop(message.chat.id, None)
            await self.bot.send_message(
                message.chat.id,
                "⚠️ Изменение сохранено, но запись аудита не удалась. Не повторяйте изменение.",
                reply_markup=self.get_main_menu(),
            )
            return
        except AppError as error:
            await self.bot.send_message(
                message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu()
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        await self.bot.send_message(
            message.chat.id,
            f"✅ Использованный трафик обновлён: {used} байт",
            reply_markup=self.get_main_menu(),
        )

    async def _cb_edit_limit(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        try:
            info = await self.sub.business_svc.get_info(username, pretty=False)
        except AppError as error:
            await self.bot.send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        current = info.bandwidth.limit
        msg = await self.bot.send_message(
            chat_id,
            f"📊 Введите новый лимит в GB для <b>{username}</b> (текущий: <code>{current}</code> GB, 0 = безлимит):",
            parse_mode="HTML",
        )
        self._expect(msg, self._step_edit_limit, username, generation)  # pyright: ignore[reportUnknownMemberType]

    async def _step_edit_limit(
        self, message: types.Message, username: str, generation: str
    ) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        try:
            limit = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Введите число.",
                reply_markup=self.get_main_menu(),
            )
            return
        try:
            await self.sub.business_svc.update_params(username=username, limit=limit)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ Ошибка: {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        await self.bot.send_message(
            message.chat.id,
            f"✅ Месячный лимит обновлён: <code>{limit}</code> GB",
            parse_mode="HTML",
            reply_markup=self.get_main_menu(),
        )

    async def _cb_edit_wl_limit(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        try:
            info = await self.sub.business_svc.get_info(username, pretty=False)
        except AppError as error:
            await self.bot.send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        current = info.bandwidth.wl_limit
        msg = await self.bot.send_message(
            chat_id,
            f"🌍 Введите новый лимит ВЛ в GB для <b>{username}</b> (текущий: <code>{current}</code> GB, 0 = безлимит):",
            parse_mode="HTML",
        )
        self._expect(msg, self._step_edit_wl_limit, username, generation)  # pyright: ignore[reportUnknownMemberType]

    async def _step_edit_wl_limit(
        self, message: types.Message, username: str, generation: str
    ) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        try:
            wl_limit = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Введите число.",
                reply_markup=self.get_main_menu(),
            )
            return
        try:
            await self.sub.business_svc.update_params(username=username, wl_limit=wl_limit)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ Ошибка: {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        await self.bot.send_message(
            message.chat.id,
            f"✅ Лимит обновлён: <code>{wl_limit}</code> GB",
            parse_mode="HTML",
            reply_markup=self.get_main_menu(),
        )

    async def _cb_edit_time(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        try:
            info = await self.sub.business_svc.get_info(username, pretty=False)
        except AppError as error:
            await self.bot.send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        current_time = info.time
        date = fmt_timestamp(current_time) if current_time else "N/A"
        msg = await self.bot.send_message(
            chat_id,
            f"⏰ Введите новое кол-во дней для <b>{username}</b> (текущая дата: <code>{date}</code>, 0 = безлимит):",
            parse_mode="HTML",
        )
        self._expect(msg, self._step_edit_time, username, generation)  # pyright: ignore[reportUnknownMemberType]

    async def _step_edit_time(self, message: types.Message, username: str, generation: str) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        try:
            days = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Введите число.",
                reply_markup=self.get_main_menu(),
            )
            return
        timee = int(time.time()) + days * 86400 if days else 0
        try:
            await self.sub.business_svc.update_params(username=username, timee=timee)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ Ошибка: {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        if days:
            new_date = fmt_timestamp(timee)
            await self.bot.send_message(
                message.chat.id,
                f"✅ Срок заменён: <code>{days}</code> дней от текущего момента, дата: <code>{new_date}</code>",
                parse_mode="HTML",
                reply_markup=self.get_main_menu(),
            )
        else:
            await self.bot.send_message(
                message.chat.id,
                "✅ Срок установлен в безлимит.",
                parse_mode="HTML",
                reply_markup=self.get_main_menu(),
            )

    async def _cb_edit_name(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        try:
            info = await self.sub.business_svc.get_info(username, pretty=False)
        except AppError as error:
            await self.bot.send_message(
                chat_id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        current = info.displayname
        msg = await self.bot.send_message(
            chat_id,
            f"🏷 Введите новое отображаемое для <b>{username}</b> (текущий: <code>{current}</code>):",
            parse_mode="HTML",
        )
        self._expect(msg, self._step_edit_name, username, generation)  # pyright: ignore[reportUnknownMemberType]

    async def _step_edit_name(self, message: types.Message, username: str, generation: str) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        new_name = text.strip()
        if len(new_name) > 16:
            await self.bot.send_message(
                message.chat.id,
                "❌ Имя слишком длинное (макс. 16 символов).",
                reply_markup=self.get_main_menu(),
            )
            return
        try:
            await self.sub.business_svc.update_params(username=username, displayname=new_name)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ Ошибка: {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        await self.bot.send_message(
            message.chat.id,
            f"✅ Имя обновлено: <code>{new_name}</code>",
            parse_mode="HTML",
            reply_markup=self.get_main_menu(),
        )

    async def _cb_edit_login(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        msg = await self.bot.send_message(
            chat_id, "👤 Введите новый веб-логин (только для существующего веб-аккаунта):"
        )
        self._expect(msg, self._step_edit_login, username, generation)  # pyright: ignore[reportUnknownMemberType]

    async def _step_edit_login(
        self, message: types.Message, username: str, generation: str
    ) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text).strip()
        if text.startswith("/"):
            return
        if not text or len(text) > 32:
            await self.bot.send_message(
                message.chat.id,
                "❌ Введите логин длиной от 1 до 32 символов.",
                reply_markup=self.get_main_menu(),
            )
            return
        try:
            await self.sub.business_svc.edit_user(username=username, new_username=text)
        except CommittedAuditError:
            self.log.exception("user edit committed but audit failed")
            if self._pending_edits.get(message.chat.id) is pending:
                self._pending_edits.pop(message.chat.id, None)
            await self.bot.send_message(
                message.chat.id,
                "⚠️ Изменение сохранено, но запись аудита не удалась. Не повторяйте изменение.",
                reply_markup=self.get_main_menu(),
            )
            return
        except AppError as error:
            await self.bot.send_message(
                message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu()
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        await self.bot.send_message(
            message.chat.id, "✅ Веб-логин обновлён.", reply_markup=self.get_main_menu()
        )

    async def _cb_edit_date(self, chat_id: int, username: str) -> None:
        generation = self._pending_edits.get(chat_id, {}).get("generation", "")
        msg = await self.bot.send_message(
            chat_id, "📅 Введите дату окончания в UTC: ГГГГ-ММ-ДД ЧЧ:ММ, или 0 для безлимита."
        )
        self._expect(msg, self._step_edit_date, username, generation)  # pyright: ignore[reportUnknownMemberType]

    async def _step_edit_date(self, message: types.Message, username: str, generation: str) -> None:
        pending = self._pending_edits.get(message.chat.id)
        if not pending or pending != {"username": username, "generation": generation}:
            return
        text = cast(str, message.text).strip()
        if text.startswith("/"):
            return
        try:
            timee = parse_edit_expiry(text)
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Неверная дата. Используйте ГГГГ-ММ-ДД ЧЧ:ММ (UTC) или 0.",
                reply_markup=self.get_main_menu(),
            )
            return
        try:
            await self.sub.business_svc.update_params(username=username, timee=timee)
        except CommittedAuditError:
            self.log.exception("user edit committed but audit failed")
            if self._pending_edits.get(message.chat.id) is pending:
                self._pending_edits.pop(message.chat.id, None)
            await self.bot.send_message(
                message.chat.id,
                "⚠️ Изменение сохранено, но запись аудита не удалась. Не повторяйте изменение.",
                reply_markup=self.get_main_menu(),
            )
            return
        except AppError as error:
            await self.bot.send_message(
                message.chat.id, f"❌ Ошибка: {error.message}", reply_markup=self.get_main_menu()
            )
            return
        if self._pending_edits.get(message.chat.id) is pending:
            self._pending_edits.pop(message.chat.id, None)
        await self.bot.send_message(
            message.chat.id, "✅ Дата окончания обновлена.", reply_markup=self.get_main_menu()
        )

    async def _step_reset_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        username = text.strip()

        try:
            obj = await self.sub.business_svc.reset_user(username)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id,
                f"❌ {error.message}",
                reply_markup=self.get_main_menu(),
            )
            return

        await self.bot.send_message(
            message.chat.id,
            f"✅ Пользователь был сброшен.\n\nToken: <code>{obj.token}</code>\nUUID: <code>{obj.uuid}</code>",
            parse_mode="HTML",
            reply_markup=self.get_main_menu(),
        )

    async def _step_add_user_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        username = text.strip()
        if await self.sub.user_svc.isuser(username):
            await self.bot.send_message(
                message.chat.id,
                "❌ Этот username уже существует.",
                reply_markup=self.get_main_menu(),
            )
            return

        msg = await self.bot.send_message(message.chat.id, "Введите отображаемое имя:")
        self._expect(msg, self._step_add_user_display, username)  # pyright: ignore[reportUnknownMemberType]

    async def _step_add_user_display(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        displayname = text.strip()

        msg = await self.bot.send_message(
            message.chat.id,
            "Введите лимит в гигабайтах (или 0 для безлимита):",
        )
        self._expect(msg, self._step_add_user_limit, username, displayname)  # pyright: ignore[reportUnknownMemberType]

    async def _step_add_user_limit(
        self,
        message: types.Message,
        username: str,
        displayname: str,
    ) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        try:
            limit = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Ошибка: Лимит должен быть числом.",
                reply_markup=self.get_main_menu(),
            )
            return
        msg = await self.bot.send_message(
            message.chat.id,
            "Введите кол-во дней подписки (0 для безлимита):",
        )
        self._expect(  # pyright: ignore[reportUnknownMemberType]
            msg,
            self._step_add_user_time,
            username,
            displayname,
            limit,
        )

    async def _step_add_user_time(
        self,
        message: types.Message,
        username: str,
        displayname: str,
        limit: int,
    ) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return

        try:
            timee = int(text.strip())
        except ValueError:
            await self.bot.send_message(
                message.chat.id,
                "❌ Ошибка: Лимит времени должен быть числом.",
                reply_markup=self.get_main_menu(),
            )
            return

        timee = int(time.time()) + timee * 86400 if timee else 0
        try:
            await self.sub.business_svc.add_new_user(
                username=username,
                displayname=displayname,
                limit=limit,
                timee=timee,
            )
            await self._send_message(
                message.chat.id,
                f"✅ Пользователь <b>{username}</b> успешно добавлен!",
                parse_mode="HTML",
                reply_markup=self.get_main_menu(),
            )
        except AppError as error:
            await self._send_message(
                message.chat.id,
                f"❌ Ошибка: {error.message}",
                reply_markup=self.get_main_menu(),
            )
        except Exception:
            self.log.exception("Telegram handler failed")
            await self._send_message(
                message.chat.id,
                "❌ Внутренняя ошибка",
                reply_markup=self.get_main_menu(),
            )

    async def _step_info_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        username = text.strip()
        await self._cb_info_user(message.chat.id, username)

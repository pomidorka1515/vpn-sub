"""Administrator traffic chart workflows."""
from __future__ import annotations

from ..composition import AdminFeatureMixin

import threading
from collections.abc import Mapping
from typing import cast

from telebot import types

from chart import bandwidth_chart
from errors import AppError
from util import fmt_bytes, format_usage, truncate_utf8
__all__ = ["AdminTrafficMixin"]


class AdminTrafficMixin(AdminFeatureMixin):
    """Per-user traffic chart workflows."""

    def _step_chart_username(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        username = text.strip()

        try:
            self.sub.user_svc.get_user_state(username)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_main_menu())
            return

        msg = self.bot.send_message(message.chat.id, "Введите количество дней (1-90):")
        self.bot.register_next_step_handler(msg, self._step_chart_days, username)  # pyright: ignore[reportUnknownMemberType]

    def _step_chart_days(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return

        try:
            days = int(text.strip())
            if not 1 <= days <= 90:
                raise ValueError
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число от 1 до 90.", reply_markup=self.get_main_menu())
            return

        self.bot.send_message(message.chat.id, "⏳ Генерация графика...")

        thread = threading.Thread(
            target=self._render_chart,
            kwargs={"username": username, "days": days, "chat_id": message.chat.id},
            daemon=True,
            name=f"admin-chart-{username}"
        )
        thread.start()

    def _render_chart(self, *, username: str, days: int, chat_id: int) -> None:
        try:
            snapshots = self.sub.bandwidth_svc.get_bw_history(username, days=days)
            info = self.sub.business_svc.get_info(username, pretty=False)

            bandwidths = info.bandwidth

            upload_fmt = fmt_bytes(bandwidths.total.upload)
            download_fmt = fmt_bytes(bandwidths.total.download)
            wl_upload_fmt = fmt_bytes(bandwidths.wl_total.upload)
            wl_download_fmt = fmt_bytes(bandwidths.wl_total.download)

            limit = bandwidths.limit
            monthly = bandwidths.monthly

            used_str, limit_str, percent_str = format_usage(monthly, limit)

            wl_limit = bandwidths.wl_limit
            wl_monthly = bandwidths.wl_monthly

            wl_used_str, wl_limit_str, wl_percent_str = format_usage(wl_monthly, wl_limit)

            text = f"""📈 <b>График трафика за {days} дней</b>

<b>Пользователь:</b> <code>{username}</code> ({info.displayname})

<b>Общий трафик:</b>
├ Upload: {upload_fmt}
├ Download: {download_fmt}
├ Использовано: {used_str} / {limit_str}
└ Процент: {percent_str}

<b>WL трафик:</b>
├ Upload: {wl_upload_fmt}
├ Download: {wl_download_fmt}
├ Использовано: {wl_used_str} / {wl_limit_str}
└ Процент: {wl_percent_str}"""


            text = truncate_utf8(text, 1024)
            chart_img = bandwidth_chart(
                snapshots,
                label=info.displayname,
                lang=self.lang_cfg.get('chart',
                    as_type=Mapping[str, Mapping[str, str]]
                )['ru']
            )
            if chart_img is not None:
                self.bot.send_photo(chat_id, chart_img, caption=text, parse_mode="HTML", reply_markup=self.get_main_menu())
            else:
                self.bot.send_message(chat_id, text + "\n\n❌ Нет данных для графика", parse_mode="HTML", reply_markup=self.get_main_menu())
        except AppError as error:
            self._send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
        except Exception as e:
            self.log.error(f"Chart error for username {username}: {e}", exc_info=True)
            self._send_message(chat_id, "❌ Внутренняя ошибка", reply_markup=self.get_main_menu())



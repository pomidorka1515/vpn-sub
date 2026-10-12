"""Administrator traffic chart workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from chart import bandwidth_chart
from errors import AppError
from util import fmt_bytes, format_usage, truncate_utf8

from ..composition import AdminFeatureMixin

if TYPE_CHECKING:
    from telebot import types

__all__ = ["AdminTrafficMixin"]


class AdminTrafficMixin(AdminFeatureMixin):
    """Per-user traffic chart workflows."""

    async def _step_chart_username(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return
        username = text.strip()

        try:
            await self.sub.user_svc.get_user_state(username)
        except AppError as error:
            await self.bot.send_message(
                message.chat.id, f"❌ {error.message}", reply_markup=self.get_main_menu(),
            )
            return

        msg = await self.bot.send_message(message.chat.id, "Введите количество дней (1-90):")
        self._expect(msg, self._step_chart_days, username)  # pyright: ignore[reportUnknownMemberType]

    async def _step_chart_days(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return

        try:
            days = int(text.strip())
            if not 1 <= days <= 90:
                raise ValueError
        except ValueError:
            await self.bot.send_message(
                message.chat.id, "❌ Введите число от 1 до 90.", reply_markup=self.get_main_menu(),
            )
            return

        await self.bot.send_message(message.chat.id, "⏳ Генерация графика...")

        await self._render_chart(username=username, days=days, chat_id=message.chat.id)

    async def _render_chart(self, *, username: str, days: int, chat_id: int) -> None:
        try:
            snapshots = await self.sub.bandwidth_svc.get_bw_history(username, days=days)
            info = await self.sub.business_svc.get_info(username, pretty=False)

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
            language = self.lang_cfg.view()
            chart_img = await self.sub.res.offloads.rendering.run(bandwidth_chart,
                snapshots, label=info.displayname, lang=language["chart"]["ru"],
            )
            if chart_img is not None:
                await self.bot.send_photo(
                    chat_id,
                    chart_img,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=self.get_main_menu(),
                )
            else:
                await self.bot.send_message(
                    chat_id,
                    text + "\n\n❌ Нет данных для графика",
                    parse_mode="HTML",
                    reply_markup=self.get_main_menu(),
                )
        except AppError as error:
            await self._send_message(chat_id, f"❌ {error.message}", reply_markup=self.get_main_menu())
        except Exception:
            self.log.exception("Chart error for username %s", username)
            await self._send_message(chat_id, "❌ Внутренняя ошибка", reply_markup=self.get_main_menu())

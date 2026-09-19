"""Public traffic chart workflows."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

from collections.abc import Mapping
from typing import cast

from telebot import types

from chart import bandwidth_chart
from errors import AppError
from util import fmt_bytes, format_usage, truncate_utf8
__all__ = ["PublicTrafficMixin"]


class PublicTrafficMixin(PublicFeatureMixin):
    """Chart selection and background rendering workflows."""

    def chart_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id

        if not self.sub.is_registered(uid):
            return

        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        try:
            days = int(data.split('_', 1)[1])
            if not 1 <= days <= 90:
                raise ValueError
        except (ValueError, IndexError):
            self.bot.answer_callback_query(call.id, t['chart_invalid_period'])
            return

        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str):
            return

        self.bot.answer_callback_query(call.id, t['chart_generating'])

        try:
            self._executor.submit(
                self._render_chart,
                uid=uid, username=username, days=days, lang=lang, chat_id=message.chat.id
            )
        except Exception:
            self.log.error("chart submission failed", exc_info=True)
            self._send_message(message.chat.id, t.get("error_generic", "⚠️ Error"))

    def _render_chart(
        self,
        *,
        uid: int,
        username: str,
        days: int,
        lang: str,
        chat_id: int
    ) -> None:
        t = self.TEXTS[lang]
        try:
            snapshots = self.sub.get_bw_history(username, days=days)
            info = self.sub.get_info(username, pretty=False)

            bandwidths = info.bandwidth

            upload_fmt = fmt_bytes(bandwidths.total.upload)
            download_fmt = fmt_bytes(bandwidths.total.download)
            wl_upload_fmt = fmt_bytes(bandwidths.wl_total.upload)
            wl_download_fmt = fmt_bytes(bandwidths.wl_total.download)

            limit = bandwidths.limit
            monthly = bandwidths.monthly

            used_str, limit_str, percent_str = format_usage(monthly, limit, t['unlimited'])

            wl_limit = bandwidths.wl_limit
            wl_monthly = bandwidths.wl_monthly

            wl_used_str, wl_limit_str, wl_percent_str = format_usage(wl_monthly, wl_limit, t['unlimited'])

            text = t['chart_text'].format(
                days=days,
                upload=upload_fmt,
                download=download_fmt,
                used=used_str,
                limit=limit_str,
                percent=percent_str,
                wl_upload=wl_upload_fmt,
                wl_download=wl_download_fmt,
                wl_used=wl_used_str,
                wl_limit=wl_limit_str,
                wl_percent=wl_percent_str
            )

            text = truncate_utf8(text, 1024)

            chart_img = bandwidth_chart(
                snapshots,
                label=info.displayname,
                lang=self.lang_cfg.get('chart',
                    as_type=Mapping[str, Mapping[str, str]]
                )[lang]
            )
            if chart_img is not None:
                self.bot.send_photo(chat_id, chart_img, caption=text, parse_mode="HTML", reply_markup=self.get_menu(uid))
            else:
                self.bot.send_message(chat_id, text + "\n\n" + t.get('no_data', 'No chart data available'), parse_mode="HTML", reply_markup=self.get_menu(uid))
        except AppError as error:
            self._send_message(chat_id, error.message, reply_markup=self.get_menu(uid))
        except Exception:
            self.log.error(f"Chart error for uid {uid}", exc_info=True)
            self._send_message(chat_id, "Error occurred", reply_markup=self.get_menu(uid))



"""Public subscription information workflows."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

import time
import urllib.parse
from datetime import datetime, timezone
from util import fmt_bytes, make_qr
from telebot import types
__all__ = ["PublicSubscriptionMixin"]


class PublicSubscriptionMixin(PublicFeatureMixin):
    """Subscription-link and account-information workflows."""

    def send_info(self, chat_id: int, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        info = self.sub.telegram_svc.get_info_telegram(uid)
        if not info:
            return
        daystext = "дней" if lang == 'ru' else "days"
        unlimited = f"<i>{t['unlimited']}</i>"

        def quota(used: int | float, limit: int) -> str:
            if not limit:
                return unlimited
            return f"{fmt_bytes(used)} / {limit} GB"

        if info.time:
            days_left = str((info.time - int(time.time())) // 86400)
            date_end = datetime.fromtimestamp(info.time, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)")
            time_str = f"{days_left} {daystext} ({date_end})"
        else:
            time_str = t['lifetime']

        status = "🟢" if info.enabled else "🔴"
        wl_status = "🟢" if info.wl_enabled else "🔴"
        online = "🟢" if info.online else "🔴"
        bw = info.bandwidth
        text = t['info_text'].format(
            username=info.displayname,
            status=status,
            wl_status=wl_status,
            online=online,
            total=fmt_bytes(bw.total.total),
            monthly=quota(bw.monthly, bw.limit),
            wl_total=fmt_bytes(bw.wl_total.total),
            wl_monthly=quota(bw.wl_monthly, bw.wl_limit),
            up=fmt_bytes(bw.total.upload),
            down=fmt_bytes(bw.total.download),
            wl_up=fmt_bytes(bw.wl_total.upload),
            wl_down=fmt_bytes(bw.wl_total.download),
            days=time_str,
            fingerprint=info.fingerprint
        )
        self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_menu(uid))

    def send_link(self, chat_id: int, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        info = self.sub.telegram_svc.get_info_telegram(uid)
        if not info:
            return

        sub_uri = self.cfg['uri'].strip("/")
        domain = self.cfg['domain'].rstrip("/")
        
        link = f"{domain}/{sub_uri}?token={info.token}&lang={lang}"
            
        qr = make_qr(link)

        text = t['get_sub_text'].format(
            link=link
        )

        domain = self.cfg['domain']
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton(t['get_sub_btn_link'], url=link),
            types.InlineKeyboardButton(t['get_sub_btn_happ'], url=f"{domain}/{sub_uri}/redirect?url={urllib.parse.quote(link)}&prefix={urllib.parse.quote("happ://add/")}")
        )
        self.bot.send_photo(chat_id, qr, text, parse_mode="Markdown", reply_markup=markup)

"""Public subscription information workflows."""
from __future__ import annotations

from ..composition import PublicFeatureMixin

import time
import urllib.parse
from datetime import datetime, timezone

from telebot import types
__all__ = ["PublicSubscriptionMixin"]


class PublicSubscriptionMixin(PublicFeatureMixin):
    """Subscription-link and account-information workflows."""

    def send_info(self, chat_id: int, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        info = self.sub.get_info_telegram(uid)
        if not info:
            return
        daystext = "дней" if lang == 'ru' else "days"
        limit_str = f"{info.bandwidth.limit} GB" if info.bandwidth.limit else t['unlimited']
        wl_limit_str = f"{info.bandwidth.wl_limit} GB" if info.bandwidth.wl_limit else t['unlimited']

        monthly_str = f"{info.bandwidth.monthly / (1024 ** 2):.2f}"
        wl_monthly_str = f"{info.bandwidth.wl_monthly / (1024 ** 2):.2f}"

        if limit_str == t['unlimited']:
            monthly_str = t['unlimited']
        if wl_limit_str == t['unlimited']:
            wl_monthly_str = t['unlimited']

        if info.time:
            days_left = str((info.time - int(time.time())) // 86400)
            date_end = datetime.fromtimestamp(info.time, tz=timezone.utc).strftime("%d.%m.%y %H:%M (UTC)")
            time_str = f"{days_left} {daystext} ({date_end})"
        else:
            time_str = t['lifetime']

        status = "🟢" if info.enabled else "🔴"
        wl_status = "🟢" if info.wl_enabled else "🔴"

        online = "🟢" if info.online else "🔴"
        text = t['info_text'].format(
            username=info.displayname,
            status=status,
            wl_status=wl_status,
            online=online,
            total=info.bandwidth.total.total,
            monthly=monthly_str,
            limit=limit_str,
            wl_total=info.bandwidth.wl_total.total,
            wl_monthly=wl_monthly_str,
            wl_limit=wl_limit_str,
            up=info.bandwidth.total.upload,
            down=info.bandwidth.total.download,
            wl_up=info.bandwidth.wl_total.upload,
            wl_down=info.bandwidth.wl_total.download,
            days=time_str,
            fingerprint=info.fingerprint
        )
        self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_menu(uid))

    def send_link(self, chat_id: int, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        info = self.sub.get_info_telegram(uid)
        if not info:
            return

        sub_uri = self.cfg['uri'].strip("/")
        domain = self.cfg['domain'].rstrip("/")
        
        link = f"{domain}/{sub_uri}?token={info.token}&lang={lang}"
            
        qr = self.sub.make_qr(link)

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



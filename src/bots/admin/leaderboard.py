"""Administrator leaderboard workflows."""
from __future__ import annotations

from ..composition import AdminFeatureMixin

from collections.abc import Mapping
from typing import Literal, cast

from telebot import types

from chart import leaderboard_chart
from util import fmt_bytes
__all__ = ["AdminLeaderboardMixin"]


class AdminLeaderboardMixin(AdminFeatureMixin):
    """Leaderboard selection and rendering workflows."""

    def _cb_leaderboard_type(self, chat_id: int) -> None:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("📊 Общий (total)", callback_data="lbt_total"),
            types.InlineKeyboardButton("📅 Месячный (monthly)", callback_data="lbt_monthly"),
            types.InlineKeyboardButton("🛡 Белый список (wl_monthly)", callback_data="lbt_wl_monthly"),
        )
        self.bot.send_message(chat_id, "🏆 <b>Таблица лидеров</b>\n\nВыберите тип трафика:", parse_mode="HTML", reply_markup=markup)

    def _cb_leaderboard_order(self, chat_id: int) -> None:
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(  # pyright: ignore[reportUnknownMemberType]
            types.InlineKeyboardButton("🔽 По убыванию (1st = most)", callback_data="lbo_desc"),
            types.InlineKeyboardButton("🔼 По возрастанию (1st = least)", callback_data="lbo_asc"),
        )
        self.bot.send_message(chat_id, "📋 <b>Сортировка</b>\n\nВыберите порядок:", parse_mode="HTML", reply_markup=markup)

    def _cb_leaderboard_window(self, chat_id: int) -> None:
        msg = self.bot.send_message(
            chat_id,
            "🔢 <b>Количество записей</b>\n\nВведите число (0 = все пользователи, >= 0):",
            parse_mode="HTML"
        )
        self.bot.register_next_step_handler(msg, self._step_leaderboard_window)  # pyright: ignore[reportUnknownMemberType]

    def _step_leaderboard_window(self, message: types.Message) -> None:
        chat_id = message.chat.id
        if message.text is None:
            self.bot.send_message(chat_id, "❌ Введите число.", reply_markup=self.get_main_menu())
            return

        try:
            window = int(message.text)
            if window < 0:
                raise ValueError
        except ValueError:
            self.bot.send_message(chat_id, "❌ Введите неотрицательное целое число.", reply_markup=self.get_main_menu())
            return

        pending = self._pending_leaderboard.pop(chat_id, {})
        if pending:
            bw_type = cast(Literal["total", "monthly", "wl_monthly"], pending.get('type', 'total'))
            order = cast(Literal["asc", "desc"], pending.get('order', 'desc'))
            self._handle_leaderboard_result(chat_id, bw_type, order, window)
        else:
            self.bot.send_message(chat_id, "❌ Сессия истекла, начните заново.", reply_markup=self.get_main_menu())

    def _handle_leaderboard_result(
        self,
        chat_id: int,
        bw_type: Literal["total", "monthly", "wl_monthly"],
        order: Literal["asc", "desc"],
        window: int
    ) -> None:
        lb_data = self.sub.leaderboard_svc.leaderboard(
            category=bw_type,
            top_n=window,
            use_displaynames=True,
            flip=True if order == "asc" else False
        )

        chart = leaderboard_chart(
            lb_data, bandwidth_type=bw_type,
            lang=self.lang_cfg.get('chart',
                as_type=Mapping[str, Mapping[str, str]]
            )['ru']
        )

        if chart is None:
            self.bot.send_message(chat_id, "Нет данных!")
            return

        sorted_items = sorted(lb_data.items(), key=lambda x: x[1], reverse=(order == "desc"))

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for rank, (user, score) in enumerate(sorted_items, start=1):
            prefix = medals.get(rank, f"{rank}.")
            lines.append(f"{prefix} <b>{user}</b> - {fmt_bytes(score)}")
        text = "<b>Список лидеров по трафику:</b>\n\n" + "\n".join(lines)

        b = text.encode("utf-8")
        if len(b) > 1024:
            b = b[:1021]
            while b and (b[-1] & 0x80):  # back up from a continuation byte
                b = b[:-1]
            text = b.decode("utf-8", errors="ignore") + "..."

        self.bot.send_photo(chat_id, chart, text, parse_mode="HTML")



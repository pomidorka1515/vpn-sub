from __future__ import annotations

from config import Config
from loggers import Logger
from core import Subscription, fmt_bytes
from session import XUiSession
from chart import bandwidth_chart

import telebot
import time
import threading
import urllib.parse

from typing import cast, Any
from datetime import timedelta, timezone, datetime
from telebot import types


__all__ = ['AdminBot', 'PublicBot']

class AdminBot:
    """Administrator bot for management purposes.
    Dependencies: Subscription
    Classes depending on this: none"""

    def __init__(self,
                 sub: Subscription,
                 cfg: Config):
        self.log = Logger(type(self).__name__)
        with self.log.loading():        
            self.cfg = cfg
            self.sub = sub 
            self.bot = telebot.TeleBot(self.cfg['bot']['token'])
            self.admin_uids = self.cfg['bot']['whitelist']
            
            self.bot.message_handler(commands=['start', 'menu'])(self.cmd_start) # type: ignore
            self.bot.callback_query_handler(func=lambda call: True)(self.handle_callbacks) # type: ignore
            self._pending_codes: dict[Any, Any] = {}

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_uids
 
    def msg(self, text: str, parse_mode: str = "HTML") -> None:
        for uid in self.admin_uids:
            try:
                self.bot.send_message(uid, text, parse_mode=parse_mode)
            except Exception as e:
                self.log.error(f"failed to send message to admin ID {uid}: {e}")
    
    def get_main_menu(self) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add( # type: ignore[call-arg]
            types.InlineKeyboardButton("👥 Список юзеров", callback_data="list_users"),
            types.InlineKeyboardButton("ℹ️ Инфо о юзере", callback_data="action_info"),
            types.InlineKeyboardButton("➕ Добавить", callback_data="add_user"),
            types.InlineKeyboardButton("❌ Удалить", callback_data="action_del"),
            types.InlineKeyboardButton("🔄 Refresh всех", callback_data="refresh_all"),
            types.InlineKeyboardButton("🎟 Коды", callback_data="codes_menu"),
            types.InlineKeyboardButton("🟢Пользователи онлайн", callback_data="online_users"),
            types.InlineKeyboardButton("⚠ Сбросить пользователя", callback_data="reset_user"),
            types.InlineKeyboardButton("ℹ️ Статус панелей", callback_data="status_panels")
        )
        return markup
 
    def get_codes_menu(self) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add( # type: ignore[call-arg]
            types.InlineKeyboardButton("➕ Добавить код", callback_data="add_code"),
            types.InlineKeyboardButton("❌ Удалить код", callback_data="del_code"),
            types.InlineKeyboardButton("📋 Список кодов", callback_data="list_codes"),
            types.InlineKeyboardButton("ℹ️ Инфо о коде", callback_data="info_code"),
            types.InlineKeyboardButton("🔙 В меню", callback_data="cancel")
        )
        return markup
 
    def get_users_menu(self, prefix: str) -> types.InlineKeyboardMarkup:
        markup = types.InlineKeyboardMarkup(row_width=2)
        buttons: list[types.InlineKeyboardButton] = []
        for user in self.cfg['users'].keys():
            buttons.append(types.InlineKeyboardButton(user, callback_data=f"{prefix}_{user}"))
        markup.add(*buttons)  # type: ignore[call-arg]
        markup.add(types.InlineKeyboardButton("🔙 Отмена / В меню", callback_data="cancel")) # type: ignore[call-arg]
        return markup
 
 
    def cmd_start(self, message: types.Message) -> None:
        if not self.is_admin(cast(types.User, message.from_user).id):
            return
        self.bot.send_message(
            message.chat.id, 
            "👋 Привет! Панель управления VPN запущена.", 
            reply_markup=self.get_main_menu()
        )
 
    def handle_callbacks(self, call: types.CallbackQuery):
        if not self.is_admin(call.from_user.id):
            return
        
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        chat_id = message.chat.id
        self.bot.answer_callback_query(call.id)


        try:
            if data == "cancel":
                self.bot.edit_message_text("Действие отменено. Главное меню:", chat_id, message.message_id, reply_markup=self.get_main_menu())
                self.bot.clear_step_handler_by_chat_id(chat_id) 

            elif data == "online_users":
                self._cb_online_users(chat_id)  
            
            elif data == "list_users":
                self._cb_list_users(chat_id)
                
            elif data == "refresh_all":
                self._cb_refresh(chat_id)

            elif data == "reset_user":
                msg = self.bot.send_message(chat_id, "Введите username пользователя:")
                self.bot.register_next_step_handler(msg, self._step_reset_user) # type: ignore

            elif data == "add_user":
                msg = self.bot.send_message(chat_id, "Введите username нового пользователя (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_add_user_name) # type: ignore
 
            elif data == "action_info":
                msg = self.bot.send_message(chat_id, "Введите username пользователя (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_info_user) # type: ignore
 
            elif data == "action_del":
                if not self.cfg['users']:
                    self.bot.send_message(chat_id, "Список пуст.", reply_markup=self.get_main_menu())
                    return
                self.bot.edit_message_text("Выберите пользователя для УДАЛЕНИЯ ⚠️:", chat_id, message.message_id, reply_markup=self.get_users_menu("dodel"))
 
            elif data.startswith("dodel_"):
                username = data.split("dodel_", 1)[1]
                self._cb_del_user(chat_id, username)
 
            elif data == "codes_menu":
                self.bot.edit_message_text("🎟 Управление кодами:", chat_id, message.message_id, reply_markup=self.get_codes_menu())
 
            elif data == "list_codes":
                self._cb_list_codes(chat_id)
 
            elif data == "info_code":
                msg = self.bot.send_message(chat_id, "Введите код (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_info_code)  # type: ignore
 
            elif data == "del_code":
                msg = self.bot.send_message(chat_id, "Введите код для удаления (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_del_code) # type: ignore
 
            elif data == "add_code":
                msg = self.bot.send_message(chat_id, "Введите название кода (или /start для отмены):")
                self.bot.register_next_step_handler(msg, self._step_add_code_name) # type: ignore

            elif data == "status_panels":
                self._cb_all_panels_status(chat_id)
            elif data.startswith("codetype_"):
                code_type = data.split("codetype_", 1)[1]
                code_name = self._pending_codes.pop(chat_id, None)
                if not code_name:
                    self.bot.send_message(chat_id, "❌ Сессия истекла, начните заново.")
                    return
                msg = self.bot.send_message(chat_id, f"Тип: <b>{code_type}</b>\nВведите количество дней:", parse_mode="HTML")
                self.bot.register_next_step_handler(msg, self._step_add_code_days, code_type, code_name) # type: ignore[call-arg]
        except Exception as e:
            self.log.error(f"Ошибка в боте: {e}", exc_info=True)
            self.bot.send_message(chat_id, f"⚠️ Произошла ошибка: {e}")

    def _cb_panel_info(self, 
                       chat_id: int, 
                       panel: XUiSession, 
                       last: bool) -> None:
        info = self.sub.getstatus(panel)
        if info is None:
            self.bot.send_message(chat_id, "Произошла ошибка: getstatus() вернул None", reply_markup=self.get_main_menu())
            return
        info = info['obj']
        sys_up = self.sub.fmt_time(info.get('uptime', 0))
        app_up = self.sub.fmt_time(info.get('appStats', {}).get('uptime', 0))
        xr = info.get('xray', {})
        xr_state = xr.get('state', 'unknown')
        xr_status = "🟢 Работает" if xr_state == "running" else f"🔴 {xr.get('errorMsg', 'unknown error')}"        
        GB = 1024 ** 3
        MB = 1024 ** 2
        text = f"""📊 <b>Статус сервера {panel.name}</b>

🖥 <b>Система</b>
├ <b>CPU:</b> <code>{int(round(info['cpu']))}%</code> (<code>{info['cpuCores']}</code>/<code>{info['logicalPro']}</code> ядер, <code>{int(round(info['cpuSpeedMhz']))} MHz</code>)
├ <b>Load:</b> <code>{info['loads'][0]}</code> | <code>{info['loads'][1]}</code> | <code>{info['loads'][2]}</code>
├ <b>RAM:</b> <code>{info['mem']['current'] / GB:.2f} GB</code> / <code>{info['mem']['total'] / GB:.2f} GB</code>
└ <b>Uptime:</b> <code>{sys_up}</code>

💾 <b>Накопители</b>
├ <b>Диск:</b> <code>{info['disk']['current'] / GB:.2f} GB</code> / <code>{info['disk']['total'] / GB:.2f} GB</code>
└ <b>Swap:</b> <code>{info['swap']['current'] / GB:.2f} GB</code> / <code>{info['swap']['total'] / GB:.2f} GB</code>

🌐 <b>Сеть & IP</b>
├ <b>IPv4:</b> <code>{info['publicIP']['ipv4']}</code>
├ <b>IPv6:</b> <code>{info['publicIP']['ipv6'] or 'Отключен'}</code>
├ <b>Соединения:</b> <code>{info['tcpCount']}</code> TCP / <code>{info['udpCount']}</code> UDP
├ <b>Скорость:</b> ⬇️ <code>{info['netIO']['down'] / MB:.2f} MB/s</code> | ⬆️ <code>{info['netIO']['up'] / MB:.2f} MB/s</code>
└ <b>Трафик:</b> ⬇️ <code>{info['netTraffic']['recv'] / GB:.2f} GB</code> | ⬆️ <code>{info['netTraffic']['sent'] / GB:.2f} GB</code>

⚡️ <b>Xray Core v{info['xray']['version']}</b>
└ <b>Статус:</b> {xr_status}

🤖 <b>Other</b>
├ <b>Потоков:</b> <code>{info['appStats']['threads']}</code>
├ <b>RAM:</b> <code>{info['appStats']['mem'] / MB:.2f} MB</code>
└ <b>Uptime:</b> <code>{app_up}</code>"""
        self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_main_menu() if last else None)

    def _cb_all_panels_status(self, chat_id: int) -> None:
        msg = self.bot.send_message(chat_id, "⏳ Получение статуса панелей...")
        panels = list(self.sub.panels)
        for i, panel in enumerate(panels):
            last = i == len(panels) - 1
            try:
                self._cb_panel_info(chat_id, panel, last)
            except Exception as e:
                self.bot.send_message(chat_id, f"❌ <code>{panel.address}:{panel.port} ({panel.name})</code>: {e}", parse_mode="HTML")
        try:
            self.bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass
    def _cb_list_users(self, chat_id: int) -> None:
        users = list(self.cfg['users'].keys())

        if not users:
            self.bot.send_message(chat_id, "Список пользователей пуст.", reply_markup=self.get_main_menu())
            return
        
        text = "👥 <b>Список пользователей:</b>\n\n" + "\n".join([f"- <code>{u}</code>" for u in users])
        self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_main_menu())
    def _cb_online_users(self, chat_id: int) -> None:
        online_users = cast(dict[str, Any], self.sub.get_online_users(new = True))
        if not online_users:
            self.bot.send_message(chat_id, "Нет пользователей в сети.", reply_markup=self.get_main_menu())
            return
        text = "👥 <b>Список пользователей онлайн:</b>\n\n" + "\n".join([f"- <code>{u}</code>{", логин: "+v if v else ""}" for u, v in online_users.items()])
        self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_main_menu())
    def _cb_refresh(self, chat_id: int) -> None:
        try:
            for cc in self.cfg['users'].keys():
                x = self.sub.add_users(cc)
                if x is not None:
                    self.bot.send_message(chat_id, f"Произошла ошибка: {x}")
                    return
            self.bot.send_message(chat_id, "✅ Все пользователи успешно обновлены.", reply_markup=self.get_main_menu())
        except Exception as e:
            self.bot.send_message(chat_id, f"❌ Ошибка: {e}")
 
    def _cb_info_user(self, chat_id: int, username: str) -> None:
        try:
            info = self.sub.get_info(username, pretty=True)
            if not info:
                self.bot.send_message(chat_id, "❌ Пользователь не найден.", reply_markup=self.get_main_menu())
                return

            bw = info['bandwidth']
            up = bw['total']['upload']
            down = bw['total']['download']
            wl_up = bw['wl_total']['upload']
            wl_down = bw['wl_total']['download']
            monthly = bw['monthly']
            wl_monthly = bw['wl_monthly']
            limit = bw['limit']
            wl_limit = bw['wl_limit']
            times = info['time']
            token = info['token']
            displayname = info['displayname']
            status = "🟢 Включен" if info['enabled'] else "🔴 Отключен"
            wl_status = "🟢 Включен" if info['wl_enabled'] else "🔴 Отключен"
            online = "🟢 Да" if info['online'] else "🔴 Нет"
            fingerprint = info['fingerprint']
            if times:
                days_left = str((times - int(time.time())) // 86400)
                date = datetime.fromtimestamp(times, tz=timezone(timedelta(hours=3))).strftime("%d.%m.%y %H:%M")
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
                f"Ссылка: <code>https://pomi.lol/sub?token={token}&lang=ru</code>\n"
            )
            self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_main_menu())
        except Exception as e:
            self.bot.send_message(chat_id, f"❌ Ошибка: {e}", reply_markup=self.get_main_menu())
 
    def _cb_del_user(self, chat_id: int, username: str) -> None:
        if username not in self.cfg['users']:
            self.bot.send_message(chat_id, "❌ Пользователь не найден.", reply_markup=self.get_main_menu())
            return
        try:
            x = self.sub.delete_user(username=username, perma=True)
            if x is not None:
                self.bot.send_message(chat_id, f"Произошла ошибка: {x}")
                return
            self.bot.send_message(chat_id, f"✅ Пользователь <b>{username}</b> удален.", parse_mode="HTML", reply_markup=self.get_main_menu())
        except Exception as e:
            self.bot.send_message(chat_id, f"❌ Ошибка: {e}")
 
    def _step_reset_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return 
        username = text.strip()

        if not self.sub.isuser(username):
            self.bot.send_message(message.chat.id, "❌ Пользователь не найден.", reply_markup=self.get_main_menu())
            return
    
        
        obj = self.sub.reset_user(username)
        if isinstance(obj, str):
            self.bot.send_message(message.chat.id, f"❌ Ошибка сброса: {obj}")
            return
        
        self.bot.send_message(message.chat.id, f"✅ Пользователь был сброшен.\n\nToken: <code>{obj['token']}</code>\nUUID: <code>{obj['uuid']}</code>", parse_mode="HTML", reply_markup=self.get_main_menu())
    def _step_add_user_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return 
        username = text.strip()
        if username in self.cfg['users']:
            self.bot.send_message(message.chat.id, "❌ Этот username уже существует.", reply_markup=self.get_main_menu())
            return
        
        msg = self.bot.send_message(message.chat.id, "Введите DisplayName (Отображаемое имя):")
        self.bot.register_next_step_handler(msg, self._step_add_user_display, username)  # type: ignore[call-arg]
    def _step_add_user_display(self, message: types.Message, username: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        displayname = text.strip()
        
        msg = self.bot.send_message(message.chat.id, "Введите лимит в гигабайтах (или 0 для безлимита):")
        self.bot.register_next_step_handler(msg, self._step_add_user_limit, username, displayname) # type: ignore[call-arg]
    def _step_add_user_limit(self, message: types.Message, username: str, displayname: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            limit = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Ошибка: Лимит должен быть числом.", reply_markup=self.get_main_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Введите кол-во дней подписки (0 для безлимита):")
        self.bot.register_next_step_handler(msg, self._step_add_user_time, username, displayname, limit) # type: ignore[call-arg]
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
            result = self.sub.add_new_user(username=username, displayname=displayname, limit=limit, timee=timee)
            if isinstance(result, str):
                self.bot.send_message(message.chat.id, f"❌ Ошибка: {result}", reply_markup=self.get_main_menu())
                return
            self.bot.send_message(message.chat.id, f"✅ Пользователь <b>{username}</b> успешно добавлен!", parse_mode="HTML", reply_markup=self.get_main_menu())
        except Exception as e:
            self.bot.send_message(message.chat.id, f"❌ Ошибка при добавлении: {e}", reply_markup=self.get_main_menu())
    def _step_info_user(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        username = text.strip()
        self._cb_info_user(message.chat.id, username)
 
    def _cb_list_codes(self, chat_id: int) -> None:
        try:
            codes = self.sub.list_code()
            if not codes:
                self.bot.send_message(chat_id, "Список кодов пуст.", reply_markup=self.get_codes_menu())
                return
            text = "🎟 <b>Список кодов:</b>\n\n" + "\n".join([f"- <code>{c}</code>" for c in codes])
            self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_codes_menu())
        except Exception as e:
            self.bot.send_message(chat_id, f"❌ Ошибка: {e}", reply_markup=self.get_codes_menu())
 
    def _step_info_code(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        code = text.strip()
        try:
            info = self.sub.get_code(code)
            if not info or isinstance(info, bool):
                self.bot.send_message(message.chat.id, "❌ Код не найден.", reply_markup=self.get_codes_menu())
                return
            text = (
                f"ℹ️ <b>Код: <code>{code}</code></b>\n\n"
                f"Тип: <code>{info.get('action', 'N/A')}</code>\n"
                f"Перманентный: <b>{"Да" if info.get('perma', False) else "Нет"}</b>\n"
                f"Дней: <code>{info.get('days', 'N/A')}</code>\n"
                f"Гигабайт: <code>{info.get('gb', 'N/A')}</code>\n"
                f"ВЛ Гигабайт: <code>{info.get('wl_gb', 'N/A')}</code>\n"
            )
            self.bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=self.get_codes_menu())
        except Exception as e:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=self.get_codes_menu())
 
    def _step_del_code(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        code = text.strip()
        try:
            x = self.sub.delete_code(code)
            if x is False:
                self.bot.send_message(message.chat.id, f"❌ Код <code>{code}</code> не существует.", parse_mode="HTML", reply_markup=self.get_codes_menu())
            else:
                self.bot.send_message(message.chat.id, f"✅ Код <code>{code}</code> удалён.", parse_mode="HTML", reply_markup=self.get_codes_menu())
        except Exception as e:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=self.get_codes_menu())
 
    def _step_add_code_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        code_name = text.strip()
        self._pending_codes[message.chat.id] = code_name  
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add( # type: ignore
            types.InlineKeyboardButton("📝 register", callback_data="codetype_register"),
            types.InlineKeyboardButton("🎁 bonus", callback_data="codetype_bonus"),
            types.InlineKeyboardButton("🔙 Отмена", callback_data="codes_menu")
        )
        self.bot.send_message(message.chat.id, f"Код: <b>{code_name}</b>\nВыберите тип:", parse_mode="HTML", reply_markup=markup)
    

    def _step_add_code_days(self, message: types.Message, code_type: str, code_name: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            days = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_codes_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Введите количество гигабайтов (в гб, или 0 для безлимита):")
        self.bot.register_next_step_handler(msg, self._step_add_code_time, code_type, code_name, days) # type: ignore[call-arg]
 
    def _step_add_code_time(self, message: types.Message, code_type: str, code_name: str, days: int) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            gb = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_codes_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Введите количество гигабайтов для ВЛ локаций (в гб, или 0 для безлимита)")
        self.bot.register_next_step_handler(msg, self._step_add_code_wl_time, code_type, code_name, days, gb) # type: ignore[call-arg]
    
    def _step_add_code_wl_time(self, message: types.Message, code_type: str, code_name: str, days: int, gb: int) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        try:
            wl_gb = int(text.strip())
        except ValueError:
            self.bot.send_message(message.chat.id, "❌ Введите число.", reply_markup=self.get_codes_menu())
            return
        msg = self.bot.send_message(message.chat.id, "Перманентный код? Да/Нет:")
        self.bot.register_next_step_handler(msg, self._step_add_code_perma, code_type, code_name, days, gb, wl_gb) # type: ignore[call-arg]
    def _step_add_code_perma(self, message: types.Message, code_type: str, code_name: str, days: int, gb: int, wl_gb: int) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return
        content = text.strip().lower()
        perma = False
        if content == "да":
            perma = True
        elif content == "нет":
            pass
        else:
            self.bot.send_message(message.chat.id, "❌ Неизвестное значение (только да/нет)", reply_markup=self.get_codes_menu())
            return
        try:
            err = self.sub.add_code(
                code=code_name, action=code_type, permanent=perma, 
                days=days, gb=gb, wl_gb=wl_gb
            )
            if isinstance(err, str):
                self.bot.send_message(message.chat.id, f"❌ {err}", reply_markup=self.get_codes_menu())
                return
            self.bot.send_message(
                message.chat.id,
                f"""✅ Код создан!

Код: <code>{code_name}</code>
Тип: <code>{code_type}</code>
Перманентный: <b>{"Да" if perma else "Нет"}</b>
Дней: <code>{days}</code>
Гб: <code>{gb}</code>
ВЛ Гб: <code>{wl_gb}</code>""",
                parse_mode="HTML",
                reply_markup=self.get_codes_menu()
            )
        except Exception as e:
            self.bot.send_message(message.chat.id, f"❌ Ошибка: {e}", reply_markup=self.get_codes_menu())
    def start(self) -> None:
        bot_thread = threading.Thread(target=self.bot.infinity_polling, daemon=True, name="Admin TG Bot") # type: ignore[call-arg]
        bot_thread.start()
    def stop(self):
        try:
            self.msg("⚠️ Shutdown")
        except Exception:
            pass
        self.bot.stop_polling()
class PublicBot:
    """Public telegram bot for end users.
    Dependencies: Subscription
    Classes depending on this: none"""
    def __init__(self, sub: Subscription, cfg: Config):
        self.log = Logger(type(self).__name__)
        with self.log.loading():        
            self.cfg = cfg
            self.sub = sub
            if 'tg_lang' not in self.cfg['publicbot']:
                with self.cfg as data:
                    data['publicbot']['tg_lang'] = {}
    
            token = self.cfg['publicbot'].get('token')
            if not token:
                self.log.critical("public_bot_token not found in config.json! Public bot will not start.")
                return

            self.bot = telebot.TeleBot(token)

            self.TEXTS = self.cfg['publicbot']['lang']

            self.bot.message_handler(commands=['start', 'menu'])(self.cmd_start) # type: ignore
            self.bot.callback_query_handler(func=lambda call: call.data.startswith('lang_'))(self.set_lang_callback) # type: ignore
            self.bot.callback_query_handler(func=lambda call: call.data.startswith('set_'))(self.settings_callback) # type: ignore
            self.bot.callback_query_handler(func=lambda call: call.data.startswith('fp_'))(self.fp_callback) # type: ignore
            self.bot.callback_query_handler(func=lambda call: call.data.startswith('login_'))(self.login_callback) # type: ignore
            self.bot.callback_query_handler(func=lambda call: call.data.startswith('chart_'))(self.chart_callback) # type: ignore
            self.bot.message_handler(func=lambda thisIsAVeryUsefulFunction_pleaseBelieveMe_Hello__whatamidoimg_pleasehelp_iAmGoingToMakeThisLongerEveryCommit: True)(self.handle_text) # type: ignore
  
    def get_lang(self, uid: int) -> str:
        return self.cfg['publicbot']['tg_lang'].get(str(uid), 'ru')

    def set_lang(self, uid: int, lang: str):
        with self.cfg as data:
            data['publicbot']['tg_lang'][str(uid)] = lang
    def msg(self, tgid: int | str | None, key: str, **kwargs: Any) -> None:
        if tgid is None or isinstance(tgid, str):
            return
        lang = self.get_lang(tgid)
        t = self.TEXTS[lang]
        text = t.get(key, None)
        if not text:
            return
        if kwargs:
            text = text.format(**kwargs)
        try: self.bot.send_message(tgid, text, parse_mode="HTML")
        except Exception: pass
    def get_menu(self, uid: int) -> types.ReplyKeyboardMarkup:
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        is_reg = self.sub.is_registered(uid)
        
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        if not is_reg:
            markup.add( # type: ignore
                types.KeyboardButton(t['btn_login'])
            )
        else:
            markup.add( # type: ignore
                types.KeyboardButton(t['btn_main_account']),
                types.KeyboardButton(t['btn_main_sub'])
            )
        markup.add( # type: ignore
                types.KeyboardButton(t['btn_lang']), 
                types.KeyboardButton(t['btn_support'])
        )
        return markup


    def send_info(self, chat_id: int, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        info = self.sub.get_info_telegram(uid)
        if not info:
            return
        daystext = "дней" if lang == 'ru' else "days"  
        limit_str = f"{info['bandwidth']['limit']} GB" if info['bandwidth']['limit'] else t['unlimited']
        wl_limit_str = f"{info['bandwidth']['wl_limit']} GB" if info['bandwidth']['wl_limit'] else t['unlimited']

        monthly_str = info['bandwidth']['monthly']
        wl_monthly_str = info['bandwidth']['wl_monthly']

        if limit_str == t['unlimited']:
            monthly_str = t['unlimited']
        if wl_limit_str == t['unlimited']:
            wl_monthly_str = t['unlimited']
        
        if info['time']:
            days_left = str((info['time'] - int(time.time())) // 86400)
            date_end = datetime.fromtimestamp(info['time'], tz=timezone(timedelta(hours=3))).strftime("%d.%m.%y %H:%M")
            time_str = f"{days_left} {daystext} ({date_end})"
        else:
            time_str = t['lifetime']
            
        status = "🟢" if info['enabled'] else "🔴"
        wl_status = "🟢" if info['wl_enabled'] else "🔴"

        online = "🟢" if info['online'] else "🔴"
        text = t['info_text'].format(
            username=info['displayname'],
            status=status,
            wl_status=wl_status,
            online=online,
            total=info['bandwidth']['total']['total'],
            monthly=monthly_str,
            limit=limit_str,
            wl_total=info['bandwidth']['wl_total']['total'],
            wl_monthly=wl_monthly_str,
            wl_limit=wl_limit_str,
            up=info['bandwidth']['total']['upload'],
            down=info['bandwidth']['total']['download'],
            wl_up=info['bandwidth']['wl_total']['upload'],
            wl_down=info['bandwidth']['wl_total']['download'],
            days=time_str,
            fingerprint=info['fingerprint']
        )
        self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=self.get_menu(uid))


    def cmd_start(self, message: types.Message) -> None:
        uid = cast(types.User, message.from_user).id
        self.bot.clear_step_handler_by_chat_id(message.chat.id)
        
        if str(uid) not in self.cfg['publicbot']['tg_lang']:
            markup = types.InlineKeyboardMarkup()
            markup.add( # type: ignore
                types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
            )
            self.bot.send_message(message.chat.id, "Welcome! Please choose your language:\nДобро пожаловать! Выберите язык:", reply_markup=markup)
        else:
            lang = self.get_lang(uid)
            t = self.TEXTS[lang]
            msg_text = t['welcome_reg'] if self.sub.is_registered(uid) else t['welcome_new']
            self.bot.send_message(message.chat.id, msg_text, reply_markup=self.get_menu(uid))

    def set_lang_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        new_lang = data.split('_', 1)[1]
        self.set_lang(uid, new_lang)
        
        self.bot.answer_callback_query(call.id)
        t = self.TEXTS[new_lang]
        self.bot.send_message(message.chat.id, t['lang_set'], reply_markup=self.get_menu(uid))

        self.bot.delete_message(message.chat.id, message.message_id)

    def handle_text(self, message: types.Message) -> None:
        
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        text = message.text

        
        if text in [self.TEXTS['ru']['btn_info'], self.TEXTS['en']['btn_info']]:
            if not self.sub.is_registered(uid): return
            self.send_info(message.chat.id, uid, lang)
            
        elif text in [self.TEXTS['ru']['btn_bonus'], self.TEXTS['en']['btn_bonus']]:
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['enter_bonus'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_bonus) # type: ignore
            
        elif text in [self.TEXTS['ru']['btn_lang'], self.TEXTS['en']['btn_lang']]:
            markup = types.InlineKeyboardMarkup()
            markup.add( # type: ignore
                types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
            )
            self.bot.send_message(message.chat.id, t['choose_lang'], reply_markup=markup)
        elif text in [self.TEXTS['ru']['btn_login'], self.TEXTS['en']['btn_login']]:
            if self.sub.is_registered(uid): return
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add( # type: ignore
                types.InlineKeyboardButton(t['btn_login_credentials'], callback_data="login_credentials"),
                types.InlineKeyboardButton(t['btn_login_token'], callback_data="login_token")
            )
            self.bot.send_message(message.chat.id, t['choose_login'], reply_markup=markup)
            # msg = self.bot.send_message(message.chat.id, t['enter_email'], reply_markup=types.ReplyKeyboardRemove())
            # self.bot.register_next_step_handler(msg, self.step_login_email)
        elif text in [self.TEXTS['ru']['btn_main_sub'], self.TEXTS['en']['btn_main_sub']]:
            if not self.sub.is_registered(uid): return
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            markup.add( # type: ignore
                types.KeyboardButton(t['btn_main_back']),

                types.KeyboardButton(t['btn_info']),
                types.KeyboardButton(t['btn_get_sub']),
                types.KeyboardButton(t['btn_bonus']),
                types.KeyboardButton(t['btn_reset']),
                types.KeyboardButton(t['btn_chart']),

                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
            )

            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=markup)

        elif text in [self.TEXTS['ru']['btn_main_account'], self.TEXTS['en']['btn_main_account']]:
            if not self.sub.is_registered(uid): return
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            markup.add( # type: ignore
                types.KeyboardButton(t['btn_main_back']),

                types.KeyboardButton(t['btn_settings']),
                types.KeyboardButton(t['btn_logout']),
                types.KeyboardButton(t['btn_help']),
                types.KeyboardButton(t['btn_delete']),

                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
            )

            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=markup)

        elif text in [self.TEXTS['ru']['btn_main_back'], self.TEXTS['en']['btn_main_back']]:
            if not self.sub.is_registered(uid): return
            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=self.get_menu(uid))

        elif text in [self.TEXTS['ru']['btn_reset'], self.TEXTS['en']['btn_reset']]:
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['confirm_reset'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_reset) # type: ignore
        
        elif text in [self.TEXTS['ru']['btn_support'], self.TEXTS['en']['btn_support']]:
            self.bot.send_message(message.chat.id, t['support_text'], parse_mode="HTML")
        
        elif text in [self.TEXTS['ru']['btn_logout'], self.TEXTS['en']['btn_logout']]:
            if not self.sub.is_registered(uid): return
            with self.cfg as d:
                d['tgids'].pop(str(uid), None)
            self.bot.send_message(message.chat.id, t['logout_success'], reply_markup=self.get_menu(uid))

        elif text in [self.TEXTS['ru']['btn_help'], self.TEXTS['en']['btn_help']]:
            if not self.sub.is_registered(uid): return
            t = self.TEXTS[lang]
            text = ""
            for profile, desc in self.cfg['profileDescriptions'].items():
                profile_name = self.cfg['profiles'][profile][0 if lang == "en" else 1]
                profile_desc = desc[0 if lang == "en" else 1]
                text = text + f"<code>{profile_name}</code> — {profile_desc}\n"
            final_text = t['help_text'].format(
                text=text
            )
            self.bot.send_message(message.chat.id, final_text, parse_mode="HTML")

        elif text in [self.TEXTS['ru']['btn_settings'], self.TEXTS['en']['btn_settings']]:
            if not self.sub.is_registered(uid): return
            t = self.TEXTS[lang]
            markup = types.InlineKeyboardMarkup(row_width=2)
            name_label = t['name_label']
            fp_label = t['fp_label']
            pass_label = t['pass_label']
            login_label = t['login_label']
            markup.add( # type: ignore
                types.InlineKeyboardButton(name_label, callback_data="set_name"),
                types.InlineKeyboardButton(fp_label, callback_data="set_fp"),
                types.InlineKeyboardButton(login_label, callback_data="set_login"),
                types.InlineKeyboardButton(pass_label, callback_data="set_pass")
            )
            self.bot.send_message(message.chat.id, t['settings_menu'], parse_mode="HTML", reply_markup=markup)

        elif text in [self.TEXTS['ru']['btn_delete'], self.TEXTS['en']['btn_delete']]:
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['confirm_delete'], parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_delete) # type: ignore

        elif text in [self.TEXTS['ru']['btn_get_sub'], self.TEXTS['en']['btn_get_sub']]:
            if not self.sub.is_registered(uid): return
            self.send_link(message.chat.id, uid, lang)

        elif text in [self.TEXTS['ru']['btn_chart'], self.TEXTS['en']['btn_chart']]:
            if not self.sub.is_registered(uid): return
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add( # type: ignore
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=3), callback_data="chart_3"),
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=14), callback_data="chart_14"),
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=30), callback_data="chart_30"),
                types.InlineKeyboardButton(t['btn_chart_days'].format(days=90), callback_data="chart_90")
            )
            self.bot.send_message(message.chat.id, t['choose_chart_days'], reply_markup=markup)
    def send_link(self, chat_id: int, uid: int, lang: str) -> None:
        t = self.TEXTS[lang]
        info = self.sub.get_info_telegram(uid)
        if not info:
            return

        link = f"https://pomi.lol/{self.cfg['uri']}?token={info['token']}&lang={lang}"
        
        qr = self.sub.make_qr(link)

        text = t['get_sub_text'].format(
            link=link
        )
        
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add( # type: ignore
            types.InlineKeyboardButton(t['get_sub_btn_link'], url=link),
            types.InlineKeyboardButton(t['get_sub_btn_happ'], url=f"https://pomi.lol/{self.cfg['uri']}/redirect?url={urllib.parse.quote(link)}&prefix={urllib.parse.quote("happ://add/")}")
        )
        self.bot.send_photo(chat_id, qr, text, parse_mode="Markdown", reply_markup=markup)
    def login_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if self.sub.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data

        self.bot.answer_callback_query(call.id)

        if action == "login_credentials":
            msg = self.bot.send_message(message.chat.id, t['enter_email'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_login_email) # type: ignore
        elif action == "login_token":
            msg = self.bot.send_message(message.chat.id, t['enter_token'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_login_token) # type: ignore

    def settings_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not self.sub.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data  # set_name, set_fp, set_pass, set_login

        self.bot.answer_callback_query(call.id)
        try: self.bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass

        if action == "set_name":
            msg = self.bot.send_message(message.chat.id, t['settings_name_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_name) # type: ignore
        elif action == "set_fp":
            markup = types.InlineKeyboardMarkup(row_width=2)
            username = self.sub.get_username_telegram(uid)
            current_fp = self.cfg['userFingerprints'].get(username, '')
            for fp in self.cfg['fingerprints']:
                label = f"✅ {fp}" if fp == current_fp else fp
                markup.add(types.InlineKeyboardButton(label, callback_data=f"fp_{fp}")) # type: ignore
            self.bot.send_message(message.chat.id, t['settings_fp_prompt'], reply_markup=markup)
        elif action == "set_login":
            msg = self.bot.send_message(message.chat.id, t['settings_login_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_login) # type: ignore
        elif action == "set_pass":
            msg = self.bot.send_message(message.chat.id, t['settings_pass_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_pass) # type: ignore

    def fp_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not self.sub.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        fp = data[3:]  # strip "fp_"

        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str): return

        self.bot.answer_callback_query(call.id)
        result = self.sub.update_params(username=username, fingerprint=fp)
        if isinstance(result, str):
            self.bot.send_message(message.chat.id, f"❌ {result}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_fp_success'], reply_markup=self.get_menu(uid))
        try: self.bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass

    def step_settings_name(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str): return
        
        new_name = text.strip()
        if len(new_name) > 16:
            self.bot.send_message(message.chat.id, t['length_displayname'].format(ln=16), reply_markup=self.get_menu(uid))
            return

        result = self.sub.update_params(username=username, displayname=new_name)
        if isinstance(result, str):
            self.bot.send_message(message.chat.id, f"❌ {result}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_name_success'], reply_markup=self.get_menu(uid))

    def step_settings_login(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str): return

        new_login = text.strip()
        if len(new_login) > 32:
            self.bot.send_message(message.chat.id, t['length_username'].format(ln=16), reply_markup=self.get_menu(uid))
            return

        result = self.sub.update_params(username=username, ext_username=new_login)
        if isinstance(result, str):
            self.bot.send_message(message.chat.id, f"❌ {result}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_login_success'], reply_markup=self.get_menu(uid))

    def step_settings_pass(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str): return
        if not self.cfg['webui_users'].get(username, None):
            self.bot.send_message(message.chat.id, t['no_account'], reply_markup=self.get_menu(uid))
            return
        new_pass = text.strip()
        try: self.bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass

        # Need current ext_username to update password (update_params requires both)
        ext_username = None
        for email, uname in cast(dict[str, Any], self.cfg.get('webui_users', {})).items():
            if uname == username:
                ext_username = email
                break
        if not ext_username:
            self.bot.send_message(message.chat.id, "❌ No login found", reply_markup=self.get_menu(uid))
            return

        result = self.sub.update_params(username=username, ext_username=ext_username, ext_password=new_pass)
        if isinstance(result, str):
            self.bot.send_message(message.chat.id, f"❌ {result}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_pass_success'], reply_markup=self.get_menu(uid))

    def step_delete(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'):
            return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        confirm = t['delete_confirm_input']
        if text.strip().lower() != confirm.lower():
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return

        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str):
            self.bot.send_message(message.chat.id, "❌ Error", reply_markup=self.get_menu(uid))
            return
        try:
            err = self.sub.delete_user(username=username, perma=True)
            if isinstance(err, str):
                self.log.error(f"Delete error for uid {uid}: {err}")
                self.bot.send_message(message.chat.id, "⚠️ Error", reply_markup=self.get_menu(uid))
                return
            self.bot.send_message(message.chat.id, t['delete_success'], reply_markup=self.get_menu(uid))
        
        except Exception as e:
            self.log.error(f"Delete error for uid {uid}: {e}")
            self.bot.send_message(message.chat.id, "⚠️ Error", reply_markup=self.get_menu(uid))

    def step_login_token(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)

        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        raw = text.strip()

        if '?' in raw:
            qs = urllib.parse.urlparse(raw).query
            token = urllib.parse.parse_qs(qs).get('token', [None])[0]
        else:
            token = raw

        if not token:
            self.bot.send_message(message.chat.id, t['login_fail'], reply_markup=self.get_menu(uid))
            return

        internal_username = self.sub.usertotoken(token)
        if not internal_username:
            self.bot.send_message(message.chat.id, t['login_fail'], reply_markup=self.get_menu(uid))
            return

        with self.cfg as data:
            data.setdefault('tgids', {})[str(uid)] = internal_username

        self.bot.send_message(message.chat.id, t['login_success'], reply_markup=self.get_menu(uid))
        self.send_info(message.chat.id, uid, lang)
    def step_login_email(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)
            
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        email = text.strip()
            
        msg = self.bot.send_message(message.chat.id, t['enter_pass'])
        self.bot.register_next_step_handler(msg, self.step_login_pass, email) # type: ignore
    def step_login_pass(self, message: types.Message, email: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)
        
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        password = text.strip()
            
        try: self.bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass    
        users_pw = cast(dict[str, Any], self.cfg.get('webui_passwords', {}))
        webui_users = cast(dict[str, Any], self.cfg.get('webui_users', {}))
        
        if email in users_pw and users_pw[email] == self.sub.hash(password):
            internal_username = webui_users.get(email)
            if internal_username and internal_username in self.cfg['users']:
                with self.cfg as data:
                    data.setdefault('tgids', {})[str(uid)] = internal_username
                
                self.bot.send_message(message.chat.id, t['login_success'], reply_markup=self.get_menu(uid))
                self.send_info(message.chat.id, uid, lang)
                return
                    
        self.bot.send_message(message.chat.id, t['login_fail'], reply_markup=self.get_menu(uid))

    def step_reset(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        confirm = t['reset_confirm_input'] # string they need to say
        userinput = text.strip().lower()
        if userinput == confirm.lower():
            try:
                username = self.sub.get_username_telegram(uid)
                if not isinstance(username, str):
                    return
                obj = self.sub.reset_user(username)
                if not isinstance(obj, dict):
                    self.bot.send_message(message.chat.id, "Unknown error", reply_markup=self.get_menu(uid))
                    return
                self.bot.send_message(message.chat.id, t['reset_success'], reply_markup=self.get_menu(uid))
            except Exception as e:
                self.log.critical(f"error in reset_user, {e}")
        else:
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return

    def chart_callback(self, call: types.CallbackQuery) -> None:
        data = cast(str, call.data)
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not self.sub.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]

        days = int(data.split('_')[1])
        username = self.sub.get_username_telegram(uid)
        if not isinstance(username, str): return

        self.bot.answer_callback_query(call.id)

        try:
            snapshots = self.sub.get_bw_history(username, days=days)
            info = self.sub.get_info(username, pretty=False)
            if not info or 'bandwidth' not in info:
                self.bot.send_message(message.chat.id, "Error", reply_markup=self.get_menu(uid))
                return

            bandwidths = info['bandwidth']

            upload_fmt = fmt_bytes(bandwidths['total']['upload'])
            download_fmt = fmt_bytes(bandwidths['total']['download'])
            wl_upload_fmt = fmt_bytes(bandwidths['wl_total']['upload'])
            wl_download_fmt = fmt_bytes(bandwidths['wl_total']['download'])

            limit = bandwidths['limit']
            monthly = bandwidths['monthly']

            if limit == 0:
                limit_str = t['unlimited']
                used_str = t['unlimited']
                percent_str = "N/A"
            else:
                limit_str = f"{limit} GB"
                used_str = fmt_bytes(monthly)
                percent_str = f"{int((monthly / (limit * 10**9)) * 100)}%" if monthly > 0 else "0%"

            wl_limit = bandwidths['wl_limit']
            wl_monthly = bandwidths['wl_monthly']

            if wl_limit == 0:
                wl_limit_str = t['unlimited']
                wl_used_str = t['unlimited']
                wl_percent_str = "N/A"
            else:
                wl_limit_str = f"{wl_limit} GB"
                wl_used_str = fmt_bytes(wl_monthly)
                wl_percent_str = f"{int((wl_monthly / (wl_limit * 10**9)) * 100)}%" if wl_monthly > 0 else "0%"

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

            chart_img = bandwidth_chart(snapshots, label=info['displayname'], lang=lang)
            if chart_img is not None:
                self.bot.send_photo(message.chat.id, chart_img, caption=text, parse_mode="HTML", reply_markup=self.get_menu(uid))
            else:
                self.bot.send_message(message.chat.id, text + "\n\n" + t.get('no_data', 'No chart data available'), parse_mode="HTML", reply_markup=self.get_menu(uid))
        except Exception as e:
            self.log.error(f"Chart error for uid {uid}: {e}", exc_info=True)
            self.bot.send_message(message.chat.id, "Error occurred", reply_markup=self.get_menu(uid))

    def step_bonus(self, message: types.Message) -> None:
        text = cast(str, message.text)
        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        
        if text.startswith('/'): 
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return
            
        code = text.strip()
        try:
            res = self.sub.bonus_code(value=uid, code=code)
            if isinstance(res, str):
                if res.lower() == "unknown code":
                    self.bot.send_message(message.chat.id, t['invalid_code'], reply_markup=self.get_menu(uid))
                else:
                    self.bot.send_message(message.chat.id, "Internal Server Error", reply_markup=self.get_menu(uid))
                return
            else:
                self.bot.send_message(message.chat.id, t['bonus_success'], reply_markup=self.get_menu(uid))
                self.send_info(message.chat.id, uid, lang)
        except Exception as e:
             self.log.error(f"Bonus error for uid {uid}: {e}")
             self.bot.send_message(message.chat.id, "⚠️ Error occurred", reply_markup=self.get_menu(uid))

    def start(self) -> None:
        if hasattr(self, 'bot'):
            bot_thread = threading.Thread(target=self.bot.infinity_polling, daemon=True, name="Public TG Bot") # type: ignore
            bot_thread.start()

    def stop(self):
        self.bot.stop_polling()

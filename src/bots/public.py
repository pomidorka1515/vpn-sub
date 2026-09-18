import telebot
import urllib.parse
import time
import logging
import threading

from telebot import types
from typing import Any, cast, Callable
from collections.abc import Mapping
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from core import Subscription
from config import ConfigLike
from loggers import Logger
from util import format_usage, fmt_bytes, truncate_utf8
from errors import AppError
from chart import bandwidth_chart

from .mixins import TelegramPollingMixin

# pyright: reportUnknownMemberType=false

class PublicBot(TelegramPollingMixin):
    """Public telegram bot for end users.
    Dependencies: Subscription
    Classes depending on this: none"""
    def __init__(self, sub: Subscription, cfg: ConfigLike, lang_cfg: ConfigLike):
        self.log = Logger(type(self).__name__)
        with self.log.loading():
            self.cfg: ConfigLike = cfg
            self.lang_cfg: ConfigLike = lang_cfg
            self.sub: Subscription = sub
            token: str = self.cfg['publicbot'].get('token')
            if not token:
                raise RuntimeError("public bot token not found in config.json")

            self.bot = telebot.TeleBot(token)

            self.TEXTS: dict[str, dict[str, str]] = lang_cfg['publicbot']

            # NOTE: dumbass telebot has trash typing
            self.bot.message_handler(commands=['start', 'menu'])(self.cmd_start)
            callbacks: tuple[tuple[str, Callable[..., Any]], ...] = (
                ('lang_', self.set_lang_callback),
                ('set_', self.settings_callback),
                ('fp_', self.fp_callback),
                ('login_', self.login_callback),
                ('chart_', self.chart_callback),
            )
            for prefix, handler in callbacks:
                self.bot.callback_query_handler(func=lambda call, p=prefix: call.data.startswith(p))(handler) # pyright: ignore[reportUnknownLambdaType]
            
            self.bot.message_handler(func=lambda g: True)(self.handle_text) # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]

            self._executor = ThreadPoolExecutor(max_workers=15, thread_name_prefix=f"{type(self).__name__}-chart")

            self.polling_thread: threading.Thread | None = None

    def _send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        try:
            self.bot.send_message(chat_id, text, **cast(Any, kwargs))
        except Exception as error:
            self.log.error(f"failed to send message to chat {chat_id}: {error}", exc_info=True)

    def _delete_message(self, chat_id: int, message_id: int, *, secret: bool = False) -> None:
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception as error:
            level = logging.CRITICAL if secret else logging.ERROR
            prefix = "secret" if secret else "message"
            self.log.log(
                level,
                f"failed to delete {prefix} {message_id} in chat {chat_id}: {error}",
                exc_info=True,
            )
            if secret:
                self._send_message(chat_id, "⚠️ Не удалось удалить ваше сообщение с секретом.")

    def get_lang(self, uid: int) -> str:
        return self.sub.get_telegram_language(uid)

    def set_lang(self, uid: int, lang: str) -> None:
        self.sub.set_telegram_language(uid, lang)

    def msg(self, tgid: int | str | None, key: str, **kwargs: str | int | float | bool) -> None:
        if tgid is None or isinstance(tgid, str):
            return
        lang = self.get_lang(tgid)
        t = self.TEXTS[lang]
        text = t.get(key, None)
        if not text:
            return
        if kwargs:
            text = text.format(**kwargs)
        try:
            self.bot.send_message(tgid, text, parse_mode="HTML")
        except Exception as error:
            self.log.error(
                f"failed to send notification {key!r} to user {tgid}: {error}",
                exc_info=True,
            )
    def get_menu(self, uid: int) -> types.ReplyKeyboardMarkup:
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        is_reg = self.sub.is_registered(uid)

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        if not is_reg:
            markup.add(
                types.KeyboardButton(t['btn_login'])
            )
        else:
            markup.add(
                types.KeyboardButton(t['btn_main_account']),
                types.KeyboardButton(t['btn_main_sub'])
            )
        markup.add(
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


    def cmd_start(self, message: types.Message) -> None:
        uid = cast(types.User, message.from_user).id
        self.bot.clear_step_handler_by_chat_id(message.chat.id)

        if not self.sub.has_telegram_language(uid):
            markup = types.InlineKeyboardMarkup()
            markup.add(
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

        self._delete_message(message.chat.id, message.message_id)


    def handle_text(self, message: types.Message) -> None:

        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        text = message.text


        if text in (self.TEXTS['ru']['btn_info'], self.TEXTS['en']['btn_info']):
            if not self.sub.is_registered(uid): return
            self.send_info(message.chat.id, uid, lang)

        elif text in (self.TEXTS['ru']['btn_bonus'], self.TEXTS['en']['btn_bonus']):
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['enter_bonus'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_bonus)

        elif text in (self.TEXTS['ru']['btn_lang'], self.TEXTS['en']['btn_lang']):
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
            )
            self.bot.send_message(message.chat.id, t['choose_lang'], reply_markup=markup)
        elif text in (self.TEXTS['ru']['btn_login'], self.TEXTS['en']['btn_login']):
            if self.sub.is_registered(uid): return
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton(t['btn_login_credentials'], callback_data="login_credentials"),
                types.InlineKeyboardButton(t['btn_login_token'], callback_data="login_token")
            )
            self.bot.send_message(message.chat.id, t['choose_login'], reply_markup=markup)
            # msg = self.bot.send_message(message.chat.id, t['enter_email'], reply_markup=types.ReplyKeyboardRemove())
            # self.bot.register_next_step_handler(msg, self.step_login_email)
        elif text in (self.TEXTS['ru']['btn_main_sub'], self.TEXTS['en']['btn_main_sub']):
            if not self.sub.is_registered(uid): return
            reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            reply_markup.add(
                types.KeyboardButton(t['btn_main_back']),

                types.KeyboardButton(t['btn_info']),
                types.KeyboardButton(t['btn_get_sub']),
                types.KeyboardButton(t['btn_bonus']),
                types.KeyboardButton(t['btn_reset']),
                types.KeyboardButton(t['btn_chart']),

                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
            )

            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=reply_markup)

        elif text in (self.TEXTS['ru']['btn_main_account'], self.TEXTS['en']['btn_main_account']):
            if not self.sub.is_registered(uid): return
            reply_markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            reply_markup.add(
                types.KeyboardButton(t['btn_main_back']),

                types.KeyboardButton(t['btn_settings']),
                types.KeyboardButton(t['btn_logout']),
                types.KeyboardButton(t['btn_help']),
                types.KeyboardButton(t['btn_delete']),

                types.KeyboardButton(t['btn_lang']),
                types.KeyboardButton(t['btn_support'])
            )

            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=reply_markup)

        elif text in (self.TEXTS['ru']['btn_main_back'], self.TEXTS['en']['btn_main_back']):
            if not self.sub.is_registered(uid): return
            self.bot.send_message(message.chat.id, t['welcome_reg'], reply_markup=self.get_menu(uid))

        elif text in (self.TEXTS['ru']['btn_reset'], self.TEXTS['en']['btn_reset']):
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['confirm_reset'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_reset)

        elif text in (self.TEXTS['ru']['btn_support'], self.TEXTS['en']['btn_support']):
            self.bot.send_message(message.chat.id, t['support_text'], parse_mode="HTML")

        elif text in (self.TEXTS['ru']['btn_logout'], self.TEXTS['en']['btn_logout']):
            if not self.sub.is_registered(uid): return
            self.sub.set_telegram_user(uid, None)
            self.bot.send_message(message.chat.id, t['logout_success'], reply_markup=self.get_menu(uid))

        elif text in (self.TEXTS['ru']['btn_help'], self.TEXTS['en']['btn_help']):
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

        elif text in (self.TEXTS['ru']['btn_settings'], self.TEXTS['en']['btn_settings']):
            if not self.sub.is_registered(uid): return
            t = self.TEXTS[lang]
            markup = types.InlineKeyboardMarkup(row_width=2)
            name_label = t['name_label']
            fp_label = t['fp_label']
            pass_label = t['pass_label']
            login_label = t['login_label']
            markup.add(
                types.InlineKeyboardButton(name_label, callback_data="set_name"),
                types.InlineKeyboardButton(fp_label, callback_data="set_fp"),
                types.InlineKeyboardButton(login_label, callback_data="set_login"),
                types.InlineKeyboardButton(pass_label, callback_data="set_pass")
            )
            self.bot.send_message(message.chat.id, t['settings_menu'], parse_mode="HTML", reply_markup=markup)

        elif text in (self.TEXTS['ru']['btn_delete'], self.TEXTS['en']['btn_delete']):
            if not self.sub.is_registered(uid): return
            msg = self.bot.send_message(message.chat.id, t['confirm_delete'], parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_delete)

        elif text in (self.TEXTS['ru']['btn_get_sub'], self.TEXTS['en']['btn_get_sub']):
            if not self.sub.is_registered(uid): return
            self.send_link(message.chat.id, uid, lang)

        elif text in (self.TEXTS['ru']['btn_chart'], self.TEXTS['en']['btn_chart']):
            if not self.sub.is_registered(uid): return
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
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

        sub_uri = self.cfg['uri'].strip("/")
        domain = self.cfg['domain'].rstrip("/")
        
        link = f"{domain}/{sub_uri}?token={info.token}&lang={lang}"
            
        qr = self.sub.make_qr(link)

        text = t['get_sub_text'].format(
            link=link
        )

        domain = self.cfg['domain']
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(t['get_sub_btn_link'], url=link),
            types.InlineKeyboardButton(t['get_sub_btn_happ'], url=f"{domain}/{sub_uri}/redirect?url={urllib.parse.quote(link)}&prefix={urllib.parse.quote("happ://add/")}")
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
            self.bot.register_next_step_handler(msg, self.step_login_email)
        elif action == "login_token":
            msg = self.bot.send_message(message.chat.id, t['enter_token'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_login_token)

    def settings_callback(self, call: types.CallbackQuery) -> None:
        message = cast(types.Message, call.message)
        uid = call.from_user.id
        if not self.sub.is_registered(uid): return
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        action = call.data  # set_name, set_fp, set_pass, set_login

        self.bot.answer_callback_query(call.id)
        self._delete_message(message.chat.id, message.message_id)

        if action == "set_name":
            msg = self.bot.send_message(message.chat.id, t['settings_name_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_name)
        elif action == "set_fp":
            markup = types.InlineKeyboardMarkup(row_width=2)
            username = self.sub.get_username_telegram(uid)
            current_fp = self.sub.get_fingerprint(username) if isinstance(username, str) else ''
            for fp in self.cfg['fingerprints']:
                label = f"✅ {fp}" if fp == current_fp else fp
                markup.add(types.InlineKeyboardButton(label, callback_data=f"fp_{fp}"))
            self.bot.send_message(message.chat.id, t['settings_fp_prompt'], reply_markup=markup)
        elif action == "set_login":
            msg = self.bot.send_message(message.chat.id, t['settings_login_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_login)
        elif action == "set_pass":
            msg = self.bot.send_message(message.chat.id, t['settings_pass_prompt'], reply_markup=types.ReplyKeyboardRemove())
            self.bot.register_next_step_handler(msg, self.step_settings_pass)

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
        try:
            self.sub.update_params(username=username, fingerprint=fp)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['settings_fp_success'], reply_markup=self.get_menu(uid))
        self._delete_message(message.chat.id, message.message_id)

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

        try:
            self.sub.update_params(username=username, displayname=new_name)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
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

        try:
            self.sub.update_params(username=username, ext_username=new_login)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
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
        if not self.sub.get_external_username(username):
            self.bot.send_message(message.chat.id, t['no_account'], reply_markup=self.get_menu(uid))
            return
        new_pass = text.strip()
        self._delete_message(message.chat.id, message.message_id, secret=True)

        # Need current ext_username to update password (update_params requires both)
        ext_username = self.sub.get_external_username(username)
        if not ext_username:
            self.bot.send_message(message.chat.id, "❌ No login found", reply_markup=self.get_menu(uid))
            return

        try:
            self.sub.update_params(username=username, ext_username=ext_username, ext_password=new_pass)
        except AppError as error:
            self.bot.send_message(message.chat.id, f"❌ {error.message}", reply_markup=self.get_menu(uid))
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
            self.sub.delete_user(username=username, perma=True)
            self.bot.send_message(message.chat.id, t['delete_success'], reply_markup=self.get_menu(uid))

        except AppError as error:
            self._send_message(message.chat.id, error.message, reply_markup=self.get_menu(uid))
            return
        except Exception as error:
            self.log.error(f"Delete error for uid {uid}: {error}", exc_info=True)
            self._send_message(message.chat.id, "⚠️ Error", reply_markup=self.get_menu(uid))

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

        self.sub.set_telegram_user(uid, internal_username)

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
        self.bot.register_next_step_handler(msg, self.step_login_pass, email)
    def step_login_pass(self, message: types.Message, email: str) -> None:
        text = cast(str, message.text)
        if text.startswith('/'): return self.cmd_start(message)

        uid = cast(types.User, message.from_user).id
        lang = self.get_lang(uid)
        t = self.TEXTS[lang]
        password = text.strip()

        self._delete_message(message.chat.id, message.message_id, secret=True)

        internal_username = self.sub.validate_credentials(email, password)
        if internal_username:
            self.sub.set_telegram_user(uid, internal_username)
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
                self.sub.reset_user(username)
                self.bot.send_message(message.chat.id, t['reset_success'], reply_markup=self.get_menu(uid))
            except AppError as error:
                self._send_message(message.chat.id, error.message, reply_markup=self.get_menu(uid))
            except Exception:
                self.log.critical("reset_user failed", exc_info=True)
                self._send_message(message.chat.id, t.get("error_generic", "⚠️ Error"), reply_markup=self.get_menu(uid))
        else:
            self.bot.send_message(message.chat.id, t['cancelled'], reply_markup=self.get_menu(uid))
            return

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
            self.sub.bonus_code(value=uid, code=code)
            self.bot.send_message(message.chat.id, t['bonus_success'], reply_markup=self.get_menu(uid))
            self.send_info(message.chat.id, uid, lang)
        except AppError:
            self._send_message(message.chat.id, t['invalid_code'], reply_markup=self.get_menu(uid))
        except Exception:
            self.log.error(f"Bonus error for uid {uid}", exc_info=True)
            self._send_message(message.chat.id, "⚠️ Error occurred", reply_markup=self.get_menu(uid))

    def start(self) -> None:
        self.start_polling()

    def stop(self) -> None:
        self.stop_polling()
        self._executor.shutdown(wait=False, cancel_futures=True)

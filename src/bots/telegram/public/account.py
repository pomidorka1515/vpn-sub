"""Public account lifecycle workflows."""

from __future__ import annotations

from typing import cast

from telebot import types

from errors import AppError

from ..composition import PublicFeatureMixin

__all__ = ["PublicAccountMixin"]


class PublicAccountMixin(PublicFeatureMixin):
    """Reset, deletion, logout, and bonus workflows."""

    async def step_delete(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]

        confirm = t["delete_confirm_input"]
        if text.strip().lower() != confirm.lower():
            await self.bot.send_message(
                message.chat.id,
                t["cancelled"],
                reply_markup=await self.get_menu(uid),
            )
            return None

        username = await self.sub.telegram_svc.get_username_telegram(uid)
        if not isinstance(username, str):
            await self.bot.send_message(
                message.chat.id,
                "❌ Error",
                reply_markup=await self.get_menu(uid),
            )
            return None
        try:
            await self.sub.business_svc.delete_user(username=username, perma=True)
            await self.bot.send_message(
                message.chat.id,
                t["delete_success"],
                reply_markup=await self.get_menu(uid),
            )

        except AppError as error:
            await self._send_message(
                message.chat.id,
                error.message,
                reply_markup=await self.get_menu(uid),
            )
            return None
        except Exception:
            self.log.exception("Delete error for uid %s", uid)
            await self._send_message(
                message.chat.id,
                "⚠️ Error",
                reply_markup=await self.get_menu(uid),
            )

    async def step_reset(self, message: types.Message) -> None:
        text = cast(str, message.text)
        if text.startswith("/"):
            return await self.cmd_start(message)
        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]

        confirm = t["reset_confirm_input"]  # string they need to say
        userinput = text.strip().lower()
        if userinput == confirm.lower():
            try:
                username = await self.sub.telegram_svc.get_username_telegram(uid)
                if not isinstance(username, str):
                    return None
                await self.sub.business_svc.reset_user(username)
                await self.bot.send_message(
                    message.chat.id,
                    t["reset_success"],
                    reply_markup=await self.get_menu(uid),
                )
            except AppError as error:
                await self._send_message(
                    message.chat.id,
                    error.message,
                    reply_markup=await self.get_menu(uid),
                )
            except Exception:
                self.log.critical("reset_user failed", exc_info=True)
                await self._send_message(
                    message.chat.id,
                    t["error_generic"],
                    reply_markup=await self.get_menu(uid),
                )
        else:
            await self.bot.send_message(
                message.chat.id,
                t["cancelled"],
                reply_markup=await self.get_menu(uid),
            )
            return None

    async def step_bonus(self, message: types.Message) -> None:
        text = cast(str, message.text)
        uid = cast(types.User, message.from_user).id
        lang = await self.get_lang(uid)
        t = self.TEXTS[lang]

        if text.startswith("/"):
            await self.bot.send_message(
                message.chat.id,
                t["cancelled"],
                reply_markup=await self.get_menu(uid),
            )
            return

        code = text.strip()
        try:
            await self.sub.telegram_svc.bonus_code(value=uid, code=code)
            await self.bot.send_message(
                message.chat.id,
                t["bonus_success"],
                reply_markup=await self.get_menu(uid),
            )
            await self.send_info(message.chat.id, uid, lang)
        except AppError:
            await self._send_message(
                message.chat.id,
                t["invalid_code"],
                reply_markup=await self.get_menu(uid),
            )
        except Exception:
            self.log.exception(f"Bonus error for uid {uid}")
            await self._send_message(
                message.chat.id,
                t["error_generic"],
                reply_markup=await self.get_menu(uid),
            )

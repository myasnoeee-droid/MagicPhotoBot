# payments/stars_v3.py
# -*- coding: utf-8 -*-
# Telegram Stars (XTR) payments for aiogram v3 + SQLite persistence
import os
import sqlite3
import time
from typing import Tuple

from aiogram import Router, F, Bot
from aiogram.dispatcher.dispatcher import Dispatcher
from aiogram.types import (
    Message, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery
)

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "stars.db")
os.makedirs(DB_DIR, exist_ok=True)

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_cur = _conn.cursor()
_cur.execute("""
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  credits INTEGER DEFAULT 0,
  stars_topup_total INTEGER DEFAULT 0,
  day_unlimited_until INTEGER DEFAULT 0
);
""")
_conn.commit()

def _get_user(user_id: int) -> Tuple[int, int, int, int]:
    _cur.execute("SELECT user_id, credits, stars_topup_total, day_unlimited_until FROM users WHERE user_id=?", (user_id,))
    row = _cur.fetchone()
    if not row:
        _cur.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,))
        _conn.commit()
        return (user_id, 0, 0, 0)
    return row

def add_credits(user_id: int, amount: int, stars_paid: int):
    _get_user(user_id)
    _cur.execute(
        "UPDATE users SET credits = credits + ?, stars_topup_total = stars_topup_total + ? WHERE user_id=?",
        (amount, stars_paid, user_id)
    )
    _conn.commit()

def set_unlimited_day(user_id: int, hours: int = 24):
    until = int(time.time()) + hours * 3600
    _cur.execute("UPDATE users SET day_unlimited_until=? WHERE user_id=?", (until, user_id))
    _conn.commit()

def has_unlimited(user_id: int) -> bool:
    _, _, _, until = _get_user(user_id)
    return int(time.time()) < int(until or 0)

def spend_credit(user_id: int) -> bool:
    # Безлимит на сутки — кредит не тратим
    if has_unlimited(user_id):
        return True
    uid, credits, _, _ = _get_user(user_id)
    if credits > 0:
        _cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id=?", (user_id,))
        _conn.commit()
        return True
    return False

def get_balance(user_id: int) -> int:
    _, credits, _, _ = _get_user(user_id)
    return int(credits or 0)

# ---------------- UI ----------------
def buy_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("💫 Купить оживления (⭐)", callback_data="buy_menu"),
        InlineKeyboardButton("💰 Баланс", callback_data="balance")
    )
    return kb

def _buy_packs_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("1 оживление — 150⭐", callback_data="buy_1"),
        InlineKeyboardButton("5 оживлений — 600⭐", callback_data="buy_5"),
        InlineKeyboardButton("Безлимит на сутки — 1200⭐", callback_data="buy_day")
    )
    return kb

# ------------- PAYMENTS -------------
PACKS = {
    "buy_1":   {"stars": 150,  "credits": 1,    "title": "1 оживление"},
    "buy_5":   {"stars": 600,  "credits": 5,    "title": "5 оживлений"},
    "buy_day": {"stars": 1200, "credits": 0,    "title": "Безлимит на сутки", "unlimited": True}
}

async def _send_stars_invoice(bot: Bot, chat_id: int, pack_key: str):
    pack = PACKS[pack_key]
    await bot.send_invoice(
        chat_id=chat_id,
        title=pack["title"],
        description=f"Оплата в Telegram Stars (XTR). Пакет: {pack['title']}",
        payload=f"stars::{pack_key}::v1",
        currency="XTR",  # ⭐
        # ВАЖНО: для Stars должен быть РОВНО один LabeledPrice
        prices=[LabeledPrice(label=pack["title"], amount=pack["stars"])],
        start_parameter="stars-payment",
        provider_token=""  # для Stars пустая строка ОК
    )

# ------------- REGISTRATION ----------
def register_stars_payments(dp: Dispatcher, bot: Bot):
    """
    Регистрирует все хэндлеры оплаты в общий Dispatcher aiogram v3.
    Вызывать из app.py перед start_polling.
    """
    router = Router(name="stars-payments")

    @router.message(F.text.in_({"/buy", "buy"}))
    async def cmd_buy(message: Message):
        await message.answer("Выберите пакет:", reply_markup=_buy_packs_kb())

    @router.message(F.text.in_({"/balance", "balance"}))
    async def cmd_balance(message: Message):
        bal = get_balance(message.from_user.id)
        ul = "да" if has_unlimited(message.from_user.id) else "нет"
        await message.answer(
            f"💰 Баланс\n• Кредиты: {bal}\n• Безлимит на сутки: {ul}",
            reply_markup=buy_menu_kb()
        )

    @router.callback_query(F.data == "balance")
    async def cb_balance(call):
        bal = get_balance(call.from_user.id)
        ul = "да" if has_unlimited(call.from_user.id) else "нет"
        await call.message.edit_text(
            f"💰 Баланс\n• Кредиты: {bal}\n• Безлимит на сутки: {ul}",
            reply_markup=buy_menu_kb()
        )
        await call.answer()

    @router.callback_query(F.data == "buy_menu")
    async def cb_buy_menu(call):
        await call.message.edit_text("Выберите пакет:", reply_markup=_buy_packs_kb())
        await call.answer()

    @router.callback_query(F.data.in_(PACKS.keys()))
    async def cb_buy_pack(call):
        await _send_stars_invoice(bot, call.from_user.id, call.data)
        await call.answer()

    @router.pre_checkout_query()
    async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery):
        # Для Stars просто подтверждаем
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

    @router.message(F.successful_payment)
    async def on_successful_payment(message: Message):
        sp = message.successful_payment
        payload = sp.invoice_payload or ""
        currency = sp.currency  # "XTR"
        stars_amount = sp.total_amount  # целое число ⭐
        try:
            _, pack_key, _ = payload.split("::")
        except Exception:
            pack_key = "buy_1"
        pack = PACKS.get(pack_key, PACKS["buy_1"])
        if pack.get("unlimited"):
            set_unlimited_day(message.from_user.id, hours=24)
            text = f"✅ Оплата успешна!\nВы купили: {pack['title']}\nСписано: {stars_amount} {currency}\nБезлимит на сутки активирован."
        else:
            add_credits(message.from_user.id, pack["credits"], stars_amount)
            text = f"✅ Оплата успешна!\nВы купили: {pack['title']}\nСписано: {stars_amount} {currency}\nНачислено {pack['credits']} кредит(ов)."
        await message.answer(text, reply_markup=buy_menu_kb())

    dp.include_router(router)

__all__ = [
    "register_stars_payments",
    "spend_credit",
    "buy_menu_kb",
    "get_balance",
]

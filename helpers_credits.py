# helpers_credits.py
from db import get_credits_balance, add_credits, consume_credit


async def get_user_credits(uid: int) -> int:
    """Аналог user_credits.get(uid, 0), но из Postgres."""
    return await get_credits_balance(uid)


async def add_user_credits(uid: int, amount: int, reason: str) -> int:
    """
    Аналог:
        user_credits[uid] = user_credits.get(uid, 0) + amount
    Возвращает новый баланс.
    """
    return await add_credits(uid, amount, reason)


async def consume_user_credit(uid: int, amount: int = 1) -> tuple[bool, int]:
    """
    Аналог:
        user_credits[uid] -= amount
    Но безопасно, не уйдём в минус.
    """
    return await consume_credit(uid, amount=amount)

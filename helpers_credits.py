from db import (
    get_credits_balance,
    add_credits,
    consume_credit,
)


async def get_user_credits(uid: int) -> int:
    return await get_credits_balance(uid)


async def add_user_credits(uid: int, amount: int, reason: str) -> int:
    return await add_credits(uid, amount, reason)


async def consume_user_credit(uid: int, amount: int = 1) -> tuple[bool, int]:
    """
    Возвращает (успех, новый_баланс)
    """
    return await consume_credit(uid, amount)

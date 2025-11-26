# db.py
import os
import asyncpg
from typing import Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

_pool: asyncpg.Pool | None = None


# ============ ИНИЦИАЛИЗАЦИЯ БАЗЫ ============

async def init_db():
    """
    Создаём пул соединений и при желании — таблицы (они у тебя уже есть,
    но CREATE TABLE IF NOT EXISTS безопасен, можно оставить).
    """
    global _pool
    if _pool is not None:
        return

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in .env")

    _pool = await asyncpg.create_pool(DATABASE_URL)

    async with _pool.acquire() as conn:
        # Твоё создание таблиц (можно вообще удалить этот блок, если уже всё создано)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                lang VARCHAR(10) NOT NULL DEFAULT 'en',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                inviter_id BIGINT NOT NULL,
                invited_id BIGINT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS credits (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount INT NOT NULL,
                reason VARCHAR(50) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS usage_stats (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                used_free INT NOT NULL DEFAULT 0,
                last_used_at TIMESTAMPTZ
            );

            CREATE TABLE IF NOT EXISTS ref_stars (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                stars_accumulated INT NOT NULL DEFAULT 0,
                stars_balance INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS ref_pushes (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                last_push_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )


async def close_db():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        raise RuntimeError("DB pool is not initialized. Call init_db() first.")
    return _pool


# ============ USERS ============

async def ensure_user(tg_id: int) -> None:
    """
    Гарантируем, что пользователь есть в таблице users.
    Язык из БД не используем в логике — он тебе не нужен.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM users WHERE tg_id = $1",
            tg_id,
        )
        if row:
            return

        await conn.execute(
            """
            INSERT INTO users (tg_id)
            VALUES ($1)
            ON CONFLICT (tg_id) DO NOTHING;
            """,
            tg_id,
        )


# ============ РЕФЕРАЛЬНАЯ СИСТЕМА ============

async def register_referral(inviter_id: int, invited_id: int) -> bool:
    """
    Регистрируем рефералку: inviter_id пригласил invited_id.
    inviter_id и invited_id — это tg_id.

    Возвращает True, если запись добавлена впервые.
    False, если реферал уже был (invited_id уникален).
    """
    if inviter_id == invited_id:
        return False  # защита от самореферала

    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                INSERT INTO referrals (inviter_id, invited_id)
                VALUES ($1, $2);
                """,
                inviter_id,
                invited_id,
            )
            return True
        except asyncpg.UniqueViolationError:
            # invited_id уже есть в таблице
            return False


# ============ КРЕДИТЫ (АНИМАЦИИ) ЧЕРЕЗ ТАБЛИЦУ credits ============

async def get_credits_balance(tg_id: int) -> int:
    """
    Считаем баланс анимаций как сумму amount из credits по user_id = tg_id.
    amount > 0 — начисление, < 0 — списание.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount), 0) AS balance
            FROM credits
            WHERE user_id = $1;
            """,
            tg_id,
        )
        return int(row["balance"]) if row else 0


async def add_credits(tg_id: int, amount: int, reason: str) -> int:
    """
    Добавляем запись в credits и возвращаем новый баланс.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO credits (user_id, amount, reason)
            VALUES ($1, $2, $3);
            """,
            tg_id,
            amount,
            reason,
        )
        # получаем новый баланс
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount), 0) AS balance
            FROM credits
            WHERE user_id = $1;
            """,
            tg_id,
        )
        return int(row["balance"]) if row else 0


async def consume_credit(tg_id: int, amount: int = 1) -> Tuple[bool, int]:
    """
    Пытаемся списать amount анимаций.
    Возвращаем (успех, новый_баланс).
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        # посчитаем текущий баланс
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount), 0) AS balance
            FROM credits
            WHERE user_id = $1
            FOR UPDATE;
            """,
            tg_id,
        )
        current = int(row["balance"]) if row else 0

        if current < amount:
            return False, current

        # списываем (amount -> запись с отрицательным значением)
        await conn.execute(
            """
            INSERT INTO credits (user_id, amount, reason)
            VALUES ($1, $2, $3);
            """,
            tg_id,
            -amount,
            "spend_generation",
        )

        new_bal = current - amount
        return True, new_bal


# ============ БЕСПЛАТНОЕ ОЖИВЛЕНИЕ ЧЕРЕЗ usage_stats ============

async def has_used_free(tg_id: int) -> bool:
    """
    Проверяем, использовал ли пользователь бесплатное оживление.
    Логика: used_free > 0 = уже использовал.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT used_free
            FROM usage_stats
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT 1;
            """,
            tg_id,
        )
        if not row:
            return False
        return int(row["used_free"]) > 0


async def mark_free_used(tg_id: int) -> None:
    """
    Отмечаем, что free-анимация была использована.
    Если запись уже есть — обновляем.
    Если нет — создаём.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE usage_stats
            SET used_free = 1,
                last_used_at = NOW()
            WHERE user_id = $1;
            """,
            tg_id,
        )
        # result выглядит как 'UPDATE 0' или 'UPDATE 1'
        if result == "UPDATE 0":
            await conn.execute(
                """
                INSERT INTO usage_stats (user_id, used_free, last_used_at)
                VALUES ($1, 1, NOW());
                """,
                tg_id,
            )


# ============ REF_STARS (реферальные звезды) ============

async def get_ref_stars(tg_id: int) -> Tuple[int, int]:
    """
    Возвращает (stars_accumulated, stars_balance) для пользователя.
    Если записи нет — создаём её с нулями.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT stars_accumulated, stars_balance
            FROM ref_stars
            WHERE user_id = $1;
            """,
            tg_id,
        )
        if row:
            return int(row["stars_accumulated"]), int(row["stars_balance"])

        # создаём запись
        await conn.execute(
            """
            INSERT INTO ref_stars (user_id, stars_accumulated, stars_balance)
            VALUES ($1, 0, 0);
            """,
            tg_id,
        )
        return 0, 0


async def add_ref_stars(tg_id: int, stars_delta: int) -> Tuple[int, int]:
    """
    Изменяем реферальный баланс в ref_stars.
    stars_delta > 0 — начисление; звезды копятся в accumulated и balance.
    stars_delta < 0 — списание из balance.
    Возвращаем (новый_accumulated, новый_balance).
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT stars_accumulated, stars_balance
            FROM ref_stars
            WHERE user_id = $1
            FOR UPDATE;
            """,
            tg_id,
        )

        if not row:
            # создаём запись
            await conn.execute(
                """
                INSERT INTO ref_stars (user_id, stars_accumulated, stars_balance)
                VALUES ($1, 0, 0);
                """,
                tg_id,
            )
            current_acc = 0
            current_bal = 0
        else:
            current_acc = int(row["stars_accumulated"])
            current_bal = int(row["stars_balance"])

        if stars_delta >= 0:
            new_acc = current_acc + stars_delta
        else:
            new_acc = current_acc  # накопительные не уменьшаем

        new_bal = current_bal + stars_delta

        await conn.execute(
            """
            UPDATE ref_stars
            SET stars_accumulated = $2,
                stars_balance = $3
            WHERE user_id = $1;
            """,
            tg_id,
            new_acc,
            new_bal,
        )

        return new_acc, new_bal

import os
import asyncpg
from typing import Tuple
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
_pool: asyncpg.Pool | None = None


async def init_db():
    global _pool
    if _pool is not None:
        return

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in .env")

    _pool = await asyncpg.create_pool(DATABASE_URL)

    async with _pool.acquire() as conn:
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

            -- Индексы
            CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users(tg_id);
            CREATE INDEX IF NOT EXISTS idx_credits_user_id ON credits(user_id);
            CREATE INDEX IF NOT EXISTS idx_usage_stats_user_id ON usage_stats(user_id);
            CREATE INDEX IF NOT EXISTS idx_ref_stars_user_id ON ref_stars(user_id);
            CREATE INDEX IF NOT EXISTS idx_ref_pushes_user_id ON ref_pushes(user_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_invited_id ON referrals(invited_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_inviter_id ON referrals(inviter_id);
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


async def ensure_user(tg_id: int) -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE tg_id=$1", tg_id)
        if row:
            return
        await conn.execute(
            "INSERT INTO users (tg_id) VALUES ($1) ON CONFLICT (tg_id) DO NOTHING",
            tg_id,
        )


async def register_referral(inviter_id: int, invited_id: int) -> bool:
    if inviter_id == invited_id:
        return False

    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO referrals (inviter_id, invited_id) VALUES ($1,$2)",
                inviter_id,
                invited_id,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


async def get_credits_balance(tg_id: int) -> int:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount),0) AS balance FROM credits WHERE user_id=$1",
            tg_id,
        )
        return int(row["balance"]) if row else 0


async def add_credits(tg_id: int, amount: int, reason: str) -> int:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO credits (user_id, amount, reason) VALUES ($1,$2,$3)",
            tg_id,
            amount,
            reason,
        )
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount),0) AS balance FROM credits WHERE user_id=$1",
            tg_id,
        )
        return int(row["balance"]) if row else 0


async def consume_credit(tg_id: int, amount: int = 1) -> Tuple[bool, int]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount),0) AS balance FROM credits WHERE user_id=$1",
                tg_id,
            )
            current = int(row["balance"]) if row else 0

            if current < amount:
                return False, current

            await conn.execute(
                "INSERT INTO credits (user_id, amount, reason) VALUES ($1,$2,$3)",
                tg_id,
                -amount,
                "spend_generation",
            )

            new_balance = current - amount
            return True, new_balance


async def has_used_free(tg_id: int) -> bool:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT used_free FROM usage_stats WHERE user_id=$1 ORDER BY id DESC LIMIT 1",
            tg_id,
        )
        return bool(row and row["used_free"] > 0)


async def mark_free_used(tg_id: int) -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE usage_stats SET used_free=1,last_used_at=NOW() WHERE user_id=$1",
            tg_id,
        )
        if result == "UPDATE 0":
            await conn.execute(
                "INSERT INTO usage_stats (user_id,used_free,last_used_at) VALUES ($1,1,NOW())",
                tg_id,
            )


async def get_ref_stars(tg_id: int) -> Tuple[int, int]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stars_accumulated,stars_balance FROM ref_stars WHERE user_id=$1",
            tg_id,
        )
        if row:
            return int(row["stars_accumulated"]), int(row["stars_balance"])

        await conn.execute(
            "INSERT INTO ref_stars (user_id,stars_accumulated,stars_balance) VALUES ($1,0,0)",
            tg_id,
        )
        return 0, 0


async def add_ref_stars(tg_id: int, delta: int) -> Tuple[int, int]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT stars_accumulated,stars_balance FROM ref_stars WHERE user_id=$1",
                tg_id,
            )
            if not row:
                await conn.execute(
                    "INSERT INTO ref_stars (user_id,stars_accumulated,stars_balance) VALUES ($1,0,0)",
                    tg_id,
                )
                acc, bal = 0, 0
            else:
                acc, bal = int(row["stars_accumulated"]), int(row["stars_balance"])

            new_acc = acc + delta if delta >= 0 else acc
            new_bal = bal + delta

            await conn.execute(
                "UPDATE ref_stars SET stars_accumulated=$2, stars_balance=$3 WHERE user_id=$1",
                tg_id,
                new_acc,
                new_bal,
            )

            return new_acc, new_bal

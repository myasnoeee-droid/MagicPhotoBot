# db.py
import os
import asyncpg
from typing import Optional, Tuple, List
from datetime import datetime, timezone

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # на Railway ты должен был завести переменную DATABASE_URL = ${{ Postgres.DATABASE_URL }}
    raise RuntimeError("DATABASE_URL is not set")

_pool: Optional[asyncpg.Pool] = None


# ---------- ИНИЦИАЛИЗАЦИЯ ----------

async def init_db():
    """
    Создаёт пул соединений и на всякий случай выполняет CREATE TABLE IF NOT EXISTS
    (то же самое, что мы уже запускали в TablePlus — дубликат не навредит).
    """
    global _pool
    if _pool is not None:
        return

    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    ddl = """
    CREATE TABLE IF NOT EXISTS users (
        id          SERIAL PRIMARY KEY,
        tg_id       BIGINT NOT NULL UNIQUE,
        lang        VARCHAR(10) NOT NULL DEFAULT 'en',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS referrals (
        id          SERIAL PRIMARY KEY,
        inviter_id  BIGINT NOT NULL,
        invited_id  BIGINT NOT NULL UNIQUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS credits (
        id          SERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL,
        amount      INT NOT NULL,
        reason      VARCHAR(50) NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS usage (
        id          SERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL UNIQUE,
        free_used   INT NOT NULL DEFAULT 0,
        last_used_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS ref_stars (
        id              SERIAL PRIMARY KEY,
        user_id         BIGINT NOT NULL UNIQUE,
        total_stars     BIGINT NOT NULL DEFAULT 0,
        stars_balance   BIGINT NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS ref_pushes (
        id              SERIAL PRIMARY KEY,
        user_id         BIGINT NOT NULL UNIQUE,
        last_push_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    async with _pool.acquire() as conn:
        await conn.execute(ddl)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- USERS (known_users, user_lang) ----------

async def ensure_user(tg_id: int, lang: str = "en") -> None:
    """
    Создаёт пользователя, если его ещё нет.
    Если есть — только обновляет язык (если он поменялся).
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT lang FROM users WHERE tg_id = $1", tg_id)
        if row is None:
            await conn.execute(
                "INSERT INTO users (tg_id, lang) VALUES ($1, $2)",
                tg_id, lang,
            )
        else:
            if row["lang"] != lang:
                await conn.execute(
                    "UPDATE users SET lang = $1 WHERE tg_id = $2",
                    lang, tg_id,
                )


async def get_user_lang(tg_id: int) -> Optional[str]:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT lang FROM users WHERE tg_id = $1", tg_id)
        return row["lang"] if row else None


async def set_user_lang(tg_id: int, lang: str) -> None:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (tg_id, lang)
            VALUES ($1, $2)
            ON CONFLICT (tg_id) DO UPDATE
            SET lang = EXCLUDED.lang
            """,
            tg_id, lang,
        )


async def get_all_user_ids() -> List[int]:
    """
    Аналог known_users — вернёт всех tg_id из таблицы users.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id FROM users")
        return [r["tg]()]()

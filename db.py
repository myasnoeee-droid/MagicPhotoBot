# db.py
import os
from typing import Optional, List, Tuple
from datetime import datetime, timezone

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # на Railway ты должен был завести переменную DATABASE_URL = ${{ Postgres.DATABASE_URL }}
    raise RuntimeError("DATABASE_URL is not set")

_pool: Optional[asyncpg.Pool] = None


# ---------- ИНИЦИАЛИЗАЦИЯ ----------

async def init_db():
    """
    Создаёт пул соединений и на всякий случай выполняет CREATE TABLE IF NOT EXISTS.
    Если ты уже создавал таблицы руками — второй раз просто ничего не изменится.
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
        id           SERIAL PRIMARY KEY,
        user_id      BIGINT NOT NULL UNIQUE,
        free_used    INT NOT NULL DEFAULT 0,
        last_used_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS ref_stars (
        id            SERIAL PRIMARY KEY,
        user_id       BIGINT NOT NULL UNIQUE,
        total_stars   BIGINT NOT NULL DEFAULT 0,
        stars_balance BIGINT NOT NULL DEFAULT 0,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS ref_pushes (
        id           SERIAL PRIMARY KEY,
        user_id      BIGINT NOT NULL UNIQUE,
        last_push_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    Если есть — обновляет язык (если поменялся).
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lang FROM users WHERE tg_id = $1",
            tg_id,
        )
        if row is None:
            await conn.execute(
                "INSERT INTO users (tg_id, lang) VALUES ($1, $2)",
                tg_id, lang,
            )
        elif row["lang"] != lang:
            await conn.execute(
                "UPDATE users SET lang = $1 WHERE tg_id = $2",
                lang, tg_id,
            )


async def get_user_lang(tg_id: int) -> Optional[str]:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT lang FROM users WHERE tg_id = $1",
            tg_id,
        )
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
    Аналог known_users — вернёт все tg_id из таблицы users.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT tg_id FROM users")
        return [r["tg_id"] for r in rows]


# ---------- REFERRALS ----------

async def add_referral(inviter_id: int, invited_id: int) -> bool:
    """
    Пытается записать факт, что inviter пригласил invited.
    Возвращает True, если новая запись создана, False — если уже было.
    """
    global _pool
    assert _pool is not None

    if inviter_id == invited_id:
        return False

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM referrals WHERE invited_id = $1",
            invited_id,
        )
        if row:
            return False

        await conn.execute(
            """
            INSERT INTO referrals (inviter_id, invited_id)
            VALUES ($1, $2)
            """,
            inviter_id, invited_id,
        )
        return True


async def count_referrals(inviter_id: int) -> int:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS c FROM referrals WHERE inviter_id = $1",
            inviter_id,
        )
        return int(row["c"]) if row else 0


async def get_inviter(invited_id: int) -> Optional[int]:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT inviter_id FROM referrals WHERE invited_id = $1",
            invited_id,
        )
        return int(row["inviter_id"]) if row else None


# ---------- CREDITS (user_credits) ----------

async def add_credits(user_id: int, amount: int, reason: str) -> None:
    """
    Записываем транзакцию по кредитам. Баланс будем считать суммой по пользователю.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO credits (user_id, amount, reason)
            VALUES ($1, $2, $3)
            """,
            user_id, amount, reason,
        )


async def get_credits_balance(user_id: int) -> int:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS bal FROM credits WHERE user_id = $1",
            user_id,
        )
        return int(row["bal"]) if row else 0


# ---------- FREE USAGE (usage / limiter) ----------

async def get_free_usage(user_id: int) -> Tuple[int, Optional[datetime]]:
    """
    Возвращает (free_used, last_used_at)
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT free_used, last_used_at FROM usage WHERE user_id = $1",
            user_id,
        )
        if not row:
            return 0, None
        return int(row["free_used"]), row["last_used_at"]


async def increment_free_usage(user_id: int) -> None:
    """
    Увеличивает счётчик бесплатных использований.
    """
    global _pool
    assert _pool is not None

    now = _now()
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage (user_id, free_used, last_used_at)
            VALUES ($1, 1, $2)
            ON CONFLICT (user_id) DO UPDATE
              SET free_used = usage.free_used + 1,
                  last_used_at = EXCLUDED.last_used_at
            """,
            user_id, now,
        )


# ---------- REF_STARS (баланс звёзд у реферала) ----------

async def add_ref_stars(user_id: int, stars: int) -> None:
    """
    Прибавить к пользователю реферальные Stars.
    """
    global _pool
    assert _pool is not None

    if stars <= 0:
        return

    now = _now()
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ref_stars (user_id, total_stars, stars_balance, created_at, updated_at)
            VALUES ($1, $2, $2, $3, $3)
            ON CONFLICT (user_id) DO UPDATE
              SET total_stars = ref_stars.total_stars + $2,
                  stars_balance = ref_stars.stars_balance + $2,
                  updated_at = $3
            """,
            user_id, stars, now,
        )


async def get_ref_stars(user_id: int) -> Tuple[int, int]:
    """
    Возвращает (total_stars, stars_balance).
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT total_stars, stars_balance
            FROM ref_stars
            WHERE user_id = $1
            """,
            user_id,
        )
        if not row:
            return 0, 0
        return int(row["total_stars"]), int(row["stars_balance"])


async def spend_ref_stars(user_id: int, stars: int) -> bool:
    """
    Пытается списать stars с реферального баланса.
    Возвращает True, если списание прошло, False — если не хватило.
    """
    global _pool
    assert _pool is not None

    if stars <= 0:
        return True

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stars_balance FROM ref_stars WHERE user_id = $1",
            user_id,
        )
        if not row or row["stars_balance"] < stars:
            return False

        await conn.execute(
            """
            UPDATE ref_stars
            SET stars_balance = stars_balance - $1,
                updated_at = $2
            WHERE user_id = $3
            """,
            stars, _now(), user_id,
        )
        return True


# ---------- REF_PUSHES (last_ref_push) ----------

async def get_last_ref_push(user_id: int) -> Optional[datetime]:
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_push_at FROM ref_pushes WHERE user_id = $1",
            user_id,
        )
        return row["last_push_at"] if row else None


async def set_last_ref_push(user_id: int, ts: Optional[datetime] = None) -> None:
    global _pool
    assert _pool is not None

    if ts is None:
        ts = _now()

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ref_pushes (user_id, last_push_at)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
              SET last_push_at = EXCLUDED.last_push_at
            """,
            user_id, ts,
        )

import os
import asyncpg
from typing import Optional, Tuple, List
from datetime import datetime, timezone

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    # На Railway должна быть переменная DATABASE_URL = ${Postgres.DATABASE_URL}
    raise RuntimeError("DATABASE_URL is not set")

_pool: Optional[asyncpg.Pool] = None


# ---------- ВСПОМОГАТЕЛЬНОЕ ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- ИНИЦИАЛИЗАЦИЯ БАЗЫ ----------

async def init_db():
    """
    Создаёт пул соединений и таблицы (если их ещё нет).
    Можно спокойно вызывать при каждом старте бота.
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
        return [r["tg_id"] for r in rows]


# ---------- USAGE (free usage) ----------

async def get_free_usage(user_id: int) -> int:
    """
    Сколько бесплатных анимаций уже использовал пользователь.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT free_used FROM usage WHERE user_id = $1",
            user_id,
        )
        return int(row["free_used"]) if row else 0


async def inc_free_usage(user_id: int, delta: int = 1) -> None:
    """
    Увеличить счётчик бесплатных анимаций.
    """
    global _pool
    assert _pool is not None

    now = _now()
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO usage (user_id, free_used, last_used_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET free_used = usage.free_used + EXCLUDED.free_used,
                last_used_at = EXCLUDED.last_used_at
            """,
            user_id, delta, now,
        )


# ---------- CREDITS (покупки и баланс) ----------

async def change_credits(user_id: int, amount: int, reason: str) -> None:
    """
    Добавляет или списывает кредиты (amount может быть отрицательным).
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
    """
    Считает текущий баланс кредитов как сумму всех операций.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS balance FROM credits WHERE user_id = $1",
            user_id,
        )
        return int(row["balance"]) if row else 0


# ---------- REFERRALS (кто кого привёл) ----------

async def add_referral(inviter_id: int, invited_id: int) -> bool:
    """
    Регистрирует реферала. Возвращает True, если запись была создана,
    False, если такой invited_id уже есть.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO referrals (inviter_id, invited_id)
            VALUES ($1, $2)
            ON CONFLICT (invited_id) DO NOTHING
            """,
            inviter_id, invited_id,
        )
        # asyncpg возвращает строку вида "INSERT 0 1" или "INSERT 0 0"
        return result.endswith("1")


async def get_referral_count(inviter_id: int) -> int:
    """
    Кол-во приглашённых этим пользователем.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM referrals WHERE inviter_id = $1",
            inviter_id,
        )
        return int(row["cnt"]) if row else 0


async def get_invited_users(inviter_id: int) -> List[int]:
    """
    Список tg_id (invited_id), которых привёл этот inviter.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT invited_id FROM referrals WHERE inviter_id = $1",
            inviter_id,
        )
        return [r["invited_id"] for r in rows]


async def get_inviter(user_id: int) -> Optional[int]:
    """
    Кто пригласил этого пользователя (если есть запись).
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT inviter_id FROM referrals WHERE invited_id = $1",
            user_id,
        )
        return int(row["inviter_id"]) if row else None


# ---------- REF_STARS (накопленные реферальные Stars) ----------

async def get_ref_stars(user_id: int) -> Tuple[int, int]:
    """
    Возвращает (total_stars, stars_balance).
    total_stars — сколько всего начислено за всё время.
    stars_balance — текущий баланс для конвертации в кредиты.
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_stars, stars_balance FROM ref_stars WHERE user_id = $1",
            user_id,
        )
        if not row:
            return 0, 0
        return int(row["total_stars"]), int(row["stars_balance"])


async def add_ref_stars(user_id: int, bonus_stars: int) -> Tuple[int, int]:
    """
    Увеличивает total_stars и stars_balance на bonus_stars.
    Возвращает (total_stars, stars_balance) после обновления.
    """
    global _pool
    assert _pool is not None

    now = _now()
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ref_stars (user_id, total_stars, stars_balance, created_at, updated_at)
            VALUES ($1, $2, $2, $3, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET total_stars   = ref_stars.total_stars   + EXCLUDED.total_stars,
                stars_balance = ref_stars.stars_balance + EXCLUDED.stars_balance,
                updated_at    = EXCLUDED.updated_at
            RETURNING total_stars, stars_balance
            """,
            user_id, bonus_stars, now,
        )
        return int(row["total_stars"]), int(row["stars_balance"])


async def set_ref_stars_balance(user_id: int, new_balance: int) -> None:
    """
    Обновляет только stars_balance (используется после конвертации в кредиты).
    """
    global _pool
    assert _pool is not None

    now = _now()
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ref_stars (user_id, total_stars, stars_balance, created_at, updated_at)
            VALUES ($1, 0, $2, $3, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET stars_balance = $2,
                updated_at    = $3
            """,
            user_id, new_balance, now,
        )


# ---------- REF_PUSHES (когда последний пуш рефералки) ----------

async def get_last_ref_push(user_id: int) -> Optional[datetime]:
    """
    Возвращает время последнего пуша (или None, если ещё не слали).
    """
    global _pool
    assert _pool is not None

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_push_at FROM ref_pushes WHERE user_id = $1",
            user_id,
        )
        return row["last_push_at"] if row else None


async def set_last_ref_push(user_id: int) -> None:
    """
    Обновляет время последнего пуша на текущее.
    """
    global _pool
    assert _pool is not None

    now = _now()
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ref_pushes (user_id, last_push_at)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
            SET last_push_at = EXCLUDED.last_push_at
            """,
            user_id, now,
        )

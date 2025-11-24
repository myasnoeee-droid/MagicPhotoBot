# db.py
import os
import logging
from typing import Optional, Dict, Any, List, Tuple

import asyncpg

logger = logging.getLogger("livephotobot.db")

DATABASE_URL = os.getenv("DATABASE_URL")


class Database:
    """
    Обёртка над asyncpg под твою модель:
      - users            (tg_id, lang, created_at)
      - referrals        (inviter_id, invited_id, created_at)
      - credits          (user_id, amount, reason, created_at)
      - usage_stats      (user_id, used_free, last_used_at)
      - ref_stars        (user_id, stars_accumulated, stars_balance, created_at)
      - ref_pushes       (user_id, last_push_at)
    Во всех таблицах user_id / inviter_id / invited_id — это именно tg_id.
    """

    def __init__(self) -> None:
        self.pool: Optional[asyncpg.Pool] = None

    # ---------- БАЗА ----------

    async def connect(self) -> None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")

        self.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
        )
        logger.info("Database pool created")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            logger.info("Database pool closed")

    # ---------- USERS ----------

    async def ensure_user(self, tg_id: int, lang: Optional[str] = None) -> None:
        """
        Создаёт юзера, если нет. Если lang передан — обновит язык.
        """
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (tg_id, lang)
                VALUES ($1, COALESCE($2, 'en'))
                ON CONFLICT (tg_id) DO UPDATE
                SET lang = COALESCE(EXCLUDED.lang, users.lang)
                """,
                tg_id,
                lang,
            )

    async def get_lang(self, tg_id: int) -> str:
        """Получить язык пользователя, по умолчанию 'en'."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT lang FROM users WHERE tg_id = $1",
                tg_id,
            )
            return row["lang"] if row and row["lang"] else "en"

    async def set_lang(self, tg_id: int, lang: str) -> None:
        """Обновить язык пользователя (создаст юзера, если его ещё нет)."""
        await self.ensure_user(tg_id, lang=lang)

    async def get_all_users(self) -> List[int]:
        """Все tg_id пользователей — нужно, например, для реферальных пушей."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT tg_id FROM users")
            return [r["tg_id"] for r in rows]

    # ---------- FREE USAGE (usage_stats) ----------

    async def get_free_used(self, tg_id: int) -> int:
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT used_free FROM usage_stats WHERE user_id = $1",
                tg_id,
            )
            return int(row["used_free"]) if row else 0

    async def increment_free_used(self, tg_id: int) -> int:
        """
        +1 к used_free и обновление last_used_at.
        Возвращает новое значение used_free.
        """
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO usage_stats (user_id, used_free, last_used_at)
                VALUES ($1, 1, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET used_free = usage_stats.used_free + 1,
                    last_used_at = NOW()
                RETURNING used_free
                """,
                tg_id,
            )
            return int(row["used_free"])

    async def total_free_used(self) -> int:
        """Суммарное количество бесплатных оживлений по всем пользователям."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            value = await conn.fetchval("SELECT COALESCE(SUM(used_free), 0) FROM usage_stats")
            return int(value or 0)

    async def free_users_count(self) -> int:
        """Сколько пользователей вообще использовали бесплатное оживление."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            value = await conn.fetchval("SELECT COUNT(*) FROM usage_stats")
            return int(value or 0)

    # ---------- CREDITS (баланс за счёт суммирования операций) ----------

    async def get_credits(self, tg_id: int) -> int:
        """
        Текущий баланс: сумма amount по таблице credits.
        """
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE user_id = $1",
                tg_id,
            )
            return int(value or 0)

    async def add_credits(self, tg_id: int, delta: int, reason: str) -> int:
        """
        Добавить (или списать, если delta < 0) кредиты.
        Возвращает новый баланс.
        """
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if delta != 0:
                    await conn.execute(
                        """
                        INSERT INTO credits (user_id, amount, reason)
                        VALUES ($1, $2, $3)
                        """,
                        tg_id,
                        delta,
                        reason,
                    )
                value = await conn.fetchval(
                    "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE user_id = $1",
                    tg_id,
                )
                return int(value or 0)

    async def spend_credits(self, tg_id: int, amount: int = 1) -> bool:
        """
        Пытается списать amount кредитов.
        Возвращает True, если баланс позволил это сделать.
        """
        assert self.pool is not None
        if amount <= 0:
            return True

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                balance = await conn.fetchval(
                    "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE user_id = $1",
                    tg_id,
                )
                balance = int(balance or 0)
                if balance < amount:
                    return False

                await conn.execute(
                    """
                    INSERT INTO credits (user_id, amount, reason)
                    VALUES ($1, $2, $3)
                    """,
                    tg_id,
                    -amount,
                    "spend",
                )
                return True

    # ---------- REFERRALS (кто кого привёл) ----------

    async def register_referral(
        self,
        inviter_tg_id: int,
        invited_tg_id: int,
    ) -> Tuple[bool, int, bool]:
        """
        Регистрирует рефералку (inviter -> invited).
        Возвращает:
          created            — была ли создана новая запись (False, если invited уже кем-то привёл или сам себя)
          total_invited      — сколько всего людей пригласил inviter
          earned_free_credit — получил ли он сейчас бесплатное оживление (каждые 3 приглашённых)
        """
        assert self.pool is not None

        if inviter_tg_id == invited_tg_id:
            return False, 0, False

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO referrals (inviter_id, invited_id)
                    VALUES ($1, $2)
                    ON CONFLICT (invited_id) DO NOTHING
                    RETURNING id
                    """,
                    inviter_tg_id,
                    invited_tg_id,
                )
                if row is None:
                    # invited уже привязан к какому-то инвайтеру
                    total = await conn.fetchval(
                        "SELECT COUNT(*) FROM referrals WHERE inviter_id = $1",
                        inviter_tg_id,
                    )
                    return False, int(total or 0), False

                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM referrals WHERE inviter_id = $1",
                    inviter_tg_id,
                )
                total = int(total or 0)

                earned_free = False
                if total % 3 == 0:
                    # каждые 3 приглашённых даём +1 кредит
                    await conn.execute(
                        """
                        INSERT INTO credits (user_id, amount, reason)
                        VALUES ($1, 1, 'referral_bonus')
                        """,
                        inviter_tg_id,
                    )
                    earned_free = True

                return True, total, earned_free

    async def get_invited_count(self, inviter_tg_id: int) -> int:
        """Сколько людей пригласил данный юзер."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COUNT(*) FROM referrals WHERE inviter_id = $1",
                inviter_tg_id,
            )
            return int(value or 0)

    # ---------- REFERRAL STARS (5% от пополнений друзей) ----------

    async def add_ref_stars_bonus(
        self,
        inviter_tg_id: int,
        bonus_stars: int,
        stars_per_credit: int = 60,
    ) -> Tuple[int, int]:
        """
        Начисляет бонусные Stars:
          - добавляет в ref_stars.total / balance
          - конвертирует Stars в кредиты по курсу stars_per_credit
        Возвращает:
          (gained_credits, new_credits_balance)
        """
        assert self.pool is not None
        if bonus_stars <= 0:
            balance = await self.get_credits(inviter_tg_id)
            return 0, balance

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO ref_stars (user_id, stars_accumulated, stars_balance)
                    VALUES ($1, $2, $2)
                    ON CONFLICT (user_id) DO UPDATE
                    SET stars_accumulated = ref_stars.stars_accumulated + $2,
                        stars_balance     = ref_stars.stars_balance + $2
                    RETURNING stars_balance
                    """,
                    inviter_tg_id,
                    bonus_stars,
                )
                balance_stars = int(row["stars_balance"])

                gained_credits = balance_stars // stars_per_credit
                if gained_credits > 0:
                    new_balance_stars = balance_stars - gained_credits * stars_per_credit
                    await conn.execute(
                        "UPDATE ref_stars SET stars_balance = $2 WHERE user_id = $1",
                        inviter_tg_id,
                        new_balance_stars,
                    )
                    await conn.execute(
                        """
                        INSERT INTO credits (user_id, amount, reason)
                        VALUES ($1, $2, 'ref_stars_convert')
                        """,
                        inviter_tg_id,
                        gained_credits,
                    )

                new_credits_balance = await conn.fetchval(
                    "SELECT COALESCE(SUM(amount), 0) FROM credits WHERE user_id = $1",
                    inviter_tg_id,
                )
                return gained_credits, int(new_credits_balance or 0)

    async def get_ref_stars_info(self, tg_id: int) -> Dict[str, int]:
        """Инфа по реферальным Stars для юзера."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT stars_accumulated, stars_balance FROM ref_stars WHERE user_id = $1",
                tg_id,
            )
            if not row:
                return {"stars_accumulated": 0, "stars_balance": 0}
            return {
                "stars_accumulated": int(row["stars_accumulated"]),
                "stars_balance": int(row["stars_balance"]),
            }

    # ---------- REF PUSHES (когда последний раз пушили рефералку) ----------

    async def get_last_ref_push(self, tg_id: int) -> Optional[str]:
        """
        Возвращает ISO-строку времени последнего пуша по рефералке или None.
        """
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT last_push_at
                FROM ref_pushes
                WHERE user_id = $1
                ORDER BY last_push_at DESC
                LIMIT 1
                """,
                tg_id,
            )
            if not row:
                return None
            return row["last_push_at"].isoformat()

    async def add_ref_push(self, tg_id: int) -> None:
        """Записать, что юзеру отправлен реферальный пуш прямо сейчас."""
        assert self.pool is not None
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ref_pushes (user_id, last_push_at) VALUES ($1, NOW())",
                tg_id,
            )

    # ---------- Сводная статистика для /ref ----------

    async def get_referral_stats(self, tg_id: int) -> Dict[str, Any]:
        """
        Сводная статистика:
          - invited_count
          - free_from_invites (invited_count // 3)
          - ref_stars_balance
          - credits_balance
        """
        invited = await self.get_invited_count(tg_id)
        stars_info = await self.get_ref_stars_info(tg_id)
        credits = await self.get_credits(tg_id)
        return {
            "invited_count": invited,
            "free_from_invites": invited // 3,
            "ref_stars_balance": stars_info["stars_balance"],
            "credits_balance": credits,
        }


# Глобальный объект, который будем импортировать в app.py
db = Database()


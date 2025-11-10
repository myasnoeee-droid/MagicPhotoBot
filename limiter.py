from collections import defaultdict

class FreeUsageLimiter:
    """Наивный лимитер бесплатных использований в памяти процесса."""
    def __init__(self, max_free: int = 1):
        self.max_free = max_free
        self._usage = defaultdict(int)
        self._total = 0

    def can_use(self, user_id: int) -> bool:
        """Можно ли ещё бесплатно использовать сервис для этого пользователя."""
        return self._usage[user_id] < self.max_free

    def mark_used(self, user_id: int):
        """Отмечаем одно использование пользователем (после успешной анимации)."""
        self._usage[user_id] += 1
        self._total += 1

    def users_count(self) -> int:
        """Сколько уникальных пользователей уже использовали бота."""
        return len(self._usage)

    def total_count(self) -> int:
        """Сколько всего анимаций обработано (счётчик по процессу)."""
        return self._total

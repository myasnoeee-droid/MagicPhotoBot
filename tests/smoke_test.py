# Мини-проверка окружения. Запуск: python -m tests.smoke_test
import sys, os

def _assert(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)

print("[smoke] Python:", sys.version)

# Импорты пакетов
try:
    import aiogram, requests, pydantic, dotenv
    print("[smoke] aiogram:", getattr(aiogram, "__version__", "?"))
    print("[smoke] requests:", requests.__version__)
    print("[smoke] pydantic:", pydantic.__version__)
except Exception as e:
    raise SystemExit(f"[smoke] Import error: {e}")

# Базовая проверка токена (если он задан)
bt = os.getenv("BOT_TOKEN", "")
if bt:
    _assert(":" in bt, "BOT_TOKEN выглядит странно — проверьте токен из @BotFather")

print("[smoke] OK")

import os
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart, Command
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties

from dotenv import load_dotenv

from limiter import FreeUsageLimiter
from processing import animate_photo_via_replicate, download_file

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger("magicphotobot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")
ECONOMY_MODEL = os.getenv("ECONOMY_MODEL")  # дешевая модель (опционально)
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(',') if x]
MAX_FREE_ANIMS_PER_USER = int(os.getenv("MAX_FREE_ANIMS_PER_USER", "1"))
DOWNLOAD_TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
limiter = FreeUsageLimiter(max_free=MAX_FREE_ANIMS_PER_USER)

WELCOME = (
    "<b>👋 Привет!</b> Это <b>MagicPhotoBot</b>\n\n"
    "🪄 Пришли мне <b>фото</b>, и я оживлю его в стиле фильмов о Гарри Поттере.\n"
    "Первое оживление — <b>бесплатно</b>. Дальше — доступные пакеты.\n\n"
    "Подсказка: лучше всего работают портреты, где лицо прямо и хорошо освещено."
)

PRICING = (
    "<b>Тарифы:</b>\n"
    "• 1 бесплатное оживление\n"
    "• 3 анимации — 2$\n"
    "• 10 анимаций — 5$\n\n"
    "Оплата скоро: TON / USDT / Telegram Stars.\n\n"
    "<b>Эконом-режим</b>: могу переключить на дешёвую модель (~$0.0075 за запуск). Напишите сюда — активирую."
)

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer("Бот временно доступен по инвайту. Напишите администратору.")
        return
    await message.answer(WELCOME)

@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    await message.answer(PRICING)

@dp.message(Command("admin"))
async def on_admin(message: Message):
    if ADMIN_USER_ID and message.from_user and message.from_user.id == ADMIN_USER_ID:
        await message.answer(
            f"Users in memory: {limiter.users_count()} | Total anims: {limiter.total_count()}"
        )
    else:
        await message.answer("Недостаточно прав.")

@dp.message(F.photo)
async def on_photo(message: Message):
    user_id = message.from_user.id if message.from_user else 0

    if not limiter.can_use(user_id):
        await message.answer("Вы использовали бесплатное оживление. Оформите пакет: /pricing")
        return

    photo = message.photo[-1]

    try:
        status = await message.answer("⚙️ Обрабатываю фото, это займёт ~20–60 секунд...")

        file_info = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        # Пытаемся основной моделью
        result = await animate_photo_via_replicate(source_image_url=file_url)

        # Если не хватает кредитов — пробуем эконом-модель (если задана)
        if not result.get("ok") and result.get("code") == "replicate_402" and ECONOMY_MODEL:
            fallback = await animate_photo_via_replicate(source_image_url=file_url, model_override=ECONOMY_MODEL)
            if fallback.get("ok"):
                result = fallback
                used_economy = True
            else:
                used_economy = False
        else:
            used_economy = False

        if not result.get("ok"):
            code = result.get("code", "unknown")
            if code == "replicate_402":
                await status.edit_text(
                    "💳 <b>Нужно пополнить кредиты Replicate</b>\n"
                    "replicate.com → Account → Billing → Add credit.\n"
                    "После пополнения подождите 1–2 минуты и пришлите фото снова.\n\n"
                    "<i>Альтернатива:</i> могу переключить на <b>эконом-модель</b> (~$0.0075/запуск). Напишите /pricing."
                )
                return
            if code == "replicate_422_fields":
                fields = result.get("fields") or []
                if set(fields) >= {"face_image", "driving_video"}:
                    await status.edit_text(
                        "⚠️ Выбрана модель, которая требует <b>два входа</b>: face_image и driving_video.\n"
                        "Для оживления <b>из одного фото</b> переключитесь на модель <b>live-portrait</b> и задайте:\n"
                        "REPLICATE_MODEL = fofr/live-portrait:<версия из вкладки API>\n"
                        "REPLICATE_INPUT_KEY = image\n\n"
                        "Зайдите в Railway → Variables, обновите значения и нажмите Redeploy."
                    )
                    return
                else:
                    await status.edit_text(
                        "⚠️ Модель требует дополнительные поля: " + ", ".join(fields) + ".\n"
                        "Переключитесь на модель live-portrait или скажите — я подберу параметры."
                    )
                    return
            if code in ("replicate_auth", "config"):
                await status.edit_text("⚠️ Проблема доступа к AI. Админ уже оповещён.")
                return
            else:
                await status.edit_text("❌ Не удалось оживить фото. Попробуйте другое изображение.")
                return

        video_url = result["url"]

        tmp_video_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{photo.file_unique_id}.mp4")
        await download_file(video_url, tmp_video_path)

        caption = "Готово! Если понравилось — /pricing"
        if used_economy:
            caption = "Готово! (Эконом-режим) Если понравилось — /pricing"

        await bot.send_video(chat_id=message.chat.id, video=FSInputFile(tmp_video_path), caption=caption)

        limiter.mark_used(user_id)

        try:
            os.remove(tmp_video_path)
        except Exception:
            pass

        await status.delete()

    except Exception as e:
        logger.exception("Animation failed: %s", e)
        await message.answer("⚠️ Произошла ошибка на обработке. Попробуйте ещё раз или другое фото.")


def main():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()

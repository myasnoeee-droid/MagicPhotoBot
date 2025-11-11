import os
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties

from dotenv import load_dotenv

from limiter import FreeUsageLimiter
from processing import animate_photo_via_replicate, download_file

load_dotenv()

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger("magicphotobot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x]
MAX_FREE_ANIMS_PER_USER = int(os.getenv("MAX_FREE_ANIMS_PER_USER", "1"))
DOWNLOAD_TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
limiter = FreeUsageLimiter(max_free=MAX_FREE_ANIMS_PER_USER)

# --- i18n (минималистично, без БД) ---
DEFAULT_LANG = "ru"
user_lang: dict[int, str] = {}  # user_id -> "ru"|"uk"|"en"

I18N = {
    "ru": {
        "welcome": (
            "<b>Привет!</b> Пришли мне <b>фото</b> и, при желании, подпись-промпт.\n"
            "Я сделаю короткое видео из изображения.\n\n"
            "Подсказка: лучше всего работают фронтальные портреты с хорошим светом."
        ),
        "pricing": (
            "<b>Тарифы:</b>\n"
            "• 1 бесплатное видео на пользователя\n"
            "• Пакеты скоро (TON / USDT / Telegram Stars)"
        ),
        "invite_only": "Бот временно доступен по инвайту. Напишите администратору.",
        "free_used": "Вы использовали бесплатное видео. Смотрите /pricing",
        "status_work": "Готовлю ваше видео... ~20–60 секунд",
        "insufficient_credit": (
            "Недостаточно кредитов на Replicate. Зайдите: replicate.com → Account → Billing → Add credit.\n"
            "После оплаты подождите 1–2 минуты и повторите."
        ),
        "auth_error": "Ошибка доступа к AI-провайдеру. Админ уже оповещен.",
        "model_fields": "Выбранная модель требует другие входы: {fields}.\nУбедитесь, что используете модель image-to-video (WAN i2v).",
        "fail": "Не удалось сгенерировать. Попробуйте другое фото.",
        "done": "Готово! Если понравилось — смотрите /pricing",
        "choose_lang": "Выберите язык интерфейса:",
        "lang_set": "Язык переключен на: Русский",
        "lang_button": "Русский",
        "lang_button_uk": "Українська",
        "lang_button_en": "English",
        "lang_set_uk": "Мову змінено на: Українська",
        "lang_set_en": "Language switched to: English",
        "hint_prompt": "natural smile, subtle head motion, cinematic lighting"
    },
    "uk": {
        "welcome": (
            "<b>Привіт!</b> Надішли <b>фото</b> і, за бажання, підпис-промпт.\n"
            "Я зроблю коротке відео з зображення.\n\n"
            "Підказка: найкраще працюють фронтальні портрети з хорошим світлом."
        ),
        "pricing": (
            "<b>Тарифи:</b>\n"
            "• 1 безкоштовне відео на користувача\n"
            "• Пакети скоро (TON / USDT / Telegram Stars)"
        ),
        "invite_only": "Бот тимчасово доступний за інвайтом. Напишіть адміністратору.",
        "free_used": "Ви використали безкоштовне відео. Дивіться /pricing",
        "status_work": "Готую ваше відео... ~20–60 секунд",
        "insufficient_credit": (
            "Недостатньо кредитів на Replicate. Зайдіть: replicate.com → Account → Billing → Add credit.\n"
            "Після оплати зачекайте 1–2 хвилини та повторіть."
        ),
        "auth_error": "Помилка доступу до AI-провайдера. Адміна вже сповіщено.",
        "model_fields": "Обрана модель потребує інші входи: {fields}.\nПереконайтеся, що це image-to-video (WAN i2v).",
        "fail": "Не вдалося згенерувати. Спробуйте інше фото.",
        "done": "Готово! Якщо сподобалось — дивіться /pricing",
        "choose_lang": "Оберіть мову інтерфейсу:",
        "lang_set": "Мову змінено на: Українська",
        "lang_button": "Русский",
        "lang_button_uk": "Українська",
        "lang_button_en": "English",
        "lang_set_en": "Language switched to: English",
        "hint_prompt": "natural smile, subtle head motion, cinematic lighting"
    },
    "en": {
        "welcome": (
            "<b>Hi!</b> Send a <b>photo</b> and optionally a <b>prompt</b> in caption.\n"
            "I will generate a short video from your image.\n\n"
            "Tip: front-facing portraits with good lighting work best."
        ),
        "pricing": (
            "<b>Pricing:</b>\n"
            "• 1 free video per user\n"
            "• Packs soon (TON / USDT / Telegram Stars)"
        ),
        "invite_only": "This bot is invite-only for now.",
        "free_used": "You used your free video. See /pricing",
        "status_work": "Working on your video... ~20–60s",
        "insufficient_credit": (
            "Insufficient Replicate credit. Go to replicate.com → Account → Billing → Add credit.\n"
            "Try again 1–2 minutes after payment."
        ),
        "auth_error": "AI provider auth/config error. Admin notified.",
        "model_fields": "Selected model requires different inputs: {fields}.\nPlease use an image-to-video model (WAN i2v).",
        "fail": "Failed to generate. Please try a different image.",
        "done": "Done! If you like it — see /pricing",
        "choose_lang": "Choose interface language:",
        "lang_set": "Language switched to: English",
        "lang_button": "Русский",
        "lang_button_uk": "Українська",
        "lang_button_en": "English",
        "hint_prompt": "natural smile, subtle head motion, cinematic lighting"
    },
}

def t(uid: int, key: str) -> str:
    lang = user_lang.get(uid, DEFAULT_LANG)
    return I18N.get(lang, I18N[DEFAULT_LANG]).get(key, "")

def lang_keyboard(uid: int) -> InlineKeyboardMarkup:
    # Кнопки всегда одинаковые по надписям, просто берем из RU (понятнее для RU/UA)
    ru = InlineKeyboardButton(text=I18N["ru"]["lang_button"], callback_data="lang:ru")
    uk = InlineKeyboardButton(text=I18N["ru"]["lang_button_uk"], callback_data="lang:uk")
    en = InlineKeyboardButton(text=I18N["ru"]["lang_button_en"], callback_data="lang:en")
    return InlineKeyboardMarkup(inline_keyboard=[[ru, uk, en]])

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer(I18N[DEFAULT_LANG]["invite_only"])
        return
    # Если язык еще не выбран — покажем меню выбора
    uid = message.from_user.id if message.from_user else 0
    if uid not in user_lang:
        await message.answer(I18N[DEFAULT_LANG]["choose_lang"], reply_markup=lang_keyboard(uid))
    else:
        await message.answer(t(uid, "welcome"))

@dp.message(Command("lang"))
async def on_lang(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(t(uid, "choose_lang"), reply_markup=lang_keyboard(uid))

@dp.callback_query(F.data.startswith("lang:"))
async def on_lang_set(query: CallbackQuery):
    uid = query.from_user.id
    _, lang = query.data.split(":", 1)
    if lang in I18N:
        user_lang[uid] = lang
        # Подтверждение на выбранном языке
        if lang == "ru":
            await query.message.edit_text(I18N["ru"]["lang_set"])
        elif lang == "uk":
            await query.message.edit_text(I18N["ru"]["lang_set_uk"])
        else:
            await query.message.edit_text(I18N["ru"]["lang_set_en"])
        # Показ приветствия
        await query.message.answer(t(uid, "welcome"))

@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(t(uid, "pricing"))

@dp.message(Command("admin"))
async def on_admin(message: Message):
    uid = message.from_user.id if message.from_user else 0
    if ADMIN_USER_ID and message.from_user and message.from_user.id == ADMIN_USER_ID:
        await message.answer(f"Users: {limiter.users_count()} | Total renders: {limiter.total_count()}")
    else:
        await message.answer("No permission.")

@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id if message.from_user else 0

    if not limiter.can_use(uid):
        await message.answer(t(uid, "free_used"))
        return

    photo = message.photo[-1]
    try:
        status = await message.answer(t(uid, "status_work"))

        file_info = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        user_prompt = (message.caption or "").strip()
        if not user_prompt:
            # Дефолтный промпт лучше оставить на EN для качества
            user_prompt = t(uid, "hint_prompt")

        result = await animate_photo_via_replicate(source_image_url=file_url, prompt=user_prompt)

        if not result.get("ok"):
            code = result.get("code", "unknown")
            if code == "replicate_402":
                await status.edit_text(t(uid, "insufficient_credit"))
                return
            if code in ("replicate_auth", "config"):
                await status.edit_text(t(uid, "auth_error"))
                return
            if code == "replicate_422_fields":
                fields = result.get("fields") or []
                await status.edit_text(t(uid, "model_fields").format(fields=", ".join(fields)))
                return
            await status.edit_text(t(uid, "fail"))
            return

        video_url = result["url"]

        tmp_video_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{photo.file_unique_id}.mp4")
        await download_file(video_url, tmp_video_path)

        await bot.send_video(chat_id=message.chat.id, video=FSInputFile(tmp_video_path), caption=t(uid, "done"))

        limiter.mark_used(uid)

        try:
            os.remove(tmp_video_path)
        except Exception:
            pass

        await status.delete()

    except Exception as e:
        logger.exception("Animation failed: %s", e)
        await message.answer("Unexpected error. Please try again with another photo.")

def main():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()

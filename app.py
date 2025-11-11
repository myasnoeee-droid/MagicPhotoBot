import os
import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
)
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

# ---------------- i18n (simple in-memory) ----------------
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
        "free_used": "Вы использовали бесплатное видео. Смотрите /pricing или /buy",
        "status_work": "Готовлю ваше видео... ~20–60 секунд",
        "insufficient_credit": (
            "Недостаточно кредитов на Replicate. Зайдите: replicate.com → Account → Billing → Add credit.\n"
            "После оплаты подождите 1–2 минуты и повторите."
        ),
        "auth_error": "Ошибка доступа к AI-провайдеру. Админ уже оповещен.",
        "model_fields": "Выбранная модель требует другие входы: {fields}.\nУбедитесь, что используете image-to-video (WAN i2v).",
        "fail": "Не удалось сгенерировать. Попробуйте другое фото.",
        "done": "Готово! Если понравилось — смотрите /pricing",
        "choose_lang": "Выберите язык интерфейса:",
        "lang_set": "Язык переключен на: Русский",
        "lang_button": "Русский",
        "lang_button_uk": "Українська",
        "lang_button_en": "English",
        "lang_set_uk": "Мову змінено на: Українська",
        "lang_set_en": "Language switched to: English",
        "hint_prompt": "natural smile, subtle head motion, cinematic lighting",

        # Presets
        "presets": [
            "мягкая улыбка, легкое моргание, кинематографичный свет",
            "естественная улыбка, легкий поворот головы вправо, фотореалистично",
            "фэшн-портрет, едва заметная улыбка, 720p"
        ],
        "choose_preset": "Выберите стиль (или пришлите свой текст в подписи):",
        "btn_preset_1": "😊 Мягкая улыбка",
        "btn_preset_2": "🙂 Естественная улыбка",
        "btn_preset_3": "📸 Fashion 720p",
        "btn_use_caption": "✍️ Использовать мою подпись",
        "btn_cancel": "✖️ Отмена",
        "cancelled": "Отменено.",

        # Stars
        "buy_title": "Выберите пакет:",
        "buy_btn_3": "3 фото — 300 ⭐",
        "buy_btn_5": "5 фото — 450 ⭐",
        "buy_btn_10": "10 фото — 900 ⭐",
        "balance_title": "💰 Баланс\n• Кредиты: {credits}",
        "paid_ok": "✅ Оплата успешна! Начислено {credits} анимаций.\nБаланс: {balance}."
    },
    "uk": {
        "welcome": (
            "<b>Привіт!</b> Надішли <b>фото</b> і, за бажання, підпис-промпт.\n"
            "Я зроблю коротке відео із зображення.\n\n"
            "Підказка: найкраще працюють фронтальні портрети з хорошим світлом."
        ),
        "pricing": (
            "<b>Тарифи:</b>\n"
            "• 1 безкоштовне відео на користувача\n"
            "• Пакети скоро (TON / USDT / Telegram Stars)"
        ),
        "invite_only": "Бот тимчасово доступний за інвайтом. Напишіть адміністратору.",
        "free_used": "Ви використали безкоштовне відео. Дивіться /pricing або /buy",
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
        "hint_prompt": "natural smile, subtle head motion, cinematic lighting",

        "presets": [
            "ніжна усмішка, легке кліпання, кінематографічне освітлення",
            "природна усмішка, легкий поворот голови праворуч, фотореалістично",
            "fashion-портрет, ледь помітна усмішка, 720p"
        ],
        "choose_preset": "Оберіть стиль (або надішліть свій текст у підписі):",
        "btn_preset_1": "😊 Ніжна усмішка",
        "btn_preset_2": "🙂 Природна усмішка",
        "btn_preset_3": "📸 Fashion 720p",
        "btn_use_caption": "✍️ Мій підпис",
        "btn_cancel": "✖️ Скасувати",
        "cancelled": "Скасовано.",

        "buy_title": "Оберіть пакет:",
        "buy_btn_3": "3 фото — 300 ⭐",
        "buy_btn_5": "5 фото — 450 ⭐",
        "buy_btn_10": "10 фото — 900 ⭐",
        "balance_title": "💰 Баланс\n• Кредити: {credits}",
        "paid_ok": "✅ Оплата успішна! Нараховано {credits} анімацій.\nБаланс: {balance}."
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
        "free_used": "You used your free video. See /pricing or /buy",
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
        "hint_prompt": "natural smile, subtle head motion, cinematic lighting",

        "presets": [
            "smile softly, gentle eye blink, cinematic lighting",
            "natural smile, slight head turn right, photorealistic",
            "fashion portrait, subtle smile, 720p"
        ],
        "choose_preset": "Choose a style (or send your own prompt in caption):",
        "btn_preset_1": "😊 Soft smile",
        "btn_preset_2": "🙂 Natural smile",
        "btn_preset_3": "📸 Fashion 720p",
        "btn_use_caption": "✍️ Use my caption",
        "btn_cancel": "✖️ Cancel",
        "cancelled": "Cancelled.",

        "buy_title": "Choose a pack:",
        "buy_btn_3": "3 photos — 300 ⭐",
        "buy_btn_5": "5 photos — 450 ⭐",
        "buy_btn_10": "10 photos — 900 ⭐",
        "balance_title": "💰 Balance\n• Credits: {credits}",
        "paid_ok": "✅ Payment successful! Added {credits} animations.\nBalance: {balance}."
    },
}

def t(uid: int, key: str) -> str:
    lang = user_lang.get(uid, DEFAULT_LANG)
    return I18N.get(lang, I18N[DEFAULT_LANG]).get(key, "")

def lang_keyboard(uid: int) -> InlineKeyboardMarkup:
    ru = InlineKeyboardButton(text=I18N["ru"]["lang_button"], callback_data="lang:ru")
    uk = InlineKeyboardButton(text=I18N["ru"]["lang_button_uk"], callback_data="lang:uk")
    en = InlineKeyboardButton(text=I18N["ru"]["lang_button_en"], callback_data="lang:en")
    return InlineKeyboardMarkup(inline_keyboard=[[ru, uk, en]])

# Store last photo until user picks a preset
pending_photo: dict[int, dict] = {}  # user_id -> {"file_id": str, "caption": str}

def preset_keyboard(uid: int, has_caption: bool) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text=I18N["ru"]["btn_preset_1"], callback_data="preset:1"),
            InlineKeyboardButton(text=I18N["ru"]["btn_preset_2"], callback_data="preset:2"),
            InlineKeyboardButton(text=I18N["ru"]["btn_preset_3"], callback_data="preset:3"),
        ]
    ]
    row2 = []
    if has_caption:
        row2.append(InlineKeyboardButton(text=I18N["ru"]["btn_use_caption"], callback_data="preset:usecap"))
    row2.append(InlineKeyboardButton(text=I18N["ru"]["btn_cancel"], callback_data="preset:cancel"))
    kb.append(row2)
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ---------------- Stars (XTR) payments ----------------
# payload -> (title, credits, amount in XTR)
PACKS = {
    "pack_3":  ("3 animations", 3,  300),
    "pack_5":  ("5 animations", 5,  450),
    "pack_10": ("10 animations", 10, 900),
}
# user_id -> remaining paid credits
user_credits: dict[int, int] = {}

def buy_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=I18N["ru"]["buy_btn_3"], callback_data="buy:pack_3")
    ],[
        InlineKeyboardButton(text=I18N["ru"]["buy_btn_5"], callback_data="buy:pack_5")
    ],[
        InlineKeyboardButton(text=I18N["ru"]["buy_btn_10"], callback_data="buy:pack_10")
    ]])

def buy_cta_keyboard() -> InlineKeyboardMarkup:
    # короткие кнопки под видео (в одну строку)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=I18N["ru"]["buy_btn_3"], callback_data="buy:pack_3"),
        InlineKeyboardButton(text=I18N["ru"]["buy_btn_5"], callback_data="buy:pack_5"),
        InlineKeyboardButton(text=I18N["ru"]["buy_btn_10"], callback_data="buy:pack_10"),
    ]])

# ---------------- Handlers ----------------

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer(I18N[DEFAULT_LANG]["invite_only"])
        return

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
        if lang == "ru":
            await query.message.edit_text(I18N["ru"]["lang_set"])
        elif lang == "uk":
            await query.message.edit_text(I18N["ru"]["lang_set_uk"])
        else:
            await query.message.edit_text(I18N["ru"]["lang_set_en"])
        await query.message.answer(t(uid, "welcome"))

@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(t(uid, "pricing"))

@dp.message(Command("admin"))
async def on_admin(message: Message):
    uid = message.from_user.id if message.from_user else 0
    if ADMIN_USER_ID and message.from_user and message.from_user.id == ADMIN_USER_ID:
        await message.answer(
            f"Users: {limiter.users_count()} | Total renders: {limiter.total_count()} | Paid credits: {user_credits.get(uid,0)}"
        )
    else:
        await message.answer("No permission.")

# ---------- Stars commands ----------
@dp.message(Command("buy"))
async def on_buy(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(t(uid, "buy_title"), reply_markup=buy_menu_keyboard(uid))

@dp.callback_query(F.data.startswith("buy:"))
async def on_buy_click(query: CallbackQuery):
    uid = query.from_user.id
    code = query.data.split(":", 1)[1]
    pack = PACKS.get(code)
    if not pack:
        await query.message.edit_text("Unknown pack.")
        await query.answer()
        return

    title, credits, amount_xtr = pack
    payload = code
    prices = [LabeledPrice(label=title, amount=amount_xtr)]

    # Stars: provider_token MUST be empty string, currency MUST be "XTR"
    await bot.send_invoice(
        chat_id=query.message.chat.id,
        title=title,
        description=f"{title} for MagicPhotoBot",
        payload=payload,
        provider_token="",   # Stars → empty
        currency="XTR",
        prices=prices
    )
    await query.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)

@dp.message(F.successful_payment)
async def process_success(message: Message):
    uid = message.from_user.id if message.from_user else 0
    sp = message.successful_payment
    payload = sp.invoice_payload  # "pack_3" / "pack_5" / "pack_10"
    pack = PACKS.get(payload)
    if not pack:
        await message.answer("Платёж получен, но пакет не распознан. Напишите администратору.")
        return

    title, credits, amount_xtr = pack
    user_credits[uid] = user_credits.get(uid, 0) + credits
    await message.answer(t(uid, "paid_ok").format(credits=credits, balance=user_credits[uid]))

@dp.message(Command("balance"))
async def on_balance(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(t(uid, "balance_title").format(credits=user_credits.get(uid, 0)))

# ---------- Photo -> Presets flow ----------
@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id if message.from_user else 0

    # если есть платные кредиты — не блокируем по бесплатному лимиту
    if user_credits.get(uid, 0) <= 0 and not limiter.can_use(uid):
        await message.answer(t(uid, "free_used"))
        return

    photo = message.photo[-1]
    pending_photo[uid] = {
        "file_id": photo.file_id,
        "caption": (message.caption or "").strip(),
    }
    await message.answer(
        t(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption=bool(pending_photo[uid]["caption"]))
    )

@dp.callback_query(F.data.startswith("preset:"))
async def on_preset(query: CallbackQuery):
    uid = query.from_user.id
    lang = user_lang.get(uid, DEFAULT_LANG)
    data = query.data.split(":", 1)[1]

    info = pending_photo.get(uid)
    if not info:
        await query.message.edit_text(t(uid, "fail"))
        return

    if data == "cancel":
        pending_photo.pop(uid, None)
        await query.message.edit_text(t(uid, "cancelled"))
        return

    # какой промпт использовать
    if data == "usecap":
        user_prompt = info["caption"] if info["caption"] else t(uid, "hint_prompt")
    else:
        idx = int(data) - 1
        presets = I18N.get(lang, I18N[DEFAULT_LANG])["presets"]
        if idx < 0 or idx >= len(presets):
            user_prompt = t(uid, "hint_prompt")
        else:
            user_prompt = presets[idx]

    try:
        await query.message.edit_text(t(uid, "status_work"))

        # URL файла в Telegram
        file_id = info["file_id"]
        file_info = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        # запомним, был ли платный кредит до генерации
        had_paid = user_credits.get(uid, 0) > 0

        # Генерация
        result = await animate_photo_via_replicate(source_image_url=file_url, prompt=user_prompt)

        if not result.get("ok"):
            code = result.get("code", "unknown")
            if code == "replicate_402":
                await query.message.edit_text(t(uid, "insufficient_credit"))
                return
            if code in ("replicate_auth", "config"):
                await query.message.edit_text(t(uid, "auth_error"))
                return
            if code == "replicate_422_fields":
                fields = result.get("fields") or []
                await query.message.edit_text(t(uid, "model_fields").format(fields=", ".join(fields)))
                return
            await query.message.edit_text(t(uid, "fail"))
            return

        video_url = result["url"]

        tmp_video_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{file_id}.mp4")
        await download_file(video_url, tmp_video_path)
        await bot.send_video(
            chat_id=query.message.chat.id,
            video=FSInputFile(tmp_video_path),
            caption="Готово! ✨",
            reply_markup=buy_cta_keyboard(),  # кнопки 3/5/10 звёзд сразу под видео
        )

        # списываем кредит или отмечаем бесплатное использование
        if had_paid and user_credits.get(uid, 0) > 0:
            user_credits[uid] -= 1
        else:
            limiter.mark_used(uid)

        try:
            os.remove(tmp_video_path)
        except Exception:
            pass

        # чистим состояние (не затираем сообщение с видео)
        pending_photo.pop(uid, None)

    except Exception as e:
        logger.exception("Preset flow failed: %s", e)
        await query.message.edit_text("Unexpected error. Please try again with another photo.")

def main():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()

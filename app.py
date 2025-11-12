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

# ---------------- i18n ----------------
DEFAULT_LANG = "ru"
user_lang: dict[int, str] = {}

I18N = {
    "ru": {
        "welcome": (
            "<b>Привет!</b> Пришли мне <b>фото</b> и, при желании, подпись-промпт.\n"
            "Я сделаю короткое видео из изображения.\n\n"
            "Подсказка: лучше всего работают фронтальные портреты с хорошим светом."
        ),
        "pricing": "<b>Тарифы:</b>\n• 1 бесплатное видео\n• Пакеты — TON / USDT / Stars",
        "invite_only": "Бот временно доступен по инвайту.",
        "free_used": "Вы использовали бесплатное видео. Смотрите /pricing или /buy",
        "status_work": "🎞 Обрабатываю фото... ~20–60 секунд",
        "fail": "Не удалось оживить. Попробуйте другое фото.",
        "done": "Готово! ✨",
        "choose_preset": "Выберите стиль:",
        "btn_use_caption": "✍️ Мой промпт",
        "btn_cancel": "✖️ Отмена",
        "cancelled": "Отменено.",
        "buy_title": "Выберите пакет:",
        "buy_btn_1": "1 фото — 150 ⭐",
        "buy_btn_3": "3 фото — 300 ⭐",
        "buy_btn_5": "5 фото — 450 ⭐",
        "buy_btn_10": "10 фото — 800 ⭐",
        "balance_title": "💰 Баланс\n• Кредиты: {credits}",
        "paid_ok": "✅ Оплата успешна! Добавлено {credits} оживлений. Баланс: {balance}."
    }
}

# -------- Presets (9 вариантов) --------
PRESET_PROMPTS = [
    "natural smile, slight head turn right, photorealistic",                     # 1 Natural smile
    "cinematic portrait, subtle breathing, soft studio light, 24fps",           # 2 Cinematic look
    "gentle movement, hair flutter, soft focus, ethereal glow",                 # 3 Dreamy motion
    "smile softly, natural head tilt, expressive eyes, warm tone lighting",     # 4 Expressive vibe
    "gentle eye blink, slow smile, cinematic lighting, photorealistic",         # 5 Blink & glow
    "subtle wink, slight smile, natural head motion, photorealistic lighting",  # 6 Wink
    "vintage 35mm film look, soft focus, warm tones, subtle motion",            # 7 Vintage film
    "dramatic lighting, strong shadows, cinematic mood, expressive face",       # 8 Dramatic lighting
    "editorial portrait, soft bounce light, slight head movement, elegant expression", # 9 Editorial portrait
]

def lang_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="Українська", callback_data="lang:uk"),
                InlineKeyboardButton(text="English", callback_data="lang:en"),
            ]
        ]
    )

pending_photo: dict[int, dict] = {}

def preset_keyboard(uid: int, has_caption: bool) -> InlineKeyboardMarkup:
    titles = [
        "😊 Natural smile",
        "🎬 Cinematic look",
        "🕊️ Dreamy motion",
        "🔥 Expressive vibe",
        "💡 Blink & glow",
        "😉 Wink",
        "🎞 Vintage film",
        "💥 Dramatic lighting",
        "🖼 Editorial portrait",
    ]
    kb = [[InlineKeyboardButton(text=titles[i], callback_data=f"preset:{i+1}")] for i in range(len(titles))]
    row2 = []
    if has_caption:
        row2.append(InlineKeyboardButton(text="✍️ Мой промпт", callback_data="preset:usecap"))
    row2.append(InlineKeyboardButton(text="✖️ Отмена", callback_data="preset:cancel"))
    kb.append(row2)
    return InlineKeyboardMarkup(inline_keyboard=kb)

# -------- Оплата Stars --------
PACKS = {
    "pack_1": ("1 animation", 1, 150),
    "pack_3": ("3 animations", 3, 300),
    "pack_5": ("5 animations", 5, 450),
    "pack_10": ("10 animations", 10, 800),
}
user_credits: dict[int, int] = {}

def buy_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 фото — 150 ⭐", callback_data="buy:pack_1")],
        [InlineKeyboardButton(text="3 фото — 300 ⭐", callback_data="buy:pack_3")],
        [InlineKeyboardButton(text="5 фото — 450 ⭐", callback_data="buy:pack_5")],
        [InlineKeyboardButton(text="10 фото — 800 ⭐", callback_data="buy:pack_10")],
    ])

def buy_cta_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💫 1 фото — 150 ⭐", callback_data="buy:pack_1")],
        [InlineKeyboardButton(text="💫 3 фото — 300 ⭐", callback_data="buy:pack_3"),
         InlineKeyboardButton(text="💫 5 фото — 450 ⭐", callback_data="buy:pack_5")],
        [InlineKeyboardButton(text="💫 10 фото — 800 ⭐", callback_data="buy:pack_10")],
    ])

# -------- Handlers --------
@dp.message(CommandStart())
async def on_start(message: Message):
    await message.answer(I18N["ru"]["welcome"])

@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    await message.answer(I18N["ru"]["pricing"])

@dp.message(Command("buy"))
async def on_buy(message: Message):
    await message.answer(I18N["ru"]["buy_title"], reply_markup=buy_menu_keyboard(message.from_user.id))

@dp.callback_query(F.data.startswith("buy:"))
async def on_buy_click(query: CallbackQuery):
    code = query.data.split(":", 1)[1]
    pack = PACKS.get(code)
    if not pack:
        await query.answer("Unknown pack")
        return
    title, credits, amount = pack
    prices = [LabeledPrice(label=title, amount=amount)]
    await bot.send_invoice(
        chat_id=query.message.chat.id,
        title=title,
        description=f"{title} for MagicPhotoBot",
        payload=code,
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await query.answer()

@dp.pre_checkout_query()
async def on_checkout(pre: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre.id, ok=True)

@dp.message(F.successful_payment)
async def on_payment(message: Message):
    uid = message.from_user.id
    code = message.successful_payment.invoice_payload
    pack = PACKS.get(code)
    if not pack:
        await message.answer("Платёж получен, но пакет не распознан.")
        return
    title, credits, _ = pack
    user_credits[uid] = user_credits.get(uid, 0) + credits
    await message.answer(I18N["ru"]["paid_ok"].format(credits=credits, balance=user_credits[uid]))

@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id
    if user_credits.get(uid, 0) <= 0 and not limiter.can_use(uid):
        await message.answer(I18N["ru"]["free_used"])
        return
    photo = message.photo[-1]
    pending_photo[uid] = {"file_id": photo.file_id, "caption": (message.caption or "").strip()}
    await message.answer(I18N["ru"]["choose_preset"],
                         reply_markup=preset_keyboard(uid, has_caption=bool(message.caption)))

@dp.callback_query(F.data.startswith("preset:"))
async def on_preset(query: CallbackQuery):
    uid = query.from_user.id
    data = query.data.split(":", 1)[1]
    info = pending_photo.get(uid)
    if not info:
        await query.message.edit_text(I18N["ru"]["fail"])
        return
    if data == "cancel":
        pending_photo.pop(uid, None)
        await query.message.edit_text(I18N["ru"]["cancelled"])
        return
    if data == "usecap":
        prompt = info["caption"] or "natural smile, subtle head motion, cinematic lighting"
    else:
        idx = int(data) - 1
        prompt = PRESET_PROMPTS[idx] if 0 <= idx < len(PRESET_PROMPTS) else "natural smile"
    try:
        await query.message.edit_text(I18N["ru"]["status_work"])
        file_info = await bot.get_file(info["file_id"])
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        had_paid = user_credits.get(uid, 0) > 0
        result = await animate_photo_via_replicate(source_image_url=file_url, prompt=prompt)
        if not result.get("ok"):
            await query.message.edit_text(I18N["ru"]["fail"])
            return
        video_url = result["url"]
        tmp_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{info['file_id']}.mp4")
        await download_file(video_url, tmp_path)
        await bot.send_video(chat_id=query.message.chat.id,
                             video=FSInputFile(tmp_path),
                             caption=I18N["ru"]["done"],
                             reply_markup=buy_cta_keyboard(uid))
        if had_paid and user_credits[uid] > 0:
            user_credits[uid] -= 1
        else:
            limiter.mark_used(uid)
        os.remove(tmp_path)
        pending_photo.pop(uid, None)
    except Exception as e:
        logger.exception("Animation error: %s", e)
        await query.message.edit_text("Ошибка при обработке. Попробуйте другое фото.")

def main():
    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()

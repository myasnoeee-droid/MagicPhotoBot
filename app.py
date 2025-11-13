import os
import asyncio
import logging
import json
from pathlib import Path
from typing import Dict

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.filters import CommandStart, Command
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties

from dotenv import load_dotenv

from limiter import FreeUsageLimiter
from processing import animate_photo_via_replicate, download_file

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger("magicphotobot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))  # опционально: чат/канал для поддержки
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x]
MAX_FREE_ANIMS_PER_USER = int(os.getenv("MAX_FREE_ANIMS_PER_USER", "1"))
DOWNLOAD_TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
limiter = FreeUsageLimiter(max_free=MAX_FREE_ANIMS_PER_USER)

# ---------- i18n через JSON-файлы ----------
LOCALE_CODES = ("ua", "en", "es", "pt")
DEFAULT_LANG = "en"
LOCALES: Dict[str, Dict[str, str]] = {}
user_lang: Dict[int, str] = {}  # user_id -> "ua"/"en"/"es"/"pt"


def load_locales():
    base = Path(__file__).parent / "locales"
    for code in LOCALE_CODES:
        path = base / f"{code}.json"
        if not path.exists():
            logger.warning("Locale file not found: %s", path)
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                LOCALES[code] = json.load(f)
            logger.info("Loaded locale %s from %s", code, path)
        except Exception as e:
            logger.exception("Failed to load locale %s: %s", code, e)


load_locales()
if DEFAULT_LANG not in LOCALES:
    raise RuntimeError("Default locale not loaded (check locales/en.json).")


def get_lang(uid: int) -> str:
    return user_lang.get(uid, DEFAULT_LANG)


def tr(uid: int, key: str) -> str:
    lang = get_lang(uid)
    loc = LOCALES.get(lang) or LOCALES[DEFAULT_LANG]
    return loc.get(key, LOCALES[DEFAULT_LANG].get(key, ""))


def tr_lang(lang: str, key: str) -> str:
    loc = LOCALES.get(lang) or LOCALES[DEFAULT_LANG]
    return loc.get(key, LOCALES[DEFAULT_LANG].get(key, ""))


def lang_choice_keyboard() -> InlineKeyboardMarkup:
    # Магический экран выбора языка
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang:ua"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ],
            [
                InlineKeyboardButton(text="🇪🇸 Español", callback_data="lang:es"),
                InlineKeyboardButton(text="🇵🇹 Português", callback_data="lang:pt"),
            ],
        ]
    )


# ---------- Пресеты (региональные) ----------

# Базовые EN-промпты (fallback для всех)
PRESET_PROMPTS_BASE = [
    "natural smile, slight head turn right, photorealistic",                     # 1 Natural smile
    "cinematic portrait, subtle breathing, soft studio light, 24fps",           # 2 Cinematic look
    "gentle movement, hair flutter, soft focus, ethereal glow",                 # 3 Dreamy motion
    "smile softly, natural head tilt, expressive eyes, warm tone lighting",     # 4 Expressive vibe
    "gentle eye blink, slow smile, cinematic lighting, photorealistic",         # 5 Blink & glow
    "subtle wink, slight smile, natural head motion, photorealistic lighting",  # 6 Wink
    "vintage 35mm film look, soft focus, warm tones, subtle motion",            # 7 Vintage film
    "dramatic lighting, strong shadows, cinematic mood, expressive face",       # 8 Dramatic lighting
    "editorial portrait, soft bounce light, slight head movement, elegant expression",  # 9 Editorial portrait
]

# Языковые вариации промптов (все на EN, но с нюансами под регион)
PRESET_PROMPTS_BY_LANG: Dict[str, list[str]] = {
    "ua": PRESET_PROMPTS_BASE,
    "en": PRESET_PROMPTS_BASE,
    "es": [
        "warm natural smile, slight head turn right, photorealistic skin texture",        # 1
        "cinematic close-up portrait, subtle breathing, soft studio light, 24fps",        # 2
        "gentle flowing movement, light hair flutter, dreamy soft focus, ethereal glow",  # 3
        "soft smile, relaxed head tilt, very expressive eyes, warm golden lighting",      # 4
        "slow gentle eye blink, slow smile, cinematic contrast, photorealistic detail",   # 5
        "playful subtle wink, small smile, natural head motion, beauty lighting",         # 6
        "nostalgic vintage 35mm film look, film grain, warm tones, subtle motion",        # 7
        "strong dramatic lighting, deep shadows, intense cinematic mood, expressive face",# 8
        "fashion editorial portrait, soft bounce light, elegant slow head movement",      # 9
    ],
    "pt": [
        "soft natural smile, slight head turn, realistic skin and eyes",                  # 1
        "cinematic portrait shot, calm breathing, soft studio light, 24fps look",         # 2
        "smooth gentle movement, light hair motion, dreamy soft focus, glow",             # 3
        "soft sweet smile, natural head tilt, warm expressive eyes, cozy lighting",       # 4
        "gentle eye blink, slow friendly smile, cinematic lighting, realistic details",   # 5
        "cute subtle wink, light smile, natural head motion, flattering light",           # 6
        "retro 35mm film style, film grain, warm nostalgic tones, subtle motion",         # 7
        "cinematic dramatic lighting, strong contrast, emotional portrait, deep shadows", # 8
        "elegant editorial portrait, soft studio bounce light, slow refined movement",    # 9
    ],
}


def get_preset_prompt(lang: str, idx: int) -> str:
    arr = PRESET_PROMPTS_BY_LANG.get(lang) or PRESET_PROMPTS_BASE
    if 0 <= idx < len(arr):
        return arr[idx]
    return PRESET_PROMPTS_BASE[0]


# Локализованные подписи кнопок пресетов (названия, не промпты)
PRESET_TITLES: Dict[str, list[str]] = {
    "en": [
        "😊 Natural smile",
        "🎬 Cinematic look",
        "🕊️ Dreamy motion",
        "🔥 Expressive vibe",
        "💡 Blink & glow",
        "😉 Wink",
        "🎞 Vintage film",
        "💥 Dramatic lighting",
        "🖼 Editorial portrait",
    ],
    "ua": [
        "😊 Natural smile",
        "🎬 Cinematic look",
        "🕊️ Dreamy motion",
        "🔥 Expressive vibe",
        "💡 Blink & glow",
        "😉 Wink",
        "🎞 Vintage film",
        "💥 Dramatic lighting",
        "🖼 Editorial portrait",
    ],
    "es": [
        "😊 Sonrisa natural",
        "🎬 Look cinematográfico",
        "🕊️ Movimiento suave",
        "🔥 Vibras expresivas",
        "💡 Parpadeo suave & brillo",
        "😉 Guiño sutil",
        "🎞 Estilo película vintage",
        "💥 Iluminación dramática",
        "🖼 Retrato editorial",
    ],
    "pt": [
        "😊 Sorriso natural",
        "🎬 Visual cinematográfico",
        "🕊️ Movimento suave",
        "🔥 Vibração expressiva",
        "💡 Piscar suave & brilho",
        "😉 Piscadinha sutil",
        "🎞 Filme vintage 35mm",
        "💥 Iluminação dramática",
        "🖼 Retrato editorial",
    ],
}

pending_photo: Dict[int, Dict[str, str]] = {}  # user_id -> {"file_id":..., "caption":...}

def preset_keyboard(uid: int, has_caption: bool) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])
    kb = [
        [InlineKeyboardButton(text=titles[i], callback_data=f"preset:{i+1}")]
        for i in range(len(titles))
    ]
    # нижний ряд — свой промпт / отмена (локализовано)
    row2 = []
    if has_caption:
        row2.append(
            InlineKeyboardButton(
                text=tr(uid, "btn_use_caption"),
                callback_data="preset:usecap",
            )
        )
    row2.append(
        InlineKeyboardButton(
            text=tr(uid, "btn_cancel"),
            callback_data="preset:cancel",
        )
    )
    kb.append(row2)
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ---------- Stars (XTR) тарифы ----------
PACKS = {
    "pack_1": ("1 animation", 1, 150),
    "pack_3": ("3 animations", 3, 300),
    "pack_5": ("5 animations", 5, 450),
    "pack_10": ("10 animations", 10, 800),
}
user_credits: Dict[int, int] = {}  # user_id -> credits


def buy_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr_lang(lang, "buy_btn_1"), callback_data="buy:pack_1")],
            [InlineKeyboardButton(text=tr_lang(lang, "buy_btn_3"), callback_data="buy:pack_3")],
            [InlineKeyboardButton(text=tr_lang(lang, "buy_btn_5"), callback_data="buy:pack_5")],
            [InlineKeyboardButton(text=tr_lang(lang, "buy_btn_10"), callback_data="buy:pack_10")],
        ]
    )


def buy_cta_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    t1 = "💫 " + tr_lang(lang, "buy_btn_1")
    t3 = "💫 " + tr_lang(lang, "buy_btn_3")
    t5 = "💫 " + tr_lang(lang, "buy_btn_5")
    t10 = "💫 " + tr_lang(lang, "buy_btn_10")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t1, callback_data="buy:pack_1")],
            [
                InlineKeyboardButton(text=t3, callback_data="buy:pack_3"),
                InlineKeyboardButton(text=t5, callback_data="buy:pack_5"),
            ],
            [InlineKeyboardButton(text=t10, callback_data="buy:pack_10")],
        ]
    )


# ---------- Главное меню (ReplyKeyboard) ----------

MENU_BUTTONS = {
    "ua": {
        "animate": "🪄 Оживити фото",
        "buy": "💫 Купити генерації",
        "support": "🆘 Підтримка",
        "share": "📤 Розповісти друзям",
        "balance": "💰 Баланс",
    },
    "en": {
        "animate": "🪄 Animate photo",
        "buy": "💫 Buy generations",
        "support": "🆘 Support",
        "share": "📤 Tell friends",
        "balance": "💰 Balance",
    },
    "es": {
        "animate": "🪄 Animar foto",
        "buy": "💫 Comprar generaciones",
        "support": "🆘 Soporte",
        "share": "📤 Contar a amigos",
        "balance": "💰 Balance",
    },
    "pt": {
        "animate": "🪄 Animar foto",
        "buy": "💫 Comprar gerações",
        "support": "🆘 Suporte",
        "share": "📤 Contar aos amigos",
        "balance": "💰 Saldo",
    },
}


def get_menu_labels(lang: str) -> Dict[str, str]:
    return MENU_BUTTONS.get(lang, MENU_BUTTONS["en"])


def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    lang = get_lang(uid)
    labels = get_menu_labels(lang)
    kb = ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text=labels["animate"])],
            [
                KeyboardButton(text=labels["buy"]),
                KeyboardButton(text=labels["balance"]),
            ],
            [
                KeyboardButton(text=labels["support"]),
                KeyboardButton(text=labels["share"]),
            ],
        ],
    )
    return kb


# ---------- Состояние поддержки ----------
awaiting_support: Dict[int, bool] = {}  # user_id -> True/False


# ---------- Handlers ----------

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer(
            LOCALES[DEFAULT_LANG].get("invite_only", "Invite only. Contact admin.")
        )
        return

    uid = message.from_user.id if message.from_user else 0

    if uid not in user_lang:
        text = (
            "🧙‍♂️ <b>Magl’sBot вітає тебе, мандрівнику-магу!</b>\n\n"
            "✨ Обери мову чарівної книги:"
        )
        await message.answer(text, reply_markup=lang_choice_keyboard())
        return

    await message.answer(tr(uid, "welcome"), reply_markup=main_menu_keyboard(uid))


@dp.callback_query(F.data.startswith("lang:"))
async def on_lang_set(query: CallbackQuery):
    uid = query.from_user.id
    _, code = query.data.split(":", 1)
    if code not in LOCALES:
        await query.answer("Language not available", show_alert=True)
        return
    user_lang[uid] = code
    await query.message.edit_text(tr(uid, "lang_set"))
    await query.message.answer(
        tr(uid, "welcome"),
        reply_markup=main_menu_keyboard(uid)
    )
    await query.answer()


@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(tr(uid, "pricing"))


@dp.message(Command("buy"))
async def on_buy(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(tr(uid, "buy_title"), reply_markup=buy_menu_keyboard(uid))


@dp.message(Command("balance"))
async def on_balance(message: Message):
    uid = message.from_user.id if message.from_user else 0
    await message.answer(
        tr(uid, "balance_title").format(credits=user_credits.get(uid, 0))
    )


@dp.message(Command("menu"))
async def on_menu(message: Message):
    uid = message.from_user.id if message.from_user else 0
    awaiting_support.pop(uid, None)
    await message.answer("Меню оновлено ⬇️", reply_markup=main_menu_keyboard(uid))


@dp.callback_query(F.data.startswith("buy:"))
async def on_buy_click(query: CallbackQuery):
    uid = query.from_user.id
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
        description=f"{title} for Magl’sBot",
        payload=code,
        provider_token="",  # Stars
        currency="XTR",
        prices=prices,
    )
    await query.answer()


@dp.pre_checkout_query()
async def on_checkout(pre: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre.id, ok=True)


@dp.message(F.successful_payment)
async def on_payment(message: Message):
    uid = message.from_user.id if message.from_user else 0
    sp = message.successful_payment
    payload = sp.invoice_payload
    pack = PACKS.get(payload)
    if not pack:
        await message.answer("Payment received, but pack not recognized. Contact admin.")
        return
    title, credits, amount = pack
    user_credits[uid] = user_credits.get(uid, 0) + credits
    await message.answer(
        tr(uid, "paid_ok").format(
            credits=credits,
            balance=user_credits[uid],
        )
    )


# ---------- Главное меню: текстовые кнопки + поддержка ----------

@dp.message(F.text)
async def on_text(message: Message):
    text = message.text or ""
    uid = message.from_user.id if message.from_user else 0
    lang = get_lang(uid)
    labels = get_menu_labels(lang)

    # 1) Сначала проверяем — это одна из кнопок меню?
    if text == labels["animate"]:
        awaiting_support.pop(uid, None)
        await message.answer(
            {
                "ua": "🪄 Надішли мені фото, і я оживлю його. Найкраще працюють фронтальні портрети з хорошим світлом.",
                "en": "🪄 Send me a photo and I’ll animate it. Front-facing portraits with good light work best.",
                "es": "🪄 Envíame una foto y la animaré. Los retratos frontales con buena luz funcionan mejor.",
                "pt": "🪄 Envie uma foto e eu vou animá-la. Retratos de frente com boa iluminação funcionam melhor.",
            }.get(lang, "🪄 Send me a photo and I’ll animate it.")
        )
        return

    if text == labels["buy"]:
        awaiting_support.pop(uid, None)
        await message.answer(tr(uid, "buy_title"), reply_markup=buy_menu_keyboard(uid))
        return

    if text == labels["balance"]:
        awaiting_support.pop(uid, None)
        await message.answer(
            tr(uid, "balance_title").format(credits=user_credits.get(uid, 0))
        )
        return

    if text == labels["support"]:
        # Включаем режим поддержки
        awaiting_support[uid] = True
        msg = {
            "ua": "🆘 Напиши, будь ласка, своє запитання або проблему одним повідомленням — я передам це живому магу підтримки.",
            "en": "🆘 Please write your question or issue in one message — I’ll send it to the human support wizard.",
            "es": "🆘 Escribe tu pregunta o problema en un solo mensaje — lo enviaré al mago de soporte humano.",
            "pt": "🆘 Escreva sua pergunta ou problema em uma única mensagem — eu vou enviar para o mago humano de suporte.",
        }.get(lang, "🆘 Please write your question in one message — I’ll send it to human support.")
        await message.answer(msg)
        return

    if text == labels["share"]:
        awaiting_support.pop(uid, None)
        share_texts = {
            "ua": (
                "📤 Поділись ботом з друзями:\n"
                "Оживляємо фото в стилі Гаррі Поттера 🎬🪄\n"
                "https://t.me/LIvePotterPhotoBot"
            ),
            "en": (
                "📤 Share this bot with friends:\n"
                "We animate photos like in Harry Potter portraits 🎬🪄\n"
                "https://t.me/LIvePotterPhotoBot"
            ),
            "es": (
                "📤 Comparte este bot con tus amigos:\n"
                "Animamos fotos como los retratos de Harry Potter 🎬🪄\n"
                "https://t.me/LIvePotterPhotoBot"
            ),
            "pt": (
                "📤 Compartilhe este bot com seus amigos:\n"
                "Animamos fotos como nos retratos de Harry Potter 🎬🪄\n"
                "https://t.me/LIvePotterPhotoBot"
            ),
        }
        await message.answer(share_texts.get(lang, share_texts["en"]))
        return

    # 2) Если это не кнопка меню — возможно, это сообщение для поддержки
    if awaiting_support.get(uid):
        # Куда слать: SUPPORT_CHAT_ID > ADMIN_USER_ID
        dest = SUPPORT_CHAT_ID or ADMIN_USER_ID
        if dest:
            username = (message.from_user.username if message.from_user else None) or "unknown"
            header = f"📩 Support message from @{username} (id={uid}):"
            try:
                await bot.send_message(
                    chat_id=dest,
                    text=f"{header}\n\n{text}"
                )
                confirm = {
                    "ua": "✅ Дякую! Я передав твоє повідомлення магу підтримки. Він відповість, щойно зможе.",
                    "en": "✅ Thanks! I’ve sent your message to support. They will reply as soon as possible.",
                    "es": "✅ ¡Gracias! He enviado tu mensaje al soporte. Te responderán lo antes posible.",
                    "pt": "✅ Obrigado! Eu enviei sua mensagem para o suporte. Eles vão responder assim que possível.",
                }.get(lang, "✅ Thanks! I’ve sent your message to support.")
                await message.answer(confirm)
            except Exception as e:
                logger.exception("Failed to send support message: %s", e)
                await message.answer("⚠️ Support is temporarily unavailable. Please try again later.")
        else:
            await message.answer("⚠️ Support is not configured yet. Contact bot admin.")
        awaiting_support.pop(uid, None)
        return

    # 3) Иначе просто игнорируем текст — другие хэндлеры (фото и т.п.) его подхватят/или нет
    # Ничего не делаем здесь


# ---------- Фото + пресеты ----------

@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id if message.from_user else 0
    awaiting_support.pop(uid, None)

    if user_credits.get(uid, 0) <= 0 and not limiter.can_use(uid):
        await message.answer(tr(uid, "free_used"))
        return

    photo = message.photo[-1]
    pending_photo[uid] = {
        "file_id": photo.file_id,
        "caption": (message.caption or "").strip(),
    }

    await message.answer(
        tr(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption=bool(pending_photo[uid]["caption"])),
    )


@dp.callback_query(F.data.startswith("preset:"))
async def on_preset(query: CallbackQuery):
    uid = query.from_user.id
    data = query.data.split(":", 1)[1]
    info = pending_photo.get(uid)

    if not info:
        await query.message.edit_text(tr(uid, "fail"))
        return

    if data == "cancel":
        pending_photo.pop(uid, None)
        await query.message.edit_text(tr(uid, "cancelled"))
        return

    if data == "usecap":
        prompt = info["caption"] or "natural smile, subtle head motion, cinematic lighting"
    else:
        idx = int(data) - 1
        lang = get_lang(uid)
        prompt = get_preset_prompt(lang, idx)

    try:
        await query.message.edit_text(tr(uid, "status_work"))

        file_info = await bot.get_file(info["file_id"])
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        had_paid = user_credits.get(uid, 0) > 0

        result = await animate_photo_via_replicate(
            source_image_url=file_url,
            prompt=prompt,
        )
        if not result.get("ok"):
            await query.message.edit_text(tr(uid, "fail"))
            return

        video_url = result["url"]
        tmp_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{info['file_id']}.mp4")
        await download_file(video_url, tmp_path)

        await bot.send_video(
            chat_id=query.message.chat.id,
            video=FSInputFile(tmp_path),
            caption=tr(uid, "done"),
            reply_markup=buy_cta_keyboard(uid),
        )

        if had_paid and user_credits.get(uid, 0) > 0:
            user_credits[uid] -= 1
        else:
            limiter.mark_used(uid)

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        pending_photo.pop(uid, None)

    except Exception as e:
        logger.exception("Animation error: %s", e)
        await query.message.edit_text("Error while processing. Try another photo.")


def main():
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()

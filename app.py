import os
import asyncio
import logging
import json
import random
from pathlib import Path
from typing import Dict, Any

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
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))  # чат/канал для поддержки (опц.)
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x]
MAX_FREE_ANIMS_PER_USER = int(os.getenv("MAX_FREE_ANIMS_PER_USER", "1"))
DOWNLOAD_TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp")

# Заставка — оживлённое видео Гарри Поттера (file_id из Telegram / .env)
INTRO_VIDEO_FILE_ID = os.getenv("INTRO_VIDEO_FILE_ID", "")

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
LOCALES: Dict[str, Dict[str, Any]] = {}
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
    "natural smile, slight head turn right, photorealistic",                     # 0 Natural smile
    "cinematic portrait, subtle breathing, soft studio light, 24fps",           # 1 Cinematic look
    "gentle movement, hair flutter, soft focus, ethereal glow",                 # 2 Dreamy motion
    "smile softly, natural head tilt, expressive eyes, warm tone lighting",     # 3 Expressive vibe
    "gentle eye blink, slow smile, cinematic lighting, photorealistic",         # 4 Blink & glow (рекомендованный)
    "subtle wink, slight smile, natural head motion, photorealistic lighting",  # 5 Wink
    "vintage 35mm film look, soft focus, warm tones, subtle motion",            # 6 Vintage film
    "dramatic lighting, strong shadows, cinematic mood, expressive face",       # 7 Dramatic lighting
    "editorial portrait, soft bounce light, slight head movement, elegant expression"  # 8 Editorial portrait
]

# Языковые вариации промптов (все на EN, но адаптированы под регион)
PRESET_PROMPTS_BY_LANG: Dict[str, list[str]] = {
    "ua": PRESET_PROMPTS_BASE,
    "en": PRESET_PROMPTS_BASE,
    "es": [
        "warm natural smile, slight head turn right, photorealistic skin texture",
        "cinematic close-up portrait, subtle breathing, soft studio light, 24fps",
        "gentle flowing movement, light hair flutter, dreamy soft focus, ethereal glow",
        "soft smile, relaxed head tilt, very expressive eyes, warm golden lighting",
        "slow gentle eye blink, slow smile, cinematic contrast, photorealistic detail",
        "playful subtle wink, small smile, natural head motion, beauty lighting",
        "nostalgic vintage 35mm film look, film grain, warm tones, subtle motion",
        "strong dramatic lighting, deep shadows, intense cinematic mood, expressive face",
        "fashion editorial portrait, soft bounce light, elegant slow head movement"
    ],
    "pt": [
        "soft natural smile, slight head turn, realistic skin and eyes",
        "cinematic portrait shot, calm breathing, soft studio light, 24fps look",
        "smooth gentle movement, light hair motion, dreamy soft focus, glow",
        "soft sweet smile, natural head tilt, warm expressive eyes, cozy lighting",
        "gentle eye blink, slow friendly smile, cinematic lighting, realistic details",
        "cute subtle wink, light smile, natural head motion, flattering light",
        "retro 35mm film style, film grain, warm nostalgic tones, subtle motion",
        "cinematic dramatic lighting, strong contrast, emotional portrait, deep shadows",
        "elegant editorial portrait, soft studio bounce light, slow refined movement"
    ],
}


def get_preset_prompt(lang: str, idx: int) -> str:
    arr = PRESET_PROMPTS_BY_LANG.get(lang) or PRESET_PROMPTS_BASE
    if 0 <= idx < len(arr):
        return arr[idx]
    return PRESET_PROMPTS_BASE[0]


# Локализованные подписи кнопок пресетов
PRESET_TITLES: Dict[str, list[str]] = {
    "en": [
        "😊 Natural smile",
        "🎬 Cinematic look",
        "🕊️ Dreamy motion",
        "🔥 Expressive vibe",
        "💡 Blink & Glow ⭐ recommended for old photos",
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
        "💡 Blink & Glow ⭐ рекомендовано для старих фото",
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
        "💡 Parpadeo suave & brillo ⭐ ideal para fotos antiguas",
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
        "💡 Piscar suave & brilho ⭐ ideal para fotos antigas",
        "😉 Piscadinha sutil",
        "🎞 Filme vintage 35mm",
        "💥 Iluminação dramática",
        "🖼 Retrato editorial",
    ],
}

# pending_* — состояние диалога
pending_photo: Dict[int, Dict[str, Any]] = {}   # user_id -> {"file_id":..., "caption":..., "is_old_like": bool}
pending_choice: Dict[int, Dict[str, Any]] = {}  # user_id -> {"type": "preset"/"caption", "idx": int | None}


def preset_keyboard(uid: int, has_caption: bool) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])

    # Лейбл для Random magic
    random_labels = {
        "ua": "✨ Random magic",
        "en": "✨ Random magic",
        "es": "✨ Magia aleatoria",
        "pt": "✨ Magia aleatória",
    }
    random_text = random_labels.get(lang, "✨ Random magic")

    rows: list[list[InlineKeyboardButton]] = []

    # Первая строка — Random magic
    rows.append(
        [InlineKeyboardButton(text=random_text, callback_data="preset:random")]
    )

    # Далее — все пресеты по одному в строке
    for i in range(len(titles)):
        rows.append(
            [InlineKeyboardButton(text=titles[i], callback_data=f"preset:{i+1}")]
        )

    # Последняя строка — использовать caption (если есть) + отмена
    row_last: list[InlineKeyboardButton] = []
    if has_caption:
        row_last.append(
            InlineKeyboardButton(
                text=tr(uid, "btn_use_caption"),
                callback_data="preset:usecap",
            )
        )
    row_last.append(
        InlineKeyboardButton(
            text=tr(uid, "btn_cancel"),
            callback_data="preset:cancel",
        )
    )
    rows.append(row_last)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_preset_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    confirm_labels = {
        "ua": "✅ Запустити",
        "en": "✅ Start",
        "es": "✅ Iniciar",
        "pt": "✅ Iniciar",
    }
    back_labels = {
        "ua": "🔙 Назад",
        "en": "🔙 Back",
        "es": "🔙 Volver",
        "pt": "🔙 Voltar",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=confirm_labels.get(lang, "✅ Start"),
                    callback_data="confirm:ok"
                )
            ],
            [
                InlineKeyboardButton(
                    text=back_labels.get(lang, "🔙 Back"),
                    callback_data="confirm:back"
                )
            ],
        ]
    )

# ---------- Stars (XTR) тарифы и кредиты ----------

# title, credits, amount_in_stars
PACKS = {
    "pack_1": ("1 animation", 1, 60),
    "pack_3": ("3 animations", 3, 150),
    "pack_5": ("5 animations", 5, 300),
    "pack_10": ("10 animations", 10, 500),
}
user_credits: Dict[int, int] = {}  # user_id -> credits

# ----- Рефералка -----
ref_inviter: Dict[int, int] = {}         # кто кого пригласил: invited_id -> inviter_id
ref_count: Dict[int, int] = {}           # сколько людей привёл каждый юзер: inviter_id -> count
ref_stars_balance: Dict[int, int] = {}   # накопленные реферальные Stars (для конвертации в кредиты)


def buy_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    """
    Клавиатура для /buy и кнопки «Купить генерации».
    Популярный пакет (3 оживления) — первым, с 🔥.
    Каждая кнопка в отдельной строке.
    """
    lang = get_lang(uid)

    popular_text = "🔥 " + tr_lang(lang, "buy_btn_3")

    buttons = [
        InlineKeyboardButton(
            text=popular_text,
            callback_data="buy:pack_3",
        ),
        InlineKeyboardButton(
            text=tr_lang(lang, "buy_btn_5"),
            callback_data="buy:pack_5",
        ),
        InlineKeyboardButton(
            text=tr_lang(lang, "buy_btn_10"),
            callback_data="buy:pack_10",
        ),
        InlineKeyboardButton(
            text=tr_lang(lang, "buy_btn_1"),
            callback_data="buy:pack_1",
        ),
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=[[b] for b in buttons]  # каждая кнопка в своей строке
    )


def buy_cta_keyboard(uid: int) -> InlineKeyboardMarkup:
    """
    Клавиатура, которая показывается под готовым видео.
    Пакеты + кнопка «Поделиться ботом» (с реф-ссылкой).
    """
    lang = get_lang(uid)

    popular_text = "🔥 " + tr_lang(lang, "buy_btn_3")

    buy_buttons = [
        InlineKeyboardButton(
            text=popular_text,
            callback_data="buy:pack_3",
        ),
        InlineKeyboardButton(
            text="💫 " + tr_lang(lang, "buy_btn_5"),
            callback_data="buy:pack_5",
        ),
        InlineKeyboardButton(
            text="💫 " + tr_lang(lang, "buy_btn_10"),
            callback_data="buy:pack_10",
        ),
        InlineKeyboardButton(
            text="💫 " + tr_lang(lang, "buy_btn_1"),
            callback_data="buy:pack_1",
        ),
    ]

    share_labels = {
        "ua": "📤 Поділитися ботом",
        "en": "📤 Share this bot",
        "es": "📤 Compartir el bot",
        "pt": "📤 Compartilhar o bot",
    }
    ref_link = f"https://t.me/LIvePotterPhotoBot?start=ref_{uid}"
    share_button = InlineKeyboardButton(
        text=share_labels.get(lang, share_labels["en"]),
        url=ref_link,
    )

    rows = [[b] for b in buy_buttons]
    rows.append([share_button])

    return InlineKeyboardMarkup(inline_keyboard=rows)

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
        "share": "📤 Compartir",
        "balance": "💰 Balance",
    },
    "pt": {
        "animate": "🪄 Animar foto",
        "buy": "💫 Comprar gerações",
        "support": "🆘 Suporte",
        "share": "📤 Compartilhar",
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

# ---------- Поддержка (support) ----------

awaiting_support: Dict[int, bool] = {}  # user_id -> waiting for support message

# ---------- АДМИНСКИЕ СЧЁТЧИКИ И TEST MODE ----------

TEST_MODE = False  # если True — для ADMIN_USER_ID анимации не списывают кредиты/фри лимиты
pack_stats: Dict[str, int] = {key: 0 for key in PACKS.keys()}
gen_success: int = 0
gen_fail: int = 0


def admin_keyboard() -> InlineKeyboardMarkup:
    mode = "🧪 Test mode: ON" if TEST_MODE else "🧪 Test mode: OFF"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats"),
                InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
            ],
            [InlineKeyboardButton(text=mode, callback_data="admin:test_toggle")],
        ]
    )


def build_admin_summary() -> str:
    paid_users = [uid for uid, c in user_credits.items() if c > 0]
    total_paid_credits = sum(user_credits.values())
    free_users_count = limiter.users_count()
    free_used_total = limiter.total_count()

    lines = []
    lines.append("🛠 <b>Admin Panel</b>")
    lines.append("")
    lines.append(f"🧪 Test mode: <b>{'ON' if TEST_MODE else 'OFF'}</b>")
    lines.append("")
    lines.append(f"💳 Users with paid credits: <b>{len(paid_users)}</b>")
    lines.append(f"💰 Total paid credits: <b>{total_paid_credits}</b>")
    lines.append(f"🆓 Free users count: <b>{free_users_count}</b>")
    lines.append(f"🆓 Free animations used: <b>{free_used_total}</b>")
    lines.append("")
    lines.append(f"🎞 Generations: success=<b>{gen_success}</b>, fail=<b>{gen_fail}</b>")
    lines.append("")
    lines.append("📦 Packs purchased:")
    for code, cnt in pack_stats.items():
        title = PACKS.get(code, ("?", 0, 0))[0]
        lines.append(f"• {code} ({title}) — <b>{cnt}</b> times")
    return "\n".join(lines)

# ---------- РЕФЕРАЛЬНАЯ МАГИЯ ----------

def referral_info_text(lang: str) -> str:
    ua = (
        "✨ <b>Реферальна магія Magl’sBot</b>\n\n"
        "1) Запроси 3 друзів — отримай 1 <b>безкоштовне оживлення</b>.\n"
        "2) Отримуй <b>5% Stars</b> від усіх поповнень друзів.\n\n"
        "Поділись ботом через кнопку «Розповісти друзям» в меню — і нехай магія розлітається світом 🪄"
    )
    en = (
        "✨ <b>Magl’sBot referral magic</b>\n\n"
        "1) Invite 3 friends — get 1 <b>free animation</b>.\n"
        "2) Earn <b>5% Stars</b> from all your friends’ top-ups.\n\n"
        "Share the bot via “Tell friends” button in the menu and let the magic spread 🪄"
    )
    es = (
        "✨ <b>Magia de referidos de Magl’sBot</b>\n\n"
        "1) Invita a 3 amigos — recibe 1 <b>animación gratis</b>.\n"
        "2) Gana <b>5% en Stars</b> de todas las recargas de tus amigos.\n\n"
        "Comparte el bot con el botón “Compartir” en el menú y deja que la magia se expanda 🪄"
    )
    pt = (
        "✨ <b>Magia de indicação do Magl’sBot</b>\n\n"
        "1) Convide 3 amigos — ganhe 1 <b>animação grátis</b>.\n"
        "2) Ganhe <b>5% em Stars</b> de todas as recargas dos seus amigos.\n\n"
        "Compartilhe o bot pelo botão “Compartilhar” no menu e deixe a magia se espalhar 🪄"
    )
    mapping = {
        "ua": ua,
        "en": en,
        "es": es,
        "pt": pt,
    }
    return mapping.get(lang, en)


async def register_referral(new_user_id: int, inviter_id: int):
    """
    Регистрируем реферала:
    - не даём приглашать самого себя
    - не пересчитываем, если уже был привязан
    - за каждые 3 приглашённых → +1 бесплатное оживление (credit)
    """
    if new_user_id == inviter_id:
        return
    if new_user_id in ref_inviter:
        return

    ref_inviter[new_user_id] = inviter_id
    ref_count[inviter_id] = ref_count.get(inviter_id, 0) + 1
    count = ref_count[inviter_id]

    earned_free = 1 if (count % 3 == 0) else 0
    if earned_free:
        user_credits[inviter_id] = user_credits.get(inviter_id, 0) + earned_free

    try:
        # уведомление пригласившему
        lang = get_lang(inviter_id)
        msg_lines = [
            "🧙‍♂️ Новий маг приєднався за твоїм посиланням!",
            f"Ти вже запросив: <b>{count}</b> друзів.",
        ]
        if earned_free:
            msg_lines.append(
                f"За кожні 3 запрошених — +1 безкоштовне оживлення.\n"
                f"🎁 Ти щойно отримав +1! Зараз у тебе {user_credits[inviter_id]} кредитів."
            )
        else:
            left = 3 - (count % 3)
            msg_lines.append(
                f"Ще <b>{left}</b> друзів — і ти отримаєш +1 безкоштовне оживлення ✨"
            )
        await bot.send_message(inviter_id, "\n".join(msg_lines))
    except Exception as e:
        logger.warning("Failed to notify inviter: %s", e)

# ---------- Handlers ----------

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer(
            LOCALES[DEFAULT_LANG].get("invite_only", "Invite only. Contact admin.")
        )
        return

    uid = message.from_user.id if message.from_user else 0

    # --- разбор реферального payload ---
    # /start ref_12345
    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""
    if payload.startswith("ref_"):
        try:
            inviter_id = int(payload[4:])
            await register_referral(uid, inviter_id)
        except ValueError:
            pass
    # --- конец блока рефералки ---

    # 🎬 Заставка с оживлённым Гарри Поттером
    if INTRO_VIDEO_FILE_ID:
        try:
            await message.answer_video(
                video=INTRO_VIDEO_FILE_ID,
                supports_streaming=True
            )
        except Exception as e:
            logger.warning("Failed to send intro video: %s", e)

    if uid not in user_lang:
        text = tr_lang("ua", "choose_language") or (
            "🧙‍♂️ <b>Magl’sBot вітає тебе, мандрівнику-магу!</b>\n\n✨ Обери мову чарівної книги:"
        )
        await message.answer(text, reply_markup=lang_choice_keyboard())
        return

    awaiting_support.pop(uid, None)
    await message.answer(tr(uid, "welcome"), reply_markup=main_menu_keyboard(uid))


@dp.callback_query(F.data.startswith("lang:"))
async def on_lang_set(query: CallbackQuery):
    uid = query.from_user.id
    _, code = query.data.split(":", 1)
    if code not in LOCALES:
        await query.answer("Language not available", show_alert=True)
        return
    user_lang[uid] = code
    awaiting_support.pop(uid, None)
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

# ---------- /admin и admin callbacks ----------

@dp.message(Command("admin"))
async def on_admin(message: Message):
    uid = message.from_user.id if message.from_user else 0
    if uid != ADMIN_USER_ID:
        await message.answer("⛔️ You are not an admin.")
        return
    text = build_admin_summary()
    await message.answer(text, reply_markup=admin_keyboard())


@dp.callback_query(F.data.startswith("admin:"))
async def on_admin_action(query: CallbackQuery):
    uid = query.from_user.id
    if uid != ADMIN_USER_ID:
        await query.answer("Not admin", show_alert=True)
        return

    action = query.data.split(":", 1)[1]
    global TEST_MODE

    if action == "stats":
        text = build_admin_summary()
        await query.message.edit_text(text, reply_markup=admin_keyboard())
        await query.answer("Stats updated")
        return

    if action == "users":
        all_ids = set(user_credits.keys())
        try:
            free_usage = getattr(limiter, "_usage", {})
            all_ids.update(free_usage.keys())
        except Exception:
            free_usage = {}
        if not all_ids:
            await query.message.edit_text("👥 No users yet.", reply_markup=admin_keyboard())
            await query.answer()
            return

        lines = ["👥 <b>Users snapshot</b> (top 50):"]
        for i, u in enumerate(sorted(all_ids)):
            if i >= 50:
                lines.append("… (truncated)")
                break
            lang = get_lang(u)
            paid = user_credits.get(u, 0)
            fu = free_usage.get(u, 0) if isinstance(free_usage, dict) else "?"
            lines.append(f"• id={u}, lang={lang}, paid={paid}, free_used={fu}")
        text = "\n".join(lines)
        await query.message.edit_text(text, reply_markup=admin_keyboard())
        await query.answer("Users list")
        return

    if action == "test_toggle":
        TEST_MODE = not TEST_MODE
        status = "ON" if TEST_MODE else "OFF"
        text = build_admin_summary()
        await query.message.edit_text(text, reply_markup=admin_keyboard())
        await query.answer(f"Test mode {status}", show_alert=True)
        return

# ---------- Покупка пакетов ----------

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

    global pack_stats
    if payload in pack_stats:
        pack_stats[payload] += 1

    # 5% Stars реферал-бонус пригласившему
    inviter_id = ref_inviter.get(uid)
    if inviter_id:
        total_stars = sp.total_amount  # сколько Stars списалось
        bonus_stars = int(total_stars * 0.05)
        if bonus_stars > 0:
            ref_stars_balance[inviter_id] = ref_stars_balance.get(inviter_id, 0) + bonus_stars
            gained_credits = 0
            # конвертируем каждые 60 Stars в 1 кредит
            while ref_stars_balance[inviter_id] >= 60:
                ref_stars_balance[inviter_id] -= 60
                user_credits[inviter_id] = user_credits.get(inviter_id, 0) + 1
                gained_credits += 1
            try:
                text_lines = [
                    "💫 Твій друг поповнив баланс у Magl’sBot!",
                    f"Ти отримав <b>{bonus_stars}</b> Stars (5% від його поповнення).",
                ]
                if gained_credits > 0:
                    text_lines.append(
                        f"Це перетворено на +{gained_credits} додаткових оживлень.\n"
                        f"Зараз у тебе: {user_credits[inviter_id]} кредитів."
                    )
                else:
                    text_lines.append(
                        "Ці Stars збережені на реферальному балансі. "
                        "Ще трохи — і вони перетворяться на нове безкоштовне оживлення ✨"
                    )
                await bot.send_message(inviter_id, "\n".join(text_lines))
            except Exception as e:
                logger.warning("Failed to notify inviter about stars bonus: %s", e)

    await message.answer(
        tr(uid, "paid_ok").format(
            credits=credits,
            balance=user_credits[uid],
        )
    )

# ---------- Главное меню: текстовые кнопки + поддержка + share ----------

@dp.message(F.text)
async def on_text(message: Message):
    text = message.text or ""
    uid = message.from_user.id if message.from_user else 0
    lang = get_lang(uid)
    labels = get_menu_labels(lang)

    if text == labels["animate"]:
        awaiting_support.pop(uid, None)
        prompt_texts = {
            "ua": "🪄 Надішли мені фото, і я оживлю його. Найкраще працюють фронтальні портрети з хорошим світлом.",
            "en": "🪄 Send me a photo and I’ll animate it. Front-facing portraits with good light work best.",
            "es": "🪄 Envíame una foto y la animaré. Los retratos frontales con buena luz funcionan mejor.",
            "pt": "🪄 Envie uma foto e eu vou animá-la. Retratos de frente com boa iluminação funcionam melhor.",
        }
        await message.answer(prompt_texts.get(lang, prompt_texts["en"]))
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
        awaiting_support[uid] = True
        msg = {
            "ua": "🆘 Напишіть, будь ласка, своє запитання або проблему одним повідомленням — я передам це живому магу підтримки.",
            "en": "🆘 Please write your question or issue in one message — I’ll send it to the human support wizard.",
            "es": "🆘 Escribe tu pregunta o problema en un solo mensaje — lo enviaré al mago de soporte humano.",
            "pt": "🆘 Escreva sua dúvida ou problema em uma única mensagem — eu vou enviar para o mago humano de suporte.",
        }.get(lang, "🆘 Please write your question in one message — I’ll send it to human support.")
        await message.answer(msg)
        return

    if text == labels["share"]:
        awaiting_support.pop(uid, None)
        ref_link = f"https://t.me/LIvePotterPhotoBot?start=ref_{uid}"
        share_texts = {
            "ua": (
                "📤 Поділись ботом з друзями:\n"
                "Оживляємо фото в стилі Гаррі Поттера 🎬🪄\n"
                f"{ref_link}"
            ),
            "en": (
                "📤 Share this bot with friends:\n"
                "We animate photos like Harry Potter portraits 🎬🪄\n"
                f"{ref_link}"
            ),
            "es": (
                "📤 Comparte este bot con tus amigos:\n"
                "Animamos fotos como los retratos de Harry Potter 🎬🪄\n"
                f"{ref_link}"
            ),
            "pt": (
                "📤 Compartilhe este bot com seus amigos:\n"
                "Animamos fotos como nos retratos de Harry Potter 🎬🪄\n"
                f"{ref_link}"
            ),
        }
        await message.answer(share_texts.get(lang, share_texts["en"]))
        return

    if awaiting_support.get(uid):
        dest = SUPPORT_CHAT_ID or ADMIN_USER_ID
        if dest:
            username = (message.from_user.username if message.from_user else None) or "unknown"
            header = f"📩 Support message from @{username} (id={uid}):"
            try:
                await bot.send_message(
                    chat_id=dest,
                    text=f"{header}\n\n{text}"
                )
                confirm = tr(uid, "support_sent")
                await message.answer(confirm)
            except Exception as e:
                logger.exception("Failed to send support message: %s", e)
                await message.answer("⚠️ Support is temporarily unavailable. Please try again later.")
        else:
            await message.answer("⚠️ Support is not configured yet. Contact bot admin.")
        awaiting_support.pop(uid, None)
        return
    # Остальной текст игнорим — фото и др. обрабатываются отдельными хендлерами

# ---------- Фото + пресеты (с авто-рекомендацией Blink & Glow) ----------

@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id if message.from_user else 0
    awaiting_support.pop(uid, None)

    is_admin = (uid == ADMIN_USER_ID)

    # Лимиты
    if not (TEST_MODE and is_admin):
        if user_credits.get(uid, 0) <= 0 and not limiter.can_use(uid):
            await message.answer(tr(uid, "free_used"))
            return

    photo = message.photo[-1]

    # Heuristic: старое/маленькое фото (часто скан или архив)
    width = photo.width
    height = photo.height
    file_size = getattr(photo, "file_size", 0) or 0

    area = width * height
    is_small_res = area < 400_000 or max(width, height) < 700  # например <= ~800x500
    is_small_size = file_size and file_size < 200_000          # < 200kb

    is_old_like = is_small_res or is_small_size

    pending_photo[uid] = {
        "file_id": photo.file_id,
        "caption": (message.caption or "").strip(),
        "is_old_like": is_old_like,
    }
    pending_choice.pop(uid, None)

    lang = get_lang(uid)

    # Если фото «похоже на старое» — сразу предлагаем Blink & Glow (preset index 4)
    if is_old_like:
        idx = 4  # 0-based => 5-й пресет Blink & Glow
        pending_choice[uid] = {"type": "preset", "idx": idx}

        titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])
        title_txt = titles[idx] if 0 <= idx < len(titles) else "Blink & Glow"

        desc_map = LOCALES.get(lang, {}).get("preset_desc", {})
        desc = ""
        if isinstance(desc_map, dict):
            desc = desc_map.get(str(idx + 1), "")

        confirm_texts = {
            "ua": "✨ Це фото виглядає як старе/архівне.\nРекомендуємо пресет нижче — запустити з ним анімацію?",
            "en": "✨ This photo looks like an old/archival one.\nWe recommend the preset below — start animation with it?",
            "es": "✨ Esta foto parece antigua/de archivo.\nTe recomendamos este preset — ¿iniciar la animación con él?",
            "pt": "✨ Esta foto parece antiga/de arquivo.\nRecomendamos este preset — iniciar animação com ele?",
        }
        confirm_line = confirm_texts.get(lang, confirm_texts["en"])

        if desc:
            header_text = f"🎨 {title_txt}\n\n{desc}\n\n{confirm_line}"
        else:
            header_text = f"🎨 {title_txt}\n\n{confirm_line}"

        await message.answer(
            header_text,
            reply_markup=confirm_preset_keyboard(uid)
        )
        return

    # Обычный сценарий — показать меню пресетов
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
        await query.message.edit_text(tr(uid, "done"))
        await query.answer()
        return

    # Отмена
    if data == "cancel":
        pending_photo.pop(uid, None)
        pending_choice.pop(uid, None)
        await query.message.edit_text(tr(uid, "btn_cancel"))
        await query.answer()
        return

    lang = get_lang(uid)

    # Текст вопроса-подтверждения
    confirm_texts = {
        "ua": "✅ Запустити анімацію з цим пресетом?",
        "en": "✅ Start animation with this preset?",
        "es": "✅ ¿Iniciar la animación con este preset?",
        "pt": "✅ Iniciar a animação com este preset?",
    }
    confirm_line = confirm_texts.get(lang, confirm_texts["en"])

    # Выбор по caption
    if data == "usecap":
        pending_choice[uid] = {"type": "caption", "idx": None}
        desc = info["caption"] or ""
        if desc:
            header_text = f"📝 {desc}\n\n{confirm_line}"
        else:
            header_text = confirm_line
        await query.message.edit_text(header_text, reply_markup=confirm_preset_keyboard(uid))
        await query.answer()
        return

    # Random magic
    if data == "random":
        idx = random.randint(0, len(PRESET_PROMPTS_BASE) - 1)
    else:
        idx = int(data) - 1
        if idx < 0 or idx >= len(PRESET_PROMPTS_BASE):
            await query.answer("Unknown preset")
            return

    pending_choice[uid] = {"type": "preset", "idx": idx}

    titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])
    title_txt = titles[idx] if 0 <= idx < len(titles) else "Preset"

    # Описание из локали
    desc_map = LOCALES.get(lang, {}).get("preset_desc", {})
    desc = ""
    if isinstance(desc_map, dict):
        desc = desc_map.get(str(idx + 1), "")

    if desc:
        header_text = f"🎨 {title_txt}\n\n{desc}\n\n{confirm_line}"
    else:
        header_text = f"🎨 {title_txt}\n\n{desc}\n\n{confirm_line}"

    await query.message.edit_text(header_text, reply_markup=confirm_preset_keyboard(uid))
    await query.answer()

# ---------- Подтверждение пресета (✅ / 🔙) ----------

@dp.callback_query(F.data == "confirm:back")
async def on_confirm_back(query: CallbackQuery):
    uid = query.from_user.id
    info = pending_photo.get(uid)
    if not info:
        await query.message.edit_text(tr(uid, "done"))
        await query.answer()
        return

    pending_choice.pop(uid, None)
    has_caption = bool(info.get("caption"))
    await query.message.edit_text(
        tr(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption=has_caption),
    )
    await query.answer()


@dp.callback_query(F.data == "confirm:ok")
async def on_confirm_ok(query: CallbackQuery):
    uid = query.from_user.id
    info = pending_photo.get(uid)
    choice = pending_choice.get(uid)
    if not info or not choice:
        await query.message.edit_text(tr(uid, "done"))
        await query.answer()
        return

    is_admin = (uid == ADMIN_USER_ID)
    had_paid = user_credits.get(uid, 0) > 0

    # Собираем prompt
    lang = get_lang(uid)
    if choice["type"] == "caption":
        prompt = info["caption"] or "natural smile, subtle head motion, cinematic lighting"
    else:
        idx = int(choice["idx"] or 0)
        prompt = get_preset_prompt(lang, idx)

    await query.message.edit_text(tr(uid, "status_work"))
    await query.answer()

    global gen_success, gen_fail

    try:
        file_info = await bot.get_file(info["file_id"])
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        result = await animate_photo_via_replicate(
            source_image_url=file_url,
            prompt=prompt,
        )
        if not result.get("ok"):
            gen_fail += 1
            await query.message.edit_text(tr(uid, "done"))
            return

        gen_success += 1

        video_url = result["url"]
        tmp_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{info['file_id']}.mp4")
        await download_file(video_url, tmp_path)

        await bot.send_video(
            chat_id=query.message.chat.id,
            video=FSInputFile(tmp_path),
            caption=tr(uid, "done"),
            reply_markup=buy_cta_keyboard(uid),
        )

        # 🔥 После магии — рассказываем про реферальную программу
        ref_text = referral_info_text(lang)
        await bot.send_message(
            chat_id=query.message.chat.id,
            text=ref_text
        )

        if not (TEST_MODE and is_admin):
            if had_paid and user_credits.get(uid, 0) > 0:
                user_credits[uid] -= 1
            else:
                limiter.mark_used(uid)

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        pending_photo.pop(uid, None)
        pending_choice.pop(uid, None)

    except Exception as e:
        gen_fail += 1
        logger.exception("Animation error: %s", e)
        await query.message.edit_text("Error while processing. Try another photo.")

# ---------- MAIN ----------

def main():
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()

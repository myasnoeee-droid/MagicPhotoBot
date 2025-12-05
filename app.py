import os
import asyncio
import logging
import json
import random
import time
from pathlib import Path
from typing import Dict, Any
from db import (
    init_db,
    close_db,
    ensure_user,
    has_used_free,
    mark_free_used,
    register_referral as db_register_referral,
)
from helpers_credits import (
    get_user_credits,
    add_user_credits,
    consume_user_credit,
)
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

from processing import animate_photo_via_replicate, download_file, omni_talking_head


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger("magicphotobot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "LIvePotterPhotoBot")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", "0"))  # чат/канал для поддержки (опц.)
ALLOWED_CHAT_IDS = [int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if x]
MAX_FREE_ANIMS_PER_USER = int(os.getenv("MAX_FREE_ANIMS_PER_USER", "1"))
DOWNLOAD_TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp")
ANIMATION_PRICE = 60       # Stars per normal animation
OMNI_PRICE = 400           # Stars per Omni video

# Заставка — оживлённое видео Гарри Поттера
INTRO_VIDEO_FILE_ID = os.getenv(
    "INTRO_VIDEO_FILE_ID",
    "BAACAgIAAxkBAAICuWkgf1x1yIEgxE8FQoImZ5vuoxbOAALGiwACIA4JSfhC7_NPZQrDNgQ"
)

# Чат для заявок на видео "под ключ"
ORDER_CHAT_ID = int(os.getenv("ORDER_CHAT_ID", "-5085880330"))

# Интервал пушей рефералок (по умолчанию 24 часа)
PUSH_INTERVAL_SECONDS = int(os.getenv("REF_PUSH_INTERVAL", "86400"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ---------- i18n через JSON-файлы ----------
LOCALE_CODES = ("ua", "en", "es", "pt")
DEFAULT_LANG = "en"
LOCALES: Dict[str, Dict[str, Any]] = {}
user_lang: Dict[int, str] = {}  # user_id -> "ua"/"en"/"es"/"pt"

# ---------- Режимы работы бота ----------

MODE_PHOTO = "photo"   # обычное оживление фото
MODE_DUB = "dub"       # говорящая голова (OmniHuman: фото + аудио)

# режим пользователя: uid -> "photo" / "dub"
user_mode: Dict[int, str] = {}

# для OmniHuman: сюда кладём URL фото, по которому потом будем делать говорящую голову
omni_pending_photo: Dict[int, str] = {}  # uid -> image_url


def get_mode(uid: int) -> str:
    """
    Текущий режим пользователя. По умолчанию — MODE_PHOTO.
    """
    return user_mode.get(uid, MODE_PHOTO)


def mode_choice_keyboard(lang: str) -> InlineKeyboardMarkup:
    # Локализация кнопок выбора режима
    labels = {
        "ua": {
            "photo": "✨ Оживлення фото",
            "dub":   "🧠 Говоряча голова (OmniHuman)",
        },
        "en": {
            "photo": "✨ Photo animation",
            "dub":   "🧠 Talking head (OmniHuman)",
        },
        "es": {
            "photo": "✨ Animar foto",
            "dub":   "🧠 Cabeza parlante (OmniHuman)",
        },
        "pt": {
            "photo": "✨ Animação de foto",
            "dub":   "🧠 Cabeça falante (OmniHuman)",
        },
    }
    l = labels.get(lang, labels["en"])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=l["photo"], callback_data="mode:photo")],
            [InlineKeyboardButton(text=l["dub"],   callback_data="mode:dub")],
        ]
    )


def mode_choice_text(lang: str) -> str:
    if lang == "ua":
        return (
            "Обери режим магії 🪄\n\n"
            "1️⃣ <b>Оживлення фото</b> — анімація портретів, як у магічних фільмах.\n"
            "2️⃣ <b>Говоряча голова (OmniHuman)</b> — надішли фото, потім аудіо, і фото заговорить твоїм голосом."
        )
    if lang == "es":
        return (
            "Elige el modo de magia 🪄\n\n"
            "1️⃣ <b>Animar foto</b> — retratos animados como en películas mágicas.\n"
            "2️⃣ <b>Cabeza parlante (OmniHuman)</b> — envía una foto y luego un audio, y la foto hablará con tu voz."
        )
    if lang == "pt":
        return (
            "Escolha o modo de magia 🪄\n\n"
            "1️⃣ <b>Animação de foto</b> — retratos animados como em filmes mágicos.\n"
            "2️⃣ <b>Cabeça falante (OmniHuman)</b> — envie uma foto e depois um áudio, e a foto falará com a sua voz."
        )
    # en по умолчанию
    return (
        "Choose your magic mode 🪄\n\n"
        "1️⃣ <b>Photo animation</b> — animate portraits like in magic movies.\n"
        "2️⃣ <b>Talking head (OmniHuman)</b> — send a photo, then an audio, and the photo will speak with your voice."
    )


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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇦 Українська", callback_data="lang:ua"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ],
            [
                InlineKeyboardButton(text="🇪🇸 Español", callback_data="lang:es"),
                InlineKeyboardButton(text="🇧🇷 Português (BR)", callback_data="lang:pt"),
            ],
        ]
    )




# ---------- Пресеты (региональные) ----------

PRESET_PROMPTS_BASE = [
    "natural smile, slight head turn right, photorealistic",                     # 0 Natural smile
    "cinematic portrait, subtle breathing, soft studio light, 24fps",           # 1 Cinematic look
    "gentle movement, hair flutter, soft focus, ethereal glow",                 # 2 Dreamy motion
    "smile softly, natural head tilt, expressive eyes, warm tone lighting",     # 3 Expressive vibe
    "gentle eye blink, slow smile, cinematic lighting, photorealistic",         # 4 Blink & glow
    "subtle wink, slight smile, natural head motion, photorealistic lighting",  # 5 Wink
    "vintage 35mm film look, soft focus, warm tones, subtle motion",            # 6 Vintage film
    "dramatic lighting, strong shadows, cinematic mood, expressive face",       # 7 Dramatic lighting
    "editorial portrait, soft bounce light, slight head movement, elegant expression"  # 8 Editorial portrait
]

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

# ---------- Пользователи и пуши ----------

known_users: set[int] = set()
last_ref_push: Dict[int, float] = {}  # user_id -> last push ts
pending_photo: Dict[int, Dict[str, Any]] = {}
pending_choice: Dict[int, Dict[str, Any]] = {}


def register_user(uid: int):
    if uid and uid > 0:
        known_users.add(uid)


def preset_keyboard(uid: int, has_caption: bool) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])

    random_labels = {
        "ua": "✨ Random magic",
        "en": "✨ Random magic",
        "es": "✨ Magia aleatoria",
        "pt": "✨ Magia aleatória",
    }
    random_text = random_labels.get(lang, "✨ Random magic")

    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [InlineKeyboardButton(text=random_text, callback_data="preset:random")]
    )

    for i in range(len(titles)):
        rows.append(
            [InlineKeyboardButton(text=titles[i], callback_data=f"preset:{i+1}")]
        )

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

PACKS = {
    "pack_1": ("1 animation", 1 * ANIMATION_PRICE, 60),
    "pack_3": ("3 animations", 3 * ANIMATION_PRICE, 150),
    "pack_5": ("5 animations", 5 * ANIMATION_PRICE, 300),
    "pack_10": ("10 animations", 10 * ANIMATION_PRICE, 500),
    "pack_25": ("25 animations", 25 * ANIMATION_PRICE, 1000),
}

# ----- Рефералка -----
ref_inviter: Dict[int, int] = {}        # invited_id -> inviter_id
ref_count: Dict[int, int] = {}          # inviter_id -> count
ref_stars_balance: Dict[int, int] = {}  # остаток Stars, не конвертированных
ref_stars_total: Dict[int, int] = {}    # суммарно начисленных Stars за всё время (5%)
payer_users: set[int] = set()           # кто хоть раз платил (покупал Stars)

# ---------- Главное меню (ReplyKeyboard) ----------

MENU_BUTTONS = {
    "ua": {
        "animate": "🪄 Оживити фото",
        "omni": "🧠 Говоряча голова (Omni)",
        "buy": "💫 Купити генерації",
        "support": "🆘 Підтримка",
        "share": "📤 Розповісти друзям",
        "balance": "💰 Баланс",
        "order_video": "🎬 Замовити відео під ключ",
        "partner": "🤝 Партнерський кабінет",
    },
    "en": {
        "animate": "🪄 Animate photo",
        "omni": "🧠 Talking head (Omni)",
        "buy": "💫 Buy generations",
        "support": "🆘 Support",
        "share": "📤 Tell friends",
        "balance": "💰 Balance",
        "order_video": "🎬 Order custom video",
        "partner": "🤝 Partner dashboard",
    },
    "es": {
        "animate": "🪄 Animar foto",
        "omni": "🧠 Cabeza parlante (Omni)",
        "buy": "💫 Comprar generaciones",
        "support": "🆘 Soporte",
        "share": "📤 Compartir",
        "balance": "💰 Balance",
        "order_video": "🎬 Encargar video a medida",
        "partner": "🤝 Panel de socio",
    },
    "pt": {
        "animate": "🪄 Animar foto",
        "omni": "🧠 Cabeça falante (Omni)",
        "buy": "💫 Comprar gerações",
        "support": "🆘 Suporte",
        "share": "📤 Compartilhar",
        "balance": "💰 Saldo",
        "order_video": "🎬 Encomendar vídeo sob medida",
        "partner": "🤝 Painel de parceiro",
    },
}


def get_menu_labels(lang: str) -> Dict[str, str]:
    return MENU_BUTTONS.get(lang, MENU_BUTTONS["en"])


def detect_lang_by_button(text: str):
    for code, labels in MENU_BUTTONS.items():
        for v in labels.values():
            if text == v:
                return code
    return None


def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    lang = get_lang(uid)
    labels = get_menu_labels(lang)

    kb = ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [
                KeyboardButton(text=labels["animate"]),
                KeyboardButton(text=labels["omni"]),   # новая кнопка
            ],
            [
                KeyboardButton(text=labels["buy"]),
                KeyboardButton(text=labels["balance"]),
            ],
            [
                KeyboardButton(text=labels["support"]),
                KeyboardButton(text=labels["share"]),
            ],
            [
                KeyboardButton(text=labels["order_video"]),
            ],
            [
                KeyboardButton(text=labels["partner"]),
            ],
        ],
    )
    return kb

# ---------- Поддержка и заявки на видео ----------

awaiting_support: Dict[int, bool] = {}
awaiting_video_order: Dict[int, bool] = {}

# ---------- АДМИНСКИЕ СЧЁТЧИКИ И TEST MODE ----------

TEST_MODE = False
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
    lines = []
    lines.append("🛠 <b>Admin Panel</b>")
    lines.append("")
    lines.append(f"🧪 Test mode: <b>{'ON' if TEST_MODE else 'OFF'}</b>")
    lines.append("")
    lines.append(f"👥 Known users (session): <b>{len(known_users)}</b>")
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
        "Запроси 3 друзів — отримай 1 безкоштовне оживлення.\n"
        "Отримуй 5% Stars від усіх поповнень друзів.\n\n"
        "Поділись ботом через кнопку «Розповісти друзям» в меню — і нехай магія розлітається світом 🪄"
    )
    en = (
        "✨ <b>Magl’sBot referral magic</b>\n\n"
        "Invite 3 friends — get 1 free animation.\n"
        "Earn 5% Stars from all your friends’ top-ups.\n\n"
        "Share the bot via the “Tell friends” button and let the magic spread 🪄"
    )
    es = (
        "✨ <b>Magia de referidos de Magl’sBot</b>\n\n"
        "Invita a 3 amigos — recibe 1 animación gratis.\n"
        "Gana 5% en Stars de todas las recargas de tus amigos.\n\n"
        "Comparte el bot con el botón “Compartir” y deja que la magia se expanda 🪄"
    )
    pt = (
        "✨ <b>Magia de indicação do Magl’sBot</b>\n\n"
        "Convide 3 amigos — ganhe 1 animação grátis.\n"
        "Ganhe 5% em Stars de todas as recargas dos seus amigos.\n\n"
        "Compartilhe o bot pelo botão “Compartilhar” e deixe a magia se espalhar 🪄"
    )
    mapping = {
        "ua": ua,
        "en": en,
        "es": es,
        "pt": pt,
    }
    return mapping.get(lang, en)


def get_ref_main_text(lang: str) -> str:
    """
    Экран при входе в реферальный раздел (/ref)
    """
    if lang not in ("ua", "en", "es", "pt"):
        lang = "en"

    if lang == "ua":
        return (
            "✨ <b>Реферальна магія Magl’sBot</b>\n\n"
            "Запроси 3 друзів — отримай 1 безкоштовне оживлення.\n"
            "Отримуй 5% Stars від усіх поповнень друзів.\n\n"
            "Поділись ботом через кнопку нижче — і нехай магія розлітається світом 🪄"
        )
    if lang == "en":
        return (
            "✨ <b>Magl’sBot referral magic</b>\n\n"
            "Invite 3 friends — get 1 free animation.\n"
            "Earn 5% Stars from all your friends’ top-ups.\n\n"
            "Use the buttons below to share your link and track your stats 🪄"
        )
    if lang == "es":
        return (
            "✨ <b>Magia de referidos de Magl’sBot</b>\n\n"
            "Invita a 3 amigos — recibe 1 animación gratis.\n"
            "Gana 5% en Stars de todas las recargas de tus amigos.\n\n"
            "Usa los botones de abajo para compartir tu enlace y ver tus estadísticas 🪄"
        )
    if lang == "pt":
        return (
            "✨ <b>Magia de indicação do Magl’sBot</b>\n\n"
            "Convide 3 amigos — ganhe 1 animação grátis.\n"
            "Ganhe 5% em Stars de todas as recargas dos seus amigos.\n\n"
            "Use os botões abaixo para compartilhar seu link e ver suas estatísticas 🪄"
        )
    return ""


async def build_referral_stats_text(uid: int) -> str:
    lang = get_lang(uid)
    invited = ref_count.get(uid, 0)
    free_from_invites = invited // 3
    pending_stars = ref_stars_balance.get(uid, 0)

    credits = await get_user_credits(uid)  # 👈 из Postgres

    if lang not in ("ua", "en", "es", "pt"):
        lang = "en"

    if lang == "ua":
        lines = [
            "📊 <b>Твоя реферальна статистика</b>",
            "",
            f"👥 Запрошено друзів: <b>{invited}</b>",
            f"🎁 Безкоштовних оживлень за друзів (накопичено всього): <b>{free_from_invites}</b>",
            f"⭐ Накопичено реферальних Stars (ще не конвертовано): <b>{pending_stars}</b>",
            f"💰 Поточний баланс оживлень: <b>{credits}</b>",
        ]
        return "\n".join(lines)

    if lang == "en":
        lines = [
            "📊 <b>Your referral stats</b>",
            "",
            f"👥 Friends invited: <b>{invited}</b>",
            f"🎁 Free animations from invites (total accrued): <b>{free_from_invites}</b>",
            f"⭐ Referral Stars accumulated (not yet converted): <b>{pending_stars}</b>",
            f"💰 Current animation balance: <b>{credits}</b>",
        ]
        return "\n".join(lines)

    if lang == "es":
        lines = [
            "📊 <b>Tus estadísticas de referidos</b>",
            "",
            f"👥 Amigos invitados: <b>{invited}</b>",
            f"🎁 Animaciones gratis por referidos (acumuladas): <b>{free_from_invites}</b>",
            f"⭐ Stars de referidos acumuladas (sin convertir): <b>{pending_stars}</b>",
            f"💰 Balance actual de animaciones: <b>{credits}</b>",
        ]
        return "\n".join(lines)

    if lang == "pt":
        lines = [
            "📊 <b>Suas estatísticas de indicação</b>",
            "",
            f"👥 Amigos indicados: <b>{invited}</b>",
            f"🎁 Animações grátis por indicações (acumuladas): <b>{free_from_invites}</b>",
            f"⭐ Stars de indicação acumuladas (ainda não convertidas): <b>{pending_stars}</b>",
            f"💰 Saldo atual de animações: <b>{credits}</b>",
        ]
        return "\n".join(lines)

    return ""


def referral_main_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    share_labels = {
        "ua": "📤 Розповісти друзям",
        "en": "📤 Tell friends",
        "es": "📤 Compartir con amigos",
        "pt": "📤 Compartilhar com amigos",
    }
    stats_labels = {
        "ua": "📊 Моя статистика",
        "en": "📊 My stats",
        "es": "📊 Mis estadísticas",
        "pt": "📊 Minhas estatísticas",
    }
    share_text = share_labels.get(lang, share_labels["en"])
    stats_text = stats_labels.get(lang, stats_labels["en"])

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=share_text, callback_data="ref:share")],
            [InlineKeyboardButton(text=stats_text, callback_data="ref:stats")],
        ]
    )

# ---------- ПАРТНЁРСКИЙ КАБИНЕТ ----------

def get_partner_level(total_invited: int, lang: str) -> str:
    if total_invited >= 100:
        ua = "🏆 <b>Archmage Partner</b>"
        en = "🏆 <b>Archmage Partner</b>"
        es = "🏆 <b>Socio Archimago</b>"
        pt = "🏆 <b>Parceiro Arquimago</b>"
    elif total_invited >= 30:
        ua = "💎 <b>Master Mage Partner</b>"
        en = "💎 <b>Master Mage Partner</b>"
        es = "💎 <b>Socio Mago Maestro</b>"
        pt = "💎 <b>Parceiro Mago Mestre</b>"
    elif total_invited >= 10:
        ua = "✨ <b>Apprentice Mage Partner</b>"
        en = "✨ <b>Apprentice Mage Partner</b>"
        es = "✨ <b>Socio Mago Aprendiz</b>"
        pt = "✨ <b>Parceiro Mago Aprendiz</b>"
    elif total_invited >= 1:
        ua = "🔮 <b>New Mage Partner</b>"
        en = "🔮 <b>New Mage Partner</b>"
        es = "🔮 <b>Nuevo socio mago</b>"
        pt = "🔮 <b>Novo parceiro mago</b>"
    else:
        ua = "🌱 <b>Почни свою магічну подорож</b>"
        en = "🌱 <b>Start your magic journey</b>"
        es = "🌱 <b>Comienza tu viaje mágico</b>"
        pt = "🌱 <b>Comece sua jornada mágica</b>"

    if lang == "ua":
        return ua
    if lang == "es":
        return es
    if lang == "pt":
        return pt
    return en


def build_partner_dashboard_text(uid: int) -> str:
    lang = get_lang(uid)

    invited_users = [u for u, inv in ref_inviter.items() if inv == uid]
    total = len(invited_users)

    active = len(invited_users)
    payers = sum(1 for u in invited_users if u in payer_users)

    bonus = ref_stars_total.get(uid, 0)
    total_users = len(known_users)

    cr_active = (active / total * 100) if total else 0.0
    cr_payers = (payers / total * 100) if total else 0.0

    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    level_text = get_partner_level(total, lang)

    if lang not in ("ua", "en", "es", "pt"):
        lang = "en"

    if lang == "ua":
        text = (
            "🤝 <b>Партнерський кабінет Magl’sBot</b>\n\n"
            f"{level_text}\n\n"
            "🔗 <b>Твоя магічна реферальна лінка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            "📊 <b>Статистика:</b>\n"
            f"👥 Запрошено користувачів: <b>{total}</b>\n"
            f"✨ Оживили фото: <b>{active}</b> (<i>{cr_active:.1f}%</i>)\n"
            f"⭐ Купили Stars: <b>{payers}</b> (<i>{cr_payers:.1f}%</i>)\n"
            f"💰 Отримано бонусних Stars: <b>{bonus}</b>\n\n"
            f"🌍 Учасників бота всього: <b>{total_users}</b>\n\n"
            "📌 Поширюй цю лінку в сторіс, постах та чатах — "
            "і отримуй магічні винагороди за кожного активного мага 🪄"
        )
        return text

    if lang == "en":
        text = (
            "🤝 <b>Magl’sBot Partner Dashboard</b>\n\n"
            f"{level_text}\n\n"
            "🔗 <b>Your magic referral link:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            "<b>📊 Stats:</b>\n"
            f"👥 Users invited: <b>{total}</b>\n"
            f"✨ Did at least one animation: <b>{active}</b> (<i>{cr_active:.1f}%</i>)\n"
            f"⭐ Bought Stars: <b>{payers}</b> (<i>{cr_payers:.1f}%</i>)\n"
            f"💰 Bonus Stars earned: <b>{bonus}</b>\n\n"
            f"🌍 Total bot users: <b>{total_users}</b>\n\n"
            "📌 Share this link in your stories, posts and chats — "
            "and earn magic rewards for each active mage 🪄"
        )
        return text

    if lang == "es":
        text = (
            "🤝 <b>Panel de socio Magl’sBot</b>\n\n"
            f"{level_text}\n\n"
            "🔗 <b>Tu enlace mágico de referido:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            "📊 <b>Estadísticas:</b>\n"
            f"👥 Usuarios invitados: <b>{total}</b>\n"
            f"✨ Animaron al menos una foto: <b>{active}</b> (<i>{cr_active:.1f}%</i>)\n"
            f"⭐ Compraron Stars: <b>{payers}</b> (<i>{cr_payers:.1f}%</i>)\n"
            f"💰 Stars de bono recibidas: <b>{bonus}</b>\n\n"
            f"🌍 Usuarios totales del bot: <b>{total_users}</b>\n\n"
            "📌 Comparte este enlace en stories, posts y chats — "
            "y gana recompensas mágicas por cada mago activo 🪄"
        )
        return text

    if lang == "pt":
        text = (
            "🤝 <b>Painel de parceiro Magl’sBot</b>\n\n"
            f"{level_text}\n\n"
            "🔗 <b>Seu link mágico de indicação:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            "📊 <b>Estatísticas:</b>\n"
            f"👥 Usuários indicados: <b>{total}</b>\n"
            f"✨ Fizeram ao menos uma animação: <b>{active}</b> (<i>{cr_active:.1f}%</i>)\n"
            f"⭐ Compraram Stars: <b>{payers}</b> (<i>{cr_payers:.1f}%</i>)\n"
            f"💰 Stars de bônus recebidas: <b>{bonus}</b>\n\n"
            f"🌍 Usuários totais do bot: <b>{total_users}</b>\n\n"
            "📌 Compartilhe este link em stories, posts e chats — "
            "e ganhe recompensas mágicas por cada mago ativo 🪄"
        )
        return text

    return ""


def partner_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"

    share_texts = {
        "ua": (
            f"🪄 Спробуй Magl’sBot — бот, який оживляє фото, як у магічних фільмах:\n"
            f"{ref_link}"
        ),
        "en": (
            f"🪄 Try Magl’sBot — a bot that animates your photos like in magic movies:\n"
            f"{ref_link}"
        ),
        "es": (
            f"🪄 Prueba Magl’sBot — un bot que anima tus fotos como en películas mágicas:\n"
            f"{ref_link}"
        ),
        "pt": (
            f"🪄 Experimente o Magl’sBot — um bot que anima suas fotos como em filmes mágicos:\n"
            f"{ref_link}"
        ),
    }
    share_text = share_texts.get(lang, share_texts["en"])

    share_labels = {
        "ua": "📤 Поділитися посиланням",
        "en": "📤 Share link",
        "es": "📤 Compartir enlace",
        "pt": "📤 Compartilhar link",
    }
    reload_labels = {
        "ua": "🔄 Оновити статистику",
        "en": "🔄 Refresh stats",
        "es": "🔄 Actualizar estadísticas",
        "pt": "🔄 Atualizar estatísticas",
    }

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=share_labels.get(lang, share_labels["en"]),
                    switch_inline_query=share_text,
                )
            ],
            [
                InlineKeyboardButton(
                    text=reload_labels.get(lang, reload_labels["en"]),
                    callback_data="partner:reload",
                )
            ],
        ]
    )

# ---------- Пуши рефералок ----------

def get_ref_push_text(lang: str, variant: int) -> str:
    if lang not in ("ua", "en", "es", "pt"):
        lang = "en"

    texts = {
        "ua": {
            1: "✨ У тебе ще є шанс отримати безкоштовне оживлення.\nЗапроси 3 друзів — і магія зробить це за тебе 🪄",
            2: "🔥 Магічний бонус чекає!\nЗапроси ще 1 друга — і відкриється нове безкоштовне оживлення.",
        },
        "en": {
            1: "✨ You still have a chance to get a free animation.\nInvite 3 friends and let the magic do the rest 🪄",
            2: "🔥 A magic bonus is waiting!\nInvite 1 more friend to unlock a new free animation.",
        },
        "es": {
            1: "✨ Aún tienes la oportunidad de conseguir una animación gratis.\nInvita a 3 amigos y deja que la magia haga el resto 🪄",
            2: "🔥 ¡Un bono mágico te espera!\nInvita a 1 amigo más y se activará una nueva animación gratis.",
        },
        "pt": {
            1: "✨ Você ainda tem a chance de ganhar uma animação grátis.\nConvide 3 amigos e deixe a magia fazer o resto 🪄",
            2: "🔥 Um bônus mágico está esperando!\nConvide mais 1 amigo para liberar uma nova animação grátis.",
        },
    }
    return texts.get(lang, {}).get(variant, "")


def get_ref_bonus_text(lang: str, bonus_stars: int, gained_credits: int, credits_balance: int) -> str:
    if lang not in ("ua", "en", "es", "pt"):
        lang = "en"

    if lang == "ua":
        lines = [
            "🎉 Один із твоїх друзів поповнив Stars!",
            "Ти отримав свій магічний бонус — +5% ✨",
            f"Це <b>{bonus_stars}</b> Stars на твоєму реферальному балансі.",
        ]
        if gained_credits > 0:
            lines.append(
                f"Частину вже конвертовано у додаткові оживлення.\n"
                f"Зараз у тебе: <b>{credits_balance}</b> кредитів."
            )
        lines.append("\nЗапроси ще, щоб отримати більше подарунків 🪄")
        return "\n".join(lines)

    if lang == "en":
        lines = [
            "🎉 One of your friends just topped up Stars!",
            "You received your magic bonus — +5% ✨",
            f"That’s <b>{bonus_stars}</b> Stars on your referral balance.",
        ]
        if gained_credits > 0:
            lines.append(
                f"Part of it has already been converted into extra animations.\n"
                f"Your current balance: <b>{credits_balance}</b> credits."
            )
        lines.append("\nInvite more friends to get even more rewards 🪄")
        return "\n".join(lines)

    if lang == "es":
        lines = [
            "🎉 ¡Uno de tus amigos recargó Stars!",
            "Has recibido tu bono mágico — +5% ✨",
            f"Son <b>{bonus_stars}</b> Stars en tu saldo de referidos.",
        ]
        if gained_credits > 0:
            lines.append(
                f"Parte ya se convirtió en animaciones extra.\n"
                f"Tu saldo actual: <b>{credits_balance}</b> animaciones."
            )
        lines.append("\nInvita a más amigos para recibir más regalos 🪄")
        return "\n".join(lines)

    if lang == "pt":
        lines = [
            "🎉 Um dos seus amigos acabou de recarregar Stars!",
            "Você recebeu seu bônus mágico — +5% ✨",
            f"Isto é <b>{bonus_stars}</b> Stars no seu saldo de indicação.",
        ]
        if gained_credits > 0:
            lines.append(
                f"Uma parte já foi convertida em animações extras.\n"
                f"Seu saldo atual: <b>{credits_balance}</b> créditos."
            )
        lines.append("\nConvide mais amigos para ganhar ainda mais recompensas 🪄")
        return "\n".join(lines)

    return ""


async def register_referral(new_user_id: int, inviter_id: int):
    if new_user_id == inviter_id:
        return

    # сначала пишем в Postgres, чтобы не было дублей
    created = await db_register_referral(inviter_id=inviter_id, invited_id=new_user_id)
    if not created:
        # уже был такой реферал
        return

    # дальше поддерживаем in-memory статистику
    ref_inviter[new_user_id] = inviter_id
    ref_count[inviter_id] = ref_count.get(inviter_id, 0) + 1
    count = ref_count[inviter_id]

    earned_free = 1 if (count % 3 == 0) else 0
    if earned_free:
        # начисляем 1 бесплатную анимацию через БД
        new_balance = await add_user_credits(inviter_id, earned_free, "referral_3_friends")
    else:
        new_balance = await get_user_credits(inviter_id)

    try:
        lang = get_lang(inviter_id)
        msg_lines = [
            "🧙‍♂️ Новий маг приєднався за твоїм посиланням!",
            f"Ти вже запросив: <b>{count}</b> друзів.",
        ]
        if earned_free:
            msg_lines.append(
                f"За кожні 3 запрошених — +1 безкоштовне оживлення.\n"
                f"🎁 Ти щойно отримав +1! Зараз у тебе {new_balance} кредитів."
            )
        else:
            left = 3 - (count % 3)
            msg_lines.append(
                f"Ще <b>{left}</b> друзів — і ти отримаєш +1 безкоштовне оживлення ✨"
            )
        await bot.send_message(inviter_id, "\n".join(msg_lines))
    except Exception as e:
        logger.warning("Failed to notify inviter: %s", e)


async def referral_reminder_worker():
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.sleep(PUSH_INTERVAL_SECONDS)
            now = time.time()

            for uid in list(known_users):
                if uid <= 0:
                    continue

                last = last_ref_push.get(uid, 0)
                if now - last < PUSH_INTERVAL_SECONDS * 0.9:
                    continue

                count = ref_count.get(uid, 0)
                if count <= 0:
                    friends_to_next = 3
                else:
                    mod = count % 3
                    friends_to_next = 3 if mod == 0 else (3 - mod)

                if friends_to_next == 1:
                    variant = 2
                else:
                    variant = 1

                lang = get_lang(uid)
                text = get_ref_push_text(lang, variant)
                if not text:
                    continue

                try:
                    await bot.send_message(uid, text)
                    last_ref_push[uid] = now
                    logger.info(f"Sent referral push (variant={variant}) to {uid}")
                except Exception as e:
                    logger.warning(f"Failed to send referral push to {uid}: {e}")
        except Exception as e:
            logger.exception(f"Error in referral_reminder_worker: {e}")
            await asyncio.sleep(60)

# ---------- Handlers ----------

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer(
            LOCALES[DEFAULT_LANG].get("invite_only", "Invite only. Contact admin.")
        )
        return

    uid = message.from_user.id if message.from_user else 0
    register_user(uid)

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""
    if payload.startswith("ref_"):
        try:
            inviter_id = int(payload[4:])
            await register_referral(uid, inviter_id)
            register_user(inviter_id)
        except ValueError:
            pass

    if uid not in user_lang:
        caption = (
            "Magl’sBot вітає тебе, мандрівнику-магу!\n\n"
            "✨ Обери мову чарівної книги:"
        )

        if INTRO_VIDEO_FILE_ID:
            try:
                await message.answer_video(
                    video=INTRO_VIDEO_FILE_ID,
                    caption=caption,
                    supports_streaming=True,
                    reply_markup=lang_choice_keyboard(),
                )
                return
            except Exception as e:
                logger.warning("Failed to send intro video with caption: %s", e)

        await message.answer(caption, reply_markup=lang_choice_keyboard())
        return

    if INTRO_VIDEO_FILE_ID:
        try:
            await message.answer_video(
                video=INTRO_VIDEO_FILE_ID,
                supports_streaming=True
            )
        except Exception as e:
            logger.warning("Failed to send intro video (known lang): %s", e)

    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)
    await message.answer(tr(uid, "welcome"), reply_markup=main_menu_keyboard(uid))


@dp.callback_query(F.data.startswith("lang:"))
async def on_lang_set(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    _, code = query.data.split(":", 1)
    if code not in LOCALES:
        await query.answer("Language not available", show_alert=True)
        return

    user_lang[uid] = code
    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)

    # по умолчанию режим фото
    user_mode[uid] = MODE_PHOTO

    # пытаемся обновить подпись/текст
    try:
        await query.message.edit_caption(tr(uid, "lang_set"))
    except Exception:
        try:
            await query.message.edit_text(tr(uid, "lang_set"))
        except Exception:
            await query.message.answer(tr(uid, "lang_set"))

    lang = get_lang(uid)

    # теперь предлагаем выбрать режим
    await query.message.answer(
        mode_choice_text(lang),
        reply_markup=mode_choice_keyboard(lang),
    )

    await query.answer()

@dp.callback_query(F.data == "mode:photo")
async def on_mode_photo(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    user_mode[uid] = MODE_PHOTO
    lang = get_lang(uid)

    texts = {
        "ua": "Режим: ✨ Оживлення фото.\n\nНадішли мені фото — я оживлю його 🪄",
        "en": "Mode: ✨ Photo animation.\n\nSend me a photo and I’ll animate it 🪄",
        "es": "Modo: ✨ Animar foto.\n\nEnvíame una foto y la animaré 🪄",
        "pt": "Modo: ✨ Animação de foto.\n\nEnvie uma foto e eu vou animá-la 🪄",
    }
    await query.message.answer(
        texts.get(lang, texts["en"]),
        reply_markup=main_menu_keyboard(uid),
    )
    await query.answer()


@dp.callback_query(F.data == "mode:dub")
async def on_mode_dub(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    user_mode[uid] = MODE_DUB
    lang = get_lang(uid)

    texts = {
        "ua": (
            "Режим: 🎧 Говоряча голова (Omni).\n\n"
            "1) Спочатку надішли фото з обличчям,\n"
            "2) Потім — аудіо (voice або аудіофайл), і я зроблю відео, де це фото говорить твоїм голосом."
        ),
        "en": (
            "Mode: 🎧 Talking head (Omni).\n\n"
            "1) First send a photo with a face,\n"
            "2) Then send an audio (voice message or audio file), and I’ll make a video of this photo speaking with your voice."
        ),
        "es": (
            "Modo: 🎧 Cabeza parlante (Omni).\n\n"
            "1) Primero envía una foto con un rostro,\n"
            "2) Luego un audio (nota de voz o archivo), y haré un vídeo donde esta foto habla con tu voz."
        ),
        "pt": (
            "Modo: 🎧 Cabeça falante (Omni).\n\n"
            "1) Primeiro envie uma foto com um rosto,\n"
            "2) Depois um áudio (mensagem de voz ou arquivo), e farei um vídeo em que essa foto fala com a sua voz."
        ),
    }
    await query.message.answer(
        texts.get(lang, texts["en"]),
        reply_markup=main_menu_keyboard(uid),
    )
    await query.answer()
    

@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
    await message.answer(tr(uid, "pricing"))


@dp.message(Command("buy"))
async def on_buy(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
    await message.answer(tr(uid, "buy_title"), reply_markup=buy_menu_keyboard(uid))


@dp.message(Command("balance"))
async def on_balance(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)

    credits = await get_user_credits(uid)  # 👈 из Postgres

    await message.answer(
        tr(uid, "balance_title").format(credits=credits)
    )


@dp.message(Command("menu"))
async def on_menu(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)
    await message.answer("Меню оновлено ⬇️", reply_markup=main_menu_keyboard(uid))


@dp.message(Command("ref"))
async def on_ref_command(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
    lang = get_lang(uid)
    await message.answer(
        get_ref_main_text(lang),
        reply_markup=referral_main_keyboard(uid)
    )


@dp.message(Command("partner"))
async def on_partner_command(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
    text = build_partner_dashboard_text(uid)
    await message.answer(text, reply_markup=partner_keyboard(uid))

# ---------- /admin и admin callbacks ----------

@dp.message(Command("admin"))
async def on_admin(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
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
        all_ids = sorted(u for u in known_users if u > 0)

        if not all_ids:
            await query.message.edit_text("👥 No users yet.", reply_markup=admin_keyboard())
            await query.answer()
            return

        lines = ["👥 <b>Users snapshot</b> (top 50):"]
        for i, u in enumerate(all_ids):
            if i >= 50:
                lines.append("… (truncated)")
                break
            lang_u = get_lang(u)
            lines.append(f"• id={u}, lang={lang_u}")

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

def buy_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)

    # Лейбл для Omni (1 відео = 400 Stars)
    omni_labels = {
        "ua": "🧠 1 відео Omni — 400 ⭐",
        "en": "🧠 1 Omni video — 400 ⭐",
        "es": "🧠 1 vídeo Omni — 400 ⭐",
        "pt": "🧠 1 vídeo Omni — 400 ⭐",
    }
    omni_text = omni_labels.get(lang, omni_labels["en"])

    popular_text = "🔥 " + tr_lang(lang, "buy_btn_3")

    buttons = [
        # OmniHuman покупка
        InlineKeyboardButton(
            text=omni_text,
            callback_data="buy:omni",
        ),
        # Пакеты оживлений фото
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
            text=tr_lang(lang, "buy_btn_25") or "25 animations — 1000 ⭐",
            callback_data="buy:pack_25",
        ),
        InlineKeyboardButton(
            text=tr_lang(lang, "buy_btn_1"),
            callback_data="buy:pack_1",
        ),
    ]

    return InlineKeyboardMarkup(
        inline_keyboard=[[b] for b in buttons]
    )


def buy_cta_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)

    omni_labels = {
        "ua": "🧠 1 відео Omni — 400 ⭐",
        "en": "🧠 1 Omni video — 400 ⭐",
        "es": "🧠 1 vídeo Omni — 400 ⭐",
        "pt": "🧠 1 vídeo Omni — 400 ⭐",
    }
    omni_text = omni_labels.get(lang, omni_labels["en"])

    popular_text = "🔥 " + tr_lang(lang, "buy_btn_3")

    buy_buttons = [
        # OmniHuman сверху
        InlineKeyboardButton(
            text=omni_text,
            callback_data="buy:omni",
        ),
        # Пакеты оживлений
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
            text="💫 " + (tr_lang(lang, "buy_btn_25") or "25 animations — 1000 ⭐"),
            callback_data="buy:pack_25",
        ),
        InlineKeyboardButton(
            text="💫 " + tr_lang(lang, "buy_btn_1"),
            callback_data="buy:pack_1",
        ),
    ]

    share_labels = {
        "ua": "📤 Поділитися",
        "en": "📤 Share",
        "es": "📤 Compartir",
        "pt": "📤 Compartilhar",
    }
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
    share_button = InlineKeyboardButton(
        text=share_labels.get(lang, share_labels["en"]),
        url=ref_link,
    )

    rows = [[b] for b in buy_buttons]
    rows.append([share_button])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("buy:"))
async def on_buy_click(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    code = query.data.split(":", 1)[1]

    # 🔹 Отдельный кейс для Omni-видео
    if code == "omni":
        lang = get_lang(uid)
        title_map = {
            "ua": "1 відео Omni",
            "en": "1 Omni video",
            "es": "1 vídeo Omni",
            "pt": "1 vídeo Omni",
        }
        title = title_map.get(lang, "1 Omni video")

        prices = [LabeledPrice(label=title, amount=OMNI_PRICE)]

        await bot.send_invoice(
            chat_id=query.message.chat.id,
            title=title,
            description=f"{title} for Magl’sBot",
            payload="omni",  # 👈 важный payload
            provider_token="",  # Stars
            currency="XTR",
            prices=prices,
        )
        await query.answer()
        return

    # 🔹 Остальные (старые) пакеты
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
    register_user(uid)
    payer_users.add(uid)

    sp = message.successful_payment
    payload = sp.invoice_payload

    # 🔹 Оплата за Omni-видео
    if payload == "omni":
        # Пока делаем максимально просто: за покупку Omni добавляем OMNI_PRICE
        # в общий баланс (чтобы его хватило на 1 видео Omni).
        new_balance = await add_user_credits(uid, OMNI_PRICE, "purchase_omni")

        lang = get_lang(uid)
        texts = {
            "ua": f"✅ Оплачено 1 відео Omni (400 ⭐).\nЗараз на балансі: <b>{new_balance}</b>",
            "en": f"✅ Paid for 1 Omni video (400 ⭐).\nCurrent balance: <b>{new_balance}</b>",
            "es": f"✅ Pagado 1 vídeo Omni (400 ⭐).\nSaldo actual: <b>{new_balance}</b>",
            "pt": f"✅ Pago 1 vídeo Omni (400 ⭐).\nSaldo atual: <b>{new_balance}</b>",
        }
        await message.answer(texts.get(lang, texts["en"]))
        return

    # 🔹 Обычные пакеты
    pack = PACKS.get(payload)
    if not pack:
        await message.answer("Payment received, but pack not recognized. Contact admin.")
        return

    title, credits, amount = pack

    # 👇 начисляем кредиты в Postgres
    new_balance = await add_user_credits(uid, credits, f"purchase_{payload}")

    global pack_stats
    if payload in pack_stats:
        pack_stats[payload] += 1

    inviter_id = ref_inviter.get(uid)
    if inviter_id:
        register_user(inviter_id)
        total_stars = sp.total_amount
        bonus_stars = int(total_stars * 0.05)
        if bonus_stars > 0:
            # реферальные Stars по-прежнему считаем в памяти
            ref_stars_total[inviter_id] = ref_stars_total.get(inviter_id, 0) + bonus_stars
            ref_stars_balance[inviter_id] = ref_stars_balance.get(inviter_id, 0) + bonus_stars

            gained_credits = 0
            while ref_stars_balance[inviter_id] >= ANIMATION_PRICE:
                ref_stars_balance[inviter_id] -= ANIMATION_PRICE
                await add_user_credits(inviter_id, ANIMATION_PRICE, "referral_stars_convert")
                gained_credits += 1

            try:
                lang_inv = get_lang(inviter_id)
                inviter_balance = await get_user_credits(inviter_id)  # 👈 из БД

                text = get_ref_bonus_text(
                    lang_inv,
                    bonus_stars=bonus_stars,
                    gained_credits=gained_credits,
                    credits_balance=inviter_balance,
                )
                await bot.send_message(inviter_id, text)
            except Exception as e:
                logger.warning("Failed to notify inviter about stars bonus: %s", e)

    await message.answer(
        tr(uid, "paid_ok").format(
            credits=credits,
            balance=new_balance,  # 👈 баланс из БД
        )
    )

# ---------- Главное меню: текстовые кнопки ----------

@dp.message(F.text)
async def on_text(message: Message):
    text = message.text or ""
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)

    # 🔥 FIX: авто-определение языка по кнопке
    if uid not in user_lang:
        guessed = detect_lang_by_button(text)
        if guessed:
            user_lang[uid] = guessed

    lang = get_lang(uid)
    labels = get_menu_labels(lang)

    # 🪄 Оживить фото — всегда включает режим фото
    if text == labels["animate"]:
        awaiting_support.pop(uid, None)
        awaiting_video_order.pop(uid, None)
        user_mode[uid] = MODE_PHOTO

        prompt_texts = {
            "ua": "🪄 Надішли мені фото, і я оживлю його. Найкраще працюють фронтальні портрети з хорошим світлом.",
            "en": "🪄 Send me a photo and I’ll animate it. Front-facing portraits with good light work best.",
            "es": "🪄 Envíame una foto y la animaré. Los retratos frontales con buena luz funcionan mejor.",
            "pt": "🪄 Envie uma foto e eu vou animá-la. Retratos de frente com boa iluminação funcionam melhor.",
        }
        await message.answer(prompt_texts.get(lang, prompt_texts["en"]))
        return

    # 🧠 Говорящая голова (Omni)
    if text == labels["omni"]:
        awaiting_support.pop(uid, None)
        awaiting_video_order.pop(uid, None)
        user_mode[uid] = MODE_DUB

        prompt_texts = {
            "ua": "🧠 Режим говорячої голови (OmniHuman).\n\n1) Надішли фото з обличчям\n2) Потім — аудіо (voice або аудіофайл).",
            "en": "🧠 Talking head mode (OmniHuman).\n\n1) Send a photo with a face\n2) Then send an audio (voice message or audio file).",
            "es": "🧠 Modo cabeza parlante (OmniHuman).\n\n1) Envía una foto con rostro\n2) Luego envía un audio (nota de voz o archivo).",
            "pt": "🧠 Modo cabeça falante (OmniHuman).\n\n1) Envie uma foto com rosto\n2) Depois envie um áudio (mensagem de voz ou arquivo).",
        }
        await message.answer(prompt_texts.get(lang, prompt_texts["en"]))
        return

    if text == labels["buy"]:
        awaiting_support.pop(uid, None)
        awaiting_video_order.pop(uid, None)
        await message.answer(tr(uid, "buy_title"), reply_markup=buy_menu_keyboard(uid))
        return

    if text == labels["balance"]:
        awaiting_support.pop(uid, None)
        awaiting_video_order.pop(uid, None)

        credits = await get_user_credits(uid)  # 👈

        await message.answer(
            tr(uid, "balance_title").format(credits=credits)
        )
        return


    if text == labels["support"]:
        awaiting_video_order.pop(uid, None)
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
        awaiting_video_order.pop(uid, None)
        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
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

    if text == labels["order_video"]:
        awaiting_support.pop(uid, None)
        awaiting_video_order[uid] = True
        msg = {
            "ua": "🎬 Опиши, будь ласка, яке відео тобі потрібно: формат, тривалість, стиль, для чого воно — і ми звʼяжемося з тобою з детальною пропозицією.",
            "en": "🎬 Please describe what kind of video you need: format, length, style, purpose — and we’ll get back to you with a custom offer.",
            "es": "🎬 Describe qué tipo de vídeo necesitas: formato, duración, estilo y propósito — y nos pondremos en contacto contigo con una propuesta.",
            "pt": "🎬 Descreva que tipo de vídeo você precisa: formato, duração, estilo e objetivo — e entraremos em contato com uma proposta.",
        }.get(lang, "🎬 Please describe what kind of video you need in one message.")
        await message.answer(msg)
        return

    if text == labels["partner"]:
        awaiting_support.pop(uid, None)
        awaiting_video_order.pop(uid, None)
        dash = build_partner_dashboard_text(uid)
        await message.answer(dash, reply_markup=partner_keyboard(uid))
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

    if awaiting_video_order.get(uid):
        dest = ORDER_CHAT_ID or SUPPORT_CHAT_ID or ADMIN_USER_ID
        if dest:
            username = (message.from_user.username if message.from_user else None) or "unknown"
            header = f"🎬 New video order from @{username} (id={uid}):"
            try:
                await bot.send_message(
                    chat_id=dest,
                    text=f"{header}\n\n{text}"
                )
                confirm = {
                    "ua": "✅ Дякуємо! Твоє замовлення на відео передано. Ми звʼяжемося з тобою найближчим часом.",
                    "en": "✅ Thank you! Your video request has been sent. We’ll contact you shortly.",
                    "es": "✅ ¡Gracias! Tu solicitud de vídeo ha sido enviada. Nos pondremos en contacto contigo pronto.",
                    "pt": "✅ Obrigado! Seu pedido de vídeo foi enviado. Entraremos em contato em breve.",
                }.get(lang, "✅ Your video request has been sent. We’ll contact you soon.")
                await message.answer(confirm)
            except Exception as e:
                logger.exception("Failed to send video order message: %s", e)
                await message.answer("⚠️ Video orders are temporarily unavailable. Please try again later.")
        else:
            await message.answer("⚠️ Video order chat is not configured yet. Contact bot admin.")
        awaiting_video_order.pop(uid, None)
        return
    # Остальной текст игнорим — фото и др. обрабатываются отдельными хендлерами

# ---------- Callback: реферальные кнопки (share + stats) ----------

@dp.callback_query(F.data == "ref:share")
async def on_ref_share(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    lang = get_lang(uid)
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
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
    await query.message.answer(share_texts.get(lang, share_texts["en"]))
    await query.answer()


@dp.callback_query(F.data == "ref:stats")
async def on_ref_stats(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)

    text = await build_referral_stats_text(uid)  # 👈

    await query.message.answer(text)
    await query.answer()

# ---------- Callback: партнёрский кабинет (share + reload) ----------

@dp.callback_query(F.data == "partner:reload")
async def on_partner_reload(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    text = build_partner_dashboard_text(uid)
    await query.message.edit_text(text, reply_markup=partner_keyboard(uid))
    await query.answer("Оновлено!")

# ---------- Фото ----------
@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id if message.from_user else 0

    # ✅ Регистрируем пользователя в БД (таблица users по tg_id)
    await ensure_user(uid)

    # 🔹 Если register_user делает что-то ещё (настройка режима, локали и т.п.) — оставляем
    register_user(uid)

    # как и было
    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)

    mode = get_mode(uid)

    # ------ 1) Режим говорящей головы (OmniHuman) ------
    if mode == MODE_DUB:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        omni_pending_photo[uid] = image_url

        lang = get_lang(uid)
        texts = {
            "ua": "✅ Фото збережено! Тепер надішли аудіо (voice або аудіофайл).",
            "en": "✅ Photo saved! Now send an audio (voice message or audio file).",
            "es": "✅ Foto guardada. Ahora envía un audio.",
            "pt": "✅ Foto salva! Agora envie um áudio.",
        }
        await message.answer(texts.get(lang, texts["en"]))
        return

    # ------ 2) Обычная анимация фото (как было раньше), но через Postgres ------
    is_admin = (uid == ADMIN_USER_ID)

    # 🧠 Лимиты: если не тестовый админ, проверяем, можно ли вообще продолжать
    if not (TEST_MODE and is_admin):
        # использовал ли юзер бесплатное оживление?
        free_used = await has_used_free(uid)

        if free_used:
            # бесплатка уже была → смотрим баланс кредитов
            credits_balance = await get_user_credits(uid)

            if credits_balance <= 0:
                # ❌ ни бесплатки, ни кредитов — дальше не пускаем
                await message.answer(tr(uid, "free_used"))
                return
        # если free_used == False → бесплатка ещё не использована, пропускаем дальше без проверок

    # дальше оставляем твою логику без изменений
    photo = message.photo[-1]

    width = photo.width
    height = photo.height
    file_size = getattr(photo, "file_size", 0) or 0

    area = width * height
    is_small_res = area < 400_000 or max(width, height) < 700
    is_small_size = file_size and file_size < 200_000

    is_old_like = is_small_res or is_small_size

    pending_photo[uid] = {
        "file_id": photo.file_id,
        "caption": (message.caption or "").strip(),
        "is_old_like": is_old_like,
    }
    pending_choice.pop(uid, None)

    lang = get_lang(uid)

    if is_old_like:
        idx = 4
        pending_choice[uid] = {"type": "preset", "idx": idx}

        titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])
        title_txt = titles[idx] if 0 <= idx < len(titles) else "Blink & Glow"

        desc_map = LOCALES.get(lang, {}).get("preset_desc", {})
        desc = desc_map.get(str(idx + 1), "") if isinstance(desc_map, dict) else ""

        confirm_texts = {
            "ua": "✨ Це фото виглядає як старе. Використати цей пресет?",
            "en": "✨ This photo looks old. Use this preset?",
            "es": "✨ Esta foto parece antigua. ¿Usar este preset?",
            "pt": "✨ Esta foto parece antiga. Usar este preset?",
        }
        confirm_line = confirm_texts.get(lang, confirm_texts["en"])

        header_text = f"🎨 {title_txt}\n\n{desc}\n\n{confirm_line}".strip()

        await message.answer(
            header_text,
            reply_markup=confirm_preset_keyboard(uid)
        )
        return

    await message.answer(
        tr(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption=bool(pending_photo[uid]["caption"])),
    )


# ---------- Аудио для OmniHuman ----------
@dp.message(F.audio | F.voice)
async def on_audio_omni(message: Message):
    uid = message.from_user.id if message.from_user else 0
    register_user(uid)
    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)

    # работаем только в режиме говорящей головы
    if get_mode(uid) != MODE_DUB:
        return

    image_url = omni_pending_photo.get(uid)
    if not image_url:
        lang = get_lang(uid)
        texts = {
            "ua": "Спочатку надішли фото 🙂",
            "en": "First send a photo 🙂",
            "es": "Primero envía una foto 🙂",
            "pt": "Primeiro envie uma foto 🙂",
        }
        await message.answer(texts.get(lang, texts["en"]))
        return

    lang = get_lang(uid)
    is_admin = (uid == ADMIN_USER_ID)

    # ---- ПРОВЕРКА СТАРОВ ДЛЯ OMNI ----
    # В TEST_MODE для админа — без списаний и без проверок
    if not (TEST_MODE and is_admin):
        credits = await get_user_credits(uid)  # 👈 из БД
        if credits < OMNI_PRICE:
            not_enough_texts = {
                "ua": (
                    f"🧠 Режим говорячої голови коштує <b>{OMNI_PRICE} Stars</b>.\n"
                    f"У тебе зараз {credits} ⭐️.\n\nНатисни кнопку нижче, щоб поповнити баланс."
                ),
                "en": (
                    f"🧠 Talking head mode costs <b>{OMNI_PRICE} Stars</b>.\n"
                    f"You now have {credits} ⭐️.\n\nTap the button below to top up."
                ),
                "es": (
                    f"🧠 El modo cabeza parlante cuesta <b>{OMNI_PRICE} Stars</b>.\n"
                    f"Ahora tienes {credits} ⭐️.\n\nPulsa el botón de abajo para recargar."
                ),
                "pt": (
                    f"🧠 O modo cabeça falante custa <b>{OMNI_PRICE} Stars</b>.\n"
                    f"Você tem {credits} ⭐️.\n\nToque no botão abaixo para recarregar."
                ),
            }
            await message.answer(
                not_enough_texts.get(lang, not_enough_texts["en"]),
                reply_markup=buy_cta_keyboard(uid),
            )
            return

    # ---- Получаем URL аудио из Telegram ----
    audio_file_id = message.audio.file_id if message.audio else message.voice.file_id
    file_info_a = await bot.get_file(audio_file_id)
    audio_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info_a.file_path}"

    msg = await message.answer(
        {
            "ua": "🎧 Створюю відео…",
            "en": "🎧 Creating video…",
            "es": "🎧 Creando vídeo…",
            "pt": "🎧 Criando vídeo…",
        }.get(lang, "🎧 Creating video…")
    )

    global gen_success, gen_fail

    try:
        result = await omni_talking_head(image_url=image_url, audio_url=audio_url)
    except Exception as e:
        gen_fail += 1
        await msg.edit_text(f"⚠️ Omni exception: {e}")
        return

    if not result.get("ok"):
        gen_fail += 1
        await msg.edit_text(f"⚠️ Omni error: {result.get('error')}")
        return

    gen_success += 1
    out_url = result["url"]

    tmp_path = os.path.join(DOWNLOAD_TMP_DIR, f"omni_{uid}.mp4")
    try:
        # качаем файл, чтобы отправить как видео
        await download_file(out_url, tmp_path)

        try:
            await msg.delete()
        except Exception:
            pass

        wm_map = {
            "ua": "\n\n🔖 Зроблено в Magl’sBot (OmniHuman)",
            "en": "\n\n🔖 Made with Magl’sBot (OmniHuman)",
            "es": "\n\n🔖 Hecho con Magl’sBot (OmniHuman)",
            "pt": "\n\n🔖 Feito com Magl’sBot (OmniHuman)",
        }
        watermark_suffix = wm_map.get(lang, wm_map["en"])

        await bot.send_video(
            chat_id=message.chat.id,
            video=FSInputFile(tmp_path),
            caption=tr(uid, "done") + watermark_suffix,
            reply_markup=buy_cta_keyboard(uid),
        )

        # 💰 списываем 400 "кредитов" за Omni в БД (кроме админа в TEST_MODE)
        if not (TEST_MODE and is_admin):
            ok, new_balance = await consume_user_credit(uid, OMNI_PRICE)
            if not ok:
                logger.warning("User %s had insufficient credits when charging Omni", uid)

    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    # очищаем сохранённое фото, чтобы следующая Omni была с новым фото
    omni_pending_photo.pop(uid, None)




@dp.callback_query(F.data.startswith("preset:"))
async def on_preset(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
    data = query.data.split(":", 1)[1]
    info = pending_photo.get(uid)

    if not info:
        await query.message.edit_text(tr(uid, "done"))
        await query.answer()
        return

    if data == "cancel":
        pending_photo.pop(uid, None)
        pending_choice.pop(uid, None)
        await query.message.edit_text(tr(uid, "btn_cancel"))
        await query.answer()
        return

    lang = get_lang(uid)

    confirm_texts = {
        "ua": "✅ Запустити анімацію з цим пресетом?",
        "en": "✅ Start animation with this preset?",
        "es": "✅ ¿Iniciar la animación con este preset?",
        "pt": "✅ Iniciar a animação com este preset?",
    }
    confirm_line = confirm_texts.get(lang, confirm_texts["en"])

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

    desc_map = LOCALES.get(lang, {}).get("preset_desc", {})
    desc = ""
    if isinstance(desc_map, dict):
        desc = desc_map.get(str(idx + 1), "")

    if desc:
        header_text = f"🎨 {title_txt}\n\n{desc}\n\n{confirm_line}"
    else:
        header_text = f"🎨 {title_txt}\n\n{confirm_line}"

    await query.message.edit_text(header_text, reply_markup=confirm_preset_keyboard(uid))
    await query.answer()

# ---------- Подтверждение пресета (✅ / 🔙) ----------

@dp.callback_query(F.data == "confirm:back")
async def on_confirm_back(query: CallbackQuery):
    uid = query.from_user.id
    register_user(uid)
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

    # ✅ гарантируем, что юзер есть в таблице users
    await ensure_user(uid)

    # если register_user делает что-то ещё (локали, режим и т.п.) — оставляем
    register_user(uid)

    info = pending_photo.get(uid)
    choice = pending_choice.get(uid)
    if not info or not choice:
        await query.message.edit_text(tr(uid, "done"))
        await query.answer()
        return

    is_admin = (uid == ADMIN_USER_ID)

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

        # ---- Проверка ответа от Replicate ----
        if not result.get("ok"):
            gen_fail += 1
            err = result.get("error") or "unknown"
            await query.message.edit_text(
                "⚠️ Модель зараз перевантажена, спробуй ще раз через хвилину."
            )
            return

        gen_success += 1

        video_url = result["url"]
        tmp_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{info['file_id']}.mp4")
        await download_file(video_url, tmp_path)

    except Exception as e:
        gen_fail += 1
        logger.exception("Animation error: %s", e)
        await query.message.edit_text("Error while processing. Try another photo.")
        return

    # ---- сюда мы попадаем только если всё ОК ----

    wm_map = {
        "ua": "\n\n🔖 Зроблено в Magl’sBot",
        "en": "\n\n🔖 Made with Magl’sBot",
        "es": "\n\n🔖 Hecho en Magl’sBot",
        "pt": "\n\n🔖 Feito no Magl’sBot",
    }
    watermark_suffix = wm_map.get(lang, "\n\n🔖 Made with Magl’sBot")

    await bot.send_video(
        chat_id=query.message.chat.id,
        video=FSInputFile(tmp_path),
        caption=tr(uid, "done") + watermark_suffix,
        reply_markup=buy_cta_keyboard(uid),
    )

    ref_text = referral_info_text(lang)
    await bot.send_message(
        chat_id=query.message.chat.id,
        text=ref_text,
    )

    # 💾 после УСПЕШНОЙ генерации:
    # либо отмечаем бесплатку, либо списываем кредит
    if not (TEST_MODE and is_admin):
        free_used = await has_used_free(uid)

        if not free_used:
            # это была первая (бесплатная) анимация
            await mark_free_used(uid)
        else:
            # бесплатка уже была — списываем 1 кредит через helpers_credits
            ok, new_balance = await consume_user_credit(uid, ANIMATION_PRICE)
            if not ok:
                logger.warning("User %s has no credits at confirm stage", uid)

    try:
        os.remove(tmp_path)
    except Exception:
        pass

    pending_photo.pop(uid, None)
    pending_choice.pop(uid, None)

# ---------- MAIN ----------

async def main_async():
    # 1️⃣ Подключаемся к базе
    await init_db()

    # 2️⃣ Запускаем фоновый воркер (если используешь)
    asyncio.create_task(referral_reminder_worker())

    # 3️⃣ Запускаем бота
    try:
        await dp.start_polling(bot)
    finally:
        # 4️⃣ Закрываем соединение с БД
        await close_db()


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

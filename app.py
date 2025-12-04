import os
import json
import time
import asyncio
import logging
import random
from pathlib import Path
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    FSInputFile,
    LabeledPrice, PreCheckoutQuery
)

from dotenv import load_dotenv

# --- наши модули ---
from db import (
    init_db, close_db,
    ensure_user,
    has_used_free, mark_free_used,
    register_referral as db_register_referral,
)
from helpers_credits import (
    get_user_credits,
    add_user_credits,
    consume_user_credit,
)
from processing import (
    animate_photo_via_replicate,
    omni_talking_head,
    download_file
)

# ================================
#     ИНИЦИАЛИЗАЦИЯ
# ================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s:%(name)s:%(message)s'
)
logger = logging.getLogger("maglsbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))
SUPPORT_CHAT_ID = int(os.getenv("SUPPORT_CHAT_ID", 0))
ORDER_CHAT_ID = int(os.getenv("ORDER_CHAT_ID", 0))
DOWNLOAD_TMP_DIR = os.getenv("DOWNLOAD_TMP_DIR", "/tmp")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ================================
#     РЕЖИМЫ БОТА
# ================================
MODE_PHOTO = "photo"     # обычная анимация фото
MODE_OMNI = "omni"       # говорящая голова (фото + аудио)

user_mode: Dict[int, str] = {}          # uid → "photo"/"omni"
omni_pending_photo: Dict[int, str] = {} # uid → фото URL для Omni

def get_mode(uid: int) -> str:
    return user_mode.get(uid, MODE_PHOTO)

# ================================
#     ЛОКАЛИЗАЦИИ
# ================================
LOCALE_CODES = ("ua", "en", "es", "pt")
DEFAULT_LANG = "en"
LOCALES: Dict[str, Dict[str, Any]] = {}
user_lang: Dict[int, str] = {}   # uid → "ua"/"en"/"es"/"pt"

def load_locales():
    base = Path(__file__).parent / "locales"
    for code in LOCALE_CODES:
        path = base / f"{code}.json"
        if not path.exists():
            logger.warning(f"Locale missing: {path}")
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                LOCALES[code] = json.load(f)
            logger.info(f"Locale {code} loaded")
        except Exception as e:
            logger.exception(f"Locale {code} load error: {e}")

load_locales()

if DEFAULT_LANG not in LOCALES:
    raise RuntimeError("Default locale not loaded")

def get_lang(uid: int) -> str:
    return user_lang.get(uid, DEFAULT_LANG)

def tr(uid: int, key: str) -> str:
    lang = get_lang(uid)
    loc = LOCALES.get(lang, LOCALES[DEFAULT_LANG])
    return loc.get(key, LOCALES[DEFAULT_LANG].get(key, ""))

def tr_lang(lang: str, key: str) -> str:
    loc = LOCALES.get(lang, LOCALES[DEFAULT_LANG])
    return loc.get(key, LOCALES[DEFAULT_LANG].get(key, ""))


# ================================
#     ВСТУПИТЕЛЬНОЕ ВИДЕО
# ================================
INTRO_VIDEO_FILE_ID = os.getenv(
    "INTRO_VIDEO_FILE_ID",
    "BAACAgIAAxkBAAICuWkgf1x1yIEgxE8FQoImZ5vuoxbOAALGiwACIA4JSfhC7_NPZQrDNgQ"
)
# ================================================================
#                      ЯЗЫК — ВЫБОР ПОЛЬЗОВАТЕЛЕМ
# ================================================================

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


# ================================================================
#                   КНОПКИ МЕНЮ (ReplyKeyboard)
# ================================================================

MENU_BUTTONS = {
    "ua": {
        "animate": "🪄 Оживити фото",
        "omni": "🧠 Говоряча голова (Omni)",
        "buy": "💫 Купити генерації",
        "balance": "💰 Баланс",
        "support": "🆘 Підтримка",
        "share": "📤 Розповісти друзям",
        "order_video": "🎬 Замовити відео під ключ",
        "partner": "🤝 Партнерський кабінет",
    },
    "en": {
        "animate": "🪄 Animate photo",
        "omni": "🧠 Talking head (Omni)",
        "buy": "💫 Buy generations",
        "balance": "💰 Balance",
        "support": "🆘 Support",
        "share": "📤 Tell friends",
        "order_video": "🎬 Order custom video",
        "partner": "🤝 Partner dashboard",
    },
    "es": {
        "animate": "🪄 Animar foto",
        "omni": "🧠 Cabeza parlante (Omni)",
        "buy": "💫 Comprar generaciones",
        "balance": "💰 Balance",
        "support": "🆘 Soporte",
        "share": "📤 Compartir",
        "order_video": "🎬 Encargar video a medida",
        "partner": "🤝 Panel de socio",
    },
    "pt": {
        "animate": "🪄 Animar foto",
        "omni": "🧠 Cabeça falante (Omni)",
        "buy": "💫 Comprar gerações",
        "balance": "💰 Saldo",
        "support": "🆘 Suporte",
        "share": "📤 Compartilhar",
        "order_video": "🎬 Encomendar vídeo sob medida",
        "partner": "🤝 Painel de parceiro",
    },
}

def get_menu_labels(lang: str) -> Dict[str, str]:
    return MENU_BUTTONS.get(lang, MENU_BUTTONS["en"])


def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    lang = get_lang(uid)
    labels = get_menu_labels(lang)
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [
                KeyboardButton(text=labels["animate"]),
                KeyboardButton(text=labels["omni"]),
            ],
            [
                KeyboardButton(text=labels["buy"]),
                KeyboardButton(text=labels["balance"]),
            ],
            [
                KeyboardButton(text=labels["support"]),
                KeyboardButton(text=labels["share"]),
            ],
            [KeyboardButton(text=labels["order_video"])],
            [KeyboardButton(text=labels["partner"])],
        ]
    )


# ================================================================
#                        РЕЖИМЫ БОТА (UI)
# ================================================================

def mode_choice_keyboard(lang: str) -> InlineKeyboardMarkup:
    labels = {
        "ua": {"photo": "✨ Оживлення фото", "omni": "🧠 Говоряча голова (Omni)"},
        "en": {"photo": "✨ Photo animation", "omni": "🧠 Talking head (Omni)"},
        "es": {"photo": "✨ Animar foto", "omni": "🧠 Cabeza parlante (Omni)"},
        "pt": {"photo": "✨ Animação de foto", "omni": "🧠 Cabeça falante (Omni)"},
    }
    l = labels.get(lang, labels["en"])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=l["photo"], callback_data="mode:photo")],
            [InlineKeyboardButton(text=l["omni"],  callback_data="mode:omni")],
        ]
    )


def mode_choice_text(lang: str) -> str:
    mapping = {
        "ua": (
            "Обери режим магії 🪄\n\n"
            "1️⃣ <b>Оживлення фото</b>\n"
            "2️⃣ <b>Говоряча голова (OmniHuman)</b> — фото + аудіо → відео"
        ),
        "en": (
            "Choose your magic mode 🪄\n\n"
            "1️⃣ <b>Photo animation</b>\n"
            "2️⃣ <b>Talking head (OmniHuman)</b> — photo + audio → video"
        ),
        "es": (
            "Elige el modo de magia 🪄\n\n"
            "1️⃣ <b>Animación de foto</b>\n"
            "2️⃣ <b>Cabeza parlante (OmniHuman)</b> — foto + audio → video"
        ),
        "pt": (
            "Escolha o modo mágico 🪄\n\n"
            "1️⃣ <b>Animação de foto</b>\n"
            "2️⃣ <b>Cabeça falante (OmniHuman)</b> — foto + áudio → vídeo"
        ),
    }
    return mapping.get(lang, mapping["en"])


# ================================================================
#                           /start
# ================================================================

known_users: set[int] = set()

def register_user(uid: int):
    if uid > 0:
        known_users.add(uid)


@dp.message(CommandStart())
async def on_start(message: Message):
    uid = message.from_user.id
    register_user(uid)

    # рефералка
    payload = (message.text or "").split(maxsplit=1)
    payload = payload[1] if len(payload) > 1 else ""
    if payload.startswith("ref_"):
        try:
            inviter = int(payload[4:])
            await register_referral(uid, inviter)
            register_user(inviter)
        except:
            pass

    # выбор языка
    if uid not in user_lang:
        caption = "Magl’sBot вітає тебе!\n✨ Обери мову:"
        try:
            await message.answer_video(
                video=INTRO_VIDEO_FILE_ID,
                caption=caption,
                supports_streaming=True,
                reply_markup=lang_choice_keyboard()
            )
        except:
            await message.answer(caption, reply_markup=lang_choice_keyboard())
        return

    # если язык выбран — отправляем интро + меню
    try:
        await message.answer_video(
            video=INTRO_VIDEO_FILE_ID,
            supports_streaming=True
        )
    except:
        pass

    await message.answer(tr(uid, "welcome"), reply_markup=main_menu_keyboard(uid))


# ================================================================
#                      УСТАНОВКА ЯЗЫКА
# ================================================================

@dp.callback_query(F.data.startswith("lang:"))
async def on_lang_set(query: CallbackQuery):
    uid = query.from_user.id
    _, code = query.data.split(":", 1)

    if code not in LOCALES:
        await query.answer("Unavailable", show_alert=True)
        return

    user_lang[uid] = code

    try:
        await query.message.edit_caption(tr(uid, "lang_set"))
    except:
        try:
            await query.message.edit_text(tr(uid, "lang_set"))
        except:
            await query.message.answer(tr(uid, "lang_set"))

    # предлагаем выбрать режим
    await query.message.answer(
        mode_choice_text(code),
        reply_markup=mode_choice_keyboard(code)
    )

    await query.answer()


# ================================================================
#                   ВЫБОР РЕЖИМА (photo / omni)
# ================================================================

@dp.callback_query(F.data == "mode:photo")
async def mode_photo(query: CallbackQuery):
    uid = query.from_user.id
    user_mode[uid] = MODE_PHOTO
    lang = get_lang(uid)

    texts = {
        "ua": "Режим: ✨ Оживлення фото.\nНадішли фото 🪄",
        "en": "Mode: ✨ Photo animation.\nSend a photo 🪄",
        "es": "Modo: ✨ Animar foto.\nEnvía una foto 🪄",
        "pt": "Modo: ✨ Animação de foto.\nEnvie uma foto 🪄",
    }

    await query.message.answer(texts.get(lang, texts["en"]), reply_markup=main_menu_keyboard(uid))
    await query.answer()


@dp.callback_query(F.data == "mode:omni")
async def mode_omni(query: CallbackQuery):
    uid = query.from_user.id
    user_mode[uid] = MODE_OMNI
    lang = get_lang(uid)

    texts = {
        "ua": "Режим: 🧠 Говоряча голова.\n1) Надішли фото\n2) Потім аудіо",
        "en": "Mode: 🧠 Talking head.\n1) Send a photo\n2) Then audio",
        "es": "Modo: 🧠 Cabeza parlante.\n1) Envía una foto\n2) Luego audio",
        "pt": "Modo: 🧠 Cabeça falante.\n1) Envie uma foto\n2) Depois um áudio",
    }

    await query.message.answer(texts.get(lang, texts["en"]), reply_markup=main_menu_keyboard(uid))
    await query.answer()
# ================================================================
#                PACKS — ЦЕНЫ, КОЛИЧЕСТВА, ОМНИ
# ================================================================

OMNI_PRICE = 400  # сколько Stars стоит одно Omni-видео

PACKS = {
    "pack_1":  ("1 animation", 1, 60),
    "pack_3":  ("3 animations", 3, 150),
    "pack_5":  ("5 animations", 5, 300),
    "pack_10": ("10 animations", 10, 500),
    "pack_25": ("25 animations", 25, 1000),
}

payer_users: set[int] = set()            # пользователи, которые хоть раз платили
ref_inviter: dict[int, int] = {}         # кто кого пригласил
ref_stars_total: dict[int, int] = {}     # всего начислено Stars
ref_stars_balance: dict[int, int] = {}   # остаток Stars до конвертации


# ================================================================
#                      BUY MENU / CTA KEYBOARD
# ================================================================

def buy_menu_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)

    omni_labels = {
        "ua": "🧠 1 відео Omni — 400 ⭐",
        "en": "🧠 1 Omni video — 400 ⭐",
        "es": "🧠 1 vídeo Omni — 400 ⭐",
        "pt": "🧠 1 vídeo Omni — 400 ⭐",
    }
    omni_text = omni_labels.get(lang, "🧠 1 Omni video — 400 ⭐")

    popular_text = "🔥 " + tr_lang(lang, "buy_btn_3")

    buttons = [
        InlineKeyboardButton(text=omni_text, callback_data="buy:omni"),
        InlineKeyboardButton(text=popular_text, callback_data="buy:pack_3"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_5"), callback_data="buy:pack_5"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_10"), callback_data="buy:pack_10"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_25"), callback_data="buy:pack_25"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_1"), callback_data="buy:pack_1"),
    ]

    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons])


def buy_cta_keyboard(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)

    omni_labels = {
        "ua": "🧠 1 відео Omni — 400 ⭐",
        "en": "🧠 1 Omni video — 400 ⭐",
        "es": "🧠 1 vídeo Omni — 400 ⭐",
        "pt": "🧠 1 vídeo Omni — 400 ⭐",
    }
    omni_text = omni_labels.get(lang, "🧠 1 Omni video — 400 ⭐")

    popular_text = "🔥 " + tr_lang(lang, "buy_btn_3")

    buy_buttons = [
        InlineKeyboardButton(text=omni_text, callback_data="buy:omni"),
        InlineKeyboardButton(text=popular_text, callback_data="buy:pack_3"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_5"), callback_data="buy:pack_5"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_10"), callback_data="buy:pack_10"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_25"), callback_data="buy:pack_25"),
        InlineKeyboardButton(text=tr_lang(lang, "buy_btn_1"), callback_data="buy:pack_1"),
    ]

    # кнопка "Поделиться"
    share_labels = {
        "ua": "📤 Поділитися",
        "en": "📤 Share",
        "es": "📤 Compartir",
        "pt": "📤 Compartilhar",
    }
    ref_link = f"https://t.me/LIvePotterPhotoBot?start=ref_{uid}"

    share_btn = InlineKeyboardButton(
        text=share_labels.get(lang, "📤 Share"),
        url=ref_link
    )

    rows = [[b] for b in buy_buttons]
    rows.append([share_btn])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ================================================================
#                     CALLBACK “buy:*”
# ================================================================

@dp.callback_query(F.data.startswith("buy:"))
async def on_buy_click(query: CallbackQuery):
    uid = query.from_user.id
    code = query.data.split(":", 1)[1]

    # 🧠 Покупка Omni
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
            payload="omni",
            provider_token="",  # Stars
            currency="XTR",
            prices=prices,
        )
        await query.answer()
        return

    # 🪄 Покупка стандартных пакетов
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
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await query.answer()


# ================================================================
#               Stars: подтверждение платежа
# ================================================================

@dp.pre_checkout_query()
async def on_checkout(pre: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre.id, ok=True)


# ================================================================
#                 Stars успешный платёж
# ================================================================

@dp.message(F.successful_payment)
async def on_payment(message: Message):
    uid = message.from_user.id
    sp = message.successful_payment
    payload = sp.invoice_payload

    payer_users.add(uid)

    # ============================================================
    #               Покупка Omni 400 ⭐
    # ============================================================
    if payload == "omni":
        new_balance = await add_user_credits(uid, OMNI_PRICE, "purchase_omni")
        lang = get_lang(uid)

        texts = {
            "ua": f"✅ Оплачено 1 відео Omni (400 ⭐).\nБаланс: <b>{new_balance}</b>",
            "en": f"✅ Paid for 1 Omni video (400 ⭐).\nBalance: <b>{new_balance}</b>",
            "es": f"✅ Pagado 1 vídeo Omni (400 ⭐).\nSaldo: <b>{new_balance}</b>",
            "pt": f"✅ Pago 1 vídeo Omni (400 ⭐).\nSaldo: <b>{new_balance}</b>",
        }

        await message.answer(texts.get(lang, texts["en"]))
        return

    # ============================================================
    #               Покупка стандартных пакетов
    # ============================================================
    pack = PACKS.get(payload)
    if not pack:
        await message.answer("Payment received but pack not recognized.")
        return

    title, credits, amount = pack

    new_balance = await add_user_credits(uid, credits, f"purchase_{payload}")

    # Увеличение статистики
    global pack_stats
    pack_stats[payload] += 1

    # ============================================================
    #                  Рефералка — 5% Stars
    # ============================================================
    inviter_id = ref_inviter.get(uid)
    if inviter_id:
        total_stars = sp.total_amount
        bonus_stars = total_stars * 5 // 100

        ref_stars_total[inviter_id] = ref_stars_total.get(inviter_id, 0) + bonus_stars
        ref_stars_balance[inviter_id] = ref_stars_balance.get(inviter_id, 0) + bonus_stars

        # 🔄 Конвертация каждых 60 ⭐ → 1 анимация
        gained = 0
        while ref_stars_balance[inviter_id] >= 60:
            ref_stars_balance[inviter_id] -= 60
            await add_user_credits(inviter_id, 1, "referral_stars_convert")
            gained += 1

        try:
            lang_i = get_lang(inviter_id)
            inviter_balance = await get_user_credits(inviter_id)

            msg = get_ref_bonus_text(
                lang_i,
                bonus_stars=bonus_stars,
                gained_credits=gained,
                credits_balance=inviter_balance,
            )
            await bot.send_message(inviter_id, msg)

        except Exception as e:
            logger.warning("Failed to notify inviter: %s", e)

    # ============================================================
    #               Сообщение клиенту после покупки
    # ============================================================

    await message.answer(
        tr(uid, "paid_ok").format(
            credits=credits,
            balance=new_balance
        )
    )
# ================================================================
#                         OMNI HUMAN (DUB)
# ================================================================

OMNI_TIMEOUT = 900  # 15 минут

omni_pending_photo: dict[int, str] = {}   # uid → image_url
omni_pending_audio: dict[int, str] = {}   # uid → audio_url


# ---------- Функция ожидания результата Replicate ----------

async def _poll_replicate_prediction(session, get_url: str, timeout_sec=OMNI_TIMEOUT):
    for _ in range(timeout_sec):
        await asyncio.sleep(1)
        try:
            async with session.get(get_url, headers={"Authorization": f"Token {REPLICATE_API_TOKEN}"}) as r:
                data = await r.json()
        except Exception as e:
            logger.error("Omni poll error: %s", e)
            continue

        status = data.get("status")

        if status == "succeeded":
            output = data.get("output")
            if isinstance(output, list) and output:
                for u in output:
                    if isinstance(u, str) and ("mp4" in u or u.endswith(".mp4")):
                        return {"ok": True, "url": u}
                return {"ok": True, "url": output[0]}
            elif isinstance(output, str):
                return {"ok": True, "url": output}
            else:
                return {"ok": False, "error": "no_output"}

        if status in ("failed", "canceled"):
            return {"ok": False, "error": data.get("error") or status}

    return {"ok": False, "error": "timeout"}


# ---------- Основная функция OmniHuman ----------

async def omni_talking_head(image_url: str, audio_url: str) -> dict:
    if not REPLICATE_OMNI_MODEL or not REPLICATE_API_TOKEN:
        return {"ok": False, "error": "no_omni_model"}

    raw = REPLICATE_OMNI_MODEL.strip()
    version = raw.split(":")[-1] if ":" in raw else raw

    payload = {
        "version": version,
        "input": {
            "image": image_url,
            "audio": audio_url,
        },
    }

    timeout = aiohttp.ClientTimeout(total=OMNI_TIMEOUT)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # create prediction
        try:
            async with session.post(
                "https://api.replicate.com/v1/predictions",
                headers={
                    "Authorization": f"Token {REPLICATE_API_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                if resp.status not in (200, 201):
                    txt = await resp.text()
                    logger.error("Omni create error: %s %s", resp.status, txt)
                    return {"ok": False, "error": "create_failed"}
                pred = await resp.json()
        except Exception as e:
            logger.exception("Omni create exception: %s", e)
            return {"ok": False, "error": "create_exception"}

        get_url = pred.get("urls", {}).get("get")
        if not get_url:
            return {"ok": False, "error": "no_get_url"}

        # polling
        return await _poll_replicate_prediction(session, get_url, OMNI_TIMEOUT)


# ================================================================
#               ВЫБОР РЕЖИМА — DUB (говорящая голова)
# ================================================================

@dp.callback_query(F.data == "mode:dub")
async def switch_to_dub(query: CallbackQuery):
    uid = query.from_user.id
    set_mode(uid, MODE_DUB)

    lang = get_lang(uid)
    txt = {
        "ua": "🧠 Режим OmniHuman активовано!\nНадішли фотографію з обличчям.",
        "en": "🧠 OmniHuman mode activated!\nSend a face photo.",
        "es": "🧠 ¡Modo OmniHuman activado!\nEnvía una foto con rostro.",
        "pt": "🧠 Modo OmniHuman ativado!\nEnvie uma foto com rosto.",
    }
    await query.message.answer(txt.get(lang, txt["en"]))
    await query.answer()


# ================================================================
#                ПОЛУЧЕНИЕ ФОТО ДЛЯ OMNI
# ================================================================

async def process_omni_photo(message: Message, uid: int):
    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    omni_pending_photo[uid] = image_url
    lang = get_lang(uid)

    txt = {
        "ua": "📸 Фото отримано!\nТепер надішли голосове або аудіофайл.",
        "en": "📸 Photo received!\nNow send a voice message or audio file.",
        "es": "📸 ¡Foto recibida!\nAhora envía un mensaje de voz o archivo de audio.",
        "pt": "📸 Foto recebida!\nAgora envie um áudio.",
    }
    await message.answer(txt.get(lang, txt["en"]))


# ================================================================
#                ПОЛУЧЕНИЕ АУДИО ДЛЯ OMNI
# ================================================================

async def process_omni_audio(message: Message, uid: int):
    file = message.voice or message.audio or message.document
    if not file:
        await message.answer("❗ Send a voice or audio file.")
        return

    file_info = await bot.get_file(file.file_id)
    audio_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    omni_pending_audio[uid] = audio_url

    # когда есть фото и аудио → запускаем OmniHuman
    await run_omni_generation(uid, message)


# ================================================================
#            ЗАПУСК ОМНИ-ГЕНЕРАЦИИ (основной процесс)
# ================================================================

async def run_omni_generation(uid: int, message: Message):
    img = omni_pending_photo.get(uid)
    audio = omni_pending_audio.get(uid)

    if not img:
        await message.answer("❗ Спочатку пришліть фото.")
        return

    if not audio:
        await message.answer("❗ Тепер надішліть аудіо.")
        return

    lang = get_lang(uid)

    await message.answer(tr(uid, "status_work"))

    # списываем 400 ⭐ (если не TEST_MODE)
    is_admin = uid == ADMIN_USER_ID
    if not (TEST_MODE and is_admin):
        ok, new_balance = await consume_user_credit(uid, OMNI_PRICE)
        if not ok:
            await message.answer("⚠️ Недостатньо ⭐. Купи пакет у меню!")
            return

    # запускаем Replicate
    result = await omni_talking_head(img, audio)

    if not result.get("ok"):
        await message.answer("⚠️ OmniHuman overloaded. Try again later.")
        return

    # скачиваем полученное видео
    url = result["url"]
    tmp = f"/tmp/omni_{uid}.mp4"
    await download_file(url, tmp)

    await bot.send_video(
        uid,
        FSInputFile(tmp),
        caption="🎉 Готово! Це твоє Omni-вiдео.",
        reply_markup=buy_cta_keyboard(uid)
    )

    try:
        os.remove(tmp)
    except:
        pass

    # очистка
    omni_pending_photo.pop(uid, None)
    omni_pending_audio.pop(uid, None)


# ================================================================
#       МАРШРУТИЗАЦИЯ (куда отправлять фото/аудио)
# ================================================================

@dp.message(F.photo)
async def on_photo(message: Message):
    uid = message.from_user.id
    mode = get_mode(uid)

    if mode == MODE_DUB:
        await process_omni_photo(message, uid)
        return

    # обычная анимация фото → блок 5
    await process_photo_mode(message)


@dp.message(F.voice | F.audio | F.document)
async def on_audio(message: Message):
    uid = message.from_user.id
    mode = get_mode(uid)

    if mode == MODE_DUB:
        await process_omni_audio(message, uid)
# ================================================================
#                      ОБЫЧНАЯ АНИМАЦИЯ ФОТО
# ================================================================

pending_photo: dict[int, dict] = {}      # uid → {file_id, caption, is_old_like}
pending_choice: dict[int, dict] = {}     # uid → {"type": preset|caption, "idx": X}


# ================================================================
#                     ПОЛУЧЕНИЕ ФОТО В MODE_PHOTO
# ================================================================

async def process_photo_mode(message: Message):
    uid = message.from_user.id
    lang = get_lang(uid)

    # ——— регистрация пользователя в БД ———
    await ensure_user(uid)

    photo = message.photo[-1]
    width, height = photo.width, photo.height
    file_size = getattr(photo, "file_size", 0) or 0

    # ——— проверка лимитов (бесплатка или кредиты) ———
    is_admin = uid == ADMIN_USER_ID
    if not (TEST_MODE and is_admin):
        free_used = await has_used_free(uid)
        if free_used:
            balance = await get_user_credits(uid)
            if balance <= 0:
                await message.answer(tr(uid, "free_used"))
                return

    # ——— анализ качества фото ———
    area = width * height
    is_small_res = area < 400_000 or max(width, height) < 700
    is_small_size = file_size < 200_000

    is_old_like = is_small_res or is_small_size

    pending_photo[uid] = {
        "file_id": photo.file_id,
        "caption": (message.caption or "").strip(),
        "is_old_like": is_old_like,
    }
    pending_choice.pop(uid, None)

    # ——— если фото похоже на старое → сразу предлагаем пресет №5 ———
    if is_old_like:
        idx = 4
        pending_choice[uid] = {"type": "preset", "idx": idx}

        title = PRESET_TITLES.get(lang, PRESET_TITLES["en"])[idx]
        desc = LOCALES.get(lang, {}).get("preset_desc", {}).get(str(idx+1), "")

        msg = (
            f"🎨 {title}\n\n"
            f"{desc}\n\n"
            f"{tr(uid, 'confirm_old')}"
        )

        await message.answer(msg, reply_markup=confirm_preset_keyboard(uid))
        return

    # ——— обычное фото → показываем выбор пресета ———
    await message.answer(
        tr(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption=bool(pending_photo[uid]["caption"]))
    )


# ================================================================
#                     ПОДТВЕРЖДЕНИЕ ВЫБОРА ПРЕСЕТА
# ================================================================

@dp.callback_query(F.data.startswith("preset:"))
async def choose_preset(query: CallbackQuery):
    uid = query.from_user.id
    idx = int(query.data.split(":")[1])

    pending_choice[uid] = {"type": "preset", "idx": idx}
    await query.message.edit_text(tr(uid, "confirm_preset"), reply_markup=confirm_preset_keyboard(uid))
    await query.answer()


# ================================================================
#                           ПОДТВЕРЖДЕНИЕ OK
# ================================================================

@dp.callback_query(F.data == "confirm:ok")
async def on_confirm_ok(query: CallbackQuery):
    uid = query.from_user.id
    lang = get_lang(uid)
    info = pending_photo.get(uid)
    choice = pending_choice.get(uid)

    if not info or not choice:
        await query.message.edit_text(tr(uid, "done"))
        await query.answer()
        return

    # ——— готовим prompt ———
    if choice["type"] == "caption":
        prompt = info["caption"] or "natural smile, subtle head motion, cinematic lighting"
    else:
        idx = int(choice["idx"])
        prompt = get_preset_prompt(lang, idx)

    await query.message.edit_text(tr(uid, "status_work"))
    await query.answer()

    # ——— получаем прямую ссылку фото ———
    try:
        file_info = await bot.get_file(info["file_id"])
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
    except Exception:
        await query.message.edit_text("Error loading file.")
        return

    # ——— вызов Replicate ———
    result = await animate_photo_via_replicate(
        source_image_url=file_url,
        prompt=prompt,
    )

    if not result.get("ok"):
        await query.message.edit_text(
            "⚠️ Модель зараз перевантажена, спробуй ще раз через хвилину."
        )
        return

    # ——— скачиваем видео ———
    video_url = result["url"]
    tmp_path = f"/tmp/anim_{uid}.mp4"

    try:
        await download_file(video_url, tmp_path)
    except Exception:
        await query.message.edit_text("Error downloading result.")
        return

    # ——— watermark ———
    wm_map = {
        "ua": "\n\n🔖 Зроблено в Magl’sBot",
        "en": "\n\n🔖 Made with Magl’sBot",
        "es": "\n\n🔖 Hecho en Magl’sBot",
        "pt": "\n\n🔖 Feito no Magl’sBot",
    }
    watermark = wm_map.get(lang, wm_map["en"])

    # ——— отправляем видео ———
    await bot.send_video(
        uid,
        video=FSInputFile(tmp_path),
        caption=tr(uid, "done") + watermark,
        reply_markup=buy_cta_keyboard(uid),
    )

    # ——— реферальное сообщение ———
    await bot.send_message(uid, referral_info_text(lang))

    # =====================================================
    #               СПИСАНИЕ КРЕДИТОВ / БЕСПЛАТКА
    # =====================================================

    is_admin = uid == ADMIN_USER_ID

    if not (TEST_MODE and is_admin):
        free_used = await has_used_free(uid)

        if not free_used:
            await mark_free_used(uid)
        else:
            await consume_user_credit(uid, 1)

    # ——— очищаем временные данные ———
    try:
        os.remove(tmp_path)
    except:
        pass

    pending_photo.pop(uid, None)
    pending_choice.pop(uid, None)
# ================================================================
#                        BLOCK 6 — STARS SYSTEM
# ================================================================

OMNI_PRICE = 400     # стоимость OmniHuman
PHOTO_PRICE = 1      # стоимость обычной анимации
REFERRAL_BONUS = 20  # бонус за приглашенного

# пакеты пополнения Stars
PACKAGES = {
    "pack_1":  {"stars": 10,  "price": 10,  "title": "10 Stars"},
    "pack_5":  {"stars": 50,  "price": 50,  "title": "50 Stars"},
    "pack_10": {"stars": 100, "price": 100, "title": "100 Stars"},
    "pack_25": {"stars": 250, "price": 250, "title": "250 Stars"},
}


# ================================================================
#                   PostgreSQL Stars functions
# ================================================================

async def get_stars(uid: int) -> int:
    row = await _pool.fetchrow(
        "SELECT stars_balance FROM ref_stars WHERE user_id=$1",
        uid
    )
    if not row:
        await _pool.execute(
            "INSERT INTO ref_stars (user_id, stars_balance) VALUES ($1, 0)",
            uid
        )
        return 0
    return row["stars_balance"]


async def add_stars(uid: int, amount: int):
    await _pool.execute("""
        INSERT INTO ref_stars (user_id, stars_balance)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET stars_balance = ref_stars.stars_balance + $2
    """, uid, amount)


async def consume_stars(uid: int, amount: int) -> bool:
    balance = await get_stars(uid)
    if balance < amount:
        return False

    await _pool.execute(
        "UPDATE ref_stars SET stars_balance = stars_balance - $1 WHERE user_id=$2",
        amount, uid
    )
    return True


# ================================================================
#                     Referral bonus (Stars)
# ================================================================

async def apply_referral_bonus(invited_id: int):
    row = await _pool.fetchrow(
        "SELECT inviter_id FROM referrals WHERE invited_id=$1",
        invited_id
    )
    if not row:
        return

    inviter = row["inviter_id"]
    await add_stars(inviter, REFERRAL_BONUS)

    try:
        await bot.send_message(
            inviter,
            f"🎁 Ваш друг поповнив баланс!\nВи отримали +{REFERRAL_BONUS}⭐"
        )
    except:
        pass


# ================================================================
#                       Buy Stars keyboard
# ================================================================

def buy_stars_keyboard(uid: int) -> InlineKeyboardMarkup:
    btns = []
    for key, pack in PACKAGES.items():
        btns.append([
            InlineKeyboardButton(
                text=f"{pack['title']} — {pack['price']}⭐",
                callback_data=f"buy:{key}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=btns)


# ================================================================
#                       /balance command
# ================================================================

@dp.message(Command("balance"))
async def show_balance(message: Message):
    uid = message.from_user.id
    bal = await get_stars(uid)

    txt = (
        f"💰 <b>Ваш баланс:</b> {bal}⭐\n\n"
        f"Щоб поповнити — оберіть пакет нижче."
    )
    await message.answer(txt, reply_markup=buy_stars_keyboard(uid))


# ================================================================
#                     Нажатие “Купить Stars”
# ================================================================

@dp.callback_query(F.data.startswith("buy:"))
async def buy_package(query: CallbackQuery):
    uid = query.from_user.id
    pack_id = query.data.split(":")[1]

    if pack_id not in PACKAGES:
        await query.answer("Unknown pack")
        return

    pack = PACKAGES[pack_id]

    await bot.send_invoice(
        chat_id=uid,
        title=f"Buy {pack['title']}",
        description="Поповнення балансу Stars у Magl’sBot",
        payload=pack_id,
        currency="XTR",
        prices=[LabeledPrice(label=pack["title"], amount=pack["price"])],
        start_parameter="buy_stars"
    )

    await query.answer()


# ================================================================
#                     Stars checkout callback
# ================================================================

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


# ================================================================
#                    Successful payment handler
# ================================================================

@dp.message(F.successful_payment)
async def success_payment(message: Message):
    uid = message.from_user.id
    payload = message.successful_payment.invoice_payload

    if payload not in PACKAGES:
        await message.answer("❗ Unknown package.")
        return

    pack = PACKAGES[payload]

    # начисляем Stars
    await add_stars(uid, pack["stars"])

    # реферальный бонус (если есть пригласивший)
    await apply_referral_bonus(uid)

    await message.answer(
        f"🎉 Поповнення успішне!\n"
        f"Вам зараховано {pack['stars']}⭐.\n\n"
        f"Перевірити баланс → /balance"
    )
# ================================================================
#                    BLOCK 7 — MAIN MENU & UX
# ================================================================

# ---------- Локализованные подписи меню ----------
MENU_LABELS = {
    "ua": {
        "animate": "🪄 Оживити фото",
        "omni": "🧠 Говоряча голова (Omni)",
        "buy": "💫 Купити Stars",
        "balance": "💰 Баланс",
        "support": "🆘 Підтримка",
        "share": "📤 Розповісти друзям",
        "partner": "🤝 Партнерський кабінет",
    },
    "en": {
        "animate": "🪄 Animate photo",
        "omni": "🧠 Talking head (Omni)",
        "buy": "💫 Buy Stars",
        "balance": "💰 Balance",
        "support": "🆘 Support",
        "share": "📤 Tell friends",
        "partner": "🤝 Partner dashboard",
    },
    "es": {
        "animate": "🪄 Animar foto",
        "omni": "🧠 Cabeza parlante (Omni)",
        "buy": "💫 Comprar Stars",
        "balance": "💰 Balance",
        "support": "🆘 Soporte",
        "share": "📤 Compartir",
        "partner": "🤝 Panel de socio",
    },
    "pt": {
        "animate": "🪄 Animação de foto",
        "omni": "🧠 Cabeça falante (Omni)",
        "buy": "💫 Comprar Stars",
        "balance": "💰 Saldo",
        "support": "🆘 Suporte",
        "share": "📤 Compartilhar",
        "partner": "🤝 Painel de parceiro",
    },
}

def get_labels(lang: str) -> dict:
    return MENU_LABELS.get(lang, MENU_LABELS["en"])


# ---------- Главное меню ----------
def main_menu(uid: int) -> ReplyKeyboardMarkup:
    lang = get_lang(uid)
    L = get_labels(lang)

    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(L["animate"]), KeyboardButton(L["omni"])],
            [KeyboardButton(L["buy"]), KeyboardButton(L["balance"])],
            [KeyboardButton(L["support"]), KeyboardButton(L["share"])],
            [KeyboardButton(L["partner"])],
        ],
    )


# ---------- /menu ----------
@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    uid = message.from_user.id
    await message.answer("Меню оновлено ⬇️", reply_markup=main_menu(uid))


# ================================================================
#            Main menu text commands (UX routing)
# ================================================================

@dp.message(F.text)
async def handle_menu_buttons(message: Message):
    uid = message.from_user.id
    lang = get_lang(uid)
    L = get_labels(lang)
    text = message.text.strip()

    # -------------------
    # 1) ОЖИВИТЬ ФОТО
    # -------------------
    if text == L["animate"]:
        user_mode[uid] = MODE_PHOTO
        await message.answer(
            {
                "ua": "🪄 Надішли мені фото, і я оживлю його!",
                "en": "🪄 Send me a photo and I’ll animate it!",
                "es": "🪄 Envíame una foto y la animaré!",
                "pt": "🪄 Envie uma foto e eu vou animá-la!",
            }.get(lang),
            reply_markup=main_menu(uid),
        )
        return

    # -------------------
    # 2) OMNI HUMAN
    # -------------------
    if text == L["omni"]:
        user_mode[uid] = MODE_DUB
        await message.answer(
            {
                "ua": "🧠 Спочатку надішли фото з обличчям, потім аудіо.",
                "en": "🧠 Send a face photo first, then an audio message.",
                "es": "🧠 Envía una foto con rostro y luego el audio.",
                "pt": "🧠 Envie uma foto com rosto e depois um áudio.",
            }.get(lang),
            reply_markup=main_menu(uid),
        )
        return

    # -------------------
    # 3) ПОКУПКА STARS
    # -------------------
    if text == L["buy"]:
        await message.answer(
            "💫 Обери пакет Stars:",
            reply_markup=buy_stars_keyboard(uid)
        )
        return

    # -------------------
    # 4) БАЛАНС
    # -------------------
    if text == L["balance"]:
        bal = await get_stars(uid)
        await message.answer(
            f"💰 Ваш баланс: <b>{bal}</b>⭐",
            reply_markup=main_menu(uid)
        )
        return

    # -------------------
    # 5) ПОДДЕРЖКА
    # -------------------
    if text == L["support"]:
        await message.answer(
            {
                "ua": "🆘 Напишіть своє питання одним повідомленням.",
                "en": "🆘 Write your question in one message.",
                "es": "🆘 Escribe tu pregunta en un mensaje.",
                "pt": "🆘 Envie sua dúvida em uma mensagem.",
            }.get(lang)
        )
        awaiting_support[uid] = True
        return

    # -------------------
    # 6) ПОДЕЛИТЬСЯ
    # -------------------
    if text == L["share"]:
        link = f"https://t.me/{(await bot.me()).username}?start=ref_{uid}"
        await message.answer(
            {
                "ua": f"📤 Поділись магією!\nТвоє посилання:\n{link}",
                "en": f"📤 Share the magic!\Your link:\n{link}",
                "es": f"📤 ¡Comparte la magia!\nTu enlace:\n{link}",
                "pt": f"📤 Compartilhe a magia!\nSeu link:\n{link}",
            }.get(lang)
        )
        return

    # -------------------
    # 7) ПАРТНЕРСКИЙ КАБИНЕТ
    # -------------------
    if text == L["partner"]:
        from math import floor

        total_invited = sum(1 for k, v in ref_inviter.items() if v == uid)
        bal = await get_stars(uid)

        msg = (
            f"🤝 <b>Партнерський кабінет</b>\n\n"
            f"👥 Запрошено: {total_invited}\n"
            f"⭐ Баланс Stars: {bal}\n\n"
            f"Поділіться посиланням і заробляйте 20⭐ за кожного друга!"
        )

        await message.answer(msg, reply_markup=main_menu(uid))
        return

    # -------------------
    # 8) Если текст попадает в поддержку
    # -------------------
    if awaiting_support.get(uid):
        try:
            await bot.send_message(
                ADMIN_USER_ID,
                f"📩 Повідомлення у підтримку від @{message.from_user.username}:\n\n{text}"
            )
            await message.answer(
                {
                    "ua": "✅ Передано у підтримку!",
                    "en": "✅ Sent to support!",
                    "es": "✅ Enviado al soporte!",
                    "pt": "✅ Enviado ao suporte!",
                }.get(lang)
            )
        except:
            await message.answer("⚠️ Помилка надсилання в підтримку.")
        awaiting_support.pop(uid, None)
        return
# ================================================================
#                    BLOCK 8 — /start + LANGUAGE + REF
# ================================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id

    # -------- 1) Регистрируем юзера (DB + память) ----------
    await ensure_user(uid)
    register_user(uid)

    # -------- 2) Читаем payload (рефералка) ----------
    payload = message.text.split(" ", 1)
    payload = payload[1] if len(payload) > 1 else ""

    if payload.startswith("ref_"):
        try:
            inviter_id = int(payload.replace("ref_", ""))
            if inviter_id != uid:
                await register_referral(uid, inviter_id)
        except:
            pass

    # -------- 3) Если язык не выбран — показываем выбор ----------
    if uid not in user_lang:
        try:
            await message.answer_video(
                INTRO_VIDEO_FILE_ID,
                caption="🧙‍♂️ Magl’sBot welcomes you!\n\n✨ Choose your language:",
                supports_streaming=True,
                reply_markup=lang_choice_keyboard(),
            )
        except:
            await message.answer(
                "🧙‍♂️ Magl’sBot welcomes you!\n\n✨ Choose your language:",
                reply_markup=lang_choice_keyboard()
            )
        return

    # -------- 4) Язык уже выбран → приветственный экран ----------
    lang = get_lang(uid)

    try:
        await message.answer_video(
            INTRO_VIDEO_FILE_ID,
            supports_streaming=True
        )
    except:
        pass

    welcome_text = {
        "ua": "🪄 Надішли мені фото — я оживлю його!",
        "en": "🪄 Send me a photo and I will animate it!",
        "es": "🪄 Envíame una foto y la animaré!",
        "pt": "🪄 Envie uma foto e eu vou animá-la!",
    }.get(lang, "Send me a photo!")

    await message.answer(welcome_text, reply_markup=main_menu(uid))
# ---------- Выбор языка ----------
@dp.callback_query(F.data.startswith("lang:"))
async def set_lang(query: CallbackQuery):
    uid = query.from_user.id
    _, code = query.data.split(":")

    if code not in LOCALES:
        await query.answer("Language not available", show_alert=True)
        return

    user_lang[uid] = code
    register_user(uid)

    # режим по умолчанию
    user_mode[uid] = MODE_PHOTO

    # подтверждение
    await query.message.edit_caption(tr(uid, "lang_set")) if query.message.caption \
        else await query.message.edit_text(tr(uid, "lang_set"))

    # показать выбор режима
    await query.message.answer(
        mode_choice_text(code),
        reply_markup=mode_choice_keyboard(code)
    )

    await query.answer()
# ---------- Выбор режима Photo ----------
@dp.callback_query(F.data == "mode:photo")
async def set_mode_photo(query: CallbackQuery):
    uid = query.from_user.id
    user_mode[uid] = MODE_PHOTO
    lang = get_lang(uid)

    text = {
        "ua": "✨ Режим: оживлення фото.\nНадішли мені фото 🪄",
        "en": "✨ Mode: photo animation.\nSend me a photo 🪄",
        "es": "✨ Modo: animar foto.\nEnvíame una foto 🪄",
        "pt": "✨ Modo: animação de foto.\nEnvie uma foto 🪄",
    }.get(lang)

    await query.message.answer(text, reply_markup=main_menu(uid))
    await query.answer()


# ---------- Выбор режима Omni ----------
@dp.callback_query(F.data == "mode:dub")
async def set_mode_dub(query: CallbackQuery):
    uid = query.from_user.id
    user_mode[uid] = MODE_DUB
    lang = get_lang(uid)

    text = {
        "ua": "🧠 Режим Omni.\n1) Надішли фото\n2) Потім аудіо",
        "en": "🧠 Omni mode.\n1) Send a photo\n2) Then send audio",
        "es": "🧠 Modo Omni.\n1) Envía foto\n2) Luego audio",
        "pt": "🧠 Modo Omni.\n1) Envie foto\n2) Depois áudio",
    }.get(lang)

    await query.message.answer(text, reply_markup=main_menu(uid))
    await query.answer()
# ================================================================
#                     BLOCK 9 — PHOTO HANDLER
# ================================================================

@dp.message(F.photo)
async def handle_photo(message: Message):
    uid = message.from_user.id

    await ensure_user(uid)
    register_user(uid)

    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)

    mode = get_mode(uid)

    # ------------------------------------------------------------
    # 1) Если Omni режим — просто сохраняем фото и ждём аудио
    # ------------------------------------------------------------
    if mode == MODE_DUB:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        image_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        omni_pending_photo[uid] = image_url

        await message.answer({
            "ua": "✅ Фото збережено! Тепер надішли аудіо.",
            "en": "✅ Photo saved! Now send audio.",
            "es": "✅ Foto guardada. Ahora envía el audio.",
            "pt": "✅ Foto salva! Agora envie o áudio."
        }.get(get_lang(uid)))

        return

    # ------------------------------------------------------------
    # 2) Проверяем бесплатную анимацию / баланс Stars
    # ------------------------------------------------------------
    lang = get_lang(uid)
    is_admin = uid == ADMIN_USER_ID
    free_used = await has_used_free(uid)

    if not (TEST_MODE and is_admin):  # админ в тесте — без ограничений
        if free_used:
            credits = await get_user_credits(uid)
            if credits <= 0:
                await message.answer(
                    tr(uid, "free_used"),
                    reply_markup=buy_cta_keyboard(uid)
                )
                return

    # ------------------------------------------------------------
    # 3) Сохраняем фото в pending_photo
    # ------------------------------------------------------------
    photo = message.photo[-1]
    width = photo.width
    height = photo.height
    file_size = getattr(photo, "file_size", 0) or 0

    area = width * height
    is_small_res = area < 400_000 or max(width, height) < 700
    is_small_size = file_size and file_size < 200_000

    looks_old = is_small_res or is_small_size

    pending_photo[uid] = {
        "file_id": photo.file_id,
        "caption": (message.caption or "").strip(),
        "is_old": looks_old
    }
    pending_choice.pop(uid, None)

    # ------------------------------------------------------------
    # 4) Если фото похоже на старое → авто-пресет Blink&Glow (#4)
    # ------------------------------------------------------------
    if looks_old:
        idx = 4
        pending_choice[uid] = {"type": "preset", "idx": idx}

        titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])
        title = titles[idx]

        desc_map = LOCALES.get(lang).get("preset_desc", {})
        desc = desc_map.get(str(idx+1), "")

        confirm = {
            "ua": "✨ Це фото виглядає як старе. Використати цей пресет?",
            "en": "✨ This photo looks old. Use this preset?",
            "es": "✨ Esta foto parece antigua. ¿Usar este preset?",
            "pt": "✨ Esta foto parece antiga. Usar este preset?"
        }.get(lang)

        await message.answer(
            f"🎨 {title}\n\n{desc}\n\n{confirm}",
            reply_markup=confirm_preset_keyboard(uid)
        )
        return

    # ------------------------------------------------------------
    # 5) Обычный выбор пресета
    # ------------------------------------------------------------
    await message.answer(
        tr(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption=bool(pending_photo[uid]["caption"]))
    )
# ================================================================
#                     BLOCK 9.2 — PRESET SELECTION
# ================================================================

@dp.callback_query(F.data.startswith("preset:"))
async def handle_preset(query: CallbackQuery):
    uid = query.from_user.id
    lang = get_lang(uid)

    if uid not in pending_photo:
        await query.answer()
        return

    data = query.data.split(":", 1)[1]

    # CANCEL
    if data == "cancel":
        pending_photo.pop(uid, None)
        pending_choice.pop(uid, None)
        await query.message.edit_text(tr(uid, "btn_cancel"))
        await query.answer()
        return

    # USE CAPTION
    if data == "usecap":
        pending_choice[uid] = {"type": "caption", "idx": None}
        caption = pending_photo[uid]["caption"]
        confirm = {
            "ua": "Запустити анімацію з цим описом?",
            "en": "Start with this caption?",
            "es": "¿Usar este texto?",
            "pt": "Iniciar com esta descrição?"
        }.get(lang)
        await query.message.edit_text(
            f"📝 {caption}\n\n{confirm}",
            reply_markup=confirm_preset_keyboard(uid)
        )
        await query.answer()
        return

    # RANDOM PRESET
    if data == "random":
        idx = random.randint(0, len(PRESET_PROMPTS_BASE) - 1)
    else:
        idx = int(data) - 1

    pending_choice[uid] = {"type": "preset", "idx": idx}

    titles = PRESET_TITLES.get(lang, PRESET_TITLES["en"])
    title = titles[idx]

    desc_map = LOCALES.get(lang).get("preset_desc", {})
    desc = desc_map.get(str(idx+1), "")

    confirm = {
        "ua": "Запустити анімацію з цим пресетом?",
        "en": "Start animation with this preset?",
        "es": "¿Iniciar con este preset?",
        "pt": "Iniciar com este preset?"
    }.get(lang)

    await query.message.edit_text(
        f"🎨 {title}\n\n{desc}\n\n{confirm}",
        reply_markup=confirm_preset_keyboard(uid)
    )
    await query.answer()
# ================================================================
#                     BLOCK 9.3 — BACK BUTTON
# ================================================================

@dp.callback_query(F.data == "confirm:back")
async def preset_back(query: CallbackQuery):
    uid = query.from_user.id

    if uid not in pending_photo:
        await query.answer()
        return

    has_caption = bool(pending_photo[uid]["caption"])

    await query.message.edit_text(
        tr(uid, "choose_preset"),
        reply_markup=preset_keyboard(uid, has_caption)
    )
    await query.answer()
# ================================================================
#                 BLOCK 9.4 — CONFIRM & GENERATE
# ================================================================

@dp.callback_query(F.data == "confirm:ok")
async def start_animation(query: CallbackQuery):
    uid = query.from_user.id
    lang = get_lang(uid)

    if uid not in pending_photo or uid not in pending_choice:
        await query.answer()
        return

    info = pending_photo.pop(uid)
    choice = pending_choice.pop(uid)

    # -------- Get prompt --------
    if choice["type"] == "caption":
        prompt = info["caption"] or "natural smile, cinematic portrait"
    else:
        idx = choice["idx"]
        prompt = get_preset_prompt(lang, idx)

    await query.message.edit_text(tr(uid, "status_work"))

    # -------- Download photo --------
    file_info = await bot.get_file(info["file_id"])
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

    # -------- Send to Replicate --------
    result = await animate_photo_via_replicate(file_url, prompt)

    if not result["ok"]:
        await query.message.edit_text("⚠️ Server overloaded, try again later.")
        return

    out_url = result["url"]

    # -------- Download result --------
    tmp = f"/tmp/anim_{uid}.mp4"
    await download_file(out_url, tmp)

    wm = {
        "ua": "\n\n🔖 Зроблено в Magl’sBot",
        "en": "\n\n🔖 Made with Magl’sBot",
        "es": "\n\n🔖 Hecho en Magl’sBot",
        "pt": "\n\n🔖 Feito no Magl’sBot"
    }.get(lang)

    # -------- Send video --------
    await bot.send_video(
        uid,
        video=FSInputFile(tmp),
        caption=tr(uid, "done") + wm,
        reply_markup=buy_cta_keyboard(uid)
    )

    # -------- Referral promo --------
    await bot.send_message(uid, referral_info_text(lang))

    os.remove(tmp)

    # -------- Списание Stars --------
    is_admin = uid == ADMIN_USER_ID

    if not (TEST_MODE and is_admin):
        free_used = await has_used_free(uid)
        if not free_used:
            await mark_free_used(uid)
        else:
            ok, _ = await consume_user_credit(uid, 1)
            if not ok:
                logger.warning("Credit mismatch for user %s", uid)
# ======================================================================
#                     BLOCK 10 — OMNI AUDIO HANDLER
# ======================================================================

@dp.message(F.audio | F.voice)
async def handle_omni_audio(message: Message):
    uid = message.from_user.id
    register_user(uid)
    awaiting_support.pop(uid, None)
    awaiting_video_order.pop(uid, None)

    lang = get_lang(uid)

    # ------------------------------------------------------------
    # 1) Проверяем: включён ли у юзера режим Omni
    # ------------------------------------------------------------
    if get_mode(uid) != MODE_DUB:
        return

    # ------------------------------------------------------------
    # 2) Проверяем: есть ли сохранённое фото
    # ------------------------------------------------------------
    if uid not in omni_pending_photo:
        await message.answer({
            "ua": "Спочатку надішли фото 🙂",
            "en": "First send a photo 🙂",
            "es": "Primero envía una foto 🙂",
            "pt": "Primeiro envie uma foto 🙂"
        }.get(lang))
        return

    image_url = omni_pending_photo[uid]

    # ------------------------------------------------------------
    # 3) Проверяем баланс Stars (если не админ в TEST_MODE)
    # ------------------------------------------------------------
    is_admin = (uid == ADMIN_USER_ID)

    if not (TEST_MODE and is_admin):
        credits = await get_user_credits(uid)
        if credits < OMNI_PRICE:
            await message.answer(
                {
                    "ua": f"🧠 Відео Omni коштує <b>{OMNI_PRICE} Stars</b>.\n"
                          f"У тебе {credits} ⭐.\nПоповни баланс:",
                    "en": f"🧠 Omni video costs <b>{OMNI_PRICE} Stars</b>.\n"
                          f"You have {credits} ⭐.\nTop up:",
                    "es": f"🧠 El vídeo Omni cuesta <b>{OMNI_PRICE} Stars</b>.\n"
                          f"Tienes {credits} ⭐.\nRecarga:",
                    "pt": f"🧠 O vídeo Omni custa <b>{OMNI_PRICE} Stars</b>.\n"
                          f"Você tem {credits} ⭐.\nRecarregue:"
                }.get(lang),
                reply_markup=buy_cta_keyboard(uid)
            )
            return

    # ------------------------------------------------------------
    # 4) Извлекаем аудио URL
    # ------------------------------------------------------------
    audio_file_id = message.audio.file_id if message.audio else message.voice.file_id
    file_info_a = await bot.get_file(audio_file_id)
    audio_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info_a.file_path}"

    # ------------------------------------------------------------
    # 5) Отправляем статус «создаю»
    # ------------------------------------------------------------
    status_msg = await message.answer(
        {
            "ua": "🎧 Створюю відео… (Omni може займати 5–15 хвилин)",
            "en": "🎧 Creating video… (Omni may take 5–15 minutes)",
            "es": "🎧 Creando vídeo… (Omni puede tardar 5–15 minutos)",
            "pt": "🎧 Criando vídeo… (Omni pode levar 5–15 minutos)"
        }.get(lang)
    )

    # ------------------------------------------------------------
    # 6) Запускаем Replicate Omni
    # ------------------------------------------------------------
    try:
        result = await omni_talking_head(
            image_url=image_url,
            audio_url=audio_url
        )
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Omni exception: {e}")
        return

    # ------------------------------------------------------------
    # 7) Проверка результата
    # ------------------------------------------------------------
    if not result.get("ok"):
        await status_msg.edit_text(
            f"⚠️ Omni error: {result.get('error', 'unknown')}"
        )
        return

    out_url = result["url"]

    # ------------------------------------------------------------
    # 8) Скачиваем результат
    # ------------------------------------------------------------
    tmp_path = os.path.join(DOWNLOAD_TMP_DIR, f"omni_{uid}.mp4")

    try:
        await download_file(out_url, tmp_path)
    except Exception as e:
        await status_msg.edit_text("⚠️ Error downloading result.")
        return

    # ------------------------------------------------------------
    # 9) Удаляем сообщение «создаю»
    # ------------------------------------------------------------
    try:
        await status_msg.delete()
    except:
        pass

    # ------------------------------------------------------------
    # 10) Делаем красивый watermark
    # ------------------------------------------------------------
    wm = {
        "ua": "\n\n🔖 Зроблено в Magl’sBot (OmniHuman)",
        "en": "\n\n🔖 Made with Magl’sBot (OmniHuman)",
        "es": "\n\n🔖 Hecho con Magl’sBot (OmniHuman)",
        "pt": "\n\n🔖 Feito com Magl’sBot (OmniHuman)"
    }.get(lang)

    # ------------------------------------------------------------
    # 11) Отправляем финальное видео пользователю
    # ------------------------------------------------------------
    await bot.send_video(
        chat_id=uid,
        video=FSInputFile(tmp_path),
        caption=tr(uid, "done") + wm,
        reply_markup=buy_cta_keyboard(uid)
    )

    # ------------------------------------------------------------
    # 12) Списание Stars (если не адм. в TEST_MODE)
    # ------------------------------------------------------------
    if not (TEST_MODE and is_admin):
        ok, new_balance = await consume_user_credit(uid, OMNI_PRICE)
        if not ok:
            logger.warning(f"User {uid} did not have enough credits for Omni at deduction stage.")

    # ------------------------------------------------------------
    # 13) Чистим временный файл
    # ------------------------------------------------------------
    try:
        os.remove(tmp_path)
    except:
        pass

    # ------------------------------------------------------------
    # 14) Чистим сохранённое фото для Omni
    # ------------------------------------------------------------
    omni_pending_photo.pop(uid, None)

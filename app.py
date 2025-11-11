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

WELCOME = (
    "<b>Hi!</b> Send me a <b>photo</b> and an optional <b>prompt</b> in the caption.\n"
    "I will generate a short video from your image.\n\n"
    "Tip: front-facing portraits with good lighting work best."
)

PRICING = (
    "<b>Pricing:</b>\n"
    "• 1 free render per user\n"
    "• Packs coming soon (TON / USDT / Telegram Stars)"
)

@dp.message(CommandStart())
async def on_start(message: Message):
    if ALLOWED_CHAT_IDS and message.chat.id not in ALLOWED_CHAT_IDS:
        await message.answer("This bot is invite-only for now.")
        return
    await message.answer(WELCOME)

@dp.message(Command("pricing"))
async def on_pricing(message: Message):
    await message.answer(PRICING)

@dp.message(Command("admin"))
async def on_admin(message: Message):
    if ADMIN_USER_ID and message.from_user and message.from_user.id == ADMIN_USER_ID:
        await message.answer(f"Users: {limiter.users_count()} | Total renders: {limiter.total_count()}")
    else:
        await message.answer("No permission.")

@dp.message(F.photo)
async def on_photo(message: Message):
    user_id = message.from_user.id if message.from_user else 0

    if not limiter.can_use(user_id):
        await message.answer("You used your free render. See /pricing")
        return

    photo = message.photo[-1]
    try:
        status = await message.answer("Working on your video... ~20–60s")

        # Telegram file URL
        file_info = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"

        # Prompt from caption (optional)
        user_prompt = (message.caption or "").strip()
        if not user_prompt:
            user_prompt = "natural smile, subtle head motion, cinematic lighting"

        # Run WAN i2v
        result = await animate_photo_via_replicate(source_image_url=file_url, prompt=user_prompt)

        if not result.get("ok"):
            code = result.get("code", "unknown")
            if code == "replicate_402":
                await status.edit_text(
                    "Insufficient credit on Replicate. Go to replicate.com → Account → Billing → Add credit.\n"
                    "Try again in 1–2 minutes after payment."
                )
                return
            if code in ("replicate_auth", "config"):
                await status.edit_text("AI provider auth/config error. Admin notified.")
                return
            if code == "replicate_422_fields":
                fields = result.get("fields") or []
                await status.edit_text(
                    "The selected model requires different inputs: " + ", ".join(fields) + ".\n"
                    "Please ensure you use an image-to-video model (WAN i2v)."
                )
                return
            await status.edit_text("Failed to generate. Please try a different image.")
            return

        video_url = result["url"]

        # Download and send
        tmp_video_path = os.path.join(DOWNLOAD_TMP_DIR, f"anim_{photo.file_unique_id}.mp4")
        await download_file(video_url, tmp_video_path)

        await bot.send_video(chat_id=message.chat.id, video=FSInputFile(tmp_video_path),
                             caption="Done! If you like it — see /pricing")

        limiter.mark_used(user_id)

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

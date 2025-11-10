import os
import time
import asyncio
import logging
from typing import Optional

import requests

logger = logging.getLogger("processing")

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")

REPLICATE_API_URL = "https://api.replicate.com/v1/predictions"
HEADERS = {
    "Authorization": f"Token {REPLICATE_API_TOKEN}" if REPLICATE_API_TOKEN else "",
    "Content-Type": "application/json"
}


async def animate_photo_via_replicate(source_image_url: str) -> Optional[str]:
    """Отправляет фото в Replicate и возвращает ссылку на видео (mp4/gif)."""
    if not REPLICATE_API_TOKEN or not REPLICATE_MODEL:
        logger.error("Replicate credentials/model are not set")
        return None

    # Параметры модели — можно подстраивать под выбранную модель на replicate.com
    payload = {
        "version": REPLICATE_MODEL,
        "input": {
            "image": source_image_url,
        }
    }

    # 1. Отправляем запрос на создание задачи
    r = requests.post(REPLICATE_API_URL, json=payload, headers=HEADERS, timeout=60)
    if r.status_code != 201:
        logger.error("Replicate create failed: %s %s", r.status_code, r.text)
        return None

    pred = r.json()
    get_url = pred.get("urls", {}).get("get")

    # 2. Ждём завершения задачи
    for _ in range(120):  # максимум 2 минуты ожидания
        time.sleep(1)
        rr = requests.get(get_url, headers=HEADERS, timeout=30)
        data = rr.json()
        status = data.get("status")
        if status in ("succeeded", "failed", "canceled"):
            if status == "succeeded":
                output = data.get("output")
                if isinstance(output, list) and output:
                    # возвращаем первую ссылку на mp4/gif
                    for url in output:
                        if isinstance(url, str) and (url.endswith(".mp4") or url.endswith(".gif")):
                            return url
                    return output[0] if isinstance(output[0], str) else None
                else:
                    logger.error("Unexpected output format: %s", output)
                return None
            else:
                logger.error("Replicate status: %s", status)
                return None
    return None


async def download_file(url: str, dst_path: str):
    """Скачивает файл по ссылке, не блокируя основной поток."""
    loop = asyncio.get_running_loop()

    def _download():
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dst_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

    await loop.run_in_executor(None, _download)

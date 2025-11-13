import os
import time
import asyncio
import logging
from typing import Optional, Dict, Any

import requests
import aiohttp  # используем для неблокирующих запросов к Replicate

logger = logging.getLogger("processing")

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")

REPLICATE_API_URL = "https://api.replicate.com/v1/predictions"


async def animate_photo_via_replicate(
    source_image_url: str,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Запрашивает анимацию у Replicate и возвращает dict:
    { "ok": True, "url": "https://..." } или { "ok": False, "error": "..." }

    ВАЖНО: сейчас эта функция неблокирующая:
    - использует aiohttp
    - ждёт статусы через asyncio.sleep()
    """

    if not REPLICATE_API_TOKEN or not REPLICATE_MODEL:
        logger.error("Replicate credentials/model are not set")
        return {"ok": False, "error": "no_replicate_credentials"}

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # Общий формат для WAN / i2v моделей:
    # "image" + (опционально) "prompt"
    input_payload: Dict[str, Any] = {
        "image": source_image_url,
    }
    if prompt:
        # если промпт есть — добавляем
        input_payload["prompt"] = prompt

    payload = {
        "version": REPLICATE_MODEL,
        "input": input_payload,
    }

    timeout = aiohttp.ClientTimeout(total=600)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1) создаём prediction
        try:
            async with session.post(
                REPLICATE_API_URL,
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status != 201:
                    text = await resp.text()
                    logger.error(
                        "Replicate create failed: %s %s", resp.status, text
                    )
                    return {
                        "ok": False,
                        "error": "create_failed",
                        "status": resp.status,
                        "body": text,
                    }
                pred = await resp.json()
        except Exception as e:
            logger.exception("Replicate create exception: %s", e)
            return {"ok": False, "error": "create_exception"}

        get_url = pred.get("urls", {}).get("get")
        if not get_url:
            logger.error("Replicate: no get URL in response")
            return {"ok": False, "error": "no_get_url"}

        # 2) Ожидаем завершения (polling), НЕ блокируя event loop
        for _ in range(120):  # до ~2 минут
            await asyncio.sleep(1)
            try:
                async with session.get(get_url, headers=headers) as resp2:
                    data = await resp2.json()
            except Exception as e:
                logger.exception("Replicate poll exception: %s", e)
                continue

            status = data.get("status")
            if status in ("succeeded", "failed", "canceled"):
                if status == "succeeded":
                    out = data.get("output")
                    url = None

                    # большинство моделей возвращают список ссылок
                    if isinstance(out, list) and out:
                        for u in out:
                            if isinstance(u, str) and (
                                u.endswith(".mp4")
                                or u.endswith(".gif")
                                or "mp4" in u
                                or "gif" in u
                            ):
                                url = u
                                break
                        if url is None and isinstance(out[0], str):
                            url = out[0]
                    elif isinstance(out, str):
                        url = out

                    if url:
                        return {"ok": True, "url": url}
                    else:
                        logger.error("Replicate succeeded but no output URL")
                        return {"ok": False, "error": "no_output_url"}
                else:
                    logger.error("Replicate status: %s", status)
                    return {"ok": False, "error": status}

        logger.error("Replicate timeout")
        return {"ok": False, "error": "timeout"}


async def download_file(url: str, dst_path: str):
    """
    Загрузка файла по URL в отдельном потоке, чтобы не блокировать event loop.
    Тут можно оставить requests + run_in_executor — это нормально.
    """
    loop = asyncio.get_running_loop()

    def _download():
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            with open(dst_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

    await loop.run_in_executor(None, _download)

import os
import aiohttp
import asyncio
import logging
import time
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger("processing")

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")              # модель для анимации фото (как было)
REPLICATE_OMNI_MODEL = os.getenv("REPLICATE_OMNI_MODEL")    # модель bytedance/omni-human-1.5 (говорящая голова)

REPLICATE_API_URL = "https://api.replicate.com/v1/predictions"


# ---------- АНИМАЦИЯ ФОТО (как было) ----------

async def animate_photo_via_replicate(
    source_image_url: str,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    if not REPLICATE_API_TOKEN or not REPLICATE_MODEL:
        logger.error("Replicate credentials/model are not set for animate_photo_via_replicate")
        return {"ok": False, "error": "no_replicate_credentials"}

    raw_model = REPLICATE_MODEL.strip()
    version = raw_model.split(":")[-1] if ":" in raw_model else raw_model

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    input_payload: Dict[str, Any] = {"image": source_image_url}
    if prompt:
        input_payload["prompt"] = prompt

    payload = {"version": version, "input": input_payload}

    PHOTO_TIMEOUT = 600   # 10 минут
    POLL_INTERVAL = 3     # раз в 3 сек

    timeout = aiohttp.ClientTimeout(total=PHOTO_TIMEOUT + 30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1) create prediction
        try:
            async with session.post(REPLICATE_API_URL, headers=headers, json=payload) as resp:
                if resp.status != 201:
                    text = await resp.text()
                    logger.error("Replicate create (photo) failed: %s %s", resp.status, text)
                    return {"ok": False, "error": "create_failed", "status": resp.status, "body": text}
                pred = await resp.json()
        except Exception as e:
            logger.exception("Replicate create (photo) exception: %s", e)
            return {"ok": False, "error": "create_exception"}

        get_url = pred.get("urls", {}).get("get")
        if not get_url:
            logger.error("Replicate (photo): no get URL in response")
            return {"ok": False, "error": "no_get_url"}

        # 2) polling up to 10 minutes
        deadline = time.monotonic() + PHOTO_TIMEOUT

        while time.monotonic() <= deadline:
            await asyncio.sleep(POLL_INTERVAL)

            try:
                async with session.get(get_url, headers=headers) as resp2:
                    data = await resp2.json()
            except Exception as e:
                logger.exception("Replicate poll (photo) exception: %s", e)
                continue

            status = data.get("status")
            if status in ("succeeded", "failed", "canceled"):
                if status == "succeeded":
                    out = data.get("output")
                    url = None

                    if isinstance(out, list) and out:
                        for u in out:
                            if isinstance(u, str) and (u.endswith(".mp4") or u.endswith(".gif") or "mp4" in u or "gif" in u):
                                url = u
                                break
                        if url is None and isinstance(out[0], str):
                            url = out[0]
                    elif isinstance(out, str):
                        url = out

                    return {"ok": True, "url": url} if url else {"ok": False, "error": "no_output_url"}

                err_msg = data.get("error") or data.get("logs") or status
                logger.error("Replicate (photo) status=%s, error=%s", status, err_msg)
                return {"ok": False, "error": err_msg}

        logger.error("Replicate (photo) timeout after %ss", PHOTO_TIMEOUT)
        return {"ok": False, "error": "timeout"}


# ---------- ГОВОРЯЩАЯ ГОЛОВА ЧЕРЕЗ OMNI-HUMAN (фото + аудио) ----------

OMNI_TIMEOUT = 900  # 15 минут

async def _wait_for_omni(session, prediction_url, headers):
    """
    Ждём завершения модели (polling), максимум 15 минут.
    """
    for _ in range(OMNI_TIMEOUT // 3):   # ~300 итераций по 3 секунды = 900 сек
        await asyncio.sleep(3)

        try:
            async with session.get(prediction_url, headers=headers) as resp:
                data = await resp.json()
        except Exception as e:
            logger.exception("Replicate omni poll exception: %s", e)
            continue

        status = data.get("status")

        if status in ("succeeded", "failed", "canceled"):
            if status == "succeeded":
                out = data.get("output")
                url = None

                if isinstance(out, list) and out:
                    for u in out:
                        if isinstance(u, str) and "mp4" in u:
                            url = u
                            break
                    if url is None and isinstance(out[0], str):
                        url = out[0]
                elif isinstance(out, str):
                    url = out

                if url:
                    return {"ok": True, "url": url}
                else:
                    return {"ok": False, "error": "no_output_url"}

            else:
                err_msg = data.get("error") or data.get("logs") or status
                return {"ok": False, "error": err_msg}

    logger.error("Replicate omni timeout")
    return {"ok": False, "error": "timeout"}


async def omni_talking_head(image_url: str, audio_url: str) -> Dict[str, Any]:
    """
    Генерация говорящей головы (omni-human 1.5) на Replicate.
    Ждёт до 15 минут.
    """
    if not REPLICATE_API_TOKEN or not REPLICATE_OMNI_MODEL:
        logger.error("Replicate omni-human credentials/model are not set")
        return {"ok": False, "error": "no_omni_model"}

    raw = REPLICATE_OMNI_MODEL.strip()
    if ":" in raw:
        version = raw.split(":")[-1]
    else:
        version = raw

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "version": version,
        "input": {
            "image": image_url,
            "audio": audio_url,
        },
    }

    timeout = aiohttp.ClientTimeout(total=OMNI_TIMEOUT)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1) создаём prediction
            async with session.post(
                REPLICATE_API_URL,
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status != 201:
                    text = await resp.text()
                    logger.error("Omni create failed: %s", text)
                    return {"ok": False, "error": "create_failed"}

                pred = await resp.json()

            prediction_url = pred["urls"]["get"]

            # 2) poll до 15 минут
            return await _wait_for_omni(session, prediction_url, headers)

    except asyncio.TimeoutError:
        logger.error("Replicate omni timeout (aiohttp)")
        return {"ok": False, "error": "timeout"}

    except Exception as e:
        logger.exception("Omni exception: %s", e)
        return {"ok": False, "error": str(e)}


# ---------- СКАЧИВАНИЕ ФАЙЛОВ ----------

async def download_file(url: str, dst_path: str):
    """
    Загрузка файла по URL в отдельном потоке, чтобы не блокировать event loop.
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

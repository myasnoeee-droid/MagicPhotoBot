import os
import asyncio
import logging
from typing import Optional, Dict, Any

import requests
import aiohttp  # используем для неблокирующих запросов к Replicate

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
    """
    Анимация фото через Replicate.
    Возвращает dict:
      { "ok": True, "url": "https://..." }
      или
      { "ok": False, "error": "..." }
    """

    if not REPLICATE_API_TOKEN or not REPLICATE_MODEL:
        logger.error("Replicate credentials/model are not set for animate_photo_via_replicate")
        return {"ok": False, "error": "no_replicate_credentials"}

    # REPLICATE_MODEL может быть:
    # - "owner/model:HASH"
    # - или просто "HASH"
    raw_model = REPLICATE_MODEL.strip()
    if ":" in raw_model:
        version = raw_model.split(":")[-1]
    else:
        version = raw_model

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    input_payload: Dict[str, Any] = {"image": source_image_url}
    if prompt:
        input_payload["prompt"] = prompt

    payload = {
        "version": version,
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
                    logger.error("Replicate create (photo) failed: %s %s", resp.status, text)
                    return {
                        "ok": False,
                        "error": "create_failed",
                        "status": resp.status,
                        "body": text,
                    }
                pred = await resp.json()
        except Exception as e:
            logger.exception("Replicate create (photo) exception: %s", e)
            return {"ok": False, "error": "create_exception"}

        get_url = pred.get("urls", {}).get("get")
        if not get_url:
            logger.error("Replicate (photo): no get URL in response")
            return {"ok": False, "error": "no_get_url"}

        # 2) Ожидаем завершения (polling)
        for _ in range(120):  # до ~2 минут
            await asyncio.sleep(1)
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
                        logger.error("Replicate (photo) succeeded but no output URL")
                        return {"ok": False, "error": "no_output_url"}

                else:
                    err_msg = data.get("error") or data.get("logs") or status
                    logger.error("Replicate (photo) status=%s, error=%s", status, err_msg)
                    return {"ok": False, "error": err_msg}

        logger.error("Replicate (photo) timeout")
        return {"ok": False, "error": "timeout"}


# ---------- ГОВОРЯЩАЯ ГОЛОВА ЧЕРЕЗ OMNI-HUMAN (фото + аудио) ----------

async def omni_talking_head(
    image_url: str,
    audio_url: str,
) -> Dict[str, Any]:
    """
    Генерация говорящей головы по фото и аудио через bytedance/omni-human-1.5 на Replicate.

    input:
      image: URL фото с лицом
      audio: URL аудиофайла (voice/audio/file из Telegram)
    """

    if not REPLICATE_API_TOKEN or not REPLICATE_OMNI_MODEL:
        logger.error("Replicate omni-human credentials/model are not set")
        return {"ok": False, "error": "no_omni_model"}

    # REPLICATE_OMNI_MODEL может быть:
    # - "bytedance/omni-human-1.5:HASH"
    # - или просто "HASH"
    raw = REPLICATE_OMNI_MODEL.strip()
    if ":" in raw:
        version = raw.split(":")[-1]
    else:
        version = raw

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # Входы omni-human для talking head:
    # image — фото
    # audio — аудио
    payload: Dict[str, Any] = {
        "version": version,
        "input": {
            "image": image_url,
            "audio": audio_url,
        },
    }

    timeout = aiohttp.ClientTimeout(total=900)  # чуть больше, модель может думать дольше

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
                    logger.error("Replicate omni create failed: %s %s", resp.status, text)
                    return {
                        "ok": False,
                        "error": "create_failed",
                        "status": resp.status,
                        "body": text,
                    }
                pred = await resp.json()
        except Exception as e:
            logger.exception("Replicate omni create exception: %s", e)
            return {"ok": False, "error": "create_exception"}

        get_url = pred.get("urls", {}).get("get")
        if not get_url:
            logger.error("Replicate omni: no get URL in response")
            return {"ok": False, "error": "no_get_url"}

        # 2) Ожидаем завершения (polling)
        for _ in range(240):  # до ~4 минут
            await asyncio.sleep(1)
            try:
                async with session.get(get_url, headers=headers) as resp2:
                    data = await resp2.json()
            except Exception as e:
                logger.exception("Replicate omni poll exception: %s", e)
                continue

            status = data.get("status")
            if status in ("succeeded", "failed", "canceled"):
                if status == "succeeded":
                    out = data.get("output")
                    url = None

                    if isinstance(out, list) and out:
                        # omni обычно возвращает список URL'ов
                        for u in out:
                            if isinstance(u, str) and ("mp4" in u or u.endswith(".mp4")):
                                url = u
                                break
                        if url is None and isinstance(out[0], str):
                            url = out[0]
                    elif isinstance(out, str):
                        url = out

                    if url:
                        return {"ok": True, "url": url}
                    else:
                        logger.error("Replicate omni succeeded but no output URL")
                        return {"ok": False, "error": "no_output_url"}

                else:
                    err_msg = data.get("error") or data.get("logs") or status
                    logger.error("Replicate omni status=%s, error=%s", status, err_msg)
                    return {"ok": False, "error": err_msg}

        logger.error("Replicate omni timeout")
        return {"ok": False, "error": "timeout"}


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

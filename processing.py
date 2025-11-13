import os
import asyncio
import logging
from typing import Optional, Dict, Any, List

import aiohttp

logger = logging.getLogger("processing")

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")

# Тонкая настройка через ENV (безопасные дефолты)
ANIMATE_CONCURRENCY = int(os.getenv("ANIMATE_CONCURRENCY", "4"))   # одновременных задач к Replicate
ANIMATE_TIMEOUT     = int(os.getenv("ANIMATE_TIMEOUT", "180"))      # общий таймаут одной генерации, сек
ANIMATE_POLL        = float(os.getenv("ANIMATE_POLL", "0.8"))       # период опроса статуса, сек

# Глобальный семафор — чтобы не словить rate limit провайдера и не забить CPU
_SEM = asyncio.Semaphore(ANIMATE_CONCURRENCY)

_REPLICATE_API_URL = "https://api.replicate.com/v1/predictions"
_HEADERS = {
    "Authorization": f"Token {REPLICATE_API_TOKEN}" if REPLICATE_API_TOKEN else "",
    "Content-Type": "application/json",
}

def _result_error(code: str, **extra) -> Dict[str, Any]:
    out = {"ok": False, "code": code}
    out.update(extra)
    return out

async def animate_photo_via_replicate(
    source_image_url: str,
    prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Неблокирующая анимация через Replicate.
    Возвращает:
      { "ok": True, "url": "https://..." } либо { "ok": False, "code": "...", ... }
    """
    if not REPLICATE_API_TOKEN or not REPLICATE_MODEL:
        logger.error("Replicate credentials/model are not set")
        return _result_error("config")

    # Минимальный кросс-модельный набор входов:
    # image / prompt — понимают WAN i2v и большинство i2v-моделей.
    payload = {
        "version": REPLICATE_MODEL,
        "input": {
            "image": source_image_url,
        }
    }
    if prompt:
        payload["input"]["prompt"] = prompt

    # Не держим всех в одном котле — ограничиваемся семафором
    async with _SEM:
        try:
            timeout = aiohttp.ClientTimeout(total=ANIMATE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                # 1) Создать prediction
                async with session.post(_REPLICATE_API_URL, json=payload, headers=_HEADERS) as resp:
                    if resp.status == 402:
                        # нет кредитов на Replicate
                        text = await resp.text()
                        logger.error("Replicate 402: %s", text)
                        return _result_error("replicate_402")
                    if resp.status not in (200, 201):
                        text = await resp.text()
                        logger.error("Replicate create failed: %s %s", resp.status, text)
                        # Иногда указывают обязательные поля:
                        if resp.status == 422:
                            return _result_error("replicate_422_fields", fields=_extract_fields(text))
                        return _result_error("replicate_create")

                    pred = await resp.json()
                    get_url = pred.get("urls", {}).get("get")
                    if not get_url:
                        return _result_error("replicate_create")

                # 2) Опрос статуса (не блокируем event loop)
                while True:
                    await asyncio.sleep(ANIMATE_POLL)
                    async with session.get(get_url, headers=_HEADERS) as r2:
                        if r2.status != 200:
                            logger.warning("Replicate poll status=%s", r2.status)
                            continue
                        data = await r2.json()
                        status = data.get("status")
                        if status in ("succeeded", "failed", "canceled"):
                            if status == "succeeded":
                                out = data.get("output")
                                url = _pick_video_url(out)
                                if url:
                                    return {"ok": True, "url": url}
                                return _result_error("replicate_no_output")
                            else:
                                logger.error("Replicate status: %s", status)
                                return _result_error("replicate_status", status=status)
                        # иначе — "starting"|"processing" — продолжаем опрос
        except asyncio.TimeoutError:
            logger.error("Replicate timeout (>%ss)", ANIMATE_TIMEOUT)
            return _result_error("timeout")
        except aiohttp.ClientResponseError as e:
            logger.exception("Replicate client error: %s", e)
            if e.status == 401:
                return _result_error("replicate_auth")
            return _result_error("replicate_http")
        except Exception as e:
            logger.exception("Replicate unexpected: %s", e)
            return _result_error("unexpected")

def _pick_video_url(out: Any) -> Optional[str]:
    """
    Выбираем первый подходящий URL (mp4/gif) из ответа.
    """
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        for u in out:
            if isinstance(u, str) and (u.endswith(".mp4") or u.endswith(".gif")):
                return u
        # если список строк, вернем первый
        for u in out:
            if isinstance(u, str):
                return u
    if isinstance(out, dict):
        # некоторые модели кладут под ключ "video" или "result"
        for k in ("video", "result", "output"):
            v = out.get(k)
            if isinstance(v, str):
                return v
    return None

def _extract_fields(text: str) -> List[str]:
    """
    Пытаемся вытащить названия обязательных полей из текста 422.
    Это эвристика — просто помогает показать юзеру подсказку.
    """
    fields: List[str] = []
    lower = text.lower()
    for key in ("face_image", "driving_video", "image", "prompt"):
        if key in lower and key not in fields:
            fields.append(key)
    return fields

async def download_file(url: str, dst_path: str):
    """
    Асинхронная закачка файла (без блокировок event loop).
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            # Стримим в файл по кускам
            with open(dst_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    if chunk:
                        f.write(chunk)

import os
import time
import asyncio
import logging
from typing import Optional, Dict

import requests

logger = logging.getLogger("processing")

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_MODEL = os.getenv("REPLICATE_MODEL")

# WAN controls via env
WAN_RESOLUTION = os.getenv("WAN_RESOLUTION", "720p")  # 480p/720p/1080p (check model docs)
WAN_DURATION = os.getenv("WAN_DURATION", "5")         # seconds
WAN_SEED = os.getenv("WAN_SEED", "")                  # optional int

REPLICATE_API_URL = "https://api.replicate.com/v1/predictions"
HEADERS = {
    "Authorization": f"Token {REPLICATE_API_TOKEN}" if REPLICATE_API_TOKEN else "",
    "Content-Type": "application/json"
}

def _extract_required_fields_422(text: str):
    try:
        import json, re
        data = json.loads(text)
        fields = []
        for item in data.get("invalid_fields", []) or []:
            desc = item.get("description", "")
            if " is required" in desc:
                fields.append(desc.split(" is required")[0])
        if not fields and isinstance(data.get("detail"), str):
            fields = re.findall(r"([a-zA-Z_]+) is required", data["detail"]) or []
        return list(dict.fromkeys(fields))
    except Exception:
        return []

async def animate_photo_via_replicate(
    source_image_url: str,
    model_override: Optional[str] = None,
    prompt: Optional[str] = None
) -> Dict[str, str]:
    """
    Returns:
      {"ok": True,  "url": "https://...mp4"}
      {"ok": False, "code": "...", "msg": "..."}
    """
    model = model_override or REPLICATE_MODEL
    if not REPLICATE_API_TOKEN or not model:
        logger.error("Replicate credentials/model are not set")
        return {"ok": False, "code": "config", "msg": "REPLICATE_API_TOKEN/REPLICATE_MODEL not set"}

    # WAN i2v expects: image + prompt (+ resolution/duration/seed)
    input_payload = {
        "image": source_image_url
    }
    if prompt:
        input_payload["prompt"] = prompt
    if WAN_RESOLUTION:
        input_payload["resolution"] = WAN_RESOLUTION
    if WAN_DURATION:
        try:
            input_payload["duration"] = int(WAN_DURATION)
        except ValueError:
            pass
    if WAN_SEED:
        try:
            input_payload["seed"] = int(WAN_SEED)
        except ValueError:
            pass

    payload = {"version": model, "input": input_payload}

    # 1) create prediction
    r = requests.post(REPLICATE_API_URL, json=payload, headers=HEADERS, timeout=60)
    if r.status_code != 201:
        logger.error("Replicate create failed: %s %s", r.status_code, r.text)
        if r.status_code == 402:
            return {"ok": False, "code": "replicate_402", "msg": "Insufficient credit"}
        if r.status_code in (401, 403):
            return {"ok": False, "code": "replicate_auth", "msg": "Invalid token or access"}
        if r.status_code == 422:
            return {"ok": False, "code": "replicate_422_fields", "fields": _extract_required_fields_422(r.text)}
        return {"ok": False, "code": "replicate_create", "msg": r.text}

    pred = r.json()
    get_url = pred.get("urls", {}).get("get")

    # 2) poll
    for _ in range(180):  # up to 3 minutes
        time.sleep(1)
        rr = requests.get(get_url, headers=HEADERS, timeout=30)
        data = rr.json()
        status = data.get("status")
        if status in ("succeeded", "failed", "canceled"):
            if status == "succeeded":
                out = data.get("output")
                if isinstance(out, list) and out:
                    for u in out:
                        if isinstance(u, str) and (u.endswith(".mp4") or u.endswith(".gif")):
                            return {"ok": True, "url": u}
                    if isinstance(out[0], str):
                        return {"ok": True, "url": out[0]}
                if isinstance(out, str):
                    return {"ok": True, "url": out}
                if isinstance(out, dict):
                    maybe = out.get("video") or out.get("url")
                    if isinstance(maybe, str):
                        return {"ok": True, "url": maybe}
                return {"ok": False, "code": "replicate_output", "msg": f"Unexpected output: {out}"}
            else:
                logger.error("Replicate status: %s", status)
                return {"ok": False, "code": f"replicate_{status}", "msg": status}
    return {"ok": False, "code": "replicate_timeout", "msg": "Timeout waiting result"}

async def download_file(url: str, dst_path: str):
    loop = asyncio.get_running_loop()
    def _download():
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dst_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
    await loop.run_in_executor(None, _download)

# было:
# async def animate_photo_via_replicate(source_image_url: str, model_override: Optional[str] = None) -> Dict[str, str]:

async def animate_photo_via_replicate(
    source_image_url: str,
    model_override: Optional[str] = None,
    prompt: Optional[str] = None
) -> Dict[str, str]:
    ...

    input_payload = {REPLICATE_INPUT_KEY: source_image_url}

    # Параметры WAN из .env
    wan_resolution = os.getenv("WAN_RESOLUTION", "")    # напр. 720p
    wan_duration  = os.getenv("WAN_DURATION", "")       # напр. 5
    wan_seed      = os.getenv("WAN_SEED", "")

    if prompt:
        input_payload["prompt"] = prompt  # WAN 2.5 i2v fast принимает prompt. :contentReference[oaicite:4]{index=4}
    if wan_resolution:
        input_payload["resolution"] = wan_resolution     # 480p/720p/1080p. :contentReference[oaicite:5]{index=5}
    if wan_duration:
        try:
            input_payload["duration"] = int(wan_duration)
        except ValueError:
            pass
    if wan_seed:
        try:
            input_payload["seed"] = int(wan_seed)
        except ValueError:
            pass

    payload = {"version": model, "input": input_payload}
    ...

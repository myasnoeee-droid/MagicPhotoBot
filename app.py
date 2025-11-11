@dp.message(F.photo)
async def on_photo(message: Message):
    ...
    user_prompt = (message.caption or "").strip()
    if not user_prompt:
        # дефолт на случай пустой подписи
        user_prompt = "the person smiles naturally, gentle facial motion, subtle head movement, realistic lighting"

    ...
    result = await animate_photo_via_replicate(source_image_url=file_url, prompt=user_prompt)
    ...
    # в ветке фоллбэка:
    # fallback = await animate_photo_via_replicate(source_image_url=file_url, model_override=ECONOMY_MODEL, prompt=user_prompt)

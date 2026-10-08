"""Auto-split from the original telegram.py monolith. No behavior change."""
from ._common import asyncio, logger, re, telegram_service, time



async def _link_content_for_llm(db, url: str):
    """Fetch (title, content_or_None, error) for the summary/post LLM prompts.

    Goes through SearchService.fetch_urls, which already substitutes the transcript for YouTube
    links (so the model never summarizes contentless watch-page HTML). Callers must NOT generate
    from None content - that's how the hallucinated summaries/posts happened.
    """
    import asyncio as _asyncio
    from app.services.search_service import SearchService as _SS
    try:
        f = await _asyncio.wait_for(_SS(db).fetch_urls([url], max_urls=1), timeout=25)
    except _asyncio.TimeoutError:
        return "", None, "timed out fetching the URL"
    if f and f[0].get("content") and not f[0].get("error"):
        return f[0].get("title", ""), f[0]["content"], None
    return (f[0].get("title", "") if f else ""), None, (f[0].get("error") if f else "") or "could not fetch content"


async def _send_png_as_document(chat_id: str, image_b64: str, caption: str = None) -> bool:
    """Send a base64 PNG to a chat as a Telegram document. Returns True on success.

    Used as a fallback when send_photo rejects an image — Telegram caps photo
    dimensions/size, which full-page screenshots routinely exceed; documents don't.
    """
    try:
        import base64 as _b64
        png = image_b64
        if isinstance(png, str):
            if png.startswith("data:image"):
                png = png.split(",", 1)[1]
            png = _b64.b64decode(png)
        res = await telegram_service.send_document_bytes(chat_id, png, "image.png", caption, content_type="image/png")
        return bool(res.get("ok"))
    except Exception as e:
        logger.error(f"send_png_as_document failed: {e}")
        return False


async def _send_screenshot(chat_id: str, image_b64: str, caption: str) -> None:
    """Deliver a screenshot as a PDF — full resolution and uncompressed (Telegram squashes photos
    of tall pages to an unreadable size); a PDF also previews inline on mobile and feeds the PDF
    tools. Falls back to a PNG document, then a photo, then plain text."""
    import base64 as _b64
    # Decode the PNG once: used for the PDF and the document fallback.
    png = image_b64
    if isinstance(png, str):
        if png.startswith("data:image"):
            png = png.split(",", 1)[1]
        png = _b64.b64decode(png)

    sent = False
    try:
        # Build the PDF with PyMuPDF (LOSSLESS — Flate), NOT Pillow's PDF save which re-encodes the
        # image as JPEG (lossy) and would blur the website text / break OCR. One page sized to the
        # capture, so a tall full-page screenshot is a single crisp, scrollable page.
        def _png_to_pdf(b: bytes) -> bytes:
            import fitz  # PyMuPDF
            src = fitz.open(stream=b, filetype="png")
            try:
                return src.convert_to_pdf()
            finally:
                src.close()
        pdf = await asyncio.to_thread(_png_to_pdf, png)
        res = await telegram_service.send_document_bytes(
            chat_id, pdf, "screenshot.pdf", caption, content_type="application/pdf")
        sent = bool(res.get("ok"))
    except Exception as e:
        logger.warning(f"[screenshot] PDF build/send failed, falling back to PNG document: {e}")
    if not sent:
        sent = await _send_png_as_document(chat_id, png, caption)
    if not sent:
        photo_result = await telegram_service.send_photo(chat_id, image_b64, caption)
        sent = photo_result.get("ok", False)
        if not sent:
            await telegram_service.send_message(chat_id, f"{caption}\n\n(Screenshot failed to send)")
            return



async def _deliver_pin_result(chat_id: str, result: dict) -> None:
    """Deliver a re-run pinned command's result to a Telegram chat. Covers the result types a
    pinnable command produces (text / search / image / images / files / video / audio /
    flashcards), reusing the same senders as the main message handler. Share-offer prompts are
    intentionally omitted — re-running a pin shouldn't nag to repost."""
    import base64 as _b64, os as _os, tempfile as _tmp

    rtype = (result or {}).get("type", "text")
    content = (result or {}).get("content", "") or ""
    image = result.get("image")

    if rtype == "generated_image" and image and result.get("prefer_document"):
        await _send_screenshot(chat_id, image, content or "")
    elif rtype == "generated_image" and image:
        res = await telegram_service.send_photo(chat_id, image, content or None)
        if not res.get("ok") and not await _send_png_as_document(chat_id, image, content or ""):
            await telegram_service.send_message(chat_id, f"{content}\n\n(Image failed to send)", parse_mode="")
    elif rtype == "generated_video" and result.get("video"):
        path = None
        try:
            fd, path = _tmp.mkstemp(prefix="tg_pin_", suffix=".mp4")
            with _os.fdopen(fd, "wb") as f:
                f.write(_b64.b64decode(result["video"]))
            r = await telegram_service.send_video(chat_id, path, caption=content)
            if not r.get("ok"):
                await telegram_service.send_message(chat_id, f"{content}\n\n(Video failed to send)", parse_mode="")
        finally:
            if path and _os.path.exists(path):
                try: _os.unlink(path)
                except OSError: pass
    elif rtype == "generated_audio" and result.get("audio"):
        fmt = (result.get("format") or "mp3").lower()
        path = None
        try:
            fd, path = _tmp.mkstemp(prefix="tg_pin_", suffix="." + fmt)
            with _os.fdopen(fd, "wb") as f:
                f.write(_b64.b64decode(result["audio"]))
            r = await telegram_service.send_audio(chat_id, path, title="PosterChanAI", caption=content)
            if not r.get("ok"):
                await telegram_service.send_message(chat_id, f"{content}\n\n(Audio failed to send)", parse_mode="")
        finally:
            if path and _os.path.exists(path):
                try: _os.unlink(path)
                except OSError: pass
    elif rtype == "search":
        links = ""
        results = result.get("results") or []
        if results:
            lines = []
            for r in results[:5]:
                title = (r.get("title") or r.get("url", ""))[:60]
                url = r.get("url", "")
                if url:
                    lines.append(f"{len(lines) + 1}. {title}\n{url}")
            if lines:
                links = "\n\n🔗 Sources:\n" + "\n".join(lines)
        await telegram_service.send_message(chat_id, (content or "(no summary)") + links, parse_mode="")
    elif rtype == "images":
        await telegram_service.send_message(chat_id, content or "Image results", parse_mode="")
        for img in (result.get("images") or []):
            img_url = img.get("img_src", "")
            if not img_url:
                continue
            await telegram_service.send_photo(chat_id, img_url, ((img.get("title") or "")[:80]) or None)
            await asyncio.sleep(0.15)
    elif rtype == "files":
        if content:
            await telegram_service.send_message(chat_id, content, parse_mode="")
        for f in (result.get("files") or []):
            f_bytes = f.get("data")
            if not f_bytes:
                continue
            r = await telegram_service.send_document_bytes(chat_id, f_bytes, f.get("filename", "file"))
            if not r.get("ok"):
                await telegram_service.send_message(chat_id, f"❌ Failed to send {f.get('filename', 'file')}", parse_mode="")
            await asyncio.sleep(0.15)
    else:
        await telegram_service.send_message(chat_id, content or "Done.", parse_mode="")

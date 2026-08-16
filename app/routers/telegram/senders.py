"""Auto-split from the original telegram.py monolith. No behavior change."""
from ._common import Optional, SessionLocal, User, _flashcard_decks_cache, _geni_image_cache, _media_action_cache, _news_source_cache, _nostr_post_cache, _pleroma_post_cache, asyncio, datetime, logger, re, telegram_service, time
from .keyboards import _flashcard_keyboard, _has_nostr, _has_pleroma, _news_source_keyboard, _strip_cmd_links, _torrent_nav_keyboard, _ytdl_video_keyboard


async def _post_to_nostr(user, text: str, image_bytes: Optional[bytes] = None) -> None:
    """Publish a note on the user's linked Nostr account, uploading media to their
    configured Blossom/NIP-96 host first (Nostr's media model). Shared by the
    individual `nostr:post` button and `all:post`."""
    from app.services.nostr import nostr_service as _ns
    seckey = _ns.decode_seckey(user.nostr_nsec)
    relays = _ns.relay.normalize_relays(user.nostr_relays) or _ns.DEFAULT_RELAYS
    media_cfg = {"service": getattr(user, "nostr_media_service", None) or "blossom",
                 "endpoint": getattr(user, "nostr_media_endpoint", None) or ""}
    media_list = []
    if image_bytes:
        from app.services.media_service import detect_mime
        mime, _ = detect_mime(image_bytes)
        media_list = [(image_bytes, mime)]
    await _ns.post_note(seckey, relays, text or "", media_list=media_list, media_cfg=media_cfg)

async def _send_news_source_selector(chat_id: str, sources: list):
    """Send news source selection menu."""
    # Cache sources for callback handling
    _news_source_cache[chat_id] = sources

    source_list = "\n".join([f"• {s.get('name', 'Unknown')}" for s in sources[:8]])
    text = f"📰 *Select a news source:*\n\n{source_list}"

    await telegram_service.send_message(
        chat_id,
        text,
        reply_markup=_news_source_keyboard(sources)
    )


async def _send_torrent_results(chat_id: str, category: str, user_id: int):
    """Send each torrent result as its own message with a download button beneath it."""
    import asyncio
    from app.services.command_service import _torrent_cache

    cached = _torrent_cache.get(user_id, {}).get(category, [])
    if not cached:
        await telegram_service.send_message(chat_id, "No results found.")
        return

    cat_label = {"movies": "🎬 Movies", "tv": "📺 TV", "music": "🎵 Music", "anime": "🎌 Anime", "search": "🔍 Search"}.get(category, category.upper())
    await telegram_service.send_message(chat_id, f"**{cat_label}** — {len(cached)} results:")

    for i, t in enumerate(cached, 1):
        title = t.title[:80] + "..." if len(t.title) > 80 else t.title
        title_escaped = title.replace("[", "(").replace("]", ")")
        if t.url:
            title_line = f"[{title_escaped}]({t.url})"
        else:
            title_line = title_escaped
        text = f"**{i}. {title_line}**\n🌱 {t.seeders}  👤 {t.leechers}  📦 {t.size}"
        markup = {"inline_keyboard": [[
            {"text": f"📥 Download #{i}", "callback_data": f"t:dl:{category}:{i}"}
        ]]}
        await telegram_service.send_message(chat_id, text, reply_markup=markup)
        await asyncio.sleep(0.1)

    # Nav buttons at the end
    await telegram_service.send_message(chat_id, "Choose another category:", reply_markup=_torrent_nav_keyboard())


async def _send_nyaa_results(chat_id: str, user_id: int):
    """Send each nyaa result as its own message with a download button beneath it."""
    import asyncio
    from app.services.command_service import _nyaa_cache

    cached = _nyaa_cache.get(user_id, [])
    if not cached:
        await telegram_service.send_message(chat_id, "No results found.")
        return

    await telegram_service.send_message(chat_id, f"**🎌 Nyaa** — {len(cached)} results:")

    for i, t in enumerate(cached, 1):
        title = t.title[:80] + "..." if len(t.title) > 80 else t.title
        title_escaped = title.replace("[", "(").replace("]", ")")
        if t.url:
            title_line = f"[{title_escaped}]({t.url})"
        else:
            title_line = title_escaped
        text = f"**{i}. {title_line}**\n🌱 {t.seeders}  👤 {t.leechers}  📦 {t.size}"
        markup = {"inline_keyboard": [[
            {"text": f"📥 Download #{i}", "callback_data": f"n:dl:{i}"}
        ]]}
        await telegram_service.send_message(chat_id, text, reply_markup=markup)
        await asyncio.sleep(0.1)

    await telegram_service.send_message(chat_id, "Search again:", reply_markup={"inline_keyboard": [[
        {"text": "🔎 New Nyaa Search", "callback_data": "n:search_hint:0"},
        {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
    ]]})


async def _send_active_torrents(chat_id: str, raw_content: str) -> None:
    """Send each active torrent as its own message with Pause/Remove buttons beneath it."""
    import asyncio

    refresh_btn = {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "t:list:0"}]]}

    if not raw_content or raw_content.strip() == "No torrents.":
        await telegram_service.send_message(chat_id, "No active torrents.", reply_markup=refresh_btn)
        return

    # Separate any leading status message (e.g. "⏸️ Paused torrent #1") from the list
    header = ""
    list_content = raw_content
    if "**Torrents:**" in raw_content:
        pre, _, rest = raw_content.partition("**Torrents:**")
        header = pre.strip()
        list_content = "**Torrents:**" + rest
    else:
        # No torrent list — forward as-is with refresh button
        await telegram_service.send_message(chat_id, _strip_cmd_links(raw_content), reply_markup=refresh_btn)
        return

    if header:
        await telegram_service.send_message(chat_id, header)
        await asyncio.sleep(0.1)

    # Split into individual torrent blocks on lines starting with **N.
    blocks = re.split(r'\n(?=\*\*\d+\. )', list_content)
    torrent_blocks = [b for b in blocks if re.match(r'\*\*\d+\. ', b)]

    if not torrent_blocks:
        await telegram_service.send_message(chat_id, _strip_cmd_links(list_content), reply_markup=refresh_btn)
        return

    for block in torrent_blocks:
        num_match = re.match(r'\*\*(\d+)\. ', block)
        if not num_match:
            continue
        i = int(num_match.group(1))

        is_paused = f"cmd:torrents resume {i}" in block
        if is_paused:
            toggle = {"text": "▶ Resume", "callback_data": f"t:resume:{i}"}
        else:
            toggle = {"text": "⏸ Pause", "callback_data": f"t:pause:{i}"}
        remove = {"text": "🗑 Remove", "callback_data": f"t:rm:{i}"}

        markup = {"inline_keyboard": [[toggle, remove]]}
        await telegram_service.send_message(chat_id, _strip_cmd_links(block), reply_markup=markup)
        await asyncio.sleep(0.1)

    await telegram_service.send_message(
        chat_id,
        "Manage active downloads:",
        reply_markup=refresh_btn,
    )


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
    tools. Falls back to a PNG document, then a photo, then plain text.

    Then cache the full-res PNG and offer one-tap 🔤 Read text / 🌐 Translate buttons, so the user
    can OCR/translate the capture WITHOUT re-uploading it (a re-uploaded photo is compressed too
    small to read)."""
    import base64 as _b64, time as _t
    # Decode the PNG once: used for the PDF, the document fallback, and the OCR cache.
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

    try:
        _media_action_cache[chat_id] = {"attachments": [("screenshot.png", png, "image/png")], "ts": _t.time()}
        await telegram_service.send_message(
            chat_id,
            "Do more with this capture? Tap below — uses the full-resolution image (no re-upload needed):",
            reply_markup={"inline_keyboard": [[
                {"text": "🔤 Read text", "callback_data": "media:ocr"},
                {"text": "🌐 Translate", "callback_data": "media:translate"},
                {"text": "🎴 Flashcards", "callback_data": "media:fc"},
            ]]},
        )
    except Exception as _e:
        logger.warning(f"[screenshot] OCR/translate offer failed: {_e}")

async def _offer_social_post(chat_id: str, post_text: str, user, telegram_svc, prompt: str = "📣 *Post this?*", image_bytes: Optional[bytes] = None):
    """Show the generated post and offer to share it on configured social platforms.

    Single source of truth for the per-chat image cache: callers that have an
    image to attach pass it as `image_bytes`; text-only callers (link summaries,
    news, yt) leave it None, which CLEARS any stale image left over from an
    earlier `geni`/photo share so it is never attached to an unrelated post.
    """
    if image_bytes is not None:
        _geni_image_cache[chat_id] = image_bytes
    else:
        _geni_image_cache.pop(chat_id, None)

    has_plr = _has_pleroma(user)
    has_nostr = _has_nostr(user)

    platform_count = sum([has_plr, has_nostr])
    if platform_count == 0:
        # No platforms connected: echo the post text, but never send an EMPTY message
        # (Telegram rejects empty text). Image-only posts — e.g. a glowing text card —
        # have already delivered the image to the chat, so there's nothing more to say.
        if (post_text or "").strip():
            await telegram_svc.send_message(chat_id, post_text)
        return

    # Store post in all platform caches now so any button works
    if has_plr:
        _pleroma_post_cache[chat_id] = post_text
    if has_nostr:
        _nostr_post_cache[chat_id] = post_text

    # Individual platform buttons on the first row
    individual = []
    if has_plr:
        individual.append({"text": "📣 Pleroma", "callback_data": "plr:post"})
    if has_nostr:
        individual.append({"text": "📣 Nostr", "callback_data": "nostr:post"})

    rows = [individual]

    # "Post to All" row — only shown when 2+ platforms configured
    if platform_count >= 2:
        rows.append([{"text": "🚀 Post to All", "callback_data": "all:post"}])

    # Glow it — render this text as a glowing neon graphic and attach it. Only for
    # text-only posts (when no image is already attached we'd otherwise clobber); once
    # glowed, image_bytes is set so the button won't reappear (no double-glow).
    if image_bytes is None and (post_text or "").strip():
        rows.append([{"text": "🌟 Glow it", "callback_data": "glow:textpost"}])

    rows.append([{"text": "❌ Skip", "callback_data": "mk:skip"}])

    await telegram_svc.send_message(
        chat_id,
        post_text + f"\n\n{prompt}",
        reply_markup={"inline_keyboard": rows},
    )


async def _deliver_files_result(chat_id: int, user, result: dict, offer_share: bool = True):
    """Send a CommandService 'files' result back as Telegram documents, optionally
    following up with a 'Post to social' prompt for the first image/video output.

    Module-level so both the callback handler and the message-handler caption reply
    deliver effect results identically (the callback's local `_send_files_result`
    delegates here)."""
    if result.get("type") == "files":
        if result.get("content"):
            await telegram_service.send_message(chat_id, result["content"])
        for f in result.get("files", []):
            if f.get("data"):
                await telegram_service.send_document_bytes(chat_id, f["data"], f.get("filename", "file"))
                await asyncio.sleep(0.15)
        if offer_share:
            _files = [f for f in result.get("files", []) if f.get("data")]
            _shareable = next(
                (f for f in _files if (f.get("content_type") or "").startswith(("image/", "video/"))),
                None,
            )
            if _shareable and (_has_pleroma(user) or _has_nostr(user)):
                _media_action_cache[chat_id] = {
                    "attachments": [(
                        _shareable.get("filename", "file"),
                        _shareable["data"],
                        _shareable.get("content_type", ""),
                    )],
                    "ts": time.time(),
                }
                await telegram_service.send_message(
                    chat_id, "📣 Post this to your timeline?",
                    reply_markup={"inline_keyboard": [[
                        {"text": "📣 Post to social", "callback_data": "media:post"},
                    ]]},
                )
    else:
        await telegram_service.send_message(chat_id, result.get("content", "Done."))


async def _send_flashcard(chat_id: str, deck: dict, message_id=None):
    """Render + send (new) or edit-in-place the deck's current card as a PNG."""
    import base64 as _b64
    from app.services import flashcards_service
    idx = deck["idx"]; total = len(deck["cards"]); card = deck["cards"][idx]
    answered = deck["answered"][idx]
    png = await asyncio.to_thread(
        flashcards_service.render_card_png, deck.get("title", "Flashcards"),
        idx, total, card, answered is not None, answered)
    kb = _flashcard_keyboard(deck)
    if message_id is None:
        res = await telegram_service.send_photo(chat_id, _b64.b64encode(png).decode(), None, reply_markup=kb)
        if res.get("ok"):
            deck["message_id"] = res.get("result", {}).get("message_id")
    else:
        await telegram_service.edit_message_media_photo(chat_id, message_id, png, reply_markup=kb)


async def _offer_ytdl_video_actions(chat_id: str, dl_result, source_url: str, user, db) -> None:
    """After a video download, cache it and offer Send / Compress / Clip / Clip+Compress.

    Shared by the `ytdl video` command and the pasted-link 🎬 Movie button so both
    let the user trim/shrink before sending (a 100-min video need not be sent whole).
    Above a sane in-RAM cap the file is saved to storage instead (clip/compress on a
    file that large isn't worth holding in memory). The temp dir is the caller's to
    clean — we read the bytes into the cache here so it survives that cleanup.
    """
    import os as _os
    _raw = _os.path.getsize(dl_result.local_path)
    if _raw > 250 * 1024 * 1024:
        from app.services.youtube_service import (
            download_video_and_save_to_storage, format_download_result,
        )
        save_result = await download_video_and_save_to_storage(
            url=source_url, user_id=user.id, db=db, subfolder="YouTube Videos",
        )
        await telegram_service.send_message(
            chat_id,
            f"❌ Video is too large to process here ({_raw // (1024*1024)} MB).\n\n{format_download_result(save_result)}"
        )
        return
    _fn = _os.path.basename(dl_result.local_path)
    with open(dl_result.local_path, "rb") as _vf:
        _vbytes = _vf.read()
    _cap = f"🎬 {dl_result.title}" if dl_result.title else "🎬 Video"
    _media_action_cache[chat_id] = {
        "attachments": [(_fn, _vbytes, "video/mp4")],
        "ts": time.time(),
        "ytdl": {"caption": _cap, "duration": int(dl_result.duration) if dl_result.duration else None},
    }
    await telegram_service.send_message(
        chat_id,
        f"✅ Downloaded ({_raw // (1024*1024)} MB). Send it as-is, or trim/shrink it first?",
        reply_markup=_ytdl_video_keyboard(),
    )


async def _offer_ytdl_share(chat_id: str, filename: str, video_bytes: bytes, db) -> None:
    """After a ytdl video is delivered (as-is or trimmed/compressed), offer to post
    it to the user's connected social platforms. No-op if none are connected.

    Points the media-action cache at the *delivered* bytes so 'Post to social'
    shares exactly what the user just received (e.g. the trimmed clip), then reuses
    the standard `media:post` flow.
    """
    user = db.query(User).filter(
        User.telegram_chat_id == chat_id, User.telegram_enabled == True
    ).first()
    if not (user and (_has_pleroma(user) or _has_nostr(user))):
        return
    _media_action_cache[chat_id] = {
        "attachments": [(filename or "video.mp4", video_bytes, "video/mp4")],
        "ts": time.time(),
    }
    await telegram_service.send_message(
        chat_id, "📣 Post this to your timeline?",
        reply_markup={"inline_keyboard": [[{"text": "📣 Post to social", "callback_data": "media:post"}]]},
    )


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
    elif rtype == "flashcards":
        cards = result.get("cards") or []
        if not cards:
            await telegram_service.send_message(chat_id, content or "Couldn't make flashcards.", parse_mode="")
        else:
            deck = {"title": result.get("title") or "Flashcards", "cards": cards, "idx": 0,
                    "answered": [None] * len(cards), "score": 0, "ts": time.time()}
            _flashcard_decks_cache[chat_id] = deck
            await _send_flashcard(chat_id, deck)
    else:
        await telegram_service.send_message(chat_id, content or "Done.", parse_mode="")

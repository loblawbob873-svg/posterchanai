"""Auto-split from webhook.py: the callback_query half of _handle_telegram_update."""
from ._common import ChatService, CommandService, User, _CLIP_START_PROMPT, _CONSUMED, _EFFECT_CAPTION_PROMPT, _FIN_INCOME_PROMPT, _FLASHCARD_TTL, _HELP_SECTIONS, _MEDIA_ACTION_TTL, _MEME_PROMPT, _SOCIAL_CAPTION_PROMPT, _clip_pending, _effect_caption_pending, _effect_char_pending, _finance_bills_cache, _flashcard_decks_cache, _geni_image_cache, _link_action_cache, _matrix_post_cache, _matrix_room_cache, _media_action_cache, _misskey_post_cache, _news_post_cache, _news_source_cache, _pleroma_post_cache, _youtube_action_cache, asyncio, logger, re, telegram_service, time
from .keyboards import _4chan_initial_keyboard, _build_torrent_keyboard, _character_prompt_keyboard, _has_matrix, _has_misskey, _has_pleroma, _help_main_keyboard, _media_action_keyboard, _media_effects_keyboard, _media_fx_memes_keyboard, _media_fx_sounds_keyboard, _media_fx_themes_keyboard, _media_translate_keyboard, _news_menu_keyboard, _recover_post_text, _split_news_into_articles, _strip_cmd_links, _strip_hashtags, _torrents_menu_keyboard, re
from .senders import User, _deliver_files_result, _finance_bills_cache, _geni_image_cache, _has_matrix, _has_misskey, _has_pleroma, _link_content_for_llm, _matrix_post_cache, _media_action_cache, _misskey_post_cache, _news_source_cache, _offer_social_post, _offer_ytdl_share, _offer_ytdl_video_actions, _pleroma_post_cache, _send_4chan_catalog, _send_4chan_thread, _send_active_torrents, _send_bills_list, _send_budget, _send_flashcard, _send_news_source_selector, _send_screenshot, _send_torrent_results, _strip_cmd_links, asyncio, logger, re, telegram_service, time


async def _handle_callback(update, db):
    try:
        callback_query = update.get("callback_query")
        if callback_query:
            # Handle inline button callbacks
            chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id"))
            data = callback_query.get("data", "")
            callback_query_id = callback_query.get("id")

            logger.info(f"Received Telegram callback query: {data}")

            # Acknowledge immediately so Telegram removes the loading spinner
            await telegram_service.answer_callback_query(callback_query_id)

            if data.startswith("rem:"):
                # Reminder Cancel button (from the `reminders` list).
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                parts = data.split(":")
                if len(parts) >= 3 and parts[1] == "cancel" and parts[2].isdigit():
                    from app.services import reminder_service
                    ok = reminder_service.cancel_reminder(db, cb_user, int(parts[2]))
                    await telegram_service.send_message(
                        chat_id, "🗑️ Reminder cancelled." if ok else "No matching pending reminder.")
                return {"ok": True}

            if data.startswith("pin:"):
                # Pinned-search Run / Delete buttons.
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                parts = data.split(":")
                if len(parts) >= 3 and parts[2].isdigit():
                    from app.services import saved_search_service
                    sid = int(parts[2])
                    if parts[1] == "del":
                        ok = saved_search_service.delete_saved_search(db, cb_user, sid)
                        await telegram_service.send_message(
                            chat_id, "🗑️ Saved search deleted." if ok else "No matching saved search.")
                    elif parts[1] == "run":
                        s = next((x for x in saved_search_service.list_saved_searches(db, cb_user) if x.id == sid), None)
                        if not s:
                            await telegram_service.send_message(chat_id, "That saved search is gone.")
                        else:
                            _q = saved_search_service.normalize_query(s.query)
                            await telegram_service.send_message(chat_id, f"🔍 Searching: {_q}", parse_mode="")
                            _res = await CommandService(db, user=cb_user).execute_command("search", _q)
                            # Send the AI summary PLUS the source links so it's clearly grounded in a
                            # real web search (matches the web UI, which shows the result list too).
                            _msg = _res.get("content", "") or "(no summary)"
                            _results = _res.get("results") or []
                            if _results:
                                _msg += "\n\n🔗 Sources:"
                                for _i, _r in enumerate(_results[:5], 1):
                                    _msg += f"\n{_i}. {_r.get('title', 'link')}\n{_r.get('url', '')}"
                            await telegram_service.send_message(chat_id, _msg, parse_mode="")
                return {"ok": True}

            if data.startswith("t:"):
                # Torrent inline button — look up the linked user and run the command
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                cb_command_service = CommandService(db, user=cb_user)
                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "menu":
                    # Show torrents main menu
                    await telegram_service.send_message(
                        chat_id,
                        "🧲 *Torrents Menu*\n\nChoose an option:",
                        reply_markup=_torrents_menu_keyboard()
                    )
                    return {"ok": True}

                elif action == "cat" and len(parts) >= 3:
                    # Category browse: send individual result messages
                    category = parts[2]
                    try:
                        await cb_command_service.execute_command("torrents", category)
                        await _send_torrent_results(chat_id, category, cb_user.id)
                    except Exception as cb_err:
                        logger.error(f"Torrent callback error: {cb_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error: {cb_err}")
                    return {"ok": True}
                elif action == "dl" and len(parts) >= 4:
                    # Download from browse list: t:dl:movies:3
                    category = parts[2]
                    num = parts[3]
                    torrents_arg = f"download {category} {num}"
                elif action in ("pause", "resume", "rm") and len(parts) >= 3:
                    # Manage active torrent: t:pause:2
                    num = parts[2]
                    torrents_arg = f"{action} {num}"
                elif action == "list":
                    torrents_arg = "list"
                elif action == "search_hint":
                    await telegram_service.send_message(
                        chat_id,
                        "🔍 Type your torrent search:",
                        reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": "e.g. dark knight 1080p"}
                    )
                    return {"ok": True}
                else:
                    return {"ok": True}

                try:
                    cb_result = await cb_command_service.execute_command("torrents", torrents_arg)
                    cb_content = cb_result.get("content", "")

                    cb_arg_parts = torrents_arg.strip().split()
                    cb_arg_sub = cb_arg_parts[0].lower() if cb_arg_parts else ""
                    if cb_arg_sub in ("list", "ls", "pause", "resume", "rm"):
                        # Send each torrent as its own message with buttons beneath it
                        await _send_active_torrents(chat_id, cb_content)
                    else:
                        cb_reply_markup = _build_torrent_keyboard(cb_arg_sub, cb_content, cb_user.id)
                        if cb_arg_sub in ("download", "dl", "get") and cb_reply_markup is None:
                            cb_reply_markup = {"inline_keyboard": [[
                                {"text": "📋 Active Downloads", "callback_data": "t:list:0"}
                            ]]}
                        cb_content = _strip_cmd_links(cb_content)
                        await telegram_service.send_message(chat_id, cb_content, reply_markup=cb_reply_markup)
                except Exception as cb_err:
                    logger.error(f"Torrent callback error: {cb_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"Error: {cb_err}")

            elif data == "ytdlv:send":
                # "Send as-is" after `ytdl video` — deliver the cached download as a video.
                import os as _os, tempfile, shutil
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That download expired — run `ytdl video <url>` again.")
                    return {"ok": True}
                _vid = next((a for a in _entry["attachments"] if (a[2] or "").startswith("video/")), None)
                if not _vid:
                    await telegram_service.send_message(chat_id, "Nothing to send.")
                    return {"ok": True}
                _fn, _vbytes, _ = _vid
                if len(_vbytes) > 50 * 1024 * 1024:
                    await telegram_service.send_message(
                        chat_id,
                        f"❌ Too large to send as-is ({len(_vbytes) // (1024*1024)} MB). "
                        "Tap 🗜 Compress or ✂️ Clip to shrink it under Telegram's 50 MB limit.")
                    return {"ok": True}
                _ymeta = _entry.get("ytdl", {})
                _tmpdir = tempfile.mkdtemp(prefix="tg_ytdlv_send_")
                try:
                    _sp = _os.path.join(_tmpdir, _fn)
                    with open(_sp, "wb") as _of:
                        _of.write(_vbytes)
                    _r = await telegram_service.send_video(
                        chat_id=chat_id, file_path=_sp,
                        caption=_ymeta.get("caption"), duration=_ymeta.get("duration"),
                    )
                    if not _r.get("ok"):
                        await telegram_service.send_message(chat_id, f"❌ Failed to send video: {_r.get('description', _r.get('error', 'Unknown error'))}")
                    else:
                        await _offer_ytdl_share(chat_id, _fn, _vbytes, db)
                finally:
                    shutil.rmtree(_tmpdir, ignore_errors=True)
                return {"ok": True}

            elif data == "media:fc":
                # 🎴 Flashcards button on an uploaded file → build the quiz from the cached upload.
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the file again.")
                    return {"ok": True}
                await telegram_service.send_message(chat_id, "🎴 Generating flashcards…")
                _res = await CommandService(db, user=cb_user).execute_command(
                    "flashcards", "", attachments=_entry["attachments"])
                if _res.get("type") == "flashcards" and _res.get("cards"):
                    if _res.get("note"):
                        await telegram_service.send_message(chat_id, _res["note"])
                    _deck = {"title": _res.get("title") or "Flashcards", "cards": _res["cards"],
                             "idx": 0, "answered": [None] * len(_res["cards"]), "score": 0, "ts": time.time()}
                    _flashcard_decks_cache[chat_id] = _deck
                    await _send_flashcard(chat_id, _deck)
                else:
                    await telegram_service.send_message(chat_id, _res.get("content") or "Couldn't make flashcards from that file.")
                return {"ok": True}

            elif data.startswith("fc:"):
                # Flashcard quiz navigation/answer. State lives in _flashcard_decks_cache[chat_id].
                _deck = _flashcard_decks_cache.get(chat_id)
                _mid = (callback_query.get("message") or {}).get("message_id")
                if not _deck or (time.time() - _deck.get("ts", 0)) > _FLASHCARD_TTL:
                    _flashcard_decks_cache.pop(chat_id, None)
                    await telegram_service.answer_callback_query(
                        callback_query_id, "This quiz expired — send the file again.", show_alert=True)
                    return {"ok": True}
                _deck["ts"] = time.time()
                _parts = data.split(":")
                _act = _parts[1] if len(_parts) > 1 else ""
                _total = len(_deck["cards"])
                if _act == "ans":
                    _idx = _deck["idx"]
                    try:
                        _opt = int(_parts[2])
                    except (IndexError, ValueError):
                        _opt = -1
                    if _deck["answered"][_idx] is None and 0 <= _opt < len(_deck["cards"][_idx].get("options", [])):
                        _deck["answered"][_idx] = _opt
                        if _opt == _deck["cards"][_idx].get("correct"):
                            _deck["score"] += 1
                        await _send_flashcard(chat_id, _deck, message_id=_mid)
                elif _act == "next" and _deck["idx"] < _total - 1:
                    _deck["idx"] += 1
                    await _send_flashcard(chat_id, _deck, message_id=_mid)
                elif _act == "prev" and _deck["idx"] > 0:
                    _deck["idx"] -= 1
                    await _send_flashcard(chat_id, _deck, message_id=_mid)
                elif _act == "restart":
                    _deck["idx"] = 0
                    _deck["score"] = 0
                    _deck["answered"] = [None] * _total
                    await _send_flashcard(chat_id, _deck, message_id=_mid)
                return {"ok": True}

            elif data.startswith("media:"):
                # Uploaded-file action buttons (compress / convert / read text / summarize)
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                # Keep the entry (don't pop) so multiple actions can run on the
                # same upload; it expires by TTL or is overwritten by the next upload.
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the file again.")
                    return {"ok": True}

                _atts = _entry["attachments"]
                _action = data.split(":", 1)[1]
                from app.services.media_service import is_image, is_video, is_pdf
                cb_command_service = CommandService(db, user=cb_user)

                async def _send_files_result(result, offer_share: bool = True):
                    """Deliver a CommandService 'files' result (callback-scope wrapper
                    around the module-level `_deliver_files_result`)."""
                    await _deliver_files_result(chat_id, cb_user, result, offer_share)

                try:
                    if _action == "compress":
                        await telegram_service.send_message(chat_id, "🗜 Compressing…")
                        _cres = await cb_command_service.execute_command("compress", "", attachments=_atts)
                        await _send_files_result(_cres, offer_share=False)
                        # For a ytdl download, offer to post the compressed result.
                        if _entry.get("ytdl") and _cres.get("files") and _cres["files"][0].get("data"):
                            _cf = _cres["files"][0]
                            await _offer_ytdl_share(chat_id, _cf.get("filename", "video.mp4"), _cf["data"], db)
                    elif _action in ("clip", "clipcompress"):
                        # Kick off the interactive trim: ask for the start time. The end
                        # time is requested after the user replies (see ForceReply routing).
                        # "clipcompress" also compresses the clipped result; that intent is
                        # stashed on the cache entry so it survives the two-step prompt.
                        if not any(is_video(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to clip — that upload has no video.")
                        else:
                            _clip_pending.pop(chat_id, None)
                            _entry["compress_after"] = (_action == "clipcompress")
                            await telegram_service.send_message(
                                chat_id, _CLIP_START_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "0:10"},
                            )
                    elif _action == "removebackground":
                        _imgs = [a for a in _atts if is_image(a[0], a[2])]
                        await telegram_service.send_message(chat_id, "✂️ Removing background…")
                        await _send_files_result(await cb_command_service.execute_command("removebackground", "", attachments=_imgs), offer_share=False)
                    elif _action == "topdf":
                        _imgs = [a for a in _atts if is_image(a[0], a[2])]
                        await _send_files_result(await cb_command_service.execute_command("convert", "pdf", attachments=_imgs), offer_share=False)
                    elif _action == "toimg":
                        _pdfs = [a for a in _atts if is_pdf(a[0], a[2])]
                        await telegram_service.send_message(chat_id, "🖼 Converting…")
                        await _send_files_result(await cb_command_service.execute_command("convert", "images", attachments=_pdfs), offer_share=False)
                    elif _action == "ocr":
                        import base64 as _ocr_b64
                        from app.services.document_service import extract_image_text
                        _texts = []
                        for _fn, _fd, _ct in _atts:
                            if is_image(_fn, _ct):
                                _t = extract_image_text(_ocr_b64.b64encode(_fd).decode())
                                if _t:
                                    _texts.append(_t)
                        await telegram_service.send_message(chat_id, ("🔤 *Extracted text:*\n\n" + "\n\n".join(_texts)) if _texts else "No text found in the image(s).")
                    elif _action == "post":
                        # Prompt for an optional caption before showing the platform
                        # buttons. The reply is routed (see _SOCIAL_CAPTION_PROMPT) back
                        # into _offer_social_post with the media pulled from the cache.
                        _media = next((fd for fn, fd, ct in _atts if is_image(fn, ct)), None) \
                            or next((fd for fn, fd, ct in _atts if is_video(fn, ct)), None)
                        if not _media:
                            await telegram_service.send_message(chat_id, "Nothing to post.")
                        else:
                            await telegram_service.send_message(
                                chat_id, _SOCIAL_CAPTION_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "Caption (optional)"},
                            )
                    elif _action == "summarize":
                        import base64 as _sum_b64
                        from app.services.document_service import extract_pdf_text
                        _doc = "\n\n".join(
                            extract_pdf_text(_sum_b64.b64encode(_fd).decode()) or ""
                            for _fn, _fd, _ct in _atts if is_pdf(_fn, _ct)
                        ).strip()
                        if not _doc:
                            await telegram_service.send_message(chat_id, "Couldn't extract any text from the PDF.")
                        else:
                            _summary = await cb_command_service.chat_service.chat([
                                {"role": "system", "content": "Summarize the following document concisely. Output only the summary."},
                                {"role": "user", "content": _doc[:12000]},
                            ])
                            await telegram_service.send_message(chat_id, f"📝 *Summary:*\n\n{_summary}")
                    elif _action == "effects":
                        # Open the Effects submenu (meme / dildo / poo). The image stays in
                        # the cache; the submenu's buttons reuse the existing actions.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        else:
                            await telegram_service.send_message(
                                chat_id, "✨ Effects — pick a category:",
                                reply_markup=_media_effects_keyboard(),
                            )
                    elif _action.startswith("fxcat:"):
                        # An Effects category was chosen → show that sub-keyboard.
                        _cat = _action.split(":", 1)[1]
                        _cat_kbd = {
                            "themes": _media_fx_themes_keyboard,
                            "sounds": _media_fx_sounds_keyboard,
                            "memes": _media_fx_memes_keyboard,
                        }.get(_cat)
                        if not _cat_kbd:
                            await telegram_service.send_message(chat_id, "Unknown effects category.")
                        elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        else:
                            _cat_label = {"themes": "📺 TV/Movie Themes", "sounds": "🔊 Sound clips", "memes": "🎨 Memes / overlays"}[_cat]
                            await telegram_service.send_message(
                                chat_id, f"{_cat_label}:", reply_markup=_cat_kbd(),
                            )
                    elif _action == "back":
                        # Return from the Effects submenu to the main file actions.
                        _kbd = _media_action_keyboard(_atts, user=cb_user)
                        if _kbd:
                            await telegram_service.send_message(
                                chat_id, "📎 File actions:", reply_markup=_kbd,
                            )
                    elif _action.startswith("zq:"):
                        # Effect chosen from the Effects menu → offer a motion (zoom
                        # pan-out / camera shake). "No motion" reuses the effect's own
                        # media:<eff> handler.
                        _eff = _action.split(":", 1)[1]
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        elif _eff in CommandService.ANIMATED_EFFECTS:
                            # Already-animated effect — zoom/shake would freeze it, so skip the motion
                            # menu, but STILL offer the caption (meme text overlays fine on the video).
                            _effect_caption_pending[chat_id] = {"eff": _eff, "motion": "", "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                        else:
                            # Left column = motion alone; right column = the same motion
                            # with the trippy hue-cycle layered on top (the only combo
                            # that composes — geometry motions don't stack). 🌈 Trippy
                            # alone + ❌ None on the last row.
                            _rows = [
                                [
                                    {"text": "🔍 Zoom", "callback_data": f"media:mo:dz:{_eff}"},
                                    {"text": "🔍🌈 Zoom+", "callback_data": f"media:mo:dzt:{_eff}"},
                                ],
                                [
                                    {"text": "📳 Shake", "callback_data": f"media:mo:sh:{_eff}"},
                                    {"text": "📳🌈 Shake+", "callback_data": f"media:mo:sht:{_eff}"},
                                ],
                                [
                                    {"text": "〰️ Med", "callback_data": f"media:mo:ms:{_eff}"},
                                    {"text": "〰️🌈 Med+", "callback_data": f"media:mo:mst:{_eff}"},
                                ],
                                [
                                    {"text": "💥 Begin", "callback_data": f"media:mo:bs:{_eff}"},
                                    {"text": "💥🌈 Begin+", "callback_data": f"media:mo:bst:{_eff}"},
                                ],
                                [
                                    {"text": "💓 Pulse", "callback_data": f"media:mo:pl:{_eff}"},
                                    {"text": "💓🌈 Pulse+", "callback_data": f"media:mo:plt:{_eff}"},
                                ],
                                [
                                    {"text": "🪄 Alive (3D)", "callback_data": f"media:mo:al:{_eff}"},
                                    {"text": "🌟 Glow", "callback_data": f"media:mo:gl:{_eff}"},
                                ],
                                [
                                    {"text": "🌈 Trippy", "callback_data": f"media:mo:tr:{_eff}"},
                                    {"text": "❌ None", "callback_data": f"media:mo:none:{_eff}"},
                                ],
                            ]
                            await telegram_service.send_message(
                                chat_id, "✨ Add motion?", reply_markup={"inline_keyboard": _rows},
                            )
                    elif _action.startswith("mo:"):
                        # A motion (and optional trippy combo) was chosen. Code maps to
                        # the command arg, e.g. "dzt" → "zoom trippy", "none" → no motion.
                        # Caption is the FINAL step: after the motion we ask "Add text?"
                        # so any motion can be combined with a meme caption.
                        _, _code, _eff = _action.split(":", 2)
                        _motion = {
                            "dz": "zoom", "dzt": "zoom trippy",
                            "sh": "shake", "sht": "shake trippy",
                            "ms": "medshake", "mst": "medshake trippy",
                            "bs": "beginshake", "bst": "beginshake trippy",
                            "pl": "pulse", "plt": "pulse trippy",
                            "al": "alive", "gl": "glow",
                            "tr": "trippy", "none": "",
                        }.get(_code, "")
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        elif _eff == "thug":
                            # thug bakes its own "THUG LIFE" text — no custom caption; render now.
                            await telegram_service.send_message(chat_id, f"✨ {_eff}{(' + ' + _motion) if _motion else ''}…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command(_eff, _motion, attachments=_imgs))
                        else:
                            # Remember the effect + chosen motion, then offer the caption.
                            _effect_caption_pending[chat_id] = {"eff": _eff, "motion": _motion, "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                    elif _action.startswith("capq:"):
                        # Caption decision after a motion was picked. "add" → ForceReply
                        # for the text (render happens on the reply); "skip" → render now
                        # with just the motion.
                        _decision = _action.split(":", 1)[1]
                        _pend = _effect_caption_pending.get(chat_id)
                        if not _pend or (time.time() - _pend.get("ts", 0)) > _MEDIA_ACTION_TTL:
                            await telegram_service.send_message(chat_id, "⏳ That upload expired — tap the effect again.")
                            _effect_caption_pending.pop(chat_id, None)
                        elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                            _effect_caption_pending.pop(chat_id, None)
                        elif _decision == "add":
                            _pend["ts"] = time.time()
                            await telegram_service.send_message(
                                chat_id, _EFFECT_CAPTION_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "TOP TEXT"},
                            )
                        else:  # skip caption → the character step is the FINAL one; render there.
                            _eff, _motion = _pend["eff"], _pend.get("motion", "")
                            _effect_char_pending[chat_id] = {"eff": _eff, "motion": _motion, "caption": "", "ts": time.time()}
                            _effect_caption_pending.pop(chat_id, None)
                            await telegram_service.send_message(
                                chat_id, "🧸 Add a character (bottom-right)?",
                                reply_markup=_character_prompt_keyboard(),
                            )
                    elif _action.startswith("chr:"):
                        # FINAL step: a character (or "none") was chosen. Render ONCE with a combined
                        # arg so the shared parser applies motion + character + caption together.
                        _char = _action.split(":", 1)[1]
                        _pend = _effect_char_pending.get(chat_id)
                        if not _pend or (time.time() - _pend.get("ts", 0)) > _MEDIA_ACTION_TTL:
                            _effect_char_pending.pop(chat_id, None)
                            await telegram_service.send_message(chat_id, "⏳ That upload expired — tap the effect again.")
                        elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                            _effect_char_pending.pop(chat_id, None)
                            await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                        else:
                            _e = _pend["eff"]; _m = _pend.get("motion", ""); _c = _pend.get("caption", "")
                            _parts = []
                            if _m:
                                _parts.append(_m)
                            if _char != "none":
                                _parts.append(f"char {_char}")
                            if _c:
                                _parts.append(f"meme {_c}")
                            _arg = " ".join(_parts).strip()
                            _lbl = _e + (f" + {_char}" if _char != "none" else "")
                            await telegram_service.send_message(chat_id, f"✨ {_lbl}…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command(_e, _arg, attachments=_imgs))
                            _effect_char_pending.pop(chat_id, None)
                    elif _action == "meme":
                        # ForceReply for the caption; the image stays in the cache and is
                        # captioned when the reply arrives (see _MEME_PROMPT routing).
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to caption — that upload has no image.")
                        else:
                            await telegram_service.send_message(
                                chat_id, _MEME_PROMPT,
                                reply_markup={"force_reply": True, "selective": True,
                                              "input_field_placeholder": "TOP TEXT"},
                            )
                    elif _action == "dildo":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🍆 Adding dildos…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            # Send as a document (not send_photo): the result is a JPEG,
                            # whose base64 starts with "/9j/" — send_photo would treat that
                            # as a file path and fail.
                            await _send_files_result(await cb_command_service.execute_command("dildo", "", attachments=_imgs))
                    elif _action == "poo":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💩 Adding poop…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("poo", "", attachments=_imgs))
                    elif _action == "cum":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💦 Adding cum…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("cum", "", attachments=_imgs))
                    elif _action == "blood":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🩸 Adding blood…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("blood", "", attachments=_imgs))
                    elif _action == "bullethole":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕳️ Adding bullet holes…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("bullethole", "", attachments=_imgs))
                    elif _action == "fire":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔥 Setting it on fire…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("fire", "", attachments=_imgs))
                    elif _action == "glow":
                        # Enter the shared caption → character → render flow (so glow can get text +
                        # a character too, and the branding outro, like the other effects).
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to enhance — that upload has no image.")
                        else:
                            _effect_caption_pending[chat_id] = {"eff": "glow", "motion": "", "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                    elif _action == "gay":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to stamp — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏳️‍🌈 Stamping…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("gay", "", attachments=_imgs))
                    elif _action == "blacked":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to stamp — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🥷 Slapping the logo on…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("blacked", "", attachments=_imgs))
                    elif _action == "kosher":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to stamp — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "✡️ Certifying kosher…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("kosher", "", attachments=_imgs))
                    elif _action == "blue":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to paint — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔵 Dripping blue paint…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("blue", "", attachments=_imgs))
                    elif _action == "barked":
                        # No caption needed — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to bark at — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🐶 Barking…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("barked", "", attachments=_imgs))
                    elif _action == "hava":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎻 Hava Nagila-ing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("hava", "", attachments=_imgs))
                    elif _action == "indian":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🇮🇳 Adding the song…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("indian", "", attachments=_imgs))
                    elif _action == "yakety":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎷 Yakety Sax-ing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("yakety", "", attachments=_imgs))
                    elif _action == "yamete":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🛑 Yamete kudasai…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("yamete", "", attachments=_imgs))
                    elif _action == "curb":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😬 Curbing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("curb", "", attachments=_imgs))
                    elif _action == "depressing":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😢 Getting depressing…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("depressing", "", attachments=_imgs))
                    elif _action == "fahh":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌀 Fahh…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("fahh", "", attachments=_imgs))
                    elif _action == "helpme":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🆘 Helpme…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("helpme", "", attachments=_imgs))
                    elif _action == "gong":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔔 Gong…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("gong", "", attachments=_imgs))
                    elif _action == "fbi":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🚨 FBI OPEN UP…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("fbi", "", attachments=_imgs))
                    elif _action == "redeem":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💳 Do NOT redeem it…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("redeem", "", attachments=_imgs))
                    elif _action == "gigity":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😏 Gigity…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("gigity", "", attachments=_imgs))
                    elif _action == "beavis":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤤 Beavis…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("beavis", "", attachments=_imgs))
                    elif _action == "smell":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "👃 Can you imagine the smell…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("smell", "", attachments=_imgs))
                    elif _action == "hood":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏚️ Hood…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("hood", "", attachments=_imgs))
                    elif _action == "akbar":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕌 Akbar…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("akbar", "", attachments=_imgs))
                    elif _action == "retard":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "⚠️ Retard alert…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("retard", "", attachments=_imgs))
                    elif _action == "whoabuddy":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤠 Whoa buddy…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("whoabuddy", "", attachments=_imgs))
                    elif _action == "seth":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎬 Seth…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("seth", "", attachments=_imgs))
                    elif _action == "robocop":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤖 Robocop…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("robocop", "", attachments=_imgs))
                    elif _action == "titan":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🗿 Titan…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("titan", "", attachments=_imgs))
                    elif _action == "terminator":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🦾 Terminator…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("terminator", "", attachments=_imgs))
                    elif _action == "reze":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💣 Reze…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("reze", "", attachments=_imgs))
                    elif _action == "feliz":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎉 Feliz…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("feliz", "", attachments=_imgs))
                    elif _action == "prayer":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to the prayer clip — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🙏 Prayer…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("prayer", "", attachments=_imgs))
                    elif _action == "alive":
                        # 3D parallax — enter the shared caption → character → render flow.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to animate — that upload has no image.")
                        else:
                            _effect_caption_pending[chat_id] = {"eff": "alive", "motion": "", "ts": time.time()}
                            await telegram_service.send_message(
                                chat_id, "📝 Add a caption?",
                                reply_markup={"inline_keyboard": [[
                                    {"text": "✍️ Add text", "callback_data": "media:capq:add"},
                                    {"text": "▶️ No, render", "callback_data": "media:capq:skip"},
                                ]]},
                            )
                    elif _action == "sopranos":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🇮🇹 Sopranos…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("sopranos", "", attachments=_imgs))
                    elif _action == "cheers":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🍻 Cheers…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("cheers", "", attachments=_imgs))
                    elif _action == "munsters":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🧛 Munsters…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("munsters", "", attachments=_imgs))
                    elif _action == "happydays":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕺 Happy Days…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("happydays", "", attachments=_imgs))
                    elif _action == "dontwanttowait":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌊 Don't Want to Wait…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("dontwanttowait", "", attachments=_imgs))
                    elif _action == "strangerthings":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🔦 Stranger Things…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("strangerthings", "", attachments=_imgs))
                    elif _action == "adamsfamily":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🖤 Addams Family…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("adamsfamily", "", attachments=_imgs))
                    elif _action == "xmen":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "❌ X-Men…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("xmen", "", attachments=_imgs))
                    elif _action == "futurama":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🚀 Futurama…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("futurama", "", attachments=_imgs))
                    elif _action == "charliesangles":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "👼 Charlie's Angels…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("charliesangles", "", attachments=_imgs))
                    elif _action == "differentstroke":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌍 Diff'rent Strokes…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("differentstroke", "", attachments=_imgs))
                    elif _action == "seinfeld":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎤 Seinfeld…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("seinfeld", "", attachments=_imgs))
                    elif _action == "onepiece":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏴‍☠️ One Piece…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("onepiece", "", attachments=_imgs))
                    elif _action == "overtaken":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🏎️ Overtaken…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("overtaken", "", attachments=_imgs))
                    elif _action == "freebird":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🦅 Free Bird…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("freebird", "", attachments=_imgs))
                    elif _action == "kanye":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🐻 Kanye…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("kanye", "", attachments=_imgs))
                    elif _action == "darkness":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🌑 Darkness…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("darkness", "", attachments=_imgs))
                    elif _action == "bike":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🚲 Bike…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("bike", "", attachments=_imgs))
                    elif _action == "jobs":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "💼 They took our jobs…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("jobs", "", attachments=_imgs))
                    elif _action == "ree":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😡 REEEE…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("ree", "", attachments=_imgs))
                    elif _action == "liberal":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🗽 Liberal…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("liberal", "", attachments=_imgs))
                    elif _action == "moving":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "📦 Moving…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("moving", "", attachments=_imgs))
                    elif _action == "harlem":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🕺 Harlem Shake…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("harlem", "", attachments=_imgs))
                    elif _action == "chimp":
                        # No caption needed — render the overlay video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to overlay — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🐵 Chimp…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("chimp", "", attachments=_imgs))
                    elif _action == "consider":
                        # Image overlay — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to decorate — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🤔 Consider the following…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("consider", "", attachments=_imgs))
                    elif _action == "clay":
                        # Animated overlay — run immediately and post the result.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to overlay — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🗣️ Sheeeit…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("clay", "", attachments=_imgs))
                    elif _action == "wasteland":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🎸 Teenage wasteland…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("wasteland", "", attachments=_imgs))
                    elif _action == "mixalot":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "🍑 Baby got back…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("mixalot", "", attachments=_imgs))
                    elif _action == "thug":
                        # No caption needed — render the video and post it.
                        if not any(is_image(fn, ct) for fn, _, ct in _atts):
                            await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                        else:
                            await telegram_service.send_message(chat_id, "😎 THUG LIFE…")
                            _imgs = [a for a in _atts if is_image(a[0], a[2])]
                            await _send_files_result(await cb_command_service.execute_command("thug", "", attachments=_imgs))
                    elif _action == "translate":
                        # Ask which language to translate the upload's text into.
                        await telegram_service.send_message(
                            chat_id, "🌐 Translate to which language?",
                            reply_markup=_media_translate_keyboard(),
                        )
                    elif _action.startswith("tr:"):
                        _lang = _action[3:].strip() or "english"
                        await telegram_service.send_message(chat_id, f"🌐 Translating to {_lang.title()}…")
                        # Shared helper: OCRs the upload and translates the FULL text
                        # (raised output budget so long pages don't get cut off).
                        _res = await cb_command_service._translate_command(_lang, attachments=_atts)
                        if _res.get("error") == "no_text":
                            # Almost always a Telegram-compressed photo (a tall screenshot
                            # gets shrunk too narrow to read) — point at the File workaround.
                            _txt = ("📸 Couldn't read any text in that image. Telegram compresses photos, "
                                    "so a tall screenshot gets shrunk too small to read.\n\n"
                                    "Send it as a *File* (📎 attach → File) instead of a photo for full "
                                    "resolution, then tap 🌐 Translate.")
                        else:
                            _txt = _res.get("content", "Translation failed.")
                        await telegram_service.send_message(chat_id, _txt)
                except Exception as _media_err:
                    logger.error(f"Media action '{_action}' failed: {_media_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Failed: {_media_err}")

            elif data.startswith("n:"):
                # Nyaa inline button
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "search_hint":
                    await telegram_service.send_message(
                        chat_id,
                        "🔎 Type your anime search:",
                        reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": "e.g. one piece 1080p"}
                    )
                    return {"ok": True}

                if action == "dl" and len(parts) >= 3:
                    nyaa_arg = f"download {parts[2]}"
                else:
                    return {"ok": True}

                try:
                    cb_command_service = CommandService(db, user=cb_user)
                    cb_result = await cb_command_service.execute_command("nyaa", nyaa_arg)
                    cb_content = _strip_cmd_links(cb_result.get("content", ""))
                    cb_reply_markup = {"inline_keyboard": [[
                        {"text": "🔎 New Nyaa Search", "callback_data": "n:search_hint:0"},
                        {"text": "📋 Active Downloads", "callback_data": "t:list:0"},
                    ]]}
                    await telegram_service.send_message(chat_id, cb_content, reply_markup=cb_reply_markup)
                except Exception as cb_err:
                    logger.error(f"Nyaa callback error: {cb_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"Error: {cb_err}")

            elif data.startswith("4c:"):
                # 4chan inline button
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                # 4chan buttons carry numeric offsets / thread ids in parts[3:]. A stale or
                # tampered button with a non-numeric value would raise ValueError on int() —
                # bail gracefully instead.
                if any(p and not p.lstrip("-").isdigit() for p in parts[3:]):
                    await telegram_service.answer_callback_query(
                        callback_query_id, text="That button is no longer valid — reopen the menu.",
                        show_alert=True)
                    return {"ok": True}

                if action == "select":
                    # Show board selector
                    await telegram_service.send_message(
                        chat_id,
                        "🍀 *4chan Board Selector*\n\nChoose a board to browse:",
                        reply_markup=_4chan_initial_keyboard()
                    )
                    return {"ok": True}

                elif action == "board" and len(parts) >= 3:
                    board = parts[2]
                    offset = int(parts[3]) if len(parts) >= 4 else 0
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_catalog(chat_id, board, user_id, offset=offset)
                    return {"ok": True}

                elif action == "catalognext" and len(parts) >= 4:
                    board = parts[2]
                    offset = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_catalog(chat_id, board, user_id, offset=offset)
                    return {"ok": True}

                elif action == "catalogprev" and len(parts) >= 4:
                    board = parts[2]
                    offset = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_catalog(chat_id, board, user_id, offset=offset)
                    return {"ok": True}

                elif action == "thread" and len(parts) >= 4:
                    board = parts[2]
                    thread_id = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_thread(chat_id, board, thread_id, user_id)
                    return {"ok": True}

                elif action == "summarize" and len(parts) >= 4:
                    board = parts[2]
                    thread_id = int(parts[3])
                    user_id = cb_user.id if cb_user else 0
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, summarize=True)
                    return {"ok": True}

                elif action == "refreshthread" and len(parts) >= 4:
                    board = parts[2]
                    thread_id = int(parts[3])
                    offset = int(parts[4]) if len(parts) >= 5 else 0
                    user_id = cb_user.id if cb_user else 0
                    # Send loading message
                    await telegram_service.send_message(chat_id, "🔄 Refreshing thread...")
                    # Reload the thread at current offset
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, offset=offset)
                    return {"ok": True}

                elif action == "nextpage" and len(parts) >= 5:
                    board = parts[2]
                    thread_id = int(parts[3])
                    offset = int(parts[4])
                    user_id = cb_user.id if cb_user else 0
                    # Load next page
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, offset=offset)
                    return {"ok": True}

                elif action == "prevpage" and len(parts) >= 5:
                    board = parts[2]
                    thread_id = int(parts[3])
                    offset = int(parts[4])
                    user_id = cb_user.id if cb_user else 0
                    # Load previous page
                    await _send_4chan_thread(chat_id, board, thread_id, user_id, offset=offset)
                    return {"ok": True}

            elif data.startswith("news:"):
                # News source selection callback
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}

                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "menu":
                    # Show news main menu
                    await telegram_service.send_message(
                        chat_id,
                        "📰 *News Menu*\n\nChoose an option:",
                        reply_markup=_news_menu_keyboard()
                    )
                    return {"ok": True}

                elif action == "select":
                    # Fetch user's news sources and show selector
                    from app.routers.news import get_user_news_sources
                    sources = get_user_news_sources(cb_user, db)
                    if not sources:
                        await telegram_service.send_message(
                            chat_id,
                            "📰 *News Sources*\n\nNo news sources configured.\n\nAdd sources in User Settings → News Sources."
                        )
                        return {"ok": True}
                    await _send_news_source_selector(chat_id, sources)
                    return {"ok": True}

                elif action == "config_hint":
                    await telegram_service.send_message(
                        chat_id,
                        "⚙️ *Configure News Sources*\n\nTo add or manage news sources:\n1. Open the Web UI\n2. Go to User Settings\n3. Click on 'News Sources'\n\nYou can add RSS feeds or news websites there."
                    )
                    return {"ok": True}

                elif action == "all":
                    # Fetch news from all sources
                    cb_command_service = CommandService(db, user=cb_user)
                    result = await cb_command_service.execute_command("news", "")
                    content = _strip_cmd_links(result.get("content", ""))
                    
                    # Parse articles and add buttons
                    has_social = _has_misskey(cb_user) or _has_pleroma(cb_user) or _has_matrix(cb_user)

                    articles = _split_news_into_articles(content)
                    if articles:
                        # Cache (title, url) pairs for the Post callbacks
                        _news_post_cache[chat_id] = [(title, url) for (_, title, url, _) in articles]

                        # Send header (date/source summary line) if present
                        header_match = re.match(r'^(##[^\n]+)', content)
                        if header_match:
                            await telegram_service.send_message(chat_id, header_match.group(1))

                        # Send each article as its own message with buttons
                        for i, (_, title, url, msg_text) in enumerate(articles[:10], 1):
                            buttons = []
                            if has_social:
                                buttons.append([
                                    {"text": "📝 Summarize", "callback_data": f"news:summarize:{i}"},
                                    {"text": "📣 Post", "callback_data": f"nk:post:{i}"}
                                ])
                            else:
                                buttons.append([
                                    {"text": "📝 Summarize", "callback_data": f"news:summarize:{i}"}
                                ])
                            kbd = {"inline_keyboard": buttons}
                            await telegram_service.send_message(chat_id, msg_text, reply_markup=kbd)
                    else:
                        # Fallback: no articles parsed — send raw content
                        await telegram_service.send_message(chat_id, content)
                    return {"ok": True}

                elif action == "source" and len(parts) >= 3:
                    # Fetch news from specific source
                    try:
                        source_idx = int(parts[2]) - 1  # Convert to 0-based index
                        sources = _news_source_cache.get(chat_id, [])
                        if 0 <= source_idx < len(sources):
                            source_name = sources[source_idx].get("name", "")
                            cb_command_service = CommandService(db, user=cb_user)
                            result = await cb_command_service.execute_command("news", source_name)
                            content = _strip_cmd_links(result.get("content", ""))

                            has_social = _has_misskey(cb_user) or _has_pleroma(cb_user) or _has_matrix(cb_user)

                            articles = _split_news_into_articles(content)
                            if articles:
                                # Cache (title, url) pairs for the Post callbacks
                                _news_post_cache[chat_id] = [(title, url) for (_, title, url, _) in articles]

                                # Send header (date/source summary line) if present
                                header_match = re.match(r'^(##[^\n]+)', content)
                                if header_match:
                                    await telegram_service.send_message(chat_id, header_match.group(1))

                                # Send each article as its own message with buttons
                                for i, (_, title, url, msg_text) in enumerate(articles[:10], 1):
                                    buttons = []
                                    if has_social:
                                        buttons.append([
                                            {"text": "📝 Summarize", "callback_data": f"news:summarize:{i}"},
                                            {"text": "📣 Post", "callback_data": f"nk:post:{i}"}
                                        ])
                                    else:
                                        buttons.append([
                                            {"text": "📝 Summarize", "callback_data": f"news:summarize:{i}"}
                                        ])
                                    kbd = {"inline_keyboard": buttons}
                                    await telegram_service.send_message(chat_id, msg_text, reply_markup=kbd)
                            else:
                                # Fallback: no articles parsed — send raw content
                                await telegram_service.send_message(chat_id, content)
                        else:
                            await telegram_service.send_message(chat_id, "❌ Source not found. Please try again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid source selection.")
                    return {"ok": True}

                elif action == "summarize" and len(parts) >= 3:
                    # Summarize a news article
                    try:
                        article_idx = int(parts[2]) - 1  # Convert to 0-based index
                        cached_articles = _news_post_cache.get(chat_id, [])
                        if 0 <= article_idx < len(cached_articles):
                            title, url = cached_articles[article_idx]
                            await telegram_service.send_message(chat_id, f"📝 Summarizing article...")
                            # Use AI to summarize
                            chat_service = ChatService(db, user=cb_user)
                            messages = [
                                {"role": "system", "content": "Summarize the following news article in 2-3 sentences. Be concise and factual."},
                                {"role": "user", "content": f"Title: {title}\nURL: {url}\n\nPlease summarize this article."}
                            ]
                            summary = await chat_service.chat(messages)
                            await telegram_service.send_message(
                                chat_id,
                                f"📝 *Summary*\n\n*{title}*\n\n{summary}\n\n[Read full article]({url})"
                            )
                        else:
                            await telegram_service.send_message(chat_id, "❌ Article not found. Please fetch news again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid article selection.")
                    return {"ok": True}

                elif action == "post" and len(parts) >= 3:
                    # Generate social media post for a news article
                    try:
                        article_idx = int(parts[2]) - 1  # Convert to 0-based index
                        cached_articles = _news_post_cache.get(chat_id, [])
                        if 0 <= article_idx < len(cached_articles):
                            title, url = cached_articles[article_idx]
                            await telegram_service.send_message(chat_id, f"📣 Generating social media post...")
                            # Use AI to generate post
                            chat_service = ChatService(db, user=cb_user)
                            messages = [
                                {"role": "system", "content": "Generate a short, engaging social media post (under 280 characters) for this news article. Use emojis but no hashtags."},
                                {"role": "user", "content": f"Title: {title}\nURL: {url}\n\nGenerate a social media post."}
                            ]
                            post_text = _strip_hashtags(await chat_service.chat(messages))
                            await _offer_social_post(chat_id, post_text, cb_user, telegram_service)
                        else:
                            await telegram_service.send_message(chat_id, "❌ Source not found. Please try again.")
                    except (ValueError, IndexError):
                        await telegram_service.send_message(chat_id, "❌ Invalid source selection.")
                    return {"ok": True}

            elif data.startswith("fin:"):
                # Finance buttons: fin:pay:<bill_id> | fin:refresh | fin:add
                # | fin:addincome | fin:bills:<unpaid|paid|all>
                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""
                message_id = (callback_query.get("message") or {}).get("message_id")
                cb_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not cb_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                elif action == "add":
                    await telegram_service.send_message(
                        chat_id,
                        "💰 Add a bill — reply: name amount",
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "e.g. Rent 1200"},
                    )
                elif action == "addincome":
                    await telegram_service.send_message(
                        chat_id,
                        _FIN_INCOME_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "e.g. Paycheck 3000"},
                    )
                elif action == "bills" and len(parts) > 2:
                    await _send_bills_list(chat_id, cb_user, db, parts[2], message_id=message_id)
                elif action == "refresh":
                    await _send_budget(chat_id, cb_user, db, message_id=message_id)
                elif action == "pay" and len(parts) > 2:
                    from app.services import finance_service
                    bill = _finance_bills_cache.get(chat_id, {}).get(parts[2])
                    if not bill:
                        await telegram_service.answer_callback_query(
                            callback_query_id, text="Bill list expired — tap Refresh.", show_alert=True)
                    else:
                        try:
                            base, key = finance_service.get_config(db, cb_user)
                            res = await finance_service.pay_bill(base, key, bill["name"])
                            await telegram_service.answer_callback_query(
                                callback_query_id, text=res.get("message", "Paid."))
                        except finance_service.FinanceError as e:
                            await telegram_service.answer_callback_query(
                                callback_query_id, text=str(e), show_alert=True)
                        await _send_budget(chat_id, cb_user, db, message_id=message_id)

            elif data.startswith("prompt:"):
                action = data.split(":", 1)[1]
                _PROMPT_CONFIGS = {
                    "search":   ("🔍 What would you like to search for?", "e.g. latest AI news"),
                    "images":   ("🖼 What images would you like to search for?", "e.g. northern lights"),
                    "geni":     ("🎨 Describe the image you want to generate:", "e.g. a sunset over a cyberpunk city"),
                    "nyaa":     ("🔎 Type your anime search:", "e.g. one piece 1080p"),
                    "torrents": ("🔍 Type your torrent search:", "e.g. dark knight 1080p"),
                    "4chan":    ("🍀 Which board? (g, pol, a, or h)", "e.g. g"),
                    "screenshot": ("📸 Send the URL to screenshot:", "e.g. example.com"),
                }
                cfg = _PROMPT_CONFIGS.get(action)
                if cfg:
                    prompt_text, placeholder = cfg
                    await telegram_service.send_message(
                        chat_id,
                        prompt_text,
                        reply_markup={"force_reply": True, "selective": True, "input_field_placeholder": placeholder},
                    )

            elif data.startswith("help:"):
                section = data.split(":", 1)[1]
                section_text = _HELP_SECTIONS.get(section)
                back_button = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "help:menu"}]]}
                if section == "menu":
                    await telegram_service.send_message(
                        chat_id,
                        "🤖 *PosterChanAI Help*\n\nTap any button below to learn about a feature:",
                        parse_mode="MarkdownV2",
                        reply_markup=_help_main_keyboard(),
                    )
                elif section == "finance":
                    # Open the interactive budget directly instead of showing help text,
                    # so finance is fully button-driven from the help menu.
                    cb_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    if cb_user:
                        await _send_budget(chat_id, cb_user, db)
                    else:
                        await telegram_service.send_message(
                            chat_id,
                            "Your Telegram account is not linked.",
                            reply_markup=back_button,
                        )
                elif section == "logs":
                    # Execute the logs command directly instead of showing help text
                    cb_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    if cb_user:
                        cb_command_service = CommandService(db, user=cb_user)
                        try:
                            result = await cb_command_service.execute_command("logs", "")
                            await telegram_service.send_message(
                                chat_id,
                                result.get("content", "No logs available."),
                                reply_markup=back_button,
                            )
                        except Exception as logs_err:
                            logger.error(f"Logs command error: {logs_err}", exc_info=True)
                            await telegram_service.send_message(
                                chat_id,
                                f"Error fetching logs: {logs_err}",
                                reply_markup=back_button,
                            )
                    else:
                        await telegram_service.send_message(
                            chat_id,
                            "Your Telegram account is not linked.",
                            reply_markup=back_button,
                        )
                elif section_text:
                    await telegram_service.send_message(
                        chat_id,
                        section_text,
                        parse_mode="MarkdownV2",
                        reply_markup=back_button,
                    )

            elif data.startswith("lnk:"):
                action = data.split(":", 1)[1]
                cached_url = _link_action_cache.pop(chat_id, None)

                # If cache missed (e.g. after a server restart), try to recover URL from
                # the button message text (forwarded-link prompts embed the URL there).
                if cached_url is None and action != "cancel":
                    from app.services.search_service import SearchService as _SS
                    _msg_text = (callback_query.get("message") or {}).get("text", "")
                    _recovered = _SS.extract_urls(_msg_text)
                    if _recovered:
                        cached_url = _recovered[0]
                        logger.info(f"lnk:{action} - recovered URL from message text: {cached_url}")

                if action == "cancel" or cached_url is None:
                    if action != "cancel":
                        await telegram_service.send_message(chat_id, "No pending link found. Please send the URL again.")
                    return {"ok": True}

                lnk_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if action == "summary":
                    await telegram_service.send_message(chat_id, "⏳ Fetching and summarizing link, please wait...")
                    try:
                        import asyncio as _asyncio
                        title, content, err = await _link_content_for_llm(db, cached_url)
                        if content:
                            content = content[:4000]
                            lnk_chat = ChatService(db, user=lnk_user)
                            summary_msgs = [
                                {"role": "system", "content": "You are a thorough summarizer. Output only the summary, nothing else. No introductions or meta-commentary."},
                                {"role": "user", "content": f"Title: {title}\n\n{content}\n\nWrite a detailed summary of the above. Include the key points, important facts, context, and any notable details. Use bullet points where helpful."}
                            ]
                            summary = await _asyncio.wait_for(lnk_chat.chat(summary_msgs), timeout=120)
                            await telegram_service.send_message(chat_id, summary)
                        else:
                            # No real content -> do NOT let the model invent a summary.
                            await telegram_service.send_message(chat_id, f"Could not fetch content from the URL. ({err})")
                    except _asyncio.TimeoutError:
                        await telegram_service.send_message(chat_id, "Timed out fetching or summarizing the link.")
                    except Exception as lnk_err:
                        logger.error(f"Link summary error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error: {lnk_err}")

                elif action == "flashcards":
                    # Flashcards from the page's REAL fetched text (same source as Summary) — never
                    # OCR a screenshot, so proper nouns/numbers stay correct (no hallucination).
                    if not lnk_user:
                        await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                        return {"ok": True}
                    await telegram_service.send_message(chat_id, "🎴 Reading the page and generating flashcards…")
                    try:
                        from app.services import flashcards_service
                        title, content, err = await _link_content_for_llm(db, cached_url)
                        if not content:
                            # No real text (e.g. a JS-only page or a video) — refuse rather than invent.
                            await telegram_service.send_message(chat_id, f"Couldn't read that link to make flashcards. ({err})")
                            return {"ok": True}
                        src = f"{title}\n\n{content}" if title else content
                        cards = await flashcards_service.generate_flashcards(src, ChatService(db, user=lnk_user))
                        if cards:
                            _deck = {"title": title or "Flashcards", "cards": cards, "idx": 0,
                                     "answered": [None] * len(cards), "score": 0, "ts": time.time()}
                            _flashcard_decks_cache[chat_id] = _deck
                            await _send_flashcard(chat_id, _deck)
                        else:
                            await telegram_service.send_message(chat_id, "Couldn't make flashcards from that page.")
                    except Exception as lnk_err:
                        logger.error(f"Link flashcards error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error: {lnk_err}")

                elif action == "post":
                    await telegram_service.send_message(chat_id, "⏳ Generating post, please wait...")
                    try:
                        import asyncio as _asyncio
                        title, content, err = await _link_content_for_llm(db, cached_url)
                        if not content:
                            # No real content (e.g. a YouTube video with no captions) -> refuse
                            # rather than letting the model invent a post from the bare URL.
                            await telegram_service.send_message(chat_id, f"Couldn't read that link to write a post. ({err})")
                            return {"ok": True}
                        article_context = f"Title: {title}\n\n{content[:3000]}"

                        post_messages = [
                            {
                                "role": "system",
                                "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Write a viral and engaging social media post based on this content. "
                                    "Be detailed — include key facts, context, and why it matters. "
                                    "Use emojis.\n\n"
                                    f"Content:\n{article_context}"
                                )
                            }
                        ]

                        lnk_chat = ChatService(db, user=lnk_user)
                        lnk_chat.num_predict = min(lnk_chat.num_predict, 900)
                        post_text = await _asyncio.wait_for(lnk_chat.chat(post_messages), timeout=120)
                        post_text = _strip_hashtags(post_text).rstrip() + f"\n\n{cached_url}"
                        await _offer_social_post(chat_id, post_text, lnk_user, telegram_service)
                    except _asyncio.TimeoutError:
                        await telegram_service.send_message(chat_id, "Timed out generating post.")
                    except Exception as lnk_err:
                        logger.error(f"Link post generation error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error generating post: {lnk_err}")

                elif action == "screenshot":
                    await telegram_service.send_message(chat_id, "⏳ Capturing screenshot, please wait...")
                    try:
                        lnk_cmd_service = CommandService(db, user=lnk_user)
                        shot_result = await lnk_cmd_service.execute_command("screenshot", cached_url)
                        if shot_result.get("type") == "generated_image" and shot_result.get("image"):
                            await _send_screenshot(chat_id, shot_result["image"], shot_result.get("content", cached_url))
                        else:
                            # error text from the command (e.g. Firefox missing / capture failed)
                            await telegram_service.send_message(chat_id, shot_result.get("content", "Screenshot failed."))
                    except Exception as lnk_err:
                        logger.error(f"Link screenshot error: {lnk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"Error capturing screenshot: {lnk_err}")

            elif data.startswith("yt:"):
                action = data.split(":", 1)[1]
                yt_url = _youtube_action_cache.pop(chat_id, None)

                if yt_url is None:
                    await telegram_service.send_message(chat_id, "No pending YouTube URL found.")
                    return {"ok": True}

                yt_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if action == "summary":
                    await telegram_service.send_message(chat_id, "⏳ Summarizing video, please wait...")
                    try:
                        yt_cmd_service = CommandService(db, user=yt_user)
                        yt_result = await yt_cmd_service.execute_command("yt", yt_url)
                        await telegram_service.send_message(chat_id, yt_result.get("content", "Error generating summary."))
                    except Exception as yt_err:
                        logger.error(f"YouTube summary callback error: {yt_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")

                elif action == "post":
                    # Generate a social media post for the YouTube video
                    await telegram_service.send_message(chat_id, "⏳ Generating social media post...")
                    try:
                        from app.services.youtube_service import fetch_video_info
                        import asyncio as _asyncio
                        
                        # Fetch video info for context
                        video_info = await _asyncio.wait_for(
                            fetch_video_info(yt_url),
                            timeout=15
                        )
                        
                        if video_info and video_info.get("title"):
                            video_context = f"Title: {video_info.get('title')}\n\n"
                            if video_info.get("description"):
                                desc = video_info.get("description", "")[:1000]
                                video_context += f"Description: {desc}\n\n"
                            if video_info.get("channel"):
                                video_context += f"Channel: {video_info.get('channel')}\n"
                        else:
                            video_context = f"YouTube Video: {yt_url}"
                        
                        # Generate social media post
                        yt_chat = ChatService(db, user=yt_user)
                        yt_chat.num_predict = min(yt_chat.num_predict, 900)
                        post_messages = [
                            {
                                "role": "system",
                                "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Write a viral and engaging social media post for this YouTube video. "
                                    "Be detailed — include key facts, context, and why it matters. "
                                    "Use emojis.\n\n"
                                    f"Content:\n{video_context}"
                                )
                            }
                        ]
                        post_text = await yt_chat.chat(post_messages)
                        post_text = _strip_hashtags(post_text).rstrip() + f"\n\n{yt_url}"
                        await _offer_social_post(chat_id, post_text, yt_user, telegram_service)
                    except Exception as yt_err:
                        logger.error(f"YouTube post generation error: {yt_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Error generating post: {yt_err}")

                elif action in ("mp3", "video"):
                    from app.services.youtube_service import (
                        check_ytdlp_available,
                        download_as_mp3,
                    )
                    import tempfile, shutil, os as _os, asyncio as _asyncio

                    if not check_ytdlp_available():
                        await telegram_service.send_message(chat_id, "❌ yt-dlp is not installed on the server.")
                        return {"ok": True}

                    if action == "mp3":
                        await telegram_service.send_message(chat_id, "⏳ Downloading MP3, please wait...")
                        from app.models import Setting as _Setting
                        _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
                        _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
                        if _cookies_path and not _os.path.isfile(_cookies_path):
                            _cookies_path = None
                        _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
                        _no_ssl = (
                            str(_ssl_s.value).strip().lower() in ("true", "1", "yes")
                            if _ssl_s and _ssl_s.value else False
                        )
                        temp_dir = tempfile.mkdtemp(prefix="tg_ytdl_")
                        try:
                            dl_result = await _asyncio.to_thread(
                                download_as_mp3, yt_url, temp_dir, _cookies_path, _no_ssl
                            )
                            if not dl_result.success:
                                await telegram_service.send_message(chat_id, f"❌ Download failed: {dl_result.error}")
                                return {"ok": True}
                            file_size = _os.path.getsize(dl_result.local_path)
                            if file_size > 50 * 1024 * 1024:
                                await telegram_service.send_message(
                                    chat_id,
                                    f"❌ File too large to send via Telegram ({file_size // (1024*1024)} MB). Limit is 50 MB."
                                )
                                return {"ok": True}
                            duration_int = int(dl_result.duration) if dl_result.duration else None
                            await telegram_service.send_audio(
                                chat_id=chat_id,
                                file_path=dl_result.local_path,
                                title=dl_result.title,
                                performer=dl_result.artist,
                                duration=duration_int,
                            )
                        except Exception as yt_err:
                            logger.error(f"YouTube MP3 callback error: {yt_err}", exc_info=True)
                            await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")
                        finally:
                            shutil.rmtree(temp_dir, ignore_errors=True)

                    else:  # video
                        await telegram_service.send_message(chat_id, "⏳ Downloading video, please wait...")
                        import tempfile as _tempfile
                        import shutil as _shutil
                        import os as _os
                        from app.services.youtube_service import download_as_video

                        temp_dir = _tempfile.mkdtemp(prefix="tg_ytdlvideo_")
                        try:
                            from app.models import Setting as _Setting
                            _cookies_s = db.query(_Setting).filter(_Setting.key == "ytdl_cookies_path").first()
                            _cookies_path = str(_cookies_s.value).strip() if _cookies_s and _cookies_s.value else None
                            if _cookies_path and not _os.path.isfile(_cookies_path):
                                _cookies_path = None
                            _ssl_s = db.query(_Setting).filter(_Setting.key == "ytdl_no_ssl_verify").first()
                            _no_ssl = (
                                str(_ssl_s.value).strip().lower() in ("true", "1", "yes")
                                if _ssl_s and _ssl_s.value else False
                            )

                            dl_result = await _asyncio.to_thread(
                                download_as_video, yt_url, temp_dir, "best", _cookies_path, _no_ssl
                            )
                            if not dl_result.success:
                                await telegram_service.send_message(chat_id, f"❌ Download failed: {dl_result.error}")
                                return {"ok": True}

                            # Offer Send / Compress / Clip / Clip+Compress (same as the
                            # `ytdl video` command), so a long video can be trimmed/shrunk
                            # instead of bouncing off Telegram's 50 MB send limit.
                            await _offer_ytdl_video_actions(chat_id, dl_result, yt_url, yt_user, db)
                        except Exception as yt_err:
                            logger.error(f"YouTube video callback error: {yt_err}", exc_info=True)
                            await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")
                        finally:
                            _shutil.rmtree(temp_dir, ignore_errors=True)

            elif data.startswith("nk:"):
                # News → Post to Misskey: nk:post:<article_number>
                parts = data.split(":")
                action = parts[1] if len(parts) > 1 else ""

                if action == "post" and len(parts) >= 3:
                    try:
                        article_num = int(parts[2])
                    except ValueError:
                        return {"ok": True}

                    cached_articles = _news_post_cache.get(chat_id)
                    if not cached_articles or article_num < 1 or article_num > len(cached_articles):
                        await telegram_service.send_message(chat_id, "⚠️ News article not found. Fetch the news again and try.")
                        return {"ok": True}

                    title, url = cached_articles[article_num - 1]

                    nk_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()

                    if not nk_user or (not _has_misskey(nk_user) and not _has_pleroma(nk_user) and not _has_matrix(nk_user)):
                        await telegram_service.send_message(chat_id, "⚠️ No social platform (Misskey, Pleroma, or Matrix) configured on your account.")
                        return {"ok": True}

                    await telegram_service.send_message(chat_id, f"⏳ Generating social media post for: {title}")

                    try:
                        from app.services.search_service import SearchService as _SS
                        import asyncio as _asyncio
                        _ss = _SS(db)
                        fetched = await _asyncio.wait_for(_ss.fetch_urls([url], max_urls=1), timeout=15)
                        article_context = f"Title: {title}\n\n{url}"
                        if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                            article_context = f"Title: {fetched[0].get('title', title)}\n\n{fetched[0]['content'][:3000]}"

                        nk_chat = ChatService(db, user=nk_user)
                        nk_chat.num_predict = min(nk_chat.num_predict, 900)
                        post_messages = [
                            {
                                "role": "system",
                                "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                            },
                            {
                                "role": "user",
                                "content": (
                                    "Write a viral and engaging social media post based on this news article. "
                                    "Be detailed — include key facts, context, and why it matters. "
                                    "Use emojis.\n\n"
                                    f"Content:\n{article_context}"
                                )
                            }
                        ]
                        post_text = await nk_chat.chat(post_messages)
                        post_text = _strip_hashtags(post_text).rstrip() + f"\n\n{url}"
                        await _offer_social_post(chat_id, post_text, nk_user, telegram_service)
                    except Exception as nk_err:
                        logger.error(f"News social post generation error: {nk_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Error generating post: {nk_err}")

            elif data == "glow:textpost":
                # Render the pending post text as a glowing neon graphic, then re-offer
                # the SAME share buttons with it attached. Reuses the standard image
                # plumbing (_geni_image_cache, which every platform post handler reads),
                # so nothing about the existing post/share workflow changes — the text
                # body and platform targets are untouched, just an image gets added.
                _gp = (_misskey_post_cache.get(chat_id) or _pleroma_post_cache.get(chat_id)
                       or _matrix_post_cache.get(chat_id))
                if _gp in (None, _CONSUMED):
                    _gp = _recover_post_text(callback_query) or None
                if not _gp:
                    await telegram_service.send_message(chat_id, "No post text to glow — generate a post first.")
                    return {"ok": True}
                _gu = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True,
                ).first()
                # A URL baked into the glow image is useless (not clickable) — keep links OUT of
                # the image and put them in the post body instead.
                import re as _re
                _glow_urls = _re.findall(r'https?://\S+', _gp)
                _glow_text = _re.sub(r'https?://\S+', '', _gp).strip()
                try:
                    from app.services import effects_service as _fx
                    _glow_png = await asyncio.to_thread(_fx.render_glow_text_card, _glow_text or _gp)
                except Exception as _ge:
                    logger.error(f"glow text card failed: {_ge}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Couldn't render the glowing text: {_ge}")
                    return {"ok": True}
                # send_photo takes a str (URL/path/base64), not raw bytes — encode for the
                # preview. _offer_social_post keeps the RAW bytes (the platform post
                # handlers attach _geni_image_cache as raw image_bytes).
                import base64 as _b64
                await telegram_service.send_photo(
                    chat_id, _b64.b64encode(_glow_png).decode("ascii"), "🌟 Glowing text preview")
                # The glowing TEXT is now the image; keep any link(s) in the post body so they
                # stay clickable (don't re-post the text — it's in the image).
                _glow_body = "\n".join(_glow_urls)
                await _offer_social_post(chat_id, _glow_body, _gu, telegram_service,
                                         prompt="📣 *Post this glowing image?*", image_bytes=_glow_png)
                return {"ok": True}

            elif data.startswith("mk:"):
                action = data.split(":", 1)[1]

                if action == "skip":
                    # Clear all social post caches so stale posts can't be sent
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    _geni_image_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post skipped.")
                    return {"ok": True}

                # action == "post"
                pending_post = _misskey_post_cache.pop(chat_id, None)
                if pending_post == _CONSUMED:
                    await telegram_service.send_message(chat_id, "Already posted via 'Post to All'.")
                    return {"ok": True}
                if pending_post is None:
                    pending_post = _recover_post_text(callback_query) or None
                if pending_post is None:
                    await telegram_service.send_message(chat_id, "No pending Misskey post found. Please generate a new post.")
                    return {"ok": True}

                mk_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if (
                    not mk_user
                    or not getattr(mk_user, "misskey_enabled", False)
                    or not getattr(mk_user, "misskey_instance_url", None)
                    or not getattr(mk_user, "misskey_api_token", None)
                ):
                    await telegram_service.send_message(chat_id, "Misskey is not configured on your account.")
                    return {"ok": True}

                _mk_image = _geni_image_cache.get(chat_id)  # .get so other platforms can still use it
                try:
                    from app.services.misskey_service import post_note as _misskey_post_note
                    await _misskey_post_note(
                        mk_user.misskey_instance_url,
                        mk_user.misskey_api_token,
                        pending_post,
                        image_bytes=_mk_image,
                    )
                    await telegram_service.send_message(chat_id, "✅ Posted to Misskey!")
                except Exception as mk_err:
                    logger.error(f"Misskey post error: {mk_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Failed to post to Misskey: {mk_err}")

            elif data.startswith("plr:"):
                action = data.split(":", 1)[1]

                if action == "skip":
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    _geni_image_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post skipped.")
                    return {"ok": True}

                # action == "post"
                pending_post = _pleroma_post_cache.pop(chat_id, None)
                if pending_post == _CONSUMED:
                    await telegram_service.send_message(chat_id, "Already posted via 'Post to All'.")
                    return {"ok": True}
                if pending_post is None:
                    pending_post = _recover_post_text(callback_query) or None
                if pending_post is None:
                    await telegram_service.send_message(chat_id, "No pending Pleroma post found. Please generate a new post.")
                    return {"ok": True}

                plr_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if not plr_user or not _has_pleroma(plr_user):
                    await telegram_service.send_message(chat_id, "Pleroma is not configured on your account.")
                    return {"ok": True}

                _plr_image = _geni_image_cache.get(chat_id)  # .get so other platforms can still use it
                try:
                    from app.services.pleroma_service import post_status as _pleroma_post_status
                    await _pleroma_post_status(
                        plr_user.pleroma_instance_url,
                        plr_user.pleroma_access_token,
                        pending_post,
                        image_bytes=_plr_image,
                    )
                    await telegram_service.send_message(chat_id, "✅ Posted to Pleroma!")
                except Exception as plr_err:
                    logger.error(f"Pleroma post error: {plr_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Failed to post to Pleroma: {plr_err}")

            elif data.startswith("mtx:"):
                # Matrix post flow:
                # mtx:post   → fetch rooms, show room selector
                # mtx:room:N → send to room N (index into _matrix_room_cache[chat_id])
                parts = data.split(":", 2)
                action = parts[1] if len(parts) > 1 else ""

                mtx_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                if action == "post":
                    pending_post = _matrix_post_cache.get(chat_id)
                    # Cache miss (e.g. after service restart) — recover from message text.
                    # "" is a valid caption-less media post, so only recover when truly absent.
                    if pending_post is None:
                        pending_post = _recover_post_text(callback_query) or None
                        if pending_post:
                            _matrix_post_cache[chat_id] = pending_post
                            logger.info(f"mtx:post — recovered post text from message ({len(pending_post)} chars)")
                    if pending_post is None:
                        await telegram_service.send_message(chat_id, "No pending Matrix post found. Please generate a new post.")
                        return {"ok": True}

                    if not mtx_user or not _has_matrix(mtx_user):
                        await telegram_service.send_message(chat_id, "Matrix is not configured on your account.")
                        return {"ok": True}

                    # Fetch rooms
                    try:
                        from app.services.matrix_service import get_joined_rooms as _mtx_rooms
                        rooms = await _mtx_rooms(mtx_user.matrix_homeserver, mtx_user.matrix_access_token)
                    except Exception as mtx_err:
                        logger.error(f"Matrix fetch rooms error: {mtx_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Could not fetch Matrix rooms: {mtx_err}")
                        return {"ok": True}

                    if not rooms:
                        await telegram_service.send_message(chat_id, "⚠️ No Matrix rooms found. Join a room first.")
                        return {"ok": True}

                    _matrix_room_cache[chat_id] = rooms

                    # Build room selector keyboard (up to 20 rooms, 2 per row)
                    buttons = []
                    row: list = []
                    for i, room in enumerate(rooms[:20]):
                        label = room["name"][:30]
                        row.append({"text": label, "callback_data": f"mtx:room:{i}"})
                        if len(row) == 2:
                            buttons.append(row)
                            row = []
                    if row:
                        buttons.append(row)
                    buttons.append([{"text": "❌ Cancel", "callback_data": "mtx:cancel"}])

                    await telegram_service.send_message(
                        chat_id,
                        "📬 Which Matrix room do you want to post to?",
                        reply_markup={"inline_keyboard": buttons},
                    )

                elif action == "room" and len(parts) >= 3:
                    try:
                        room_idx = int(parts[2])
                    except ValueError:
                        return {"ok": True}

                    # Pop only the matrix caches — leave Misskey/Pleroma caches intact
                    # so the user can still post to those platforms after choosing a Matrix room
                    pending_post = _matrix_post_cache.pop(chat_id, None)
                    rooms = _matrix_room_cache.pop(chat_id, [])

                    # "" is a valid caption-less media post; only bail when truly absent.
                    if pending_post is None:
                        await telegram_service.send_message(chat_id, "No pending Matrix post found.")
                        return {"ok": True}

                    if not rooms or room_idx < 0 or room_idx >= len(rooms):
                        await telegram_service.send_message(chat_id, "Room not found. Please try again.")
                        return {"ok": True}

                    if not mtx_user or not _has_matrix(mtx_user):
                        await telegram_service.send_message(chat_id, "Matrix is not configured on your account.")
                        return {"ok": True}

                    room = rooms[room_idx]
                    image_bytes = _geni_image_cache.pop(chat_id, None)
                    try:
                        if image_bytes:
                            from app.services.matrix_service import send_image as _mtx_send_img
                            await _mtx_send_img(
                                mtx_user.matrix_homeserver,
                                mtx_user.matrix_access_token,
                                room["room_id"],
                                image_bytes,
                                caption=pending_post,
                            )
                        else:
                            from app.services.matrix_service import send_message as _mtx_send
                            await _mtx_send(
                                mtx_user.matrix_homeserver,
                                mtx_user.matrix_access_token,
                                room["room_id"],
                                pending_post,
                            )
                        await telegram_service.send_message(chat_id, f"✅ Posted to Matrix room: {room['name']}")
                    except Exception as mtx_err:
                        logger.error(f"Matrix send error: {mtx_err}", exc_info=True)
                        await telegram_service.send_message(chat_id, f"❌ Failed to post to Matrix: {mtx_err}")

                elif action == "cancel":
                    _matrix_post_cache.pop(chat_id, None)
                    _matrix_room_cache.pop(chat_id, None)
                    _misskey_post_cache.pop(chat_id, None)
                    _pleroma_post_cache.pop(chat_id, None)
                    _geni_image_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Post cancelled.")

            elif data == "all:post":
                # Post to every configured platform simultaneously.
                # Misskey + Pleroma are posted right away; Matrix shows room selector.
                all_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                # Recover post text from message if caches were lost (e.g. service restart).
                # Shared _recover_post_text() strips the prompt and refuses to post a bare
                # prompt — same helper used by the individual mk:/plr:/mtx: handlers.

                results = []
                matrix_attempted = False

                _all_image = _geni_image_cache.get(chat_id)  # leave in cache for Matrix room-picker step

                # Misskey — post when there's text OR a media attachment (caption-less post).
                mk_post = _misskey_post_cache.pop(chat_id, None) or _recover_post_text(callback_query)
                if (mk_post or _all_image) and all_user and _has_misskey(all_user):
                    try:
                        from app.services.misskey_service import post_note as _mk_note
                        await _mk_note(all_user.misskey_instance_url, all_user.misskey_api_token, mk_post,
                                       image_bytes=_all_image)
                        results.append("✅ Misskey")
                    except Exception as _e:
                        logger.error(f"all:post Misskey error: {_e}", exc_info=True)
                        results.append(f"❌ Misskey: {_e}")
                    # Sentinel prevents old Misskey button from double-posting
                    _misskey_post_cache[chat_id] = _CONSUMED

                # Pleroma
                plr_post = _pleroma_post_cache.pop(chat_id, None) or _recover_post_text(callback_query)
                if (plr_post or _all_image) and all_user and _has_pleroma(all_user):
                    try:
                        from app.services.pleroma_service import post_status as _plr_status
                        await _plr_status(all_user.pleroma_instance_url, all_user.pleroma_access_token, plr_post,
                                          image_bytes=_all_image)
                        results.append("✅ Pleroma")
                    except Exception as _e:
                        logger.error(f"all:post Pleroma error: {_e}", exc_info=True)
                        results.append(f"❌ Pleroma: {_e}")
                    # Sentinel prevents old Pleroma button from double-posting
                    _pleroma_post_cache[chat_id] = _CONSUMED

                if results:
                    await telegram_service.send_message(chat_id, "\n".join(results))

                # Matrix — needs room selection; show picker if configured
                mtx_post = _matrix_post_cache.get(chat_id) or _recover_post_text(callback_query)
                if (mtx_post or _all_image) and mtx_post != _CONSUMED and all_user and _has_matrix(all_user):
                    matrix_attempted = True
                    try:
                        from app.services.matrix_service import get_joined_rooms as _mtx_rooms
                        rooms = await _mtx_rooms(all_user.matrix_homeserver, all_user.matrix_access_token)
                        if rooms:
                            _matrix_room_cache[chat_id] = rooms
                            if mtx_post != _matrix_post_cache.get(chat_id):
                                _matrix_post_cache[chat_id] = mtx_post
                            btns = []
                            row: list = []
                            for i, room in enumerate(rooms[:20]):
                                row.append({"text": room["name"][:30], "callback_data": f"mtx:room:{i}"})
                                if len(row) == 2:
                                    btns.append(row)
                                    row = []
                            if row:
                                btns.append(row)
                            btns.append([{"text": "❌ Skip Matrix", "callback_data": "mtx:cancel"}])
                            await telegram_service.send_message(
                                chat_id,
                                "📬 Which Matrix room?",
                                reply_markup={"inline_keyboard": btns},
                            )
                        else:
                            _matrix_post_cache.pop(chat_id, None)
                            await telegram_service.send_message(chat_id, "⚠️ No Matrix rooms found — skipped.")
                    except Exception as _e:
                        logger.error(f"all:post Matrix rooms error: {_e}", exc_info=True)
                        _matrix_post_cache.pop(chat_id, None)
                        await telegram_service.send_message(chat_id, f"❌ Matrix room fetch failed: {_e}")

                if not results and not matrix_attempted:
                    await telegram_service.send_message(chat_id, "No social platforms configured.")

            return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram callback_query handler error: {e}", exc_info=True)

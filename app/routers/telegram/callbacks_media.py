"""Auto-split from callbacks.py: media callback handlers. Bodies moved verbatim."""
from ._common import CommandService, User, _CLIP_START_PROMPT, _EFFECT_CAPTION_PROMPT, _FLASHCARD_TTL, _MEDIA_ACTION_TTL, _MEME_PROMPT, _SOCIAL_CAPTION_PROMPT, _clip_pending, _effect_caption_pending, _effect_char_pending, _flashcard_decks_cache, _media_action_cache, logger, telegram_service, time
from .keyboards import _character_prompt_keyboard, _media_action_keyboard, _media_effects_keyboard, _media_fx_characters_keyboard, _media_fx_memes_keyboard, _media_fx_sounds_keyboard, _media_fx_themes_keyboard, _media_translate_keyboard
from .senders import User, _deliver_files_result, _media_action_cache, _offer_ytdl_share, _send_flashcard, logger, telegram_service, time


async def _cb_mediafc(update, db, chat_id, data, callback_query, callback_query_id):
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


async def _cb_fc(update, db, chat_id, data, callback_query, callback_query_id):
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


async def _cb_media(update, db, chat_id, data, callback_query, callback_query_id):
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
            elif _action == "extractaudio":
                _vids = [a for a in _atts if is_video(a[0], a[2])]
                if not _vids:
                    await telegram_service.send_message(chat_id, "No video to extract audio from.")
                else:
                    await telegram_service.send_message(chat_id, "🎵 Extracting audio…")
                    await _send_files_result(await cb_command_service.execute_command("extractaudio", "", attachments=_vids), offer_share=False)
            elif _action == "circlecrop":
                _imgs = [a for a in _atts if is_image(a[0], a[2])]
                if not _imgs:
                    await telegram_service.send_message(chat_id, "No image to circle-crop.")
                else:
                    await telegram_service.send_message(chat_id, "⭕ Circle-cropping…")
                    await _send_files_result(await cb_command_service.execute_command("circlecrop", "", attachments=_imgs), offer_share=False)
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
            elif _action == "bill":
                # 🧾 Bill: read the attachment, then offer Remind / Cancel as BUTTONS. The command
                # form needs a second typed message (`bill add`) to confirm; from a photo you have
                # just tapped, another round of typing is the wrong shape.
                # NOTE: this can only set the REMINDER. The budget itself is encrypted to the user's
                # own Nostr key and writable only by their client (Discover → Budget), so there is
                # no "add to budget" this side of the wire any more — the reply text says so.
                _res = await cb_command_service.execute_command("bill", "", attachments=_atts)
                _txt = (_res or {}).get("content") or "Couldn't read that bill."
                # type == "bill" is exactly "a parse was staged"; matching on the message text would
                # break the moment that wording changes (it already did once).
                _staged = (_res or {}).get("type") == "bill"
                await telegram_service.send_message(
                    chat_id, _txt.replace("**", ""),
                    reply_markup=({"inline_keyboard": [[
                        {"text": "⏰ Set reminder", "callback_data": "media:billadd"},
                        {"text": "✖ Cancel", "callback_data": "media:billno"},
                    ]]} if _staged else None),
                )
            elif _action == "remind":
                # Screenshot -> reminder: same one-tap shape as Bill. No confirm step here — a wrong
                # reminder is a notification you dismiss, not a permanent row in your accounts.
                _res = await cb_command_service.execute_command("remind", "", attachments=_atts)
                await telegram_service.send_message(
                    chat_id, ((_res or {}).get("content") or "Couldn't read that.").replace("**", "").replace("_", ""))
            elif _action == "billadd":
                _res = await cb_command_service.execute_command("bill", "add", attachments=[])
                await telegram_service.send_message(chat_id, (_res or {}).get("content") or "Couldn't add it.")
            elif _action == "billno":
                await telegram_service.send_message(chat_id, "🧾 Not added.")
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
                    "characters": _media_fx_characters_keyboard,
                }.get(_cat)
                if not _cat_kbd:
                    await telegram_service.send_message(chat_id, "Unknown effects category.")
                elif not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to do — that upload has no image.")
                else:
                    _cat_label = {"themes": "📺 TV/Movie Themes", "sounds": "🔊 Sound clips",
                                  "memes": "🎨 Memes / overlays", "characters": "🧍 Characters"}[_cat]
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
                else:
                    # Left column = motion alone; right column = the same motion
                    # with the trippy hue-cycle layered on top (the only combo
                    # that composes — movements don't stack). 🌈 Trippy
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
                    # Hide the buttons check_motion_combo would refuse: a motion that repeats the
                    # effect itself (glow on glow), and `alive` (3D parallax on a still) for the
                    # effects that always output a video. The rest — zoom/shake/pulse/trippy —
                    # re-render an animated effect's real frames now, so they're offered for it.
                    _self = {"al": "alive", "gl": "glow"}
                    def _keep(b):
                        _code = b["callback_data"].split(":")[2]
                        if _self.get(_code) == _eff:
                            return False
                        return not (_code == "al" and _eff in CommandService.ANIMATED_EFFECTS)
                    _rows = [r for r in ([b for b in row if _keep(b)] for row in _rows) if r]
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
            elif _action == "heat":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "\U0001F525 It was the heat of the moment…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("heat", "", attachments=_imgs))
            elif _action == "whoabuddy":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🤠 Whoa buddy…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("whoabuddy", "", attachments=_imgs))
            elif _action == "diarrhea":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "💩 Explosive diarrhea…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("diarrhea", "", attachments=_imgs))
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
            elif _action == "makima":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to shoot at — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🔫 Makima…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("makima", "", attachments=_imgs))
            elif _action == "gura":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to pog at — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🦈 Gura…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("gura", "", attachments=_imgs))
            elif _action == "rebecca":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "👍 Rebecca…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("rebecca", "", attachments=_imgs))
            elif _action == "vibe":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "💖 Vibe…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("vibe", "", attachments=_imgs))
            elif _action == "feliz":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🎉 Feliz…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("feliz", "", attachments=_imgs))
            elif _action == "horse":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🐴 Horse…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("horse", "", attachments=_imgs))
            elif _action == "knightrider":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🚗 Knight Rider…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("knightrider", "", attachments=_imgs))
            elif _action == "hugebitch":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🗣️ Huge Bitch…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("hugebitch", "", attachments=_imgs))
            elif _action == "sleepwell":
                # No caption needed — render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "😴 Sleep Well…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("sleepwell", "", attachments=_imgs))
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
            elif _action == "jerry":
                # No caption needed — composite Jerry onto the image, render the video and post it.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to set to music — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "🎙️ Jerry…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("jerry", "", attachments=_imgs))
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
            elif _action == "uwu":
                # Animated overlay — run immediately and post the result.
                if not any(is_image(fn, ct) for fn, _, ct in _atts):
                    await telegram_service.send_message(chat_id, "Nothing to overlay — that upload has no image.")
                else:
                    await telegram_service.send_message(chat_id, "\U0001F97A uwu…")
                    _imgs = [a for a in _atts if is_image(a[0], a[2])]
                    await _send_files_result(await cb_command_service.execute_command("uwu", "", attachments=_imgs))
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

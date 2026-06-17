"""Auto-split from webhook.py: the message half of _handle_telegram_update."""
from ._common import ChatService, CommandService, Conversation, Message, User, _CLIP_END_PROMPT, _CLIP_START_PROMPT, _EFFECT_CAPTION_PROMPT, _FIN_INCOME_PROMPT, _MEDIA_ACTION_TTL, _MEDIA_GROUP_CACHE, _MEME_PROMPT, _SOCIAL_CAPTION_PROMPT, _clip_pending, _effect_caption_pending, _effect_char_pending, _flashcard_decks_cache, _link_action_cache, _matrix_post_cache, _media_action_cache, _misskey_post_cache, _news_post_cache, _pleroma_post_cache, _youtube_action_cache, asyncio, datetime, logger, re, telegram_service, time
from .keyboards import _4chan_initial_keyboard, _build_torrent_keyboard, _character_prompt_keyboard, _has_matrix, _has_misskey, _has_pleroma, _help_main_keyboard, _media_action_keyboard, _news_menu_keyboard, _split_news_into_articles, _strip_cmd_links, _strip_hashtags, _torrent_nav_keyboard, re
from .senders import User, _has_matrix, _has_misskey, _has_pleroma, _matrix_post_cache, _media_action_cache, _misskey_post_cache, _offer_social_post, _offer_ytdl_share, _offer_ytdl_video_actions, _pleroma_post_cache, _send_4chan_catalog, _send_active_torrents, _send_budget, _send_flashcard, _send_nyaa_results, _send_png_as_document, _send_screenshot, _send_torrent_results, _strip_cmd_links, _torrent_nav_keyboard, asyncio, datetime, logger, re, telegram_service, time


async def _handle_message(update, db):
    from app.services.chat_service import ChatService
    from .webhook import _make_tg_node_notify
    try:
        message = update.get("message")
        if message:
            
            chat_id = str(message.get("chat", {}).get("id"))
            # Get text OR caption (Telegram sends caption separately for photos)
            text = message.get("text", "") or message.get("caption", "")
            user = message.get("from", {})
            username = user.get("username", "unknown")
            
            # Check for reply_to_message (when user replies to a message)
            reply_to = message.get("reply_to_message", {})
            reply_text = reply_to.get("text", "") if reply_to else ""

            # Detect replies to bot ForceReply prompts and route them as commands.
            # We identify our prompts by their exact text content.
            _FORCE_REPLY_ROUTES = {
                "🔎 Type your anime search:": "nyaa",
                "🔍 Type your torrent search:": "torrents search",
                "🔍 What would you like to search for?": "search",
                "🖼 What images would you like to search for?": "images",
                "🎨 Describe the image you want to generate:": "geni",
                "📸 Send the URL to screenshot:": "screenshot",
                "💰 Add a bill — reply: name amount": "addbill",
            }
            reply_from = (reply_to or {}).get("from", {})
            if reply_from.get("is_bot") and text.strip():
                route = _FORCE_REPLY_ROUTES.get(reply_text.strip())
                if route:
                    text = f"{route} {text.strip()}"
                    text_lower = text.lower()
                    reply_to = {}
                    reply_text = ""
                elif reply_text.strip() == _FIN_INCOME_PROMPT:
                    # "💵 Add Income" button → reuse addbill with the income flag appended.
                    text = f"addbill {text.strip()} income"
                    text_lower = text.lower()
                    reply_to = {}
                    reply_text = ""

            # Reply to the "Post to social" caption prompt → attach the cached media
            # and show the platform buttons with the user's caption (or none if "-").
            if reply_from.get("is_bot") and reply_text.strip() == _SOCIAL_CAPTION_PROMPT:
                from app.services.media_service import is_image, is_video
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the file again.")
                    return {"ok": True}
                _atts = _entry["attachments"]
                _media = next((fd for fn, fd, ct in _atts if is_image(fn, ct)), None) \
                    or next((fd for fn, fd, ct in _atts if is_video(fn, ct)), None)
                if not _media:
                    await telegram_service.send_message(chat_id, "Nothing to post.")
                    return {"ok": True}
                _cap_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not _cap_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                _caption = text.strip()
                if _caption in ("-", "skip"):
                    _caption = ""
                await _offer_social_post(
                    chat_id, _caption, _cap_user, telegram_service,
                    prompt="📣 *Post this?*", image_bytes=_media,
                )
                return {"ok": True}

            # Reply to the "🖼 Meme" caption prompt → caption the cached image.
            if reply_from.get("is_bot") and reply_text.strip() == _MEME_PROMPT:
                from app.services.media_service import is_image
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the image again.")
                    return {"ok": True}
                _caption = text.strip()
                if not _caption:
                    await telegram_service.send_message(
                        chat_id, "⚠️ Empty caption. " + _MEME_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "TOP TEXT"},
                    )
                    return {"ok": True}
                _meme_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not _meme_user:
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                _atts = [a for a in _entry["attachments"] if is_image(a[0], a[2])]
                await telegram_service.send_message(chat_id, "🖼 Adding caption…")
                try:
                    _res = await CommandService(db, user=_meme_user).execute_command(
                        "meme", _caption, attachments=_atts
                    )
                    if _res.get("type") == "files":
                        # Send as a document (not send_photo): the meme is a JPEG, whose
                        # base64 starts with "/9j/" — send_photo would treat that as a
                        # file path and fail. send_document_bytes takes raw bytes.
                        for _f in _res.get("files", []):
                            if _f.get("data"):
                                await telegram_service.send_document_bytes(chat_id, _f["data"], _f.get("filename", "meme.jpg"))
                                await asyncio.sleep(0.15)
                    else:
                        await telegram_service.send_message(chat_id, _res.get("content", "Done."))
                except Exception as _meme_err:
                    logger.error(f"Meme failed: {_meme_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Meme failed: {_meme_err}")
                return {"ok": True}

            # Reply to the effect caption prompt → the motion was already chosen (held in
            # _effect_caption_pending["motion"]); render the effect with motion + caption.
            if reply_from.get("is_bot") and reply_text.strip() == _EFFECT_CAPTION_PROMPT:
                from app.services.media_service import is_image
                _pend = _effect_caption_pending.get(chat_id)
                _entry = _media_action_cache.get(chat_id)
                if not _pend or not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _effect_caption_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the image again.")
                    return {"ok": True}
                _cap = text.strip()
                if not _cap:
                    await telegram_service.send_message(
                        chat_id, "⚠️ Empty caption. " + _EFFECT_CAPTION_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "TOP TEXT"},
                    )
                    return {"ok": True}
                _eff = _pend["eff"]
                _motion = _pend.get("motion", "")
                _cap_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()
                if not _cap_user:
                    _effect_caption_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "Your Telegram account is not linked.")
                    return {"ok": True}
                # Caption captured — the character step is the FINAL one; render happens there.
                _effect_char_pending[chat_id] = {"eff": _eff, "motion": _motion, "caption": _cap, "ts": time.time()}
                _effect_caption_pending.pop(chat_id, None)
                await telegram_service.send_message(
                    chat_id, "🧸 Add a character (bottom-right)?",
                    reply_markup=_character_prompt_keyboard(),
                )
                return {"ok": True}

            # Interactive video-clip flow: replies to the start/end ForceReply prompts.
            # Handled here (before social-reply/command routing) since it spans two
            # steps and pulls the source video from the media-action cache.
            if reply_from.get("is_bot") and reply_text.strip() in (_CLIP_START_PROMPT, _CLIP_END_PROMPT):
                from app.services.media_service import parse_timecode, clip_attachment, is_video
                _val = parse_timecode(text.strip())
                if reply_text.strip() == _CLIP_START_PROMPT:
                    if _val is None:
                        await telegram_service.send_message(
                            chat_id, "⚠️ Couldn't read that time. " + _CLIP_START_PROMPT,
                            reply_markup={"force_reply": True, "selective": True,
                                          "input_field_placeholder": "0:10"},
                        )
                        return {"ok": True}
                    _clip_pending[chat_id] = {"start": _val, "ts": time.time()}
                    await telegram_service.send_message(
                        chat_id, _CLIP_END_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "0:30"},
                    )
                    return {"ok": True}

                # End-time reply → validate against the stored start, then clip.
                if _val is None:
                    await telegram_service.send_message(
                        chat_id, "⚠️ Couldn't read that time. " + _CLIP_END_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "0:30"},
                    )
                    return {"ok": True}
                _pending = _clip_pending.get(chat_id)
                if not _pending or (time.time() - _pending.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _clip_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That clip request expired — tap ✂️ Clip video again.")
                    return {"ok": True}
                _entry = _media_action_cache.get(chat_id)
                if not _entry or (time.time() - _entry.get("ts", 0)) > _MEDIA_ACTION_TTL:
                    _media_action_cache.pop(chat_id, None)
                    _clip_pending.pop(chat_id, None)
                    await telegram_service.send_message(chat_id, "⏳ That upload expired — please send the video again.")
                    return {"ok": True}
                _start = _pending["start"]
                if _val <= _start:
                    await telegram_service.send_message(
                        chat_id, "⚠️ The end time must be after the start. " + _CLIP_END_PROMPT,
                        reply_markup={"force_reply": True, "selective": True,
                                      "input_field_placeholder": "0:30"},
                    )
                    return {"ok": True}
                _clip_pending.pop(chat_id, None)
                _compress_after = bool(_entry.get("compress_after"))
                _is_ytdl = bool(_entry.get("ytdl"))  # offer a share prompt afterwards
                _entry.pop("compress_after", None)
                _atts = [a for a in _entry["attachments"] if is_video(a[0], a[2])]
                await telegram_service.send_message(chat_id, "✂️ Clipping…" + (" then compressing…" if _compress_after else ""))
                try:
                    _outs, _summary = await asyncio.to_thread(clip_attachment, _atts, _start, _val)
                    if not _outs:
                        await telegram_service.send_message(chat_id, _summary)
                    else:
                        # Optionally compress the clipped result (the "Clip + Compress" action).
                        if _compress_after:
                            from app.services.media_service import compress_attachments
                            _catts = [(f["filename"], f["data"], f["content_type"]) for f in _outs if f.get("data")]
                            _couts, _csummary = await asyncio.to_thread(compress_attachments, _catts)
                            if _couts:
                                _outs, _summary = _couts, f"{_summary}\n{_csummary}"
                        await telegram_service.send_message(chat_id, _summary)
                        for _f in _outs:
                            if _f.get("data"):
                                await telegram_service.send_document_bytes(chat_id, _f["data"], _f.get("filename", "clip.mp4"))
                                await asyncio.sleep(0.15)
                        # For a ytdl download, offer to post the trimmed/compressed result.
                        if _is_ytdl and _outs[0].get("data"):
                            await _offer_ytdl_share(chat_id, _outs[0].get("filename", "clip.mp4"), _outs[0]["data"], db)
                except Exception as _clip_err:
                    logger.error(f"Clip failed: {_clip_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, f"❌ Clip failed: {_clip_err}")
                return {"ok": True}

            # Reply to a forwarded social notification → post it back to that platform.
            # Checked before command/intent handling so the freeform reply isn't misread.
            _reply_msg_id = (reply_to or {}).get("message_id")
            if _reply_msg_id and text.strip() and reply_from.get("is_bot"):
                from app.services import social_notifications_service
                try:
                    _social_resp = await social_notifications_service.handle_reply(
                        db, chat_id, _reply_msg_id, text.strip()
                    )
                except Exception as _e:
                    logger.warning(f"[social] reply handling error: {_e}")
                    _social_resp = "❌ Failed to send reply."
                if _social_resp is not None:
                    await telegram_service.send_message(chat_id, _social_resp)
                    return {"ok": True}

            # Detect forwarded messages
            is_forwarded = bool(
                message.get("forward_date") or
                message.get("forward_origin") or
                message.get("forward_from") or
                message.get("forward_from_chat")
            )
            
            # Check for attachments (photos, documents, videos)
            # Photos in Telegram messages are in a list - get the highest res (last one)
            photos = message.get("photo", [])
            document = message.get("document", [])
            # Video / animation (GIF) attachments — used by the compress command
            video = message.get("video") or message.get("animation")
            
            logger.warning(f"TELEGRAM: text='{text}', reply_to='{reply_text[:50] if reply_text else ''}', photos={len(photos) if photos else 0}")
            
            # Strip /no_think prefix — it's a Qwen3 control token, not a user query.
            # If it appears verbatim in the message the model describes it instead of obeying it.
            # chat_service no longer injects /no_think unconditionally; strip_thinking_tags
            # already cleans thinking blocks from every response.
            if text.lower().startswith("/no_think"):
                text = text[len("/no_think"):].strip()
                if not text:
                    # User sent /no_think with no message — just confirm and wait for next message.
                    await telegram_service.send_message(
                        chat_id,
                        "✅ Got it — I'll respond directly without thinking.\n\nJust send your message now."
                    )
                    return {"ok": True}

            # Convert text to lowercase for command matching
            text_lower = text.lower().strip()

            # --- Authorization check ---
            # Allow /start <key> for account linking; block all other messages from unlinked users.
            _auth_user = db.query(User).filter(
                User.telegram_chat_id == chat_id,
                User.telegram_enabled == True
            ).first()

            if not _auth_user:
                if text.startswith("/start "):
                    import hmac
                    from sqlalchemy.exc import IntegrityError
                    key = text.replace("/start ", "").strip()
                    keyed_user = db.query(User).filter(User.telegram_key == key).first()
                    # Constant-time compare as defense-in-depth (DB already did the lookup)
                    key_valid = (
                        keyed_user is not None
                        and hmac.compare_digest(keyed_user.telegram_key or "", key)
                        and (
                            keyed_user.telegram_key_expires_at is None
                            or keyed_user.telegram_key_expires_at > datetime.utcnow()
                        )
                    )
                    if key_valid:
                        # Reject if this user is already linked to a different Telegram chat
                        if keyed_user.telegram_enabled and keyed_user.telegram_chat_id and keyed_user.telegram_chat_id != chat_id:
                            await telegram_service.send_message(
                                chat_id,
                                "This account is already linked to a different Telegram chat. Unlink it first from User Settings."
                            )
                            return {"ok": True}
                        try:
                            keyed_user.telegram_chat_id = chat_id
                            keyed_user.telegram_enabled = True
                            keyed_user.telegram_key = None
                            keyed_user.telegram_key_expires_at = None
                            db.commit()
                            await telegram_service.send_message(
                                chat_id,
                                f"Your Telegram account has been linked to {keyed_user.username}! You can now use the bot."
                            )
                        except IntegrityError:
                            db.rollback()
                            await telegram_service.send_message(
                                chat_id,
                                "This Telegram chat is already linked to a different user. Unlink it first from that account's settings."
                            )
                    else:
                        await telegram_service.send_message(
                            chat_id,
                            "Invalid or expired key. Please generate a new key from User Settings - Telegram and try again."
                        )
                else:
                    await telegram_service.send_message(
                        chat_id,
                        "Your Telegram account is not linked. Generate a key from User Settings - Telegram tab and send /start <key> to this bot."
                    )
                return {"ok": True}

            # Check if the message starts with a known command
            command = None
            arg = text
            commands = ["help", "new", "ytdl", "geni", "musicgeni", "videogeni", "narrate", "mail", "news", "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post", "share", "remind", "reminders", "pin", "pins", "removebackground", "compress", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "seth", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", "node", "budget", "finance", "bills", "pay", "addbill", "screenshot", "shot", "ss"]
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break
            # Telegram skips parse_command, so resolve aliases (e.g. shot/ss -> screenshot) to the
            # canonical name, or execute_command rejects them as "Unknown command".
            if command:
                command = CommandService.COMMAND_ALIASES.get(command, command)

            # "post" can appear anywhere in a short reply message (e.g. "send post", "make a post")
            if command is None and reply_to and len(text_lower.split()) <= 5 and "post" in text_lower:
                command = "post"
                # Only use words AFTER "post" as tone modifier (e.g. "post professional" → "professional")
                parts = text_lower.split("post", 1)
                arg = parts[1].strip() if len(parts) > 1 else ""

            # If it's a reply and translate command, handle it
            if reply_text and command == "translate":
                logger.warning(f"TRANSLATE: Processing reply with text: {reply_text[:100]}...")
                # Use the replied text for translation. Language = 1-2 words after an optional
                # leading "to", dropping any trailing instruction ("... and explain"). (Plain
                # arg.replace("to","") mangled words like "Esperanto".)
                _lm = re.match(r'^(?:to\s+)?([A-Za-z][A-Za-z\- ]*?)(?:\s+and\s+.*)?$',
                               arg.strip(), re.IGNORECASE)
                language = (_lm.group(1).strip().title() if _lm and _lm.group(1).strip() else "English")
                
                from app.services.chat_service import ChatService as FreshChatService
                fresh_chat_service = FreshChatService(db, user=None)
                
                translate_messages = [
                    {"role": "system", "content": f"Translate the following text to {language}. Output ONLY the translation, nothing else. Do NOT add any commentary, emojis, or persona."},
                    {"role": "user", "content": reply_text}
                ]
                
                try:
                    translated = await fresh_chat_service.chat(translate_messages)
                    logger.warning(f"TRANSLATE: Got translation: {translated[:100]}...")
                    result = {"type": "text", "content": f"## Translation to {language}\n\n{translated}"}
                except Exception as e:
                    logger.error(f"Translation error: {e}")
                    result = {"type": "text", "content": f"Translation failed: {str(e)}"}
                
                await telegram_service.send_message(chat_id, result.get("content", ""))
                logger.warning(f"TRANSLATE: Sent translation result")
                return {"ok": True}
            
            # post command: generate a social media post from a replied-to link
            if command == "post":
                if not reply_to and not text:
                    await telegram_service.send_message(chat_id, "Reply to a message and send `post` to generate a social media post, or `post raw` to share it exactly as written. You can add instructions too, e.g. `post professional` or `post don't include links`.")
                    return {"ok": True}

                # `post raw` / `post verbatim` shares the replied-to text AS-IS,
                # skipping the LLM rewrite. The keyword is consumed so it isn't
                # mistaken for a tone modifier (e.g. `post professional`).
                _arg_l = arg.strip().lower()
                verbatim = False
                _inline_after = ""
                for _kw in ("verbatim", "as-is", "as is", "asis", "raw", "exact", "exactly"):
                    if _arg_l == _kw or _arg_l.startswith(_kw + " "):
                        verbatim = True
                        _inline_after = arg.strip()[len(_kw):].strip()
                        break

                import re as _re

                # Extract URL from replied-to message — check text, entities, and caption
                def _extract_url_from_msg(msg: dict) -> str:
                    # 1. Raw URL in text
                    for field in ("text", "caption"):
                        val = msg.get(field, "") or ""
                        found = _re.findall(r'https?://\S+', val)
                        if found:
                            return found[0].rstrip('.,)')
                    # 2. URL entity (Telegram stores link-preview URLs here)
                    for entity_field in ("entities", "caption_entities"):
                        for ent in msg.get(entity_field, []) or []:
                            if ent.get("type") in ("url", "text_link"):
                                url = ent.get("url") or ""
                                if url.startswith("http"):
                                    return url.rstrip('.,)')
                    # 3. Link preview metadata
                    web = msg.get("web_page") or msg.get("link_preview") or {}
                    if web.get("url"):
                        return web["url"].rstrip('.,)')
                    return ""

                url_to_append = _extract_url_from_msg(reply_to or {}) or _extract_url_from_msg(message)
                source_text = reply_text or url_to_append or text
                logger.info(f"post command: url={url_to_append!r}, source_text={source_text[:80] if source_text else ''}...")

                # If the reply contains a photo but no URL, share the image directly
                # instead of generating an AI post with no real content
                _rt_photos = (reply_to or {}).get("photo", []) or message.get("photo", [])
                if not url_to_append and _rt_photos:
                    _rt_file_id = _rt_photos[-1].get("file_id")
                    if _rt_file_id:
                        _rt_fr = await telegram_service.get_file(_rt_file_id)
                        if _rt_fr and _rt_fr.get("ok"):
                            _rt_fp = _rt_fr.get("result", {}).get("file_path")
                            if _rt_fp:
                                _rt_data = await telegram_service.download_file(_rt_fp)
                                if _rt_data:
                                    # In verbatim mode `arg` is just the keyword ("raw"); don't
                                    # let it become the image caption — fall back to the reply text.
                                    _cap_arg = "" if verbatim else arg.strip()
                                    _share_caption = _cap_arg or reply_text or "Image"
                                    _tg_user_share = db.query(User).filter(
                                        User.telegram_chat_id == chat_id,
                                        User.telegram_enabled == True
                                    ).first()
                                    await _offer_social_post(chat_id, _share_caption, _tg_user_share,
                                                             telegram_service, prompt="📣 *Share this image?*",
                                                             image_bytes=_rt_data)
                                    return {"ok": True}

                # Verbatim mode: share the reply text exactly as written, no LLM rewrite.
                if verbatim:
                    raw_text = (reply_text or _inline_after).strip()
                    if not raw_text:
                        await telegram_service.send_message(chat_id, "Nothing to post — reply to a message with text and send `post raw`.")
                        return {"ok": True}
                    # Append the source URL if the reply references one but doesn't already include it.
                    if url_to_append and url_to_append not in raw_text:
                        raw_text = raw_text.rstrip() + f"\n\n{url_to_append}"
                    _tg_user_raw = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    await _offer_social_post(
                        chat_id, raw_text, _tg_user_raw, telegram_service,
                        prompt="📣 *Post this (as written)?*"
                    )
                    return {"ok": True}

                # Fetch URL content if available
                article_context = source_text
                if url_to_append:
                    try:
                        from app.services.search_service import SearchService
                        _ss = SearchService(db)
                        import asyncio as _asyncio
                        fetched = await _asyncio.wait_for(_ss.fetch_urls([url_to_append], max_urls=1), timeout=15)
                        if fetched and fetched[0].get("content") and not fetched[0].get("error"):
                            article_context = f"Title: {fetched[0].get('title', '')}\n\n{fetched[0]['content'][:3000]}"
                            logger.info(f"post command: fetched article, {len(article_context)} chars")
                        else:
                            logger.warning(f"post command: fetch failed or empty: {fetched[0].get('error') if fetched else 'no result'}")
                    except Exception as _fe:
                        logger.warning(f"post command: failed to fetch URL: {_fe}")

                # `arg` is free-form: a tone adjective (e.g. "professional") or an
                # explicit instruction (e.g. "don't include links", "keep it short").
                # Pass it to the model as an instruction rather than jamming it into the
                # sentence, so multi-word directions are actually honored.
                _extra = arg.strip()
                _extra_l = _extra.lower()
                # If the user asked to omit links, skip the forced URL append below too —
                # otherwise the link reappears no matter what the model does.
                _suppress_link = any(p in _extra_l for p in (
                    "no link", "no links", "without link", "don't include link",
                    "dont include link", "do not include link", "exclude link",
                    "no url", "without url", "skip link", "no source",
                )) if _extra else False
                _tone = "viral and engaging" if not _extra else "compelling"
                _user_prompt = (
                    f"Write a {_tone}, detailed social media post based on this content. "
                    f"Be detailed — include key facts, context, and why it matters. Use emojis."
                )
                if _extra:
                    _user_prompt += f"\n\nFollow these user instructions exactly: {_extra}"
                _user_prompt += f"\n\nContent:\n{article_context}"
                post_messages = [
                    {
                        "role": "system",
                        "content": "You are a social media expert. Write compelling, detailed social media posts. Output ONLY the post text. No introductions, no 'here is your post', no URL placeholders like 'link' or 'read more'."
                    },
                    {
                        "role": "user",
                        "content": _user_prompt,
                    }
                ]

                from app.services.chat_service import ChatService as _CS
                _cs = _CS(db, user=None)
                _cs.num_predict = min(_cs.num_predict, 900)
                try:
                    post_text = await _cs.chat(post_messages)
                    post_text = _strip_hashtags(post_text)
                    # Append the real URL at the end (the model may mangle it), unless
                    # the user explicitly asked to omit links.
                    if url_to_append and not _suppress_link:
                        post_text = post_text.rstrip() + f"\n\n{url_to_append}"
                    result_content = post_text
                except Exception as e:
                    result_content = f"Error generating post: {str(e)}"

                # Check if the linked user has any social platform configured
                _tg_user = db.query(User).filter(
                    User.telegram_chat_id == chat_id,
                    User.telegram_enabled == True
                ).first()

                await _offer_social_post(chat_id, result_content, _tg_user, telegram_service)
                return {"ok": True}

            # User is guaranteed to be linked at this point (auth check above)
            user_obj = _auth_user
            logger.info(f"Found user: {user_obj.username}")

            # Process the message - check for commands first
            chat_service = ChatService(db, user=user_obj)
            command_service = CommandService(db, user=user_obj)
            text_lower = text.lower().strip()
            
            logger.info(f"Telegram message: '{text}'")
            
            # Process attachments (photos, documents) - download first
            attachments = []
            has_images = False
            ocr_text = None
            # Cloud Bot API caps bot downloads at 20 MiB; a local Bot API server
            # raises it to ~2 GB. Track any oversized attachment so compress/convert
            # can explain why it can't be processed.
            TELEGRAM_MAX_DOWNLOAD_BYTES = (2000 * 1024 * 1024) if telegram_service.is_local_api else (20 * 1024 * 1024)
            oversized_attachment = None  # (filename, size_bytes)
            
            # Check if the message starts with a known command
            command = None
            arg = text
            commands = ["help", "new", "ytdl", "geni", "musicgeni", "videogeni", "narrate", "mail", "news", "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post", "share", "remind", "reminders", "pin", "pins", "removebackground", "compress", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "seth", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", "node", "budget", "finance", "bills", "pay", "addbill", "screenshot", "shot", "ss"]
            for cmd in commands:
                if text_lower.startswith(cmd + " ") or text_lower == cmd:
                    command = cmd
                    arg = text[len(cmd):].strip()
                    break
            # Telegram skips parse_command, so resolve aliases (e.g. shot/ss -> screenshot) to the
            # canonical name, or execute_command rejects them as "Unknown command".
            if command:
                command = CommandService.COMMAND_ALIASES.get(command, command)

            # Auto-detect a bare magnet link OR a .torrent URL — route to "torrents add <link>"
            _tl = text.strip()
            if not command and (_tl.startswith("magnet:?") or
                                (_tl.lower().startswith(("http://", "https://"))
                                 and __import__("re").search(r'\.torrent(\?|$)', _tl, __import__("re").IGNORECASE))):
                command = "torrents"
                arg = f"add {_tl}"

            logger.warning(f"TELEGRAM: text='{text}', cmd={command}, arg='{arg}', photos={len(photos) if photos else 0}")
            
            # Download photos FIRST (before any command processing that needs OCR)
            if photos:
                logger.info(f"Processing {len(photos)} photos from Telegram")
                if photos:
                    photo = photos[-1]  # Get highest resolution
                    file_id = photo.get("file_id")
                    logger.info(f"Using photo file_id: {file_id}")
                    if file_id:
                        # Get the file path from Telegram
                        file_result = await telegram_service.get_file(file_id)
                        logger.info(f"File result: {file_result}")
                        if file_result and file_result.get("ok"):
                            file_path = file_result.get("result", {}).get("file_path")
                            logger.info(f"File path: {file_path}")
                            if file_path:
                                # Download the file
                                downloaded_data = await telegram_service.download_file(file_path)
                                if downloaded_data:
                                    import base64
                                    b64_size = len(base64.b64encode(downloaded_data))
                                    attachments.append(("photo.jpg", downloaded_data, "image/jpeg"))
                                    has_images = True
                                    logger.info(f"Downloaded photo, data size: {len(downloaded_data)}, base64 size: {b64_size}")
                                else:
                                    logger.warning("Failed to download photo data")
            
            # Now if translate command with images, do OCR
            if command == "translate" and has_images and attachments:
                # Run OCR on the image
                for filename, file_data, content_type in attachments:
                    if content_type.startswith("image/"):
                        import base64
                        image_b64 = base64.b64encode(file_data).decode('utf-8')
                        try:
                            from app.services.document_service import extract_image_text
                            ocr_result = extract_image_text(image_b64)
                            if ocr_result:
                                ocr_text = ocr_result
                                logger.warning(f"TRANSLATE: Extracted OCR text: {len(ocr_text)} chars")
                        except Exception as e:
                            logger.error(f"OCR error: {e}")
                        break
                
                if ocr_text:
                    language = arg.replace("to", "").strip() or "Thai"
                    logger.warning(f"TRANSLATE: Translating OCR text to {language}, text: {ocr_text[:50]}...")
                    
                    # Create a fresh chat service WITHOUT user context for translation
                    from app.services.chat_service import ChatService as FreshChatService
                    fresh_chat_service = FreshChatService(db, user=None)
                    
                    translate_messages = [
                        {"role": "system", "content": f"Translate the following text to {language}. Output ONLY the translation, nothing else. Do NOT add any commentary, emojis, or persona."},
                        {"role": "user", "content": ocr_text}
                    ]
                    
                    try:
                        translated = await fresh_chat_service.chat(translate_messages)
                        logger.warning(f"TRANSLATE: Got translation: {translated[:100]}...")
                        result = {"type": "text", "content": f"## Translation to {language}\n\n{translated}"}
                    except Exception as e:
                        logger.error(f"Translation error: {e}")
                        result = {"type": "text", "content": f"Translation failed: {str(e)}"}
                    
                    await telegram_service.send_message(chat_id, result.get("content", ""))
                    logger.warning(f"TRANSLATE: Sent translation result")
                    return {"ok": True}
            
            # Download document
            if document:
                file_id = document.get("file_id")
                file_name = document.get("file_name", "document")
                doc_size = document.get("file_size") or 0
                if file_id and doc_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
                    oversized_attachment = (file_name, doc_size)
                    logger.warning(f"Document {file_name} is {doc_size} bytes — exceeds Telegram bot download limit")
                elif file_id:
                    logger.info(f"Processing document: {file_name}")
                    file_result = await telegram_service.get_file(file_id)
                    if file_result.get("ok"):
                        file_path = file_result.get("result", {}).get("file_path")
                        if file_path:
                            downloaded_data = await telegram_service.download_file(file_path)
                            if downloaded_data:
                                # Determine content type — prefer Telegram's mime_type,
                                # fall back to the filename extension.
                                content_type = document.get("mime_type") or "application/octet-stream"
                                lname = file_name.lower()
                                if lname.endswith('.pdf'):
                                    content_type = "application/pdf"
                                elif lname.endswith(('.jpg', '.jpeg')):
                                    content_type = "image/jpeg"
                                elif lname.endswith('.png'):
                                    content_type = "image/png"
                                elif lname.endswith('.gif'):
                                    content_type = "image/gif"
                                elif lname.endswith(('.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v')):
                                    content_type = "video/mp4"
                                attachments.append((file_name, downloaded_data, content_type))
                                logger.info(f"Downloaded document: {file_name}, size: {len(downloaded_data)}")

            # Download video / animation attachments (for the compress command)
            if video:
                file_id = video.get("file_id")
                v_size = video.get("file_size") or 0
                if file_id and v_size > TELEGRAM_MAX_DOWNLOAD_BYTES:
                    oversized_attachment = (video.get("file_name") or "video.mp4", v_size)
                    logger.warning(f"Video is {v_size} bytes — exceeds Telegram bot download limit")
                    file_id = None  # skip the doomed getFile call
                if file_id:
                    v_name = video.get("file_name") or "video.mp4"
                    v_mime = video.get("mime_type") or "video/mp4"
                    file_result = await telegram_service.get_file(file_id)
                    if file_result.get("ok"):
                        file_path = file_result.get("result", {}).get("file_path")
                        if file_path:
                            downloaded_data = await telegram_service.download_file(file_path)
                            if downloaded_data:
                                attachments.append((v_name, downloaded_data, v_mime))
                                logger.info(f"Downloaded video: {v_name}, size: {len(downloaded_data)}")

            # Handle Telegram media groups: multiple docs sent together arrive as separate webhooks
            # with the same media_group_id. Accumulate them before processing.
            media_group_id = message.get("media_group_id")
            if media_group_id and attachments:
                _mg = _MEDIA_GROUP_CACHE.setdefault(
                    media_group_id, {"attachments": [], "text": "", "created_at": time.time()}
                )
                if text.strip():
                    _mg["text"] = text  # caption rides on whichever message has it
                _mg["attachments"].extend(attachments)
                _mg["last"] = time.time()
                # Album photos arrive as SEPARATE webhooks and download at different
                # speeds, so wait until the group has been QUIET for ~1.5s rather than a
                # fixed sleep — otherwise the fastest handler popped before the others had
                # added their image (symptom: only 1 image was used). Each late arrival
                # bumps `last`, so this keeps waiting until the whole album is in.
                while True:
                    await asyncio.sleep(1.5)
                    _cur = _MEDIA_GROUP_CACHE.get(media_group_id)
                    if _cur is None:
                        return {"ok": True}  # another handler already processed the group
                    if time.time() - _cur.get("last", 0) >= 1.4:
                        break
                _mg_data = _MEDIA_GROUP_CACHE.pop(media_group_id, None)
                if _mg_data is None:
                    return {"ok": True}
                attachments = _mg_data["attachments"]
                text = _mg_data["text"] or text
                text_lower = text.lower().strip()
                # Re-derive the command from the ASSEMBLED caption: the handler that wins
                # the pop may be a caption-less photo, so the `command` parsed earlier could
                # be None even though the album carries a caption like "whoabuddy".
                command = None
                arg = text
                for cmd in commands:
                    if text_lower.startswith(cmd + " ") or text_lower == cmd:
                        command = cmd
                        arg = text[len(cmd):].strip()
                        break
                if command:
                    command = CommandService.COMMAND_ALIASES.get(command, command)
                logger.info(f"[MEDIA-GROUP] {media_group_id}: assembled {len(attachments)} attachments, cmd={command}, text={text!r}")

            # Extract text from PDF/Office document attachments (concatenate all, not just last)
            doc_text = None
            pdf_attachments = []  # collect raw PDF bytes for potential merge
            if attachments:
                import base64 as _b64
                from app.services.document_service import extract_pdf_text, extract_document_text, merge_pdfs
                doc_parts = []
                for _fname, _fdata, _ctype in attachments:
                    try:
                        _fdata_b64 = _b64.b64encode(_fdata).decode('utf-8')
                        if _ctype == "application/pdf" or _fname.lower().endswith('.pdf'):
                            pdf_attachments.append((_fname, _fdata))
                            _extracted = extract_pdf_text(_fdata_b64)
                            if _extracted:
                                doc_parts.append(f"[PDF: {_fname}]\n\n{_extracted}")
                                logger.info(f"Extracted {len(_extracted)} chars from PDF: {_fname}")
                        elif _ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                            _extracted = extract_document_text(_fdata_b64)
                            if _extracted:
                                doc_parts.append(f"[Document: {_fname}]\n\n{_extracted}")
                                logger.info(f"Extracted {len(_extracted)} chars from document: {_fname}")
                    except Exception as _doc_err:
                        logger.error(f"Document extraction error for {_fname}: {_doc_err}")
                if doc_parts:
                    doc_text = "\n\n---\n\n".join(doc_parts)

            # If user asks to merge/combine/join multiple PDFs, do it server-side and send back as file
            # This is completely independent of PDF analysis/summarization
            _is_merge_intent = bool(re.search(r'\b(merge|combine|join|concatenate|concat)\b', text_lower)) and len(pdf_attachments) >= 2
            if _is_merge_intent:
                try:
                    _merged_bytes = merge_pdfs([_fdata for _, _fdata in pdf_attachments])
                    if _merged_bytes:
                        _names = "+".join(fn.replace('.pdf', '') for fn, _ in pdf_attachments[:3])
                        _out_name = f"merged_{_names}.pdf"
                        await telegram_service.send_document_bytes(chat_id, _merged_bytes, _out_name, f"✅ Merged {len(pdf_attachments)} PDFs into {_out_name}")
                        return {"ok": True}
                    else:
                        await telegram_service.send_message(chat_id, "❌ PDF merge failed — could not process the files.")
                        return {"ok": True}
                except Exception as _merge_err:
                    logger.error(f"PDF merge error: {_merge_err}")
                    await telegram_service.send_message(chat_id, f"❌ PDF merge failed: {_merge_err}")
                    return {"ok": True}

            # If we have images, always run OCR for later use
            # (skip for compress/convert — they operate on the raw file, not its text)
            if has_images and attachments and command not in ("compress", "removebackground", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "seth", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz"):
                for filename, file_data, content_type in attachments:
                    if content_type.startswith("image/"):
                        import base64
                        image_b64 = base64.b64encode(file_data).decode('utf-8')
                        try:
                            from app.services.document_service import extract_image_text
                            ocr_result = extract_image_text(image_b64)
                            if ocr_result:
                                ocr_text = ocr_result
                                logger.info(f"Extracted OCR text: {len(ocr_text)} chars")
                        except Exception as e:
                            logger.error(f"OCR error: {e}")
                        break
            
            # If translate command with OCR text, handle it directly
            if command == "translate" and ocr_text:
                language = arg.replace("to", "").strip() or "Thai"
                logger.warning(f"TRANSLATE: Final check - Using OCR text ({len(ocr_text)} chars) to translate to '{language}'")
                logger.warning(f"TRANSLATE: ocr_text content: {ocr_text[:100]}...")
                
                # Build messages for translation
                translate_messages = [
                    {"role": "system", "content": f"Translate the following text to {language}. Output ONLY the translation, nothing else."},
                    {"role": "user", "content": ocr_text}
                ]
                
                logger.warning(f"TRANSLATE: Calling chat_service.chat with messages: {translate_messages}")
                
                try:
                    translated = await chat_service.chat(translate_messages)
                    logger.warning(f"TRANSLATE: Got translation result: {translated[:100]}...")
                    result = {"type": "text", "content": f"## Translation to {language}\n\n{translated}"}
                except Exception as e:
                    logger.error(f"Translation error: {e}", exc_info=True)
                    result = {"type": "text", "content": f"Translation failed: {str(e)}"}
                
                # Send result and return early
                await telegram_service.send_message(chat_id, result.get("content", ""))
                logger.warning(f"TRANSLATE: Sent translation result")
                return {"ok": True}
            elif command == "translate" and has_images:
                logger.warning(f"TRANSLATE: Command detected but no OCR text yet, has_images={has_images}, attachments={len(attachments)}")
            
            # Attachment too large for Telegram to hand to the bot (20 MB cap).
            # Handle here so it works whether or not a command caption was given,
            # instead of falling through to the chat model.
            if oversized_attachment and command in ("compress", "removebackground", "clip", "convert", "flashcards", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "alive", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "seth", "robocop", "titan", "terminator", "reze", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", None):
                _ov_name, _ov_size = oversized_attachment
                _cap_mb = TELEGRAM_MAX_DOWNLOAD_BYTES / (1024 * 1024)
                if telegram_service.is_local_api:
                    _msg = (
                        f"❌ `{_ov_name}` is {_ov_size / (1024 * 1024):.1f} MB, over the "
                        f"{_cap_mb:.0f} MB limit of the configured Bot API server."
                    )
                else:
                    _msg = (
                        f"❌ `{_ov_name}` is {_ov_size / (1024 * 1024):.1f} MB. The cloud Telegram Bot "
                        f"API only lets bots download files up to 20 MiB (≈20.97 MB).\n\n"
                        f"Use the **web UI** for larger files, or enable a local Bot API server "
                        f"in Admin → Services."
                    )
                await telegram_service.send_message(chat_id, _msg)
                return {"ok": True}

            # A *caption-less* media upload: prompt with action buttons (like the
            # YouTube/link prompts) instead of guessing. A caption WITH text flows
            # to normal command/chat routing so attachments never hijack features.
            if attachments and not command and not text.strip():
                _media_kbd = _media_action_keyboard(attachments, user=user_obj)
                if _media_kbd:
                    # Evict expired entries so abandoned uploads don't linger in
                    # memory (each can hold video-sized bytes).
                    _now = time.time()
                    for _cid in [k for k, v in _media_action_cache.items()
                                 if _now - v.get("ts", 0) > _MEDIA_ACTION_TTL]:
                        _media_action_cache.pop(_cid, None)
                    _media_action_cache[chat_id] = {"attachments": attachments, "ts": _now}
                    _n = len(attachments)
                    await telegram_service.send_message(
                        chat_id,
                        f"📎 Got {_n} file{'s' if _n != 1 else ''}. What would you like to do?",
                        reply_markup=_media_kbd,
                    )
                    return {"ok": True}

            reply_markup = None
            if command:
                logger.info(f"Executing command: {command} with arg: {arg}, attachments: {len(attachments)}")
                try:
                    # New glowing TEXT post from scratch: `glow <text>` with NO image →
                    # render the neon card and go straight to the social-post offer
                    # (empty body — the text IS the image). Tightly gated so it never
                    # touches `glow`+image (the effect), bare `glow`, or any other
                    # command — those all fall through to the existing handlers below.
                    if command == "glow" and not has_images and arg.strip():
                        try:
                            from app.services import effects_service as _fx
                            _png = await asyncio.to_thread(_fx.render_glow_text_card, arg)
                            import base64 as _b64
                            await telegram_service.send_photo(
                                chat_id, _b64.b64encode(_png).decode("ascii"), "🌟 Glowing text preview")
                            await _offer_social_post(chat_id, "", user_obj, telegram_service,
                                                     prompt="📣 *Post this glowing image?*", image_bytes=_png)
                        except Exception as _ge:
                            logger.error(f"glow text post failed: {_ge}", exc_info=True)
                            await telegram_service.send_message(chat_id, f"❌ Couldn't make the glowing text post: {_ge}")
                        return {"ok": True}
                    if command == "help":
                        await telegram_service.send_message(
                            chat_id,
                            "🤖 *PosterChanAI* — tap a topic to learn more:",
                            parse_mode="MarkdownV2",
                            reply_markup=_help_main_keyboard(),
                        )
                        return {"ok": True}
                    elif command == "new":
                        # Clear the Telegram conversation history for this user
                        tg_conv = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()
                        if tg_conv:
                            db.query(Message).filter(Message.conversation_id == tg_conv.id).delete()
                            db.commit()
                        await telegram_service.send_message(chat_id, "Conversation cleared. Starting fresh!")
                        return {"ok": True}
                    elif command in ("budget", "finance"):
                        await _send_budget(chat_id, user_obj, db)
                        return {"ok": True}
                    elif command in ("bills", "pay", "addbill"):
                        # pay/addbill mutate, then show the refreshed interactive budget;
                        # bills just returns the formatted list from the shared command service.
                        result = await command_service.execute_command(command, arg)
                        await telegram_service.send_message(
                            chat_id, result.get("content", "Done."), parse_mode=""
                        )
                        if command in ("pay", "addbill") and arg.strip():
                            await _send_budget(chat_id, user_obj, db)
                        return {"ok": True}
                    elif command == "ytdl":
                        if not arg:
                            await telegram_service.send_message(
                                chat_id,
                                "Usage:\n`ytdl <youtube_url>` - Download as MP3\n`ytdl video <youtube_url>` - Download as video"
                            )
                            return {"ok": True}

                        from app.services.youtube_service import (
                            check_ytdlp_available,
                            download_as_mp3,
                            download_as_video,
                            download_video_and_save_to_storage,
                            extract_download_urls,
                        )
                        import tempfile, shutil, os as _os, asyncio as _asyncio

                        if not check_ytdlp_available():
                            await telegram_service.send_message(chat_id, "❌ yt-dlp is not installed on the server.")
                            return {"ok": True}

                        # Check if user wants video or MP3
                        arg_parts = arg.strip().split(maxsplit=1)
                        first_word = arg_parts[0].lower() if arg_parts else ""
                        if first_word == "video" and len(arg_parts) > 1:
                            as_video = True
                            url_arg = arg_parts[1]
                        elif first_word == "mp3" and len(arg_parts) > 1:
                            as_video = False
                            url_arg = arg_parts[1]
                        else:
                            as_video = False
                            url_arg = arg

                        # Optional post-processing modifiers:
                        #   clip <start> <end>   — trim the downloaded video
                        #   compress             — shrink it (applied after clip)
                        # These only apply to video, so their presence implies `video`
                        # even without the keyword (you can't trim/shrink an MP3).
                        _clip_arg = None
                        _toks = url_arg.split()
                        _low = [t.lower() for t in _toks]
                        _compress = "compress" in _low
                        if "clip" in _low:
                            _ci = _low.index("clip")
                            _rest = [t for t in _toks[_ci + 1:_ci + 3] if t.lower() != "compress"]
                            if len(_rest) == 2:
                                _clip_arg = f"{_rest[0]} {_rest[1]}"
                            else:
                                await telegram_service.send_message(chat_id, "❌ `clip` needs a start and end, e.g. `ytdl video <url> clip 0:10 0:30`.")
                                return {"ok": True}
                        if _clip_arg or _compress:
                            as_video = True

                        urls = extract_download_urls(url_arg)
                        if not urls:
                            await telegram_service.send_message(chat_id, "❌ Could not find a valid YouTube URL in your message.")
                            return {"ok": True}

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

                        if as_video:
                            # Download and send video
                            await telegram_service.send_message(chat_id, "⏳ Downloading video, please wait...")
                            temp_dir = tempfile.mkdtemp(prefix="tg_ytdlvideo_")
                            try:
                                dl_result = await _asyncio.to_thread(
                                    download_as_video, urls[0], temp_dir, "best", _cookies_path, _no_ssl
                                )
                                if not dl_result.success:
                                    await telegram_service.send_message(chat_id, f"❌ Download failed: {dl_result.error}")
                                    return {"ok": True}

                                # No inline clip/compress given → offer the actions
                                # interactively (Send / Compress / Clip / Clip+Compress).
                                if not (_clip_arg or _compress):
                                    await _offer_ytdl_video_actions(chat_id, dl_result, urls[0], user_obj, db)
                                    return {"ok": True}

                                # Optional post-processing: clip then compress, reusing the
                                # standalone-command transforms so results are identical.
                                send_path = dl_result.local_path
                                if _clip_arg or _compress:
                                    from app.services.media_service import (
                                        parse_timecode, clip_attachment, compress_attachments,
                                    )
                                    await telegram_service.send_message(chat_id, "⏳ Processing video…")
                                    _fn = _os.path.basename(dl_result.local_path)
                                    with open(dl_result.local_path, "rb") as _vf:
                                        _vbytes = _vf.read()
                                    _mime = "video/mp4"
                                    if _clip_arg:
                                        _p = _clip_arg.split()
                                        _s, _e = parse_timecode(_p[0]), parse_timecode(_p[1])
                                        if _s is None or _e is None or _e <= _s:
                                            await telegram_service.send_message(chat_id, "❌ Invalid clip times — use `clip 0:10 0:30` (end after start).")
                                            return {"ok": True}
                                        _outs, _ = await _asyncio.to_thread(clip_attachment, [(_fn, _vbytes, _mime)], _s, _e)
                                        if not _outs:
                                            await telegram_service.send_message(chat_id, "❌ Clip failed.")
                                            return {"ok": True}
                                        _fn, _vbytes, _mime = _outs[0]["filename"], _outs[0]["data"], _outs[0]["content_type"]
                                    if _compress:
                                        _outs, _ = await _asyncio.to_thread(compress_attachments, [(_fn, _vbytes, _mime)])
                                        if not _outs:
                                            await telegram_service.send_message(chat_id, "❌ Compress failed.")
                                            return {"ok": True}
                                        _fn, _vbytes, _mime = _outs[0]["filename"], _outs[0]["data"], _outs[0]["content_type"]
                                    send_path = _os.path.join(temp_dir, _fn)
                                    with open(send_path, "wb") as _of:
                                        _of.write(_vbytes)

                                file_size = _os.path.getsize(send_path)
                                # Telegram bot limit is 50 MB for videos
                                if file_size > 50 * 1024 * 1024:
                                    # File too large - save to storage and notify. (For a
                                    # clipped/compressed result this re-downloads the full
                                    # source to storage; the trimmed copy can't be stored.)
                                    save_result = await download_video_and_save_to_storage(
                                        url=urls[0],
                                        user_id=user_obj.id,
                                        db=db,
                                        subfolder="YouTube Videos",
                                    )
                                    from app.services.youtube_service import format_download_result
                                    await telegram_service.send_message(
                                        chat_id,
                                        f"❌ Video is too large to send via Telegram ({file_size // (1024*1024)} MB).\n\n{format_download_result(save_result)}"
                                    )
                                    return {"ok": True}

                                # Send the video. After a clip the source duration no
                                # longer matches, so let Telegram infer it from the file.
                                duration_int = None if _clip_arg else (int(dl_result.duration) if dl_result.duration else None)
                                caption = f"🎬 **{dl_result.title}**" if dl_result.title else "🎬 Video"
                                if dl_result.artist:
                                    caption += f"\n👤 {dl_result.artist}"

                                video_result = await telegram_service.send_video(
                                    chat_id=chat_id,
                                    file_path=send_path,
                                    caption=caption,
                                    duration=duration_int,
                                )
                                if not video_result.get("ok"):
                                    error_desc = video_result.get('description', video_result.get('error', 'Unknown error'))
                                    logger.error(f"Failed to send video: {video_result}")
                                    await telegram_service.send_message(chat_id, f"❌ Failed to send video: {error_desc}")
                            except Exception as yt_err:
                                logger.error(f"YouTube video callback error: {yt_err}", exc_info=True)
                                await telegram_service.send_message(chat_id, f"❌ Error: {yt_err}")
                            finally:
                                shutil.rmtree(temp_dir, ignore_errors=True)
                        return {"ok": True}
                    elif command == "torrents":
                        arg_parts = arg.strip().split()
                        arg_sub = arg_parts[0].lower() if arg_parts else ""

                        if not arg_sub:
                            # Show category navigation menu without scraping all categories
                            result = {"type": "text", "content": "🧲 **Torrents** — choose a category:"}
                            reply_markup = _torrent_nav_keyboard()
                        elif arg_sub in ("movies", "tv", "anime", "music", "search", "s"):
                            # Execute to populate the cache, then send individual result messages
                            result = await command_service.execute_command(command, arg)
                            user_id = user_obj.id if user_obj else 0
                            cache_key = "search" if arg_sub in ("search", "s") else arg_sub
                            await _send_torrent_results(chat_id, cache_key, user_id)
                            return {"ok": True}
                        else:
                            if attachments:
                                result = await command_service.execute_command(command, arg, attachments=attachments)
                            else:
                                result = await command_service.execute_command(command, arg)
                            content = result.get("content", "")
                            user_id = user_obj.id if user_obj else 0
                            if arg_sub in ("list", "ls"):
                                await _send_active_torrents(chat_id, content)
                                return {"ok": True}
                            reply_markup = _build_torrent_keyboard(arg_sub, content, user_id)
                            # Clean non-functional links from torrent result text
                            result["content"] = _strip_cmd_links(content)
                    elif command == "nyaa":
                        result = await command_service.execute_command(command, arg)
                        user_id = user_obj.id if user_obj else 0
                        await _send_nyaa_results(chat_id, user_id)
                        return {"ok": True}
                    elif command == "4chan":
                        # Parse board from argument
                        arg_parts = arg.strip().split()
                        board = arg_parts[0].lower() if arg_parts else None
                        allowed_boards = ("g", "pol", "a", "h")
                        
                        if board and board in allowed_boards:
                            # Valid board specified, show catalog
                            user_id = user_obj.id if user_obj else 0
                            await _send_4chan_catalog(chat_id, board, user_id)
                        else:
                            # No board specified or invalid board, show board selector
                            await telegram_service.send_message(
                                chat_id,
                                "🍀 *4chan Board Selector*\n\nChoose a board to browse:",
                                reply_markup=_4chan_initial_keyboard()
                            )
                        return {"ok": True}
                    elif command == "news":
                        # If no argument provided, show the news menu
                        if not arg.strip():
                            await telegram_service.send_message(
                                chat_id,
                                "📰 *News Menu*\n\nChoose an option:",
                                reply_markup=_news_menu_keyboard()
                            )
                            return {"ok": True}
                        
                        # Otherwise, fetch news from specific source
                        result = await command_service.execute_command(command, arg)
                        content = _strip_cmd_links(result.get("content", ""))

                        has_social = _has_misskey(user_obj) or _has_pleroma(user_obj) or _has_matrix(user_obj)

                        articles = _split_news_into_articles(content)
                        if articles:
                            # Cache (title, url) pairs for the Post callbacks
                            _news_post_cache[chat_id] = [(title, url) for (_, title, url, _) in articles]

                            # Send header (date/source summary line) if present
                            header_match = re.match(r'^(##[^\n]+)', content)
                            if header_match:
                                await telegram_service.send_message(chat_id, header_match.group(1))

                            # Send each article as its own message
                            for i, (_, title, url, msg_text) in enumerate(articles[:10], 1):
                                # Build keyboard with Summarize and Post buttons (only if a social platform is configured)
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
                            return {"ok": True}

                        # Fallback: no articles parsed — send raw content
                        result["content"] = content
                    elif command == "share":
                        # Share command: take the user's text (+ optional attachment) and offer to post it
                        # to configured social platforms directly (no AI generation needed)
                        share_text = arg.strip() if arg.strip() else text.strip()
                        if not share_text and not has_images and not attachments:
                            await telegram_service.send_message(
                                chat_id,
                                "Usage: send `share <your post text>` (optionally attach a photo).\n"
                                "The text will be shared directly to your configured social platforms."
                            )
                            return {"ok": True}

                        _share_user = db.query(User).filter(
                            User.telegram_chat_id == chat_id,
                            User.telegram_enabled == True
                        ).first()

                        # Build share text
                        final_share_text = share_text or "(image)"

                        # Collect image bytes so Matrix can send the actual image
                        _share_img = None
                        if has_images and attachments:
                            for _fn, _fd, _ct in attachments:
                                if _ct.startswith("image/"):
                                    _share_img = _fd
                                    break
                        # Also check replied-to message for a photo
                        if not _share_img and reply_to:
                            _rt_photos = reply_to.get("photo", [])
                            if _rt_photos:
                                _rt_file_id = _rt_photos[-1].get("file_id")
                                if _rt_file_id:
                                    _rt_fr = await telegram_service.get_file(_rt_file_id)
                                    if _rt_fr and _rt_fr.get("ok"):
                                        _rt_fp = _rt_fr.get("result", {}).get("file_path")
                                        if _rt_fp:
                                            _rt_data = await telegram_service.download_file(_rt_fp)
                                            if _rt_data:
                                                _share_img = _rt_data

                        await _offer_social_post(chat_id, final_share_text, _share_user, telegram_service,
                                                  prompt="📣 *Share this?*", image_bytes=_share_img)
                        return {"ok": True}
                    elif command in ("remind", "reminders"):
                        # Reminders: create/list. For the list, attach a Cancel button per reminder
                        # so Telegram is interactive too (mirrors the web UI's clickable list).
                        result = await command_service.execute_command(command, arg)
                        if result.get("type") == "reminders" and result.get("reminders"):
                            kb = []
                            for _r in result["reminders"]:
                                _label = _r.get("text", "")[:40]
                                kb.append([{"text": f"🗑️ Cancel: {_label}", "callback_data": f"rem:cancel:{_r['id']}"}])
                            await telegram_service.send_message(
                                chat_id, result.get("content", ""),
                                reply_markup={"inline_keyboard": kb}, parse_mode="",
                            )
                        else:
                            await telegram_service.send_message(chat_id, result.get("content", "Done."), parse_mode="")
                        return {"ok": True}
                    elif command in ("pin", "pins"):
                        # Pinned searches: save/list. The list gets a Run + Delete button per item.
                        result = await command_service.execute_command(command, arg)
                        if result.get("type") == "saved_searches" and result.get("saved_searches"):
                            kb = []
                            for _s in result["saved_searches"]:
                                _q = _s.get("query", "")
                                kb.append([
                                    {"text": f"🔍 {_q[:32]}", "callback_data": f"pin:run:{_s['id']}"},
                                    {"text": "🗑️", "callback_data": f"pin:del:{_s['id']}"},
                                ])
                            await telegram_service.send_message(
                                chat_id, result.get("content", ""),
                                reply_markup={"inline_keyboard": kb}, parse_mode="",
                            )
                        else:
                            await telegram_service.send_message(chat_id, result.get("content", "Done."), parse_mode="")
                        return {"ok": True}
                    else:
                        # For `node` (long jobs finish after this handler returns) and `logs`
                        # (multi-minute agentic health report), stream step progress back to THIS
                        # Telegram chat as it runs.
                        node_notify = _make_tg_node_notify(telegram_service, chat_id) if command in ("node", "logs") else None
                        # Pass attachments to any command that supports them
                        if attachments:
                            result = await command_service.execute_command(command, arg, attachments=attachments, node_notify=node_notify)
                        else:
                            result = await command_service.execute_command(command, arg, node_notify=node_notify)
                    logger.info(f"Command result: {result}")
                except Exception as e:
                    logger.error(f"Command execution error: {e}", exc_info=True)
                    result = {"type": "text", "content": f"Error: {str(e)}"}
            else:
                # Regular chat - check for images and do OCR or pass to vision model
                from app.services.intent_service import IntentService
                intent_service = IntentService(db, user=user_obj)
                text_stripped = text.strip()

                # Detect YouTube URLs anywhere in the message
                _yt_domains = ('youtube.com/watch', 'youtu.be/', 'youtube.com/shorts/')
                _all_urls_in_text = [u for u in __import__('re').findall(r'https?://\S+', text_stripped)]
                youtube_url = next((u for u in _all_urls_in_text if any(d in u for d in _yt_domains)), None)

                # Detect an X/Twitter/Nitter status URL (downloadable via yt-dlp, no transcript so no
                # Summary option). extract_download_urls returns the x.com-normalized form (nitter
                # rewritten); keep the ORIGINAL url too so the bare/forwarded check works on the text.
                _x_orig = _x_dl = None
                if not youtube_url:
                    from app.services.youtube_service import extract_download_urls as _edl
                    for _u in _all_urls_in_text:
                        _got = _edl(_u)
                        if _got:
                            _x_orig, _x_dl = _u, _got[0]
                            break

                # YouTube URL (bare or forwarded): ask the user what they want to do
                if youtube_url and (is_forwarded or not text_stripped.replace(youtube_url, '').strip()):
                    logger.info(f"Telegram: YouTube URL detected, prompting action: {youtube_url}")
                    _youtube_action_cache[chat_id] = youtube_url
                    
                    # Check if user has social platforms configured
                    _yt_user_for_social = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()

                    # Build keyboard with social post option if any platform is configured
                    yt_keyboard = [
                        [
                            {"text": "📋 Summary",  "callback_data": "yt:summary"},
                            {"text": "🎵 MP3",      "callback_data": "yt:mp3"},
                            {"text": "🎬 Movie",    "callback_data": "yt:video"},
                        ]
                    ]
                    if _has_misskey(_yt_user_for_social) or _has_pleroma(_yt_user_for_social) or _has_matrix(_yt_user_for_social):
                        yt_keyboard.append([
                            {"text": "📣 Post", "callback_data": "yt:post"}
                        ])
                    
                    await telegram_service.send_message(
                        chat_id,
                        "🎬 What would you like to do with this video?",
                        reply_markup={"inline_keyboard": yt_keyboard},
                    )
                    return {"ok": True}

                # X/Twitter/Nitter status URL (bare or forwarded): same prompt as YouTube minus
                # Summary (tweets have no transcript). Reuses the yt: callbacks — the cached URL is
                # the x.com-normalized form, so MP3/Video/Post all download via yt-dlp's Twitter path.
                if _x_dl and (is_forwarded or not text_stripped.replace(_x_orig, '').strip()):
                    logger.info(f"Telegram: X/Nitter URL detected, prompting action: {_x_dl}")
                    _youtube_action_cache[chat_id] = _x_dl

                    _x_user_for_social = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()

                    x_keyboard = [
                        [
                            {"text": "🎵 MP3",   "callback_data": "yt:mp3"},
                            {"text": "🎬 Video", "callback_data": "yt:video"},
                        ]
                    ]
                    if _has_misskey(_x_user_for_social) or _has_pleroma(_x_user_for_social) or _has_matrix(_x_user_for_social):
                        x_keyboard.append([
                            {"text": "📣 Post", "callback_data": "yt:post"}
                        ])

                    await telegram_service.send_message(
                        chat_id,
                        "🐦 What would you like to do with this post?",
                        reply_markup={"inline_keyboard": x_keyboard},
                    )
                    return {"ok": True}

                # Forwarded messages with URLs prompt the user what to do (same as bare URL)
                if is_forwarded:
                    import re as _fwd_re
                    _fwd_url = None

                    # 1. Check entities for text_link (Miniflux puts the real article URL here)
                    for _ent in message.get("entities", []) or []:
                        if _ent.get("type") == "text_link":
                            _u = _ent.get("url", "")
                            if _u.startswith("http"):
                                _fwd_url = _u.rstrip(".,)>")
                                break

                    # 2. Check link_preview_options.url
                    if not _fwd_url:
                        _lpo = message.get("link_preview_options") or {}
                        _u = _lpo.get("url", "")
                        if _u.startswith("http"):
                            _fwd_url = _u.rstrip(".,)>")

                    # 3. Fall back to raw https:// URLs in the text
                    if not _fwd_url:
                        _fwd_raw = _fwd_re.findall(r'https?://\S+', text_stripped)
                        if _fwd_raw:
                            _fwd_url = _fwd_raw[0].rstrip(".,)>")

                    if _fwd_url:
                        logger.info(f"Telegram: Forwarded message with URL, prompting action: {_fwd_url}")
                        _link_action_cache[chat_id] = _fwd_url
                        await telegram_service.send_message(
                            chat_id,
                            f"🔗 What would you like to do with this link?\n{_fwd_url}",
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {"text": "📋 Summary",    "callback_data": "lnk:summary"},
                                        {"text": "📸 Screenshot", "callback_data": "lnk:screenshot"},
                                        {"text": "🎴 Flashcards", "callback_data": "lnk:flashcards"},
                                    ],
                                    [
                                        {"text": "📣 Post",   "callback_data": "lnk:post"},
                                        {"text": "❌ Cancel", "callback_data": "lnk:cancel"},
                                    ],
                                ]
                            },
                        )
                        return {"ok": True}

                # Skip intent detection for bare URLs — they are never commands and the
                # LLM always fails or returns garbage for URL-only input.
                is_bare_url = (
                    text_stripped.startswith(("http://", "https://")) and
                    " " not in text_stripped
                )
                intent = None if (is_bare_url or is_forwarded) else await intent_service.detect_intent(text)
                # intent["command"] is the full command string (e.g. "geni a sunset")
                # parse it to split command name from arguments
                intent_command_str = intent.get("command", "") if intent else ""
                command, arg = command_service.parse_command(intent_command_str) if intent_command_str else (None, "")

                if command:
                    logger.info(f"Detected intent: command={command}, arg={arg}")
                    if attachments:
                        result = await command_service.execute_command(command, arg, attachments=attachments)
                    else:
                        result = await command_service.execute_command(command, arg)
                else:
                    # Regular chat - use the chat service
                    from app.models import Conversation, Message

                    # Forwarded messages and bare URLs use a clean summarization context —
                    # no history, focused system prompt to avoid hallucination loops.
                    if is_bare_url or is_forwarded:
                        messages = [
                            {"role": "system", "content": "You are a concise summarizer. Summarize the provided content clearly and in detail. Include key facts, main points, and any important details. Output only the summary, nothing else."},
                        ]
                        last_role = "system"
                    else:
                        # Build messages for the LLM - no DB conversation needed for Telegram
                        # History is managed within the Telegram chat itself
                        _system_prompt = chat_service.system_prompt.replace(
                            "{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d")
                        )
                        # Casual Telegram chat: keep it conversational and stop the
                        # model from drifting into code/script dumps for short or
                        # ambiguous messages (a recurring failure mode).
                        _system_prompt += (
                            "\n\nThis is a casual Telegram chat. Reply conversationally and briefly. "
                            "Do NOT output code, shell scripts, or ``` code blocks unless the user's "
                            "most recent message explicitly asks you to write code."
                        )
                        messages = [
                            {"role": "system", "content": _system_prompt},
                        ]

                        last_role = "system"

                        # Add recent message history from the Telegram conversation (limited, truncated)
                        conversation = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()

                        if conversation:
                            recent_messages = db.query(Message).filter(
                                Message.conversation_id == conversation.id
                            ).order_by(Message.id.desc()).limit(6).all()

                            HISTORY_CHAR_LIMIT = 2000  # large enough to hold a full URL summary
                            for msg in reversed(recent_messages):
                                if msg.role == last_role:
                                    continue
                                # Don't feed prior code-block replies back as context — they make
                                # the model keep emitting code (self-perpetuating loop). Skip them.
                                if msg.role == "assistant" and "```" in (msg.content or ""):
                                    continue
                                content = msg.content[:HISTORY_CHAR_LIMIT] if len(msg.content) > HISTORY_CHAR_LIMIT else msg.content
                                messages.append({"role": msg.role, "content": content})
                                last_role = msg.role
                    
                    # If there are image attachments, add them to the message for vision models
                    if has_images and attachments:
                        # Build vision-capable message content
                        vision_content = []
                        for filename, file_data, content_type in attachments:
                            if content_type.startswith("image/"):
                                import base64
                                image_b64 = base64.b64encode(file_data).decode('utf-8')
                                # Try OCR first
                                try:
                                    from app.services.document_service import extract_image_text
                                    ocr_text = extract_image_text(image_b64)
                                    if ocr_text:
                                        vision_content.append({"type": "text", "text": f"[Image OCR text:\n{ocr_text}]"})
                                        logger.info(f"Extracted OCR text, length: {len(ocr_text)}")
                                    else:
                                        # No OCR - pass image directly for vision models
                                        vision_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
                                        logger.info("No OCR text, passing image to vision model")
                                except Exception as ocr_err:
                                    logger.error(f"OCR error: {ocr_err}")
                                    # Pass image directly for vision models
                                    vision_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
                                break
                        
                        if vision_content:
                            # If no user text, add an explicit instruction so the model summarizes
                            # rather than echoing the OCR content back.
                            user_instruction = text if text.strip() else (
                                "Summarize the content in this image in detail." if is_forwarded
                                else "What does this image show?"
                            )
                            vision_content.append({"type": "text", "text": user_instruction})
                            # If last_role is user, merge with last message instead of creating duplicate
                            if last_role == "user":
                                if isinstance(messages[-1]["content"], list):
                                    messages[-1]["content"].extend(vision_content)
                                else:
                                    messages[-1]["content"] += "\n\n" + str(vision_content)
                            else:
                                messages.append({"role": "user", "content": vision_content})
                            logger.info(f"Sending vision message with {len(vision_content)} content parts")
                        else:
                            # If last_role is user, merge with last message instead of creating duplicate
                            if last_role == "user":
                                messages[-1]["content"] += "\n\n" + text
                            else:
                                messages.append({"role": "user", "content": text})
                    else:
                        # Build user message, prepending any extracted document text
                        _user_msg_text = text
                        if doc_text:
                            if text.strip():
                                _user_msg_text = f"Here is a document the user shared:\n\n{doc_text}\n\nUser's message: {text}"
                            else:
                                _user_msg_text = f"The user uploaded a document. Please summarize and explain its contents:\n\n{doc_text}"
                        # If last_role is user, merge with last message instead of creating duplicate
                        if last_role == "user":
                            messages[-1]["content"] += "\n\n" + _user_msg_text
                        else:
                            messages.append({"role": "user", "content": _user_msg_text})

                    # If the user replied to a message, inject that context so the model
                    # knows what content/URL to reference (e.g. "make a post with this URL").
                    if reply_text:
                        reply_prefix = f"[Replying to: {reply_text}]\n\n"
                        if isinstance(messages[-1]["content"], list):
                            messages[-1]["content"].append({"type": "text", "text": reply_prefix})
                        else:
                            messages[-1]["content"] = reply_prefix + messages[-1]["content"]

                    # Detect and fetch URLs in user message and reply context (like web UI does)
                    from app.services.search_service import SearchService
                    search_service = SearchService(db)
                    url_context = ""
                    urls = SearchService.extract_urls(text + " " + reply_text)

                    # Deduplicate URLs: www.example.com and example.com are the same article.
                    # Normalize by stripping scheme + www prefix for comparison.
                    if urls:
                        def _url_key(u: str) -> str:
                            import re as _re
                            return _re.sub(r'^https?://(www\.)?', '', u.lower().rstrip('/'))
                        seen_keys: set = set()
                        deduped: list = []
                        for u in urls:
                            k = _url_key(u)
                            if k not in seen_keys:
                                seen_keys.add(k)
                                deduped.append(u)
                        if len(deduped) < len(urls):
                            logger.info(f"Telegram: Deduplicated URLs {urls} -> {deduped}")
                        urls = deduped

                    # Check if message is ONLY a URL (no other text)
                    is_only_url = False
                    if urls and len(text.strip()) < 500:
                        text_without_urls = text
                        for url in urls:
                            text_without_urls = text_without_urls.replace(url, '').strip()
                            if url.startswith("https://"):
                                text_without_urls = text_without_urls.replace(url[len("https://"):], '').strip()
                        is_only_url = not text_without_urls

                    # If message is only a URL, ask what the user wants to do with it.
                    # Embed the URL in the message text so the lnk: callback can recover
                    # it from the message if the in-memory cache is lost (e.g. server restart).
                    if is_only_url and urls:
                        _link_action_cache[chat_id] = urls[0]
                        await telegram_service.send_message(
                            chat_id,
                            f"🔗 What would you like to do with this link?\n{urls[0]}",
                            reply_markup={
                                "inline_keyboard": [
                                    [
                                        {"text": "📋 Summary",    "callback_data": "lnk:summary"},
                                        {"text": "📸 Screenshot", "callback_data": "lnk:screenshot"},
                                        {"text": "🎴 Flashcards", "callback_data": "lnk:flashcards"},
                                    ],
                                    [
                                        {"text": "📣 Post",   "callback_data": "lnk:post"},
                                        {"text": "❌ Cancel", "callback_data": "lnk:cancel"},
                                    ],
                                ]
                            },
                        )
                        return {"ok": True}

                    if urls:
                        logger.info(f"Telegram: Detected URLs in message: {urls}")
                        MAX_URL_CONTENT_CHARS = 2000  # Truncation only — no content cleaning
                        try:
                            fetched = await asyncio.wait_for(
                                search_service.fetch_urls(urls, max_urls=3),
                                timeout=15
                            )
                            for result in fetched:
                                if result.get("content") and not result.get("error"):
                                    content = result['content']
                                    if len(content) > MAX_URL_CONTENT_CHARS:
                                        content = content[:MAX_URL_CONTENT_CHARS] + "\n...[content truncated]"
                                    logger.info(f"Telegram: Fetched {len(result['content'])} chars (using {len(content)}) from {result['url']}")
                                    url_context += f"\n\n---\nContent from {result['url']}:\nTitle: {result['title']}\n\n{content}\n---"
                                elif result.get("error"):
                                    logger.warning(f"Telegram: Failed to fetch {result['url']}: {result['error']}")
                                    url_context += f"\n\n[Failed to fetch {result['url']}: {result['error']}]"
                        except asyncio.TimeoutError:
                            logger.warning(f"Telegram: URL fetching timed out for: {urls}")
                            url_context = "\n\n[Note: Could not fetch URL content due to timeout]"
                    
                    # Append URL context to user message if URLs were found
                    if url_context:
                        injected = url_context

                        if injected:
                            if isinstance(messages[-1]["content"], list):
                                messages[-1]["content"].append({"type": "text", "text": injected})
                            else:
                                messages[-1]["content"] += injected
                            logger.info(f"Telegram: Added URL context ({len(url_context)} chars) to message")
                    
                    if len(messages) > 1:
                        user_content = messages[1]['content']
                        logger.info(f"Final messages structure: system={messages[0]['content'][:50]}..., user content type={type(user_content)}")
                        if isinstance(user_content, list):
                            logger.info(f"User content has {len(user_content)} parts")

                    # FINAL VALIDATION: Ensure messages alternate properly
                    validated_messages = [messages[0]]  # Keep system message
                    for msg in messages[1:]:
                        if msg['role'] != validated_messages[-1]['role']:
                            validated_messages.append(msg)
                        else:
                            # Merge with previous same-role message; handle list content gracefully
                            prev = validated_messages[-1]
                            prev_content = prev['content']
                            msg_content = msg['content']
                            if isinstance(prev_content, list) or isinstance(msg_content, list):
                                # Convert both sides to string for merging
                                prev_str = str(prev_content) if isinstance(prev_content, list) else prev_content
                                msg_str = str(msg_content) if isinstance(msg_content, list) else msg_content
                                prev['content'] = prev_str + f"\n\n{msg_str}"
                            else:
                                prev['content'] += f"\n\n{msg_content}"
                    messages = validated_messages
                    logger.info(f"Validated message sequence: {[m['role'] for m in messages]}")
                    
                    # Log messages for debugging
                    for i, m in enumerate(messages):
                        content_preview = str(m.get('content', ''))[:50] if not isinstance(m.get('content'), list) else '[vision content]'
                        logger.info(f"  Message {i}: role={m.get('role')}, content={content_preview}...")
                    
                    try:
                        result = {"type": "text", "content": await chat_service.chat(messages)}
                    except Exception as chat_err:
                        error_msg = str(chat_err)
                        logger.error(f"Telegram chat error: {error_msg}", exc_info=True)
                        if "Conversation roles must alternate" in error_msg:
                            logger.error(f"ROLE ERROR - Messages that caused error:")
                            for i, m in enumerate(messages):
                                content_preview = str(m.get('content', ''))[:100] if not isinstance(m.get('content'), list) else '[vision content]'
                                logger.error(f"  Message {i}: role={m.get('role')}, content={content_preview}...")
                        result = {"type": "text", "content": f"Sorry, I encountered an error: {error_msg}"}

                    # Save user message + bot response to the Telegram conversation so
                    # follow-up messages ("turn that into a post", "translate it", etc.)
                    # have the context they need.
                    try:
                        tg_conv = db.query(Conversation).filter(
                            Conversation.user_id == user_obj.id,
                            Conversation.title == "📱 Telegram"
                        ).order_by(Conversation.updated_at.desc()).first()
                        if not tg_conv:
                            tg_conv = Conversation(user_id=user_obj.id, title="📱 Telegram")
                            db.add(tg_conv)
                            db.flush()
                        # Save the raw user text (not the injected URL content — keep history short).
                        # Save the full bot reply so follow-ups ("turn that into a post") have
                        # complete context — truncating to 500 chars cut off summaries mid-sentence.
                        db.add(Message(conversation_id=tg_conv.id, role="user", content=text))
                        bot_reply = result.get("content", "")
                        APOLOGY = "I apologize, I wasn't able to generate a proper response. Please try again."
                        # Don't save errors, apologies, or truncated responses (they corrupt future context)
                        _reply_looks_complete = bot_reply and not (len(bot_reply) < 80 and bot_reply.rstrip().endswith(":"))
                        if _reply_looks_complete and bot_reply != APOLOGY and not bot_reply.startswith("Error:") and not bot_reply.startswith("Sorry,"):
                            db.add(Message(conversation_id=tg_conv.id, role="assistant", content=bot_reply))
                        tg_conv.updated_at = datetime.utcnow()
                        db.commit()
                    except Exception as _save_err:
                        logger.warning(f"Failed to save Telegram history: {_save_err}")
                        try:
                            db.rollback()
                        except Exception:
                            pass
            
            # Handle the result
            response_type = result.get("type", "text")
            response_content = result.get("content", "")
            image_data = result.get("image")
            
            # Clean response content - remove template artifacts and any leaked thinking
            if response_content:
                from app.services.text_utils import strip_thinking_tags
                response_content = strip_thinking_tags(response_content)
                # Remove template tokens
                for pattern in [r'\[INST\]', r'\[/INST\]', r'INST\]', r'<\|im_end\|>', r'<\|im_start\|>']:
                    response_content = re.sub(pattern, '', response_content, flags=re.IGNORECASE)
                # Remove orphan brackets
                response_content = re.sub(r'\[(?=\s|$)', '', response_content)
                response_content = re.sub(r'^\]', '', response_content)
                response_content = response_content.strip()
                
                if not response_content:
                    response_content = "I didn't get a proper response. Please try again."
            
            logger.info(f"Result type: {response_type}, has image: {bool(image_data)}")
            
            if response_type == "generated_image" and image_data and result.get("prefer_document"):
                # Screenshots: deliver document-first (full resolution) and skip the
                # photo/social-share path, which compresses the image too small to read.
                logger.info(f"Screenshot detected, sending as document, image length: {len(image_data)}")
                await _send_screenshot(chat_id, image_data, response_content)
            elif response_type == "generated_image" and image_data:
                logger.info(f"Generated image detected, sending via Telegram, image length: {len(image_data)}")
                photo_result = await telegram_service.send_photo(chat_id, image_data, response_content)
                if not photo_result.get("ok"):
                    logger.error(f"Failed to send photo: {photo_result}")
                    # Telegram rejects photos that are too tall/large (common for full-page
                    # screenshots) — retry as a document, which has far looser limits.
                    if not await _send_png_as_document(chat_id, image_data, response_content):
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Image failed to send)")
                else:
                    # Offer to share the generated image to configured social platforms
                    _geni_user = db.query(User).filter(
                        User.telegram_chat_id == chat_id,
                        User.telegram_enabled == True
                    ).first()
                    if _geni_user and (_has_misskey(_geni_user) or _has_pleroma(_geni_user) or _has_matrix(_geni_user)):
                        _geni_caption = response_content or "Generated image"
                        # Store image BYTES so Matrix/Misskey/Pleroma share paths (which all
                        # pass this as image_bytes to send_image) get raw bytes — matching the
                        # pasted-image path. image_data from generate_image is base64 (optionally
                        # a data: URL); storing the base64 string broke image posts on all
                        # platforms (Matrix most visibly).
                        _geni_bytes = image_data
                        if isinstance(_geni_bytes, str):
                            import base64 as _geni_b64
                            if _geni_bytes.startswith("data:image"):
                                _geni_bytes = _geni_bytes.split(",", 1)[1]
                            try:
                                _geni_bytes = _geni_b64.b64decode(_geni_bytes)
                            except Exception:
                                _geni_bytes = None
                        # Store caption in platform caches using the same offer-post format so
                        # message-text recovery strips the suffix correctly on restart
                        _misskey_post_cache[chat_id] = _geni_caption
                        _pleroma_post_cache[chat_id] = _geni_caption
                        _matrix_post_cache[chat_id] = _geni_caption
                        await _offer_social_post(
                            chat_id, _geni_caption, _geni_user, telegram_service,
                            prompt="📣 *Share this image?*", image_bytes=_geni_bytes
                        )
            elif response_type == "generated_video" and result.get("video"):
                # Branded MP4 from musicgeni (song-over-bg) OR videogeni (generated clip): decode to
                # a temp file and send as a Telegram video.
                import base64 as _mv_b64, tempfile as _mv_tmp, os as _mv_os
                _mv_path = None
                try:
                    _mv_bytes = _mv_b64.b64decode(result["video"])
                    fd, _mv_path = _mv_tmp.mkstemp(prefix="tg_music_", suffix=".mp4")
                    with _mv_os.fdopen(fd, "wb") as _f:
                        _f.write(_mv_bytes)
                    _mv_res = await telegram_service.send_video(chat_id, _mv_path, caption=response_content)
                    if not _mv_res.get("ok"):
                        logger.error(f"Failed to send generated music video: {_mv_res}")
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Song failed to send)")
                except Exception as _mv_err:
                    logger.error(f"Generated music video send error: {_mv_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, "🎵 Couldn't deliver the generated song.")
                finally:
                    if _mv_path and _mv_os.path.exists(_mv_path):
                        try:
                            _mv_os.unlink(_mv_path)
                        except Exception:
                            pass
            elif response_type == "generated_audio" and result.get("audio"):
                # Generated song (musicgeni): decode the base64 audio to a temp file and send it
                # as a Telegram audio message.
                import base64 as _mg_b64, tempfile as _mg_tmp, os as _mg_os
                _mg_fmt = (result.get("format") or "mp3").lower()
                _mg_path = None
                try:
                    _mg_bytes = _mg_b64.b64decode(result["audio"])
                    fd, _mg_path = _mg_tmp.mkstemp(prefix="tg_music_", suffix="." + _mg_fmt)
                    with _mg_os.fdopen(fd, "wb") as _f:
                        _f.write(_mg_bytes)
                    _mg_res = await telegram_service.send_audio(
                        chat_id, _mg_path, title="PosterChanAI", caption=response_content,
                    )
                    if not _mg_res.get("ok"):
                        logger.error(f"Failed to send generated audio: {_mg_res}")
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Song failed to send)")
                except Exception as _mg_err:
                    logger.error(f"Generated audio send error: {_mg_err}", exc_info=True)
                    await telegram_service.send_message(chat_id, "🎵 Couldn't deliver the generated song.")
                finally:
                    if _mg_path and _mg_os.path.exists(_mg_path):
                        try:
                            _mg_os.unlink(_mg_path)
                        except Exception:
                            pass
            elif response_type == "flashcards":
                # Interactive multiple-choice study quiz — store the deck per chat and send card 0
                # as a PNG with answer buttons; fc: callbacks navigate/reveal in place.
                _fc_cards = result.get("cards") or []
                if not _fc_cards:
                    await telegram_service.send_message(chat_id, response_content or "Couldn't make flashcards.")
                else:
                    if result.get("note"):
                        await telegram_service.send_message(chat_id, result["note"])
                    _deck = {
                        "title": result.get("title") or "Flashcards",
                        "cards": _fc_cards,
                        "idx": 0,
                        "answered": [None] * len(_fc_cards),
                        "score": 0,
                        "ts": time.time(),
                    }
                    _flashcard_decks_cache[chat_id] = _deck
                    await _send_flashcard(chat_id, _deck)
            elif response_type == "search":
                # Send AI summary, then append top result links
                search_results = result.get("results", [])
                links = ""
                if search_results:
                    link_lines = []
                    for r in search_results[:5]:
                        title = (r.get("title") or r.get("url", ""))[:60]
                        url = r.get("url", "")
                        if url:
                            link_lines.append(f"• [{title}]({url})")
                    if link_lines:
                        links = "\n\n**Sources:**\n" + "\n".join(link_lines)
                await telegram_service.send_message(chat_id, response_content + links)
            elif response_type == "images":
                images = result.get("images", [])
                if not images:
                    await telegram_service.send_message(chat_id, response_content)
                else:
                    await telegram_service.send_message(chat_id, response_content)
                    for img in images:
                        img_url = img.get("img_src", "")
                        page_url = img.get("url", img_url)
                        title = (img.get("title") or "")[:80]
                        if not img_url:
                            continue
                        caption = f"[{title}]({page_url})" if title and page_url else title
                        photo_result = await telegram_service.send_photo(chat_id, img_url, caption or None)
                        if not photo_result.get("ok"):
                            logger.warning(f"Could not send image {img_url}: {photo_result.get('description', '')}")
                        await asyncio.sleep(0.15)
            elif response_type == "files":
                # compress/convert output — send each file back as a document
                files = result.get("files", [])
                if response_content:
                    await telegram_service.send_message(chat_id, response_content)
                for f in files:
                    f_bytes = f.get("data")
                    f_name = f.get("filename", "file")
                    if not f_bytes:
                        continue
                    send_result = await telegram_service.send_document_bytes(chat_id, f_bytes, f_name)
                    if not send_result.get("ok"):
                        logger.error(f"Failed to send file {f_name}: {send_result}")
                        await telegram_service.send_message(chat_id, f"❌ Failed to send {f_name}")
                    await asyncio.sleep(0.15)
                # After an effect, offer to post the result to the user's timeline.
                if command in CommandService.MOTION_EFFECTS:
                    _share = next(
                        (f for f in files if f.get("data") and (f.get("content_type") or "").startswith(("image/", "video/"))),
                        None,
                    )
                    _share_u = db.query(User).filter(
                        User.telegram_chat_id == chat_id, User.telegram_enabled == True
                    ).first() if _share else None
                    if _share and _share_u and (_has_misskey(_share_u) or _has_pleroma(_share_u) or _has_matrix(_share_u)):
                        _media_action_cache[chat_id] = {
                            "attachments": [(_share.get("filename", "file"), _share["data"], _share.get("content_type", ""))],
                            "ts": time.time(),
                        }
                        await telegram_service.send_message(
                            chat_id, "📣 Post this to your timeline?",
                            reply_markup={"inline_keyboard": [[
                                {"text": "📣 Post to social", "callback_data": "media:post"},
                            ]]},
                        )
            else:
                await telegram_service.send_message(chat_id, response_content, reply_markup=reply_markup)

            return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram message handler error: {e}", exc_info=True)

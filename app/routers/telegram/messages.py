"""Auto-split from webhook.py: the message half of _handle_telegram_update."""
from .messages_command import _msg_command
from .messages_chat import _msg_chat
from ._common import ChatService, CommandService, Conversation, Message, User, _CLIP_END_PROMPT, _CLIP_START_PROMPT, _EFFECT_CAPTION_PROMPT, _MEDIA_ACTION_TTL, _MEDIA_GROUP_CACHE, _MEME_PROMPT, _SOCIAL_CAPTION_PROMPT, _clip_pending, _effect_caption_pending, _effect_char_pending, _flashcard_decks_cache, _link_action_cache, _media_action_cache, _news_post_cache, _pleroma_post_cache, _youtube_action_cache, asyncio, datetime, logger, re, telegram_service, time
from .keyboards import _4chan_initial_keyboard, _build_torrent_keyboard, _character_prompt_keyboard, _has_nostr, _has_pleroma, _help_main_keyboard, _media_action_keyboard, _news_menu_keyboard, _split_news_into_articles, _strip_cmd_links, _strip_hashtags, _torrent_nav_keyboard, re
from .senders import User, _has_pleroma, _media_action_cache, _offer_social_post, _offer_ytdl_share, _offer_ytdl_video_actions, _pleroma_post_cache, _send_4chan_catalog, _send_active_torrents, _send_flashcard, _send_nyaa_results, _send_png_as_document, _send_screenshot, _send_torrent_results, _strip_cmd_links, _torrent_nav_keyboard, asyncio, datetime, logger, re, telegram_service, time

# Telegram matches command words LITERALLY (it never calls parse_command), so it needs its own list —
# but only of the NON-effect commands. The effects come from CommandService, because a second copy of
# them drifts: the hand-written one had already lost `goon`/`hag`, and renaming `anyways` →
# `lookingaway` left the new name falling through to the LLM. Order matters (first match wins), so the
# literals keep theirs and the derived effects — all single words, none of them a prefix of a literal —
# are appended.
_TG_BASE_COMMANDS = [
    "help", "new", "ytdl", "geni", "musicgeni", "videogeni", "narrate", "voice", "mail", "news", "dailynews",
    "search", "images", "yt", "torrents", "nyaa", "4chan", "logs", "translate", "post", "share",
    "remind", "reminders", "pin", "pins", "removebackground", "compress", "clip", "convert",
    "extractaudio", "circlecrop", "ocr", "flashcards",
    "node", "bill", "budget", "bills", "pay", "addbill", "finance", "screenshot", "shot", "ss",
]
_TG_EFFECTS = set(CommandService.MOTION_EFFECTS) | set(CommandService.ANIMATED_EFFECTS)
# The effects' OLD names have to be matchable too — aliases are resolved AFTER this match, so a word
# that isn't here never gets as far as COMMAND_ALIASES (that's what keeps `anyways` working).
_TG_EFFECT_WORDS = _TG_EFFECTS | {k for k, v in CommandService.COMMAND_ALIASES.items() if v in _TG_EFFECTS}
_TG_COMMANDS = _TG_BASE_COMMANDS + sorted(_TG_EFFECT_WORDS - set(_TG_BASE_COMMANDS))
# Commands that consume the upload's raw BYTES: OCR'ing the image for them is wasted work (they never
# read the text), and an oversized one has to be reported rather than fed to the chat model.
_TG_RAW_MEDIA_COMMANDS = _TG_EFFECTS | {
    "compress", "removebackground", "clip", "convert", "extractaudio", "circlecrop", "flashcards",
    # `voice` clones the speaker in the attached clip — it needs the audio BYTES, not OCR'd text.
    "voice",
}


async def _handle_message(update, db):
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
            }
            reply_from = (reply_to or {}).get("from", {})
            if reply_from.get("is_bot") and text.strip():
                route = _FORCE_REPLY_ROUTES.get(reply_text.strip())
                if route:
                    text = f"{route} {text.strip()}"
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
                    # execute_command(command, arg, ...) — the caption is the ARG. A bulk find/replace
                    # that added theraped/would/shrug to the command allowlists hit this CALL too, so
                    # it was passing them as arg/last_prompt/stop_check and `_caption` positionally as
                    # `attachments`, which also arrived as a keyword: TypeError on every use. The
                    # surrounding try/except turned that into a plain "Meme failed" — Telegram's
                    # "add a caption to this image" flow has not worked since.
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
            commands = _TG_COMMANDS
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
            commands = _TG_COMMANDS
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

            # OCR every image up front so a later step can use the text — but NOT for the commands
            # that work on the RAW FILE (compress/convert/every effect): they never read it, so the
            # OCR is pure latency on the upload path.
            if has_images and attachments and command not in _TG_RAW_MEDIA_COMMANDS:
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
            if oversized_attachment and (command is None or command in _TG_RAW_MEDIA_COMMANDS):
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
                        f"in Admin → Nodes."
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
                _r = await _msg_command(_make_tg_node_notify, arg, attachments, chat_id, command, command_service, db, has_images, reply_to, text, user_obj)
                if not (isinstance(_r, dict) and "type" in _r):
                    return _r if isinstance(_r, dict) else {"ok": True}
                result = _r
            else:
                # Regular chat - check for images and do OCR or pass to vision model
                _r = await _msg_chat(attachments, chat_id, chat_service, command_service, db, doc_text, has_images, is_forwarded, message, reply_text, text, user_obj)
                if not (isinstance(_r, dict) and "type" in _r):
                    return _r if isinstance(_r, dict) else {"ok": True}
                result = _r
            
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
                    if _geni_user and (_has_pleroma(_geni_user) or _has_nostr(_geni_user)):
                        _geni_caption = response_content or "Generated image"
                        # Store image BYTES so the Pleroma/Nostr share paths (which all
                        # pass this as image_bytes to send_image) get raw bytes — matching the
                        # pasted-image path. image_data from generate_image is base64 (optionally
                        # a data: URL); storing the base64 string broke image posts on all
                        # platforms.
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
                        _pleroma_post_cache[chat_id] = _geni_caption
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
                    if _share and _share_u and (_has_pleroma(_share_u) or _has_nostr(_share_u)):
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

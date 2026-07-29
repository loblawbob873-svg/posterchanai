"""Auto-split from messages.py: _msg_chat."""
from ._common import Conversation, Message, User, _link_action_cache, _youtube_action_cache, asyncio, datetime, logger, telegram_service
from .keyboards import _has_nostr, _has_pleroma
from .senders import User, _has_pleroma, asyncio, datetime, logger, telegram_service


async def _msg_chat(attachments, chat_id, chat_service, command_service, db, doc_text, has_images, is_forwarded, message, reply_text, text, user_obj):
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
                    if _has_pleroma(_yt_user_for_social) or _has_nostr(_yt_user_for_social):
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
                    if _has_pleroma(_x_user_for_social) or _has_nostr(_x_user_for_social):
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

                # END THE TRANSACTION before the long part. Everything below — intent detection, a
                # command, or a plain LLM reply — can run for MINUTES (a 3-5 minute song waits for the
                # GPU lock, generates, then renders an MP4), and the reads above have already opened a
                # transaction. Connections carry `idle_in_transaction_session_timeout=60000`
                # (app/database.py), so Postgres kills any session that sits in an open transaction for
                # 60s: the history save below then failed with "Failed to save Telegram history" and the
                # user's message AND the bot's reply were silently dropped, and the session close at the
                # end of _process_telegram_update raised OperationalError as an unhandled ASGI error.
                # Committing here leaves the connection merely IDLE, which that timeout does not touch;
                # the save below opens a fresh transaction when it needs one.
                try:
                    db.commit()
                except Exception as _txn_err:
                    logger.warning(f"pre-generation commit failed: {_txn_err}")
                    try:
                        db.rollback()
                    except Exception:
                        pass

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
                    # (Conversation/Message come from the module-level import; a local re-import
                    # here would make them function-local and break the earlier `new` command.)

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
                            # History comes from the ENCRYPTED relay transcript, not plaintext rows.
                            from app.services import chat_history
                            recent_messages = (await chat_history.load(db, user_obj, conversation.id))[-6:]

                            HISTORY_CHAR_LIMIT = 2000  # large enough to hold a full URL summary
                            for msg in recent_messages:
                                _role = msg.get("role") or ""
                                if not _role or _role == last_role:
                                    continue
                                # Don't feed prior code-block replies back as context — they make
                                # the model keep emitting code (self-perpetuating loop). Skip them.
                                if _role == "assistant" and "```" in (msg.get("content") or ""):
                                    continue
                                messages.append({"role": _role, "content": (msg.get("content") or "")[:HISTORY_CHAR_LIMIT]})
                                last_role = _role
                    
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
                    # Defined OUT here because the retry in the except clause needs it too — inside
                    # the try it is unbound if the failure lands before this line.
                    APOLOGY = "I apologize, I wasn't able to generate a proper response. Please try again."
                    # Plain int, read while the session is still usable. rollback() EXPIRES every ORM
                    # instance, so touching `user_obj.id` in the except would re-SELECT it on the very
                    # connection that just died — the recovery would die with it.
                    _uid = user_obj.id
                    # What actually reached the transcript. `chat_history.append` self-heals on a
                    # fresh session and returns False rather than raising, so the recovery below must
                    # re-send only what is genuinely missing — re-sending blindly would DUPLICATE a
                    # turn whenever the failure landed on the commit instead of the lookup.
                    _did_user = _did_bot = False
                    # Decided BEFORE the try for the same reason: if the connection died during the
                    # generation, the very first statement below is what raises, and the recovery still
                    # has to know whether this reply was worth keeping.
                    bot_reply = result.get("content", "")
                    # Don't save errors, apologies, or truncated responses (they corrupt future context)
                    _reply_looks_complete = bot_reply and not (len(bot_reply) < 80 and bot_reply.rstrip().endswith(":"))
                    _bot_worth_saving = bool(_reply_looks_complete and bot_reply != APOLOGY
                                             and not bot_reply.startswith(("Error:", "Sorry,")))
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
                        from app.services import chat_history as _ch
                        _did_user = await _ch.append(db, user_obj, tg_conv.id, "user", text)
                        if _bot_worth_saving:
                            _did_bot = await _ch.append(db, user_obj, tg_conv.id, "assistant", bot_reply)
                        tg_conv.updated_at = datetime.utcnow()
                        db.commit()
                    except Exception as _save_err:
                        # A dead connection is the expected failure here (a generation long enough to
                        # outlive the session), and swallowing it is how history silently disappeared.
                        # Retry the whole save in a FRESH session so the turn survives it.
                        logger.warning(f"Failed to save Telegram history: {_save_err}; retrying in a fresh session")
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        try:
                            from app.database import SessionLocal
                            from app.services import chat_history as _ch2
                            _s = SessionLocal()
                            try:
                                _u = _s.query(User).filter(User.id == _uid).first()
                                if _u is None:
                                    raise RuntimeError(f"user {_uid} is gone")
                                _c = _s.query(Conversation).filter(
                                    Conversation.user_id == _u.id,
                                    Conversation.title == "📱 Telegram"
                                ).order_by(Conversation.updated_at.desc()).first()
                                if not _c:
                                    _c = Conversation(user_id=_u.id, title="📱 Telegram")
                                    _s.add(_c)
                                    _s.flush()
                                if not _did_user:
                                    await _ch2.append(_s, _u, _c.id, "user", text)
                                if _bot_worth_saving and not _did_bot:
                                    await _ch2.append(_s, _u, _c.id, "assistant", bot_reply)
                                _c.updated_at = datetime.utcnow()
                                _s.commit()
                                logger.info("Telegram history saved on the retry "
                                            f"(user={not _did_user}, assistant={_bot_worth_saving and not _did_bot})")
                            finally:
                                try:
                                    _s.close()
                                except Exception:
                                    pass
                        except Exception as _retry_err:
                            logger.error(f"Telegram history retry ALSO failed: {_retry_err}")
                return result

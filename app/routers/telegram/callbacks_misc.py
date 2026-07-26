"""Auto-split from callbacks.py: misc callback handlers. Bodies moved verbatim."""
from ._common import ChatService, CommandService, User, _CONSUMED, _HELP_SECTIONS, _flashcard_decks_cache, _geni_image_cache, _link_action_cache, _misskey_post_cache, _nostr_post_cache, _pleroma_post_cache, asyncio, logger, telegram_service, time
from .keyboards import _has_misskey, _has_nostr, _has_pleroma, _help_main_keyboard, _recover_post_text, _strip_hashtags
from .senders import User, _deliver_pin_result, _geni_image_cache, _has_misskey, _has_nostr, _has_pleroma, _link_content_for_llm, _misskey_post_cache, _offer_social_post, _pleroma_post_cache, _post_to_nostr, _send_flashcard, _send_screenshot, asyncio, logger, telegram_service, time


async def _cb_rem(update, db, chat_id, data, callback_query, callback_query_id):
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


async def _cb_pin(update, db, chat_id, data, callback_query, callback_query_id):
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
                    chat_id, "🗑️ Pin deleted." if ok else "No matching pin.")
            elif parts[1] == "run":
                s = next((x for x in saved_search_service.list_saved_searches(db, cb_user) if x.id == sid), None)
                if not s:
                    await telegram_service.send_message(chat_id, "That pin is gone.")
                else:
                    # Resolve the pin to a real command: a bare query → `search <query>`, a
                    # command word (screenshot/geni/news/…) → that command verbatim. Then run it
                    # and deliver whatever it produces (text/image/video/files/…) generically.
                    _svc = CommandService(db, user=cb_user)
                    _cmd, _arg = _svc.parse_command(s.query)
                    if _cmd is None:
                        _cmd, _arg = "search", saved_search_service.normalize_query(s.query)
                    await telegram_service.send_message(
                        chat_id, f"▶ Running: {(_cmd + ' ' + _arg).strip()}", parse_mode="")
                    try:
                        _res = await _svc.execute_command(_cmd, _arg)
                    except Exception as _pin_err:
                        logger.error(f"pin run error: {_pin_err}", exc_info=True)
                        _res = {"type": "text", "content": f"Error running pin: {_pin_err}"}
                    await _deliver_pin_result(chat_id, _res)
        return {"ok": True}


async def _cb_prompt(update, db, chat_id, data, callback_query, callback_query_id):
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


async def _cb_help(update, db, chat_id, data, callback_query, callback_query_id):
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


async def _cb_lnk(update, db, chat_id, data, callback_query, callback_query_id):
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


async def _cb_glowtextpost(update, db, chat_id, data, callback_query, callback_query_id):
        _gp = (_misskey_post_cache.get(chat_id) or _pleroma_post_cache.get(chat_id)
               or _nostr_post_cache.get(chat_id))
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


async def _cb_mk(update, db, chat_id, data, callback_query, callback_query_id):
        action = data.split(":", 1)[1]

        if action == "skip":
            # Clear all social post caches so stale posts can't be sent
            _misskey_post_cache.pop(chat_id, None)
            _pleroma_post_cache.pop(chat_id, None)
            _nostr_post_cache.pop(chat_id, None)
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


async def _cb_plr(update, db, chat_id, data, callback_query, callback_query_id):
        action = data.split(":", 1)[1]

        if action == "skip":
            _misskey_post_cache.pop(chat_id, None)
            _pleroma_post_cache.pop(chat_id, None)
            _nostr_post_cache.pop(chat_id, None)
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


async def _cb_nostr(update, db, chat_id, data, callback_query, callback_query_id):
        action = data.split(":", 1)[1]

        if action == "skip":
            _misskey_post_cache.pop(chat_id, None)
            _pleroma_post_cache.pop(chat_id, None)
            _nostr_post_cache.pop(chat_id, None)
            _geni_image_cache.pop(chat_id, None)
            await telegram_service.send_message(chat_id, "Post skipped.")
            return {"ok": True}

        # action == "post"
        pending_post = _nostr_post_cache.pop(chat_id, None)
        if pending_post == _CONSUMED:
            await telegram_service.send_message(chat_id, "Already posted via 'Post to All'.")
            return {"ok": True}
        if pending_post is None:
            pending_post = _recover_post_text(callback_query) or None
        if pending_post is None:
            await telegram_service.send_message(chat_id, "No pending Nostr post found. Please generate a new post.")
            return {"ok": True}

        nostr_user = db.query(User).filter(
            User.telegram_chat_id == chat_id,
            User.telegram_enabled == True
        ).first()

        if not nostr_user or not _has_nostr(nostr_user):
            await telegram_service.send_message(chat_id, "Nostr is not configured on your account.")
            return {"ok": True}

        _nostr_image = _geni_image_cache.get(chat_id)  # .get so other platforms can still use it
        try:
            await _post_to_nostr(nostr_user, pending_post, _nostr_image)
            await telegram_service.send_message(chat_id, "✅ Posted to Nostr!")
        except Exception as nostr_err:
            logger.error(f"Nostr post error: {nostr_err}", exc_info=True)
            await telegram_service.send_message(chat_id, f"❌ Failed to post to Nostr: {nostr_err}")




async def _cb_allpost(update, db, chat_id, data, callback_query, callback_query_id):
        all_user = db.query(User).filter(
            User.telegram_chat_id == chat_id,
            User.telegram_enabled == True
        ).first()

        # Recover post text from message if caches were lost (e.g. service restart).
        # Shared _recover_post_text() strips the prompt and refuses to post a bare
        # prompt — same helper used by the individual mk:/plr: handlers.

        results = []

        _all_image = _geni_image_cache.get(chat_id)

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

        # Nostr
        nostr_post = _nostr_post_cache.pop(chat_id, None) or _recover_post_text(callback_query)
        if (nostr_post or _all_image) and nostr_post != _CONSUMED and all_user and _has_nostr(all_user):
            try:
                await _post_to_nostr(all_user, nostr_post, _all_image)
                results.append("✅ Nostr")
            except Exception as _e:
                logger.error(f"all:post Nostr error: {_e}", exc_info=True)
                results.append(f"❌ Nostr: {_e}")
            # Sentinel prevents the old Nostr button from double-posting
            _nostr_post_cache[chat_id] = _CONSUMED

        if results:
            await telegram_service.send_message(chat_id, "\n".join(results))

        if not results:
            await telegram_service.send_message(chat_id, "No social platforms configured.")

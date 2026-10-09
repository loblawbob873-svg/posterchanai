"""Auto-split from callbacks.py: misc callback handlers. Bodies moved verbatim."""
from ._common import ChatService, CommandService, User, _HELP_SECTIONS, _link_action_cache, asyncio, logger, telegram_service, time
from .keyboards import _help_main_keyboard
from .senders import _deliver_pin_result, _link_content_for_llm, _send_screenshot, asyncio, logger, telegram_service, time


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
                    # command word (screenshot/geni/…) → that command verbatim. Then run it
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
            "geni":     ("🎨 Describe the image you want to generate:", "e.g. a sunset over a cyberpunk city"),
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

        elif action == "share":
            # 📣 Share to Social: the same `post` command the web app runs (a kind-1 from the account's
            # linked key to this node's relay, which federates it and sends it on to the fediverse).
            try:
                share_res = await CommandService(db, user=lnk_user).execute_command("post", cached_url)
                said = str((share_res or {}).get("content") or "")
                if "✅" in said:
                    await telegram_service.send_message(chat_id, "📣 Shared to Social:\n" + cached_url)
                else:   # no linked key, relay refused, ... -- say what the command said, without its markdown
                    await telegram_service.send_message(chat_id, said.replace("**", "").replace("`", "") or "Could not share that link.")
            except Exception as lnk_err:
                logger.error(f"Link share error: {lnk_err}", exc_info=True)
                await telegram_service.send_message(chat_id, f"Could not share that link: {lnk_err}")

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

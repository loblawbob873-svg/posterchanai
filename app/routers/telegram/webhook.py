"""Auto-split from the original telegram.py monolith. No behavior change."""
from .messages import _handle_message
from .callbacks import _handle_callback
from app.services import settings_store
from ._common import BackgroundTasks, ChatService, CommandService, Conversation, Message, Session, SessionLocal, User, _CLIP_END_PROMPT, _CLIP_START_PROMPT, _CONSUMED, _EFFECT_CAPTION_PROMPT, _FLASHCARD_TTL, _HELP_SECTIONS, _MAX_SEEN_IDS, _MEDIA_ACTION_TTL, _MEDIA_GROUP_CACHE, _MEME_PROMPT, _SOCIAL_CAPTION_PROMPT, _clip_pending, _configure_telegram, _effect_caption_pending, _effect_char_pending, _flashcard_decks_cache, _geni_image_cache, _link_action_cache, _media_action_cache, _misskey_post_cache, _news_post_cache, _news_source_cache, _pleroma_post_cache, _youtube_action_cache, asyncio, datetime, logger, re, router, telegram_service, time
from .keyboards import _4chan_initial_keyboard, _build_torrent_keyboard, _character_prompt_keyboard, _has_misskey, _has_pleroma, _help_main_keyboard, _media_action_keyboard, _media_effects_keyboard, _media_fx_memes_keyboard, _media_fx_sounds_keyboard, _media_fx_themes_keyboard, _media_translate_keyboard, _news_menu_keyboard, _recover_post_text, _split_news_into_articles, _strip_cmd_links, _strip_hashtags, _torrent_nav_keyboard, _torrents_menu_keyboard
from .senders import _deliver_files_result, _link_content_for_llm, _offer_social_post, _offer_ytdl_share, _offer_ytdl_video_actions, _send_4chan_catalog, _send_4chan_thread, _send_active_torrents, _send_flashcard, _send_news_source_selector, _send_nyaa_results, _send_png_as_document, _send_screenshot, _send_torrent_results

_seen_update_ids: set = set()
_seen_msg_keys: set = set()
_seen_cq_ids: set = set()


def _make_tg_node_notify(telegram_service, chat_id):
    """Build an async callback that DMs a finished `node` job's output to this chat.
    Used so long-running node jobs started from Telegram report back here when done."""
    async def _notify(job):
        # Agent step-streaming passes a plain string (e.g. "⚙️ `cmd`"); a finished background AGENT passes
        # a {"type":"agent_result", ...} dict; job-completion passes a Job.
        if isinstance(job, str):
            try:
                await telegram_service.send_message(str(chat_id), job)
            except Exception as e:
                logger.warning(f"[node] telegram step notify failed: {e}")
            return
        if isinstance(job, dict) and job.get("type") == "agent_result":
            # `node agent …` now runs in the background; deliver its summary here when it's done, so the
            # user can fire it off and walk away (and a long run won't stall the webhook). Long summaries
            # go as a .txt so we never trip Telegram's 4096-char message limit.
            atext = (job.get("content") or "").strip() or "(the agent produced no summary)"
            try:
                if len(atext) > 3800:
                    await telegram_service.send_message(str(chat_id), atext[:3800] + "\n\n…(full summary attached)")
                    await telegram_service.send_document_bytes(str(chat_id), atext.encode("utf-8", "replace"), "node-agent-summary.txt")
                else:
                    await telegram_service.send_message(str(chat_id), atext)
            except Exception as e:
                logger.warning(f"[node] telegram agent-result send failed: {e}")
            return
        if isinstance(job, dict) and job.get("type") == "agent_files":
            # Files the agent handed back (e.g. its /workspace backup) → deliver each as a document.
            caption = (job.get("content") or "").strip()
            for _bf in job.get("files", []):
                _bd = _bf.get("data")
                if not _bd:
                    continue
                try:
                    await telegram_service.send_document_bytes(
                        str(chat_id), _bd, _bf.get("filename", "workspace.tar.gz"), caption or None)
                except Exception as e:
                    logger.warning(f"[node] telegram agent-files send failed: {e}")
            return
        from app.services.node_service import tail, INLINE_LIMIT
        icon = {"done": "✅", "failed": "❌", "killed": "🛑"}.get(job.status, "ℹ️")
        out = (job.output or "(no output)").strip()
        text = (
            f"{icon} Job #{job.id} on `{job.node}` {job.status} (exit {job.exit_code})\n"
            f"`{job.command}`\n\n```\n{tail(out, 3000)}\n```"
        )
        try:
            await telegram_service.send_message(str(chat_id), text)
            # Long output: also deliver the full thing as a .txt document.
            if len(out) > INLINE_LIMIT:
                await telegram_service.send_document_bytes(
                    str(chat_id), out.encode("utf-8", "replace"), f"node-{job.node}-job{job.id}.txt"
                )
        except Exception as e:
            logger.warning(f"[node] telegram notify failed for job #{job.id}: {e}")
    return _notify


@router.post("/webhook")
async def telegram_webhook(update: dict, background_tasks: BackgroundTasks):
    """Handle incoming webhook updates from Telegram.

    Returns 200 OK immediately so Telegram doesn't time out (60s limit), then processes the message
    in a background task. Intentionally takes NO `db` dependency: the body doesn't use one (dedup is
    in-memory; the background task opens its own session), and depending on `get_db` here meant a
    drained connection pool would block the ACK → Telegram would replay its backlog.
    """
    global _seen_update_ids, _seen_msg_keys, _seen_cq_ids
    update_id = update.get("update_id", 0)
    if update_id in _seen_update_ids:
        logger.info(f"Skipping duplicate update_id: {update_id}")
        return {"ok": True}
    _seen_update_ids.add(update_id)
    if len(_seen_update_ids) > _MAX_SEEN_IDS:
        # Trim oldest entries — update_ids are monotonically increasing
        _seen_update_ids = set(sorted(_seen_update_ids)[-_MAX_SEEN_IDS:])

    # Dedup the SAME user message arriving under a different update_id (multi-bot delivery to this
    # webhook URL) — without this it would be processed twice → duplicate replies.
    msg = update.get("message") or update.get("edited_message")
    if msg and msg.get("message_id") is not None:
        mkey = ((msg.get("chat") or {}).get("id"), msg.get("message_id"))
        if mkey in _seen_msg_keys:
            logger.info(f"Skipping duplicate message {mkey} (arrived under a 2nd update_id)")
            return {"ok": True}
        _seen_msg_keys.add(mkey)
        if len(_seen_msg_keys) > _MAX_SEEN_IDS:
            # Keep the newest by message_id (monotonic per chat)
            _seen_msg_keys = set(sorted(_seen_msg_keys, key=lambda k: k[1] or 0)[-_MAX_SEEN_IDS:])

    # Dedup button taps too: the multi-bot delivery doubles callback_query updates (same callback
    # `id` under two update_ids), which the update_id set can't catch → every tap (flashcards
    # answers, effect picks, etc.) would run TWICE. callback_query.id is unique per tap.
    cq = update.get("callback_query")
    if cq and cq.get("id"):
        cqid = cq["id"]
        if cqid in _seen_cq_ids:
            logger.info(f"Skipping duplicate callback {cqid} (arrived under a 2nd update_id)")
            return {"ok": True}
        _seen_cq_ids.add(cqid)
        if len(_seen_cq_ids) > _MAX_SEEN_IDS:
            _seen_cq_ids = set(list(_seen_cq_ids)[-_MAX_SEEN_IDS:])

    # Acknowledge immediately — processing may take longer than Telegram's 60s timeout
    background_tasks.add_task(_process_telegram_update, update)
    return {"ok": True}


async def _process_telegram_update(update: dict):
    """Process a Telegram update in the background with its own DB session."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        await _handle_telegram_update(update, db)
    except Exception as e:
        logger.error(f"Background Telegram processing error: {e}", exc_info=True)
    finally:
        # close() ROLLS BACK first, so on a connection Postgres already terminated (a generation that
        # outlived idle_in_transaction_session_timeout) it raises OperationalError — out of a finally,
        # with the work already done, surfacing as "Exception in ASGI application". Nothing here can
        # act on it: the session is being discarded either way.
        try:
            db.close()
        except Exception as _close_err:
            logger.warning(f"Telegram session close failed (connection already gone): {_close_err}")


async def _handle_telegram_update(update: dict, db: Session):
    """Core Telegram update processing logic."""
    logger.info(f"Received Telegram webhook update: {update}")
    try:
        bot_token = settings_store.get("telegram_bot_token")
        if not bot_token:
            logger.warning("Telegram bot not configured")
            return {"ok": False, "error": "Bot not configured"}

        telegram_service.set_token(bot_token)
        # Point at a local Bot API server if the admin enabled one (lifts the
        # 20 MB download cap to ~2 GB); otherwise use the cloud API.
        _configure_telegram(db)

        message = update.get("message")
        logger.warning(f"TELEGRAM WEBHOOK: Received update")
        
        if message:
            await _handle_message(update, db)
        callback_query = update.get("callback_query")
        if callback_query:
            await _handle_callback(update, db)

        return {"ok": True}
    
    except Exception as e:
        logger.error(f"Error processing Telegram webhook: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}

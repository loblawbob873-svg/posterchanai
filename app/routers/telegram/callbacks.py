"""Auto-split from webhook.py: the callback_query half of _handle_telegram_update."""
from .callbacks_media import _cb_mediafc, _cb_fc, _cb_media
from .callbacks_content import _cb_t, _cb_ytdlvsend, _cb_n, _cb_4c, _cb_news, _cb_yt, _cb_nk
from .callbacks_misc import _cb_rem, _cb_pin, _cb_prompt, _cb_help, _cb_lnk, _cb_glowtextpost, _cb_plr, _cb_nostr, _cb_allpost
from ._common import ChatService, CommandService, User, _CLIP_START_PROMPT, _CONSUMED, _EFFECT_CAPTION_PROMPT, _FLASHCARD_TTL, _HELP_SECTIONS, _MEDIA_ACTION_TTL, _MEME_PROMPT, _SOCIAL_CAPTION_PROMPT, _clip_pending, _effect_caption_pending, _effect_char_pending, _flashcard_decks_cache, _geni_image_cache, _link_action_cache, _media_action_cache, _news_post_cache, _news_source_cache, _pleroma_post_cache, _youtube_action_cache, asyncio, logger, re, telegram_service, time
from .keyboards import _4chan_initial_keyboard, _build_torrent_keyboard, _character_prompt_keyboard, _has_pleroma, _help_main_keyboard, _media_action_keyboard, _media_effects_keyboard, _media_fx_memes_keyboard, _media_fx_sounds_keyboard, _media_fx_themes_keyboard, _media_translate_keyboard, _news_menu_keyboard, _recover_post_text, _split_news_into_articles, _strip_cmd_links, _strip_hashtags, _torrents_menu_keyboard, re
from .senders import User, _deliver_files_result, _geni_image_cache, _has_pleroma, _link_content_for_llm, _media_action_cache, _news_source_cache, _offer_social_post, _offer_ytdl_share, _offer_ytdl_video_actions, _pleroma_post_cache, _send_4chan_catalog, _send_4chan_thread, _send_active_torrents, _send_flashcard, _send_news_source_selector, _send_screenshot, _send_torrent_results, _strip_cmd_links, asyncio, logger, re, telegram_service, time


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
                await _cb_rem(update, db, chat_id, data, callback_query, callback_query_id)

            if data.startswith("pin:"):
                # Pinned-search Run / Delete buttons.
                await _cb_pin(update, db, chat_id, data, callback_query, callback_query_id)

            if data.startswith("t:"):
                # Torrent inline button — look up the linked user and run the command
                await _cb_t(update, db, chat_id, data, callback_query, callback_query_id)

            elif data == "ytdlv:send":
                # "Send as-is" after `ytdl video` — deliver the cached download as a video.
                await _cb_ytdlvsend(update, db, chat_id, data, callback_query, callback_query_id)

            elif data == "media:fc":
                # 🎴 Flashcards button on an uploaded file → build the quiz from the cached upload.
                await _cb_mediafc(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("fc:"):
                # Flashcard quiz navigation/answer. State lives in _flashcard_decks_cache[chat_id].
                await _cb_fc(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("media:"):
                # Uploaded-file action buttons (compress / convert / read text / summarize)
                await _cb_media(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("n:"):
                # Nyaa inline button
                await _cb_n(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("4c:"):
                # 4chan inline button
                await _cb_4c(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("news:"):
                # News source selection callback
                await _cb_news(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("prompt:"):
                await _cb_prompt(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("help:"):
                await _cb_help(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("lnk:"):
                await _cb_lnk(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("yt:"):
                await _cb_yt(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("nk:"):
                # News → post an article: nk:post:<article_number>
                await _cb_nk(update, db, chat_id, data, callback_query, callback_query_id)

            elif data == "glow:textpost":
                # Render the pending post text as a glowing neon graphic, then re-offer
                # the SAME share buttons with it attached. Reuses the standard image
                # plumbing (_geni_image_cache, which every platform post handler reads),
                # so nothing about the existing post/share workflow changes — the text
                # body and platform targets are untouched, just an image gets added.
                await _cb_glowtextpost(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("plr:"):
                await _cb_plr(update, db, chat_id, data, callback_query, callback_query_id)

            elif data.startswith("nostr:"):
                await _cb_nostr(update, db, chat_id, data, callback_query, callback_query_id)

            elif data == "all:post":
                # Post to every configured platform simultaneously.
                # Pleroma is posted right away.
                await _cb_allpost(update, db, chat_id, data, callback_query, callback_query_id)

            return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram callback_query handler error: {e}", exc_info=True)

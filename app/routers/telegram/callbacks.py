"""Auto-split from webhook.py: the callback_query half of _handle_telegram_update."""
from .callbacks_misc import _cb_rem, _cb_pin, _cb_prompt, _cb_help, _cb_lnk
from ._common import logger, telegram_service

# Buttons for features the bot no longer offers (2026-10-08 -- they live in PosterChan now). They are still in
# people's chat history, so a tap must answer rather than spin and do nothing.
_MOVED_PREFIXES = ("t:", "n:", "fc:", "media:", "yt:", "ytdlv:", "glow:", "nostr:", "all:")


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
            elif data.startswith("pin:"):
                # Pinned-search Run / Delete buttons.
                await _cb_pin(update, db, chat_id, data, callback_query, callback_query_id)
            elif data.startswith("prompt:"):
                await _cb_prompt(update, db, chat_id, data, callback_query, callback_query_id)
            elif data.startswith("help:"):
                await _cb_help(update, db, chat_id, data, callback_query, callback_query_id)
            elif data.startswith("lnk:"):
                await _cb_lnk(update, db, chat_id, data, callback_query, callback_query_id)
            elif data.startswith(_MOVED_PREFIXES):
                from .messages import _TG_MOVED_TEXT
                await telegram_service.send_message(chat_id, _TG_MOVED_TEXT)

            return {"ok": True}
    except Exception as e:
        logger.error(f"Telegram callback_query handler error: {e}", exc_info=True)

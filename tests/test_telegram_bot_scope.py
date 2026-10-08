"""The Telegram bot keeps AI chat, generation, notifications and alerts, the admin tools, reminders and pins --
and answers everything else with where it lives now.

2026-10-08: "since posterchan is great now, we don't need most of the telegram features in the bot" -- kept: AI chat,
generation, "keep any notification code or alerts", admin tools, reminders & pins. Removed: the effects and media
tools, downloads and search, mail/bills/budget, posting/sharing, flashcards, translate. A removed WORD must still be
recognised: unmatched, `torrents` or `compress` falls through to the chat model, which invents an answer.
"""
import ast
import asyncio
from pathlib import Path

from app.routers.telegram import callbacks, keyboards, messages

ROOT = Path(__file__).resolve().parents[1]
KEPT = ["help", "new", "geni", "musicgeni", "videogeni", "narrate", "voice", "talk", "logs", "syslogs",
        "healthreport", "node", "screenshot", "remind", "reminders", "pin", "pins"]
REMOVED = ["torrents", "nyaa", "ytdl", "yt", "search", "images", "mail", "translate", "post", "share", "compress",
           "convert", "clip", "removebackground", "ocr", "flashcards", "bill", "budget", "mentioned", "meme", "nami"]


def test_kept_commands_still_run():
    for w in KEPT:
        assert w in messages._TG_COMMANDS and w not in messages._TG_MOVED, w


def test_removed_commands_are_recognised_and_answered_with_where_they_live():
    for w in REMOVED:
        assert w in messages._TG_COMMANDS and w in messages._TG_MOVED, w
    assert "PosterChan" in messages._TG_MOVED_TEXT
    # ...and the answer comes BEFORE any download, OCR or command run.
    src = Path(messages.__file__).read_text()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_message")
    body = ast.get_source_segment(src, fn)
    moved, photos, run = (body.index("command in _TG_MOVED"), body.index("# Download photos FIRST"),
                          body.index("await _msg_command("))
    assert moved < photos < run, "a removed command does work before it is turned away"


def test_old_buttons_in_peoples_chats_answer_instead_of_spinning(monkeypatch):
    sent = []

    class TS:
        async def answer_callback_query(self, *_a, **_k):
            return {"ok": True}

        async def send_message(self, chat_id, text, **_k):
            sent.append(text)
            return {"ok": True}
    monkeypatch.setattr(callbacks, "telegram_service", TS())
    for data in ("t:menu", "media:compress", "yt:mp3", "fc:ans:1", "nostr:post", "all:post", "n:dl:1", "ytdlv:send"):
        asyncio.run(callbacks._handle_callback({"callback_query": {"id": "1", "data": data,
                                                                   "message": {"chat": {"id": 5}}}}, None))
    assert sent == [messages._TG_MOVED_TEXT] * 8, sent


def test_menus_offer_only_what_the_bot_still_does():
    data = [b["callback_data"] for row in keyboards._help_main_keyboard()["inline_keyboard"] for b in row]
    assert data and all(d.startswith(("help:", "prompt:geni", "prompt:screenshot")) for d in data), data
    chat = (ROOT / "app/routers/telegram/messages_chat.py").read_text()
    assert "lnk:flashcards" not in chat and "lnk:post" not in chat and "yt:" not in chat


def test_notifications_and_alerts_keep_their_paths():
    src = Path(messages.__file__).read_text()
    assert "social_notifications_service.handle_reply(" in src, "replying to a forwarded notification broke"
    from app.services import social_notifications_service, telegram_service, uptime_service  # noqa: F401
    assert hasattr(telegram_service.telegram_service, "send_message")


def test_a_guessed_intent_for_a_removed_feature_is_answered_as_chat():
    src = (ROOT / "app/routers/telegram/messages_chat.py").read_text()
    i = src.index("command_service.parse_command(intent_command_str)")
    assert "if command and command in _TG_MOVED:\n                    command, arg = None, \"\"" in src[i:i + 600]


def test_the_app_still_mounts_the_bots_webhook():
    """The package __init__ re-exports `router` without using it -- an import cleanup that removed it left the bot
    with no /webhook at all (caught before it shipped)."""
    import app.routers.telegram as tg
    paths = {r.path for r in tg.router.routes}
    assert "/api/telegram/webhook" in paths, sorted(paths)[:10]
    assert "from app.routers.telegram import router as telegram_router" in (ROOT / "app/main.py").read_text()

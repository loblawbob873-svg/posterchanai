"""Telegram half of retiring the `news` / `dailynews` command.

The command is gone server-side (CommandService.RETIRED_COMMANDS answers it). On Telegram that only
helps if the WORDS still route to execute_command -- Telegram never calls parse_command, so a word
missing from its own list falls through to the LLM, which would invent headlines. And the bot's own
News UI (a help-menu button, a source picker, per-article Summarize/Post buttons) must not be left
offering a feature that can no longer answer.
"""
import asyncio
import importlib
import pkgutil

import app.routers.telegram as tg
from app.routers.telegram import keyboards, messages, messages_command
from app.services.command_service import CommandService


def _callback_data(kbd):
    return [b.get("callback_data", "") for row in kbd["inline_keyboard"] for b in row]


def test_news_words_still_route_to_the_command_not_the_llm():
    for word in ("news", "dailynews"):
        assert word in messages._TG_COMMANDS, f"`{word}` would fall through to the LLM on Telegram"


def test_news_on_telegram_returns_the_retirement_text(monkeypatch):
    sent = []

    async def _send(*a, **k):
        sent.append((a, k))
        return {"ok": True}

    monkeypatch.setattr(messages_command.telegram_service, "send_message", _send)
    svc = CommandService.__new__(CommandService)  # a retired command needs no user/db
    for word, arg in (("news", ""), ("news", "bbc"), ("dailynews", "")):
        res = asyncio.run(messages_command._msg_command(
            None, arg, [], "123", word, svc, None, False, None, word, None))
        # A {"type": ...} result goes on to the shared text delivery in messages.py; a bare
        # {"ok": True} would mean this handler answered by itself (the old News menu).
        assert res.get("type") == "text", res
        assert res["content"] == CommandService.RETIRED_COMMANDS[word]
    assert not sent, f"the old News menu is still being sent: {sent}"


def test_help_menu_offers_no_news_button():
    data = _callback_data(keyboards._help_main_keyboard())
    assert not [d for d in data if d.startswith(("news:", "nk:"))], data


def test_no_news_ui_left_in_the_telegram_package():
    names = []
    for m in pkgutil.iter_modules(tg.__path__):
        mod = importlib.import_module(f"{tg.__name__}.{m.name}")
        names += [f"{m.name}.{n}" for n in vars(mod) if "news" in n.lower() or n == "_cb_nk"]
    assert not names, names

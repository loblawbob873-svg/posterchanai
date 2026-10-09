"""Telegram can share a link to Social again -- from the link menu, as a reply, or as a command.

2026-10-09: "Telegram: add ability to share links from chat to Social". The bot had been trimmed the day
before and `post`/`share` went with it. Three ways in, each driven here:
  * the menu the bot shows for a link (plain or forwarded) has 📣 Share to Social, and the button posts
    THAT link through the same `post` command the web app runs;
  * `share` sent as a REPLY shares the message it answers -- including links Telegram keeps OUTSIDE the
    text (a channel post's "Read more" is a text_link entity whose URL appears nowhere in `text`);
  * `share <link> <comment>` is a command the bot runs instead of answering "that's in PosterChan now".
"""
import ast
import asyncio
from pathlib import Path

from app.routers.telegram import _common, callbacks_misc, messages

ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.org/story"


def test_share_and_post_are_bot_commands_again():
    for w in ("share", "post"):
        assert w in messages._TG_COMMANDS and w not in messages._TG_MOVED, w


def test_a_reply_shares_the_text_and_the_links_hidden_behind_words():
    msg = {"text": "Big news today. Read more",
           "entities": [{"type": "text_link", "offset": 16, "length": 9, "url": URL},
                        {"type": "bold", "offset": 0, "length": 3}]}
    assert messages._shareable_text(msg) == "Big news today. Read more\n" + URL
    # A caption's hidden link, a link already in the text is not repeated, and javascript: is not a link.
    assert messages._shareable_text({"caption": "pic", "caption_entities": [{"type": "text_link", "url": URL}]}) == "pic\n" + URL
    assert messages._shareable_text({"text": "see " + URL, "entities": [{"type": "text_link", "url": URL}]}) == "see " + URL
    assert messages._shareable_text({"text": "x", "entities": [{"type": "text_link", "url": "javascript:alert(1)"}]}) == "x"
    assert messages._shareable_text(None) == "" and messages._shareable_text({}) == ""


def test_the_reply_is_attached_before_the_command_runs():
    src = Path(messages.__file__).read_text()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_handle_message")
    body = ast.get_source_segment(src, fn)
    hook = body.index('if command == "post" and reply_to:')
    assert body.rindex("COMMAND_ALIASES.get(command, command)", 0, hook) < hook < body.index("await _msg_command("), \
        "share-as-reply must see the canonical command name and run before the command does"


def test_both_link_menus_offer_share_to_social():
    data = [b["callback_data"] for row in _common._LINK_MENU for b in row]
    assert "lnk:share" in data and "lnk:summary" in data and "lnk:cancel" in data, data
    chat = (ROOT / "app/routers/telegram/messages_chat.py").read_text()
    assert chat.count('"inline_keyboard": _LINK_MENU') == 2, "the plain-link and forwarded-link menus must both offer it"


def test_the_button_posts_that_link_through_the_post_command(monkeypatch):
    sent, ran = [], []

    class TS:
        async def send_message(self, chat_id, text, **_k):
            sent.append(text)
            return {"ok": True}

    class CS:
        def __init__(self, db, user=None):
            self.user = user

        async def execute_command(self, command, arg, *a, **k):
            ran.append((command, arg, self.user))
            return {"type": "text", "content": "📣 **Post**\n✅ Nostr"}

    class Q:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def first(self):
            return "the-user"

    monkeypatch.setattr(callbacks_misc, "telegram_service", TS())
    monkeypatch.setattr(callbacks_misc, "CommandService", CS)
    _common._link_action_cache["77"] = URL
    asyncio.run(callbacks_misc._cb_lnk({}, Q(), "77", "lnk:share", {"message": {"text": ""}}, "1"))
    assert ran == [("post", URL, "the-user")], ran
    assert sent == ["📣 Shared to Social:\n" + URL], sent

    # A refusal (no linked key) is said in plain text, without the markdown the web UI renders.
    class CSNo(CS):
        async def execute_command(self, command, arg, *a, **k):
            return {"type": "text", "content": "No Nostr key linked. Link one in **Settings → Nostr**, then `post <text>`."}
    monkeypatch.setattr(callbacks_misc, "CommandService", CSNo)
    sent.clear(); _common._link_action_cache["77"] = URL
    asyncio.run(callbacks_misc._cb_lnk({}, Q(), "77", "lnk:share", {"message": {"text": ""}}, "1"))
    assert sent and "**" not in sent[0] and "`" not in sent[0] and "No Nostr key linked" in sent[0], sent

"""✨ in Telegram and DMs — Generate reply, Summarize chat, Summarize YouTube & links.

Asked for: "AI replies to Telegram and DM's also" and "For Telegram, maybe the AI sparkle button should
have a Generate Reply Summarize youtube and links". /api/chat-assist is RUN with the model and the page
fetcher stubbed. Pinned:

  * reply: several drafts in ONE model call, drafted for the medium asked (a Telegram message, a DM),
    with the Texts never-invent rules; the Texts prompt itself is byte-for-byte unchanged;
  * summarize: a BOUNDED tail (newest kept), speakers named in a group, the user is "Me";
  * links: the NEWEST links first, at most three, each read through search_service.fetch_url_content
    (the SSRF-guarded fetcher that also turns YouTube into its transcript); an unreadable link is a
    sentence, not an invented summary; no links is a sentence;
  * the AI gate refuses before any model call; nothing logs what anybody wrote.
"""
import asyncio
import json
import logging
import os
from unittest import mock

import pytest

os.environ.setdefault("POSTERCHANAI_SKIP_DB", "1")

from app.routers import chat_assist as R  # noqa: E402
from app.services import chat_assist_service as svc  # noqa: E402
from app.services import texts_ai_service as texts  # noqa: E402

SECRET = "the gate code is 4471"


class _Chat:
    def __init__(self, answers):
        self.answers, self.calls, self.temperature = list(answers), [], 0.7

    async def chat(self, msgs):
        self.calls.append(msgs)
        return self.answers.pop(0) if self.answers else ""


class _User:
    is_admin = False
    can_ai = True
    nostr_npub = ""


def _run(body, chat, user=_User(), allowed=True, pages=None):
    class _CS:
        def __init__(self, db, user=None):
            self.chat_service = chat

    async def ai_allowed(u):
        return allowed

    fetched = []

    class _Search:
        async def fetch_url_content(self, url, max_length=0):
            fetched.append(url)
            return (pages or {}).get(url) or {"url": url, "title": url, "content": "", "error": "URL blocked: private address"}

    with mock.patch("app.services.command_service.CommandService", _CS), \
         mock.patch("app.services.nip05_access.ai_allowed", ai_allowed), \
         mock.patch("app.services.search_service.get_search_service", lambda db: _Search()):
        resp = asyncio.run(R.chat_assist(R.AssistReq(**body), db=None, user=user))
    code, data = (200, resp) if isinstance(resp, dict) else (resp.status_code, json.loads(resp.body))
    return code, data, fetched


def _msgs(n, me_every=2, who="Alice"):
    return [{"me": i % me_every == 0, "text": f"message {i}", "who": who} for i in range(n)]


def test_reply_offers_three_drafts_for_the_medium_in_one_call():
    chat = _Chat(["1. On my way\n2. Give me ten minutes!\n3. Want me to bring anything?"])
    code, data, _ = _run({"action": "reply", "medium": "telegram",
                          "messages": [{"me": False, "text": "Are you coming?"}], "count": 3}, chat)
    assert code == 200 and data["choices"] == ["On my way", "Give me ten minutes!", "Want me to bring anything?"]
    assert data["content"] == "On my way" and len(chat.calls) == 1
    system = chat.calls[0][0]["content"]
    assert "Telegram chat message replies" in system and "never invent" in system
    code, _, _ = _run({"action": "reply", "medium": "dm", "messages": [{"me": False, "text": "hi"}]}, _Chat(["1. hey"]))
    assert code == 200


def test_the_texts_prompt_is_unchanged():
    msgs = texts.build_messages([(False, "hi")])
    assert msgs[0]["content"].startswith('You draft text-message (SMS) replies for the user')
    assert texts.build_choice_messages([(False, "hi")], 3)[0]["content"].startswith("You draft text-message (SMS) replies")


def test_summarize_reads_a_bounded_tail_with_speakers():
    chat = _Chat(["- Alice asked about Friday\n- Waiting on you: confirm the time"])
    items = _msgs(200)
    items[-1] = {"me": False, "text": "Bob here: is Friday still on?", "who": "Bob"}
    code, data, _ = _run({"action": "summarize", "medium": "telegram", "messages": items}, chat)
    assert code == 200 and data["summary"].startswith("- Alice asked")
    prompt = chat.calls[0][1]["content"]
    assert "message 199" not in prompt and "Bob: Bob here: is Friday still on?" in prompt
    assert "message 120" in prompt and "message 119" not in prompt, "the newest 80 are kept, the oldest dropped"
    assert "Me: message 198" in prompt
    code, data, _ = _run({"action": "summarize", "messages": []}, _Chat([]))
    assert code == 400 and "nothing in this chat" in data["error"]


def test_links_are_read_newest_first_through_the_guarded_fetcher():
    yt, art, old, lan = ("https://youtu.be/dQw4w9WgXcQ", "https://example.com/news/story.",
                         "https://old.example/a", "http://192.168.0.1/admin")
    items = [{"me": False, "text": f"see {old}"}, {"me": True, "text": f"also {lan}"},
             {"me": False, "text": f"watch {yt} and read {art}"}]
    pages = {yt: {"url": yt, "title": "A video", "content": "transcript " * 30},
             "https://example.com/news/story": {"url": art, "title": "Story", "content": "story text " * 30}}
    chat = _Chat(["The video says hello.", "The story says things."])
    code, data, fetched = _run({"action": "links", "messages": items}, chat, pages=pages)
    assert code == 200
    assert fetched == [yt, "https://example.com/news/story", lan], "newest first, three at most, punctuation trimmed"
    got = {l["url"]: l for l in data["links"]}
    assert got[yt]["summary"] == "The video says hello." and got[yt]["title"] == "A video"
    assert "blocked" in got[lan]["error"] and "summary" not in got[lan], "an unreadable link is not summarized"
    assert len(chat.calls) == 2
    code, data, fetched = _run({"action": "links", "messages": [{"me": False, "text": "no links here"}]}, _Chat([]))
    assert code == 400 and "no links" in data["error"] and fetched == []


def test_the_gate_refuses_before_the_model_and_probe_asks_nothing():
    chat = _Chat(["1. x"])
    code, data, _ = _run({"action": "reply", "messages": [{"me": False, "text": "hi"}]}, chat, allowed=False)
    assert code == 403 and chat.calls == []
    code, data, _ = _run({"probe": True}, chat, allowed=True)
    assert data == {"ok": True, "allowed": True} and chat.calls == []
    code, data, _ = _run({"action": "reply", "messages": [{"me": False, "text": "hi"}]}, chat, user=None)
    assert code == 401 and chat.calls == []
    code, _, _ = _run({"action": "delete-everything", "messages": [{"me": False, "text": "hi"}]}, chat)
    assert code == 400


def test_nothing_anybody_wrote_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    items = [{"me": False, "text": SECRET + " https://example.com/x"}]
    pages = {"https://example.com/x": {"url": "https://example.com/x", "title": "t", "content": SECRET * 10}}
    for action in ("reply", "summarize", "links"):
        _run({"action": action, "messages": items}, _Chat(["1. " + SECRET, SECRET]), pages=pages)
    assert SECRET not in caplog.text and "4471" not in caplog.text

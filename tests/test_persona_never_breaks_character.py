"""A persona bot never posts the MODEL talking about itself.

Reported about the Judge Dread bot: "need to make sure it never breaks character and says 'this is a
post in judge dreads likeness' or whatever". Small models wrap an in-character reply in their own
voice -- "This is a post in X's likeness.", "*As an AI, I…*", "(Parody account)", "Disclaimer: …", a
trailing "— written in the style of X" -- and the bot published those lines verbatim, under the
character's name. Nothing between the model and the timeline knew what they were: the cleaner only
knew PREAMBLES ("Here is what X would say:"), never a disclaimer after the reply or an aside inside it.

Fixed in two halves, generic to every persona bot:
  * the persona system prompt forbids it (ai/client.py), and
  * a deterministic guard removes it whatever the model does (ai/persona_guard.py), inside
    `generate_reply` -- the one function every persona path posts through (mentions, random replies,
    Concord rooms, autopost, the community bots). A reply that was NOTHING but such text is regenerated
    once and then not posted at all; a refusal is never regenerated.

These run the SHIPPED `generate_reply` against a stub model, and the shipped Nostr dispatcher and
autopost on top of it, and look at what would be POSTED.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

BOTS = Path(__file__).resolve().parents[1] / "botframework"

PERSONA = ("Your name is Judge Dread. You are a stern, arrogant old lawman who believes he is the law "
           "and talks down to everybody. Stay in character and only reply with your response.")

# (what the model sent, what the character actually said, the out-of-character text that must go)
SHAPES = [
    ("I am the law. Get off my street, creep.\n\nThis is a post in Judge Dread's likeness.",
     "I am the law. Get off my street, creep.", "likeness"),
    ("*As an AI, I can't actually arrest you.* But listen here, citizen: you are guilty.",
     "But listen here, citizen: you are guilty.", "As an AI"),
    ("(Parody account) Twelve years in the cubes for jaywalking.",
     "Twelve years in the cubes for jaywalking.", "Parody"),
    ("Sentence is death. Next case.\nDisclaimer: this is fictional and does not reflect real views.",
     "Sentence is death. Next case.", "Disclaimer"),
    ("Your appeal is denied, punk. — written in the style of Judge Dread",
     "Your appeal is denied, punk.", "style of"),
    ("I sentence you to ten years.\n\nNote: this response was written in character.",
     "I sentence you to ten years.", "Note:"),
    ("[In character] Drop the weapon, perp.", "Drop the weapon, perp.", "In character"),
    ("Silence in my court! I'm just an AI language model, but you are guilty as charged.",
     "Silence in my court!", "AI language model"),
]

ALL_META = [
    "This is a post in Judge Dread's likeness.",
    "*As an AI, I'm role-playing Judge Dread here.*",
    "(Parody account)\nDisclaimer: for entertainment purposes only.",
    "— written in the style of Judge Dread",
]


# --------------------------------------------------------------------------- the shipped modules
@pytest.fixture
def bot(monkeypatch):
    """A fresh import of the bot's AI client with a persona loaded, and a stub model behind it.

    `bot.replies` is what the model will answer, in order; `bot.sent` collects every request."""
    monkeypatch.syspath_prepend(str(BOTS))
    monkeypatch.setenv("PROMPT", PERSONA)
    monkeypatch.setenv("OPENAI_ENDPOINT", "http://127.0.0.1:9/api/chat/completions")
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    for k in ("DEBUG_MODE", "NOSTR_TALK", "NOSTR_TALK_ONLY", "AUTO_POST_SEED"):
        monkeypatch.delenv(k, raising=False)
    for m in ("config", "ai", "ai.client", "ai.response_cleaner", "ai.persona_guard",
              "nostrListener", "autopost"):
        monkeypatch.delitem(sys.modules, m, raising=False)       # restored at teardown
    client = importlib.import_module("ai.client")

    class Bot:
        replies = []
        sent = []
    b = Bot()
    b.client = client

    class _R:
        status_code = 200

        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self.text}}]}

    def post(url, headers=None, data=None, timeout=0):
        b.sent.append(json.loads(data))
        return _R(b.replies.pop(0) if b.replies else "")
    monkeypatch.setattr(client.requests, "post", post)
    monkeypatch.setattr(client, "_acquire_ai_slot", lambda: None)
    monkeypatch.setattr(client, "_release_ai_slot", lambda: None)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    return b


def _no_meta(text):
    from ai.persona_guard import is_meta
    low = text.lower()
    for word in ("likeness", "as an ai", "parody", "disclaimer", "style of", "in character",
                 "note:", "language model"):
        assert word not in low, f"{word!r} reached the post: {text!r}"
    for line in text.splitlines():
        assert not is_meta(line), line


# --------------------------------------------------------------------------- the guard itself
@pytest.mark.parametrize("raw,kept,meta", SHAPES)
def test_the_guard_removes_the_aside_and_keeps_the_character(bot, raw, kept, meta):
    from ai.persona_guard import strip_persona_meta
    out, dropped = strip_persona_meta(raw)
    assert out == kept
    assert dropped >= 1
    assert meta.lower() not in out.lower()


@pytest.mark.parametrize("line", [
    "The election was a parody of justice. I judge it guilty.",
    "Note the time, punk. Curfew is at nine.",
    "I have written as many citations today as there are perps on this block.",
    "That robot of yours is evidence now.",
    "Stay out of my sector.",
])
def test_the_characters_own_words_are_never_touched(bot, line):
    """A false positive deletes what the character SAID. These look close to a disclaimer and are not."""
    from ai.persona_guard import strip_persona_meta
    assert strip_persona_meta(line) == (line, 0)


@pytest.mark.parametrize("raw", ALL_META)
def test_a_reply_that_is_only_out_of_character_leaves_nothing(bot, raw):
    from ai.persona_guard import strip_persona_meta
    assert strip_persona_meta(raw)[0] == ""


# --------------------------------------------------------------------------- generate_reply
@pytest.mark.parametrize("raw,kept,meta", SHAPES)
def test_generate_reply_returns_only_the_character(bot, raw, kept, meta):
    bot.replies[:] = [raw]
    got = bot.client.generate_reply("What is my sentence, Judge?")
    assert got == kept
    _no_meta(got)


def test_the_persona_prompt_forbids_disclaimers(bot):
    """The request half: the system turn tells the model not to produce them at all."""
    bot.replies[:] = ["I am the law."]
    bot.client.generate_reply("hello judge")
    system = bot.sent[0]["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("Your name is Judge Dread")
    low = system["content"].lower()
    for word in ("disclaimer", "an ai", "parody", "likeness", "in the style of", "in character"):
        assert word in low, f"the persona prompt does not forbid {word!r}"


def test_an_all_disclaimer_reply_is_regenerated_once(bot):
    bot.replies[:] = ["This is a post in Judge Dread's likeness.", "Twelve years in the cubes, creep."]
    assert bot.client.generate_reply("judge me") == "Twelve years in the cubes, creep."
    assert len(bot.sent) == 2


def test_two_all_disclaimer_replies_post_nothing(bot):
    bot.replies[:] = ["(Parody account)", "*As an AI, I'm role-playing here.*", "should never be asked for"]
    assert bot.client.generate_reply("judge me") is None
    assert len(bot.sent) == 2, "regenerated more than once"


def test_a_refusal_is_not_regenerated(bot):
    """Declining is the model's call; the bot posts nothing and does not ask again."""
    bot.replies[:] = ["As an AI, I can't write that.", "should never be asked for"]
    assert bot.client.generate_reply("judge me") is None
    assert len(bot.sent) == 1


def test_a_service_answer_is_left_alone(bot):
    """A DVM job or a news summary is not the character talking -- `in_character=False` keeps it whole."""
    text = "The satirical account was suspended on Tuesday. Note: the ban was later reversed."
    bot.replies[:] = [text]
    assert bot.client.generate_reply("summarize", in_character=False) == text


def test_nothing_the_model_wrote_is_logged(bot, capsys):
    secret = "Sentence is death for the perp in sector nine"
    bot.replies[:] = [secret + ".\n\nThis is a post in Judge Dread's likeness."]
    assert bot.client.generate_reply("what is the sentence for loitering in sector nine") == secret + "."
    out = capsys.readouterr().out
    assert "sector nine" not in out.lower() and "likeness" not in out.lower()
    assert "out-of-character" in out


# --------------------------------------------------------------------------- what actually gets POSTED
def _nostr(bot, monkeypatch):
    nl = importlib.import_module("nostrListener")
    assert nl.generate_reply is bot.client.generate_reply, "the listener must post through ai.client"
    monkeypatch.setattr(nl, "web_query", lambda text: None)
    posted = []
    nl._dispatch(None, "judge, what is my sentence for littering", None, None,
                 reply=lambda text="", **kw: posted.append(text))
    return posted


@pytest.mark.parametrize("raw,kept,meta", SHAPES)
def test_a_nostr_mention_reply_posts_only_the_character(bot, monkeypatch, raw, kept, meta):
    bot.replies[:] = [raw]
    posted = _nostr(bot, monkeypatch)
    assert posted == [kept]
    _no_meta(posted[0])


def test_a_nostr_mention_with_only_a_disclaimer_posts_nothing(bot, monkeypatch):
    bot.replies[:] = ["This is a post in Judge Dread's likeness.", "(Parody account)"]
    assert _nostr(bot, monkeypatch) == []


def test_an_autopost_posts_only_the_character(bot):
    ap = importlib.import_module("autopost")
    assert ap.generate_reply is bot.client.generate_reply
    bot.replies[:] = ["Curfew is at nine. Be indoors or be in a cube.\n— written in the style of Judge Dread"]
    assert ap._generate_post() == "Curfew is at nine. Be indoors or be in a cube."
    bot.replies[:] = ["Disclaimer: this is a parody.", "This is a post in Judge Dread's likeness."]
    assert ap._generate_post() is None

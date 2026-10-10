"""A bot asked for the news searches the web -- it never lets the model make the news up.

2026-10-09, a Concord room: "can you search news in Denver, CO" reached the bare model (only a message STARTING
with `search `/`news ` reached the real search). It has no web access and answered anyway -- "let me check
what's happening in Denver! ... here's what I found" -- a state of emergency and a mayor who left office in 2023,
all invented, in a room people read. And the AI client printed that reply IN FULL to the server journal: replies
quote what people said, and a Concord room is end-to-end encrypted.

  * `searxng.web_query` recognises a request for the web however it is phrased, and leaves ordinary chat alone
    (an existing rule: "I like the news" said to the bot is conversation, not a headline dump);
  * the dispatcher (timeline AND Concord, one implementation) sends such a question to the real search, never to
    the model, and an empty search says so instead of inventing;
  * the AI client never prints what the model said, only its size.
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "botframework"))

import searxng            # noqa: E402


@pytest.mark.parametrize("text,query", [
    ("can you search news in denver, CO", "news in denver, CO"),
    ("search news in Denver, CO", "news in Denver, CO"),
    ("what's happening in Denver?", "what's happening in Denver"),
    ("any news about the broncos", "any news about the broncos"),
    ("latest news", "latest news"),
    ("please google rust 2.0", "rust 2.0"),
    ("look up the weather in denver", "the weather in denver"),
])
def test_a_request_for_the_web_is_recognised_however_it_is_phrased(text, query):
    assert searxng.web_query(text) == query


@pytest.mark.parametrize("text", ["I like the news", "how are you", "that search was funny", "news bbc",
                                  "tell me about the news cycle in general", "find me a joke", "", "   "])
def test_ordinary_conversation_is_not_a_web_request(text):
    assert searxng.web_query(text) is None


def test_the_dispatcher_searches_and_never_asks_the_model(monkeypatch):
    import nostrListener as nl
    asked = []
    monkeypatch.setattr(nl, "generate_reply", lambda *a, **k: asked.append(a) or "INVENTED NEWS")
    monkeypatch.setattr(nl, "smart_search", lambda q: ([{"title": "Denver Post", "url": "https://dp.example/1",
                                                         "content": "x"}], "news"))
    monkeypatch.setattr(nl, "summarize_search_results", lambda r, q, c: f"SUMMARY of {q} (1 source)")
    said = []
    nl._dispatch(None, "can you search news in denver, CO", None, None, reply=lambda text="", **k: said.append(text),
                 sender_key="ab" * 32, media_ok=False)
    assert said == ["SUMMARY of news in denver, CO (1 source)"], said
    assert asked == [], "a news question reached the bare model"


def test_an_empty_search_says_so_instead_of_inventing(monkeypatch):
    import nostrListener as nl
    monkeypatch.setattr(nl, "generate_reply", lambda *a, **k: "INVENTED NEWS")
    monkeypatch.setattr(nl, "smart_search", lambda q: ([], None))
    said = []
    nl._dispatch(None, "what's happening in Denver?", None, None, reply=lambda text="", **k: said.append(text),
                 sender_key="ab" * 32, media_ok=False)
    assert len(said) == 1 and "found nothing" in said[0] and "INVENTED" not in said[0]


def test_a_concord_room_sends_a_web_question_to_the_dispatcher():
    import concordListener as cl
    assert cl._asks_the_web(cl._strip_address("@PosterChan AI can you search news in denver, CO",
                                              "npub1x", ["PosterChan AI"]))
    assert not cl._asks_the_web("how are you")


def test_the_ai_client_never_prints_what_the_model_said(monkeypatch, tmp_path):
    from ai import client
    secret = "a private line the room said 7f3a"

    class R:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Sure! Here is what I think: " + secret}}]}
    monkeypatch.setattr(client.requests, "post", lambda *a, **k: R())
    monkeypatch.setattr(client, "OPENAI_ENDPOINT", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setattr(client, "_AI_LOCK_DIR", str(tmp_path / "locks"))   # never queue behind the live bots
    out = io.StringIO()
    with redirect_stdout(out):
        reply = client.generate_reply("what do you think about the weather?")
    assert reply and secret in reply, reply
    assert secret not in out.getvalue(), "the journal got the model's words: " + out.getvalue()[-400:]

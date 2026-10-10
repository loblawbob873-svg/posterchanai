"""Every command the AI bot answers, through BOTH transports: a Nostr mention and a Concord room.

"we need test cases for all the AI bot commands because we found an issue already with search": a Concord room
asked "can you search news in Denver, CO", the question never reached the search, and the bare model invented
the news. Single-command tests had covered `search ...` and passed. So this is one TABLE: each row is something a
person types, which backend must answer it, and what they must get back -- run on the timeline (the reply goes to
`send_reply`) and in a room (the room's gate decides command vs chat, then the same dispatcher with the room's
`reply` and no inbound files). A command the room gate does not recognise goes to the bare model, which is the
Denver failure, so every non-chat row asserts the room reaches the dispatcher.

Every backend is a stub that records it was called: nothing here touches a network, a GPU or a model.
"""
import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "botframework"))

import concordListener as cl   # noqa: E402
import nostrListener as nl     # noqa: E402

BAD = "badwordxyz"
PNG = (b"\x89PNG fake", "image/png")


@pytest.fixture
def bot(monkeypatch):
    calls = []

    def rec(name, result):
        def f(*a, **k):
            calls.append((name, a, k))
            if isinstance(result, Exception):
                raise result
            return result(*a, **k) if callable(result) else result
        return f
    stubs = {
        "generate_reply": "MODEL SAYS HI",
        "smart_search": ([{"title": "t", "url": "https://e.example/1", "content": "c"}], "general"),
        "summarize_search_results": lambda r, q, c: f"RESULTS for {q}",
        "search_and_download_images": ("IMAGES for cats", [PNG]),
        "fetch_news_from_source": lambda src, max_headlines=10: f"HEADLINES from {src}",
        "capture_screenshot": (b"png", None),
        "fetch_ytdl_media": lambda url, video=False, **k: (b"vid" if video else b"aud",
                                                            "video/mp4" if video else "audio/mpeg", None),
        "process_media": ("glowed", [{"data": b"g", "content_type": "image/png", "filename": "g.png"}]),
        "_sender_brand": (None, None),
        "_gather_media": ([("in.png", b"x", "image/png")], True),     # the mention carries a picture
        "is_ai_configured": True,
    }
    state = {"stubs": stubs}
    for name, res in stubs.items():
        monkeypatch.setattr(nl, name, rec(name, res))
    monkeypatch.setattr(nl, "contains_bad_words", lambda t: BAD in (t or "").lower())
    monkeypatch.setattr(nl, "_send_spoken", lambda reply, text: reply(text))
    monkeypatch.setattr(nl, "_TALK_ONLY", False)
    monkeypatch.setattr(nl, "_ytdl_last_request", {})
    monkeypatch.setitem(sys.modules, "image_backend", types.SimpleNamespace(
        generate_image_bytes_with_retries=rec("geni", [PNG])))
    monkeypatch.setitem(sys.modules, "tts", types.SimpleNamespace(
        generate_narration_video=rec("narration_video", None), generate_speech_with_retries=rec("speech", b"mp3")))
    state["calls"] = calls
    state["mp"] = monkeypatch
    state["set"] = lambda name, res: monkeypatch.setattr(nl, name, rec(name, res))
    return state


def _nostr(bot, text):
    """A mention on the timeline: the default reply goes through send_reply(note, ...)."""
    said = []
    note = {"id": "n1", "user": {"pubkey": "ab" * 32}}
    bot["mp"].setattr(nl, "send_reply", lambda n, text="", **k: said.append((text, {x: v for x, v in k.items() if v})))
    nl._dispatch(note, text, {"avatarUrl": None}, None)
    return said


def _room(bot, text):
    """The same words said to the bot in a Concord room: the room's gate, then the dispatcher or the model."""
    said = []
    body = cl._strip_address("@PosterChan AI " + text, "npub1bot", ["PosterChan AI"])
    if cl._is_command(body) or cl._asks_the_web(body):
        nl._dispatch(None, body, None, None, reply=lambda text="", **k: said.append((text, {x: v for x, v in k.items() if v})),
                     sender_key="cd" * 32, media_ok=False)
        return said, "dispatcher"
    return [(nl.generate_reply(text), {})], "model"


def _backends(bot):
    return [c[0] for c in bot["calls"]]


# (what a person types, backend that must answer, substring of the answer, media kind in the answer or None)
ROWS = [
    ("help", None, "mention me", None),
    ("?", None, "mention me", None),
    ("search cats", "smart_search", "RESULTS for cats", None),
    ("images cats", "search_and_download_images", "IMAGES for cats", "image_bytes"),
    ("news bbc", "fetch_news_from_source", "HEADLINES from bbc", None),
    ("geni a cat in a hat", "geni", "Here is your image", "image_bytes"),
    ("screenshot https://example.com", "capture_screenshot", "https://example.com", "image_bytes"),
    ("ytdl https://v.example/1", "fetch_ytdl_media", "https://v.example/1", "audio_bytes"),
    ("ytdl video https://v.example/1", "fetch_ytdl_media", "https://v.example/1", "video_bytes"),
    ("/narrate tell a story", "generate_reply", "MODEL SAYS HI", "audio_bytes"),
    ("can you search news in denver, CO", "smart_search", "RESULTS for news in denver, CO", None),
    ("what's happening in Denver?", "smart_search", "RESULTS for", None),
    ("please google rust 2.0", "smart_search", "RESULTS for rust 2.0", None),
    ("glow hello world", "process_media", "", "image_bytes"),
]


@pytest.mark.parametrize("text,backend,answer,media", ROWS, ids=[r[0] for r in ROWS])
@pytest.mark.parametrize("where", ["nostr", "concord"])
def test_every_command_reaches_its_backend_and_answers(bot, where, text, backend, answer, media):
    if where == "nostr":
        said = _nostr(bot, text)
    else:
        said, route = _room(bot, text)
        assert route == "dispatcher", "the room sent %r to the bare model -- it would make the answer up" % text
    assert len(said) == 1, said
    reply, kw = said[0]
    assert answer in reply, (reply, answer)
    if backend:
        assert backend in _backends(bot), "%r never reached %s (called: %s)" % (text, backend, _backends(bot))
    if backend != "generate_reply":
        assert "generate_reply" not in _backends(bot), "%r was answered by the model, not the tool" % text
    if media:
        assert kw.get(media), "%r: no %s in the answer %r" % (text, media, kw)


@pytest.mark.parametrize("text", ["how are you", "I like the news", "what do you think about films?"])
@pytest.mark.parametrize("where", ["nostr", "concord"])
def test_conversation_goes_to_the_model_and_to_no_tool(bot, where, text):
    said = _nostr(bot, text) if where == "nostr" else _room(bot, text)[0]
    assert said and said[0][0] == "MODEL SAYS HI", said
    tools = set(_backends(bot)) - {"generate_reply"}
    assert not tools, "conversation %r ran %s" % (text, tools)


@pytest.mark.parametrize("text,refusal", [("geni a " + BAD, "cannot generate"), ("images " + BAD, "cannot search"),
                                          ("meme " + BAD, "cannot add that text")])
def test_blocked_words_are_refused_before_any_backend_runs(bot, text, refusal):
    said = _nostr(bot, text)
    assert said and refusal in said[0][0].lower(), said
    assert not set(_backends(bot)) & {"geni", "search_and_download_images", "process_media"}


@pytest.mark.parametrize("text", ["screenshot", "ytdl", "/narrate"])
def test_a_command_missing_its_argument_says_how_to_use_it(bot, text):
    said = _nostr(bot, text)
    assert said and "usage" in said[0][0].lower(), said


def test_an_empty_or_failing_backend_says_so_instead_of_saying_nothing(bot):
    bot["set"]("smart_search", ([], None))
    assert "No results found" in _nostr(bot, "search zzqx")[0][0]
    bot["set"]("fetch_news_from_source", RuntimeError("feed down"))
    assert "error fetching news" in _nostr(bot, "news bbc")[0][0]
    bot["set"]("capture_screenshot", (None, "❌ that site refused"))
    assert "refused" in _nostr(bot, "screenshot https://x.example")[0][0]
    bot["set"]("fetch_ytdl_media", (None, None, "geo-blocked"))
    assert "geo-blocked" in _nostr(bot, "ytdl https://v.example/2")[0][0]


def test_ytdl_is_rate_limited_per_person(bot):
    _nostr(bot, "ytdl https://v.example/1")
    again = _nostr(bot, "ytdl https://v.example/2")
    assert "wait" in again[0][0].lower(), again
    assert _backends(bot).count("fetch_ytdl_media") == 1


@pytest.mark.parametrize("cmd", ["compress", "clip 0:10 0:20", "convert", "meme hello", "hava", "glow"])
def test_file_commands_run_on_nostr_and_say_why_not_in_a_room(bot, cmd):
    said = _nostr(bot, cmd)
    ran = [c for c in bot["calls"] if c[0] == "process_media"]
    assert ran and ran[0][1][0] == cmd.split()[0] and ran[0][1][2], "%r did not run on the attached file" % cmd
    assert said and (said[0][1].get("image_bytes") or said[0][1].get("video_bytes")), said
    said, route = _room(bot, cmd)
    assert route == "dispatcher" and said and "attachment" in said[0][0], (cmd, said)


def test_a_file_command_with_no_file_asks_for_one(bot):
    bot["set"]("_gather_media", ([], False))
    assert "Attach or link a file" in _nostr(bot, "compress")[0][0]
    bot["set"]("_gather_media", ([], True))                       # it was there and the download failed
    assert "try again" in _nostr(bot, "compress")[0][0]


def test_every_command_the_dispatcher_knows_is_in_the_table():
    """A command added to `_dispatch` without a row here is exactly how `search` regressed unseen."""
    import inspect
    src = inspect.getsource(nl._dispatch)
    words = set()
    for line in src.splitlines():
        if "lower" in line and ("startswith" in line or " in lower" in line or "lower ==" in line or "lower in" in line):
            words |= {w.strip() for w in re.findall(r'"(/?[a-z]+) ?"', line)}
    covered = {r[0].split()[0] for r in ROWS} | {"shot", "ss", "commands", "/help", "video", "mp3"}   # ytdl's own arguments
    missing = sorted(words - covered)
    assert not missing, "commands with no row in ROWS: %s" % missing

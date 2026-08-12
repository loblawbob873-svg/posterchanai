"""Reading a link the user posted: fetch it, render it if it is a shell, and let it outrank priors.

Run: venv-unified/bin/python -m pytest tests/test_url_reading.py -q

THREE MEASURED FAILURES, all from one request — "check Jordan's posts and tell me if he's an
asshole or nice person: https://poster.place/npub1p3za04z…" — and one unrelated-looking report,
"Summarize this page: https://www.cnn.com/ is broken". The journal says they are three bugs:

  1. THE PAGE SAID NOTHING. A server-side fetch gets what the SERVER sent, which on a JS-rendered
     site is a shell. Measured on this deployment: cnn.com 7260 chars extracted, wikipedia 6878,
     news.ycombinator 2828, poster.place **0**. There is no per-site fix for that — the generic one
     is to open the page in a browser when extraction comes back with nothing, which is what a
     person does with the same link.

  2. THE CONTENT LOST TO THE MODEL'S PRIORS. `[STREAM] First content chunk: "Jordan Peterson is
     definitely an asshole. Here's w"` — while 4709 chars of the real profile (a crypto-anarchist
     meme poster called Jordan S) sat in the very same prompt. It was appended AFTER the question,
     as an appendix to something the model had already answered from what it knew about the name.
     The bare-URL summarize path in the same function had always put content first and had never
     shown this bug.

  3. THE LINK WAS SEARCHED FOR INSTEAD OF READ. `intent_service` classified "Summarize this page:
     https://www.cnn.com/" as `search https://www.cnn.com/`, so the node ran a SearXNG query for the
     URL string and summarized the results — the page itself was never opened.

Everything here is offline: no relay, no network, no model. The browser test renders a local file
and is skipped where Chrome is not installed (nas has none, CI has none).
"""
import asyncio
import os
import tempfile

import pytest

from app.services import page_render
from app.services.search_service import SearchService

SVC = SearchService.__new__(SearchService)      # these paths need no DB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------------------------
# 1. A page that said nothing is read in a browser — and one that spoke is not.
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("", True),
    ("   \n  ", True),
    ("PosterChan · Nostr Offline — showing saved posts The Ultimate Nostr Experience", True),
    ("x" * 399, True),
    ("x" * 401, False),
    ("word " * 600, False),
])
def test_only_a_page_that_said_nothing_is_worth_a_browser(text, expected):
    assert page_render.looks_unrendered(text) is expected


def test_the_threshold_clears_every_real_page_measured():
    """The margin is the whole safety of the fallback: the thinnest REAL page measured on this
    deployment extracted 2828 chars, so the trigger has to sit far below that and far above 0."""
    assert 0 < page_render.UNRENDERED_TEXT_CHARS < 2828


def test_a_render_fits_inside_the_callers_timeout():
    """Every caller wraps URL fetching in asyncio.wait_for(..., timeout=15). A render that could
    outlast that turns a readable page into "[Could not fetch URL content due to timeout]"."""
    assert page_render.RENDER_TIMEOUT < 15


@pytest.mark.skipif(not page_render.chrome_available(), reason="no Chrome on this node")
def test_a_javascript_page_is_actually_read():
    """The real thing: a page whose text exists ONLY after its script runs. Served from a file so
    this asserts the renderer, not the network."""
    html = ("<!doctype html><title>Shell</title><body><div id=root></div>"
            "<script>setTimeout(function(){document.getElementById('root').textContent="
            "'RENDERED-BODY-TEXT ' + 'x'.repeat(500);}, 300)</script>")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(html)
        path = fh.name
    try:
        # What the server sent — the shell — has no text at all, which is the trigger.
        assert page_render.looks_unrendered("")
        out = page_render.render_page_text("file://" + path, timeout=20)
        assert out is not None, "chrome produced nothing for a page that renders in 300ms"
        title, text = out
        assert "RENDERED-BODY-TEXT" in text
        assert title == "Shell"
    finally:
        os.unlink(path)


def test_the_browser_is_spent_on_at_most_one_url_per_message(monkeypatch):
    """Three shell links in one message must not queue three renders — the batch has 15s."""
    seen = []

    async def fake_fetch(url, max_length=15000, allow_render=True):
        seen.append((url, allow_render))
        # Pretend the first one rendered; the rest must then be told not to.
        return {"url": url, "title": url, "content": "x", "error": None, "rendered": allow_render}

    monkeypatch.setattr(SVC, "fetch_url_content", fake_fetch)
    _run(SVC.fetch_urls(["https://a.example/", "https://b.example/", "https://c.example/"]))
    assert [allow for _, allow in seen] == [True, False, False]


def test_where_the_page_ended_up_is_re_checked():
    """The fetcher validates every HTTP redirect hop; a browser also follows the ones the page
    performs in script. A render that lands somewhere the guard refuses is thrown away, not read."""
    if not page_render.chrome_available():
        pytest.skip("no Chrome on this node")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write("<!doctype html><title>T</title><body>" + "text " * 200 + "</body>")
        path = fh.name
    try:
        assert page_render.render_page_text("file://" + path, timeout=15,
                                            is_allowed=lambda u: False) is None
        # …and the same page IS read when the guard allows it, so this cannot pass by never rendering
        assert page_render.render_page_text("file://" + path, timeout=15,
                                            is_allowed=lambda u: True) is not None
    finally:
        os.unlink(path)


def test_the_fetcher_hands_the_renderer_its_ssrf_guard():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "app/services/search_service.py")).read()
    assert "is_safe_url(u)[0]" in src, "the render fallback was wired up without the guard"


def test_a_page_that_never_renders_is_not_reported_as_empty():
    """None means "could not read", never "the page is blank" — same distinction every other
    reader here draws. A page with no body text at all must not come back as ("", "")."""
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write("<!doctype html><title>Nothing</title><body></body>")
        path = fh.name
    try:
        if not page_render.chrome_available():
            pytest.skip("no Chrome on this node")
        assert page_render.render_page_text("file://" + path, timeout=8) is None
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------------------------
# 2. What we read outranks what the model thinks it knows.
# ---------------------------------------------------------------------------------------------

QUESTION = "check Jordan's posts and tell me if he's an asshole or nice person"
FETCHED = ("\n\n---\nContent from https://poster.place/npub1p3za04z:\nTitle: Jordan S\n\n"
           "Terminally online Crypto-Anarchist that re-uploads bizarre memes.\n---")


def test_the_fetched_page_comes_before_the_question():
    msg = SearchService.build_grounded_message(QUESTION, FETCHED)
    assert msg.index("Crypto-Anarchist") < msg.index(QUESTION), (
        "the question was put first again — that is exactly how 4709 chars of a real profile "
        "lost to the model's memory of a famous Jordan")


def test_the_question_is_still_there():
    """Grounding must not eat the request: content-first only helps if the ask survives."""
    assert QUESTION in SearchService.build_grounded_message(QUESTION, FETCHED)


def test_a_famous_namesake_is_ruled_out_in_words():
    note = SearchService.GROUNDING_NOTE.lower()
    assert "famous" in note and "coincidence" in note
    assert "only from it" in note


def test_it_says_what_to_do_when_the_page_does_not_answer():
    assert "say exactly that" in SearchService.GROUNDING_NOTE.lower()


def test_a_bare_url_keeps_its_summarize_instruction_after_the_content():
    msg = SearchService.build_grounded_message(
        "", FETCHED, instruction="Write a single concise paragraph summarizing the above.")
    assert msg.index("Crypto-Anarchist") < msg.index("Write a single concise paragraph")
    assert "The user's message:" not in msg      # there was no message, only a link


@pytest.mark.parametrize("module,marker", [
    ("app/routers/chat.py", "build_grounded_message"),
    ("app/routers/openai_api.py", "build_grounded_message"),
    ("app/routers/telegram/messages_chat.py", "build_grounded_message"),
])
def test_every_chat_surface_uses_the_one_ordering(module, marker):
    """Three surfaces had three orderings of the same three pieces, so the same fetch behaved
    three ways. If a new one appends URL context by hand, this is the test that should fail."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert marker in open(os.path.join(root, module)).read(), module


# ---------------------------------------------------------------------------------------------
# 3. A link is read, not looked up.
# ---------------------------------------------------------------------------------------------

class _FakeChat:
    def __init__(self, reply):
        self.reply = reply

    async def chat(self, messages, **kw):
        return self.reply


def _intent(message, model_says):
    from app.services import intent_service as _is
    svc = _is.IntentService.__new__(_is.IntentService)
    svc.db = None
    svc.user = None
    svc.chat_service = _FakeChat(model_says)
    return _run(svc.detect_intent(message))


@pytest.mark.parametrize("message,model_says", [
    ("Summarize this page: https://www.cnn.com/", "search https://www.cnn.com/"),
    ("what does this say https://example.com/article", "search https://example.com/article"),
    ("check this guy's posts https://poster.place/npub1p3za04z7mv86mkjzz",
     "search poster.place npub1p3za04z7mv86mkjzz"),
    ("any news on this https://example.com/x", "news https://example.com/x"),
])
def test_a_linked_page_is_never_turned_into_a_web_search(message, model_says):
    assert _intent(message, model_says) is None, (
        "the classifier searched FOR the url instead of letting chat read it")


@pytest.mark.parametrize("message,model_says,expected", [
    # A search with no link in the message is untouched.
    ("Search for the latest AI news", "search latest AI news", "search latest AI news"),
    # Things you legitimately DO with a link keep working.
    ("Download this song https://youtu.be/xyz7890abcd", "ytdl https://youtu.be/xyz7890abcd",
     "ytdl https://youtu.be/xyz7890abcd"),
    ("Summarize https://youtube.com/watch?v=abc123456", "yt https://youtube.com/watch?v=abc123456",
     "yt https://youtube.com/watch?v=abc123456"),
])
def test_the_guard_only_drops_the_lookup_verbs(message, model_says, expected):
    got = _intent(message, model_says)
    assert got and got["command"] == expected


# ---------------------------------------------------------------------------------------------
# The nostr special case is gone — this is the regression that keeps it gone.
# ---------------------------------------------------------------------------------------------

def test_the_url_reader_holds_no_per_site_knowledge():
    """`fetch_url_content` briefly resolved nostr entities off the relays itself. It worked, and it
    was a second kind of URL reading that only one kind of link could ever benefit from. A page is
    read by fetching it and, when that says nothing, rendering it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "app/services/search_service.py")).read()
    for token in ("_fetch_nostr_entity", "NOSTR_BARE_ENTITY", "_NOSTR_ENTITY_RE",
                  "_nostr_profile_data"):
        assert token not in src, f"{token} is back in the generic URL reader"

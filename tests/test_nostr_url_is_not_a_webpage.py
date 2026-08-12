"""A nostr profile link must be read from the RELAYS, never fetched as a web page.

Run: venv-unified/bin/python -m pytest tests/test_nostr_url_is_not_a_webpage.py

THE BUG. Asked "tell me about this user: https://poster.place/npub14q8uff…", the assistant answered
about an e-commerce poster shop — "The Poster Place (posterplace.co) … 1,835+ Facebook followers and
active Shopify store". Confidently, and about entirely the wrong subject.

Nothing was broken in the fetch. `https://<instance>/<npub…>` is a CLIENT ROUTE: the profile is
pulled from relays by JS that never runs server-side, so the fetch returns 200 and ~39 KB of shell.
Measured on the reported URL, the extracted text was "PosterChan · Nostr … Offline — showing saved
posts The Ultimate Nostr Experience POSTER//CHAN", with the npub appearing nowhere in it. Handed that
as "the page", the model has no user to describe, falls back to a web search for the host name, and
finds a poster shop.

This is the same failure `fetch_url_content` already documents for YouTube — "the watch-page HTML is
contentless and makes the LLM hallucinate" — so the fix sits at the same interception point: resolve
the entity from the relays and hand the model a person.

What is asserted here (all offline — the relay layer is stubbed, so this cannot flake on a network):

  intercepts      npub/nprofile/note/nevent, on ANY host, and as a bare `nostr:` URI. Host-agnostic
                  because the entity is self-describing: njump, primal and this instance all name the
                  same person and all serve a client shell to a server-side fetch.
  falls through   an ordinary URL returns None so the normal HTML path still runs. This is the half
                  that keeps "Summarize this page: https://cnn.com" working, and it is the half a
                  greedy regex would quietly break.
  says what it is the content must announce it is a nostr profile and must carry the npub, since the
                  failure mode being fixed is the model reaching for the domain name instead.
  never guesses   with no metadata on the relays, it says so and tells the model not to infer from
                  the URL — an empty answer beats a confident wrong one.
"""
import asyncio
import re

import pytest

from app.services.search_service import SearchService

NPUB = "npub14q8uffuxxnhzd24u4j23kn8a0dt2ux96h5eutt7u76lddhyqa0gs97ct2x"
SVC = SearchService.__new__(SearchService)      # this path needs no DB


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("url", [
    f"https://poster.place/{NPUB}",
    f"https://njump.me/{NPUB}",
    f"https://primal.net/p/{NPUB}",
    f"nostr:{NPUB}",
    f"https://poster.place/client?e={NPUB}",
    "https://poster.place/note1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
])
def test_a_nostr_entity_is_recognised_wherever_it_appears(url):
    assert SearchService._NOSTR_ENTITY_RE.search(url), url


@pytest.mark.parametrize("url", [
    "https://cnn.com",
    "https://poster.place/client",
    "https://en.wikipedia.org/wiki/Nostr",
    "https://example.com/notes/2024",          # 'note' as a word, not an entity
    "https://example.com/npubs-explained",     # 'npub' as a word, not an entity
])
def test_an_ordinary_url_is_left_alone(url):
    """The regex must not be greedy. If this fails, "Summarize this page: https://cnn.com" breaks —
    the case the user confirmed WORKS, which a fix must not trade away."""
    assert _run(SVC._fetch_nostr_entity(url)) is None, url


def test_naddr_falls_through_deliberately():
    """naddr's TLV type-0 is a `d` identifier, not a 32-byte key, so there is nothing to look up
    without also carrying the kind and author. Falling through is the documented choice; guessing is
    what produced the original bug."""
    assert _run(SVC._fetch_nostr_entity(
        "https://poster.place/naddr1qqxnzd3exqmrzv3exgmr2wfeqgsx")) is None


def _stub_nostr(monkeypatch, meta, notes):
    """Stub the relay layer so these assertions are about OUR formatting, not the network."""
    from app.services.nostr import nostr_service as ns, relay as rl

    async def fake_meta(pk, relays):
        return meta

    async def fake_query(relays, filters, timeout=None, **kw):
        return notes

    monkeypatch.setattr(ns, "get_metadata", fake_meta)
    monkeypatch.setattr(rl, "query", fake_query)


def test_the_model_is_told_this_is_a_person_not_a_page(monkeypatch):
    _stub_nostr(monkeypatch,
                {"display_name": "Uno", "about": "REJECT THE ORDINARY.", "nip05": "me@primal.net"},
                [{"created_at": 1760000000, "content": "hello nostr", "pubkey": "a" * 64}])
    got = _run(SVC._fetch_nostr_entity(f"https://poster.place/{NPUB}"))
    assert got is not None and got["error"] is None
    body = got["content"]
    # The framing is the fix: without it the model reaches for the host name.
    assert "NOT a web page" in body
    assert "Uno" in body and NPUB in body
    assert "REJECT THE ORDINARY." in body and "me@primal.net" in body
    assert "hello nostr" in body
    assert "Nostr profile" in got["title"]


def test_an_unknown_user_is_said_to_be_unknown_never_guessed(monkeypatch):
    """The reported answer was invented from the DOMAIN. With nothing on the relays the content has to
    say so out loud, because 'no information' is the honest answer and the one that was not given."""
    _stub_nostr(monkeypatch, {}, [])
    body = _run(SVC._fetch_nostr_entity(f"https://poster.place/{NPUB}"))["content"]
    assert "No profile metadata" in body
    assert "Do NOT guess" in body
    assert "No recent posts" in body


def test_the_note_list_is_capped(monkeypatch):
    """`limit` is per-RELAY, so a 20-note request across the pool came back with 167 — enough to
    overrun max_length and get chopped mid-sentence, which reads as a post trailing off."""
    _stub_nostr(monkeypatch, {"name": "Uno"},
                [{"created_at": 1760000000 + i, "content": f"post {i}", "pubkey": "a" * 64}
                 for i in range(167)])
    body = _run(SVC._fetch_nostr_entity(f"https://poster.place/{NPUB}"))["content"]
    assert "Their 20 most recent posts:" in body
    assert len(re.findall(r"^- \[", body, re.M)) == 20
    # Newest first — a profile summary built from the oldest 20 posts describes who they used to be.
    assert "post 166" in body and "post 0" not in body


def test_it_runs_before_the_html_fetch(monkeypatch):
    """Wiring check. Resolving correctly is worthless if fetch_url_content still fetches the shell."""
    import inspect
    src = inspect.getsource(SearchService.fetch_url_content)
    assert "_fetch_nostr_entity" in src
    assert src.index("_fetch_nostr_entity") < src.index("is_safe_url"), \
        "the nostr path must be tried before the HTML fetch, or the shell is what the model gets"


# --- a BARE npub is a link too --------------------------------------------------------------
# "tell me about npub14q8uff…" extracted NOTHING, so the chat fetched nothing, and the model —
# asked about a user it had no data for — invented one, producing a display name and a bio out of
# the surrounding words. Pasting a bare npub is the normal way to name somebody on nostr, so this
# is the common case rather than the edge, and the URL-only fix did not cover it.

@pytest.mark.parametrize("text", [
    f"tell me about {NPUB}",
    f"tell me about nostr:{NPUB}",
    f"who is {NPUB}?",
])
def test_a_bare_entity_becomes_a_fetchable_pseudo_url(text):
    got = SearchService.extract_urls(text)
    assert got == [f"nostr:{NPUB}"], got


def test_a_profile_url_is_not_also_fetched_as_a_bare_entity():
    """Both forms name the same person. Emitting both fetches the profile TWICE and burns one of the
    three URL slots a message gets."""
    got = SearchService.extract_urls(f"https://poster.place/{NPUB}")
    assert got == [f"https://poster.place/{NPUB}"], got


@pytest.mark.parametrize("text", [
    "what is a npub anyway?",
    "Summarize this page: https://cnn.com",
    "note this down for me",
    "nevermind",
])
def test_ordinary_prose_produces_no_nostr_entity(text):
    assert not [u for u in SearchService.extract_urls(text) if u.startswith("nostr:")], text


def test_a_real_url_still_survives_alongside_a_bare_entity():
    got = SearchService.extract_urls(f"check example.com and {NPUB}")
    assert "https://example.com" in got and f"nostr:{NPUB}" in got, got


def test_the_pseudo_url_is_one_the_resolver_accepts():
    """The whole point of emitting `nostr:<entity>` is that it travels the SAME path as a profile
    link. If the resolver stopped accepting that form, extraction would succeed and the fetch would
    silently produce nothing — the original bug with an extra step."""
    assert SearchService._NOSTR_ENTITY_RE.search(f"nostr:{NPUB}")

"""The Nostr block bot is a MUTE notifier too: it sees the mutes that live on other relays, calls them
mutes, announces each one once, and can actually read its data.

Run: venv-unified/bin/python -m pytest tests/test_nostr_mute_notifier.py

Four problems, each measured before the change:

  1. It could not read anything. A Nostr bot with no bots API key configured sent no credential to
     /api/community/*, and its first poll after being created (server1, 2026-09-26 16:49) was
     "HTTP Error 401" -- logged as "could not read", retried for ever, nothing ever announced. The
     manager now hands every bot a credential scoped to those read-only endpoints.
  2. It saw a third of the mutes. A mute list lives on its author's relays; this node's relay only
     held 583 of the 1,564 (muter, member) pairs four public relays had for poster.place's members.
     The node's upstream relays are now asked too -- signatures verified, newest list per author wins.
  3. A mute was announced as a block ("X blocked Y (on Nostr)"), and the default AI prompt's example
     pushed any rewording back to "blocked".
  4. Forgetting a mute the moment it went missing: with several relays behind the answer, one relay
     not answering made a mute vanish for a poll and be announced again when it came back.
"""
import asyncio
import json
import os
import sys
import time

import pytest

from app.services import community_stats as cs, settings_store
from app.services.nostr.event import build_event

KEYS = {n: bytes([i + 1]) * 32 for i, n in enumerate(("alice", "bob", "muter", "other"))}
from app.services.nostr import bip340  # noqa: E402
PK = {n: bip340.pubkey_from_seckey(k).hex() for n, k in KEYS.items()}


def run(c):
    return asyncio.run(c)


def mute_list(who, targets, at):
    return build_event(KEYS[who], 10000, "", [["p", PK[t]] for t in targets], created_at=at)


@pytest.fixture
def upstream(monkeypatch):
    """Two public relays whose answers the test controls; the local relay holds nothing."""
    relays = {"wss://one": [], "wss://two": []}
    asked = []

    async def ask(uri, pks):
        asked.append(uri)
        r = relays[uri]
        if isinstance(r, Exception):
            raise r
        return [ev for ev in r if any(t[0] == "p" and t[1] in pks for t in ev["tags"])]

    async def local(port, filters, **kw):
        return []
    monkeypatch.setattr(cs, "_upstream_relays", lambda: list(relays))
    monkeypatch.setattr(cs, "_ask_relay", ask)
    monkeypatch.setattr(cs, "_ext_lists", {})
    monkeypatch.setattr(cs, "_ext_state", {"at": 0.0})
    monkeypatch.setattr(cs, "_ext_lock", None)
    from app.services import nostr_store
    monkeypatch.setattr(nostr_store, "_ws_query", local)
    known = {PK["alice"]: "@alice@poster.place", PK["bob"]: "@bob@poster.place"}
    return {"relays": relays, "asked": asked, "known": known}


def _pairs(rel):
    return {(r["blocker"], r["blocked"]) for r in rel}


# ---- 2. mutes that only exist on other relays ----------------------------------------------------

def test_a_mute_that_only_lives_on_a_public_relay_is_seen(upstream):
    upstream["relays"]["wss://two"].append(mute_list("muter", ["alice"], 1000))
    got = _pairs(run(cs.mute_relations(upstream["known"])))
    assert (PK["muter"], PK["alice"]) in got


def test_a_forged_mute_list_is_ignored(upstream):
    ev = mute_list("muter", ["alice"], 1000)
    ev["tags"] = [["p", PK["bob"]]]                       # tampered after signing
    upstream["relays"]["wss://one"].append(ev)
    assert run(cs.mute_relations(upstream["known"])) == [], \
        "an unverified list would let anyone announce that a stranger muted one of ours"


def test_the_newest_list_wins_wherever_it_was_found(upstream):
    upstream["relays"]["wss://one"].append(mute_list("muter", ["alice", "bob"], 1000))
    upstream["relays"]["wss://two"].append(mute_list("muter", ["bob"], 2000))   # later: unmuted alice
    got = _pairs(run(cs.mute_relations(upstream["known"])))
    assert got == {(PK["muter"], PK["bob"])}


def test_a_relay_that_does_not_answer_is_not_an_unmute(upstream, monkeypatch):
    upstream["relays"]["wss://one"].append(mute_list("muter", ["alice"], 1000))
    assert _pairs(run(cs.mute_relations(upstream["known"]))) == {(PK["muter"], PK["alice"])}
    upstream["relays"]["wss://one"] = ConnectionError("down")
    monkeypatch.setitem(cs._ext_state, "at", 0.0)         # force a fresh ask
    assert _pairs(run(cs.mute_relations(upstream["known"]))) == {(PK["muter"], PK["alice"])}, \
        "a relay failing to answer made a known mute disappear"


def test_upstream_is_asked_at_most_every_ttl(upstream):
    upstream["relays"]["wss://one"].append(mute_list("muter", ["alice"], 1000))
    run(cs.mute_relations(upstream["known"]))
    run(cs.mute_relations(upstream["known"]))
    assert upstream["asked"].count("wss://one") == 1, "the minute-by-minute poll must not hit public relays"


# ---- 1. the bot can read its data ----------------------------------------------------------------

def test_the_manager_hands_every_bot_a_community_credential(monkeypatch):
    from app.services import bot_manager_service as bm
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    env = bm._load_global_env() if hasattr(bm, "_load_global_env") else None
    assert env and env.get("POSTERCHANAI_COMMUNITY_TOKEN") == cs.bot_token()
    assert len(cs.bot_token()) == 64 and cs.bot_token() == cs.bot_token(), "must be stable, not per call"


def test_the_community_api_accepts_that_credential_and_nothing_like_it(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.routers import community

    app = FastAPI()
    app.include_router(community.router)

    class _Q:
        def all(self):
            return []

    class _DB:
        def query(self, *a):
            return _Q()
    app.dependency_overrides[get_db] = lambda: _DB()
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)

    async def blocks():
        return [{"via": "nostr", "blocker": "x", "blocked": "y"}]
    monkeypatch.setattr(cs, "blocks", blocks)
    route = next(r for r in community.router.routes if r.path.endswith("/blocks"))
    with TestClient(app) as tc:
        assert tc.get(route.path).status_code == 401, "no credential must still be refused"
        assert tc.get(route.path, headers={"X-PC-Community-Token": "0" * 64}).status_code in (401, 403)
        ok = tc.get(route.path, headers={"X-PC-Community-Token": cs.bot_token()})
        assert ok.status_code == 200, ok.text


def test_the_bot_sends_it(monkeypatch):
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    sys.modules.pop("community_api", None)
    monkeypatch.setenv("POSTERCHANAI_COMMUNITY_TOKEN", "tok-123")
    import community_api
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"blocks": []}'

    def fake_open(req, timeout=0):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return _Resp()
    monkeypatch.setattr(community_api.request, "urlopen", fake_open)
    assert community_api.get("blocks") == {"blocks": []}
    assert seen.get("x-pc-community-token") == "tok-123"


# ---- 3 + 4. the bot's announcements --------------------------------------------------------------

@pytest.fixture
def bot(monkeypatch, tmp_path):
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    for m in ("nostr_blockbot", "nostr_engagement", "community_api"):
        sys.modules.pop(m, None)
    import nostr_blockbot
    monkeypatch.setattr(nostr_blockbot, "_state_path", lambda: str(tmp_path / "state.json"))
    posted, rows = [], []
    monkeypatch.setattr(nostr_blockbot, "_post", lambda text, *a, **k: posted.append(text))
    monkeypatch.setattr(nostr_blockbot, "_ai_on", lambda: False)
    monkeypatch.setattr(nostr_blockbot.community_api, "get", lambda path, **k: {"blocks": list(rows)})
    return nostr_blockbot, posted, rows


MUTE = {"via": "nostr", "blocker": "m", "blocked": "a", "blocker_handle": "nostr:npub1muter",
        "blocked_handle": "@alice@poster.place", "at": 1}


def test_a_nostr_mute_is_announced_as_a_mute(bot):
    b, posted, rows = bot
    b.blocks()                                            # first look remembers only
    rows.append(dict(MUTE))
    b.blocks()
    assert posted == ["MUTER: nostr:npub1muter muted @alice@poster.place (on Nostr)"]


def test_a_mute_that_blinks_out_for_a_poll_is_not_announced_twice(bot):
    b, posted, rows = bot
    b.blocks()
    rows.append(dict(MUTE))
    b.blocks()
    rows.clear()
    b.blocks()                                            # a relay did not answer
    rows.append(dict(MUTE))
    b.blocks()
    assert len(posted) == 1, "the same mute was announced again after one missing poll"


def test_a_mute_gone_for_days_is_news_again_when_it_returns(bot, monkeypatch):
    b, posted, rows = bot
    b.blocks()
    rows.append(dict(MUTE))
    b.blocks()
    rows.clear()
    b.blocks()
    later = time.time() + b.FORGET_AFTER + 60
    monkeypatch.setattr(b.time, "time", lambda: later)
    b.blocks()                                            # still gone, now long enough to forget
    rows.append(dict(MUTE))
    b.blocks()
    assert len(posted) == 2


def test_an_old_memory_file_still_loads(bot, tmp_path):
    b, posted, rows = bot
    with open(b._state_path(), "w") as f:
        json.dump({"seen": [b._key(MUTE)], "at": 1}, f)
    rows.append(dict(MUTE))
    b.blocks()
    assert posted == [], "a mute remembered by the previous version must not be re-announced"


def test_an_ai_rewording_may_not_turn_a_mute_into_a_block(bot):
    b, _, _ = bot
    handles = ["nostr:npub1muter", "@alice@poster.place"]
    assert not b.validate_block_message("BLOCKER: nostr:npub1muter blocked @alice@poster.place!", handles, mutes=True)
    assert b.validate_block_message("nostr:npub1muter just MUTED @alice@poster.place!", handles, mutes=True)
    assert b.validate_block_message("BLOCKER: nostr:npub1muter blocked @alice@poster.place", handles, mutes=False)


def test_the_upstream_relay_list_really_resolves(monkeypatch):
    """Not stubbed: every other test here replaces _upstream_relays/_ask_relay, which is how an
    ImportError inside them (`from app.services import nostr_service` -- it lives under .nostr)
    passed every test and would have made the upstream read fail silently in production."""
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: d)
    relays = cs._upstream_relays()
    assert relays and all(r.startswith("wss://") for r in relays) and len(relays) <= cs._EXT_RELAYS_MAX
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: "wss://a.example\nwss://b.example" if k == "nostr_relay_upstream_relays" else d)
    assert [r.rstrip("/") for r in cs._upstream_relays()] == ["wss://a.example", "wss://b.example"]
    import inspect
    src = inspect.getsource(cs._ask_relay)
    assert "from app.services.nostr import nostr_service" in src


def test_a_burst_is_one_readable_post(bot):
    b, posted, rows = bot
    b.blocks()
    for i in range(25):
        rows.append(dict(MUTE, blocker=f"m{i}", blocker_handle=f"nostr:npub1m{i}"))
    b.blocks()
    assert len(posted) == 1 and posted[0].count("\n") == b.MAX_LINES and posted[0].endswith("and 15 more")
    b.blocks()
    assert len(posted) == 1, "the ones counted but not listed came back as posts of their own"

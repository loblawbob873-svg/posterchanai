"""The community bots on Nostr and the fediverse, and what they read.

  * community_stats: blocks from BOTH sides (fediverse Blocks at the ActivityPub server, public Nostr
    mute lists), the most-blocked leaderboard, active members and the day's top posts -- what the
    Pleroma block bot and engagement used to read out of Pleroma's own database.
  * the Nostr block bot announces only what is NEW, and its first look announces nothing.
  * a Nostr bot created without a key gets one, with a NIP-05 name nobody else holds.
  * the delivered-notes ledger is pruned on the relay's retention window (the job that lived in the
    retired bridge).
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, FediBridgeDelivered, FediPuppet
from app.services import settings_store

ALICE, BOB, CAROL_PUPPET, STRANGER = "a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32


def run(c):
    return asyncio.run(c)


@pytest.fixture
def world(monkeypatch):
    settings = {"activitypub_domain": "poster.place", "nostr_relay_nip05_names": f"alice {ALICE}\nbob {BOB}",
                "nostr_relay_retention_days": "30"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: settings.get(k, d))
    monkeypatch.setattr(settings_store, "_port", lambda db=None: 1)
    from app.services.activitypub import actors
    actors._names_cache["raw"] = None
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[FediBridgeDelivered.__table__, FediPuppet.__table__])
    Session = sessionmaker(bind=engine)
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", Session)
    s = Session()
    s.add(FediPuppet(actor_uri="https://m.example/users/carol", acct="carol@m.example", pubkey_hex=CAROL_PUPPET,
                     nip05_name="carol"))
    s.commit()
    relay = []

    async def ws_query(port, filters, **kw):
        f = filters[0]
        out = []
        for e in relay:
            if e["kind"] not in f.get("kinds", [e["kind"]]):
                continue
            if "authors" in f and e["pubkey"] not in f["authors"]:
                continue
            if "#p" in f and not any(t[0] == "p" and t[1] in f["#p"] for t in e["tags"]):
                continue
            if "#e" in f and not any(t[0] == "e" and t[1] in f["#e"] for t in e["tags"]):
                continue
            if e["created_at"] < f.get("since", 0):
                continue
            out.append(e)
        return out
    from app.services import nostr_store
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    fedi_blocks = []

    async def blocks(strict=True):
        return list(fedi_blocks)
    from app.services.activitypub import state
    monkeypatch.setattr(state, "blocks", blocks)
    return {"settings": settings, "relay": relay, "fedi": fedi_blocks, "Session": Session}


def _ev(i, pk, kind, tags, content="", created=None):
    return {"id": f"{i:064x}", "pubkey": pk, "kind": kind, "tags": tags, "content": content,
            "created_at": created or int(time.time())}


def test_blocks_come_from_both_the_fediverse_and_nostr_mute_lists(world):
    from app.services import community_stats as cs
    world["fedi"].append({"member": ALICE, "actor": "https://m.example/users/carol", "acct": "carol@m.example", "at": 100})
    world["relay"].append(_ev(1, STRANGER, 10000, [["p", BOB]], created=200))            # a stranger mutes bob
    world["relay"].append(_ev(2, ALICE, 10000, [["p", CAROL_PUPPET]], created=300))      # alice mutes carol
    world["relay"].append(_ev(3, STRANGER, 10000, [["p", "e5" * 32]], created=400))      # nothing to do with us
    got = {(b["via"], b["blocker_handle"], b["blocked_handle"]) for b in run(cs.blocks())}
    assert ("fediverse", "@carol@m.example", "@alice@poster.place") in got
    assert ("nostr", "@alice@poster.place", "@carol@m.example") in got
    assert any(v == "nostr" and blocked == "@bob@poster.place" and blocker.startswith("nostr:npub1")
               for v, blocker, blocked in got)
    assert len(got) == 3
    board = run(cs.leaderboard())
    assert [(r["handle"], r["count"]) for r in board] == [("@alice@poster.place", 1), ("@bob@poster.place", 1)]


def test_activity_counts_members_and_ranks_their_posts(world):
    from app.services import community_stats as cs
    now = int(time.time())
    hot, quiet = _ev(10, ALICE, 1, [], "hot take", now - 100), _ev(11, BOB, 1, [], "quiet", now - 50)
    mirror = _ev(12, ALICE, 1, [["proxy", "https://x", "activitypub"]], "mirror", now - 10)
    world["relay"] += [hot, quiet, mirror, _ev(13, STRANGER, 7, [["e", hot["id"]]], "+", now),
                       _ev(14, STRANGER, 6, [["e", hot["id"]]], "", now), _ev(15, BOB, 7, [["e", quiet["id"]]], "🔥", now),
                       _ev(16, BOB, 1, [], "old", now - 20 * 86400)]
    a = run(cs.activity())
    assert a["dau"] == 2 and a["mau"] == 2 and a["members"] == 2
    assert [p["text"] for p in a["top_posts"]] == ["hot take", "quiet"]
    assert a["top_posts"][0]["reposts"] == 1 and a["top_posts"][0]["handle"] == "@alice@poster.place"


def test_the_ledger_is_pruned_on_the_relays_retention_window(world):
    from app.services.activitypub import ledger
    s = world["Session"]()
    old = datetime.utcnow() - timedelta(days=40)
    for n, when in (("old", old), ("new", datetime.utcnow())):
        s.add(FediBridgeDelivered(platform="activitypub", instance_url="https://m.example", note_id=n,
                                  note_uri=f"https://m.example/{n}", nostr_event_id=n * 8, created_at=when))
    s.commit()
    world["settings"]["nostr_relay_retention_days"] = "0"
    assert ledger.prune() == 0, "Auto-clean off keeps the ledger too"
    world["settings"]["nostr_relay_retention_days"] = "30"
    assert ledger.prune() == 1
    assert [r.note_id for r in world["Session"]().query(FediBridgeDelivered).all()] == ["new"]


def test_a_nostr_bot_without_a_key_gets_one_and_a_free_name(world, monkeypatch):
    from app.routers import bots
    world["settings"]["nostr_relay_nip05_names"] = f"posterchan {ALICE}\nposterchan-bot {BOB}"
    assert bots._free_nip05_name("PosterChan") == "posterchan-bot2", "an automatic name took one somebody holds"
    assert bots._free_nip05_name("posterchan", ALICE) == "posterchan", "a bot's own name is not 'taken'"
    minted = []

    async def mint(nip05, host):
        minted.append(nip05)
        return {"nsec": "nsec1new", "npub": "npub1new", "nip05": f"{nip05}@{host}", "followed": True}
    monkeypatch.setattr(bots, "_mint_identity", mint)
    cfg = bots._ensure_identity("Weather", {"prompt": "x"}, "poster.place")
    assert cfg["nostr_nsec"] == "nsec1new" and cfg["nostr_profile_nip05"] == "weather@poster.place"
    assert cfg["nostr_profile_name"] == "Weather" and cfg["prompt"] == "x"
    kept = bots._ensure_identity("Weather", {"nostr_nsec": "nsec1mine"}, "poster.place")
    assert kept == {"nostr_nsec": "nsec1mine"} and minted == ["weather"], "a supplied key was replaced"


@pytest.fixture
def blockbot(monkeypatch, tmp_path):
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    for m in ("nostr_blockbot", "nostr_engagement", "community_api"):
        sys.modules.pop(m, None)
    import nostr_blockbot
    monkeypatch.setattr(nostr_blockbot, "_state_path", lambda: str(tmp_path / "state.json"))
    return nostr_blockbot


def test_the_block_bot_announces_only_what_is_new(blockbot, monkeypatch):
    posted, rows = [], []
    monkeypatch.setattr(blockbot, "_post", lambda text, *a, **k: posted.append(text))
    monkeypatch.setattr(blockbot, "_ai_on", lambda: False)
    monkeypatch.setattr(blockbot.community_api, "get", lambda path, **k: {"blocks": list(rows)})
    rows.append({"via": "nostr", "blocker": "x", "blocked": ALICE, "blocker_handle": "nostr:npub1x",
                 "blocked_handle": "@alice@poster.place", "at": 1})
    blockbot.blocks()
    assert posted == [], "the first look announced every block that already existed"
    rows.append({"via": "fediverse", "blocker": "https://m.example/users/carol", "blocked": BOB,
                 "blocker_handle": "@carol@m.example", "blocked_handle": "@bob@poster.place", "at": 2})
    blockbot.blocks()
    assert len(posted) == 1 and "@carol@m.example blocked @bob@poster.place" in posted[0]
    blockbot.blocks()
    assert len(posted) == 1, "a block was announced twice"


def test_the_block_bot_never_reads_could_not_ask_as_nothing_happened(blockbot, monkeypatch):
    posted = []
    monkeypatch.setattr(blockbot, "_post", lambda text, *a, **k: posted.append(text))

    def down(path, **k):
        raise blockbot.community_api.Unavailable("relay down")
    monkeypatch.setattr(blockbot.community_api, "get", down)
    blockbot.blocks()
    assert posted == [] and not os.path.exists(blockbot._state_path()), "an unreadable relay rewrote the memory"


def test_an_ai_rewording_must_keep_every_name(blockbot):
    names = ["@carol@m.example", "@bob@poster.place"]
    assert blockbot.validate_block_message("Wow! @carol@m.example blocked @bob@poster.place.", names)
    assert not blockbot.validate_block_message("Wow! Carol blocked @bob@poster.place.", names)


def _run_bot(*argv, env=None):
    import subprocess
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "botframework")
    e = {k: v for k, v in os.environ.items() if not k.startswith(("PLEROMA_", "NOSTR_", "POSTERCHANAI_"))}
    e.update(env or {})
    return subprocess.run([sys.executable, "main.py", *argv], cwd=here, env=e, capture_output=True,
                          text=True, timeout=120)


def test_every_bot_mode_still_parses():
    """A broken argparse entry raises at STARTUP, for every bot in every mode (the Nitter removal's
    lesson) -- no unit test of one mode can see it."""
    r = _run_bot("--help")
    assert r.returncode == 0, r.stderr[-2000:]
    assert "--blockbot" in r.stdout and "--blocks-print" in r.stdout


def test_a_nostr_only_block_bot_takes_the_nostr_path():
    """No PLEROMA_ENDPOINT, a Nostr key, nothing listening: the one-shot report goes to the Nostr block
    bot, which says it could not ask -- it must not fall into the Pleroma database code."""
    r = _run_bot("--blocks-print", env={"NOSTR_NSEC": "11" * 32,
                                        "POSTERCHANAI_API_ENDPOINT": "http://127.0.0.1:9"})
    out = r.stdout + r.stderr
    assert "psycopg" not in out and "PLEROMA_DB" not in out, out[-2000:]
    assert "could not" in out.lower() or "unavailable" in out.lower(), out[-2000:]


def test_one_server_cannot_top_the_leaderboard_by_itself(world):
    """A fediverse Block is free to fake in bulk (actors on a server somebody controls), so one server
    puts at most PER_SERVER blockers on a member's count."""
    from app.services import community_stats as cs
    for i in range(10):
        world["fedi"].append({"member": ALICE, "actor": f"https://spam.example/u/{i}", "acct": f"s{i}@spam.example", "at": i})
    world["fedi"].append({"member": BOB, "actor": "https://a.example/u/1", "acct": "a@a.example", "at": 1})
    board = {r["handle"]: r["count"] for r in run(cs.leaderboard())}
    assert board == {"@alice@poster.place": cs.PER_SERVER, "@bob@poster.place": 1}

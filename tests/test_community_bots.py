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
    # No public relays in a unit test: community_stats also asks the node's upstream relays for mute
    # lists (tests/test_nostr_mute_notifier.py covers that half), and a test must not reach the network.
    from app.services import community_stats
    monkeypatch.setattr(community_stats, "_upstream_relays", lambda: [])
    monkeypatch.setattr(community_stats, "_ext_lists", {})
    monkeypatch.setattr(community_stats, "_ext_state", {"at": 0.0})
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
    from app.services.nostr import nostr_service
    assert a["top_posts"][0]["ref"] == "nostr:" + nostr_service.npub_of(ALICE), \
        "the top-posts bot needs each author as a tag it can post, not only a printed name"


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


def test_a_bot_with_a_generated_key_and_no_name_still_gets_a_nip05(world, monkeypatch):
    """"Generate identity" with a blank NIP-05 field saved a bot with a key and NO name: no
    @name@<domain>, no nip05 in its profile (bot `fever`, 2026-09-26). The key is kept exactly as
    given; the NAME is still assigned -- free, or already this key's, never somebody else's."""
    from app.routers import bots
    from app.services.nostr import bech32, bip340
    sk = bytes([9]) * 32
    nsec, pub = bech32.encode("nsec", sk), bip340.pubkey_from_seckey(sk).hex()
    world["settings"]["nostr_relay_nip05_names"] = f"jonnyfever {ALICE}"
    monkeypatch.setattr(bots, "_mint_identity", lambda *a: (_ for _ in ()).throw(AssertionError("minted a new key")))
    cfg = bots._ensure_identity("fever", {"nostr_nsec": nsec, "nostr_profile_name": "JonnyFever"}, "poster.place")
    assert cfg["nostr_nsec"] == nsec, "the generated key was replaced"
    assert cfg["nostr_profile_nip05"] == "jonnyfever-bot@poster.place", \
        "the bot took a name somebody holds, or got none: %r" % cfg.get("nostr_profile_nip05")
    world["settings"]["nostr_relay_nip05_names"] = f"jonnyfever {pub}"
    assert bots._ensure_identity("fever", {"nostr_nsec": nsec, "nostr_profile_name": "JonnyFever"},
                                 "poster.place")["nostr_profile_nip05"] == "jonnyfever@poster.place", \
        "the bot's OWN registered name must be reused, not a -bot variant"
    chosen = {"nostr_nsec": nsec, "nostr_profile_nip05": "jf@poster.place"}
    assert bots._ensure_identity("fever", dict(chosen), "poster.place") == chosen, "a chosen NIP-05 was changed"


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


# ── The Nostr report bot and welcome bot ("make reportbot work for nostr but only for the instance
#    users ie poster.place" / "if you can get welcome bot too") ─────────────────────────────────────

def test_reports_are_only_the_ones_involving_our_members(world):
    from app.services import community_stats as cs
    note = "ab" * 32
    world["relay"] += [
        _ev(30, STRANGER, 1984, [["p", ALICE, "spam"], ["e", note, "spam"]], "bot spam", created=100),  # about ours
        _ev(31, BOB, 1984, [["p", CAROL_PUPPET, "impersonation"]], "  not   the real carol ", created=200),  # by ours
        _ev(32, STRANGER, 1984, [["p", "e5" * 32, "spam"]], "elsewhere", created=300),          # none of ours
        _ev(33, ALICE, 1984, [["p", ALICE, "other"]], "self", created=400),                     # self-report
        _ev(34, STRANGER, 1984, [["e", note, "spam"]], "no account named", created=500),         # not NIP-56
        _ev(35, STRANGER, 1984, [["p", BOB, "made-up-type"]], "", created=600),
    ]
    got = run(cs.reports())
    assert [r["id"] for r in got] == [f"{30:064x}", f"{31:064x}", f"{35:064x}"]
    a, b, c = got
    assert (a["reported_handle"], a["type"], a["reason"]) == ("@alice@poster.place", "spam", "bot spam")
    assert a["note_ref"].startswith("nostr:note1") and a["reporter_ref"].startswith("nostr:npub1")
    assert (b["reporter_handle"], b["reported_handle"]) == ("@bob@poster.place", "@carol@m.example")
    assert b["reason"] == "not the real carol", "whitespace is collapsed for a one-line headline"
    assert c["type"] == "other", "a type outside NIP-56's list is not repeated as fact"
    assert [r["id"] for r in run(cs.reports(since=250))] == [f"{35:064x}"]


def test_the_member_roll_marks_our_own_bots(world, monkeypatch):
    from app.services import community_stats as cs
    monkeypatch.setattr(cs, "bot_pubkeys", lambda: {BOB})
    roll = {m["pubkey"]: m for m in cs.member_list()}
    assert set(roll) == {ALICE, BOB}
    assert roll[BOB]["bot"] and not roll[ALICE]["bot"]
    assert roll[ALICE]["handle"] == "@alice@poster.place" and roll[ALICE]["ref"].startswith("nostr:npub1")


def _botmod(monkeypatch, tmp_path, name):
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    for m in (name, "community_api"):
        sys.modules.pop(m, None)
    mod = __import__(name)
    monkeypatch.setattr(mod, "_state_path", lambda: str(tmp_path / f"{name}.json"))
    monkeypatch.setattr(mod, "_ai_on", lambda: False)
    posted = []
    monkeypatch.setattr(mod, "_post", lambda text, *a, **k: posted.append(text))
    return mod, posted


def _report(i, reporter="nostr:npub1rep", reported="nostr:npub1bob", **kw):
    return {"id": f"r{i}", "at": i, "type": "spam", "reason": "", "note_ref": "",
            "reporter_ref": reporter, "reported_ref": reported,
            "reporter_handle": "@x@else.where", "reported_handle": "@bob@poster.place", **kw}


def test_the_report_bot_announces_each_report_once_and_never_on_its_first_look(monkeypatch, tmp_path):
    bot, posted = _botmod(monkeypatch, tmp_path, "nostr_reportbot")
    rows = [_report(1)]
    monkeypatch.setattr(bot.community_api, "get", lambda path, **k: {"reports": list(rows)})
    bot.reports()
    assert posted == [], "the first look announced every report of the last week"
    rows.append(_report(2, reason="stolen art", note_ref="nostr:note1xyz"))
    bot.reports()
    assert len(posted) == 1
    assert "🚨 nostr:npub1rep reported nostr:npub1bob for spam" in posted[0], posted[0]
    assert '"stolen art"' in posted[0] and "nostr:note1xyz" in posted[0]
    bot.reports()
    assert len(posted) == 1, "a report was announced twice"


def test_the_report_bot_never_reads_could_not_ask_as_no_reports(monkeypatch, tmp_path):
    bot, posted = _botmod(monkeypatch, tmp_path, "nostr_reportbot")

    def down(path, **k):
        raise bot.community_api.Unavailable("relay down")
    monkeypatch.setattr(bot.community_api, "get", down)
    bot.reports()
    assert posted == [] and not os.path.exists(bot._state_path())


def test_the_report_bot_skips_reports_involving_a_listed_bot(monkeypatch, tmp_path):
    bot, posted = _botmod(monkeypatch, tmp_path, "nostr_reportbot")
    monkeypatch.setattr(bot, "BOT_BLACKLIST", ["reportbot"])
    rows = []
    monkeypatch.setattr(bot.community_api, "get", lambda path, **k: {"reports": list(rows)})
    bot.reports()
    rows.append(_report(3, reporter_handle="@reportbot@poster.place"))
    bot.reports()
    assert posted == []


def _member(pk, name, **kw):
    return {"pubkey": pk, "handle": f"@{name}@poster.place", "ref": f"nostr:npub1{name}", "bot": False, **kw}


def test_the_welcome_bot_greets_a_new_member_by_tag_and_nobody_else(monkeypatch, tmp_path):
    bot, posted = _botmod(monkeypatch, tmp_path, "nostr_welcomebot")
    monkeypatch.setattr(bot, "WELCOME_MESSAGE", "Welcome to {instance_name}")
    roll = [_member(ALICE, "alice")]
    monkeypatch.setattr(bot.community_api, "get", lambda path, **k: {"members": list(roll)})
    bot.welcome()
    assert posted == [], "the first look welcomed every existing member"
    roll += [_member(BOB, "bob"), _member(STRANGER, "newbot", bot=True)]
    bot.welcome()
    assert posted == ["nostr:npub1bob Welcome to poster.place"], posted
    bot.welcome()
    assert len(posted) == 1, "a member was welcomed twice"


def test_the_welcome_bot_changes_nothing_on_an_empty_or_unreadable_roll(monkeypatch, tmp_path):
    bot, posted = _botmod(monkeypatch, tmp_path, "nostr_welcomebot")
    roll = [_member(ALICE, "alice")]
    monkeypatch.setattr(bot.community_api, "get", lambda path, **k: {"members": list(roll)})
    bot.welcome()
    roll.clear()
    bot.welcome()                              # reads empty for a moment...
    roll.append(_member(ALICE, "alice"))
    bot.welcome()                              # ...and back: alice is not a newcomer
    assert posted == []

    def down(path, **k):
        raise bot.community_api.Unavailable("app down")
    monkeypatch.setattr(bot.community_api, "get", down)
    bot.welcome()
    assert posted == []


def test_a_bulk_jump_in_the_roll_is_an_import_not_newcomers(monkeypatch, tmp_path):
    bot, posted = _botmod(monkeypatch, tmp_path, "nostr_welcomebot")
    roll = [_member(ALICE, "alice")]
    monkeypatch.setattr(bot.community_api, "get", lambda path, **k: {"members": list(roll)})
    bot.welcome()
    roll += [_member(f"{i:064x}", f"m{i}") for i in range(bot.BULK + 1)]
    bot.welcome()
    assert posted == []
    roll.append(_member(BOB, "bob"))
    bot.welcome()
    assert len(posted) == 1 and "nostr:npub1bob" in posted[0], "the import's names were not remembered"


def test_an_ai_welcome_tags_the_member_in_place_of_their_address(monkeypatch, tmp_path):
    bot, _ = _botmod(monkeypatch, tmp_path, "nostr_welcomebot")
    monkeypatch.setattr(bot, "_ai_on", lambda: True)
    monkeypatch.setattr(bot, "WELCOME_PROMPT", "Welcome @{username} to {instance_name}")
    m = _member(BOB, "bob")
    monkeypatch.setattr(bot, "generate_reply", lambda p: "Hey @bob@poster.place, glad you made it here!")
    assert bot.message(m) == "Hey nostr:npub1bob, glad you made it here!"
    monkeypatch.setattr(bot, "generate_reply", lambda p: "Glad you made it, friend, enjoy the place!")
    assert bot.message(m) == "nostr:npub1bob Glad you made it, friend, enjoy the place!"


def test_the_welcome_model_is_told_what_the_instance_is_and_that_it_knows_nothing_else(monkeypatch, tmp_path):
    """'the welcome is really hallucinating': told only 'welcome bob to poster.place', the model wrote a
    brochure for a Polish poster-art gallery. The prompt must carry the facts and forbid inventing,
    even under an operator's own WELCOME_PROMPT -- and a long brochure must not be posted."""
    bot, _ = _botmod(monkeypatch, tmp_path, "nostr_welcomebot")
    monkeypatch.setattr(bot, "_ai_on", lambda: True)
    monkeypatch.setattr(bot, "WELCOME_PROMPT", "Welcome the new user @{username} to {instance_name}. Use hashtags.")
    monkeypatch.setattr(bot, "WELCOME_MESSAGE", "Welcome to {instance_name}")
    seen = []
    monkeypatch.setattr(bot, "generate_reply", lambda p: seen.append(p) or "Welcome aboard @bob@poster.place, you are free here.")
    assert bot.message(_member(BOB, "bob")) == "Welcome aboard nostr:npub1bob, you are free here."
    prompt = seen[0]
    assert "Nostr community" in prompt and "do not describe the site" in prompt and "invent" in prompt
    brochure = ("WELCOME TO POSTER.PL! This platform is a digital gallery of Polish poster art. " * 12)
    monkeypatch.setattr(bot, "generate_reply", lambda p: brochure)
    assert bot.message(_member(BOB, "bob")) == "nostr:npub1bob Welcome to poster.place"


@pytest.mark.parametrize("flag", ["--report-print", "--welcome-print"])
def test_a_nostr_report_and_welcome_bot_take_the_nostr_path(flag):
    r = _run_bot(flag, env={"NOSTR_NSEC": "11" * 32, "POSTERCHANAI_API_ENDPOINT": "http://127.0.0.1:9"})
    out = r.stdout + r.stderr
    assert "psycopg" not in out and "PLEROMA" not in out and "Database configuration" not in out, out[-2000:]
    assert "could not read" in out.lower(), out[-2000:]


def test_a_bot_with_several_daemon_modes_runs_every_one_of_them(monkeypatch):
    """main.py ran its daemon modes through an elif chain, so `--blockbot --hashtagbot --welcome
    --report` started the block bot and NOTHING ELSE -- found by switching the welcome and report bots
    on and reading the log ("Starting the Nostr block bot daemon..." and no other line). Runs the
    shipped main() with stand-in daemon modules and asserts every one of them was started."""
    import threading
    import types
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    monkeypatch.delenv("PLEROMA_ENDPOINT", raising=False)
    for m in ("main", "config"):
        sys.modules.pop(m, None)
    ran, lock = [], threading.Lock()

    def fake(name, **extra):
        mod = types.ModuleType(name)
        mod.waitToStart = lambda: None

        def background():
            with lock:
                ran.append(name)
        mod.background = background
        for k, v in extra.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, name, mod)
    for n in ("nostr_blockbot", "nostr_welcomebot", "nostr_reportbot"):
        fake(n)
    fake("hashtagbot", get_config=lambda: None)
    fake("nostr", ensure_profile=lambda: None, ensure_server_list=lambda: None)
    import main
    monkeypatch.setattr(sys, "argv", ["main.py", "--blockbot", "--hashtagbot", "--welcome", "--report"])
    main.main()
    assert sorted(ran) == ["hashtagbot", "nostr_blockbot", "nostr_reportbot", "nostr_welcomebot"], ran


def test_one_daemon_dying_does_not_take_the_others(monkeypatch):
    import types
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    monkeypatch.setenv("NOSTR_NSEC", "11" * 32)
    monkeypatch.delenv("PLEROMA_ENDPOINT", raising=False)
    for m in ("main", "config"):
        sys.modules.pop(m, None)
    ran = []
    for n in ("nostr_welcomebot", "nostr_reportbot"):
        mod = types.ModuleType(n)
        mod.waitToStart = lambda: None
        if n == "nostr_welcomebot":
            def boom():
                raise RuntimeError("relay down")
            mod.background = boom
        else:
            mod.background = lambda n=n: ran.append(n)
        monkeypatch.setitem(sys.modules, n, mod)
    nk = types.ModuleType("nostr")
    nk.ensure_profile = nk.ensure_server_list = lambda: None
    monkeypatch.setitem(sys.modules, "nostr", nk)
    import main
    monkeypatch.setattr(sys, "argv", ["main.py", "--welcome", "--report"])
    main.main()
    assert ran == ["nostr_reportbot"]


def test_the_top_posts_post_tags_names_and_links_posts_plainly(blockbot, monkeypatch):
    """"see latest post summary, did not display right, names and links not clickable, bad markdown" --
    the model rewrote the whole list: `@mranderson` (opens nothing), `[View post](url)` (printed as raw
    brackets by Nostr clients) and an emoji reduced to its invisible joiners. The list is built in Python
    now and the model only writes one sentence before and after it."""
    import nostr_engagement as eng
    from app.services.nostr import nostr_service
    ref = "nostr:" + nostr_service.npub_of(ALICE)
    rows = [{"id": "aa" * 32, "handle": "@alice@poster.place", "ref": ref, "score": 19, "reactions": 9,
             "reposts": 3, "replies": 2, "text": "Didn't I tell you\nabout this", "url": "https://poster.place/note1abc"}]
    posted = []
    monkeypatch.setattr(eng, "_post", lambda text, *a, **k: posted.append(text))
    monkeypatch.setattr(eng.community_api, "get", lambda path, **k: {"top_posts": list(rows)})
    monkeypatch.setattr(eng, "_ai_on", lambda: True)
    # What the model actually sent on 2026-10-02, as both the intro and the outro.
    bad = ("Hey anon! Ready for your daily dose of internet gold? Let's dive in! \u200d\ufe0f\n\n #1: @mranderson - 19 pts\n"
           " [View post](https://poster.place/note1abc)")
    monkeypatch.setattr(eng, "generate_reply", lambda prompt: bad)
    eng.daily_top_posts()
    msg = posted[0]
    assert ref in msg, "the author is not a tag a client can open"
    assert "[View post]" not in msg and "](" not in msg, "markdown link in a Nostr post"
    assert "https://poster.place/note1abc" in msg, "the post's link is missing"
    assert "@mranderson" not in msg, "the model's rewrite of the list reached the post"
    assert "\u200d" not in msg and "\ufe0f" not in msg
    # A clean sentence from the model IS used, and its stray joiners are dropped.
    monkeypatch.setattr(eng, "generate_reply", lambda prompt: "Today was a good one! \u200d\ufe0f")
    eng.daily_top_posts()
    assert "Today was a good one!" in posted[1] and "\u200d" not in posted[1]
    assert posted[1].index("Today was a good one!") < posted[1].index(ref)


def test_blocks_ask_the_database_a_fixed_number_of_times_however_many_rows(world, monkeypatch):
    """/api/community/blocks named every account with its own query -- 1,786 of them per call on
    poster.place (2.4 s on the event loop, polled ~once a minute by the block bot). The puppet lookups are
    batched now: the number of sessions must not grow with the rows, and every name must still resolve."""
    import app.database
    from app.services import community_stats as cs
    s = world["Session"]()
    puppets = [f"{i:02x}" * 32 for i in range(20, 50)]
    for i, pk in enumerate(puppets):
        s.add(FediPuppet(actor_uri=f"https://m.example/users/u{i}", acct=f"u{i}@m.example", pubkey_hex=pk,
                         nip05_name=f"u{i}"))
    s.commit()
    for i, pk in enumerate(puppets):                                   # 30 puppets each mute alice
        world["relay"].append(_ev(100 + i, pk, 10000, [["p", ALICE]], created=500 + i))
        world["fedi"].append({"member": BOB, "actor": f"https://m.example/users/u{i}",   # and block bob
                              "acct": f"u{i}@m.example", "at": 50 + i})
    opened = []
    real = app.database.SessionLocal

    def counting():
        opened.append(1)
        return real()
    monkeypatch.setattr(app.database, "SessionLocal", counting)
    rows = run(cs.blocks())
    assert len(opened) <= 3, f"{len(opened)} database sessions for {len(rows)} rows"
    named = {(b["via"], b["blocker_handle"], b["blocked_handle"]) for b in rows}
    for i in range(30):
        assert ("nostr", f"@u{i}@m.example", "@alice@poster.place") in named
        assert ("fediverse", f"@u{i}@m.example", "@bob@poster.place") in named
    assert all(b["blocker_ref"].startswith("nostr:npub1") for b in rows if b["via"] == "fediverse")

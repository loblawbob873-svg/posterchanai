"""posterchan@poster.place "keeps losing its nip05" — "it was set earlier today and now its gone".

Measured 2026-09-28: three bot rows share one key ("Nostr" answers mentions, "lounge" sits in a
Concord room, "nostr-image" posts pictures). Each published ITS OWN kind-0 on every start, built from
its own three profile settings and nothing else, so the last one up won — and "lounge" has no NIP-05
in its config. The profile on the relay after the 18:09 restart: name "PosterChan", no nip05.

Two rules, each tested against the shipped code:
  * one profile per key (bot_manager_service._profile_owner);
  * a start MERGES into the profile the key already has and never erases a field it does not set,
    and a profile that could not be read is not published over (botframework/nostr.ensure_profile).
"""
import importlib
import json
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Bot
from app.services import bot_manager_service as mgr

SHARED = "nsec1shared-key-for-tests"


@pytest.fixture
def bots(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[Bot.__table__])
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(mgr, "SessionLocal", Session)
    mgr._PROFILE_OWNER_CACHE.update(ts=0.0, map={})
    s = Session()
    rows = [("Nostr", True, {"nostr_nsec": SHARED, "nostr_profile_name": "PosterChan AI", "nostr_profile_nip05": "posterchan"}),
            ("lounge", True, {"nostr_nsec": SHARED, "nostr_profile_name": "PosterChan"}),
            ("nostr-image", False, {"nostr_nsec": SHARED, "nostr_profile_nip05": "posterchan"}),
            ("chesstr", True, {"nostr_nsec": "nsec1other", "nostr_profile_name": "chesstr"})]
    for name, enabled, cfg in rows:
        s.add(Bot(name=name, platform="nostr", bot_type="text", enabled=enabled, host="", modes="nostr",
                  config=json.dumps(cfg)))
    s.commit()
    return {n: mgr.bot_to_dict(b) for n, b in ((b.name, b) for b in s.query(Bot).all())}


def test_one_bot_owns_a_shared_keys_profile(bots):
    assert mgr._profile_owner(bots["Nostr"]) is True, "the bot that carries the NIP-05 must own the profile"
    assert mgr._profile_owner(bots["lounge"]) is False, "a second bot on the same key still publishes over it"
    assert mgr._profile_owner(bots["chesstr"]) is True, "a bot with its own key lost its profile"


def test_the_manager_hands_the_profile_to_the_owner_only(bots):
    lounge = mgr._build_env(bots["lounge"], {})
    nostr = mgr._build_env(bots["Nostr"], {})
    assert not any(k.startswith("NOSTR_PROFILE_") for k in lounge), sorted(k for k in lounge if k.startswith("NOSTR_PROFILE"))
    assert nostr.get("NOSTR_PROFILE_NIP05", "").startswith("posterchan"), "the owner lost its NIP-05"
    assert lounge.get("NOSTR_NSEC") == SHARED, "the second bot must still run as the account"


# ------------------------------------------------------------------------------------ ensure_profile

@pytest.fixture
def bot(monkeypatch):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "botframework"))
    mod = importlib.import_module("nostr")
    relay = {"profile": None, "published": [], "readable": True}

    class R:
        @staticmethod
        async def query(relays, filters):
            if not relay["readable"]:
                return None
            return [relay["profile"]] if relay["profile"] else []

        @staticmethod
        async def publish(relays, ev):
            relay["published"].append(ev)
            relay["profile"] = ev
            return {"ok": True}

    class Svc:
        relay = R

    from app.services.nostr import nostr_service as ns
    sk = ns.decode_seckey("0" * 63 + "1")
    monkeypatch.setattr(mod, "_SECKEY", sk)
    monkeypatch.setattr(mod, "_PUBKEY", ns.derive_pubkey(sk) if isinstance(ns.derive_pubkey(sk), str) else ns.derive_pubkey(sk).hex())
    monkeypatch.setattr(mod, "_svc", Svc)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod, relay


def _profile(relay):
    return json.loads(relay["profile"]["content"])


def test_a_start_keeps_the_nip05_and_everything_else_it_does_not_set(bot, monkeypatch):
    mod, relay = bot
    from app.services.nostr import event as E
    relay["profile"] = E.build_event(mod._SECKEY, 0, json.dumps({"name": "PosterChan AI", "nip05": "posterchan@poster.place",
                                                                "about": "the node's AI", "lud16": "tips@poster.place"}), tags=[])
    monkeypatch.setenv("NOSTR_PROFILE_NAME", "PosterChan")
    monkeypatch.delenv("NOSTR_PROFILE_NIP05", raising=False)
    monkeypatch.delenv("NOSTR_PROFILE_PICTURE", raising=False)
    mod.ensure_profile()
    got = _profile(relay)
    assert got["nip05"] == "posterchan@poster.place", "a restart erased the NIP-05"
    assert got["about"] == "the node's AI" and got["lud16"] == "tips@poster.place"
    assert got["name"] == "PosterChan", "the configured name must still apply"


def test_an_unreadable_profile_is_not_published_over(bot, monkeypatch):
    mod, relay = bot
    relay["readable"] = False
    monkeypatch.setenv("NOSTR_PROFILE_NAME", "PosterChan")
    mod.ensure_profile()
    assert relay["published"] == [], "published a profile it could not read — the replaceable-doc wipe"


def test_an_unchanged_profile_is_not_republished(bot, monkeypatch):
    mod, relay = bot
    monkeypatch.setenv("NOSTR_PROFILE_NAME", "chesstr")
    mod.ensure_profile()
    assert len(relay["published"]) == 1
    mod.ensure_profile()
    assert len(relay["published"]) == 1, "every restart re-signed an identical profile"

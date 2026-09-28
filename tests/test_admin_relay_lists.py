"""Admin → Relay: every list setting is a list, and every edit persists the way Save does.

Run: venv-unified/bin/python -m pytest tests/test_admin_relay_lists.py

The page half (layout at phone and desktop width, Enter adds without submitting the form, the text
box and Save's baseline follow the server) is scripts/check_admin_relay_lists.py. This file pins the
server half: entries are split exactly the way the relay reads them, an edit applies to the value the
server holds NOW (never to the page's stale copy), it goes through update_settings so the same live
reload as a Save fires, it is written through to the relay, and it refuses rather than guess.
"""
import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import nip05_registry, relay_blocklist, relay_lists, settings_store
from app.services.nostr import bip340, nostr_service

ROOT = Path(__file__).resolve().parents[1]
PKA = bip340.pubkey_from_seckey(b"\x07" * 32).hex()
PKB = bip340.pubkey_from_seckey(b"\x08" * 32).hex()


@pytest.fixture
def store(monkeypatch):
    vals = {
        "nostr_relay_blocked_words": "spam\nbuy now",
        "nostr_relay_upstream_relays": "wss://a.example, wss://b.example",
        "nostr_relay_wot_seeds": nostr_service.npub_of(PKA),
        "nostr_dvm_peers": f"{nostr_service.npub_of(PKA)} wss://peer.example",
    }
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    written = []

    async def write_through(db, changes):
        written.append(dict(changes))
        return len(changes)
    monkeypatch.setattr(settings_store, "write_through", write_through)
    import app.services.nostr_relay.thread as thread
    reloads = []
    for name in ("trigger_block_reload", "trigger_upstream_reload", "trigger_nip05_reload"):
        monkeypatch.setattr(thread, name, lambda n=name: reloads.append(n) or {})
    return vals, written, reloads


# ---- how a value becomes entries -----------------------------------------------------------------

def test_entries_are_split_the_way_the_relay_reads_them():
    assert relay_lists.entries("relay", "wss://a, wss://b\n wss://c  wss://A") == ["wss://a", "wss://b", "wss://c"]
    assert relay_lists.entries("word", "buy now\n\nfree, crypto\n") == ["buy now", "free, crypto"], \
        "a blocked PHRASE is a whole line -- splitting it on spaces/commas would block each word"
    assert relay_lists.entries("peer", "npub1x wss://p1, npub1y   wss://p2") == ["npub1x wss://p1", "npub1y wss://p2"]


@pytest.mark.parametrize("kind,good,bad", [
    ("relay", "wss://relay.example", "https://relay.example"),
    ("origin", "https://poster.place/", "poster.place"),
    ("domain", "https://Mostr.pub/x", "not a domain"),
    ("pubkey", "__NPUB__", "npub1nonsense"),
    ("peer", "__NPUB__ wss://p.example", "__NPUB__"),
    ("word", "buy now", ""),
])
def test_a_new_entry_is_validated_for_its_list(kind, good, bad):
    npub = nostr_service.npub_of(PKA)
    ok, err = relay_lists.validate(kind, good.replace("__NPUB__", npub))
    assert ok and err is None, err
    assert relay_lists.validate(kind, bad.replace("__NPUB__", npub))[0] is None
    if kind == "origin":
        assert ok == "https://poster.place", "a trailing slash is not part of an origin"
    if kind == "domain":
        assert ok == "mostr.pub"


def test_the_page_and_the_server_list_the_same_settings():
    out = subprocess.run(["node", "-e", "process.stdout.write(JSON.stringify(require(process.argv[1]).LISTS))",
                          str(ROOT / "static/js/admin-relay-lists.js")], capture_output=True, text=True, timeout=20)
    js = json.loads(out.stdout)
    assert {k: v[0] for k, v in js.items()} == relay_lists.LISTS
    tab = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    for k in relay_lists.LISTS:
        assert re.search(rf'<textarea id="{k}" name="{k}"', tab), f"{k}: the text box Save sends must stay"
    assert "admin-relay-lists.js" in (ROOT / "templates/admin.html").read_text()


# ---- one edit --------------------------------------------------------------------------------------

def _edit(**kw):
    from app.routers import admin
    return admin.relay_list_edit(admin.RelayListEditReq(**kw), db=None, admin=None)


def test_add_edits_the_servers_current_value_applies_it_live_and_persists(store):
    vals, written, reloads = store
    vals["nostr_relay_blocked_words"] += "\nadded elsewhere"      # changed after the page loaded
    r = _edit(key="nostr_relay_blocked_words", add="free crypto")
    assert r["value"] == "spam\nbuy now\nadded elsewhere\nfree crypto", "an entry added elsewhere was dropped"
    assert vals["nostr_relay_blocked_words"] == r["value"]
    assert "trigger_block_reload" in reloads, "the running relay was not told (a Save would have told it)"
    assert {"nostr_relay_blocked_words": r["value"]} in written and r["durable"], "not written through to the relay"


def test_remove_takes_one_entry_and_the_upstream_reconnects(store):
    vals, _written, reloads = store
    r = _edit(key="nostr_relay_upstream_relays", remove="WSS://B.example")
    assert r["value"] == "wss://a.example"
    assert "trigger_upstream_reload" in reloads


def test_removing_the_last_entry_clears_the_list(store):
    vals, _w, _r = store
    npub = nostr_service.npub_of(PKA)
    assert _edit(key="nostr_relay_wot_seeds", remove=npub)["value"] == ""
    assert vals["nostr_relay_wot_seeds"] == "", "a text list must be clearable"


@pytest.mark.parametrize("kw,code", [
    ({"key": "nostr_relay_blocked_words", "add": "spam"}, 400),                 # already there
    ({"key": "nostr_relay_upstream_relays", "add": "https://x"}, 400),         # not a relay url
    ({"key": "nostr_relay_blocked_words", "remove": "never listed"}, 400),
    ({"key": "llm_api_key", "add": "x"}, 404),                                 # not a list at all
])
def test_a_bad_edit_is_refused_and_changes_nothing(store, kw, code):
    vals, written, _ = store
    before = dict(vals)
    with pytest.raises(HTTPException) as e:
        _edit(**kw)
    assert e.value.status_code == code and vals == before and not written


def test_an_edit_waits_for_settings_to_load(store, monkeypatch):
    """Unloaded, get() is "": one added entry would be written over the whole list."""
    vals, written, _ = store
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: False)
    with pytest.raises(HTTPException) as e:
        _edit(key="nostr_relay_blocked_words", add="x")
    assert e.value.status_code == 503 and vals["nostr_relay_blocked_words"] == "spam\nbuy now" and not written


def test_key_lists_show_whose_key_it_is(store, monkeypatch):
    async def profiles(pks):
        return {PKA: {"name": "Alice", "picture": "https://x/a.png", "nip05": "alice@poster.place"}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    out = asyncio.run(relay_lists.rows("nostr_dvm_peers", settings_store.get("nostr_dvm_peers")))
    row = out["items"][0]
    assert row["name"] == "Alice" and row["pubkey"] == PKA and row["relay"] == "wss://peer.example" and row["valid"]


# ---- Identities: remove every "not in profile" --------------------------------------------------------

@pytest.fixture
def registry(monkeypatch, store):
    vals = store[0]
    vals[nip05_registry.KEY] = f"# comment\nalice {PKA}\nghost {PKB}\nliar {PKB}"
    state = {"complete": True, "alice_ok": True}

    async def profiles(pks):
        return ({PKA: {"name": "A", "nip05": "alice@poster.place" if state["alice_ok"] else ""},
                 PKB: {"name": "B", "nip05": "someone@else"}}, state["complete"])
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    return vals, state


def test_bulk_remove_takes_only_the_unverified_it_was_shown(registry):
    vals, _ = registry
    r = asyncio.run(nip05_registry.remove_unverified("poster.place", ["ghost", "liar", "alice"]))
    assert r["ok"] and sorted(r["names"]) == ["ghost", "liar"], "a verified identity must never go"
    assert vals[nip05_registry.KEY] == f"# comment\nalice {PKA}"


def test_bulk_remove_leaves_a_name_that_was_not_on_screen(registry):
    vals, state = registry
    state["alice_ok"] = False                                    # alice broke her profile AFTER the page loaded
    r = asyncio.run(nip05_registry.remove_unverified("poster.place", ["ghost"]))
    assert r["names"] == ["ghost"] and "alice" in vals[nip05_registry.KEY] and "liar" in vals[nip05_registry.KEY]


def test_bulk_remove_refuses_when_a_profile_could_not_be_read(registry):
    """An unread profile looks exactly like one that does not publish the address."""
    vals, state = registry
    state["complete"] = False
    before = vals[nip05_registry.KEY]
    r = asyncio.run(nip05_registry.remove_unverified("poster.place", ["ghost", "liar"]))
    assert not r["ok"] and r["retry"] and vals[nip05_registry.KEY] == before


def test_bulk_remove_endpoint_persists_and_reports(registry, monkeypatch):
    from app.routers import admin, client
    monkeypatch.setattr(client, "_nip05_domain", lambda request, db: "poster.place")
    r = asyncio.run(admin.relay_identities_remove_unverified(
        admin.RelayIdentitiesPruneReq(names=["ghost", "liar"]), request=None, db=None, admin=None))
    assert r["removed"] == 2 and r["durable"]
    registry[1]["complete"] = False
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin.relay_identities_remove_unverified(
            admin.RelayIdentitiesPruneReq(names=["alice"]), request=None, db=None, admin=None))
    assert e.value.status_code == 409

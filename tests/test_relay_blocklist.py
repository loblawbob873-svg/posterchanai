"""An account blocked on the relay can be FOUND and UNBLOCKED -- from Admin → Relay and from the ⋯ menu.

Reported 2026-09-24: the NSFW account had been blocked (a stray "🚫 Block author", most likely) and the
admin could not undo it: the list was a textarea of 461 bare npubs, no names, and the browser's Ctrl+F
does not search inside a text box -- "I can't unblock something I can't see!". These run the shipped
service and routes, with real signed admin proofs.
"""
import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.services import relay_blocklist, settings_store
from app.services.nostr import bip340, nostr_service
from app.services.nostr import event as nostr_event

ROOT = Path(__file__).resolve().parents[1]
ADMIN_SK = (0xA11CE).to_bytes(32, "big")
ADMIN_PK = bip340.pubkey_from_seckey(ADMIN_SK).hex()
NSFW = "2495d53db24bd7e229cfafba318f0f282c2646dabb610f5b070b53ee6d86d9ec"
OTHER = bip340.pubkey_from_seckey((0xBEEF).to_bytes(32, "big")).hex()


@pytest.fixture
def store(monkeypatch):
    vals = {relay_blocklist.KEY: "\n".join([nostr_service.npub_of(OTHER), nostr_service.npub_of(NSFW)])}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    reloads = []
    import app.services.nostr_relay.thread as thread
    monkeypatch.setattr(thread, "trigger_block_reload", lambda: reloads.append(1) or {})
    import app.services.blossom_service as bs
    monkeypatch.setattr(bs, "_operator_pubkeys", lambda db: {ADMIN_PK})
    return vals, reloads


def test_unblocking_takes_exactly_that_key_off_and_applies_it_live(store):
    vals, reloads = store
    assert relay_blocklist.blocked_hex() == [OTHER, NSFW]
    assert relay_blocklist.set_blocked(None, NSFW, False) == {"ok": True, "blocked": False, "count": 1}
    assert relay_blocklist.blocked_hex() == [OTHER]
    assert vals[relay_blocklist.KEY] == nostr_service.npub_of(OTHER)       # stored as npubs, readable
    assert reloads, "the running relay was never told"


def test_the_nodes_own_key_is_never_blocked(store):
    assert relay_blocklist.set_blocked(None, ADMIN_PK, True)["ok"] is False
    assert ADMIN_PK not in relay_blocklist.blocked_hex()


class _DB:
    """Just enough of a Session for the admin-proof check: the signer is an admin."""
    def query(self, _m):
        return self

    def filter(self, *a):
        return self

    def first(self):
        return object()


def _proof(content, tags):
    ev = nostr_event.build_event(ADMIN_SK, 27235, content, tags=tags)
    return base64.b64encode(json.dumps(ev).encode()).decode()


def test_a_block_proof_cannot_be_replayed_as_an_unblock(store):
    from app.routers import client
    block_proof = _proof("block", [["action", "block"], ["p", NSFW]])
    r = asyncio.run(client.block_pubkey(client.BlockReq(target=NSFW, remove=True, auth=block_proof), _DB()))
    assert r.status_code == 403 and NSFW in relay_blocklist.blocked_hex()
    unblock_proof = _proof("unblock", [["action", "unblock"], ["p", NSFW]])
    r = asyncio.run(client.block_pubkey(client.BlockReq(target=NSFW, remove=True, auth=unblock_proof), _DB()))
    assert r.status_code == 200 and NSFW not in relay_blocklist.blocked_hex()


def test_the_menu_can_ask_who_is_blocked_only_with_an_admin_proof(store):
    from app.routers import client
    r = asyncio.run(client.blocked_list(client.BlockedListReq(auth=_proof("blocked-list", [])), _DB()))
    assert json.loads(r.body) == {"ok": True, "pubkeys": [OTHER, NSFW]}
    r = asyncio.run(client.blocked_list(client.BlockedListReq(auth=_proof("block", [])), _DB()))
    assert r.status_code == 403, "a proof signed for another action listed the blocklist"


def test_the_admin_list_shows_names_and_keeps_an_account_with_no_profile(store, monkeypatch):
    """The whole point: the entry reads "NSFW", and one with no profile on this relay is still listed."""
    from app.routers import admin

    async def profiles(pks):
        return {NSFW: {"name": "NSFW", "picture": "", "nip05": ""}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    out = asyncio.run(admin.relay_blocked(db=None, admin=None))
    assert [(a["pubkey"], a["name"]) for a in out["accounts"]] == [(OTHER, ""), (NSFW, "NSFW")]
    assert out["accounts"][1]["npub"].startswith("npub1yj2a20djf")
    admin.relay_unblock(admin.RelayUnblockReq(target=nostr_service.npub_of(NSFW)), db=None, admin=None)
    assert relay_blocklist.blocked_hex() == [OTHER]


def test_the_admin_list_finds_the_account_by_what_a_person_would_type():
    js = r"""
const { matches } = require(process.argv[1]);
const row = { name: 'NSFW', nip05: 'nsfw@example.com', npub: 'npub1yj2a20djf0t7', pubkey: '2495d53db24b' };
const out = ['nsfw', 'NsFw', 'example.com', 'npub1yj2a', '2495d5', '', 'pagan'].map(q => matches(row, q));
process.stdout.write(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", js, str(ROOT / "static/js/admin-blocklist.js")],
                       capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(r.stdout) == [True, True, True, True, True, True, False]


def test_the_page_and_the_menu_are_wired():
    tab = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    assert 'id="blk_search"' in tab and 'id="blk_list"' in tab
    # the text box Save sends is still there, with the same id and name
    assert 'id="nostr_relay_blocked_pubkeys" name="nostr_relay_blocked_pubkeys"' in tab
    assert "admin-blocklist.js" in (ROOT / "templates/admin.html").read_text()
    menus = (ROOT / "static/js/client/menus.js").read_text()
    assert "isRelayBlocked(pk) ? ['unblock'" in menus and "doUnblock(pk)" in menus

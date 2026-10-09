"""A relay list that only NAMES a bridge relay does not make its author a bridge account.

2026-10-09: Vyram (a Concord user) could not save settings -- his relay list (kind 10002) names
relay.ditto.pub and relay.momostr.pink, both on this node's bridge block list, and the relay answered
"blocked: bridged relay not accepted" AND marked his account bridged. Listing a relay to read from it is
not being hosted on it. The real bridge signals still work: a nip05 on the bridge domain, a proxy tag.
"""
import json

from app.services.nostr_relay.bridges import author_on_blocked_bridge, reveals_blocked_bridge

BLOCKED = {"ditto.pub", "momostr.pink", "mostr.pub"}


def test_a_relay_list_naming_bridge_relays_is_accepted():
    for kind in (10002, 3):
        ev = {"kind": kind, "pubkey": "b" * 64, "content": "",
              "tags": [["r", "wss://relay.ditto.pub/"], ["r", "wss://relay.momostr.pink/"], ["r", "wss://nos.lol/"]]}
        assert reveals_blocked_bridge(ev, BLOCKED) is False, kind


def test_the_real_bridge_signals_still_identify_a_bridge_account():
    profile = {"kind": 0, "pubkey": "c" * 64, "tags": [], "content": json.dumps({"nip05": "someone@mostr.pub"})}
    assert reveals_blocked_bridge(profile, BLOCKED) is True
    assert author_on_blocked_bridge(profile, BLOCKED) is True
    mirrored = {"kind": 1, "pubkey": "d" * 64, "content": "hi",
                "tags": [["proxy", "https://momostr.pink/notes/abc", "activitypub"]]}
    assert reveals_blocked_bridge(mirrored, BLOCKED) is True
    ordinary = {"kind": 0, "pubkey": "e" * 64, "tags": [], "content": json.dumps({"nip05": "vyram@example.com"})}
    assert reveals_blocked_bridge(ordinary, BLOCKED) is False

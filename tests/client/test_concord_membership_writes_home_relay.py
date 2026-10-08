"""A Concord membership list is written to THIS instance's relay, which is where every device reads it.

"concord on desktop i am in a developer room but phone dont show room" (2026-10-08). Measured: the account's
newest membership vault (33302, 10-08 08:43) was on nos.lol, primal and jskitty -- and poster.place's relay
held the 09-10 one. Writes went to `relayUrls()` (the PUBLIC write set, never the home relay) + the CORD relays,
while each device reads its own relay on every pass and the outside relays once per session. So the device that
joined a room wrote a list the other devices never read. Runs the shipped `membershipWriteRelays`.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _fn(name):
    i = CONCORD.index(f"  function {name}(")
    return CONCORD[i:CONCORD.index("\n  }\n", i) + 4]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_home_relay_is_written_first_and_nothing_is_lost():
    js = _fn("membershipWriteRelays") + r"""
    const p = {homeRelay: () => 'wss://relay.poster.place', relayUrls: () => ['wss://nos.lol', 'wss://relay.poster.place']};
    const none = {relayUrls: () => ['wss://nos.lol']};
    console.log(JSON.stringify({with: membershipWriteRelays(p, ['wss://jskitty.com/nostr']),
                                standalone: membershipWriteRelays(none, ['wss://jskitty.com/nostr'])}));"""
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["with"] == ["wss://relay.poster.place", "wss://nos.lol", "wss://jskitty.com/nostr"]
    assert got["standalone"] == ["wss://nos.lol", "wss://jskitty.com/nostr"], "no instance: the public relays still get it"


def test_every_membership_write_goes_through_it_and_the_client_exposes_the_home_relay():
    # Both membership writers -- the Armada vault (33302/13302) and the kind-10009 group list -- go through it,
    # and the old public-only spelling is gone from the file.
    assert CONCORD.count("relayPublishTo(membershipWriteRelays(p,CORD_RELAYS),ev)") == 2
    assert "relayPublishTo([...new Set([...(p.relayUrls?.()||[]),...CORD_RELAYS])],ev)" not in CONCORD
    k10009 = CONCORD[CONCORD.index("kind:10009,created_at:now"): CONCORD.index("kind:10009,created_at:now") + 200]
    assert "membershipWriteRelays(p,CORD_RELAYS)" in k10009, "the group-list write skips the home relay"
    assert "homeRelay: () => (CFG && CFG.relay_url)" in APP

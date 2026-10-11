"""A COMMUNITY MUST NOT SAY "8 RELAYS ASKED" ABOUT RELAYS IT NEVER ASKED.

Operator's report from the app: the PosterChan room showed only "could not refresh room history: no
channels readable yet — 8 relays asked, N control event(s) held, M new", while the PosterChan bot, a
member, read the same community fine on the server. Measured on the operator's desktop, the stored
join bundle is identical to what its invite opens to, and its relays serve a control stream that
decrypts into channels — the membership was intact; the read was never sent.

`Relay.queryFrom` silently skips a relay while another read of the same slot is in flight there and
for thirty minutes after it failed once, and a member's split-control plane (every community this
client mints) has no AUTH key, so its slot is the bare relay URL that every other community on those
relays shares. Both shapes are reproduced in concord_unasked_relays_runtime.mjs with the shipped
concord.js + relay.js and a really minted community; on the pre-fix code both end in exactly the
reported sentence. The third case — relays that answer with control events this membership cannot
open — is a different problem (a stale or rotated membership) and must say so.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run():
    result = subprocess.run(['node', str(ROOT / 'tests/client/concord_unasked_relays_runtime.mjs')],
                            capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    line = [l for l in result.stdout.splitlines() if l.startswith('{')][-1]
    return json.loads(line)


def test_unasked_relays_are_asked_and_each_failure_says_what_to_do():
    got = run()
    assert got['baseline']['ok'] and got['baseline']['hydrated'], ('the fixture room must be readable', got)

    busy = got['busy']
    assert busy['ok'] and busy['hydrated'], (
        "another community's read in flight on the same relays made this room ask nobody", busy)

    cooled = got['cooled']
    assert cooled['ok'] and cooled['hydrated'], (
        'one failed moment left every relay resting, and the room asked nobody once the radio was back', cooled)
    assert 'relay(s) asked' not in cooled['first'], ('a room whose relays all failed claimed they were asked', cooled)
    assert 'check your connection' in cooled['first'].lower(), ('the network failure must say what to do', cooled)

    rotated = got['rotated']
    assert not rotated['ok'], rotated
    assert 'answered' in rotated['error'] and 'fresh invite' in rotated['error'], (
        'relays that answered with nothing this membership opens is a membership problem, and must say so', rotated)

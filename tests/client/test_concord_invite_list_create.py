"""CREATING A COMMUNITY MUST NOT FAIL BECAUSE A RELAY WAS NEVER ASKED.

Operator's user: "community creation failed: Invite List sync is incomplete; retry before changing
it". Creation saves the new link's signing key into the creator's encrypted Invite List (kind
13303, replaceable), and that write rightly refuses to go ahead after a partial read. But the read
went through `Relay.queryFrom`, which silently SKIPS a relay while another read of the same slot is
in flight there, and for thirty minutes after one failure -- so a busy or resting relay counted as
"did not answer" without being asked, and the whole community was discarded.

concord_invite_list_create_runtime.mjs drives the shipped concord.js mintPublicRoom through the
shipped relay.js and cord-invite-links.js with really minted communities and fake sockets. On the
pre-fix code every one of busy / cooled / failing ends in exactly the reported sentence with
nothing published.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run():
    result = subprocess.run(['node', str(ROOT / 'tests/client/concord_invite_list_create_runtime.mjs')],
                            capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    line = [l for l in result.stdout.splitlines() if l.startswith('{')][-1]
    return json.loads(line)


def test_creation_asks_every_relay_and_never_discards_a_community_over_its_invite_list():
    got = run()
    base = got['baseline']
    assert base['ok'] and base['created'] and base['inviteListSaved'] and base['communityPublished'], base
    assert not base['pending'], base

    for name in ('busy', 'cooled'):
        case = got[name]
        assert case['ok'] and case['created'], (
            f'{name}: a relay that was skipped, not asked, made creation fail', case)
        assert case['inviteListSaved'] and not case['pending'], (
            f'{name}: every relay can answer, so the Invite List must actually be saved', case)

    failing = got['failing']
    # A relay that was ASKED and did not answer still makes the list read incomplete -- the list is
    # not written over -- but the community itself is kept and published, with the link key on it.
    assert failing['ok'] and failing['created'] and failing['communityPublished'], (
        'the community was thrown away because only its Invite List could not be saved', failing)
    assert not failing['inviteListSaved'], ('a partial read must never be written over', failing)
    assert failing['secrets'], ('the link signing key must stay with the room so it can be saved later', failing)
    pending = failing['pending']
    assert 'wss://nostr.computingcache.com' in pending, ('name the relay that did not answer', pending)
    assert 'retry' in pending.lower() and 'invite' in pending.lower(), ('say how to finish it', pending)
    assert failing.get('recovered'), ('the retry the message names must actually save the link', failing)

    refresh = got['refresh']
    assert refresh['ok'] and refresh['refreshed'] == 1, (
        "refreshing an existing community's links read 'incomplete' while another room's read was in flight",
        refresh)

"""CALLS DO NOT RING UNTIL THE PERSON TURNS RINGING ON.

"User is getting call spam from people he knows" (2026-10-31) -> "do not disturb should be on by default until they
turn it off for calls". The shipped client in two headless Chromes over a real relay (test_calls_end_to_end_full_app's
rig). Bob has never touched the switch: Alice's call does not ring him, does not open his call screen and is not
answered -- it is listed under Missed. He turns "Ring for incoming calls" on from the Calls screen, and Alice's next
call rings him.

Fail-first: on the code before this change Bob's phone rings on the first call.
"""
from pathlib import Path

import pytest

from tests.client import test_calls_end_to_end_full_app as calls

pytestmark = pytest.mark.skipif(not Path(calls.CHROME).exists(), reason='Chrome required')

BOB_NEVER_TURNED_RINGING_ON = (2, 'bob', False)
RINGING = "!!document.getElementById('call-accept')"


@calls.scenario(calls.ALICE, BOB_NEVER_TURNED_RINGING_ON)
async def _a_call_does_not_ring_until_ringing_is_on(relay, alice, bob):
    # First call: Bob is never rung, and his device sends Alice nothing at all (not even a busy/decline).
    await alice.call(bob)
    await bob.until(f"(()=>{{const l=JSON.parse(localStorage.getItem('pc_missed_calls:'+__PC.me().pubkey)||'[]');"
                    f"return l.some(x=>x.pk==={calls.json.dumps(alice.pubkey)});}})()",
                    what='the screened call to be listed as missed')
    await calls.asyncio.sleep(1.5)        # the window in which a ring would have appeared
    assert not await bob.js(RINGING), 'a call rang a person who never turned ringing on'
    assert not await bob.js("!!document.querySelector('#call-overlay')")
    assert not [e for e in relay.frames() if e['pubkey'] == bob.pubkey], 'the screened callee answered the caller'
    assert await bob.missed() == [alice.pubkey], 'one call is one Missed row, however often the invite arrives'
    await alice.press('call-hang')

    # The Calls screen says why, and offers the switch.
    await bob.js("__PC.switchView('calls')")
    await bob.until("!!document.querySelector('#call-ring-switch')", what='the ringing switch on the Calls screen')
    assert await bob.js("document.querySelector('#call-ring-switch').checked") is False
    assert 'missed' in (await bob.js("document.querySelector('#call-ringing').textContent")).lower()
    assert await bob.js("document.querySelector('.call-missed .call-contact').dataset.pk") == alice.pubkey

    # He turns it on; the next call rings.
    await bob.js("document.querySelector('#call-ring-switch').click()")
    await bob.until("document.querySelector('#call-ring-switch') && document.querySelector('#call-ring-switch').checked"
                    " && !document.querySelector('#call-ring-switch').disabled", what='the switch to stay on')
    await alice.until("!document.querySelector('#call-overlay')", what='her first call to end')
    await alice.call(bob)
    await bob.until(RINGING, what='the call to ring once ringing is on')
    await bob.press('call-decline')
    await alice.until("!document.querySelector('#call-overlay')", what='her call to end')


def test_a_call_does_not_ring_until_ringing_is_on():
    _a_call_does_not_ring_until_ringing_is_on()

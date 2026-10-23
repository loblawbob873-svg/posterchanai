"""1:1 calls, the flows test_calls_end_to_end_full_app.py does not reach: the CALLEE ending a call, a callee who
is OFFLINE, a caller who VANISHES while the other phone rings, and a relay that never answers.

Same rig as that file (its `scenario` is reused): one headless Chrome per device running the SHIPPED client,
Chrome's fake camera/mic, a real NIP-01 relay between them, real WebRTC. The only extra fixture is the clock for
the two long ring timers in calls.js (the caller gives up at 45 s, a ringing callee at 60 s): they are shortened
to a couple of seconds, and ONLY for timers armed from calls.js (read off the stack), so the client's own logic
decides when a call is over and every other timer in the app keeps its real length.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/calls.js=<broken copy>" serves a different calls.js to every
device (same convention as tests/client/realnode_harness.py).
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.client import test_calls_end_to_end_full_app as calls
from tests.client.test_effects_full_app import Handler

pytestmark = pytest.mark.skipif(not Path(calls.CHROME).exists(), reason='Chrome required')

# The two ring timers in calls.js, shortened. Anything else (and any 45 s/60 s timer elsewhere) is untouched.
FAST_RING = r'''
(()=>{const st=window.setTimeout;
  window.setTimeout=function(f,d,...a){
    if((d===45000||d===60000)&&/\/calls\.js/.test(new Error().stack||''))d=d===45000?2500:3500;
    return st.call(this,f,d,...a);};})();
'''


class OverrideHandler(Handler):
    """The instance's static files, with PC_JOURNEY_OVERRIDE="/static/js/client/x.js=/tmp/x.js" swapping one."""
    def do_GET(self):
        for pair in filter(None, os.environ.get('PC_JOURNEY_OVERRIDE', '').split(',')):
            path, _, src = pair.partition('=')
            if self.path.split('?')[0] == path:
                data = Path(src).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        super().do_GET()


class SilentRelay(calls.Relay):
    """Accepts the socket and every frame, and never answers anything: no OK, no EOSE, no EVENT."""
    async def handler(self, ws):
        self.subs[ws] = {}
        try:
            async for raw in ws:
                try: m = json.loads(raw)
                except ValueError: continue
                if m[0] == 'REQ':
                    self.subs[ws][m[1]] = m[2:]
                elif m[0] == 'EVENT':
                    self.log.append(m[1])
        finally:
            self.subs.pop(ws, None)


def flow(*people, relay_cls=None, real_clock=False):
    return calls.scenario(*people, extra='' if real_clock else FAST_RING, relay_cls=relay_cls, handler=OverrideHandler)


ALICE, BOB = calls.ALICE, calls.BOB
NO_OVERLAY = "!document.querySelector('#call-overlay')"
CONNECTED = "(document.querySelector('#call-status')||{}).textContent==='connected'"


async def _toasts(p):
    return await p.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')")


async def _watch_toasts(p):
    # Toasts disappear after a few seconds; keep every one this device shows.
    await p.js("window.__toasts=[];new MutationObserver(()=>document.querySelectorAll('.toast').forEach(t=>{"
               "if(!t.__seen){t.__seen=1;__toasts.push(t.textContent)}})).observe(document.body,{childList:true,subtree:true});1")


async def _released(p):
    """No live connection and no live microphone/camera track: the call is over on this device, not just hidden."""
    assert await p.live_connections() == 0, f'{p.name} still holds a connection'
    assert await p.js("__pcs.every(pc=>pc.getSenders().every(s=>!s.track||s.track.readyState==='ended'))"), \
        f'{p.name} still holds a live microphone/camera track'


# ---------------------------------------------------------------------------------------------------
# The callee hangs up
# ---------------------------------------------------------------------------------------------------
@flow(ALICE, BOB)
async def _the_callee_hanging_up_ends_it_for_the_caller(relay, alice, bob):
    await alice.call(bob)
    await bob.press('call-accept')
    for p in (alice, bob):
        await p.until(CONNECTED, what='"connected"')
    await alice.receives('audio', "Bob's voice")
    # Bob, who did not start the call, ends it.
    await bob.press('call-hang')
    await bob.until(NO_OVERLAY, what='his call screen to close')
    await alice.until(NO_OVERLAY, timeout=6, what='HER call screen to close when he hung up')
    for p in (alice, bob):
        await _released(p)
        # A call that was answered is nobody's missed call, on either side.
        assert await p.missed() == [], f'{p.name} logged an answered call as missed'
    # Both are back on a usable Calls screen: the dial box is there and nothing is left in a call state.
    for p in (alice, bob):
        await p.js("__PC.switchView('calls')")
        await p.until("!!document.querySelector('#call-npub') && !document.querySelector('.call-missed')",
                      what='the Calls screen, with no Missed list')
    # And a second call between the same two works — the first one left nothing behind.
    await alice.call(bob)
    await bob.press('call-accept')
    await alice.until(CONNECTED, what='"connected" on the second call')
    await bob.receives('audio', 'her voice on the second call')


def test_the_callee_hanging_up_ends_it_for_the_caller():
    _the_callee_hanging_up_ends_it_for_the_caller()


# ---------------------------------------------------------------------------------------------------
# A callee who is offline, and a caller who vanishes
# ---------------------------------------------------------------------------------------------------
@flow(ALICE)
async def _calling_someone_who_is_offline_says_no_answer(relay, alice):
    bob_pk = await alice.js("NostrTools.getPublicKey(new Uint8Array(32).fill(2))")
    bob_npub = await alice.js(f"NostrTools.nip19.npubEncode({json.dumps(bob_pk)})")
    await _watch_toasts(alice)
    await alice.js("__PC.switchView('calls')")
    await alice.until("!!document.querySelector('#call-npub')", what='the Calls screen')
    await alice.js(f"document.querySelector('#call-npub').value={json.dumps(bob_npub)};document.querySelector('#call-start-btn').click()")
    await alice.until("!!document.querySelector('#call-overlay')", what='her call screen')
    # The invite went out (to nobody listening): the relay saw exactly one, addressed to Bob.
    for _ in range(100):
        if any(['t', 'invite'] in e['tags'] for e in relay.frames()): break
        await asyncio.sleep(.1)
    invites = [e for e in relay.frames() if ['t', 'invite'] in e['tags']]
    assert len(invites) == 1 and ['p', bob_pk] in invites[0]['tags'], invites
    # While it waits, the screen says it is calling — never "connected".
    ov = await alice.overlay()
    assert ov and ov['status'] in ('calling…', 'ringing…'), ov
    # Nobody answers: the call gives up by itself, SAYS so, and lets go of the microphone.
    await alice.until(NO_OVERLAY, timeout=15, what='the unanswered call to give up')
    assert 'no answer' in await alice.js("__toasts.join(' | ')"), await alice.js("__toasts")
    await _released(alice)
    assert await alice.missed() == [], 'the CALLER did not miss a call'
    # Bob comes online after she gave up: his phone must not ring for a call that is already over.
    bob = await relay.open(*BOB)
    for _ in range(100):
        if any(bob.pubkey in f.get('#p', []) and 25050 in f.get('kinds', [])
               for subs in relay.subs.values() for fs in subs.values() for f in fs): break
        await asyncio.sleep(.1)
    await asyncio.sleep(1.5)      # the window in which a replayed invite would ring (nothing to wait FOR here)
    assert not await bob.js("!!document.querySelector('#call-overlay')"), 'a call that was over rang anyway'
    assert not await bob.js('__errors'), await bob.js('__errors')


def test_calling_someone_who_is_offline_says_no_answer():
    _calling_someone_who_is_offline_says_no_answer()


@flow(ALICE, BOB)
async def _a_caller_who_vanishes_mid_ring_is_logged_as_missed(relay, alice, bob):
    await alice.call(bob)
    await bob.until("!!document.getElementById('call-accept')", what='the incoming call')
    # Alice's device dies (tab killed, phone off): nothing more leaves it — in particular no `bye`.
    await alice.js("WebSocket.prototype.send=function(){};1")
    await alice.b.call('Page.navigate', {'url': 'about:blank'})
    # Bob's phone must not ring for ever: it stops on its own and tells him who he missed.
    await bob.until(NO_OVERLAY, timeout=15, what='his phone to stop ringing for a caller who is gone')
    assert await bob.missed() == [alice.pubkey]
    await bob.js("__PC.switchView('calls')")
    await bob.until("!!document.querySelector('.call-missed .call-contact[data-pk]')", what='the Missed list')
    assert await bob.js("document.querySelector('.call-missed .call-contact').dataset.pk") == alice.pubkey
    await _released(bob)
    # The scenario's own end-of-test check reads Alice's page, which is now about:blank.
    await alice.b.call('Runtime.evaluate', {'expression': 'window.__errors=[]'})


def test_a_caller_who_vanishes_mid_ring_is_logged_as_missed():
    _a_caller_who_vanishes_mid_ring_is_logged_as_missed()


# ---------------------------------------------------------------------------------------------------
# A relay that never answers, and one that refuses
# ---------------------------------------------------------------------------------------------------
class RefusingRelay(calls.Relay):
    """Answers every read (EOSE) and refuses every write: OK false."""
    async def handler(self, ws):
        self.subs[ws] = {}
        try:
            async for raw in ws:
                try: m = json.loads(raw)
                except ValueError: continue
                if m[0] == 'REQ':
                    self.subs[ws][m[1]] = m[2:]
                    await ws.send(json.dumps(['EOSE', m[1]]))
                elif m[0] == 'EVENT':
                    self.log.append(m[1])
                    await ws.send(json.dumps(['OK', m[1]['id'], False, 'blocked: not accepting writes']))
        finally:
            self.subs.pop(ws, None)


async def _a_call_nobody_could_hear_is_not_reported_as_unanswered(relay, alice, bob, verdict_within):
    await _watch_toasts(alice)
    await alice.call(bob)
    await alice.until("!!document.querySelector('#call-overlay')", what='her call screen')
    await alice.until(NO_OVERLAY, timeout=verdict_within, what='the undeliverable call to end')
    shown = await alice.js("__toasts.join(' | ')")
    # She is told the call did not get out — NOT "no answer", which blames Bob for a call he never received.
    assert 'not delivered' in shown and 'no answer' not in shown, shown
    await _released(alice)
    # Nothing reached Bob, and nothing pretends it did.
    assert not await bob.js("!!document.querySelector('#call-overlay')")
    assert await bob.missed() == []
    # Her Calls screen is usable afterwards.
    await alice.js("__PC.switchView('calls')")
    await alice.until("!!document.querySelector('#call-start-btn') && !document.querySelector('#call-overlay')",
                      what='a usable Calls screen')


@flow(ALICE, BOB, relay_cls=SilentRelay)
async def _a_relay_that_never_answers(relay, alice, bob):
    # Her give-up timer (shortened to 2.5 s) fires with the invite still unconfirmed.
    await _a_call_nobody_could_hear_is_not_reported_as_unanswered(relay, alice, bob, 15)


def test_a_call_through_a_relay_that_never_answers_says_it_was_not_delivered():
    _a_relay_that_never_answers()


@flow(ALICE, BOB, relay_cls=RefusingRelay, real_clock=True)
async def _a_relay_that_refuses(relay, alice, bob):
    # A refusal is an answer: the call ends on it. The give-up timer runs at its REAL 45 s here, so ending within
    # 15 s can only be the refusal being heard.
    await _a_call_nobody_could_hear_is_not_reported_as_unanswered(relay, alice, bob, 15)
    assert any(['t', 'invite'] in e['tags'] for e in relay.log), 'the invite was never even attempted'


def test_a_call_every_relay_refuses_ends_at_once_and_says_so():
    _a_relay_that_refuses()


# ---------------------------------------------------------------------------------------------------
# Two accounts on one device: a missed call is the ACCOUNT's, not the device's
# ---------------------------------------------------------------------------------------------------
MISSED_ON_SCREEN = "[...document.querySelectorAll('.call-missed .call-contact[data-pk]')].map(e=>e.dataset.pk)"


async def _add_account(p, key_byte):
    """The account card -> "＋ Add an account" -> the login screen -> that account's nsec, as a person does."""
    await p.js("document.querySelector('#me-card').click();true")
    await p.until("!!document.querySelector('.acct-pop [data-act=add]')", what='the account menu')
    await p.js("document.querySelector('.acct-pop [data-act=add]').click();true")
    await p.until("!!window.__PC && document.body.classList.contains('guest') && !!document.querySelector('#nsec-input')",
                  timeout=60, what='the login screen for another account')
    await p.js("(()=>{const k=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
               "NostrTools.nip19.nsecEncode(k);document.querySelector('#btn-nsec-login').click();return true})()" % key_byte)
    await p.until("!!__PC.me() && __PC.me().pubkey===NostrTools.getPublicKey(new Uint8Array(32).fill(%d))" % key_byte,
                  timeout=60, what='signed in as the added account')


async def _switch_account(p, pk):
    """The account card -> the other account's row (the page reloads signed in as it)."""
    await p.js("document.querySelector('#me-card').click();true")
    row = json.dumps(".acct-pop .acct-p[data-pk=\"%s\"]" % pk)
    await p.until("!!document.querySelector(%s)" % row, what='the other account in the menu')
    await p.js("window.__pcBeforeSwitch=true;document.querySelector(%s).click();true" % row)
    await p.until("!window.__pcBeforeSwitch && !!window.__PC && !!__PC.me() && __PC.me().pubkey===%s" % json.dumps(pk),
                  timeout=60, what='signed in as the account switched to')


async def _calls_screen(p):
    await p.js("__PC.switchView('calls')")
    await p.until("!!document.querySelector('#call-npub')", what='the Calls screen')
    await asyncio.sleep(.5)


@flow(ALICE, BOB)
async def _a_missed_call_stays_with_the_account_it_rang(relay, alice, bob):
    # Alice rings Bob and gives up: Bob's device logs a missed call from her (the real producer -- calls.js).
    await alice.call(bob)
    await bob.until("!!document.getElementById('call-accept')", what='the incoming call')
    await alice.press('call-hang')
    await bob.until(NO_OVERLAY, what='his phone to stop ringing when she gave up')
    await _calls_screen(bob)
    await bob.until("%s.length===1" % MISSED_ON_SCREEN, what="Bob's Missed list")
    # Someone else signs in on Bob's device (key 4). Who called BOB is not theirs to see.
    bob_pk = bob.pubkey
    await _add_account(bob, 4)
    await _calls_screen(bob)
    assert await bob.js(MISSED_ON_SCREEN) == [], "the other account is shown Bob's missed calls"
    assert alice.pubkey not in await bob.js("document.querySelector('#feed').innerHTML"), \
        "Bob's caller is on the other account's Calls screen"
    # Bob again: his missed call is still his.
    await _switch_account(bob, bob_pk)
    await _calls_screen(bob)
    await bob.until("%s.length===1 && %s[0]===%s" % (MISSED_ON_SCREEN, MISSED_ON_SCREEN, json.dumps(alice.pubkey)),
                    what="Bob's missed call back under Bob")
    # Clear (by its id) clears Bob's list only, and it stays cleared for him.
    await bob.press('missed-clear')
    await bob.until("!document.querySelector('.call-missed')", what='the Missed list to go')
    assert await bob.missed() == []


def test_a_missed_call_stays_with_the_account_it_rang():
    _a_missed_call_stays_with_the_account_it_rang()

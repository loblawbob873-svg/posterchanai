"""AN INCOMING CALL ON A PAGE THE BROWSER WILL NOT PLAY SOUND ON MUST STILL RING -- AND MUST NOT FREEZE THE PAGE.

calls.js `_ringtone(true)` plays the PosterChan ringtone file and, when `play()` is refused (the autoplay rule: a tab or
PWA nobody has touched yet is not allowed to start audio on its own -- the ordinary state of a page that a call wakes),
was meant to fall back to the WebAudio beep. The fallback called `_ringtone(true)` again, which took the FILE branch
again (nothing had changed), was refused again, and so on: a new Audio element and a rejected play() per turn of the
event loop for as long as the phone rang. The callee's renderer sat at 130% CPU, answered nothing -- not the Accept
button, not the tab -- and the person saw a frozen app instead of a call. Found by tests/client/test_upgrade_from_old_
builds.py, whose device (a Chrome launched without --autoplay-policy=no-user-gesture-required, i.e. with the browser's
real autoplay rules) never answered again once Account C rang it.

Here: the caller is the shipped client with Chrome's fake camera/microphone; the callee is the shipped client in a
Chrome with its DEFAULT autoplay policy. It rings: the page keeps answering, the Accept button is drawn, the ringtone
file was tried at most twice, and the beep is what rings.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/calls.js=/tmp/old/calls.js" (realnode_harness serves that copy).
"""
import asyncio
import json
import time

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
CALLER, CALLEE = 221, 222
FAKE_MEDIA = ('--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
              '--autoplay-policy=no-user-gesture-required')
# Count what the page builds to make a sound: every Audio element and every AudioContext.
COUNT = r'''
(()=>{window.__audio=0;window.__ctx=0;const A=window.Audio;
  window.Audio=function(...a){window.__audio++;return new A(...a);};window.Audio.prototype=A.prototype;
  const C=window.AudioContext;if(C){window.AudioContext=function(...a){window.__ctx++;return new C(...a);};
    window.AudioContext.prototype=C.prototype;}})();
'''


async def answers(d, seconds=5):
    """The page evaluates a trivial expression within `seconds` -- it is not frozen."""
    try:
        return await asyncio.wait_for(d.b.js('1+1'), seconds) == 2
    except (asyncio.TimeoutError, TimeoutError):
        return False


@CHROME
def test_a_ringtone_the_browser_refuses_rings_the_beep_and_never_freezes_the_page():
    async def main(n):
        caller = R.Device(n, CALLER, pre_init=F.CLIENT_INIT, chrome_args=FAKE_MEDIA)
        callee = R.Device(n, CALLEE, pre_init=F.CLIENT_INIT + COUNT)      # the browser's own autoplay rules
        await caller.__aenter__()
        await callee.__aenter__()
        try:
            assert not await callee.b.js("navigator.userActivation&&navigator.userActivation.hasBeenActive"), \
                'the callee page has had a user gesture -- it would be allowed to play the ringtone'
            npub = await callee.b.js("NostrTools.nip19.npubEncode(__PC.me().pubkey)")
            # Ringing is opt-in (2026-10-31): the callee turned "Ring for incoming calls" on, as a person would.
            assert await callee.b.js("(async()=>{try{__PC.setNotificationPreference('calls',true);}catch(_){}for(let i=0;i<100;i++){if(await __PC.notificationAllowed('calls'))return true;await new Promise(r=>setTimeout(r,100));}return false;})()"), 'could not turn ringing on'
            await asyncio.sleep(2)                       # both subscribed for their own call frames
            await caller.b.js("__PC.switchView('calls');true")
            await R.wait(caller.b, "!!document.querySelector('#call-npub')", 30, 'the Calls screen')
            await caller.b.js("document.querySelector('#call-npub').value=%s;"
                              "document.querySelector('#call-start-btn').click();true" % json.dumps(npub))
            deadline = time.monotonic() + 30
            rang = False
            while time.monotonic() < deadline:
                assert await answers(callee), 'the callee\'s page stopped answering while the call rang (frozen)'
                if await callee.b.js("!!document.getElementById('call-accept')"):
                    rang = True
                    break
                await asyncio.sleep(.5)
            assert rang, 'the call never rang on the callee'
            # Ringing for a few seconds more: still a live page, and the file was not retried in a loop.
            for _ in range(8):
                await asyncio.sleep(.5)
                assert await answers(callee), 'the callee\'s page froze while ringing'
            built = await callee.b.js("({audio:window.__audio,ctx:window.__ctx})")
            assert built['audio'] <= 2, ('the refused ringtone file was retried in a loop', built)
            assert built['ctx'] >= 1, ('nothing rang: the beep fallback never started', built)
            await caller.b.js("document.getElementById('call-hang').click();true")
            await R.wait(callee.b, "!document.querySelector('#call-overlay')", 30, 'the callee stops ringing')
        finally:
            for d in (callee, caller):
                try:
                    await d.__aexit__()
                except Exception:
                    pass

    with R.Node(members=[R.pubkey(CALLER), R.pubkey(CALLEE)]) as n:
        asyncio.run(main(n))

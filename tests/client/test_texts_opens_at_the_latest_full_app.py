"""Opening a Texts conversation lands on its NEWEST message.

Reported: "did you fix text messages on android? does not scroll to latest". Texts remembers each
conversation's scroll offset so a REPAINT of the thread on screen (a receipt, a focus sync, a contact
refresh) keeps the reader where they are. It also used that offset when the conversation was OPENED
again — from the list, a notification, another thread — so a conversation you had once scrolled up in
reopened in the middle of its history, above whatever had arrived since. Driven in the real bundled
client: open, scroll up, go back to the list, reopen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""(()=>{const s=PCSms._state();s.msgs.clear();
  for(let i=1;i<=60;i++){const id='m'+i;s.msgs.set(id,{id,address:'+15550001111',body:'message number '+i+' '+'words '.repeat(i%7+3),date:1700000000000+i*60000,incoming:i%2===0});}
  PCSms.refreshNames();})()"""
AT = r"""(()=>{const l=document.querySelector('.sms-msgs');return l?{fromBottom:Math.round(l.scrollHeight-l.scrollTop-l.clientHeight),
  scrollable:l.scrollHeight>l.clientHeight+100, last:(l.textContent||'').includes('message number 60')}:null})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_conversation_reopens_on_its_newest_message():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 411, "height": 830, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js("PCOpenNotificationRoute('texts')")
        await b.until("!!document.querySelector('#sms-q')")
        await b.js(SEED)
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("!!document.querySelector('.sms-msgs')")
        await asyncio.sleep(.4)
        first = await b.js(AT)
        assert first["scrollable"] and first["fromBottom"] <= 2, ("the conversation did not open at its newest message", first)
        # Read some history, then leave.
        await b.js("(()=>{const l=document.querySelector('.sms-msgs');l.scrollTop=100;l.dispatchEvent(new Event('scroll'));})()")
        await asyncio.sleep(.2)
        # A REPAINT of the open thread keeps the reader where they are…
        await b.js("PCSms.refreshNames()")
        await asyncio.sleep(.3)
        assert await b.js("document.querySelector('.sms-msgs').scrollTop") < 400, "a repaint threw the reader to the bottom"
        await b.js("document.querySelector('#sms-back').click()")
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        # …but OPENING it again starts at the newest message.
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("!!document.querySelector('.sms-msgs')")
        await asyncio.sleep(.4)
        again = await b.js(AT)
        assert again["fromBottom"] <= 2, ("reopening the conversation landed mid-history", again)

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser("online", "", check, extra))

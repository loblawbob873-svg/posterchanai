"""A Concord message's Reply can be taken back, and Attach → From device opens the file chooser.

Real minted community, real reader, signer and DOM (tests/client/concord_fixture_relay.py):
  * Reply on a message shows "replying to" above the composer; its ✕ removes it, and the next message sent is NOT a
    reply (no `q`/`e` reference to the message);
  * Attach offers "From device" and "Files"; From device opens this device's file chooser (the composer's own input).
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def send(b, text):
    await b.js("(()=>{const i=document.querySelector('#cc-input');i.focus();i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));"
               "document.querySelector('#cc-send').click();})(); 1" % json.dumps(text))
    await b.until("document.querySelector('.cc-messages').innerText.includes(%s)" % json.dumps(text))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_reply_can_be_cancelled_and_attach_from_device_opens_the_chooser():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Reply room')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-input') && !!document.querySelector('#cc-send')")

        await send(b, 'first message')
        await b.until("!!document.querySelector('[data-cc-reply]')")
        await b.js("document.querySelector('[data-cc-reply]').click(); 1")
        await b.until("!document.querySelector('#cc-reply').classList.contains('hidden') && !!document.querySelector('#cc-reply-cancel')")
        await b.js("document.querySelector('#cc-reply-cancel').click(); 1")
        await b.until("document.querySelector('#cc-reply').classList.contains('hidden')")
        await send(b, 'not a reply')
        # The rumor is sealed; what the room shows is the honest read: no reply quote on the second message.
        last = await b.js("(()=>{const a=[...document.querySelectorAll('.cc-message')].find(m=>m.innerText.includes('not a reply'));"
                          "return a?{quote:!!a.querySelector('.cc-message-reply')}:null})()")
        assert last and not last['quote'], ('the message sent after cancelling the reply is still a reply', last)

        await b.js("(()=>{window.__chooser=0;const c=HTMLInputElement.prototype.click;HTMLInputElement.prototype.click=function(){"
                   "if(this.type==='file'){__chooser++;return;}return c.call(this);};})()")
        await b.js("document.querySelector('#cc-attach').click(); 1")
        await b.until("!!document.querySelector('#cc-attach-device') && !!document.querySelector('#cc-attach-blossom')")
        await b.js("document.querySelector('#cc-attach-device').click(); 1")
        await b.until("__chooser===1 && !document.querySelector('#cc-attach-device')")
        assert not await b.js('__errors'), await b.js('__errors')

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_message_sent_while_the_last_is_still_going_out_is_sent_after_it():
    """2026-10-22: Send pressed while the previous message was still being published returned silently -- the second
    message sat in the box, nothing went out. Now it waits its turn: both arrive, in order, and pressing Send again
    while one is waiting does not send it twice."""
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Quick room')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-input')")
        # The relay takes its time accepting, as a real one over a radio does.
        await b.js("(()=>{const pub=__PC.relayPublishRoom;__PC.relayPublishRoom=async(...a)=>{await new Promise(r=>setTimeout(r,700));return pub(...a);};})()")
        before = await b.js("__relayPublished.filter(e=>e.kind===1059).length")
        await b.js("(()=>{const i=document.querySelector('#cc-input');i.value='one';document.querySelector('#cc-send').click();})(); 1")
        await b.js("(()=>{const i=document.querySelector('#cc-input');i.value='two';const s=document.querySelector('#cc-send');s.click();s.click();})(); 1")
        await b.until("document.querySelector('.cc-messages').innerText.includes('two') && document.querySelector('#cc-input').value===''")
        await asyncio.sleep(2)
        text = await b.js("document.querySelector('.cc-messages').innerText")
        assert text.index('one') < text.index('two'), text
        assert text.count('two') == 1, ('a double tap sent the waiting message twice', text)
        assert await b.js("__relayPublished.filter(e=>e.kind===1059).length") - before == 2
        assert not await b.js('__errors'), await b.js('__errors')

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))

"""Two ways one click opened the wrong number of windows, both in the shipped client.

  1. "Click on post notification reply and it loads 2 post windows."

     A notification row carries `data-open` AND #feed carries a DELEGATED `[data-open]` handler
     (bindFeedActions), so one click ran the row's own `onclick` and the delegate — two openThread
     calls in the same tick. The desktop's "one window per app" guards cannot both catch that (the
     native dedupe reads a compositor snapshot a tick late), and on a REPLY notification they are
     not even the same post: the row embeds the parent as a real quote card, whose `.quoted` carries
     the PARENT's id. Two different ids, so every dedupe correctly declines to merge them, and
     clicking the largest part of the row opened two different Post windows.

  2. "Reply modal: clicking preview messes up the UI."

     On the desktop a reply is not an in-page modal: it is its own popup window, whose renderer
     closes the window when #modal-root empties. The preview's handler was `closeModal();
     openThread(id)` — correct on a page, and in that window it destroyed the composer (losing what
     had been typed to an autosaved draft) and then painted the thread into a #feed that client.css
     hides in a popup. Nothing opened, nothing errored.

Both are invisible to every other check here: the notification checks assert that a post window
EXISTS rather than counting them, and nothing drove `.cmp-parent` inside a popup at all.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Count every openThread, and answer pcPopup.act the way the shell does.
EXTRA = r'''
window.__opened=[];
window.__acts=[];
window.pcPopup = window.pcPopup || {};
window.pcPopup.act = async action => { __acts.push(action); return true; };
window.__events=JSON.parse(localStorage.getItem('__replyEvents')||'[]');
'''

SEED = """(()=>{
  const mine=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(2);
  const now=Math.floor(Date.now()/1000);
  const parent=NostrTools.finalizeEvent({kind:1,created_at:now-60,content:'The original post',tags:[]},mine);
  const reply=NostrTools.finalizeEvent({kind:1,created_at:now-10,content:'A reply to it',
    tags:[['e',parent.id,'','root'],['p',parent.pubkey]]},other);
  const profile=NostrTools.finalizeEvent({kind:0,created_at:now-90,
    content:JSON.stringify({name:'Fixture Author'}),tags:[]},other);
  __events=[parent,reply,profile];
  localStorage.setItem('__replyEvents',JSON.stringify(__events));
  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(mine);
  document.querySelector('#btn-nsec-login').click();
})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_notification_row_opens_exactly_one_post():
    """Click the EMBEDDED parent card inside a reply notification — the biggest part of the row, and
    the one whose id differs from the row's, which is what made this two windows rather than two
    attempts at one.

    Measured in DESKTOP MODE against PCOS.openDoc, because that is the only place the second open
    is visible: on an ordinary page both calls render the same `#feed` and the last one wins, which
    is why this survived every in-page check. Without the fix this records two ids."""
    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED)
        await b.until("!!__PC.me()")
        await b.js("PCOS.enter()")
        await b.js('new Promise(r=>setTimeout(r,800))')
        await b.js("__PC.switchView('notifications')")
        # SCOPED TO #feed: in desktop mode the shell's own notification panel draws a second row
        # with the same class, bound by different code entirely.
        await b.until("!!document.querySelector('#feed .notif[data-open] .quoted[data-open]')")
        await b.js("(()=>{PCOS.openDoc=(...a)=>{__opened.push(a[0]);return true;};})()")
        point = await b.js("""(()=>{const n=document.querySelector('#feed .notif[data-open] .quoted[data-open]');
            if(!n) return null; const r=n.getBoundingClientRect();
            const x=r.left+r.width/2, y=r.top+r.height/2, hit=document.elementFromPoint(x,y);
            return {x,y,w:r.width,inQuote:!!hit?.closest('.quoted[data-open]')};})()""")
        assert point and point['w'] > 0 and point['inQuote'], f'nothing clickable there: {point}'
        await b.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': point['x'],
                                                  'y': point['y'], 'button': 'left', 'clickCount': 1})
        await b.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': point['x'],
                                                  'y': point['y'], 'button': 'left', 'clickCount': 1})
        await b.js('new Promise(r=>setTimeout(r,400))')
        opened = await b.js('__opened')
        assert len(opened) == 1, f'one click opened {len(opened)} post windows: {opened}'
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_composer_survives_clicking_the_post_it_is_replying_to():
    """In a popup the composer IS the window, so the preview must ask the desktop to open the post
    and leave the composer alone. On an ordinary page the old behaviour is still the right one."""
    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED)
        await b.until("!!__PC.me()")
        url = await b.js("location.origin+'/index.html?pcpopup=compose'")
        await b.call('Page.navigate', {'url': url})
        await b.until("!!window.__PC && !!window.PCOS && !!window.Store")
        # The preview reads the parent out of the Store synchronously, and a popup opens on a fresh
        # page with an empty one. How it got there is not what this test is about.
        await b.js("(()=>{ for(const e of __events) Store.saveEvent(e); })()")
        await b.until("!!Store.get(__events[0].id)")
        await b.js("__PC.compose({reply:__events[0].id})")
        await b.until("!!document.querySelector('.cmp-parent')")
        await b.js("(()=>{const t=document.querySelector('#cmp');if(t)t.value='half a sentence';})()")
        await b.js("document.querySelector('.cmp-parent').click()")
        await b.js('new Promise(r=>setTimeout(r,250))')
        acts = await b.js('__acts')
        expected = await b.js("'thread:'+__events[0].id")
        assert expected in acts, f'the desktop was never asked to open the post: {acts}'
        assert await b.js("!!document.querySelector('.cmp-parent')"), \
            'the composer window was closed by its own preview'
        assert await b.js("document.querySelector('#cmp').value") == 'half a sentence', \
            'what had been typed did not survive'
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))

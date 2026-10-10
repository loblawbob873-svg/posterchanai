"""Email: arrow keys, Space and Delete -- select several conversations and delete them from the keyboard.

Asked for: "I want to be able to select emails with space bar so I can delete them easier using arrow
keys, delete button, and space bar". Drives the shipped mail.js in the real bundle with REAL key
presses (CDP), against a stub mail server that records what is deleted:

  * ↓/↑ move one CONVERSATION at a time -- the cursor used to step through individual messages, so
    inside a thread it pointed at a message no row stands for and the highlight vanished;
  * Space ticks the row under the cursor (the whole conversation, like its checkbox), Space again
    unticks it, and the page does not scroll;
  * Delete removes everything ticked, after the usual confirm, which Enter accepts;
  * Delete with nothing ticked removes the conversation under the cursor; Esc on the confirm keeps it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.__deleted=[];
const originalFetch=window.fetch;
const m=(uid,subject,from,ts)=>({uid,account:'me@home.test',folder:'INBOX',subject,from,ts,preview:'…',read:true,attachments:0});
window.__inbox=[m('11','Re: Lunch plans','Alice',100),m('10','Lunch plans','Alice',90),
  m('20','Invoice 4411','Billing',80),m('30','Your parcel','Courier',70),m('40','Weekly digest','News',60),
  m('50','Old newsletter','News',50)];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent','Archive'],sent:'Sent'});
if(u.pathname.endsWith('/messages'))return reply({messages:u.searchParams.get('folder')==='INBOX'?__inbox.filter(x=>!__deleted.includes(x.uid)):[],next_until:0});
if(u.pathname.endsWith('/delete')){const b=JSON.parse(opts.body);(b.uids||[b.uid]).forEach(x=>__deleted.push(String(x)));return reply({ok:true});}
return reply({ok:true});};
__PC.switchView('mail');'''

KEYS = {'ArrowDown': (40, 'ArrowDown', ''), 'ArrowUp': (38, 'ArrowUp', ''), ' ': (32, 'Space', ' '),
        'Delete': (46, 'Delete', ''), 'Enter': (13, 'Enter', '\r'), 'Escape': (27, 'Escape', '')}


async def press(b, k):
    code, name, text = KEYS[k]
    base = {'key': k, 'code': name, 'windowsVirtualKeyCode': code, 'nativeVirtualKeyCode': code}
    await b.call('Input.dispatchKeyEvent', dict(base, type='keyDown', **({'text': text} if text else {})))
    await b.call('Input.dispatchKeyEvent', dict(base, type='keyUp'))
    await asyncio.sleep(.08)


ROWS = "[...document.querySelectorAll('#mail-items .mail-item')]"
STATE = ("(()=>{const r=" + ROWS + ";return {cursor:r.findIndex(e=>e.classList.contains('cursor')),"
         "ticked:r.map((e,i)=>e.querySelector('.mi-chk').checked?i:-1).filter(i=>i>=0),"
         "keys:r.map(e=>e.dataset.keys.split(',').map(k=>k.split('|').pop())),"
         "bulk:(document.querySelector('#mail-bulk-act')||{}).textContent||'',"
         "scroll:document.scrollingElement.scrollTop}})()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_select_with_space_and_delete_with_the_delete_key(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.until("document.querySelectorAll('#mail-items .mail-item').length===5")
        await b.js("document.activeElement&&document.activeElement.blur()")
        st = await b.js(STATE)
        assert st['keys'][0] == ['11', '10'], ('the fixture needs a two-message thread on top', st['keys'])

        # Down, Down: the SECOND ROW, not the second message of the first thread.
        await press(b, 'ArrowDown'); await press(b, 'ArrowDown')
        assert (await b.js(STATE))['cursor'] == 1
        await press(b, ' ')                                   # tick Invoice
        await press(b, 'ArrowDown'); await press(b, ' ')      # tick parcel
        await press(b, 'ArrowDown'); await press(b, ' ')      # tick digest...
        await press(b, ' ')                                   # ...and untick it again
        await press(b, 'ArrowUp'); await press(b, 'ArrowUp'); await press(b, 'ArrowUp')
        await press(b, ' ')                                   # tick the thread (both messages)
        st = await b.js(STATE)
        assert st['cursor'] == 0 and st['ticked'] == [0, 1, 2], st
        assert '4 selected' in st['bulk'], st                 # the thread is two messages
        assert st['scroll'] == 0, 'Space scrolled the page'

        await press(b, 'Delete')
        await b.until("!!document.querySelector('.uiconfirm-bg')")
        await press(b, 'Enter')
        await b.until("__deleted.length===4")
        assert sorted(await b.js("__deleted")) == ['10', '11', '20', '30']
        await b.until("document.querySelectorAll('#mail-items .mail-item').length===2")

        # Nothing ticked: Delete takes the conversation under the cursor -- and Esc keeps it.
        await b.js("document.activeElement&&document.activeElement.blur()")
        await press(b, 'ArrowDown')
        cur = await b.js(STATE)
        target = cur['keys'][cur['cursor']]
        await press(b, 'Delete')
        await b.until("!!document.querySelector('.uiconfirm-bg')")
        await press(b, 'Escape')
        await asyncio.sleep(.3)
        assert await b.js("__deleted.length") == 4, 'Esc on the confirm still deleted'
        assert (await b.js(STATE))['ticked'] == [], 'a cancelled delete left the row ticked'
        await press(b, 'Delete')
        await b.until("!!document.querySelector('.uiconfirm-bg')")
        await press(b, 'Enter')
        await b.until("__deleted.length===5")
        assert (await b.js("__deleted"))[4:] == target
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check))

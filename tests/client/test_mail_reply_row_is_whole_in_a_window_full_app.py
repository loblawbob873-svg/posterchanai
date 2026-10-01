"""PosterChanOS: the Reply / Forward row at the bottom of an open email is never cut off.

Reported: "Reply and Forward buttons at the bottom of an email message on OS cut off on bottom".
`.mail-thread` was `flex:1; min-height:0` inside the scrolling reading pane, so it shrank below its
content, the content overflowed it, and its 24px bottom padding never counted toward the scroll length:
scrolled to the end, the row sat exactly ON the window's bottom edge (measured 729 = 729 at 768px tall)
and the window frame clipped it. Drives the shipped mail.js in a real desktop window.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''const originalFetch=window.fetch;
const m=(uid,subject,ts)=>({uid,account:'me@home.test',folder:'INBOX',subject,from:'Sender',from_email:'s@x.test',ts,preview:'…',read:true,attachments:0,body_text:'Line\n'.repeat(60),message_id:'<'+uid+'@x>'});
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent'],sent:'Sent'});
if(u.pathname.endsWith('/messages'))return reply({messages:[m('7','Invoice',10)],next_until:0});
if(u.pathname.endsWith('/message'))return reply({message:m('7','Invoice',10)});
if(u.pathname.endsWith('/thread'))return reply({messages:[m('7','Invoice',10)]});
return reply({ok:true});};'''

MEASURE = r"""(()=>{const r=document.querySelector('.mail-thread-reply'),w=r.closest('.osw'),body=w.querySelector('.osw-body');
 const rr=r.getBoundingClientRect(),br=body.getBoundingClientRect(),wr=w.getBoundingClientRect();
 const btns=[...r.querySelectorAll('.btn')].map(x=>x.getBoundingClientRect());
 return {gap:br.bottom-rr.bottom, inside:btns.every(x=>x.bottom<=br.bottom-4&&x.top>=br.top), btnBottom:rr.bottom, bodyBottom:br.bottom, winBottom:wr.bottom}})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('height', [768, 900, 1080])
def test_the_reply_row_has_room_below_it_in_a_desktop_window(height):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1366, height=height, deviceScaleFactor=1, mobile=False))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.until('!!window.PCOS')
        if not await b.js('PCOS.isOn()'):
            await b.js('PCOS.enter()')
        await b.js("__PC.switchView('mail')")
        await b.until("document.querySelectorAll('.mail-item').length===1")
        await b.js("document.querySelector('.mail-item .mi-content').click()")
        await b.until("!!document.querySelector('.osw .mail-thread-reply')")
        await asyncio.sleep(.6)
        await b.js("(()=>{const p=document.querySelector('.mail-read');p.scrollTop=p.scrollHeight;})()")
        await asyncio.sleep(.4)
        got = await b.js(MEASURE)
        assert got['inside'] and got['gap'] >= 12, ('the Reply / Forward row is on (or past) the window edge', got)
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check))

"""Email: a delete leaves the screen AT ONCE, the server is asked behind it, and a refusal is said.

"Email: deletes are slow and do not update the UI well... messages should disappear from the read
pane." Nothing on screen changed until the server had finished -- two IMAP sessions per message on
the server, in series for a bulk delete, and then a reload of the whole folder AND the Sent folder --
and the reader went on showing the deleted message the whole time.

Drives the SHIPPED bundle against a stub mail server whose /delete does not answer until the test
releases it -- i.e. a slow mail server, the condition of the report -- and checks what the user SEES:

  * deleting the open message from the reader removes its row and takes the reader off it BEFORE
    the server has answered: on a wide screen the reader moves to the next conversation; on a phone
    (where the reader is a full-screen sheet over the list) it closes back to the list;
  * nothing re-reads the folder after a delete -- what is on screen is the answer;
  * a bulk delete is ONE request per folder carrying every uid;
  * a delete the server refuses puts the rows back and SAYS so in a toast.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.__deletes=[];window.__lists=0;window.__release=[];window.__toasts=[];
new MutationObserver(()=>document.querySelectorAll('.toast').forEach(t=>{if(!t.__seen){t.__seen=1;__toasts.push(t.textContent)}}))
  .observe(document.body,{childList:true,subtree:true});
const originalFetch=window.fetch;
const m=(uid,subject,from,ts)=>({uid,account:'me@home.test',folder:'INBOX',subject,from,from_email:from.toLowerCase()+'@x.test',ts,preview:'…',read:true,attachments:0,to:'me@home.test',body_text:'Body of '+subject});
window.__inbox=[m('1','Alpha','Alice',100),m('2','Bravo','Bob',90),m('3','Charlie','Carol',80),m('4','Delta','Dan',70)];
window.__gone=new Set();
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}});
const p=u.pathname;
if(p.endsWith('/accounts'))return Promise.resolve(reply({accounts:[{email:'me@home.test'}]}));
if(p.endsWith('/folders'))return Promise.resolve(reply({folders:['INBOX','Sent','Trash'],sent:'Sent'}));
if(p.endsWith('/messages')){if(u.searchParams.get('folder')==='INBOX')__lists++;
  return Promise.resolve(reply({messages:u.searchParams.get('folder')==='INBOX'?__inbox.filter(x=>!__gone.has(x.uid)):[],next_until:0}));}
if(p.endsWith('/message')){const x=__inbox.find(y=>y.uid===u.searchParams.get('uid'));return Promise.resolve(reply({message:x}));}
if(p.endsWith('/thread'))return Promise.resolve(reply({messages:[]}));
if(p.endsWith('/delete')){const b=JSON.parse(opts.body);__deletes.push(b);
  // A SLOW mail server: the answer waits until the test releases it.
  return new Promise(res=>__release.push(ok=>{ if(ok){(b.uids||[b.uid]).forEach(x=>__gone.add(String(x)));res(reply({ok:true}));}
                                               else res(reply({detail:'the mail server would not delete that message'},502)); }));}
return Promise.resolve(reply({ok:true}));};
__PC.switchView('mail');'''

ROWS = "[...document.querySelectorAll('#mail-items .mail-item')].map(e=>e.querySelector('.mi-subj').textContent.trim())"
READER = ("(()=>{const p=document.querySelector('#mail-read');const s=p&&p.querySelector('.mr-subj');"
          "return {open:!!(p&&p.classList.contains('has-open')),subj:s?s.textContent.trim():'',"
          "visible:!!(p&&p.getClientRects().length&&getComputedStyle(p).display!=='none')}})()")


async def within(b, expr, secs=1.5):
    """The UI must get there in `secs` -- well inside the time the stub server is being held."""
    for _ in range(int(secs / .05)):
        if await b.js(expr):
            return
        await asyncio.sleep(.05)
    raise AssertionError({'not within %.1fs' % secs: expr, 'rows': await b.js(ROWS), 'reader': await b.js(READER)})


async def confirm(b):
    await b.until("!!document.querySelector('.uiconfirm [data-uc=\"1\"]')")
    await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click(); true")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_delete_leaves_the_list_and_the_reader_before_the_server_answers(width):
    phone = width < 600

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=phone))
        await desktop.login(b)
        await b.js(STUB)
        await b.until("document.querySelectorAll('#mail-items .mail-item').length===4")
        await asyncio.sleep(.4)                                  # let the opening loads settle

        # 1. Open Alpha, delete it from the reader. The server does NOT answer yet.
        await click(b, '#mail-items .mail-item .mi-content')
        await b.until("(document.querySelector('#mail-read .mr-subj')||{}).textContent==='Alpha'")
        lists_before = await b.js('__lists')
        await click(b, '.mail-actions [data-act="delete"]')
        await confirm(b)
        await b.until('__deletes.length===1')
        await within(b, ROWS + ".join()==='Bravo,Charlie,Delta'")
        if phone:
            # The reader is a sheet over the list: it closes, and the list is what you see.
            await within(b, "!document.querySelector('#mail-read').classList.contains('has-open')")
            await within(b, "!!document.elementFromPoint(195,300)?.closest('.mail-list')")
        else:
            await within(b, "(document.querySelector('#mail-read .mr-subj')||{}).textContent==='Bravo'")
        assert await b.js('__release.length') == 1, 'the server has not been answered yet -- the UI did not wait for it'
        await b.js('__release.shift()(true); true')
        await b.until("__toasts.some(t=>/deleted/i.test(t))")
        await asyncio.sleep(.5)
        assert await b.js(ROWS) == ['Bravo', 'Charlie', 'Delta']
        assert await b.js('__lists') == lists_before, 'the whole folder was re-read after the delete'
        assert await b.js('__deletes[0]') == {'account': 'me@home.test', 'folder': 'INBOX', 'uid': '1', 'uids': ['1']}

        # 2. Bulk: tick Charlie and Delta; ONE request carrying both, and the server REFUSES it.
        if phone and await b.js("document.querySelector('#mail-read').classList.contains('has-open')"):
            await click(b, '#mail-back')
        await b.js("[...document.querySelectorAll('#mail-items .mail-item')].slice(1).forEach(e=>e.querySelector('.mi-chk').click()); true")
        await b.until("!!document.querySelector('[data-bulk=\"delete\"]')")
        await b.js("document.querySelector('[data-bulk=\"delete\"]').click(); true")
        await confirm(b)
        await b.until('__deletes.length===2')
        await within(b, ROWS + ".join()==='Bravo'")
        assert sorted(await b.js('__deletes[1].uids')) == ['3', '4'], await b.js('__deletes[1]')
        await b.js('__release.shift()(false); true')
        await b.until(ROWS + ".join()==='Bravo,Charlie,Delta'")
        await b.until("__toasts.some(t=>/could not delete/i.test(t))")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))

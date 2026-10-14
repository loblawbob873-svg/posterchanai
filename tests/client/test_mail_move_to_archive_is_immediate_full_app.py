"""Email: Move → Archive takes the message off the list AT ONCE, and puts it back if the server refuses.

"Mail performance for moving to archive is slow." Besides the server's login-per-message (see
tests/test_mail_archive_one_session.py), the reader's Move → Archive WAITED for the mail server's answer and
then reloaded the whole list before anything moved on screen -- while the toolbar's Archive had long been
immediate. Drives the shipped mail.js in the real bundle against a stub mail server whose /archive answer is
HELD until the test releases it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.__archived=[]; window.__release=null; window.__answer=200;
const originalFetch=window.fetch;
const m=(uid,subject,from,ts)=>({uid,account:'me@home.test',folder:'INBOX',subject,from,ts,preview:'…',read:true,attachments:0});
window.__inbox=[m('20','Invoice 4411','Billing',80),m('30','Your parcel','Courier',70),m('40','Weekly digest','News',60)];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent','Receipts'],sent:'Sent'});
if(u.pathname.endsWith('/messages'))return reply({messages:u.searchParams.get('folder')==='INBOX'?__inbox.filter(x=>!__archived.includes(x.uid)):[],next_until:0});
if(u.pathname.endsWith('/message'))return reply({message:Object.assign({},__inbox[0],{body_html:'<p>hello</p>'})});
if(u.pathname.endsWith('/thread'))return reply({messages:[Object.assign({},__inbox[0],{body_html:'<p>hello</p>'})]});
if(u.pathname.endsWith('/archive')){const b=JSON.parse(opts.body);
  return new Promise(res=>{window.__release=()=>{
    if(__answer===200){(b.uids||[b.uid]).forEach(x=>__archived.push(String(x)));res(new Response(JSON.stringify({ok:true}),{status:200,headers:{'Content-Type':'application/json'}}));}
    else res(new Response(JSON.stringify({detail:'the mail server would not archive that message'}),{status:502,headers:{'Content-Type':'application/json'}}));};});}
return reply({ok:true});};
__PC.switchView('mail');'''

ROWS = "[...document.querySelectorAll('#mail-items .mail-item')].map(e=>e.dataset.keys.split('|').pop())"


async def _archive_from_the_reader(b):
    await b.until("document.querySelectorAll('#mail-items .mail-item').length===3")
    await b.js("document.querySelector('#mail-items .mail-item .mi-content').click()")
    await b.until("!!document.querySelector('#mail-read [data-act=\"move\"]')")
    await b.js("document.querySelector('#mail-read [data-act=\"move\"]').click()")
    await b.until("!!document.querySelector('.uipick[data-v=\"__archive\"]')")
    await b.js("document.querySelector('.uipick[data-v=\"__archive\"]').click()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('answer', [200, 502])
def test_move_to_archive_leaves_the_list_before_the_server_answers(answer):
    got = {}

    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.js("window.__answer=%d" % answer)
        await _archive_from_the_reader(b)
        await b.until("typeof window.__release==='function'")   # the request is out, and held
        await asyncio.sleep(.4)
        got['while_waiting'] = await b.js(ROWS)
        await b.js("__release()")
        await asyncio.sleep(1.2)
        got['after'] = await b.js(ROWS)
        got['toasts'] = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')")
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['while_waiting'] == ['30', '40'], ('the message stayed on the list until the mail server answered', got)
    if answer == 200:
        assert got['after'] == ['30', '40'], got
    else:
        assert got['after'] == ['20', '30', '40'], ('a refused archive must put the message back', got)
    assert not got['errors'], got['errors']

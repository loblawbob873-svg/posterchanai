"""Email search in All Inboxes covers every account and every folder -- and says so while it works.

Reported: "email search should search every mailbox and folder if you are in All Inboxes. It don't work
like that now". The server always searched everything (measured: 'payroll' -> 730 hits across 3 accounts
and 9 folders) but took ~12 s, and while it ran the list kept showing the folder you were in -- so it
looked like a search of that folder that found nothing. Now the list says it is searching everywhere,
and the results say where they came from. Drives the shipped mail.js in the real bundle.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.__searchAsked=[];
const originalFetch=window.fetch;
const m=(uid,account,folder,subject,ts)=>({uid,account,folder,subject,from:'Payroll',from_email:'pay@x.test',ts,preview:'…',read:true,attachments:0});
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'a@one.test'},{email:'b@two.test'}]});
if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent','Archive'],sent:'Sent'});
if(u.pathname.endsWith('/messages'))return reply({messages:[m('1','a@one.test','INBOX','Lunch',100)],next_until:0});
if(u.pathname.endsWith('/search')){__searchAsked.push(u.search);
  return new Promise(r=>setTimeout(()=>r(new Response(JSON.stringify({messages:[
    m('7','a@one.test','INBOX.Archive','Payroll March',90),m('8','b@two.test','INBOX.Sent','Re: payroll question',80),
    m('9','b@two.test','Trash','Old payroll',70)]}),{status:200,headers:{'Content-Type':'application/json'}})),1500));}
return reply({ok:true});};
__PC.switchView('mail');'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_all_inboxes_search_says_it_searches_everywhere_and_shows_every_mailbox(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.until("document.querySelectorAll('.mail-item').length===1")
        acct = await b.js("(document.querySelector('#mail-acct')||{}).value||''")
        assert acct == '__all', ('the fixture is not in All Inboxes', acct)
        await b.js("(()=>{const s=document.querySelector('#mail-search');s.value='payroll';s.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.until("/Searching every account and folder/.test(document.querySelector('#mail-items').textContent)")
        assert await b.js("document.querySelectorAll('#mail-items .mail-item').length") == 0, \
            'the folder you were in stayed on screen while searching everywhere'
        await b.until("document.querySelectorAll('#mail-items .mail-item').length>=3")
        text = await b.js("document.querySelector('#mail-items').innerText")
        assert '3 matches in every account and folder' in text, text[:300]
        assert 'a@one.test' in text and 'b@two.test' in text, ('results from one mailbox only', text[:400])
        for subj in ('Payroll March', 'payroll question', 'Old payroll'):
            assert subj in text, (subj, text[:400])
        assert all('account=' not in q and 'folder=' not in q for q in await b.js('__searchAsked')), \
            'the search was narrowed to one account or folder'
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check))

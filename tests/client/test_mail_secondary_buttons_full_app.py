"""Email's secondary buttons do what they say, pressed in the SHIPPED bundle against a stubbed mail API:

  * no account yet: "Open Settings → Mail" and "Open Messages" leave for those screens;
  * the sign-in failed: Retry asks again and, when the server answers, draws the mailbox;
  * the folder strip's More lists the other folders and opens the one picked;
  * the folder browser's "Fetch folder list" asks the server again and draws the new folder, Close closes it;
  * the sender card's "Their messages" searches the mailbox for that address, "Copy address" copies it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.__mailReq=[];window.__copied=[];
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__copied.push(String(t));}}});}catch(_){}
const originalFetch=window.fetch;
window.__mail={accounts:[{email:'me@home.test'}],authFail:0,folders:['INBOX','Sent','Drafts','Archive','Receipts']};
const base={account:'me@home.test',ts:100,read:true,attachments:0,preview:'…'};
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
__mailReq.push(u.pathname+u.search);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
const p=u.pathname;
if(p.endsWith('/accounts')){if(__mail.authFail>0){__mail.authFail--;return reply({detail:'could not establish your app session: test'},401);}return reply({accounts:__mail.accounts});}
if(p.endsWith('/folders'))return reply({folders:__mail.folders,sent:'Sent'});
if(p.endsWith('/messages')){const f=u.searchParams.get('folder')||'INBOX';
  return reply({messages:[Object.assign({},base,{folder:f,uid:'1',subject:'In '+f,from:'Alice Smith <alice@x.test>',from_email:'alice@x.test',to:'me@home.test',body_text:'hello'})],next_until:0});}
if(p.endsWith('/message'))return reply({message:Object.assign({},base,{folder:'INBOX',uid:'1',subject:'In INBOX',from:'Alice Smith <alice@x.test>',from_email:'alice@x.test',to:'me@home.test',body_text:'hello'})});
if(p.endsWith('/thread'))return reply({messages:[]});
return reply({ok:true,messages:[]});};'''


# The single-screen layout: in the windowed desktop a view opens in a window of its own beside Mail.
PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
         "osMode:false}));")


def _run(check):
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


async def _boot(b, setup=''):
    await desktop.login(b)
    await b.js(STUB + setup + "\n__PC.switchView('mail');")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_account_offers_settings_and_messages():
    async def check(b):
        await _boot(b, "__mail.accounts=[];")
        await b.until("!!document.querySelector('#mail-go-settings')")
        await click(b, '#mail-go-settings')
        await b.until("!document.querySelector('#mail-go-settings')&&!!document.querySelector('[data-view=\"settings\"].active, [data-view=\"settings\"].on')")
        await b.js("__PC.switchView('mail'); 1")
        await b.until("!!document.querySelector('#mail-go-dms')")
        await click(b, '#mail-go-dms')
        await b.until("!document.querySelector('#mail-go-dms')&&!!document.querySelector('[data-view=\"messages\"].active, [data-view=\"messages\"].on')")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_retry_after_a_failed_sign_in_draws_the_mailbox():
    async def check(b):
        await _boot(b, "__mail.authFail=1;")
        await b.until("!!document.querySelector('#mail-auth-retry')")
        # The server's own reason, not "http 401".
        assert 'could not establish your app session: test' in await b.js("document.querySelector('.mail-empty').textContent")
        await click(b, '#mail-auth-retry')
        await b.until("[...document.querySelectorAll('#mail-items .mail-item')].some(e=>e.textContent.includes('In INBOX'))")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_more_folders_and_the_folder_browser():
    async def check(b):
        await _boot(b)
        await b.until("!!document.querySelector('#mail-more')")
        await click(b, '#mail-more')
        await b.until("!!document.querySelector('[data-m=\"Receipts\"]')")
        await b.js("document.querySelector('[data-m=\"Receipts\"]').click(); 1")
        await b.until("[...document.querySelectorAll('#mail-items .mail-item')].some(e=>e.textContent.includes('In Receipts'))")
        assert await b.js("__mailReq.some(r=>r.includes('/messages')&&r.includes('folder=Receipts'))")

        # The folder browser: a folder the server adds appears after "Fetch folder list"; Close closes it.
        await click(b, '#mail-folders-open')
        await b.until("!!document.querySelector('#mail-fbrowse-reload')")
        assert not await b.js("!!document.querySelector('#mail-fbrowse-list [data-folder=\"Taxes\"]')")
        await b.js("__mail.folders=__mail.folders.concat(['Taxes']); 1")
        await click(b, '#mail-fbrowse-reload')
        await b.until("!!document.querySelector('#mail-fbrowse-list [data-folder=\"Taxes\"]')")
        await click(b, '#mail-fbrowse-close')
        await b.until("!document.querySelector('#mail-fbrowse-close')")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_sender_card_finds_their_messages_and_copies_the_address():
    async def check(b):
        await _boot(b)
        await b.until("document.querySelectorAll('#mail-items .mail-item').length>0")
        await b.js("document.querySelector('#mail-items .mail-item .mi-content').click(); 1")
        await b.until("!!document.querySelector('#mail-read b.mm-sender')")
        await click(b, '#mail-read b.mm-sender')
        await b.until("!!document.querySelector('#msc-copy')")
        await click(b, '#msc-copy')
        await b.until("__copied.includes('alice@x.test')")
        assert await b.js("!!document.querySelector('#msc-find')"), 'copying closed the card'
        before = await b.js("__mailReq.length")
        await click(b, '#msc-find')
        await b.until("!document.querySelector('#msc-find')")
        assert await b.js("document.querySelector('#mail-search').value") == 'alice@x.test'
        await b.until(f"__mailReq.slice({before}).some(r=>r.includes('alice%40x.test')||r.includes('alice@x.test'))")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


# The node's answer to "summarize this email" (the AI itself is out of scope; the Copy under its answer is not).
SUMMARY = r'''window.__aiAsked=[];const __sumFetch=window.fetch;
window.fetch=(url,opts)=>{if(String(url).includes('/api/mail/ai')){__aiAsked.push(JSON.parse(opts.body));
  return Promise.resolve(new Response(JSON.stringify({content:'Alice says hello and wants nothing.'}),{status:200,headers:{'Content-Type':'application/json'}}));}
  return __sumFetch(url,opts);};'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_copy_under_a_summary_copies_the_summary():
    """✨ → Summarize this email shows the summary with a Copy button (#ma-copy); Copy puts exactly that text on the
    clipboard."""
    async def check(b):
        await _boot(b, SUMMARY)
        await b.until("document.querySelectorAll('#mail-items .mail-item').length>0")
        await b.js("document.querySelector('#mail-items .mail-item .mi-content').click(); 1")
        await b.until("!!document.querySelector('#mail-read [data-act=\"ai\"]')")
        await b.js("document.querySelector('#mail-read [data-act=\"ai\"]').click(); 1")
        await b.until("!!document.querySelector('.uipick[data-v=\"sum\"]')")
        await b.js("document.querySelector('.uipick[data-v=\"sum\"]').click(); 1")
        await b.until("!!document.querySelector('#ma-copy')")
        assert await b.js("__aiAsked[0].mode") == 'summarize' and 'hello' in await b.js("__aiAsked[0].text")
        assert await b.js("__copied.length") == 0
        await click(b, '#ma-copy')
        await b.until("__copied.includes('Alice says hello and wants nothing.')")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)

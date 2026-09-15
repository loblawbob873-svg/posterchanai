"""Shipped Email UI searches every account without changing the user's browsing folder."""
import asyncio
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click

@pytest.fixture(scope='module',autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.parametrize('width',[390,1280])
def test_global_search_provenance_open_clear_and_stale_paging(width):
    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=900,deviceScaleFactor=1,mobile=width<600))
        await browser.until("document.body.classList.contains('guest')")
        await desktop.login(browser)
        await browser.js('''window.mailRequests=[];window.pendingMailPage=null;window.failSearch=false;window.holdWork=false;window.releaseWork=null;window.holdHomeThread=false;window.releaseHomeThread=null;
const originalFetch=window.fetch;
window.fixtureMail=[{uid:'7',account:'same@work.test',folder:'INBOX',subject:'Invoice',from:'Sender',ts:10,preview:'work match',read:true,attachments:0},
{uid:'7',account:'same@home.test',folder:'Archive',subject:'Invoice',from:'Sender',ts:20,preview:'home match',read:true,attachments:0}];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
mailRequests.push(u.pathname+u.search);const reply=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
if(u.pathname.endsWith('/accounts'))return Promise.resolve(reply({accounts:fixtureMail.map(m=>({email:m.account}))}));
if(u.pathname.endsWith('/folders'))return Promise.resolve(reply({folders:['INBOX','Sent','Archive'],sent:'Sent'}));
if(u.pathname.endsWith('/search'))return Promise.resolve(reply({messages:fixtureMail,next_until:999},failSearch?503:200));
if(u.pathname.endsWith('/messages')){if(u.searchParams.has('until'))return new Promise(resolve=>pendingMailPage=()=>resolve(reply({messages:[{...fixtureMail[0],uid:'old',subject:'stale page'}]})));
return Promise.resolve(reply({messages:u.searchParams.get('folder')==='Sent'?[]:[fixtureMail[0]],next_until:123}));}
if(u.pathname.endsWith('/message')){const message={...fixtureMail.find(m=>m.account===u.searchParams.get('account')),attachments:[],body_text:'Opened '+u.searchParams.get('account')};if(holdWork&&message.account==='same@work.test')return new Promise(resolve=>releaseWork=()=>resolve(reply({message})));return Promise.resolve(reply({message}));}
if(u.pathname.endsWith('/thread')){if(holdHomeThread&&u.searchParams.get('account')==='same@home.test')return new Promise(resolve=>releaseHomeThread=()=>resolve(reply({messages:[{...fixtureMail[1],attachments:[],body_text:'STALE HOME THREAD'},{...fixtureMail[1],uid:'8',attachments:[],body_text:'STALE HOME THREAD'}]})));return Promise.resolve(reply({messages:[]}));}
return Promise.resolve(reply({ok:true,folders:['INBOX','Sent','Archive']}));};
__PC.switchView('mail');''')
        await browser.until("!!document.querySelector('#mail-acct')")
        await browser.js("const select=document.querySelector('#mail-acct');select.value='same@work.test';select.dispatchEvent(new Event('change',{bubbles:true}));")
        await browser.until("!!document.querySelector('#mail-more-btn')")
        await click(browser,'#mail-more-btn')
        await browser.until('!!pendingMailPage')
        await browser.js("var input=document.querySelector('#mail-search');input.value='invoice';input.dispatchEvent(new Event('input',{bubbles:true}));")
        await browser.until("document.querySelectorAll('.mail-item').length===2")
        assert await browser.js("mailRequests.some(p=>p==='/api/mail/search?q=invoice')")
        assert await browser.js("document.querySelector('#mail-acct').value==='same@work.test'")
        assert await browser.js("[...document.querySelectorAll('.mi-acct')].map(x=>x.textContent).sort().join(',')==='same@home.test,same@work.test'")
        assert not await browser.js("!!document.querySelector('#mail-more-btn')")
        await browser.js('pendingMailPage()')
        await browser.js('new Promise(r=>setTimeout(r,30))')
        assert await browser.js("document.querySelectorAll('.mail-item').length===2&&!document.querySelector('#mail-items').textContent.includes('stale page')")
        await browser.js('holdWork=true')
        await click(browser,'.mail-item[data-account="same@work.test"] .mi-content')
        await browser.until('!!releaseWork')
        await click(browser,'.mail-item[data-account="same@home.test"] .mi-content')
        await browser.until("mailRequests.some(p=>p.includes('/message?account=same%40home.test&folder=Archive&uid=7'))")
        assert await browser.js("document.querySelectorAll('.mail-item.active').length===1&&document.querySelector('.mail-item.active').dataset.account==='same@home.test'")
        await browser.until("document.querySelector('#mail-read').textContent.includes('Opened same@home.test')")
        await browser.js('releaseWork();holdWork=false;holdHomeThread=true')
        await browser.js('new Promise(r=>setTimeout(r,30))')
        assert not await browser.js("document.querySelector('#mail-read').textContent.includes('Opened same@work.test')")
        if width<600:
            await click(browser,'#mail-back')
        await click(browser,'.mail-item[data-account="same@home.test"] .mi-content')
        await browser.until('!!releaseHomeThread')
        if width<600:
            await click(browser,'#mail-back')
        await click(browser,'.mail-item[data-account="same@work.test"] .mi-content')
        await browser.until("document.querySelector('#mail-read').textContent.includes('Opened same@work.test')")
        await browser.js('releaseHomeThread()')
        await browser.js('new Promise(r=>setTimeout(r,30))')
        assert not await browser.js("document.querySelector('#mail-read').textContent.includes('STALE HOME THREAD')")
        if width<600:
            await click(browser,'#mail-back')
        await browser.js("failSearch=true;var input=document.querySelector('#mail-search');input.value='new search';input.dispatchEvent(new Event('input',{bubbles:true}));")
        await browser.until("document.querySelector('#mail-items').textContent.includes('Could not search email')")
        await browser.js("var input=document.querySelector('#mail-search');input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));")
        await browser.until("document.querySelectorAll('.mail-item').length===1")
        assert await browser.js("mailRequests.filter(p=>p.includes('/messages?')).at(-1).includes('account=same%40work.test')")
    init="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra_init=init))

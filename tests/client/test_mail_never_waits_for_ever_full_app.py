"""Mail never sits on a spinner for ever when the node does not answer -- it says so, and comes back by itself.

Reported 2026-10-29: "Telegram and Email on windows app is stuck in a loading state for many minutes now" -- the app
was opened during a deploy. Telegram got a time limit on 10-21; Mail had none, and nginx holds an /api/ request up to
an hour when it reached a process that was shutting down. The SHIPPED mail.js in the real bundle, with the node's
/api/mail/accounts never answering: Mail shows "Email couldn't sign in" with the reason instead of a spinner, and when
the node answers again it opens the inbox by itself, without Retry being pressed. At phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.mailHang=true; window.__pcMailPatienceMs=1500;
const originalFetch=window.fetch;
const inbox=[{uid:'7',account:'me@home.test',folder:'INBOX',subject:'Invoice',from:'Sender',ts:10,preview:'pay me',read:true,attachments:0}];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
if(mailHang)return new Promise(()=>{});   // a request that reached a process shutting down: never answered
if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent','Archive'],sent:'Sent'});
if(u.pathname.endsWith('/messages'))return reply({messages:u.searchParams.get('folder')==='INBOX'?inbox:[],next_until:0});
return reply({ok:true});};'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_mail_says_the_node_did_not_answer_and_comes_back_by_itself(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.js("__PC.switchView('mail')")
        # a spinner for ever on the old code; within the limit it says what happened
        await b.until("!!document.querySelector('#mail-auth-retry')")
        text = await b.js("document.querySelector('.mail-empty').textContent")
        assert "did not answer" in text, text
        assert not await b.js("!!document.querySelector('.mail-loading .spinner')")
        # the node is back: Mail opens the inbox by itself (the retry is 10 s; nobody presses anything)
        await b.js("mailHang=false")
        for _ in range(3):
            if await b.js("document.querySelectorAll('.mail-item').length===1"):
                break
            try:
                await b.until("document.querySelectorAll('.mail-item').length===1")
            except AssertionError:
                pass
        assert await b.js("document.querySelectorAll('.mail-item').length===1"), "Mail did not come back by itself"
        assert 'Invoice' in await b.js("document.querySelector('#mail-items').textContent")
    asyncio.run(desktop.with_browser('online', '', check))

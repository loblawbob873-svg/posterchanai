"""'we cant have our core apps not working if network outage' -- Mail keeps the inbox on screen when a
refresh cannot reach the server.

Every deploy restarts the relay for ~30s. The server used to answer an empty inbox then (loose read);
it now answers 503 (tests/test_mail_strict_reads.py), and this drives the SHIPPED mail.js in the real
bundle through what the client must do with that: a refresh of the folder already on screen that fails
keeps its messages and says the list is not fresh -- it used to blank to "Could not load email" -- and
the next good refresh clears the note. Opening a DIFFERENT folder that fails still says it could not
load (there is nothing of that folder to keep). At phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STUB = r'''window.mailDown=false;
const originalFetch=window.fetch;
const inbox=[{uid:'7',account:'me@home.test',folder:'INBOX',subject:'Invoice',from:'Sender',ts:10,preview:'pay me',read:true,attachments:0}];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return originalFetch(url,opts);
const reply=(v,s=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:s,headers:{'Content-Type':'application/json'}}));
if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent','Archive'],sent:'Sent'});
if(u.pathname.endsWith('/messages')){if(mailDown)return reply({detail:'Could not reach your mail just now — try again.'},503);
  return reply({messages:u.searchParams.get('folder')==='INBOX'?inbox:[],next_until:0});}
return reply({ok:true});};
__PC.switchView('mail');'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_failed_refresh_keeps_the_inbox_and_says_so(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js(STUB)
        await b.until("document.querySelectorAll('.mail-item').length===1")

        await b.js("mailDown=true")
        await click(b, '#mail-refresh')
        await b.until("!!document.querySelector('.mail-stale')")
        st = await b.js("""({items:document.querySelectorAll('.mail-item').length,
                            text:document.querySelector('#mail-items').textContent,
                            fits:document.querySelector('#mail-items').scrollWidth<=document.querySelector('#mail-items').clientWidth+1})""")
        assert st['items'] == 1 and 'Invoice' in st['text'], ('the inbox was blanked by a refresh that failed', st)
        assert 'could not reach the server' in st['text'] and st['fits'], st

        await b.js("mailDown=false")
        await click(b, '#mail-refresh')
        await b.until("!document.querySelector('.mail-stale') && document.querySelectorAll('.mail-item').length===1")
    asyncio.run(desktop.with_browser('online', '', check))

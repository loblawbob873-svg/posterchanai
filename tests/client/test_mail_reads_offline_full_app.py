"""OFFLINE, EMAIL STILL SHOWS THE MAIL YOU HAVE READ.

"we need to make sure that email, notes works when no connection either". The mailbox lives on the
instance and the Email screen only ever asked /api/mail, so with no network it was an error over an
empty screen. Two phases in ONE browser profile: online, the real client opens the inbox and a message;
then a reload with no network at all (navigator.onLine false, /api/mail unreachable, every relay socket
dead) — the inbox and that message must still be there, said to be the copy kept on this device, and
what was kept on the device must not be readable as plaintext.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__mailAsked=0;
const dead=localStorage.getItem('__dead')==='1';
if(dead){
  Object.defineProperty(Navigator.prototype,'onLine',{configurable:true,get:()=>false});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
}
const realFetch=window.fetch;
const inbox=[{uid:'7',account:'me@home.test',folder:'INBOX',subject:'Ferry timetable',from:'Harbour Office',ts:10,preview:'the 7:40 sails',read:true,attachments:0}];
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);if(!u.pathname.startsWith('/api/mail/'))return realFetch(url,opts);
  __mailAsked++;
  if(dead)return Promise.reject(new TypeError('Failed to fetch'));
  const reply=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(u.pathname.endsWith('/accounts'))return reply({accounts:[{email:'me@home.test'}]});
  if(u.pathname.endsWith('/folders'))return reply({folders:['INBOX','Sent'],sent:'Sent'});
  if(u.pathname.endsWith('/messages'))return reply({messages:u.searchParams.get('folder')==='INBOX'?inbox:[],next_until:0});
  if(u.pathname.endsWith('/message'))return reply({message:{...inbox[0],attachments:[],body_text:'The 7:40 sails from pier four on weekdays.'}});
  if(u.pathname.endsWith('/thread'))return reply({messages:[]});
  return reply({ok:true});};
'''

OPEN = "document.querySelector('#mail-read') && /pier four/.test(document.querySelector('#mail-read').textContent)"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_email_reads_what_was_opened_before_with_no_network(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchView('mail')")
        await b.until("document.querySelectorAll('.mail-item').length===1")
        await click(b, '.mail-item .mi-content')
        await b.until(OPEN)
        await asyncio.sleep(1.5)                        # the device copy is written behind the paint
        got['sealed'] = await b.js("""new Promise(res=>{const r=indexedDB.open('pc-mail-v1');r.onsuccess=()=>{
            const q=r.result.transaction('r').objectStore('r').getAll();q.onsuccess=()=>{
            const raw=JSON.stringify(q.result.map(x=>Array.from(new Uint8Array(x.ct||[])).map(c=>String.fromCharCode(c)).join('')));
            res({n:q.result.length,plain:/pier four|Ferry/.test(raw)})};};r.onerror=()=>res({n:-1})})""")

        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC')
        await b.until('!!__PC.me()')
        await b.js("__PC.switchView('mail')")
        await b.until("document.querySelectorAll('.mail-item').length===1")
        got['list'] = await b.js("document.querySelector('#mail-items').textContent")
        await click(b, '.mail-item .mi-content')
        await b.until(OPEN)
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['sealed']['n'] >= 3 and not got['sealed']['plain'], ('mail kept on the device in the clear', got['sealed'])
    assert 'Ferry timetable' in got['list'] and 'kept on this device' in got['list'], got['list'][:300]

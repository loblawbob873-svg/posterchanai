"""Calendar's buttons, pressed in the SHIPPED bundle against a stubbed calendar API:

  * Today / ‹ ›: the month moves and Today brings it back;
  * an empty month says where the items are, and "Go there" goes there;
  * ☰ → New calendar → Create posts the name and selects the new calendar;
  * ☰ → Subscribe to a URL → Subscribe posts the address; Cancel closes without posting;
  * ☰ → Import .ics opens the file picker, and a chosen file is uploaded and imported;
  * ☰ → Sync to a device → Generate shows a password, Copy copies it, Revoke (confirmed) revokes;
  * ☰ → Publish an event → Publish signs a NIP-52 kind 31923 with the title and times.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
localStorage.setItem('pc_cal_nostr','0');
window.__sentEvents=[];
const __cSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __cSend.call(this,raw)};
window.__copied=[];
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__copied.push(String(t));}}});}catch(_){}
window.__picked=[];
const __iclick=HTMLInputElement.prototype.click;
HTMLInputElement.prototype.click=function(){if(this.type==='file'){__picked.push(this.id);return;}return __iclick.call(this);};
const old=['BEGIN:VCALENDAR','VERSION:2.0','BEGIN:VEVENT','UID:old','DTSTART;VALUE=DATE:20110304','SUMMARY:Long ago','END:VEVENT','END:VCALENDAR',''].join('\r\n');
window.__calendarData={a:[{uid:'old',ics:old,component:'VEVENT'}]};
window.__calendarCalls=[];
window.__calendarList=[{id:'a',displayname:'Personal'}];
const calendarFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=new URL(String(url),location.href),m=String(opts.method||'GET').toUpperCase();
 const reply=(body,status=200)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
 const p=u.pathname;
 if(!p.startsWith('/api/calendar/'))return calendarFetch(url,opts);
 let body=null;try{body=opts.body&&typeof opts.body==='string'?JSON.parse(opts.body):null;}catch(_){}
 if(m!=='GET')__calendarCalls.push({op:m+' '+p+u.search,body,form:opts.body instanceof FormData});
 if(p==='/api/calendar/config')return reply({enabled:true,url:'https://fixture.invalid/caldav/',username:'me',has_password:true});
 if(p==='/api/calendar/calendars'&&m==='POST'){const id='new-'+__calendarList.length;__calendarList.push({id,displayname:body.name});__calendarData[id]=[];return reply({id});}
 if(p==='/api/calendar/calendars')return reply({calendars:__calendarList});
 if(p==='/api/calendar/subscribe'){__calendarList.push({id:'school',displayname:'School',subscribe:{url:body.url}});__calendarData.school=[];return reply({id:'school',count:3});}
 if(p==='/api/calendar/password'&&m==='POST')return reply({password:'pw-fixture-123'});
 if(p==='/api/calendar/password'&&m==='DELETE')return reply({ok:true});
 if(p==='/api/calendar/import')return reply({imported:2,calendar:'a'});
 if(p==='/api/calendar/items')return reply({items:__calendarData[u.searchParams.get('cal')]||[]});
 return reply({ok:true});
};
'''

TITLE = "document.querySelector('.cal-title').textContent.trim()"


async def calendar(b):
    await desktop.login(b)
    await b.js("window.__publishOK=true; __PC.switchView('calendar'); 1")
    await b.until("!!document.querySelector('#cal-pick')")


async def menu(b, button):
    await b.js("document.querySelector('#cal-menu').click(); 1")
    await b.until(f"!!document.querySelector('#{button}')")
    await b.js(f"document.querySelector('#{button}').click(); 1")


def _run(check):
    asyncio.run(desktop.with_browser('online', '', check, BOUNDARIES))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_month_navigation_and_go_there():
    async def check(b):
        await calendar(b)
        now = await b.js(TITLE)
        # An empty month that knows where its items are says so, and Go there goes there.
        await b.until("!!document.querySelector('#cal-jump')")
        await b.js("document.querySelector('#cal-jump').click(); 1")
        await b.until(f"{TITLE}==='March 2011'")
        await b.js("document.querySelector('#cal-next').click(); 1")
        await b.until(f"{TITLE}==='April 2011'")
        await b.js("document.querySelector('#cal-today').click(); 1")
        await b.until(f"{TITLE}==={json.dumps(now)}")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_new_calendar_subscribe_and_import():
    async def check(b):
        await calendar(b)
        await menu(b, 'cal-add')
        await b.until("!!document.querySelector('#cnew-save')")
        await b.js("document.querySelector('#cnew-name').value='Gym'; document.querySelector('#cnew-save').click(); 1")
        await b.until("!document.querySelector('#cnew-save') && [...document.querySelector('#cal-pick').options].some(o=>o.selected&&o.textContent==='Gym')")
        assert await b.js("__calendarCalls.find(c=>c.op==='POST /api/calendar/calendars').body.name") == 'Gym'

        # Subscribe: Cancel posts nothing; Subscribe posts the address.
        await menu(b, 'cal-sub')
        await b.until("!!document.querySelector('#csub-x')")
        await b.js("document.querySelector('#csub-x').click(); 1")
        await b.until("!document.querySelector('#csub-x')")
        assert not await b.js("__calendarCalls.some(c=>c.op.includes('/subscribe'))")
        await menu(b, 'cal-sub')
        await b.until("!!document.querySelector('#csub-go')")
        await b.js("document.querySelector('#csub-url').value='https://school.example/term.ics'; document.querySelector('#csub-go').click(); 1")
        await b.until("!document.querySelector('#csub-go') && [...document.querySelector('#cal-pick').options].some(o=>o.selected&&o.value==='school')")
        assert await b.js("__calendarCalls.find(c=>c.op.includes('/subscribe')).body.url") == 'https://school.example/term.ics'

        # Import: the button opens the file picker; the chosen file is uploaded.
        await menu(b, 'cal-import')
        await b.until("__picked.includes('cal-file')")
        await b.js("(()=>{const i=document.querySelector('#cal-file');const dt=new DataTransfer();"
                   "dt.items.add(new File(['BEGIN:VCALENDAR\\r\\nEND:VCALENDAR\\r\\n'],'mine.ics',{type:'text/calendar'}));"
                   "i.files=dt.files;i.dispatchEvent(new Event('change'));})(); 1")
        await b.until("__calendarCalls.some(c=>c.op.startsWith('POST /api/calendar/import')&&c.form)")
        await b.until("!document.querySelector('#cal-file')")
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_device_password_and_publishing_a_nostr_event():
    async def check(b):
        await calendar(b)
        await menu(b, 'cal-phone')
        await b.until("!!document.querySelector('#cph-gen')")
        await b.js("document.querySelector('#cph-gen').click(); 1")
        await b.until("!!document.querySelector('#cph-copy')")
        assert 'pw-fixture-123' in await b.js("document.querySelector('#cph-out').textContent")
        await b.js("document.querySelector('#cph-copy').click(); 1")
        await b.until("__copied.includes('pw-fixture-123')")
        await b.js("document.querySelector('#cph-clear').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until("__calendarCalls.some(c=>c.op==='DELETE /api/calendar/password') && !document.querySelector('#cph-gen')")

        await menu(b, 'cal-nostr-pub')
        await b.until("!!document.querySelector('#n52-go')")
        await b.js("document.querySelector('#n52-t').value='Meetup'; document.querySelector('#n52-l').value='Library'; document.querySelector('#n52-go').click(); 1")
        await b.until("__sentEvents.some(e=>e.kind===31923)")
        ev = await b.js("__sentEvents.find(e=>e.kind===31923)")
        tags = {t[0]: t[1] for t in ev["tags"]}
        assert tags["title"] == "Meetup" and tags["location"] == "Library", ev
        assert int(tags["end"]) - int(tags["start"]) == 3600, ev
        assert not await b.js("__errors"), await b.js("__errors")
    _run(check)

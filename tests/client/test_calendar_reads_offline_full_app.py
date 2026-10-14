"""OFFLINE, THE CALENDAR STILL SHOWS YOUR EVENTS -- at once, not after the server times out.

"Notes, Email, Music, etc must work offline! we can't have the OS broken when server issues happen" ...
"and check calendar too". A calendar is encrypted with the server-held storage key and served by
/api/calendar, so the desktop's local relay cannot hold it; what keeps it usable in an outage is the
copy calendar.js saves on the device. Two phases in ONE browser profile: online, the real client opens
the calendar and an event is on the grid; then a reload with no network (navigator.onLine false,
/api/calendar unreachable, every relay socket dead) -- the event must still be on the grid, marked as
the saved copy, within a few seconds of opening the screen.
"""
import asyncio
import time
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
localStorage.setItem('pc_cal_nostr','0');
const dead=localStorage.getItem('__dead')==='1';
if(dead){
  Object.defineProperty(Navigator.prototype,'onLine',{configurable:true,get:()=>false});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
}
const realFetch=window.fetch;
const d=new Date(), ymd=d.getFullYear()+String(d.getMonth()+1).padStart(2,'0')+'15';
const ics='BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:ferry-1\r\nDTSTART:'+ymd+'T090000Z\r\nDTEND:'+ymd+'T100000Z\r\nSUMMARY:Ferry to the island\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n';
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);
  if(!u.pathname.startsWith('/api/calendar/') && !u.pathname.startsWith('/api/auth/'))return realFetch(url,opts);
  if(dead)return Promise.reject(new TypeError('Failed to fetch'));
  const reply=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(u.pathname.startsWith('/api/auth/'))return realFetch(url,opts);
  if(u.pathname.endsWith('/config'))return reply({enabled:true,url:'https://x/caldav/',username:'me'});
  if(u.pathname.endsWith('/calendars'))return reply({calendars:[{id:'home',displayname:'Home',kind:'VCALENDAR'}]});
  if(u.pathname.endsWith('/items'))return reply({items:[{uid:'ferry-1',href:'ferry-1.ics',ics}]});
  return reply({ok:true});};
'''

HAS = "/Ferry to the island/.test((document.querySelector('#feed')||{}).textContent||'')"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_the_calendar_shows_its_events_with_no_network(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchView('calendar')")
        await b.until(HAS)
        await asyncio.sleep(1.5)                        # the device copy is written behind the paint

        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC')
        await b.until('!!__PC.me()')
        t0 = time.monotonic()
        await b.js("__PC.switchView('calendar')")
        got['secs'] = None
        while time.monotonic() - t0 < 20:
            if await b.js(HAS):
                got['secs'] = time.monotonic() - t0
                break
            await asyncio.sleep(0.1)
        got['text'] = await b.js("((document.querySelector('#feed')||{}).textContent||'').slice(0,400)")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['secs'] is not None, ('offline, the calendar lost the events it had shown online', got['text'])
    assert got['secs'] < 5, ('offline, the calendar took %.1fs to show the saved events' % got['secs'], got['text'])
    assert 'saved' in got['text'] or 'offline' in got['text'], ('the saved copy is not said to be one', got['text'])

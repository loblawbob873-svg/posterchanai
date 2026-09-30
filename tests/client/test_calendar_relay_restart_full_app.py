"""'why is my phone showing 0 calendar events' -- a read that could not reach the relay must never draw
an empty calendar.

Measured: a deploy restarts the relay, and for the ~30s it is not listening the server answered
`GET /api/calendar/calendars` with 200 `{"calendars": []}`. The APK drew no events (and saved that
as its offline copy) for an account with 724. The server now answers 503 (tests/test_calendar.py);
this drives the SHIPPED calendar.js in the real bundle through what the client must then do:

  * the calendar list failing keeps the calendars and events already on screen, with a note and the
    "offline copy" pill -- and the note no longer REPLACES the grid it describes;
  * one calendar's events failing keeps that calendar's events instead of blanking it;
  * a good read afterwards clears the note.
At desktop and phone width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
localStorage.setItem('pc_cal_nostr','0');
const day=new Date(), date=[day.getFullYear(),String(day.getMonth()+1).padStart(2,'0'),String(day.getDate()).padStart(2,'0')].join('');
const ics=['BEGIN:VCALENDAR','VERSION:2.0','BEGIN:VEVENT','UID:dentist','DTSTART;VALUE=DATE:'+date,'SUMMARY:Dentist','END:VEVENT','END:VCALENDAR',''].join('\r\n');
window.__down='none';
const calFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=new URL(String(url),location.href);
 const reply=(body,status=200)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
 const down=()=>reply({detail:'Could not reach your calendars just now — try again.'},503);
 if(u.pathname==='/api/calendar/config')return reply({enabled:true});
 if(u.pathname==='/api/calendar/calendars')return __down==='calendars'?down():reply({calendars:[{id:'main',displayname:'Main'}]});
 if(u.pathname==='/api/calendar/items')return __down==='items'?down():reply({items:[{uid:'dentist',ics,component:'VEVENT'}]});
 return calFetch(url,opts);
};
'''

STATE = r"""(()=>{const f=document.getElementById('feed');
  return {event:!!f.querySelector('.cal-edit[data-uid=dentist]'), note:(f.querySelector('.cal-stale')||{}).textContent||'',
          pill:!!f.querySelector('.cal-pending.cached'), err:!!f.querySelector('.ws-err'),
          wide:f.scrollWidth<=f.clientWidth+1}})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,height,mobile', [(1280, 900, False), (390, 844, True)])
def test_a_relay_that_cannot_answer_keeps_the_calendar_on_screen(width, height, mobile):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': mobile})
        await desktop.login(b)
        await b.js("__PC.switchView('calendar')")
        await b.until("!!document.querySelector('#feed .cal-edit[data-uid=dentist]')")
        first = await b.js(STATE)
        assert first['event'] and not first['note'] and not first['pill'], first

        # The relay restarts: the calendar LIST cannot be read.
        await b.js("__down='calendars'; PCCalendar.reload()")
        await b.until("!!document.querySelector('#feed .cal-stale')")
        st = await b.js(STATE)
        assert st['event'], ('the calendar was blanked by a read that failed', st)
        assert 'saved calendar' in st['note'] and st['pill'] and not st['err'], st
        assert st['wide'], st

        # One calendar's EVENTS cannot be read: that calendar keeps what it had.
        await b.js("__down='items'; PCCalendar.reload()")
        await b.until("!!document.querySelector('#feed .cal-pending.cached')")
        st = await b.js(STATE)
        assert st['event'] and st['pill'], ('a calendar whose events failed to load was emptied', st)

        # Back to normal: fresh, and it says so.
        await b.js("__down='none'; PCCalendar.reload()")
        await b.until("!document.querySelector('#feed .cal-pending.cached') && !document.querySelector('#feed .cal-stale')")
        assert (await b.js(STATE))['event']
    asyncio.run(desktop.with_browser('online', '', check, BOUNDARIES))

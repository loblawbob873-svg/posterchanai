"""APPS THAT OPEN AT LOGIN FILL IN EVEN WHEN ANOTHER WINDOW IS IN FRONT.

Reported 2026-10-30 on the Windows app: "telegram still not good at autostarting on windows app, stuck in a connecting
loop", "and messages shows 0 messages", "restarting the app solves it". The plain app opens its startup apps one every
500 ms (os.js runStartupApps), so by the time Telegram's first answer or Messages' history arrives another window is
in front -- and both only painted while they were the window in front. Whether they filled in was a race against the
server, which is why a restart "fixed" it. This opens them the way runStartupApps does, with a server slower than the
spacing, and reads what the two windows show while they are still behind the others. Real bundle, real desktop.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED
from tests.client.test_telegram_client_full_app import FAKE


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Telegram signed in, and its server slower than the startup spacing.
SLOW_TG = FAKE.replace("window.__tg = { posts:[], files:[], state:'none', notes:[] };",
                       "window.__tg = { posts:[], files:[], state:'ready', notes:[] };") \
              .replace("if(path.startsWith('/status')) return __j(",
                       "if(path.startsWith('/status')) return new Promise(r=>setTimeout(r,1500)).then(()=>__j(") \
              .replace("state:__tg.state, me:{name:'Me'}});", "state:__tg.state, me:{name:'Me'}}));", 1)

# Calendar's server, slower than the spacing too ("i see spinning loading thing on calendar", 2026-10-30).
SLOW_CAL = r"""
(()=>{const f0=window.fetch, j=o=>new Promise(r=>setTimeout(r,1500)).then(()=>new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}}));
 window.fetch=function(url,opts){const u=String(url);
   if(u.includes('/api/calendar/config')) return j({enabled:true});
   if(u.includes('/api/calendar/calendars')) return j({calendars:[{id:'home',name:'Home',color:'#3b82f6'}]});
   if(u.includes('/api/calendar/items')) return j({items:[]});
   return f0.apply(this,arguments);};})();
"""

# What each window shows, wherever it is (in front or parked behind another).
SHOWN = r"""(()=>{const wins=[...document.querySelectorAll('.osw')];
  const tg=wins.find(w=>w.querySelector('.tg-app')), dm=wins.find(w=>w.querySelector('#dm-rows')), cal=wins.find(w=>w.querySelector('.cal-wrap'));
  const front=(document.querySelector('.osw.focused .osw-title')||{}).textContent||'';
  return {front:front.trim(),
    tg: tg ? {connecting:/CONNECTING/.test(tg.textContent), chats:tg.querySelectorAll('.tg-dialog').length} : null,
    dm: dm ? {rows:dm.querySelectorAll('#dm-rows .dm-peer').length, text:dm.textContent.replace(/\s+/g,' ').slice(0,160)} : null,
    cal: cal ? {spinner:!!cal.querySelector('.cal-wrap .spinner'), days:cal.querySelectorAll('.cal-day').length} : null,
    calStray:[...document.querySelectorAll('.cal-wrap')].filter(w=>w.closest('.osw')!==cal).length,
    // nothing of Telegram's may be drawn into another app's window
    stray:[...document.querySelectorAll('.tg-app')].filter(a=>!a.closest('.osw')||a.closest('.osw')!==tg).length};})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_telegram_and_messages_opened_at_login_fill_in_behind_the_last_window():
    got = {}

    async def check(b):
        await b.js("localStorage.setItem('__relayMs','1500')")
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.js(SEED + '(5)')
        await b.js("PCOS.enter();true")
        await b.until("document.body.classList.contains('os-on')")
        # runStartupApps on the plain app: routeView(v) at 400 + i*500 ms
        views = ['tg', 'messages', 'calendar', 'notifications', 'social']
        await b.js("(%s).forEach((v,i)=>setTimeout(()=>{try{PCOS.routeView(v)}catch(_){}},400+i*500));true" % json.dumps(views))
        await asyncio.sleep(9)
        got['shown'] = await b.js(SHOWN)

    asyncio.run(desktop.with_browser('online', '', check, RELAY + SLOW_TG + SLOW_CAL))
    s = got['shown']
    assert s['tg'] and s['dm'], ('the startup windows did not open', s)
    assert all(x not in s['front'] for x in ('Telegram', 'Messages', 'Calendar')), ('the test must leave them behind', s)
    assert not s['tg']['connecting'] and s['tg']['chats'] == 2, ('Telegram opened at login is stuck connecting', s)
    assert s['dm']['rows'] >= 1, ('Messages opened at login shows no conversations', s)
    assert s['stray'] == 0, ('Telegram drew itself into another window', s)
    assert s['cal'] and not s['cal']['spinner'] and s['cal']['days'] >= 28, ('Calendar opened at login is stuck on its spinner', s)
    assert s['calStray'] == 0, ('Calendar drew itself into another window', s)

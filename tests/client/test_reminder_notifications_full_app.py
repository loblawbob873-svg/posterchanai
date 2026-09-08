"""Actual client document, login, settings controls and reload; network boundaries are private."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
import httpx
import pytest
import websockets
from tests.client.test_effects_full_app import Browser, Handler, INIT

EXTRA=r'''
window.__publishOK=true;window.__history=[];window.__historyOK=true;window.__historyRequests=0;
const historyFetch=window.fetch;
window.fetch=async(url,opts)=>String(url).includes('/api/auth/reminder-notifications')
 ?(__historyRequests++,new Response(JSON.stringify({items:__history}),{status:__historyOK?200:503,headers:{'Content-Type':'application/json'}})):historyFetch(url,opts);
'''

async def run(width):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-reminder-history-') as profile:
        proc=subprocess.Popen(['/opt/google/chrome/chrome','--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile,'DevToolsActivePort').exists():break
                await asyncio.sleep(.1)
            port=Path(profile,'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:pages=(await h.get(f'http://127.0.0.1:{port}/json')).json()
            async with websockets.connect(next(p for p in pages if p.get('type')=='page')['webSocketDebuggerUrl'],max_size=20_000_000) as ws:
                b=Browser(ws);await b.call('Page.enable');await b.call('Network.enable')
                await b.call('Network.setBlockedURLs',{'urls':['https://*','wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<600})
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':INIT+EXTRA})
                await b.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/client'})
                await b.until("document.body?.classList.contains('guest')")
                await b.js("document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(new Uint8Array(32).fill(1));document.querySelector('#btn-nsec-login').click()")
                await b.until('!!window.__PC?.me()')
                await b.until('document.readyState==="complete"')
                await b.js("__PC.switchView('ai')")
                await b.until("__sockets.some(s=>s.url.includes('/api/ws/chat/'))")
                await b.js("__PC.setNotificationPreference('reminders',false)")
                await b.js("window.__reminder={type:'reminder',reminder_id:7,due_at:'2026-09-08T12:00:00Z',delivered_at:'2026-09-08T12:00:01Z',content:'Fixture calendar appointment',route:'calendar'};__sockets.findLast(s=>s.url.includes('/api/ws/chat/')).fire('message',__reminder)")
                assert await b.js("!document.querySelector('#reminderOverlay')")
                await b.js("__PC.switchView('notifications')")
                await b.until("document.querySelectorAll('#feed .reminder-notif').length===1")
                assert await b.js("document.querySelector('#feed .reminder-notif').textContent.includes('Fixture calendar appointment')")
                await b.js("__sockets.findLast(s=>s.url.includes('/api/ws/chat/')).fire('message',__reminder)")
                assert await b.js("__PC.notifItems(100).filter(x=>x.type==='reminder').length===1")
                await b.js("document.querySelector('#feed .reminder-notif').click()")
                await b.until("__PC.isView('calendar')")
                assert not await b.js("__PC.isView('ai')")
                await b.js("__PC.setNotificationPreference('reminders',true)")
                await b.js("window.__realNow=Date.now;window.__offset=31000;Date.now=()=>__realNow()+__offset;window.__history=[{...__reminder,reminder_id:10,due_at:new Date(__realNow()+1000).toISOString(),delivered_at:new Date(__realNow()+1000).toISOString(),content:'Arrived outside AI'}];window.__beforeHistory=__historyRequests;__PC.notifItems(100)")
                await b.until("__historyRequests>__beforeHistory && !!document.querySelector('#reminderOverlay')")
                assert await b.js("__PC.isView('calendar')")
                await b.js("document.querySelector('#reminderDismiss').click();__offset+=31000;window.__beforeHistory=__historyRequests;__PC.notifItems(100)")
                await b.until("__historyRequests>__beforeHistory")
                await asyncio.sleep(.1)
                assert await b.js("!document.querySelector('#reminderOverlay')")
                await b.js("__PC.switchView('ai')")
                await b.until("__sockets.some(s=>s.url.includes('/api/ws/chat/')&&s.readyState===1)")
                await b.js("__PC.setNotificationPreference('reminders',true)")
                await b.js("window.__nextReminder={...__reminder,reminder_id:9,content:'Another calendar appointment'};__sockets.findLast(s=>s.url.includes('/api/ws/chat/')).fire('message',__nextReminder)")
                await b.until("!!document.querySelector('#reminderOverlay')")
                await b.js("document.querySelector('#reminderDismiss').click();__sockets.findLast(s=>s.url.includes('/api/ws/chat/')).fire('message',__nextReminder)")
                assert await b.js("!document.querySelector('#reminderOverlay') && __PC.notifItems(100).filter(x=>x.type==='reminder').length===3")
                # Failed history request on reload must retain the received reminder locally.
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':'window.__historyOK=false;'})
                await b.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/client'})
                await b.until('!!window.__PC?.me()')
                await b.js("__PC.switchView('notifications')")
                await b.until("document.querySelectorAll('#feed .reminder-notif').length===3")
                assert await b.js("document.querySelector('#feed .reminder-notif').dataset.route==='calendar'")
                assert await b.js("(()=>{const r=document.querySelector('#feed .reminder-notif').getBoundingClientRect();return r.width>100&&r.right<=innerWidth+2})()")
                assert await b.js("!document.querySelector('#reminderOverlay')")
                # Actual logout reloads the document. A newly fetched reminder from before the next
                # login belongs in history, even though that account never saw its live frame.
                old={'reminder_id':11,'due_at':'2026-09-08T10:00:00Z','delivered_at':'2026-09-08T10:00:01Z','content':'While signed out','route':'calendar'}
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':'window.__historyOK=true;window.__history='+json.dumps([old])+';'})
                await b.js("document.querySelector('#btn-logout').click()")
                await b.until("document.body?.classList.contains('guest') && document.querySelector('#btn-logout')?.textContent==='Log in'")
                await b.js("document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(new Uint8Array(32).fill(1));document.querySelector('#btn-nsec-login').click()")
                await b.until("!!window.__PC?.me() && !document.body.classList.contains('guest')")
                await b.js("__PC.switchView('notifications')")
                await b.until("document.querySelectorAll('#feed .reminder-notif').length===4")
                assert await b.js("!document.querySelector('#reminderOverlay')")
                assert not await b.js('__errors')
        finally:
            proc.terminate();proc.wait(timeout=10);server.shutdown();server.server_close()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('width',[1440,390])
def test_calendar_reminder_actual_ws_history_click_and_offline_reload(width):
    asyncio.run(run(width))

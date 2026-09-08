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
window.__publishOK=true;
const OriginalAudio=window.AudioContext;window.__audioCount=0;
if(OriginalAudio)window.AudioContext=new Proxy(OriginalAudio,{construct(target,args){__audioCount++;return Reflect.construct(target,args)}});
'''

async def run(width):
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='pc-notification-settings-') as profile:
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
                await b.js("__PC.switchView('settings')")
                await b.until("!!document.querySelector('.us-tab[data-tab=notifications]')")
                await b.js("document.querySelector('.us-tab[data-tab=notifications]').click()")
                await b.until("document.querySelector('[data-pane=notifications]').classList.contains('active')")
                assert await b.js("document.querySelectorAll('[data-notification-type]').length===10")
                await b.js("document.querySelector('[data-notification-type=email]').click()")
                assert await b.js("!__PC.notificationAllowed('email')")
                await b.until("__published.some(e=>e.kind===30078&&JSON.parse(e.content).notificationPrefs?.email===false)")
                await b.js("const s=document.querySelector('#us-notification-sound');s.value='off';s.dispatchEvent(new Event('change'));window.__beforeAudio=__audioCount;document.querySelector('#us-notification-preview').click()")
                assert await b.js('__audioCount===__beforeAudio')
                await b.js("document.querySelector('#us-notification-sound').value='soft';document.querySelector('#us-notification-sound').dispatchEvent(new Event('change'));document.querySelector('#us-notification-preview').click()")
                assert await b.js('__audioCount===__beforeAudio+1')
                # The pane must fit the viewport; its tabs may scroll independently on narrow phones.
                assert await b.js("(()=>{const r=document.querySelector('[data-pane=notifications]').getBoundingClientRect();return r.width>100&&r.right<=innerWidth+2})()")
                await b.js("window.__savedPrefs=JSON.parse(localStorage.getItem('pc_notification_prefs:'+__PC.me().pubkey));window.__events=__published.filter(e=>e.kind===30078).slice(-1)")
                # Save the actual signed event for replay after document reload; never publish externally.
                events=await b.js('__events')
                await b.call('Page.addScriptToEvaluateOnNewDocument',{'source':'window.__events='+json.dumps(events)+';'})
                await b.call('Page.navigate',{'url':f'http://127.0.0.1:{server.server_port}/client'})
                await b.until('!!window.__PC?.me()')
                await b.js("__PC.switchView('settings')")
                await b.until("!!document.querySelector('[data-notification-type=email]')")
                assert await b.js("!document.querySelector('[data-notification-type=email]').checked")
                assert await b.js("document.querySelector('#us-notification-sound').value==='soft'")
                assert not await b.js('__errors')
        finally:
            proc.terminate();proc.wait(timeout=10);server.shutdown();server.server_close()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('width',[1440,390])
def test_notification_tab_actual_app_controls_reload_and_mobile(width):
    asyncio.run(run(width))

"""A follow is a notification in EVERY desktop window, or in none of them.

Reported on PosterChanOS: "a notification came up from <npub> ('Technical Debt'), then I clicked
Notifications and it was missing." The only event that person had addressed to the owner was a
kind-3 contact list saved the evening before, while the desktop was closed -- so the row was
"followed you". The taskbar bell's flyout (`?pcpopup=noti`) listed it; the Notifications window
(`?pcwin=notifications`, its own renderer since shell start) did not.

Each desktop window is its own renderer with its own copy of the follow pins (`pc_follow_seen`), and
each decided "is this a NEW follow?" from its own timing:

  * a renderer whose follower seed had already finished when a relay replayed that old kind-3 (the
    backlog of a relay that connected late, re-armed after EOSE) took it for a LIVE follow and pinned
    it at NOW -- above the epoch, so "All" shows it, the bell counts it and a toast pops;
  * a renderer whose seed contained it pinned it as history (at/below the epoch) -- hidden from "All";
  * and every renderer wrote its WHOLE in-memory map back to localStorage, so the last writer's
    verdict replaced the others' in storage while each open renderer kept its own in memory -- and a
    window opened later (the flyout) read whichever verdict happened to be stored.

Real client bundle, real login and real renderers in one Chrome profile (shared localStorage, the
same as the Electron shell); only the relay socket and the instance HTTP are fixtures.
"""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_effects_full_app import Browser, INIT as NETWORK_FIXTURE

NOW = int(time.time())

# Runs before the network fixture on every page: an install that has been tracking follows for two
# months (the reporting desktop's epoch is long before the follow).
PRE = r'''
if(!localStorage.getItem('pc_notif_epoch'))localStorage.setItem('pc_notif_epoch',String(%d-60*86400));
''' % NOW

# Runs after the network fixture: the relay's follower kind-3s, signed for real, and a record of every
# REQ so a test can deliver an event on an already-open subscription (what a relay does live, and what
# a late relay's backlog looks like after the client's EOSE).
RELAY = r'''
window.__reqs=[];
const _fxSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='REQ')__reqs.push({sock:this,id:m[1],filters:m.slice(2)});}catch(_){}return _fxSend.call(this,raw);};
const NOW=%d;
window.__fx=null;
function __fxEvents(){
  if(!window.NostrTools)return null;
  if(!__fx){
    const NT=NostrTools,me=NT.getPublicKey(new Uint8Array(32).fill(1));
    const mk=(n,at)=>NT.finalizeEvent({kind:3,created_at:at,tags:[['p',me]],content:''},new Uint8Array(32).fill(n));
    __fx={me,
      old:mk(2,NOW-30*86400),     // followed a month ago: history in every window
      late:mk(3,NOW-15*3600),     // followed last night while the desktop was closed ("Technical Debt")
      race:mk(5,NOW-5),           // followed just now: one window met it in its seed, another live
      live:mk(4,NOW+5)};          // follows while the windows are open: a notification in every window
  }
  return __fx;
}
Object.defineProperty(window,'__events',{configurable:true,get(){
  const f=__fxEvents();if(!f)return [];
  const out=[f.old];
  if(!window.__withholdLate)out.push(f.late,f.race);
  // Once published, the relay holds it for EVERY page that asks -- a window opened afterwards (the flyout)
  // gets it from its own REQ, as from a real relay; page-local only, it depended on another window's cache write.
  if(window.__liveSent||localStorage.getItem('__fx_liveSent'))out.push(f.live);
  return out;
}});
window.__deliver=(name)=>{const ev=__fxEvents()[name];let n=0;
  for(const r of __reqs){if(r.sock.readyState!==1)continue;
    if(r.filters.some(f=>f.kinds&&f.kinds.includes(3)&&f['#p'])){r.sock.fire('message',['EVENT',r.id,ev]);n++;}}
  return n;};
window.__followRows=()=>{try{return (__PC.notifItems(60)||[]).filter(e=>e&&e.kind===3).map(e=>e.pubkey).sort();}catch(_){return null;}};
''' % NOW


async def _page(port, server_port, route, extra=''):
    async with httpx.AsyncClient(trust_env=False) as h:
        target = (await h.put(f'http://127.0.0.1:{port}/json/new?about:blank')).json()
    ws = await websockets.connect(target['webSocketDebuggerUrl'], max_size=20_000_000)
    b = Browser(ws)
    await b.call('Page.enable')
    await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
    await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': PRE + extra})
    await b.call('Page.addScriptToEvaluateOnNewDocument', {'source': NETWORK_FIXTURE + desktop.OFFLINE.replace('MODE', 'online') + RELAY})
    await b.call('Page.navigate', {'url': f'http://127.0.0.1:{server_port}/index.html' + route})
    await b.until('!!window.__PC && !!window.PCOS')
    return b, ws


async def _settled(b):
    """The follower seed has run and the live follows subscription has reached EOSE."""
    await b.until("__reqs.some(r=>r.filters.some(f=>f.kinds&&f.kinds.length===1&&f.kinds[0]===3&&f['#p']&&f.limit===150))")
    await asyncio.sleep(1.5)


async def scenario():
    import tempfile, subprocess, threading
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(('127.0.0.1', 0), desktop.BundleHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sockets = []
    with tempfile.TemporaryDirectory(prefix='pc-follow-verdict-') as profile, tempfile.TemporaryFile(mode='w+b') as log:
        proc = subprocess.Popen(['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
                                 '--window-size=1440,1000', '--remote-debugging-port=0', '--user-data-dir=' + profile,
                                 'about:blank'], stdout=log, stderr=log)
        try:
            port = await desktop.wait_browser_port(proc, Path(profile, 'DevToolsActivePort'), log)
            await desktop.wait_browser_target(proc, port, log)
            sp = server.server_port
            # The desktop SHELL renderer. Its relay is the one that connected late: last night's kind-3
            # is not in its answer to the follower seed, and arrives afterwards as backlog.
            shell, ws = await _page(port, sp, '', 'window.__withholdLate=true;')
            sockets.append(ws)
            await desktop.login(shell)
            await _settled(shell)
            # The late relay's backlog reaches the shell after its seed and its EOSE -- before any other
            # window has an opinion about that follower, so nothing stored can correct the shell.
            assert await shell.js("__deliver('late')") >= 1
            await asyncio.sleep(.5)
            # The Notifications WINDOW: its seed holds every follower.
            win, ws = await _page(port, sp, '?pcwin=notifications')
            sockets.append(ws)
            await win.until('!!__PC.me()')
            await _settled(win)
            # A follow made a moment ago, which the window has already met in its seed, now reaches
            # the shell live. Whichever window decided first, no window may hold a different answer.
            assert await shell.js("__deliver('race')") >= 1
            await asyncio.sleep(1.5)
            # Ten minutes later the owner clicks the taskbar bell: a NEW renderer.
            fly, ws = await _page(port, sp, '?pcpopup=noti')
            sockets.append(ws)
            await fly.until("!!__PC.me() && !!document.querySelector('#os-noti')")
            await _settled(fly)
            late = await shell.js('__fx.late.pubkey')
            race = await shell.js('__fx.race.pubkey')
            old = await shell.js('__fx.old.pubkey')
            verdict = {
                'shell': await shell.js('__followRows()'),
                'notifications window': await win.js('__followRows()'),
                'bell flyout': await fly.js('__followRows()'),
            }
            fly_dom = await fly.js("[...document.querySelectorAll('#os-noti .notif.follow')].map(x=>x.dataset.prof)")
            win_dom = await win.js("[...document.querySelectorAll('#feed .notif.follow')].map(x=>x.dataset.prof)")
            print('late-backlog verdicts', json.dumps(verdict), 'flyout DOM', fly_dom, 'window DOM', win_dom)
            # ONE verdict, in every window -- the flyout's rows and the Notifications window's rows.
            assert verdict['shell'] == verdict['notifications window'] == verdict['bell flyout'], verdict
            assert sorted(fly_dom) == sorted(win_dom), (fly_dom, win_dom)
            # ...and it is the documented one: a follow that predates this session is history, never
            # a "followed you" row in All (it is still in the Follows tab).
            assert late not in verdict['bell flyout'] and old not in verdict['bell flyout'], verdict

            # A follow made while the windows are OPEN is a notification -- in every one of them,
            # including a window that judged nothing itself because the event reached it first
            # through another renderer's pin.
            await shell.js("window.__liveSent=true;localStorage.setItem('__fx_liveSent','1')")
            await win.js('window.__liveSent=true')
            assert await shell.js("__deliver('live')") >= 1
            await asyncio.sleep(.5)
            assert await win.js("__deliver('live')") >= 1
            # Wait for the follow to be RECORDED, not a fixed 1.5 s: under the deploy gate's load the
            # pin had not landed yet and every window read [] (deploy 224's gate, 2026-10-14). The
            # assertion below is unchanged -- every window, including a fresh flyout, holds one answer.
            await shell.until("__followRows().includes(__fx.live.pubkey)")
            await win.until("__followRows().includes(__fx.live.pubkey)")
            fly2, ws = await _page(port, sp, '?pcpopup=noti')
            sockets.append(ws)
            await fly2.until("!!__PC.me() && !!document.querySelector('#os-noti')")
            await _settled(fly2)
            # A NEW renderer lists a follow once its OWN subscription has delivered it -- the stored pin says
            # when it happened, not that it is on screen yet. Wait for it to be listed (a flyout that never
            # lists it still fails here), then compare every window's answer.
            await fly2.until("__followRows().includes(__fx.live.pubkey)")
            live = await shell.js('__fx.live.pubkey')
            verdict = {
                'shell': await shell.js('__followRows()'),
                'notifications window': await win.js('__followRows()'),
                'bell flyout': await fly2.js('__followRows()'),
            }
            print('live verdicts', json.dumps(verdict))
            assert verdict['shell'] == verdict['notifications window'] == verdict['bell flyout'] == [live], verdict
            await win.until(f"!!document.querySelector('#feed .notif.follow[data-prof=\"{live}\"]')")
            assert await fly2.js(f"!!document.querySelector('#os-noti .notif.follow[data-prof=\"{live}\"]')")
            # Everybody's pin survived everybody else's writes.
            pins = await fly2.js("JSON.parse(localStorage.getItem('pc_follow_seen')||'{}')")
            assert {old, late, race, live} <= set(pins), pins
        finally:
            for ws in sockets:
                try:
                    await ws.close()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()
            server.server_close()


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_follow_gets_one_verdict_in_every_desktop_window():
    asyncio.run(scenario())

"""A route delivered to a PosterChanOS window is performed THERE, once -- for every kind of route there is.

2026-10-26, the laptop: "texts is stealing focus from any other window including notifications, keeps loading, can't
type anywhere else". A Texts notification's route reached the open Texts window, the window handed it back to the
desktop, and the desktop -- seeing that window open -- broadcast it to the window again: ~190 times a second, each
ending in window.focus(). Two guards, both proven here in the real bundled client:

* NO ROUTE KIND CALLS BACK INTO THE DESKTOP. The window's desktop is a Proxy that records ANY call and answers it the
  way the real desktop answered Texts -- by broadcasting the same route to the window again. Every route the window's
  listener handles (oswin.js ch.onmessage) is delivered once; a kind that calls the desktop is a loop.
* A STORM IS ONE ACTION AND ONE FOCUS (oswin.js's breaker), whatever causes the next one: 200 identical deliveries.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


HEX = 'ab' * 32
# (window view, route arg) for every branch of the route listener in oswin.js.
ROUTES = [('home', ''), ('global', ''), ('trending', ''), ('texts', '+15550300'), ('tg', '-100123'),
          ('messages', HEX), ('doc:search', 'cats'), ('meme', HEX), ('ai', HEX), ('__music', ''),
          ('doc:post:' + HEX, ''), ('doc:prof:' + HEX, ''), ('doc:trace:' + HEX, ''), ('notes', ''), ('calendar', '')]

EXTRA = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
         "window.pcShell.windowContext={role:'app',view:'texts'};window.pcShell.backgroundOwner=false;")

HARNESS = r"""(() => {
  window.__desk = { calls: [] };
  let focuses = 0; const wf = window.focus; window.focus = function(){ focuses++; return wf.apply(this, arguments); };
  window.__focuses = () => focuses;
  // The desktop: whatever it is asked, it answers the way the real one answered Texts -- the same route, again.
  const deskPC = new Proxy({}, { get: (_, name) => (typeof name !== 'string' || name === 'then') ? undefined
    : (...a) => { __desk.calls.push(name); const s = window.__PC_WIN_STATE__ || {};
        const ch = new BroadcastChannel('pc-os-window-route-v1'); ch.postMessage(window.__lastRoute); ch.close(); return true; } });
  const deskWin = new Proxy({ __PC: deskPC }, { get: (t, k) => k in t ? t[k] : undefined });
  PCOSWin.desktop = () => deskWin;
  window.__deliver = (view, arg) => { window.__PC_WIN_STATE__ = { ...(window.__PC_WIN_STATE__ || {}), view };
    window.__lastRoute = arg ? { view, arg } : { view };
    const ch = new BroadcastChannel('pc-os-window-route-v1'); ch.postMessage(window.__lastRoute); ch.close(); };
  return true;
})()"""


async def _window(browser):
    await desktop.login(browser)
    url = await browser.js("location.origin+'/index.html?pcwin=texts'")
    await browser.call('Page.navigate', {'url': url})
    await browser.until("!!window.PCSms && !!window.PCOSWin && PCOSWin.isWindow()")
    await browser.js(HARNESS)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_route_kind_hands_its_route_back_to_the_desktop():
    bad = {}

    async def check(browser):
        await _window(browser)
        for view, arg in ROUTES:
            got = await browser.js("""(async () => { __desk.calls.length = 0; const f0 = __focuses();
                __deliver(%s, %s); await new Promise(r => setTimeout(r, 700));
                return { calls: __desk.calls.slice(0, 5), n: __desk.calls.length, focuses: __focuses() - f0 }; })()"""
                                   % (json.dumps(view), json.dumps(arg)))
            if got['n'] or got['focuses'] > 2:
                bad[view] = got
            await asyncio.sleep(1.6)                                  # past the breaker's window: next kind is fresh
    asyncio.run(desktop.with_browser('online', '?pcwin=texts', check, EXTRA))
    assert not bad, ("a route delivered to a window called back into the desktop (a loop) or kept taking focus", bad)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_storm_of_one_route_is_one_action_and_one_focus():
    async def check(browser):
        await _window(browser)
        got = await browser.js("""(async () => {
            PCOSWin.desktop = () => null;                               // nothing loops back: the storm is all from outside
            window.__PC_WIN_STATE__ = { ...(window.__PC_WIN_STATE__ || {}), view: 'texts' };
            const s0 = PCOSWin.routeStats(), f0 = __focuses();
            for (let i = 0; i < 200; i++) { const ch = new BroadcastChannel('pc-os-window-route-v1');
              ch.postMessage({ view: 'texts', arg: '+15550400' }); ch.close(); }
            await new Promise(r => setTimeout(r, 1000));
            const s1 = PCOSWin.routeStats();
            return { performed: s1.performed - s0.performed, dropped: s1.dropped, focuses: __focuses() - f0,
                     open: PCSms._state().open === PCSms._key('+15550400') };
        })()""")
        assert got['performed'] == 1, ("200 copies of one route were performed more than once", got)
        assert got['focuses'] <= 1, ("a storm of one route took focus again and again", got)
        assert got['dropped'] >= 150, got
        assert got['open'], ("the route itself was not performed", got)
        # ...and the same route asked for again AFTER the storm is a new request, performed.
        await asyncio.sleep(1.7)
        again = await browser.js("""(async () => { const s0 = PCOSWin.routeStats().performed;
            const ch = new BroadcastChannel('pc-os-window-route-v1'); ch.postMessage({ view: 'texts', arg: '+15550400' }); ch.close();
            await new Promise(r => setTimeout(r, 400)); return PCOSWin.routeStats().performed - s0; })()""")
        assert again == 1, ("a later, deliberate request for the same route was dropped as part of the storm", again)
    asyncio.run(desktop.with_browser('online', '?pcwin=texts', check, EXTRA))

"""Super with Start open CLOSES it -- it must not close and reopen.

Reported as "pressing super to open then super to close opened it again". Wayfire's own event stream
on the desk, one tap with Start open:

    50.806 / 50.912  Super down / up
    50.928           view-focused  PosterChan Desktop     <- _bareSuper raised the shell
    50.991           view-unmapped PosterChan Popup       <- the popup closes on blur
    51.316           view-mapped   PosterChan Popup       <- then the toggle saw "closed" and OPENED

`_bareSuper(true)` (the compositor's pc:start) raised the desktop BEFORE toggling. That raise exists
for the in-page menu, which lives inside the desktop and needs its keyboard; the Start POPUP is its own
window that takes focus when it maps, and raising the desktop under an OPEN one blurs it shut, so the
toggle -- decided in main by whether a popup exists -- always found none and opened a new one.

The fixture is that compositor in two rules: focusing the desktop closes an open popup (blur), and
pcPopup.toggle closes the popup if one exists, else opens one.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

COMPOSITOR = r'''
window.__wm = {popup: false, log: [], listeners: []};
window.pcWM = {
  windows: async () => [{id: 970, app: 'place.poster.desktop', title: 'PosterChan Desktop', rect: {x:0,y:0,width:1440,height:1000}}],
  onEvent: (fn) => { __wm.listeners.push(fn); return () => {}; },
  focus: async (id) => { __wm.log.push('focus:' + id); if (__wm.popup) { __wm.popup = false; __wm.log.push('blur-closed'); } return true; },
  shellFront: async () => true, launch: async () => ({pid: 1}),
};
const _pp = window.pcPopup || {};
window.pcPopup = Object.assign({}, _pp, {
  toggle: async (kind) => { __wm.popup = !__wm.popup; __wm.log.push('toggle:' + kind + '->' + (__wm.popup ? 'open' : 'closed')); return __wm.popup; },
  close: async () => { __wm.popup = false; __wm.log.push('close'); return true; },
});
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _tick(b, payload):
    await b.js("__wm.listeners.forEach(fn => { try { fn({name: 'tick', payload: %r}); } catch (_) {} })" % payload)
    await asyncio.sleep(0.8)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_super_with_start_open_closes_it_and_does_not_reopen_it():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && __wm.listeners.length > 0")
        await _tick(b, 'pc:start')                       # Super: open
        assert await b.js("__wm.popup"), await b.js("__wm.log")
        await b.js("__wm.log.length = 0")
        await _tick(b, 'pc:start')                       # Super again: close
        log = await b.js("__wm.log")
        assert not await b.js("__wm.popup"), ("Super with Start open reopened it", log)
        assert 'blur-closed' not in log, ("the desktop was raised under the open menu, which shuts it", log)
    asyncio.run(desktop.with_browser('online', '', check, extra_init=COMPOSITOR))

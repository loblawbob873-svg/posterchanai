"""Push notifications live in Settings' Notifications tab, not in a card above the tabs.

"User Settings: Move Notifications part under Notifications Tab." The switch that turns push on, its
Test, its status line and Stay connected were their own card ABOVE the tabs, apart from everything
else about notifications. Now they open with the Notifications tab -- and still appear when the account
settings could not load, because turning push on must never depend on that one request.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WHERE = r"""(()=>{const t=document.getElementById('set-push-toggle');
  const pane=t&&t.closest('.us-pane'), tabs=document.querySelector('.us-tabs');
  const above = !!(t && tabs && (t.compareDocumentPosition(tabs) & Node.DOCUMENT_POSITION_FOLLOWING));
  return {found:!!t, pane:pane&&pane.dataset.pane, above, visible:!!(t&&t.getClientRects().length),
          test:!!document.getElementById('set-push-test'), status:!!document.getElementById('set-push-status')};})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_push_opens_with_the_notifications_tab(width):
    out = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('settings'); true")
        await b.until("!!document.querySelector('.us-tab[data-tab=\"notifications\"]') && !!document.getElementById('set-push-toggle')")
        out['before'] = await b.js(WHERE)
        await b.js("document.querySelector('.us-tab[data-tab=\"notifications\"]').click(); true")
        await asyncio.sleep(.3)
        out['after'] = await b.js(WHERE)
        out['hit'] = await b.js("""(()=>{const t=document.getElementById('set-push-toggle');t.scrollIntoView({block:'center'});
            const r=t.getBoundingClientRect();const e=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);return {ok:!!(e&&t.contains(e)), r:[r.left,r.top,r.width,r.height].map(Math.round), on:e?(e.id||e.className||e.tagName).toString().slice(0,60):null, disp:getComputedStyle(t).display, hidden:t.hidden};})()""")

    asyncio.run(desktop.with_browser('online', '', check))
    b, a = out['before'], out['after']
    assert b['found'] and b['pane'] == 'notifications' and not b['above'], ("push is not inside the Notifications tab", b)
    assert not b['visible'], ("push shows outside the Notifications tab", b)
    assert a['visible'] and a['test'] and a['status'], ("the Notifications tab does not show push", a)
    assert out["hit"]["ok"], ("the push switch cannot be pressed in the Notifications tab", out["hit"])   # Test shows only once push is on


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_push_still_appears_when_the_account_settings_cannot_load():
    out = {}
    fail = r"""(()=>{try{localStorage.removeItem('pc_settings_cache');}catch(_){}
      const of=window.fetch; window.fetch=(u,o)=>String(u).includes('/api/auth/settings')
        ? Promise.resolve(new Response('{"detail":"down"}',{status:500,headers:{'Content-Type':'application/json'}})) : of(u,o);
      if(window.__PC && __PC.authFetch){ const oa=__PC.authFetch; __PC.authFetch=(u,o)=>String(u).includes('/api/auth/settings')
        ? Promise.resolve(new Response('{"detail":"down"}',{status:500,headers:{'Content-Type':'application/json'}})) : oa(u,o); }})()"""

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(fail)
        await b.js("__PC.switchView('settings'); true")
        await b.until("!!document.getElementById('us-retry') || !!document.querySelector('.us-tabs')")
        out.update(await b.js(WHERE))
        out['error'] = await b.js("!!document.getElementById('us-retry')")

    asyncio.run(desktop.with_browser('online', '', check))
    assert out['error'], ("the failure branch was not reached -- re-read this test", out)
    assert out['found'] and out['visible'] and out['test'], ("push vanished with the account settings", out)

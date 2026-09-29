"""A repo's README / Files / Commits / Issues / Patches tabs sit on ONE line on a phone.

Reported from Android: "The README Files Commits Issues Patches section should appear on the same
line" — the tab row wrapped onto two lines. The real bundled client at phone widths, on a self-hosted
repo (which has all five tabs).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""
(() => {
  const me = __PC.me().pubkey, now = Math.floor(Date.now()/1000);
  const repo = { id: 'c'.repeat(64), kind: 30617, pubkey: me, created_at: now - 3600, sig: 'e'.repeat(128), content: '',
    tags: [['d','demo'],['name','demo'],['clone', 'https://fixture.invalid/git/' + me + '/demo.git']] };
  window.__events = [repo];
  __PC.openRepo(repo);
})()
"""

HOSTED = r"""
const __cfgFetch = window.fetch;
window.fetch = (u, o) => String(u).includes('/client/config')
  ? __cfgFetch(u, o).then(r => r.json()).then(j => new Response(JSON.stringify(Object.assign(j, {git_host_base:'https://fixture.invalid/git'})),
      {status:200, headers:{'Content-Type':'application/json'}}))
  : __cfgFetch(u, o);
"""

ROW = r"""(()=>{const t=[...document.querySelectorAll('.rv-tabs .rv-tab')];
  return {n:t.length, rows:new Set(t.map(x=>Math.round(x.getBoundingClientRect().top))).size,
          overflow:document.documentElement.scrollWidth>innerWidth+1,
          labels:t.map(x=>x.textContent.trim().split(/\s+/)[0])}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [360, 390, 412])
def test_the_repo_tabs_are_one_row_on_a_phone(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=844, deviceScaleFactor=2, mobile=True))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.rv-tabs .rv-tab').length>=5")
        got = await b.js(ROW)
        assert got["rows"] == 1, f"the tabs wrapped onto {got['rows']} lines at {width}px: {got}"
        assert not got["overflow"], f"the tab row pushed the page sideways at {width}px"

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check, HOSTED))

"""The Git page paints what it holds at once, your repos first, and everyone's behind them.

Vyram, 2026-10-21 ("Reduce Load Time on Git"): "The git takes a long time to load … make mine load first then
display everything & then ask it to load again starred & others while you're already in the menu". It waited
for the 5000-announcement read, your own repos AND the stars before drawing anything. Here the big read is
made SLOW on purpose (4 s) and the page must already show your repo, with the scope bar, long before it lands
— then everyone's repos arrive in place, the search text intact. Phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 393])
def test_your_repos_paint_before_the_slow_listing(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=851, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(r"""(()=>{
          const NT=NostrTools,me=NT.getPublicKey(new Uint8Array(32).fill(1)),now=Math.floor(Date.now()/1000);
          const ann=(sk,d)=>NT.finalizeEvent({kind:30617,created_at:now-sk,content:'',tags:[['d',d],['name',d]]},new Uint8Array(32).fill(sk));
          window.__events=[...(window.__events||[]),ann(1,'my-own-repo'),ann(7,'someone-elses-repo'),ann(8,'third-repo')];
          const q=Relay.query.bind(Relay);
          Relay.query=(f,...a)=>(f&&f[0]&&f[0].limit===5000&&!f[0].authors)
            ? new Promise(r=>setTimeout(()=>r(q(f,...a)),4000)) : q(f,...a);
          window.__t0=performance.now();
          __PC.switchView('repos');
        })()""")
        await b.until("[...document.querySelectorAll('.repo-card')].some(c=>c.textContent.includes('my-own-repo'))")
        waited = await b.js("performance.now()-__t0")
        assert waited < 2500, f"your repo took {waited:.0f} ms — the page still waits for the slow listing"
        assert await b.js("!!document.querySelector('.repo-scope [data-scope=mine].on')"), "Mine is not the open scope"
        assert not await b.js("[...document.querySelectorAll('.repo-card')].some(c=>c.textContent.includes('someone-elses-repo'))")
        # Everyone's arrive later, in place: switch to All and the slow listing's repos are there.
        await b.js("document.querySelector('[data-scope=all]').click()")
        await b.until("[...document.querySelectorAll('.repo-card')].some(c=>c.textContent.includes('third-repo'))")
        over = await b.js("document.scrollingElement.scrollWidth-innerWidth")
        assert over <= 1, f"the Git page scrolls sideways by {over}px"

    asyncio.run(desktop.with_browser("online", "", check))

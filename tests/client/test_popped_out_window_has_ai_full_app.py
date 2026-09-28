"""A popped-out PosterChan window has the same ✨ AI panel as the web desktop's windows.

Reported: "AI Features built into the posterchan windows on the webui are missing on PosterChanOS."
On the web desktop every window's title bar has ✨ (os.js toggleWindowAI: suggestions for what the
window shows, ask about it, watch it). On PosterChanOS each app is its own toplevel and draws
oswin.js's title bar, which had only − □ ×.

Runs the real bundled client as a popped-out Social window and asserts: ✨ is in the title bar and
on screen; it opens the same panel, fitted to the window; "Open in AI" hands the window's context to
the DESKTOP (which opens the AI window) and leaves this window's view alone.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(1100, 760), (420, 700)])
def test_the_sparkle_opens_the_ai_panel_and_hands_off_to_the_desktop(width, height):
    async def check(b):
        await desktop.login(b)
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
        # The chrome is installed by adopt() on a real toplevel; the fixture has no compositor.
        await b.js("document.getElementById('pc-oswin-chrome') || PCOSWin.adopt({view:'global', label:'Social'})")
        await b.until("!!document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]')")
        vis = await b.js("""(()=>{const r=document.querySelector('#pc-oswin-chrome [data-action="ai"]').getBoundingClientRect();
                           return r.width>=16&&r.height>=16&&r.right<=innerWidth&&r.left>=0})()""")
        assert vis, "✨ is not visible in the window's title bar"
        await b.js("""window.__asked=[]; PCOSWin.desktop=()=>({ __PC:{ askWindowContext:(ctx,ins,opt)=>{ __asked.push({ctx,ins,opt}); } } });""")
        view0 = await b.js("__PC.VIEW")
        await b.js("document.querySelector('#pc-oswin-chrome [data-action=\"ai\"]').click()")
        await b.until("!!document.querySelector('.osw-ai-panel')")
        fit = await b.js("""(()=>{const p=document.querySelector('.osw-ai-panel').getBoundingClientRect();
          return {fits:p.left>=0&&p.right<=innerWidth+1&&p.top>=38, actions:document.querySelectorAll('.osw-ai-panel [data-ai-action]').length,
                  title:(document.querySelector('.osw-ai-panel header b')||{}).textContent||''}})()""")
        assert fit["fits"] and fit["actions"] == 3, fit
        assert "Social" in fit["title"], fit
        await b.js("document.querySelector('.osw-ai-panel [data-ai-action=\"0\"]').click()")
        await asyncio.sleep(.3)
        asked = await b.js("__asked")
        assert len(asked) == 1 and asked[0]["ctx"]["windows"][0]["kind"] == "PosterChan app", asked
        assert await b.js("__PC.VIEW") == view0, "asking about the window repainted it"
        assert await b.js("!document.querySelector('.osw-ai-panel')"), "the panel stayed open after launching"

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))

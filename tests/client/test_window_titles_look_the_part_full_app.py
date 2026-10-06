"""Window titles are the app's NAME, styled by the theme -- never its lowercase route.

Reported from PosterChanOS: "all the posterchan window titles are lowercase and ugly, make it look cool
and cyberpunk, adjust for other themes as well". Photographed on the desk: four windows titled
`messages`, `mail`, `torrents`, `tg` in small grey type. The cause was the opener: os.js hands
`w.label || view` to the window, so a window with no label of its own arrived "labelled" with its
route, and oswin.js took any label as final.

Driven in the shipped bundle as a popped-out window (`?pcwin=`) whose opener passed the view id as the
label, exactly as os.js does, then measured with computed styles in each theme family:
  * the title reads "Messages";
  * cyberpunk (default): Orbitron, uppercase, a neon gradient, a lit bar in front;
  * Professional: plain text in the palette's own colour, no gradient;
  * Windows 98 / XP: white title text on the blue title bars those systems had.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

WINDOW = ("window.pcShell=window.pcShell||{};window.pcShell.windowContext={role:'app',view:'messages'};"
          "window.pcShell.backgroundOwner=false;window.__PC_WINDOW_LABEL__='messages';")

READ = r"""(()=>{const t=document.querySelector('.pc-oswin-title'), bar=document.getElementById('pc-oswin-chrome');
  if(!t) return null; const cs=getComputedStyle(t), before=getComputedStyle(t,'::before'), b=getComputedStyle(bar);
  return {text:t.textContent, transform:cs.textTransform, font:cs.fontFamily, image:cs.backgroundImage,
          fill:cs.webkitTextFillColor, color:cs.color, bar:b.backgroundImage, beforeW:before.display==='none'?0:parseFloat(before.width)||0}})()"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_window_is_titled_by_name_and_styled_by_the_theme():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 900, "height": 700, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.until("!!document.querySelector('.pc-oswin-title')")
        await asyncio.sleep(.4)
        for theme in ("cyberpunk", "professional", "win98", "winxp"):
            await b.js("document.documentElement.%s" % ("removeAttribute('data-theme')" if theme == "cyberpunk"
                                                       else "setAttribute('data-theme','%s')" % theme))
            await asyncio.sleep(.15)
            got[theme] = await b.js(READ)

    asyncio.run(desktop.with_browser("online", "?pcwin=messages", check, extra_init=WINDOW))
    cy = got["cyberpunk"]
    assert cy and cy["text"] == "Messages", ("the window is titled with its route", cy)
    assert cy["transform"] == "uppercase" and "Orbitron" in cy["font"], cy
    assert "linear-gradient" in cy["image"] and cy["beforeW"] >= 2, ("no neon title on the cyberpunk theme", cy)
    pro = got["professional"]
    assert pro["image"] == "none" and pro["fill"] not in ("rgba(0, 0, 0, 0)", "transparent"), pro
    for theme in ("win98", "winxp"):
        t = got[theme]
        assert t["fill"] == "rgb(255, 255, 255)" and t["transform"] == "none", (theme, t)
        assert "linear-gradient" in t["bar"] and t["beforeW"] == 0, (theme, t)
    assert "rgb(0, 0, 128)" in got["win98"]["bar"], got["win98"]

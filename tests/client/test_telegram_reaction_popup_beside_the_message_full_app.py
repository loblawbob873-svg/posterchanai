"""Telegram's ☺ reactions open beside the message, never in the window's top-left corner.

Reported on PosterChanOS: "Telegram should have the emoji reaction popup not in the top left of the
window". Driven in the shipped bundle as Telegram's OWN window (`?pcwin=tg`) at the desktop's scale, and
measured on screen: the quick row and the "＋" full list must sit next to the message whose ☺ was
pressed -- including when Telegram has repainted the message list in between (it does on every update).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE

WINDOW = "window.pcShell.windowContext={role:'app',view:'tg'};window.pcShell.backgroundOwner=false;"

NEAR = r"""((sel)=>{const p=document.querySelector(sel), b=document.querySelector('.tg-msg[data-id="1"] [data-react-pick]')||window.__anchorRect;
  if(!p) return {pop:false};
  const r=p.getBoundingClientRect(), a=b.getBoundingClientRect?b.getBoundingClientRect():b;
  return {pop:true, left:Math.round(r.left), top:Math.round(r.top), w:Math.round(r.width),
          inView:r.left>=-1&&r.top>=-1&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1,
          dy:Math.round(Math.min(Math.abs(r.bottom-a.top),Math.abs(r.top-a.bottom),Math.abs(r.top-a.top))),
          dx:Math.round(Math.abs((r.left+r.right)/2-(a.left+a.right)/2))}})"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("scale", ["1", "1.25"])
def test_the_reaction_popups_open_beside_the_message(scale):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js(f"document.documentElement.style.setProperty('--ui-scale','{scale}');true")
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('.tg-msg[data-id=\"1\"] [data-react-pick]')")
        await asyncio.sleep(.3)
        # The quick row.
        await b.js("window.__anchorRect=document.querySelector('.tg-msg[data-id=\"1\"] [data-react-pick]').getBoundingClientRect();"
                   "document.querySelector('.tg-msg[data-id=\"1\"] [data-react-pick]').click()")
        await b.until("!!document.querySelector('.tg-react-pop')")
        got["row"] = await b.js(NEAR + "('.tg-react-pop')")
        # The full picker, after Telegram repainted the list (a reaction on another message does that).
        await b.js("PCTelegram.react && PCTelegram.react(2,'👍')")
        await asyncio.sleep(.4)
        if not await b.js("!!document.querySelector('.tg-react-pop [data-more]')"):
            await b.js("document.querySelector('.tg-msg[data-id=\"1\"] [data-react-pick]').click()")
            await b.until("!!document.querySelector('.tg-react-pop [data-more]')")
        await b.js("window.__anchorRect=document.querySelector('.tg-msg[data-id=\"1\"] [data-react-pick]').getBoundingClientRect();"
                   "document.querySelector('.tg-react-pop [data-more]').click()")
        # ＋ now opens the rest of THIS chat's reaction list in the same popup (telegram.js pickReaction),
        # not the general emoji picker, nine in ten of whose emoji Telegram refuses.
        await b.until("!!document.querySelector('.tg-react-pop.all')")
        await asyncio.sleep(.2)
        got["picker"] = await b.js(NEAR + "('.tg-react-pop.all')")

    asyncio.run(desktop.with_browser("online", "?pcwin=tg", check,
                                     extra_init=FAKE.replace("state:'none'", "state:'ready'") + WINDOW))
    row, picker = got["row"], got["picker"]
    assert row["pop"] and row["inView"] and row["dy"] < 60 and row["dx"] < 320, ("the quick row is not beside the message", scale, row)
    assert picker["pop"] and picker["inView"], ("the full picker is off screen", scale, picker)
    assert not (picker["left"] <= 12 and picker["top"] <= 60), ("the full picker opened in the window's top-left corner", scale, picker)
    assert picker["dy"] < 120 and picker["dx"] < 420, ("the full picker is not beside the message", scale, picker)

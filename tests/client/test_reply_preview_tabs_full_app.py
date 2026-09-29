"""The Reply sheet keeps its Write / Preview tabs clear of the post being answered, in Preview too.

Reported with a screenshot from a phone: "that tab squish in Reply modal, happens sometimes". In
Preview, the Write/Preview buttons were drawn on top of the "Replying to" card. The sheet is a flex
column in which the reference card gives up space first (see client.css, "The compose sheet is a
COLUMN"), and its box was allowed to shrink to NOTHING (`min-height:0`) while the card inside keeps
a 72px floor. A preview taller than the textarea it replaces — any reply with a few lines, links or
pictures — squeezed the box to zero and the card spilled out of it, under the tabs that follow.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PARENT = r"""(()=>{const other=new Uint8Array(32).fill(2);
  const ev=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-60,content:'what do you mean exactly?',tags:[]},other);
  Store.saveEvent(ev); return ev.id;})()"""
TEXT = "\n".join(["The icons of each function here (image %d)\nsome more words on this line to wrap it" % i for i in range(1, 9)])
GEOM = r"""(()=>{const r=s=>{const e=document.querySelector(s);return e?e.getBoundingClientRect():null};
  const card=r('.cmp-modal .cmp-ctx .quoted'), ctx=r('.cmp-modal .cmp-ctx'), tabs=r('.cmp-modal .cmp-tabs'), pv=r('#cmp-preview'), send=r('#cmp-send');
  return {cardBottom:Math.round(card.bottom), ctxBottom:Math.round(ctx.bottom), tabsTop:Math.round(tabs.top), tabsBottom:Math.round(tabs.bottom),
          previewTop:Math.round(pv.top), previewShown:pv.height>0, sendBottom:Math.round(send.bottom), vh:innerHeight}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("w,h", [(411, 830), (390, 700), (1280, 640)], ids=["android", "short-phone", "short-desktop"])
def test_the_tabs_never_sit_on_the_quoted_post_in_preview(w, h):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": w < 800})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        pid = await b.js(PARENT)
        await b.js("__PC.openThread(%s)" % json.dumps(pid))
        await b.until("!!document.querySelector('article[data-id=%s] .act[data-a=\"reply\"]')" % json.dumps(pid))
        await b.js("document.querySelector('article[data-id=%s] .act[data-a=\"reply\"]').click()" % json.dumps(pid))
        await b.until("!!document.querySelector('.cmp-modal #cmp')")
        await b.js("(()=>{const t=document.querySelector('#cmp');t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));})()" % json.dumps(TEXT))
        await b.js("document.querySelector('.cmp-tab[data-t=\"preview\"]').click()")
        await asyncio.sleep(.4)
        g = await b.js(GEOM)
        assert g["previewShown"], g
        assert g["cardBottom"] <= g["tabsTop"] + 1, ("the Replying-to card runs under the Write/Preview tabs", g)
        assert g["ctxBottom"] <= g["tabsTop"] + 1 and g["tabsBottom"] <= g["previewTop"] + 1, g
        assert g["sendBottom"] <= g["vh"] + 1, ("Post fell off the bottom", g)

    asyncio.run(desktop.with_browser("online", "", check))

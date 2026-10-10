"""Communities: right-clicking a name in Known members opens the member menu, on top of everything.

Reported 2026-10-09: "in communities rightclicking on username in Known members does nothing". Every
existing member-menu test read the source text; none ever right-clicked a row. This one does, with a real
mouse event through Chrome's input pipeline, on the page and as Concord's own PosterChanOS window, and
requires the menu to be the thing actually at its own centre -- present but covered is the same failure.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP
from tests.client.test_concord_member_search_full_app import MEMBERS

WINDOW = "window.pcShell.windowContext={role:'app',view:'concord'};window.pcShell.backgroundOwner=false;"
ON_TOP = r"""(()=>{const m=document.querySelector('.cc-member-menu');if(!m)return {menu:false};
  const r=m.getBoundingClientRect(),hit=document.elementFromPoint(r.left+r.width/2,r.top+Math.min(18,r.height/2));
  return {menu:true,visible:r.width>0&&r.height>0,onTop:!!hit&&m.contains(hit),
          items:[...m.querySelectorAll('[role=menuitem]')].map(b=>b.textContent)}})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _right_click_a_member(b):
    await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=20")
    x, y = await b.js("(()=>{const r=document.querySelectorAll('.cc-members-pane .cc-member')[3].getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()")
    await b.call("Input.dispatchMouseEvent", dict(type="mouseMoved", x=x, y=y))
    await b.call("Input.dispatchMouseEvent", dict(type="mousePressed", x=x, y=y, button="right", buttons=2, clickCount=1))
    await b.call("Input.dispatchMouseEvent", dict(type="mouseReleased", x=x, y=y, button="right", buttons=0, clickCount=1))
    await asyncio.sleep(.4)
    return await b.js(ON_TOP)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("where", ["page", "pcos-window"])
def test_right_clicking_a_known_member_opens_the_menu_on_top(where):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1500, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        if where == "page":
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js(MEMBERS)
        await b.js("PCConcord.render()")
        got.update(await _right_click_a_member(b))
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "?pcwin=concord" if where == "pcos-window" else "", check,
                                     WINDOW if where == "pcos-window" else ""))
    assert got.get("menu"), ("right-click opened no menu", where, got)
    assert got["visible"] and got["onTop"], ("the menu opened but something covers it", where, got)
    assert "View profile" in got["items"], got
    assert not got["errors"], got["errors"]


# 2026-10-10: "I still can't right click on usernames in concord and see options like before". A name in a
# MESSAGE (and its avatar) is where people right-click; it had no menu at all -- only the Known members list did.
from tests.client.test_concord_react_full_app import ROOM as BOB_ROOM  # noqa: E402

NOT_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


async def _right_click(b, selector):
    x, y = await b.js("(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));"
                      "const r=m.querySelector(%r).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % selector)
    await b.call("Input.dispatchMouseEvent", dict(type="mouseMoved", x=x, y=y))
    await b.call("Input.dispatchMouseEvent", dict(type="mousePressed", x=x, y=y, button="right", buttons=2, clickCount=1))
    await b.call("Input.dispatchMouseEvent", dict(type="mouseReleased", x=x, y=y, button="right", buttons=0, clickCount=1))
    await asyncio.sleep(.4)
    return await b.js(ON_TOP)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("part", ["name", "avatar"])
def test_right_clicking_a_name_in_a_message_opens_the_member_menu(part):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=850, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(BOB_ROOM)
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('hello from bob'))")
        got.update(await _right_click(b, ".cc-message-body > b" if part == "name" else ".cc-message-avatar"))
        if got.get("menu"):
            # …and the menu's first item does what it says, for the person whose name was clicked.
            await b.js("(()=>{window.__opened=null;const o=__PC.openProfile;__PC.openProfile=pk=>{window.__opened=pk;};"
                       "document.querySelector('.cc-member-menu [data-cc-member-profile]').click();})()")
            got["opened"] = await b.js("window.__opened")

    asyncio.run(desktop.with_browser("online", "", check, NOT_OS))
    assert got.get("menu"), ("right-clicking a name in a message opened no menu", part, got)
    assert got["visible"] and got["onTop"], ("the menu opened but something covers it", part, got)
    assert "View profile" in got["items"] and "Message" in got["items"], got
    assert got.get("opened") == "b" * 64, ("View profile opened the wrong person", got)

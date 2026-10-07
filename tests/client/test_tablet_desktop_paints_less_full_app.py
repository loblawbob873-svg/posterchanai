"""A tablet's desktop holds less in the compositor: no backdrop layers, nothing painted under a window.

Reported: Android tablet, desktop mode, "it runs out of memory with a few apps open ... reloads". Measured
at tablet size (1280x800, DPR 2, touch, the real feeds; see the commit): JS and DOM were small; the
compositor and the decoded-image cache grew with every window, and six full-screen layers existed before
any window was open. The backdrop's translateZ/fixed/scroller made the city skyline, .scanlines and
.grid-bg layers of their own, and every surface painted OVER them overlapped a layer and was promoted too
("Overlap") -- #feed became one 70,000 px layer. On a coarse pointer the backdrop now paints into the page
(painted layer area 194 -> 11 MP). And a window entirely under another paints nothing at all.

Both are read from Chrome's own LayerTree / computed style, never from the CSS text.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

BACKDROP = [".city-bg", ".scanlines", ".grid-bg", ".city-far", ".city-mid", ".city-near"]


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


async def _raw(b, method, params=None, want_layers=None):
    """One CDP call read off the socket directly, keeping LayerTree's event (Browser.call drops events)."""
    b.sequence += 1
    me = b.sequence
    await b.ws.send(json.dumps({"id": me, "method": method, "params": params or {}}))
    result, deadline = None, asyncio.get_event_loop().time() + 20
    while asyncio.get_event_loop().time() < deadline:
        try:
            m = json.loads(await asyncio.wait_for(b.ws.recv(), 1))
        except asyncio.TimeoutError:
            if result is not None:
                return result
            continue
        if want_layers is not None and m.get("method") == "LayerTree.layerTreeDidChange" and m["params"].get("layers"):
            want_layers[:] = m["params"]["layers"]
        if m.get("id") == me:
            result = m.get("result", {})
            if want_layers is None or want_layers:
                return result
        elif result is not None and want_layers:
            return result
    return result


async def _layered_backdrop(b):
    """Which backdrop elements Chrome has given a compositor layer of their own."""
    doc = (await b.call("DOM.getDocument", {"depth": 0}))["root"]["nodeId"]
    ids = {}
    for sel in BACKDROP:
        nid = (await b.call("DOM.querySelector", {"nodeId": doc, "selector": sel})).get("nodeId")
        if nid:
            ids[(await b.call("DOM.describeNode", {"nodeId": nid}))["node"]["backendNodeId"]] = sel
    assert ids, "the backdrop is not on the page at all -- the check would pass vacuously"
    layers = []
    await _raw(b, "LayerTree.enable", want_layers=layers)
    await _raw(b, "LayerTree.disable")
    assert layers, "no layer tree"
    return sorted({ids[l["backendNodeId"]] for l in layers if l.get("backendNodeId") in ids})


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_touch_screen_gives_the_backdrop_no_layers_of_its_own():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 800, "deviceScaleFactor": 2, "mobile": False})
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        await b.js("location.reload()")
        await asyncio.sleep(1)
        await b.until("!!window.__PC && !!window.PCOS")
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-start')")
        assert await b.js("matchMedia('(pointer:coarse)').matches"), "touch emulation did not make the pointer coarse"
        await b.call("DOM.enable")
        await asyncio.sleep(.5)
        layered = await _layered_backdrop(b)
        assert layered == [], f"on a touch screen the backdrop still holds compositor layers: {layered}"
        # Same picture: the backdrop is still there and still fills the screen.
        box = await b.js("(()=>{const r=document.querySelector('.city-bg').getBoundingClientRect();return [r.width,r.height]})()")
        assert box == [1280, 800], box

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_window_under_a_maximised_one_paints_nothing_and_keeps_its_place():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 800, "deviceScaleFactor": 2, "mobile": False})
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-start')")

        async def open_app(q, view):
            await b.js("document.querySelector('#os-startmenu') || document.querySelector('#os-start').click()")
            await b.until("!!document.querySelector('#os-q')")
            await b.js(f"(()=>{{const i=document.querySelector('#os-q');i.value={json.dumps(q)};i.dispatchEvent(new Event('input',{{bubbles:true}}))}})()")
            await b.until(f"!!document.querySelector('#os-startmenu [data-view={view}]')")
            await b.js(f"document.querySelector('#os-startmenu [data-view={view}]').click()")
            await b.until(f"[...document.querySelectorAll('.osw')].length >= 1")
            await asyncio.sleep(.4)

        await open_app("Notes", "notes")
        await b.js("window.__under=document.querySelector('.osw')")
        await open_app("Terminal", "terminal")
        top = "[...document.querySelectorAll('.osw')].find(w=>w!==__under)"
        # #feed follows FOCUS to the new window; the first one is now PARKED, its content in its own
        # slot -- that slot is what has to keep its place. (Read after parking, not before.)
        # Whatever scrolls inside the parked window, scrolled there now -- covering is what is under test.
        await b.js("(()=>{const pad=document.createElement('div');pad.style.height='6000px';pad.className='test-pad';"
                   "__under.querySelector('.osw-slot').appendChild(pad)})()")
        before = await b.js("(()=>{window.__scroller=[...__under.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+500"
                            "&&/(auto|scroll)/.test(getComputedStyle(e).overflowY)).sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];"
                            "if(!__scroller)return -1;__scroller.scrollTop=2345;return __scroller.scrollTop})()")
        assert before > 2000, f"no scroller in the parked window to test with: {before}"
        await b.js(f"{top}.querySelector('[data-w=max]').click()")
        await b.until("__under.classList.contains('osw-covered')")
        hidden = await b.js("getComputedStyle(__under.querySelector('.osw-body')).visibility")
        assert hidden == "hidden", f"a window entirely under a maximised one still paints: visibility={hidden}"
        # The top window is never marked.
        assert not await b.js(f"{top}.classList.contains('osw-covered')")
        assert await b.js("__scroller.scrollTop") == before, "the window lost its place while covered"
        # Uncovered again WITHOUT a focus change (focusing a parked window un-parks it, which is a different
        # mechanism): the top window is simply moved off it. It paints, and it is exactly where it was.
        await b.js(f"(()=>{{const t={top};t.style.left='1270px';t.style.top='790px';}})()")
        await b.until("!__under.classList.contains('osw-covered')")
        assert await b.js("getComputedStyle(__under.querySelector('.osw-body')).visibility") == "visible"
        assert await b.js("__scroller.scrollTop") == before, "covering the window lost its scroll position"

    asyncio.run(desktop.with_browser("online", "", check))

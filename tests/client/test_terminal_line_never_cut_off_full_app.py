"""The terminal's current line is never left below the window's edge.

Reported: "terminal, sometimes the current line gets cut off with the window". Measured in the real
web desktop: _fit ignores a terminal whose window is not focused (a focus change briefly reports a
TEMPORARY size while the shared feed is parked, and fitting to that would make the shell rewrap). But a
window resized, snapped or tiled while ANOTHER window has focus then kept its old, taller grid --
43 rows in a box that fits 35 -- and the prompt line sat ~115px below the edge until somebody clicked
it. Now an ignored measurement is re-taken twice: a size that holds is fitted; a transient that
reverts is not.

Driven with the real term.js / xterm in the shipped bundle, at desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


GEOM = r"""(()=>{const s=document.getElementById('tty-screen'); const b=s.getBoundingClientRect();
  const rows=[...s.querySelectorAll('.xterm-rows > div')]; const lr=rows[rows.length-1].getBoundingClientRect();
  return {rows:rows.length, cut:Math.round(lr.bottom-b.bottom), focused:s.closest('.osw').classList.contains('focused')}})()"""
FOCUS = "(sel=>{const w=[...document.querySelectorAll('.osw')].find(x=>sel?!!x.querySelector('#tty-screen'):!x.querySelector('#tty-screen'));w.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));})(%s)"
TERM_WIN = "document.getElementById('tty-screen').closest('.osw')"


async def _desktop_with_terminal(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    await desktop.login(b)
    await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
    for v in ("terminal", "calculator"):
        await b.js("(()=>{const el=document.querySelector('#os-desk [data-view=\"%s\"]');el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));el.click();})()" % v)
        await asyncio.sleep(1.2)
    await b.until("!!document.querySelector('#tty-screen .xterm-rows > div')")
    await b.js(FOCUS % "true")
    await asyncio.sleep(.6)
    g = await b.js(GEOM)
    assert g["focused"] and g["cut"] <= 1, g


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_terminal_resized_while_another_window_has_focus_refits():
    async def check(b):
        await _desktop_with_terminal(b)
        before = await b.js(GEOM)
        await b.js(FOCUS % "false")
        await asyncio.sleep(.4)
        await b.js("(w=>{w.style.height=(w.offsetHeight-180)+'px';})(%s)" % TERM_WIN)
        await asyncio.sleep(1.5)                      # never clicked
        after = await b.js(GEOM)
        assert not after["focused"], after
        assert after["rows"] < before["rows"], ("the grid kept its old height", before, after)
        assert after["cut"] <= 1, ("the current line is below the window's edge", after)
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_transient_size_while_unfocused_is_not_fitted():
    """What the focus guard protects: a size that is gone a moment later must not rewrap the shell."""
    async def check(b):
        await _desktop_with_terminal(b)
        before = await b.js(GEOM)
        await b.js(FOCUS % "false")
        await asyncio.sleep(.4)
        await b.js("window.__rowsSeen=new Set();setInterval(()=>__rowsSeen.add(document.querySelectorAll('#tty-screen .xterm-rows > div').length),20);"
                   "(w=>{const h=w.style.height;w.style.height=(w.offsetHeight-180)+'px';setTimeout(()=>{w.style.height=h;},150);})(%s)" % TERM_WIN)
        await asyncio.sleep(1.5)
        seen = await b.js("[...__rowsSeen]")
        assert seen == [before["rows"]], ("a transient size rewrapped the shell", seen, before)
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_focused_resize_still_fits():
    async def check(b):
        await _desktop_with_terminal(b)
        before = await b.js(GEOM)
        await b.js("(w=>{w.style.height=(w.offsetHeight-180)+'px';})(%s)" % TERM_WIN)
        await asyncio.sleep(.8)
        after = await b.js(GEOM)
        assert after["rows"] < before["rows"] and after["cut"] <= 1, (before, after)
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_no_window_height_leaves_the_last_row_below_the_edge():
    """The "sometimes": FitAddon reads its parent's computed height, which under this client's
    `*{box-sizing:border-box}` includes #tty-screen's padding. Heights within those pixels of a row
    boundary got one row more than fits. Sweep a whole row's worth of heights, one pixel at a time."""
    async def check(b):
        await _desktop_with_terminal(b)
        bad = []
        base = await b.js("%s.offsetHeight" % TERM_WIN)
        for d in range(0, 20):
            await b.js("(w=>{w.style.height=(%d)+'px';})(%s)" % (base - 120 - d, TERM_WIN))
            await asyncio.sleep(.35)
            g = await b.js(GEOM)
            if g["cut"] > 1:
                bad.append((base - 120 - d, g))
        assert not bad, ("the current line is below the window's edge at these heights", bad)
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_focus_does_not_change_the_terminal_box_on_the_desktop_build():
    """`body.native.term-view .tty-bar` is the PHONE's status-bar inset, but the desktop build is
    `native` too and `term-view` is on only while the Terminal is focused -- so focusing it made the bar
    6px taller, the screen 6px shorter, and every focus change a refit and a rewrap."""
    async def check(b):
        await _desktop_with_terminal(b)
        box = "(()=>{const r=document.getElementById('tty-screen').getBoundingClientRect();return [Math.round(r.top*10),Math.round(r.height*10)]})()"
        focused = await b.js(box)
        await b.js(FOCUS % "false")
        await asyncio.sleep(.6)
        unfocused = await b.js(box)
        assert focused == unfocused, ("focus changed the terminal's box", focused, unfocused)
    asyncio.run(desktop.with_browser("online", "", check, ""))

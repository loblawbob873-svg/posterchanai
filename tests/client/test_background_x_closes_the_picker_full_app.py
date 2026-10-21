"""Post -> Background -> ✕ takes the background off AND puts the picker away -- in both composers.

Reported: "Post -> Background -> If I click x, nothing happens. If users change mind, it should disappear." The ✕ was a
"no background" swatch: it only cleared the choice, so with nothing picked yet (✕ already the selected swatch) a click
changed nothing on screen and the row of colours stayed open.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

needs_chrome = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ROW = "(document.querySelector('{sel}')||{{}}).classList&&!document.querySelector('{sel}').classList.contains('hidden')"


@needs_chrome
@pytest.mark.parametrize("width", [390, 1280])
def test_x_closes_the_background_picker_in_the_social_composer(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 821})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('global')")
        await b.until("!!document.getElementById('tl-cmp-more')")
        open_row = ("(async()=>{document.getElementById('tl-cmp-more').click();await new Promise(r=>setTimeout(r,150));"
                    "document.querySelector('.menu-pop [data-m=\"bg\"]').click();})()")
        # changed their mind before picking anything
        await b.js(open_row)
        await b.until(ROW.format(sel="#tl-cmp-bgs"))
        await b.js("document.querySelector('#tl-cmp-bgs .cmp-swatch-none').click()")
        got["closed_unpicked"] = not await b.js(ROW.format(sel="#tl-cmp-bgs"))
        # picked a colour, then changed their mind
        await b.js("(()=>{const t=document.getElementById('tl-cmp-ta');t.value='hello world';t.dispatchEvent(new Event('input'));})()")
        await b.js(open_row)
        await b.until(ROW.format(sel="#tl-cmp-bgs"))
        await b.js("document.querySelectorAll('#tl-cmp-bgs .cmp-swatch:not(.cmp-swatch-none)')[0].click()")
        got["armed"] = await b.js("document.querySelector('#tl-cmp-bgs .cmp-swatch.on:not(.cmp-swatch-none)')!==null")
        await b.js("document.querySelector('#tl-cmp-bgs .cmp-swatch-none').click()")
        got["closed_picked"] = not await b.js(ROW.format(sel="#tl-cmp-bgs"))
        got["disarmed"] = await b.js("document.querySelector('#tl-cmp-bgs .cmp-swatch.on:not(.cmp-swatch-none)')===null")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["closed_unpicked"], ("✕ with nothing picked left the picker open (nothing happened)", got)
    assert got["armed"], got
    assert got["closed_picked"] and got["disarmed"], ("✕ after picking did not take it off and close the picker", got)


@needs_chrome
def test_x_closes_the_background_picker_in_the_full_composer():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.compose && __PC.compose()")
        await b.until("!!document.getElementById('cmp-bg-btn')")
        await b.js("document.getElementById('cmp-bg-btn').click()")
        await b.until(ROW.format(sel="#cmp-bg-strip"))
        await b.js("document.querySelector('#cmp-bg-strip .cmp-swatch-none').click()")
        got["closed"] = not await b.js(ROW.format(sel="#cmp-bg-strip"))
        got["button_off"] = not await b.js("document.getElementById('cmp-bg-btn').classList.contains('active')")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["closed"] and got["button_off"], ("✕ left the background row open in the composer", got)


# Framed card and Newspaper, the two other ways a post gets a background: each has to come off the same way, with
# nothing left armed and nothing left on screen ("check framed card and newspaper too").
COMPOSERS = {
    "social": dict(ta="#tl-cmp-ta", ai="#tl-cmp-ai", strip="#tl-cmp-bgs", prev="#tl-cmp-cardprev", setup=""),
    "modal": dict(ta="#cmp", ai="#cmp-ai", strip="#cmp-bg-strip", prev="#cmp-cardprev", setup="__PC.compose && __PC.compose()"),
}
SHOWN = "(()=>{{const e=document.querySelector('{sel}');return !!e&&!e.classList.contains('hidden')&&e.getBoundingClientRect().height>0;}})()"


async def _open(b, c):
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('global')")
    await b.until("!!document.getElementById('tl-cmp-ta')")
    if c["setup"]:
        await b.js(c["setup"])
    await b.until("!!document.querySelector('%s')" % c["ta"])
    await b.js("(()=>{const t=document.querySelector('%s');t.value='Big news today\\nThe whole story is here.';"
               "t.dispatchEvent(new Event('input'));})()" % c["ta"])


async def _ai(b, c, item):
    await b.js("document.querySelector('%s').click()" % c["ai"])
    await b.until("!!document.querySelector('.menu-pop [data-m=\"%s\"]')" % item)
    label = await b.js("document.querySelector('.menu-pop [data-m=\"card\"]')?.textContent||''")
    await b.js("document.querySelector('.menu-pop [data-m=\"%s\"]').click()" % item)
    await asyncio.sleep(.4)
    return label


@needs_chrome
@pytest.mark.parametrize("where", ["social", "modal"])
def test_a_framed_card_comes_off_with_x(where):
    c, got = COMPOSERS[where], {}

    async def check(b):
        await _open(b, c)
        await _ai(b, c, "card")                                    # framed on: the colours open
        await b.until(SHOWN.format(sel=c["strip"]))
        await b.js("document.querySelectorAll('%s .cmp-swatch:not(.cmp-swatch-none)')[0].click()" % c["strip"])
        await b.until(SHOWN.format(sel=c["prev"]))               # the card is drawn asynchronously
        got["preview_on"] = await b.js(SHOWN.format(sel=c["prev"]))
        await b.js("document.querySelector('%s .cmp-swatch-none').click()" % c["strip"])
        await asyncio.sleep(1)
        got["strip_after"] = await b.js(SHOWN.format(sel=c["strip"]))
        got["preview_after"] = await b.js(SHOWN.format(sel=c["prev"]))
        got["menu_after"] = await _ai(b, c, "translate")          # read the Framed card label, pick something harmless
        await b.js("document.body.click()")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["preview_on"], ("the framed card never showed, so this measured nothing", got)
    assert not got["strip_after"] and not got["preview_after"], ("✕ left the framed card on screen", got)
    assert "✓" not in got["menu_after"], ("✕ left the frame armed for the next post", got)


@needs_chrome
@pytest.mark.parametrize("where", ["social", "modal"])
def test_a_newspaper_comes_off_with_its_x(where):
    c, got = COMPOSERS[where], {}

    async def check(b):
        await _open(b, c)
        await _ai(b, c, "paper")
        await b.until("!!document.querySelector('.cmp-paper-chip .cmp-paper-x')")
        await b.until(SHOWN.format(sel=c["prev"]))
        got["preview_on"] = await b.js(SHOWN.format(sel=c["prev"]))
        await b.js("document.querySelector('.cmp-paper-chip .cmp-paper-x').click()")
        await asyncio.sleep(1)
        got["chip_after"] = await b.js("!!document.querySelector('.cmp-paper-chip')")
        got["preview_after"] = await b.js(SHOWN.format(sel=c["prev"]))
        got["strip_after"] = await b.js(SHOWN.format(sel=c["strip"]))
        got["armed_after"] = await b.js("document.querySelector('%s .cmp-swatch.on:not(.cmp-swatch-none)')!==null" % c["strip"])

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["preview_on"], ("the newspaper never showed, so this measured nothing", got)
    assert not got["chip_after"] and not got["preview_after"] and not got["strip_after"], ("✕ left the newspaper on screen", got)
    assert not got["armed_after"], ("✕ left the newspaper armed for the next post", got)

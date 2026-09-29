"""The "Join or create a community" sheet is laid out on a computer, not only on a phone.

Reported: "UI buttons look messed on Communities 'Join or create community'". Measured: on any
window wider than 820px "or start your own" and its Create button sat jammed on one line under
Cancel / Preview invite. concord.css's `@media(max-width:820px){…` line had rules inserted into the
MIDDLE of it twice, which split its closing tail onto a later line — so everything in between
(.cc-join-alt, .cc-section-head, the channel "+") silently became phone-only.
"""
import asyncio
import re
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _top_level_selectors(css):
    """Selectors of rules that are NOT inside any @-block."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, depth, buf = set(), 0, ""
    for ch in css:
        if ch == "{":
            if depth == 0:
                out.update(s.strip() for s in buf.split(","))
            depth += 1
            buf = ""
        elif ch == "}":
            depth -= 1
            buf = ""
        else:
            buf += ch
    assert depth == 0, "unbalanced braces"
    return out


def test_the_join_sheet_and_channel_rules_apply_at_every_width():
    top = _top_level_selectors((ROOT / "static/css/concord.css").read_text())
    for sel in (".cc-join-alt", ".cc-join-alt .btn", ".cc-section-head", ".cc-add-channel", ".cc-add-channel:hover"):
        assert sel in top, f"{sel} is inside an @media block — it only applies on some screens"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_create_a_community_is_its_own_row_under_the_invite_actions(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        if width < 600:
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.getElementById('cc-welcome-join')")
        await b.js("document.getElementById('cc-welcome-join').click()")
        got = await b.js("""(()=>{const R=s=>document.querySelector(s).getBoundingClientRect();
          const card=R('#cc-join .cc-join-card'), go=R('#cc-join-go'), cr=R('#cc-join-create'),
                alt=getComputedStyle(document.querySelector('.cc-join-alt'));
          return {dir:alt.flexDirection, divider:parseFloat(alt.borderTopWidth), below:cr.top-go.bottom,
                  span:cr.width/(card.width-parseFloat(getComputedStyle(document.querySelector('#cc-join .cc-join-card')).paddingLeft)*2),
                  inside:card.left>=0 && card.right<=innerWidth+1}})()""")
        assert got["dir"] == "column" and got["divider"] >= 1, f"the create section is not its own block: {got}"
        assert got["below"] >= 20, f"Create a community is jammed against the invite actions: {got}"
        assert got["span"] > .95, f"Create a community is not a full-width row: {got}"
        assert got["inside"], f"the sheet runs off the screen: {got}"

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_re_render_keeps_an_open_sheet_and_what_was_typed_in_it():
    """Found measuring the report above: the sheet opened, then shut itself as soon as the view
    re-rendered (relay answers arriving), taking a pasted invite with it."""
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.getElementById('cc-welcome-join')")
        await b.js("document.getElementById('cc-welcome-join').click()")
        await b.js("const i=document.getElementById('cc-invite-url'); i.value='https://x.invalid/invite/naddr1abc#k'; i.focus()")
        await b.js("PCConcord.render()")
        await asyncio.sleep(.2)
        got = await b.js("({open:!document.getElementById('cc-join').classList.contains('hidden'),"
                         "value:document.getElementById('cc-invite-url').value, focus:document.activeElement.id})")
        assert got == {"open": True, "value": "https://x.invalid/invite/naddr1abc#k", "focus": "cc-invite-url"}, got
        await b.js("document.getElementById('cc-join-cancel').click()")
        await b.js("PCConcord.render()")
        await asyncio.sleep(.2)
        assert await b.js("document.getElementById('cc-join').classList.contains('hidden')"), "Cancel did not stay closed"

    asyncio.run(desktop.with_browser("online", "", check))

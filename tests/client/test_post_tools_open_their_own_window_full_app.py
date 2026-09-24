"""🎞️ Meme Builder and 🎬 Effect on a post open in THEIR OWN window on PosterChanOS.

Reported: "if you choose Meme Builder on a post, built it, send the reply, no way to go back to
social. maybe meme build should just open in a new window". Every app on PosterChanOS is a popped-out
window (its own document, `?pcwin=`); the in-page desktop's rule "a feature opened from inside
another gets its own window" lived in switchView behind `PCOS.isOn()`, which is false in such a
window -- so the builder REPLACED the Social timeline, in a window with no navigation to get back.

Opens the client exactly as PosterChanOS opens an app, with `window.open` recorded (the shell's
side of it is Electron, which cannot run here).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

POST = "ab" * 32
EVENT = {"id": POST, "pubkey": "cd" * 32, "created_at": 1_700_000_000, "kind": 1, "tags": [], "sig": "",
         "content": "look https://example.test/cat.jpg"}


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RECORD = "window.__opened=[];window.open=(u,n,f)=>{__opened.push(String(u));return null};true"
# The desktop this window belongs to (a live same-origin `window.opener` on PosterChanOS): only IT
# may open windows, so what is recorded is the request that reached it.
DESKTOP = ("window.__asked=[];Object.defineProperty(window,'opener',{configurable:true,value:{closed:false,__PC:{},"
           "PCOSWin:{enabled:()=>true,routable:()=>true,open:(v,l,o)=>{__asked.push([v,o&&o.arg]);return {}}}}});true")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize("tool", ["meme", "ai"])
def test_from_the_social_window_a_tool_opens_its_own_window(tool):
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin') && !!window.__PC")
        await b.js(DESKTOP)
        await b.js("Store.saveEvent(" + json.dumps(EVENT) + ");true")
        await b.js(f"__PC.postTool({json.dumps(tool)}, {json.dumps(POST)});true")
        await asyncio.sleep(.5)
        got['asked'] = await b.js("__asked")
        got['still_social'] = await b.js("__PC.isView('global')")

    asyncio.run(desktop.with_browser('online', '?pcwin=global', check, ''))
    assert got['asked'] == [[tool, POST]], "the desktop was not asked to open the tool for this post: %r" % got
    assert got['still_social'], "the Social window was repainted into the tool: %r" % got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_meme_builder_window_takes_the_post_in_itself():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin') && !!window.__PC")
        await b.js(RECORD)
        got['param_consumed'] = await b.js("!new URLSearchParams(location.search).has('pcpost')")
        await b.js("Store.saveEvent(" + json.dumps(EVENT) + ");true")
        await b.js(f"__PC.postTool('meme', {json.dumps(POST)});true")
        await b.until("/added to the Meme Builder|could not add/.test(document.body.innerText)")
        got['toast'] = await b.js("document.body.innerText.includes('added to the Meme Builder')")
        got['opened'] = await b.js("__opened")
        got['meme'] = await b.js("__PC.isView('meme')")

    asyncio.run(desktop.with_browser('online', f'?pcwin=meme&pcpost={POST}', check, ''))
    assert got['param_consumed'], got
    assert got['opened'] == [], "the Meme Builder window opened ANOTHER window instead of taking the post: %r" % got
    assert got['meme'] and got['toast'], got


def test_only_a_post_id_crosses_to_the_new_window():
    """The route channel is shared by every window: the argument is validated as a 64-hex id at
    both ends, never trusted as a URL or a payload."""
    src = (Path(__file__).resolve().parents[2] / "static/js/client/oswin.js").read_text(encoding="utf-8")
    assert "POST_TOOLS.includes(view) && /^[0-9a-f]{64}$/i.test(String(o.arg||''))" in src
    assert "POST_TOOLS.includes(v) && /^[0-9a-f]{64}$/i.test(arg)" in src
    assert "'&pcpost='" in src

"""Communities does not rebuild its screen when there is nothing new to show.

Reported: "Communities: keeps flashing every few seconds." Measured on the reporting desktop: 46 full
rebuilds of #feed in two minutes -- the rail, every avatar and every message torn down and redrawn --
from three loops that each ask for a render when they believe something changed (membership sync,
room metadata, the background sweep), and render() itself starts the membership sync, so every
rebuild scheduled the next. The saved rooms were byte-identical throughout.

Driven in the real bundle, at phone and desktop width: repeated renders and the live loops with
nothing new keep the SAME screen (same nodes, a composer mid-sentence untouched); a real new message
still appears.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WATCH = r"""(()=>{window.__rebuilt=0;const feed=document.getElementById('feed');
  new MutationObserver(rs=>{for(const r of rs)if(r.target===feed&&[...r.addedNodes].some(n=>n.classList&&n.classList.contains('cc-app')))__rebuilt++;})
    .observe(feed,{childList:true});window.__root=feed.querySelector('.cc-app');})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_nothing_new_draws_nothing_and_something_new_still_appears(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(SETUP)
        await b.until("!!document.querySelector('.cc-messages') && document.querySelectorAll('.cc-messages .cc-message').length>0")
        await asyncio.sleep(1)
        await b.js(WATCH)
        # Somebody is typing while the loops run.
        await b.js("(()=>{const i=document.querySelector('#cc-input');if(i){i.value='half a sente';i.focus();window.__input=i;}})()")
        for _ in range(5):
            await b.js("PCConcord.backgroundRender(); PCConcord.render();")
        await asyncio.sleep(9)                       # two live ticks of the 4s loop
        st = await b.js("({rebuilt:__rebuilt, same:document.querySelector('#feed .cc-app')===__root, "
                        "input:!!window.__input && __input.isConnected && __input.value==='half a sente'})")
        assert st == {"rebuilt": 0, "same": True, "input": True}, ("the screen was rebuilt with nothing new to show", st)

        # Something new: it is drawn.
        await b.js("__msgs.push({id:'new'+'z'.repeat(61),pubkey:'b'.repeat(64),text:'a brand new message',at:999999,kind:9,tags:[]});"
                   "PCConcord.refreshActiveChannel(__PC);")
        await b.until("document.querySelector('.cc-messages').textContent.includes('a brand new message')")
    asyncio.run(desktop.with_browser("online", "", check))


def test_identical_rooms_with_reordered_keys_are_the_same_room():
    src = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
    start = src.index("  function _canon(v)")
    end = src.index("\n", src.index("  function sameJson(a,b)"))
    prog = src[start:end] + r"""
      const a={name:'Room',cord:{bundle:{owner:'x',relays:['r']},hydrated:true},channels:[{id:'g',name:'general'}]};
      const b={channels:[{name:'general',id:'g'}],cord:{hydrated:true,bundle:{relays:['r'],owner:'x'}},name:'Room'};
      console.log(JSON.stringify([sameJson(a,b), sameJson(a,{...b,name:'Other'}), sameJson(a,{...a,channels:[{id:'g',name:'general'},{id:'h',name:'x'}]})]));"""
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [True, False, False]

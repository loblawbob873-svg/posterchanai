"""A post opened in its own window shows at once -- from what the desktop that opened it already holds.

Reported: "opening a post is laggy on desktop". On PosterChanOS a post opens as its own window, a fresh
page with an empty memory: measured on a laptop, the window was up in about a second and the post
appeared 2.7-12.6s later, because the window re-read its cache from disk and, when routing ran first,
waited for the relays -- for a post the desktop was showing a moment earlier. The window now copies
the post, its context and its author's profile from its opener before routing. Here the relay never
answers for that post at all, so the only way it can be on screen is the hand-over.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PK = "a" * 64
POST = {"id": "b" * 64, "kind": 1, "pubkey": PK, "created_at": 1790000000, "tags": [],
        "content": "The desktop already had this post: HANDOVERCANARY", "sig": "c" * 128}
REPLY = {"id": "d" * 64, "kind": 1, "pubkey": "e" * 64, "created_at": 1790000100,
         "tags": [["e", "b" * 64, "", "root"], ["p", PK]], "content": "a reply the desktop held: REPLYCANARY", "sig": "c" * 128}
PROFILE = {"id": "f" * 64, "kind": 0, "pubkey": PK, "created_at": 1789000000, "tags": [],
           "content": json.dumps({"name": "Opener Alice"}), "sig": "c" * 128}

# The desktop: window.opener, holding the post, a reply and the author's profile in its Store.
OPENER = r"""(()=>{const evs=%s;
  const match=(e,f)=>(!f.kinds||f.kinds.includes(e.kind))&&(!f.authors||f.authors.includes(e.pubkey))
      &&(!f['#e']||(e.tags||[]).some(t=>t[0]==='e'&&f['#e'].includes(t[1])));
  const S={get:id=>evs.find(e=>e.id===id)||null, query:fs=>evs.filter(e=>fs.some(f=>match(e,f)))};
  try{ Object.defineProperty(window,'opener',{value:{Store:S},configurable:true}); }catch(_){ window.opener={Store:S}; }})();""" % json.dumps([POST, REPLY, PROFILE])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_post_window_shows_the_post_the_desktop_held_without_asking_the_relays():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.__PC && document.documentElement.classList.contains('pc-oswin')")
        t0 = await b.js("performance.now()")
        for _ in range(100):          # up to 10s to SEE it; the speed is asserted below (< 1.5s)
            if await b.js("/HANDOVERCANARY/.test((document.getElementById('feed')||{}).innerText||'')"):
                break
            await asyncio.sleep(0.1)
        got["ms"] = (await b.js("performance.now()")) - t0
        got["text"] = await b.js("(document.getElementById('feed')||{}).innerText||''")

    asyncio.run(desktop.with_browser("online", "?pcwin=doc:post:" + "b" * 64, check, OPENER))
    assert "HANDOVERCANARY" in got["text"], ("the post window stayed empty: it did not use what the desktop held", got["text"][:300])
    assert got["ms"] < 1500, ("the post appeared, but slowly", got["ms"])
    assert "Opener Alice" in got["text"], "the author's name was not handed over (the card reads anon)"
    assert "REPLYCANARY" in got["text"], "the replies the desktop held were not handed over"

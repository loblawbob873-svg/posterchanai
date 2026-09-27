"""Go Live → Choose a cover finds your images, and says the right thing when it can't.

Reported (issue b9f35e03, Android 16, browser): "I can't choose some thumbnail for live stream, I get
the error 'Couldn't reach your drive just now. Check your connection and try again.'" Measured: the
reporter's kind-10063 list starts with blossom.jumble.social, which a fresh device adopts as its media
server, and that server has no BUD-02 `/list` (404) -- while their images sat on this node's drive,
listing in 20 ms. The picker only ever asked the one server, and read "it answered 404" as "your
connection is down".

The SHIPPED picker (`__PC.pickDriveImage`, the Go Live cover picker) runs in the real bundled client;
only the `/list` answers are fixtures.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

EXTERNAL = "https://blossom.jumble.social"
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

LISTS = r"""
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),
  mediaServer:'%s/',blossomEnabled:true,mediaProto:'blossom'}));
window.__lists={external:'missing', builtin:'missing'};
const __f0=window.fetch;
window.fetch=function(url,opts){
  const u=String(url), m=u.match(/\/list\/[0-9a-f]{64}$/);
  if(!m) return __f0(url,opts);
  const which=u.startsWith('%s/')?'external':'builtin', a=window.__lists[which];
  if(a==='down') return Promise.reject(new TypeError('network down'));
  if(a==='missing') return Promise.resolve(new Response('404 Not Found',{status:404}));
  return Promise.resolve(new Response(JSON.stringify(a),{status:200,headers:{'Content-Type':'application/json'}}));
};
""" % (EXTERNAL, EXTERNAL)


def _blob(sha, n):
    return {"url": PNG + "#" + sha[:6], "sha256": sha, "size": 1, "type": "image/png", "uploaded": n}


A, B = "a" * 64, "b" * 64


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


async def _open(b, external, builtin):
    await b.js("document.querySelectorAll('.bp-modal #bp-x').forEach(x=>x.click())")
    await b.js("window.__lists=" + json.dumps({"external": external, "builtin": builtin}))
    await b.js("window.__PC.pickDriveImage(u=>{window.__picked_cover=u})")
    await b.until("(()=>{const g=document.querySelector('.bp-modal #bp-grid');"
                  "return g && !/Loading your images/.test(g.textContent) || !!(g&&g.querySelector('.bp-cell'))})()")
    await asyncio.sleep(0.3)
    return await b.js("""(()=>{const g=document.querySelector('.bp-modal #bp-grid');
        return {cells:[...g.querySelectorAll('.bp-cell')].map(c=>c.dataset.url), text:g.textContent.trim()}})()""")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_cover_picker_reads_your_drive_wherever_it_is():
    async def check(b):
        await desktop.login(b)

        # THE REPORTED CASE: your media server cannot list, this node's drive can.
        got = await _open(b, "missing", [_blob(A, 2)])
        assert len(got["cells"]) == 1, f"your images on this node's drive were not offered: {got}"
        assert "reach your drive" not in got["text"]

        # Both hold images, one of them twice: each image once.
        got = await _open(b, [_blob(A, 2), _blob(B, 1)], [_blob(A, 2)])
        assert len(got["cells"]) == 2, got

        # Nothing CAN list: say so, and name the server -- never "check your connection".
        got = await _open(b, "missing", "missing")
        assert got["cells"] == [] and "blossom.jumble.social" in got["text"] and "list" in got["text"], got
        assert "connection" not in got["text"], "a server that answered was blamed on the network"

        # Nothing answered at all: that one IS the connection, and it offers Retry.
        got = await _open(b, "down", "down")
        assert "reach your drive" in got["text"] and "Retry" in got["text"], got

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check, extra_init=LISTS))

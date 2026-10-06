"""A deep reply sits under the post it ANSWERS, not under the original post.

Reported: "replies seem definitely disjointed ... looking like they are replying earlier when they are
not ... if I click through further this is where it looks like he's replying further down into the
convo, which is right." The thread finds replies with `#e` queries against posts it already holds; a deep
reply (which tags the root) arrives, its direct parent may not -- and renderThread hung every such reply
off the ROOT, drawing it as an answer to the original post. Opening the reply itself climbs its ancestors,
which is why "clicking through" looked right.

Drives the shipped client: the relay is stubbed so `#e` returns the deep reply but not its parent.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

A_PK, B_PK = "a1" * 32, "b2" * 32
ROOT = {"id": "1" * 64, "pubkey": A_PK, "kind": 1, "created_at": 1790000000, "sig": "", "content": "the original post", "tags": []}
MID = {"id": "2" * 64, "pubkey": B_PK, "kind": 1, "created_at": 1790000100, "sig": "", "content": "a reply in the middle",
       "tags": [["e", ROOT["id"], "", "root"], ["p", A_PK]]}
DEEP = {"id": "3" * 64, "pubkey": A_PK, "kind": 1, "created_at": 1790000200, "sig": "", "content": "answering the middle",
        "tags": [["e", ROOT["id"], "", "root"], ["e", MID["id"], "", "reply"], ["p", B_PK]]}


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _open(parent_loadable):
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.__PC && !!window.Relay")
        stub = ("(()=>{const R=%s,M=%s,D=%s,loadable=%s;Store.saveEvent(R);"
                "Relay.query=async(filters)=>{const f=filters[0]||{};let out=[];"
                "if(f['#e']) out=f['#e'].includes(R.id)?[D]:[];"          # the deep reply comes back, its parent does not
                "else if(f.ids) out=[R,M,D].filter(e=>f.ids.includes(e.id)&&(e.id!==M.id||loadable));"
                "out.complete=true;return out;};return true;})()") % (
            json.dumps(ROOT), json.dumps(MID), json.dumps(DEEP), "true" if parent_loadable else "false")
        await b.js(stub)
        await b.js(f"__PC.openThread('{ROOT['id']}');true")
        await b.until(f"!!document.querySelector('.thread-node[data-tid=\"{DEEP['id']}\"]')")
        await asyncio.sleep(.3)
        got.update(await b.js("""(()=>{const n=id=>document.querySelector('.thread-node[data-tid="'+id+'"]');
            const m=e=>e?parseInt(e.style.marginLeft||'0',10):null;
            const order=[...document.querySelectorAll('.thread-node[data-tid]')].map(e=>e.dataset.tid);
            return {mid:m(n('%s')), deep:m(n('%s')), order, label:!!n('%s').querySelector('.reply-ctx')};})()"""
                              % (MID["id"], DEEP["id"], DEEP["id"])))

    asyncio.run(desktop.with_browser("online", "", check, ""))
    return got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_deep_reply_nests_under_the_parent_it_answers():
    got = _open(parent_loadable=True)
    assert got["mid"] is not None, ("the middle reply was never fetched", got)
    assert got["order"].index(MID["id"]) < got["order"].index(DEEP["id"]), got
    assert got["deep"] > got["mid"], ("the deep reply is drawn as answering the original post", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_reply_whose_parent_cannot_load_says_whom_it_answers():
    got = _open(parent_loadable=False)
    assert got["mid"] is None
    assert got["label"], ("a reply to an unloaded post is presented as a reply to the original post", got)

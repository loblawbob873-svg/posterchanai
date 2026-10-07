"""A conversation whose FIRST post is missing still shows everything that is there.

Reported: "how come i cant see entire thread" -- a fediverse conversation of 167 replies whose first
post was never bridged. Opening one reply showed that reply, "Replying to a post that couldn't be
loaded", and "0 replies": the climb to the top gave up at the missing first post without trying the
direct parent (which the relay held), and the reply expansion then only asked about the opened post,
so every other reply -- all of them tagging the missing first post -- was never requested.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


R, P, E = "1" * 64, "2" * 64, "3" * 64     # missing first post, held parent, the opened reply
INIT = r"""(()=>{const R='%s',P='%s',E='%s',now=Math.floor(Date.now()/1000);
const ev=(id,tags,c,t)=>({id,kind:1,pubkey:'a'.repeat(64),created_at:t,content:c,tags,sig:''});
const held=[ev(P,[['e',R,'','root']],'the parent PARENTCANARY',now-500),
  ev(E,[['e',R,'','root'],['e',P,'','reply']],'the opened reply OPENEDCANARY',now-400)];
for(let i=0;i<4;i++) held.push(ev(String(4+i).repeat(64),[['e',R,'','root'],['e',P,'','reply']],'sibling SIBCANARY'+i,now-300+i));
let Rl;
Object.defineProperty(window,'Relay',{configurable:true,get(){return Rl;},set(v){Rl=v;const real=Rl.query.bind(Rl);
  Rl.query=async(filters,...rest)=>{const f=filters&&filters[0]||{};
    if(f.ids||f['#e']){const out=held.filter(x=>(f.ids&&f.ids.includes(x.id))||(f['#e']&&x.tags.some(t=>t[0]==='e'&&f['#e'].includes(t[1]))));out.complete=true;return out;}
    return real(filters,...rest);};}});})();""" % (R, P, E)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_reply_in_a_thread_whose_first_post_is_missing_shows_the_whole_conversation():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openThread('%s');true" % E)
        for _ in range(60):
            t = await b.js("(document.getElementById('feed')||{}).innerText||''")
            if "SIBCANARY3" in t and "PARENTCANARY" in t:
                break
            await asyncio.sleep(0.2)
        got["text"] = await b.js("(document.getElementById('feed')||{}).innerText||''")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    t = got["text"]
    assert "PARENTCANARY" in t, ("the held parent was skipped because the first post was missing", t[:400])
    assert all(f"SIBCANARY{i}" in t for i in range(4)), ("the rest of the conversation was never asked for", t[:600])
    assert "couldn't be loaded" in t, "the missing start of the conversation is not said"

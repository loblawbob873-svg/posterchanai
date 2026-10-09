"""A fediverse reply whose parent nothing here holds says WHAT it replies to, in the feed and opened.

Reported 2026-10-08: "i can't tell what this post is" -- dj@parcero.casa answering a hashtag post on
detroitriotcity.com with a bare GIF. The inbox could not hold the parent, so it recorded it as an `r`
URL beside the event's `proxy ... activitypub` tag, and cards.js already draws "↩ reply to a post on
<host>" for exactly that. It never showed: the feed only wraps a reply label around what `isReply`
calls a reply, which counted `e` tags alone, and the post view drew the event as a thread ROOT, which
gets no label. A post with no visible context is indistinguishable from spam.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


E = "9" * 64
PARENT = "https://detroitriotcity.com/objects/4ca9adee-12a0-4f6a-99b9-540d1d577157"
INIT = r"""(()=>{const E='%s',now=Math.floor(Date.now()/1000);
const ev={id:E,kind:1,pubkey:'7'.repeat(64),created_at:now-60,content:'GIFCANARY',sig:'',
  tags:[['r','%s'],['fedibridge','https://parcero.casa/users/dj'],
        ['proxy','https://parcero.casa/objects/3de5','activitypub']]};
let Rl;
Object.defineProperty(window,'Relay',{configurable:true,get(){return Rl;},set(v){Rl=v;const real=Rl.query.bind(Rl);
  Rl.query=async(filters,...rest)=>{const f=filters&&filters[0]||{};
    if(f.ids||f['#e']){const out=(f.ids&&f.ids.includes(E))?[ev]:[];out.complete=true;return out;}
    return real(filters,...rest);};}});})();""" % (E, PARENT)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_opened_post_says_which_fediverse_post_it_answers():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openThread('%s');true" % E)
        for _ in range(60):
            if "GIFCANARY" in (await b.js("(document.getElementById('feed')||{}).innerText||''")):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(1.5)   # the thread's own expansion repaints after the head
        got["text"] = await b.js("(document.getElementById('feed')||{}).innerText||''")
        got["href"] = await b.js("((document.querySelector('#feed .reply-ctx-remote a'))||{}).href||''")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert "GIFCANARY" in got["text"], got["text"][:300]
    assert "reply to a post on detroitriotcity.com" in got["text"].lower(), ("no context on the opened post", got["text"][:400])
    assert got["href"] == PARENT


def test_the_feed_treats_it_as_a_reply_and_an_ordinary_link_as_not():
    js = r"""
const fs=require('fs'),vm=require('vm');const code=fs.readFileSync('static/js/client/app.js','utf8');
const i=code.indexOf('  function isReply(');const j=code.indexOf('\n  }\n',i)+4;
vm.runInThisContext(code.slice(i,j));
const fedi={kind:1,tags:[['r','%s'],['proxy','https://parcero.casa/objects/3de5','activitypub']]};
const link={kind:1,tags:[['r','https://example.com/article']]};
const proxiedTop={kind:1,tags:[['proxy','https://parcero.casa/objects/1','activitypub']]};
process.stdout.write(JSON.stringify([isReply(fedi),isReply(link),isReply(proxiedTop)]));
""" % PARENT
    out = json.loads(subprocess.run(["node", "-e", js], cwd=ROOT, capture_output=True, text=True,
                                    timeout=30, check=True).stdout)
    assert out == [True, False, False], out

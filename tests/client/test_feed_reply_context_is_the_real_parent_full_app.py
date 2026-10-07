"""The "↩ replying to …" label above a reply in the FEED names the post it actually answers.

Reported: "Feed reply context shows an earlier post instead of the real parent (thread view is right)."
Three conversations, each root (alice) → middle reply (bob) → a reply to the middle reply (carol). Under
the last reply a person must read "replying to bob", never alice (the root's author) or anybody quoted.

Two separate causes, both reproduced here:

  * The parent was resolved right but NAMED from a guess. With the parent not yet in the Store (the
    ordinary case on a feed — the parent is fetched after the card is drawn), the label named the
    reply's LAST `p` tag. This client and the ActivityPub bridge both write the parent's author FIRST
    and the earlier participants after it, so the last `p` is somebody EARLIER in the thread, and the
    label was never repainted once the parent landed. The thread view fetches the parent before it
    draws, which is why it was right.
  * The parent was resolved WRONG for two tag shapes: a `root`-marked tag plus an UNMARKED parent tag
    (nostr-tools' nip10.parse — and NIP-10 — read the unmarked one as the reply; we read the root), and
    a positional reply whose last `e` is a `mention` (a quote is never the parent).

Drives the shipped bundled client against a fake relay that serves the replies on the Global feed and
the parents only when asked for by id.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Each case: the reply's kind, its tags (templated with R=root id, M=middle id, Q=quoted id and
# a/b/d = alice/bob/dave pubkeys), and whom the label must name.
CASES = {
    "marked": (1, [["e", "R", "", "root"], ["e", "M", "", "reply"], ["p", "b"], ["p", "a"]], "bob"),
    "positional": (1, [["e", "R"], ["e", "M"], ["p", "b"], ["p", "a"]], "bob"),
    "root_marked_parent_unmarked": (1, [["e", "R", "", "root"], ["e", "M", ""], ["p", "b"], ["p", "a"]], "bob"),
    "positional_with_a_mention_quote": (1, [["e", "R"], ["e", "M"], ["e", "Q", "", "mention"], ["p", "b"], ["p", "a"], ["p", "d"]], "bob"),
    "marked_root_with_a_mention_quote": (1, [["e", "R", "", "root"], ["e", "M", "", "reply"], ["e", "Q", "", "mention"], ["p", "b"], ["p", "a"], ["p", "d"]], "bob"),
    "nip22_comment": (1111, [["E", "R", "", "a"], ["K", "1"], ["P", "a"], ["e", "M", "", "b"], ["k", "1111"], ["p", "b"], ["p", "a"]], "bob"),
    # sanity: a direct reply to the root names the root's author
    "reply_to_the_root": (1, [["e", "R", "", "root"], ["p", "a"], ["p", "d"]], "alice"),
}

RELAY = r'''
window.__CASES = %s;
window.__made = null;
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000);
  const key=i=>Uint8Array.from({length:32},(_,j)=>j===31?i:(j===30?3:0));
  const K={alice:key(11),bob:key(12),carol:key(13),dave:key(14)}, P={};
  for(const n in K) P[n]=NT.getPublicKey(K[n]);
  const prof={}; for(const n in K) prof[P[n]]=NT.finalizeEvent({kind:0,created_at:now-9999,tags:[],content:JSON.stringify({name:n})},K[n]);
  const byId={}, feed=[]; let t=now-3000;
  const put=e=>{byId[e.id]=e;return e;};
  for(const [name,[kind,tpl]] of Object.entries(window.__CASES)){
    const R=put(NT.finalizeEvent({kind:1,created_at:t++,tags:[],content:'ROOT of '+name},K.alice));
    const M=put(kind===1111
      ? NT.finalizeEvent({kind:1111,created_at:t++,tags:[['E',R.id,'',P.alice],['K','1'],['P',P.alice],['e',R.id,'',P.alice],['k','1'],['p',P.alice]],content:'MIDDLE of '+name},K.bob)
      : NT.finalizeEvent({kind:1,created_at:t++,tags:[['e',R.id,'','root'],['p',P.alice]],content:'MIDDLE of '+name},K.bob));
    const Q=put(NT.finalizeEvent({kind:1,created_at:t++,tags:[],content:'QUOTED in '+name},K.dave));
    const sub={R:R.id,M:M.id,Q:Q.id,a:P.alice,b:P.bob,d:P.dave};
    const tags=tpl.map(x=>x.map((v,i)=>i>0&&sub[v]!==undefined?sub[v]:v));
    feed.push(put(NT.finalizeEvent({kind,created_at:now-60-feed.length,tags,content:'LAST REPLY in '+name},K.carol)));
  }
  return (window.__made={prof,byId,feed,carol:P.carol}); }
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});}; const D=__mk();
  const now=[], late=[];
  for(const f of m.slice(2)){
    const kinds=f.kinds||null, has=k=>!kinds||kinds.includes(k);
    // The parents arrive only when asked for by id, and late: the card is drawn before they land.
    if(f.ids){ f.ids.forEach(x=>D.byId[x]&&late.push(D.byId[x])); continue; }
    if(f.authors&&has(0)) f.authors.forEach(a=>D.prof[a]&&now.push(D.prof[a]));
    if(f['#e']||f['#p']) continue;
    // carol's replies: on the Global timeline (no authors) and on her profile (authors=[carol])
    D.feed.filter(e=>has(e.kind)&&(!f.authors||f.authors.includes(e.pubkey))&&e.created_at<=(f.until||1e12)).forEach(e=>now.push(e));
  }
  now.forEach(e=>out(['EVENT',id,e]));
  if(late.length) setTimeout(()=>{ late.forEach(e=>out(['EVENT',id,e])); out(['EOSE',id]); },400);
  else out(['EOSE',id]); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
''' % json.dumps({k: [v[0], v[1]] for k, v in CASES.items()})

LABELS = r"""(()=>{const out={};
  for(const pair of document.querySelectorAll('#feed .reply-pair')){
    const txt=(pair.querySelector('article.note')||{}).textContent||'';
    const m=txt.match(/LAST REPLY in (\w+)/); if(!m) continue;
    const lbl=pair.querySelector('.reply-ctx');
    out[m[1]]=lbl?lbl.textContent.replace(/\s+/g,' ').trim():'(no label)';
  }
  return out;})()"""


async def _settle(b, cases):
    got = {}
    for _ in range(40):
        got = await b.js(LABELS)
        if all(got.get(k) == f"↩ replying to {CASES[k][2]}" for k in cases):
            break
        await asyncio.sleep(.25)
    return got


def _wrong(got, cases):
    return {k: got.get(k, "(not drawn as a reply)") for k in cases if got.get(k) != f"↩ replying to {CASES[k][2]}"}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_label_above_a_reply_names_the_post_it_answers():
    got = {}
    # The Global timeline draws kind 1 only; a profile's Replies tab also draws NIP-22 comments.
    timeline_cases = [k for k in CASES if CASES[k][0] == 1]

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        await b.until("[...document.querySelectorAll('#feed .reply-pair')].filter(p=>/LAST REPLY in/.test(p.textContent)).length>=%d" % len(timeline_cases))
        got["timeline"] = await _settle(b, timeline_cases)
        # The profile reads carol's posts from the Store (cache-first); the Global pass put her kind-1
        # replies there, and the NIP-22 comment is seeded the same way, as a relay would have.
        await b.js("__mk().feed.forEach(e=>Store.saveEvent(e));true")
        await b.js("__PC.openProfile(__mk().carol)")
        await b.until("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')")
        await b.js("document.querySelector('.prof-tab[data-tab=\"replies\"]').click()")
        await b.until("[...document.querySelectorAll('#feed .reply-pair')].filter(p=>/LAST REPLY in/.test(p.textContent)).length>=%d" % len(CASES))
        got["profile"] = await _settle(b, list(CASES))

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    wrong = {"timeline": _wrong(got["timeline"], timeline_cases), "profile replies": _wrong(got["profile"], list(CASES))}
    assert not wrong["timeline"] and not wrong["profile replies"], \
        ("the feed names the wrong post as the one being answered", wrong)

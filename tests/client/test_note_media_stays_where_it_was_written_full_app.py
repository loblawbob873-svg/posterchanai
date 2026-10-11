"""A post's pictures are drawn WHERE THE AUTHOR PUT THEM, in the order they were written.

Reported (baunilha): "When I paste an image in the middle of the text, it moves to the end (after the last
chunk of text). And if I add more than one image, regardless of where they are placed, the client creates an
image box where all of them appear together, completely ignoring the order I set them in. But when I click
preview, that shows exactly how it was supposed to be."

The card lifted every media URL out of the text into ONE gallery and put it above all the text (when the post
started with a picture) or below all of it (otherwise). The composer's Preview runs linkify over the raw text,
which leaves each picture in place -- hence the difference the reporter saw.

What a reader must see, on the timeline card:
  * a picture between two paragraphs stays between them;
  * a picture before / after the text stays before / after it;
  * only a RUN of pictures (nothing but whitespace between them) becomes one gallery, in their order --
    whether they are written on one line or one per line;
  * two pictures with text between them are two separate pictures, not one gallery.
And the composer's Preview of the middle case reads in the same order as the posted card.

Drives the shipped bundled client against a fake relay that serves these posts on the Global feed. The
order is read from the rendered DOM: every text block and every picture under the card's body, top to
bottom.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _img(n):
    return f"https://img.example/p{n}.png"


# name -> (content, expected sequence). "T:<text>" is a text block, "G:[...]" one gallery of pictures
# (in order), "I:<n>" a single picture on its own.
CASES = {
    "middle": (f"first chunk\n{_img(1)}\nlast chunk", ["T:first chunk", "I:1", "T:last chunk"]),
    "before": (f"{_img(2)}\nafter the picture", ["I:2", "T:after the picture"]),
    "after": (f"before the picture\n{_img(3)}", ["T:before the picture", "I:3"]),
    "run_one_line": (f"intro\n{_img(4)} {_img(5)} {_img(6)}\noutro", ["T:intro", "G:4,5,6", "T:outro"]),
    "run_lines": (f"{_img(7)}\n{_img(8)}\n{_img(9)}", ["G:7,8,9"]),
    "split": (f"one {_img(10)} two {_img(11)} three", ["T:one", "I:10", "T:two", "I:11", "T:three"]),
}

RELAY = r'''
window.__CASES = %s;
window.__made = null;
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000);
  const key=Uint8Array.from({length:32},(_,j)=>j===31?21:(j===30?5:0)), pk=NT.getPublicKey(key);
  const prof=NT.finalizeEvent({kind:0,created_at:now-9999,tags:[],content:JSON.stringify({name:'baunilha'})},key);
  const feed=Object.entries(window.__CASES).map(([name,content],i)=>NT.finalizeEvent(
    {kind:1,created_at:now-60-i,tags:[['t','case-'+name]],content},key));
  return (window.__made={prof,feed,pk}); }
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});}; const D=__mk();
  for(const f of m.slice(2)){
    const kinds=f.kinds||null, has=k=>!kinds||kinds.includes(k);
    if(f.ids){ D.feed.filter(e=>f.ids.includes(e.id)).forEach(e=>out(['EVENT',id,e])); continue; }
    if(f.authors&&has(0)&&f.authors.includes(D.pk)) out(['EVENT',id,D.prof]);
    if(f['#e']||f['#p']) continue;
    D.feed.filter(e=>has(e.kind)&&(!f.authors||f.authors.includes(e.pubkey))&&e.created_at<=(f.until||1e12)).forEach(e=>out(['EVENT',id,e]));
  }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
''' % json.dumps({k: v[0] for k, v in CASES.items()})

# The visible order of text blocks and pictures inside `root`: a text block is a .txt (its own text, which
# never includes a picture's URL), a picture is an <img> and a gallery is a .media-car (one entry, listing
# its pictures in order). Read in document order.
SEQ = r"""function __seq(root){
  const out=[], num=s=>(String(s).match(/p(\d+)\.png/)||[])[1];
  const walk=el=>{
    for(const c of el.children){
      if(c.classList.contains('media-car')){ out.push('G:'+[...c.querySelectorAll('img')].map(i=>num(i.getAttribute('src'))).join(',')); continue; }
      if(c.tagName==='IMG' && /p\d+\.png/.test(c.getAttribute('src')||'')){ out.push('I:'+num(c.getAttribute('src'))); continue; }
      if(c.classList.contains('txt')){
        // Pictures INSIDE a text block (the Preview draws them there) count as pictures, in place.
        let buf='';
        const flush=()=>{ const t=buf.replace(/\s+/g,' ').trim(); if(t) out.push('T:'+t); buf=''; };
        const tw=n=>{ for(const k of n.childNodes){
          if(k.nodeType===3) buf+=k.textContent;
          else if(k.tagName==='IMG' && /p\d+\.png/.test(k.getAttribute('src')||'')){ flush(); out.push('I:'+num(k.getAttribute('src'))); }
          else if(k.nodeType===1) tw(k); } };
        tw(c); flush(); continue;
      }
      if(c.classList.contains('acts') || c.classList.contains('hd')) continue;
      walk(c);
    }
  };
  walk(root); return out; }"""

CARDS = r"""(()=>{ %s
  const out={};
  for(const art of document.querySelectorAll('#feed article.note')){
    const ev=Store.get(art.dataset.id); if(!ev) continue;
    const t=(ev.tags.find(x=>x[0]==='t'&&/^case-/.test(x[1]))||[])[1]; if(!t) continue;
    out[t.slice(5)]=__seq(art.querySelector('.body'));
  }
  return out; })()""" % SEQ


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_pictures_render_in_the_order_they_were_written():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        await b.until("[...document.querySelectorAll('#feed article.note')].length>=%d" % len(CASES))
        for _ in range(20):
            got["cards"] = await b.js(CARDS)
            if len(got["cards"]) == len(CASES):
                break
            await asyncio.sleep(.25)
        # The composer's Preview of the middle case: the reporter's reference for "how it was supposed to be".
        await b.js("__PC.compose({text:%s})" % json.dumps(CASES["middle"][0]))
        await b.until("!!document.querySelector('.cmp-tab[data-t=\"preview\"]')")
        await b.js("document.querySelector('.cmp-tab[data-t=\"preview\"]').click()")
        await b.until("!!document.querySelector('#cmp-preview .txt')")
        got["preview"] = await b.js("(()=>{ %s; return __seq(document.querySelector('#cmp-preview')); })()" % SEQ)

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    cards = got["cards"]
    wrong = {k: {"expected": v[1], "drawn": cards.get(k)} for k, v in CASES.items() if cards.get(k) != v[1]}
    assert not wrong, ("pictures are not drawn where the post put them", wrong)
    assert got["preview"] == cards["middle"], \
        ("the posted card and the composer's Preview disagree", {"preview": got["preview"], "card": cards["middle"]})


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_a_phone_the_inline_pictures_keep_their_order_and_fit_the_screen():
    """The reporter is on a phone. At 390px the same cards must keep their order, and no picture,
    text block or gallery may reach past the screen (a picture in the middle of a post is the new
    shape on that screen: nothing drew one before)."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',
                     {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        await b.until("[...document.querySelectorAll('#feed article.note')].length>=%d" % len(CASES))
        await b.until("[...document.querySelectorAll('#feed article.note img')].some(i=>i.complete&&i.naturalWidth>0)")
        for _ in range(20):
            got["cards"] = await b.js(CARDS)
            if len(got["cards"]) == len(CASES):
                break
            await asyncio.sleep(.25)
        got["over"] = await b.js(r"""(()=>{ const vw=document.documentElement.clientWidth, bad=[], cards=[];
          // Measured against each CARD, not the screen: this harness is the desktop bundle, which may
          // offset the feed by its own chrome. A card no wider than a phone, and nothing in it reaching
          // past it, is the layout rule; a carousel's later slides sit off to the side by design.
          for(const art of document.querySelectorAll('#feed article.note')){
            const A=art.getBoundingClientRect(); cards.push(Math.round(A.width));
            for(const el of art.querySelectorAll('.body img, .body .txt, .body .media-car')){
              if(el.tagName==='IMG' && el.closest('.media-car')) continue;
              const r=el.getBoundingClientRect(); if(r.width&&(r.right>A.right+1||r.left<A.left-1)) bad.push([el.className||el.tagName, Math.round(r.left-A.left), Math.round(r.right-A.right)]); }
          }
          return {vw, bad, widest: Math.max(...cards)}; })()""")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    cards = got["cards"]
    wrong = {k: {"expected": v[1], "drawn": cards.get(k)} for k, v in CASES.items() if cards.get(k) != v[1]}
    assert not wrong, ("on a phone, pictures are not drawn where the post put them", wrong)
    assert not got["over"]["bad"] and got["over"]["widest"] <= got["over"]["vw"], "a post's media reaches past a phone screen: " + json.dumps(got["over"])

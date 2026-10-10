"""An MP3 posted on the timeline carries its track info in the event, and every client's version of
that post draws as the same track card.

Reported: "Posting an mp3 on the timeline has no track info like Amethyst/Ditto". What those two
actually read for a kind-1 audio `imeta` (their source, 2026-10-10):

  * Ditto  (soapbox-pub/ditto a111f824) writes `duration` -- src/lib/fileMetadata.ts `audioMeta()`,
    `String(Math.round(seconds * 1000) / 1000)` -- and reads title/artist/cover from the FILE.
  * Amethyst (vitorpamplona/amethyst 07295735) reads `image` as the player's artwork and `duration`
    as a double (commons/.../richtext/RichTextParser.kt: `artworkUri = … ImageTag`,
    `durationSeconds = … DurationTag … toDoubleOrNull()`), and NIP-94 `alt` as the description.
  * Neither puts title/artist/album in a kind-1 imeta; this client adds them for its own card.

The real client: the composer's own 📎 input is handed a real tagged MP3 (tests/fixtures/music/
tagged-tone.mp3: ID3v2.3 title/artist/album + a JPEG cover), Blossom is stubbed at the network
boundary, and the event that goes to the relay is read. Then that event, an Amethyst-shaped post and a
Ditto-shaped post are drawn by the real feed renderer (via Bookmarks).
"""
import asyncio
import base64
import io
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/music/tagged-tone.mp3"
TAGGED = FIXTURE.read_bytes()
CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Blossom at the network boundary. An image is "stored" at a file the bundle really serves, so the cover
# can actually load; audio lives on media.example, which answers range reads -- with the tagged bytes,
# with tagless bytes (Amethyst strips ID3), or not at all (our own post: the card must not NEED the file).
INIT = r"""
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
// The relay's stored events survive the reload below (the feed reads its bookmark list once per session).
Object.defineProperty(window,'__events',{configurable:true,set(v){localStorage.setItem('__fixtureEvents',JSON.stringify(v));},
  get(){try{return JSON.parse(localStorage.getItem('__fixtureEvents')||'[]');}catch(_){return [];}}});
window.__puts=[];window.__tagged=Uint8Array.from(atob('B64'),c=>c.charCodeAt(0));
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const u=String(url), m=String(opts.method||'GET').toUpperCase();
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();
    return new Response(JSON.stringify({...d,blossom_enabled:true}),{status:200,headers:{'Content-Type':'application/json'}});}
  if(u.includes('/client/blossom-access'))return new Response(JSON.stringify({allowed:true,whitelisted:true}),{status:200,headers:{'Content-Type':'application/json'}});
  if(m==='PUT'&&/\/upload$/.test(u)){
    const type=String((opts.headers||{})['Content-Type']||'');const b=new Uint8Array(await opts.body.arrayBuffer());
    let s='';for(const x of b)s+=String.fromCharCode(x);__puts.push({type,b64:btoa(s)});
    const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',b))).map(x=>x.toString(16).padStart(2,'0')).join('');
    const url=/^image\//.test(type)?location.origin+'/static/icon-512.png':'https://media.example/'+sha+'.mp3';
    return new Response(JSON.stringify({url,sha256:sha}),{status:200,headers:{'Content-Type':'application/json'}});}
  if(u.startsWith('https://media.example/tagged'))return new Response(__tagged,{status:206,headers:{'Content-Type':'audio/mpeg'}});
  if(u.startsWith('https://media.example/bare'))return new Response(__tagged.slice(375),{status:206,headers:{'Content-Type':'audio/mpeg'}});
  if(u.startsWith('https://media.example/'))return new Response('gone',{status:404});
  return prev.apply(this,arguments);};}
""".replace("B64", base64.b64encode(TAGGED).decode())

CARDS = r"""[...document.querySelectorAll('#feed .pc-track')].map(c=>{const img=c.querySelector('.pct-art img');
  return {src:c.dataset.src,title:c.querySelector('.pct-title').textContent,sub:c.querySelector('.pct-sub').textContent,
    time:c.querySelector('.pct-time').textContent,img:img?img.getAttribute('src'):null,
    cover:!!(img&&img.complete&&img.naturalWidth>0),play:!!c.querySelector('.pct-play')};})"""


def _imeta(tag):
    out = {}
    for part in tag[1:]:
        k, _, v = part.partition(" ")
        out.setdefault(k, v)
    return out


def _assert_published(got):
    ev = got["event"]
    audio = [t for t in ev["tags"] if t[0] == "imeta" and any(p.startswith("m audio/") for p in t[1:])]
    assert len(audio) == 1, ("the post has no imeta for its MP3", ev["tags"])
    im = _imeta(audio[0])
    assert im["url"] in ev["content"] and im["m"] == "audio/mpeg"
    assert im["x"] == __import__("hashlib").sha256(TAGGED).hexdigest() and im["size"] == str(len(TAGGED)), im
    assert 1.9 <= float(im["duration"]) <= 2.2, ("duration in seconds, Ditto's format", im)
    assert (im.get("title"), im.get("artist"), im.get("album")) == ("Night City Drive", "Neon Ghost", "Afterglow"), im
    assert im.get("alt") == "Night City Drive — Neon Ghost", im
    # The cover is its own blob, and `image` points at it (Amethyst's artwork).
    covers = [p for p in got["puts"] if p["type"].startswith("image/")]
    assert len(covers) == 1 and im.get("image") == got["origin"] + "/static/icon-512.png", (im, got["puts"])
    apic = TAGGED.index(b"\xff\xd8")              # the JPEG inside the APIC frame
    # Compared as PICTURES: the upload path strips metadata segments (exifstrip.js), so the bytes differ.
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(covers[0]["b64"]))) as a, Image.open(io.BytesIO(TAGGED[apic:])) as e:
        assert a.size == e.size == (16, 16) and a.convert("RGB").getpixel((8, 8)) == e.convert("RGB").getpixel((8, 8)), \
            "the uploaded cover is not the file's own picture"



@CHROME
def test_a_posted_mp3_publishes_its_track_info_and_draws_as_a_track_everywhere():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__publishOK=true")
        await b.js("__PC.compose({text:'new song'})")
        await b.until("!!document.querySelector('#cmp-file')")
        doc = await b.call("DOM.getDocument", {"depth": -1})
        node = await b.call("DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": "#cmp-file"})
        await b.call("DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(FIXTURE)]})
        await b.until("/https:\\/\\/media\\.example\\/[0-9a-f]{64}\\.mp3/.test(document.querySelector('#cmp').value)")
        await b.js("document.querySelector('#cmp-send').click()")
        await b.until("__published.some(e=>e.kind===1&&/media\\.example/.test(e.content))")
        ev = await b.js("__published.find(e=>e.kind===1&&/media\\.example/.test(e.content))")
        got["event"] = ev
        got["puts"] = await b.js("__puts.map(p=>({type:p.type,len:atob(p.b64).length,b64:p.b64}))")
        got["origin"] = await b.js("location.origin")
        _assert_published(got)          # here, so a missing tag is named before anything is drawn

        # Draw it, beside what Amethyst and Ditto publish for the same thing.
        await b.js("""(()=>{const me=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(7), now=Math.floor(Date.now()/1000);
          const ours=__published.find(e=>e.kind===1&&/media\\.example/.test(e.content));
          const amethyst=NostrTools.finalizeEvent({kind:1,created_at:now-50,content:'from amethyst https://media.example/bare-'+'a'.repeat(58)+'.mp3',
            tags:[['imeta','url https://media.example/bare-'+'a'.repeat(58)+'.mp3','m audio/mpeg','size 8000','image '+location.origin+'/static/icon-512.png','duration 213.5']]},other);
          const ditto=NostrTools.finalizeEvent({kind:1,created_at:now-40,content:'from ditto https://media.example/tagged-'+'d'.repeat(57)+'.mpga',
            tags:[['imeta','url https://media.example/tagged-'+'d'.repeat(57)+'.mpga','m audio/mpeg','size 8707','duration 2.04']]},other);
          const list=NostrTools.finalizeEvent({kind:10003,created_at:now-30,content:'',tags:[['e',ours.id],['e',amethyst.id],['e',ditto.id]]},me);
          window.__events=[ours,amethyst,ditto,list];})()""")
        await b.js("location.reload()")
        await asyncio.sleep(1)
        await b.until("!!window.__PC && !!__PC.me()")
        await b.js("__PC.switchView('bookmarks')")
        await b.until("document.querySelectorAll('#feed .pc-track').length===3")
        await b.until("[...document.querySelectorAll('#feed .pc-track .pct-title')].some(t=>t.textContent==='Night City Drive' && t.closest('.pc-track').dataset.src.includes('tagged-'))")
        await b.until("[...document.querySelectorAll('#feed .pc-track .pct-art img')].filter(i=>i.complete&&i.naturalWidth>0).length>=2")
        got["cards"] = await b.js(CARDS)
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, INIT))

    cards = {("ours" if "/media.example/" in c["src"] and "bare-" not in c["src"] and "tagged-" not in c["src"]
              else "amethyst" if "bare-" in c["src"] else "ditto"): c for c in got["cards"]}
    ours, ame, dit = cards["ours"], cards["amethyst"], cards["ditto"]
    # Ours: everything from the event -- its file is not even reachable here.
    assert (ours["title"], ours["sub"]) == ("Night City Drive", "Neon Ghost · Afterglow") and ours["cover"], ours
    assert ours["time"] == "0:02" and ours["play"], ours
    # Amethyst: its `image` is the cover and its `duration` the time; the stripped file has no title.
    assert ame["cover"] and ame["img"] == got["origin"] + "/static/icon-512.png" and ame["time"] == "3:33", ame
    # Ditto: the file's own tags (as Ditto itself reads them) plus the event's duration.
    assert (dit["title"], dit["sub"]) == ("Night City Drive", "Neon Ghost · Afterglow") and dit["cover"], dit
    assert dit["time"] == "0:02", dit
    assert not got["errors"], got["errors"]

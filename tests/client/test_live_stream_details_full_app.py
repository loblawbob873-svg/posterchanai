"""Go Live carries a description, tags, a language and a content warning -- and keeps them.

Asked for by a streamer: "I want to put more details into live like language, tags, details of live."
All four are standard tags on the NIP-53 kind-30311 (`summary`, `t`, the NIP-32 `L`/`l` pair,
NIP-36 `content-warning`), so other clients see them too. The trap is that the 30311 is REPLACEABLE
and re-signed in four places (announce, viewer count, end, the parked end sentinel): a field that is
not carried by every one of them is erased by the first viewer-count update, as the cover once was.

Runs the real Go Live form in the bundled client; only the ingest endpoint and the relay's answer are
fixtures. Then opens the stream as SOMEBODY ELSE sees it.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

INGEST = r"""
window.__posts=[]; window.__sentinels=[];
const __f1=window.fetch;
window.fetch=function(url,opts){
  const u=String(url);
  if(u.includes('/api/streams/ingest')) return Promise.resolve(new Response(JSON.stringify({enabled:true,
    rtmp_url:'rtmp://fixture.invalid/live', stream_key:'k3y', token:'tok123',
    hls_url:'https://fixture.invalid/hls/tok123/index.m3u8'}),{status:200,headers:{'Content-Type':'application/json'}}));
  if(u.includes('/api/streams/sentinel')){ try{ window.__sentinels.push(JSON.parse(opts.body)); }catch(_){}
    return Promise.resolve(new Response('{"ok":true}',{status:200,headers:{'Content-Type':'application/json'}})); }
  if(u.includes('/api/streams/')) return Promise.resolve(new Response('{"ok":true}',{status:200,headers:{'Content-Type':'application/json'}}));
  if(u.includes('/hls/')) return Promise.resolve(new Response('#EXTM3U',{status:200}));
  return __f1(url,opts);
};
"""


def _tags(ev):
    return [t for t in ev["tags"]]


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_live_details_are_published_kept_and_shown():
    async def check(b):
        await desktop.login(b)
        await b.js("""Relay.publish=async (ev)=>{ if(ev.kind===30311) __posts.push(ev); return {ok:true, msg:''}; }""")
        await b.js("document.getElementById('nav-golive').click()")
        await b.until("!!document.querySelector('#gl-summary')")
        await b.js("""(()=>{const r=document;
          r.querySelector('#gl-title').value='Drawing anime';
          r.querySelector('#gl-summary').value='Sketching tonight — requests open';
          r.querySelector('#gl-tags').value='#Anime, gaming ,anime, retro games';
          r.querySelector('#gl-lang').value='pt';
          const cw=r.querySelector('#gl-cw'); cw.checked=true; cw.dispatchEvent(new Event('change'));
          r.querySelector('#gl-cwr').value='flashing lights';
          const a=r.querySelector('#gl-announce'); if(a) a.checked=false;
          const obs=r.querySelector('input[name=gl-src][value=obs]'); if(obs){ obs.checked=true; obs.dispatchEvent(new Event('change',{bubbles:true})); }
          r.querySelector('#gl-go').disabled=false; r.querySelector('#gl-go').click(); })()""")
        await b.until("window.__posts.length>=1")
        ev = (await b.js("window.__posts"))[0]
        t = _tags(ev)
        assert ["title", "Drawing anime"] in t
        assert ["summary", "Sketching tonight — requests open"] in t, t
        # "change it to commas instead of spaces": commas separate, a tag of two words is one tag.
        assert [x[1] for x in t if x[0] == "t"] == ["anime", "gaming", "retrogames"], "tags not split on commas/normalised/deduped"
        assert ["L", "ISO-639-1"] in t and ["l", "pt", "ISO-639-1"] in t, t
        assert ["content-warning", "flashing lights"] in t, t

        # The parked end sentinel is one of the four re-signs: it must carry the details too, or the
        # stream's "ended" event erases them.
        await b.until("window.__sentinels.length>=1")
        sent = json.dumps(await b.js("window.__sentinels"))
        assert "Sketching tonight" in sent and '"l", "pt"' in sent.replace('","', '", "'), sent[:600]

        # Edit while live: one re-sign that changes the tags and keeps everything else.
        await b.until("!!document.querySelector('#st-editdet')")
        await b.js("document.querySelector('#st-editdet').click()")
        await b.until("!!document.querySelector('#gl-det-save')")
        assert await b.js("document.querySelector('#gl-tags').value") == "anime, gaming, retrogames", \
            "editing shows the saved tags with the separator the form asks for"
        await b.js("""document.querySelector('#gl-tags').value='anime, drawing';
                      document.querySelector('#gl-det-save').click()""")
        await b.until("window.__posts.some(e=>e.tags.some(t=>t[0]==='t'&&t[1]==='drawing'))")
        posts = await b.js("window.__posts")
        t2 = _tags(next(e for e in posts if ["t", "drawing"] in e["tags"]))
        # EVERY re-sign so far (announce, any viewer-count update, the edit) kept the description.
        assert all(["summary", "Sketching tonight — requests open"] in e["tags"] for e in posts), \
            [e["tags"] for e in posts]
        assert [x[1] for x in t2 if x[0] == "t"] == ["anime", "drawing"], t2
        assert ["summary", "Sketching tonight — requests open"] in t2 and ["l", "pt", "ISO-639-1"] in t2
        assert ["status", "live"] in t2 and ["title", "Drawing anime"] in t2

        # As somebody else sees it.
        other = dict(ev, pubkey="9" * 64, id="8" * 64)
        await b.js("window.__PC.openStream(" + json.dumps(other) + ")")
        await b.until("!!document.querySelector('.stream-view')")
        shown = await b.js("""({lang:(document.querySelector('.st-lang')||{}).textContent||'',
            tags:[...document.querySelectorAll('.st-tag')].map(x=>x.textContent),
            about:(document.querySelector('.stream-view .about')||{}).textContent||'',
            cw:!!document.querySelector('#st-cw'), veiled:!!document.querySelector('#st-video.st-veiled')})""")
        assert "Português" in shown["lang"] and shown["tags"] == ["#anime", "#gaming", "#retrogames"], shown
        assert "Sketching tonight" in shown["about"], shown
        assert shown["cw"] and shown["veiled"], "the content warning did not hold the player back"
        await b.js("document.querySelector('#st-cw-show').click()")
        assert await b.js("!document.querySelector('#st-video.st-veiled') && !document.querySelector('#st-cw')")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check, extra_init=INGEST))

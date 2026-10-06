"""📰 Newspaper clipping: a link summary drawn as a clipping, with the real link posted under it.

The owner: "I wanted it to look like a newspaper clipping with the actual link below the post, similar to
how we do the framed feature". It rides the 🖼️ framed-card pipeline (buildBgPost uploads the picture and
appends the link, makeCardPreview shows it first); these drive the shipped composer and look at the
picture itself:

  * the 📰 swatch is in the Background strip and its preview is a clipping -- light newsprint in the
    middle, the dark desk in the corners, ink where the masthead, headline and columns are;
  * the masthead names the LINK'S site and the first line is the headline (read from the pixels by
    comparing two drafts that differ only in those);
  * 🤖 AI → 📰 Newspaper clipping asks the summarizer for the newspaper shape and arms the 📰 style.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


STORY = ("Relay operators agree to meet on Friday\n\n"
         + "Operators of several public relays said on Monday they will meet on Friday at 3pm to agree on "
           "shared limits for spam. The meeting follows a week of heavy traffic. " * 6
         + "\n\nhttps://www.example-news.com/story/relays")

# The preview <img> as numbers: 1080 square, light paper in the middle, dark desk in the corners, and how
# much ink there is in the masthead band and in the body.
MEASURE = r"""(async()=>{const img=document.querySelector('.cmp-cardprev-img'); if(!img) return null;
  await (img.decode?img.decode():Promise.resolve());
  const c=document.createElement('canvas'); c.width=img.naturalWidth; c.height=img.naturalHeight;
  const x=c.getContext('2d'); x.drawImage(img,0,0); const W=c.width,H=c.height;
  const px=(a,b)=>{const d=x.getImageData(a,b,1,1).data;return (d[0]+d[1]+d[2])/3;};
  const ink=(y0,y1)=>{const d=x.getImageData(140,y0,W-280,y1-y0).data;let n=0;for(let i=0;i<d.length;i+=4) if((d[i]+d[i+1]+d[i+2])/3<90) n++;return n;};
  const band=(y0,y1)=>{const d=x.getImageData(140,y0,W-280,y1-y0).data;let s=0;for(let i=0;i<d.length;i+=4) s+=d[i]*3+d[i+1]*7+d[i+2]*11;return s;};
  return {w:W,h:H, paper:px(W/2,H*0.62)>150 || px(W*0.3,H*0.9)>150, corner:Math.max(px(6,6),px(W-6,H-6)),
          mast:ink(100,230), body:ink(500,950), mastSig:band(100,230), headSig:band(250,450)};})()"""


async def _compose(b, text):
    await desktop.login(b)
    await b.js("__PC.compose({text:%s});true" % __import__("json").dumps(text))
    await b.until("!!document.querySelector('#cmp-send') && !!document.querySelector('#cmp-bg-strip')")


async def _pick_paper(b):
    await b.js("(()=>{const s=document.querySelector('#cmp-bg-strip');s.classList.remove('hidden');"
               "[...s.querySelectorAll('.cmp-swatch')].find(x=>x.title==='newspaper').click();})();true")
    await b.until("!!document.querySelector('.cmp-cardprev-img') && document.querySelector('.cmp-cardprev-img').complete")
    await asyncio.sleep(.3)


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_the_clipping_is_newsprint_on_a_desk_with_masthead_headline_and_columns():
    got = {}

    async def check(b):
        await _compose(b, STORY)
        got["glyph"] = await b.js("([...document.querySelectorAll('#cmp-bg-strip .cmp-swatch')].find(x=>x.title==='newspaper')||{}).textContent||''")
        await _pick_paper(b)
        got["a"] = await b.js(MEASURE)
        got["note"] = await b.js("document.querySelector('#cmp-cardprev').textContent")
        # Same story, another site and another headline: the masthead band and the headline band change.
        await b.js("(()=>{const ta=document.querySelector('#cmp');ta.value=%s;ta.dispatchEvent(new Event('input',{bubbles:true}));})();true"
                   % __import__("json").dumps(STORY.replace("Relay operators agree to meet on Friday", "Storm closes the harbour")
                                              .replace("example-news.com", "harbourtimes.org")))
        await asyncio.sleep(1.2)
        got["b"] = await b.js(MEASURE)

    asyncio.run(desktop.with_browser("online", "", check))
    a, bb = got["a"], got["b"]
    assert got["glyph"] == "📰", got
    assert a and (a["w"], a["h"]) == (1080, 1080), a
    assert a["paper"] and a["corner"] < 70, ("not newsprint on a dark desk", a)
    assert a["mast"] > 1500 and a["body"] > 20000, ("no masthead or no story columns on the clipping", a)
    assert "link posts under it" in got["note"], got["note"]
    assert bb["mastSig"] != a["mastSig"], "the masthead does not name the link's site"
    assert bb["headSig"] != a["headSig"], "the first line is not the headline"


@CHROME
def test_ai_newspaper_clipping_asks_for_the_newspaper_shape_and_arms_the_style():
    got = {}

    async def check(b):
        await _compose(b, "https://www.example-news.com/story/relays")
        await b.js(r"""(()=>{window.__asked=[];const real=window.fetch;window.fetch=(u,o)=>{
          if(String(u).includes('/client/compose-from-url')){ __asked.push(JSON.parse(o.body));
            return Promise.resolve(new Response(JSON.stringify({text:'Relay operators agree to meet\n\nOperators said on Monday they will meet on Friday.\n\nhttps://www.example-news.com/story/relays'}),{status:200,headers:{'Content-Type':'application/json'}})); }
          return real(u,o);};})();true""")
        await b.js("document.querySelector('#cmp-ai').click();true")
        await b.until("[...document.querySelectorAll('.menu-pop button, .pop button, [role=menuitem]')].some(x=>/Newspaper clipping/.test(x.textContent))")
        await b.js("[...document.querySelectorAll('.menu-pop button, .pop button, [role=menuitem]')].find(x=>/Newspaper clipping/.test(x.textContent)).click();true")
        await b.until("__asked.length===1 && !!document.querySelector('#cmp-bg-strip .cmp-swatch.on[title=\"newspaper\"]')")
        got["asked"] = await b.js("__asked[0]")
        got["text"] = await b.js("document.querySelector('#cmp').value")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["asked"] == {"url": "https://www.example-news.com/story/relays", "style": "newspaper"}, got["asked"]
    assert got["text"].startswith("Relay operators agree to meet\n\n"), got["text"]

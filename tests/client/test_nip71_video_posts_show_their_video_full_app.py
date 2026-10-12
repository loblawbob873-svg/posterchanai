"""A NIP-71 VIDEO POST (kind 21/22) AND A NIP-68 PICTURE POST (kind 20) SHOW THEIR MEDIA, NOT ONLY THE CAPTION.

Reported 2026-10-12: "video is missing from this post and amethyst shows it but not posterchan" --
nevent1qqsfyk4hwxx…, a kind-22 short video from Amethyst. Its video is ONLY in the imeta tag
(url https://blossom.ditto.pub/<sha>.mp4, m video/mp4, dim 720x1280, blurhash); the content is the caption.
The client had no case for kinds 20/21/22, so noteCard drew the caption and nothing else -- it only turns
URLs found in the TEXT into media.

The shipped client opens each event as a thread; the card must hold a <video> (or <img>) whose source is the
imeta url, with the caption, and the reserved box must be the imeta's aspect (720x1280 is portrait).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SHA = "800bc687c1c1febca865979d53b5ec6c447088dc05cce2b965ded40608c9b176"
VIDEO = f"https://blossom.ditto.pub/{SHA}.mp4"
PIC = "https://image.example/abc.jpg"
CASES = {
    "kind22-short": dict(id="a" * 64, kind=22, content="enjoy the #beatboxing CAPTIONCANARY",
                         tags=[["imeta", f"url {VIDEO}", "m video/mp4", "dim 720x1280", f"x {SHA}",
                                "blurhash ]FC$fm00~q9F-;RiozD*t7V@4"], ["client", "Amethyst"]]),
    "kind21-video": dict(id="b" * 64, kind=21, content="a normal video CAPTIONCANARY",
                         tags=[["title", "TITLECANARY"], ["imeta", f"url {VIDEO}", "m video/mp4", "dim 1280x720"]]),
    "kind20-picture": dict(id="c" * 64, kind=20, content="a picture CAPTIONCANARY",
                           tags=[["imeta", f"url {PIC}", "m image/jpeg", "dim 800x600"]]),
}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("case", list(CASES))
def test_a_media_post_shows_its_media(case):
    ev = dict(CASES[case], pubkey="d" * 64, created_at=1_800_000_000, sig="")
    init = r"""(()=>{const held=[%s];let Rl;
Object.defineProperty(window,'Relay',{configurable:true,get(){return Rl;},set(v){Rl=v;const real=Rl.query.bind(Rl);
  Rl.query=async(filters,...rest)=>{const f=filters&&filters[0]||{};
    if(f.ids){const out=held.filter(x=>f.ids.includes(x.id));out.complete=true;return out;}
    if(f['#e']){const out=[];out.complete=true;return out;}
    return real(filters,...rest);};}});})();""" % json.dumps(ev)
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openThread('%s');true" % ev["id"])
        for _ in range(60):
            if await b.js("!!document.querySelector('#feed article[data-id=\"%s\"]')" % ev["id"]):
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)
        got.update(await b.js(r"""(()=>{const a=document.querySelector('#feed article[data-id="%s"]');if(!a)return {card:false};
          const v=a.querySelector('video'), i=[...a.querySelectorAll('img')].find(x=>/abc\.jpg/.test(x.src||x.dataset.src||''));
          const m=v||i, r=m&&m.getBoundingClientRect();
          return {card:true,text:a.innerText,
            video:v?(v.currentSrc||v.src||(v.querySelector('source')||{}).src||v.dataset.src||''):'',
            img:i?(i.src||i.dataset.src||''):'', w:r?r.width:0, h:r?r.height:0};})()""" % ev["id"]))

    asyncio.run(desktop.with_browser("online", "", check, init))
    assert got.get("card"), got
    assert "CAPTIONCANARY" in got["text"], got
    if ev["kind"] == 20:
        assert PIC in got["img"], ("the picture post drew no picture", got)
    else:
        assert VIDEO in got["video"], ("the video post drew no video", got)
        if ev["kind"] == 21:
            assert "TITLECANARY" in got["text"], ("the video's title is not shown", got)
        if ev["kind"] == 22:
            assert got["h"] > got["w"] > 0, ("a 720x1280 short is not drawn portrait", got)

"""Meme Builder: the image layer panel is tidy and fits a phone.

Asked for 2026-10-09: "The many many buttons on Image Layer is getting crowded and ugly. Please improve
and make sure good on mobile". Real client, a photo layer selected, at 360/390px phones and a desktop
window: the header's actions are icon buttons with accessible names, Fit/Fill/Back/Front are ONE row,
the AI/photo tools live in their own named group, and every control is a >=40px target on screen with
nothing scrolling sideways.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_meme_drawing_full_app import CHROME, _MEDIA, _open, media  # noqa: F401 (fixture)

AUDIT = r"""(()=>{const i=document.getElementById('mb-inspector');if(!i)return null;
  const vw=innerWidth, out={overflow:document.documentElement.scrollWidth>vw+1, bad:[]};
  const hdr=[...i.querySelectorAll('.mb-insp-acts button')];
  out.hdrText=hdr.map(b=>b.textContent.replace(/[⧉\s]/g,'')).filter(Boolean);
  out.hdrNames=hdr.map(b=>b.getAttribute('aria-label')||'');
  const q=['mb-fit','mb-fill','mb-back','mb-front'].map(id=>document.getElementById(id));
  out.quickRow=q.every(Boolean) && new Set(q.map(b=>Math.round(b.getBoundingClientRect().top))).size===1;
  const tools=document.getElementById('mb-nobg');
  out.toolsGrouped=!!tools && !!tools.closest('details.mb-sec');
  out.groupTitle=tools && tools.closest('details.mb-sec') ? tools.closest('details.mb-sec').querySelector('summary').textContent : '';
  i.querySelectorAll('button, input, select').forEach(e=>{
    if(e.checkVisibility && !e.checkVisibility()) return;
    if(e.type==='range'||e.type==='color'||e.type==='checkbox') return;
    const r=e.getBoundingClientRect();
    // A finger needs 40px; a mouse needs WCAG 2.5.8's 24px, measured AFTER the desktop's UI zoom.
    const min=matchMedia('(pointer:coarse)').matches||innerWidth<600?40:24;
    if(r.height<min||r.width<min) out.bad.push([e.id||e.getAttribute('aria-label')||e.className,'small',Math.round(r.width),Math.round(r.height)]);
    if(r.left<-1||r.right>vw+1) out.bad.push([e.id||e.className,'offscreen',Math.round(r.left),Math.round(r.right)]);
  });
  return out;})()"""


async def _panel(b):
    media = _MEDIA[0]
    await b.js("PCMeme.reset()")
    assert await b.js(f"PCMeme.addMedia({json.dumps(media.base + '/photo.png')},'image/png')")
    await b.until("!!document.getElementById('mb-inspector') && /Image layer/.test(document.getElementById('mb-inspector').textContent)")
    await b.js("document.getElementById('mb-inspector').scrollIntoView({block:'start'})")
    await asyncio.sleep(.3)
    return await b.js(AUDIT)


def _check(got, where):
    assert got, where
    assert not got["overflow"], (where, "the page scrolls sideways", got)
    assert got["hdrText"] == [], (where, "header actions still carry text labels", got)
    assert all(got["hdrNames"]), (where, "an icon button has no accessible name", got)
    assert got["quickRow"], (where, "Fit / Fill / Back / Front are not one row", got)
    assert got["toolsGrouped"] and "Photo tools" in got["groupTitle"], (where, "the photo tools are not grouped", got)
    assert not got["bad"], (where, "controls too small or off screen", got["bad"])


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
@pytest.mark.parametrize('size', [(360, 740), (390, 844)])
def test_image_layer_panel_on_a_phone(media, size):
    got = {}

    async def check(b):
        got.update(await _panel(b) or {})
    asyncio.run(_open(size[0], size[1], True, '', media, check))
    _check(got, size)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_image_layer_panel_on_a_desktop(media):
    got = {}

    async def check(b):
        got.update(await _panel(b) or {})
    asyncio.run(_open(1280, 800, False, '', media, check))
    _check(got, 'desktop')

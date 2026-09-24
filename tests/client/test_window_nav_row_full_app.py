"""The row above a profile or a post: Back, labelled, and the PosterChan avatar where IE3 kept its logo.

Reported: "make each PosterChanOS/desktop mode window look better, it's ugly now with the back button"
-- in a window the content opened with a strip holding one small "←" at the far left and nothing
else. It is a toolbar now (`_navTopHtml`, one builder for profile and post): a labelled pill Back on
the left, the avatar (`--pc-logo`, the brand logo) at the right, flush under the title bar and
sticky while the content scrolls. On a phone, where there is no window, the row stays the compact
arrow it was -- the label and the avatar exist only inside a desktop window.

Drives the SHIPPED bundle through the desktop icon, and measures geometry, not class names.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r'''(()=>{
  const row=document.querySelector('.osw .pc-navbar'); if(!row) return {row:false};
  const win=row.closest('.osw'), bar=win.querySelector('.osw-bar');
  const back=row.querySelector('.pc-nav-back'), label=row.querySelector('.pc-nav-label'), logo=row.querySelector('.pc-nav-logo');
  const r=row.getBoundingClientRect(), b=bar.getBoundingClientRect(), br=back.getBoundingClientRect(), lr=logo.getBoundingClientRect();
  const brand=(document.querySelector('.brand-logo')||{}).src||'';
  return {row:true, gap:Math.round(r.top-b.bottom), label:getComputedStyle(label).display!=='none' && label.textContent.trim(),
    logoShown:getComputedStyle(logo).display!=='none' && lr.width>=24,
    logoRight:Math.round(r.right-lr.right), backLeft:Math.round(br.left-r.left),
    logoImage:getComputedStyle(logo).backgroundImage, brand:brand.replace(location.origin,''),
    sticky:getComputedStyle(row).position};
})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_desktop_window_gets_a_toolbar_with_back_and_the_avatar():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("(()=>{const el=[...document.querySelectorAll('#os-desk [data-view]')].find(e=>/my profile/i.test(e.textContent||''));"
                   "el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));el.click();})();true")
        await b.until("!!document.querySelector('.osw .pc-navbar')")
        await asyncio.sleep(.5)
        got['win'] = await b.js(MEASURE)
        got['backWorks'] = await b.js("(()=>{const n=document.querySelectorAll('.osw').length;"
                                      "document.querySelector('.osw #prof-back').click();"
                                      "return new Promise(r=>setTimeout(()=>r(!document.querySelector('.osw #prof-back')||document.querySelectorAll('.osw').length<n),600))})()")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    w = got['win']
    assert w['row'], got
    assert w['label'] == 'Back', 'the Back button has no label in a window: %r' % w
    assert w['logoShown'], 'no avatar at the right of the row: %r' % w
    assert w['logoRight'] <= 16 and w['backLeft'] <= 16, 'Back and the avatar are not at the two ends: %r' % w
    assert w['brand'] and w['brand'] in w['logoImage'], 'the avatar is not the brand logo: %r' % w
    assert abs(w['gap']) <= 1, 'a gap between the title bar and the toolbar: %r' % w
    assert w['sticky'] == 'sticky', w
    assert got['backWorks'], 'Back no longer leaves the profile'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_outside_a_window_the_row_stays_the_compact_arrow():
    """The phone/web layout is unchanged: no label, no avatar."""
    html = (Path(__file__).resolve().parents[2] / 'static/css/client.css').read_text(encoding='utf-8')
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.__PC")
        got['phone'] = await b.js("(()=>{document.body.classList.remove('os-on');const d=document.createElement('div');"
                                  "d.innerHTML=" + json.dumps(
            '<div class="thread-top pc-navbar"><button class="pc-nav-back">←<span class="pc-nav-label">Back</span></button>'
            '<span class="pc-nav-logo"></span></div>') + ";document.body.appendChild(d);"
                                  "return {label:getComputedStyle(d.querySelector('.pc-nav-label')).display,"
                                  "logo:getComputedStyle(d.querySelector('.pc-nav-logo')).display}})()")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['phone'] == {'label': 'none', 'logo': 'none'}, got
    assert ':root{--pc-logo:' in html


ROW = ('<div class="thread-top pc-navbar"><button class="btn btn-ghost small pc-nav-back" id="prof-back" title="Back" '
       'aria-label="Back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg>'
       '<span class="pc-nav-label">Back</span></button><span class="pc-nav-logo" aria-hidden="true"></span></div>')


def test_the_row_markup_is_the_builders():
    """ROW below must stay the markup `_navTopHtml` writes, or the window test measures something else."""
    app = (Path(__file__).resolve().parents[2] / 'static/js/client/app.js').read_text(encoding='utf-8')
    for part in ('class="thread-top pc-navbar"', 'class="btn btn-ghost small pc-nav-back"',
                 '<span class="pc-nav-label">Back</span>', '<span class="pc-nav-logo" aria-hidden="true">'):
        assert part in app and part in ROW, part


POPPED = r'''(()=>{
  const row=[...document.querySelectorAll('.pc-navbar')].find(r=>r.offsetParent); if(!row) return {row:false};
  const bar=document.getElementById('pc-oswin-chrome');
  const back=row.querySelector('.pc-nav-back'), label=row.querySelector('.pc-nav-label'), logo=row.querySelector('.pc-nav-logo');
  const r=row.getBoundingClientRect(), b=bar?bar.getBoundingClientRect():{bottom:NaN}, br=back.getBoundingClientRect(), lr=logo.getBoundingClientRect();
  return {row:true, oswin:document.documentElement.classList.contains('pc-oswin'), osw:!!document.querySelector('.osw'),
    gap:Math.round(r.top-b.bottom), label:getComputedStyle(label).display!=='none' && label.textContent.trim(),
    logoShown:getComputedStyle(logo).display!=='none' && lr.width>=24,
    logoRight:Math.round(r.right-lr.right), backLeft:Math.round(br.left-r.left), sticky:getComputedStyle(row).position};
})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_posterchanos_window_gets_the_same_toolbar():
    """EVERY APP ON PosterChanOS IS A POPPED-OUT WINDOW -- its own document (`html.pc-oswin`), with no
    `.osw` in it. The row was gated on `.osw` alone, so it never appeared on the machine it was asked
    for, through several desktop releases, while the test above (a window INSIDE the desktop page)
    passed every time. This opens the client exactly as PosterChanOS opens an app: `?pcwin=`."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin') && !!document.getElementById('pc-oswin-chrome')")
        # The row as `_navTopHtml` writes it, in the window's own feed: what is under test is whether
        # the WINDOW's stylesheet dresses it, which is exactly what failed (the offline fixture has no
        # relay, so the profile itself never finishes loading here).
        await b.js("(()=>{const f=document.querySelector('#feed');f.innerHTML=" + json.dumps(ROW) +
                   "+'<div style=\"height:3000px\"></div>';return true})()")
        await asyncio.sleep(.3)
        got['win'] = await b.js(POPPED)
        # Measured against the SCROLLER's top edge -- the window keeps the view's own header above it.
        got['scrolled'] = await b.js("(()=>{const r=document.querySelector('.pc-navbar');let s=r.parentElement;"
                                     "while(s&&!(s.scrollHeight>s.clientHeight+10&&/auto|scroll/.test(getComputedStyle(s).overflowY)))s=s.parentElement;"
                                     "const top=()=>Math.round(r.getBoundingClientRect().top-s.getBoundingClientRect().top);"
                                     "const before=top();s.scrollTop=800;return {scroller:!!s,before,after:top()}})()")

    asyncio.run(desktop.with_browser('online', '?pcwin=profile', check, ''))
    sc = got['scrolled']
    assert sc['scroller'] and abs(sc['before']) <= 1 and abs(sc['after']) <= 1, \
        'the toolbar is not flush at the top of the window content, or does not stay there: %r' % got
    w = got['win']
    assert w['row'] and w['oswin'] and not w['osw'], got
    assert w['label'] == 'Back', 'no labelled Back in a PosterChanOS window: %r' % w
    assert w['logoShown'], 'no avatar at the right of the row in a PosterChanOS window: %r' % w
    assert w['logoRight'] <= 16 and w['backLeft'] <= 16, w
    assert w['sticky'] == 'sticky', w

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

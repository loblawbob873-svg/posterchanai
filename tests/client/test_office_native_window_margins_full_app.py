"""PosterChanOS: Office's Close / Save as PDF / Save As / Save row stays off the window border.

Reported: "PosterChan Office -> Close, Save as PDF, Save As, Save buttons -> bottom of buttons go
into window border". In a native PosterChanOS window (html.pc-oswin) the editor's host is BOTH
`.office-win` (10px padding) and `.office-view` (padding 0, a later rule) -- so it had no padding
at all: Save's bottom edge was 1px from the window's, its right edge 0px, and the compositor's accent
ring is drawn over exactly that strip. Measured here in that mode, through the shipped Files → Office
path (the same boundaries as test_office_close_files_full_app).
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_office_close_files_full_app as office


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_office_actions_keep_clear_of_a_native_windows_edges():
    async def check(b):
        await desktop.login(b)
        for sel in ['[data-host="1"]', '[data-p="/home/test/Reports"]', '[data-p="/home/test/Reports/report.odt"]',
                    '[data-ow=office]']:
            await b.until(f"!!document.querySelector({sel!r})")
            await b.js(f"document.querySelector({sel!r}).click()")
        await b.until("!!document.querySelector('#office-save')")
        assert await b.js("document.documentElement.classList.contains('pc-oswin')")
        m = await b.js(r'''(()=>{const r=e=>e.getBoundingClientRect();
          const acts=[...document.querySelectorAll('.office-actions .btn')].map(r);
          const head=r(document.querySelector('.office-head'));
          return {bottom:innerHeight-Math.max(...acts.map(a=>a.bottom)), right:innerWidth-Math.max(...acts.map(a=>a.right)),
                  left:Math.min(...acts.map(a=>a.left)), headLeft:head.left}})()''')
        for side in ('bottom', 'right'):
            assert m[side] >= 8, f'{side} gap {m[side]:.1f}px -- the buttons run into the window border: {m}'
        assert m['headLeft'] >= 8, f'the file name touches the left border: {m}'

    extra = office.BOUNDARIES + "window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, extra))


LOGO = r'''(()=>{const t=document.querySelector('.main>.topbar'); if(!t) return null;
  const a=getComputedStyle(t,'::after'), r=t.getBoundingClientRect();
  return {content:a.content, bg:a.backgroundImage, w:a.width, visible:r.height>0&&getComputedStyle(t).display!=='none',
          title:(document.querySelector('#view-title')||{}).textContent};})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_files_and_office_windows_carry_the_posterchan_avatar_top_right():
    """ "Files and Office should have PosterChan Avatar like Social and others in the top right corner" """
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('[data-host=\"1\"]')")
        files = await b.js(LOGO)
        for sel in ['[data-host="1"]', '[data-p="/home/test/Reports"]', '[data-p="/home/test/Reports/report.odt"]',
                    '[data-ow=office]']:
            await b.until(f"!!document.querySelector({sel!r})")
            await b.js(f"document.querySelector({sel!r}).click()")
        await b.until("!!document.querySelector('#office-save')")
        office = await b.js(LOGO)
        for name, m in (('Files', files), ('Office', office)):
            assert m and m['visible'], f'{name}: no title row: {m}'
            assert m['content'] not in ('none', 'normal') and 'posterchan-relay' in m['bg'] and m['w'] == '36px', \
                f'{name}: no PosterChan avatar at the end of the title row: {m}'

    extra = office.BOUNDARIES + "window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, extra))

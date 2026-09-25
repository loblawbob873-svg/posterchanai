"""PosterChanOS: Back in a post window closes it -- it never becomes a timeline that walks back into
the post it was first opened on.

Reported: "Social Back Button issue on OS: clicking back on a new post I opened brings me back to an
older post I was looking at every time". A post opens in its own native window (`?pcwin=doc:post:<id>`)
where the in-page frame logic does not exist (PCOS is off inside it), so Back fell through to the
timeline branch: the post window TURNED INTO a timeline, later posts opened from it joined its history,
and every Back from a new post went back to the first one. Driven as a SEQUENCE, because each single
step is defensible and only the order is wrong.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MK = r'''(async()=>{const me=__PC.me();window.__posts={};
  for(const n of ['A','B']){const ev=await __PC.signTemplate({kind:1,pubkey:me.pubkey,
    created_at:Math.floor(Date.now()/1000),tags:[],content:'post '+n});Store.saveEvent(ev);__posts[n]=ev.id;}
  window.__closed=0; window.close=()=>{window.__closed++;}; return __posts})()'''
STATE = r'''(()=>({view:__PC.VIEW,shown:(((document.querySelector('.thread-hl')||{}).dataset)||{}).tid||null,closed:window.__closed}))()'''
BACK = "(()=>{const b=document.querySelector('#th-back');b&&b.click();return !!b})()"


def _ctx(view):
    return "window.pcShell.windowContext={role:'app',view:'%s'};window.pcShell.backgroundOwner=false;" % view


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_back_closes_a_post_window_every_time():
    got = []
    view = 'doc:post:' + 'ab' * 32

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin')")
        ids = await b.js(MK)
        for n in ('A', 'B'):                     # the reported sequence: a post, Back, another post, Back
            await b.js(f"__PC.openThread(__posts['{n}'])")
            await b.until(f"(document.querySelector('.thread-hl')||{{dataset:{{}}}}).dataset.tid===__posts['{n}']")
            assert await b.js(BACK)
            await asyncio.sleep(.6)
            got.append((n, ids, await b.js(STATE)))

    asyncio.run(desktop.with_browser('online', '?pcwin=' + view, check, _ctx(view)))
    for n, ids, st in got:
        assert st['closed'] >= 1, f"Back from post {n} did not close the post window: {st}"
        assert st['view'] == 'thread' and st['shown'] == ids[n], \
            f"Back from post {n} navigated inside the window instead of closing it: {st}"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_back_in_a_social_window_returns_to_the_timeline_and_keeps_the_window():
    got = []

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin')")
        await b.js(MK)
        for n in ('A', 'B'):
            await b.js(f"__PC.openThread(__posts['{n}'])")
            await b.until(f"(document.querySelector('.thread-hl')||{{dataset:{{}}}}).dataset.tid===__posts['{n}']")
            assert await b.js(BACK)
            await b.until("__PC.VIEW!=='thread'")
            got.append((n, await b.js(STATE)))

    asyncio.run(desktop.with_browser('online', '?pcwin=global', check, _ctx('global')))
    for n, st in got:
        assert st['closed'] == 0, f"Back closed the Social window itself: {st}"
        assert st['view'] != 'thread' and not st['shown'], f"Back from {n} landed on a post: {st}"

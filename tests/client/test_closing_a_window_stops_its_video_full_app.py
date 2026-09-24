"""Closing a desktop window stops a video that was playing in it.

Reported: "I played video in a social post then closed the social post window and I hear the song
playing still". A window shows the SHARED #feed; closing it hands the feed back to its hidden home
(releaseFeed) -- a move, not a destruction -- so a playing video went on playing, invisible and with
no control anywhere. Measured before the fix: after ✕ the element was still connected, still in
#feed, and `paused === false`.

Drives the shipped bundle: a real window from the desktop icon, a real playing <video> (a canvas
stream, so no media file and no network), and the window's own ✕.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAY = """(async()=>{const f=document.querySelector('.osw #feed');const c=document.createElement('canvas');c.width=64;c.height=64;
  const g=c.getContext('2d');window.__pvT=setInterval(()=>{g.fillStyle='#'+Math.floor(Math.random()*16777215).toString(16);g.fillRect(0,0,64,64)},50);
  const v=document.createElement('video');v.muted=true;v.srcObject=c.captureStream(20);f.prepend(v);await v.play();
  window.__pv=v;return !v.paused})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_video_stops_when_its_window_is_closed():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("(()=>{const el=[...document.querySelectorAll('#os-desk [data-view]')].find(e=>/my profile/i.test(e.textContent||''));"
                   "el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));el.click();})();true")
        await b.until("!!document.querySelector('.osw #feed')")
        got['before'] = await b.js(PLAY)
        got['closed'] = await b.js("(()=>{const w=__pv.closest('.osw');const x=w.querySelector('[data-act=close],.osw-close,button[title=Close]');"
                                   "if(!x)return false;x.click();return true})()")
        await asyncio.sleep(1)
        got['after'] = await b.js("({playing:!__pv.paused, windows:document.querySelectorAll('.osw').length})")
        await b.js("clearInterval(__pvT)")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['before'] is True and got['closed'] is True, got
    assert got['after']['windows'] == 0, got
    assert got['after']['playing'] is False, "the video kept playing after its window was closed: %r" % got

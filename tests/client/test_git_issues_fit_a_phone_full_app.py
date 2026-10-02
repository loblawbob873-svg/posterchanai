"""A repo's Issues screen and its New issue dialog fit a phone's width — nothing pans sideways.

Reported with a screen recording (Android, 1080 px wide): "the width isn't auto/responsive". Frame by
frame the New issue dialog and the issue list behind it slide left and right under a finger, with a
horizontal scrollbar at the bottom: something on the page is wider than the screen. The issues carry
what real ones carry — long unbroken blossom URLs. The real bundled client, Android's CSS width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


OWNER = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"
SETUP = r"""
(() => {
  const now = Math.floor(Date.now()/1000), owner = '%s';
  const url = 'https://blossom.jumble.social/c9984669b680e8e3dbc143be3d6a151b6db4802b9e9ed2b11c0ba1d09cd6bef2.png';
  const repo = { id: 'a'.repeat(64), kind: 30617, pubkey: owner, created_at: now - 900, sig: 'e'.repeat(128), content: '',
    tags: [['d','posterchanai'],['name','PosterChanAI'],['description','A self-hosted app'],
           ['clone','https://poster.place/git/npub1fdtthaqtest/posterchanai.git'],['web','https://poster.place/r/npub1x/posterchanai']] };
  const issue = (i, subject, body) => ({ id: String(i+1).repeat(64).slice(0,64), kind: 1621, pubkey: 'c'.repeat(64),
    created_at: now - i*60, sig: 'e'.repeat(128), content: body,
    tags: [['a','30617:'+owner+':posterchanai'],['subject',subject],['p',owner]] });
  window.__events = [repo,
    issue(0, 'UI/UX: The "header" bar on app is cut off by the notification bar', 'This show like the image below:\n'+url+'\n\nAnd the width isn\'t auto/responsive.\n'+url.replace('.png','.mp4')),
    issue(1, 'Bug: Preview images out of place', 'See '+url+url),
    issue(2, 'Being followers seen?', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')];
  __PC.openRepo(repo);
})()
""" % OWNER

OVERFLOW = r"""
(() => {
  const vw = document.documentElement.clientWidth, se = document.scrollingElement;
  const wide = [...document.querySelectorAll('body *')].filter(el => {
    const r = el.getBoundingClientRect(); if (!r.width || getComputedStyle(el).position === 'fixed' && r.right <= vw + 1) return false;
    let p = el.parentElement; while (p) { const o = getComputedStyle(p).overflowX; if (o === 'hidden' || o === 'auto' || o === 'scroll' || o === 'clip') { if (p !== document.body && p !== document.documentElement) return false; } p = p.parentElement; }
    return r.right > vw + 1 || r.left < -1;
  }).slice(0, 8).map(el => (el.id ? '#'+el.id : el.tagName.toLowerCase()+'.'+[...el.classList].join('.')) + ' ' + Math.round(el.getBoundingClientRect().left) + '..' + Math.round(el.getBoundingClientRect().right));
  /* A scrolling container that is wider inside than out pans sideways under a finger -- the dialog's own
     backdrop is one (overflow:auto), so the page-level numbers above cannot see it. */
  const panning = [...document.querySelectorAll('.modal-bg, #feed, main')].filter(e => /auto|scroll/.test(getComputedStyle(e).overflowX) && e.scrollWidth > e.clientWidth + 1)
    .map(e => (e.id ? '#'+e.id : '.'+[...e.classList].join('.')) + ' ' + e.scrollWidth + '/' + e.clientWidth);
  return { vw, scroll: se.scrollWidth, body: document.body.scrollWidth, wide, panning };
})()
"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [280, 300, 320, 340, 360, 412])
def test_the_issues_screen_and_new_issue_dialog_do_not_pan_sideways(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=2.625, mobile=True))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("!!document.querySelector('#rv-newissue')")
        await b.js("document.querySelector('.rv-tab[data-tab=\"issues\"]').click()")
        await b.until("[...document.querySelectorAll('.rv-pane, [data-pane], #feed *')].some(e=>e.offsetParent && /cut off by the notification/.test(e.textContent||''))")
        await asyncio.sleep(.5)
        page = await b.js(OVERFLOW)
        assert page["scroll"] <= page["vw"] and not page["wide"] and not page["panning"], ("the Issues screen is wider than the phone", page)
        await b.js("document.querySelector('#rv-newissue').click()")
        await b.until("!!document.querySelector('#ri-attach')")
        await b.js("(()=>{const t=document.querySelector('.modal textarea, textarea');if(t){t.value='https://blossom.jumble.social/c9984669b680e8e3dbc143be3d6a151b6db4802b9e9ed2b11c0ba1d09cd6bef2.png';t.dispatchEvent(new Event('input'));}})()")
        modal = await b.js(OVERFLOW)
        assert modal["scroll"] <= modal["vw"] and not modal["wide"] and not modal["panning"], ("the New issue dialog pans sideways", modal)

    asyncio.run(desktop.with_browser("online", "", check))

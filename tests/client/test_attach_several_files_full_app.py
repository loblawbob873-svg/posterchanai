"""Posts and replies: attach SEVERAL files from 📁 Files at once.

Asked for: "Posts and Replies: should be able to add multiple files at once". Picking from the device
already took many (the file input is `multiple`, and the APK's picker honours it); 📁 Files -- the
drive picker -- took one and closed. From a post or reply composer a tap now toggles a file (numbered
in the order picked) and "Attach N" puts them all in, in that order. Every other picker caller (Texts,
Mail, Concord, ...) passes a callback for ONE file and keeps single-tap -- the last test pins that.

Driven in the shipped bundle, the drive listing served by a stub, at phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({"
         "...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

# Three files on the drive, newest first.
LISTING = r'''(()=>{const real=window.fetch;
  window.fetch=(u,o)=>String(u).includes('/list/')
    ? Promise.resolve(new Response(JSON.stringify([
        {url:'https://files.test/one.png',sha256:'1'.repeat(64),size:1234,type:'image/png',uploaded:1700000300},
        {url:'https://files.test/two.jpg',sha256:'2'.repeat(64),size:2234,type:'image/jpeg',uploaded:1700000200},
        {url:'https://files.test/three.mp4',sha256:'3'.repeat(64),size:3234,type:'video/mp4',uploaded:1700000100}]),
        {status:200,headers:{'content-type':'application/json'}}))
    : real(u,o);
  return true;})()'''

CARD = "[...document.querySelectorAll('.bp-pick-card')].find(c=>c.dataset.url.includes(%r))"


async def _pick_three(b):
    await b.until("document.querySelectorAll('.bp-pick-card').length===3")
    got = {"bar": await b.js("(()=>{const m=document.querySelector('.bp-multi-bar');const r=m.getBoundingClientRect();"
                              "return {shown:!m.hidden&&r.height>0,say:m.textContent,go:document.querySelector('.bp-multi-go').disabled,"
                              "inView:r.bottom<=innerHeight+.5&&r.right<=innerWidth+.5}})()")}
    # Pick three.mp4, then one.png, then two.jpg; untoggle and re-toggle one.png -> order three, two, one.
    for name in ("three.mp4", "one.png", "two.jpg", "one.png", "one.png"):
        await b.js((CARD % name) + ".click()")
    got["still_open"] = await b.js("!!document.querySelector('.bp-file-picker')")
    got["numbers"] = await b.js("[...document.querySelectorAll('.bp-pick-card')].map(c=>[c.dataset.url.split('/').pop(),c.dataset.pickNo,c.getAttribute('aria-pressed')])")
    got["go_label"] = await b.js("document.querySelector('.bp-multi-go').textContent")
    go = await b.js("(()=>{const r=document.querySelector('.bp-multi-go').getBoundingClientRect();return r.width>0&&r.bottom<=innerHeight+.5&&r.right<=innerWidth+.5})()")
    got["go_visible"] = go
    await b.js("document.querySelector('.bp-multi-go').click()")
    await b.until("!document.querySelector('.bp-file-picker')")
    return got


def _check(got, text):
    bar = got["bar"]
    assert bar["shown"] and bar["inView"] and "Tap files to select" in bar["say"] and bar["go"], bar
    assert got["still_open"], "a tap closed the picker instead of selecting"
    nums = {n: (no, pressed) for n, no, pressed in got["numbers"]}
    assert nums == {"three.mp4": ("1", "true"), "two.jpg": ("2", "true"), "one.png": ("3", "true")}, got["numbers"]
    assert got["go_label"] == "Attach 3" and got["go_visible"], got
    lines = [l for l in text.split("\n") if "files.test" in l]
    assert lines == ["https://files.test/three.mp4", "https://files.test/two.jpg", "https://files.test/one.png"], text


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("phone", [True, False])
def test_a_reply_attaches_several_files_from_the_drive(phone):
    got = {}

    async def check(b):
        w, h = (390, 844) if phone else (1280, 900)
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=w, height=h, deviceScaleFactor=1, mobile=phone))
        await desktop.login(b)
        await b.js(LISTING)
        await b.js("__PC.compose()")
        await b.until("!!document.querySelector('#cmp') && !!document.querySelector('#cmp-attach')")
        await b.js("document.querySelector('#cmp').value='look at these'")
        await b.js("document.querySelector('#cmp-attach').click()")
        await b.until("!!document.querySelector('.menu-pop [data-m=\"blossom\"]')")
        await b.js("document.querySelector('.menu-pop [data-m=\"blossom\"]').click()")
        got.update(await _pick_three(b))
        got["text"] = await b.js("document.querySelector('#cmp').value")

    asyncio.run(desktop.with_browser("online", "", check, PLAIN))
    _check(got, got["text"])
    assert got["text"].startswith("look at these\n"), "what was typed was lost"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_timeline_composer_attaches_several_files_from_the_drive():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(LISTING)
        await b.js("__PC.switchView('home')")
        await b.until("!!document.querySelector('#tl-cmp-attach')")
        await b.js("document.querySelector('#tl-cmp-attach').click()")
        await b.until("!!document.querySelector('.menu-pop [data-m=\"blossom\"]')")
        await b.js("document.querySelector('.menu-pop [data-m=\"blossom\"]').click()")
        got.update(await _pick_three(b))
        got["text"] = await b.js("document.querySelector('#tl-cmp-ta').value")

    asyncio.run(desktop.with_browser("online", "", check, PLAIN))
    _check(got, got["text"])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_caller_that_takes_one_file_still_takes_one_on_a_tap():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js(LISTING)
        await b.js("window.__one=[];__PC.blossomPicker(null,f=>__one.push(f.url))")
        await b.until("document.querySelectorAll('.bp-pick-card').length===3")
        got["bar_hidden"] = await b.js("document.querySelector('.bp-multi-bar').hidden")
        await b.js((CARD % "two.jpg") + ".click()")
        await b.until("!document.querySelector('.bp-file-picker')")
        got["picked"] = await b.js("__one")

    asyncio.run(desktop.with_browser("online", "", check, PLAIN))
    assert got["bar_hidden"] is True
    assert got["picked"] == ["https://files.test/two.jpg"], got["picked"]

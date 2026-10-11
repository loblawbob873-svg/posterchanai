"""FILING A REPORT: a post's ⋯ → Report, and a profile's ⋯ → Report user (NIP-56, kind 1984).

"we need a way to file reports on users posts. make sure works on fedi/nostr" (2026-10-11). The client
could only ever SHOW reports (notifications "🚩 reported you", a profile's "Reports received"); nothing
could file one. Real bundled client, real signer, real relay code -- only the relay socket is a fixture
(it records every EVENT and which relay it was written to).

What is checked, as the person sees it:
  * a post's ⋯ menu offers Report; picking "Spam" + a comment and submitting publishes EXACTLY the NIP-56
    shape -- ['e', <post>, 'spam'] and ['p', <author>, 'spam'], the comment as content -- to the relays;
  * the sheet says out loud that a Nostr report is public, and offers Mute afterwards (which works);
  * a profile's ⋯ menu offers Report user, which publishes ['p', <pubkey>, <type>] and no `e` tag;
  * a guest sees the action DISABLED with the reason, and nothing is signed;
  * at 360px (the narrowest phone) the sheet fits: nothing past the right edge, Submit reachable.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

SHOTS = Path(os.environ.get("PC_REPORT_SHOTS", "/tmp/pc-report-shots"))


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


EXTRA = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__publishOK=true;window.__wire=[];
const _send=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='EVENT')__wire.push({url:this.url,kind:m[1].kind,id:m[1].id});}catch(_){}return _send.call(this,raw)};
'''

SEED = r'''(()=>{
  const other=new Uint8Array(32).fill(2),now=Math.floor(Date.now()/1000);
  window.__target=NostrTools.finalizeEvent({kind:1,created_at:now,content:'Buy cheap followers now',tags:[]},other);
  window.__events=[__target];Store.saveEvent(__target);
  __PC.timelineTop('global');
})()'''


async def _shot(b, name):
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        import base64
        data = (await b.call("Page.captureScreenshot", {"format": "png"}))["data"]
        (SHOTS / name).write_bytes(base64.b64decode(data))
    except Exception:
        pass                                   # a picture is evidence, never a pass/fail condition


async def _open_post_report(b):
    sel = "'.note[data-id=\"'+__target.id+'\"] .act[data-a=menu]'"
    await b.until("!!document.querySelector(" + sel + ")")
    await b.js("document.querySelector(" + sel + ").click()")
    await b.until("!!document.querySelector('.menu-pop [data-m=report]')")


async def _fits(b, width):
    geo = await b.js("""(()=>{const m=document.querySelector('.modal .rep-form').closest('.modal'),r=m.getBoundingClientRect(),
        s=document.querySelector('#rep-submit').getBoundingClientRect();
        return {left:r.left,right:r.right,sub:{left:s.left,right:s.right,bottom:s.bottom,w:s.width,h:s.height},
                vh:innerHeight,note:document.querySelector('.rep-note').getBoundingClientRect().bottom,
                page:document.documentElement.scrollWidth,
                labels:[...document.querySelectorAll('.rep-type')].map(l=>{const q=l.getBoundingClientRect();return [q.left,q.right,q.height]})}})()""")
    assert geo["left"] >= 0 and geo["right"] <= width + 0.5, geo
    assert geo["page"] <= width, ("the page scrolls sideways with the report sheet open", geo)
    assert geo["sub"]["w"] > 40 and geo["sub"]["h"] > 20 and geo["sub"]["right"] <= width, geo
    # Submit and the "reports are public" notice are on screen without scrolling the sheet.
    assert geo["sub"]["bottom"] <= geo["vh"] and geo["note"] <= geo["vh"], ("Submit or the notice is below the fold", geo)
    assert all(l >= 0 and r <= width + 0.5 and h > 18 for l, r, h in geo["labels"]), geo
    return geo


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 360])
def test_report_a_post_publishes_a_nip56_report_to_the_relays(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 800, "deviceScaleFactor": 1, "mobile": width < 640})
        await desktop.login(b)
        await b.js(SEED)
        await _open_post_report(b)
        await b.js("document.querySelector('.menu-pop [data-m=report]').click()")
        await b.until("!!document.querySelector('.rep-form')")
        text = await b.js("document.querySelector('.rep-form').closest('.modal').textContent")
        assert "public" in text.lower(), "the sheet must say a Nostr report is public"
        for label in ("Spam", "Nudity", "Malware", "Profanity", "Illegal", "Impersonation", "Other"):
            assert label in text, (label, text)
        await _fits(b, width)
        await _shot(b, f"report-post-{width}.png")
        # Nothing chosen yet: Submit does nothing and says why -- never a report of type "".
        await b.js("document.querySelector('#rep-submit').click()")
        await asyncio.sleep(.3)
        assert not await b.js("__published.some(e=>e.kind===1984)")
        await b.js("""(()=>{document.querySelector('.rep-form input[value=spam]').click();
            const c=document.querySelector('#rep-comment');c.value='Selling fake followers';c.dispatchEvent(new Event('input',{bubbles:true}));
            document.querySelector('#rep-submit').click()})()""")
        await b.until("__published.some(e=>e.kind===1984)")
        rep = await b.js("__published.find(e=>e.kind===1984)")
        target = await b.js("__target")
        nip56 = [t for t in rep["tags"] if t[0] in ("e", "p", "L", "l", "x")]
        assert nip56 == [["e", target["id"], "spam"], ["p", target["pubkey"], "spam"]], rep["tags"]
        assert rep["content"] == "Selling fake followers"
        assert rep["pubkey"] == await b.js("__PC.me().pubkey")
        wire = await b.js("__wire.filter(w=>w.id===" + json.dumps(rep["id"]) + ").map(w=>w.url)")
        assert any("fixture.invalid" in u for u in wire), ("never written to this node's relay", wire)
        # After it lands: said so, and the person can mute the author from the same sheet.
        await b.until("!!document.querySelector('.rep-done')")
        await _shot(b, f"report-done-{width}.png")
        assert await b.js("!!document.querySelector('.rep-done #rep-mute')")
        await b.js("document.querySelector('#rep-mute').click()")
        await b.until("__published.some(e=>e.kind===10000&&e.tags.some(t=>t[0]==='p'&&t[1]===__target.pubkey))")
        assert not await b.js("__errors.length"), await b.js("__errors")
    asyncio.run(desktop.with_browser("online", "", check, EXTRA))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_report_a_user_from_their_profile():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 800, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        await b.js(SEED)
        sel = "'.note[data-id=\"'+__target.id+'\"] [data-prof]'"
        await b.until("!!document.querySelector(" + sel + ")")
        await b.js("document.querySelector(" + sel + ").click()")
        await b.until("!!document.querySelector('#prof-menu')")
        await b.js("document.querySelector('#prof-menu').click()")
        await b.until("!!document.querySelector('.menu-pop [data-m=report]')")
        assert "Report" in await b.js("document.querySelector('.menu-pop [data-m=report]').innerText")
        await b.js("document.querySelector('.menu-pop [data-m=report]').click()")
        await b.until("!!document.querySelector('.rep-form')")
        await b.js("""(()=>{document.querySelector('.rep-form input[value=impersonation]').click();
            document.querySelector('#rep-comment').value='Pretends to be the node operator';
            document.querySelector('#rep-submit').click()})()""")
        await b.until("__published.some(e=>e.kind===1984)")
        rep = await b.js("__published.find(e=>e.kind===1984)")
        target = await b.js("__target")
        nip56 = [t for t in rep["tags"] if t[0] in ("e", "p", "L", "l", "x")]
        assert nip56 == [["p", target["pubkey"], "impersonation"]], rep["tags"]
        assert rep["content"] == "Pretends to be the node operator"
    asyncio.run(desktop.with_browser("online", "", check, EXTRA))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_guest_sees_report_disabled_with_the_reason():
    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED)
        await _open_post_report(b)
        item = await b.js("""(()=>{const b=document.querySelector('.menu-pop [data-m=report]');
            return {disabled:b.disabled||b.getAttribute('aria-disabled')==='true',text:b.innerText,title:b.title}})()""")
        assert item["disabled"], item
        assert "log in" in (item["text"] + " " + item["title"]).lower(), item
        await b.js("document.querySelector('.menu-pop [data-m=report]').click()")
        await asyncio.sleep(.4)
        assert not await b.js("!!document.querySelector('.rep-form')"), "a guest was shown a form it cannot submit"
        assert not await b.js("__published.some(e=>e.kind===1984)")
    asyncio.run(desktop.with_browser("online", "", check, EXTRA))

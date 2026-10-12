"""VOTING "BAD POST OF THE DAY" from a post's ⋯ menu (NIP-32 label, kind 1985).

"users can vote for their 'Bad Post of the Day' by clicking on post hamburger menu" (2026-10-11).
Real bundled client, real signer, real relay code -- only the relay socket is a fixture (it records every
EVENT and which relay it was written to, and answers OK or refuses).

What is checked, as the person sees it:
  * a post's ⋯ menu offers "Vote Bad Post of the Day"; picking it publishes EXACTLY the label the bot
    counts -- ['L','pcai.badpost'], ['l','bad-post-of-the-day','pcai.badpost'], ['e',<post>],
    ['p',<author>] -- to THIS node's relay, and a toast says it went through;
  * the row is a toggle: reopened, it says "Withdraw", and withdrawing sends a NIP-09 deletion of that
    exact vote, after which it offers the vote again (a second tap never stacks votes);
  * a relay that REFUSES the vote is said out loud and the row is not flipped to "Withdraw";
  * your own post has no vote row; a guest sees the row disabled with the reason and nothing is signed;
  * the same at 360px (a phone).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


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
  window.__target=NostrTools.finalizeEvent({kind:1,created_at:now,content:'pineapple belongs on everything',tags:[]},other);
  window.__events=[__target];Store.saveEvent(__target);
  __PC.timelineTop('global');
})()'''

ROW = "document.querySelector('.menu-pop [data-m=badpost]')"


async def _open_menu(b, ev="__target"):
    sel = "'.note[data-id=\"'+" + ev + ".id+'\"] .act[data-a=menu]'"
    await b.until("!!document.querySelector(" + sel + ")")
    await b.js("document.querySelector('.menu-pop')&&document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))")
    await b.js("document.querySelector(" + sel + ").click()")
    await b.until("!!document.querySelector('.menu-pop [data-m]')")


async def _toasts(b):
    return await b.js("[...document.querySelectorAll('#toast-root .toast')].map(t=>t.textContent).join(' | ')")


@CHROME
@pytest.mark.parametrize("width", [1280, 360])
def test_vote_publishes_the_label_to_this_relay_and_toggles(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 800, "deviceScaleFactor": 1, "mobile": width < 640})
        await desktop.login(b)
        await b.js(SEED)
        await _open_menu(b)
        assert await b.js("!!" + ROW), "the ⋯ menu has no Bad Post vote"
        assert "Vote Bad Post of the Day" in await b.js(ROW + ".innerText")
        geo = await b.js("(()=>{const r=" + ROW + ".getBoundingClientRect();return [r.left,r.right,r.height,innerWidth]})()")
        assert geo[0] >= 0 and geo[1] <= geo[3] + 0.5 and geo[2] > 18, ("the row is off screen or squashed", geo)
        await b.js(ROW + ".click()")
        await b.until("__published.some(e=>e.kind===1985)")
        vote = await b.js("__published.find(e=>e.kind===1985)")
        target = await b.js("__target")
        assert vote["tags"] == [["L", "pcai.badpost"], ["l", "bad-post-of-the-day", "pcai.badpost"],
                                ["e", target["id"]], ["p", target["pubkey"]]], vote["tags"]
        assert vote["pubkey"] == await b.js("__PC.me().pubkey")
        wire = await b.js("__wire.filter(w=>w.id===" + json.dumps(vote["id"]) + ").map(w=>w.url)")
        assert wire and all("fixture.invalid" in u for u in wire), ("not sent to this node's relay", wire)
        await b.until("[...document.querySelectorAll('#toast-root .toast')].some(t=>/Voted Bad Post/.test(t.textContent))")
        # Reopened: the row withdraws, and withdrawing deletes exactly that vote.
        await _open_menu(b)
        assert "Withdraw" in await b.js(ROW + ".innerText")
        await b.js(ROW + ".click()")
        await b.until("__published.some(e=>e.kind===5)")
        dele = await b.js("__published.find(e=>e.kind===5)")
        assert ["e", vote["id"]] in dele["tags"] and ["k", "1985"] in dele["tags"], dele["tags"]
        await b.until("[...document.querySelectorAll('#toast-root .toast')].some(t=>/withdrawn/.test(t.textContent))")
        await _open_menu(b)
        assert "Vote Bad Post of the Day" in await b.js(ROW + ".innerText")
        assert await b.js("__published.filter(e=>e.kind===1985).length") == 1, "a second vote was stacked"
        assert not await b.js("__errors.length"), await b.js("__errors")
    asyncio.run(desktop.with_browser("online", "", check, EXTRA))


@CHROME
def test_a_refused_vote_is_said_and_not_marked():
    async def check(b):
        await desktop.login(b)
        await b.js(SEED)
        await b.js("window.__publishOK=false")
        await _open_menu(b)
        await b.js(ROW + ".click()")
        await b.until("__published.some(e=>e.kind===1985)")
        await b.until("[...document.querySelectorAll('#toast-root .toast')].some(t=>/couldn’t send the vote/.test(t.textContent))")
        await _open_menu(b)
        assert "Vote Bad Post of the Day" in await b.js(ROW + ".innerText"), "a refused vote reads as cast"
    asyncio.run(desktop.with_browser("online", "", check, EXTRA))


@CHROME
def test_no_vote_on_your_own_post_and_a_guest_cannot_vote():
    async def mine(b):
        await desktop.login(b)
        await b.js("""(()=>{const now=Math.floor(Date.now()/1000);
            window.__mine={kind:1,created_at:now,content:'my own post',tags:[],pubkey:__PC.me().pubkey,id:'f'.repeat(64),sig:'0'.repeat(128)};
            window.__events=[__mine];Store.saveEvent(__mine);__PC.timelineTop('global');})()""")
        await _open_menu(b, "__mine")
        assert not await b.js("!!" + ROW), "you can vote your own post Bad Post of the Day"
    asyncio.run(desktop.with_browser("online", "", mine, EXTRA))

    async def guest(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED)
        await _open_menu(b)
        item = await b.js("(()=>{const b=" + ROW + ";return b&&{disabled:b.disabled||b.getAttribute('aria-disabled')==='true',text:b.innerText,title:b.title}})()")
        assert item and item["disabled"] and "log in" in (item["text"] + " " + item["title"]).lower(), item
        await b.js(ROW + ".click()")
        await asyncio.sleep(.4)
        assert not await b.js("__published.some(e=>e.kind===1985)")
    asyncio.run(desktop.with_browser("online", "", guest, EXTRA))

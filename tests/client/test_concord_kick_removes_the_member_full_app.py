"""Kicking a member takes them off Known members at once.

"i just kicked a member out of the community and it does not work, he still there" (2026-10-16). A kick is a guestbook
event (kind 3309); the member list is folded from the guestbook THIS client holds, which is re-read at most once a
minute and never contained our own kick -- so after "Member kicked." the list was unchanged. Drives the real menu
(right-click -> Kick from community -> confirm) against a reader whose guestbook fold honours kicks, as the real one
does (concord_vector_parity_runtime.mjs proves the fold itself).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP
from tests.client.test_concord_member_search_full_app import MEMBERS


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


GUESTBOOK = r"""(()=>{const R=window.PosterCordReader, base=R.inspectControl;
  R.inspectControl=(...a)=>Object.assign({},base(...a),{guestbookPubkeys:['9'.repeat(64)]});
  window.__gb=[];R.inspectGuestbook=(bundle,controls,wraps,observed)=>{const kicked=new Set((wraps||[]).flatMap(w=>(w.tags||[]).filter(t=>t[0]==='p').map(t=>t[1])));
    const out=[...new Set((observed||[]).map(o=>o.pubkey))].filter(pk=>!kicked.has(pk));window.__gb.push((wraps||[]).length+':'+(observed||[]).length+'->'+out.length+' k='+[...kicked].map(k=>k.slice(0,6)).join(','));return {members:out,complete:false};};
  R.createKickWrap=async(_b,_w,target)=>{window.__kicked=target;return {rumorId:'k'.repeat(64),wrap:{kind:1059,id:'7'.repeat(64),pubkey:'9'.repeat(64),created_at:2000,tags:[['p',target]],content:'',sig:'0'.repeat(128)}};};
  window.__kicked=null;})()"""

NAMES = "[...document.querySelectorAll('.cc-members-pane .cc-member')].map(r=>r.dataset.ccMember)"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_kicked_member_leaves_known_members_at_once():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1500, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js(MEMBERS)
        await b.js(GUESTBOOK)
        await b.js("PCConcord.render()")
        await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=20")
        await asyncio.sleep(1.5)                                    # the guestbook entry exists (its first read ran)
        await b.js("PCConcord.render()")
        before = await b.js(NAMES)
        me = await b.js("__PC.me().pubkey")
        target = next(pk for pk in before if pk != me)
        x, y = await b.js("(()=>{const r=document.querySelector('.cc-members-pane .cc-member[data-cc-member=\"%s\"]').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % target)
        await b.call("Input.dispatchMouseEvent", dict(type="mousePressed", x=x, y=y, button="right", buttons=2, clickCount=1))
        await b.call("Input.dispatchMouseEvent", dict(type="mouseReleased", x=x, y=y, button="right", buttons=0, clickCount=1))
        await b.until("!!document.querySelector('[data-cc-member-kick]')")
        await b.js("document.querySelector('[data-cc-member-kick]').click()")
        KICK_BTN = "[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Kick'&&x.getBoundingClientRect().width>0)"
        await b.until("!!" + KICK_BTN)
        await b.js(KICK_BTN + ".click()")
        await b.until("window.__kicked")
        await asyncio.sleep(1.0)
        got.update(target=target, before=before, after=await b.js(NAMES), kicked=await b.js("window.__kicked"),gb=await b.js("window.__gb"),
                   toast=await b.js("(document.querySelector('.toast')||{}).textContent||''"))

    asyncio.run(desktop.with_browser("online", "", check, ""))
    assert got["kicked"] == got["target"], got
    assert got["target"] in got["before"], got
    assert got["target"] not in got["after"], ("the kicked member is still on Known members", got["toast"], got["gb"])

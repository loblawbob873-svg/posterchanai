"""Banning a member is ONE confirmation that names them -- unless the community has private channels.

"why is banning UI so complex? I only care about the person I am banning" (2026-10-16). The owner's ban opened
"Review community access": an editable npub list for the community, one per private channel, and an "I checked
every retained member" box -- for a decision that is only "who is banned". Without private channels everyone else
on Known members keeps access, so the ban is a single confirmation; private channels keep the review, because their
access is whoever holds the channel key and the community does not list them.
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


PRIVATE = r"""(()=>{const k='pc.concord.rooms.v1.'+__PC.me().pubkey,rooms=JSON.parse(localStorage.getItem(k));
  rooms[0].cord.bundle.channels=[{id:'e'.repeat(64),name:'staff',epoch:'1',key:'0'.repeat(64)}];localStorage.setItem(k,JSON.stringify(rooms));})()"""
VISIBLE = "[...document.querySelectorAll('%s')].find(x=>x.getBoundingClientRect().width>0)"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("private", [False, True])
def test_banning_asks_one_question(private):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1500, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js(MEMBERS)
        if private:
            await b.js(PRIVATE)
        await b.js("PCConcord.render()")
        await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=20")
        me = await b.js("__PC.me().pubkey")
        target = await b.js("[...document.querySelectorAll('.cc-members-pane .cc-member')].map(r=>r.dataset.ccMember).find(pk=>pk!==%r)" % me)
        x, y = await b.js("(()=>{const r=document.querySelector('.cc-members-pane .cc-member[data-cc-member=\"%s\"]').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % target)
        await b.call("Input.dispatchMouseEvent", dict(type="mousePressed", x=x, y=y, button="right", buttons=2, clickCount=1))
        await b.call("Input.dispatchMouseEvent", dict(type="mouseReleased", x=x, y=y, button="right", buttons=0, clickCount=1))
        await b.until("!!document.querySelector('[data-cc-member-ban]')")
        await b.js("document.querySelector('[data-cc-member-ban]').click()")
        await asyncio.sleep(.8)
        got['review'] = await b.js("!!" + VISIBLE % "[aria-label=\"Review community access\"]")
        got['text'] = await b.js("document.body.innerText.slice(-400)")
        if not got['review']:
            ban = "[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Ban'&&x.getBoundingClientRect().width>0)"
            got['one_button'] = await b.js("!!" + ban)
            await b.js(ban + ".click()")
            await asyncio.sleep(1.0)
            got['toast'] = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')")

    asyncio.run(desktop.with_browser("online", "", check, ""))
    if private:
        assert got['review'], ("a community with private channels must still review who keeps channel access", got['text'])
        return
    assert not got['review'], ("banning opened the access-review dialog", got['text'])
    assert "Ban alice?" in got['text'] and "keep access" in got['text'], got['text']
    assert got['one_button'], got
    # past the question: the ban reached key rotation (this fixture's reader has none, so it says so)
    assert "refounding is unavailable" in got['toast'], got['toast']

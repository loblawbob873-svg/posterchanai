"""Banning a member is ONE confirmation that names them, and everyone else keeps exactly the access they had.

"why is banning UI so complex? I only care about the person I am banning" (2026-10-16). The owner's ban opened
"Review community access": an editable npub list for the community, one per private channel, and an "I checked
every retained member" box -- for a decision that is only "who is banned" ("the ban UI should be a simple button to
ban like every other program"). Everyone else on Known members keeps the community; each private channel keeps the
members holding its channel role (CORD's own rule for who may hold a channel key) -- checked here by capturing what
the ban hands to key rotation.
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

# key rotation captured: the reader's inspectControl reports one channel role, granted to two members (one of them is
# the member being banned); prepareRefounding records what the ban asked for and stops there.
CAPTURE = r"""((target,staff)=>{const R=window.PosterCordReader,base=R.inspectControl;
  R.inspectControl=(...a)=>Object.assign({},base(...a),{controlPubkeys:[],roles:[{id:'r'.repeat(64),name:'staff',scope:'channel',channelId:'e'.repeat(64)}],
    grants:{[target]:['r'.repeat(64)],[staff]:['r'.repeat(64)]}});
  R.createBanWrap=async()=>({wrap:{kind:1059,id:'b'.repeat(64),pubkey:'9'.repeat(64),created_at:1,tags:[],content:'',sig:'0'.repeat(128)}});
  R.prepareRefounding=async(_prior,_controls,opts)=>{window.__opts=JSON.parse(JSON.stringify(opts));throw new Error('captured');};
  __PC.cordRekeyEncrypt=__PC.cordRekeyEncrypt||(async()=>'');})(%r,%r)"""
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
        others = await b.js("[...document.querySelectorAll('.cc-members-pane .cc-member')].map(r=>r.dataset.ccMember).filter(pk=>pk!==%r)" % me)
        target, staff = others[0], others[1]
        got.update(me=me, target=target, staff=staff, others=others)
        await b.js(CAPTURE % (target, staff))
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
            got['opts'] = await b.js("window.__opts||null")

    asyncio.run(desktop.with_browser("online", "", check, ""))
    assert not got['review'], ("banning opened the access-review dialog", got['text'])
    assert "Ban alice from this community?" in got['text'], got['text']
    assert got['one_button'], got
    o = got['opts']
    assert o, ("the ban never reached key rotation", got['toast'])
    kept = {r['pubkey'] for r in o['recipients']}
    assert o['removed'] == [got['target']] and got['target'] not in kept, o
    assert {got['me']} | set(got['others'][1:]) <= kept, "a member on Known members lost access to the community"
    if private:
        staff = {r['pubkey'] for r in o['channelRecipients']['e' * 64]}
        assert staff == {got['me'], got['staff']}, ("the private channel must keep exactly its role holders", staff)
    else:
        assert o['channelRecipients'] == {}, o

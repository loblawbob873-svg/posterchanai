"""Music: the "stop" actions on a shared playlist sit behind ⋯ instead of filling the screen.

Reported on Android: "buttons stop getting this playlist and stop receiving from matthew look bad and take up
a lot of UI space" -- two long red labels in the shared playlist's header beside Shuffle / Refresh / Keep, and a
full-width "Stop receiving from …" on every offer card. Driven at phone width in the shipped client with a REAL
share (a kind-30078 from "Matthew", NIP-44-sealed to the signed-in account, on the fixture relay):
  * the header and the offer card show no "Stop …" label, and the header's buttons fit on one row;
  * ⋯ opens a menu holding exactly those actions, and picking one runs it (the same confirm as before);
  * an accepted playlist's card offers a compact Remove instead of a full-width "Stop getting this playlist".
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SEED = r"""(()=>{
  const me=new Uint8Array(32).fill(1), mePk=NostrTools.getPublicKey(me), mat=new Uint8Array(32).fill(7), matPk=NostrTools.getPublicKey(mat);
  const now=Math.floor(Date.now()/1000), b64=u=>btoa(String.fromCharCode(...u));
  const track=i=>({s:(i+'a').repeat(32).slice(0,64), k:b64(new Uint8Array(32).fill(i+3)), iv:b64(new Uint8Array(12).fill(i+5)), n:'Song '+i, m:'audio/mpeg'});
  const body={v:1,id:'roadtrip1',name:'Road Trip',from:matPk,created:now,updated:now,tracks:[track(1),track(2),track(3)]};
  const ck=NostrTools.nip44.getConversationKey(mat, mePk);
  const ev=NostrTools.finalizeEvent({kind:30078,created_at:now-60,content:NostrTools.nip44.encrypt(JSON.stringify(body),ck),
    tags:[['d','pcai:musicshare:roadtrip1:'+mePk.slice(0,16)],['p',mePk],['l','pcai-musicshare']]},mat);
  const prof=NostrTools.finalizeEvent({kind:0,created_at:now-999,tags:[],content:JSON.stringify({name:'Matthew'})},mat);
  const r=_rel(); r.push(ev,prof); localStorage.setItem('__relayEvents',JSON.stringify(r));
  window.__shareKey=matPk+':roadtrip1';
})()"""

VISIBLE_STOP = "[...%s.querySelectorAll('button')].filter(b=>b.getClientRects().length && /stop/i.test(b.textContent)).length"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_stop_actions_are_behind_more_on_a_phone():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 860, "deviceScaleFactor": 2, "mobile": True})
        await b.js(SEED)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('music'); true")
        await b.until("!!window.PCMusicShare")
        await b.js("(async()=>{ await PCMusicShare.loadIn(); })()")
        for _ in range(100):
            if await b.js("PCMusicShare.pendingShares().length>0"):
                break
            await asyncio.sleep(.1)
        # The offer card (Shared with me).
        await b.js("window.__box=document.createElement('div'); __box.id='box'; document.getElementById('feed').prepend(__box); PCMusicShare.renderIn(__box,{}); true")
        await b.until("!!document.querySelector('#box .msh-offer')")
        got["offer_stop_visible"] = await b.js(VISIBLE_STOP % "document.querySelector('#box .msh-offer')")
        got["offer_buttons"] = await b.js("[...document.querySelectorAll('#box .msh-offer button')].filter(b=>b.getClientRects().length).map(b=>b.textContent.trim())")
        await b.js("document.querySelector('#box .msh-offer .msh-more').click(); true")
        await b.until("!!document.querySelector('.menu-pop')")
        got["offer_menu"] = await b.js("document.querySelector('.menu-pop').innerText")
        await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); true")
        # The shared playlist itself (its header).
        await b.js("window.__pl=document.createElement('div'); __pl.id='pl'; document.getElementById('feed').prepend(__pl); PCMusicShare.renderShared(__shareKey, __pl, {}); true")
        await b.until("!!document.querySelector('#pl .music-head-primary')")
        got["head_stop_visible"] = await b.js(VISIBLE_STOP % "document.querySelector('#pl .music-head-primary')")
        got["head_rows"] = await b.js("""(()=>{const bs=[...document.querySelectorAll('#pl .music-head-primary > button')].filter(b=>b.getClientRects().length);
            const tops=bs.map(b=>b.getBoundingClientRect().top); return tops.filter(t=>t-Math.min(...tops)>6).length+1})()""")
        await b.js("document.querySelector('#pl .msh-more').click(); true")
        await b.until("!!document.querySelector('.menu-pop')")
        got["head_menu"] = await b.js("document.querySelector('.menu-pop').innerText")
        # Picking "Stop getting this playlist" runs the real action (its confirm appears).
        await b.js("[...document.querySelectorAll('.menu-pop button, .menu-pop [role=menuitem], .menu-pop .menu-item')].find(x=>/Stop getting/.test(x.textContent)).click(); true")
        for _ in range(100):      # 10s: the confirm is a lazily-built modal on a loaded gate
            if await b.js("!!document.querySelector('.modal-bg, .ui-confirm, [role=dialog]')"):
                break
            await asyncio.sleep(.1)
        got["confirm"] = await b.js("(document.querySelector('.modal-bg, .ui-confirm, [role=dialog]')||{innerText:''}).innerText")
        got["overflow"] = await b.js("document.documentElement.scrollWidth > window.innerWidth + 1")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    assert got["offer_stop_visible"] == 0, ("a Stop label still fills the offer card", got["offer_buttons"])
    assert "Stop receiving from" in got["offer_menu"], got["offer_menu"]
    assert got["head_stop_visible"] == 0, "a Stop label is still in the playlist header"
    assert got["head_rows"] == 1, ("the header's buttons wrap onto more than one row", got["head_rows"])
    assert "Stop getting this playlist" in got["head_menu"] and "Stop receiving from" in got["head_menu"], got["head_menu"]
    assert got["confirm"], "picking the menu item did nothing"
    assert not got["overflow"]

"""Communities: the Known members list can be searched, and a repaint keeps the search.

Asked for: "add a Search for Known members. will be useful for large rooms". Real bundled client; the
room is a local fixture with 20 members, each with a profile name.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP

NAMES = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi", "ivan", "judy",
         "mallory", "niaj", "olivia", "peggy", "rupert", "sybil", "trent", "victor", "walter", "zelda"]

MEMBERS = r"""(()=>{const names=%s;window.__msgs=names.map((n,i)=>{const sk=new Uint8Array(32).fill(i+20);const pk=NostrTools.getPublicKey(sk);
  Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:1000,tags:[],content:JSON.stringify({name:n,nip05:n+'@example.com'})},sk));
  return {id:'m'+String(i).padStart(3,'0')+'x'.repeat(60),pubkey:pk,text:'hello from '+n,at:1000+i,kind:9,tags:[]};});})()""" % str(NAMES).replace("'", '"')

SHOWN = r"""(sel=>[...document.querySelectorAll(sel+' .cc-member')].filter(r=>!r.classList.contains('hidden')).map(r=>(r.querySelector('b')||{}).textContent))"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_known_members_can_be_searched_and_the_search_survives_a_repaint():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1500, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js(MEMBERS)
        await b.js("PCConcord.render()")
        await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=20")
        assert await b.js("!!document.querySelector('.cc-members-pane .cc-member-search')"), "no search box on a 20-member room"
        await b.js("document.querySelector('.cc-members-pane .cc-member-search').focus()")
        await b.call("Input.insertText", {"text": "zel"})
        assert await b.js(SHOWN + "('.cc-members-pane')") == ["zelda"]
        # A message arriving repaints the whole room; the search, its result and the caret stay.
        await b.js("PCConcord.render()")
        await asyncio.sleep(.2)
        got = await b.js("({shown:" + SHOWN + "('.cc-members-pane'), value:document.querySelector('.cc-members-pane .cc-member-search').value,"
                         " focused:document.activeElement===document.querySelector('.cc-members-pane .cc-member-search')})")
        assert got == {"shown": ["zelda"], "value": "zel", "focused": True}, got
        # By NIP-05 too, and a miss says so.
        await b.js("(()=>{const i=document.querySelector('.cc-members-pane .cc-member-search');i.value='grace@example';i.dispatchEvent(new Event('input'));})()")
        assert await b.js(SHOWN + "('.cc-members-pane')") == ["grace"]
        await b.js("(()=>{const i=document.querySelector('.cc-members-pane .cc-member-search');i.value='nobody-here';i.dispatchEvent(new Event('input'));})()")
        assert "No member matches" in await b.js("document.querySelector('.cc-members-pane').textContent")

    asyncio.run(desktop.with_browser("online", "", check))

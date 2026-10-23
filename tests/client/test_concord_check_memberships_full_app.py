"""Concord's community list has "Check my communities", and on a phone it opens a readable report.

Reported 2026-10-09: "I don't see the developers room on phone", after the vault held the room and
its invite resolved from a desktop. Every step of the membership pass fails into a silent
`continue`, and there is no device here to debug, so the phone has to say what it measured. The
report logic is driven in concord_runtime.mjs; this proves the button is reachable on the shipped
page at phone and desktop width, that it opens a report with real lines in it (not an empty modal),
and that the report stays inside the screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_check_my_communities_opens_a_report_on_the_community_list(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.getElementById('cc-check-memberships')")
        await b.js("document.getElementById('cc-check-memberships').click()")
        await b.until("((document.getElementById('cc-mcheck-out')||{}).textContent||'').length>0")
        got.update(await b.js("""(()=>{const o=document.getElementById('cc-mcheck-out'),r=o.getBoundingClientRect(),
            btn=document.getElementById('cc-check-memberships').getBoundingClientRect();
          return {text:o.textContent, inside:r.left>=0&&r.right<=innerWidth+1, btnInside:btn.right<=innerWidth+1&&btn.width>0,
                  copy:!document.getElementById('cc-mcheck-copy').hidden}})()"""))

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["btnInside"], got
    assert "Membership documents found:" in got["text"], ("the report says nothing it measured", got["text"][:300])
    assert got["inside"], ("the report runs off the screen", got)
    assert got["copy"], "no way to copy the report off the phone"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_restore_brings_back_a_community_this_device_recorded_leaving():
    """The "I left and rejoined, and it never comes back on this device" shape (2026-10-22): the account's membership
    list holds the community, but this device remembers leaving it, so it stays hidden. The check names that and
    offers Restore; Restore forgets the device's record and the community is back in the list."""
    import json
    from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=850, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Came back')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        # The account's membership list is written to this node's relay, which keeps it and serves it back.
        await b.js("window.__publishOK=true; PCConcord.persistArmadaMembership(__PC,__cordRoom)")
        await b.until("(window.__published||[]).some(e=>e.kind===33302||e.kind===30078||e.kind===10009)")
        await b.js("window.__events=[...(window.__events||[]),...__published]; __relaySeed(__published); 1")
        # This device recorded leaving it, and it is gone from the list here.
        await b.js("(()=>{const pk=__PC.me().pubkey;PCConcord.rememberLeftCommunity(pk,__cordRoom);"
                   "localStorage.setItem('pc.concord.rooms.v1.'+pk,'[]');localStorage.removeItem('pc.concord.active.v1.'+pk);PCConcord.render();})()")
        await b.until("!!document.getElementById('cc-discovery')")
        await b.js("document.getElementById('cc-discovery').click()")
        await b.until("!!document.getElementById('cc-check-memberships')")
        await b.js("document.getElementById('cc-check-memberships').click()")
        await b.until("((document.getElementById('cc-mcheck-out')||{}).textContent||'').includes('Decrypted')")
        await b.until("/Came back: hidden: this device recorded leaving it/.test((document.getElementById('cc-mcheck-out')||{}).textContent||'')")
        await b.until("!document.getElementById('cc-mcheck-fix').hidden")
        await b.js("document.getElementById('cc-mcheck-fix').click()")
        await b.until("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]').some(r=>r.name==='Came back')")
        await b.until("/Came back: shown/.test(document.getElementById('cc-mcheck-out').textContent)")
        assert not await b.js("__errors"), await b.js("__errors")

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser("online", "", check, extra))

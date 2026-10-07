"""A NIP-05 is `name@domain`: a profile that put its npub there does not show the 63-character key as its handle.

Reported: "see profile, why long npub there?" -- npub1rfnr93… had written its own npub into the NIP-05 field
(through Edit Profile, which accepted anything), and every place that shows a NIP-05 drew the whole key.
The display now shows only a real `name@domain`, and Edit Profile refuses anything else with a sentence
(keeping the editor open, like the Monero/BCH address checks beside it).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PK = "1a6632c7fba955b8d822d7f06d307f26a58544be46c2560297540994a18f2252"
NPUB = "npub1rfnr93lm492m3kpz6lcx6vrly6jc2397gmp9vq5h2syefgv0yffqs0kksp"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_npub_in_the_nip05_field_is_not_drawn_and_cannot_be_saved():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("Store.saveProfile({id:'9'.repeat(64),kind:0,pubkey:'%s',created_at:1790000000,tags:[],"
                   "content:JSON.stringify({name:'Justyn',nip05:'%s'}),sig:''});true" % (PK, NPUB))
        await b.js("__PC.openProfile('%s');true" % PK)
        await b.until("!!document.querySelector('.prof-nip05s')")
        await asyncio.sleep(.5)
        got["header"] = await b.js("(document.querySelector('.pbody')||document.body).innerText")
        # Edit Profile: an npub in the field is refused, with a sentence, and nothing is published.
        await b.js("window.__pub=0; const r=__PC.publish; try{ __PC.publish=(...a)=>{__pub++; return r?r(...a):null}; }catch(_){ } true")
        me = await b.js("__PC.me().pubkey")
        await b.js("__PC.openProfile(%s);true" % json.dumps(me))
        await b.until("!!document.getElementById('edit-prof')")
        await b.js("document.getElementById('edit-prof').click();true")
        await b.until("!!document.getElementById('pf-nip05')")
        await b.js("document.getElementById('pf-nip05').value=%s;document.getElementById('pf-save').click();true" % json.dumps(NPUB))
        await asyncio.sleep(.6)
        got["still_open"] = await b.js("!!document.getElementById('pf-nip05')")
        got["toast"] = await b.js("(document.querySelector('.toast, #toast, .pc-toast')||{}).textContent||''")

    asyncio.run(desktop.with_browser("online", "", check))
    assert NPUB not in got["header"], ("the npub is drawn as the profile's NIP-05", got["header"][:300])
    assert got["still_open"], "Edit Profile saved an npub as a NIP-05"
    assert "name@domain" in got["toast"], ("no sentence said why", got["toast"])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_profile_saved_elsewhere_with_an_npub_opens_the_editor_without_it():
    """npub1rfnr93… did not type it here: a profile from another app (no client tag, the day before) already
    held the npub, and Edit Profile carried it into every save. The editor now opens with the field empty
    (the granted name fills it when there is one), so Save goes through instead of being refused."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        me = await b.js("__PC.me().pubkey")
        npub = await b.js("NostrTools.nip19.npubEncode(__PC.me().pubkey)")
        await b.js("Store.saveProfile({id:'8'.repeat(64),kind:0,pubkey:%s,created_at:Math.floor(Date.now()/1000),tags:[],"
                   "content:JSON.stringify({name:'Justyn',nip05:%s}),sig:''});true" % (json.dumps(me), json.dumps(npub)))
        await b.js("__PC.openProfile(%s);true" % json.dumps(me))
        await b.until("!!document.getElementById('edit-prof')")
        await b.js("document.getElementById('edit-prof').click();true")
        await b.until("!!document.getElementById('pf-nip05')")
        await asyncio.sleep(.5)
        got["field"] = await b.js("document.getElementById('pf-nip05').value")
        got["name"] = await b.js("document.getElementById('pf-name').value")
        got["npub"] = npub

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["name"] == "Justyn", ("the editor did not open on the stored profile", got)
    assert got["field"] != got["npub"], "Edit Profile opened with the npub in the NIP-05 field"
    assert got["field"] == "" or "@" in got["field"], got

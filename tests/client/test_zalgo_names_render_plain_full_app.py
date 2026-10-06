"""A "Zalgo" display name is shown as its letters -- and an owner's own name is never rewritten.

Reported: "the text in his name is not displaying correctly, some new font or something". It was no
font: every letter of the name was buried under 5-12 stacked combining marks, which smear across the
card and the timeline. Store.profile() -- what every card, header and mention reads -- now hands out a
copy with those runs removed. The owner's profile editor republishes what it loads, so it reads the RAW
profile (Store.profileRaw): taming for display must never change anybody's published name.
Real accents are left alone: they are precomposed by NFC, or at most two marks per letter (Vietnamese),
and Thai / Devanagari marks are other Unicode blocks.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


ZALGO = ("P͓̬͐̆̇ͣ͊Ủ͍̋͑͂̏M̸̝̖"
         "͌̆O̶̱̹̹N̤͙̖̺")
PK = "e" * 64
REAL = ["Zoë", "Nguyễn Thị", "สมชาย", "देवनागरी", "Åsa"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_zalgo_name_is_shown_as_its_letters_and_real_names_are_untouched():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        names = json.dumps([ZALGO] + REAL)
        await b.js("""(()=>{const ns=%s; ns.forEach((n,i)=>Store.saveProfile({pubkey:(''+i).padStart(64,'e'),kind:0,
             created_at:1700000000+i,content:JSON.stringify({name:n,display_name:n}),tags:[]})); return true;})()""" % names)
        got["shown"] = await b.js("""%s.map((n,i)=>Store.profile((''+i).padStart(64,'e')).name)""" % names)
        got["raw"] = await b.js("(Store.profileRaw||Store.profile)('%s').name" % ("e" * 63 + "0"))
        await b.js("__PC.openProfile('%s');true" % ("e" * 63 + "0"))
        await b.until("!!document.querySelector('.pbody h2')")
        got["header"] = await b.js("(document.querySelector('.pbody h2')||{}).textContent")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["shown"][0] == "PUMON", got["shown"][0]
    assert got["shown"][1:] == REAL, got["shown"][1:]
    assert "PUMON" in (got["header"] or ""), ("the profile header still draws the marks", got["header"])
    assert got["raw"] == ZALGO, "the raw profile (what the owner's editor republishes) was changed"

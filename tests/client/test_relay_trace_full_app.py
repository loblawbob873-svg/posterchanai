"""Profile ⋯ → "Trace on this relay" shows an admin WHY an account is on the relay, in the real app.

Reported 2026-10-08: "i suspect this user is spam ... how did it make it to my relay!" and then "maybe we
need a trace feature in the profile hamburger menu so we can easily figure this out next time, nice ui".
The answer that day took a hand-run crawl of the web of trust. This drives the shipped bundle as an admin:
open a profile, ⋯, Trace — and asserts the panel shows the verdict, the signals, the voucher chain and the
followers the SERVER computed (the canned answer is relay_trace.explain()'s own output, so the client is
checked against the real shape, not a copy of it), that the request is a signed admin proof bound to that
account, and that the panel fits a phone.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services import relay_trace
from app.services.nostr.event import build_event
from app.services.nostr import nostr_service
from tests.client import test_desktop_offline_full_app as desktop

ME_PK = build_event(bytes([1] * 32), 1, "x")["pubkey"]
ME_NPUB = nostr_service.npub_of(ME_PK)
SPAM = "a54a1e6952b308be6defb06c0ec3596919c07dbdcce33baaaaef83bdf569ada7"
VOUCHER = "78b512a29311693e5357c4cf2e8a3552ed58af3d8582da439df45ed524df9bfe"
SEEDED = "b05ddaa79926f85b23723a8938cfe432d84ec0d7a9b3137d979af6d0877da8a7"
ZW = "‌⁠​" * 50

FACTS = {
    "pubkey": SPAM,
    "rows": {"groups": [(1, "wot", 434, 1791408302, 1791474187)], "wot": (1, 1791432699),
             "tier_row": ([3, 6], 1791432699, 3, 3),
             "recent": [("Anyone else excited? " + ZW, '[["t","webmesh-v1-nodes"]]', 1791474187 - i * 150)
                        for i in range(40)],
             "profile": None},
    "followers": [VOUCHER, SEEDED, "c" * 64],
    "tiers": {VOUCHER: [2, 14], SEEDED: [1, 3]},
    "chain": [{"pubkey": VOUCHER, "tier": [2, 14]}, {"pubkey": SEEDED, "tier": [1, 3]}],
}
ANSWER = relay_trace.explain(FACTS)

INIT = r"""
window.__traceReq=null; window.__blockMany=[];
const __prevFetch=window.fetch;
window.fetch=async function(url,opts={}){
  const u=String(url);
  if(u.includes('/client/relay-trace')){ window.__traceReq=JSON.parse(opts.body||'{}');
    return new Response(JSON.stringify(ANSWER),{status:200,headers:{'Content-Type':'application/json'}}); }
  if(u.includes('/client/block-many')){ const b=JSON.parse(opts.body||'{}'); window.__blockMany.push(b);
    return new Response(JSON.stringify({ok:true,blocked:true,count:3,changed:b.targets.length,refused:[]}),{status:200,headers:{'Content-Type':'application/json'}}); }
  const r=await __prevFetch(url,opts);
  if(u.includes('/client/config')){ try{ const j=await r.clone().json(); j.admin_npubs=[ME_NPUB];
    return new Response(JSON.stringify(j),{status:200,headers:{'Content-Type':'application/json'}}); }catch(_){} }
  return r;
};
""".replace("ANSWER", json.dumps(ANSWER)).replace("ME_NPUB", json.dumps(ME_NPUB))


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_admin_traces_an_account_from_its_profile_menu():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await b.until("!!document.body && !document.body.classList.contains('guest')")
        await b.js(f"__PC.openProfile('{SPAM}'); true")
        await b.until("!!document.getElementById('prof-menu')")
        await b.js("document.getElementById('prof-menu').click(); true")
        await b.until("!!document.querySelector('.menu-pop [data-m=\"trace\"]')")
        got["label"] = await b.js("document.querySelector('.menu-pop [data-m=\"trace\"]').textContent.trim()")
        await b.js("document.querySelector('.menu-pop [data-m=\"trace\"]').click(); true")
        await b.until("!!document.querySelector('.relay-trace .tr-head')")
        got["req"] = await b.js("__traceReq")
        got["panel"] = await b.js("""(()=>{const r=document.querySelector('.relay-trace');return {
            head:r.querySelector('.tr-head').textContent, tone:r.querySelector('.tr-card').className,
            signals:[...r.querySelectorAll('.tr-sig')].map(x=>x.className+' '+x.textContent),
            steps:r.querySelectorAll('.tr-step').length, badges:[...r.querySelectorAll('.tr-step .tr-badge')].map(x=>x.textContent),
            followers:r.querySelectorAll('.tr-fol').length, stats:r.querySelector('.tr-grid').innerText,
            chips:r.querySelector('.tr-chips').innerText, block:!!r.querySelector('#tr-block')}})()""")
        # Bulk block: the account is pre-selected; tick two followers; one signed request names all three.
        await b.js("""(()=>{const r=document.querySelector('.relay-trace');
            [...r.querySelectorAll('.tr-fols [data-pick]')].slice(0,2).forEach(c=>{c.checked=true;c.dispatchEvent(new Event('change'))});return 1})()""")
        got["btn"] = await b.js("document.querySelector('#tr-block').textContent")
        await b.js("document.querySelector('#tr-block').click(); true")
        await b.until("!!document.querySelector('.uiconfirm [data-uc=\"1\"]')")
        got["confirm_text"] = await b.js("document.querySelector('.uiconfirm-msg').textContent")
        await b.js("document.querySelector('.uiconfirm [data-uc=\"1\"]').click(); true")
        await b.until("window.__blockMany.length===1 && document.querySelectorAll('.relay-trace .tr-blocked').length===3")
        got["bulk"] = await b.js("window.__blockMany[0]")
        # The panel at phone width: nothing sticks out sideways.
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await asyncio.sleep(.4)
        got["overflow"] = await b.js("""(()=>{const r=document.querySelector('.relay-trace');
            return [...r.querySelectorAll('*')].filter(e=>{const b=e.getBoundingClientRect();return b.width&&b.right>window.innerWidth+1}).length})()""")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=INIT))
    req = got["req"]
    assert req and req["target"] == SPAM, got
    proof = json.loads(__import__("base64").b64decode(req["auth"]))
    assert proof["kind"] == 27235 and proof["content"] == "relay-trace" and ["p", SPAM] in proof["tags"], proof
    assert proof["pubkey"] == ME_PK
    p = got["panel"]
    assert p["head"] == ANSWER["headline"] and "three hops out" in p["head"], p
    assert "tr-bad" in p["tone"], p
    assert any("tr-bad" in s and "hidden-character" in s for s in p["signals"]), p
    assert p["steps"] == 3 and p["badges"] == ["friend of a friend", "follows of a seed"], p
    assert p["followers"] == 3, p
    assert "434" in p["stats"] and "webmesh-v1-nodes" in p["chips"], p
    assert p["block"], p
    assert got["overflow"] == 0, got
    assert got["label"] == "🔎 Trace", got["label"]
    assert "Block 3 selected" in got["btn"], got["btn"]
    assert got["confirm_text"].startswith("Block 3 accounts on this relay?"), got["confirm_text"]
    bulk = got["bulk"]
    assert set(bulk["targets"]) == {SPAM, VOUCHER, SEEDED}, bulk["targets"]
    bproof = json.loads(__import__("base64").b64decode(bulk["auth"]))
    assert bproof["kind"] == 27235 and bproof["content"] == "block-many" and bproof["pubkey"] == ME_PK
    assert {t[1] for t in bproof["tags"] if t[0] == "p"} == set(bulk["targets"]), "the proof must name every account"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_trace_window_renders_the_panel_as_its_own_page():
    """`doc:trace:<pubkey>` is what a PosterChanOS Trace window is opened with; its page is the panel itself,
    so opening a follower (a window of its own) leaves the trace where it was."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await b.until("!!document.body && !document.body.classList.contains('guest')")
        await b.js(f"__PC.switchView('doc:trace:{SPAM}'); true")
        await b.until("!!document.querySelector('#feed .relay-trace.tr-page .tr-head')")
        got["head"] = await b.js("document.querySelector('#feed .tr-head').textContent")
        got["title"] = await b.js("document.querySelector('#view-title').textContent")
        got["modal"] = await b.js("!!document.querySelector('.modal .relay-trace')")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=INIT))
    assert got["head"] == ANSWER["headline"], got
    assert got["title"] == "Trace" and got["modal"] is False, got

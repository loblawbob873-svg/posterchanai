"""Admin -> Nostr Relay -> Pinned authors: the list, and Unpin -- the shipped tab markup and script in headless Chrome.

Asked 2026-10-27 (option 3 of the DB-growth answer): list the "Sync this user's data" pins with what each keeps past
the retention window, an Unpin per pin, and an optional expiry (off by default). A MEMBER is kept anyway, so their
row says so and has no Unpin. The relay process does the work (thread.py pins/unpin control commands); this pins the
screen: pressing List shows the rows newest-largest first, a member row has no button, Unpin asks first and then
sends exactly that key, and the list is redrawn after. Desktop and phone width; nothing sideways.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import music_art_fixture as A
from tests.client.test_bot_form_lists_look_like_the_relay_tab import ROOT
from tests.client import test_bot_form_lists_look_like_the_relay_tab as H

STRANGER, MEMBER = "b" * 64, "c" * 64
STUB = r"""<script>
window.__errors=[]; onerror=m=>__errors.push(String(m));
window.__asked=[]; window.pcConfirm=async m=>{ __asked.push(m); return true; };
window.__sent=[]; let ts=0, pins=[%r, %r];
const row=(pk,member,kept)=>({pubkey:pk,npub:'npub1'+pk.slice(0,20),name:member?'Alice (member)':'Stranger',pinned_at:1700000000,kept,member});
window.csrfFetch=async(u,o)=>{ o=o||{}; const J=v=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
  if(u.endsWith('/nostr-relay/pins')) return J({ok:true,ts,days:0,members_read:true,
     list:pins.map(p=>p===%r?row(p,true,900):row(p,false,5000))});
  if(u.endsWith('/pins/refresh')){ ts++; return J({ok:true}); }
  if(u.endsWith('/unpin')){ const b=JSON.parse(o.body); __sent.push(b.pubkeys); pins=pins.filter(p=>!b.pubkeys.includes(p)); return J({ok:true}); }
  return J({ok:true}); };
</script>""" % (STRANGER, MEMBER, MEMBER)

PROBE = r"""(()=>{const rows=[...document.querySelectorAll('#relayPinsList tr')];
  return {rows:rows.map(r=>({pk:r.dataset.pk,text:r.textContent,btn:!!r.querySelector('.relay-unpin')})),
    summary:(document.querySelector('#relayPinsList p')||{}).textContent||'',
    sideways:document.documentElement.scrollWidth>innerWidth+1, asked:__asked.slice(), sent:__sent.slice(), errors:__errors.slice()}})()"""


async def _run(width):
    html = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    css = (ROOT / "static/css/admin-tabs.css").read_text() + (ROOT / "static/css/admin-theme.css").read_text()
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><style>" + css
            + "</style>" + STUB + "<body class=admin-page><div class=admin-page>" + html + "</div>")
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1,
                                                            "mobile": width < 600})
        frame = (await b.call("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        await b.call("Page.setDocumentContent", {"frameId": frame, "html": page})
        await asyncio.sleep(.3)
        await b.js("document.querySelectorAll('section,fieldset,.tab-content').forEach(e=>{e.hidden=false;e.style.display='block'}); 1")
        await b.js("document.getElementById('relayPins').scrollIntoView({block:'center'}); 1")
        await A.press(b, "#relayPinsBtn")
        for _ in range(120):
            if await b.js("document.querySelectorAll('#relayPinsList tr').length===2"):
                break
            await asyncio.sleep(.1)
        got["listed"] = await b.js(PROBE)
        await b.js("document.querySelector('#relayPinsList .relay-unpin').scrollIntoView({block:'center'}); 1")
        await A.press(b, "#relayPinsList .relay-unpin")
        for _ in range(120):
            if await b.js("document.querySelectorAll('#relayPinsList tr').length===1"):
                break
            await asyncio.sleep(.1)
        got["after"] = await b.js(PROBE)

    await H_browser(check)
    return got


async def H_browser(check):
    """The same headless-Chrome launch the bot-lists test uses, with the check run on its page."""
    import subprocess
    import tempfile
    import httpx
    import websockets
    from tests.client.test_effects_full_app import Browser
    with tempfile.TemporaryDirectory(prefix="pc-pins-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await check(b)
        finally:
            proc.kill()
            try:
                proc.wait(timeout=15)
            except Exception:
                pass


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_the_pins_list_and_unpin(width):
    got = asyncio.run(_run(width))
    L, after = got["listed"], got["after"]
    assert [r["pk"] for r in L["rows"]] == [STRANGER, MEMBER], (width, "the rows (largest first)", L["rows"])
    stranger, member = L["rows"]
    assert stranger["btn"] and "5,000 old" in stranger["text"], (width, stranger)
    assert not member["btn"] and "member" in member["text"] and "kept anyway" in member["text"], \
        (width, "a member is offered Unpin", member)
    assert "5,000 old events kept only by a pin" in L["summary"] and "never expire" in L["summary"], L["summary"]
    assert after["asked"] and "cannot be undone" in after["asked"][0], (width, "Unpin did not ask first", after["asked"])
    assert after["sent"] == [[STRANGER]], (width, "Unpin sent the wrong keys", after["sent"])
    assert [r["pk"] for r in after["rows"]] == [MEMBER], (width, "the list was not redrawn", after["rows"])
    assert not L["sideways"] and not after["sideways"], width

"""Files → Storage admin: the user list is searchable from a box at the TOP.

"File Manager -> Admin -> Make users searchable at the top." The admin's storage overview listed every
uploader with no way to find one. Now a search box sits above the table and filters it as you type --
by name, display name, NIP-05, npub or hex key, case-insensitively -- with a clear button and a line
saying so when nothing matches. Typing filters the list the screen already has: the usage report is
fetched ONCE, never per keystroke.

The shipped bundle, driven by real taps/clicks and typed text; the server is stubbed at the network
boundary only. Phone widths (390x844, 360x780) also check the box fits with no sideways scroll, is not
covered by the header or bottom nav (hit-tested at its centre), and still does after the on-screen
keyboard takes half the screen. Screenshots go to $PC_SHOT_DIR (default /tmp/pc-badm-search-shots).
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop

ME = nostr_service.npub_of(bip340.pubkey_from_seckey(b"\x01" * 32).hex())     # desktop.login's key
SHOTS = Path(os.environ.get("PC_SHOT_DIR", "/tmp/pc-badm-search-shots"))


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r"""
window.__usage=0;
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const u=String(url), J=(o,s=200)=>new Response(JSON.stringify(o),{status:s,headers:{'Content-Type':'application/json'}});
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();return J({...d,admin_npubs:[%(me)r]});}
  if(u.includes('/client/admin-blossom-usage')){ window.__usage++;
    const users=(window.__badmUsers||[]).map((x,i)=>({pubkey:x.pk,npub:NostrTools.nip19.npubEncode(x.pk),count:3+i,size:(1+i)*1048576}));
    return J({ok:true,users,total:users.reduce((a,x)=>a+x.size,0)});}
  return prev.apply(this,arguments);};}
""" % {"me": ME}

# Three uploaders with real signed profiles in the Store, so no relay is needed to name them.
SEED = r"""(()=>{const mk=(n,c)=>{const sk=new Uint8Array(32).fill(n),pk=NostrTools.getPublicKey(sk);
  Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:Math.floor(Date.now()/1000),tags:[],content:JSON.stringify(c)},sk));
  return {pk};};
  window.__badmUsers=[mk(11,{name:'Alice',nip05:'alice@poster.place'}),mk(12,{name:'Bob',display_name:'Robert Builder'}),
                      mk(13,{name:'Carol',nip05:'carol@example.org'})];
  return window.__badmUsers.map(x=>x.pk);})()"""

NAMES = "[...document.querySelectorAll('.badm-row td:first-child b')].map(b=>b.textContent).sort()"

GEO = r"""(()=>{const i=document.querySelector('#badm-find'),r=i.getBoundingClientRect(),vw=document.documentElement.clientWidth;
  const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
  const scrollers=[document.documentElement,document.body,document.querySelector('#feed'),document.querySelector('#files-special')].filter(Boolean);
  return {l:r.left,r:r.right,t:r.top,b:r.bottom,w:r.width,vw,vh:innerHeight,
    sideways:scrollers.some(s=>s.scrollWidth>s.clientWidth+1),
    hitIsBox:!!hit&&!!hit.closest('.badm-find')};})()"""


async def _tap(b, sel, phone):
    x, y = await b.js(f"(()=>{{const r=document.querySelector({sel!r}).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]}})()")
    if phone:
        await b.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        await b.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    else:
        for t in ("mousePressed", "mouseReleased"):
            await b.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})


async def _open_admin(b, width, height, os_mode):
    phone = width < 600
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=height, deviceScaleFactor=1, mobile=phone))
    if phone:
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    await desktop.login(b)
    if not os_mode:
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(SEED)
    await b.js("__PC.switchView('blossom');true")
    await b.until("!!document.querySelector('[data-files-mode=\"admin\"]')")
    await b.js("document.querySelector('[data-files-mode=\"admin\"]').click();true")
    await b.until("document.querySelectorAll('.badm-row').length===3")
    return phone


async def _search(b, width, height, os_mode, shots):
    got = {}
    phone = await _open_admin(b, width, height, os_mode)
    got["all"] = await b.js(NAMES)
    got["geo"] = await b.js(GEO)
    await _tap(b, "#badm-find", phone)
    got["focused"] = await b.js("document.activeElement===document.querySelector('#badm-find')")
    await b.call("Input.insertText", {"text": "aLI"})                 # name, case-insensitive
    await b.until(NAMES + ".length===1")
    got["by_name"] = await b.js(NAMES)
    if phone:
        # The on-screen keyboard takes roughly half the screen: the box must stay usable above it.
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=height // 2, deviceScaleFactor=1, mobile=True))
        await asyncio.sleep(.3)
        got["kb_geo"] = await b.js(GEO)
        got["kb_focused"] = await b.js("document.activeElement===document.querySelector('#badm-find')")
        if shots:
            SHOTS.mkdir(parents=True, exist_ok=True)
            (SHOTS / f"files-admin-search-{width}-keyboard.png").write_bytes(
                base64.b64decode((await b.call("Page.captureScreenshot", {"format": "png"}))["data"]))
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=height, deviceScaleFactor=1, mobile=True))
        await asyncio.sleep(.2)
    if phone and shots:
        (SHOTS / f"files-admin-search-{width}-filtered.png").write_bytes(
            base64.b64decode((await b.call("Page.captureScreenshot", {"format": "png"}))["data"]))
    await _tap(b, "#badm-find-clear", phone)                           # the clear button
    await b.until(NAMES + ".length===3")
    got["cleared_value"] = await b.js("document.querySelector('#badm-find').value")
    got["clear_hidden"] = await b.js("document.querySelector('#badm-find-clear').getBoundingClientRect().width===0")
    await b.call("Input.insertText", {"text": "robert"})              # display name
    await b.until(NAMES + ".length===1")
    got["by_display"] = await b.js(NAMES)
    await b.js("(()=>{const i=document.querySelector('#badm-find');i.value='';i.dispatchEvent(new Event('input',{bubbles:true}))})()")
    await b.call("Input.insertText", {"text": "EXAMPLE.ORG"})         # NIP-05
    await b.until(NAMES + ".length===1")
    got["by_nip05"] = await b.js(NAMES)
    await b.js("(()=>{const i=document.querySelector('#badm-find');i.value='';i.dispatchEvent(new Event('input',{bubbles:true}))})()")
    npub = await b.js("NostrTools.nip19.npubEncode(__badmUsers[1].pk).slice(5,20)")
    await b.call("Input.insertText", {"text": npub})                  # part of an npub
    await b.until(NAMES + ".length===1")
    got["by_npub"] = await b.js(NAMES)
    await b.js("(()=>{const i=document.querySelector('#badm-find');i.value='';i.dispatchEvent(new Event('input',{bubbles:true}))})()")
    await b.call("Input.insertText", {"text": "zzz-nobody"})
    await b.until("!!document.querySelector('.badm-none')")
    got["none"] = await b.js("document.querySelector('.badm-none').textContent")
    got["none_rows"] = await b.js("document.querySelectorAll('.badm-row').length")
    if phone and shots:
        (SHOTS / f"files-admin-search-{width}-nomatch.png").write_bytes(
            base64.b64decode((await b.call("Page.captureScreenshot", {"format": "png"}))["data"]))
    got["usage_fetches"] = await b.js("window.__usage")
    return got


def _assert(got, phone):
    assert got["all"] == ["Alice", "Bob", "Carol"], got
    g = got["geo"]
    assert g["w"] > 0 and g["l"] >= 0 and g["r"] <= g["vw"] + .5, ("the search box runs off the screen", g)
    assert not g["sideways"], ("the page scrolls sideways", g)
    assert g["t"] >= 0 and g["b"] <= g["vh"], ("the search box is not on screen", g)
    assert g["hitIsBox"], ("something (header / bottom nav) covers the search box", g)
    assert got["focused"], "tapping the search box did not focus it"
    assert got["by_name"] == ["Alice"], got
    assert got["cleared_value"] == "" and got["clear_hidden"], got
    assert got["by_display"] == ["Bob"], got
    assert got["by_nip05"] == ["Carol"], got
    assert got["by_npub"] == ["Bob"], got
    assert "No users match" in got["none"] and got["none_rows"] == 0, got
    assert got["usage_fetches"] == 1, ("typing re-fetched the usage report", got["usage_fetches"])
    if phone:
        k = got["kb_geo"]
        assert got["kb_focused"], "the keyboard opening took focus out of the box"
        assert k["w"] > 0 and k["l"] >= 0 and k["r"] <= k["vw"] + .5 and not k["sideways"], k
        assert k["t"] >= 0 and k["b"] <= k["vh"] and k["hitIsBox"], ("with the keyboard up the box is hidden", k)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(390, 844), (360, 780), (1280, 850)])
def test_the_admin_user_list_is_searchable_from_the_top(width, height):
    got = {}

    async def check(b):
        got.update(await _search(b, width, height, False, True))

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    _assert(got, width < 600)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_admin_user_search_works_in_the_windowed_desktop():
    got = {}

    async def check(b):
        got["os"] = None
        got.update(await _search(b, 1440, 1000, True, False))
        got["os"] = await b.js("!!(window.PCOS && PCOS.isOn())")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert got["os"], "the windowed desktop was not on"
    _assert(got, False)

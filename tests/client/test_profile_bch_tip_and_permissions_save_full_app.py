"""A profile's two buttons no test pressed (2026-10-22: every button gets a test).

  * '#bchtip-prof' "Tip" (Bitcoin Cash): shown only on a profile that carries a BCH address; it opens the tip sheet
    for THAT person and address (bitcoincash: wallet link, the address to copy), and "I sent it" then posts the
    public tip note naming them (kind 1, ['p', <them>], ['t','bchtip']).
  * Admin → ☰ → Permissions → '#caps-save' "Save": signs and sends ONLY what changed -- AI access granted, Blossom
    revoked, the feature caps with Video switched on -- each a kind-27235 proof naming the person, nothing for the
    untouched Live streaming / NIP-05, then "permissions saved" and the sheet closes. A refused change says
    "some changes failed".
Shipped bundle; the relay socket and the server's endpoints are fixtures that record what was sent.
"""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop

ME = nostr_service.npub_of(bip340.pubkey_from_seckey(b"\x01" * 32).hex())     # desktop.login's key
BCH = "qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"

PROFILES = r"""(()=>{const now=Math.floor(Date.now()/1000);window.__publishOK=true;
  const mk=(n,c)=>{const ev=NostrTools.finalizeEvent({kind:0,created_at:now-60,content:JSON.stringify(c),tags:[]},new Uint8Array(32).fill(n));Store.saveProfile(ev);return ev;};
  const bea=mk(3,{name:'Bea',bitcoincash_address:'bitcoincash:BCH'}), cal=mk(4,{name:'Cal'});
  window.__events=[bea,cal];window.__bea=bea.pubkey;window.__cal=cal.pubkey;return 1;})()""".replace("BCH", BCH)


def toast_has(text):
    return "[...document.querySelectorAll('#toast-root .toast')].some(t=>t.textContent===" + json.dumps(text) + ")"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_bch_tip_opens_the_sheet_for_this_person_and_posts_the_tip_note():
    async def check(b):
        await desktop.login(b)
        await b.js(PROFILES)
        # No address, no button.
        await b.js("__PC.openProfile(__cal); 1")
        await b.until("!!document.querySelector('#prof-menu')")
        await asyncio.sleep(.3)
        assert not await b.js("!!document.querySelector('#bchtip-prof')"), "Tip offered on a profile with no BCH address"
        await b.js("__PC.openProfile(__bea); 1")
        await b.until("!!document.querySelector('#bchtip-prof')")
        await b.js("document.querySelector('#bchtip-prof').click(); 1")
        await b.until("!!document.querySelector('#bch-addr')")
        assert await b.js("document.querySelector('#bch-addr').textContent") == BCH
        assert "Tip Bea" in await b.js("document.querySelector('#bch-addr').closest('.modal').querySelector('h3').textContent")
        assert await b.js("document.querySelector('#bch-open').getAttribute('href')") == "bitcoincash:" + BCH
        await b.js("(()=>{const a=document.querySelector('#bch-amt');a.value='0.01';a.dispatchEvent(new Event('input'));})(); 1")
        assert await b.js("document.querySelector('#bch-open').getAttribute('href')") == "bitcoincash:" + BCH + "?amount=0.01"
        await b.js("document.querySelector('#bch-sent').click(); 1")
        await b.until("__published.some(e=>e.kind===1&&e.tags.some(t=>t[0]==='t'&&t[1]==='bchtip'))")
        note = await b.js("__published.find(e=>e.kind===1&&e.tags.some(t=>t[0]==='t'&&t[1]==='bchtip'))")
        assert ["p", await b.js("__bea")] in note["tags"] and ["amount_bch", "0.01"] in note["tags"], note["tags"]
        await b.until(toast_has("🟢 tip note posted"))
    asyncio.run(desktop.with_browser("online", "", check, PLAIN))


# This account is an admin; the permission endpoints are stateful and record every POST.
PERMS = PLAIN + r"""
window.__perm={posts:[],refuse:''};
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const u=String(url), m=String(opts.method||'GET').toUpperCase(), J=(o,s=200)=>new Response(JSON.stringify(o),{status:s,headers:{'Content-Type':'application/json'}});
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();return J({...d,admin_npubs:[%(me)r]});}
  const ep=(u.match(/\/client\/(ai-access|blossom-access|stream-access|user-caps|admin-nip05)/)||[])[1];
  if(!ep) return prev.apply(this,arguments);
  if(m==='GET'){
    if(ep==='ai-access')return J({enabled:false});
    if(ep==='blossom-access')return J({whitelisted:true});
    if(ep==='stream-access')return J({enabled:false});
    if(ep==='user-caps')return J({exists:true,caps:{can_image:true,can_music:false,can_video:false,can_torrent:false,can_media:false}});
    return J({ok:true,domain:'poster.place',name:null,names:[]});}
  const b=JSON.parse(opts.body);__perm.posts.push({ep,...b});
  return J({ok:__perm.refuse!==ep});};}
""" % {"me": ME}


async def open_permissions(b):
    await b.js("__PC.openProfile(__cal); 1")
    await b.until("!!document.querySelector('#prof-menu')")
    await b.js("document.querySelector('#prof-menu').click(); 1")
    item = "[...document.querySelectorAll('.menu-pop button, .menu-pop [role=menuitem]')].find(x=>/Permissions/.test(x.textContent))"
    await b.until("!!" + item)
    await b.js(item + ".click(); 1")
    await b.until("!!document.querySelector('#caps-save')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_permissions_save_sends_only_what_changed_signed_for_that_person():
    async def check(b):
        await desktop.login(b)
        await b.js(PROFILES)
        await open_permissions(b)
        # The sheet shows what the server says now.
        assert await b.js("[document.querySelector('#perm-ai').checked,document.querySelector('#perm-blossom').checked,"
                          "document.querySelector('[data-cap=can_image]').checked,document.querySelector('[data-cap=can_video]').checked]") == [False, True, True, False]
        await b.js("document.querySelector('#perm-ai').click();document.querySelector('#perm-blossom').click();"
                   "document.querySelector('[data-cap=can_video]').click(); 1")
        await b.js("document.querySelector('#caps-save').click(); 1")
        await b.until(toast_has("permissions saved"))
        await b.until("!document.querySelector('#caps-save')")
        posts = await b.js("__perm.posts")
        cal = await b.js("__cal")
        me = await b.js("__PC.me().pubkey")
        assert sorted(p["ep"] for p in posts) == ["ai-access", "blossom-access", "user-caps"], posts
        by = {p["ep"]: p for p in posts}
        assert by["ai-access"]["target"] == cal and by["ai-access"]["grant"] is True
        assert by["blossom-access"]["target"] == cal and by["blossom-access"]["grant"] is False
        assert by["user-caps"]["target"] == cal and by["user-caps"]["caps"] == {
            "can_media": False, "can_image": True, "can_music": False, "can_video": True, "can_torrent": False}, by["user-caps"]
        for ep, action in (("ai-access", "grant"), ("blossom-access", "revoke"), ("user-caps", None)):
            auth = json.loads(base64.b64decode(by[ep]["auth"]))
            assert auth["kind"] == 27235 and auth["pubkey"] == me and ["p", cal] in auth["tags"], auth
            if action:
                assert ["action", action] in auth["tags"], auth["tags"]
            assert await b.js("NostrTools.verifyEvent(" + json.dumps(auth) + ")"), ep
        # A refused change is said, not swallowed.
        await b.js("__perm.posts=[];__perm.refuse='stream-access';document.querySelectorAll('#toast-root .toast').forEach(t=>t.remove()); 1")
        await open_permissions(b)
        await b.js("document.querySelector('#perm-stream').click(); document.querySelector('#caps-save').click(); 1")
        await b.until(toast_has("some changes failed"))
        assert [p["ep"] for p in await b.js("__perm.posts")] == ["stream-access"]
    asyncio.run(desktop.with_browser("online", "", check, PERMS))

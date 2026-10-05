"""One person, several NIP-05 names: the admin can see, add and remove each; the person can pick which to show.

"add support for user having multiple nip05 addresses". Admin → a profile → ☰ → 🔑 Permissions lists
EVERY name the person holds here, each with its own ✕, and "Add name" grants one more without touching
the rest. Edit Profile offers the person's own addresses under the NIP-05 field (a profile publishes one).
The shipped bundle, driven by clicks; the server is stubbed at the network boundary only, statefully.
"""
import asyncio
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop

ME = nostr_service.npub_of(bip340.pubkey_from_seckey(b"\x01" * 32).hex())     # desktop.login's key
OTHER = "cd" * 32


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r"""
window.__nip={names:{}, posts:[]};
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const u=String(url), m=String(opts.method||'GET').toUpperCase(), J=(o,s=200)=>new Response(JSON.stringify(o),{status:s,headers:{'Content-Type':'application/json'}});
  if(u.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();return J({...d,admin_npubs:[%(me)r]});}
  if(u.includes('/client/admin-nip05')){
    if(m==='GET'){const pk=new URL(u,location.href).searchParams.get('pubkey');const n=(__nip.names[pk]||[]).slice().sort();
      return J({ok:true,domain:'poster.place',name:n[0]||null,nip05:n[0]?n[0]+'@poster.place':null,names:n,addresses:n.map(x=>x+'@poster.place')});}
    const b=JSON.parse(opts.body);__nip.posts.push(b);const cur=__nip.names[b.target]=(__nip.names[b.target]||[]);
    if(b.remove&&b.name){const i=cur.indexOf(b.name);if(i<0)return J({ok:false,error:'no'},404);cur.splice(i,1);}
    else if(b.remove){cur.length=0;} else if(b.add){if(!cur.includes(b.name))cur.push(b.name);} else {cur.length=0;cur.push(b.name);}
    return J({ok:true,name:b.remove?null:b.name,nip05:b.remove?null:b.name+'@poster.place',names:cur.slice().sort()});}
  if(/\/client\/(user-caps|ai-access|blossom-access|stream-access)/.test(u)) return J({ok:true,exists:true,caps:{}});
  return prev.apply(this,arguments);};}
""" % {"me": ME}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_the_admin_sees_adds_and_removes_each_name(width):
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(f"__nip.names[{OTHER!r}]=['alice','ally'];true")
        await b.js(f"__PC.openProfile({OTHER!r});true")
        await b.until("!!document.querySelector('#prof-menu')")
        await b.js("document.querySelector('#prof-menu').click();true")
        await b.until("[...document.querySelectorAll('.menu-pop button, .menu-pop [role=menuitem]')].some(x=>/Permissions/.test(x.textContent))")
        await b.js("[...document.querySelectorAll('.menu-pop button, .menu-pop [role=menuitem]')].find(x=>/Permissions/.test(x.textContent)).click();true")
        await b.until("document.querySelectorAll('#perm-nip05-names .perm-nip05-name').length===2")
        chips = "[...document.querySelectorAll('#perm-nip05-names .perm-nip05-name span')].map(s=>s.textContent)"
        res["shown"] = await b.js(chips)
        res["fits"] = await b.js("(()=>{const r=document.querySelector('#perm-nip05-addrow').getBoundingClientRect();return r.right<=innerWidth+1&&r.width>0})()")
        await b.js("document.getElementById('perm-nip05-new').value='Third One';document.getElementById('perm-nip05-addbtn').click();true")
        await b.until("document.querySelectorAll('#perm-nip05-names .perm-nip05-name').length===3")
        res["added"] = await b.js(chips)
        await b.js("document.querySelector('[data-nip05-rm=\"ally\"]').click();true")
        await b.until("document.querySelectorAll('#perm-nip05-names .perm-nip05-name').length===2")
        res["after"] = await b.js(chips)
        res["posts"] = await b.js("__nip.posts.map(p=>({name:p.name,add:!!p.add,remove:!!p.remove}))")
        res["checked"] = await b.js("document.getElementById('perm-nip05').checked")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert res["shown"] == ["alice@poster.place", "ally@poster.place"], res
    assert res["fits"], "the add row runs off the screen"
    assert res["added"] == ["alice@poster.place", "ally@poster.place", "thirdone@poster.place"], res
    assert res["after"] == ["alice@poster.place", "thirdone@poster.place"], res
    assert res["posts"] == [{"name": "thirdone", "add": True, "remove": False},
                            {"name": "ally", "add": False, "remove": True}], res
    assert res["checked"] is True


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_person_picks_which_of_their_addresses_the_profile_shows():
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__nip.names[__PC.me().pubkey]=['alice','ally'];true")
        await b.js("__PC.editOwnProfile();true")
        await b.until("document.querySelectorAll('#pf-nip05-mine [data-nip05]').length===2")
        res["field0"] = await b.js("document.getElementById('pf-nip05').value")
        await b.js("document.querySelector('#pf-nip05-mine [data-nip05=\"ally@poster.place\"]').click();true")
        res["field1"] = await b.js("document.getElementById('pf-nip05').value")
        res["on"] = await b.js("[...document.querySelectorAll('#pf-nip05-mine .on')].map(x=>x.textContent)")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert res["field0"] == "alice@poster.place", res
    assert res["field1"] == "ally@poster.place" and res["on"] == ["ally@poster.place"], res


SEE_BOTH = r"""(()=>{const sk=new Uint8Array(32).fill(5),pk=NostrTools.getPublicKey(sk);
  Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:Math.floor(Date.now()/1000),tags:[],
    content:JSON.stringify({name:'Bob',nip05:'bob@nostrplebs.com'})},sk));
  __nip.names[pk]=['bobby'];return pk;})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_a_profile_shows_its_own_nip05_and_the_name_granted_here(width):
    """"the entire point was to display both": somebody who publishes an identity of their own
    (bob@nostrplebs.com) and holds a name here shows BOTH, once each, on their profile."""
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        pk = await b.js(SEE_BOTH)
        await b.js(f"__PC.openProfile({pk!r});true")
        await b.until("document.querySelectorAll('#prof-nip05s .prof-nip05').length===2")
        res["shown"] = await b.js("[...document.querySelectorAll('#prof-nip05s .prof-nip05')].map(x=>x.textContent)")
        res["fits"] = await b.js("[...document.querySelectorAll('#prof-nip05s .prof-nip05')].every(x=>{const r=x.getBoundingClientRect();return r.width>0&&r.right<=innerWidth+1})")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert res["shown"] == ["bob@nostrplebs.com", "bobby@poster.place"], res
    assert res["fits"], res


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_edit_profile_keeps_your_own_nip05_and_lists_the_one_granted_here():
    """With ONE name here (the case that used to show nothing), the editor says it is shown anyway and
    leaves the field -- the person's own address -- exactly as it was."""
    res = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__nip.names[__PC.me().pubkey]=['alice'];true")
        await b.js("__PC.editOwnProfile();true")
        await b.until("!!document.getElementById('pf-nip05')")
        await b.js("document.getElementById('pf-nip05').value='me@mydomain.example';true")
        await b.until("document.querySelectorAll('#pf-nip05-mine [data-nip05]').length===1")
        res["field"] = await b.js("document.getElementById('pf-nip05').value")
        res["box"] = await b.js("document.getElementById('pf-nip05-mine').textContent")

    asyncio.run(desktop.with_browser("online", "", check, INIT))
    assert res["field"] == "me@mydomain.example", res
    assert "Also shown on your profile" in res["box"] and "alice@poster.place" in res["box"], res

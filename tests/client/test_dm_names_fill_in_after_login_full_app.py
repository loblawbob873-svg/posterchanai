"""Messages fills in a conversation's name and picture when the person's profile arrives AFTER the list drew.

Reported: "for messages, I had to refresh after login to webui for the nip05 names and profile pics to render".
Right after a login no profile is cached, so every row draws with the npub fallback and the profiles land a moment
later -- and the list had stripped `data-prof` off each row's name (so the name would not steal the row's tap),
which is the very attribute the arrival pass (decorateProfiles) finds names by. A refresh worked only because the
profiles were cached by then. Driven in the shipped client with a real NIP-17 DM.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WRAP = r'''(()=>{
 const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),now=Math.floor(Date.now()/1000);
 const sk=new Uint8Array(32).fill(77); window.__peer=NostrTools.getPublicKey(sk);
 const {createRumor,createSeal}=NostrTools.nip59;
 const seal=createSeal(createRumor({kind:14,created_at:now-60,tags:[['p',mePk]],content:'hello there'},sk),sk,mePk);
 const eph=NostrTools.generateSecretKey();
 const w=NostrTools.finalizeEvent({kind:1059,created_at:now-120,tags:[['p',mePk]],
   content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph);
 const r=_rel(); r.push(w); localStorage.setItem('__relayEvents',JSON.stringify(r)); return true; })()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_conversations_name_and_picture_fill_in_when_the_profile_arrives():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await b.js(WRAP)
        await b.js("__PC.switchView('messages');true")
        await b.until("!!document.querySelector('#dm-rows .dm-peer[data-peer=\"'+__peer+'\"]')")
        got["before"] = await b.js("document.querySelector('#dm-rows .dm-peer[data-peer=\"'+__peer+'\"] .dm-peer-top .name').textContent")
        # The profile lands now -- what flushProfiles does when its answer arrives.
        await b.js("""(()=>{const sk=new Uint8Array(32).fill(77);
          const p=NostrTools.finalizeEvent({kind:0,created_at:Math.floor(Date.now()/1000),tags:[],
            content:JSON.stringify({name:'Dana Peer',picture:'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',nip05:'dana@example.com'})},sk);
          Store.saveProfile(p); __PC.decorateProfiles();})();true""")
        await asyncio.sleep(.4)
        row = "document.querySelector('#dm-rows .dm-peer[data-peer=\"'+__peer+'\"]')"
        got["name"] = await b.js(row + ".querySelector('.dm-peer-top .name').textContent")
        got["pic"] = await b.js(row + ".querySelector('.dmav').getAttribute('src')")
        # The name must still not steal the row's tap: clicking it opens the conversation.
        await b.js(row + ".querySelector('.dm-peer-top .name').click();true")
        await asyncio.sleep(.5)
        got["opened"] = await b.js("document.body.innerText.includes('hello there')")

    asyncio.run(desktop.with_browser('online', '', check, extra_init=RELAY))
    assert "Dana Peer" not in got["before"], ("fixture: the profile was expected to be missing at first", got)
    assert got["name"] == "Dana Peer", ("the conversation kept its npub after the profile arrived", got)
    assert got["pic"] == "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", ("the picture did not fill in", got)
    assert got["opened"], ("tapping the name no longer opens the conversation", got)


# A second relay that is still CONNECTING when the first profile lookup goes out, and the only one that holds the
# person's profile -- the shape of a login: the home relay answers first, the rest of the pool opens a moment later.
LATE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),
  relaysEnabled:true,relays:['wss://late.test/'],relaySwitchAsked:'kept'}));
{const Base=window.WebSocket;window.__held=[];window.__late=false;
 const lateRel=()=>{try{return JSON.parse(localStorage.getItem('__relayEventsLate')||'[]')}catch(_){return[]}};
 class Late extends Base{
  constructor(url){
   if(!String(url).includes('late.test')){super(url);return;}
   super(url);this.__late=true;
   // Held CONNECTING until released: the base fixture opens every socket after 10 ms by assigning readyState.
   Object.defineProperty(this,'readyState',{configurable:true,get(){return this.__open?1:(this.__closed?3:0)},
     set(v){if(v===3)this.__closed=true;else if(v===1&&window.__late)this.__open=true;}});
   if(!window.__late)window.__held.push(this);
  }
  fire(type,data){
   if(this.__late&&type==='open'&&!window.__late){return;}
   if(this.__late&&type==='message'&&Array.isArray(data)&&data[0]==='EOSE'&&this.__pend&&this.__pend.has(data[1])){
     const ex=this.__pend.get(data[1]);this.__pend.delete(data[1]);for(const ev of ex)super.fire('message',['EVENT',data[1],ev]);}
   return super.fire(type,data);
  }
  send(raw){
   if(this.__late){const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='REQ'){const fs=m.slice(2);
     (this.__pend=this.__pend||new Map()).set(m[1],lateRel().filter(ev=>fs.some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey)))));}}
   return super.send(raw);
  }
 }
 window.WebSocket=Late;
 window.__openLate=()=>{window.__late=true;for(const s of window.__held.splice(0)){if(s.readyState===0){s.__open=true;s.fire('open',{});}}};
}
'''

PROFILE = r'''(()=>{const sk=new Uint8Array(32).fill(77);
  const p=NostrTools.finalizeEvent({kind:0,created_at:Math.floor(Date.now()/1000),tags:[],
    content:JSON.stringify({name:'Dana Peer',nip05:'dana@example.com'})},sk);
  // Somebody you follow, never on screen: their name reaches you only through @-autocomplete.
  const fk=new Uint8Array(32).fill(88), now=Math.floor(Date.now()/1000);
  const f=NostrTools.finalizeEvent({kind:0,created_at:now,tags:[],content:JSON.stringify({name:'Erin Follow'})},fk);
  const k3=NostrTools.finalizeEvent({kind:3,created_at:now,tags:[['p',NostrTools.getPublicKey(fk)]],content:''},new Uint8Array(32).fill(1));
  const r=_rel(); r.push(k3); localStorage.setItem('__relayEvents',JSON.stringify(r));
  localStorage.setItem('__relayEventsLate',JSON.stringify([p,f]));return true;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_profile_held_only_by_a_relay_that_connects_after_the_lookup_still_fills_in():
    """The lookup that went out while only the home relay was open came back empty and was remembered as "no such
    profile" for five minutes; the relay that HAS it connected a moment later and nothing asked again."""
    got = {}

    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js(PROFILE)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await b.js(WRAP)
        await b.js("__PC.switchView('messages');true")
        await b.until("!!document.querySelector('#dm-rows .dm-peer[data-peer=\"'+__peer+'\"]')")
        await asyncio.sleep(2.5)   # the first lookup has gone out and come back empty
        row = "document.querySelector('#dm-rows .dm-peer[data-peer=\"'+__peer+'\"] .dm-peer-top .name')"
        got["before"] = await b.js(row + ".textContent")
        got["held"] = await b.js("__held.length")
        got["urls"] = await b.js("__sockets.map(s=>s.url)")
        await b.js("__openLate();true")
        for _ in range(60):
            if "Dana Peer" in (await b.js(row + ".textContent") or ""):
                break
            await asyncio.sleep(.1)
        got["after"] = await b.js(row + ".textContent")
        await b.js("__PC.compose();true")
        await b.until("!!document.querySelector('#cmp')")
        await b.js("(()=>{const ta=document.querySelector('#cmp');ta.focus();ta.value='hi @erin';ta.selectionStart=ta.selectionEnd=ta.value.length;"
                   "ta.dispatchEvent(new Event('input',{bubbles:true}));})();true")
        await asyncio.sleep(.3)
        got["mention"] = await b.js("(document.querySelector('.mention-box')||{}).textContent||''")

    asyncio.run(desktop.with_browser('online', '', check, extra_init=RELAY + LATE))
    assert got["held"] >= 1, ("fixture: no second relay was held back", got)
    assert "Dana Peer" not in got["before"], ("fixture: the profile was expected to be missing at first", got)
    assert "Dana Peer" in got["after"], ("the name never filled in once the relay holding it connected", got)
    assert "Erin Follow" in got["mention"], ("@-autocomplete never learned a followed name from the relay that connected late", got)

"""PosterChan with the person's OWN relays, end to end -- "we need to make sure posterchan works good if they use
their own relays".

The unit tests pin the rules (their list is unioned with discovery, the switch is theirs, bootstrap never
overwrites it); nothing ran the app against relays that behave like real personal relays. Here every relay is a
separate fake with its own store and its own behaviour, and the account's list is three of them:

  * wss://mine.test  -- works, and holds things NO other relay has (a followed account's post, their profile);
  * wss://dead.test  -- never finishes connecting (a relay that is down, or blocked on this network);
  * wss://auth.test  -- NIP-42: demands AUTH and refuses every read and write until given it.

and asserts what the person sees: the post that exists only on their relay is on the timeline, a dead relay does
not hold the timeline hostage, what they publish reaches their relay and is reported as posted (one relay saying
"auth-required" is not a failure when another accepted it), their profile comes from their relay, and the app's
own private data (Notes) still saves.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


MINE, DEAD, AUTH = "wss://mine.test", "wss://dead.test", "wss://auth.test"

# One fake per URL. Stores live in localStorage keyed by host, so a reload keeps them.
RELAYS = r"""
window.__sockets=[]; window.__sent=[];
const _host=u=>{ try{ return new URL(u).host; }catch(_){ return String(u); } };
const _store=h=>{ try{ return JSON.parse(localStorage.getItem('__rel:'+h)||'[]'); }catch(_){ return []; } };
const _save=(h,a)=>localStorage.setItem('__rel:'+h,JSON.stringify(a));
const _match=(f,ev)=>{
  if(f.kinds&&!f.kinds.includes(ev.kind))return false; if(f.ids&&!f.ids.includes(ev.id))return false;
  if(f.authors&&!f.authors.includes(ev.pubkey))return false;
  if(f.until!=null&&ev.created_at>f.until)return false; if(f.since!=null&&ev.created_at<f.since)return false;
  for(const k of Object.keys(f)) if(k[0]==='#'){ if(!(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])))return false; }
  return true; };
class RelaySocket extends EventTarget{
  static OPEN=1; static CONNECTING=0; static CLOSING=2; static CLOSED=3;
  constructor(url){ super(); this.url=String(url); this.host=_host(url); this.readyState=0; __sockets.push(this);
    if(this.host==='dead.test') return;                                   // never opens
    setTimeout(()=>{ this.readyState=1; this.fire('open',{}); if(this.host==='auth.test') this.fire('message',['AUTH','challenge-'+Math.random()]); },15); }
  fire(type,data){ const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type); this['on'+type]?.(e); this.dispatchEvent(e); }
  send(raw){ const m=JSON.parse(raw); if(!Array.isArray(m)) return; __sent.push({host:this.host,type:m[0],kind:m[0]==='EVENT'?m[1].kind:null});
    if(this.host==='auth.test'){
      if(m[0]==='REQ') setTimeout(()=>this.fire('message',['CLOSED',m[1],'auth-required: this relay needs you to sign in']),20);
      if(m[0]==='EVENT') setTimeout(()=>this.fire('message',['OK',m[1].id,false,'auth-required: this relay needs you to sign in']),20);
      return; }
    if(m[0]==='REQ'){ const sub=m[1], fs=m.slice(2);
      setTimeout(()=>{ const all=_store(this.host).sort((a,b)=>b.created_at-a.created_at), out=new Map();
        for(const f of fs){ let n=0; const lim=Math.min(f.limit??500,500); for(const ev of all){ if(n>=lim)break; if(_match(f,ev)){ out.set(ev.id,ev); n++; } } }
        for(const ev of out.values()) this.fire('message',['EVENT',sub,ev]); this.fire('message',['EOSE',sub]); },40); }
    if(m[0]==='EVENT'){ const a=_store(this.host); if(!a.some(e=>e.id===m[1].id)){ a.push(m[1]); _save(this.host,a); }
      setTimeout(()=>this.fire('message',['OK',m[1].id,true,'']),30); }
  }
  close(){ this.readyState=3; this.fire('close',{}); }
}
window.WebSocket=RelaySocket;
"""

# Before login: the account's own relay list, and data that exists ONLY on their relay.
SEED = r"""(()=>{
  const me=new Uint8Array(32).fill(1), mePk=NostrTools.getPublicKey(me), friend=new Uint8Array(32).fill(7), fPk=NostrTools.getPublicKey(friend);
  const now=Math.floor(Date.now()/1000), fin=(t,k)=>NostrTools.finalizeEvent(t,k);
  const mine=[ fin({kind:0,created_at:now-5000,tags:[],content:JSON.stringify({name:'Own Relay Person'})},me),
               fin({kind:3,created_at:now-5000,tags:[['p',fPk]],content:''},me),
               fin({kind:10002,created_at:now-5000,tags:[['r','%(MINE)s'],['r','%(DEAD)s'],['r','%(AUTH)s']],content:''},me),
               fin({kind:0,created_at:now-5000,tags:[],content:JSON.stringify({name:'Friend On My Relay'})},friend),
               fin({kind:1,created_at:now-120,tags:[],content:'ONLY ON MY OWN RELAY'},friend) ];
  localStorage.setItem('__rel:mine.test',JSON.stringify(mine));
  ClientSettings.set('relaysEnabled',true); ClientSettings.set('relays',['%(MINE)s','%(DEAD)s','%(AUTH)s']);
  return mePk; })()""" % {"MINE": MINE, "DEAD": DEAD, "AUTH": AUTH}

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_posterchan_works_on_the_persons_own_relays():
    got = {}

    async def check(b):
        await b.js(SEED)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        got["hosts"] = await b.js("[...new Set(__sockets.map(s=>s.host))]")
        # 1. The timeline shows the post that exists only on their relay -- with a dead relay in the list.
        await b.js("__PC.switchView('home');true")
        t0 = await b.js("performance.now()")
        for _ in range(150):
            if await b.js("document.getElementById('feed').innerText.includes('ONLY ON MY OWN RELAY')"):
                break
            await asyncio.sleep(.1)
        got["timeline"] = await b.js("document.getElementById('feed').innerText.includes('ONLY ON MY OWN RELAY')")
        got["timeline_ms"] = await b.js("performance.now()") - t0
        # 2. A post reaches their relay, and a relay saying auth-required does not turn it into a failure.
        got["publish"] = await b.js("(async()=>{ const r=await __PC.publish(1,'posted from my own relays',[],{quiet:true}); return {ok:!!(r&&r.ok), id:r&&r.ev&&r.ev.id}; })()")
        await asyncio.sleep(.5)
        got["on_mine"] = await b.js("JSON.parse(localStorage.getItem('__rel:mine.test')||'[]').some(e=>e.content==='posted from my own relays')")
        got["auth_tried"] = await b.js("__sent.some(s=>s.host==='auth.test'&&s.type==='EVENT')")
        # 3. Their profile comes from their relay.
        me = await b.js("__PC.me().pubkey")
        await b.js("__PC.openProfile(%s);true" % json.dumps(me))
        for _ in range(100):
            if await b.js("document.body.innerText.includes('Own Relay Person')"):
                break
            await asyncio.sleep(.1)
        got["profile"] = await b.js("document.body.innerText.includes('Own Relay Person')")
        # 4. The app's private data still saves (Notes).
        await b.js("__PC.switchView('notes');true")
        await b.until("!!(window.PCNotes && PCNotes.save)")
        got["note"] = await b.js("(async()=>{ try{ const r=await PCNotes.save({title:'own relays',body:'still saves'}); return !!(r&&r.id); }catch(e){ return String(e&&e.message||e); } })()")
        got["errors"] = await b.js("(window.__errors||[]).slice(0,5)")

    asyncio.run(desktop.with_browser("online", "", check, RELAYS))
    assert {"mine.test", "dead.test", "auth.test"} <= set(got["hosts"]), ("their relays were not all used", got["hosts"])
    assert got["timeline"], "a post that exists only on their own relay never reached the timeline"
    assert got["timeline_ms"] < 10_000, ("the dead relay held the timeline back", got["timeline_ms"])
    assert got["publish"]["ok"], ("publishing failed although their relay accepted it", got["publish"])
    assert got["on_mine"], "the post never reached their own relay"
    assert got["auth_tried"], "the AUTH relay in their list was never offered the post"
    assert got["profile"], "their profile (only on their relay) was not shown"
    assert got["note"] is True, ("Notes could not save on own relays", got["note"])

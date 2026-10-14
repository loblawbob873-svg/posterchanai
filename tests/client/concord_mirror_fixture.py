"""A URL-AWARE relay network for the Concord-mirror full-app tests (not a test module itself).

The other Concord full-app tests replace the client's transport doors with one store that ignores relay
URLs, which is exactly what cannot test "read THIS node's relay, never the community's". This fixture
replaces `WebSocket` instead, so the SHIPPED relay.js (queryFrom, subscribeFrom, publishTo, their
`report`s and cooldowns) runs unchanged against three kinds of relay:

  * HOME (`wss://fixture.invalid`, the instance's relay_url in the shared network fixture): stores what it
    is sent, answers REQs from its store, accepts kind-27059 mirror registrations (recorded in
    `__registrations`), and answers the Concord DIRECTORY searches according to `__dirMode`
    ('serve' | 'unavailable' | 'empty'). `__homeMirror='unavailable'` makes it refuse stream REQs the
    way the server does while its mirror is not in step.
  * THE DIRECTORY RELAY (`wss://relay.dreamith.to`): up when `__dirRelayUp`, serving `__directory`.
  * `__upRelays`: plain relays that are UP, accept everything and record it in `__upWrites`, and answer REQs
    from `__upHeld` ([{url, ev}]).
  * EVERY OTHER RELAY: down — the socket errors and closes, like a relay nobody can reach.
"""

INSTALL_NETWORK = r'''(()=>{
const HOME='wss://fixture.invalid',DIR='wss://relay.dreamith.to';
const norm=u=>String(u).replace(/\/+$/,'');
window.__home=window.__home||[];window.__homeWrites=[];window.__registrations=[];window.__dials=[];
window.__directory=window.__directory||[];window.__dirWrites=[];window.__upWrites=[];
if(window.__dirMode===undefined)window.__dirMode='unavailable';
if(window.__dirRelayUp===undefined)window.__dirRelayUp=false;
if(window.__homeMirror===undefined)window.__homeMirror='serve';
const tagMatch=(f,ev)=>Object.keys(f).filter(k=>k[0]==='#').every(k=>(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
const match=(f,ev)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))&&(!f.ids||f.ids.includes(ev.id))
  &&(f.since==null||ev.created_at>=f.since)&&(f.until==null||ev.created_at<=f.until)&&tagMatch(f,ev)
  &&(!f.search||String(ev.content||'').toLowerCase().includes(String(f.search).toLowerCase()));
const subs=new Set();
class NetSocket extends EventTarget{
  static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
  constructor(url){super();this.url=String(url);this.readyState=0;const u=norm(url);__dials.push(u);
    this.target=u;this.kind=u===HOME?'home':u===DIR?'dir':(window.__upRelays||[]).map(norm).includes(u)?'up':'ext';
    setTimeout(()=>{
      if(this.kind==='ext'||(this.kind==='dir'&&!__dirRelayUp)){this.readyState=3;this.fire('error',{});this.fire('close',{});return;}
      this.readyState=1;this.fire('open',{});},10);}
  fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
  send(raw){
    if(this.readyState!==1)return;let m;try{m=JSON.parse(raw);}catch(_){return;}if(!Array.isArray(m))return;
    const reply=x=>setTimeout(()=>{if(this.readyState===1)this.fire('message',x);},15);
    const later=fn=>{const t=setInterval(()=>{if(!window.__dirHold){clearInterval(t);fn();}},20);};
    if(this.kind==='up'){if(m[0]==='REQ'){const fs=m.slice(2);for(const x of (window.__upHeld||[]))if(norm(x.url)===this.target&&fs.some(f=>match(f,x.ev)))reply(['EVENT',m[1],x.ev]);reply(['EOSE',m[1]]);}if(m[0]==='EVENT'){__upWrites.push({url:this.url,ev:m[1]});reply(['OK',m[1].id,true,'']);}return;}
    if(this.kind==='dir'){
      if(m[0]==='REQ'){const fs=m.slice(2);later(()=>{for(const ev of __directory)if(fs.some(f=>match(f,ev)))reply(['EVENT',m[1],ev]);reply(['EOSE',m[1]]);});}
      if(m[0]==='EVENT'){__dirWrites.push(m[1]);reply(['OK',m[1].id,true,'']);}
      return;
    }
    if(m[0]==='REQ'){
      const fs=m.slice(2);
      if(fs.length&&fs.every(f=>f.search)){later(()=>{
        if(__dirMode==='unavailable'){reply(['CLOSED',m[1],'unavailable: the community directory could not be reached from this node']);return;}
        if(__dirMode==='serve')for(const ev of __directory)if(fs.some(f=>match(f,ev)))reply(['EVENT',m[1],ev]);
        reply(['EOSE',m[1]]);});return;
      }
      if((__homeMirror==='unavailable'||(__homeMirror==='partial'&&!String(m[1]).startsWith('pcmp')))&&fs.some(f=>(f.kinds||[]).includes(1059)&&(f.authors||[]).length)){
        reply(['CLOSED',m[1],'unavailable: this node is still catching up with this community']);return;}
      for(const ev of __home)if(fs.some(f=>match(f,ev)))reply(['EVENT',m[1],ev]);
      reply(['EOSE',m[1]]);subs.add({ws:this,id:m[1],fs});return;
    }
    if(m[0]==='CLOSE'){for(const s of [...subs])if(s.ws===this&&s.id===m[1])subs.delete(s);return;}
    if(m[0]==='EVENT'){
      const ev=m[1];
      if(ev.kind===27059){let body={};try{body=JSON.parse(ev.content);}catch(_){}__registrations.push({...body,pubkey:ev.pubkey});reply(['OK',ev.id,true,window.__regReply||'mirroring']);return;}
      if(!__home.some(x=>x.id===ev.id))__home.push(ev);__homeWrites.push(ev);reply(['OK',ev.id,true,'']);
      for(const s of [...subs])if(s.fs.some(f=>match({...f,since:undefined,until:undefined},ev)))setTimeout(()=>{if(s.ws.readyState===1)s.ws.fire('message',['EVENT',s.id,ev]);},20);
    }
  }
  close(){if(this.readyState===3)return;this.readyState=3;for(const s of [...subs])if(s.ws===this)subs.delete(s);this.fire('close',{});}
}
window.WebSocket=NetSocket;
})();'''

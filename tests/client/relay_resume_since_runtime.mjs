// A LIVE SUBSCRIPTION RESUMES FROM WHAT IT HAS, NOT FROM ITS BACKLOG (the SHIPPED relay.js, a fake socket).
//
// Every reconnect re-sent each live REQ unchanged, so every return to the app re-delivered the notifications
// (limit 150) and up to the relay's cap of gift-wrapped DMs -- events the client already held and dropped as
// `seen` (2026-10-21 Android audit). Now a re-arm asks from the newest event it accepted minus a margin.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const sent=[];let sockets=[];
class FakeWS{
  constructor(url){this.url=url;this.readyState=0;sockets.push(this);setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},1);}
  send(raw){sent.push({url:this.url,msg:JSON.parse(raw)});}
  close(){this.readyState=3;setTimeout(()=>this.onclose&&this.onclose(),1);}
  feed(msg){this.onmessage&&this.onmessage({data:JSON.stringify(msg)});}
}
const sandbox={window:{},self:{},Worker:class{postMessage(){}terminate(){}},console,setTimeout,clearTimeout,setInterval,clearInterval,WebSocket:FakeWS,Date,Math,JSON,Promise,
  navigator:{onLine:true},document:{hidden:false,addEventListener(){}},location:{origin:'https://poster.place'}};
sandbox.window=sandbox;vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('static/js/client/relay.js','utf8'),sandbox);
const R=sandbox.Relay||sandbox.window.Relay;assert.ok(R,'relay.js did not publish window.Relay');
const wait=ms=>new Promise(r=>setTimeout(r,ms));
R.connect('wss://relay.example');await wait(20);
const now=Math.floor(Date.now()/1000);
const notes=R.subscribe([{kinds:[1,7],'#p':['a'.repeat(64)],limit:150}],{live:true,onEvent(){}});
const dms=R.subscribe([{kinds:[1059],'#p':['a'.repeat(64)],since:now-7*86400}],{live:true,onEvent(){}});
const window_=R.subscribe([{kinds:[1],until:now-100,limit:20}],{live:true,onEvent(){}});
await wait(10);
const reqs=id=>sent.filter(s=>s.msg[0]==='REQ'&&s.msg[1]===id).map(s=>s.msg.slice(2));
assert.deepEqual(reqs(notes)[0],[{kinds:[1,7],'#p':['a'.repeat(64)],limit:150}],'the first REQ is the filter as given');
const ws=sockets[0],ev=(id,kind,at)=>({id:id.padEnd(64,'0'),kind,created_at:at,pubkey:'b'.repeat(64),content:'',tags:[],sig:'c'.repeat(128)});
ws.feed(['EVENT',notes,ev('1',1,now-500)]);ws.feed(['EVENT',notes,ev('2',7,now-50)]);ws.feed(['EVENT',notes,ev('3',1,now+99999)]);   // a forged future stamp
ws.feed(['EVENT',dms,ev('4',1059,now-3600)]);
ws.feed(['EOSE',notes]);ws.feed(['EOSE',dms]);
// The app goes to the background long enough for the socket to drop, then comes back.
ws.close();await wait(1500);
const again=reqs(notes);assert.ok(again.length>=2,'the live subscription was not re-armed after the reconnect');
const f=again[again.length-1][0];
assert.equal(f.limit,150,'the limit is kept');
assert.ok(f.since<=now+60-3600&&f.since>=now-50-3600-5,'resume asks from the newest ACCEPTED event minus 1h, a forged future stamp capped: '+JSON.stringify(f));
const d=reqs(dms);const g=d[d.length-1][0];
assert.equal(g.since,now-3600-3*86400,'gift wraps resume with a 3-day margin (NIP-59 backdating)');
const w=reqs(window_);assert.deepEqual(w[w.length-1][0],{kinds:[1],until:now-100,limit:20},'a filter with `until` is re-sent as it was');
console.log('OK');process.exit(0);   // relay.js keeps heartbeat timers alive

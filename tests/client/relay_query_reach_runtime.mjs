/* queryFrom SAYS WHICH RELAYS ANSWERED, and a user's retry is not refused by our own cooldown.
 *
 * Runs the SHIPPED Relay.queryFrom against a fake socket. The bug this exists for is not a wrong
 * value, it is a MISSING one: an empty result carried no way to tell "every relay said nothing"
 * from "every relay refused us or was still cooling down", and a caller that drew a conclusion
 * from it drew the wrong one. */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const sockets=[];
class FakeSocket{
  constructor(url){this.url=url;this.readyState=1;sockets.push(this);
    setTimeout(()=>{try{this.onopen&&this.onopen();}catch(_){}},0);}
  send(raw){const m=JSON.parse(raw);if(m[0]!=='REQ')return;const id=m[1];
    setTimeout(()=>{const s=SCRIPT[this.url];
      if(s==='closed')this.onmessage&&this.onmessage({data:JSON.stringify(['CLOSED',id,'ERROR: blocked'])});
      else if(s==='error')this.onerror&&this.onerror();
      else this.onmessage&&this.onmessage({data:JSON.stringify(['EOSE',id])});},0);}
  close(){this.readyState=3;}
}
const SCRIPT={'wss://good.example/':'eose','wss://bad.example/':'error'};

const ctx={console,setTimeout,clearTimeout,setInterval,clearInterval,JSON,Date,Math,URL,Promise,Error,
  WebSocket:FakeSocket,localStorage:{getItem:()=>null,setItem(){},removeItem(){}},
  document:{addEventListener(){},removeEventListener(){},visibilityState:'visible'},
  addEventListener(){},removeEventListener(){},navigator:{onLine:true},
  location:{protocol:'https:',host:'example.test',href:'https://example.test/'},
  /* relay.js offloads signature verification to a Worker; node's vm has none and this test is not
   * about verification. */
  Worker:class{constructor(){}postMessage(){}addEventListener(){}terminate(){}},
  Blob:class{constructor(){}},crypto:{randomUUID:()=>'x'}};
ctx.window=ctx;ctx.globalThis=ctx;ctx.self=ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(new URL('../../static/js/client/relay.js',import.meta.url),'utf8'),ctx);
const Relay=ctx.window.Relay;

const urls=['wss://good.example/','wss://bad.example/'];
const first={};
await Relay.queryFrom(urls,[{kinds:[1],limit:1}],{timeout:500,failureCooldown:60000,report:first});
assert.deepEqual([...new Set(first.asked)].sort(),[...urls].sort(),'both were asked on a cold pass');
assert.deepEqual([...first.ok],['wss://good.example/'],JSON.stringify(first));
assert.deepEqual([...first.failed],['wss://bad.example/'],JSON.stringify(first));

/* The SECOND pass is the state the bug lived in: the failure is now silent, because the relay is
 * simply skipped. It has to be reported as cooled, never as an answer. */
const second={};
await Relay.queryFrom(urls,[{kinds:[1],limit:1}],{timeout:500,failureCooldown:60000,report:second});
assert.deepEqual([...second.cooled],['wss://bad.example/'],JSON.stringify(second));
assert(!(second.failed||[]).length,'it was never asked, so it did not fail again');

/* Rate limiting is a different word. `held` must not be confused with a relay that refused us, or
 * a poll that runs every four seconds would report an outage every four seconds. */
const held={};
await Relay.queryFrom(['wss://good.example/'],[{kinds:[1],limit:1}],
  {timeout:500,purpose:'p',minInterval:60000,report:{}});
await Relay.queryFrom(['wss://good.example/'],[{kinds:[1],limit:1}],
  {timeout:500,purpose:'p',minInterval:60000,report:held});
assert.deepEqual([...held.held],['wss://good.example/'],JSON.stringify(held));
assert(!(held.cooled||[]).length,'a rate limit is not a refusal');

/* A retry somebody pressed must actually reach the relay. Without this the button is a no-op for
 * the whole cooldown — thirty minutes on Concord's room reads. */
assert(Relay.clearQueryCooldown(['wss://bad.example/'])>0,'the cooldown entry was there to clear');
SCRIPT['wss://bad.example/']='eose';
const retried={};
await Relay.queryFrom(urls,[{kinds:[1],limit:1}],{timeout:500,failureCooldown:60000,report:retried});
assert.deepEqual([...new Set(retried.ok)].sort(),[...urls].sort(),'after clearing, the relay is asked again: '+JSON.stringify(retried));

console.log('relay query reach runtime ok');

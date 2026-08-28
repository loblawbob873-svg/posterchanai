/* Live interoperability smoke test. This intentionally uses Iroh's real default relay map; it is
 * not part of the offline unit suite. Run with: node scripts/check_webxdc_iroh.mjs */
import fs from 'node:fs';
import init,{RealtimeNode} from '../static/vendor/webxdc-rt/webxdc_rt.js';
await init({module_or_path:fs.readFileSync(new URL('../static/vendor/webxdc-rt/webxdc_rt_bg.wasm',import.meta.url))});
const topic=crypto.getRandomValues(new Uint8Array(32)),a=await new RealtimeNode(),b=await new RealtimeNode();
let done;const received=new Promise(resolve=>{done=resolve;});
await a.join(topic,[b.nodeAddrJson()],bytes=>done([...bytes]));
await b.join(topic,[a.nodeAddrJson()],bytes=>done([...bytes]));
await new Promise(resolve=>setTimeout(resolve,3000));
const sent=[9,8,7,6];await a.send(topic,Uint8Array.from(sent));
const got=await Promise.race([received,new Promise((_,reject)=>setTimeout(()=>reject(new Error('Iroh gossip timed out')),15000))]);
a.leave(topic);b.leave(topic);
if(JSON.stringify(got)!==JSON.stringify(sent))throw new Error(`wrong gossip payload: ${got}`);
console.log(JSON.stringify({ok:true,a:JSON.parse(a.nodeAddrJson()),b:JSON.parse(b.nodeAddrJson()),payload:got}));
process.exit(0);

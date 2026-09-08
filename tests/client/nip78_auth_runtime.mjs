import fs from 'fs';
import vm from 'vm';

const src=fs.readFileSync(new URL('../../static/js/client/relay.js',import.meta.url),'utf8');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
class WS{
  constructor(url){this.url=url;this.readyState=0;this.sent=[];WS.all.push(this);setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},0);}
  send(raw){this.sent.push(JSON.parse(raw));}
  close(){this.readyState=3;}
  reply(msg){this.onmessage&&this.onmessage({data:JSON.stringify(msg)});}
}
WS.all=[];
const ctx={console,setTimeout,clearTimeout,setInterval,clearInterval,WebSocket:WS,URL,atob,btoa,
  Worker:class{constructor(){}postMessage(){}},document:{addEventListener(){},hidden:false},navigator:{onLine:true}};
ctx.window=ctx;ctx.self=ctx;ctx.globalThis=ctx;
vm.createContext(ctx);vm.runInContext(src,ctx);
const R=ctx.Relay, fail=m=>{throw new Error(m)};
let signed=0;
R.setAuthSigner(async tpl=>{signed++;return {...tpl,pubkey:'a'.repeat(64),id:String(signed).padStart(64,'f'),sig:'e'.repeat(128)};});
R.configure({urls:['wss://relay.example/relay'],verify:false});
await sleep(20);
const ws=WS.all[0];
ws.reply(['AUTH','challenge-1']);
let ended=0;
const id=R.subscribe([{kinds:[30078],authors:['a'.repeat(64)]}],{onEvent(){},onEose(){ended++;},live:true});
ws.reply(['CLOSED',id,'auth-required: matching owner required']);
await sleep(20);
const auth=ws.sent.find(m=>m[0]==='AUTH');
if(!auth)fail('client did not answer the NIP-42 challenge');
if(auth[1].kind!==22242)fail('AUTH used wrong event kind');
if(!auth[1].tags.some(t=>t[0]==='relay'&&t[1]==='wss://relay.example/relay'))fail('AUTH omitted exact relay URL');
if(!auth[1].tags.some(t=>t[0]==='challenge'&&t[1]==='challenge-1'))fail('AUTH omitted relay challenge');
ws.reply(['OK',auth[1].id,true,'']);
await sleep(20);
const reqs=ws.sent.filter(m=>m[0]==='REQ'&&m[1]===id);
if(reqs.length!==2)fail(`private REQ was not replayed exactly once after AUTH (${reqs.length})`);
if(signed!==1)fail(`one challenge caused ${signed} signer calls`);

// A second auth-required verdict is final for this relay/subscription. It must not re-sign forever.
ws.reply(['CLOSED',id,'auth-required: owner still mismatched']);
await sleep(20);
if(signed!==1)fail('repeated refusal caused another AUTH signature');
if(ended!==1)fail('repeated refusal left subscription waiting for EOSE forever');

// An ownerless private filter can never meet the relay rule: do not prompt a signer even once.
let impossibleEnded=0;
const impossible=R.subscribe([{kinds:[30078]}],{onEvent(){},onEose(){impossibleEnded++;},live:true});
ws.reply(['CLOSED',impossible,'auth-required: matching authors required']);
await sleep(20);
if(signed!==1)fail('impossible ownerless filter prompted the signer');
if(impossibleEnded!==1)fail('impossible private subscription never completed');

// The same live private subscription must authenticate again after a relay restart.
const conn=R._conns.get('wss://relay.example/relay');
conn._teardownSocket(); conn._open();
await sleep(20);
const reconnected=WS.all.at(-1);
reconnected.reply(['AUTH','challenge-reconnected']);
reconnected.reply(['CLOSED',id,'auth-required: new connection']);
await sleep(20);
const reconnectAuth=reconnected.sent.find(m=>m[0]==='AUTH');
if(!reconnectAuth)fail('relay restart permanently disabled AUTH for the live private subscription');
reconnected.reply(['OK',reconnectAuth[1].id,true,'']);
await sleep(20);
if(reconnected.sent.filter(m=>m[0]==='REQ'&&m[1]===id).length!==2)
  fail('live private subscription did not recover after relay restart');

// A slow signer answer from a retired connection cannot authenticate its replacement,
// clear the replacement's pending latch, or poison its accepted owner state.
let releases=[];
R.setAuthSigner(tpl=>new Promise(resolve=>releases.push(()=>resolve({
  ...tpl,pubkey:'a'.repeat(64),id:String(++signed).padStart(64,'d'),sig:'e'.repeat(128)
}))));
conn.authPubkeys.clear();
reconnected.reply(['AUTH','challenge-slow-old']);
const oldAttempt=R._authenticate(conn,'a'.repeat(64));
await sleep(0);
conn._teardownSocket();conn._open();
await sleep(20);
const replacement=WS.all.at(-1);
replacement.reply(['AUTH','challenge-slow-new']);
const newAttempt=R._authenticate(conn,'a'.repeat(64));
await sleep(0);
releases[0]();
if(await oldAttempt!==false)fail('retired authentication reported success');
if(replacement.sent.some(m=>m[0]==='AUTH'))fail('old challenge was sent on replacement socket');
if(conn._authPromise!==newAttempt)fail('old signer completion cleared new authentication latch');
releases[1]();
await sleep(0);
const freshAuth=replacement.sent.find(m=>m[0]==='AUTH');
if(!freshAuth||!freshAuth[1].tags.some(t=>t[1]==='challenge-slow-new'))
  fail('new connection failed to use its own challenge');
// Another relay cannot acknowledge this connection's authentication event.
R._onMessage({url:'wss://other.example',ws:{}},JSON.stringify(['OK',freshAuth[1].id,true,'']));
if(!R._okWaiters.has(freshAuth[1].id))fail('another connection settled an AUTH waiter');
replacement.reply(['OK',freshAuth[1].id,true,'']);
if(await newAttempt!==true)fail('replacement authentication did not recover');
if(!conn.authPubkeys.has('a'.repeat(64)))fail('accepted owner was not recorded');
R.setAuthSigner(async tpl=>({...tpl,pubkey:'a'.repeat(64),id:String(++signed).padStart(64,'f'),sig:'e'.repeat(128)}));

// A negative AUTH OK settles immediately and completes the denied subscription; no 8s timeout.
R.configure({urls:['wss://reject.example/relay'],verify:false});
await sleep(20);
const reject=WS.all.at(-1); reject.reply(['AUTH','challenge-2']);
let rejectEnded=0;
const rid=R.subscribe([{kinds:[30078],authors:['a'.repeat(64)]}],{onEvent(){},onEose(){rejectEnded++;},live:true});
reject.reply(['CLOSED',rid,'auth-required: authenticate']);
await sleep(20);
const rejectedAuth=reject.sent.find(m=>m[0]==='AUTH');
if(!rejectedAuth)fail('rejected relay received no AUTH');
reject.reply(['OK',rejectedAuth[1].id,false,'invalid: challenge expired']);
await sleep(30);
if(rejectEnded!==1)fail('rejected AUTH left subscription loading');

// Logged-out/locked signers may decline synchronously. The transport must turn that into a normal
// unauthenticated result, finish the subscription, and never leak it as an unhandled rejection.
let unhandled=[];
process.on('unhandledRejection',reason=>unhandled.push(String(reason)));
R.setAuthSigner(()=>{throw new Error('login required for relay AUTH');});
R.configure({urls:['wss://guest.example/relay'],verify:false});
await sleep(20);
const guest=WS.all.at(-1); guest.reply(['AUTH','challenge-guest']);
let guestEnded=0;
const gid=R.subscribe([{kinds:[30078],authors:['b'.repeat(64)]}],{onEvent(){},onEose(){guestEnded++;},live:true});
guest.reply(['CLOSED',gid,'auth-required: authenticate']);
await sleep(30);
if(guest.sent.some(m=>m[0]==='AUTH'))fail('logged-out signer sent an AUTH event');
if(guestEnded!==1)fail('logged-out AUTH challenge left subscription loading');
if(unhandled.length)fail(`logged-out AUTH escaped as unhandled rejection: ${unhandled}`);
console.log('nip78 auth challenge and private subscription retry ok');
process.exit(0);

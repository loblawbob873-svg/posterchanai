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
R.setAuthSigner(async tpl=>{signed++;return {...tpl,pubkey:'a'.repeat(64),id:'f'.repeat(64),sig:'e'.repeat(128)};});
R.configure({urls:['wss://relay.example/relay'],verify:false});
await sleep(20);
const ws=WS.all[0];
ws.reply(['AUTH','challenge-1']);
const id=R.subscribe([{kinds:[30078],authors:['a'.repeat(64)]}],{onEvent(){},onEose(){},live:true});
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
console.log('nip78 auth challenge and private subscription retry ok');
process.exit(0);

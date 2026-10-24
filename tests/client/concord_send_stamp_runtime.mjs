// The REAL reader (cord-reader.js) with real keys: createChatWrap's `notBefore` sets the floor of the message's stamp
// (what every client orders a room by), and a floor in the past changes nothing. See concord.js sendStamp.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
globalThis.window=globalThis;
for(const file of ['static/vendor/nostr/nostr.bundle.js','static/js/client/cord-reader.js','static/js/client/cord-protocol.js'])vm.runInThisContext(fs.readFileSync(file,'utf8'));
const NT=NostrTools,R=PosterCordReader,sk=NT.generateSecretKey(),owner=NT.getPublicKey(sk),sign=e=>NT.finalizeEvent(e,sk);
const made=await PosterCord.createCommunity({owner,name:'Order',relays:['wss://r.example'],base:'https://fixture.example',signEvent:sign});
const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot},id=made.generalChannelId,controls=made.events.filter(e=>e.kind===1059);
const ahead=Date.now()+30000;
const later=await R.createChatWrap(bundle,controls,id,'my answer',owner,sign,[],9,ahead);
assert(later.ms>=ahead,'the stamp ignored the floor: '+later.ms+' < '+ahead);
const past=await R.createChatWrap(bundle,controls,id,'plain',owner,sign,[],9,Date.now()-600000);
assert(Math.abs(past.ms-Date.now())<5000,'a floor in the past moved the stamp');
const plain=await R.createChatWrap(bundle,controls,id,'no floor',owner,sign);
assert(Math.abs(plain.ms-Date.now())<5000);
// What another client reads back: the message carries the floored time, so it orders after what it answered.
const read=await R.inspectChat(bundle,controls,id,[later.wrap,past.wrap,plain.wrap]);
const mine=read.messages.find(m=>m.text==='my answer');
assert(mine && Number(mine.at||mine.ms)>=ahead,'the time a reader sees is not the floored one: '+JSON.stringify(mine&&{at:mine.at,ms:mine.ms}));
console.log('send stamp floor passed');

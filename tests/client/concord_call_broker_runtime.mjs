// Which call broker a Concord call starts with (concord.js callBroker): THIS instance when it hosts calls, a broker
// somebody chose stays theirs, armada.buzz only when neither -- and armada.buzz already in storage is not a choice
// (the panel stores whatever its field held on Join, and the field used to default to it).
import assert from 'node:assert/strict';import fs from 'node:fs';import vm from 'node:vm';
globalThis.window=globalThis;const noop=()=>{};
globalThis.document={querySelector:()=>({}),createElement:()=>({dataset:{}}),head:{appendChild:noop},documentElement:{appendChild:noop},addEventListener:noop};
globalThis.addEventListener=noop;
const store=new Map();globalThis.localStorage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};
globalThis.sessionStorage=localStorage;
let answer=204,asked=[];globalThis.fetch=async url=>{asked.push(url);if(answer===null)throw new Error('offline');return {status:answer};};
const load=()=>{vm.runInThisContext(fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8'));return window.PCConcord.callBroker;};
const p={serverOrigin:()=>'https://poster.place'};

let callBroker=load();
assert.equal(await callBroker(p),'https://poster.place','an instance that hosts calls was not used');
assert.deepEqual(asked,['https://poster.place/.well-known/concord/av']);
await callBroker(p);assert.equal(asked.length,1,'the probe is asked once per page');

store.set('pc.concord.call-broker','https://armada.buzz');callBroker=load();asked=[];
assert.equal(await callBroker(p),'https://poster.place','the old default in storage beat this instance');

store.set('pc.concord.call-broker','https://calls.example');callBroker=load();asked=[];
assert.equal(await callBroker(p),'https://calls.example','a broker somebody chose was overridden');
assert.equal(asked.length,0);

store.clear();answer=404;callBroker=load();
assert.equal(await callBroker(p),'https://armada.buzz','a node without calls must fall back');
store.clear();answer=null;callBroker=load();
assert.equal(await callBroker(p),'https://armada.buzz','an unreachable probe must fall back');
store.clear();answer=204;callBroker=load();asked=[];
assert.equal(await callBroker({serverOrigin:()=>'https://localhost'}),'https://armada.buzz','no instance: nothing to ask');
assert.equal(asked.length,0);
console.log('concord call broker choice ok');

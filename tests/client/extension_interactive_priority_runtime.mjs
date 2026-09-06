import fs from 'node:fs';
import assert from 'node:assert/strict';
const source=fs.readFileSync(process.argv[2] || new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const start=source.indexOf('  const _extGate = {');
const end=source.indexOf('\n  async function _nip17wrapVia',start);
assert(start>=0 && end>start);
const gate=new Function(source.slice(start,end)+';return _extGate;')();
await gate.call(null,async()=> 'initial permission granted');
let started=0,release=[];
const restores=Array.from({length:200},(_,i)=>gate.call('ciphertext-'+i,()=>{
  started++;return new Promise(resolve=>release.push(resolve));
}));
assert.equal(started,5,'background restore must leave an interactive slot');
let signed=false;
const auth=gate.call(null,async()=>{signed=true;return 'session proof';});
assert(signed,'authentication must start before any blocked restore completes');
assert.equal(await auth,'session proof');
while(release.length){
 const batch=release.splice(0);batch.forEach(resolve=>resolve('plaintext'));
 for(let i=0;i<15;i++)await Promise.resolve();
}
await Promise.all(restores);
assert.equal(started,200);
assert.equal(gate._busy,0);assert.equal(gate._background,0);
console.log('ok: authentication starts immediately during 200 pending history decryptions; restore completes');

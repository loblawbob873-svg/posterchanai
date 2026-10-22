// THE SIGNATURE THAT LISTS YOUR FILES (the SHIPPED upload.js blossomListAuth, sliced out and run against a stub signer).
//
// This node's Blossom lists a drive only to its owner (2026-10-21). Every listing in the app asks this helper, so it
// must: sign a kind-24242 `list` event with an expiration; reuse it (a remote signer prompts a phone per signature,
// and Files lists on every open); share one signature between listings that ask at the same moment; sign again near
// expiry and for a different account; and when signing fails, answer {} (the request goes unsigned, the server says
// why) and try again next time rather than remembering the failure.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const src=fs.readFileSync('static/js/client/upload.js','utf8');
const a=src.indexOf('  const _listAuth = new Map();'),b=src.indexOf('  async function uploadBlob(file, opts){');
assert.ok(a>0&&b>a,'blossomListAuth not found');
let signs=0,fail=false,clock=1_800_000_000_000;
const S={ME:{pubkey:'a'.repeat(64)}};
const ctx={S,Map,JSON,Math,Promise,btoa:s=>Buffer.from(s,'binary').toString('base64'),
  Date:{now:()=>clock},
  sign:async(kind,content,tags)=>{signs++;await new Promise(r=>setTimeout(r,5));if(fail)throw new Error('signer said no');return {kind,content,tags,pubkey:S.ME.pubkey,sig:'s'};}};
vm.createContext(ctx);vm.runInContext(src.slice(a,b)+';this.f=blossomListAuth;',ctx);
const decode=h=>JSON.parse(Buffer.from(h.Authorization.slice(6),'base64').toString());
// Two listings at once share ONE signature.
const [x,y]=await Promise.all([ctx.f(),ctx.f()]);
assert.equal(signs,1,'two listings at the same moment asked the signer twice');
assert.equal(x.Authorization,y.Authorization);
const ev=decode(x);assert.equal(ev.kind,24242);
assert.deepEqual(ev.tags.find(t=>t[0]==='t'),['t','list']);
const exp=+ev.tags.find(t=>t[0]==='expiration')[1];assert.ok(exp>clock/1000+3600,'the signature must last hours');
// Reused while it lasts.
clock+=60*60*1000;await ctx.f();assert.equal(signs,1,'re-signed while the signature was still good');
// Near expiry: a fresh one.
clock=(exp-300)*1000;await ctx.f();assert.equal(signs,2,'an almost-expired signature was reused');
// Another account: its own.
S.ME={pubkey:'b'.repeat(64)};await ctx.f();assert.equal(signs,3);
// Signing fails: {} now, and a real attempt next time.
S.ME={pubkey:'c'.repeat(64)};fail=true;assert.equal(JSON.stringify(await ctx.f()),'{}');
fail=false;const ok=await ctx.f();assert.ok(ok.Authorization,'a failed signature was remembered');
// Nobody signed in: nothing to sign.
S.ME=null;assert.equal(JSON.stringify(await ctx.f()),'{}');
console.log('OK');

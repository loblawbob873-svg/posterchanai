import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const sourceUrl = process.argv[2] ? pathToFileURL(process.argv[2]) : new URL('../../static/js/client/vault.js', import.meta.url);
const source = fs.readFileSync(sourceUrl,'utf8');
const core = fs.readFileSync(new URL('vaultcore.js',sourceUrl),'utf8');
async function harness({incomplete=false, locked=false, noKey=false, wrapped=false, leave=null}={}) {
  const data=new Map(), writes=[];
  let calls=0, release;
  const relayWait=new Promise(r=>release=r);
  const ctx=vm.createContext({crypto:webcrypto, URL, Uint8Array, TextEncoder, TextDecoder, atob,btoa,
    setTimeout, clearTimeout, setInterval:()=>0, clearInterval:()=>{},
    navigator:{onLine:true}, document:{querySelector:()=>null,addEventListener:()=>{}},
    localStorage:{getItem:k=>data.get(k)??null,setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)}});
  ctx.window=ctx; ctx.addEventListener=()=>{};
  vm.runInContext(core,ctx);
  const V=ctx.PCVaultCore, key=V.newVaultKey();
  if(!noKey && !wrapped)data.set('pcaiVaultKey:raw:alice',V.toB64(key));
  if(wrapped)data.set('pcaiVaultKey:alice','wrapped-fixture');
  if(locked)data.set('pcaiVaultStayUnlocked','0');
  const events=[];
  for(const [id,uri] of [['printer','http://printer.local'],['chase','https://chase.com'],['wells','https://wellsfargo.com']]){
    events.push({kind:30078,pubkey:'alice',created_at:1,tags:[['d','pcai:pw:'+id],['l','pcai-pw']],
      content:await V.seal(key,{kind:'login',id,title:id,username:'test',password:'fixture-only',uris:[uri],updated:1})});
  }
  ctx.__PC={ME:{pubkey:'alice',mode:'local'},VIEW:'home',isView:()=>false,
    publish:()=>{throw Error('background sync must not create or publish a key');},
    nip44dec:async()=>JSON.stringify({k:V.toB64(key)})};
  ctx.Store={query:()=>noKey?[]:[events[0]]};
  ctx.Relay={ready:async()=>true,query:async filters=>{
    calls++;
    if(filters[0]['#d']) return [];
    await relayWait;
    const result=[...events];result.complete=!incomplete;return result;
  }};
  ctx.Capacitor={Plugins:{VaultAutofill:{put:async({items})=>{writes.push(JSON.parse(items));return {ok:true};},clear:async()=>{}}}};
  vm.runInContext(source,ctx);
  const sync=ctx.PCVault.syncBackground();
  for(let i=0;i<20;i++)await new Promise(r=>setTimeout(r,1));
  assert.equal(writes.length,0,'a printer-only cache must not overwrite Android before the relay answers');
  if(leave === 'logout') await ctx.PCVault.forget();
  if(leave === 'switch') ctx.__PC.ME={pubkey:'bob',mode:'local'};
  release(); await sync;
  return {ctx,writes,calls};
}
const cold=await harness();
assert.equal(cold.ctx.__PC.VIEW,'home');
assert.deepEqual(cold.writes.at(-1).map(x=>x.id).sort(),['chase','printer','wells']);
assert(cold.writes.at(-1).find(x=>x.id==='wells').hosts.includes('wellsfargo.com'));
assert.equal((await harness({wrapped:true})).writes.at(-1).length,3,'a cached wrapped key syncs with the local signer');
assert.equal((await harness({leave:'logout'})).writes.length,0,'logout during a relay read must not repopulate autofill');
assert.equal((await harness({leave:'switch'})).writes.length,0,'late account reads must not populate another account');
const count=cold.writes.length;
await Promise.all(Array.from({length:12},()=>cold.ctx.PCVault.syncBackground()));
assert.equal(cold.writes.length,count+1,'reconnects coalesce and republish even an unchanged complete snapshot');
assert.equal((await harness({incomplete:true})).writes.length,0,'incomplete relay reads preserve native snapshot');
assert.equal((await harness({locked:true})).calls,0,'disabled persistence does not background-unlock');
assert.equal((await harness({noKey:true})).writes.length,0,'background sync must never mint a vault');
console.log('PASS: cold local-key sync without opening Passwords; bank entries, reconnect coalescing, incomplete reads, lock and missing key');

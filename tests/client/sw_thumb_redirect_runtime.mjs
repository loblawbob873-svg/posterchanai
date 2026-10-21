// A THUMBNAIL THAT REDIRECTED TO THE ORIGINAL IS SHOWN, NEVER KEPT (the SHIPPED sw.js cacheFirstMedia).
//
// /client/thumb answers "not made yet" with a no-store 302 to the full-size original. fetch() follows it to a 200,
// and the worker stored THAT under the thumbnail's address, cache-first, for ever -- every picture first seen
// before its thumbnail existed stayed full size on that phone (2026-10-21 Android audit). A real thumbnail (not
// redirected) is still cached; an ordinary cross-origin picture still is.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const src=fs.readFileSync('static/js/client/sw.js','utf8');
const a=src.indexOf('async function cacheFirstMedia(req){'),b=src.indexOf('\n}\n',a)+3;assert.ok(a>0,'cacheFirstMedia not found');
const put=[];
const resp=(url,redirected)=>({status:200,redirected,url,headers:{get:()=>'1000'},clone(){return this;}});
const run=async(url,answer)=>{
  const ctx={caches:{open:async()=>({match:async()=>null,put:async(r,res)=>put.push(r.url)})},MEDIA_CACHE:'m',MEDIA_MAX:10,VIDEO_MAX_BYTES:1e9,
    trimCache:()=>{},URL,self:{location:{protocol:'https:',origin:'https://poster.place'}},
    fetch:async()=>answer,Response:{error:()=>null}};
  vm.createContext(ctx);vm.runInContext(src.slice(a,b)+';this.f=cacheFirstMedia;',ctx);
  return ctx.f({url,destination:'image'});
};
put.length=0;await run('https://poster.place/client/thumb?w=990&u=https%3A%2F%2Fcdn.example%2Fbig.gif',resp('https://cdn.example/big.gif',true));
assert.deepEqual(put,[],'a redirected thumbnail (the full-size original) was cached under the thumbnail address');
put.length=0;await run('https://poster.place/client/thumb?w=990&u=https%3A%2F%2Fcdn.example%2Fbig.png',resp('https://poster.place/client/thumb?w=990&u=x',false));
assert.equal(put.length,1,'a real thumbnail must still be cached');
put.length=0;await run('https://cdn.example/avatar.png',resp('https://cdn.example/avatar.png',false));
assert.equal(put.length,1,'an ordinary picture must still be cached');
console.log('OK');

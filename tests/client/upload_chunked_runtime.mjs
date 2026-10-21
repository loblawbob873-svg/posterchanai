// A FILE OVER CLOUDFLARE'S 100 MB BODY LIMIT GOES UP IN PARTS (the SHIPPED upload.js _putChunked, fake XHR/fetch).
//
// 2026-10-21: the Meme Builder became a video editor and a phone video over 100 MB could not be uploaded from
// outside the LAN. Pinned: <= 64 MB parts carrying the whole file's hash, a dropped part is resent on its own, the
// finish names the part count / size / hash, progress runs to the whole file, and a server that predates part
// uploads (404 on the first part) makes the caller fall back to one PUT.
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const src=fs.readFileSync('static/js/client/upload.js','utf8');
const a=src.indexOf('  const CHUNK_OVER'),b=src.indexOf('  async function uploadBlob(file, opts){');assert.ok(a>0&&b>a,'_putChunked not found');
const MB=1024*1024;
function world(answers){
  const sent=[],fin=[];let n=0;
  class XHR{
    constructor(){this.h={};this.upload={};}
    open(m,u){this.m=m;this.u=u;}
    setRequestHeader(k,v){this.h[k]=v;}
    getResponseHeader(){return null;}
    send(blob){const st=answers(n++,this.u);sent.push({u:this.u,h:this.h,size:blob.size,st});
      setTimeout(()=>{ if(st===0) return this.onerror(); this.status=st; this.upload.onprogress&&this.upload.onprogress({loaded:blob.size}); this.onload(); },0);}
  }
  const ctx={XMLHttpRequest:XHR,crypto:globalThis.crypto,setTimeout:(f)=>setTimeout(f,0),Math,JSON,Error,Promise,Uint8Array,
    fetch:async(u,o)=>{fin.push({u,o});return {ok:true,json:async()=>({url:'https://x/'+'f'.repeat(64)}),headers:{get:()=>null}};}};
  vm.createContext(ctx);vm.runInContext(src.slice(a,b)+';this.put=_putChunked;this.OVER=CHUNK_OVER;',ctx);
  return {ctx,sent,fin};
}
const file={size:150*MB+5,type:'video/mp4',slice(s,e){return {size:e-s};}};
const hdr={Authorization:'Nostr abc','Content-Type':'video/mp4','X-Filename':'clip.mp4'};
const sha='e'.repeat(64);
{ // the happy path, with part 1 dropped once on the way
  const w=world((n,u)=>n===1?0:200);const prog=[];
  assert.ok(w.ctx.OVER<100*MB,'the part threshold must be under the CDN body limit');
  const d=await w.ctx.put(file,'https://poster.place/blossom',hdr,sha,{onProgress:p=>prog.push(p)});
  assert.equal(d.url,'https://x/'+'f'.repeat(64));
  const ok=w.sent.filter(s=>s.st===200);
  assert.deepEqual(ok.map(s=>s.u.split('/').pop()),['0','1','2'],'every part arrives, in order, the dropped one resent');
  assert.ok(ok.every(s=>s.size<=64*MB),'a part over 64 MB');
  assert.equal(ok.reduce((t,s)=>t+s.size,0),file.size);
  assert.ok(ok.every(s=>s.h['X-Upload-Sha256']===sha&&s.h.Authorization==='Nostr abc'),'each part carries the auth and the whole-file hash');
  assert.ok(new Set(ok.map(s=>s.u.split('/')[5])).size===1,'one upload id for every part');
  const body=JSON.parse(w.fin[0].o.body);
  assert.deepEqual([body.parts,body.size,body.sha256],[3,file.size,sha]);
  assert.equal(w.fin[0].o.headers['X-Filename'],'clip.mp4');
  assert.equal(prog[prog.length-1].loaded,file.size,'progress reaches the whole file');
}
{ // an older server: the first part 404s -> null, the caller sends it whole
  const w=world(()=>404);
  assert.equal(await w.ctx.put(file,'https://poster.place/blossom',hdr,sha,{}),null);
  assert.equal(w.fin.length,0);
}
{ // a refusal (403) is an error, not retried
  const w=world(()=>403);
  await assert.rejects(()=>w.ctx.put(file,'https://poster.place/blossom',hdr,sha,{}));
  assert.equal(w.sent.length,1);
}
console.log('OK');

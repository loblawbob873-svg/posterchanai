/* A CHUNKED UPLOAD MUST CARRY A CONTENT CHECKSUM, or nothing verifies the download.
 *
 * `csum` is what the receiving side checks a download against — verifyPart returns early without one
 * — and what lets a resumed download reuse a part file. It used to arrive from the scan, which hashed
 * everything on a first sweep. Skipping that hash (rightly: it reads tens of gigabytes to answer a
 * question about a few hundred paths, and it is why Pause appeared to hang) made "no checksum" the
 * NORMAL state for every large file a phone uploads — so a truncated or mis-assembled video was
 * written to the other device unchecked and nothing noticed. Reported as videos that arrived and
 * would not play.
 *
 * It is hashed at upload time now, natively and streamed. Where the adapter cannot hash, the entry
 * keeps its chunk list as identity exactly as before: unverified, but no worse than it was.
 */
const crypto=require('crypto');
const path=require('path');
const RUN=require(path.join(__dirname,'..','..','static','js','client','syncrun.js'));
const sha=s=>crypto.createHash('sha256').update(String(s)).digest('hex');
const MB=1024*1024, CHUNK=4*MB;
function run(withHashFile){
  const disk={'DCIM/vid.mp4':{size:40*MB,mtime:100}};      // a big file: the chunked path
  const state={manifest:{},base:{}};
  const fs={ chunkBytes:CHUNK,
    scan:async()=>({files:JSON.parse(JSON.stringify(disk)),skipped:[]}),
    scanPage:async(i,s2,o,l)=>({files:o?{}:JSON.parse(JSON.stringify(disk)),skipped:[],total:1,done:true}),
    read:async()=>new Uint8Array(16), readPart:async(i,r,o,l)=>new Uint8Array(l),
    hashPart:async()=>sha('p'), partSize:async()=>0, discardPart:async()=>{},
    writePart:async()=>{}, writeCommit:async()=>({size:40*MB,mtime:200}),
    write:async()=>({size:16,mtime:200}), move:async()=>{}, trash:async()=>{},
    sweepParts:async()=>({removed:0}) };
  if(withHashFile) fs.hashFile=async(i,rel)=>sha('content-of-'+rel);
  const store={ manifest:async()=>JSON.parse(JSON.stringify(state.manifest)),
    base:async()=>JSON.parse(JSON.stringify(state.base)),
    saveBase:async(k,b)=>{state.base=b||{};},
    save:async(k,m)=>{const n=(m&&m.manifest)||{};for(const p of (m&&m.touched)||[]){if(n[p])state.manifest[p]=n[p];}
      if(m&&m.base)state.base=m.base;},
    putBlob:async()=>({sha:sha('u')}),
    putParts:async()=>({sha:sha('u'),chunks:[sha('c1'),sha('c2')],parts:[sha('c1')],cs:CHUNK}),
    getBlob:async()=>new Uint8Array(16), getParts:async(c,w,size)=>{await w(0,new Uint8Array(size||16));},
    hashBytes:async()=>sha('h'), blobSha:async()=>sha('h'), chunkShas:async()=>[] };
  return RUN.sweep(fs,store,{id:'t',key:'Pictures',device:'phone',now:Date.now(),excludes:[],
    maxBytes:8*1024*MB,chunkBytes:CHUNK,chunkAbove:CHUNK}).then(()=>state.manifest['DCIM/vid.mp4']);
}
(async()=>{
  const withIt=await run(true), without=await run(false);
  console.log(JSON.stringify({
    withNativeHash:{ hasCsum: !!(withIt&&withIt.csum), chunks:(withIt&&withIt.chunks||[]).length },
    withoutIt:{ hasCsum: !!(without&&without.csum), chunks:(without&&without.chunks||[]).length },
  },null,1));
})().catch(e=>{console.error(e.stack);process.exit(1)});

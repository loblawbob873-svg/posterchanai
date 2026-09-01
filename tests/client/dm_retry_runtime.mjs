/* A WRAP WHOSE FIRST UNWRAP FAILED MUST BE TRIED AGAIN.
 *
 * Reported as "on desktop, Messages -> DM, it did not load the new message from the user".
 *
 * `ingestWrap` is built to allow the retry: `_wrapTried` returns immediately for a wrap already in
 * flight or done, and it DELETES that entry when the unwrap throws so a redelivery can try again.
 * The live subscription overrode that by gating on the Store's dedup — and the Store answers "seen",
 * not "read". A wrap stored on its first (failed) attempt was dropped on every redelivery, so the
 * message stayed invisible for the rest of the session.
 *
 * This drives the SHIPPED ingestWrap: fail the unwrap once, deliver the same wrap again, and assert
 * the message arrives — and that a wrap which succeeded is never decrypted twice.
 */
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
function extract(decl){
  const start = source.indexOf(decl);
  if(start < 0) throw new Error('missing: ' + decl);
  let open = source.indexOf('{', start), depth = 0, end = -1;
  for(let i=open; i<source.length; i++){
    if(source[i]==='{') depth++;
    else if(source[i]==='}' && --depth===0){ end=i+1; break; }
  }
  if(end < 0) throw new Error('extract failed: ' + decl);
  return source.slice(start, end);
}

const ME_PK = 'a'.repeat(64), PEER = 'b'.repeat(64);
function harness({ failFirst }){
  const dmPeers = new Map(), _wrapTried = new Set();
  const calls = { unwraps: 0 };
  let failed = false;
  const ctx = {
    dmPeers, _wrapTried, ME:{pubkey:ME_PK},
    _dmTotal:0, _dmDone:0, _dmTick(){}, needProfile(){}, _scheduleDmRefresh(){},
    _dmAttachmentMeta: () => [],
    DmCache:{ get: async () => null, put(){} },
    signer:{ nip17unwrap: async () => {
      calls.unwraps++;
      if(failFirst && !failed){ failed = true; throw new Error('signer timed out'); }
      return { kind:14, pubkey:PEER, created_at:1000, content:'hi', tags:[['p',ME_PK]] };
    }},
    ClientSettings:{ get:(k,d)=>d, set(){} }, MUTED:new Set(),
    _dmUnread:0, bumpDm(){}, _dmNotify(){}, dmNotify(){},
    dmUnread:new Map(), _dmBadge(){}, toast(){}, osNotify(){}, profOf:()=>({}),
    document:{ hidden:true, getElementById:()=>null, querySelector:()=>null },
    VIEW:'home', dmActive:'',
    Date, Math, String, Object, Array, JSON, Promise, setTimeout, Number, Boolean,
  };
  vm.runInNewContext(extract('async function ingestWrap(ev, live){') + '\nthis.ingest=ingestWrap;',
                     ctx, {filename:'app-ingest.js'});
  return { ctx, dmPeers, calls };
}

// THE REPORT: the first unwrap fails; the relay redelivers the same wrap.
{
  const { ctx, dmPeers, calls } = harness({ failFirst:true });
  const wrap = { id:'wrap-1' };
  if(await ctx.ingest(wrap, true) !== false) throw new Error('a failed unwrap reported success');
  if((dmPeers.get(PEER)||[]).length !== 0) throw new Error('a failed unwrap filed a message anyway');
  await ctx.ingest(wrap, true);                       // redelivery
  const thread = dmPeers.get(PEER) || [];
  if(thread.length !== 1)
    throw new Error('the redelivered wrap was never retried — this is the missing message');
  if(calls.unwraps !== 2) throw new Error('expected exactly one retry, got ' + calls.unwraps);
}

// A wrap that SUCCEEDS is never decrypted twice, however often it is redelivered.
{
  const { ctx, dmPeers, calls } = harness({ failFirst:false });
  const wrap = { id:'wrap-2' };
  await ctx.ingest(wrap, true);
  await ctx.ingest(wrap, true);
  await ctx.ingest(wrap, true);
  if(calls.unwraps !== 1) throw new Error('a delivered wrap was decrypted ' + calls.unwraps + ' times');
  if((dmPeers.get(PEER)||[]).length !== 1) throw new Error('the same message was filed twice');
}

console.log('ok');

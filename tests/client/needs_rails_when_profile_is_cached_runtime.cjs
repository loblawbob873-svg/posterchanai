'use strict';
/* THE RAIL IS ASKED FOR EVEN WHEN THE PROFILE IS ALREADY CACHED.
 *
 * Reported: "something is broken with zapping, the monero/lightning icon don't show on the post
 * until after you click on it." The NIP-A3 targets ride the profile REQ — one query, two filters —
 * but `needProfile` returns early for an author whose kind-0 is cached, so for THAT author the
 * kind-10133 filter was never sent. `_advertises()` stayed false, the card drew with no ɱ, and
 * opening the post issued its own query, which is when the mark appeared.
 *
 * WHY EVERY EXISTING TEST PASSED: they start COLD. A fresh session looking at a stranger's post
 * takes the `!haveProfile` branch, which was never broken. The bug lives entirely in the WARM
 * path — the common one, since you have seen most authors before. This runs the SHIPPED
 * `needProfile`/`needRails` against both cache states and asserts what goes ON THE WIRE.
 */
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),path=require('path');
const root=path.resolve(__dirname,'../..'),src=fs.readFileSync(root+'/static/js/client/app.js','utf8');
function part(start,end){const a=src.indexOf(start);assert(a>=0,'missing slice start: '+start);
  const b=src.indexOf(end,a+start.length);assert(b>a,'missing slice end: '+end);return src.slice(a,b);}

/* The three functions under test, lifted verbatim from the shipped file. */
const slice = part('  const _railAsked = new Set();', '  // Lazy profile loading (data saver)')
            + part('  async function flushProfiles(){', '  async function fetchMyProfile(){');

function run(cached){
  const sent=[];
  /* The timer is RECORDED, never run: `flushProfiles` clears `_profQ`, so firing it here would
     empty the very queue this case is about and the assertion would pass for the wrong reason.
     (It did, on the first draft — the warm case reported 0 queued because the flush had drained
     it, not because nothing was queued.) */
  const ctx={
    console,setTimeout:()=>1,clearTimeout(){},Date,Set,Map,
    _profQ:new Set(),_profT:null,_profMiss:new Map(),_PROF_MISS_TTL:300000,
    Store:{ haveProfile:(pk)=>cached.has(pk), saveProfile(){}, saveEvent(){} },
    Relay:{ ready:async()=>true, query:async(filters)=>{ sent.push(filters); return []; } },
    _learnRailsFromEvent:()=>false, renderMe(){}, decorateProfiles(){},
  };
  vm.createContext(ctx);
  vm.runInContext(slice+'\nthis.__needProfile=needProfile;this.__flush=flushProfiles;', ctx);
  ctx.__needProfile('ab'.repeat(32));
  return { sent, queued:[...ctx._profQ], missed:[...ctx._profMiss.keys()] };
}

(async()=>{
  const PK='ab'.repeat(32);

  // COLD: the branch that always worked.
  {
    const r=run(new Set());
    assert.equal(r.queued.length,1,'a stranger was not queued');
  }

  // WARM: the reported bug. The profile is cached; the RAIL still has to be asked for.
  {
    const r=run(new Set([PK]));
    assert.equal(r.queued.length,1,
      'an author whose profile is cached was never queued, so no kind-10133 is ever sent — '
      + 'this is the reported bug: the ɱ mark only appears after you open the post');
  }

  // The REQ that goes out must carry BOTH filters, or queuing achieved nothing.
  {
    const sent=[];
    const ctx={console,setTimeout:(fn)=>{fn();return 1;},clearTimeout(){},Date,Set,Map,
      _profQ:new Set([PK]),_profT:null,_profMiss:new Map(),_PROF_MISS_TTL:300000,
      Store:{haveProfile:()=>true,saveProfile(){},saveEvent(){}},
      Relay:{ready:async()=>true,query:async(f)=>{sent.push(f);return [];}},
      _learnRailsFromEvent:()=>false,renderMe(){},decorateProfiles(){}};
    vm.createContext(ctx);
    vm.runInContext(slice+'\nthis.__flush=flushProfiles;',ctx);
    await ctx.__flush();
    assert.equal(sent.length,1,'no query was sent');
    /* JSON, not deepEqual: `sent` was built INSIDE the vm realm, so its arrays have that realm's
       Array.prototype and a structural compare fails on identical contents. This harness has been
       bitten by exactly that before. */
    const kinds=JSON.stringify(sent[0].map(f=>f.kinds[0]).sort((a,b)=>a-b));
    assert.equal(kinds,'[0,10133]','the rails filter is missing from the REQ: '+kinds);
  }

  // A pubkey queued only for its RAIL must not be recorded as a missing profile.
  {
    const ctx={console,setTimeout:(fn)=>{fn();return 1;},clearTimeout(){},Date,Set,Map,
      _profQ:new Set([PK]),_profT:null,_profMiss:new Map(),_PROF_MISS_TTL:300000,
      Store:{haveProfile:()=>true,saveProfile(){},saveEvent(){}},
      Relay:{ready:async()=>true,query:async()=>[]},
      _learnRailsFromEvent:()=>false,renderMe(){},decorateProfiles(){}};
    vm.createContext(ctx);
    vm.runInContext(slice+'\nthis.__flush=flushProfiles;',ctx);
    await ctx.__flush();
    assert.equal(ctx._profMiss.size,0,
      'a profile we already hold was cached as a MISS, backing off a profile that is not missing');
  }

  // Asked ONCE. The rail is not re-requested for every card by the same author.
  {
    const ctx={console,setTimeout:(fn)=>{fn();return 1;},clearTimeout(){},Date,Set,Map,
      _profQ:new Set(),_profT:1,_profMiss:new Map(),_PROF_MISS_TTL:300000,
      Store:{haveProfile:()=>true,saveProfile(){},saveEvent(){}},
      Relay:{ready:async()=>true,query:async()=>[]},
      _learnRailsFromEvent:()=>false,renderMe(){},decorateProfiles(){}};
    vm.createContext(ctx);
    vm.runInContext(slice+'\nthis.__needProfile=needProfile;',ctx);
    for(let i=0;i<5;i++) ctx.__needProfile(PK);
    assert.equal(ctx._profQ.size,1,
      'five cards by one author must cost one rail lookup, not five');
  }

  console.log('OK a cached profile still learns its payment rail');
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});

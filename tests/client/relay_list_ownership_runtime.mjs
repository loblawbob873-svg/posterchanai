/* THE SAVED RELAY LIST IS THE USER'S — WE ONLY EVER ADD TO IT.
 *
 * Runs the SHIPPED seedRelaysFromNip65 / _dropLegacyAutoRelays / _relayTagUrls out of app.js
 * against a stub relay and a stub localStorage. Source assertions cannot see any of this: the two
 * failures are a list that gets SHORTER and a publish that goes out on a read nobody answered.
 */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const code=fs.readFileSync(process.env.PC_APP_SOURCE||new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const cut=(from,to)=>{const i=code.indexOf(from);assert(i>=0,'missing: '+from);
  const j=code.indexOf(to,i);assert(j>i,'missing: '+to);return code.slice(i,j);};

/* The relay-list policy, plus the two helpers it leans on, taken verbatim. */
const slice=cut('  function _dropLegacyAutoRelays(){','  function defaultRelays(){')
  +cut('  function normalizeRelay(u){','  function mergeRelays(');

function makeCtx({saved={}, nip65=null, complete=true, verify=()=>true}={}){
  const store={...saved};
  const ctx={console,Map,Set,Promise,JSON,String,Number,Array,URL,setTimeout,
    published:[], toasts:[], dialled:0, drawn:0,
    GUEST:false, ME:{pubkey:'me'},
    NostrTools:{verifyEvent:verify},
    ClientSettings:{get:(k,d)=>store[k]===undefined?d:store[k], set:(k,v)=>{store[k]=v;}},
    userRelays:()=>(store.relays||[]).map(u=>String(u||'').trim()).filter(Boolean),
    LEGACY_AUTO_RELAYS:['wss://relay.poster.place/','wss://nos.lol/','wss://relay.primal.net/',
                        'wss://nostr.mom/','wss://offchain.pub/','wss://relay.ditto.pub/'],
    _nostrPrefsLoaded:false, _setRelays:[], drawRelayRows(){ctx.drawn++;},
    connectRelays(){ctx.dialled++;},
    toast:m=>{ctx.toasts.push(m);},
    _store:store};
  ctx.window=ctx;
  ctx.Relay={query:async()=>{const out=nip65?[nip65]:[];out.complete=complete;return out;}};
  vm.createContext(ctx);
  // `let _nip65Confirmed` is a lexical binding, not a global — expose a reader for it.
  vm.runInContext(slice+'\nglobalThis.nip65Confirmed=()=>_nip65Confirmed;',ctx);
  return ctx;
}
/* Arrays built inside the vm carry that context's Array prototype, which deepStrictEqual counts as
 * a difference. Compare the VALUES. */
const same=(got,want,msg)=>assert.deepEqual(JSON.parse(JSON.stringify(got===undefined?null:got)),want,msg);

const ev=(urls,extra={})=>({id:'a1',kind:10002,pubkey:'me',created_at:100,
  tags:urls.map(u=>Array.isArray(u)?['r',...u]:['r',u]),...extra});

/* ---- 1. THE SEED IS ADDITIVE ------------------------------------------------------------- */
{
  const c=makeCtx({saved:{relays:['wss://mine.example'],relaysEnabled:true},
                   nip65:ev(['wss://theirs.example'])});
  assert.equal(await c.seedRelaysFromNip65(),true);
  same(c._store.relays,['wss://mine.example','wss://theirs.example'],
    'the published list JOINS this device\'s — neither replaces the other');
  assert.equal(c.dialled,1,'the newly learned relay is actually dialled');
  assert.equal(c.published.length,0,'seeding must never publish');
}

/* ---- 2. A SHORT PUBLISHED LIST IS NOT A DELETE ORDER -------------------------------------- */
{
  const c=makeCtx({saved:{relays:['wss://a.example','wss://b.example'],relaysEnabled:true},
                   nip65:ev(['wss://a.example'])});
  await c.seedRelaysFromNip65();
  same(c._store.relays,['wss://a.example','wss://b.example'],
    'a relay absent from the published list must not be dropped from the device');
}

/* ---- 3. "COULD NOT ASK" IS NEVER "THEY HAVE NONE" ----------------------------------------- */
{
  const c=makeCtx({saved:{relays:['wss://mine.example'],relaysEnabled:true},
                   nip65:ev(['wss://theirs.example']), complete:false});
  assert.equal(await c.seedRelaysFromNip65(),false);
  same(c._store.relays,['wss://mine.example'],'an unanswered REQ changes nothing');
  assert.equal(c.nip65Confirmed(),false,'…and must not unlock the publish');
}

/* ---- 4. A COMPLETE ANSWER OF "NOTHING" IS SAFE TO PUBLISH OVER ---------------------------- */
{
  const c=makeCtx({saved:{},nip65:null,complete:true});
  assert.equal(await c.seedRelaysFromNip65(),false,'nothing to add');
  assert.equal(c.nip65Confirmed(),true,'but the question WAS answered, so a first publish is safe');
}

/* ---- 5. A FORGED OR FOREIGN 10002 IS NOT THE USER'S --------------------------------------- */
{
  const bad=makeCtx({saved:{},nip65:ev(['wss://evil.example']),verify:()=>false});
  await bad.seedRelaysFromNip65();
  same(bad._store.relays,null,'an unverifiable event seeds nothing');
  const other=makeCtx({saved:{},nip65:{...ev(['wss://evil.example']),pubkey:'someone-else'}});
  await other.seedRelaysFromNip65();
  same(other._store.relays,null,'another author\'s relay list is not ours to adopt');
}

/* ---- 6. URL RULES: wss only, no embedded credentials, markers kept ------------------------ */
{
  const c=makeCtx({saved:{},nip65:ev([['wss://read.example','read'],['wss://write.example','write'],
    'ws://plain.example','wss://user:pw@creds.example','https://notarelay.example','wss://frag.example/#x'])});
  await c.seedRelaysFromNip65();
  same(c._store.relays,['wss://read.example','wss://write.example'],
    'both NIP-65 markers are dialled; ws://, credentials, non-wss and fragments are refused');
}

/* ---- 7. A STALE SETTINGS PANE MUST NOT WRITE THE PRE-SEED LIST BACK ----------------------- */
{
  const c=makeCtx({saved:{relays:['wss://mine.example'],relaysEnabled:true},
                   nip65:ev(['wss://theirs.example'])});
  c._nostrPrefsLoaded=true; c._setRelays=['wss://mine.example'];
  await c.seedRelaysFromNip65();
  same(c._setRelays,['wss://mine.example','wss://theirs.example'],
    'the open Settings pane follows the seed, or its next Save removes what was just added');
  assert.equal(c.drawn,1,'…and is redrawn, so the rows match what a Save would write');
}

/* ---- 7b. TURNING THE SWITCH BACK ON WOULD BE THE SAME BUG, SIGN REVERSED -------------------- */
{
  // Relays deliberately OFF with a list already saved: still add, never re-enable.
  const c=makeCtx({saved:{relays:['wss://mine.example'],relaysEnabled:false},
                   nip65:ev(['wss://theirs.example'])});
  await c.seedRelaysFromNip65();
  same(c._store.relays,['wss://mine.example','wss://theirs.example'],'the list still grows');
  assert.equal(c._store.relaysEnabled,false,'a switch the user turned off stays off');
  assert.equal(c.dialled,0,'and nothing redials a pool that did not change');
}
{
  /* NO CONFIGURATION AT ALL — and the switch STILL stays where it was.
   *
   * This used to assert the opposite: "there is nothing to override, so setting them up IS the
   * feature". Reported twice as "for some reason, use my own relays got enabled again!", and the
   * reasoning is what was wrong, not the code. An empty saved list is NOT only a first-time user:
   * it is also exactly what somebody has the instant they switch their own relays off, because
   * `_dropLegacyAutoRelays` writes `relays: []` and `relaysEnabled: false` together. So the repair
   * that turned the switch off was undone here on the very next pass, for ever — and everybody
   * who had never configured relays here but whose OTHER client had published a kind-10002, which
   * is most people, was opted in without being asked.
   *
   * Publishing a relay list says where to find you. Talking ONLY to your own relays is a different
   * statement and only the person makes it. The list still grows, so it is one tick away. */
  const c=makeCtx({saved:{},nip65:ev(['wss://theirs.example'])});
  assert.equal(await c.seedRelaysFromNip65(),true);
  same(c._store.relays,['wss://theirs.example'],'what we found is still added');
  assert.notEqual(c._store.relaysEnabled,true,
    'the seeder switched "use my own relays" on by itself — that is the report');
  assert.equal(c.dialled,0,'with the switch off the pool reads no user list, so nothing redials');
}

/* ---- 8. THE LEGACY DROP IS ONE SHOT ------------------------------------------------------- */
{
  const six=['wss://relay.poster.place','wss://nos.lol','wss://relay.primal.net',
             'wss://nostr.mom','wss://offchain.pub','wss://relay.ditto.pub'];
  const c=makeCtx({saved:{relays:six.slice(),relaysEnabled:true}});
  assert.equal(c._dropLegacyAutoRelays(),true,'the old build\'s accident is still cleaned up once');
  same(c._store.relays,[]);
  // The user puts them back ON PURPOSE. That is a choice, and it must survive every later boot.
  c._store.relays=six.slice(); c._store.relaysEnabled=true;
  for(let boot=0;boot<5;boot++) assert.equal(c._dropLegacyAutoRelays(),false);
  same(c._store.relays,six,'a deliberate list must never be deleted again');
}
{
  // Relays off at the time of the repair: nothing to clean, and the marker still burns, so turning
  // that exact list on later is read as the choice it is.
  const c=makeCtx({saved:{relays:[],relaysEnabled:false}});
  assert.equal(c._dropLegacyAutoRelays(),false);
  assert.equal(c._store.legacyAutoRelaysCleared,true);
}

console.log('relay list ownership: additive seed, no silent removal, publish gated on a real read');

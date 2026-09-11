/* THE PHONE'S PUSH TOGGLES ARE A SECOND, SEPARATE SET — and must stay separate.
 *
 * Runs the SHIPPED per-device push-preference layer out of app.js against a stub localStorage.
 * What is being proven is a NEGATIVE — that setting one list does not move the other — which no
 * source assertion can see, because both lists are spelled almost identically.
 */
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';

const code=fs.readFileSync(process.env.PC_APP_SOURCE||new URL('../../static/js/client/app.js',import.meta.url),'utf8');
const cut=(from,to)=>{const i=code.indexOf(from);assert(i>=0,'missing: '+from);
  const j=code.indexOf(to,i);assert(j>i,'missing: '+to);return code.slice(i,j);};

/* The account-wide set, then the per-device push set that sits beside it. */
const slice=cut('  const _NOTIFICATION_TYPES','  function _hydrateNotificationPreferences(');

function ctx(){
  const mem=new Map();
  const c={console,Map,Set,JSON,String,Number,Array,Object,Boolean,setTimeout,clearTimeout,
    ME:{pubkey:'owner1'},
    localStorage:{getItem:k=>mem.has(k)?mem.get(k):null,setItem:(k,v)=>mem.set(k,String(v)),
                  removeItem:k=>mem.delete(k)},
    _paintNotificationSettings(){},
    // The debounced mirror fires ~2.5s after a toggle and reaches for the DOM and the network.
    // Neither exists here; stubbing them keeps this harness about the PREFERENCES, and proves the
    // mirror cannot take the page down when it runs somewhere without them.
    document:{querySelector:()=>null},
    _standalone:()=>true,
    _mem:mem};
  c.window=c;
  vm.createContext(c);
  vm.runInContext(slice+'\nglobalThis.TYPES=_NOTIFICATION_TYPES;globalThis.pushState_=()=>_pushPrefState();',c);
  return c;
}

/* ---- 1. DEFAULT ON. Shipping this must not silence somebody who never opened the tab. ---- */
{
  const c=ctx();
  for(const [k] of c.TYPES) assert.equal(c.pushPreference(k),true,k+' must default to on');
}

/* ---- 2. IT PERSISTS, under a key of its own ---- */
{
  const c=ctx();
  assert.equal(c.setPushPreference('likes',false),true);
  assert.equal(c.pushPreference('likes'),false);
  assert.equal(c.pushPreference('replies'),true,'one toggle is not all of them');
  assert(c._mem.has('pc_push_prefs:owner1'),'stored under a per-device key');
  assert.deepEqual(JSON.parse(c._mem.get('pc_push_prefs:owner1')),{likes:false});
}

/* ---- 3. IT HYDRATES from what was stored, in a fresh page ---- */
{
  const first=ctx();
  first.setPushPreference('likes',false);
  first.setPushPreference('zaps',false);
  const saved=first._mem.get('pc_push_prefs:owner1');

  const second=ctx();                       // a new "page load" with the same storage
  second._mem.set('pc_push_prefs:owner1',saved);
  assert.equal(second.pushPreference('likes'),false,'a stored choice survives a reload');
  assert.equal(second.pushPreference('zaps'),false);
  assert.equal(second.pushPreference('replies'),true);
}

/* ---- 4. THE TWO SETS ARE INDEPENDENT — the reason this layer exists at all ---- */
{
  const c=ctx();
  c.setPushPreference('likes',false);            // phone: no likes when the app is closed
  assert.equal(c.notificationPreference('likes'),true,
    'silencing PUSH must not silence the in-app/desktop alert — that is the whole split');
  assert.equal(c._mem.has('pc_notification_prefs:owner1'),false,
    'and it must not write into the synced account store at all');
}
{
  const c=ctx();
  // …and the other way round: the synced set must not be read into the push boxes.
  c._mem.set('pc_notification_prefs:owner1',JSON.stringify({values:{likes:false},dirty:{},clock:0}));
  assert.equal(c.notificationPreference('likes'),false);
  assert.equal(c.pushPreference('likes'),true,
    'a push toggle nobody set is on, whatever the account-wide set says');
}

/* ---- 5. PER OWNER. Switching accounts must not inherit the last one's choices ---- */
{
  const c=ctx();
  c.setPushPreference('likes',false);
  c.ME={pubkey:'owner2'};
  assert.equal(c.pushPreference('likes'),true,'another account starts from the defaults');
  c.ME={pubkey:'owner1'};
  assert.equal(c.pushPreference('likes'),false,'and the first account still has its own');
}

/* ---- 6. ONLY REAL TOGGLES ARE STORED ---- */
{
  const c=ctx();
  assert.equal(c.setPushPreference('not_a_type',false),false,'an unknown key is refused');
  assert.equal(c._mem.has('pc_push_prefs:owner1'),false);
  c._mem.set('pc_push_prefs:owner1',JSON.stringify({likes:'no',replies:false}));
  assert.equal(c.pushPreference('likes'),true,'a non-boolean is not a preference');
  assert.equal(c.pushPreference('replies'),false);
}

/* ---- 7. CORRUPT STORAGE FAILS OPEN, exactly like the server ---- */
{
  const c=ctx();
  c._mem.set('pc_push_prefs:owner1','{ not json');
  for(const [k] of c.TYPES) assert.equal(c.pushPreference(k),true);
}

/* ---- 8. THE PHONE'S OWN COPY IS WRITTEN BY THE TOGGLE, not by the server mirror ----
 *
 *     "i am getting push notifications for likes when I only have DM's and concord
 *      mentions selected on the android app"
 *
 * Both filters fail OPEN on purpose (app/services/push_prefs.py says why at length), so the symptom
 * is never a wrong decision — it is that neither filter was ever TOLD. The device copy used to be
 * written from inside `mirrorPushPrefs`, after its early returns and behind the same
 * `pushState()==='on'` / `!_standalone()` preconditions as the server call it is supposed to back
 * up. A backstop that shares the failure mode of the thing it backs up is not a backstop.
 *
 * `_standalone()` is TRUE in this harness and `pushState` is not stubbed at all — i.e. the mirror is
 * IMPOSSIBLE here — and the phone must still be told.
 */
{
  const c=ctx();
  const told=[];
  c._capPlugin=(name,method)=>name==='PosterChanPush'&&method==='setPrefs'
    ? {setPrefs:async o=>{told.push(o&&o.prefs);}} : null;
  c.setPushPreference('likes',false);
  await new Promise(r=>setTimeout(r,0));
  assert.equal(told.length,1,'a toggle did not reach the device store at all');
  /* THE COMPLETE PICTURE, NOT THE DELTA. This used to assert exactly {"likes":false} — the sparse
     STORED shape passed straight through. Both filters fail open on a missing key (push_prefs.py
     argues it at length), so every type nobody had touched arrived as "no opinion" and was sent:
     mentions kept buzzing with only DMs, Zaps, Concord mentions and Texts selected. The store is
     still sparse; the WIRE now resolves every known type. */
  assert.equal(told[0].likes,false,'the explicit choice was lost on the way to the device');
  assert.ok(Object.keys(told[0]).length>1,
    'the device store is told only about the toggle that moved, so every other type is a gap a '
    +'fail-open filter turns into a notification');
  for(const [k] of c._NOTIFICATION_TYPES||[])
    assert.equal(typeof told[0][k],'boolean','no answer for '+k+' — a gap, not a preference');
  c.setPushPreference('zaps',false);
  await new Promise(r=>setTimeout(r,0));
  /* "The WHOLE set" is what this always meant, and now it is literally true: every accumulated
     choice AND every type nobody has touched, each as a boolean. Before, "whole" meant "both keys
     that happen to have been switched", and the rest reached a fail-open filter as silence. */
  assert.equal(told[1].likes,false,'an earlier choice was dropped when the next one was made');
  assert.equal(told[1].zaps,false,'the new choice did not reach the device');
  for(const [k] of c._NOTIFICATION_TYPES||[])
    assert.equal(typeof told[1][k],'boolean','no answer for '+k+' — a gap, not a preference');
}

/* ---- 9. AN APK WITHOUT THE PLUGIN METHOD MUST NOT THROW ----
 * The write is best-effort by design: an older build has no `setPrefs`, and a preference that
 * cannot be stored natively still has the server filter. What it must never do is take the toggle
 * down with it. */
{
  const c=ctx();
  c._capPlugin=()=>null;
  assert.equal(c.setPushPreference('likes',false),true);
  assert.equal(c.pushPreference('likes'),false);
}
{
  const c=ctx();
  c._capPlugin=()=>({setPrefs:async()=>{throw new Error('plugin exploded');}});
  assert.equal(c.setPushPreference('likes',false),true,'a refusing plugin must not break the toggle');
  await new Promise(r=>setTimeout(r,0));
}

/* ---- 10. THE SERVER IS TOLD WHICH DEVICE, and the endpoint string is `pcdirect:<id>` ----
 *
 * `mirrorPushPrefs` used to split the endpoint and require `direct:<x>:<id>`. The plugin has always
 * answered `"pcdirect:" + deviceId` — two fields, and a first field that is not `direct` — so
 * `device_id` was never set, the "never fall back to all my devices" guard returned before the
 * POST, and every phone's subscription row kept `prefs = NULL`. push_prefs then fails open, by
 * design, and the server sends everything while the tab shows likes switched off.
 */
{
  const c=ctx();
  const posted=[];
  c._standalone=()=>false;
  c._notificationOwner=()=>'owner1';
  c.pushState=async()=>'on';
  c._pushPlugin=()=>({getEndpoint:async()=>({endpoint:'pcdirect:dev-abc',deviceId:'dev-abc'})});
  c._capPlugin=(n,m)=>m==='setPrefs'?{setPrefs:async()=>{}}:null;
  c.sign=async()=>({id:'auth'});
  c.btoa=s=>Buffer.from(s,'binary').toString('base64');
  c.fetch=async(url,init)=>{posted.push([url,JSON.parse(init.body)]);return {json:async()=>({ok:true})};};
  c.setPushPreference('likes',false);
  assert.equal(await c.mirrorPushPrefs('owner1'),true,'the mirror gave up before the POST');
  assert.equal(posted.length,1);
  assert.equal(posted[0][0],'/api/push/prefs');
  assert.equal(posted[0][1].device_id,'dev-abc',
    'the server was not told WHICH device — the row keeps prefs=NULL and sends everything');
  /* The SERVER row gets the complete picture too — it is the other fail-open filter, and a
     backstop that shares the gap it backs up is not a backstop. */
  assert.equal(posted[0][1].prefs.likes,false,'the choice never reached the server row');
  for(const [k] of c._NOTIFICATION_TYPES||[])
    assert.equal(typeof posted[0][1].prefs[k],'boolean',
      'the server row has no answer for '+k+', and push_prefs.allows() reads that as SEND');
}

/* A plugin that answers only the endpoint string (an older build, or a future rename) must still
 * resolve — the id is the LAST field, whichever prefix it carries. */
{
  const c=ctx();
  const posted=[];
  c._standalone=()=>false;
  c._notificationOwner=()=>'owner1';
  c.pushState=async()=>'on';
  c._pushPlugin=()=>({getEndpoint:async()=>({endpoint:'pcdirect:dev-xyz'})});
  c._capPlugin=()=>null;
  c.sign=async()=>({id:'auth'});
  c.btoa=s=>Buffer.from(s,'binary').toString('base64');
  c.fetch=async(url,init)=>{posted.push(JSON.parse(init.body));return {json:async()=>({ok:true})};};
  await c.mirrorPushPrefs('owner1');
  assert.equal(posted.length,1,'no POST at all — the endpoint shape defeated the guard again');
  assert.equal(posted[0].device_id,'dev-xyz');
}

/* ---- 11. THE ONE-SHOT RE-SEND, for a phone that registered before any of this ----
 * Its row is NULL and nobody will touch a toggle again, so the bug would survive the fix. It costs
 * a SIGNATURE, so it must happen once and then never until the answer changes. */
{
  const c=ctx();
  let signs=0;
  const told=[];
  c._standalone=()=>false;
  c._notificationOwner=()=>'owner1';
  c.pushState=async()=>'on';
  c._pushPlugin=()=>({getEndpoint:async()=>({deviceId:'dev-abc'})});
  c._capPlugin=(n,m)=>m==='setPrefs'?{setPrefs:async o=>{told.push(o.prefs);}}:null;
  c.sign=async()=>{signs++;return {id:'auth'};};
  c.btoa=s=>Buffer.from(s,'binary').toString('base64');
  c.fetch=async()=>({json:async()=>({ok:true})});
  c._mem.set('pc_push_prefs:owner1',JSON.stringify({likes:false}));

  c._resendPushPrefsOnce('owner1');
  await new Promise(r=>setTimeout(r,0));
  assert.equal(signs,1,'a device the server was never told about was not re-sent');
  assert(told.length>=1,'and the phone was told too, which costs nothing');

  c._resendPushPrefsOnce('owner1');
  await new Promise(r=>setTimeout(r,0));
  assert.equal(signs,1,'it asked the signer again for prefs the server already has');

  c._mem.set('pc_push_prefs:owner1',JSON.stringify({likes:false,zaps:false}));
  c._resendPushPrefsOnce('owner1');
  await new Promise(r=>setTimeout(r,0));
  assert.equal(signs,2,'a changed answer must be re-sent');
}

console.log('push prefs: default on, persist, hydrate, per owner, independent of the synced set, '
          + 'written to the device by the toggle, and scoped to the right device on the server');

process.exit(0);

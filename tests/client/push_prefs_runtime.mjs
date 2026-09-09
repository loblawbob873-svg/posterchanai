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

console.log('push prefs: default on, persist, hydrate, per owner, and independent of the synced set');

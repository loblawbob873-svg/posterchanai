/* PosterChan Passwords — the background half.
 *
 * WHAT THIS IS. The extension holds a VAULT KEY handed to it by the app at pairing (see vault.js's
 * openPair). With it, it reads the user's `pcai:pw:*` events straight off their relay and opens them
 * locally. The server is never asked for a password and could not answer if it were: it has only
 * ciphertext, and not the key.
 *
 * TWO PAIRING MODES, chosen per install by the person doing it:
 *   read-only — the vault key only. Fill, generate, show one-time codes. A NEW login saved here is
 *               queued locally until the app publishes it. A stolen browser profile costs the
 *               passwords, not the identity.
 *   full      — also the signing key, so this browser can publish vault events itself and saving
 *               works standalone. A stolen browser profile also lets someone post as you. Stated in
 *               those words on the pairing screen, because it is the user's call to make.
 *
 * WHAT A HOSTILE RELAY CAN AND CANNOT DO. Item bodies are AES-GCM, which is authenticated: a relay
 * cannot forge or alter one without the key, so it cannot make this extension fill an attacker's
 * password into your bank. What it CAN do is withhold an update or replay an older event, i.e. show
 * you a password you have since changed. Newest-created_at-wins per item bounds that to "stale",
 * never "attacker-chosen", and a stale fill fails visibly at the login form. Worth knowing; not
 * worth pretending otherwise.
 */
'use strict';

const B = (typeof browser !== 'undefined') ? browser : chrome;
const V = (typeof PCVaultCore !== 'undefined') ? PCVaultCore : self.PCVaultCore;
const NT = () => (typeof NostrTools !== 'undefined' ? NostrTools : self.NostrTools);

const KIND = 30078;
const D_ITEM = 'pcai:pw:';
const D_FOLDER = 'pcai:pwfolder:';
const D_KEY = 'pcai:pwkey';
const L_TAG = 'pcai-pw';

// ---------------------------------------------------------------- state

let cfg = null;          // { pubkey, key(b64), relay, mode, sk? }
let key = null;          // raw Uint8Array(32)
let items = new Map();   // id -> item
/* ONE SOCKET PER RELAY. The vault is published to every relay the app is connected to, so reading
 * from only one made that one a single point of failure for getting at a password. Events are
 * merged by absorb(), which is newest-wins per item, so several relays answering the same
 * subscription is exactly what we want: whichever is reachable, and whichever is most current,
 * wins. A relay that is down simply contributes nothing. */
const conns = new Map();         // url -> { ws, timer, backoff, ready }
let ws = null, wsTimer = null, backoff = 1000;   // the primary, kept for publishAndWait
const okWaiters = new Map();     // event id -> resolver, for publishAndWait
let lastSync = 0, status = 'not paired';

async function loadCfg(){
  const got = await B.storage.local.get(['cfg', 'items', 'outbox']);
  cfg = got.cfg || null;
  if(cfg && cfg.key) key = V.fromB64(cfg.key);
  // The decrypted set is cached so the popup opens instantly and works with no network at all —
  // the same promise the app makes on a phone. It is written to extension storage, which is as
  // protected as the vault key sitting beside it; caching only the ciphertext would buy nothing
  // while making every popup wait on a relay.
  for(const it of (got.items || [])) items.set(it.id, it);
  status = cfg ? (items.size ? 'ready' : 'connecting…') : 'not paired';
}

async function saveItems(){
  await B.storage.local.set({ items: Array.from(items.values()) });
}

// ---------------------------------------------------------------- relay

function relayUrls(){
  if(!cfg) return [];
  return [...new Set([...(cfg.relays || []), cfg.relay].filter(Boolean))].slice(0, 6);
}

function connect(){
  const want = relayUrls();
  if(!want.length) return;
  for(const [u, c] of conns){ if(!want.includes(u)){ closeConn(c); conns.delete(u); } }
  for(const u of want) if(!conns.has(u)) openConn(u);
  refreshStatus();
}

function closeConn(c){
  if(!c) return;
  clearTimeout(c.timer);
  try{ if(c.ws){ c.ws.onclose = c.ws.onerror = c.ws.onmessage = c.ws.onopen = null; c.ws.close(); } }catch(_){ }
}

function openConn(url){
  const c = conns.get(url) || { ws:null, timer:null, backoff:1000, ready:false };
  conns.set(url, c);
  closeConn(c);                 // detach before replacing, or the close we cause schedules a retry
  c.ready = false;
  try{ c.ws = new WebSocket(url); }
  catch(_){ return retry(url); }
  c.ws.onopen = () => {
    c.backoff = 1000;
    // The primary socket is whichever opened first: publishing needs ONE that is definitely up.
    if(!ws || ws.readyState !== 1) ws = c.ws;
    try{ c.ws.send(JSON.stringify(
      ['REQ', 'pcvault', { kinds:[KIND], authors:[cfg.pubkey], '#l':[L_TAG], limit: 5000 }])); }catch(_){ }
    refreshStatus();
  };
  c.ws.onmessage = (e) => {
    let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
    if(m[0] === 'EVENT' && m[2]) absorb(m[2]);
    else if(m[0] === 'OK'){ const w = okWaiters.get(m[1]); if(w) w(m[2] === true); }
    else if(m[0] === 'EOSE'){ c.ready = true; lastSync = Date.now(); saveItems(); flushOutbox(); refreshStatus(); }
  };
  c.ws.onclose = () => { c.ready = false; if(ws === c.ws) ws = _anyOpen(); refreshStatus(); retry(url); };
  c.ws.onerror = () => { try{ c.ws.close(); }catch(_){ } };
}

function _anyOpen(){
  for(const c of conns.values()) if(c.ws && c.ws.readyState === 1) return c.ws;
  return null;
}

function retry(url){
  const c = conns.get(url);
  if(!c) return;
  clearTimeout(c.timer);
  c.timer = setTimeout(() => openConn(url), c.backoff);
  c.backoff = Math.min(c.backoff * 2, 60000);
}

/* What the popup shows. "1 of 3 relays" is the honest version of "offline" when two are down and
 * everything still works — and of "ready" when the only one that answered is stale. */
function refreshStatus(){
  const total = relayUrls().length;
  const up = [...conns.values()].filter(c => c.ws && c.ws.readyState === 1).length;
  if(!cfg) status = 'not paired';
  else if(!up) status = 'offline';
  else if([...conns.values()].some(c => c.ready)) status = total > 1 ? `ready · ${up}/${total} relays` : 'ready';
  else status = 'syncing…';
}

function send(msg){
  // To EVERY open relay: a subscription or a publish should not depend on which one happens to be up.
  let sent = 0;
  for(const c of conns.values()){
    if(!c.ws || c.ws.readyState !== 1) continue;
    try{ c.ws.send(JSON.stringify(msg)); sent++; }catch(_){ }
  }
  return sent;
}

const dOf = ev => ((ev.tags||[]).find(t => t[0] === 'd') || [])[1] || '';

/* Newest wins per item. This is also the only defence against a relay replaying an old copy: an
 * older created_at can never overwrite a newer one that is already held. */
async function absorb(ev){
  if(!key || !ev || ev.pubkey !== cfg.pubkey) return;   // not ours — a relay may send anything
  const d = dOf(ev);
  if(!d || d === D_KEY || d.startsWith(D_FOLDER)) return;
  if(!d.startsWith(D_ITEM)) return;
  const id = d.slice(D_ITEM.length);
  if(!id) return;
  const cur = items.get(id);
  if(cur && (cur._at || 0) > (ev.created_at || 0)) return;
  if(!ev.content){ items.delete(id); saveItemsSoon(); return; }   // tombstone
  let obj;
  try{ obj = await V.open(key, ev.content); }
  catch(_){ return; }                                  // sealed with another key — not ours to show
  obj.id = id; obj._at = ev.created_at || 0;
  items.set(id, obj);
  saveItemsSoon();
}

let _saveT = null;
function saveItemsSoon(){ clearTimeout(_saveT); _saveT = setTimeout(saveItems, 400); }

// ---------------------------------------------------------------- writing

/* A new login saved from the browser. In full mode it is signed and published here. In read-only
 * mode it goes to a local OUTBOX and stays there, visibly, until the app publishes it — which is
 * exactly what "read-only" was chosen to mean, and is said in the UI rather than failing silently. */
async function saveItem(item){
  item.id = item.id || randomId();
  item.updated = Math.floor(Date.now()/1000);
  if(!item.created) item.created = item.updated;
  item.kind = item.kind || 'login';
  items.set(item.id, item);
  await saveItems();
  if(cfg.mode === 'full' && cfg.sk){
    try{
      const ct = await V.seal(key, stripLocal(item));
      const ev = finalize({ kind: KIND, created_at: item.updated, content: ct,
                            tags: [['d', D_ITEM + item.id], ['l', L_TAG]] });
      /* Wait for the relay's OK before calling it published. send() is a silent no-op unless the
       * socket is open — and right after an event-page wake it never is — so reporting success
       * here left the only copy of a credential in this browser profile, un-queued and unsent,
       * where unpair() would later delete it. Anything short of an accepted event goes to the
       * outbox instead. */
      if(await publishAndWait(ev)) return { ok:true, published:true };
    }catch(e){ /* fall through to the outbox — never lose the entry */ }
  }
  const got = await B.storage.local.get('outbox');
  const outbox = got.outbox || [];
  const left = outbox.filter(o => o.id !== item.id);
  left.push(item);
  await B.storage.local.set({ outbox: left });
  return { ok:true, published:false, queued:true };
}

/* Publish and wait for ["OK", <id>, true]. Resolves false on a rejection, a closed socket or a
 * timeout — every one of which means "not stored", and all of which used to read as success. */
function publishAndWait(ev, ms){
  return new Promise((resolve) => {
    if(!_anyOpen()) return resolve(false);
    let done = false;
    const finish = (v) => { if(done) return; done = true; okWaiters.delete(ev.id); resolve(v); };
    okWaiters.set(ev.id, (accepted) => finish(!!accepted));
    setTimeout(() => finish(false), ms || 8000);
    if(!send(['EVENT', ev])) finish(false);
  });
}

function stripLocal(it){ const o = Object.assign({}, it); delete o._at; delete o._match; return o; }

function finalize(tmpl){
  const T = NT();
  const sk = /^[0-9a-f]{64}$/i.test(cfg.sk) ? V.fromHex(cfg.sk) : T.nip19.decode(cfg.sk).data;
  return T.finalizeEvent(tmpl, sk);
}

function randomId(){
  const b = crypto.getRandomValues(new Uint8Array(16));
  let s = ''; for(const x of b) s += x.toString(16).padStart(2,'0');
  return s;
}

/* Read-only mode's queue drains when the app publishes the item — the extension sees the event come
 * back down the subscription and drops its copy. Nothing here republishes; that is the whole point. */
async function flushOutbox(){
  const got = await B.storage.local.get('outbox');
  const outbox = got.outbox || [];
  if(!outbox.length) return;
  const left = outbox.filter(o => {
    const cur = items.get(o.id);
    return !(cur && (cur._at || 0) >= (o.updated || 0));
  });
  if(left.length !== outbox.length) await B.storage.local.set({ outbox: left });
}

// ---------------------------------------------------------------- pairing

async function pair(code){
  let payload;
  try{ payload = JSON.parse(new TextDecoder().decode(V.fromB64(code.trim()))); }
  catch(_){ throw new Error('that pairing code is not readable — copy it again from the app'); }
  if(!payload || payload.t !== 'pcvault' || !payload.pubkey || !payload.key)
    throw new Error('that is not a PosterChan pairing code');
  if(payload.mode === 'full' && !payload.sk)
    throw new Error('that code says "full" but carries no signing key — pair again');
  if(!payload.relay && !(payload.relays || []).length)
    throw new Error('that pairing code carries no relay address, so this browser could never sync. ' +
                    'Check the app has a relay configured and pair again.');
  cfg = { pubkey: payload.pubkey, key: payload.key, relay: payload.relay || '',
          relays: Array.isArray(payload.relays) ? payload.relays.filter(Boolean) : [],
          mode: payload.mode === 'full' ? 'full' : 'ro', sk: payload.sk || '' };
  key = V.fromB64(cfg.key);
  items = new Map();
  await B.storage.local.set({ cfg, items: [] });
  connect();
  return { ok:true, mode: cfg.mode };
}

async function unpair(){
  cfg = null; key = null; items = new Map();
  for(const [u, c] of conns){ closeConn(c); conns.delete(u); }
  ws = null;
  await B.storage.local.clear();
  status = 'not paired';
  return { ok:true };
}

// ---------------------------------------------------------------- messages

B.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    try{
      // On a Firefox event page the message that WOKE us arrives while loadCfg() is still pending,
      // so answering now would report "not paired" on a paired install and hand back an empty list
      // for every fill. Wait for the one load, always.
      await ready;
      switch(msg && msg.type){
        case 'state':
          return reply({ paired: !!cfg, mode: cfg && cfg.mode, count: items.size, status, lastSync });
        /* EVERY login, for searching. The popup used to hold only the matches for the current tab
         * and filter THOSE, so an entry the site did not match could not be found at all — typing
         * its name searched an empty list. Passwords are not included; the popup asks for one by id
         * when the user presses a button. */
        case 'all': {
          const list = Array.from(items.values()).filter(i => i.kind === 'login')
            .sort((a, b) => (a.title || '').localeCompare(b.title || ''));
          return reply({ items: list.map(i => ({ id:i.id, title:i.title, username:i.username,
                                                 host: (V.hostOf(V.itemUris(i)[0] || '') || ''),
                                                 hasTotp: !!i.totp })) });
        }
        case 'matches': {
          const list = V.matchesFor(Array.from(items.values()).filter(i => i.kind === 'login'), msg.url);
          // The password goes to the POPUP (the user asked for it) but never to a content script
          // unprompted — see 'fill', which is the only path that hands one to a page.
          return reply({ items: list.map(i => ({ id:i.id, title:i.title, username:i.username,
                                                 _match:i._match, hasTotp: !!i.totp })) });
        }
        case 'fill': {
          const it = items.get(msg.id);
          if(!it) return reply({ ok:false });
          /* THE FRAME HAS TO EARN IT. A content script shares a world with its page, and this
           * message can come from ANY frame in the tab — including a third-party ad or widget
           * iframe, where `sender.url` is that third party. Handing the password over on the id
           * alone put it into a cross-origin frame's DOM, where the embedder's own JS reads it
           * straight off the input. The credential is released only to a frame the item actually
           * matches, by the same rule the app uses. */
          const from = (sender && sender.url) || '';
          if(!V.matchLevel(it, from)) return reply({ ok:false, error:'this frame is not that site' });
          const totp = it.totp ? await code(it.totp) : '';
          return reply({ ok:true, username: it.username || '', password: it.password || '', totp });
        }
        case 'reveal': {
          /* The POPUP only. `sender.tab` is set for a content script and undefined for an
           * extension page, and this is the one message that hands back a password with no site
           * check at all — it exists so the popup can copy and display. A content script that
           * wanted it could ask for the whole vault one id at a time. */
          if(sender && sender.tab) return reply({ ok:false, error:'not available to a page' });
          const it = items.get(msg.id);
          if(!it) return reply({ ok:false });
          return reply({ ok:true, username: it.username||'', password: it.password||'',
                         totp: it.totp ? await code(it.totp) : '',
                         left: it.totp ? V.totpRemaining((V.totpConfig(it.totp)||{}).period) : 0 });
        }
        /* "Do I already have this?" — asked by the save bar, which HAS the typed credential and
         * must never be sent one back. The comparison happens here; the reply is a verdict, not a
         * password. */
        case 'known': {
          const here = V.matchesFor(Array.from(items.values()).filter(i => i.kind === 'login'), msg.url)
            .filter(i => (i.username || '') === (msg.username || ''));
          const exact = here.find(i => (i.password || '') === (msg.password || ''));
          return reply({ known: !!exact, rotating: !exact && here.length > 0,
                         id: here.length ? here[0].id : '' });
        }
        case 'save': return reply(await saveItem(msg.item));
        case 'pair': return reply(await pair(msg.code));
        case 'unpair': return reply(await unpair());
        case 'sync': connect(); return reply({ ok:true });
        default: return reply({ ok:false });
      }
    }catch(e){ reply({ ok:false, error: (e && e.message) || 'error' }); }
  })();
  return true;      // async reply
});

async function code(raw){
  const cfgT = V.totpConfig(raw);
  if(!cfgT) return '';
  try{ return await V.totp(cfgT.secret, cfgT); }catch(_){ return ''; }
}

// ---------------------------------------------------------------- boot

const ready = loadCfg().then(() => { if(cfg) connect(); });
// A phone suspends the whole extension; re-check the socket whenever anything talks to us.
setInterval(() => { if(cfg && !_anyOpen()) connect(); }, 30000);

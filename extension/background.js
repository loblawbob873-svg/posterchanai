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
/* Bookmarks ride the SAME subscription and the same key — one encrypted event per bookmark, exactly
 * like a vault item (see bookmarks.js). Adding the label here rather than opening a second socket
 * keeps "which relays am I talking to" a single answer. */
/* Tolerant of bookmarks.js being absent: both manifests load it before this file, but a harness (or
 * a future load order) that does not must still get a working vault rather than a background script
 * that throws before its first line of logic. The d-tag prefix and label are duplicated as fallbacks
 * for that case ONLY — bookmarks.js is the definition. */
const BM = (typeof PCBookmarks !== 'undefined') ? PCBookmarks
         : ((typeof self !== 'undefined' && self.PCBookmarks) || null);
const D_BM = (BM && BM.D_BM) || 'pcai:bm:';
const L_BM = (BM && BM.L_BM) || 'pcai-bm';

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
  const got = await B.storage.local.get(['cfg', 'items', 'outbox', 'relays']);
  userRelays = Array.isArray(got.relays) ? got.relays : [];
  cfg = got.cfg || null;
  if(cfg && cfg.key) key = V.fromB64(cfg.key);
  initBookmarks();                  // safe to call twice; the engine only wires its listeners once
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

/* Which relays this browser talks to.
 *
 * The user's own list wins when they have set one, then whatever the pairing code carried, and
 * finally a hardcoded default — because "the pairing code carried no usable relay" is otherwise a
 * dead end only a re-pair can fix, and it is invisible: the extension simply never syncs. That is
 * exactly what "I clicked sync and nothing happened" looks like from here.
 *
 * DEFAULT_RELAY is this project's own relay, the same one the app and the clients fall back to. */
const DEFAULT_RELAY = 'wss://relay.poster.place';
let userRelays = [];        // set in the popup; empty = use the pairing code's

/* NORMALISE, THEN dedupe — the paired list came from the app as raw strings and was deduped by exact
 * string, so `wss://relay.poster.place` and `wss://relay.poster.place/` (or `cfg.relay` plus the same
 * host in `cfg.relays`) counted as TWO relays and opened TWO sockets to the SAME relay: double the
 * traffic, every event and every EOSE delivered twice, and "2 relays" shown in the UI for what is one.
 * normRelay collapses scheme/trailing-slash differences, so the same relay becomes one connection. */
function _uniqRelays(list){
  const out = [];
  for(const u of list){ const n = normRelay(u); if(n && out.indexOf(n) < 0) out.push(n); }
  return out.slice(0, 6);
}
function relayUrls(){
  // The user's explicit choice wins, deduped (they may deliberately want several).
  const mine = _uniqRelays(userRelays);
  if(mine.length) return mine;
  /* DEFAULT IS EXACTLY ONE RELAY — this project's own. The pairing code carries the app's whole relay
   * list, and that list routinely holds SEVERAL URLs for the SAME server (e.g. `relay.poster.place`
   * and `poster.place`, or a node's own address) — different hostnames normRelay cannot know are one
   * relay. Syncing to "two relays" that are really one doubled all traffic and, when they were genuinely
   * two, made a delete that reached one relay reappear from the other. One relay by default, always;
   * anyone who truly wants more sets them in Relays. */
  return [DEFAULT_RELAY];
}

/* Normalised, so a typo does not open a socket to nothing: a bare host becomes wss://, http(s)
 * becomes ws(s), and anything without a plausible host is dropped rather than kept as decoration. */
function normRelay(u){
  u = String(u || '').trim();
  if(!u) return '';
  if(/^https?:\/\//i.test(u)) u = u.replace(/^http/i, 'ws');
  if(!/^wss?:\/\//i.test(u)) u = 'wss://' + u.replace(/^\/+/, '');
  try{ const x = new URL(u); if(!x.hostname || x.hostname.indexOf('.') < 0 &&
       x.hostname !== 'localhost') return ''; return x.href.replace(/\/$/, ''); }
  catch(_){ return ''; }
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
      ['REQ', 'pcvault', { kinds:[KIND], authors:[cfg.pubkey], '#l':[L_TAG, L_BM], limit: 5000 }])); }catch(_){ }
    refreshStatus();
  };
  c.ws.onmessage = (e) => {
    let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
    if(m[0] === 'EVENT' && m[2]) absorb(m[2]);
    else if(m[0] === 'OK'){ const w = okWaiters.get(m[1]); if(w) w(m[2] === true); }
    else if(m[0] === 'EOSE'){ c.ready = true; lastSync = Date.now(); saveItems(); flushOutbox(); refreshStatus();
      /* A relay has now answered, so anything that could not be published while the socket was down
       * can go. union() only sends what it has no record of having sent, and never deletes, so
       * running it here is a retry rather than a re-sync. */
      if(BM && BM.engine && BM.engine.enabled() && Date.now() - lastUnionAt > 60000){
        lastUnionAt = Date.now();
        BM.engine.union().catch(()=>{});
      } }
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

/* A merge on EOSE is a RETRY, so it is rate-limited. EOSE arrives once per relay and again on every
 * reconnect, and each merge reads the whole bookmark tree — on a flapping connection that was a
 * full-tree read every few seconds, forever, on the browser's UI thread. Anything genuinely
 * unpublished is still picked up by the next one, or by the popup's Merge button, which is not
 * rate-limited because the user asked for it. */
let lastUnionAt = 0;

/* Newest wins per item. This is also the only defence against a relay replaying an old copy: an
 * older created_at can never overwrite a newer one that is already held. */
async function absorb(ev){
  if(!key || !ev || ev.pubkey !== cfg.pubkey) return;   // not ours — a relay may send anything
  /* VERIFY THE SIGNATURE. Without it a relay can hand us an unsigned event carrying the user's
   * pubkey, and two things follow: an empty-content one DELETES an entry from the extension's only
   * local copy (the tombstone branch below, then written to storage), and an old ciphertext
   * re-stamped with a fresh created_at permanently outranks the genuine current event. That turns
   * "a relay can show you something stale" into "a relay chooses which of your old passwords you
   * see", and empties the vault on demand. nostr-tools is already bundled for signing. */
  try{ if(!NT().verifyEvent(ev)) return; }catch(_){ return; }
  const d = dOf(ev);
  if(!d || d === D_KEY || d.startsWith(D_FOLDER)) return;
  if(d.startsWith(D_BM)) return absorbBookmark(d.slice(D_BM.length), ev);
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

/* ---------------------------------------------------------------- bookmarks
 *
 * The engine lives in bookmarks.js and is handed what it needs rather than reaching for it: the
 * browser API, the decryptor, and ONE publish function so that signing, the OK-wait and the
 * read-only rule stay here, where the vault already implements them.
 *
 * READ-ONLY PAIRING SYNCS ONE WAY, and says so instead of failing quietly. A read-only device holds
 * no signing key, so it can receive bookmarks from the relay and apply them, and cannot publish its
 * own — the same line the vault draws, for the same reason. Queuing them in the vault's outbox would
 * be wrong: that queue is drained by the APP publishing vault items, and it has never heard of a
 * bookmark.
 */
async function absorbBookmark(id, ev){
  if(!(BM && BM.engine)) return;
  try{ await BM.engine.absorb(id, ev); }catch(_){ }
}

/* Wait for a live socket, briefly. publishAndWait resolves FALSE the instant nothing is open, and
 * nothing IS open in the moment a service worker (or a just-woken event page) starts handling the
 * popup's message — the relay connection is still being made. So "enable sync" ran its whole merge
 * against a closed socket, published nothing, and said so. It was not the pairing mode, which is what
 * I first blamed it on; a FULL pairing had exactly the same problem. */
function waitOpen(ms){
  return new Promise((resolve) => {
    if(_anyOpen()) return resolve(true);
    connect();                                   // idempotent; brings the sockets up if they are down
    const t0 = Date.now();
    const tick = setInterval(() => {
      if(_anyOpen()){ clearInterval(tick); return resolve(true); }
      if(Date.now() - t0 > (ms || 8000)){ clearInterval(tick); return resolve(false); }
    }, 150);
  });
}

async function publishBookmark(syncId, item){
  if(!(cfg && cfg.mode === 'full' && cfg.sk)) return false;      // read-only: receive only
  if(!await waitOpen()) return false;                            // nothing open, and none arrived
  const at = Math.floor(Date.now()/1000);
  try{
    // A tombstone is an EMPTY content, never an absent event: "I don't have it" and "it was deleted"
    // are different facts, and only the second may remove a bookmark on another device.
    const content = item ? await V.seal(key, item) : '';
    const ev = finalize({ kind: KIND, created_at: at, content,
                          tags: [['d', D_BM + syncId], ['l', L_BM]] });
    return await publishAndWait(ev);
  }catch(_){ return false; }
}

/* Initialised WITHOUT waiting for a vault key. init() only reads extension storage and wires the
 * browser's bookmark listeners; the key is needed to open or seal a body, and both closures below
 * read `key` when they are CALLED, so pairing later works with no re-init.
 *
 * Gating this on `key` meant an unpaired browser left `api` null inside the engine, and the first
 * thing the popup's toggle does is touch it: "cannot access property B, api is null" — an exception
 * from a switch, rather than the extension saying it is not paired. */
async function initBookmarks(){
  if(!BM || !BM.engine) return;
  try{
    await BM.engine.init({
      B: B,
      open: (ct) => V.open(key, ct),
      publish: publishBookmark,
      isFull: () => !!(cfg && cfg.mode === 'full' && cfg.sk),
      /* WHY a merge sent nothing, in words, for the popup to show. "Nothing happened" sent me
       * guessing at the pairing mode and asserting it as fact when the real cause was a socket that
       * had not opened yet; the software knows which it is and should say so. */
      why: () => !cfg ? 'not paired'
                : !B.bookmarks ? 'the browser has not granted the bookmarks permission to this extension'
                : !(cfg.mode === 'full' && cfg.sk) ? 'this pairing is read-only — it has no signing key'
                : !_anyOpen() ? 'no relay connection'
                : '',
    });
  }catch(_){ }
}

let _saveT = null;
function saveItemsSoon(){ clearTimeout(_saveT); _saveT = setTimeout(saveItems, 400); }

// ---------------------------------------------------------------- writing

/* A new login saved from the browser. In full mode it is signed and published here. In read-only
 * mode it goes to a local OUTBOX and stays there, visibly, until the app publishes it — which is
 * exactly what "read-only" was chosen to mean, and is said in the UI rather than failing silently. */
async function saveItem(item, full){
  /* MERGE onto what is already there. The save bar can only know a username, a password and the
   * page it is on — so replacing the stored entry with that wiped the TOTP secret, the notes, the
   * folder, the tags and every other URI. On an "Update" after rotating a password that is a
   * one-way loss of the 2FA secret, published to every device in full mode. */
  const prev = item.id ? items.get(item.id) : null;
  if(prev){
    const merged = Object.assign({}, prev, item);
    /* `full` = this came from the EDIT form, which shows every field it is changing, so an emptied
     * box means CLEAR. The backfill below exists for the SAVE BAR, which knows only a username and a
     * password: without it, updating a rotated password wiped the TOTP secret, the notes and the
     * folder. Applying that rule to an edit makes clearing a field impossible — you delete the
     * notes, save, and they come back.
     *
     * BRACED. Without them the `else` binds to the INNER if, inside the loop — the dangling-else —
     * so "full" did nothing at all and a partial save reassigned created/src once per key. It
     * happened to be harmless and it was not what it said. */
    if(full){
      // The website list is authoritative too, or a typo in a URL can never be corrected: the union
      // below would keep the wrong one forever alongside the fix.
      merged.uris = (item.uris || []).slice();
      merged.created = prev.created;               // provenance is not editable
      merged.src = prev.src;
    } else {
      merged.uris = [...new Set([...(prev.uris || []), ...(item.uris || [])])];
      for(const k of ['totp','notes','folder','tags','fields','created','src'])
        if(prev[k] !== undefined && (item[k] === undefined || item[k] === '' ||
           (Array.isArray(item[k]) && !item[k].length))) merged[k] = prev[k];
    }
    item = merged;
  }
  item.id = item.id || randomId();
  item.updated = Math.floor(Date.now()/1000);
  if(!item.created) item.created = item.updated;
  item.kind = item.kind || 'login';
  // Stamp it, or `cur._at || 0` makes ANY incoming event newer and the next sync silently reverts
  // the password that was just saved — while the new one sits invisibly in the outbox.
  item._at = item.updated;
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

/* ================================================================ share this page
 *
 * A full pairing already holds the signing key — the same key the NIP-07 signer lends to websites —
 * so the browser can post the page you are looking at as an ordinary kind-1 note, with no site, no
 * app and no copy-pasting a URL into a client.
 *
 * THE APPROVAL IS THE BUTTON. Everything in the signer section asks per origin and per kind, because
 * there a WEBSITE is asking. Here the request comes from the extension's own popup, which a page
 * cannot open and cannot message — so the guard that matters is `_fromPopup` on the message, not a
 * prompt. Without it any page could post as the user, silently, which is strictly worse than
 * anything the signer can do.
 *
 * READ-ONLY PAIRING CANNOT POST, and says so. It holds no signing key at all; queuing the note in the
 * vault's outbox would be wrong for the same reason a bookmark is not queued there — that queue is
 * drained by the app publishing VAULT items, and it has never heard of a note.
 */

/* Where a note goes, which is NOT where the vault syncs.
 *
 * relayUrls() deliberately narrows to ONE relay by default: the vault is a private document, the
 * pairing code routinely carries several URLs for the same server, and syncing a replaceable document
 * to two of them made a delete reappear. A public note is the opposite case in every respect — it is
 * append-only, it cannot be resurrected, and a note that reached one relay reached nobody but its
 * author. So a post goes to every relay the pairing knows about as well, deduped, capped, and the
 * count is reported honestly rather than assumed.
 */
function postRelayUrls(){
  /* AN EXPLICIT LIST IS EXPLICIT FOR POSTING TOO. relayUrls() promises "the user's choice wins", and
   * widening past it was a privacy bug, not a nicety: somebody who narrows to one relay in the Relays
   * pane is usually removing a relay they do NOT want carrying their public identity, and this fanned
   * every note out to it anyway — while that same pane went on reporting the narrow set as "in use".
   * Widening is only for the DEFAULT, where the single relay was chosen for the vault's sake. */
  const mine = _uniqRelays(userRelays);
  if(mine.length) return mine;
  const out = relayUrls().slice();
  for(const u of _uniqRelays([...((cfg && cfg.relays) || []), (cfg && cfg.relay) || '']))
    if(!out.includes(u)) out.push(u);
  return out.slice(0, 8);
}

/* One socket per relay, opened for this post and closed after it.
 *
 * The vault's publishAndWait resolves on the FIRST OK from any relay, which is right for "is my
 * password stored" and useless here: "posted" and "posted to one of five relays, four of which
 * refused you" are different facts, and only a per-relay accounting can tell them apart. A private
 * socket also cannot disturb the vault's subscription — an OK for this event will not be swallowed by
 * okWaiters, and a relay that closes on us takes nothing else down with it. A share is one click, not
 * a loop, so the cost of the extra sockets is paid once and knowingly. */
function _publishTo(url, ev, ms){
  return new Promise((resolve) => {
    let sock = null, done = false, timer = null;
    // `said` = this came from an OK frame, i.e. the RELAY's verdict, as opposed to a socket that
    // never got that far. broadcast() needs to tell those apart to report the useful one.
    const finish = (ok, why, said) => {
      if(done) return;
      done = true;
      clearTimeout(timer);
      try{ if(sock){ sock.onopen = sock.onmessage = sock.onerror = sock.onclose = null; sock.close(); } }catch(_){ }
      resolve({ url, ok: !!ok, why: why || '', said: !!said });
    };
    timer = setTimeout(() => finish(false, 'timed out'), ms || 8000);
    try{ sock = new WebSocket(url); }catch(_){ return finish(false, 'could not connect'); }
    sock.onopen = () => { try{ sock.send(JSON.stringify(['EVENT', ev])); }
                          catch(_){ finish(false, 'could not send'); } };
    sock.onmessage = (e) => {
      let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
      /* The relay's OWN words on a refusal. "blocked: not in the web of trust" or "rate-limited" is
       * the entire answer to "why did nothing happen", and replacing it with a generic message throws
       * away the one thing the user could act on. */
      if(m[0] === 'OK' && m[1] === ev.id) finish(m[2] === true, String(m[3] || ''), true);
    };
    sock.onerror = () => finish(false, 'could not connect');
    sock.onclose = () => finish(false, 'the relay closed the connection');
  });
}

async function broadcast(ev, only){
  // `only` = an explicit relay set. A PRIVATE note takes this; postRelayUrls' widening is reasoning
  // about public reach and does not transfer (see saveNote).
  const urls = (only && only.length) ? only : postRelayUrls();
  const res = await Promise.all(urls.map(u => _publishTo(u, ev)));
  const okd = res.filter(r => r.ok);
  /* A RELAY'S OWN REFUSAL OUTRANKS A DEAD SOCKET, whatever order the relays are in. Taking the first
   * failure meant one unreachable relay at the top of the list reported "could not connect" while the
   * other two were saying "blocked: not in the web of trust" — so the user retries a network problem
   * they do not have and never learns the actual reason. */
  /* `why` belongs to a relay that ACTUALLY REFUSED, or it is not reported at all. The fallback to any
   * failure's message meant a refusal with an empty reason string let a different relay's "timed out"
   * be printed as "1 refused it: timed out" — a transport message attributed to the relay that
   * refused, which is the thing splitting the counts was supposed to end. */
  const refusals = res.filter(r => !r.ok && r.said);
  const bad = refusals.find(r => r.why) || (refusals.length ? null : res.find(r => !r.ok && r.why));
  /* REFUSED and UNREACHABLE are counted apart, because they are different facts and the UI was
   * asserting one relay's reason about both: "2 refused it: blocked: not in the web of trust" when
   * one relay said that and the other simply never answered. A relay that did not reply did not give
   * a reason, and saying it did is inventing evidence. */
  return { accepted: okd.length, tried: urls.length, why: bad ? bad.why : '',
           failed: res.length - okd.length,
           refused: res.filter(r => !r.ok && r.said).length,
           unreachable: res.filter(r => !r.ok && !r.said).length,
           // The relays that TOOK it, for the nevent — pointing a reader at one that refused it is
           // pointing them at nothing.
           urls: (okd.length ? okd : res).map(r => r.url) };
}

// A page address, or nothing. `about:`, `file:` and `moz-extension:` are not things to tag a public
// note with — the first two say nothing to anyone else and the third leaks the per-install UUID.
function _pageUrl(u){
  try{ const x = new URL(String(u || '')); return /^https?:$/.test(x.protocol) ? x.href : ''; }
  catch(_){ return ''; }
}

/* Where a URL ends in a sentence.
 *
 * A full stop after a link is punctuation; a BRACKET may not be. Stripping `)` unconditionally
 * turned https://en.wikipedia.org/wiki/Mercury_(planet) into `…Mercury_(planet` and tagged THAT —
 * a permanent public note whose `r` tag 404s, which is worse than the leak the tag rule was added to
 * fix. So a closing bracket only comes off when the URL does not open it. */
function _trimUrl(u){
  const PAIR = { ')': '(', ']': '[', '}': '{' };
  for(;;){
    const last = u.slice(-1);
    if('.,;:!?'.indexOf(last) >= 0 && last){ u = u.slice(0, -1); continue; }
    const open = PAIR[last];
    if(open){
      const opens = u.split(open).length - 1, closes = u.split(last).length - 1;
      if(closes > opens){ u = u.slice(0, -1); continue; }     // unbalanced: it closed the sentence
    }
    return u;
  }
}

/* Every web address IN the note, normalised, in order.
 *
 * The excluded characters include the TYPOGRAPHIC quotes, not just the straight ones: draftFor wraps
 * a quoted selection in “…”, so a selection ending in a link produced a tag for
 * `https://…/track%E2%80%9D`, which resolves to nothing. The character the code itself inserts is the
 * one most likely to be there. */
function _urlsIn(text){
  const out = [];
  for(const m of String(text || '').match(/https?:\/\/[^\s<>"'`“”‘’]+/gi) || []){
    const u = _pageUrl(_trimUrl(m));
    if(u && !out.includes(u)) out.push(u);
  }
  return out.slice(0, 5);
}

/* What makes it a SHARE rather than a wall of text.
 *
 * `r` is the page itself, which is how a client knows to show a preview and how the note is findable
 * by URL at all. `t` is one per hashtag actually typed — a hashtag that exists only inside the content
 * is invisible to every hashtag feed there is, which reads as "my post didn't show up".
 *
 * THE `r` TAG COMES OUT OF THE NOTE, NOT OFF THE TAB. It used to be the popup's live tab URL, which
 * is not what the user approved: delete the address from the draft — because it is an internal wiki,
 * or because it carries a one-time login token — and the published event still carried it in a tag,
 * which clients render as a link. The note is the whole of what the user agreed to publish, so the
 * tag can only name a URL the note itself contains. `url` is a HINT for which one is the subject when
 * there are several; if it isn't in the text, it isn't in the tags. */
function _shareTags(content, url){
  const tags = [];
  const inText = _urlsIn(content);
  const hint = _pageUrl(url);
  const u = (hint && inText.includes(hint)) ? hint : (inText[0] || '');
  if(u) tags.push(['r', u]);
  const seen = new Set();
  const re = /(?:^|\s)#([\p{L}\p{N}_-]{1,64})/gu;
  let m;
  while((m = re.exec(String(content || '')))){
    const t = m[1].toLowerCase();
    if(seen.has(t) || seen.size >= 20) continue;
    seen.add(t);
    tags.push(['t', t]);
  }
  return tags;
}

/* WHAT HAS ALREADY BEEN PUBLISHED, remembered HERE.
 *
 * The popup cannot hold this. It is destroyed the instant it loses focus — which is a thing that
 * routinely happens during the up-to-8s publish itself — so a record written popup-side after the
 * await is a record that never gets written for exactly the publish most likely to be repeated. Nor
 * can the popup compare against a REBUILT draft: clear the selection, or edit before posting (which
 * is the normal case), and the rebuild differs from what went out and the guard silently misses.
 *
 * So the guard is a content hash, checked in the one place that sees every publish. The popup is free
 * to be wrong about its own state; it cannot cause a duplicate. Editing the text posts again, which is
 * the escape hatch, and 24 hours is long enough to cover "did that go through?" without turning a
 * genuine repost next week into a mystery refusal. */
const POSTED_KEY = 'sharePosted';
const POSTED_KEEP = 50;                 // newest-last; slice(-50) IS the recency eviction
const POSTED_WINDOW = 24 * 3600;

async function _postedLog(){
  const got = await B.storage.local.get(POSTED_KEY);
  return Array.isArray(got[POSTED_KEY]) ? got[POSTED_KEY] : [];
}

async function _rememberPost(rec){
  const log = (await _postedLog()).filter(r => r && r.h !== rec.h);
  log.push(rec);
  await B.storage.local.set({ [POSTED_KEY]: log.slice(-POSTED_KEEP) });
}

async function _contentHash(s){
  const bytes = new TextEncoder().encode(String(s || ''));
  const d = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(d)).map(x => x.toString(16).padStart(2, '0')).join('');
}

async function sharePost(msg){
  if(!cfg || !key) return { ok:false, error:'PosterChan Passwords is not paired' };
  if(!(cfg.mode === 'full' && cfg.sk))
    return { ok:false, error:'this browser is paired READ-ONLY, so it has no signing key and cannot ' +
                            'post. Re-pair with full access from PosterChan → Passwords → Pair a device.' };
  const content = String((msg && msg.text) || '').replace(/\s+$/, '').slice(0, 8000);
  if(!content.trim()) return { ok:false, error:'there is nothing to post' };
  const now = Math.floor(Date.now()/1000);
  const h = await _contentHash(content);
  const dup = (await _postedLog()).find(r => r && r.h === h && (now - (r.at || 0)) < POSTED_WINDOW);
  if(dup)
    return { ok:false, duplicate:true, nevent: dup.nevent || '', at: dup.at || 0,
             error:'you have already posted this exact text — edit it to post it again' };
  let ev;
  try{ ev = finalize({ kind: 1, created_at: Math.floor(Date.now()/1000), content,
                       tags: _shareTags(content, msg && msg.url) }); }
  catch(e){ return { ok:false, error:'could not sign it: ' + ((e && e.message) || 'bad key') }; }
  const r = await broadcast(ev);
  // Nothing stored anywhere is a FAILURE, said in the relay's words. Reporting "posted" here is how a
  // note that exists nowhere gets mistaken for one that does.
  if(!r.accepted)
    return { ok:false, tried: r.tried,
             error: (r.why || 'no relay accepted it') + ` (tried ${r.tried})` };
  let nevent = '';
  try{ nevent = NT().nip19.neventEncode({ id: ev.id, author: cfg.pubkey, relays: r.urls.slice(0, 3) }); }
  catch(_){ }
  /* Recorded HERE, after the publish that actually happened, so the popup dying mid-send cannot lose
   * it. Awaited so the next press cannot race a half-written record — but NEVER allowed to throw: a
   * storage failure (quota, a private-window profile) would otherwise propagate out of a successful
   * publish and be reported as "Not posted", on a note that IS posted, with the guard unarmed. The
   * user then presses again and gets the permanent duplicate this whole mechanism exists to prevent.
   * A lost record costs at most one un-guarded repeat; a thrown one guarantees it. */
  let remembered = true;
  try{ await _rememberPost({ h, url: _pageUrl(msg && msg.url), id: ev.id, nevent, at: now }); }
  catch(_){ remembered = false; }
  return { ok:true, accepted: r.accepted, tried: r.tried, id: ev.id, nevent, remembered,
           why: r.why, refused: r.refused, unreachable: r.unreachable };
}

/* ================================================================ save to Notes
 *
 * The private counterpart of a share: the same page, the same selection, kept instead of published.
 *
 * IT IS THE APP'S OWN NOTE FORMAT, not a new one — one kind-30078 event per note, `d =
 * pcai:note:<id>`, tagged `l=pcai-notes` so the library is one indexed subscription, with the JSON
 * body NIP-44 encrypted to the user's OWN key. That last part is the whole point and the reason this
 * cannot reuse the vault's AES key: a note is readable by its author and by nobody else — not this
 * server, not any server, not the AI. A note saved here opens in PosterChan → Notes, and one written
 * there is not something this extension could read either.
 *
 * THE FIRST LINE IS THE TITLE. Notes have a title field and a browser popup should not grow a second
 * text box to fill it; the draft already starts with the page title on its own line.
 *
 * No duplicate guard, unlike a post, and deliberately: a note is editable and deletable by the person
 * who wrote it, so saving twice costs a delete. A kind-1 cannot be recalled at all, which is why that
 * path is guarded and this one is not.
 */
const NOTE_KIND = 30078;
const D_NOTE = 'pcai:note:';
const L_NOTE = 'pcai-notes';

/* SAVE THE WHOLE PAGE: a picture of it, in a real note.
 *
 * The picture goes to the encrypted drive (see drive.js — the only HTTP this extension does) and the
 * note references it as `pcres:<sha>`, which is how the app's own Notes attachments work: the note
 * carries the name and mime, so nothing has to be written into the drive INDEX for it to render.
 *
 * Every failure below is REPORTED with what it was. This path has four places it can stop — no
 * instance in the pairing, a page that will not be captured, a drive that will not answer, a relay
 * that will not take the note — and "couldn't save" for all four is the shape of bug that gets
 * reported a week later as "it doesn't work".
 */
/* Build + sign an auth event. `finalize` signs a TEMPLATE; the drive's two calls each want a
 * (kind, content, tags) triple, and there is no `signEvent` function in this file — that name is a
 * NIP-07 METHOD in the permissions Set, which is exactly the kind of thing that looks callable and
 * is not. */
function _signAuth(kind, content, tags){
  return finalize({ kind, content: String(content || ''), created_at: Math.floor(Date.now()/1000),
                    tags: _cleanTags(tags) });
}

let _shotBusy = false;

async function savePage(msg, tabId){
  /* ONE SWEEP AT A TIME. The restore state lives on the PAGE's own `__pcShot*` globals, so a second
   * sweep overwrites the first one's record of which elements it hid — and whichever finishes last
   * restores the wrong set, leaving that page's header invisible until it is reloaded. The popup
   * disables its button, but the popup is not the guard: it closes the moment you click away, and the
   * message can arrive from a second browser window entirely. */
  if(_shotBusy) return { ok:false, error:'a page is already being photographed — let it finish' };
  _shotBusy = true;
  try{ return await _savePage(msg, tabId); }
  finally{ _shotBusy = false; }
}

async function _savePage(msg, tabId){
  if(!cfg || !key) return { ok:false, error:'PosterChan Passwords is not paired' };
  if(!(cfg.mode === 'full' && cfg.sk))
    return { ok:false, error:'this browser is paired READ-ONLY, so it holds no key to encrypt a note with. ' +
                            'Re-pair with full access from PosterChan → Passwords → Pair a device.' };
  if(!cfg.api)
    return { ok:false, error:'this pairing predates page saving, so it carries no address for your ' +
                            'drive. Pair this browser again from PosterChan → Passwords → Pair a device.' };
  if(!tabId) return { ok:false, error:'no page to save' };

  const step = (m) => { try{ B.runtime.sendMessage({ type:'pc-shot-progress', text:m }); }catch(_){} };
  let shot;
  try{ shot = await self.PCShot.capture(tabId, step); }
  catch(e){ return { ok:false, error:'could not photograph the page: ' + ((e && e.message) || 'unknown') }; }

  const bytes = new Uint8Array(await shot.blob.arrayBuffer());
  let sha;
  try{
    step('encrypting…');
    const mk = await self.PCDrive.masterKey(cfg, _skBytes(), _signAuth);
    const sealed = await self.PCDrive.seal(mk, bytes);
    step('uploading…');
    ({ sha } = await self.PCDrive.upload(cfg, sealed, _signAuth));
  }catch(e){ return { ok:false, error:(e && e.message) || 'the drive refused it' }; }

  const at = Math.floor(Date.now()/1000);
  const host = (() => { try{ return new URL(shot.meta.url).host; }catch(_){ return ''; } })();
  const name = (host || 'page') + '-' + at + '.png';
  const title = (shot.meta.title || host || 'Saved page').slice(0, 200);
  const body = [
    shot.meta.url,
    '',
    '![' + name + '](pcres:' + sha + ')',
    shot.meta.capped ? '\n_(the page was longer than one picture — this is the top of it)_' : '',
  ].join('\n').trim();
  const note = { v:1, id: randomId(), title, body, folder:'', tags:['clipped'],
                 created: at, updated: at,
                 res: [{ sha, name, mime:'image/png', size: bytes.length }] };
  let ev;
  try{
    const T = NT();
    const ck = T.nip44.v2.utils.getConversationKey(_skBytes(), cfg.pubkey);
    ev = finalize({ kind: NOTE_KIND, created_at: at, content: T.nip44.v2.encrypt(JSON.stringify(note), ck),
                    tags: [['d', D_NOTE + note.id], ['l', L_NOTE]] });
  }catch(e){ return { ok:false, error:'could not encrypt the note: ' + ((e && e.message) || 'bad key') }; }
  const r = await broadcast(ev);
  if(!r.accepted)
    return { ok:false, tried:r.tried,
             error:'the picture is saved in your drive but no relay took the note (tried ' + r.tried + ')' };
  return { ok:true, accepted:r.accepted, tried:r.tried, id:note.id, title:note.title,
           capped: !!shot.meta.capped };
}

async function saveNote(msg){
  if(!cfg || !key) return { ok:false, error:'PosterChan Passwords is not paired' };
  if(!(cfg.mode === 'full' && cfg.sk))
    return { ok:false, error:'this browser is paired READ-ONLY, so it holds no key to encrypt a note ' +
                            'with. Re-pair with full access from PosterChan → Passwords → Pair a device.' };
  /* Capped by what NIP-44 WILL ACTUALLY ENCRYPT, measured, not guessed.
   *
   * `nip44.v2.encrypt` throws above 65535 BYTES of plaintext — checked against the bundle that ships
   * here, not inferred from the relay's message limit. A 100000-CHARACTER cap therefore bounded
   * nothing: a long article selection sailed past it and died at encrypt time with "invalid plaintext
   * size", a crypto internal shown to somebody who just wanted to keep a page. The app's answer to a
   * big note is to spill the body to an encrypted Blossom blob, which this extension does not do, so
   * the honest thing is to trim before signing. UTF-8 bytes, because the limit is bytes and a cap
   * counted in characters is 3x wrong on non-Latin text. */
  const NOTE_MAX_BYTES = 60000;          // 65535 minus room for the JSON envelope around the body
  let text = String((msg && msg.text) || '').replace(/\s+$/, '');
  while(new TextEncoder().encode(text).length > NOTE_MAX_BYTES) text = text.slice(0, -1024);
  if(!text.trim()) return { ok:false, error:'there is nothing to save' };

  const lines = text.split('\n');
  const first = (lines[0] || '').trim();
  const title = first.slice(0, 200);
  /* A long opening line is TRUNCATED for the title and kept in full in the body. Slicing it into the
   * title and starting the body at line 2 silently dropped everything past 200 characters — text the
   * user watched go into the box, in neither field of the saved note. */
  let rest = lines.slice(1).join('\n').replace(/^\s+/, '');
  if(first.length > 200) rest = rest ? first + '\n\n' + rest : first;
  const at = Math.floor(Date.now()/1000);
  const note = { v:1, id: randomId(), title, body: rest || text.trim(), folder:'',
                 tags:[], created: at, updated: at, res:[] };
  let ev;
  try{
    const T = NT();
    const ck = T.nip44.v2.utils.getConversationKey(_skBytes(), cfg.pubkey);
    ev = finalize({ kind: NOTE_KIND, created_at: at, content: T.nip44.v2.encrypt(JSON.stringify(note), ck),
                    tags: [['d', D_NOTE + note.id], ['l', L_NOTE]] });
  }catch(e){ return { ok:false, error:'could not encrypt it: ' + ((e && e.message) || 'bad key') }; }
  /* THE SAME RELAYS A POST GOES TO, which includes the ones the PAIRING carries.
   *
   * Narrowing this to relayUrls() looked like the privacy-preserving choice and was a data-loss bug:
   * with no user-set relay list that function returns the single hardcoded DEFAULT_RELAY, not
   * anything derived from the pairing — so a note saved from a browser paired to a self-hosted node
   * went to a relay that node never reads, reported "saved", and could not be found in Notes
   * afterwards. Reaching the instance the user actually reads from is the requirement; the note is
   * ciphertext only its author can open, so the extra copies cost privacy nothing. */
  const r = await broadcast(ev);
  // Same rule as a post: "saved" has to mean a relay said so. A note that reached nowhere is a note
  // that will not be in Notes when they go looking for it.
  if(!r.accepted)
    return { ok:false, tried: r.tried, error: (r.why || 'no relay accepted it') + ` (tried ${r.tried})` };
  return { ok:true, accepted: r.accepted, tried: r.tried, id: note.id, title: note.title,
           why: r.why, refused: r.refused, unreachable: r.unreachable };
}

/* ================================================================ NIP-07 signing
 *
 * The extension already holds the signing key in FULL pairing mode, so it can be the signer a Nostr
 * app asks for — which means logging in without pasting an nsec into a web page, which is the thing
 * everyone is told never to do and does anyway.
 *
 * WHAT A SITE CAN AND CANNOT GET. `getPublicKey` is public information and is granted per origin
 * like everything else. Signing and the encrypt/decrypt calls need the key, so they are refused
 * outright in read-only pairing. Approval is per ORIGIN and, for signing, per EVENT KIND: a site
 * allowed to publish a note (kind 1) has not thereby been allowed to replace your contact list
 * (kind 3) or move money (kind 9734). That distinction is the entire security value of a signer over
 * a pasted key, and the prompt names the kind.
 *
 * The prompt is a REAL EXTENSION WINDOW, not an overlay drawn in the page. A page can draw anything
 * it likes, including a convincing copy of an approval dialog; it cannot draw a browser window that
 * says which extension is asking.
 */
const NOSTR_OK = 'nostrPerms';      // { "<origin>|<method>|<kind>": "allow" | "deny" }
const _asking = new Map();          // id -> { req, resolve }

// The methods that exist. A page-supplied `method` is checked against this BEFORE it is allowed
// anywhere near a permission key: the key is `origin|method|kind`, so an unvetted method string is
// a way to WRITE a key it doesn't own — approving a prompt for the gibberish method
// "signEvent|3" stores exactly the entry that silently authorises real kind-3 signing forever.
const NOSTR_METHODS = new Set(['getPublicKey', 'getRelays', 'signEvent',
                               'nip04.encrypt', 'nip04.decrypt', 'nip44.encrypt', 'nip44.decrypt']);
const _inflight = new Map();        // origin -> count of prompts currently open

function _originOf(sender){
  try{ return new URL((sender && (sender.origin || sender.url)) || '').origin; }catch(_){ return ''; }
}

async function _perms(){
  const got = await B.storage.local.get(NOSTR_OK);
  return got[NOSTR_OK] || {};
}
function _permKey(origin, method, kind){
  // Signing is keyed by kind; everything else is keyed by method alone.
  return origin + '|' + method + (method === 'signEvent' ? '|' + kind : '');
}

/* The kind, decided ONCE, here. It has to be a real non-negative integer and it has to be the same
 * value the prompt names and the signer signs — `"0x3"` reads as an unfamiliar number in the window
 * and then `|0`s to 3 at the signer, which is a contact-list replacement the user never saw. */
function _kindOf(ev){
  const k = ev && ev.kind;
  if(typeof k !== 'number' || !Number.isInteger(k) || k < 0 || k > 65535) return null;
  return k;
}

async function handleNostr(msg, sender){
  await ready;
  const origin = _originOf(sender);
  if(!origin || !/^https?:/.test(origin)) return { ok:false, error:'not a web page' };
  if(!cfg || !key) return { ok:false, error:'PosterChan Passwords is not paired' };

  const method = String(msg.method || '');
  const params = msg.params || {};
  if(!NOSTR_METHODS.has(method)) return { ok:false, error:'unsupported method' };
  const needsKey = method !== 'getPublicKey' && method !== 'getRelays';
  if(needsKey && !(cfg.mode === 'full' && cfg.sk))
    return { ok:false, error:'this browser is paired READ-ONLY, so it cannot sign. Re-pair with full access from PosterChan → Passwords → Pair a device.' };

  let kind = null;
  if(method === 'signEvent'){
    kind = _kindOf(params.event);
    if(kind === null) return { ok:false, error:'that event has no valid kind' };
  }
  const decision = await _ask(origin, method, kind, params);
  if(decision !== 'allow') return { ok:false, error:'refused' };

  try{
    const T = NT();
    const sk = _skBytes();
    switch(method){
      case 'getPublicKey': return { ok:true, result: cfg.pubkey };
      case 'getRelays': {
        const out = {};
        for(const u of relayUrls()) out[u] = { read:true, write:true };
        return { ok:true, result: out };
      }
      case 'signEvent': {
        // Built field by field rather than copied-and-deleted: the site does not get to choose whose
        // event this is (pubkey/id/sig come from the key we hold), and it does not get to smuggle
        // extra properties into something we signed.
        const src = params.event || {};
        const now = Math.floor(Date.now()/1000);
        let at = Number(src.created_at);
        // Clamped, not merely defaulted. A replaceable event dated in the far future outranks every
        // real update the user ever makes again — a permanent, unfixable contact list.
        if(!Number.isFinite(at) || Math.abs(at - now) > 900) at = now;
        const ev = { kind, created_at: Math.floor(at), tags: _cleanTags(src.tags),
                     content: String(src.content == null ? '' : src.content) };
        return { ok:true, result: T.finalizeEvent(ev, sk) };
      }
      case 'nip04.encrypt': return { ok:true, result: await T.nip04.encrypt(sk, params.pubkey, params.plaintext) };
      case 'nip04.decrypt': return { ok:true, result: await T.nip04.decrypt(sk, params.pubkey, params.ciphertext) };
      case 'nip44.encrypt': {
        const ck = T.nip44.v2.utils.getConversationKey(sk, params.pubkey);
        return { ok:true, result: T.nip44.v2.encrypt(params.plaintext, ck) };
      }
      case 'nip44.decrypt': {
        const ck = T.nip44.v2.utils.getConversationKey(sk, params.pubkey);
        return { ok:true, result: T.nip44.v2.decrypt(params.ciphertext, ck) };
      }
      default: return { ok:false, error:'unsupported method ' + method };
    }
  }catch(e){ return { ok:false, error:(e && e.message) || 'failed' }; }
}

function _skBytes(){
  return /^[0-9a-f]{64}$/i.test(cfg.sk) ? V.fromHex(cfg.sk) : NT().nip19.decode(cfg.sk).data;
}

// Tags are an array of arrays of strings, and nothing else gets signed.
function _cleanTags(tags){
  if(!Array.isArray(tags)) return [];
  const out = [];
  for(const t of tags.slice(0, 5000)){
    if(!Array.isArray(t) || !t.length) continue;
    out.push(t.slice(0, 100).map(x => String(x == null ? '' : x)));
  }
  return out;
}

/* Ask, unless this origin already answered for this method (and kind). A stored `deny` is honoured
 * silently — re-prompting for something the user refused is how people learn to click Allow. */
async function _ask(origin, method, kind, params){
  const perms = await _perms();
  const k = _permKey(origin, method, kind);
  if(perms[k]) return perms[k];

  // A page can call signEvent in a loop. Without a cap that is two hundred browser windows in the
  // user's face, with no gesture required to open any of them.
  const open = _inflight.get(origin) || 0;
  if(open >= 3) return 'deny';
  _inflight.set(origin, open + 1);

  const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
  const req = { id, origin, method, kind,
                preview: method === 'signEvent' ? _preview(params.event) : '' };
  const answered = new Promise(res => _asking.set(id, { req, resolve: res }));
  const url = B.runtime.getURL('approve.html#' + id);
  let shut = () => {};
  try{
    // Firefox for Android has no `windows` API at all, and this extension declares Android support —
    // without the tab fallback every NIP-07 call there is refused with no prompt ever shown.
    if(B.windows && B.windows.create){
      const w = await B.windows.create({ url, type:'popup', width:420, height:520 });
      if(w && w.id != null) shut = () => B.windows.remove(w.id).catch(()=>{});
    }else{
      const t = await B.tabs.create({ url });
      if(t && t.id != null) shut = () => B.tabs.remove(t.id).catch(()=>{});
    }
  }catch(_){
    _asking.delete(id);
    _inflight.set(origin, (_inflight.get(origin) || 1) - 1);
    return 'deny';                      // no window: refuse rather than sign unasked
  }
  // A prompt nobody answers must not leave the page hanging forever — and the window it left behind
  // must not sit there looking live after the answer stopped mattering.
  let timer;
  const decision = await Promise.race([
    answered,
    new Promise(r => { timer = setTimeout(() => r('timeout'), 115000); }),
  ]);
  clearTimeout(timer);
  _asking.delete(id);
  _inflight.set(origin, Math.max(0, (_inflight.get(origin) || 1) - 1));
  if(decision === 'timeout'){ shut(); return 'deny'; }
  return decision;
}

/* Only OUR OWN pages may drive the approval flow and the permission store. Nothing routes a page's
 * message to these cases today, but the NIP-07 bridge and this switch share one listener, so the
 * guard is what keeps a future edit from turning "a site asked" into "the user allowed".
 *
 * The test is the URL, and ONLY the URL. `!sender.tab` was the obvious-looking extra belt and it is
 * wrong: windows.create({type:'popup'}) still puts the page in a TAB, so the approval window failed
 * its own guard and every sign-in came back "that request has expired". A moz-extension:// URL under
 * our own ID is not something a web page can present — a content script's sender.url is the page it
 * runs in — so the URL alone is the whole check. */
function _fromOurPage(sender, page){
  return !!sender && typeof sender.url === 'string' &&
         sender.url.startsWith(B.runtime.getURL(page));
}
function _fromApproval(sender){ return _fromOurPage(sender, 'approve.html'); }
// The browser-action popup has no tab at all, so it has no sender.url in some builds; a message
// with neither a tab nor a URL cannot have come from a web page either.
function _fromPopup(sender){
  return _fromOurPage(sender, 'popup.html') || (!!sender && !sender.tab && !sender.url);
}

function _pendingApproval(id, sender){
  if(!_fromApproval(sender)) return { ok:false, error:'no' };
  const a = _asking.get(id);
  return a ? { ok:true, req: a.req } : { ok:false, error:'that request has expired' };
}

async function _answerApproval(msg, sender){
  if(!_fromApproval(sender)) return { ok:false };
  const a = _asking.get(msg.id);
  if(!a) return { ok:false };
  if(msg.remember){
    const perms = await _perms();
    perms[_permKey(a.req.origin, a.req.method, a.req.kind)] = msg.allow ? 'allow' : 'deny';
    await B.storage.local.set({ [NOSTR_OK]: perms });
  }
  a.resolve(msg.allow ? 'allow' : 'deny');
  return { ok:true };
}

/* What the event actually says, for the prompt. A signer that shows "sign this event?" and nothing
 * else is a rubber stamp — and CONTENT alone is that same rubber stamp for exactly the kinds that
 * matter, because a zap's amount, a delete's targets and a contact list's follows are all in TAGS
 * and leave `content` empty. */
function _preview(ev){
  if(!ev) return '';
  const out = [];
  const c = String(ev.content == null ? '' : ev.content);
  if(c) out.push(c.length > 300 ? c.slice(0, 300) + '…' : c);
  const tags = _cleanTags(ev.tags);
  const counts = new Map();
  for(const t of tags){
    if(t[0] === 'amount' || t[0] === 'relay' || t[0] === 'challenge' || t[0] === 'd')
      out.push(t[0] + ': ' + t.slice(1).join(' ').slice(0, 120));
    else counts.set(t[0], (counts.get(t[0]) || 0) + 1);
  }
  const summary = [...counts].map(([n, c2]) => c2 + ' × ' + n).join(', ');
  if(summary) out.push('tags: ' + summary);
  return out.join('\n').slice(0, 900);
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
          mode: payload.mode === 'full' ? 'full' : 'ro', sk: payload.sk || '',
          /* THE INSTANCE, and the only thing in here this extension ever makes an HTTP request to.
           *
           * Everything else is relay-only on purpose. Saving a PAGE needs the encrypted drive — a
           * screenshot cannot fit in a note, NIP-44 refuses plaintext over 65535 bytes — and a drive
           * has an address that a relay URL does not imply. Absent (an older pairing) simply means no
           * page-saving, which the popup says out loud rather than discovering at upload time. */
          api: String(payload.api || '').replace(/\/+$/, ''),
          /* WHERE ATTACHMENTS LIVE, which is not always the instance: a user with their own Blossom
           * server reads `pcres:` blobs from THAT host. Uploading to the instance instead writes a
           * note whose picture 404s on the only screen it is ever opened from — and says "saved". */
          media: String(payload.media || '').replace(/\/+$/, '') };
  key = V.fromB64(cfg.key);
  initBookmarks();
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
          return reply({ paired: !!cfg, mode: cfg && cfg.mode, count: items.size, status, lastSync,
                         bmOn: !!(BM && BM.engine && BM.engine.enabled()),
                         bmCount: (BM && BM.engine) ? BM.engine.count() : 0,
                         // The confirm a bulk delete is waiting on, so the popup can offer it on OPEN —
                         // no more keeping the popup open to finish a delete.
                         bmPending: (BM && BM.engine && BM.engine.pending) ? BM.engine.pending() : 0 });
        /* Bookmark sync, off until asked for. A read-only pairing can RECEIVE bookmarks and cannot
         * publish its own (no signing key) — the same line the vault draws, stated in the popup
         * rather than discovered when nothing leaves this browser. */
        case 'bm-enable': {
          if(!(BM && BM.engine)) return reply({ ok:false, error:'bookmark sync unavailable in this build' });
          /* No bookmarks API = no bookmark sync, and WHY differs by platform. Firefox for ANDROID does
           * not implement the bookmarks WebExtension API at all — there is no permission to grant and
           * nothing the user can do, so saying "grant the permission" sends them hunting for a setting
           * that does not exist. Desktop with the permission actually withheld is the other case. Say
           * which one it is. (Passwords and the NIP-07 signer do not use this API and work on Android.) */
          if(!B.bookmarks){
            let android = false;
            try{ android = ((await B.runtime.getPlatformInfo()).os === 'android'); }catch(_){}
            return reply({ ok:false, error: android
              ? 'Firefox for Android doesn’t support the bookmarks API, so bookmark sync is ' +
                'desktop-only. Your passwords and Nostr sign-in still work here.'
              : 'this browser has not granted the bookmarks permission — open the extension’s ' +
                'details page and allow it, then try again' });
          }
          const v = await BM.engine.setEnabled(!!msg.on);
          return reply({ ok:true, on:v, count: BM.engine.count() });
        }
        case 'bm-tidy': {
          if(!(BM && BM.engine)) return reply({ ok:false, error:'bookmark sync unavailable' });
          const r = await BM.engine.tidy();
          return reply({ ok:true, ...r });
        }
        case 'bm-sync': {
          if(!(BM && BM.engine) || !BM.engine.enabled()) return reply({ ok:false, error:'bookmark sync is off' });
          // confirmRemovals: the user answered the "this looks like a restore" question with "no, I
          // meant it". Nothing else may bypass that check.
          const r = await BM.engine.union({ confirmRemovals: !!msg.confirmRemovals });
          return reply({ ok:true, ...r });
        }
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
          /* EXACT ONLY. `matchLevel` also returns 'domain' for a shared registrable domain — and
           * `baseDomain` has no notion of shared-hosting suffixes, so `victim.github.io` and
           * `evil.github.io` reduce to the same site. Accepting 'domain' here meant a page on a
           * sibling subdomain of a hosting provider could be handed the password. The header of
           * content.js already states the rule this now implements: a domain match is offered in
           * the list for a human to choose, never released to a frame automatically. */
          const from = (sender && sender.url) || '';
          if(V.matchLevel(it, from) !== 'exact')
            return reply({ ok:false, error:'this frame is not that site' });
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
        case 'nostr':
          return reply(await handleNostr(msg, sender));
        /* Post the current page. THE POPUP ONLY — this signs and publishes with no per-site approval
         * at all: the approval is the user pressing the button inside the extension's own popup.
         * A page that could reach it would be able to post as the user silently, with none of the
         * per-origin, per-kind consent the NIP-07 path insists on. A content script always has
         * `sender.tab`. */
        case 'share-post': {
          if(!_fromPopup(sender)) return reply({ ok:false, error:'not available to a page' });
          return reply(await sharePost(msg));
        }
        /* Save the page to Notes. THE POPUP ONLY, same as posting: this writes to the user's relay
         * with the user's key, so a page that could reach it could fill their notebook. */
        case 'note-save': {
          if(!_fromPopup(sender)) return reply({ ok:false, error:'not available to a page' });
          return reply(await saveNote(msg));
        }
        /* Photograph the page and save it. POPUP ONLY, for the same reason as note-save — it writes
         * to the user's relay with the user's key — and it additionally reads the ACTIVE TAB, so a
         * page being able to ask for this would be a page able to photograph whatever tab you were
         * looking at. */
        case 'page-save': {
          if(!_fromPopup(sender)) return reply({ ok:false, error:'not available to a page' });
          let tabId = msg && msg.tabId;
          if(!tabId){
            const [t] = await B.tabs.query({ active:true, currentWindow:true });
            tabId = t && t.id;
          }
          return reply(await savePage(msg, tabId));
        }
        case 'approve-answer':
          return reply(await _answerApproval(msg, sender));
        case 'nostr-perms':
          if(!_fromPopup(sender)) return reply({ ok:false });   // the popup asks this, never a page
          return reply({ ok:true, perms: await _perms() });
        case 'nostr-forget': {
          if(!_fromPopup(sender)) return reply({ ok:false });
          const perms = await _perms();
          for(const k of Object.keys(perms)) if(k.split('|')[0] === msg.origin) delete perms[k];
          await B.storage.local.set({ [NOSTR_OK]: perms });
          return reply({ ok:true });
        }
        case 'approve-ask':
          return reply(_pendingApproval(msg.id, sender));
        case 'save': return reply(await saveItem(msg.item, !!msg.full));
        /* The whole item, for the edit form. Same rule as `reveal`: the POPUP only — `sender.tab` is
         * set for a content script, and this hands back a password with no site check at all. */
        case 'item': {
          if(sender && sender.tab) return reply({ ok:false, error:'not available to a page' });
          const it = items.get(msg.id);
          if(!it) return reply({ ok:false, error:'not found' });
          return reply({ ok:true, item: it });
        }
        case 'pair': return reply(await pair(msg.code));
        case 'unpair': return reply(await unpair());
        case 'sync': connect(); return reply({ ok:true });
        case 'relays-get':
          return reply({ ok:true, relays: userRelays, paired: relayUrls(), fallback: DEFAULT_RELAY });
        case 'relays-set': {
          const list = [...new Set((msg.relays || []).map(normRelay).filter(Boolean))].slice(0, 6);
          userRelays = list;
          await B.storage.local.set({ relays: list });
          // connect() already reconciles — it closes anything no longer wanted and opens what is
          // new — so tearing every socket down first would drop a working relay for nothing. Called
          // immediately rather than waiting for the 30s poll: somebody editing this is doing it
          // BECAUSE the current list is not working.
          connect();
          return reply({ ok:true, relays: list, using: relayUrls() });
        }
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

/* The engine's init is AWAITED here, not fired and forgotten.
 *
 * It sets `api` synchronously but loads `bmOn` and the known-bookmark map AFTER an await. A popup
 * opening right after a service-worker wake therefore asked for state mid-load and got enabled=false
 * — the toggle rendered UNCHECKED on a browser where sync was on. Re-ticking it then ran a union
 * against an EMPTY map, so nothing deduped and every bookmark was republished under a fresh sync id,
 * which every other browser dutifully created as new. "Keeps getting unchecked" and "keeps bringing
 * back dupe folders" are the same bug, twice. */
const ready = loadCfg().then(async () => { if(cfg) connect(); await initBookmarks(); });
// A phone suspends the whole extension; re-check the socket whenever anything talks to us.
setInterval(() => { if(cfg && !_anyOpen()) connect(); }, 30000);

/* KEEP SYNC ALIVE ACROSS SERVICE-WORKER SLEEP. On Chrome/Brave MV3 the worker is torn down after ~30s
 * idle, which closes the relay socket AND stops setInterval — so a bookmark added on another device
 * never arrives until something wakes the worker (opening the popup). setInterval cannot fix this: a
 * suspended worker runs no timers. chrome.alarms is the one thing that DOES wake a dead worker. A
 * one-minute alarm reconnects, re-subscribes (the subscription then delivers whatever was missed), and
 * runs a merge so local changes made while asleep go out. ~1 minute is the floor MV3 allows; true
 * instant push while idle is not possible in a service worker, and pretending otherwise is the bug.
 * Firefox keeps its event page similarly, and honours the same alarm. */
try {
  if (B.alarms && B.alarms.create) {
    B.alarms.create('pcvault-sync', { periodInMinutes: 1 });
    B.alarms.onAlarm.addListener((a) => {
      if (!a || a.name !== 'pcvault-sync' || !cfg) return;
      connect();                                   // reopen the socket the sleep closed; re-subscribe
      if (BM && BM.engine && BM.engine.enabled()) BM.engine.union().catch(() => {});
    });
  }
} catch (_) { /* no alarms permission / API: fall back to sync-on-popup-open only */ }

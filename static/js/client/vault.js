/* Vault — a password manager, in the Nostr client.
 *
 * STORAGE. One kind-30078 event PER CREDENTIAL, `d = pcai:pw:<id>`, tagged `l = pcai-pw` so the
 * whole vault is one indexed subscription. Same shape as Notes and for the same reasons: a single
 * document would be a read-modify-write of every password on every save (two devices editing two
 * logins lose one), and there is no index event anywhere, because an index is a second source of
 * truth that one empty read can wipe — the failure that has already cost this app a follows list
 * and a drive's file index.
 *
 * THE ONE REAL DIFFERENCE FROM NOTES, and the reason this file exists at all: items are sealed with
 * AES-256-GCM under a random VAULT KEY, not NIP-44-encrypted to the user's own pubkey. NIP-44-to-
 * self can only be opened by the secret key, which would mean the Firefox extension and the Android
 * autofill service each had to hold the user's nsec to read a password — the whole identity, on
 * every device, to fill a login form. A separate symmetric key can be handed to a paired device on
 * its own: a stolen browser profile then costs the vault and not the identity. The vault key itself
 * is NIP-44-wrapped to the user's own pubkey and published as `d = pcai:pwkey`, so it is still only
 * ever readable by them, and any device they log into can bootstrap from it.
 *
 * THE VAULT KEY IS THE WHOLE VAULT. Lose it and every item is ciphertext forever — there is no
 * recovery, by construction, because the server cannot read any of it. So:
 *   - it is cached in localStorage (wrapped) as well as published, so a device that has it never
 *     depends on a relay read to open the vault;
 *   - a wrapped key that will not unwrap FAILS LOUDLY and never mints a replacement. Minting one
 *     would produce a working-looking empty vault whose new key overwrites the only way back to the
 *     old items. This is exactly the rule FilesIdx._ensureMK learned the hard way;
 *   - a key is minted ONLY when the relay read succeeded and found nothing — never on a failed
 *     read, which is indistinguishable from an empty vault at the wire level;
 *   - the key event is published and confirmed BEFORE the first item is sealed under it.
 *
 * The `d` tags and timestamps are public metadata, as with Notes: an observer of the relay learns
 * how many credentials you hold and when you change them, never what or whose they are. Item ids
 * are random, never derived from the site.
 *
 * NOT ON THE SERVER, NOT IN THE AI, NOT ON TELEGRAM. There is deliberately no `password` command
 * and no server-side read path: the operator cannot serve what the operator cannot decrypt.
 */
(function(){
  'use strict';

  const KIND = 30078;
  const D_ITEM   = 'pcai:pw:';
  const D_FOLDER = 'pcai:pwfolder:';
  const D_KEY    = 'pcai:pwkey';
  const L_TAG    = 'pcai-pw';
  const PENDING_KEY = 'pcaiVaultPending';
  const WRAPPED_KEY = 'pcaiVaultKey';        // per-account, suffixed with the pubkey
  const FOLDER_ALL = '__all', FOLDER_NONE = '__none';

  let PC=null, $, $$, enc, toast, uiConfirm, uiPrompt, modal, closeModal, publish;
  const V = () => window.PCVaultCore;
  const Relay = () => window.Relay;
  const Store = () => window.Store;
  const ME = () => PC.ME;
  const now = () => Math.floor(Date.now()/1000);
  const uid = () => (crypto.getRandomValues(new Uint8Array(16)));

  let _booted=false;
  function boot(){
    if(_booted) return;
    PC = window.__PC;
    if(!PC) return setTimeout(boot, 50);
    _booted = true;
    ({ $, $$, enc, toast, uiConfirm, uiPrompt, modal, closeModal, publish } = PC);
    window.PCVault = {
      render(){ if(document.querySelector('.pv-wrap')) return; render(); },
      unmount(){ _sel=null; unwatch(); _stopTick(); },
      flush: flushPending,
      pendingCount: () => pending().length,
      // The paired-device half (extension / Android) asks for these.
      snapshot: () => _snapshot(),
      isUnlocked: () => !!_key,
      // Settings → Privacy owns the switch; this module owns what it means.
      stayUnlocked, setStayUnlocked,
      // Android: push the decrypted set into the autofill service's own store. No-op elsewhere.
      syncAndroid: () => _syncAndroid(),
      // The phone folder drawer is an overlay, so hardware Back has to close it before it walks the
      // view stack — the same hook Notes needed, for the same reason.
      drawerOpen: () => _drawerOpen,
      closeDrawer: () => _drawer(false),
      /* Plant an item without publishing it. Used by scripts/check_vault_mobile.py to reproduce a
       * vault as an OLDER build left it — there is no other way to test a migration, and testing
       * one by hand is how a migration ships broken. */
      __seed: (obj) => { if(_lib) _lib.items.set(obj.id, obj); },
    };
    window.addEventListener('online', () => { flushPending(); });
    setInterval(() => { if(navigator.onLine && pending().length) flushPending(); }, 45000);
  }

  // ---------------------------------------------------------------- ids

  function newId(){
    let s = '';
    for(const b of uid()) s += b.toString(16).padStart(2,'0');
    return s;
  }

  // ---------------------------------------------------------------- the vault key

  let _key = null;              // raw Uint8Array(32) — in memory
  let _keyWrapped = null;       // NIP-44 ciphertext, safe at rest
  let _keyLoading = null;

  const _lsKey = () => WRAPPED_KEY + ':' + (ME() && ME().pubkey || '');
  const _lsRaw = () => WRAPPED_KEY + ':raw:' + (ME() && ME().pubkey || '');
  const STAY_KEY = 'pcaiVaultStayUnlocked';

  /* KEEP THIS DEVICE UNLOCKED — on by default, and the reason the phone never asks for anything.
   *
   * Unwrapping the vault key is a NIP-44 decrypt with the user's secret key. For a local login that
   * key is already on the device and the unwrap is silent; but for an Amber / NIP-46 / NIP-07 login
   * it is a round trip to the SIGNER, and Android will pop Amber up every single time the app is
   * cold-started, the autofill service wakes, or the WebView is killed in the background — which on
   * a phone is constantly. A password manager that interrupts you to approve a decrypt before it
   * will show you a password is one you stop using.
   *
   * So the unwrapped key is cached on the device, and the toggle to stop that is in Settings rather
   * than in a nag. The honest description of the trade: with it ON, anything that can read this
   * app's local storage can read your vault — which on a local-key login is already true of your
   * nsec sitting beside it, and on an external-signer login is a real (opt-out) widening. It is NOT
   * a second password: the answer to "I don't want to type anything" is to say what is stored and
   * where, not to invent a lock and then teach people to work around it. */
  function stayUnlocked(){
    try{ return localStorage.getItem(STAY_KEY) !== '0'; }catch(_){ return true; }
  }
  function setStayUnlocked(on){
    try{
      localStorage.setItem(STAY_KEY, on ? '1' : '0');
      if(!on) localStorage.removeItem(_lsRaw());
      else if(_key) localStorage.setItem(_lsRaw(), V().toB64(_key));
    }catch(_){ }
  }

  function _cacheWrapped(w){
    _keyWrapped = w;
    try{ localStorage.setItem(_lsKey(), w); }catch(_){ }
  }
  function _cachedWrapped(){
    try{ return localStorage.getItem(_lsKey()) || null; }catch(_){ return null; }
  }
  function _cacheRaw(k){
    if(!stayUnlocked()) return;
    try{ localStorage.setItem(_lsRaw(), V().toB64(k)); }catch(_){ }
  }
  function _cachedRaw(){
    if(!stayUnlocked()) return null;
    try{
      const b = localStorage.getItem(_lsRaw());
      if(!b) return null;
      const raw = V().fromB64(b);
      return (raw && raw.length === 32) ? raw : null;
    }catch(_){ return null; }
  }

  /* Unwrap, or mint exactly once. Every branch here is about not producing a second key: a vault
   * with two keys is a vault half of which is unreadable, and nothing on screen would say so. */
  async function ensureKey(){
    if(_key) return _key;
    if(_keyLoading) return _keyLoading;
    _keyLoading = (async () => {
      // 0. Already unwrapped on this device — no signer, no relay, no prompt. This is the path a
      // phone takes on every cold start, and it is why opening the vault costs nothing.
      const raw0 = _cachedRaw();
      if(raw0){ _key = raw0; return _key; }
      // 1. This device's own cached copy of the WRAPPED key. No relay, no network, no race.
      let wrapped = _cachedWrapped();
      // 2. The published key event. Read from the local cache first, then the relay.
      let answered = false;
      if(!wrapped){
        let evs = [];
        try{ evs = Store().query([{ kinds:[KIND], authors:[ME().pubkey], '#d':[D_KEY] }]) || []; }catch(_){ }
        if(evs.length) answered = true;
        else if(Relay().query){
          /* `complete`, NOT the absence of a throw. Relay.query() has no reject path at all: when no
           * relay EOSEs it RESOLVES with [] and marks the array `complete:false` (relay.js). So
           * catching an exception here proved nothing — the guard was dead code, and the first open
           * on a device with no cached key while the socket was still connecting would have read
           * "you have no vault", minted a second key and published it over `pcai:pwkey`. That event
           * is addressable: relays drop the previous version, and every existing item on every
           * device becomes ciphertext forever, behind a screen that looks like a working empty
           * vault. Waiting for the socket first is the other half — a query fired at a dead socket
           * is exactly the "empty" answer we must not trust. */
          try{ if(Relay().ready) await Relay().ready(); }catch(_){ }
          let got = [], threw = false;
          try{ got = await Relay().query([{ kinds:[KIND], authors:[ME().pubkey], '#d':[D_KEY], limit:1 }]) || []; }
          catch(_){ threw = true; }
          // BOTH failures count as "we learned nothing": a rejection, and a resolved-but-incomplete
          // answer. An empty array from a catch carries no `complete` marker, so testing the marker
          // alone read a thrown query as a definitive "you have no vault" — which is the exact
          // remint this guard exists to prevent, reintroduced inside the guard itself.
          if(!threw && got.complete !== false) answered = true;
          evs = got;
        }
        const ev = evs.sort((a,b)=>(b.created_at||0)-(a.created_at||0))[0];
        if(ev && ev.content) wrapped = ev.content;
      } else answered = true;      // we already hold the key; the relay's opinion cannot lose it

      if(wrapped){
        // A key that will not unwrap is an error, NEVER a reason to mint. The likely causes are a
        // signer that isn't ready, the wrong account, or a refused NIP-44 — all temporary, and all
        // of which would be turned permanent by replacing the key.
        let raw;
        try{ raw = V().fromB64(JSON.parse(await PC.nip44dec(ME().pubkey, wrapped)).k); }
        catch(e){ throw new Error('couldn’t unlock your vault on this device — your signer refused ' +
                                  'to decrypt the vault key. Nothing has been changed.'); }
        if(!raw || raw.length !== 32) throw new Error('the stored vault key is malformed — refusing to replace it');
        _key = raw; _cacheWrapped(wrapped); _cacheRaw(raw);
        return _key;
      }

      if(!answered) throw new Error('couldn’t reach the relay to load your vault key. Try again when ' +
                                    'you’re back online — nothing has been created.');
      // Genuinely nothing there: first use on this account. Publish the key BEFORE anything is
      // sealed under it, or the first save would be unreadable forever.
      const k = V().newVaultKey();
      const w = await PC.nip44enc(ME().pubkey, JSON.stringify({ k: V().toB64(k), v:1 }));
      const r = await publish(KIND, w, [['d', D_KEY], ['l', L_TAG]], {quiet:true, noQueue:true});
      if(!(r && (r.ok || r.ev))) throw new Error('couldn’t create your vault — the key could not be saved');
      if(r.ev && !r.ok){ try{ Store().saveEvent(r.ev); }catch(_){ } queue(r.ev); }
      _key = k; _cacheWrapped(w); _cacheRaw(k);
      return _key;
    })().finally(()=>{ _keyLoading = null; });
    return _keyLoading;
  }

  // ---------------------------------------------------------------- data

  let _lib = null, _loading = null, _sub = null, _sel = null, _dirty = false;
  let _filter = { q:'', folder:FOLDER_ALL, tag:'' };

  const FILTER = () => ({ kinds:[KIND], authors:[ME().pubkey], '#l':[L_TAG] });

  async function load(force){
    if(_lib && !force) return _lib;
    if(!_loading) _loading = _loadCache().finally(()=>{ _loading=null; });
    return _loading;
  }

  async function _loadCache(){
    await ensureKey();
    const lib = { items:new Map(), folders:new Map() };
    let cached = [];
    try{ cached = Store().query([FILTER()]) || []; }catch(_){ cached = []; }
    await _absorb(lib, cached, true);
    _lib = lib;
    _repairUris();           // entries damaged by an older import, fixed in place
    _syncAndroid();          // the autofill service's copy tracks the vault, not the screen
    return _lib;
  }

  let _refreshing = false;
  async function refresh(){
    if(_refreshing || !_lib) return;
    _refreshing = true;
    try{
      const evs = await Relay().query([Object.assign(FILTER(), { limit: 5000 })]);
      const before = _stamp();
      await _absorb(_lib, evs || [], true);
      if(_stamp() !== before){
        _syncAndroid();                    // a change from ANOTHER device has to reach autofill too
        if(PC.VIEW === 'vault' && !_dirty) _paint();
      }
    }catch(_){ }
    finally{ _refreshing = false; }
  }

  function watch(){
    if(_sub || !Relay().subscribe) return;
    try{
      const f = Object.assign(FILTER(), { since: now() - 120 });
      delete f.limit;
      _sub = Relay().subscribe([f], { live:true, onEvent: async (ev) => {
        if(!_lib) return;
        const before = _stamp();
        await _absorb(_lib, [ev]);
        if(_stamp() !== before){
          _syncAndroid();
          if(PC.VIEW === 'vault' && !_dirty) _paint();
        }
      }});
    }catch(_){ _sub = null; }
  }
  function unwatch(){ if(_sub){ try{ Relay().close(_sub); }catch(_){ } _sub = null; } }

  const _dOf = ev => ((ev.tags||[]).find(t=>t[0]==='d')||[])[1] || '';
  function _stamp(){
    if(!_lib) return '';
    let s = _lib.items.size + ':' + _lib.folders.size;
    for(const it of _lib.items.values()) s += ',' + it.id + it.updated;
    return s;
  }

  /* Decrypt events into the library. Newest wins per id — copies of one `d` arrive from several
   * relays with different created_at, and an older one must never overwrite a newer. An item that
   * fails to open is COUNTED, not dropped silently: it means a key mismatch, and the user needs to
   * be told that rather than shown a vault that is quietly missing entries. */
  let _unreadable = 0;
  async function _absorb(lib, evs, fresh){
    if(fresh) _unreadable = 0;
    const sorted = (evs||[]).slice().sort((a,b)=>(b.created_at||0)-(a.created_at||0));
    const seen = new Set();
    for(const ev of sorted){
      const d = _dOf(ev);
      if(!d || d === D_KEY) continue;
      if(seen.has(d)) continue;
      seen.add(d);
      const isFolder = d.startsWith(D_FOLDER);
      const id = d.slice(isFolder ? D_FOLDER.length : D_ITEM.length);
      if(!id) continue;
      /* Newest wins against what is ALREADY held, not merely within this batch. Without this, a
       * second relay's stale copy arriving during refresh() replaces a password that was just
       * rotated — and because the editor re-reads the item from this map, the next edit publishes
       * the OLD password over the new one everywhere. It also stops a replayed tombstone deleting
       * a live entry. notes.js has the same line; dropping it here was the difference between a
       * cache and a downgrade attack. */
      const have = (isFolder ? lib.folders : lib.items).get(id);
      if(have && (have._at || 0) >= (ev.created_at || 0)) continue;
      if(!ev.content){ (isFolder ? lib.folders : lib.items).delete(id); continue; }   // tombstone
      let obj = null;
      try{ obj = await V().open(_key, ev.content); }
      catch(_){ _unreadable++; continue; }
      if(!obj || typeof obj !== 'object') continue;
      obj.id = id; obj._at = ev.created_at || 0;
      (isFolder ? lib.folders : lib.items).set(id, obj);
    }
  }

  /* REPAIR, rather than "please import it again".
   *
   * An older build split Bitwarden's comma-joined URI cell on newlines only, so an entry listing
   * several sites was stored as ONE unparseable URL — its host came out as `blackhillsenergy.com,
   * https` and it matched nothing, on the very site it was saved for. Fourteen entries in a real
   * 117-entry vault, including most of the banks.
   *
   * The damage is unambiguous — a parsed HOST containing a comma cannot arise any other way — and
   * the repair is exactly the split that should have happened. So it is done here, once, quietly,
   * instead of asking someone to re-run an import to fix a bug they did not cause. Only entries
   * that are actually damaged are touched, and each is republished as itself (a replaceable event),
   * so nothing is duplicated and nothing else is rewritten. */
  let _repaired = false;
  async function _repairUris(){
    if(_repaired || !_lib) return;
    _repaired = true;
    const broken = [];
    for(const it of _lib.items.values()){
      const uris = V().itemUris(it);
      if(!uris.some(u => (V().hostOf(u) || '').includes(','))) continue;
      const fixed = [];
      for(const u of uris){
        if((V().hostOf(u) || '').includes(',')) fixed.push(...V().splitUris(u));
        else fixed.push(u);
      }
      // Only a change that actually gains something counts — never rewrite an entry for nothing.
      if(fixed.length > uris.length){ it.uris = fixed; broken.push(it); }
    }
    if(!broken.length) return;
    let ok = 0;
    for(const it of broken){
      try{ await save(it, 'item'); ok++; }catch(_){ }
    }
    if(ok){
      toast(`repaired ${ok} saved login${ok===1?'':'s'} whose web addresses were stored wrong`);
      _paint();
    }
  }

  // ---------------------------------------------------------------- offline queue

  function pending(){
    try{ return JSON.parse(localStorage.getItem(PENDING_KEY)||'[]') || []; }catch(_){ return []; }
  }
  function setPending(list){
    try{ localStorage.setItem(PENDING_KEY, JSON.stringify(list)); }
    catch(_){ toast('this device is out of storage — that entry is saved here but may not sync'); }
  }
  function queue(ev){
    const list = pending().filter(e => _dOf(e) !== _dOf(ev));   // one entry per item: newest wins
    list.push(ev);
    setPending(list);
  }

  /* Same rule as Notes: a queued replaceable event is DISCARDED if the vault already holds a newer
   * version of that item, because publishing it would resurrect an old password over a new one —
   * which for a password manager means logging you out of a site you just rotated. */
  async function flushPending(){
    const list = pending();
    if(!list.length) return 0;
    const left = [], sentOk = [];
    for(const ev of list){
      const d = _dOf(ev);
      const id = d.startsWith(D_FOLDER) ? d.slice(D_FOLDER.length) : d.slice(D_ITEM.length);
      const holder = d.startsWith(D_FOLDER) ? (_lib && _lib.folders) : (_lib && _lib.items);
      const cur = holder && holder.get(id);
      if(cur && cur._at && (ev.created_at||0) < cur._at) continue;      // superseded — drop it
      try{
        const r = await Relay().publish(ev);
        if(r && r.ok) { sentOk.push(d); continue; }
      }catch(_){ }
      left.push(ev);
    }
    setPending(left);
    if(sentOk.length && PC.VIEW === 'vault' && !_dirty) _paint();
    return sentOk.length;
  }

  // ---------------------------------------------------------------- writes

  async function save(obj, kind){
    await ensureKey();
    const isFolder = kind === 'folder';
    obj.updated = now();
    if(!obj.created) obj.created = obj.updated;
    const d = (isFolder ? D_FOLDER : D_ITEM) + obj.id;
    const body = Object.assign({}, obj); delete body._at;
    const ct = await V().seal(_key, body);
    const r = await publish(KIND, ct, [['d', d], ['l', L_TAG]], {quiet:true, noQueue:true});
    if(r && r.ev){
      obj._at = r.ev.created_at || now();
      try{ Store().saveEvent(r.ev); }catch(_){ }
      if(!r.ok) queue(r.ev);
    }
    (isFolder ? _lib.folders : _lib.items).set(obj.id, obj);
    _syncAndroid();
    return { ok: !!(r && r.ok), queued: !!(r && r.ev && !r.ok) };
  }

  async function remove(obj, kind){
    const isFolder = kind === 'folder';
    const d = (isFolder ? D_FOLDER : D_ITEM) + obj.id;
    const r = await publish(KIND, '', [['d', d], ['l', L_TAG]], {quiet:true, noQueue:true});
    if(r && r.ev && !r.ok){ try{ Store().saveEvent(r.ev); }catch(_){ } queue(r.ev); }
    (isFolder ? _lib.folders : _lib.items).delete(obj.id);
    _syncAndroid();
    try{ await publish(5, '', [['a', KIND+':'+ME().pubkey+':'+d]], {quiet:true, noQueue:true}); }catch(_){ }
    return r;
  }

  // ---------------------------------------------------------------- snapshot (paired devices)

  /* What a paired device is handed: the decrypted items, plus the key so it can read future
   * updates from the relay itself. Deliberately a function and not a stored blob — nothing here
   * writes a plaintext copy of the vault anywhere. */
  function _snapshot(){
    return {
      v: 1,
      pubkey: ME() && ME().pubkey,
      key: _key ? V().toB64(_key) : null,
      items: _lib ? Array.from(_lib.items.values()) : [],
    };
  }

  /* ANDROID AUTOFILL. The native service is a separate process that wakes when another app shows a
   * login field, long after this WebView is gone — so it cannot ask us for anything, and it cannot
   * do the NIP-44 unwrap itself without reimplementing the whole crypto stack in Java. Instead the
   * app pushes what it has already decrypted into a Keystore-backed store the service reads.
   *
   * Only the fields autofill needs: title, username, password, uris, totp. Not the notes, not the
   * custom fields, not the cards — the less that sits in a second place, the better.
   *
   * Fire-and-forget: the plugin is absent in every browser, and a vault that refused to open
   * because it couldn't talk to Android would be a worse bug than no autofill. */
  async function _syncAndroid(){
    try{
      const Cap = window.Capacitor;
      const plug = Cap && Cap.Plugins && Cap.Plugins.VaultAutofill;
      if(!plug || !plug.put || !_lib) return false;
      // The MATCH KEYS are computed HERE, by the shared core, and shipped alongside each item —
      // exact hosts and registrable domains, already normalised. The alternative is a second
      // implementation of hostOf/baseDomain in Java, including the multi-label-suffix table, which
      // would be the one copy nobody tests and the one that decides whether a password is offered
      // on the right site. Java compares strings; the rule stays in one place.
      const items = Array.from(_lib.items.values())
        .filter(i => i.kind === 'login' && (i.username || i.password))
        .map(i => {
          /* Only URIs that are actually matchable BY HOST become keys. A `never` rule is excluded —
           * the whole point of it is that the site must not be offered, and indexing its host meant
           * Android autofilled exactly the place the user had excluded. A `regex` rule is excluded
           * too: it is a pattern, not an address, and hostOf() of one produces a nonsense host
           * (`bank\.example\.com` → `bank`) that would match some unrelated machine on a LAN. */
          const rules = V().itemUriRules(i).filter(r => r.match !== 'never' && r.match !== 'regex');
          const uris = rules.map(r => r.uri);
          const hosts = uris.map(u => V().hostOf(u)).filter(Boolean);
          return { id:i.id, title:i.title||'', username:i.username||'', password:i.password||'',
                   totp:i.totp||'', uris, hosts,
                   domains: Array.from(new Set(hosts.map(h => V().baseDomain(h)).filter(Boolean))) };
        });
      await plug.put({ items: JSON.stringify(items) });
      return true;
    }catch(_){ return false; }
  }

  // ---------------------------------------------------------------- view helpers

  function visibleItems(){
    if(!_lib) return [];
    const q = _filter.q.trim().toLowerCase();
    let list = Array.from(_lib.items.values());
    if(_filter.folder === FOLDER_NONE) list = list.filter(i => !i.folder);
    else if(_filter.folder !== FOLDER_ALL) list = list.filter(i => i.folder === _filter.folder);
    if(_filter.tag) list = list.filter(i => (i.tags||[]).includes(_filter.tag));
    if(q) list = list.filter(i =>
      (i.title||'').toLowerCase().includes(q) ||
      (i.username||'').toLowerCase().includes(q) ||
      V().itemUris(i).join(' ').toLowerCase().includes(q));
    return list.sort((a,b)=> (a.title||'').localeCompare(b.title||''));
  }

  function folderNames(){
    const s = new Set();
    if(_lib) for(const i of _lib.items.values()) if(i.folder) s.add(i.folder);
    return Array.from(s).sort((a,b)=>a.localeCompare(b));
  }
  function allTags(){
    const c = new Map();
    if(_lib) for(const i of _lib.items.values()) for(const t of (i.tags||[])) c.set(t, (c.get(t)||0)+1);
    return Array.from(c.entries()).sort((a,b)=> b[1]-a[1] || a[0].localeCompare(b[0]));
  }

  const blankItem = (folder) => ({ v:1, id:newId(), kind:'login', title:'', username:'', password:'',
                                   totp:'', uris:[], notes:'', folder: (folder && folder!==FOLDER_ALL &&
                                   folder!==FOLDER_NONE) ? folder : '', tags:[], fields:[],
                                   created:now(), updated:now() });

  /* Copy, and take it back. A password left on the clipboard is read by the next thing that asks —
   * every other manager clears it, and a manager that doesn't is quietly worse than pasting from a
   * text file you at least remember to close. Best effort: a background tab cannot write the
   * clipboard, so the clear is skipped rather than throwing. */
  const CLIP_CLEAR_MS = 45000;
  let _clipT = null;
  async function copy(text, what){
    try{ await navigator.clipboard.writeText(text); }
    catch(_){ toast('couldn’t reach the clipboard'); return; }
    toast((what||'copied') + ' — clipboard clears in 45s');
    clearTimeout(_clipT);
    _clipT = setTimeout(async () => {
      try{
        if(document.hasFocus() && (await navigator.clipboard.readText()) === text)
          await navigator.clipboard.writeText('');
      }catch(_){ }
    }, CLIP_CLEAR_MS);
  }

  // ---------------------------------------------------------------- render

  async function render(){
    const feed = $('#feed');
    if(!feed) return;
    if(!_lib){
      feed.innerHTML = '<div class="pv-wrap"><div class="spinner"></div></div>';
      try{ await load(); }
      catch(e){
        feed.innerHTML = `<div class="pv-wrap"><div class="empty pv-locked">
          <svg class="ic" aria-hidden="true"><use href="#i-key"></use></svg>
          <b>Your vault didn’t open</b>
          <div class="muted small">${enc(e.message||'error')}</div>
          <button class="btn btn-neon small pv-retry">Try again</button></div></div>`;
        const b = $('.pv-retry', feed); if(b) b.onclick = () => { _lib=null; render(); };
        return;
      }
    }
    _paint();
    watch();
    refresh();
  }

  function _paint(){
    const feed = $('#feed');
    if(!feed || !_lib) return;
    const items = visibleItems();
    const folders = folderNames();
    const tags = allTags();
    const total = _lib.items.size;
    const pend = pending().length;

    feed.innerHTML = `<div class="pv-wrap${_sel?' pv-open':''}${_drawerOpen?' pv-drawer':''}">
      <div class="pv-scrim"></div>
      <aside class="pv-side">
        <button class="btn btn-cyan pv-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New login</button>
        <div class="pv-sec"><span>Folders</span></div>
        <nav class="pv-folders">
          <button class="pv-folder${_filter.folder===FOLDER_ALL?' active':''}" data-f="${FOLDER_ALL}"><span>All items</span><i>${total}</i></button>
          ${folders.map(f=>`<button class="pv-folder${_filter.folder===f?' active':''}" data-f="${enc(f)}"><span>${enc(f)}</span><i>${Array.from(_lib.items.values()).filter(i=>i.folder===f).length}</i></button>`).join('')}
          <button class="pv-folder${_filter.folder===FOLDER_NONE?' active':''}" data-f="${FOLDER_NONE}"><span>Unfiled</span><i>${Array.from(_lib.items.values()).filter(i=>!i.folder).length}</i></button>
        </nav>
        ${tags.length?`<div class="pv-sec"><span>Tags</span></div>
          <div class="pv-tags">${tags.slice(0,30).map(([t,c])=>`<button class="pv-tag${_filter.tag===t?' active':''}" data-t="${enc(t)}">${enc(t)} <i>${c}</i></button>`).join('')}</div>`:''}
        <div class="pv-side-foot">
          ${pend?`<div class="pv-pending small">${pend} waiting to sync</div>`:''}
          ${_unreadable?`<div class="pv-pending small">${_unreadable} entr${_unreadable===1?'y':'ies'} could not be decrypted</div>`:''}
          <div class="pv-foot-actions">
            <button class="pv-link pv-health" title="Weak, reused and old passwords"><svg class="ic" aria-hidden="true"><use href="#i-chart"></use></svg>Health</button>
            <button class="pv-link pv-import" title="Import a Bitwarden export"><svg class="ic" aria-hidden="true"><use href="#i-download"></use></svg>Import</button>
          </div>
          <button class="pv-link pv-pair" title="Pair a browser extension or another device"><svg class="ic" aria-hidden="true"><use href="#i-link"></use></svg>Pair a device</button>
          <button class="pv-link pv-device" title="This device"><svg class="ic" aria-hidden="true"><use href="#i-gear"></use></svg>This device</button>
        </div>
      </aside>
      <section class="pv-list" aria-label="Passwords">
        <div class="pv-list-head">
          <div class="pv-list-top">
            <button class="pv-fbtn" aria-label="Choose a folder" aria-expanded="${_drawerOpen}">
              <svg class="ic" aria-hidden="true"><use href="#i-folder"></use></svg>
              <b>${enc(_filter.folder===FOLDER_ALL?'All items':_filter.folder===FOLDER_NONE?'Unfiled':_filter.folder)}</b>
              <svg class="ic pv-fbtn-c" aria-hidden="true"><use href="#i-chevron-down"></use></svg>
            </button>
            <b class="pv-list-title">${enc(_filter.folder===FOLDER_ALL?'All items':_filter.folder===FOLDER_NONE?'Unfiled':_filter.folder)}</b>
            <span class="pv-count">${items.length}</span>
            <button class="btn btn-cyan pv-new pv-new-m" title="New login" aria-label="New login"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg></button>
          </div>
          <div class="pv-searchwrap">
            <svg class="ic pv-searchic" aria-hidden="true"><use href="#i-search"></use></svg>
            <input class="input pv-search" type="search" placeholder="Search passwords…" value="${enc(_filter.q)}" autocomplete="off">
          </div>
        </div>
        ${items.length ? items.map(i=>`
          <button class="pv-item${_sel===i.id?' active':''}" data-id="${enc(i.id)}">
            <span class="pv-fav">${_favicon(i)}</span>
            <span class="pv-item-t">
              <b>${enc(i.title || V().hostOf(V().itemUris(i)[0]) || 'Untitled')}</b>
              <span class="pv-sub muted small">${enc(i.username || V().hostOf(V().itemUris(i)[0]) || '')}</span>
            </span>
            ${i.totp?'<span class="pv-badge" title="Has a one-time code">2FA</span>':''}
          </button>`).join('')
        : `<div class="empty">${total ? 'Nothing matches that.' : 'No passwords yet. Add one, or import your Bitwarden export.'}</div>`}
      </section>
      <section class="pv-editor" aria-label="Entry"></section>
    </div>`;

    $$('.pv-new', feed).forEach(b => b.onclick = () => openItem(blankItem(_filter.folder), true));
    _wireDrawer(feed);
    $('.pv-import', feed).onclick = openImport;
    $('.pv-pair', feed).onclick = openPair;
    $('.pv-device', feed).onclick = openDevice;
    $('.pv-health', feed).onclick = openHealth;
    const s = $('.pv-search', feed);
    let t=null;
    s.oninput = () => { clearTimeout(t); t=setTimeout(()=>{ _filter.q = s.value; _repaintKeepFocus(); }, 160); };
    $$('.pv-folder[data-f]', feed).forEach(b => b.onclick = () => { _filter.folder = b.dataset.f; _filter.tag=''; _drawer(false); _paint(); });
    $$('.pv-tag', feed).forEach(b => b.onclick = () => { _filter.tag = (_filter.tag===b.dataset.t?'':b.dataset.t); _drawer(false); _paint(); });
    $$('.pv-item', feed).forEach(b => b.onclick = () => { _drawer(false); const i=_lib.items.get(b.dataset.id); if(i) openItem(i, false); });
    if(_sel && _lib.items.has(_sel)) openItem(_lib.items.get(_sel), false);
  }

  function _repaintKeepFocus(){
    _paint();
    const el = document.querySelector('.pv-search');
    if(el){ el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  }

  /* A site's own icon, from ITS domain — a favicon request is a third-party GET that says "this
   * person has an account here", so it is deliberately a letter tile and not a network call. The
   * whole point of this screen is that nobody learns what is in it. */
  function _favicon(i){
    const host = V().hostOf(V().itemUris(i)[0] || '') || (i.title||'?');
    const ch = (host.replace(/^www\./,'')[0] || '?').toUpperCase();
    let h = 0; for(const c of host) h = (h*31 + c.charCodeAt(0)) >>> 0;
    return `<i class="pv-tile" style="--tile:${h % 360}deg">${enc(ch)}</i>`;
  }

  // ---------------------------------------------------------------- drawer (phone)

  let _drawerOpen = false;
  function _drawer(open){
    _drawerOpen = !!open;
    const wrap = document.querySelector('.pv-wrap'); if(!wrap) return;
    wrap.classList.toggle('pv-drawer', _drawerOpen);
    const btn = wrap.querySelector('.pv-fbtn'); if(btn) btn.setAttribute('aria-expanded', String(_drawerOpen));
  }
  function _wireDrawer(feed){
    const btn = $('.pv-fbtn', feed);
    if(btn) btn.onclick = () => _drawer(!_drawerOpen);
    const sc = $('.pv-scrim', feed);
    if(sc) sc.onclick = () => _drawer(false);
  }
  document.addEventListener('keydown', e => { if(e.key === 'Escape' && _drawerOpen) _drawer(false); });

  // ---------------------------------------------------------------- the entry

  let _saveT = null, _pendingCommit = null;
  function flushEdit(){
    if(!(_dirty && _pendingCommit)) return;
    const fn = _pendingCommit;
    clearTimeout(_saveT); _pendingCommit = null;
    fn();
  }

  function openItem(it, isNew){
    flushEdit();
    _stopTick();
    _sel = it.id;
    const host = document.querySelector('.pv-editor');
    if(!host) return;
    document.querySelector('.pv-wrap').classList.add('pv-open');
    const uris = V().itemUris(it);
    host.innerHTML = `
      <div class="pv-ed-head">
        <button class="pv-back" aria-label="Back to the list"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-left"></use></svg></button>
        <input class="input pv-title" placeholder="Name" value="${enc(it.title||'')}" maxlength="200">
        <span class="pv-state muted small"></span>
        <button class="btn btn-red pv-ico pv-del" title="Delete" aria-label="Delete"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>
      </div>
      <div class="pv-ed-body">
        <label class="pv-fld">Username
          <span class="pv-row">
            <input class="input pv-user" value="${enc(it.username||'')}" autocomplete="off" spellcheck="false">
            <button class="mini pv-copy-u" title="Copy username">Copy</button>
          </span>
        </label>
        <label class="pv-fld">Password
          <span class="pv-row">
            <input class="input pv-pass" type="password" value="${enc(it.password||'')}" autocomplete="new-password" spellcheck="false">
            <button class="mini pv-reveal" title="Show or hide" aria-label="Show password">Show</button>
            <button class="mini pv-copy-p" title="Copy password">Copy</button>
            <button class="mini pv-gen" title="Generate a new password">Generate</button>
          </span>
          <span class="pv-meter"><i></i></span>
        </label>
        <label class="pv-fld">One-time code (TOTP)
          <span class="pv-row">
            <input class="input pv-totp" value="${enc(it.totp||'')}" placeholder="secret or otpauth:// link" autocomplete="off" spellcheck="false">
            <button class="mini pv-copy-t" title="Copy the current code">Copy code</button>
          </span>
          <span class="pv-code muted small"></span>
        </label>
        <label class="pv-fld">Websites
          <textarea class="input pv-uris" rows="2" placeholder="https://example.com&#10;one per line" spellcheck="false">${enc(uris.join('\n'))}</textarea>
        </label>
        <div class="pv-row2">
          <label class="pv-fld pv-half">Folder
            <input class="input pv-folder-in" value="${enc(it.folder||'')}" list="pv-folders" placeholder="none">
            <datalist id="pv-folders">${folderNames().map(f=>`<option value="${enc(f)}"></option>`).join('')}</datalist>
          </label>
          <label class="pv-fld pv-half">Tags
            <input class="input pv-tagin" value="${enc((it.tags||[]).join(', '))}" placeholder="comma separated">
          </label>
        </div>
        <label class="pv-fld">Notes
          <textarea class="input pv-notes" rows="4" spellcheck="false">${enc(it.notes||'')}</textarea>
        </label>
        ${(it.fields||[]).length?`<div class="pv-fld"><span>Custom fields</span>
          ${(it.fields||[]).map(f=>`<div class="pv-row"><input class="input" value="${enc(f.name)}" readonly>
            <input class="input" type="${f.hidden?'password':'text'}" value="${enc(f.value)}" readonly></div>`).join('')}</div>`:''}
        ${it.card?`<div class="pv-fld"><span>Card</span><div class="muted small">${enc(it.card.brand||'')} ••••${enc(String(it.card.number||'').slice(-4))} — exp ${enc(it.card.expMonth||'')}/${enc(it.card.expYear||'')}</div></div>`:''}
        <div class="pv-meta muted small">Updated ${_fmt(it.updated)}${it.src&&it.src.app?` · imported from ${enc(it.src.app)}`:''}</div>
      </div>`;

    // Capture every field once. Same trap as the notes editor: a debounced save that re-queries the
    // DOM when it fires reads whichever entry is on screen THEN, and writes half of one onto the other.
    const title=$('.pv-title',host), user=$('.pv-user',host), pass=$('.pv-pass',host),
          totpIn=$('.pv-totp',host), urisIn=$('.pv-uris',host), folderIn=$('.pv-folder-in',host),
          tagIn=$('.pv-tagin',host), notes=$('.pv-notes',host), state=$('.pv-state',host);
    const mark = t => { if(state.isConnected) state.textContent = t; };

    const commit = async () => {
      _pendingCommit = null;
      it.title = title.value.trim();
      it.username = user.value;
      it.password = pass.value;
      it.totp = totpIn.value.trim();
      /* Keep each URI's match RULE. The textarea shows plain addresses, so rebuilding `uris` from
       * its lines threw the rules away — and a user's explicit "never autofill here" silently became
       * an ordinary domain match the next time they edited the title. Rules are re-attached by
       * address; a line the user changed loses its rule, which is right, because it is a different
       * address. */
      const wasRule = new Map(V().itemUriRules(it).filter(r => r.match).map(r => [r.uri, r.match]));
      it.uris = urisIn.value.split('\n').map(s=>s.trim()).filter(Boolean)
        .map(u => wasRule.has(u) ? { uri:u, match:wasRule.get(u) } : u);
      it.folder = folderIn.value.trim();
      it.tags = tagIn.value.split(',').map(s=>s.trim()).filter(Boolean).slice(0,30);
      it.notes = notes.value;
      try{
        const r = await save(it, 'item');
        _dirty = false;
        mark(r.ok ? 'saved' : r.queued ? 'saved on this device — will sync' : 'NOT saved');
        if(!r.ok && !r.queued) toast('couldn’t save that entry');
      }catch(e){ mark('NOT saved'); toast('couldn’t save: ' + (e.message||'error')); }
      _paintListCounts();
    };
    const touch = () => { _dirty = true; mark('saving…'); clearTimeout(_saveT);
                          _pendingCommit = commit; _saveT = setTimeout(commit, 700); };
    title.oninput = user.oninput = pass.oninput = urisIn.oninput = notes.oninput = touch;
    totpIn.oninput = () => { touch(); _startTick(host, totpIn.value); };
    folderIn.onchange = tagIn.onchange = touch;
    title.onblur = pass.onblur = flushEdit;

    $('.pv-back',host).onclick = () => { flushEdit(); _sel=null; _stopTick();
      document.querySelector('.pv-wrap').classList.remove('pv-open'); _paint(); };
    $('.pv-del',host).onclick = async () => {
      if(!await uiConfirm(`Delete “${it.title||'this entry'}”? This can’t be undone.`, {ok:'Delete', danger:true})) return;
      await remove(it, 'item'); _sel=null; _stopTick(); _paint();
    };
    const rev = $('.pv-reveal',host);
    rev.onclick = () => { const on = pass.type === 'password';
      pass.type = on ? 'text' : 'password'; rev.textContent = on ? 'Hide' : 'Show';
      rev.setAttribute('aria-label', on ? 'Hide password' : 'Show password'); };
    $('.pv-copy-u',host).onclick = () => copy(user.value, 'username copied');
    $('.pv-copy-p',host).onclick = () => copy(pass.value, 'password copied');
    $('.pv-copy-t',host).onclick = async () => {
      const cfg = V().totpConfig(totpIn.value);
      if(!cfg) return toast('no valid one-time code secret on this entry');
      copy(await V().totp(cfg.secret, cfg), 'code copied');
    };
    $('.pv-gen',host).onclick = () => openGenerator(p => { pass.value = p; pass.type='text';
      rev.textContent='Hide'; _meter(host, p); touch(); });
    pass.addEventListener('input', () => _meter(host, pass.value));
    _meter(host, it.password||'');
    _startTick(host, it.totp||'');
    if(isNew) title.focus();
  }

  function _paintListCounts(){
    const el = document.querySelector('.pv-count');
    if(el) el.textContent = String(visibleItems().length);
  }

  const _fmt = ts => { if(!ts) return '—'; const d=new Date(ts*1000);
    return d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); };

  /* Strength as a bar. Deliberately crude — length and variety, not a dictionary check — because a
   * meter that claims "strong" for `Passw0rd!` is worse than no meter at all. It measures the
   * SHAPE, and the generator next to it is the actual answer. */
  function _meter(host, pw){
    const bar = $('.pv-meter i', host); if(!bar) return;
    const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^a-zA-Z0-9]/].filter(r => r.test(pw||'')).length;
    const bits = (pw||'').length * (classes <= 1 ? 4 : classes === 2 ? 5 : classes === 3 ? 5.9 : 6.5);
    const pct = Math.max(0, Math.min(100, Math.round(bits / 110 * 100)));
    bar.style.width = pct + '%';
    bar.className = pct < 35 ? 'weak' : pct < 70 ? 'ok' : 'good';
  }

  // ---------------------------------------------------------------- TOTP ticker

  let _tick = null;
  function _stopTick(){ if(_tick){ clearInterval(_tick); _tick = null; } }
  function _startTick(host, raw){
    _stopTick();
    const out = $('.pv-code', host); if(!out) return;
    const cfg = V().totpConfig(raw);
    if(!cfg){ out.textContent = raw ? 'not a readable one-time code secret' : ''; return; }
    const paint = async () => {
      if(!out.isConnected) return _stopTick();
      try{
        const code = await V().totp(cfg.secret, cfg);
        const left = V().totpRemaining(cfg.period);
        out.innerHTML = `<b class="pv-otp">${enc(code.replace(/(\d{3})(?=\d)/, '$1 '))}</b>
                         <span class="pv-otp-left${left<=5?' low':''}">${left}s</span>`;
      }catch(_){ out.textContent = 'could not generate a code from that secret'; }
    };
    paint();
    _tick = setInterval(paint, 1000);
  }

  // ---------------------------------------------------------------- generator

  const GEN_KEY = 'pcaiVaultGen';
  function genOpts(){
    try{ return Object.assign({length:20,lower:true,upper:true,digits:true,symbols:true,avoidAmbiguous:false},
                              JSON.parse(localStorage.getItem(GEN_KEY)||'{}')); }
    catch(_){ return {length:20,lower:true,upper:true,digits:true,symbols:true,avoidAmbiguous:false}; }
  }
  function openGenerator(onUse){
    const o = genOpts();
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-key"></use></svg>Generate a password</h3>
      <div class="pv-gen-out"><code id="pg-out"></code>
        <button class="mini" id="pg-again" title="Generate another">↻</button></div>
      <div class="pv-gen-bits muted small" id="pg-bits"></div>
      <label class="fld">Length <b id="pg-len">${o.length}</b>
        <input type="range" id="pg-range" min="8" max="64" value="${o.length}" style="width:100%">
      </label>
      <div class="pv-gen-opts">
        <label><input type="checkbox" id="pg-lower" ${o.lower?'checked':''}> a-z</label>
        <label><input type="checkbox" id="pg-upper" ${o.upper?'checked':''}> A-Z</label>
        <label><input type="checkbox" id="pg-digits" ${o.digits?'checked':''}> 0-9</label>
        <label><input type="checkbox" id="pg-symbols" ${o.symbols?'checked':''}> !@#$</label>
        <label><input type="checkbox" id="pg-amb" ${o.avoidAmbiguous?'checked':''}> no look-alikes (1lI0O)</label>
      </div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px">
        <button class="btn btn-ghost small" id="pg-copy">Copy</button>
        ${onUse?'<button class="btn btn-neon small" id="pg-use">Use it</button>':''}
      </div>`, root => {
      const out = $('#pg-out', root), bits = $('#pg-bits', root);
      const read = () => ({ length: +$('#pg-range',root).value,
        lower:$('#pg-lower',root).checked, upper:$('#pg-upper',root).checked,
        digits:$('#pg-digits',root).checked, symbols:$('#pg-symbols',root).checked,
        avoidAmbiguous:$('#pg-amb',root).checked });
      const draw = () => {
        const opt = read();
        $('#pg-len',root).textContent = opt.length;
        try{
          out.textContent = V().generate(opt);
          bits.textContent = `about ${V().entropyBits(opt)} bits of entropy`;
          bits.classList.remove('pv-warn');
        }catch(e){
          out.textContent = '—';
          bits.textContent = 'pick at least one kind of character';
          bits.classList.add('pv-warn');
        }
        try{ localStorage.setItem(GEN_KEY, JSON.stringify(opt)); }catch(_){ }
      };
      $$('input', root).forEach(i => i.oninput = draw);
      $('#pg-again', root).onclick = draw;
      $('#pg-copy', root).onclick = () => copy(out.textContent, 'password copied');
      const use = $('#pg-use', root);
      if(use) use.onclick = () => { const p = out.textContent; closeModal(); if(p && p!=='—') onUse(p); };
      draw();
    });
  }

  // ---------------------------------------------------------------- health

  function openHealth(){
    const a = V().audit(Array.from(_lib.items.values()));
    const row = (label, list, why) => `<div class="pv-h-row"><b>${list.length}</b>
      <span>${label}</span><span class="muted small">${why}</span></div>` +
      (list.length ? `<div class="pv-h-list">${list.slice(0,40).map(i=>
        `<button class="pv-h-item" data-id="${enc(i.id)}">${enc(i.title||'Untitled')}
          <span class="muted small">${enc(i.username||'')}</span></button>`).join('')}</div>` : '');
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-chart"></use></svg>Vault health</h3>
      <div class="muted small">${a.total} login${a.total===1?'':'s'}. Computed on this device — none of
        this is sent anywhere, and nothing is checked against any breach service.</div>
      ${row('reused', a.reused, 'the same password on more than one site')}
      ${row('weak', a.weak, 'shorter than 12 characters')}
      ${row('old', a.old, 'unchanged for over a year')}
      ${row('without 2FA', a.noTotp, 'no one-time code stored')}
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn btn-ghost small" id="ph-close">Close</button></div>`,
      root => {
        $('#ph-close', root).onclick = closeModal;
        $$('.pv-h-item', root).forEach(b => b.onclick = () => {
          const it = _lib.items.get(b.dataset.id); closeModal(); if(it) openItem(it, false);
        });
      });
  }

  // ---------------------------------------------------------------- import

  function openImport(){
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-download"></use></svg>Import passwords</h3>
      <div class="muted small">Bitwarden’s <b>unencrypted</b> export — <code>.json</code> or <code>.csv</code>.
        In Bitwarden: Tools → Export vault → File format .json, with password protection OFF. Import it
        here, then delete the file: until you do, it is a plaintext copy of every password you own.</div>
      <div class="pv-imp-pick"><input type="file" id="pi-file" accept=".json,.csv,application/json,text/csv"></div>
      <div id="pi-prog"></div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px">
        <button class="btn btn-ghost small" id="pi-close">Close</button></div>`, root => {
      $('#pi-close', root).onclick = closeModal;
      $('#pi-file', root).onchange = async (e) => {
        const f = e.target.files && e.target.files[0]; if(!f) return;
        const prog = $('#pi-prog', root);
        prog.innerHTML = '<div class="muted small">reading…</div>';
        let parsed;
        try{ parsed = V().parseBitwarden(await f.text()); }
        catch(err){ prog.innerHTML = `<div class="nt-warn small">${enc(err.message||'could not read that file')}</div>`; return; }
        if(!parsed.items.length){ prog.innerHTML = '<div class="nt-warn small">that export has no entries in it</div>'; return; }
        await doImport(parsed, prog);
      };
    });
  }

  /* Re-importing UPDATES by Bitwarden id rather than duplicating — the same rule the Joplin importer
   * follows, and for the same reason: an interrupted import of a thousand entries has to be safe to
   * run again. Saves are serialized, not fired in parallel: a thousand concurrent publishes is how
   * you get a relay to start refusing them, and a refused save here is a password that isn't there. */
  async function doImport(parsed, prog){
    await ensureKey();
    /* Match an existing entry by Bitwarden id where there IS one (the .json export), and otherwise
     * by what identifies a login to a person: its name, its username and its first site. The .csv
     * export carries no ids at all, so without the fallback every re-import duplicated the entire
     * vault — and the failure message below tells people to run it again. */
    const bySrc = new Map(), byShape = new Map();   // byShape: key -> [items], never one
    /* Matching an incoming record to one already here.
     *
     * A Bitwarden id is authoritative when there is one (the .json export). The CSV has none, so it
     * falls back to a shape — and the shape MUST include the site, or two genuinely different
     * credentials merge and one password is destroyed. Measured on a four-item export while this
     * key was just title+username: an `amazon.com` login and an `amazon.co.uk` login under the same
     * name and address collapsed into one, and a secure note called "Wifi" was overwritten by a
     * login called "Wifi" — reported as "4 imported".
     *
     * So: same kind, same title, same username, and at least one site in common. Plus one narrow
     * migration case — an entry stored before the multi-URI split fix has a mangled host like
     * `poster.place,https`, which can share nothing with the corrected list; those are matched on
     * kind+title+username alone so the re-import people run to PICK UP that fix updates in place
     * instead of duplicating the vault. A mangled host cannot occur any other way. */
    const norm = (v) => String(v || '').trim().toLowerCase();
    const idOf = (i) => [norm(i.kind || 'login'), norm(i.title), norm(i.username)].join('|');
    const hostsOf = (i) => new Set(V().itemUris(i).map(u => V().hostOf(u)).filter(Boolean));
    const mangled = (i) => V().itemUris(i).some(u => /,/.test(V().hostOf(u) || ''));
    const findExisting = (rec) => {
      if(rec.src && rec.src.id && bySrc.has(rec.src.id)) return bySrc.get(rec.src.id);
      const cands = byShape.get(idOf(rec)) || [];
      const want = hostsOf(rec);
      for(const c of cands){
        if(mangled(c)) return c;
        for(const h of hostsOf(c)) if(want.has(h)) return c;
        if(!want.size && !hostsOf(c).size) return c;      // two site-less entries (a note, a card)
      }
      return null;
    };
    for(const i of _lib.items.values()){
      if(i.src && i.src.id) bySrc.set(i.src.id, i);
      const k = idOf(i);
      if(!byShape.has(k)) byShape.set(k, []);
      byShape.get(k).push(i);
    }
    let done = 0, failed = 0, queued = 0, oddTotp = 0;
    for(const rec of parsed.items){
      const existing = findExisting(rec);
      const it = existing || blankItem('');
      Object.assign(it, rec, { id: it.id, created: it.created || now() });
      // Track what this run has added, so a second row that is genuinely the same entry lands on
      // it — and one that merely shares a name does not.
      const k = idOf(it);
      if(!byShape.has(k)) byShape.set(k, []);
      if(!byShape.get(k).includes(it)) byShape.get(k).push(it);
      // The TOTP field may be a bare secret or a whole otpauth:// URI — totpConfig reads both, and
      // the value is kept AS GIVEN either way.
      //
      // An UNREADABLE one is kept too, and counted. Measured against a real 117-entry export: two
      // entries had a 15-character value with an `&` in it in Bitwarden's `login_totp` column —
      // not base32, not an otpauth URI, evidently something typed into the wrong box. Discarding
      // those (which this did) is deleting a thing the user wrote, silently, during an import they
      // cannot audit. The editor already says "not a readable one-time code secret" for exactly
      // this, so keeping it shows them what they have and lets them fix it.
      if(it.totp && !V().totpConfig(it.totp)) oddTotp++;
      try{
        const r = await save(it, 'item');
        if(r.queued) queued++;
        done++;
      }catch(_){ failed++; }
      if(done % 5 === 0 || done === parsed.items.length)
        prog.innerHTML = `<div class="nt-imp-bar"><i style="width:${Math.round(done/parsed.items.length*100)}%"></i></div>
          <div class="muted small">${done} / ${parsed.items.length} imported${failed?` · ${failed} failed`:''}…</div>`;
    }
    prog.innerHTML = `<div class="pv-imp-done"><b>Imported ${done} entr${done===1?'y':'ies'}.</b>
      ${queued?`<div class="muted small">${queued} are saved on this device and will sync when you’re online.</div>`:''}
      ${failed?`<div class="nt-warn small">${failed} could not be saved — run the import again to retry them.</div>`:''}
      ${oddTotp?`<div class="nt-warn small">${oddTotp} entr${oddTotp===1?'y has':'ies have'} a one-time-code
        value that isn’t a readable secret. Nothing was thrown away — open them to see what is there.</div>`:''}
      <div class="muted small">Now delete the export file.</div></div>`;
    _paint();
  }

  // ---------------------------------------------------------------- this device

  /* Android autofill, and the "never ask me again" switch. Both are per-DEVICE, which is why they
   * are here and not in the account settings that sync: the answer on your own phone and on a
   * shared laptop is allowed to differ. */
  async function openDevice(){
    const plug = window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.VaultAutofill;
    let st = null;
    if(plug && plug.status){ try{ st = await plug.status(); }catch(_){ } }
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-gear"></use></svg>This device</h3>
      ${st && st.supported ? `
        <div class="fld">Android autofill
          <div class="muted small">${st.enabled
            ? 'PosterChan is your autofill service — logins are offered in other apps and in the browser.'
            : 'Not turned on yet. Android only lets you choose an autofill service from its own settings screen.'}</div>
          ${st.enabled ? '' : '<button class="btn btn-neon small" id="pd-enable" style="margin-top:8px">Turn on autofill</button>'}
        </div>` : (plug ? `<div class="fld">Android autofill
          <div class="muted small">This version of Android has no autofill framework (it arrived in Android 8).</div></div>` : '')}
      <div class="fld">Unlocking
        <label class="nf-opt"><input type="checkbox" id="pd-stay" ${stayUnlocked()?'checked':''}>
          <b>Keep this device unlocked</b>
          <span class="muted small"> — open Passwords without approving anything, on every start.
          Turn this off and your signer is asked to unlock the vault once per session instead; on
          Amber that is a prompt every time the app is reopened. With it on, anything that can read
          this app's storage on this device can read your vault.</span></label>
      </div>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn btn-ghost small" id="pd-close">Close</button></div>`,
      root => {
        $('#pd-close', root).onclick = closeModal;
        const en = $('#pd-enable', root);
        if(en) en.onclick = async () => { try{ await plug.requestEnable(); closeModal(); }
                                          catch(e){ toast('couldn’t open Android’s autofill settings'); } };
        const stay = $('#pd-stay', root);
        if(stay) stay.onchange = () => {
          setStayUnlocked(stay.checked);
          toast(stay.checked ? 'this device stays unlocked' : 'this device will unlock through your signer');
        };
      });
  }

  // ---------------------------------------------------------------- pairing

  /* Hand the vault to a browser extension or another device. The payload carries the vault key, so
   * it is as sensitive as the vault itself — shown once, on screen, never published, never logged.
   *
   * TWO MODES, and the difference is stated on the screen rather than buried in a doc, because it
   * is the user's decision and they can only make it if they are told:
   *   read-only — the vault key alone. The device fills, generates and shows one-time codes, and a
   *               NEW login it saves is queued until this app publishes it. A stolen browser
   *               profile costs the passwords.
   *   full      — the vault key AND the signing key, so that device can publish vault events
   *               itself. Everything works standalone. A stolen browser profile also lets someone
   *               post and read DMs as you. Only offered when this device HOLDS the secret key: a
   *               NIP-07/NIP-46/Amber login has nothing to hand over, and pretending otherwise
   *               would produce a pairing that silently can't save.
   */
  function openPair(){
    const canFull = ME() && ME().mode === 'local';
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-link"></use></svg>Pair a device</h3>
      <div class="muted small">For the PosterChan Firefox extension, or another browser. The code below
        contains the key that decrypts your passwords — treat it like the passwords themselves.</div>
      <div class="fld">Access
        <label class="nf-opt"><input type="radio" name="pv-mode" value="ro" checked>
          <b>Read-only</b><span class="muted small"> — fill, generate and show 2FA codes. New logins
          saved in the browser wait for this app to publish them. Cannot post as you.</span></label>
        <label class="nf-opt${canFull?'':' pv-dim'}"><input type="radio" name="pv-mode" value="full" ${canFull?'':'disabled'}>
          <b>Full</b><span class="muted small"> — also saves new logins straight from the browser.
          This hands over your signing key: that browser can then post and read DMs as you.
          ${canFull?'':'<b>Unavailable:</b> you’re signed in with an external signer, so this device has no key to hand over.'}</span></label>
      </div>
      <div id="pv-pair-out"></div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px">
        <button class="btn btn-ghost small" id="pv-pair-close">Close</button>
        <button class="btn btn-neon small" id="pv-pair-go">Show pairing code</button>
      </div>`, root => {
      $('#pv-pair-close', root).onclick = closeModal;
      $('#pv-pair-go', root).onclick = async () => {
        const mode = ($('input[name="pv-mode"]:checked', root)||{}).value || 'ro';
        const out = $('#pv-pair-out', root);
        out.innerHTML = '<div class="muted small">preparing…</div>';
        try{
          await ensureKey();
          /* Every relay this device talks to, not just the configured one. The app already
           * publishes to all of them (Relay._send broadcasts to the pool), so the same vault is on
           * each — but the extension only ever knew about one, which made that one a single point of
           * failure for reading a password. `relay` stays for a device paired by an older build. */
          const relays = [...new Set([
            ...((Relay().urls && Relay().urls()) || []),
            (PC.CFG && PC.CFG.relay_url) || '',
          ].filter(Boolean))];
          const payload = { v:1, t:'pcvault', pubkey: ME().pubkey, key: V().toB64(_key),
                            relay: relays[0] || '', relays, mode };
          if(mode === 'full'){
            // Only a LOCAL login has a key to give. nip07/nip46/nip55 hold it in a signer that never
            // hands it over — which is the point of them — so there is nothing to pair, and the
            // radio for it is disabled above rather than producing a pairing that silently can't save.
            const s = (window.Session && window.Session.load && window.Session.load()) || null;
            const sk = (s && s.mode === 'local' && s.sk) || '';
            if(!sk){ out.innerHTML = '<div class="nt-warn small">this device has no signing key to hand over — pair read-only instead</div>'; return; }
            payload.sk = sk;
          }
          const code = V().toB64(new TextEncoder().encode(JSON.stringify(payload)));
          out.innerHTML = `<div class="pv-pair-code"><textarea class="input" id="pv-code" rows="4" readonly>${enc(code)}</textarea></div>
            <div class="row" style="gap:8px"><button class="mini" id="pv-code-copy">Copy code</button></div>
            <div class="muted small">Paste this into the extension’s Pair screen. Anyone who has it has your
              ${mode==='full'?'passwords AND your identity':'passwords'} — don’t send it over chat.</div>`;
          $('#pv-code-copy', root).onclick = () => copy(code, 'pairing code copied');
          const ta = $('#pv-code', root); if(ta) ta.onclick = () => ta.select();
        }catch(e){ out.innerHTML = `<div class="nt-warn small">${enc(e.message||'could not prepare a pairing code')}</div>`; }
      };
    });
  }

  boot();
})();

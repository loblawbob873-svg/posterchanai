/* Music sharing — hand a playlist (or your library) to other people, who can play it and keep it.
 *
 * WHY THE BYTES ARE RE-SEALED ONCE, AND NOT JUST RE-WRAPPED.
 * A library track is AES-GCM under the drive's MASTER key (app.js uploadMusicTrack). A per-track key
 * does not exist to hand out, and handing out the master key would hand out the whole drive — every
 * song, every Notes attachment, the files index. So a shared track gets ONE extra sealed copy, made
 * the first time it is shared, under a key that opens that track and nothing else:
 *
 *     key  = HMAC-SHA256(masterKey, "pcai-musicshare-v1:" + <library sha>)   (app.js musicShareKey)
 *     iv   = SHA-256(plaintext)[:12]
 *     blob = AES-GCM(key, iv, plaintext)            (raw ciphertext — the iv travels in the share)
 *
 * Both are DETERMINISTIC, so sharing the same song again — with someone else, in another playlist,
 * from another device — produces the same bytes, the same sha, and Blossom stores it once. Every
 * recipient then reads THAT blob; nothing is duplicated per person.
 *
 * THE SHARE ITSELF is a kind-30078 document PER RECIPIENT, `d = pcai:musicshare:<id>:<pk[:16]>`,
 * tagged `p = <recipient>` and `l = pcai-musicshare`, content NIP-44-encrypted to that recipient.
 * NIP-44's conversation key is symmetric, so the SHARER can decrypt it too — which is how "Shared by
 * me" is rebuilt on any device without a second, self-encrypted copy that could disagree with it.
 * What leaks: that A shares something with B (the p-tag), never what.
 *
 * ADDING TO YOUR LIBRARY does not re-encrypt: the recipient uploads the SAME ciphertext to their own
 * Blossom (a dedup on one server — it only adds them as an owner, so the sharer deleting their copy
 * no longer removes the bytes) and records the track in their drive index with its key wrapped to
 * THEMSELVES — the index's existing v1 `keyenc` shape ({k, iv} NIP-44 to self), which the player's
 * `_driveDecrypt` already reads. The added track is then an ordinary library track in every way.
 *
 * REVOKING cannot take back what was already delivered: it tombstones the documents (kind-5 FIRST,
 * then an empty-content replacement, as playlists.js does and for the same reason) and releases the
 * sharer's reference to copies nothing else of theirs shares. A recipient who added songs keeps
 * them; one who merely saw the list may have saved it. The UI says both, in so many words.
 *
 * READS ARE NEVER A LICENCE TO WRITE. Every document is written WHOLE from a body this device built
 * or decrypted — there is no merge — and an existing share is only extended (new recipients) or
 * released after a COMPLETE relay read. Writes are serialized through `_chain`.
 */
(function(){
  'use strict';

  const KIND = 30078;
  const D_PFX = 'pcai:musicshare:';
  const L_TAG = 'pcai-musicshare';
  const BODY_MAX = 60000;          // NIP-44 refuses a plaintext over 65535 bytes
  const MAX_TO = 50;
  const HEX64 = /^[0-9a-f]{64}$/;

  let PC = null;
  const ME = () => (PC && PC.me && PC.me()) || null;
  const Relay = () => window.Relay;
  const Store = () => window.Store;
  const subtle = () => ((typeof crypto !== 'undefined' && crypto.subtle) ? crypto : window.crypto).subtle;
  const now = () => Math.floor(Date.now()/1000);
  const _id = () => Math.random().toString(36).slice(2,10) + now().toString(36);

  function _boot(){ if(PC) return true; PC = window.__PC || null; return !!PC; }

  // ------------------------------------------------------------------------------------ pure helpers

  function b64(u8){ let s = ''; for(let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000)); return btoa(s); }
  function unb64(s){ const r = atob(String(s || '')); const u = new Uint8Array(r.length); for(let i = 0; i < r.length; i++) u[i] = r.charCodeAt(i); return u; }
  const hex = u8 => Array.from(u8).map(b => b.toString(16).padStart(2, '0')).join('');
  async function sha256hex(u8){ return hex(new Uint8Array(await subtle().digest('SHA-256', u8))); }

  /* The one-track key. HMAC, so knowing it tells you nothing about the master key it came from. */
  async function deriveKey(mk, sha){
    if(!(mk instanceof Uint8Array) || mk.length !== 32 || !HEX64.test(String(sha || ''))) return null;
    const k = await subtle().importKey('raw', mk, { name:'HMAC', hash:'SHA-256' }, false, ['sign']);
    const out = await subtle().sign('HMAC', k, new TextEncoder().encode('pcai-musicshare-v1:' + sha));
    return new Uint8Array(out);
  }
  async function contentIV(plain){ return new Uint8Array(await subtle().digest('SHA-256', plain)).slice(0, 12); }
  async function seal(key, iv, plain){
    const ck = await subtle().importKey('raw', key, 'AES-GCM', false, ['encrypt']);
    return new Uint8Array(await subtle().encrypt({ name:'AES-GCM', iv }, ck, plain));
  }
  async function open(key, iv, ct){
    const ck = await subtle().importKey('raw', key, 'AES-GCM', false, ['decrypt']);
    return new Uint8Array(await subtle().decrypt({ name:'AES-GCM', iv }, ck, ct));
  }

  const dTag = (id, pk) => D_PFX + id + ':' + String(pk).slice(0, 16);
  function parseD(d){
    const s = String(d || '');
    if(!s.startsWith(D_PFX)) return null;
    const rest = s.slice(D_PFX.length), i = rest.lastIndexOf(':');
    if(i <= 0) return null;
    const id = rest.slice(0, i), r16 = rest.slice(i + 1);
    if(!/^[a-z0-9]{4,40}$/.test(id) || !/^[0-9a-f]{16}$/.test(r16)) return null;
    return { id, r16 };
  }
  const tagOf = (ev, n) => ((ev.tags || []).find(t => t && t[0] === n) || [])[1] || '';

  /* Everything that came off the wire goes through here: a document anyone can publish must not be
   * able to put a non-sha address, a wrong-size key or a 10 MB name in front of the player. */
  function cleanItem(t){
    if(!t || typeof t !== 'object') return null;
    const s = String(t.s || '').toLowerCase();
    if(!HEX64.test(s)) return null;
    let k, iv;
    try{ k = unb64(t.k); iv = unb64(t.iv); }catch(_){ return null; }
    if(k.length !== 32 || iv.length !== 12) return null;
    const mime = /^audio\/[a-z0-9.+-]{1,40}$/i.test(String(t.m || '')) ? String(t.m).toLowerCase() : 'audio/mpeg';
    const ext = /^[A-Za-z0-9]{1,5}$/.test(String(t.e || '')) ? String(t.e).toLowerCase() : '';
    return { s, k: String(t.k), iv: String(t.iv), n: String(t.n || 'track').slice(0, 200) || 'track',
             m: mime, z: Math.max(0, Number(t.z) || 0), e: ext };
  }
  function cleanBody(obj, from){
    if(!obj || typeof obj !== 'object' || obj.v !== 1) return null;
    if(from && obj.from !== from) return null;          // a body naming somebody else as its author
    const tracks = [], seen = new Set();
    for(const t of (Array.isArray(obj.tracks) ? obj.tracks : [])){
      const c = cleanItem(t); if(c && !seen.has(c.s)){ seen.add(c.s); tracks.push(c); }
    }
    let tl = null;
    if(obj.tl && typeof obj.tl === 'object'){
      const c = cleanItem({ s: obj.tl.s, k: obj.tl.k, iv: obj.tl.iv });
      if(c) tl = { s: c.s, k: c.k, iv: c.iv, n: Math.max(0, Number(obj.tl.n) || 0) };
    }
    let srv = String(obj.srv || '');
    if(!/^https?:\/\/[^\s"'<>]+$/i.test(srv)) srv = '';
    return { v: 1, id: String(obj.id || ''), name: String(obj.name || 'Shared music').slice(0, 120),
             from: String(obj.from || ''), srv: srv.replace(/\/+$/, ''), created: Number(obj.created) || 0,
             updated: Number(obj.updated) || 0, tracks, tl };
  }
  const trackCount = b => (b && (b.tl ? b.tl.n : b.tracks.length)) || 0;

  /* ACCEPTING A SHARE — the recipient's own decision, kept on the ACCOUNT rather than the device.
   *
   * A share used to be a place you visited: an inbox of cards, and inside each one a screen of its
   * own with its own back button. Reported from the APK — "playlists that are shared with you should
   * just appear as a regular playlist for simplicity. The Shared With Me button should be you
   * accepting or rejecting the share." So the inbox is now only the DECISION, and everything that
   * survives it is an ordinary playlist chip beside your own.
   *
   * Stored in ClientSettings (a private per-account document), not localStorage: accepting a share
   * on the phone and finding it missing on the desktop is the same feature failing. A rejected key
   * is remembered too — otherwise the next refresh offers it again, which is a "no" that does not
   * stick. Both lists hold KEYS (`<from>:<id>`), so re-sharing under a new id asks again, and a
   * sharer cannot flip somebody's answer by editing the document they already answered about. */
  const DECIDE_D = 'pcai:musicshares', DECIDE_LOCAL = 'musicShareDecisions';
  let _dec = null, _decRead = false, _decLoading = null;

  const _cleanDec = o => {
    const out = {};
    for(const [k, v] of Object.entries(o && typeof o === 'object' ? o : {})){
      if(typeof k !== 'string' || k.length > 200 || !v || typeof v !== 'object') continue;
      const at = Math.max(0, Number(v.at) || 0);
      if(at) out[k] = { yes: v.yes === true, at };
    }
    // Bounded like every other list here: a decision is small, but the document is replaceable and
    // somebody who is shared with daily should not grow one for ever.
    return Object.fromEntries(Object.entries(out).sort((a, b) => b[1].at - a[1].at).slice(0, 500));
  };
  /* NEWEST WINS, PER KEY. Two devices can answer the same offer while one of them is offline, and a
   * union of two sets cannot say which answer came later — it can only say both happened, which for
   * accept-vs-reject is no answer at all. A timestamp per key makes the merge total and makes an
   * offline "no" survive a later sync of an older "yes". */
  const _mergeDec = (a, b) => {
    const out = { ...(a || {}) };
    for(const [k, v] of Object.entries(b || {})) if(!out[k] || v.at > out[k].at) out[k] = v;
    return _cleanDec(out);
  };
  const _localDec = () => { try{ return _cleanDec(JSON.parse(localStorage.getItem(DECIDE_LOCAL) || '{}')); }catch(_){ return {}; } };
  const _saveLocalDec = d => { try{ localStorage.setItem(DECIDE_LOCAL, JSON.stringify(d)); }catch(_){} };
  const decisions = () => (_dec || (_dec = _localDec()));

  /* THE ANSWER FOLLOWS THE ACCOUNT, NOT THE PHONE. localStorage alone is how auto-mute came back on
   * for people who had turned it off on another device: accepting a playlist here and not having it
   * on the desktop is the same feature failing the same way. So the decisions live in a private
   * kind-30078 document (NIP-44 to yourself — nobody, including this node, learns whose music you
   * kept), with the local copy as the offline-capable cache that is merged into it, never over it. */
  async function loadDecisions(){
    if(!_boot() || !ME()) return decisions();
    const owner = ME().pubkey;
    if(_decLoading && _decLoading.owner === owner) return _decLoading.promise;
    const job = { owner };
    job.promise = (async () => {
      try{
        const { evs, complete } = await _query({ kinds:[KIND], authors:[owner], '#d':[DECIDE_D] });
        const ev = newestBy(evs, e => tagOf(e, 'd'))[0];
        if(ev){
          const body = JSON.parse(await PC.nip44dec(owner, ev.content));
          if(body && body.v === 1){ _dec = _mergeDec(_localDec(), _cleanDec(body.d)); _saveLocalDec(_dec); _decRead = true; }
        }
        // A relay that ANSWERED and holds nothing means this account has never decided anywhere
        // else, so ours is the only copy and may be published. One that did not answer means
        // nothing at all, and must not be read as "there is none".
        else if(complete) _decRead = true;
      }catch(_){}
      _decLoading = null;
      return decisions();
    })();
    _decLoading = job;
    return job.promise;
  }
  async function _saveDecisions(){
    if(!_boot() || !ME()) return false;
    const owner = ME().pubkey;
    /* Never publish over a document that was never read: an unreachable relay plus a fresh device
     * would replace every answer this account has given with the one just made here — the
     * replaceable-doc wipe the rest of this app is careful about. The local copy still holds it,
     * and the next load merges it in. */
    if(!_decRead){ loadDecisions().then(ok => { if(_decRead) _saveDecisions(); }); return false; }
    try{
      const ct = await PC.nip44enc(owner, JSON.stringify({ v:1, d: decisions() }));
      const r = await PC.publish(KIND, ct, [['d', DECIDE_D]], { quiet:true });
      return !!(r && r.ok);
    }catch(_){ return false; }
  }
  const accepted = () => new Set(Object.entries(decisions()).filter(([, v]) => v.yes).map(([k]) => k));
  const rejected = () => new Set(Object.entries(decisions()).filter(([, v]) => !v.yes).map(([k]) => k));
  function decide(key, yes){
    if(!key) return;
    _dec = _cleanDec({ ...decisions(), [key]: { yes: !!yes, at: Math.floor(Date.now() / 1000) } });
    _saveLocalDec(_dec);
    _saveDecisions();          // fire and forget: the local copy already answered the UI
    _changed();
  }
  /* The three groups every screen here is built from. A share that was accepted and then STOPPED by
   * its sharer simply stops appearing — `inShares()` no longer lists it — and the stale key in the
   * decisions is harmless, which is why nothing prunes it on a read that may have failed. */
  const acceptedShares = () => { const a = accepted(); return inShares().filter(s => a.has(s.key)); };
  const pendingShares  = () => { const a = accepted(), r = rejected(); return inShares().filter(s => !a.has(s.key) && !r.has(s.key)); };

  /* The recipient's library record for an added track. `keyenc` is the v1 per-file key shape the
   * drive already reads ({k, iv}, NIP-44 to self), so the player needs no new branch — and the key
   * is WRAPPED, because a small drive index is stored inline where the server can read it. */
  function libraryEntry(item, from, keyenc, at){
    return { name: item.n, folder: 'Music', mime: item.m, enc: true, keyenc,
             size: item.z, srcName: item.n + (item.e ? '.' + item.e : ''), srcSize: item.z,
             srcExt: item.e, ts: at || now(), sharedBy: from };
  }
  /* Newest event per key wins; a tombstone (empty content) is a real answer, not an absence. */
  function newestBy(evs, keyOf){
    const m = new Map();
    for(const ev of evs || []){
      if(!ev || ev.kind !== KIND) continue;
      const k = keyOf(ev); if(!k) continue;
      const h = m.get(k);
      if(!h || (ev.created_at || 0) > (h.created_at || 0)) m.set(k, ev);
    }
    return m;
  }

  // ------------------------------------------------------------------------------------ state

  let _out = null, _outOk = false;       // id → { id, body, to: Map<pk,{at,dead}> }
  let _in = null, _inOk = false;         // from:id → { key, from, at, body }
  const _play = new Map();               // blob sha → { item, share } for tracks the player may ask about
  let _chain = Promise.resolve();
  const _watchers = new Set();
  const _changed = () => { for(const fn of _watchers){ try{ fn(); }catch(_){} } };
  const serial = fn => { const p = _chain.then(fn, fn); _chain = p.catch(() => {}); return p; };

  async function _query(filter){
    const R = Relay(), S = Store();
    let local = [];
    try{ local = (S && S.query([filter])) || []; }catch(_){}
    let remote = [], complete = false;
    try{
      if(R && R.ready) await R.ready();
      remote = (R && await R.query([filter])) || [];
      complete = remote.complete !== false;
    }catch(_){ remote = []; complete = false; }
    const byId = new Map();
    for(const e of local.concat(remote)) if(e && e.id) byId.set(e.id, e);
    for(const e of remote){ try{ if(S && S.saveEvent) S.saveEvent(e); }catch(_){} }
    return { evs: [...byId.values()], complete };
  }

  async function loadOut(){
    if(!_boot() || !ME()) return [];
    const me = ME().pubkey;
    const { evs, complete } = await _query({ kinds:[KIND], authors:[me], '#l':[L_TAG] });
    const out = new Map();
    const docs = newestBy(evs.filter(e => e.pubkey === me), e => { const p = parseD(tagOf(e, 'd')); return p ? tagOf(e, 'd') : ''; });
    // Newest live doc per share carries the body; one decrypt per share, not per recipient.
    const byShare = new Map();
    for(const ev of docs.values()){
      const p = parseD(tagOf(ev, 'd')), pk = tagOf(ev, 'p');
      if(!p || !HEX64.test(pk) || pk.slice(0, 16) !== p.r16) continue;
      if(!byShare.has(p.id)) byShare.set(p.id, []);
      byShare.get(p.id).push({ ev, pk });
    }
    for(const [id, rows] of byShare){
      const to = new Map();
      for(const r of rows) to.set(r.pk, { at: r.ev.created_at, dead: !r.ev.content });
      let body = null;
      for(const r of rows.filter(r => r.ev.content).sort((a, b) => b.ev.created_at - a.ev.created_at)){
        try{ body = cleanBody(JSON.parse(await PC.nip44dec(r.pk, r.ev.content)), me); }catch(_){ body = null; }
        if(body) break;
      }
      out.set(id, { id, body, to });
    }
    _out = out; _outOk = complete;
    _changed();
    return outShares();
  }

  async function loadIn(){
    if(!_boot() || !ME()) return [];
    const me = ME().pubkey;
    const { evs, complete } = await _query({ kinds:[KIND], '#p':[me], '#l':[L_TAG] });
    const docs = newestBy(evs.filter(e => e.pubkey !== me), e => e.pubkey + '|' + tagOf(e, 'd'));
    const inb = new Map();
    for(const ev of docs.values()){
      const p = parseD(tagOf(ev, 'd'));
      // Addressed to ME in both places, or it is somebody else's document that merely mentions me.
      if(!p || p.r16 !== me.slice(0, 16) || tagOf(ev, 'p') !== me) continue;
      const key = ev.pubkey + ':' + p.id;
      if(!ev.content){ inb.delete(key); continue; }     // revoked
      let body = null;
      try{ body = cleanBody(JSON.parse(await PC.nip44dec(ev.pubkey, ev.content)), ev.pubkey); }catch(_){ body = null; }
      if(!body) continue;
      inb.set(key, { key, from: ev.pubkey, at: ev.created_at, body });
    }
    _in = inb; _inOk = complete;
    _changed();
    return inShares();
  }

  const outShares = () => _out ? [..._out.values()].filter(s => [...s.to.values()].some(r => !r.dead))
                                   .sort((a, b) => ((b.body && b.body.updated) || 0) - ((a.body && a.body.updated) || 0)) : [];
  const inShares = () => _in ? [..._in.values()].sort((a, b) => b.at - a.at) : [];

  /* The tracks of a share. A big one keeps its list in a sealed blob so the document stays under
   * NIP-44's ceiling; that blob is fetched on demand and validated item by item. */
  async function tracksOf(share){
    const b = share && share.body; if(!b) return [];
    if(!b.tl) return b.tracks;
    if(share._tracks) return share._tracks;
    const ct = await _fetchBlob(b.srv, b.tl.s);
    const arr = JSON.parse(new TextDecoder().decode(await open(unb64(b.tl.k), unb64(b.tl.iv), ct)));
    const out = [], seen = new Set();
    for(const t of (Array.isArray(arr) ? arr : [])){ const c = cleanItem(t); if(c && !seen.has(c.s)){ seen.add(c.s); out.push(c); } }
    share._tracks = out;
    return out;
  }
  async function _fetchBlob(srv, sha){
    const bases = [srv, (PC.mediaServer && PC.mediaServer()) || ''].filter(Boolean);
    let last = null;
    for(const base of [...new Set(bases)]){
      try{
        const r = await fetch(base.replace(/\/+$/, '') + '/' + sha);
        if(r.ok) return new Uint8Array(await r.arrayBuffer());
        last = new Error('blob HTTP ' + r.status);
      }catch(e){ last = e; }
    }
    throw last || new Error('no media server');
  }

  // ------------------------------------------------------------------------------------ player

  function register(share, tracks){ for(const t of tracks || []) _play.set(t.s, { item: t, share }); }
  function meta(sha){
    const p = _play.get(sha); if(!p) return null;
    return { name: p.item.n, mime: p.item.m, size: p.item.z, enc: true, shared: true, sharedBy: p.share.from };
  }
  async function plain(sha){
    const p = _play.get(sha); if(!p) throw new Error('not a shared track');
    const ct = await _fetchBlob(p.share.body.srv, sha);
    return open(unb64(p.item.k), unb64(p.item.iv), ct);
  }

  // ------------------------------------------------------------------------------------ sharing

  const _cacheKey = () => 'pcaiMusicShareCopies:' + ((ME() && ME().pubkey) || '');
  function _cache(){ try{ return JSON.parse(localStorage.getItem(_cacheKey()) || '{}') || {}; }catch(_){ return {}; } }
  function _cacheSet(c){ try{ localStorage.setItem(_cacheKey(), JSON.stringify(c)); }catch(_){} }

  async function _present(url){
    try{ const r = await fetch(url + '?probe=' + Date.now(), { method:'HEAD', cache:'no-store' });
         return r.status === 200 || r.status === 206; }catch(_){ return false; }
  }
  /* Absolute, because the RECIPIENT resolves it: the built-in server is same-origin '/blossom' here,
   * which on somebody else's instance (or inside a bundle) would point at the wrong place. */
  function _absSrv(){ try{ return new URL(PC.mediaServer(), location.href).href.replace(/\/+$/, ''); }catch(_){ return PC.mediaServer(); } }
  const _shaOf = url => ((String(url || '').match(/([0-9a-f]{64})/i) || [])[1] || '').toLowerCase();

  /* The shareable copy of ONE library track: reused when this device already made it and the server
   * still has it, otherwise decrypted from the library and sealed under its own key. */
  async function _copyFor(t){
    const key = await PC.musicShareKey(t.sha);            // null → the drive key is not raw bytes here
    const srv = PC.mediaServer();
    const c = _cache(), hit = c[t.sha];
    if(key && hit && HEX64.test(hit.s) && await _present(srv + '/' + hit.s))
      return { s: hit.s, k: b64(key), iv: hit.iv };
    const pt = await PC.musicPlainOf(t.sha);
    const iv = await contentIV(pt);
    const k = key || crypto.getRandomValues(new Uint8Array(32));
    const ct = await seal(k, iv, pt);
    const want = await sha256hex(ct);
    const url = await PC.uploadBlob(new File([ct], 'shared-track.enc', { type:'application/octet-stream' }),
                                    { noMirror:true, keep:true, noCompress:true });
    const s = _shaOf(url);
    if(s !== want) throw new Error('the media server stored something other than what was sent');
    if(key){ const c2 = _cache(); c2[t.sha] = { s, iv: b64(iv) }; _cacheSet(c2); }
    return { s, k: b64(k), iv: b64(iv) };
  }

  async function _publishTo(body, pk){
    const ct = await PC.nip44enc(pk, JSON.stringify(body));
    const r = await PC.publish(KIND, ct, [['d', dTag(body.id, pk)], ['p', pk], ['l', L_TAG]], { quiet:true, noQueue:true });
    return !!(r && r.ok);
  }

  function cleanRecipients(pks){
    const me = ME() && ME().pubkey, out = [];
    for(const p of pks || []){ const k = String(p || '').toLowerCase(); if(HEX64.test(k) && k !== me && !out.includes(k)) out.push(k); }
    return out;
  }

  /* tracks: [{sha, name, mime, size, ext}] from the library. Resolves {ok, id, sent, failed, error}. */
  function share(opts, onStep){
    return serial(async () => {
      if(!_boot() || !ME()) return { ok:false, error:'not signed in' };
      const to = cleanRecipients(opts && opts.to);
      if(!to.length) return { ok:false, error:'no one to share with' };
      if(to.length > MAX_TO) return { ok:false, error:'at most ' + MAX_TO + ' people per share' };
      const tracks = ((opts && opts.tracks) || []).filter(t => t && HEX64.test(t.sha));
      if(!tracks.length) return { ok:false, error:'nothing to share' };
      const items = [];
      let done = 0;
      const step = () => { if(onStep) try{ onStep({ done, total: tracks.length }); }catch(_){} };
      step();
      /* Two at a time: every one is a download, a decrypt, an encrypt and an upload. */
      let i = 0, failed = 0;
      const lane = async () => {
        while(i < tracks.length){
          const t = tracks[i++];
          try{
            const c = await _copyFor(t);
            items.push(Object.assign(c, { n: String(t.name || 'track').slice(0, 200), m: t.mime || 'audio/mpeg',
                                          z: Number(t.size) || 0, e: t.ext || '', _o: tracks.indexOf(t) }));
          }catch(e){ failed++; console.warn('music share: could not prepare', t.sha.slice(0, 8), e); }
          done++; step();
        }
      };
      await Promise.all([lane(), lane()]);
      if(!items.length) return { ok:false, error:'none of the songs could be prepared', prepFailed: failed };
      items.sort((a, b) => a._o - b._o); items.forEach(x => delete x._o);
      const at = now();
      const body = { v:1, id: _id(), name: String((opts && opts.name) || 'Shared music').slice(0, 120),
                     from: ME().pubkey, srv: _absSrv(), created: at, updated: at, tracks: items };
      if(JSON.stringify(body).length > BODY_MAX){
        const lk = crypto.getRandomValues(new Uint8Array(32)), liv = crypto.getRandomValues(new Uint8Array(12));
        const lct = await seal(lk, liv, new TextEncoder().encode(JSON.stringify(items)));
        const url = await PC.uploadBlob(new File([lct], 'shared-list.enc', { type:'application/octet-stream' }),
                                        { noMirror:true, keep:true, noCompress:true });
        const s = _shaOf(url);
        if(s !== await sha256hex(lct)) return { ok:false, error:'the song list could not be stored' };
        body.tracks = []; body.tl = { s, k: b64(lk), iv: b64(liv), n: items.length };
      }
      const sent = [], bad = [];
      for(const pk of to){ (await _publishTo(body, pk) ? sent : bad).push(pk); }
      if(sent.length){
        if(!_out) _out = new Map();
        const toMap = new Map(sent.map(pk => [pk, { at, dead:false }]));
        _out.set(body.id, { id: body.id, body: cleanBody(body, ME().pubkey), to: toMap });
        _changed();
      }
      return { ok: !bad.length && !failed, id: body.id, sent, failed: bad, prepFailed: failed, count: items.length };
    });
  }

  /* More people on an EXISTING share: new documents only, each written whole from the body this
   * device decrypted — never a rewrite of anyone already on it. Needs a complete read, since a body
   * reconstructed from a partial one is exactly how a stale list goes out under a fresh date. */
  function addRecipients(id, pks){
    return serial(async () => {
      const sh = _out && _out.get(id);
      if(!_outOk || !sh || !sh.body) return { ok:false, error:'couldn’t read this share from the relays — try again' };
      const fresh = cleanRecipients(pks).filter(pk => !(sh.to.get(pk) && !sh.to.get(pk).dead));
      if(!fresh.length) return { ok:false, error:'they already have it' };
      const body = Object.assign({}, sh.body, { updated: now() });
      if(!body.tl) delete body.tl;
      const sent = [], bad = [];
      for(const pk of fresh){ (await _publishTo(body, pk) ? sent : bad).push(pk); }
      for(const pk of sent) sh.to.set(pk, { at: now(), dead:false });
      _changed();
      return { ok: !bad.length, sent, failed: bad };
    });
  }

  /* Stop sharing. The kind-5 goes FIRST: a relay deletes every version of an address with
   * `created_at <=` the deletion's, so sent second it would take the tombstone with it, and a device
   * that was offline would find nothing, keep its cached copy and go on playing the share. */
  function revoke(id, pks){
    return serial(async () => {
      if(!_boot() || !ME()) return { ok:false };
      const me = ME().pubkey, sh = _out && _out.get(id);
      if(!sh) return { ok:false, error:'not found' };
      const targets = (pks && pks.length ? cleanRecipients(pks) : [...sh.to.keys()])
        .filter(pk => sh.to.get(pk) && !sh.to.get(pk).dead);
      let bad = 0;
      for(const pk of targets){
        const d = dTag(id, pk);
        try{ await PC.publish(5, '', [['a', KIND + ':' + me + ':' + d], ['k', String(KIND)]], { quiet:true, noQueue:true }); }catch(_){}
        const r = await PC.publish(KIND, '', [['d', d], ['p', pk], ['l', L_TAG]], { quiet:true, noQueue:true });
        if(r && r.ok) sh.to.set(pk, { at: (r.ev && r.ev.created_at) || now(), dead:true }); else bad++;
      }
      let released = 0;
      if(![...sh.to.values()].some(r => !r.dead)) released = await _releaseUnused(sh);
      _changed();
      return { ok: !bad, failed: bad, released };
    });
  }

  /* Every blob a live share of mine still points at. null = "could not tell", which a caller that
   * DELETES must read as "keep everything". */
  async function _liveRefs(except){
    if(!_outOk || !_out) return null;
    const refs = new Set();
    for(const sh of _out.values()){
      if(sh === except || ![...sh.to.values()].some(r => !r.dead)) continue;
      if(!sh.body) return null;                         // a live share we cannot read
      if(sh.body.tl) refs.add(sh.body.tl.s);
      let ts;
      try{ ts = await tracksOf(sh); }catch(_){ return null; }
      for(const t of ts) refs.add(t.s);
    }
    return refs;
  }
  async function _releaseUnused(sh){
    if(!sh.body || !PC.releaseBlob) return 0;
    const refs = await _liveRefs(sh);
    if(!refs) return 0;
    let mine;
    try{ mine = await tracksOf(sh); }catch(_){ return 0; }
    const drop = mine.map(t => t.s).concat(sh.body.tl ? [sh.body.tl.s] : []).filter(s => !refs.has(s));
    let n = 0;
    for(const s of drop){ if(await PC.releaseBlob(s)) n++; }
    const c = _cache(), gone = new Set(drop);
    for(const k of Object.keys(c)) if(gone.has(c[k].s)) delete c[k];
    _cacheSet(c);
    return n;
  }

  /* For the drive check's reclaim: the share copies are keep-flagged and named by no drive index, so
   * without this they are "orphans" — and reclaiming them would break every recipient's playback. */
  async function refIds(){
    if(!_boot() || !ME()) return null;
    try{ await loadOut(); }catch(_){ return null; }
    const refs = await _liveRefs(null);
    if(!refs) return null;
    for(const v of Object.values(_cache())) if(v && HEX64.test(v.s)) refs.add(v.s);
    return refs;
  }

  // ------------------------------------------------------------------------------------ adding

  /* Copy shared tracks into MY library: the same ciphertext re-uploaded to my server (owner, not a
   * duplicate), the key wrapped to me. The verdict is the library SAVE, not the uploads. */
  async function addToLibrary(sh, items, onStep){
    if(!_boot() || !ME()) return { ok:false, error:'not signed in' };
    const have = new Set((PC.musicLibrary && PC.musicLibrary()) || []);
    const todo = (items || []).filter(t => !have.has(t.s));
    const entries = [], errors = [];
    let done = 0;
    const step = () => { if(onStep) try{ onStep({ done, total: todo.length }); }catch(_){} };
    step();
    for(const t of todo){
      try{
        const ct = await _fetchBlob(sh.body.srv, t.s);
        await open(unb64(t.k), unb64(t.iv), ct);            // prove the key before claiming the bytes
        const url = await PC.uploadBlob(new File([ct], t.n + '.enc', { type:'application/octet-stream' }),
                                        { noMirror:true, keep:true, noCompress:true });
        if(_shaOf(url) !== t.s) throw new Error('stored under a different address');
        const keyenc = await PC.nip44enc(ME().pubkey, JSON.stringify({ k: t.k, iv: t.iv }));
        entries.push([t.s, libraryEntry(t, sh.from, keyenc)]);
      }catch(e){
        const m = (e && e.message) || String(e);
        /* "Not authorized to upload" is not a broken share — it is a server that will not store files
         * for this account. Say which, and that playing still works. */
        errors.push(/403|not authori[sz]ed|can_blossom/i.test(m)
          ? 'your media server doesn’t let this account store files, so it can’t keep a copy — you can still play these while they are shared'
          : m);
      }
      done++; step();
    }
    let saved = true;
    if(entries.length) saved = !!(await PC.musicLibraryAdd(entries));
    return { ok: saved && !errors.length, added: saved ? entries.length : 0, skipped: items.length - todo.length,
             failed: errors.length, error: errors[0] || (saved ? '' : 'your library list could not be saved'), saved };
  }

  // ------------------------------------------------------------------------------------ recipients

  async function resolveRecipients(text){
    const out = [], bad = [];
    for(const raw of String(text || '').split(/[\s,]+/).map(s => s.trim()).filter(Boolean)){
      const v = raw.replace(/^nostr:/i, '');
      let pk = null;
      if(HEX64.test(v.toLowerCase())) pk = v.toLowerCase();
      else if(/^(npub1|nprofile1)/i.test(v)){
        try{ const d = window.NostrTools.nip19.decode(v); pk = d.type === 'npub' ? d.data : (d.data && d.data.pubkey); }catch(_){ pk = null; }
      } else if(v.includes('@') && PC.nip05Resolve){ try{ pk = await PC.nip05Resolve(v); }catch(_){ pk = null; } }
      if(pk && HEX64.test(pk)){ if(!out.includes(pk)) out.push(pk); } else bad.push(raw);
    }
    return { pks: out, bad };
  }

  // ------------------------------------------------------------------------------------ UI

  const E = s => (PC && PC.enc) ? PC.enc(s) : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function who(pk){
    try{ if(PC.needProfile) PC.needProfile(pk); }catch(_){}
    let p = {}; try{ p = (PC.profOf && PC.profOf(pk)) || {}; }catch(_){}
    const n = p.display_name || p.name;
    if(n) return String(n);
    try{ const np = window.NostrTools.nip19.npubEncode(pk); return np.slice(0, 12) + '…' + np.slice(-4); }catch(_){ return pk.slice(0, 10) + '…'; }
  }
  const mb = z => ((Number(z) || 0) / 1048576).toFixed(1) + 'MB';
  const icon = (id, cls) => `<svg class="ic ${cls || 'b-ic'}" aria-hidden="true"><use href="#i-${id}"></use></svg>`;
  const toast = m => { try{ PC.toast(m); }catch(_){} };

  /* Share dialog. `tracks` from the library; the people are typed or picked (the app's own
   * autocomplete — never a native prompt). */
  function openShareDialog(opts){
    if(!_boot()) return;
    const tracks = (opts && opts.tracks) || [];
    if(!tracks.length){ toast('nothing to share here yet'); return; }
    PC.modal(`<h3>${icon('share', 'h-ic')}Share music</h3>
      <p class="muted small">${tracks.length} song${tracks.length === 1 ? '' : 's'}. The people you pick can play them and add them to their own library. Nobody else can — the list is encrypted to each of them.</p>
      <label class="fld">Name<input class="input" id="msh-name" maxlength="120" value="${E((opts && opts.name) || 'Shared music')}"></label>
      <label class="fld">Share with<textarea class="input" id="msh-to" rows="3" placeholder="Type a name, or paste an npub — one per line"></textarea></label>
      <p class="muted small">Sharing makes one extra encrypted copy of each song the first time it is shared (it is sealed with a key that opens only that song). You can stop sharing at any time — but anyone who already added a song to their library keeps it.</p>
      <div class="msh-prog muted small" id="msh-prog" role="status" aria-live="polite"></div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:10px">
        <button class="btn btn-ghost small" id="msh-cancel">Cancel</button>
        <button class="btn btn-neon small" id="msh-go">${icon('share')}Share</button>
      </div>`, root => {
      const ta = root.querySelector('#msh-to');
      try{ if(PC.attachUserAutocomplete) PC.attachUserAutocomplete(ta, { multiline:true }); }catch(_){}
      root.querySelector('#msh-cancel').onclick = () => PC.closeModal();
      const go = root.querySelector('#msh-go'), prog = root.querySelector('#msh-prog');
      go.onclick = async () => {
        const { pks, bad } = await resolveRecipients(ta.value);
        if(bad.length){ prog.textContent = 'Couldn’t find: ' + bad.join(', '); return; }
        if(!pks.length){ prog.textContent = 'Pick at least one person.'; return; }
        go.disabled = true;
        root.classList.add('modal-sticky');     // a backdrop tap must not orphan a share mid-upload
        const r = await share({ name: root.querySelector('#msh-name').value.trim() || 'Shared music', tracks, to: pks },
                              s => { prog.textContent = s.total ? `Preparing ${s.done} / ${s.total}…` : ''; });
        go.disabled = false;
        root.classList.remove('modal-sticky');
        if(!r.sent || !r.sent.length){ prog.textContent = 'Not shared — ' + (r.error || 'the relays did not accept it') + '.'; return; }
        if(root.isConnected) PC.closeModal();
        toast(`shared ${r.count} song${r.count === 1 ? '' : 's'} with ${r.sent.length} ${r.sent.length === 1 ? 'person' : 'people'}`
              + (r.failed.length ? ` — ${r.failed.length} could not be reached` : '')
              + (r.prepFailed ? ` — ${r.prepFailed} song${r.prepFailed === 1 ? '' : 's'} could not be prepared` : ''));
        if(opts && opts.after) try{ opts.after(); }catch(_){}
      };
    });
  }

  function _row(t, inLib){
    return `<div class="track msh-track" data-sha="${t.s}">
      <button class="track-play" data-sha="${t.s}" aria-label="Play">${icon('play')}</button>
      <span class="track-name">${E(t.n)}</span>
      <span class="track-meta">🔒 ${mb(t.z)}</span>
      <button class="track-addlib${inLib ? ' on' : ''}" data-sha="${t.s}" ${inLib ? 'disabled' : ''}
        title="${inLib ? 'Already in your library' : 'Add to my library'}" aria-label="${inLib ? 'In your library' : 'Add to my library'}">${icon(inLib ? 'check' : 'plus')}</button>
      <button class="track-dl" data-sha="${t.s}" title="Save a copy to your files (decrypts first)" aria-label="Save a copy">${icon('download')}</button>
    </div>`;
  }

  /* SHARED WITH ME — the decision, and nothing else.
   *
   * It used to be an inbox of cards that opened into a screen per share, with its own back button
   * and its own "Add N to my library". That is two places to learn for something that is, to the
   * person receiving it, just a playlist somebody sent. Now: accept and it becomes a playlist chip;
   * reject and it goes away and stays away. Nothing here plays anything, because deciding whether
   * to keep a stranger's playlist is not the moment to start the music. */
  async function renderIn(el, ctx){
    if(!_boot() || !el) return;
    ctx = ctx || {};
    el.className = 'music-list msh';
    if(!_in) el.innerHTML = '<div class="spinner"></div>';
    else _paintIn(el, ctx);
    try{ await loadIn(); }catch(_){}
    if(el.isConnected) _paintIn(el, ctx);
  }
  function _paintIn(el, ctx){
    const waiting = pendingShares(), have = acceptedShares();
    el.innerHTML = `<div class="music-head"><div class="music-head-primary">
        <button class="btn btn-ghost small" id="msh-refresh">${icon('refresh')}Refresh</button></div>
        <span class="music-count muted small">${waiting.length
            ? `${waiting.length} waiting for an answer`
            : (have.length ? `${have.length} accepted · they are playlists now` : 'nothing waiting')}</span></div>`
      + (waiting.length ? waiting.map(s => `<div class="msh-card msh-offer" data-key="${E(s.key)}">
            <b>${E(s.body.name)}</b>
            <span class="muted small">from ${E(who(s.from))} · ${trackCount(s.body)} song${trackCount(s.body) === 1 ? '' : 's'}</span>
            <div class="msh-offer-act">
              <button class="btn btn-neon small msh-yes" data-key="${E(s.key)}">Accept</button>
              <button class="btn btn-ghost small msh-no" data-key="${E(s.key)}">Reject</button>
            </div></div>`).join('')
        : `<div class="empty">${have.length
              ? 'Nothing new. Accepted shares are in the playlist bar.'
              : `Nothing has been shared with you yet${_inOk ? '' : ' — or the relays did not answer; try Refresh'}.`}</div>`);
    el.onclick = async ev => {
      const b = ev.target.closest && ev.target.closest('button'); if(!b || !el.contains(b)) return;
      if(b.id === 'msh-refresh'){ b.disabled = true; await loadIn(); if(el.isConnected) _paintIn(el, ctx); return; }
      const yes = b.classList.contains('msh-yes'), no = b.classList.contains('msh-no');
      if(!yes && !no) return;
      const sh = _in && _in.get(b.dataset.key); if(!sh) return;
      decide(b.dataset.key, yes);
      toast(yes ? `“${(sh.body && sh.body.name) || 'Shared music'}” is in your playlists` : 'rejected');
      // Accepting SHOWS it: the chip appeared in a bar that may be scrolled off screen, and "it
      // says it accepted and nothing happened" is the same complaint in a different hat. The host
      // selects the chip when it can; on its own this module still draws the playlist it just made.
      if(yes){ if(ctx.open) ctx.open(b.dataset.key); else if(el.isConnected) renderShared(b.dataset.key, el, ctx); return; }
      if(el.isConnected) _paintIn(el, ctx);
    };
  }

  /* AN ACCEPTED SHARE, DRAWN AS A PLAYLIST. Same header as the library's own (Shuffle, Refresh, a
   * count), same track rows — the only thing that marks it out is one line saying whose it is and
   * that adding a song keeps it. */
  async function renderShared(key, el, ctx){
    if(!_boot() || !el) return;
    ctx = ctx || {};
    el.className = 'music-list msh';
    el.innerHTML = '<div class="spinner"></div>';
    if(!_in) { try{ await loadIn(); }catch(_){} }
    const cur = _in && _in.get(key);
    if(!cur){
      el.innerHTML = `<div class="empty">This share is no longer available — whoever shared it has stopped.</div>`;
      return;
    }
    let tracks;
    try{ tracks = await tracksOf(cur); }
    catch(e){ el.innerHTML = `<div class="empty">Couldn’t open this playlist: ${E((e && e.message) || e)}</div>`; return; }
    if(!el.isConnected) return;
    register(cur, tracks);
    const lib = new Set((PC.musicLibrary && PC.musicLibrary()) || []);
    const missing = tracks.filter(t => !lib.has(t.s)).length;
    const order = tracks.map(t => t.s);
    el.innerHTML = `<div class="music-head">
        <div class="music-head-primary">
          <button class="btn btn-neon small" id="msh-shuffle"${tracks.length ? '' : ' disabled'}>${icon('shuffle')}Shuffle</button>
          <button class="btn btn-ghost small" id="msh-refresh">${icon('refresh')}Refresh</button>
          <button class="btn btn-ghost small" id="msh-addall"${missing ? '' : ' disabled'}>${icon('plus')}${missing ? `Keep ${missing}` : 'Kept'}</button>
        </div>
        <span class="music-count muted small">${tracks.length} song${tracks.length === 1 ? '' : 's'} · from ${E(who(cur.from))}</span>
        <span class="msh-note muted small">These play from ${E(who(cur.from))}’s copy while it is shared. Keep a song and it stays yours even if the share is stopped.</span>
      </div>` + tracks.map(t => _row(t, lib.has(t.s))).join('');
    el.onclick = async ev => {
      const b = ev.target.closest && ev.target.closest('button'); if(!b || !el.contains(b)) return;
      if(b.id === 'msh-refresh'){ b.disabled = true; await loadIn(); if(el.isConnected) renderShared(key, el, ctx); return; }
      if(b.id === 'msh-shuffle'){
        const M = PC.MusicPlayer, pick = order[Math.floor(Math.random() * order.length)];
        if(!order.length) return;
        if(M){ M.queue = order.slice(); M.shuffle = true; M.play(pick); }
        else if(ctx.play) ctx.play(pick, order);
        return;
      }
      if(b.classList.contains('track-play')){
        /* The SHARE becomes the queue, in its order — the rule a playlist follows. */
        const M = PC.MusicPlayer;
        if(ctx.play) ctx.play(b.dataset.sha, order);
        else if(M){ M.queue = order.slice(); M.shuffle = false; M.play(b.dataset.sha); }
        return;
      }
      if(b.classList.contains('track-dl')){
        const t = tracks.find(x => x.s === b.dataset.sha); if(!t) return;
        try{ const pt = await plain(t.s); await PC.saveBlobAs(new Blob([pt], { type: t.m }), t.n + '.' + (t.e || 'mp3')); }
        catch(e){ toast('couldn’t save that song: ' + ((e && e.message) || e)); }
        return;
      }
      const pick = b.id === 'msh-addall' ? tracks : b.classList.contains('track-addlib') ? tracks.filter(t => t.s === b.dataset.sha) : null;
      if(!pick) return;
      b.disabled = true;
      const was = b.innerHTML;
      const r = await addToLibrary(cur, pick, st => { if(b.id === 'msh-addall' && st.total) b.textContent = `Keeping ${st.done} / ${st.total}…`; });
      b.innerHTML = was;
      toast(r.added ? `kept ${r.added} song${r.added === 1 ? '' : 's'}` + (r.failed ? ` — ${r.failed} failed: ${r.error}` : '')
                    : r.skipped && !r.failed ? 'already yours' : 'not kept — ' + (r.error || 'unknown error'));
      if(ctx.libraryChanged) try{ ctx.libraryChanged(); }catch(_){}
      if(el.isConnected) renderShared(key, el, ctx);
    };
  }

  async function renderOut(el, ctx){
    if(!_boot() || !el) return;
    ctx = ctx || {};
    el.className = 'music-list msh';
    if(!_out) el.innerHTML = '<div class="spinner"></div>';
    else _paintOut(el, ctx);
    try{ await loadOut(); }catch(_){}
    if(el.isConnected) _paintOut(el, ctx);
  }
  function _paintOut(el, ctx){
    const shares = outShares();
    el.innerHTML = `<div class="music-head"><div class="music-head-primary">
        <button class="btn btn-ghost small" id="msh-refresh">${icon('refresh')}Refresh</button></div>
        <span class="music-count muted small">${shares.length} share${shares.length === 1 ? '' : 's'} you made</span></div>`
      + (shares.length ? shares.map(s => {
          const live = [...s.to.entries()].filter(([, r]) => !r.dead).map(([pk]) => pk);
          return `<div class="msh-out" data-id="${E(s.id)}">
            <div class="msh-out-head"><b>${E((s.body && s.body.name) || 'Shared music')}</b>
              <span class="muted small">${s.body ? trackCount(s.body) + ' song' + (trackCount(s.body) === 1 ? '' : 's') : 'unreadable here'}</span></div>
            <div class="msh-people">${live.map(pk => `<span class="msh-person">${E(who(pk))}
              <button class="msh-unshare" data-id="${E(s.id)}" data-pk="${pk}" title="Stop sharing with ${E(who(pk))}" aria-label="Stop sharing with ${E(who(pk))}">${icon('close', 'x-ic')}</button></span>`).join('')}</div>
            <div class="row" style="gap:8px;flex-wrap:wrap">
              <button class="btn btn-ghost small msh-addpeople" data-id="${E(s.id)}"${s.body ? '' : ' disabled'}>${icon('plus')}Add people</button>
              <button class="btn btn-ghost small msh-revoke" data-id="${E(s.id)}">Stop sharing</button>
            </div></div>`; }).join('')
        : `<div class="empty">You haven’t shared any music${_outOk ? '' : ' — or the relays did not answer; try Refresh'}. Pick a playlist and press Share.</div>`);
    el.onclick = async ev => {
      const b = ev.target.closest && ev.target.closest('button'); if(!b || !el.contains(b)) return;
      if(b.id === 'msh-refresh'){ b.disabled = true; await loadOut(); if(el.isConnected) _paintOut(el, ctx); return; }
      const sh = _out && _out.get(b.dataset.id); if(!sh) return;
      const name = (sh.body && sh.body.name) || 'this share';
      if(b.classList.contains('msh-revoke') || b.classList.contains('msh-unshare')){
        const one = b.classList.contains('msh-unshare') ? [b.dataset.pk] : null;
        const q = one ? `Stop sharing “${name}” with ${who(one[0])}?` : `Stop sharing “${name}” with everyone on it?`;
        if(!await PC.uiConfirm(q + '\n\nThey will no longer see it. Songs they already added to their own library stay theirs — stopping a share cannot take those back.',
                               { ok:'Stop sharing', danger:true })) return;
        b.disabled = true;
        const r = await revoke(sh.id, one);
        toast(r.ok ? 'stopped sharing' + (r.released ? ` — ${r.released} shared cop${r.released === 1 ? 'y' : 'ies'} removed from the server` : '')
                   : 'not everyone could be updated — try again');
        if(el.isConnected) _paintOut(el, ctx);
        return;
      }
      if(b.classList.contains('msh-addpeople')){
        const txt = await PC.uiPrompt('Add people to “' + name + '” (npub or name@domain, separated by spaces)', { ok:'Share' });
        if(!txt) return;
        const { pks, bad } = await resolveRecipients(txt);
        if(bad.length){ toast('couldn’t find: ' + bad.join(', ')); return; }
        const r = await addRecipients(sh.id, pks);
        toast(r.ok ? `shared with ${r.sent.length} more` : (r.error || 'not everyone could be reached'));
        if(el.isConnected) _paintOut(el, ctx);
      }
    };
  }

  /* The Music app's chips.
   *
   * An ACCEPTED share is a playlist chip like any other — same shape, same place in the bar, opened
   * the same way. Only the two inbox views are reserved ids. "Shared with me" carries the number of
   * shares still WAITING on an answer, so it reads as a thing to deal with rather than a folder;
   * when nothing is waiting it is not a badge, it is just where the offers arrive.
   *
   * A share's chip id is its key (`<from>:<id>`), which is why `isView` has to recognise those too:
   * the bar hands the id back to renderView, and an accepted share draws its own tracks. */
  const V_IN = '__shared_in', V_OUT = '__shared_out';
  const isShare = id => !!id && !!_in && _in.has(id) && accepted().has(id);
  const isView = id => id === V_IN || id === V_OUT || isShare(id);
  function barHTML(cur, real){
    const waiting = pendingShares().length;
    const chip = (id, label, c) => `<button class="ma-pl${cur === id ? ' on' : ''}" data-pl="${E(id)}">${E(label)}${c ? ` <span class="ma-pln">${c}</span>` : ''}</button>`;
    return acceptedShares().map(s => chip(s.key, s.body.name, trackCount(s.body))).join('')
      + `<span class="ma-plsp"></span>${chip(V_IN, '📥 Shared with me', waiting)}${chip(V_OUT, '📤 Shared by me', 0)}`
      + (isView(cur) ? '' : `<button class="ma-pl ma-plshare" id="ma-plshare" title="${real ? 'Share this playlist with other people' : 'Share the songs on screen with other people'}">🔗 Share</button>`);
  }
  const renderView = (id, el, ctx) => id === V_IN ? renderIn(el, ctx)
    : id === V_OUT ? renderOut(el, ctx) : renderShared(id, el, ctx);

  window.PCMusicShare = {
    isView, isShare, barHTML, renderView, renderShared,
    // acceptance
    decide, acceptedShares, pendingShares, loadDecisions, decisions,
    // data
    loadIn, loadOut, inShares, outShares, tracksOf, share, addRecipients, revoke, addToLibrary, refIds,
    resolveRecipients, inCount: () => pendingShares().length,
    // player
    register, meta, plain,
    // ui
    openShareDialog, renderIn, renderOut, onChange(fn){ _watchers.add(fn); return () => _watchers.delete(fn); },
    // pure, exported for tests
    deriveKey, contentIV, seal, open, dTag, parseD, cleanItem, cleanBody, libraryEntry, newestBy, b64, unb64,
    _D: D_PFX, _L: L_TAG, _BODY_MAX: BODY_MAX,
  };
})();

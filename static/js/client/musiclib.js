/* musiclib.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_musiclibMod()`)
 * while it boots — the music player (musicplayer.js), the Android car/Bluetooth/media-button resume path,
 * Files, Music and the DM screen all call into it synchronously and must never wait on a network load.
 * MusicOffline is returned as an object and reached from app.js through `_lzProxy`. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCMusicLibFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.ME, S.NO_IMAGES, S._blobHave, S._blobSizes, S._musicPl, S.signer
  const {
    FilesIdx, MusicPlayer, _ENC_MARK, _dimAttrs, _driveDecrypt, _plTracks, _shaFromUrl,
    _standalone, _trackUrlOrder, _trackUrls, blossomPicker, enc, encFileUrl, gifPicker,
    mediaServer, openMenuPopover, pickSyncedFile, toast, uploadBlob,
  } = dep;

  // ---- Music: Opus-compressed + AES-256-GCM-encrypted tracks in the Music folder ----------------------
  // raw audio → server Opus transcode (compression) → AES-GCM encrypt (random per-file key) → upload the
  // CIPHERTEXT to the user's Blossom → store the NIP-44 self-wrapped key in the index. Playback fetches
  // the ciphertext, unwraps the key, decrypts in-browser → object URL. So Blossom only ever holds opaque
  // ciphertext; only the owner's signer can unwrap it.
  function _u8b64(u8){ let s='',C=0x8000; for(let i=0;i<u8.length;i+=C) s+=String.fromCharCode.apply(null,u8.subarray(i,i+C)); return btoa(s); }
  function _b64u8(b){ const s=atob(b),u=new Uint8Array(s.length); for(let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }
  async function _aesEncrypt(plain){ const key=crypto.getRandomValues(new Uint8Array(32)),iv=crypto.getRandomValues(new Uint8Array(12));
    const ck=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['encrypt']);
    const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},ck,plain)); return {ct,key,iv}; }
  async function _aesDecrypt(ct,key,iv){ const ck=await crypto.subtle.importKey('raw',key,'AES-GCM',false,['decrypt']);
    return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv},ck,ct)); }
  // SCALABLE encryption (Phase 2.5): ONE master key (wrapped once) + the IV prepended to the blob. For
  // tracks the IV is DERIVED from the content (sha256(plain)[:12]) → identical input → identical
  // ciphertext/hash → Blossom DEDUP + resumable import. For the index a random IV is used (it changes).
  /* ---- Shareable encrypted attachments -------------------------------------------------------
   *
   * Blossom stores bytes and serves them back by sha256. It never looks inside them, and it has no
   * read authorization at all — `GET /<sha>` and `GET /list/<pubkey>` both answer anyone, so a file
   * attached to a DM was world-readable AND enumerable by the sender's npub. The message was
   * encrypted; the picture in it was not.
   *
   * The drive already encrypts before upload, but under the user's MASTER key — which is exactly
   * what makes it unshareable, since nobody else can derive it. So this uses a fresh random AES-GCM
   * key per file and hands that key to the recipient out of band: in the URL FRAGMENT, inside the
   * NIP-44-encrypted DM. A fragment is never transmitted to a server, so even pasting the link into
   * a browser cannot leak the key to the host — and the host is storing ciphertext regardless.
   *
   * Deliberately OPT-IN. A client that doesn't understand the marker (Damus, Amethyst) shows a link
   * to bytes it can't render, so encrypting silently would break conversations with people not on
   * this software; the composer's 🔒 makes it a per-file choice.
   *
   * Same on-the-wire layout as the drive (iv ‖ ciphertext, AES-GCM) so _masterEncrypt/_masterDecrypt
   * are reused as-is. The IV is random here, NOT content-derived: dedup is meaningless with a
   * per-file key, and a fresh key with a fresh IV is the conservative pairing.
   */
  const _b64u = b => btoa(String.fromCharCode(...b)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  function _b64uDec(s){
    s = String(s||'').replace(/-/g,'+').replace(/_/g,'/');
    while(s.length % 4) s += '=';
    const raw = atob(s); const out = new Uint8Array(raw.length);
    for(let i=0;i<raw.length;i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  /* Encrypt → upload → return a self-contained reference. `keep` because this blob is the only copy
     the recipient will ever have (the age sweep must not delete it out from under them), `noMirror`
     because a DR mirror would hand our ciphertext to a third-party server for no benefit. */
  async function uploadSharedEnc(file, statEl, options){
    const setS = t => { if(statEl) statEl.textContent = t; };
    const key = crypto.getRandomValues(new Uint8Array(32));
    const chunkBytes = 4 * 1024 * 1024 - 28;
    const meta = { k:_b64u(key), m:file.type||'application/octet-stream', n:file.name||'' };
    const upload = async bytes => {
      const blob = await _masterEncrypt(key, bytes);
      return uploadBlob(new File([blob], (file.name||'file')+'.enc',
        {type:'application/octet-stream'}), {noMirror:true, keep:true, noCompress:true});
    };
    let url;
    if(options && options.chunked && file.size > chunkBytes){
      if(file.size > chunkBytes * 4096) throw new Error('This file exceeds the shared-file size limit');
      const chunks = [];
      let origin = '';
      for(let start = 0; start < file.size; start += chunkBytes){
        setS('uploading ' + Math.floor(start / file.size * 100) + '%…');
        const bytes = new Uint8Array(await file.slice(start, start + chunkBytes).arrayBuffer());
        const part = await upload(bytes);
        const parsed = new URL(part, location.href);
        const sha = (parsed.pathname.match(/(?:^|\/)([0-9a-f]{64})(?:\.[^/]*)?$/i)||[])[1];
        if(!sha) throw new Error('The media server returned an invalid file address');
        const base = parsed.origin + parsed.pathname.slice(0, parsed.pathname.lastIndexOf('/') + 1);
        if(origin && origin !== base) throw new Error('The media server changed during the upload; please retry');
        origin = base;
        chunks.push({sha:sha.toLowerCase(), size:bytes.length});
      }
      url = await upload(new TextEncoder().encode(JSON.stringify({v:1,size:file.size,chunks})));
      const manifestURL = new URL(url, location.href);
      if(manifestURL.origin + manifestURL.pathname.slice(0, manifestURL.pathname.lastIndexOf('/') + 1) !== origin)
        throw new Error('The media server changed during the upload; please retry');
      meta.c = 1;
    }else{
      setS('uploading…');
      url = await upload(new Uint8Array(await file.arrayBuffer()));
    }
    setS('');
    // Strip any fragment uploadBlob may have produced before appending ours, and base64url the whole
    // descriptor as ONE token: no separator can then collide with linkify's trailing-punctuation trim.
    return url.split('#')[0] + _ENC_MARK + _b64u(new TextEncoder().encode(JSON.stringify(meta)));
  }

  /* Pick from the drive / GIF search INTO a DM, honouring 🔒.
   *
   * The drive picker inserts a URL to a file that is already on Blossom in the clear, so with the
   * lock lit it used to put a world-readable link straight into an "encrypted" conversation — which
   * is exactly how a plaintext PNG uploaded eleven hours earlier ended up rendering fine on a client
   * that cannot decrypt anything. Re-uploading it encrypted keeps the LINK private; it cannot
   * un-publish the copy already on the drive, and pretending otherwise would be the worse failure,
   * so the toast says so.
   *
   * A GIF is somebody else's URL on somebody else's server. There is nothing to encrypt and no
   * honest way to make one private, so the lock says plainly that this one isn't. */
  /* ONE upload loop for every way a file reaches a message — 📎 from the device, a Music track, a
     file out of a synced folder. The 🔒 choice is read HERE, per file, so no source can quietly
     skip it; the loop used to be copied into the thread and the new-message dialog separately.
     Ends with an 'input' event, which is what the preview strip, the draft save and the Send
     button all hang off (see dmPickMedia). `st` is an optional status line. */
  async function dmAttachFiles(inp, files, st){
    const encOn=dmEncOn(); let n=0;
    for(let i=0;i<files.length;i++){
      const say=t=>{ if(st) st.textContent=t; else if(t) toast(t); };
      try{
        if(encOn || files.length>1) say(`${encOn?'encrypting':'uploading'} ${i+1}/${files.length}…`);
        const url=encOn ? await uploadSharedEnc(files[i], st) : await uploadBlob(files[i]);
        inp.value+=(inp.value && !/\s$/.test(inp.value) ? ' ' : '')+url; n++;
      }catch(err){ say('upload failed: '+((err&&err.message)||err)); if(!st) continue; return n; }
    }
    if(st) st.textContent='';
    inp.dispatchEvent(new Event('input', {bubbles:true}));
    return n;
  }
  /* The 📎 menu, shared by the thread and the new-message dialog so the two cannot drift. */
  function dmAttachMenu(anchor, inp, fileInput, st){
    const rows=[['file','📎 File from this device']];
    if(!_standalone()) rows.push(['synced','🔄 From a synced folder']);
    if(window.PCWebxdc&&PCWebxdc.attach) rows.push(['webxdc','🎮 Multiplayer mini app']);
    openMenuPopover(anchor, rows, async a=>{
      if(a==='file') fileInput.click();
      else if(a==='webxdc') PCWebxdc.attach(inp);
      else if(a==='synced'){
        const f=await pickSyncedFile();
        if(f){ if(!dmEncOn()) toast('sending a readable copy of '+f.name+' — turn on 🔒 to encrypt it to this conversation');
               await dmAttachFiles(inp, [f], st); }
      }
    });
  }
  /* A drive file stored ENCRYPTED (every Music track) is ciphertext under its own key: sent as a
     link it opens as nothing for the person you sent it to. Decrypt it here and attach the result
     like a file from the device. */
  async function _dmDecryptedDriveFile(sha, name, type){
    const local=await encFileUrl(sha, type);
    if(!local) throw new Error('it could not be decrypted on this device');
    const blob=await (await fetch(local)).blob();
    return new File([blob], name || 'file', { type: type || blob.type || '' });
  }
  function dmPickMedia(inp, st){
    return () => blossomPicker(inp, async ({url, type, ext, sha, enc:isEnc, name}) => {
      if(isEnc && sha){
        try{
          toast('decrypting '+(name||'that file')+'…');
          const f=await _dmDecryptedDriveFile(sha, name, type);
          if(!dmEncOn()) toast('sending a readable copy of '+(name||'that file')+' — turn on 🔒 to encrypt it to this conversation');
          await dmAttachFiles(inp, [f], st);
        }catch(err){ toast("couldn't attach that file: "+((err&&err.message)||err)); }
        return;
      }
      // Dispatch 'input' exactly as the picker's own insert path does. Calling the attachment-strip
      // sync directly instead looks equivalent and isn't: the composer hangs its textarea autogrow,
      // its DRAFT SAVE and the Send button's enabled state off that same event, so a file picked
      // this way left the send button dim and the draft unsaved.
      const add = u => { inp.value += (inp.value && !/\s$/.test(inp.value) ? ' ' : '') + u;
                         inp.dispatchEvent(new Event('input', {bubbles:true})); };
      const _need = ext && !/\.[a-z0-9]{1,8}$/i.test(String(url).split('?')[0]);
      const plain = url + (_need ? ('.' + ext) : '');
      if(!dmEncOn()){ add(plain); return toast('attached'); }
      // Already-encrypted drive content (octet-stream) must not be wrapped a second time: the
      // recipient would peel off one layer and be left holding master-key ciphertext.
      if(/octet-stream/i.test(type || '')){
        toast('That file is already encrypted to you — sent as a link only you can open.');
        return add(plain);
      }
      toast('encrypting…');
      try{
        add(await encryptExistingUrl(url, type));
        toast('🔒 Sent encrypted. The original copy stays on your drive and is still public there.');
      }catch(err){
        toast("Couldn't encrypt that file: " + ((err && err.message) || err));
      }
    }, {
      /* MUSIC is an encrypted folder, and pickers hide those unless the caller can decrypt what it
         picks — this one can (above), so the Music folder is offered here too. */
      allowEncrypted: true,
    });
  }

  function dmPickGif(inp){
    return () => {
      if(dmEncOn()) toast('GIFs are links to another server — this one is not encrypted.');
      gifPicker(inp);
    };
  }

  /* Is 🔒 on for DM attachments? One reader, because the lock has to mean the same thing to every
     path that can put media in a message — the 📎 picker, a pasted image, the 🌸 drive picker and
     the 🎬 GIF search. It originally only governed 📎, so the drive picker happily inserted a
     world-readable link while the lock was lit. */
  function dmEncOn(){ return !!ClientSettings.get('dmEncryptAtts'); }

  /* Re-upload an EXISTING blob as an encrypted one. Used by the drive picker: those files are
     already sitting in the clear, so this stops the DM from carrying a public link — it does NOT
     un-publish the original, and the caller says so out loud rather than implying otherwise. */
  async function encryptExistingUrl(url, mime){
    const r = await fetch(url);
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const b = await r.blob();
    const raw = url.split('?')[0].split('/').pop() || 'file';
    // decodeURIComponent throws on a stray '%' — a filename is not guaranteed to be well-formed
    // percent-encoding, and throwing here would take the whole picker down over a cosmetic label.
    let name; try{ name = decodeURIComponent(raw); }catch(_){ name = raw; }
    return await uploadSharedEnc(new File([b], name, { type: mime || b.type || 'application/octet-stream' }));
  }

  /* {url, key, mime, name} for a reference, or null if this isn't one / is malformed. */
  function encAttParse(ref){
    const s = String(ref||''); const i = s.indexOf(_ENC_MARK);
    if(i < 0) return null;
    try{
      const m = JSON.parse(new TextDecoder().decode(_b64uDec(s.slice(i + _ENC_MARK.length))));
      if(!m || !m.k) return null;
      const key = _b64uDec(m.k);
      if(key.length !== 32) return null;
      return { url:s.slice(0,i), key, mime:String(m.m||'application/octet-stream'), name:String(m.n||'') };
    }catch(_){ return null; }
  }

  /* Fetch the ciphertext, decrypt in the page, hand back an object URL. Cached per reference: a
     thread re-renders on every arriving message and re-downloading each attachment every time would
     be both slow and pointless. Bounded so a long conversation can't pin every attachment it ever
     showed in memory — object URLs hold their blob until revoked. */
  const _encAttUrls = new Map();
  const _ENC_ATT_MAX = 24;
  async function encAttObjectUrl(ref){
    const ck = ref.url + '|' + _b64u(ref.key);
    if(_encAttUrls.has(ck)) return _encAttUrls.get(ck);
    const r = await fetch(ref.url);
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const plain = await _masterDecrypt(ref.key, new Uint8Array(await r.arrayBuffer()));
    const obj = URL.createObjectURL(new Blob([plain], { type: ref.mime }));
    _encAttUrls.set(ck, obj);
    // Evict oldest-first, but NEVER an object URL something on the page is still pointing at:
    // revoking one breaks that <img>/<video> permanently, and a media-heavy thread can easily hold
    // more than the cap on screen at once. Skipping those bounds the cache by what is actually
    // visible instead of by a number that has nothing to do with the conversation.
    if(_encAttUrls.size > _ENC_ATT_MAX){
      const inUse = u => { try{ return !!document.querySelector(`[src="${u}"],[href="${u}"]`); }catch(_){ return true; } };
      for(const k of [..._encAttUrls.keys()]){
        if(_encAttUrls.size <= _ENC_ATT_MAX) break;
        if(k === ck || inUse(_encAttUrls.get(k))) continue;
        try{ URL.revokeObjectURL(_encAttUrls.get(k)); }catch(_){}
        _encAttUrls.delete(k);
      }
    }
    return obj;
  }

  /* Swap every 🔒 placeholder linkify left behind for the real media. A separate pass because a
     bubble body is built as a STRING and rendered once — the same reason mentions need
     decorateProfiles — and because decrypting is async while rendering is not. */
  async function decorateEncAtts(root){
    const nodes = [...((root||document).querySelectorAll('.encatt[data-encatt]'))];
    // Concurrently: each attachment is an independent fetch+decrypt, and doing them in sequence made
    // one slow blob hold up every later one in the thread. The claim below happens synchronously in
    // every callback before the first await, so the map can't double-start any of them.
    await Promise.all(nodes.map(async n => {
      const _refUrl = n.dataset.encatt;
      const ref = encAttParse(_refUrl);
      // The size hints every other media path emits (see _dimAttrs / the layout-stable CSS block).
      // Without them a decrypted <video> is sized from its INTRINSIC dimensions, which in a WebView do
      // not exist until playback starts — so the bubble, being width:fit-content, resized itself around
      // the clip on the first frame. Remembered under the ENCRYPTED REFERENCE, not the blob: URL that
      // carries the bytes, so the box is right the next time the same attachment is opened.
      const _dim = `${_dimAttrs(_refUrl)} data-dimkey="${enc(_refUrl)}"`;
      // Claim it before awaiting: a re-render during the fetch would otherwise start a second
      // decrypt of the same blob for the same node.
      n.removeAttribute('data-encatt');
      if(!ref){ n.innerHTML = '🔒 <span class="muted small">encrypted attachment (unreadable link)</span>'; return; }
      const label = ref.name || 'encrypted file';
      // Data saver means DON'T SPEND THE BYTES. Deciding after the fetch would have downloaded and
      // decrypted the whole thing first and only then declined to show it — the one thing the mode
      // exists to prevent. Offer it instead, and decrypt on the tap.
      if(S.NO_IMAGES && /^(image|video)\//.test(ref.mime)){
        n.innerHTML = `<span class="encatt-ds" role="button" tabindex="0">🔒 ${enc(label)} — tap to decrypt</span>`;
        const go = async () => {
          n.textContent = '🔒 decrypting…';
          try{
            const obj = await encAttObjectUrl(ref);
            n.innerHTML = /^video\//.test(ref.mime)
              ? `<video class="m" src="${enc(obj)}"${_dim} controls preload="metadata" playsinline></video>`
              : `<img class="m" src="${enc(obj)}"${_dim} alt="${enc(label)}">`;
            n.classList.add('done');
          }catch(e){ n.innerHTML = `🔒 <span class="muted small">${enc(label)} — ${enc((e&&e.message)||'failed')}</span>`; }
        };
        const el = n.firstElementChild;
        el.onclick = go;
        el.onkeydown = ev => { if(ev.key === 'Enter' || ev.key === ' '){ ev.preventDefault(); go(); } };
        return;
      }
      try{
        const obj = await encAttObjectUrl(ref);
        const t = ref.mime;
        if(/^image\//.test(t)){
          n.innerHTML = `<img class="m" src="${enc(obj)}"${_dim} alt="${enc(label)}" loading="lazy">`;
        } else if(/^video\//.test(t)){
          n.innerHTML = `<video class="m" src="${enc(obj)}"${_dim} controls preload="metadata" playsinline></video>`;
        } else if(/^audio\//.test(t)){
          n.innerHTML = `<audio src="${enc(obj)}" controls preload="none"></audio>`;
        } else {
          n.innerHTML = `<a href="${enc(obj)}" download="${enc(label)}">🔒 ${enc(label)}</a>`;
        }
        n.classList.add('done');
      }catch(e){
        // Say WHICH failure it was. "Couldn't open" over a blob that was swept, a link that lost its
        // fragment on the way through another client, and a genuine network error are three different
        // problems and only one of them is worth retrying.
        const why = /decrypt|operation/i.test((e&&e.message)||'') ? 'wrong key or damaged file'
                  : ((e&&e.message)||'download failed');
        n.innerHTML = `🔒 <span class="muted small">${enc(label)} — ${enc(why)}</span>`;
      }
    }));
  }

  async function _contentIV(plain){ return new Uint8Array(await crypto.subtle.digest('SHA-256', plain)).slice(0,12); }
  /* UNWRAP THE DRIVE KEY, OR SAY WHAT WENT WRONG.
   *
   * This used to be one expression: decrypt, JSON.parse, take `.k`, base64-decode. Every step can
   * come back empty without throwing — a signer that answers with the wrong thing, a wrapped key
   * from another account, a truncated value — and the result is a Uint8Array of the wrong length
   * that is then handed to crypto.subtle.importKey, which rejects with
   *
   *     Failed to execute 'importKey' on 'SubtleCrypto'
   *
   * from wherever the key was next used. That message names nothing: not the drive key, not this
   * device, not the fact that nothing is damaged. Checked here, at the one place the key is made, so
   * the failure says what it is and every caller inherits it.
   *
   * It NEVER falls back to minting one. A new key decrypts nothing that already exists, and writing
   * it over the old one destroys the only way back — see the guard above this. */
  async function _unwrapMK(wrapped){
    let raw;
    try{ raw = await S.signer.nip44dec(S.ME.pubkey, wrapped); }
    catch(e){ throw new Error('this device could not unwrap your drive key (' + ((e && e.message) || e)
                              + '). Your files are unchanged — check you are signed in as the same account.'); }
    let k = null;
    try{ k = JSON.parse(raw).k; }catch(_){ }
    const mk = k ? _b64u8(k) : new Uint8Array(0);
    if(mk.length !== 32){
      /* STRUCTURALLY WRONG, as opposed to temporarily unavailable — and the caller must be able to
       * tell them apart. This value can never become a key however many times it is tried, so it is
       * safe to throw away and re-fetch; a signer that merely did not answer (locked phone, denied
       * prompt, bunker offline) throws above WITHOUT this mark, because discarding a good key over a
       * momentary refusal is how a device ends up minting a fresh one and locking itself out of its
       * own files. */
      const e = new Error('your drive key came back the wrong size (' + mk.length + ' bytes, expected 32), '
                          + 'so this device cannot read your encrypted files. Nothing has been changed or lost — '
                          + 'the key on the server is untouched.');
      e.badKey = true;
      throw e;
    }
    return mk;
  }
  /* A DRIVE KEY CROSSES MORE REALMS THAN ORDINARY WEBCRYPTO INPUT: browser-extension messages,
   * Electron/native bridges, remote signers and persisted JSON. Structured clone normally keeps a
   * Uint8Array, but JSON adapters turn it into an Array, a Node Buffer envelope, numeric-keyed
   * object or base64 string; some adapters return an already imported CryptoKey. Passing any of
   * those except BufferSource straight to importKey is the reported
   * `SubtleCrypto.importKey: Argument 2 is not an object`, before AES can even say wrong key.
   *
   * Normalize at the ONE crypto boundary, strictly. A malformed value never mints a replacement
   * and never reaches a drive save; it fails with the same badKey marker used by wrapped-key
   * recovery. CryptoKeys remain non-extractable and are reused without exporting them. */
  function _masterKeyInput(mk){
    const cryptoKey = mk && mk.type==='secret' && mk.algorithm && mk.algorithm.name==='AES-GCM'
      && mk.constructor && mk.constructor.name==='CryptoKey';
    if(cryptoKey) return mk;
    let u=null;
    const bytes=a=>Array.isArray(a) && a.length===32
      && a.every(n=>Number.isInteger(n)&&n>=0&&n<=255) ? Uint8Array.from(a) : null;
    if(mk instanceof Uint8Array) u=mk;
    else if(typeof ArrayBuffer!=='undefined' && mk instanceof ArrayBuffer) u=new Uint8Array(mk);
    else if(typeof ArrayBuffer!=='undefined' && ArrayBuffer.isView && ArrayBuffer.isView(mk))
      u=new Uint8Array(mk.buffer,mk.byteOffset,mk.byteLength);
    else if(Array.isArray(mk)) u=bytes(mk);
    else if(mk && Array.isArray(mk.data)) u=bytes(mk.data); // Buffer.toJSON()
    else if(typeof mk==='string'){
      try{ const s=mk.replace(/-/g,'+').replace(/_/g,'/'); u=_b64u8(s+'==='.slice((s.length+3)%4)); }
      catch(_){ u=null; }
    }else if(mk && typeof mk==='object'){
      const keys=Object.keys(mk).filter(k=>/^\d+$/.test(k)).sort((a,b)=>+a-+b);
      if(keys.length) u=bytes(keys.map(k=>mk[k]));
    }
    const valid=u && u.length===32;
    if(!valid){ const e=new TypeError('drive master key is not 32 raw bytes'); e.badKey=true; throw e; }
    return u;
  }
  async function _masterCryptoKey(mk, usage){
    const input=_masterKeyInput(mk);
    if(input && input.constructor && input.constructor.name==='CryptoKey'){
      if(!input.usages || !input.usages.includes(usage)) throw new TypeError('drive master key cannot '+usage);
      return input;
    }
    return crypto.subtle.importKey('raw',input,'AES-GCM',false,[usage]);
  }
  async function _masterEncrypt(mk, plain, iv){ iv = iv || crypto.getRandomValues(new Uint8Array(12));
    const ck=await _masterCryptoKey(mk,'encrypt');
    const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},ck,plain));
    const out=new Uint8Array(12+ct.length); out.set(iv,0); out.set(ct,12); return out; }
  async function _masterDecrypt(mk, blob){ const iv=blob.slice(0,12), ct=blob.slice(12);
    const ck=await _masterCryptoKey(mk,'decrypt');
    return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv},ck,ct)); }
  // Already-imported check (resume a bulk import): match a source file by name+size.
  /* Is this song ALREADY in the library? The index is not enough to answer that, because an index
   * entry can outlive its blob: deleting your files removes the bytes from Blossom and leaves the
   * entry behind. musicTracks() already knows this — it hides any track whose sha the server does not
   * list — so the two disagreed, and the disagreement was a DEADLOCK: the library showed "no music
   * yet" while every re-upload was refused as "already imported". Someone who cleared their drive
   * could never put their music back. Same existence test as musicTracks, including its null rule
   * (never fetched → behave as before rather than treat everything as missing). */
  function _musicHasSrc(file){
    const fs=FilesIdx._norm().files;
    for(const sha in fs){
      const m=fs[sha];
      if(!(m && m.folder==='Music' && m.srcName===file.name && m.srcSize===file.size)) continue;
      if(S._blobHave && !S._blobHave.has(sha)) continue;   // the entry is a leftover; the bytes are gone
      return true;
    }
    return false;
  }
  /* What a saved track should be NAMED. Everything in the library used to be Opus, so `.ogg` was a
   * fact; now that an already-compressed file passes through untouched it is a guess, and a wrong
   * extension is a file the OS opens with the wrong thing. The stored mime is the answer, with the
   * uploader's own extension ahead of it since that is what they called it. */
  const _MUSIC_EXT = { 'audio/mpeg':'mp3', 'audio/mp3':'mp3', 'audio/aac':'aac', 'audio/mp4':'m4a',
    'audio/x-m4a':'m4a', 'audio/ogg':'ogg', 'audio/opus':'opus', 'audio/webm':'webm',
    'audio/wav':'wav', 'audio/flac':'flac' };
  function _musicExt(m){
    const e = (m && m.srcExt) || '';
    if(/^[A-Za-z0-9]{1,5}$/.test(e)) return e.toLowerCase();
    return _MUSIC_EXT[String((m && m.mime) || '').toLowerCase()] || 'ogg';
  }
  async function uploadMusicTrack(file, statEl){
    if(!S.signer.nip44enc) throw new Error('signer can\'t encrypt (needs NIP-44)');
    const setS=t=>{ if(statEl) statEl.textContent=t; };
    const mk=await FilesIdx._ensureMK();
    /* THE FILE YOU UPLOADED IS THE FILE THAT IS STORED. No transcode, ever.
     *
     * Every track used to be re-encoded to 96 kbps Opus by /client/music-compress on the way in.
     * That was written for a 50 MB WAV becoming 4 MB, and it ran on everything — so an mp3 someone
     * already owned came back as a lossy re-encode of a lossy source, under a different format, and
     * the original was gone. Measured on a real 1-hour narration: 54.9 MB in, 48.7 MB out. An 11%
     * saving, for a generation of quality loss, a 55 MB round trip to a transcoder, and the file no
     * longer being the file they gave us.
     *
     * It is their music. Storage is cheap and their master is not replaceable — so the bytes go up
     * exactly as they arrived, and the stored mime is the real one so it comes back out the same.
     * (The server endpoint stays for older cached clients that still call it.) */
    setS('reading…');
    const bytes=new Uint8Array(await file.arrayBuffer());
    const mime=(file.type||'audio/mpeg').split(';')[0];
    const srcExt=((file.name||'').match(/\.([A-Za-z0-9]{1,5})$/)||[])[1]||'';
    setS('encrypting…');
    const blob=await _masterEncrypt(mk, bytes, await _contentIV(bytes));   // deterministic IV → identical hash → dedup
    setS('uploading…');
    // noMirror: never DR-mirror encrypted music to the public backup servers (bandwidth/abuse).
    const url=await uploadBlob(new File([blob],(file.name||'track')+'.enc',{type:'application/octet-stream'}), {noMirror:true, keep:true, noCompress:true});
    const sha=_shaFromUrl(url); if(!sha) throw new Error('upload returned no hash');
    // The stored mime has to be the TRUTH — a passed-through mp3 labelled audio/ogg decodes by luck
    // rather than by contract, and the download would save it under the wrong extension.
    FilesIdx.setFile(sha,{name:(file.name||'track').replace(/\.[^.]+$/,''),folder:'Music',mime:mime,enc:true,mk:true,size:bytes.length,srcName:file.name,srcSize:file.size,srcExt:srcExt,ts:Math.floor(Date.now()/1000)});
    return sha;   // the caller's receipt: the sha the library now holds this track under
  }
  // One-time-per-session cleanup of leaked Files-index blobs (the old cross-session GC bug left stale
  // encrypted index blobs on Blossom — the "OCTET-STE" files filling the drive). A blob qualifies only
  // if it has NO file metadata, is octet-stream, small, AND decrypts with the master key to an object
  // shaped like an index ({files, folders}) — so it can never touch a real user file (those have meta,
  // and random/other ciphertext fails AES-GCM auth and is skipped).
  // The client-side orphan-index-blob GC is GONE. It fetched candidate blobs, tried to decrypt
  // each, and PERMANENTLY deleted any that parsed as an index — no confirmation, no undo, no
  // server-side check. It came one successful decrypt from destroying the only surviving copy of a
  // drive's folder index. Superseded index blobs are ~133 KB; keeping them is free, and they are
  // the backup. Any reclamation belongs in a server-side sweep with an age floor, not in a browser.
  /* OFFLINE MUSIC — the library, kept, on this device.
   *
   * A track already lands in the service worker's drive cache, but only by accident: only if you
   * played it, only if it was under the 8MB per-blob cap, and only until that cache's count trim
   * makes room for note attachments. "My library on my phone" cannot be luck, which is the same
   * problem Notes had and solved by PINNING (_isPinned in store.js).
   *
   * IndexedDB rather than a Cache-API store in the service worker, because the service worker is not
   * everywhere this app is: the desktop builds load their bundle over the privileged app:// scheme
   * and the APK's worker is deliberately media-only at root scope. IDB is the one store that behaves
   * the same in a browser tab, a PWA, the Android WebView and Electron — so "download for offline"
   * means the same thing on Windows, macOS, Linux and a phone, which is the point.
   *
   * The bytes are stored EXACTLY as Blossom served them: still encrypted with the user's master key,
   * never the decoded audio. An offline copy must not be a weaker copy — anything reading this
   * device's storage learns no more than anything reading the media server.
   *
   * Nothing here evicts. A download was asked for by name; it leaves when the user removes it. */
  const MusicOffline = {
    DB:'pcmusic', VER:1, STORE:'blobs', _db:null, _have:null,
    _open(){
      if(this._db) return Promise.resolve(this._db);
      return new Promise((res,rej)=>{
        let rq; try{ rq=indexedDB.open(this.DB, this.VER); }catch(e){ return rej(e); }
        rq.onupgradeneeded=()=>{ const db=rq.result;
          if(!db.objectStoreNames.contains(this.STORE)) db.createObjectStore(this.STORE); };
        rq.onsuccess=()=>{ this._db=rq.result; res(this._db); };
        rq.onerror=()=>rej(rq.error||new Error('indexeddb unavailable'));
      });
    },
    async _tx(mode, fn){
      const db=await this._open();
      return new Promise((res,rej)=>{
        const tx=db.transaction(this.STORE, mode), st=tx.objectStore(this.STORE);
        let out; try{ out=fn(st); }catch(e){ return rej(e); }
        // `'result' in out`, not `out.result !== undefined`: a MISS gives a request whose result is
        // undefined, and unwrapping on that returns the REQUEST OBJECT instead of nothing. Every
        // caller here happens to survive it — get() tests `v && v.b` — but it is the same trap that
        // in folder sync returned "a base containing one file called result", so it is spelled the
        // same way in both places rather than left as luck.
        tx.oncomplete=()=>res(out && typeof out==='object' && ('result' in out) ? out.result : out);
        tx.onerror=()=>rej(tx.error); tx.onabort=()=>rej(tx.error);
      });
    },
    // Which shas are on this device. Cached in memory: the list re-renders on every keystroke of the
    // search box and a keyed IDB scan per repaint is exactly the kind of cost that reads as "slow app".
    async have(force){
      if(this._have && !force) return this._have;
      try{
        const keys=await this._tx('readonly', st=>st.getAllKeys());
        this._have=new Set(keys||[]);
      }catch(_){ this._have=new Set(); }
      return this._have;
    },
    async get(sha){
      try{ const v=await this._tx('readonly', st=>st.get(sha));
        return v && v.b ? new Uint8Array(await v.b.arrayBuffer()) : null;
      }catch(_){ return null; }
    },
    /* WHAT THIS IS USING, and how to come back under a budget.
     *
     * Kept tracks had no ceiling at all: the store grew until the BROWSER decided, and a browser
     * evicting an origin takes everything with it — the drive cache, Notes attachments, the files
     * index — not just the music. An unbounded store is not "keep everything", it is "lose
     * everything, eventually, without being asked".
     *
     * So there is a budget, and it is a SETTING rather than a constant, because how much of a phone
     * a music library should occupy is not a decision this code can make. Eviction is oldest-first
     * by the time the track was stored, and it only ever runs when a new track pushes the total over
     * — so a library that fits is never touched, which is the promise the feature makes. */
    /* DROP CACHED BYTES FOR TRACKS THAT NO LONGER EXIST.
     *
     * The library is shared across every device through the relay, but the offline cache is not — it
     * is this device's IndexedDB. So when a track is deleted ANYWHERE (one device retires a blob
     * and every other device's index follows), the other devices keep the old bytes for a sha
     * nothing references. They are invisible
     * — the track lists and plays correctly from the new blob — and they sit there taking space until
     * the cache is cleared by hand.
     *
     * The index is the authority: anything cached whose sha the index no longer knows is orphaned.
     * Deliberately gated on a LOADED index — an empty or half-hydrated one would look like "the
     * library is gone" and this would helpfully delete the entire offline collection. That is the
     * replaceable-doc wipe in another costume, and the guard is the whole reason this is safe.
     */
    async sweep(){
      try{
        const idx = FilesIdx._norm().files || {};
        const known = Object.keys(idx);
        if(!known.length) return 0;              // nothing loaded yet → nothing is orphaned
        const live = new Set(known);
        const have = await this.have(true);
        const dead = [...have].filter(sha => !live.has(sha));
        if(!dead.length) return 0;
        for(const sha of dead) await this.drop(sha);
        console.info('[music] dropped', dead.length, 'orphaned offline track(s)');
        return dead.length;
      }catch(_){ return 0; }
    },
    async usage(){
      try{
        const rows = await this._tx('readonly', st => st.getAll());
        let bytes = 0;
        for(const r of (rows || [])) bytes += (r && r.size) || 0;
        return { bytes, count: (rows || []).length };
      }catch(_){ return { bytes: 0, count: 0 }; }
    },
    /* 0 means NO LIMIT, and that is the default, because it is what this always did and because a
     * music library is not a cache — someone who downloaded their albums for a flight did not ask
     * for the oldest ones to disappear. The danger an unbounded store carries is real (a browser
     * evicting an origin takes the drive cache and Notes attachments with it, not just music), so
     * the answer is to ASK not to be evicted rather than to silently delete — boot calls
     * navigator.storage.persist() for exactly that, and the stat line below reports the OS ceiling,
     * because "no limit" is a promise this app cannot make on its own. */
    budgetBytes(){
      const gb = +ClientSettings.get('musicOfflineGB', 0);
      return gb > 0 ? gb * 1024 * 1024 * 1024 : 0;
    },
    async trim(){
      const budget = this.budgetBytes();
      if(!budget) return 0;                       // no limit: nothing is ever evicted
      let rows;
      try{
        rows = await this._tx('readonly', st => st.getAll());
      }catch(_){ return 0; }
      let total = 0;
      for(const r of (rows || [])) total += (r && r.size) || 0;
      if(total <= budget) return 0;
      // getAll() gives values without their keys, so pair them up in the same order getAllKeys does.
      let keys = [];
      try{ keys = await this._tx('readonly', st => st.getAllKeys()) || []; }catch(_){ return 0; }
      const items = keys.map((k, i) => ({ sha: k, ts: (rows[i] && rows[i].ts) || 0, size: (rows[i] && rows[i].size) || 0 }))
                        .sort((a, b) => a.ts - b.ts);          // oldest stored, first to go
      let freed = 0;
      for(const it of items){
        if(total - freed <= budget) break;
        try{ await this.drop(it.sha); freed += it.size; }catch(_){}
      }
      if(freed) console.warn('music offline: freed', Math.round(freed / 1048576), 'MB to stay under the budget');
      return freed;
    },
    async put(sha, bytes){
      try{
        await this._tx('readwrite', st=>st.put({ b:new Blob([bytes]), size:bytes.length, ts:Math.floor(Date.now()/1000) }, sha));
        (await this.have()).add(sha);
        // After the write, not before: the track just asked for is the one thing that must survive
        // this, and trimming first could evict something to make room and then fail to store.
        try{ await this.trim(); }catch(_){}
        return true;
      }catch(e){ console.warn('music offline: could not store', sha, e); return false; }
    },
    async drop(sha){
      try{ await this._tx('readwrite', st=>st.delete(sha)); (await this.have()).delete(sha); return true; }
      catch(_){ return false; }
    },
    async stats(){
      try{ const all=await this._tx('readonly', st=>st.getAll());
        return { count:(all||[]).length, bytes:(all||[]).reduce((n,v)=>n+(v.size||0),0) };
      }catch(_){ return { count:0, bytes:0 }; }
    },
    /* Download a set of tracks, a couple at a time, reporting after each one.
     *
     * Two at a time on purpose: a library is hundreds of files and a phone on a slow radio should not
     * open hundreds of connections — the same lesson the link-card fan-out taught this node the hard
     * way. `onStep` fires per track so the list can show real progress rather than a spinner. */
    async keep(shas, onStep){
      const todo=[...new Set(shas||[])]; const have=await this.have();
      const queue=todo.filter(s=>!have.has(s));
      let done=0, ok=0, running=0, i=0;
      const total=queue.length;
      if(!total){ if(onStep) onStep({done:0,total:0,ok:0}); return {done:0,total:0,ok:0}; }
      return new Promise(res=>{
        const step=()=>{
          if(done>=total){ res({done,total,ok}); return; }
          while(running<2 && i<total){
            const sha=queue[i++]; running++;
            (async()=>{
              try{
                const r=await fetch(mediaServer()+'/'+sha);
                if(r.ok && await this.put(sha, new Uint8Array(await r.arrayBuffer()))) ok++;
              }catch(_){ }
              running--; done++;
              if(onStep) try{ onStep({done,total,ok}); }catch(_){}
              step();
            })();
          }
        };
        step();
      });
    },
  };
  // The library's record, or a not-yet-added SHARED track's (musicshare.js): one lookup for every title.
  function _trackMeta(sha){
    const m=FilesIdx.meta(sha); if(m) return m;
    try{ return (window.PCMusicShare && PCMusicShare.meta(sha)) || null; }catch(_){ return null; }
  }
  async function trackUrl(sha){
    if(_trackUrls[sha]) return _trackUrls[sha];
    // A shared track not in the library: key from the share, bytes from the sharer's server.
    const sm=FilesIdx.meta(sha)?null:_trackMeta(sha);
    const m=(sm&&sm.shared)?sm:FilesIdx.meta(sha); if(!m||!m.enc) throw new Error('not an encrypted track');
    // The kept copy first — this is what makes a downloaded library play with the radio off, and it
    // is checked before the network on EVERY platform, not only where a service worker runs.
    let blob=m.shared||await MusicOffline.get(sha);
    if(!blob){
      const r=await fetch(mediaServer()+'/'+sha); if(!r.ok) throw new Error('blob HTTP '+r.status);
      blob=new Uint8Array(await r.arrayBuffer());
    }
    // v2 master-key (IV prepended) or v1 per-track key — _driveDecrypt is the single place that knows,
    // shared with _encFileUrl. A track with NEITHER field used to throw 'no key' here; it now tries the
    // master key, which is what an index entry written before the flag existed actually needs.
    if(!m.shared && !m.mk && !m.keyenc) console.warn('track', sha.slice(0,8), 'has no key field — trying the master key');
    // `true`: trackUrl already refused anything without a record (`if(!m||!m.enc) throw` above), so
    // by here the meta IS the index's.
    const plain=m.shared?await PCMusicShare.plain(sha):await _driveDecrypt(m, blob, true);
    const u=URL.createObjectURL(new Blob([plain],{type:m.mime||'audio/ogg'})); _trackUrls[sha]=u; _trackUrlOrder.push(sha);
    while(_trackUrlOrder.length>6){ const old=_trackUrlOrder.shift(); if(old!==(MusicPlayer&&MusicPlayer.cur) && _trackUrls[old]){ URL.revokeObjectURL(_trackUrls[old]); delete _trackUrls[old]; } }
    return u;
  }
  // Blobs the server actually still HAS (from Blossom /list). The index is a local/encrypted record of
  // what you uploaded and is NOT updated when blobs are deleted elsewhere — so trusting it alone made the
  // player queue songs that no longer exist. Only _renderMusicList passed a list; openMusic, refreshQueue
  // and the player's library list all passed null, which is why Files → Music looked right while the
  // player kept trying deleted tracks. null = never fetched → behave as before rather than hide everything.
  async function _refreshBlobHave(){
    if(!S.ME) return S._blobHave;
    try{ const r=await fetch(mediaServer()+'/list/'+S.ME.pubkey);
      if(r.ok){ const l=await r.json(); if(Array.isArray(l)){
        S._blobHave=new Set(l.map(b=>b.sha256));
        // Sizes come from the same answer — a second pass to total them would be a second /list.
        S._blobSizes=new Map(l.map(b=>[b.sha256, Number(b.size)||0]));
      } } }catch(_){}
    return S._blobHave;
  }
  /* Every track the index knows about, each flagged with whether the server still HAS its bytes.
   * The index and the blobs can disagree — deleting files removes the bytes and leaves the entries —
   * and both ways of resolving that silently are bad: hiding the entries makes a library look empty
   * and unrestorable, listing them as normal makes half of it fail on play with no warning. So they
   * are listed AND marked. `have` null means "not fetched yet", not "everything is gone". */
  function musicEntries(list){
    const have=list?new Set(list.map(b=>b.sha256)):S._blobHave;
    // A track kept on THIS device is playable whatever the server says — including when the server
    // said nothing because there is no network. Marking a downloaded song "missing" would be the
    // offline library calling itself broken at exactly the moment it is doing its job.
    const kept=MusicOffline._have || null;
    return Object.keys(FilesIdx._norm().files)
      .filter(sha=> FilesIdx.folderOf(sha)==='Music' && FilesIdx.meta(sha).enc)
      .map(sha=>({sha, m:FilesIdx.meta(sha), offline: !!(kept && kept.has(sha)),
                  missing: !!(have && !have.has(sha)) && !(kept && kept.has(sha))}))
      .sort((a,b)=>(b.m.ts||0)-(a.m.ts||0));
  }
  // Playable tracks only — the queue must never contain something that cannot be played.
  function musicTracks(list){ return musicEntries(list).filter(t=>!t.missing); }
  /* Refresh must not turn a selected playlist into the whole library while leaving its chip lit.
   * Rebuild the ordered set from the freshly pulled index (rather than retaining stale entry
   * objects); callers outside a playlist keep the ordinary null = whole-library contract. */
  function _musicRefreshedSet(only){
    return only ? (S._musicPl ? _plTracks(S._musicPl) : only) : null;
  }
  function _fmtTime(s){ s=Math.floor(s||0); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }

  return {
    MusicOffline, _aesDecrypt, _b64u8, _contentIV, _fmtTime, _masterDecrypt, _masterEncrypt,
    _masterKeyInput, _musicExt, _musicHasSrc, _musicRefreshedSet, _refreshBlobHave, _trackMeta,
    _u8b64, _unwrapMK, decorateEncAtts, dmAttachFiles, dmAttachMenu, dmEncOn, dmPickGif,
    dmPickMedia, encAttParse, musicEntries, musicTracks, trackUrl, uploadMusicTrack,
    uploadSharedEnc,
  };
};

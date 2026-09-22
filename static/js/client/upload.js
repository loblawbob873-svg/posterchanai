/* Uploading and everything around a stored file — hashing, MIME/extension rules, image and video
 * compression, the Blossom and NIP-96 upload paths, imeta tags, thumbnails and their fallbacks,
 * download/save (including the native share sheet), and the Blossom/GIF pickers. Split out of app.js.
 *
 * It ships with the page rather than loading on demand: the timeline header, the composer, Mail,
 * Files, the AI chat and git all call into it SYNCHRONOUSLY (`_blossomDenied()`, `imetaTagsFor()`,
 * `thumbUrl()`), so waiting for a fetch at the point of call is not an option. app.js keeps one
 * entry point per name and builds the factory the first time one is called.
 *
 * The code below is BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets. Stayed in app.js: the office/code classifiers Mail and Files share, the
 * measured-media cache (a Map cannot be reached through a Proxy), the upload batch auth, and the
 * `pcNativeDownload` listener the APK fires, which must be bound at boot.
 */
window.PCUploadFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.ME, S.VIEW, S._aiToken, S._blossomOK, S._uploadBatchAuth
  const {
    $, $$, FilesIdx, _MEDIA_META, _blobToB64, _blossomBuiltin, _fmtBytes, _fxFileGlyph, _hold,
    _instanceBase, _isNativeApp, _popKeys, _serverOrigin, _shaFromUrl, _trapFocus,
    checkBlossomAccess, copyValue, enc, isMutedView, isReply, mediaServer, needProfile, openThread,
    profOf, safePk, sendDm, sign, toast, trackUrl, uploadTarget,
  } = dep;
  async function sha256hex(buf){ const h=await crypto.subtle.digest('SHA-256', buf); return [...new Uint8Array(h)].map(b=>b.toString(16).padStart(2,'0')).join(''); }
  // Hash a File for upload WITHOUT holding it whole in memory. Small files go through crypto.subtle
  // in one shot; large ones (ISOs, big drive uploads) are hashed in slices via PCSha256 — otherwise
  // file.arrayBuffer() caps an upload at what a browser tab can allocate (~2 GB), smaller than an ISO.
  // The incremental hasher lives in its own file (sha256.js). It is loaded by client.html, but that
  // template is server-rendered — so an instance running older server code would not carry the <script>
  // tag, and a big upload would fall back to the arrayBuffer path and OOM. Load it ON DEMAND from the
  // same static dir (the SW precaches it), so the streaming hash does not depend on the page shell.
  let _pcSha256Load = null;
  function _ensurePCSha256(){
    if(window.PCSha256) return Promise.resolve(window.PCSha256);
    if(_pcSha256Load) return _pcSha256Load;
    _pcSha256Load = new Promise((resolve, reject) => {
      const sc = document.createElement('script');
      sc.src = '/static/js/client/sha256.js';
      sc.onload = () => resolve(window.PCSha256 || null);
      sc.onerror = () => { _pcSha256Load = null; reject(new Error('could not load the streaming hasher')); };
      document.head.appendChild(sc);
    });
    return _pcSha256Load;
  }
  async function hashFileHex(file){
    if(file && typeof file.size === 'number' && file.size > 96*1024*1024){
      try{ const h = await _ensurePCSha256(); if(h) return await h.hexOfFile(file); }catch(_){}
    }
    return sha256hex(await file.arrayBuffer());
  }
  const _MIME_EXT={'image/jpeg':'jpg','image/png':'png','image/gif':'gif','image/webp':'webp','image/avif':'avif',
    'video/mp4':'mp4','video/webm':'webm','video/quicktime':'mov','audio/mpeg':'mp3','audio/ogg':'ogg','audio/wav':'wav','audio/mp4':'m4a','audio/aac':'aac','audio/flac':'flac'};
  function extFor(file){ const n=(file.name||'').match(/\.([a-z0-9]{2,5})$/i); if(n) return n[1].toLowerCase(); return _MIME_EXT[file.type]||''; }
  /* Name → MIME, for the places that rebuild a File out of RAW BYTES.
   *
   * `new File([bytes], 'holiday.jpg')` has `type === ''` — the constructor does not look at the name,
   * and nothing downstream recovers it. Blossom then stores the blob untyped, and the drive decides
   * a card's preview purely from the stored type (blobThumb), so the file rendered as a generic 📎
   * with no thumbnail: reported after copying a picture out of a synced folder. The same blank type
   * makes _wantsLibrary say "not audio", so an mp3 copied the same way went to Posts instead of the
   * music library — the one thing that button's own comment promises it does.
   *
   * Extends the reverse of _MIME_EXT rather than replacing it, so the two tables cannot disagree
   * about the formats both know; the extras are the ones that only ever arrive as a filename. */
  const _EXT_MIME = (() => {
    const m = {};
    for(const mime in _MIME_EXT) m[_MIME_EXT[mime]] = mime;
    return Object.assign(m, {
      jpeg:'image/jpeg', svg:'image/svg+xml', bmp:'image/bmp', ico:'image/x-icon', heic:'image/heic',
      tif:'image/tiff', tiff:'image/tiff',
      mkv:'video/x-matroska', avi:'video/x-msvideo', m4v:'video/mp4', mpg:'video/mpeg', mpeg:'video/mpeg',
      opus:'audio/ogg', oga:'audio/ogg', m4b:'audio/mp4', wma:'audio/x-ms-wma', aif:'audio/aiff',
      aiff:'audio/aiff', ape:'audio/x-ape', mka:'audio/x-matroska',
      pdf:'application/pdf', txt:'text/plain', md:'text/markdown', csv:'text/csv',
      conf:'text/plain', cfg:'text/plain', ini:'text/plain', env:'text/plain', log:'text/plain',
      doc:'application/msword', docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      odt:'application/vnd.oasis.opendocument.text', rtf:'application/rtf',
      xls:'application/vnd.ms-excel', xlsx:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      xlsm:'application/vnd.ms-excel.sheet.macroEnabled.12', ods:'application/vnd.oasis.opendocument.spreadsheet',
      ppt:'application/vnd.ms-powerpoint', pptx:'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      odp:'application/vnd.oasis.opendocument.presentation',
      json:'application/json', xml:'application/xml', html:'text/html', htm:'text/html',
      zip:'application/zip', gz:'application/gzip', tar:'application/x-tar', '7z':'application/x-7z-compressed',
      rar:'application/vnd.rar', epub:'application/epub+zip',
    });
  })();
  function mimeForName(name){
    const e = (String(name||'').match(/\.([A-Za-z0-9]{1,8})$/)||[])[1];
    return (e && _EXT_MIME[e.toLowerCase()]) || '';
  }
  /* A File rebuilt from bytes, carrying the type its NAME implies. Everything that reconstructs a
   * file from a decrypted/downloaded buffer should go through this rather than `new File(...)`. */
  function fileFromBytes(bytes, name, type){
    const t = type || mimeForName(name);
    return t ? new File([bytes], name, { type: t }) : new File([bytes], name);
  }
  // Pixel dimensions of an image/video file as "WxH" (decoded locally, no network). '' if unknown.
  async function _mediaDim(file){
    const t=file.type||'';
    try{
      if(/^image\//.test(t) && self.createImageBitmap){
        const bm=await createImageBitmap(file); const d=(bm.width&&bm.height)?(bm.width+'x'+bm.height):''; if(bm.close) bm.close(); return d;
      }
      if(/^video\//.test(t)){
        return await new Promise(res=>{ const v=document.createElement('video'); v.preload='metadata'; const u=URL.createObjectURL(file);
          v.onloadedmetadata=()=>{ res(v.videoWidth&&v.videoHeight?(v.videoWidth+'x'+v.videoHeight):''); URL.revokeObjectURL(u); };
          v.onerror=()=>{ res(''); URL.revokeObjectURL(u); }; v.src=u; });
      }
    }catch(_){}
    return '';
  }
  // Downscale + re-encode large images BEFORE they're uploaded or sent — keeps Blossom storage small
  // and, crucially, keeps base64 chat attachments under the size that made multi-image / big-image
  // sends hang. Skips animated/vector (gif/svg) and anything already small; never upsizes. Pure
  // browser canvas work, so it's cheap and offloads the server.
  async function compressImage(file, opts){
    const o = opts || {}; const maxDim = o.maxDim || 2560, maxBytes = o.maxBytes || 800*1024, minQ = o.minQ || 0.45;
    try{
      const t=(file.type||'').toLowerCase();
      if(!/^image\//.test(t) || /gif|svg/.test(t)) return file;     // keep animation/vector intact
      // PNG keeps its format (lossless, ALPHA preserved) so transparent avatars/logos/screenshots
      // aren't flattened onto a black background; everything else re-encodes to JPEG. imageOrientation
      // bakes EXIF rotation into the pixels so a portrait phone photo isn't uploaded sideways.
      const isPng = /png/.test(t);
      const bmp=await createImageBitmap(file, {imageOrientation:'from-image'});
      let w=bmp.width, h=bmp.height; const scale=Math.min(1, maxDim/Math.max(w,h));
      if(scale>=1 && file.size<=maxBytes){ if(bmp.close) bmp.close(); return file; }   // already small + upright-enough
      w=Math.round(w*scale); h=Math.round(h*scale);
      const cv=document.createElement('canvas'); cv.width=w; cv.height=h;
      cv.getContext('2d').drawImage(bmp,0,0,w,h); if(bmp.close) bmp.close();
      const outType = isPng ? 'image/png' : 'image/jpeg';
      // JPEG: actually ENFORCE the size cap — step the quality down until the blob is under maxBytes
      // (was a no-op before: maxBytes only gated the early return, so big photos still uploaded big).
      // PNG has no quality knob, so it's a single lossless pass.
      let blob=await new Promise(r=> cv.toBlob(r, outType, 0.9));
      if(!isPng){ let q=0.9; while(blob && blob.size>maxBytes && q>minQ){ q-=0.12; blob=await new Promise(r=> cv.toBlob(r,'image/jpeg',q)); } }
      if(!blob || blob.size>=file.size) return file;                // never make it bigger
      const ext = isPng ? 'png' : 'jpg';
      return new File([blob], (file.name||'image').replace(/\.\w+$/,'')+'.'+ext, {type:outType});
    }catch(_){ return file; }
  }
  // The video half of compressImage, which returns any NON-image untouched — so a phone clip attached to
  // a post uploaded at full size (a few hundred MB is normal for a modern phone). The browser can't do
  // this itself: there's no ffmpeg.wasm in this app (and the CSP/self-hosting rules out pulling one from
  // a CDN), so it hands the file to the node's GPU ffmpeg and uploads what comes back.
  // ALWAYS returns a File. Every failure path — offline, 503, too large, or a result that came out BIGGER
  // (re-encoding an already-thin clip inflates it) — keeps the original, because compression must never
  // cost the user their post.
  // Type OR extension: a browser hands over .mkv/.avi with an EMPTY type often enough that a type-only
  // test would quietly skip exactly the chunky files this exists for. Same predicate everywhere, so the
  // post composer and the AI chat can't disagree about what counts as a video.
  function _isVideoFile(file){
    if(/^video\//.test((file.type||'').toLowerCase())) return true;
    return /\.(mp4|webm|mov|m4v|mkv|avi)$/i.test(file.name||'');
  }
  async function compressVideo(file){
    try{
      if(!_isVideoFile(file)) return file;
      if(file.size <= 2*1024*1024) return file;      // already small — not worth the round trip
      if(file.size > 512*1024*1024) return file;     // over the server's cap; don't spend the upload to be told
      const fd=new FormData(); fd.append('file', file, file.name||'video.mp4');
      const r=await fetch('/client/media/compress-video',{method:'POST',body:fd});
      if(r.status===204 || !r.ok) return file;       // 204 = server had nothing smaller to offer
      const blob=await r.blob();
      if(!blob || !blob.size || blob.size>=file.size) return file;
      return new File([blob], (file.name||'video').replace(/\.\w+$/,'')+'.mp4', {type:'video/mp4'});
    }catch(_){ return file; }
  }
  // Files that have already been through compressMedia. _signUploadBatch runs the pass to hash what it
  // will send, hands the RESULT to uploadBlob, and uploadBlob runs the pass again — harmless for images
  // (the second call early-returns on an already-small file) but for VIDEO that would be a second upload
  // to the node and a second re-encode: double the wait, and a needless extra generation of quality loss.
  const _preparedForUpload = new WeakSet();
  // ONE entry point for "prepare this file for upload", so THE BYTES WE HASH ARE THE BYTES WE SEND.
  // uploadBlob and _signUploadBatch must run the identical pass or the batch auth's `x` tags won't match
  // the uploaded blob, and every file falls back to its own signature — one Amber prompt per clip.
  async function compressMedia(file){
    if(_preparedForUpload.has(file)) return file;
    const out = _isVideoFile(file) ? await compressVideo(file) : await compressImage(file);
    try{ _preparedForUpload.add(out); }catch(_){}
    return out;
  }
  // NIP-96 upload (nostr.build et al.): discover the endpoint from /.well-known/nostr/nip96.json,
  // POST multipart with a NIP-98 (kind-27235) Authorization header, and read the file URL out of the
  // returned nip94_event tags. Used when the target proto is 'nip96' (Blossom uploadBlob can't talk to it).
  async function uploadNip96(file, server, opts){
    const base=server.replace(/\/+$/,'');
    let api=base+'/api/v2/nip96/upload';
    try{ const wk=await fetch(base+'/.well-known/nostr/nip96.json').then(r=>r.ok?r.json():null);
      if(wk&&wk.api_url) api=new URL(wk.api_url, base+'/').href; }catch(_){}   // resolve a relative api_url against the host
    const auth=await sign(27235,'',[['u',api],['method','POST']]);   // NIP-98 HTTP-auth event
    const fd=new FormData(); fd.append('file', file, file.name||('upload.'+(extFor(file)||'bin')));
    let res;
    try{ res=await fetch(api,{ method:'POST', headers:{ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)) }, body:fd }); }
    catch(e){ throw new Error(`couldn't reach ${server} — check the URL, and that it allows cross-origin (CORS) uploads`); }
    if(!res.ok){ const t=await res.text().catch(()=>String(res.status)); throw new Error(res.headers.get('x-reason')||('upload failed: '+t)); }
    const d=await res.json();
    const tags=(d&&d.nip94_event&&d.nip94_event.tags)||[];
    const url=(tags.find(t=>t[0]==='url')||[])[1] || (d&&d.url) || '';
    if(!url) throw new Error('nostr.build: no URL in the upload response');
    try{ const t=file.type||''; if(/^(image|video)\//.test(t)){ const x=(tags.find(t=>t[0]==='x')||[])[1]; _MEDIA_META.set(url,{ m:t, x:x||undefined, dim:await _mediaDim(file) }); } }catch(_){}
    /* A NIP-96 UPLOAD MUST REPORT ITS CONTENT HASH, OR FILES THROWS THE FILE AWAY.
     *
     * uploadBlob's Blossom path ends with `opts.hashOut.sha = <the hash it computed>`, and every
     * drive caller reads that: `const sha = stored.sha || _shaFromUrl(url); if(!sha) throw new
     * Error('upload completed without a content hash')`. This function returned the URL and NOTHING
     * ELSE, from a branch that leaves BEFORE uploadBlob computes a hash at all — so on any client
     * routed to NIP-96 the bytes uploaded fine and were then discarded by the caller, every time.
     * Reported for four days as "I still can't upload to Blossom from File Manager to Backgrounds":
     * the toast says "0 added · 1 failed" and the folder stays empty, while the file IS on a server.
     * Nothing in any log, because the throw is caught per file and shown only as a ✗.
     *
     * The hash to report is the STORED blob's, which on NIP-96 is the `x` tag — the server may
     * transform what it was given (strip EXIF, transcode), so our local hash would address bytes
     * that are not there. `ox` (the original) is the next best, and only if neither is a real
     * sha256 do we fall back to hashing what we sent. */
    if(opts && opts.hashOut && typeof opts.hashOut==='object'){
      const pick = k => { const v=(tags.find(t=>t[0]===k)||[])[1]||''; return /^[0-9a-f]{64}$/i.test(v) ? v.toLowerCase() : ''; };
      let sha = pick('x') || pick('ox');
      if(!sha){ try{ sha = await sha256hex(await file.arrayBuffer()); }catch(_){ sha=''; } }
      if(sha) opts.hashOut.sha = sha;
    }
    return url;
  }
  async function _signUploadBatch(files){
    try{
      const hashes=[];
      const prepped=[];
      for(const f of files){
        const c=await compressMedia(f);            // hash what we will ACTUALLY send (images AND video)
        const buf=await c.arrayBuffer();
        hashes.push(await sha256hex(buf)); prepped.push(c);
      }
      if(hashes.length<2) return null;             // a single file gains nothing — skip the extra work
      // CHUNK the batch: one auth event per ~50 files. A single 24242 event with hundreds of `x` tags
      // makes an Authorization header tens of KB, which the nginx in front of the Blossom host rejects
      // BEFORE the app (its response carries no CORS header, so the browser reports it as a bogus CORS
      // error) — that's the "folder with subfolders / 368 files all fail, no error" bug. ~50 hashes keeps
      // each header a few KB; the only cost is one signer prompt per chunk instead of one for the import.
      const CHUNK=50, chunks=[];
      for(let i=0;i<hashes.length;i+=CHUNK){
        const slice=hashes.slice(i,i+CHUNK);
        const tags=[['t','upload'],['expiration',String(Math.floor(Date.now()/1000)+3600)]];
        slice.forEach(h=>tags.push(['x',h]));
        const ev=await sign(24242,'Upload '+slice.length+' files',tags);
        chunks.push({ ev, hashes:new Set(slice) });
      }
      S._uploadBatchAuth=chunks;
      return prepped;                              // upload the SAME bytes we hashed
    }catch(_){ S._uploadBatchAuth=null; return null; }
  }
  async function uploadBlob(file, opts){
    // Resolve built-in Blossom permission before routing, so a brand-new user's FIRST upload (right
    // after login, before the async check resolves) still diverts to nostr.build instead of 403ing
    // the built-in server. Only matters when they haven't set their own server.
    if(S._blossomOK===null && !ClientSettings.get('blossomEnabled')){ try{ await checkBlossomAccess(); }catch(_){} }
    let tgt=uploadTarget();
    // Private / no-mirror content (encrypted vault blobs) must NEVER land on the public nostr.build
    // auto-fallback — keep it on the built-in server even if that surfaces a permission error.
    if(opts&&opts.noMirror && tgt.proto==='nip96' && !ClientSettings.get('blossomEnabled')) tgt=_blossomBuiltin();
    const server=tgt.url; if(!server) throw new Error('no media server set');
    /* auto-compress images AND video (no-op for gif/svg/already-small)
     *
     * …EXCEPT for an ARCHIVAL copy. compressMedia is right for something being posted — a 6 MB phone
     * photo does not need to reach a timeline at full size — and wrong for "keep this file", which
     * has to store the bytes it was given. It re-encodes anything over 800 KB or 2560px to JPEG at a
     * quality as low as 0.45 and drops EXIF with it, so a copy kept from a synced folder would land
     * in the drive smaller, lossier, stripped of its capture date and under a DIFFERENT sha256 than
     * the original it claims to be a copy of — with nothing in the UI to say so. */
    if(!(opts && opts.noCompress)) file=await compressMedia(file);
    if(tgt.proto==='nip96') return await uploadNip96(file, server, opts);
    const hash=await hashFileHex(file);
    // Reuse the BATCH auth when this blob's hash is one it already commits to (BUD-01 allows many `x` tags,
    // and the server checks membership). That turns "sign once per file" into ONE signature for the whole
    // batch — the difference between one Amber prompt and one per clip. If the hash isn't covered (e.g.
    // compressImage changed the bytes after the batch was signed) we fall back to signing this file alone,
    // so a mismatch costs an extra prompt instead of failing the upload.
    const _batchEv=Array.isArray(S._uploadBatchAuth) ? (S._uploadBatchAuth.find(c=>c.hashes.has(hash))||{}).ev : null;
    const auth=_batchEv
      || await sign(24242,'Upload blob',[['t','upload'],['x',hash],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
    const hdr={ 'Authorization':'Nostr '+btoa(JSON.stringify(auth)), 'Content-Type':file.type||'application/octet-stream' };
    if(opts&&opts.noMirror) hdr['X-No-Mirror']='1';   // don't DR-mirror (e.g. encrypted music) to public backups
    // Encrypted-drive content (Notes attachments, music, the files index): exempt from the server's
    // age sweep forever. The server can't tell — the bytes are opaque ciphertext — so the uploader
    // that knows this is the only copy has to say so. Harmless on a server that predates the header.
    if(opts&&opts.keep) hdr['X-Keep']='1';
    // Tell the server the original filename. A blob is addressed by its hash and has no name of its
    // own, so without this a download off any other device/client saves as a bare sha256. Percent-
    // encoded because a header can only carry ASCII (a non-ASCII name would throw here).
    // ONLY to our own server: a custom header is part of the CORS preflight, so sending it to a
    // third-party Blossom host that whitelists a fixed header list would fail the whole upload.
    try{ if(file.name && server===_blossomBuiltin().url) hdr['X-Filename']=encodeURIComponent(file.name); }catch(_){}
    // Stream the File itself (body:file), never a pre-read ArrayBuffer — the browser sends it in
    // chunks, so a multi-GB ISO is never resident. With opts.onProgress we use XHR, the only reliable
    // way to report upload progress; otherwise fetch.
    let d;
    if(opts && typeof opts.onProgress === 'function'){
      d = await new Promise((resolve, reject) => {
        const x = new XMLHttpRequest();
        x.open('PUT', server+'/upload');
        for(const k in hdr){ try{ x.setRequestHeader(k, hdr[k]); }catch(_){} }
        x.upload.onprogress = e => { try{ opts.onProgress({ loaded: e.loaded, total: e.total || file.size }); }catch(_){} };
        x.onload = () => {
          if(x.status>=200 && x.status<300){ let j={}; try{ j=JSON.parse(x.responseText); }catch(_){} resolve(j); }
          else reject(new Error(x.getResponseHeader('x-reason') || x.responseText || ('upload failed: HTTP '+x.status)));
        };
        x.onerror = () => reject(new Error(`couldn't reach ${server} — check the URL, and that the server allows cross-origin (CORS) uploads`));
        x.send(file);
      });
    } else {
      let res;
      try {
        res=await fetch(server+'/upload',{ method:'PUT', headers:hdr, body:file });
      } catch(e){
        // fetch rejects (vs. an HTTP error) only when the browser can't complete the request at all:
        // server unreachable, blocked mixed content (http:// on this https page), or — most often for
        // a custom server — it doesn't send CORS headers allowing this site to upload to it.
        throw new Error(`couldn't reach ${server} — check the URL, and that the server allows cross-origin (CORS) uploads`);
      }
      if(!res.ok){ const t=await res.text().catch(()=>res.status); throw new Error(res.headers.get('x-reason')||t); }
      d=await res.json();
    }
    // The URL must carry a file extension so clients (incl. linkify below) can detect the media type
    // and embed/play it. Our server now returns one (BUD-02), and other servers may not — so append
    // ours only when the returned URL doesn't already end in an extension, never blindly (that made
    // "photo.jpg.jpg"). The server ignores the suffix when serving.
    const ext=extFor(file); let url=d.url||(server+'/'+hash);
    if(ext && !/\.[a-z0-9]{1,8}$/i.test(url.split('?')[0])) url+='.'+ext;
    // Record NIP-92 source metadata so a note carrying this URL gets an `imeta` tag (see imetaTagsFor).
    try{ const t=file.type||''; if(/^(image|video)\//.test(t)){ _MEDIA_META.set(url, { m:t, x:hash, dim:await _mediaDim(file) }); } }catch(_){}
    if(opts && opts.folder) _fileUnder(url, file, opts.folder, hash);
    /* Some Blossom servers return a CDN/signed URL whose path does not contain the content hash.
     * Files still needs that hash for its encrypted index; expose the value we already computed to
     * trusted in-process callers without changing uploadBlob's long-standing string return type. */
    if(opts && opts.hashOut && typeof opts.hashOut==='object') opts.hashOut.sha=hash;
    return url;
  }
  /* Put an upload somewhere in the drive.
   *
   * Uploads used to land in Files as a wall of undated sha256s: a blob is addressed by its hash and
   * carries no name, and only the Files screen's own uploader was writing anything to the index. So
   * every picture posted from the composer was in the drive and unidentifiable in it.
   *
   * OPT-IN by design, not applied to every uploadBlob call: the Meme Builder alone uploads working
   * files (`erase.png`, `talk-src.png`, a baked layer) that are steps in a render, not documents, and
   * filing those would bury the real content they exist to produce.
   *
   * A folder is created on demand — the folder list is part of the same index, so this is one write,
   * and a `Posts` folder that only appears once something is in it is better than one shipped empty
   * on every account. Never `Music`: see _generatedIsMusic, that folder means encrypted-library. */
  function _fileUnder(url, file, folder, hash){
    try{
      const sha = hash || _shaFromUrl(url); if(!sha) return;
      if(folder && !FilesIdx.folders().includes(folder)) FilesIdx.addFolder(folder);
      FilesIdx.setFile(sha, { name:(file && file.name) || 'upload', folder,
                              mime:(file && file.type) || '', size:(file && file.size) || 0,
                              ts:Math.floor(Date.now()/1000) });
    }catch(_){}
  }
  // NIP-92: one `imeta` tag per uploaded media URL that appears in the note content, so other clients
  // render our images/video inline (right aspect ratio via dim) and can verify them (x = sha256).
  /* Returns imeta tags — AND, for a note carrying a mini app, one `t webxdc`.
   *
   * That hashtag is what makes an attached app FINDABLE. `imeta` is a multi-letter tag, so no relay
   * indexes it and no filter can ask for it: a game posted here could only ever be found by someone
   * who happened to scroll past the post. `#t` is indexed everywhere, so Games → Mini Apps can ask
   * the network the direct question. (Ditto's apps arrive as kind 1063, which is queryable by `#m`
   * and is the gallery's other source — see PCWebxdc.gallery.)
   *
   * Emitted from HERE rather than at each of the ten call sites, and rather than at attach time.
   * Ten copies of one rule is the shape this codebase has been bitten by repeatedly, and attach time
   * is too early: it would announce an app from a post the user then decided not to send. */
  function imetaTagsFor(content){
    const out=[], seen=new Set();
    for(let u of ((content||'').match(/https?:\/\/\S+/g)||[])){
      u=u.replace(/[)\].,>'"]+$/,'');               // drop trailing punctuation (e.g. markdown `![](url)`)
      if(seen.has(u)) continue; seen.add(u);
      const m=_MEDIA_META.get(u); if(!m) continue;
      const parts=['url '+u]; if(m.m) parts.push('m '+m.m); if(m.dim) parts.push('dim '+m.dim); if(m.x) parts.push('x '+m.x);
      // A webxdc mini app carries one more property: the identifier that makes two people the same
      // GAME. Without it the app still runs and its state goes nowhere — see NOSTR_WEBXDC.
      if(m['webxdc-topic']) parts.push('webxdc-topic '+m['webxdc-topic']);
      if(m.webxdc) parts.push('webxdc '+m.webxdc);
      if(m.summary) parts.push('summary '+m.summary);
      out.push(['imeta', ...parts]);
    }
    // One `t`, however many apps are in the post, and only when there is one.
    if(out.some(t => t.includes('m application/x-webxdc')||t.includes('m application/webxdc+zip')||t.includes('m application/vnd.webxdc+zip'))) out.push(['t', 'webxdc']);
    return out;
  }
  // ---- Blossom access (request-to-upload) ----
  /* Opening a drive is a read operation and must never wake a phone/remote signer. Upload access is
   * decided by the real, user-initiated PUT; its authenticated failure enters the request-access
   * flow below. A signed HEAD probe here made merely opening Blossom freeze behind “waiting for
   * signer”, and also spent a signature to predict what the authoritative PUT would say. */
  function _blossomDenied(err){ const m=String(err&&err.message||err||'').toLowerCase(); return m.includes('not authorized')||m.includes('403')||m.includes('privilege'); }
  let _blossomReqSent=false;
  // DM the instance operator asking for upload access; the admin grants it in Admin → Users.
  let _streamReqSent=false;
  async function requestStreamAccess(btn){
    // Same shape as requestBlossomAccess: DM the operator, once per session unless the user clicks.
    const op=safePk(S.CFG.operator_npub||'');
    if(!op){ toast('no admin contact is configured on this server'); return; }
    if(btn){ btn.disabled=true; btn.textContent='Sending…'; }
    const me=profOf(S.ME.pubkey)||{}; const nm=me.name||me.display_name||'A user';
    const body=`🔴 Live-streaming access request\n${nm} (${S.ME.npub}) would like permission to go live on ${location.host}. You can grant it in Admin → Users (🔴 Go Live).`;
    // RECORD it server-side as well as DMing. A DM alone is what Blossom does, and it loses the
    // request entirely if the admin never reads that inbox; the record shows up in the admin queue.
    try{
      const auth = await sign(27235, 'stream-request', [['p', S.ME.pubkey]]);
      await fetch('/client/stream-request', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: S.ME.pubkey, auth: btoa(JSON.stringify(auth)) }) });
    }catch(_){}
    try{ await sendDm(op, body); _streamReqSent=true; toast('✅ Request sent to the admin'); if(btn){ btn.textContent='✅ Request sent'; } }
    catch(e){ toast('could not send the request'); if(btn){ btn.disabled=false; btn.textContent='🔴 Request streaming access'; } }
  }
  async function requestBlossomAccess(btn){
    if(!btn && _blossomReqSent) return;   // auto-trigger (failed upload): only DM the admin once/session
    const op=safePk(S.CFG.operator_npub||'');
    if(!op){ if(btn) toast('no admin contact is configured on this server'); return; }
    if(btn){ btn.disabled=true; btn.textContent='Sending…'; }
    const me=profOf(S.ME.pubkey)||{}; const nm=me.name||me.display_name||'A user';
    const body=`🌸 Blossom upload-access request\n${nm} (${S.ME.npub}) would like permission to upload files on ${location.host}. You can grant it in Admin → Users.`;
    try{ await sendDm(op, body); _blossomReqSent=true; toast('✅ Request sent to the admin'); if(btn) btn.textContent='✅ Request sent'; }
    catch(e){ toast('could not send the request'); if(btn){ btn.disabled=false; btn.textContent='🌸 Request upload access'; } }
  }
  // Grid thumbnail. Images load a small server-side JPEG (?thumb=1) instead of the full file, and
  // videos show an icon rather than downloading the whole clip — both to save bandwidth in the grid.
  /* A blob's preview, on its own PATH rather than as ?thumb=1 on the blob itself.
   *
   * Caches key on the path and Cloudflare (and our own nginx) ignored the query, so `<sha>.mp4` and
   * `<sha>.mp4?thumb=1` shared ONE entry, pinned for a year by the `immutable` these carry. Whichever
   * was fetched first won. Measured through the public edge: the thumbnail URL returned 200 video/mp4,
   * 1.6 MB, cf-cache-status HIT — the whole film, handed to an <img>, which cannot decode it and falls
   * back to the 🎬 icon. Images never showed it: the same collision gives them the FULL-SIZE image,
   * which renders, so only video tiles looked broken and it seemed to depend on the browser.
   *
   * Falls back to the query form for a server that predates /thumb/ (the <img> onerror does the rest). */
  function thumbUrl(u){
    u = String(u || '');
    const q = u.indexOf('?');
    const path = q < 0 ? u : u.slice(0, q), rest = q < 0 ? '' : u.slice(q);
    const cut = path.lastIndexOf('/');
    if(cut < 0) return u + (q < 0 ? '?' : '&') + 'thumb=1';
    return path.slice(0, cut) + '/thumb/' + path.slice(cut + 1) + rest;
  }
  // A file's extension (no dot, lowercase): the real one off its name when we have one, else derived
  // from the URL or the MIME type. Blossom blobs are named by their sha256, so the name — ours from
  // the Files index, or the server's from the listing — is where the true extension survives.
  function extOfBlob(b, m){
    const nm=(m&&m.name)||(b&&b.name)||'';
    let e=(nm.match(/\.([A-Za-z0-9]{1,8})$/)||[])[1]||'';
    if(!e){ try{ e=(String((b&&b.url)||'').split('?')[0].match(/\.([A-Za-z0-9]{1,8})$/)||[])[1]||''; }catch(_){} }
    /* The type the SERVER reports for a blob is often generic (application/octet-stream — always, for ciphertext)
     * while the restored index remembers the real one. Take the first type that says something. */
    const _gen=x=>!x||x==='application/octet-stream';
    const _tb=((((b&&b.type)||'').split(';')[0])||'').trim().toLowerCase(), _tm=((((m&&m.mime)||'').split(';')[0])||'').trim().toLowerCase();
    const t=_gen(_tb)?(_tm||_tb):_tb;
    if(!e) e=_MIME_EXT[t]||'';
    if(!e){ const sub=(t.split('/')[1]||''); if(sub && !/octet-stream/.test(sub)) e=sub.replace(/^x-/,'').replace(/[^a-z0-9]/g,''); }
    return e.toLowerCase().slice(0,8);
  }
  // Card label that KEEPS the extension visible: a plain name.slice(0,18) cut it off the end of every
  // longish filename, so the drive showed no file types at all. Truncate the stem, never the suffix.
  /* The tile's caption. The card gives the name a full row and TWO lines (see .file-card .meta),
   * so the budget is about two lines' worth of characters — it was 20, from when the name shared one
   * line with the buttons, and 20 characters is "Quarterly report ….pdf" for every document anybody
   * actually has. The extension is always kept: it is the half that says what the file IS. */
  function fileLabel(nm, ext, size){
    if(!nm) return (ext?ext.toUpperCase()+' · ':'')+(((size||0)/1024|0)+'KB');
    const dot=nm.lastIndexOf('.'), hasExt = dot>0 && /^[A-Za-z0-9]{1,8}$/.test(nm.slice(dot+1));
    const stem = hasExt ? nm.slice(0,dot) : nm;
    const suf  = hasExt ? nm.slice(dot+1) : ext;      // name carries no extension → show the derived one
    const room = suf ? Math.max(12, 46-suf.length) : 46;
    return (stem.length>room ? stem.slice(0,room)+'…' : stem) + (suf?'.'+suf:'');
  }
  /* Turn a tile whose preview did not load into the icon it would have been.
   *
   * Both halves matter and both must be bound wherever blobThumb's markup is rendered. The video one
   * has always existed; the image one is newly needed because an untyped blob's type is now a GUESS
   * from its extension — and a wrong guess, or a format the server cannot preview (it answers 404
   * with a day of cache), otherwise leaves the browser's broken-image glyph on the card for that
   * long. A paperclip is what these showed before, and it is the honest answer when there is no
   * picture to draw. Shared, because it was bound in the Files grid and not in the attach picker,
   * which renders the identical markup. */
  function _bindThumbFallback(root){
    if(!root) return;
    const swap=(im, kind, dflt)=>{ const d=document.createElement('div'); d.className='file-icon';
      d.innerHTML=_fxFileGlyph(kind)+'<span>'+enc(im.dataset.ext||dflt)+'</span>'; im.replaceWith(d); };
    $$('.vthumb',root).forEach(im=> im.onerror=()=>swap(im,'video','video'));
    $$('.ithumb',root).forEach(im=> im.onerror=()=>swap(im,'file','file'));
  }
  function blobThumb(b, ext){
    let t=b.type||'';
    /* A blob stored with no type at all — or the octet-stream a typeless upload becomes — is still a
     * photograph if its name ends in .jpg. This decides the CARD, so without the fallback a picture
     * copied out of a synced folder drew a paperclip and never even requested a preview. The server
     * sniffs the bytes for the same reason, and the two have to agree: the client is what asks.
     *
     * The extension is resolved from the BLOB before the type is consulted. Deriving it from the
     * MIME first — as the ext argument's old default did — turns `application/octet-stream` into the
     * string "octet-stre", which matches nothing, so the fallback silently could not fire on any
     * caller that omits `ext`: the compose/DM attach picker calls `blobThumb(b)` and showed every
     * untyped photo as an identical anonymous tile. */
    if(!ext) ext = extOfBlob(b);
    if(!t || /^application\/octet-stream/i.test(t)){ const g = ext && mimeForName('x.' + ext); if(g) t = g; }
    ext=(ext||(t.split('/')[1]||'file')).slice(0,10);
    // onerror → the icon. A guessed type can be wrong, and a server that cannot make a preview
    // answers 404 with a day's cache; without this the card shows the browser's broken-image glyph
    // for as long as that lasts, which is strictly worse than the paperclip it replaced.
    if(/image/.test(t)) return `<img class="ithumb" data-ext="${enc(ext)}" src="${enc(thumbUrl(b.url))}" loading="lazy">`;
    // video: ffmpeg frame thumbnail (server ?thumb=1); falls back to a 🎬 icon if it can't be decoded
    if(/video/.test(t)) return `<img class="vthumb" data-ext="${enc(ext)}" src="${enc(thumbUrl(b.url))}" loading="lazy">`;
    const kind = /audio/.test(t) ? 'audio' : /zip|compress|tar|gzip|7z|rar/.test(t) ? 'archive'
      : /pdf/.test(t) ? 'pdf' : /text|json|xml|csv/.test(t) ? 'document' : 'file';
    return `<div class="file-icon">${_fxFileGlyph(kind)}<span>${enc(ext)}</span></div>`;
  }
  // What a downloaded blob should be SAVED as. A blob is addressed by its hash, so with no name
  // anywhere the honest fallback is a short hash plus the real extension — never a bare 64-char hex.
  function downloadName(b, nm, ext){
    nm=(nm||'').replace(/[\\/:*?"<>|]/g,'_').trim();
    ext=ext||extOfBlob(b);
    if(!nm) return (b.sha256||'file').slice(0,12)+(ext?'.'+ext:'');
    return /\.[A-Za-z0-9]{1,8}$/.test(nm) ? nm : nm+(ext?'.'+ext:'');
  }
  // Download URL: `download=1` makes the server answer with Content-Disposition: attachment, and
  // `filename=` hands it the name we know (the drive's index — the server only knows what the
  // uploading client told it). The <a download> attribute alone is ignored cross-origin, which is
  // exactly the case for a custom media server, so the query params are what actually do the work.
  function downloadUrl(url, name){
    if(!url || /[?&]download=1(&|$)/.test(url)) return url;   // idempotent: callers may chain through saveMedia
    const sep = url.indexOf('?')<0 ? '?' : '&';
    return url + sep + 'download=1' + (name?('&filename='+encodeURIComponent(name)):'');
  }

  async function saveBlobAs(blob, name){
    name=name||'file';
    if(_isNativeApp()){
      try{
        const P=(window.Capacitor&&Capacitor.Plugins)||{};
        if(P.Filesystem && P.Share){
          const b64=await _blobToB64(blob);
          const w=await P.Filesystem.writeFile({ path:name, data:b64, directory:'CACHE' });
          try{ await P.Share.share({ files:[w.uri], dialogTitle:'Save or share '+name }); }catch(_){}   // dismissing the sheet is a cancel, not a failure
          return 'shared';   // the sheet IS the confirmation — don't also claim "saved"
        }
      }catch(_){ /* fall through to the web path */ }
    }
    const u=URL.createObjectURL(blob), a=document.createElement('a');
    a.href=u; a.download=name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(u), 30000);
    return 'saved';
  }
  // Fetch bytes for saving. Credentials go ONLY to our own origin: a cross-origin request made WITH
  // credentials is rejected outright when the server answers `Access-Control-Allow-Origin: *` — which
  // ours does, and most media hosts do. That is what made "save failed" on anything not served by this
  // instance. A host that sends no CORS headers at all can't be read by the page either way, so those
  // go through our own SSRF-guarded proxy, which fetches them server-side.
  async function fetchMediaBlob(src){
    const org=_serverOrigin(), mine=!!org && String(src).indexOf(org+'/')===0;
    try{
      const r=await fetch(src, mine ? { headers:(S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}), credentials:'include' }
                                    : { credentials:'omit' });
      if(!r.ok) throw new Error('HTTP '+r.status);
      return { blob:await r.blob(), disp:r.headers.get('content-disposition')||'' };
    }catch(e){
      if(mine || !org) throw e;
      const r=await fetch(org+'/client/proxy-image?url='+encodeURIComponent(src));
      if(!r.ok) throw new Error('HTTP '+r.status);
      return { blob:await r.blob(), disp:'' };
    }
  }
  // Extension from the BYTES. The last resort, and the one that actually matters: a blob whose stored
  // MIME is application/octet-stream (anything uploaded by a client that didn't set Content-Type, and
  // every pre-2026 artifact) has no type on the server, no extension in its URL and no name — so the
  // only honest source left is its magic number. Reads 16 bytes, never the whole file.
  const _MAGIC = [
    [[0x89,0x50,0x4e,0x47], 0, 'png'], [[0xff,0xd8,0xff], 0, 'jpg'], [[0x47,0x49,0x46,0x38], 0, 'gif'],
    [[0x66,0x74,0x79,0x70], 4, 'mp4'],                       // ISO-BMFF: mp4 / m4v / mov all start here
    [[0x1a,0x45,0xdf,0xa3], 0, 'webm'],                      // matroska container (webm is the web one)
    [[0x4f,0x67,0x67,0x53], 0, 'ogg'], [[0x66,0x4c,0x61,0x43], 0, 'flac'], [[0x49,0x44,0x33], 0, 'mp3'],
    [[0x25,0x50,0x44,0x46], 0, 'pdf'], [[0x50,0x4b,0x03,0x04], 0, 'zip'],
    [[0x1f,0x8b], 0, 'gz'], [[0x37,0x7a,0xbc,0xaf], 0, '7z'], [[0x52,0x61,0x72,0x21], 0, 'rar'],
  ];
  async function sniffExt(blob){
    try{
      if(!blob || !blob.slice) return '';
      const b=new Uint8Array(await blob.slice(0,16).arrayBuffer());
      if(b.length>=12 && b[0]===0x52&&b[1]===0x49&&b[2]===0x46&&b[3]===0x46){   // RIFF: the tag at 8 says which
        const t=String.fromCharCode(b[8],b[9],b[10],b[11]);
        return t==='WEBP'?'webp' : t==='WAVE'?'wav' : t==='AVI '?'avi' : '';
      }
      for(const [sig,off,ext] of _MAGIC){
        if(b.length>=off+sig.length && sig.every((v,i)=>b[off+i]===v)) return ext;
      }
      if(b.length>=2 && b[0]===0xff && (b[1]&0xe0)===0xe0) return 'mp3';        // MPEG audio frame sync
    }catch(_){}
    return '';
  }
  // The name to save under, in order of trust: what the caller knows (the drive's index) → the
  // server's Content-Disposition → the URL's own basename. Whatever wins then gets the extension
  // implied by the BYTES if it has none — that is what stops an extensionless /blossom/<sha> link
  // (every note published before this change) from saving as a file the OS can't open. A bare
  // 64-hex hash isn't a filename either, so it's shortened.
  function fileNameFor(src, blob, preferred, disp, sniffed){
    let nm=String(preferred||'').trim();
    if(!nm && disp){
      const m=/filename\*=UTF-8''([^;]+)/i.exec(disp) || /filename="?([^";]+)"?/i.exec(disp);
      if(m){ try{ nm=decodeURIComponent(m[1]).trim(); }catch(_){ nm=m[1].trim(); } }
    }
    // Only when there IS a URL — resolving '' against location gives the PAGE's basename ("client"),
    // which would name every decrypted file after the app.
    if(!nm && src){ try{ nm=decodeURIComponent(new URL(src, location.href).pathname.split('/').pop()||''); }
                    catch(_){ nm=String(src).split('?')[0].split('/').pop()||''; } }
    nm=nm.split('?')[0].replace(/[\\/:*?"<>|]/g,'_').trim();
    // Shorten a bare content hash BEFORE the length clamp — clamping first leaves a 60-char stump
    // that no longer looks like a hash, and the user gets `6b81…b27.mp4` instead of `6b81597fab75.mp4`.
    const bare=nm.replace(/\.[A-Za-z0-9]{1,8}$/,'');
    if(/^[0-9a-f]{64}$/i.test(bare)) nm=bare.slice(0,12)+nm.slice(bare.length);
    nm=nm.slice(0,60) || 'file';
    if(!/\.[A-Za-z0-9]{1,8}$/.test(nm)){
      const e=sniffed||extOfBlob({type:(blob&&blob.type)||''});   // real bytes beat a claimed MIME
      if(e) nm+='.'+e;
    }
    return nm;
  }
  // THE save path — every download in the app goes through here (Files, Music, the lightbox, AI files).
  // No success toast: the browser's own save dialog / download shelf, or the OS share sheet, IS the
  // confirmation — claiming "saved" before the user has even picked a folder is a lie.
  //
  // Two routes, and picking the right one matters:
  //   * OUR OWN origin in a browser → hand the URL to the browser with `download`. It streams
  //     straight to disk, so saving a 4 GB recording costs no memory; `?download=1` makes the server
  //     answer `Content-Disposition: attachment` with a real filename (it even sniffs the bytes for a
  //     blob whose MIME is generic), and same-origin is exactly where the `download` attribute works.
  //   * anything else (cross-origin, the APK where every URL is cross-origin, decrypted bytes) →
  //     read it into a blob, because that's the only way the name survives, and save that.
  async function saveMedia(src, preferred){
    if(!src) return false;
    const org=_serverOrigin(), mine=!!org && String(src).indexOf(org+'/')===0;
    // …except an AI-chat artifact under /api/, which is Bearer-gated: a plain navigation carries
    // cookies but not the token, so that one has to be fetched with the header (401 otherwise).
    const bearer = !!S._aiToken && /\/api\//.test(String(src));
    if(mine && !bearer && !_isNativeApp()){
      const a=document.createElement('a');
      a.href=downloadUrl(src, preferred); a.download=preferred||'';   // empty → the server's filename wins
      document.body.appendChild(a); a.click(); a.remove();
      return true;
    }
    try{
      const { blob, disp } = await fetchMediaBlob(src);
      await saveBlobAs(blob, fileNameFor(src, blob, preferred, disp, await sniffExt(blob)));
      return true;
    }catch(e){ toast('save failed: '+((e&&e.message)||e)); return false; }
  }
  // A Blossom blob by URL: ask the server for the download disposition too, so even a blob we know
  // nothing about comes back named.
  function downloadBlobFile(url, name){ return saveMedia(downloadUrl(url,name), name); }
  // An ENCRYPTED file: decrypt in the browser first — the URL only ever holds ciphertext.
  async function saveEncrypted(sha, name){
    try{
      toast('decrypting…');
      const blob=await fetch(await trackUrl(sha)).then(r=>r.blob());
      await saveBlobAs(blob, fileNameFor('', blob, name, '', await sniffExt(blob)));
      return true;
    }catch(e){ toast('save failed: '+((e&&e.message)||e)); return false; }
  }
  function copyUrl(u){ try{ u=new URL(u, location.href).href; }catch(_){}
    copyValue(u, 'URL copied', 'URL:'); }
  function gifPicker(ta){
    /* A SUB-modal: it opens over the composer's own modal, so it needs to sit above it. Expressed as
     * a class rather than an inline z-index, because an inline value beats every stylesheet rule —
     * including the one that lifts modals above the PosterChan OS desktop, which is why attaching
     * from Blossom silently did nothing there while Local (a native file input) worked. */
    const bg=document.createElement('div'); bg.className='modal-bg modal-sub';
    bg.innerHTML=`<div class="modal glass neon-border"><h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-film"></use></svg>GIFs</h3><input class="input" id="gif-q" placeholder="search GIFs…" autocomplete="off"><div id="gif-grid" class="gif-grid"><div class="spinner"></div></div></div>`;
    bg.onclick=e=>{ if(e.target===bg) bg.remove(); };
    $('#modal-root').appendChild(bg);
    const grid=bg.querySelector('#gif-grid'), q=bg.querySelector('#gif-q'); let t=null;
    async function load(query){
      grid.innerHTML='<div class="spinner"></div>';
      /* Ask the CONNECTED INSTANCE explicitly. Its Giphy/Tenor key stays server-side; the APK's
       * https://localhost bundle and Electron's app:// origin own no key and must never answer this
       * relative to themselves. Most bundled builds also install a root-URL fetch shim, but the GIF
       * picker is shared with web/test shells where depending on that invisible rewrite made the
       * selected instance an accident rather than part of this request. */
      const base=_instanceBase();
      let j={};
      try{
        if(!base) throw new Error('no connected instance');
        const r=await fetch(base+'/client/gif?q='+encodeURIComponent(query||''),
                            {credentials:'include'});
        j=r.ok ? await r.json() : {error:'http_'+r.status};
      }catch(_){ j={error:'unreachable'}; }
      const rs=j.results||[];
      if(!rs.length){ grid.innerHTML='<div class="empty">'+(j.error?'GIF search is unavailable on this connected instance. Add a Giphy or Tenor key in its Admin settings.':'No GIFs.')+'</div>'; return; }
      grid.innerHTML=rs.map(g=>`<img class="gif-item" src="${enc(g.preview)}" data-url="${enc(g.url)}" loading="lazy">`).join('');
      grid.querySelectorAll('.gif-item').forEach(im=> im.onclick=()=>{ ta.value+=(ta.value?'\n':'')+im.dataset.url+' '; ta.dispatchEvent(new Event('input',{bubbles:true})); bg.remove(); toast('GIF added'); });
    }
    q.oninput=()=>{ clearTimeout(t); t=setTimeout(()=>load(q.value.trim()),350); };
    load(''); q.focus();
  }
  // `ta` mode (default): tapping a file appends its media URL to the textarea (post/DM composer).
  // `onPick` mode: instead of pasting a link, call onPick({url,type,ext}) with the chosen blob — used
  // by AI chat, which fetches the bytes into a real attachment rather than a URL in the message text.
  // `opts.filter(blob)` narrows what's offered (Meme Builder: images/videos only) and `opts.title`
  // renames the header. Callers that only want a SUBSET must come through here rather than rolling
  // their own grid — the folder bar, the encrypted-blob hygiene and the thumbnails all live here, and
  // a private copy silently ships a flat, folderless drive (which is exactly what the meme picker did).
  function blossomPicker(ta, onPick, opts={}){
    const server=mediaServer(); if(!server){ toast('no media server set'); return; }
    /* STATE LIVES ON THE PICKER, NOT BESIDE IT.
       `scripts/check_blossom_picker_mobile.py` LIFTS this function out of app.js and runs it against
       a stub page, so a `const` added at module scope is simply not there — and the way that failed
       was not a ReferenceError anybody could read. It was thrown inside the listing's own catch,
       which then threw again on the undefined it was trying to set, so the listing resolved empty
       and the audit died three steps later on a card that was never drawn
       (`Cannot read properties of undefined (reading 'getBoundingClientRect')`). Anything this
       function needs has to be reachable FROM this function. The cache hangs off the function object
       because it must outlive one open; the failure note is per-open and is an ordinary local. */
    const BP_LIST_LIMIT=2000, BP_LIST_TTL=5*60*1000;
    let _bpListFailed='';
    /* A SUB-modal: it opens over the composer's own modal, so it needs to sit above it. Expressed as
     * a class rather than an inline z-index, because an inline value beats every stylesheet rule —
     * including the one that lifts modals above the PosterChan OS desktop, which is why attaching
     * from Blossom silently did nothing there while Local (a native file input) worked. */
    const bg=document.createElement('div'); bg.className='modal-bg modal-sub bp-picker-bg';
    bg.innerHTML=`<div class="modal glass neon-border bp-modal bp-file-picker">
      <div class="bp-head"><button type="button" class="mini bp-locations" aria-expanded="false">☰ Locations</button><h3>${enc(opts.title||'📁 Choose from File Manager')}</h3>
        <label class="bp-sort"><span>Sort</span><select aria-label="Sort files"><option value="date">Newest</option><option value="name">Name</option><option value="size">Size</option></select></label>
        <span class="bp-density" aria-label="Thumbnail size"><button type="button" class="mini active" data-bp-size="small" aria-pressed="true" title="Small thumbnails">S</button><button type="button" class="mini" data-bp-size="medium" aria-pressed="false" title="Medium thumbnails">M</button></span>
        <button type="button" class="mini bp-close" aria-label="Cancel file selection">×</button></div>
      <div class="bp-explorer"><nav id="bp-folders" class="bp-folders" aria-label="Blossom folders"></nav>
        <div id="bp-grid" class="files-grid"><div class="spinner"></div></div></div></div>`;
    // This sheet is hand-rolled rather than built by modal()/subModal(), so it used to get NONE of what
    // they provide: no Escape, no focus trap, and body.modal-open was never set (so `/` and the other
    // global keys still fired at the view behind it). One close path now does all of it.
    const close=()=>{ bg.remove(); const root=$('#modal-root');
      if(!root || !root.children.length) document.body.classList.remove('modal-open'); };
    bg.onclick=e=>{ if(e.target===bg) close(); };
    $('#modal-root').appendChild(bg);
    document.body.classList.add('modal-open');
    bg.querySelector('.bp-close').onclick=close;
    const closeOrDrawer=()=>{ const explorer=bg.querySelector('.bp-explorer');
      if(explorer&&explorer.classList.contains('bp-locations-on')){ explorer.classList.remove('bp-locations-on'); const b=bg.querySelector('.bp-locations'); if(b)b.setAttribute('aria-expanded','false'); return; }
      close(); };
    _trapFocus(bg.querySelector('.modal'), closeOrDrawer);
    // The grid is a grid of DIVs — nothing focusable, so Tab could never reach a file and Enter had
    // nothing to press. _popKeys is the app's grid cursor (arrows, and hjkl when Vim keys are on, with
    // the row length measured from the layout), the same one the effects and emoji pickers use.
    /* _popKeys owns Escape in capture phase, before the focus trap can see it. Give both handlers
     * the same layered close operation or Escape/keyboard Back with Locations open destroys the
     * whole picker instead of dismissing the drawer first. */
    _popKeys(bg, '#bp-grid .file-card', el=>el.click(), closeOrDrawer);
    bg.querySelectorAll('[data-bp-size]').forEach(btn=>btn.onclick=()=>{
      const medium=btn.dataset.bpSize==='medium', explorer=bg.querySelector('.bp-explorer');
      explorer.classList.toggle('bp-medium',medium);
      bg.querySelectorAll('[data-bp-size]').forEach(x=>{ const on=x===btn; x.classList.toggle('active',on); x.setAttribute('aria-pressed',on?'true':'false'); });
    });
    FilesIdx.loadLocal();
    (async()=>{
      /* The public blob list and the encrypted folder index are independent sources. Fetch them in
       * parallel: a relay that stalls while loading the index must not leave "All" as a permanent
       * spinner even though the Blossom server is healthy. Folder metadata can catch up later; the
       * complete root listing is the reliable fallback. */
      /* THE LISTING IS BOUNDED AND CACHED, AND IT CAN NEVER SPIN FOR EVER.
         A full BUD-02 listing here is 37,400 blobs / 9.7 MB (measured), refetched with
         `cache:'no-store'` on EVERY open and awaited with no timeout — so on a busy renderer the
         picker was a permanent spinner ("trying to attach files from Blossom, and circle ....never
         loading"). Three changes: ask for the newest slice (the server takes a `limit`), remember
         the answer for a few minutes so reopening is instant, and give the fetch a deadline so the
         sheet always resolves into files or a reason, never a circle. */
      const listing=(async()=>{
        const held=blossomPicker._listCache;
        if(held && held.pubkey===S.ME.pubkey && (Date.now()-held.at)<BP_LIST_TTL) return held.rows;
        const stop=new AbortController();
        const deadline=setTimeout(()=>stop.abort(),25000);
        try{
          const r=await fetch(server+'/list/'+S.ME.pubkey+'?limit='+BP_LIST_LIMIT,
                              {cache:'no-store',signal:stop.signal});
          if(!r.ok)return [];
          const body=await r.json();
          const rows=Array.isArray(body)?body:(Array.isArray(body&&body.blobs)?body.blobs:[]);
          blossomPicker._listCache={pubkey:S.ME.pubkey,at:Date.now(),rows};
          return rows;
        }catch(err){
          // An aborted or failed listing must SAY so. Returning [] made "your drive is empty" and
          // "the server never answered" the same screen, and the empty one is a lie.
          _bpListFailed = (err && err.name==='AbortError')
            ? 'the file list took too long to load'
            : 'could not read your files'+(err&&err.message?': '+err.message:'');
          return [];
        }finally{ clearTimeout(deadline); }
      })();
      // Folder names live in the encrypted Files index, which is only fetched when you OPEN Files —
      // so without this pull the picker showed a flat drive to anyone who hadn't been there yet.
      try{ await Promise.race([FilesIdx.ensure(),new Promise(resolve=>setTimeout(resolve,4000))]); }catch(_){ }
      let list=[]; try{
          const rows=await listing;
          if(Array.isArray(rows)) list=rows.filter(b=>b&&b.sha256).map(b=>Object.assign({},b,{
            /* A Blossom list entry is allowed to omit `url`. File Manager already canonicalises
             * that response; the picker did not, so All rendered cards whose image and click URL
             * were both empty on a conforming server. Keep the two consumers byte-for-byte aligned. */
            url:b.url || (server.replace(/\/$/,'')+'/'+b.sha256)
          }));
      }catch(_){}
      // Same filter as the Files grid: hide the octet-stream noise (encrypted ciphertext, stale/live
      // index blobs, unnamed binaries) — none of it renders as media in a post, and it floods the picker.
      /* Named, because a folder's rows no longer all come from the listing (see `_folderRows`) and a
       * synthesised one must pass exactly the same hygiene — otherwise ciphertext, index blobs and
       * the caller's own narrowing apply to half the grid. */
      const _keep = b => {
        if(b.sha256===FilesIdx._lastIndexSha) return false;        // the encrypted Files index blob itself
        const m=FilesIdx.meta(b.sha256);
        // Encrypted ciphertext is not publicly viewable, so it stays out unless the caller has said
        // it can decrypt and re-publish what it picks (see `allowEncrypted` above).
        if(m && m.enc && !opts.allowEncrypted) return false;
        if(!m && /octet-stream/.test(b.type||'') && !mimeForName(b.name||'')) return false;
        if(!opts.allowEncrypted && FilesIdx.isEncFolder(FilesIdx.folderOf(b.sha256))) return false;   // ditto for a whole encrypted folder
        /* Normalize old/typeless Blossom rows before a caller filters them. The Files index knows
         * the original name and MIME; applying `video/*` to the server's octet-stream placeholder
         * made an MP4 visible in Files but absent from Texts' Attach Files picker. */
        if(m){
          if(!b.name && m.name) b.name=m.name;
          if((!b.type || /^application\/octet-stream/i.test(b.type)) && m.mime) b.type=m.mime;
        }
        if(!b.type || /^application\/octet-stream/i.test(b.type))
          b.type=mimeForName(b.name||'')||b.type||'';
        if(opts.filter && !opts.filter(b)) return false;            // caller's own narrowing (e.g. media only)
        return true;
      };
      list = list.filter(_keep);
      const grid=bg.querySelector('#bp-grid'), fbar=bg.querySelector('#bp-folders'), explorer=bg.querySelector('.bp-explorer'), locations=bg.querySelector('.bp-locations');
      locations.onclick=()=>{ const open=explorer.classList.toggle('bp-locations-on'); locations.setAttribute('aria-expanded',open?'true':'false'); };
      explorer.addEventListener('click',e=>{ if(explorer.classList.contains('bp-locations-on')&&!e.target.closest('.bp-folders')&&!e.target.closest('.bp-locations')){ e.preventDefault();e.stopPropagation();closeOrDrawer(); } },true);
      // The folder bar represents the encrypted index, not this request's filtered blob listing.
      // Hiding a folder with no currently selectable public blob made real folders (for example a
      // Social folder containing encrypted items) appear deleted after a partial list or filter.
      /* MUSIC IS AN ENCRYPTED FOLDER, and that is why it was in no picker in the whole app.
       *
       * `isEncFolder()` answers TRUE for the literal name 'Music' — correct, because tracks ARE
       * stored encrypted — and every picker filtered the folder bar with `!isEncFolder(f)`. So the
       * one folder a Meme Builder user most wants was the one folder that could never be opened.
       * Measured on a real drive: the picker listed 22 folders, all of them, except Music.
       *
       * That also contradicted a deliberate decision recorded in meme.js: the dedicated
       * "🎵 Music or a voice-over" entry was REMOVED on the grounds that "music is where every
       * other file is ... pickBlossom's filter already allows it". It did not.
       *
       * `allowEncrypted` is opt-in, so every existing caller is byte-for-byte unchanged — a
       * composer that publishes a URL still must not offer ciphertext nobody else can fetch. */
      const folders=[['','🗂 All']].concat(
        FilesIdx.folders().filter(f=>opts.allowEncrypted||!FilesIdx.isEncFolder(f)).map(f=>[f,'📁 '+f]));
      let cur='', sort='date';
      /* A FOLDER'S CONTENTS COME FROM THE INDEX, NOT FROM THE NEWEST-N WINDOW.
       *
       * The listing above is deliberately bounded (`?limit=2000`) because a full BUD-02 listing on
       * this deployment is 37,483 blobs / 9.7 MB and awaiting it made the picker a permanent
       * spinner. But the grid then filtered that WINDOW by folder, so every folder whose files are
       * older than the newest 2000 rendered as "Nothing in this folder." — a drive that plainly has
       * the files, reported empty. Measured on a real account:
       *
       *     Memes 79 files -> 0 shown      Anime 75 -> 0      Blacks 60 -> 0
       *     Jews  60       -> 0            Notes 1146 -> 0    Music 2444 -> 0
       *     Messages 2903  -> 89 shown     Posts 50 -> 48
       *
       * i.e. almost every folder in the attach sheet was empty. That is a worse failure than the
       * spinner it replaced: a hang is visibly broken, and this looks like the files are gone.
       *
       * The encrypted index already holds every file's hash, name, type, size and folder — it is
       * the thing that KNOWS what is in a folder, and it is local. So a folder is drawn from it,
       * with any listing row we happen to hold preferred (it carries the server's own url/size),
       * and nothing is fetched. "All" keeps the bounded window, which is what makes it instant. */
      const _folderRows = f => {
        if(!f) return list;
        const have = new Map(list.map(b => [b.sha256, b]));
        const out = list.filter(b => (FilesIdx.folderOf(b.sha256) || '') === f);
        const files = (FilesIdx._norm && FilesIdx._norm().files) || {};
        for(const sha in files){
          if(have.has(sha)) continue;                       // already in the window, already listed
          const m = files[sha] || {};
          if((m.folder || '') !== f) continue;
          const row = { sha256: sha, url: server.replace(/\/$/,'') + '/' + sha,
                        size: Number(m.size) || 0, type: m.mime || mimeForName(m.name || '') || '',
                        name: m.name || '', uploaded: Number(m.ts) || 0 };
          if(_keep(row)) out.push(row);                     // same hygiene as every listing row
        }
        return out;
      };
      const draw=()=>{
        const shown=_folderRows(cur).slice().sort((a,b)=>{
          if(sort==='name')return String((FilesIdx.meta(a.sha256)||{}).name||a.name||'').localeCompare(String((FilesIdx.meta(b.sha256)||{}).name||b.name||''));
          if(sort==='size')return (Number(b.size)||0)-(Number(a.size)||0);
          return (Number(b.uploaded||b.created_at)||0)-(Number(a.uploaded||a.created_at)||0);
        });
        grid.innerHTML = shown.length ? shown.map(b=>{
          const m=FilesIdx.meta(b.sha256)||{};
          const ext=extOfBlob(b,m), name=m.name||b.name||downloadName(b,'',ext);
          const type=(b.type&&!/^application\/octet-stream/i.test(b.type))
                     ? b.type : (m.mime||mimeForName(name)||b.type||'');
          /* A picker tile must identify the file, not merely draw it. On a phone two similar video
           * frames (and every document icon) are otherwise indistinguishable, while the Files view
           * itself already carries the name and size. Keep the same metadata directly under the
           * preview and return it to the caller so an MMS part is not named with its blob hash. */
          const when=Number(b.uploaded||b.created_at)||0;
          return `<button type="button" class="file-card bp-pick-card" data-url="${enc(b.url)}" data-type="${enc(type)}" data-name="${enc(name)}" data-sha="${enc(b.sha256)}"><span class="bp-pick-preview">${blobThumb(Object.assign({},b,{type}),ext)}</span><span class="meta"><b class="fname">${enc(fileLabel(name,ext,b.size))}</b><small>${enc(_fmtBytes(b.size||0))}${type?' · '+enc(type.replace(/;.*/,'')):''}</small>${when?`<small class="bp-pick-date">${enc(new Date(when*1000).toLocaleDateString())}</small>`:''}</span></button>`;
        }).join('')
          /* "I could not ask" is never "you have nothing" — the same rule the drive check and the
             admin store scan follow. An empty grid after a failed listing used to read as an empty
             drive, which is the most alarming possible way to report a timeout. */
          : `<div class="empty">${_bpListFailed
              ? enc(_bpListFailed)+' <button type="button" class="mini bp-retry">Retry</button>'
              : (cur?'Nothing in this folder.':enc(opts.empty||'No files yet — upload some in the Files tab.'))}</div>`;
        _bindThumbFallback(grid);   // same markup as the Files grid, so the same fallback
        grid.querySelectorAll('[data-url]').forEach(el=> el.onclick=()=>{
          const type=el.dataset.type||'';
          // MIME parameters describe the representation, not a different file type. Exact lookup
          // turned `application/pdf; charset=binary` into no extension, so Texts and other picker
          // callers received an otherwise-openable document without its useful filename suffix.
          const bareType=type.replace(/;.*/, '').trim().toLowerCase();
          const ext=_MIME_EXT[bareType]||''; const url=el.dataset.url;
          const name=el.dataset.name||'';
          /* `sha` and `enc` ride along for a caller that asked for encrypted files: `url` is the
           * CIPHERTEXT address, which is useless on its own, so such a caller needs the hash to
           * decrypt from and a flag telling it that it must. Callers that did not opt in never see
           * an encrypted blob, so these two fields change nothing for them. */
          const sha=el.dataset.sha||''; const isEnc=!!((FilesIdx.meta(sha)||{}).enc);
          close();
          /* A CALLBACK THAT THREW USED TO BE INDISTINGUISHABLE FROM A FILE NOBODY PICKED.
           * `catch(_){}` swallowed everything, so a caller whose insert failed left the picker
           * closing over a composer that never changed — no error, no toast, nothing in the
           * console. It is what hid the Concord attach bug for as long as it existed. The catch
           * stays (a throwing caller must not break the picker) but it SAYS so. */
          if(onPick){ try{ onPick({url, type, ext, name, sha, enc:isEnc}); }
                      catch(e){ console.error('[blossomPicker] the caller could not take that file', e);
                                toast('could not attach that file'+(e&&e.message?': '+e.message:'')); }
                      return; }
          // ...only when the URL doesn't already carry one — the server's listing now includes the
          // extension, and appending unconditionally produced "…/<sha>.png.png" (which still served,
          // but is what every other client shows in the note).
          const _need = ext && !/\.[a-z0-9]{1,8}$/i.test(url.split('?')[0]);
          ta.value+=(ta.value?'\n':'')+url+(_need?('.'+ext):'');
          ta.dispatchEvent(new Event('input',{bubbles:true})); toast('attached'); });

      };
      const sorter=bg.querySelector('.bp-sort select');
      if(sorter)sorter.onchange=()=>{sort=sorter.value||'date';draw();};
      // The picker follows File Manager's navigation instead of maintaining a third, flat UI. The
      // Blossom root and its folders are a real collapsible tree; importantly, the full folder name
      // stays visible instead of being hidden inside a native select whose state was easy to miss.
      fbar.innerHTML = `<section class="fx-tree-node"><button type="button" class="fx-tree-head active" id="bp-tree-toggle" aria-expanded="true"><span class="chev">▾</span><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg><b>Files</b></button>
        <div class="fx-tree-children" id="bp-tree-children"><div class="folder-bar">${folders.map(([v,l])=>
          `<button type="button" class="folder-chip${v===''?' active':''}" data-folder="${enc(v)}">${enc(l)}</button>`).join('')}</div></div></section>`;
      const toggle=fbar.querySelector('#bp-tree-toggle'), children=fbar.querySelector('#bp-tree-children');
      toggle.onclick=()=>{ const open=!children.classList.toggle('hidden'); toggle.setAttribute('aria-expanded',open?'true':'false'); toggle.querySelector('.chev').textContent=open?'▾':'▸'; };
      fbar.querySelectorAll('[data-folder]').forEach(btn=>btn.onclick=()=>{
        cur=btn.dataset.folder||'';
        fbar.querySelectorAll('[data-folder]').forEach(x=>x.classList.toggle('active',x===btn));
        draw(); explorer.classList.remove('bp-locations-on'); locations.setAttribute('aria-expanded','false');
      });
      draw();
    })();
  }

  // ---------- Pics: a picture-first feed (NIP-68 kind-20 + image notes) as a media grid ----------
  function _firstImage(ev){
    for(const t of (ev.tags||[])){
      if((t[0]==='url'||t[0]==='image') && /^https?:\/\//i.test(t[1]||'')) return t[1];
      if(t[0]==='imeta'){ const u=(t.find(x=>/^url\s/i.test(x))||'').replace(/^url\s+/i,''); if(/^https?:\/\//i.test(u)) return u; }
    }
    const m=(ev.content||'').match(/https?:\/\/[^\s)<]+\.(?:jpe?g|png|gif|webp|avif)(?:\?[^\s)<]*)?/i);
    return m?m[0]:null;
  }
  async function renderPics(){
    const feed=$('#feed');
    feed.innerHTML='<div class="pics-grid" id="pics-grid"><div class="spinner"></div></div>';
    let evs=[];
    try{ evs=await Relay.query([{kinds:[20], limit:80},{kinds:[1], limit:160}]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(S.VIEW!=='pics') return;
    const pics=[]; const seen=new Set();
    for(const e of evs.sort((a,b)=>b.created_at-a.created_at)){
      if(e.kind===1 && isReply(e)) continue;
      if(isMutedView(e)) continue;
      const img=_firstImage(e); if(!img || seen.has(e.id)) continue;
      seen.add(e.id); pics.push({e,img});
      if(pics.length>=120) break;
    }
    const grid=$('#pics-grid'); if(!grid) return;
    grid.innerHTML = pics.length ? pics.map(x=>`<div class="pic-card" data-id="${x.e.id}">${_hold(`<img src="${enc(x.img)}" loading="lazy" onerror="this.closest('.pic-card')&&this.closest('.pic-card').remove()">`, x.img)}</div>`).join('') : '<div class="empty">No pics found yet.</div>';
    $$('.pic-card',grid).forEach(c=> c.onclick=()=> openThread(c.dataset.id));
  }

  return {
    _bindThumbFallback, _blossomDenied, _fileUnder, _firstImage, _signUploadBatch, blobThumb,
    blossomPicker, compressImage, compressVideo, copyUrl, downloadBlobFile, downloadName,
    extOfBlob, fetchMediaBlob, fileFromBytes, fileLabel, fileNameFor, gifPicker, imetaTagsFor,
    mimeForName, renderPics, requestBlossomAccess, requestStreamAccess, saveBlobAs, saveEncrypted,
    saveMedia, sha256hex, sniffExt, thumbUrl, uploadBlob,
  };
};

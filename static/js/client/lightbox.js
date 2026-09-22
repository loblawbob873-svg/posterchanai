/* The lightbox — full-screen image and video viewing with zoom/pan, the gallery swipe, copy image,
 * save, save to Blossom, and the native (APK/desktop) share/open paths it uses. Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_lightboxDeps`) and
 * builds this factory the first time a picture or video is opened full screen. The code below is
 * BYTE-IDENTICAL to what it replaced in app.js apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets.
 *
 * Stayed in app.js: _lbGroup (the feed's click handler gathers a post's media with it),
 * _isNativeApp and _blobToB64 (saving files uses them everywhere).
 */
window.PCLightboxFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.GUEST, S.ME, S._aiToken
  const {
    _blobToB64, _blossomDenied, _guestPrompt, _isNativeApp, _serverOrigin, _trapFocus, copyValue,
    fetchMediaBlob, fileNameFor, requestBlossomAccess, saveMedia, sniffExt, toast, uploadBlob,
  } = dep;
  // Re-host the media you are looking at onto your own Blossom server, then copy the new URL.
  async function _lbToBlossom(src){
    if(S.GUEST || !S.ME){ _guestPrompt&&_guestPrompt(); return; }
    try{
      toast('saving to Blossom…');
      /* Through the app's ONE fetch pipeline, the same as Save-to-disk.
       *
       * This did its own `fetch(src)` — and a third-party host that sends no CORS headers cannot be
       * read by the page AT ALL, so the browser threw "Failed to fetch" before Blossom was ever in
       * the picture. Which host you happened to pick decided whether the button worked: reported as
       * "unable to save photos from Web Search → Images … some images worked", and measured — of
       * four real image results, two threw and two did not, purely on their CORS headers.
       *
       * fetchMediaBlob already solved this for the Save button: our origin gets credentials, a
       * third-party host gets a credential-less try, and anything the browser refuses goes through
       * our own SSRF-guarded proxy, which fetches it server-side where CORS does not apply. There is
       * no reason for two buttons on the same toolbar to disagree about how to read the same
       * picture. fileNameFor comes with it, so the upload is named from the bytes rather than from a
       * URL's last segment — which for a Blossom link is a bare sha256 with no extension. */
      const { blob, disp } = await fetchMediaBlob(src);
      const name = fileNameFor(src, blob, '', disp, await sniffExt(blob)) || 'media';
      const url=await uploadBlob(new File([blob], name, { type: blob.type || 'application/octet-stream' }));
      await copyValue(url, 'saved to Blossom — link copied', 'Saved. The link:');
    }catch(err){
      if(typeof _blossomDenied==='function' && _blossomDenied(err)){ requestBlossomAccess(); toast('🔒 No upload access — requested it from the admin.'); }
      else toast('Files save failed: '+((err&&err.message)||err));
    }
  }

  function openLightbox(src, kind, group){
    // Always full-res, never the grid's preview. BOTH spellings: the old ?thumb=1 (still served, and
    // still in older clients' caches) and the /thumb/ path that replaced it — dropping only the query
    // would open the 320px JPEG in the lightbox.
    const norm=(u)=>{ try{ const x=new URL(u, location.href); x.searchParams.delete('thumb');
      x.pathname = x.pathname.replace(/\/thumb\/([^/]+)$/, '/$1');
      return x.href; }catch(_){ return u; } };
    const multi = !!(group && group.items && group.items.length>1);
    const items = multi ? group.items.map(it=>({ src:norm(it.src), kind:it.kind }))
                        : [{ src:norm(src), kind:kind||null }];
    let idx = multi ? Math.max(0, Math.min(items.length-1, group.i||0)) : 0;

    const bg=document.createElement('div'); bg.className='lightbox';
    // Reveal the actions on pointer MOVEMENT and let them fade again once you settle — see the .lb-bar
    // rules. Nothing to undo on touch: the media query leaves the bar permanently visible there, so this
    // only ever adds a class nobody is looking at.
    let hotT=null;
    const hot=()=>{ bg.classList.add('lb-hot'); clearTimeout(hotT);
      hotT=setTimeout(()=>bg.classList.remove('lb-hot'), 2200); };
    bg.addEventListener('mousemove', hot);
    const close=()=>{ clearTimeout(hotT); try{ bg.remove(); }catch(_){} document.removeEventListener('keydown', onKey); };
    const onKey=(e)=>{
      if(e.ctrlKey||e.metaKey||e.altKey) return;
      // Any key means someone is driving this from the keyboard — hold the toolbar open, since on a
      // pointer device it is otherwise only revealed by hovering, which a keyboard user never does.
      bg.classList.add('lb-keys');
      if(e.key==='Escape'){ e.preventDefault(); close(); return; }
      // The toolbar's actions as single keys. They were reachable only by pointing at them: the buttons
      // are real, but nothing gave you a way to run one without a mouse.
      const k=(e.key||'').toLowerCase();
      if(k==='c'){ e.preventDefault(); _lbCopyImg(items[idx].src); return; }
      if(k==='s'){ e.preventDefault(); _lbSaveMedia(items[idx].src); return; }
      if(k==='b'){ e.preventDefault(); _lbToBlossom(items[idx].src); return; }
      if(items.length<2) return;
      if(e.key==='ArrowRight'){ e.preventDefault(); go(1); }
      else if(e.key==='ArrowLeft'){ e.preventDefault(); go(-1); }
    };
    document.addEventListener('keydown', onKey);

    // Always-tappable toolbar — a full-screen image leaves NO backdrop to tap, so mobile couldn't close it.
    const bar=document.createElement('div'); bar.className='lb-bar';
    const mkBtn=(label,title,fn)=>{ const b=document.createElement('button'); b.className='lb-btn'; b.type='button'; b.textContent=label; b.title=title; b.setAttribute('aria-label',title); b.onclick=(e)=>{ e.stopPropagation(); fn(); }; return b; };
    // Read items[idx] at CLICK time, not now — the toolbar outlives each individual slide.
    const copyB=mkBtn('⧉','Copy image  (C)', ()=>_lbCopyImg(items[idx].src));
    const saveB=mkBtn('⤓','Save image  (S)', ()=>_lbSaveMedia(items[idx].src));
    // Keep a copy on YOUR Blossom. Media in a feed lives on whatever host the author used and can vanish;
    // this re-hosts it under your own storage and hands back the link. Offered for video/audio too — the
    // blob path is identical and "save that clip" is the same wish as "save that image".
    // A MONOCHROME florette (U+2740), not the 🌸 emoji: the emoji keeps its own pink and clashes with the
    // button's neon-gradient fill, where every other lb-btn is a clean white glyph. ︎ forces text (not
    // emoji) presentation so it stays white on every platform.
    const blossomB=mkBtn('📁','Save to Files  (B)', ()=>_lbToBlossom(items[idx].src));
    bar.appendChild(copyB); bar.appendChild(saveB); bar.appendChild(blossomB); bar.appendChild(mkBtn('✕','Close  (Esc)', close));
    bg.appendChild(bar);
    // Tab stays on the toolbar instead of walking the page behind the image, and the buttons are then
    // reachable without knowing the letters. `close` is passed so it removes THIS overlay — the lightbox
    // is not a .modal-bg and must not go through closeModal().
    _trapFocus(bg, close);

    let cur=null;
    const render=()=>{
      const it=items[idx]; const isImg = it.kind!=='video' && it.kind!=='audio';
      let el;
      if(it.kind==='video'){ el=document.createElement('video'); el.src=it.src; el.controls=true; el.playsInline=true; el.setAttribute('playsinline',''); el.preload='metadata'; }   // NO autoplay — opening media must never start playing on its own
      else if(it.kind==='audio'){ el=document.createElement('audio'); el.src=it.src; el.controls=true; el.preload='metadata'; }   // NO autoplay
      else { el=document.createElement('img'); el.src=it.src;
        el.onclick=(e)=>{ e.stopPropagation(); };   // don't let a tap on the image reach the backdrop-close
        _lbZoom(bg, el);   // continuous pinch/wheel zoom + pan (magnifies even low-res posts, unlike the old fit↔natural toggle)
      }
      // Keep the media a DIRECT child of .lightbox — the zoom/centring CSS is written against that shape.
      if(cur) bg.replaceChild(el, cur); else bg.insertBefore(el, bg.firstChild);
      cur=el; bg.classList.remove('lb-zoom');   // a fresh slide starts fitted, not stuck at the last one's zoom
      copyB.style.display=saveB.style.display=isImg?'':'none';
      if(count) count.textContent=(idx+1)+' / '+items.length;
      if(prev) prev.disabled = idx<=0;
      if(next) next.disabled = idx>=items.length-1;
    };
    const go=(d)=>{ const n=Math.max(0, Math.min(items.length-1, idx+d)); if(n===idx) return; idx=n; render(); };

    let prev=null, next=null, count=null;
    if(items.length>1){
      prev=document.createElement('button'); prev.className='lb-nav lb-prev'; prev.type='button'; prev.textContent='‹'; prev.setAttribute('aria-label','Previous');
      next=document.createElement('button'); next.className='lb-nav lb-next'; next.type='button'; next.textContent='›'; next.setAttribute('aria-label','Next');
      prev.onclick=(e)=>{ e.stopPropagation(); go(-1); };
      next.onclick=(e)=>{ e.stopPropagation(); go(1); };
      count=document.createElement('div'); count.className='lb-count';
      bg.appendChild(prev); bg.appendChild(next); bg.appendChild(count);
      // Swipe between slides on touch — but only when not zoomed, or it would fight panning.
      let sx=0, sy=0, tracking=false;
      bg.addEventListener('touchstart', e=>{ if(bg.classList.contains('lb-zoom')||e.touches.length!==1){ tracking=false; return; }
        tracking=true; sx=e.touches[0].clientX; sy=e.touches[0].clientY; }, {passive:true});
      bg.addEventListener('touchend', e=>{ if(!tracking) return; tracking=false;
        const t=e.changedTouches[0]; const dx=t.clientX-sx, dy=t.clientY-sy;
        if(Math.abs(dx)>50 && Math.abs(dx)>Math.abs(dy)*1.5) go(dx<0?1:-1); }, {passive:true});
    }
    render();
    bg.onclick=(e)=>{ if(e.target===bg) close(); };   // tap the backdrop to close too
    /* A desktop app surface can itself establish a stacking/visibility context. Mounting this as a
     * child of body then records a successful image click while drawing the viewer behind the OS
     * window. A direct documentElement child is the top-level overlay layer on classic, mobile and
     * PosterChanOS alike. */
    document.documentElement.appendChild(bg); }
  // Continuous zoom + pan for a lightbox <img>. scale=1 is the fitted view (CSS max-width/height); we
  // transform ABOVE that, so even a small, low-res post magnifies (the old toggle only went to natural
  // size — useless when natural < fitted). Wheel/pinch zoom toward the cursor, drag/one-finger to pan,
  // double-tap|dblclick to toggle. `lb-zoom` on the backdrop (scale>1) hides the pager + disables swipe.
  function _lbZoom(bg, el){
    let scale=1, tx=0, ty=0;
    const MIN=1, MAX=8;
    const vw=()=>window.innerWidth, vh=()=>window.innerHeight;
    const apply=(anim)=>{ el.style.transition = anim ? 'transform .18s ease' : 'none';
      el.style.transform=`translate(${tx}px,${ty}px) scale(${scale})`; };
    const clamp=()=>{ // keep the (scaled) image from being dragged fully off-screen
      const mx=Math.max(0,(el.clientWidth*scale - vw())/2 + 24);
      const my=Math.max(0,(el.clientHeight*scale - vh())/2 + 24);
      tx=Math.max(-mx,Math.min(mx,tx)); ty=Math.max(-my,Math.min(my,ty)); };
    const setScale=(ns,cx,cy,anim)=>{
      ns=Math.max(MIN,Math.min(MAX,ns));
      if(cx==null){ cx=vw()/2; cy=vh()/2; }
      const Cx=vw()/2+tx, Cy=vh()/2+ty, k=ns/scale;   // keep the point under the cursor fixed: C' = S - k*(S-C)
      tx=(cx - k*(cx-Cx)) - vw()/2; ty=(cy - k*(cy-Cy)) - vh()/2;
      scale=ns;
      if(scale<=1.001){ scale=1; tx=0; ty=0; }
      clamp(); apply(anim);
      const zoomed=scale>1.001;
      bg.classList.toggle('lb-zoom', zoomed);
      el.style.cursor = zoomed ? 'grab' : 'zoom-in'; };
    el.addEventListener('wheel',(e)=>{ e.preventDefault(); setScale(scale*(e.deltaY<0?1.18:1/1.18), e.clientX, e.clientY, false); }, {passive:false});
    el.addEventListener('dblclick',(e)=>{ e.preventDefault(); e.stopPropagation(); setScale(scale>1.001?1:3, e.clientX, e.clientY, true); });
    // mouse drag to pan (only meaningful when zoomed)
    let drag=false, lx=0, ly=0;
    el.addEventListener('pointerdown',(e)=>{ if(e.pointerType==='touch'||scale<=1.001) return; drag=true; lx=e.clientX; ly=e.clientY; try{el.setPointerCapture(e.pointerId);}catch(_){} el.style.cursor='grabbing'; });
    el.addEventListener('pointermove',(e)=>{ if(!drag) return; tx+=e.clientX-lx; ty+=e.clientY-ly; lx=e.clientX; ly=e.clientY; clamp(); apply(false); });
    const endDrag=(e)=>{ if(!drag) return; drag=false; try{el.releasePointerCapture(e.pointerId);}catch(_){} el.style.cursor='grab'; };
    el.addEventListener('pointerup',endDrag); el.addEventListener('pointercancel',endDrag);
    // touch: two-finger pinch, one-finger pan (when zoomed), double-tap toggle
    const dist=(a,b)=>Math.hypot(a.clientX-b.clientX, a.clientY-b.clientY);
    let pinch=null, pan=null, lastTap=0;
    el.addEventListener('touchstart',(e)=>{
      if(e.touches.length===2){ e.preventDefault(); const [a,b]=e.touches; pinch={d:dist(a,b), s:scale, mx:(a.clientX+b.clientX)/2, my:(a.clientY+b.clientY)/2}; pan=null; }
      else if(e.touches.length===1 && scale>1.001){ pan={x:e.touches[0].clientX, y:e.touches[0].clientY}; }
    }, {passive:false});
    el.addEventListener('touchmove',(e)=>{
      if(pinch && e.touches.length===2){ e.preventDefault(); const [a,b]=e.touches; setScale(pinch.s*(dist(a,b)/pinch.d), pinch.mx, pinch.my, false); }
      else if(pan && e.touches.length===1 && scale>1.001){ e.preventDefault(); const t=e.touches[0]; tx+=t.clientX-pan.x; ty+=t.clientY-pan.y; pan={x:t.clientX, y:t.clientY}; clamp(); apply(false); }
    }, {passive:false});
    el.addEventListener('touchend',(e)=>{
      if(e.touches.length===0){ pinch=null; pan=null; }
      if(e.changedTouches.length===1 && e.touches.length===0){ const now=Date.now();   // double-tap → zoom toward the tapped point
        if(now-lastTap<300){ e.preventDefault(); const t=e.changedTouches[0]; setScale(scale>1.001?1:3, t.clientX, t.clientY, true); lastTap=0; } else lastTap=now; }
    }, {passive:false});
  }
  async function _nativeShareMedia(src){
    const P=(window.Capacitor&&Capacitor.Plugins)||{}; const Filesystem=P.Filesystem, Share=P.Share;
    if(!Filesystem||!Share) return false;
    // A cross-origin fetch() of a TIMELINE image (arbitrary external / blossom host with no CORS) is blocked
    // by the WebView — the <img> renders but fetch() can't read the bytes ("couldn't share image"). Pull
    // those through our OWN public, CORS-enabled, SSRF-guarded proxy (poster.place fetches the bytes
    // server-side). Our own AUTHED /api content (e.g. AI-chat /api/files) IS same-origin + CORS-OK, so fetch
    // it directly WITH the Bearer (the proxy couldn't auth as the user for it anyway).
    const origin = _serverOrigin();
    let fetchUrl, opts;
    if(src.startsWith(origin + '/api/')){
      fetchUrl = src; opts = {headers: (S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}), credentials:'include'};
    } else {
      fetchUrl = origin + '/client/proxy-image?url=' + encodeURIComponent(src); opts = {};
    }
    const r=await fetch(fetchUrl, opts); if(!r.ok) throw new Error('HTTP '+r.status);
    const b64=await _blobToB64(await r.blob());
    let name=((src.split('/').pop()||'image').split('?')[0])||'image'; if(!/\.[a-z0-9]{2,4}$/i.test(name)) name+='.png';
    const w=await Filesystem.writeFile({ path:name, data:b64, directory:'CACHE' });
    // The sheet OPENING is the success — from here the user saves/copies/sends via the OS. Share.share
    // REJECTS when they just dismiss the sheet (a cancel, not a failure), so swallow it: nagging "couldn't
    // save" after the sheet already appeared is exactly the confusing double-signal to avoid. Only a real
    // fetch/writeFile failure ABOVE (before the sheet) should surface an error to the caller.
    try{ await Share.share({ files:[w.uri], dialogTitle:'Save or share image' }); }catch(_){}   // Capacitor 6: images ride in `files`
    return true;
  }
  /* Open an already-DECRYPTED attachment on the phone.
   *
   * In the APK neither route a browser offers exists: the Android WebView has no PDF viewer, so
   * window.open(blob:) shows nothing, and MainActivity sets no DownloadListener, so an <a download>
   * is silently ignored. A Notes attachment therefore did nothing at all when tapped — reported as
   * "couldn't open attachment error" on the latest APK, after the same code had been fixed for
   * Firefox and the desktop.
   *
   * The OS share sheet is how a file gets handed to whatever app can open it, and both plugins it
   * needs are already bundled. Returns true when it handled the file, false to fall through to the
   * web path. */
  async function nativeOpenBlob(url, name){
    if(!_isNativeApp()) return false;
    const P=(window.Capacitor&&Capacitor.Plugins)||{}; const Filesystem=P.Filesystem, Share=P.Share;
    if(!Filesystem||!Share) return false;
    const blob = await (await fetch(url)).blob();
    const b64 = await _blobToB64(blob);
    // A file name the OS will accept, and an extension, because the app that opens it picks by
    // extension far more often than by mime.
    let n = String(name||'').replace(/[\/\\?%*:|"<>\x00-\x1f]/g,'_').trim().slice(0,80) || 'attachment';
    if(!/\.[a-z0-9]{2,5}$/i.test(n)){
      const mt = (blob.type||'').split(';')[0].trim().toLowerCase();
      const ext = (mt.split('/')[1] || '').replace(/[^a-z0-9]/g,'').slice(0,5);
      if(ext) n += '.' + (ext === 'plain' ? 'txt' : ext === 'jpeg' ? 'jpg' : ext);
    }
    const w = await Filesystem.writeFile({ path:n, data:b64, directory:'CACHE' });
    // The sheet appearing IS the success; a dismissal rejects and is not a failure worth reporting.
    try{ await Share.share({ files:[w.uri], dialogTitle:'Open with…' }); }catch(_){ }
    return true;
  }

  /* WHAT TO TELL SOMEBODY WHOSE IMAGE COPY DID NOT HAPPEN.
   *
   * Every cause used to collapse into one catch and one line — "copy failed — long-press the image
   * to copy" — and on the PosterChanOS desktop that sentence was wrong twice over. Copying an image
   * had never been possible there at all (the app:// origin refuses navigator.clipboard.write, and
   * the native bridge was text-only), and long-press is not a gesture a desk with a mouse has. It was
   * reported exactly that way: a failure, and nonsense advice underneath it.
   *
   * So name the cause, and only ever offer a gesture on a surface that has one. Save (⤓) is the
   * button next to Copy on the same toolbar and works on every surface, so it is the advice that is
   * always true. */
  function _lbCopyImgFail(reason, detail){
    let touch=false;
    try{ touch = (navigator.maxTouchPoints|0) > 0 || ('ontouchstart' in window); }catch(_){}
    const alt = touch ? 'long-press the image, or use Save (⤓)' : 'use Save (⤓) to keep a copy';
    if(reason==='unavailable') return 'this app can’t put images on the clipboard — ' + alt;
    if(reason==='fetch')       return 'couldn’t load that image' + (detail?' ('+detail+')':'') + ' — ' + alt;
    if(reason==='convert')     return 'couldn’t read that image — ' + alt;
    return 'the clipboard refused the image — ' + alt;
  }
  async function _lbCopyImg(src){
    if(_isNativeApp()){ try{ if(await _nativeShareMedia(src)) return; }catch(e){ toast('couldn’t share image'); return; } }
    /* THE DESKTOP SHELL GOES NATIVE, and there it is the ONLY path — not a preference. The bundle is
     * served from app://, where navigator.clipboard.write is refused, which is the same reason
     * copyValue() exists for text. Electron's own clipboard.writeImage would not have rescued it
     * either: it fills Chromium's cache and never takes the Wayland selection, so the copy would work
     * inside PosterChan and paste nothing into Firefox or Telegram. The main process publishes the
     * selection with wl-copy and answers from ITS exit status.
     *
     * Unlike the web path below, the bytes are fetched first — there is no clipboard permission here
     * to lose by awaiting, and fetching first is what lets a 404/403 be reported as a 404/403.
     * fetchMediaBlob is the app's one media reader (bearer for our own /api, proxy-image fallback for
     * a third-party Blossom host with no CORS), so a timeline image works, not just our own. */
    if(window.pcClip && window.pcClip.writeImage){
      let blob;
      try{ blob = (await fetchMediaBlob(src)).blob; }
      catch(e){ toast(_lbCopyImgFail('fetch', (e && e.message) || '')); return; }
      try{ if((blob.type||'')!=='image/png') blob = await _blobToPng(blob); }
      catch(_){ toast(_lbCopyImgFail('convert')); return; }
      let ok=false;
      try{ ok = !!(await window.pcClip.writeImage(await blob.arrayBuffer())); }catch(_){ ok=false; }
      toast(ok ? 'image copied' : _lbCopyImgFail('refused'));
      return;
    }
    // No native bridge and no web clipboard is a MISSING CAPABILITY, not a failed attempt — say that
    // rather than letting it fall through and be reported as a refusal.
    if(!(navigator.clipboard && navigator.clipboard.write && window.ClipboardItem)){
      toast(_lbCopyImgFail('unavailable')); return; }
    let failStage=null, failDetail='';
    try{
      const hdr = S._aiToken ? {'Authorization':'Bearer '+S._aiToken} : {};
      // Pass a Promise<Blob> to ClipboardItem so navigator.clipboard.write is invoked SYNCHRONOUSLY with the
      // tap — iOS/Safari revoke the clipboard permission if you await first. Send the Bearer token too (the
      // APK WebView is cross-origin to the API host, so cookies alone 403 on /api images).
      // Each stage records WHY it failed on the way past: the rejection surfaces out of
      // navigator.clipboard.write, where a refused permission and a 403 on the image are otherwise
      // the same exception.
      const png = (async()=>{
        let r;
        try{ r=await fetch(src, {headers:hdr, credentials:'include'}); }
        catch(e){ failStage='fetch'; failDetail=(e && e.message) || 'network'; throw e; }
        if(!r.ok){ failStage='fetch'; failDetail='HTTP '+r.status; throw new Error(failDetail); }
        let b=await r.blob();
        if((b.type||'')!=='image/png'){ try{ b=await _blobToPng(b); }catch(e){ failStage='convert'; throw e; } }
        return b;
      })();
      await navigator.clipboard.write([new ClipboardItem({'image/png': png})]);
      toast('image copied');
    }catch(e){ toast(_lbCopyImgFail(failStage || 'refused', failDetail)); }
  }
  // Save the media in the lightbox. One line, because this now shares the app's single save pipeline
  // (saveMedia → fetchMediaBlob → fileNameFor → saveBlobAs): same credential rules, same proxy
  // fallback for third-party hosts, same naming. It used to take the URL's last path segment
  // verbatim, which for a Blossom link is a bare sha256 — that's why saving a video off the timeline
  // produced an extensionless file nothing could open.
  function _lbSaveMedia(src){ return saveMedia(src); }
  function _blobToPng(blob){ return new Promise((res,rej)=>{ const img=new Image(); const u=URL.createObjectURL(blob);
    img.onload=()=>{ try{ const c=document.createElement('canvas'); c.width=img.naturalWidth; c.height=img.naturalHeight; c.getContext('2d').drawImage(img,0,0); c.toBlob(b=>{ URL.revokeObjectURL(u); b?res(b):rej(new Error('toBlob')); }, 'image/png'); }catch(e){ URL.revokeObjectURL(u); rej(e); } };
    img.onerror=()=>{ URL.revokeObjectURL(u); rej(new Error('img load')); }; img.src=u; }); }

  return {
    nativeOpenBlob, openLightbox,
  };
};

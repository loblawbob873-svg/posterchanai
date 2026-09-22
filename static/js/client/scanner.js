/* The camera QR scanner — BarcodeDetector verified against jsQR, the two-pass decode budget, the
 * native (APK) scanner, the paste fallback, and what a scanned nostrconnect:// link does. Split
 * out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_scannerDeps`) and
 * builds this factory the first time a scan is opened. The code below is BYTE-IDENTICAL to what it
 * replaced in app.js apart from its reads of app.js's live `let` bindings, which the parser
 * rewrote to `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets.
 */
window.PCScannerFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.ME
  const {
    Nip46Signer, _capPlugin, _loadScript, _signerBatteryCheck, closeModal, enc, modal, toast,
  } = dep;
  // Build a QR detector: native BarcodeDetector (Chrome) if present, else lazy-load jsQR (Firefox /
  // iOS Safari, which have no BarcodeDetector). Returns an async fn(video)->decoded string|null, or
  // null if neither is available.
  /* THE DETECTOR, AND WHY BarcodeDetector IS NOT TRUSTED ON ITS OWN WORD.
   *
   * `'BarcodeDetector' in window` used to be treated as proof it works, and once that branch was
   * taken there was no way back to jsQR. On ANDROID'S WEBVIEW the class exists and constructs
   * happily, but the implementation behind it is a Google Play Services module that is frequently
   * not installed — and in that state `detect()` resolves to an EMPTY ARRAY, for ever, with no
   * error and no rejected promise. The camera opens, every frame comes back with nothing in it, and
   * the scanner sits there saying "still looking".
   *
   * That is exactly the shape of the report: the phone could NEVER scan, while Amber — which uses
   * native ML Kit rather than the web API — read the same code off the same screen every time, and
   * desktop Firefox (which has no BarcodeDetector at all, so it always took the jsQR path) worked
   * too. One platform, one API, silent by design.
   *
   * So: ASK, then VERIFY. `getSupportedFormats()` is the documented capability probe and catches
   * most of it. It is not enough on its own — a build can report `qr_code` and still no-op until the
   * module downloads — so a detector that has returned nothing for a couple of seconds is abandoned
   * and jsQR takes over permanently. jsQR is bundled, needs nothing from Play Services, and is
   * measured to read our code down to one pixel per module.
   */
  async function _qrDetector(){
    const jsqr = async () => {
      try{ await _loadScript('/static/vendor/qr/jsqr.js?v='+(window.__VER||'')); }catch(_){}
      if(!window.jsQR) return null;
      const cv=document.createElement('canvas'); const cx=cv.getContext('2d',{ willReadFrequently:true });
      /* TWO PASSES, ALTERNATING, AND A FIXED DECODE BUDGET — because "just ask for a bigger camera"
       * makes dense codes scan WORSE, which is the opposite of what everyone assumes.
       *
       * MEASURED with scripts/check_qr_scan.py's `third-party` case, which is primal.net's own shape
       * (name + url + icon + the full perms list = QR version 19, 93x93 modules). At 35% frame fill:
       * 1280x720 gives 2px/module and fails, 1920x1080 gives 3px/module and READS — and 2560x1440
       * gives 4px/module and FAILS AGAIN. More pixels per module and a worse result, because jsQR is
       * pure JS and cost scales with the whole frame: at 1440p one attempt chews 3.7M pixels, so the
       * scanner gets through far fewer frames and never catches a sharp one.
       *
       * So the frame is never handed over whole at whatever size the sensor happens to be. Every
       * attempt decodes at most ~900k pixels, and the two passes spend that budget differently:
       *
       *   full   — the entire frame scaled down to the budget. Finds a code anywhere in view,
       *            including one held off to the side, at reduced density.
       *   centre — the middle 60%, at the SENSOR'S OWN resolution. Someone scanning a code points
       *            at it, so this is where a dense one actually is, and cropping keeps every pixel
       *            the camera captured of it while staying inside the budget.
       *
       * Alternating rather than choosing means neither case has to be guessed at, and the cost per
       * attempt is the same as the old full-frame path at 720p. */
      const BUDGET = 1280 * 720;
      let pass = 0;
      return (v)=>{ const w=v.videoWidth, h=v.videoHeight; if(!w||!h) return null;
        let sx=0, sy=0, sw=w, sh=h;
        if((pass++ & 1)){
          const side = Math.round(Math.min(w, h) * 0.6);
          sx = Math.round((w - side) / 2); sy = Math.round((h - side) / 2); sw = sh = side;
        }
        let dw = sw, dh = sh;
        const over = (sw * sh) / BUDGET;
        if(over > 1){ const k = Math.sqrt(over); dw = Math.max(1, Math.round(sw / k)); dh = Math.max(1, Math.round(sh / k)); }
        cv.width=dw; cv.height=dh;
        cx.drawImage(v, sx, sy, sw, sh, 0, 0, dw, dh);
        const r=window.jsQR(cx.getImageData(0,0,dw,dh).data, dw, dh); return (r && r.data) || null; };
    };
    let bd=null;
    if('BarcodeDetector' in window){
      try{
        const formats = await BarcodeDetector.getSupportedFormats();
        if(formats && formats.indexOf('qr_code') >= 0) bd = new BarcodeDetector({ formats:['qr_code'] });
      }catch(_){ bd = null; }
    }
    if(!bd) return await jsqr();

    /* Both, with the native one on probation. `swap` is armed lazily so a scan that works costs
     * nothing extra, and once jsQR is loaded it is used for every later frame — a detector that has
     * produced nothing for this long is not about to start. */
    let fallback=null, firstAt=0;
    return async (v)=>{
      if(fallback) return fallback(v);
      try{
        const c=await bd.detect(v);
        const val=(c && c[0] && c[0].rawValue) || null;
        if(val) return val;
      }catch(_){ fallback = await jsqr(); return fallback ? fallback(v) : null; }
      if(!firstAt) firstAt=Date.now();
      else if(Date.now()-firstAt > 2500){
        fallback = await jsqr();
        try{ console.warn('[qr] BarcodeDetector read nothing in 2.5s — using jsQR'); }catch(_){}
        if(fallback) return fallback(v);
      }
      return null;
    };
  }
  async function openQrScanner(){
    if(S.ME.mode!=='local'){ toast('Log in with your key (nsec) on this device first — extension/remote-signer logins can’t sign for another device'); return; }
    /* THE NATIVE SCANNER FIRST, WHERE THERE IS ONE.
     *
     * The modal below is jsQR decoding a canvas frame that has been scaled to a fixed pixel budget,
     * and it is MEASURED to fail on a primal.net-shaped code (v19, 93x93 modules) below about 40% of
     * the frame — while the same phone's camera app reads it every time. That gap is structural, not
     * a tuning problem: a native decoder gets the sensor, we get a downscaled bitmap and a JS budget.
     *
     * So on the APK this hands off to zxing (scan/QrScanPlugin) and the modal never opens. An empty
     * result means the user pressed back, which is not a failure — fall through to our own scanner
     * rather than showing an error, so backing out never strands anybody. Anything else (a browser,
     * the desktop build, an older APK) sees exactly what it saw before. */
    try{
      const NS = _capPlugin('QrScan', 'scan');
      if(NS){
        let txt = '';
        try{ txt = ((await NS.scan()) || {}).text || ''; }catch(_){ txt = ''; }
        txt = String(txt).trim();
        if(txt){
          if(/^nostrconnect:/i.test(txt)) return onQrScanned(txt);
          toast('That code is not a pairing link — on the other device open its remote-signer screen');
          return;
        }
      }
    }catch(_){}
    if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){ return qrManualPrompt('Camera needs an HTTPS connection. Paste the link instead:'); }
    const detect=await _qrDetector();
    if(!detect){ return qrManualPrompt('QR scanning isn’t supported in this browser. Paste the link instead:'); }
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-camera"></use></svg>Scan QR to log in another device</h3>
      <video id="qr-video" class="qr-video" playsinline muted></video>
      <div class="muted small" id="qr-hint">Point at the QR shown on the other device…</div>
      <div class="set-actions"><button class="btn btn-ghost small" id="qr-paste">paste link instead</button>
        <button class="btn btn-ghost small" id="qr-cancel">cancel</button></div>`, async root=>{
      const v=root.querySelector('#qr-video'), hint=root.querySelector('#qr-hint');
      root.querySelector('#qr-cancel').onclick=()=>closeModal();
      root.querySelector('#qr-paste').onclick=()=>{ closeModal(); qrManualPrompt(); };
      let stream=null, stopped=false;
      const cleanup=()=>{ stopped=true; try{ stream && stream.getTracks().forEach(t=>t.stop()); }catch(_){} };
      /* ASK FOR A BIG FRAME. Without a resolution hint a browser hands out whatever it likes, which
       * is routinely 640x480 — and the thing that decides whether a QR decodes is PIXELS PER MODULE,
       * not megapixels. Measured: a Primal-shaped code is version 19 (93x93 modules), nearly double
       * ours, so at 640x480 and arm's length it lands at 2px/module and is unreadable no matter how
       * still you hold it. The same code at 1920x1080 is ~6px/module and reads easily. This is the
       * only lever we have over somebody else's payload — we cannot make their QR smaller.
       *
       * `ideal`, not `exact`: a device that cannot do this should hand back its best rather than
       * refuse the camera outright, which would turn "hard to scan" into "no scanner at all".
       *
       * ASKING FOR MORE THAN 1080p ONLY BECAME SAFE ONCE THE DECODER STOPPED READING WHOLE FRAMES,
       * and the order matters. Measured on the same primal-shaped code at 35% frame fill: 720p fails
       * (2px/module), 1080p reads (3px/module), and 1440p FAILED TOO — with 4px/module — because
       * jsQR is pure JS and its cost scales with the frame, so at 3.7M pixels an attempt the scanner
       * got through too few frames to catch a sharp one. Raising this line alone would have made
       * dense codes harder to scan while every number said it should have helped. With the two-pass
       * budgeted decoder in `_qrDetector` the same 1440p case reads, so the extra pixels now land
       * where they were always supposed to. */
      let camInfo = '';   // what the camera GRANTED (see the tick loop) — `ideal` above is only a request
      try{ stream=await navigator.mediaDevices.getUserMedia({ video:{ facingMode:'environment',
             width:{ ideal:2560 }, height:{ ideal:1440 } } }); v.srcObject=stream; await v.play();
           try{ const st=stream.getVideoTracks()[0].getSettings()||{};
                if(st.width && st.height) camInfo = st.width+'x'+st.height; }catch(_){} }
      catch(e){ hint.textContent='Camera unavailable ('+((e&&e.message)||e)+'). Use “paste link instead”.'; return; }
      /* SAY SOMETHING WHEN IT ISN'T WORKING. This loop swallowed every detector error and every
       * non-match and then looked identical to a camera that was simply not pointed at anything —
       * reported, accurately, as "trying to scan but nothing happens". It cannot know whether the
       * QR is out of focus, too dense for this webcam, or not in frame at all; what it CAN do is
       * stop pretending it is fine, and name the way out that always works.
       *
       * A signer QR is version ~21 (101x101 modules) because of the `perms` list, which is a lot to
       * ask of a laptop camera aimed at a screen — so "paste the link instead" is not a fallback
       * here, it is often the faster route, and it is one click away in this modal. */
      /* "IT NEVER SCANS" HAS THREE CAUSES AND THIS LOOP USED TO REPORT THEM IDENTICALLY.
       *
       * The decoder can (a) read nothing at all — too few pixels per module, out of focus, not in
       * frame; (b) read a code that is not a pairing link — someone else's npub QR, a bunker://
       * meant for the opposite flow, a plain URL; or (c) read the right link, in which case we are
       * already gone. Only (a) is a camera problem, and (a) and (b) looked the same from the sofa:
       * the hint sat on "Still looking…" for ever either way. So a build that improved the camera
       * could not be told apart from one that changed nothing, which is exactly how the last one
       * was reported back ("still doesn't scan").
       *
       * The fix is to say which. `seen` is the scheme of the last thing decoded — the SCHEME only,
       * never the payload, because a pairing link carries a bearer secret and this text is on
       * screen. And the resolution the camera actually GRANTED is printed with it: `ideal` is a
       * request, not a promise, and a phone that quietly handed back 640x480 is the one case where
       * the honest advice really is "fill more of the frame". */
      let ticks = 0, told = false, seen = '';
      const tick=async()=>{
        if(stopped || !document.body.contains(v)){ cleanup(); return; }   // modal closed → stop camera
        try{
          const val=await detect(v);
          if(val && /^nostrconnect:/i.test(val)){ cleanup(); closeModal(); return onQrScanned(val); }
          if(val){ const m=String(val).match(/^([a-z0-9+.-]{1,20}):/i); seen = m ? m[1].toLowerCase()+':' : 'plain text'; }
        }catch(_){}
        if(++ticks > 24 && (!told || seen)){             // ~7s of looking, and nothing paired
          told = true;
          hint.textContent = seen
            ? ('Read a code, but it is not a pairing link (' + seen + '). On the other device open '
               + 'its remote-signer / “connect” screen — that QR starts with nostrconnect:.')
            /* The second sentence is the one that actually works on a dense third-party code, so it
             * is not buried as a fallback: this decoder is JavaScript on a downscaled frame, and the
             * phone's own camera app is native and reads what we cannot. */
            : ('Still looking… this code is a dense one' + (camInfo ? ' and this camera is ' + camInfo : '')
               + '. Fill about half the frame with it and hold steady — or scan it with your phone’s '
               + 'camera app and share the link to PosterChan, which always works.');
        }
        setTimeout(tick, 300);
      };
      tick();
    });
  }
  function qrManualPrompt(msg){
    modal(`<h3>Log in another device</h3>
      <p class="muted small">${enc(msg||'On the other device, open Sign in → “Open in Amber / scan QR”, then copy its connection link and paste it here.')}</p>
      <textarea class="input" id="qr-paste-uri" rows="3" placeholder="nostrconnect://…"></textarea>
      <button class="btn btn-neon full" id="qr-paste-go">Log in that device</button>`, root=>{
      root.querySelector('#qr-paste-go').onclick=()=>{ const u=root.querySelector('#qr-paste-uri').value.trim(); if(!u){ return; } closeModal(); onQrScanned(u); };
    });
  }
  async function onQrScanned(uri){
    try{
      /* EVERY PAIRING IS ITS OWN DEVICE, AND THE APP DOES NOT GET A VOTE.
       *
       * There used to be a prompt here: a new pairing whose `name` matched an existing one asked
       * whether to replace the stalest. It was written for THIS app's own clients, which all
       * announce themselves as "PosterChan" — but so does every other app announce one fixed name,
       * and primal.net announces "PrimalWeb" for every device somebody signs in. So the name is not
       * an identity, it is a PRODUCT, and matching on it made the app treat four different machines
       * as four attempts at the same one. Reported, in order: "i signed in 4 devices but only see
       * 2?", "i choose keep them all and nothing goes on", "i could only sign in 1 device", "it is
       * still thinking all posterchan devices are the same". Every one of those is this block.
       *
       * Inverting the buttons (keep = default) only made the destruction less likely, which is the
       * wrong fix for a question that should never have been asked: nothing here KNOWS whether two
       * pairings are one laptop paired twice or two laptops, and the person answering does not know
       * either, because both rows say the same word. Amber does not ask; it lists the connection and
       * lets you revoke the one you mean.
       *
       * So every pairing is kept. Telling them apart is the LIST's job, and it now says when each
       * was added and marks the ones never used — which is what somebody actually needs in order to
       * revoke the right row. */
      const nm=await Nip46Signer.start(uri);
      toast('✅ “'+nm+'” is now logged in — your key stayed on this device');
      /* The startup check runs before a FIRST pairing has made the native service `wanted`, so it
       * correctly does nothing then.  Run it again after the hand-over: this is the first instant
       * Android can truthfully say that background signing is enabled, and therefore the first
       * instant an unexempted phone can be warned that Doze will defer its relay traffic until the
       * screen wakes.  Without this call a fresh pairing never saw the prompt until a later app
       * restart, which is exactly the visible failure: every desktop post waits for the phone. */
      try{ await _signerBatteryCheck(); }catch(_){}
    }catch(e){
      // NOT stop(). That killed every OTHER app this device was signing for because one QR failed —
      // start() already removes the half-made session on its own way out.
      toast('QR sign-in failed: '+((e&&e.message)||e));
    }
  }

  return {
    onQrScanned, openQrScanner,
  };
};

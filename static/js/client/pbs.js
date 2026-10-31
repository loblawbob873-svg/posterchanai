/* PBS -- PosterChan Broadcast Studio. Our OBS, built into the client, and it goes live to Nostr in one button.
 *
 * "start pbs, our obs that ties in to the connected posterchan instance for streaming" + "the goal of PBS is to
 * simplify nostr streaming" (2026-10-30). docs/PBS.md is the long form; the shape:
 *
 *   SOURCES   one scene of layers, bottom to top: screen/window capture (getDisplayMedia -- the browser's own picker,
 *             the desktop app's picker in desktop/main.js, the xdg portal on PosterChanOS), camera, image overlays,
 *             text overlays. Drag and resize on the preview, opacity, order, hide.
 *   COMPOSITE a <canvas> at the output size (720p30 default), drawn on a WORKER clock so a backgrounded window keeps
 *             broadcasting (a page timer drops to 1 Hz there), captureStream() for the video track.
 *   AUDIO     WebAudio: microphone (device picked) + the screen's own sound where the platform hands it over, each
 *             through a gain (volume, mute) into one MediaStreamDestination; meters read AFTER the fader, so a muted
 *             channel reads silence -- what the viewers hear, not what the microphone hears.
 *   GO LIVE   nothing to copy: streams.js asks THIS instance for the account's token (/api/streams/ingest), publishes
 *             over the instance's WHIP with H.264 first (the codec every GPU encodes in hardware) and the upload
 *             capped by setParameters at the node's clamp ceiling, then signs the NIP-53 30311 (`live`, streaming =
 *             the instance's HLS). The stream's chat (1311 + reactions) sits beside the preview. Stop = WHIP DELETE +
 *             the 30311 `ended`. Every failure says why in the status bar, and on every failure path nothing has
 *             been published.
 *   SAVED     the scene/mixer/stream words are ONE kind-30078 document `d=pcai:pbs`, NIP-44 to the account's own key
 *             (PCPbsDoc below). Image overlays are referenced by Blossom URL, or kept in this device's IndexedDB
 *             (`idb:` ids) when there is nowhere to upload -- never inlined into the document. A read nobody answered
 *             is "could not read": the device copy is used and NOTHING is written over the account's setup.
 *
 * HOME-RELAY WRITE GATE: driven by tests/client/home_relay_write_gates_runtime.mjs (PCPbsDoc.store).
 */
(function(){
  'use strict';
  const D = 'pcai:pbs';
  const MAX_SOURCES = 24;
  const RES = { '720': [1280, 720], '1080': [1920, 1080] };
  const KBPS = [900, 1500, 3000];
  const FONTS = { sans: "Inter, system-ui, sans-serif", mono: "'JetBrains Mono', ui-monospace, monospace",
                  display: "Orbitron, Inter, sans-serif", serif: "Georgia, 'Times New Roman', serif" };

  /* ---------- the document: shape + the replaceable-doc rule (DOM-free, driven by node tests) ---------- */
  const num = (v, lo, hi, dflt) => { v = Number(v); return Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : dflt; };
  const str = (v, max) => String(v == null ? '' : v).slice(0, max);
  const color = (v, dflt) => (/^#[0-9a-fA-F]{6}$/.test(String(v || '')) ? String(v).toLowerCase() : dflt);
  /** A URL an overlay may name: a Blossom/https URL or this device's IndexedDB id. Never a data: URI. */
  const imgRef = v => { v = String(v || ''); return (/^https?:\/\//i.test(v) || /^idb:[a-z0-9-]{6,64}$/.test(v)) ? v.slice(0, 2048) : ''; };
  function cleanSource(s){
    if(!s || typeof s !== 'object') return null;
    const type = ['screen', 'camera', 'image', 'text'].includes(s.type) ? s.type : null;
    if(!type) return null;
    const o = { id: str(s.id, 40).replace(/[^a-z0-9-]/gi, '') || Math.random().toString(36).slice(2, 10), type,
                name: str(s.name, 80), visible: s.visible !== false,
                x: num(s.x, -1, 2, 0), y: num(s.y, -1, 2, 0), w: num(s.w, 0.01, 3, 1), h: num(s.h, 0.01, 3, 1),
                opacity: num(s.opacity, 0, 1, 1) };
    if(type === 'text'){
      Object.assign(o, { text: str(s.text, 500), font: FONTS[s.font] ? s.font : 'display', size: num(s.size, 8, 240, 64),
                         color: color(s.color, '#3ce8ff'), outline: s.outline !== false, ocolor: color(s.ocolor, '#000000'),
                         shadow: s.shadow !== false, bold: s.bold !== false });
    }
    if(type === 'image'){ o.url = imgRef(s.url); if(!o.url) return null; }
    if(type === 'camera'){ o.device = str(s.device, 200); }
    return o;
  }
  function normalize(doc){
    doc = (doc && typeof doc === 'object') ? doc : {};
    const out = doc.out || {}, au = doc.audio || {}, me = doc.meta || {};
    const res = RES[String(out.res)] ? String(out.res) : '720';
    const kbps = KBPS.includes(Number(out.kbps)) ? Number(out.kbps) : 1500;
    const ch = (c, d) => ({ vol: num(c && c.vol, 0, 2, 1), muted: !!(c && c.muted), ...(d ? { device: str(c && c.device, 200) } : {}) });
    return { v: 1, t: num(doc.t, 0, 1e13, 0),
      // cap720: ONE switch that holds the stream at 720p and the standard 1500 kbps whatever res/kbps say (on by default;
      // res/kbps keep the person's own choice, so turning the cap off brings it back).
      out: { res, fps: Number(out.fps) === 60 ? 60 : 30, kbps, cap720: out.cap720 !== false },
      sources: (Array.isArray(doc.sources) ? doc.sources : []).map(cleanSource).filter(Boolean).slice(0, MAX_SOURCES),
      audio: { mic: ch(au.mic, true), desk: ch(au.desk, false) },
      meta: { title: str(me.title, 120), summary: str(me.summary, 1000), tags: str(me.tags, 400),
              thumb: me.thumb === 'drive' ? 'drive' : 'snapshot', thumbUrl: imgRef(me.thumbUrl).startsWith('http') ? imgRef(me.thumbUrl) : '',
              announce: me.announce !== false } };
  }
  /* deps = { owner, query(filters)->events, homeAnswered(events)->bool, publish(kind,content,tags)->{ok},
   *          enc(pk,text), dec(pk,text), local:{get(), set(doc)} }
   * load() -> { status:'ok'|'empty'|'unreadable', doc|null }. 'unreadable' = nobody answered, or the document
   * would not decrypt: the device copy is offered and the writer stays DISARMED, so the next save cannot replace the
   * account's setup with this device's guess. save(doc) -> { synced } ; an unsynced save is kept on the device. */
  function store(deps){
    let read = false, loading = null;
    const giveUp = ms => new Promise(r => setTimeout(() => r(undefined), ms));
    const localDoc = () => { try{ const d = deps.local.get(); return d ? normalize(d) : null; }catch(_){ return null; } };
    async function load(){
      if(loading) return loading;
      loading = (async () => {
        let evs;
        try{ evs = await Promise.race([deps.query([{ authors: [deps.owner], kinds: [30078], '#d': [D], limit: 1 }]), giveUp(9000)]); }
        catch(_){ return { status: 'unreadable', doc: localDoc() }; }
        if(!Array.isArray(evs)) return { status: 'unreadable', doc: localDoc() };
        let home = false;
        try{ home = !!(deps.homeAnswered && deps.homeAnswered(evs)); }catch(_){ home = false; }
        if(!(evs.complete !== false || home)) return { status: 'unreadable', doc: localDoc() };
        const ev = evs.slice().sort((a, b) => b.created_at - a.created_at)[0];
        const mine = localDoc();
        if(!ev || !ev.content){ read = true; return { status: 'empty', doc: mine }; }
        let doc;
        try{ doc = normalize(JSON.parse(await deps.dec(deps.owner, ev.content))); }
        catch(_){ return { status: 'unreadable', doc: mine }; }
        read = true;
        // A change made on this device while nothing could be read is newer than the account copy: keep it.
        if(mine && mine.t > doc.t){ return { status: 'ok', doc: mine, pending: true }; }
        try{ deps.local.set(doc); }catch(_){}
        return { status: 'ok', doc };
      })().finally(() => { loading = null; });
      return loading;
    }
    async function save(doc){
      doc = normalize(Object.assign({}, doc, { t: Date.now() }));
      try{ deps.local.set(doc); }catch(_){}
      if(!read) await load();
      if(!read) return { synced: false, doc };
      try{
        const ct = await deps.enc(deps.owner, JSON.stringify(doc));
        const r = await deps.publish(30078, ct, [['d', D]]);
        return { synced: !!(r && r.ok), doc };
      }catch(_){ return { synced: false, doc }; }
    }
    return { load, save, isRead: () => read };
  }
  /** Which encoder getStats says is running: 'hardware' | 'software' | 'unknown', and its name. */
  function encoderKind(rep){
    const impl = String((rep && rep.encoderImplementation) || '');
    if(rep && rep.powerEfficientEncoder === true) return { kind: 'hardware', impl };
    if(/vaapi|nvenc|nvidia|mediacodec|videotoolbox|v4l2|d3d|mediafoundation|qsv|amf|externalencoder|hardware/i.test(impl)) return { kind: 'hardware', impl };
    if(/openh264|libvpx|libaom|ffmpeg|software|x264/i.test(impl) || (rep && rep.powerEfficientEncoder === false)) return { kind: 'software', impl };
    return { kind: 'unknown', impl };
  }
  /** dBFS of a sample peak/RMS, floored at -60 (the meter's scale). */
  const dbOf = v => Math.max(-60, Math.min(0, 20 * Math.log10(Math.max(1e-9, v))));
  window.PCPbsDoc = { D, store, normalize, encoderKind, dbOf, KBPS, RES };

  /* ---------- the studio ---------- */
  window.PCPbsFactory = function(dep){
    const S = dep.state;
    const { enc, toast } = dep;
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
    const ic = n => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"></use></svg>`;
    const uid = () => Math.random().toString(36).slice(2, 10);

    /* The engine lives in the module, not the view: #feed is shared by every screen, so leaving PBS must not stop a
     * broadcast -- the canvas, the captures and the mixer keep running; the view only mounts the canvas. */
    const E = {
      owner: '', doc: normalize({}), status: 'idle', loadNote: '',
      canvas: null, ctx: null, timer: null, worker: null,
      media: new Map(),          // source id -> { stream, video?, img? }
      sel: '', drag: null,
      ac: null, dest: null, ch: new Map(),   // 'mic' | 'desk' -> { stream, src, gain, an, buf, peak, rms }
      live: false, phase: 'offline', why: '', started: 0, pc: null, addr: '', relays: [],
      stats: { kbps: 0, enc: null, last: null }, statTimer: null, clock: null, store: null, saveT: null,
      meterRaf: 0, mics: [], cams: [],
    };

    /* ---- persistence ---- */
    function storeFor(owner){
      if(E.store && E.owner === owner) return E.store;
      const key = 'pc_pbs_' + owner;
      E.owner = owner;
      E.store = store({ owner,
        query: f => Relay.query(f),
        homeAnswered: got => !!(window.Relay && Relay.homeAnswered && Relay.homeAnswered(got)),
        publish: (k, c, t) => dep.publish(k, c, t, { quiet: true }),
        enc: (pk, t) => S.signer.nip44enc(pk, t), dec: (pk, t) => S.signer.nip44dec(pk, t),
        local: { get: () => { try{ return JSON.parse(localStorage.getItem(key) || 'null'); }catch(_){ return null; } },
                 set: d => { try{ localStorage.setItem(key, JSON.stringify(d)); }catch(_){} } } });
      return E.store;
    }
    function snapshot(){
      return Object.assign({}, E.doc, { sources: E.doc.sources.map(s => Object.assign({}, s)) });
    }
    function scheduleSave(){
      E.edits = (E.edits || 0) + 1;
      if(!S.ME || S.GUEST) return;
      clearTimeout(E.saveT);
      E.saveT = setTimeout(async () => {
        const st = storeFor(S.ME.pubkey);
        // Not read yet: it is on this device the moment it is saved; the account copy waits for a relay to answer.
        sayLoad(st.isRead() ? 'Saving…' : 'Saved on this device — it syncs once your relay answers');
        const r = await st.save(snapshot());
        sayLoad(r.synced ? 'Saved to your account' : 'Saved on this device — it syncs once your relay answers');
      }, 900);
    }
    function sayLoad(t){ E.loadNote = t; const el = $('#pbs-saved'); if(el) el.textContent = t; }
    async function loadFor(owner){
      if(E.loadedFor === owner) return;
      E.loadedFor = owner;
      sayLoad('Loading your studio…');
      const before = E.edits || 0;
      const r = await storeFor(owner).load();
      if(E.owner !== owner) return;
      // Somebody started building while the relay was being asked: what they did is newer than anything the read
      // can return, so it stays (and is what the next save writes). Applying the read here would swap every
      // source object out from under the controls they are holding.
      const applied = !!(r.doc && (E.edits || 0) === before);
      if(applied) applyDoc(r.doc);
      sayLoad(r.status === 'unreadable'
        ? 'Couldn’t read your saved studio — using this device’s copy; nothing is written over your account’s setup'
        : r.status === 'empty' ? 'New studio — your setup saves to your account as you go' : 'Studio loaded from your account');
      if(r.status === 'unreadable') E.loadedFor = '';   // try again next time the studio opens
      // The form, the output controls and the stage's shape were drawn from the defaults: redraw them from the
      // loaded setup (nobody has typed into them -- `applied` says no edit happened while reading).
      if(applied && $('#pbs') && S.VIEW === 'pbs') render(); else paint();
    }
    function applyDoc(doc){
      // Live captures belong to this session: keep the ones still running, drop media for vanished layers.
      const keep = new Set(doc.sources.map(s => s.id));
      for(const [id] of E.media) if(!keep.has(id)) dropMedia(id);
      E.doc = normalize(doc);
      E.doc.sources.filter(s => s.type === 'image').forEach(loadImage);
      sizeCanvas();
    }

    /* ---- IndexedDB for overlay images with nowhere to upload ---- */
    function idb(){ return new Promise((res, rej) => { try{ const r = indexedDB.open('pc-pbs', 1);
      r.onupgradeneeded = () => r.result.createObjectStore('img'); r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); }catch(e){ rej(e); } }); }
    async function idbPut(id, blob){ const db = await idb(); return new Promise((res, rej) => { const t = db.transaction('img', 'readwrite'); t.objectStore('img').put(blob, id); t.oncomplete = () => res(); t.onerror = () => rej(t.error); }); }
    async function idbGet(id){ const db = await idb(); return new Promise((res, rej) => { const q = db.transaction('img').objectStore('img').get(id); q.onsuccess = () => res(q.result || null); q.onerror = () => rej(q.error); }); }

    /* ---- canvas + clock ---- */
    /* What actually goes out: the 720p cap wins over the resolution and upload choices while it is on. */
    function effRes(){ return E.doc.out.cap720 ? '720' : E.doc.out.res; }
    function effKbps(){ return E.doc.out.cap720 ? Math.min(E.doc.out.kbps, 1500) : E.doc.out.kbps; }
    function dims(){ return RES[effRes()] || RES['720']; }
    function ensureCanvas(){
      if(E.canvas) return;
      E.canvas = document.createElement('canvas'); E.canvas.id = 'pbs-canvas'; E.canvas.className = 'pbs-canvas';
      E.canvas.setAttribute('aria-label', 'Broadcast preview');
      E.ctx = E.canvas.getContext('2d', { alpha: false });
      sizeCanvas(); startClock();
    }
    function sizeCanvas(){
      if(!E.canvas) return;
      const [w, h] = dims();
      if(E.canvas.width !== w || E.canvas.height !== h){ E.canvas.width = w; E.canvas.height = h; }
    }
    function startClock(){
      const ms = Math.round(1000 / E.doc.out.fps);
      if(E.worker){ E.worker.postMessage(ms); return; }
      try{
        const src = 'let t=0;onmessage=e=>{clearInterval(t);if(e.data>0)t=setInterval(()=>postMessage(0),e.data)}';
        E.worker = new Worker(URL.createObjectURL(new Blob([src], { type: 'text/javascript' })));
        E.worker.onmessage = draw;
        E.worker.onerror = () => { try{ E.worker.terminate(); }catch(_){} E.worker = null; fallbackClock(ms); };
        E.worker.postMessage(ms);
      }catch(_){ E.worker = null; fallbackClock(ms); }
    }
    function fallbackClock(ms){ clearInterval(E.timer); E.timer = setInterval(draw, ms); }
    function setFps(){ if(E.worker) E.worker.postMessage(Math.round(1000 / E.doc.out.fps)); else if(E.timer) fallbackClock(Math.round(1000 / E.doc.out.fps)); }

    function fitContain(sw, sh, bx, by, bw, bh){
      if(!sw || !sh) return [bx, by, bw, bh];
      const r = Math.min(bw / sw, bh / sh), w = sw * r, h = sh * r;
      return [bx + (bw - w) / 2, by + (bh - h) / 2, w, h];
    }
    function textLines(s){ return String(s.text || '').split('\n').slice(0, 12); }
    function textFont(s, H){ return `${s.bold ? 800 : 500} ${Math.round(s.size * H / 720)}px ${FONTS[s.font] || FONTS.display}`; }
    function draw(){
      const c = E.ctx; if(!c) return;
      const W = E.canvas.width, H = E.canvas.height;
      c.globalAlpha = 1; c.shadowBlur = 0; c.shadowColor = 'transparent';
      c.fillStyle = '#000'; c.fillRect(0, 0, W, H);
      for(const s of E.doc.sources){
        if(!s.visible) continue;
        c.globalAlpha = s.opacity;
        const bx = s.x * W, by = s.y * H, bw = s.w * W, bh = s.h * H;
        if(s.type === 'screen' || s.type === 'camera'){
          const m = E.media.get(s.id), v = m && m.video;
          if(v && v.readyState >= 2 && v.videoWidth){ const [x, y, w, h] = fitContain(v.videoWidth, v.videoHeight, bx, by, bw, bh); c.drawImage(v, x, y, w, h); }
        } else if(s.type === 'image'){
          const m = E.media.get(s.id), im = m && m.img;
          if(im && im.complete && im.naturalWidth){ const [x, y, w, h] = fitContain(im.naturalWidth, im.naturalHeight, bx, by, bw, bh); c.drawImage(im, x, y, w, h); }
        } else if(s.type === 'text'){
          c.font = textFont(s, H); c.textBaseline = 'top';
          const px = Math.round(s.size * H / 720), lh = px * 1.18, lines = textLines(s);
          let tw = 0; for(const l of lines) tw = Math.max(tw, c.measureText(l).width);
          // The box follows the text, so selection, hit-testing and the saved layout agree with what is drawn.
          s.w = Math.max(0.01, (tw + px * 0.2) / W); s.h = Math.max(0.01, (lines.length * lh) / H);
          lines.forEach((l, i) => {
            const ty = by + i * lh;
            if(s.shadow){ c.shadowColor = 'rgba(0,0,0,.75)'; c.shadowBlur = px * 0.18; c.shadowOffsetX = px * 0.04; c.shadowOffsetY = px * 0.06; }
            if(s.outline){ c.lineJoin = 'round'; c.lineWidth = Math.max(2, px * 0.12); c.strokeStyle = s.ocolor; c.strokeText(l, bx + px * 0.1, ty); }
            c.shadowBlur = 0; c.shadowColor = 'transparent'; c.shadowOffsetX = 0; c.shadowOffsetY = 0;
            c.fillStyle = s.color; c.fillText(l, bx + px * 0.1, ty);
          });
        }
      }
      c.globalAlpha = 1;
      placeSel();
    }

    /* ---- sources ---- */
    const canScreen = () => !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
    const canCamera = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    function src(id){ return E.doc.sources.find(s => s.id === id) || null; }
    function hostEl(){
      // Captures play in a 1px host that stays IN the document: a detached or off-screen <video> may be paused by the
      // browser, and a paused capture is a frozen frame on the broadcast.
      let h = document.getElementById('pbs-media-host');
      if(!h){ h = document.createElement('div'); h.id = 'pbs-media-host'; h.setAttribute('aria-hidden', 'true');
        h.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;overflow:hidden;opacity:0.01;pointer-events:none;z-index:-1';
        document.body.appendChild(h); }
      return h;
    }
    function attachVideo(id, stream){
      dropMedia(id);
      const v = document.createElement('video'); v.muted = true; v.playsInline = true; v.autoplay = true; v.srcObject = stream;
      hostEl().appendChild(v);
      try{ const p = v.play(); if(p && p.catch) p.catch(() => {}); }catch(_){}
      E.media.set(id, { stream, video: v });
      const vt = stream.getVideoTracks()[0];
      if(vt) vt.addEventListener('ended', () => { if(E.media.get(id) && E.media.get(id).stream === stream){ dropMedia(id); paint(); } });
    }
    function dropMedia(id){
      const m = E.media.get(id); if(!m) return;
      E.media.delete(id);
      try{ m.stream && m.stream.getTracks().forEach(t => t.stop()); }catch(_){}
      try{ if(m.video){ m.video.srcObject = null; m.video.remove(); } }catch(_){}
      if(m.deskOf) dropChannel('desk');
    }
    async function loadImage(s){
      const im = new Image(); im.crossOrigin = 'anonymous';
      let url = s.url;
      if(url.startsWith('idb:')){ try{ const b = await idbGet(url.slice(4)); if(!b) return; url = URL.createObjectURL(b); }catch(_){ return; } }
      im.onload = () => { if(s.fresh){ fitNew(s, im.naturalWidth, im.naturalHeight, 0.4); delete s.fresh; scheduleSave(); } paintLayers(); };
      im.src = url;
      E.media.set(s.id, { img: im });
    }
    function fitNew(s, nw, nh, frac){
      const [W, H] = dims();
      s.w = frac; s.h = Math.min(1, (frac * W) * (nh / nw) / H);
      if(s.x + s.w > 1) s.x = Math.max(0, 1 - s.w); if(s.y + s.h > 1) s.y = Math.max(0, 1 - s.h);
    }
    function addSource(s){
      if(E.doc.sources.length >= MAX_SOURCES){ toast('a scene holds up to ' + MAX_SOURCES + ' sources'); return null; }
      E.doc.sources.push(s); E.sel = s.id; scheduleSave(); return s;
    }
    async function macBlocked(){
      try{ if(window.pcCapture && pcCapture.info){ const i = await pcCapture.info(); return !!(i && i.platform === 'darwin' && (i.screen === 'denied' || i.screen === 'restricted')); } }catch(_){}
      return false;
    }
    function sayScreenBlocked(){
      setWhy('macOS is blocking screen recording for PosterChan. Open System Settings → Privacy & Security → Screen Recording, turn PosterChan on, then restart the app.', 'mac');
    }
    async function captureScreen(s){
      if(!canScreen()){ setWhy('Screen capture is desktop-only — phones and tablets don’t allow it. Camera and overlays work here.'); return false; }
      if(await macBlocked()){ sayScreenBlocked(); return false; }
      let st;
      try{ st = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: { ideal: E.doc.out.fps }, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: true }); }
      catch(e){
        if(e && e.name === 'NotAllowedError'){ if(await macBlocked()) sayScreenBlocked(); else setWhy('Screen capture cancelled.'); }
        else setWhy('Screen capture could not start' + (e && e.message ? ': ' + e.message : '') + (window.pcShell ? ' — capture details are in screen-share.log' : ''));
        return false;
      }
      const vt = st.getVideoTracks()[0];
      if(!vt){ st.getTracks().forEach(t => t.stop()); setWhy('No screen was shared.'); return false; }
      try{ vt.contentHint = 'detail'; }catch(_){}
      const at = st.getAudioTracks()[0];
      attachVideo(s.id, new MediaStream([vt]));
      if(at){ addChannel('desk', new MediaStream([at])); E.media.get(s.id).deskOf = true; }
      else if(E.ch.has('desk')) dropChannel('desk');
      setWhy(at ? '' : 'Shared without its sound — this platform (or the source you picked) gives no desktop audio.', 'info');
      return true;
    }
    async function captureCamera(s){
      if(!canCamera()){ setWhy('This browser has no camera access.'); return false; }
      try{
        const v = { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: E.doc.out.fps } };
        if(s.device) v.deviceId = { exact: s.device };
        const st = await navigator.mediaDevices.getUserMedia({ video: v, audio: false });
        attachVideo(s.id, st); listDevices(); return true;
      }catch(e){ setWhy(e && e.name === 'NotAllowedError' ? 'Camera blocked — allow it for this site (or in your system settings) and try again.' : 'Camera could not start' + (e && e.message ? ': ' + e.message : '')); return false; }
    }
    async function listDevices(){
      try{ const ds = await navigator.mediaDevices.enumerateDevices();
        E.mics = ds.filter(d => d.kind === 'audioinput'); E.cams = ds.filter(d => d.kind === 'videoinput'); }catch(_){}
    }

    /* ---- audio ---- */
    function ensureAudio(){
      if(!E.ac){ const AC = window.AudioContext || window.webkitAudioContext; if(!AC) return null;
        E.ac = new AC(); E.dest = E.ac.createMediaStreamDestination(); }
      if(E.ac.state === 'suspended'){ try{ E.ac.resume(); }catch(_){} }
      return E.ac;
    }
    function chCfg(k){ return E.doc.audio[k] || { vol: 1, muted: false }; }
    function applyGain(k){ const c = E.ch.get(k); if(c){ const g = chCfg(k); c.gain.gain.value = g.muted ? 0 : g.vol; } }
    function addChannel(k, stream){
      if(!ensureAudio()) return;
      dropChannel(k);
      const src = E.ac.createMediaStreamSource(stream), gain = E.ac.createGain(), an = E.ac.createAnalyser();
      an.fftSize = 2048; src.connect(gain); gain.connect(an); gain.connect(E.dest);
      E.ch.set(k, { stream, src, gain, an, buf: new Float32Array(an.fftSize), peak: -60, rms: -60, hold: -60 });
      applyGain(k); startMeters(); paintMixer();
    }
    function dropChannel(k){
      const c = E.ch.get(k); if(!c) return;
      E.ch.delete(k);
      try{ c.src.disconnect(); c.gain.disconnect(); }catch(_){}
      if(k === 'mic'){ try{ c.stream.getTracks().forEach(t => t.stop()); }catch(_){} }
      paintMixer();
    }
    async function enableMic(){
      if(!canCamera()){ setWhy('This browser has no microphone access.'); return; }
      ensureAudio();
      const a = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
      const dev = chCfg('mic').device; if(dev) a.deviceId = { exact: dev };
      try{ const st = await navigator.mediaDevices.getUserMedia({ audio: a, video: false }); addChannel('mic', st); await listDevices(); paintMixer(); }
      catch(e){ setWhy(e && e.name === 'NotAllowedError' ? 'Microphone blocked — allow it for this site and try again.' : 'Microphone could not start' + (e && e.message ? ': ' + e.message : '')); }
    }
    function startMeters(){
      if(E.meterRaf) return;
      const tick = () => {
        E.meterRaf = 0;
        if(!E.ch.size) return;
        for(const [k, c] of E.ch){
          c.an.getFloatTimeDomainData(c.buf);
          let pk = 0, sum = 0; for(let i = 0; i < c.buf.length; i++){ const v = Math.abs(c.buf[i]); if(v > pk) pk = v; sum += v * v; }
          c.peak = dbOf(pk); c.rms = dbOf(Math.sqrt(sum / c.buf.length));
          c.hold = Math.max(c.peak, c.hold - 0.6);
          const m = document.querySelector(`.pbs-strip[data-ch="${k}"] .pbs-meter`);
          if(m){
            const pct = d => ((d + 60) / 60 * 100).toFixed(1) + '%';
            m.style.setProperty('--rms', pct(c.rms)); m.style.setProperty('--peak', pct(c.hold));
            m.dataset.peakDb = String(Math.round(c.peak)); m.dataset.rmsDb = String(Math.round(c.rms));
            m.classList.toggle('hot', c.peak > -6);
            const lab = m.parentElement && m.parentElement.querySelector('.pbs-db'); if(lab) lab.textContent = c.peak <= -59.5 ? '−∞' : c.peak.toFixed(0) + ' dB';
          }
        }
        E.meterRaf = requestAnimationFrame(tick);
      };
      E.meterRaf = requestAnimationFrame(tick);
    }

    /* ---- going live ---- */
    function setPhase(p, why){ E.phase = p; if(why !== undefined) E.why = why; paintStatus(); }
    function setWhy(t, kind){ E.why = t || ''; E.whyKind = kind || ''; paintStatus(); }
    function encodingFor(){ return { maxBitrate: effKbps() * 1000, maxFramerate: E.doc.out.fps }; }
    async function applyEncoding(){
      const pc = E.pc; if(!pc) return;
      try{ const vs = pc.getSenders().find(s => s.track && s.track.kind === 'video'); if(!vs) return;
        const p = vs.getParameters(); p.encodings = (p.encodings && p.encodings.length) ? p.encodings : [{}];
        p.encodings[0].maxBitrate = effKbps() * 1000; p.encodings[0].maxFramerate = E.doc.out.fps;
        await vs.setParameters(p); }catch(_){}
    }
    function withTimeout(p, ms){ return Promise.race([p, new Promise(r => setTimeout(() => r(''), ms))]); }
    async function thumbnailUrl(){
      const m = E.doc.meta;
      if(m.thumb === 'drive' && m.thumbUrl) return m.thumbUrl;
      try{
        const blob = await new Promise(r => E.canvas.toBlob(r, 'image/jpeg', 0.82));
        if(!blob) return '';
        const file = new File([blob], 'pbs-thumbnail.jpg', { type: 'image/jpeg' });
        return String(await withTimeout(dep.uploadBlob(file, { noMirror: true, folder: 'Streams' }).catch(() => ''), 15000) || '');
      }catch(_){ return ''; }
    }
    async function goLive(){
      if(E.live || E.phase === 'connecting') return;
      if(S.GUEST || !S.ME){ dep.guestPrompt && dep.guestPrompt(); return; }
      if(dep.standalone()){ setPhase('error', 'Streaming needs a PosterChan instance. PBS composes and previews here; connect an instance (Settings → Instance) to go live.'); return; }
      readMeta();
      setPhase('connecting', 'Connecting to your instance…');
      let mod;
      try{ mod = await dep.streams(); }catch(_){ mod = null; }
      if(!mod){ setPhase('error', 'Couldn’t load the streaming code — check your connection and try again.'); return; }
      const st = mod._studioState();
      if(st.otherLive){ setPhase('error', 'You’re already live from another source — stop that stream first.'); return; }
      ensureAudio();
      const vt = E.canvas.captureStream(E.doc.out.fps).getVideoTracks()[0];
      try{ vt.contentHint = 'detail'; }catch(_){}
      const at = E.dest.stream.getAudioTracks()[0].clone();
      const out = new MediaStream([vt, at]);
      const m = E.doc.meta;
      const r = await mod._studioGoLive(out, { title: m.title, details: { summary: m.summary, tags: m.tags },
        image: thumbnailUrl, announce: m.announce, encoding: encodingFor(),
        onDropped: () => { E.droppedAt = Date.now(); } });
      if(!r || !r.ok){ try{ out.getTracks().forEach(t => t.stop()); }catch(_){} setPhase('error', (r && r.why) || 'couldn’t go live'); return; }
      E.live = true; E.pc = r.pc; E.mod = mod; E.addr = r.addr; E.relays = r.relays || []; E.started = Date.now(); E.stats = { kbps: 0, enc: null, last: null };
      setPhase('live', r.announced ? '' : 'Live on the server, but the announcement didn’t reach your relay yet — viewers can’t find it until it does.');
      E.pc.addEventListener('connectionstatechange', () => {
        if(E.pc && (E.pc.connectionState === 'failed' || E.pc.connectionState === 'closed') && E.live){
          finishLive('error', 'The connection to the stream server dropped — the stream was ended.'); }
      });
      clearInterval(E.clock); E.clock = setInterval(paintClock, 1000);
      clearInterval(E.statTimer); E.statTimer = setInterval(pollStats, 2000); pollStats();
      hookChat();
      paint();
    }
    function finishLive(phase, why){
      E.live = false; E.pc = null; clearInterval(E.clock); clearInterval(E.statTimer);
      setPhase(phase, why); paint();
    }
    async function stopLive(){
      if(!E.live) return;
      setPhase('ending', 'Ending the stream…');
      let mod = null; try{ mod = await dep.streams(); }catch(_){}
      try{ if(mod) await mod._studioStop(); }catch(_){}
      finishLive('offline', 'Stream ended.');
    }
    async function pollStats(){
      const pc = E.pc; if(!pc) return;
      // Ended somewhere else (the stream page's End button, another tab's sweep): the broadcast is streams.js's, so
      // ask it rather than trust our own flag -- a closed PeerConnection fires no event to tell us.
      try{ if(E.mod && !E.mod._studioState().live){ finishLive('offline', 'The stream was ended.'); return; } }catch(_){}
      let rep = null;
      try{ const st = await pc.getStats(); st.forEach(x => { if(x.type === 'outbound-rtp' && (x.kind === 'video' || x.mediaType === 'video')) rep = x; }); }catch(_){}
      if(!rep) return;
      const now = rep.timestamp || performance.now(), last = E.stats.last;
      if(last && now > last.t) E.stats.kbps = Math.max(0, Math.round((rep.bytesSent - last.b) * 8 / (now - last.t)));
      E.stats.last = { t: now, b: rep.bytesSent || 0 };
      E.stats.enc = encoderKind(rep);
      E.stats.res = rep.frameWidth ? rep.frameWidth + '×' + rep.frameHeight : '';
      paintStatus();
    }
    function hookChat(){
      const box = $('#pbs-chat-msgs'); if(!box || !E.live || !E.addr) return;
      dep.streams().then(mod => {
        if(!E.live || !document.contains(box)) return;
        box.innerHTML = '<div class="pbs-empty">Live — viewers’ messages appear here.</div>';
        mod._streamChat(E.addr, '', E.relays, { box, inp: $('#pbs-chat-inp'), send: $('#pbs-chat-send'),
          alive: () => E.live && document.contains(box) });
      }).catch(() => {});
    }

    /* ---- painting ---- */
    function seg(name, opts, cur, label){
      const i = Math.max(0, opts.findIndex(o => String(o[0]) === String(cur)));
      return `<div class="pbs-seg" data-seg="${name}" role="radiogroup" aria-label="${enc(label)}" style="--n:${opts.length};--i:${i}"><span class="pbs-seg-thumb" aria-hidden="true"></span>${
        opts.map((o, k) => `<button type="button" role="radio" class="pbs-chip${k === i ? ' on' : ''}" aria-checked="${k === i}" data-v="${enc(String(o[0]))}">${enc(o[1])}</button>`).join('')}</div>`;
    }
    function wireSeg(root, name, onPick){
      const el = $(`.pbs-seg[data-seg="${name}"]`, root); if(!el) return;
      $$('.pbs-chip', el).forEach((b, k) => b.onclick = () => {
        el.style.setProperty('--i', k);
        $$('.pbs-chip', el).forEach((x, j) => { x.classList.toggle('on', j === k); x.setAttribute('aria-checked', String(j === k)); });
        onPick(b.dataset.v);
      });
    }
    function fmtClock(ms){ const s = Math.max(0, Math.floor(ms / 1000)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
      return (h ? h + ':' : '') + String(m).padStart(h ? 2 : 1, '0') + ':' + String(s % 60).padStart(2, '0'); }
    function paintClock(){ const t = $('#pbs-timer'); if(t) t.textContent = E.live ? fmtClock(Date.now() - E.started) : '0:00'; }
    function paintStatus(){
      const root = $('#pbs'); if(!root) return;
      const pill = $('#pbs-status', root);
      const label = { offline: 'OFFLINE', idle: 'OFFLINE', connecting: 'CONNECTING', live: 'LIVE', ending: 'ENDING', error: 'NOT LIVE' }[E.phase] || 'OFFLINE';
      if(pill){ pill.dataset.state = E.phase === 'idle' ? 'offline' : E.phase; $('.pbs-status-t', pill).textContent = label; }
      paintClock();
      const k = $('#pbs-kbps', root); if(k) k.textContent = E.live ? (E.stats.kbps + ' kbps') : '— kbps';
      const en = $('#pbs-enc', root);
      if(en){ const e = E.stats.enc;
        en.dataset.hw = e ? e.kind : '';
        en.textContent = !E.live ? 'Encoder: H.264 · hardware preferred' : !e ? 'Encoder: measuring…'
          : 'Encoder: ' + e.kind + (e.impl ? ' (' + e.impl + ')' : '');
        en.title = e && e.kind === 'software' ? 'This machine is encoding on the CPU. On Linux the desktop app turns on VA-API; a GPU without an H.264 encoder (or a missing driver) falls back to software.' : ''; }
      const why = $('#pbs-why', root);
      if(why){ why.textContent = E.why || ''; why.hidden = !E.why; why.dataset.kind = E.phase === 'error' ? 'error' : (E.whyKind || 'info');
        const mac = $('#pbs-mac', root); if(mac) mac.hidden = E.whyKind !== 'mac'; }
      const go = $('#pbs-go', root);
      if(go){
        go.disabled = E.phase === 'connecting' || E.phase === 'ending' || (!E.live && dep.standalone());
        go.classList.toggle('is-live', E.live);
        go.innerHTML = E.live ? `${ic('stop')}<span>End stream</span>` : E.phase === 'connecting' ? `${ic('live')}<span>Connecting…</span>` : `${ic('live')}<span>Go Live</span>`;
      }
      root.classList.toggle('pbs-is-live', E.live);
    }
    function layerName(s){
      if(s.type === 'text') return (s.text || 'Text').split('\n')[0].slice(0, 40) || 'Text';
      return s.name || { screen: 'Screen', camera: 'Camera', image: 'Image' }[s.type];
    }
    const typeIcon = { screen: 'monitor', camera: 'camera', image: 'image', text: 'text' };
    function paintLayers(){
      const box = $('#pbs-layers'); if(!box) return;
      const list = E.doc.sources.slice().reverse();      // top of the stack first, as in every editor
      box.innerHTML = list.length ? list.map(s => {
        const needs = (s.type === 'screen' || s.type === 'camera') && !E.media.get(s.id);
        return `<div class="pbs-layer${E.sel === s.id ? ' on' : ''}${s.visible ? '' : ' off'}" data-id="${s.id}" role="listitem">
          <button type="button" class="pbs-layer-main" data-a="sel" aria-pressed="${E.sel === s.id}">${ic(typeIcon[s.type])}<span class="pbs-layer-n">${enc(layerName(s))}</span>${needs ? '<span class="pbs-tag">not capturing</span>' : ''}</button>
          ${needs ? `<button type="button" class="pbs-ib pbs-cap" data-a="cap" title="Start capture" aria-label="Start capture">${ic('play')}</button>` : ''}
          <button type="button" class="pbs-ib" data-a="vis" aria-pressed="${s.visible}" title="${s.visible ? 'Hide' : 'Show'}" aria-label="${s.visible ? 'Hide' : 'Show'} ${enc(layerName(s))}">${ic('eye')}</button>
          <button type="button" class="pbs-ib" data-a="up" title="Move up" aria-label="Move up">${ic('chevron-up')}</button>
          <button type="button" class="pbs-ib" data-a="down" title="Move down" aria-label="Move down">${ic('chevron-down')}</button>
          <button type="button" class="pbs-ib pbs-del" data-a="del" title="Remove" aria-label="Remove ${enc(layerName(s))}">${ic('trash')}</button></div>`; }).join('')
        : `<div class="pbs-empty">No sources yet — add your screen, a camera, an image or text.</div>`;
      $$('.pbs-layer', box).forEach(row => {
        const s = src(row.dataset.id); if(!s) return;
        $$('[data-a]', row).forEach(b => b.onclick = async ev => {
          ev.stopPropagation(); ensureAudio();
          const a = b.dataset.a, i = E.doc.sources.indexOf(s);
          if(a === 'sel'){ E.sel = s.id; }
          else if(a === 'vis'){ s.visible = !s.visible; scheduleSave(); }
          else if(a === 'up' && i < E.doc.sources.length - 1){ E.doc.sources.splice(i, 1); E.doc.sources.splice(i + 1, 0, s); scheduleSave(); }
          else if(a === 'down' && i > 0){ E.doc.sources.splice(i, 1); E.doc.sources.splice(i - 1, 0, s); scheduleSave(); }
          else if(a === 'del'){ E.doc.sources.splice(i, 1); dropMedia(s.id); if(E.sel === s.id) E.sel = ''; scheduleSave(); }
          else if(a === 'cap'){ if(s.type === 'screen') await captureScreen(s); else await captureCamera(s); }
          paintLayers(); paintProps(); placeSel();
        });
      });
    }
    function paintProps(){
      const box = $('#pbs-props'); if(!box) return;
      const s = src(E.sel);
      if(!s){ box.innerHTML = `<div class="pbs-empty">Select a source on the preview or in the list to edit it.</div>`; return; }
      let h = `<div class="pbs-ph">${ic(typeIcon[s.type])}<b>${enc(layerName(s))}</b></div>`;
      if(s.type === 'text'){
        h += `<label class="pbs-f"><span>Text</span><div class="pbs-field"><textarea id="pbs-p-text" rows="2" maxlength="500">${enc(s.text)}</textarea></div></label>
          <div class="pbs-f"><span>Font</span>${seg('font', [['display', 'Display'], ['sans', 'Sans'], ['mono', 'Mono'], ['serif', 'Serif']], s.font, 'Font')}</div>
          <label class="pbs-f"><span>Size <output id="pbs-p-size-o">${Math.round(s.size)}</output></span><input type="range" id="pbs-p-size" min="12" max="200" value="${Math.round(s.size)}"></label>
          <div class="pbs-row3">
            <label class="pbs-f pbs-col"><span>Colour</span><input type="color" id="pbs-p-color" value="${s.color}"></label>
            <label class="pbs-f pbs-col"><span>Outline</span><span class="pbs-inl"><input type="checkbox" id="pbs-p-outline"${s.outline ? ' checked' : ''}><input type="color" id="pbs-p-ocolor" value="${s.ocolor}" aria-label="Outline colour"></span></label>
            <label class="pbs-f pbs-col"><span>Shadow</span><span class="pbs-inl"><input type="checkbox" id="pbs-p-shadow"${s.shadow ? ' checked' : ''}></span></label>
          </div>`;
      }
      if(s.type === 'camera'){
        h += `<label class="pbs-f"><span>Camera</span><div class="pbs-field"><select id="pbs-p-cam"><option value="">Default camera</option>${
          E.cams.map((d, i) => `<option value="${enc(d.deviceId)}"${d.deviceId === s.device ? ' selected' : ''}>${enc(d.label || 'Camera ' + (i + 1))}</option>`).join('')}</select></div></label>`;
      }
      if(s.type === 'screen'){
        h += `<button type="button" class="btn btn-ghost small pbs-btn" id="pbs-p-repick">${ic('monitor')}Change what’s shared…</button>`;
      }
      h += `<label class="pbs-f"><span>Opacity <output id="pbs-p-op-o">${Math.round(s.opacity * 100)}%</output></span><input type="range" id="pbs-p-opacity" min="0" max="100" value="${Math.round(s.opacity * 100)}"></label>
        <div class="pbs-acts"><button type="button" class="btn btn-ghost small pbs-btn" id="pbs-p-fit">${ic('fit')}Fill canvas</button><button type="button" class="btn btn-ghost small pbs-btn" id="pbs-p-center">${ic('target')}Center</button></div>`;
      box.innerHTML = h;
      const on = (id, ev, fn) => { const el = $('#' + id, box); if(el) el[ev] = fn; };
      on('pbs-p-text', 'oninput', e => { s.text = e.target.value.slice(0, 500); scheduleSave(); const n = $('.pbs-layer.on .pbs-layer-n'); if(n) n.textContent = layerName(s); });
      wireSeg(box, 'font', v => { s.font = v; scheduleSave(); });
      on('pbs-p-size', 'oninput', e => { s.size = Number(e.target.value); $('#pbs-p-size-o', box).textContent = e.target.value; scheduleSave(); });
      on('pbs-p-color', 'oninput', e => { s.color = e.target.value; scheduleSave(); });
      on('pbs-p-outline', 'onchange', e => { s.outline = e.target.checked; scheduleSave(); });
      on('pbs-p-ocolor', 'oninput', e => { s.ocolor = e.target.value; scheduleSave(); });
      on('pbs-p-shadow', 'onchange', e => { s.shadow = e.target.checked; scheduleSave(); });
      on('pbs-p-opacity', 'oninput', e => { s.opacity = Number(e.target.value) / 100; $('#pbs-p-op-o', box).textContent = e.target.value + '%'; scheduleSave(); });
      on('pbs-p-cam', 'onchange', async e => { s.device = e.target.value; scheduleSave(); await captureCamera(s); paintLayers(); });
      on('pbs-p-repick', 'onclick', async () => { await captureScreen(s); paintLayers(); });
      on('pbs-p-fit', 'onclick', () => { s.x = 0; s.y = 0; if(s.type === 'text'){ s.x = 0.04; s.y = 0.04; } else { s.w = 1; s.h = 1; } scheduleSave(); placeSel(); });
      on('pbs-p-center', 'onclick', () => { s.x = (1 - s.w) / 2; s.y = (1 - s.h) / 2; scheduleSave(); placeSel(); });
    }
    function paintMixer(){
      const box = $('#pbs-mixer'); if(!box) return;
      const strip = (k, name, extra) => {
        const c = chCfg(k), on = E.ch.has(k);
        return `<div class="pbs-strip${on ? '' : ' idle'}${c.muted ? ' muted' : ''}" data-ch="${k}">
          <div class="pbs-strip-h">${ic(k === 'mic' ? 'mic' : 'volume')}<b>${name}</b><span class="pbs-db">${on ? '' : '—'}</span></div>
          <div class="pbs-meter" data-peak-db="-60" data-rms-db="-60" role="meter" aria-label="${name} level" aria-valuemin="-60" aria-valuemax="0"><i></i><b></b><span class="pbs-ticks" aria-hidden="true"></span></div>
          <div class="pbs-strip-c"><input type="range" class="pbs-vol" min="0" max="200" value="${Math.round(c.vol * 100)}" aria-label="${name} volume"${on ? '' : ' disabled'}>
            <button type="button" class="pbs-ib pbs-mute" aria-pressed="${c.muted}" title="${c.muted ? 'Unmute' : 'Mute'}" aria-label="${c.muted ? 'Unmute' : 'Mute'} ${name}"${on ? '' : ' disabled'}>${ic(c.muted ? 'volume-mute' : 'volume')}</button></div>
          ${extra || ''}</div>`;
      };
      const micDev = chCfg('mic').device || '';
      const micExtra = `<div class="pbs-strip-x">${E.ch.has('mic') ? '' : `<button type="button" class="pbs-arm" id="pbs-mic-on">${ic('mic')}Use microphone</button>`}
        <div class="pbs-field pbs-dsel"><select id="pbs-mic-dev" aria-label="Microphone"><option value="">Default microphone</option>${
          E.mics.map((d, i) => `<option value="${enc(d.deviceId)}"${d.deviceId === micDev ? ' selected' : ''}>${enc(d.label || 'Microphone ' + (i + 1))}</option>`).join('')}</select></div></div>`;
      const deskExtra = E.ch.has('desk') ? '' : `<div class="pbs-strip-x pbs-note">${canScreen() ? 'Desktop sound comes with a screen capture where your system shares it.' : 'No desktop audio on this device.'}</div>`;
      box.innerHTML = strip('mic', 'Microphone', micExtra) + strip('desk', 'Desktop audio', deskExtra);
      $$('.pbs-strip', box).forEach(el => {
        const k = el.dataset.ch, c = chCfg(k);
        const vol = $('.pbs-vol', el); if(vol) vol.oninput = () => { c.vol = Number(vol.value) / 100; E.doc.audio[k] = c; applyGain(k); scheduleSave(); };
        const mu = $('.pbs-mute', el); if(mu) mu.onclick = () => { c.muted = !c.muted; E.doc.audio[k] = c; applyGain(k); scheduleSave(); paintMixer(); };
      });
      const mo = $('#pbs-mic-on', box); if(mo) mo.onclick = () => enableMic();
      const md = $('#pbs-mic-dev', box); if(md) md.onchange = async () => { E.doc.audio.mic.device = md.value; scheduleSave(); if(E.ch.has('mic')) await enableMic(); };
      if(E.ch.size) startMeters();
    }
    function placeSel(){
      const box = $('#pbs-sel'); if(!box) return;
      const s = src(E.sel);
      if(!s || !s.visible){ box.hidden = true; return; }
      box.hidden = false;
      box.style.left = (s.x * 100) + '%'; box.style.top = (s.y * 100) + '%';
      box.style.width = (s.w * 100) + '%'; box.style.height = (s.h * 100) + '%';
    }
    function readMeta(){
      const m = E.doc.meta, v = id => { const el = $('#' + id); return el ? el.value : null; };
      const t = v('pbs-title'); if(t != null) m.title = t.slice(0, 120);
      const su = v('pbs-summary'); if(su != null) m.summary = su.slice(0, 1000);
      const tg = v('pbs-tags'); if(tg != null) m.tags = tg.slice(0, 400);
      const an = $('#pbs-announce'); if(an) m.announce = !!an.checked;
    }

    function stagePoint(ev){
      const r = E.canvas.getBoundingClientRect();
      return { x: (ev.clientX - r.left) / r.width, y: (ev.clientY - r.top) / r.height };
    }
    function hit(p){
      for(let i = E.doc.sources.length - 1; i >= 0; i--){
        const s = E.doc.sources[i];
        if(s.visible && p.x >= s.x && p.x <= s.x + s.w && p.y >= s.y && p.y <= s.y + s.h) return s;
      }
      return null;
    }
    function wireStage(stage){
      stage.onpointerdown = ev => {
        ensureAudio();
        const handle = ev.target.closest && ev.target.closest('.pbs-handle');
        const p = stagePoint(ev);
        let s = handle ? src(E.sel) : hit(p);
        if(!s){ E.sel = ''; paintLayers(); paintProps(); placeSel(); return; }
        if(E.sel !== s.id){ E.sel = s.id; paintLayers(); paintProps(); }
        ev.preventDefault();
        try{ stage.setPointerCapture(ev.pointerId); }catch(_){}
        E.drag = { mode: handle ? 'size' : 'move', s, p0: p, x: s.x, y: s.y, w: s.w, h: s.h, size: s.size };
        placeSel();
      };
      stage.onpointermove = ev => {
        const d = E.drag; if(!d) return;
        const p = stagePoint(ev), dx = p.x - d.p0.x, dy = p.y - d.p0.y, s = d.s;
        if(d.mode === 'move'){ s.x = Math.min(1 - 0.02, Math.max(-s.w + 0.02, d.x + dx)); s.y = Math.min(1 - 0.02, Math.max(-s.h + 0.02, d.y + dy)); }
        else {
          const k = Math.max(0.05, (d.w + dx) / d.w);
          if(s.type === 'text') s.size = Math.min(240, Math.max(8, d.size * k));
          else { s.w = Math.max(0.03, d.w * k); s.h = Math.max(0.03, d.h * k); }
        }
        placeSel();
      };
      const end = ev => { if(!E.drag) return; E.drag = null; try{ stage.releasePointerCapture(ev.pointerId); }catch(_){} scheduleSave(); if(src(E.sel) && src(E.sel).type === 'text') paintProps(); };
      stage.onpointerup = end; stage.onpointercancel = end;
    }

    function injectCss(){
      if(document.getElementById('pc-pbs-css')) return;
      const st = document.createElement('style'); st.id = 'pc-pbs-css';
      st.textContent = PBS_CSS;
      document.head.appendChild(st);
    }

    function paint(){
      const root = $('#pbs'); if(!root) return;
      paintStatus(); paintLayers(); paintProps(); paintMixer(); placeSel();
      const n = $('#pbs-saved', root); if(n) n.textContent = E.loadNote || '';
    }

    function render(){
      const f = document.getElementById('feed'); if(!f) return;
      injectCss(); ensureCanvas();
      const standalone = dep.standalone(), m = E.doc.meta, out = E.doc.out;
      const logo = (S.LOGO || '/static/posterchan-relay.png');
      f.innerHTML = `<div class="pbs" id="pbs">
        <header class="pbs-head pbs-hud">
          <div class="pbs-brand"><span class="pbs-logo"><img src="${enc(logo)}" alt="" onerror="this.src='/static/posterchan-relay.png'"></span>
            <div class="pbs-word"><b>PBS</b><span>PosterChan Broadcast Studio</span></div></div>
          <div class="pbs-meta" role="status" aria-live="polite">
            <span class="pbs-status" id="pbs-status" data-state="offline"><i aria-hidden="true"></i><span class="pbs-status-t">OFFLINE</span></span>
            <span class="pbs-stat" title="Time live">${ic('clock')}<span id="pbs-timer">0:00</span></span>
            <span class="pbs-stat" title="Outgoing video bitrate">${ic('zap')}<span id="pbs-kbps">— kbps</span></span>
            <span class="pbs-stat" title="Watching now">${ic('eye')}<span id="pbs-viewers">0</span></span>
            <span class="pbs-stat pbs-capchip" id="pbs-capchip" title="What leaves this computer"></span>
            <span class="pbs-stat pbs-enc" id="pbs-enc" data-hw="">Encoder: H.264 · hardware preferred</span>
          </div>
        </header>
        <p class="pbs-why" id="pbs-why" hidden></p>
        <div class="pbs-mac" id="pbs-mac" hidden><button type="button" class="btn btn-ghost small pbs-btn" id="pbs-mac-open">${ic('gear')}Open Screen Recording settings</button></div>
        <section class="pbs-stagewrap pbs-hud" aria-label="Preview">
          <div class="pbs-stage" id="pbs-stage" style="aspect-ratio:${dims()[0]}/${dims()[1]}">
            <div class="pbs-sel" id="pbs-sel" hidden><span class="pbs-handle" aria-hidden="true"></span></div>
          </div>
          <div class="pbs-addbar" role="toolbar" aria-label="Add a source">
            <button type="button" class="pbs-add" id="pbs-add-screen"${canScreen() ? '' : ' aria-disabled="true"'}>${ic('monitor')}<span>Screen</span></button>
            <button type="button" class="pbs-add" id="pbs-add-camera">${ic('camera')}<span>Camera</span></button>
            <button type="button" class="pbs-add" id="pbs-add-image">${ic('image')}<span>Image</span></button>
            <button type="button" class="pbs-add" id="pbs-add-text">${ic('text')}<span>Text</span></button>
            <input type="file" id="pbs-img-file" accept="image/*" hidden>
          </div>
          ${canScreen() ? '' : `<p class="pbs-note pbs-mobile-note">Screen capture is desktop-only — phones and tablets don’t allow it. Camera, images and text work here.</p>`}
        </section>
        <aside class="pbs-side">
          <section class="pbs-panel pbs-hud pbs-golive" aria-label="Go live">
            <h3 class="pbs-h">${ic('live')}Go live on Nostr</h3>
            ${standalone ? `<p class="pbs-note pbs-warn" id="pbs-noinst">Streaming needs a PosterChan instance. PBS composes and previews here; connect an instance (Settings → Instance) to go live.</p>` : ''}
            <label class="pbs-f"><span>Title</span><div class="pbs-field"><input id="pbs-title" maxlength="120" placeholder="What are you streaming?" value="${enc(m.title)}"></div></label>
            <label class="pbs-f"><span>Description</span><div class="pbs-field"><textarea id="pbs-summary" rows="2" maxlength="1000" placeholder="What’s on, a schedule, links…">${enc(m.summary)}</textarea></div></label>
            <label class="pbs-f"><span>Tags <small>comma separated</small></span><div class="pbs-field"><input id="pbs-tags" maxlength="400" placeholder="gaming, music, art" value="${enc(m.tags)}"></div></label>
            <div class="pbs-f"><span>Thumbnail</span>${seg('thumb', [['snapshot', 'Live snapshot'], ['drive', 'From my drive']], m.thumb, 'Thumbnail')}
              <img id="pbs-thumb-prev" class="pbs-thumb"${m.thumb === 'drive' && m.thumbUrl ? ` src="${enc(m.thumbUrl)}"` : ' hidden'} alt="Chosen thumbnail"></div>
            <label class="pbs-check"><input type="checkbox" id="pbs-announce"${m.announce ? ' checked' : ''}> Post to my followers with a watch link</label>
            <button type="button" class="pbs-go" id="pbs-go"${standalone ? ' disabled' : ''}>${ic('live')}<span>Go Live</span></button>
            <details class="pbs-ext" id="pbs-ext"><summary>External encoder <small>OBS, ffmpeg, Larix</small></summary><div id="pbs-ext-body" class="pbs-note">Loading your server and stream key…</div></details>
          </section>
          <section class="pbs-panel pbs-hud pbs-chat" aria-label="Stream chat">
            <h3 class="pbs-h">${ic('chat')}Stream chat</h3>
            <div class="pbs-chat-msgs" id="pbs-chat-msgs" aria-live="polite"><div class="pbs-empty">Chat opens when you go live — viewers’ messages appear here.</div></div>
            <div class="pbs-chat-in"><div class="pbs-field"><input id="pbs-chat-inp" maxlength="500" placeholder="Say something to your viewers" aria-label="Chat message"></div><button type="button" class="pbs-ib pbs-send" id="pbs-chat-send" aria-label="Send">${ic('send')}</button></div>
          </section>
        </aside>
        <div class="pbs-dock">
          <section class="pbs-panel pbs-hud" aria-label="Sources"><h3 class="pbs-h">${ic('layout')}Sources</h3><div class="pbs-layers" id="pbs-layers" role="list"></div></section>
          <section class="pbs-panel pbs-hud" aria-label="Properties"><h3 class="pbs-h">${ic('sliders')}Properties</h3><div id="pbs-props"></div></section>
          <section class="pbs-panel pbs-hud" aria-label="Audio mixer"><h3 class="pbs-h">${ic('volume')}Audio mixer</h3><div class="pbs-mixer" id="pbs-mixer"></div></section>
          <section class="pbs-panel pbs-hud" aria-label="Output"><h3 class="pbs-h">${ic('stream')}Output</h3>
            <label class="pbs-check pbs-cap"><input type="checkbox" id="pbs-cap"${out.cap720 ? ' checked' : ''}> Cap at 720p <small>saves bandwidth — 720p, at most 1.5 Mbps</small></label>
            <div class="pbs-f"><span>Resolution</span>${seg('res', [['720', '720p'], ['1080', '1080p']], out.res, 'Resolution')}</div>
            <div class="pbs-f"><span>Frame rate</span>${seg('fps', [['30', '30 fps'], ['60', '60 fps']], out.fps, 'Frame rate')}</div>
            <div class="pbs-f"><span>Upload</span>${seg('kbps', [['900', 'Saver'], ['1500', 'Standard'], ['3000', 'High']], out.kbps, 'Upload bitrate')}</div>
            <p class="pbs-note" id="pbs-out-note"></p>
          </section>
        </div>
        <p class="pbs-saved" id="pbs-saved" role="status"></p>
      </div>`;
      const root = $('#pbs');
      const stage = $('#pbs-stage', root);
      stage.insertBefore(E.canvas, stage.firstChild);
      wireStage(stage);
      /* The cap is shown, not just applied: the 1080p and High choices are greyed while it is on, the segmented
       * controls show what is really sent, and the header chip says it at a glance. */
      const showSeg = (name, v) => { const el = $(`.pbs-seg[data-seg="${name}"]`, root); if(!el) return;
        $$('.pbs-chip', el).forEach((b, k) => { const on = b.dataset.v === String(v); b.classList.toggle('on', on); b.setAttribute('aria-checked', String(on)); if(on) el.style.setProperty('--i', k); }); };
      const outNote = () => {
        const cap = E.doc.out.cap720, n = $('#pbs-out-note', root);
        [['res', '1080'], ['kbps', '3000']].forEach(([g, v]) => { const b = $(`.pbs-seg[data-seg="${g}"] .pbs-chip[data-v="${v}"]`, root);
          if(b){ b.disabled = cap; b.title = cap ? 'Turn off “Cap at 720p” to use this' : ''; } });
        showSeg('res', effRes()); showSeg('kbps', effKbps());
        if(n) n.textContent = `${dims()[0]}×${dims()[1]} at ${E.doc.out.fps} fps, video capped at ${effKbps()} kbps on the way out`
          + (cap ? ' — the 720p cap is on.' : ' — Standard matches what this instance serves viewers, so nothing is re-encoded twice.');
        const chip = $('#pbs-capchip', root); if(chip){ chip.textContent = (effRes() === '720' ? '720p' : '1080p') + E.doc.out.fps + ' · ≤' + (effKbps() / 1000) + ' Mbps'; chip.dataset.cap = cap ? '1' : ''; }
      };
      outNote();
      $('#pbs-cap', root).onchange = e => { E.doc.out.cap720 = !!e.target.checked; sizeCanvas(); stage.style.aspectRatio = dims()[0] + '/' + dims()[1]; outNote(); applyEncoding(); scheduleSave(); };
      wireSeg(root, 'res', v => { if(E.doc.out.cap720 && v !== '720') return; E.doc.out.res = v; sizeCanvas(); stage.style.aspectRatio = dims()[0] + '/' + dims()[1]; outNote(); scheduleSave(); });
      wireSeg(root, 'fps', v => { E.doc.out.fps = Number(v); setFps(); outNote(); applyEncoding(); scheduleSave(); });
      wireSeg(root, 'kbps', v => { if(E.doc.out.cap720 && Number(v) > 1500) return; E.doc.out.kbps = Number(v); outNote(); applyEncoding(); scheduleSave(); });
      wireSeg(root, 'thumb', async v => {
        E.doc.meta.thumb = v; scheduleSave();
        const pv = $('#pbs-thumb-prev', root);
        if(v === 'drive'){ dep.pickImage(url => { E.doc.meta.thumbUrl = url; pv.src = url; pv.hidden = false; scheduleSave(); }); }
        else if(pv) pv.hidden = true;
      });
      ['pbs-title', 'pbs-summary', 'pbs-tags'].forEach(id => { const el = $('#' + id, root); if(el) el.oninput = () => { readMeta(); scheduleSave(); }; });
      $('#pbs-announce', root).onchange = () => { readMeta(); scheduleSave(); };
      $('#pbs-go', root).onclick = () => { ensureAudio(); return E.live ? stopLive() : goLive(); };
      const mac = $('#pbs-mac-open', root); if(mac) mac.onclick = () => { try{ window.pcCapture && pcCapture.openSettings(); }catch(_){} };
      $('#pbs-add-screen', root).onclick = async () => {
        ensureAudio();
        if(!canScreen()){ setWhy('Screen capture is desktop-only — phones and tablets don’t allow it. Camera and overlays work here.'); return; }
        const s = { id: uid(), type: 'screen', name: 'Screen', visible: true, x: 0, y: 0, w: 1, h: 1, opacity: 1 };
        if(await captureScreen(s)){ E.doc.sources.unshift(s); E.sel = s.id; scheduleSave(); paint(); }
      };
      $('#pbs-add-camera', root).onclick = async () => {
        ensureAudio();
        const s = { id: uid(), type: 'camera', name: 'Camera', visible: true, x: 0.66, y: 0.62, w: 0.3, h: 0.3, opacity: 1, device: '' };
        if(await captureCamera(s)){ addSource(s); paint(); }
      };
      $('#pbs-add-text', root).onclick = () => {
        ensureAudio();
        addSource(cleanSource({ id: uid(), type: 'text', text: 'Live on PosterChan', x: 0.04, y: 0.05, font: 'display', size: 56, color: '#3ce8ff' }));
        paint(); const ta = $('#pbs-p-text'); if(ta){ ta.focus(); ta.select(); }
      };
      const file = $('#pbs-img-file', root);
      $('#pbs-add-image', root).onclick = () => { ensureAudio(); file.value = ''; file.click(); };
      file.onchange = async () => {
        const fl = file.files && file.files[0]; if(!fl) return;
        if(!/^image\//.test(fl.type)){ setWhy('That isn’t an image.'); return; }
        let url = '';
        if(!dep.standalone()){ try{ url = String(await withTimeout(dep.uploadBlob(fl, { noMirror: true, folder: 'PBS' }), 30000) || ''); }catch(_){ url = ''; } }
        if(!/^https?:\/\//.test(url)){ const id = 'img-' + uid() + uid(); try{ await idbPut(id, fl); url = 'idb:' + id; }catch(_){ setWhy('Couldn’t keep that image on this device.'); return; } }
        const s = cleanSource({ id: uid(), type: 'image', url, name: fl.name.slice(0, 60), x: 0.55, y: 0.06, w: 0.4, h: 0.4 });
        s.fresh = true;
        if(addSource(s)){ loadImage(s); paint(); }
      };
      const ext = $('#pbs-ext', root);
      ext.ontoggle = async () => {
        if(!ext.open || ext.dataset.loaded) return;
        const body = $('#pbs-ext-body', root);
        if(dep.standalone()){ body.textContent = 'An external encoder streams to a PosterChan instance — connect one first.'; return; }
        try{
          const mod = await dep.streams(); const info = await mod._studioExternal();
          ext.dataset.loaded = '1';
          const full = String(info.rtmp_url || '').replace(/\/+$/, '') + '/' + String(info.stream_key || '');
          const row = (id, label, val, secret) => `<label class="pbs-f"><span>${label}</span><span class="pbs-copy"><span class="pbs-field"><input id="${id}" readonly${secret ? ' type="password"' : ''} value="${enc(val)}"></span><button type="button" class="btn btn-ghost small" data-copy="${id}">Copy</button></span></label>`;
          body.innerHTML = `<p>For OBS and other encoders: Service <b>Custom</b>. PBS needs none of this.</p>${row('pbs-ext-srv', 'Server', info.rtmp_url || '')}${row('pbs-ext-key', 'Stream key', info.stream_key || '', true)}${row('pbs-ext-url', 'Stream URL', full, true)}
            <p>Then announce it from <b>Go Live</b> in the menu, which publishes the stream for an external encoder.</p>`;
          $$('[data-copy]', body).forEach(b => b.onclick = () => dep._copyFrom($('#' + b.dataset.copy, body)));
        }catch(e){ body.textContent = (e && e.message) || 'Couldn’t load your stream key.'; }
      };
      const inp = $('#pbs-chat-inp', root), send = $('#pbs-chat-send', root);
      if(!E.live){ const say = () => toast('chat opens when you go live'); send.onclick = say; inp.onkeydown = ev => { if(ev.key === 'Enter') say(); }; }
      paint();
      hookChat();
      if(S.ME && !S.GUEST){ listDevices().then(() => paintMixer()); loadFor(S.ME.pubkey); }
      // Another go-live path is already broadcasting: say so up front rather than at the button.
      dep.streams().then(mod => { const st = mod._studioState(); if(st.otherLive && !E.live) setWhy('You’re already live from another source — stop it before going live here.'); }).catch(() => {});
    }

    const PBS_CSS = `
.pbs{--pbs-a:var(--neon,#3ce8ff);--pbs-g:var(--green,#00ff88);--pbs-line:rgba(var(--accent-rgb,60,232,255),.22);--pbs-panel:color-mix(in srgb,var(--bg,#0a0a0f) 82%,rgba(var(--accent-rgb,60,232,255),.06));
  display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,360px);grid-template-areas:"head head" "why why" "mac mac" "stage side" "dock side" "saved saved";grid-template-rows:auto auto auto auto 1fr auto;
  gap:12px;padding:12px;max-width:1520px;margin:0 auto;box-sizing:border-box;min-width:0;color:var(--text)}
.pbs *{box-sizing:border-box}
.pbs-hud{position:relative;background:var(--pbs-panel);border:1px solid var(--pbs-line);border-radius:12px;
  box-shadow:inset 0 0 0 1px rgba(0,0,0,.35),0 0 24px rgba(var(--accent-rgb,60,232,255),.06)}
.pbs-hud::before,.pbs-hud::after{content:"";position:absolute;width:14px;height:14px;pointer-events:none;border:2px solid var(--pbs-a);opacity:.75}
.pbs-hud::before{top:-1px;left:-1px;border-right:0;border-bottom:0;border-top-left-radius:12px}
.pbs-hud::after{bottom:-1px;right:-1px;border-left:0;border-top:0;border-bottom-right-radius:12px}
.pbs-head{grid-area:head;display:flex;align-items:center;justify-content:space-between;gap:10px 16px;flex-wrap:wrap;padding:10px 14px;
  background:linear-gradient(90deg,rgba(var(--accent-rgb,60,232,255),.10),transparent 55%),var(--pbs-panel)}
.pbs-brand{display:flex;align-items:center;gap:12px;min-width:0}
.pbs-logo{width:44px;height:44px;border-radius:50%;padding:2px;flex:none;background:conic-gradient(from 210deg,var(--pbs-a),var(--pbs-g),var(--pbs-a));box-shadow:0 0 16px rgba(var(--accent-rgb,60,232,255),.55)}
.pbs-logo img{width:100%;height:100%;border-radius:50%;object-fit:cover;display:block;background:#000}
.pbs-word{display:flex;flex-direction:column;line-height:1.05;min-width:0}
.pbs-word b{font:900 26px/1 Orbitron,Inter,sans-serif;letter-spacing:.14em;background:linear-gradient(90deg,var(--pbs-a),var(--pbs-g));-webkit-background-clip:text;background-clip:text;color:transparent;filter:drop-shadow(0 0 8px rgba(var(--accent-rgb,60,232,255),.45))}
.pbs-word span{font:600 10.5px/1.3 Orbitron,Inter,sans-serif;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pbs-meta{display:flex;align-items:center;gap:8px 14px;flex-wrap:wrap;font:600 12.5px/1 'JetBrains Mono',ui-monospace,monospace;color:var(--muted);min-width:0}
.pbs-stat{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}.pbs-stat .ic{width:14px;height:14px;color:var(--pbs-a)}
.pbs-enc{padding:4px 8px;border:1px solid var(--pbs-line);border-radius:6px;white-space:normal}
.pbs-enc[data-hw=hardware]{color:var(--pbs-g);border-color:color-mix(in srgb,var(--pbs-g) 50%,transparent)}
.pbs-enc[data-hw=software]{color:var(--amber,#ffcf2b)}
.pbs-status{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;border:1px solid var(--pbs-line);font:800 12px/1 Orbitron,Inter,sans-serif;letter-spacing:.12em;color:var(--muted)}
.pbs-status i{width:9px;height:9px;border-radius:50%;background:currentColor}
.pbs-status[data-state=live]{color:#fff;background:linear-gradient(90deg,#ff2d55,#ff5a36);border-color:transparent;box-shadow:0 0 18px rgba(255,45,85,.55)}
.pbs-status[data-state=live] i{animation:pbsPulse 1s infinite}
.pbs-status[data-state=connecting],.pbs-status[data-state=ending]{color:var(--pbs-a)}.pbs-status[data-state=connecting] i{animation:pbsPulse .5s infinite}
.pbs-status[data-state=error]{color:var(--danger,#ff4d6d);border-color:color-mix(in srgb,var(--danger,#ff4d6d) 55%,transparent)}
@keyframes pbsPulse{50%{opacity:.25}}
.pbs-why{grid-area:why;margin:0;padding:10px 14px;border-radius:10px;border:1px solid var(--pbs-line);background:rgba(var(--accent-rgb,60,232,255),.07);font-size:14px}
.pbs-why[data-kind=error]{border-color:color-mix(in srgb,var(--danger,#ff4d6d) 60%,transparent);background:color-mix(in srgb,var(--danger,#ff4d6d) 12%,transparent)}
.pbs-why[hidden],.pbs-mac[hidden]{display:none}.pbs-mac{grid-area:mac}
.pbs-stagewrap{grid-area:stage;align-self:start;padding:10px;min-width:0;display:flex;flex-direction:column;gap:10px}
.pbs-stage{position:relative;width:100%;background:#000;border-radius:8px;overflow:hidden;touch-action:none;user-select:none;
  box-shadow:0 0 0 1px rgba(var(--accent-rgb,60,232,255),.35),0 0 30px rgba(var(--accent-rgb,60,232,255),.12)}
.pbs-stage::after{content:"";position:absolute;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(255,255,255,.025) 0 1px,transparent 1px 3px)}
.pbs-canvas{display:block;width:100%;height:100%;cursor:crosshair}
.pbs-sel{position:absolute;outline:2px dashed var(--pbs-g);outline-offset:0;pointer-events:none;box-shadow:0 0 12px rgba(0,255,136,.35);z-index:2}
.pbs-sel[hidden]{display:none}
.pbs-handle{position:absolute;right:-9px;bottom:-9px;width:18px;height:18px;border-radius:4px;background:var(--pbs-g);box-shadow:0 0 10px var(--pbs-g);pointer-events:auto;cursor:nwse-resize}
.pbs-addbar{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
.pbs-add{display:flex;align-items:center;justify-content:center;gap:7px;min-height:42px;padding:0 8px;border-radius:10px;border:1px solid var(--pbs-line);background:rgba(var(--accent-rgb,60,232,255),.06);color:var(--text);font:700 13px Inter,sans-serif;cursor:pointer;min-width:0;transition:border-color .15s,box-shadow .15s,background .15s}
.pbs-add span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pbs-add .ic{width:18px;height:18px;color:var(--pbs-a);flex:none}
.pbs-add:hover{border-color:var(--pbs-a);box-shadow:0 0 14px rgba(var(--accent-rgb,60,232,255),.25)}
.pbs-add:focus-visible,.pbs-ib:focus-visible,.pbs-go:focus-visible,.pbs-chip:focus-visible{outline:2px solid var(--pbs-a);outline-offset:2px}
.pbs-add[aria-disabled=true]{opacity:.45}
.pbs-side{grid-area:side;display:flex;flex-direction:column;gap:12px;min-width:0;align-self:stretch}
.pbs-panel{padding:12px 14px;min-width:0}
.pbs-h{display:flex;align-items:center;gap:8px;margin:0 0 10px;font:800 12px/1 Orbitron,Inter,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:var(--pbs-a)}
.pbs-h .ic{width:15px;height:15px}
.pbs-f{display:flex;flex-direction:column;gap:5px;margin:0 0 10px;min-width:0}
.pbs-f>span{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.04em;display:flex;justify-content:space-between;gap:8px}
.pbs-f small{font-weight:500;opacity:.8}
.pbs-field{display:flex;align-items:center;min-width:0;border:1px solid var(--pbs-line);border-radius:9px;background:rgba(0,0,0,.28);transition:border-color .15s,box-shadow .15s}
.pbs-field:focus-within{border-color:var(--pbs-a);box-shadow:0 0 0 2px rgba(var(--accent-rgb,60,232,255),.25)}
.pbs-field input,.pbs-field textarea,.pbs-field select{flex:1;min-width:0;width:100%;border:0;outline:0;background:transparent;color:var(--text);font:inherit;font-size:14px;padding:9px 10px;resize:vertical}
.pbs-field input:focus,.pbs-field textarea:focus,.pbs-field select:focus{outline:0;box-shadow:none}
.pbs-field select option{background:var(--bg2,#12121a);color:var(--text)}
.pbs input[type=range]{width:100%;accent-color:var(--pbs-a)}
.pbs-check{display:flex;align-items:center;gap:8px;font-size:13px;margin:2px 0 12px;cursor:pointer}
.pbs-check input{accent-color:var(--pbs-g);width:16px;height:16px}
.pbs-cap small{color:var(--muted);font-size:11px;margin-left:2px}
.pbs-chip:disabled{opacity:.35;cursor:not-allowed}
.pbs-capchip:empty{display:none}.pbs-capchip[data-cap='1']{color:var(--pbs-g)}
.pbs-seg{--n:2;--i:0;position:relative;display:grid;grid-template-columns:repeat(var(--n),minmax(0,1fr));padding:3px;border-radius:10px;background:rgba(0,0,0,.3);border:1px solid var(--pbs-line);isolation:isolate}
.pbs-seg-thumb{position:absolute;z-index:0;top:3px;bottom:3px;left:3px;width:calc((100% - 6px) / var(--n));transform:translateX(calc(100% * var(--i)));border-radius:8px;
  background:linear-gradient(90deg,var(--pbs-a),color-mix(in srgb,var(--pbs-a) 70%,var(--pbs-g)));box-shadow:0 0 12px rgba(var(--accent-rgb,60,232,255),.45);transition:transform .22s cubic-bezier(.3,.7,.4,1)}
.pbs-chip{position:relative;z-index:1;min-width:0;min-height:32px;padding:0 6px;border:0;background:transparent;color:var(--muted);font:700 13px Inter,sans-serif;border-radius:8px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:color .18s}
.pbs-chip:hover{color:var(--text)}.pbs-chip.on,.pbs-chip.on:hover{color:var(--bg,#0a0a0f)}.pbs-chip:focus{outline:0}
@media (prefers-reduced-motion:reduce){.pbs-seg-thumb,.pbs-chip{transition:none}.pbs-status i{animation:none!important}}
.pbs-thumb{display:block;margin-top:8px;max-width:100%;max-height:120px;border-radius:8px;border:1px solid var(--pbs-line);object-fit:contain;background:#000}
.pbs-thumb[hidden]{display:none}
.pbs-go{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;min-height:50px;border:0;border-radius:12px;cursor:pointer;
  font:900 16px/1 Orbitron,Inter,sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#001a12;
  background:linear-gradient(90deg,var(--pbs-g),var(--pbs-a));box-shadow:0 0 22px rgba(0,255,136,.35),inset 0 0 0 1px rgba(255,255,255,.25);transition:filter .15s,box-shadow .15s}
.pbs-go .ic{width:20px;height:20px}
.pbs-go:hover{filter:brightness(1.08);box-shadow:0 0 30px rgba(0,255,136,.5)}
.pbs-go:disabled{opacity:.5;cursor:not-allowed;filter:grayscale(.4)}
.pbs-go.is-live{color:#fff;background:linear-gradient(90deg,#ff2d55,#ff5a36);box-shadow:0 0 24px rgba(255,45,85,.5)}
.pbs-ext{margin-top:12px;border-top:1px solid var(--pbs-line);padding-top:10px;font-size:13px}
.pbs-ext summary{cursor:pointer;color:var(--muted);font-weight:700}.pbs-ext summary small{font-weight:500;opacity:.75;margin-left:6px}
.pbs-copy{display:flex;gap:6px;align-items:center;min-width:0}.pbs-copy .pbs-field{flex:1}
.pbs-chat{display:flex;flex-direction:column;min-height:260px;flex:1}
.pbs-chat-msgs{flex:1;min-height:160px;max-height:560px;overflow:auto;display:flex;flex-direction:column;gap:6px;padding:4px;font-size:14px;border-radius:8px;background:rgba(0,0,0,.22)}
.pbs-chat-msgs .scm{display:flex;gap:8px;align-items:flex-start}.pbs-chat-msgs .scm-av{width:24px;height:24px;border-radius:50%;object-fit:cover;flex:none}
.pbs-chat-msgs .scm-name{color:var(--pbs-a);cursor:pointer}.pbs-chat-msgs .scm-txt{overflow-wrap:anywhere}
.pbs-chat-in{display:flex;gap:6px;margin-top:8px}.pbs-chat-in .pbs-field{flex:1}
.pbs-dock{grid-area:dock;align-self:start;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:12px;min-width:0}
.pbs-layers{display:flex;flex-direction:column;gap:6px}
.pbs-layer{display:flex;align-items:center;gap:2px;border:1px solid var(--pbs-line);border-radius:9px;background:rgba(0,0,0,.2);min-width:0}
.pbs-layer.on{border-color:var(--pbs-g);box-shadow:0 0 0 1px rgba(0,255,136,.35),0 0 12px rgba(0,255,136,.15)}
.pbs-layer.off .pbs-layer-main{opacity:.45}.pbs-layer.off [data-a=vis]{opacity:.5}
.pbs-layer-main{flex:1;min-width:0;display:flex;align-items:center;gap:8px;padding:8px 8px;border:0;background:none;color:var(--text);font:600 13px Inter,sans-serif;cursor:pointer;text-align:left}
.pbs-layer-main .ic{width:16px;height:16px;color:var(--pbs-a);flex:none}
.pbs-layer-n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.pbs-tag{font-size:10.5px;color:var(--amber,#ffcf2b);border:1px solid currentColor;border-radius:5px;padding:1px 5px;white-space:nowrap;flex:none}
.pbs-ib{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;flex:none;border:0;border-radius:8px;background:transparent;color:var(--muted);cursor:pointer}
.pbs-ib .ic{width:16px;height:16px}.pbs-ib:hover{color:var(--pbs-a);background:rgba(var(--accent-rgb,60,232,255),.08)}
.pbs-ib:disabled{opacity:.35;cursor:default}.pbs-del:hover{color:var(--danger,#ff4d6d)}
.pbs-ib[aria-pressed=true].pbs-mute{color:var(--danger,#ff4d6d)}
.pbs-ph{display:flex;align-items:center;gap:8px;margin-bottom:10px;min-width:0}.pbs-ph b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.pbs-ph .ic{color:var(--pbs-g)}
.pbs-row3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.pbs-inl{display:flex;align-items:center;gap:6px;min-height:34px}
.pbs input[type=color]{width:42px;height:30px;padding:0;border:1px solid var(--pbs-line);border-radius:7px;background:none;cursor:pointer}
.pbs-acts{display:flex;gap:8px;flex-wrap:wrap}.pbs-btn{display:inline-flex;align-items:center;gap:6px}.pbs-btn .ic{width:15px;height:15px}
.pbs-mixer{display:flex;flex-direction:column;gap:12px}
.pbs-strip{border:1px solid var(--pbs-line);border-radius:10px;padding:9px 10px;background:rgba(0,0,0,.2)}
.pbs-strip-h{display:flex;align-items:center;gap:7px;font-size:13px;margin-bottom:7px}.pbs-strip-h .ic{width:15px;height:15px;color:var(--pbs-a)}
.pbs-db{margin-left:auto;font:600 12px 'JetBrains Mono',monospace;color:var(--muted)}
.pbs-meter{--rms:0%;--peak:0%;position:relative;height:12px;border-radius:4px;overflow:hidden;background:rgba(255,255,255,.06);box-shadow:inset 0 0 0 1px rgba(0,0,0,.5)}
.pbs-meter i{position:absolute;inset:0;background:linear-gradient(90deg,#00ff88 0%,#00ff88 66%,#ffe14d 72%,#ffe14d 88%,#ff3b5c 92%);
  clip-path:inset(0 calc(100% - var(--rms)) 0 0);filter:drop-shadow(0 0 4px rgba(0,255,136,.6));transition:clip-path 60ms linear}
.pbs-meter b{position:absolute;top:0;bottom:0;width:2px;left:calc(var(--peak) - 1px);background:#fff;box-shadow:0 0 6px #fff}
.pbs-meter.hot b{background:#ff3b5c;box-shadow:0 0 8px #ff3b5c}
.pbs-ticks{position:absolute;inset:0;background:repeating-linear-gradient(90deg,transparent 0 calc(10% - 1px),rgba(0,0,0,.55) calc(10% - 1px) 10%)}
.pbs-strip.muted .pbs-meter i{filter:grayscale(1) brightness(.6)}
.pbs-strip-c{display:flex;align-items:center;gap:6px;margin-top:6px}
.pbs-strip-x{margin-top:8px;display:flex;flex-direction:column;gap:6px}.pbs-dsel select{font-size:13px;padding:7px 9px}
.pbs-strip.idle .pbs-meter{opacity:.5}
.pbs-arm{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:34px;padding:0 12px;border-radius:9px;cursor:pointer;font:700 13px Inter,sans-serif;
  color:#001a12;border:0;background:linear-gradient(90deg,var(--pbs-g),var(--pbs-a));box-shadow:0 0 12px rgba(0,255,136,.3)}
.pbs-arm .ic{width:15px;height:15px}.pbs-arm:hover{filter:brightness(1.08)}.pbs-arm:focus-visible{outline:2px solid var(--pbs-a);outline-offset:2px}
.pbs-note{font-size:12.5px;color:var(--muted);margin:0}.pbs-warn{color:var(--amber,#ffcf2b);margin-bottom:10px}
.pbs-empty{font-size:13px;color:var(--muted);padding:6px 2px}
.pbs-saved{grid-area:saved;margin:0;font-size:12px;color:var(--muted);text-align:right}
@media (max-width:900px){
  .pbs{grid-template-columns:minmax(0,1fr);grid-template-areas:"head" "why" "mac" "stage" "side" "dock" "saved";padding:8px;gap:10px}
  .pbs-dock{grid-template-columns:minmax(0,1fr)}
  .pbs-word b{font-size:22px}.pbs-word span{font-size:9px;letter-spacing:.16em}
  .pbs-chat-msgs{max-height:260px}
}
@media (max-width:480px){.pbs-addbar{grid-template-columns:repeat(2,minmax(0,1fr))}.pbs-meta{gap:6px 10px;font-size:11.5px}.pbs-enc{flex-basis:100%}}
`;

    return { render, _engine: () => E };
  };
})();

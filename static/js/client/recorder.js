/* Screen Recorder -- record the screen (or one window or tab), trim it, keep it or post it.
 *
 * Asked 2026-10-28 ("Screen Recorder and clip"). getDisplayMedia + MediaRecorder, all in the page: nothing is sent
 * anywhere while recording. The microphone and the screen's own sound are optional and MIXED into one track
 * (MediaRecorder takes one audio track; two would drop one). A recording stops on Stop or when the person ends
 * the share from the browser's own bar -- both land in the same review.
 *
 * THE DURATION IS MEASURED, not read: a MediaRecorder WebM carries no duration, so <video>.duration is Infinity
 * until the whole file has been read. The recorder times itself (pauses excluded) and the trim handles use that.
 *
 * TRIM goes to this node (/client/media/clip-video: an ffmpeg cut, MP4 out, which also makes a WebM playable on
 * iOS). With no instance there is nothing to cut with, so Trim is not offered and the recording is kept whole.
 * Keep = Files -> Recordings, ENCRYPTED like the rest of the drive (a screen recording is private until posted).
 * Post = the ordinary composer with the clip attached, so compression, sensitive-content, drafts all apply.
 */
window.PCRecorderFactory = function(dep){
  const S = dep.state;   // live app.js bindings: ME, GUEST
  const { enc, toast, uploadEncFile, compose, switchView, saveBlobAs, standalone, uiConfirm } = dep;

  const st = { phase: 'idle', mic: true, sys: true, takes: [], cur: null, rec: null };
  let _tick = 0;

  const can = () => !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia && window.MediaRecorder);
  const fmt = s => { s = Math.max(0, Math.round(s || 0)); const m = Math.floor(s / 60); return m + ':' + String(s % 60).padStart(2, '0'); };
  const fmtTenth = s => { s = Math.max(0, s || 0); const m = Math.floor(s / 60); return m + ':' + (s % 60).toFixed(1).padStart(4, '0'); };
  const size = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const ic = n => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"></use></svg>`;

  /** The best container this browser records: MP4 (Chrome 126+, plays everywhere) before WebM. */
  function pickType(){
    const tries = ['video/mp4;codecs=avc1,mp4a.40.2', 'video/mp4', 'video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'];
    for(const t of tries){ try{ if(MediaRecorder.isTypeSupported(t)) return t; }catch(_){} }
    return '';
  }

  function injectCss(){
    if(document.getElementById('pc-rec-css')) return;
    const s = document.createElement('style'); s.id = 'pc-rec-css';
    s.textContent = `
.rec-wrap{padding:16px;max-width:980px;margin:0 auto;box-sizing:border-box;display:flex;flex-direction:column;gap:16px}
.rec-card{border:1px solid var(--border,rgba(255,255,255,.12));border-radius:16px;padding:20px;background:var(--panel,rgba(255,255,255,.03));
  box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--neon,#0ff) 12%,transparent)}
.rec-start{display:flex;flex-direction:column;align-items:center;gap:16px;text-align:center}
.rec-big{width:112px;height:112px;border-radius:50%;border:2px solid var(--neon2,#f0f);background:radial-gradient(circle at 50% 50%,#ff2d6f 0 38%,transparent 40%),rgba(0,0,0,.35);
  cursor:pointer;box-shadow:0 0 22px color-mix(in srgb,var(--neon2,#f0f) 60%,transparent);transition:transform .15s}
.rec-big:hover,.rec-big:focus-visible{transform:scale(1.05);outline:none;box-shadow:0 0 34px var(--neon2,#f0f)}
.rec-opts{display:flex;gap:18px;flex-wrap:wrap;justify-content:center}
.rec-opts label{display:inline-flex;align-items:center;gap:8px;cursor:pointer;min-height:36px}
.rec-live{display:flex;align-items:center;gap:16px;flex-wrap:wrap;justify-content:center}
.rec-dot{width:14px;height:14px;border-radius:50%;background:#ff2d6f;box-shadow:0 0 12px #ff2d6f;animation:recPulse 1s infinite}
.rec-live.paused .rec-dot{animation:none;background:#888;box-shadow:none}
@keyframes recPulse{50%{opacity:.25}}
.rec-time{font:700 34px/1 'JetBrains Mono',ui-monospace,monospace;color:var(--neon,#0ff);text-shadow:0 0 12px var(--neon,#0ff);min-width:5ch;text-align:center}
.rec-review video{width:100%;max-height:56vh;background:#000;border-radius:12px;display:block}
.rec-trim{display:grid;grid-template-columns:auto 1fr auto;gap:8px 12px;align-items:center;margin-top:14px}
.rec-trim input[type=range]{width:100%;accent-color:var(--neon,#0ff)}
.rec-trim .mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:13px;min-width:6ch;text-align:right}
.rec-acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.rec-acts .btn{display:inline-flex;align-items:center;gap:6px}
.rec-meta{margin-top:8px}
.rec-takes{display:flex;flex-direction:column;gap:6px}
.rec-take{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--border,rgba(255,255,255,.1));border-radius:10px;background:none;color:inherit;cursor:pointer;text-align:left;font:inherit}
.rec-take:hover,.rec-take.on{border-color:var(--neon,#0ff)}
.rec-take b{flex:1}
@media(max-width:640px){.rec-wrap{padding:10px}.rec-card{padding:14px}.rec-trim{grid-template-columns:1fr auto}.rec-trim .rec-lbl{grid-column:1/-1}.rec-acts .btn{flex:1 1 45%}}`;
    document.head.appendChild(s);
  }

  function feed(){ return document.getElementById('feed'); }

  function render(){
    const f = feed(); if(!f) return;
    injectCss();
    if(!can()){
      f.innerHTML = `<div class="rec-wrap"><div class="rec-card rec-start"><h3>Screen recording is not available here</h3>
        <p class="muted">This browser cannot capture the screen — phones and tablets do not allow it. Open PosterChan on a computer (the web app, the desktop app or PosterChanOS).</p></div></div>`;
      return;
    }
    const takes = st.takes.length ? `<div class="rec-card"><h4 style="margin:0 0 10px">This session</h4><div class="rec-takes">${st.takes.map((t, i) =>
      `<button type="button" class="rec-take${st.cur === t ? ' on' : ''}" data-i="${i}">${ic('film')}<b>${enc(t.name)}</b><span class="muted small">${fmt(t.dur)} · ${size(t.blob.size)}</span></button>`).join('')}</div></div>` : '';
    let main;
    if(st.phase === 'recording' || st.phase === 'paused'){
      main = `<div class="rec-card rec-start"><div class="rec-live${st.phase === 'paused' ? ' paused' : ''}">
          <span class="rec-dot"></span><span class="rec-time">${fmt(elapsed())}</span></div>
        <div class="muted small">${st.phase === 'paused' ? 'Paused' : 'Recording'}${st.rec && st.rec.audio ? ' with sound' : ' — no sound'}</div>
        <div class="rec-acts"><button type="button" class="btn btn-ghost rec-pause">${ic(st.phase === 'paused' ? 'play' : 'pause')}${st.phase === 'paused' ? 'Resume' : 'Pause'}</button>
          <button type="button" class="btn btn-neon rec-stop">${ic('stop')}Stop</button></div></div>`;
    } else if(st.cur){
      main = reviewHtml(st.cur);
    } else {
      main = `<div class="rec-card rec-start">
        <button type="button" class="rec-big" aria-label="Start recording" title="Start recording"></button>
        <div><b>Record your screen</b><div class="muted small">Pick the whole screen, one window or a browser tab. Nothing leaves this device until you post or save it.</div></div>
        <div class="rec-opts"><label><input type="checkbox" class="rec-mic"${st.mic ? ' checked' : ''}> Microphone</label>
          <label><input type="checkbox" class="rec-sys"${st.sys ? ' checked' : ''}> Sound from the screen</label></div></div>`;
    }
    f.innerHTML = `<div class="rec-wrap">${main}${takes}</div>`;
    bind(f);
  }

  function reviewHtml(t){
    const trim = !standalone();
    return `<div class="rec-card rec-review">
      <video class="rec-video" controls playsinline preload="metadata" src="${t.url}"></video>
      <div class="rec-meta muted small">${enc(t.name)} · ${fmt(t.dur)} · ${size(t.blob.size)} · ${enc((t.type || '').split(';')[0] || 'video')}</div>
      ${trim ? `<div class="rec-trim">
        <span class="rec-lbl small">Start</span><input type="range" class="rec-a" min="0" max="${t.dur}" step="0.1" value="${t.a}" aria-label="Trim start"><span class="mono rec-av">${fmtTenth(t.a)}</span>
        <span class="rec-lbl small">End</span><input type="range" class="rec-b" min="0" max="${t.dur}" step="0.1" value="${t.b}" aria-label="Trim end"><span class="mono rec-bv">${fmtTenth(t.b)}</span>
      </div>
      <div class="rec-acts"><button type="button" class="btn btn-ghost rec-seta">Start here</button><button type="button" class="btn btn-ghost rec-setb">End here</button></div>` : ''}
      <div class="rec-acts">
        <button type="button" class="btn btn-neon rec-post">${ic('send')}Post</button>
        <button type="button" class="btn btn-ghost rec-keep">${ic('folder')}Save to Files</button>
        <button type="button" class="btn btn-ghost rec-dl">${ic('download')}Download</button>
        <button type="button" class="btn btn-ghost rec-new">${ic('plus')}New recording</button>
        <button type="button" class="btn btn-danger rec-drop">${ic('trash')}Discard</button>
      </div><div class="rec-st muted small"></div></div>`;
  }

  function bind(f){
    const q = s => f.querySelector(s);
    const big = q('.rec-big'); if(big) big.onclick = start;
    const mic = q('.rec-mic'); if(mic) mic.onchange = () => { st.mic = mic.checked; };
    const sys = q('.rec-sys'); if(sys) sys.onchange = () => { st.sys = sys.checked; };
    const ps = q('.rec-pause'); if(ps) ps.onclick = pauseResume;
    const sp = q('.rec-stop'); if(sp) sp.onclick = stop;
    f.querySelectorAll('.rec-take').forEach(b => b.onclick = () => { if(st.phase !== 'idle') return; st.cur = st.takes[+b.dataset.i]; render(); });
    const t = st.cur; if(!t || st.phase !== 'idle') return;
    const v = q('.rec-video'), a = q('.rec-a'), b = q('.rec-b');
    const show = () => { const av = q('.rec-av'), bv = q('.rec-bv'); if(av) av.textContent = fmtTenth(t.a); if(bv) bv.textContent = fmtTenth(t.b); };
    if(a) a.oninput = () => { t.a = Math.min(+a.value, t.b - 0.5); a.value = t.a; show(); try{ v.currentTime = t.a; }catch(_){} };
    if(b) b.oninput = () => { t.b = Math.max(+b.value, t.a + 0.5); b.value = t.b; show(); try{ v.currentTime = t.b; }catch(_){} };
    const sa = q('.rec-seta'); if(sa) sa.onclick = () => { t.a = Math.min(v.currentTime || 0, t.b - 0.5); a.value = t.a; show(); };
    const sb = q('.rec-setb'); if(sb) sb.onclick = () => { t.b = Math.max(Math.min(v.currentTime || t.dur, t.dur), t.a + 0.5); b.value = t.b; show(); };
    q('.rec-post').onclick = () => finish('post');
    q('.rec-keep').onclick = () => finish('keep');
    q('.rec-dl').onclick = () => finish('download');
    q('.rec-new').onclick = () => { st.cur = null; render(); };
    q('.rec-drop').onclick = async () => {
      if(!await uiConfirm('Discard this recording? It was never saved anywhere.', { ok: 'Discard', danger: true })) return;
      try{ URL.revokeObjectURL(t.url); }catch(_){}
      st.takes = st.takes.filter(x => x !== t); st.cur = null; render();
    };
  }

  function elapsed(){
    const r = st.rec; if(!r) return 0;
    return (r.acc + (r.runFrom ? (performance.now() - r.runFrom) : 0)) / 1000;
  }

  async function start(){
    if(st.phase !== 'idle') return;
    let screen, mic = null;
    try{
      screen = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: !!st.sys });
    }catch(e){
      if(e && e.name === 'NotAllowedError') toast('Recording cancelled');
      else toast('Could not capture the screen: ' + ((e && e.message) || e));
      return;
    }
    if(st.mic){
      try{ mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
      catch(_){ toast('No microphone — recording without it'); }
    }
    const tracks = [...screen.getVideoTracks()];
    const auds = [...screen.getAudioTracks(), ...(mic ? mic.getAudioTracks() : [])];
    let mixCtx = null;
    if(auds.length === 1) tracks.push(auds[0]);
    else if(auds.length > 1){
      // MediaRecorder records ONE audio track; two would silently lose one. Mix them.
      try{
        mixCtx = new AudioContext(); const dst = mixCtx.createMediaStreamDestination();
        for(const tr of auds) mixCtx.createMediaStreamSource(new MediaStream([tr])).connect(dst);
        tracks.push(dst.stream.getAudioTracks()[0]);
      }catch(_){ tracks.push(auds[0]); }
    }
    const type = pickType();
    let mr;
    try{ mr = new MediaRecorder(new MediaStream(tracks), type ? { mimeType: type, videoBitsPerSecond: 6e6 } : undefined); }
    catch(e){ [screen, mic].forEach(s => s && s.getTracks().forEach(t => t.stop())); toast('This browser cannot record: ' + ((e && e.message) || e)); return; }
    const chunks = [];
    const rec = st.rec = { mr, chunks, acc: 0, runFrom: performance.now(), audio: auds.length > 0, streams: [screen, mic], mixCtx, type: mr.mimeType || type };
    mr.ondataavailable = e => { if(e.data && e.data.size) chunks.push(e.data); };
    mr.onstop = () => done(rec);
    // Ending the share from the browser's own "Stop sharing" bar is a Stop, not a lost recording.
    screen.getVideoTracks().forEach(t => t.addEventListener('ended', () => { if(st.rec === rec && mr.state !== 'inactive') stop(); }));
    mr.start(1000);
    st.phase = 'recording'; st.cur = null;
    render();
    clearInterval(_tick);
    _tick = setInterval(() => { const el = document.querySelector('.rec-time'); if(el) el.textContent = fmt(elapsed()); }, 250);
  }

  function pauseResume(){
    const r = st.rec; if(!r) return;
    if(st.phase === 'recording'){ try{ r.mr.pause(); }catch(_){ return; } r.acc += performance.now() - r.runFrom; r.runFrom = 0; st.phase = 'paused'; }
    else if(st.phase === 'paused'){ try{ r.mr.resume(); }catch(_){ return; } r.runFrom = performance.now(); st.phase = 'recording'; }
    render();
  }

  function stop(){
    const r = st.rec; if(!r) return;
    if(r.runFrom){ r.acc += performance.now() - r.runFrom; r.runFrom = 0; }
    try{ if(r.mr.state !== 'inactive') r.mr.stop(); }catch(_){ done(r); }
  }

  function done(r){
    if(r.finished) return; r.finished = true;
    clearInterval(_tick);
    r.streams.forEach(s => s && s.getTracks().forEach(t => { try{ t.stop(); }catch(_){} }));
    try{ r.mixCtx && r.mixCtx.close(); }catch(_){}
    st.rec = null; st.phase = 'idle';
    const blob = new Blob(r.chunks, { type: (r.type || 'video/webm').split(';')[0] });
    if(!blob.size){ toast('Nothing was recorded'); render(); return; }
    const dur = Math.max(0.1, Math.round((r.acc / 1000) * 10) / 10);
    const ext = /mp4/.test(blob.type) ? 'mp4' : 'webm';
    const d = new Date(), p = n => String(n).padStart(2, '0');
    const name = `Screen recording ${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}.${p(d.getMinutes())}.${p(d.getSeconds())}.${ext}`;
    const t = { blob, url: URL.createObjectURL(blob), dur, a: 0, b: dur, name, type: blob.type };
    st.takes.unshift(t); st.cur = t;
    render();
  }

  /** The take as it should leave: trimmed (or converted to MP4 for a post) by this node when that is asked for. */
  async function produce(t, mp4){
    const trimmed = t.a > 0.05 || t.b < t.dur - 0.05;
    const toMp4 = mp4 && !/mp4/.test(t.blob.type);
    if(standalone() || (!trimmed && !toMp4)) return new File([t.blob], t.name, { type: t.blob.type });
    const fd = new FormData();
    fd.append('file', t.blob, t.name);
    fd.append('start', String(trimmed ? t.a : 0));
    if(trimmed && t.b < t.dur - 0.05) fd.append('end', String(t.b));
    const r = await fetch('/client/media/clip-video', { method: 'POST', body: fd, credentials: 'same-origin' });
    // Only a VIDEO answer is a trim. A proxy's error page or an older node's catch-all JSON would otherwise be
    // saved or posted as a "clip" of a few bytes.
    if(r.ok && !/^video\//.test(r.headers.get('content-type') || '')){
      if(!trimmed) return new File([t.blob], t.name, { type: t.blob.type });
      throw new Error('this server cannot trim videos');
    }
    if(!r.ok){
      let msg = 'the server could not trim it';
      try{ msg = (await r.json()).error || msg; }catch(_){}
      if(!trimmed) return new File([t.blob], t.name, { type: t.blob.type });   // conversion is a nicety; the original still posts
      throw new Error(msg);
    }
    const out = await r.blob();
    return new File([out], t.name.replace(/\.(webm|mp4)$/i, '') + (trimmed ? ' (trimmed)' : '') + '.mp4', { type: 'video/mp4' });
  }

  async function finish(how){
    const t = st.cur; if(!t) return;
    const f = feed(), stl = f && f.querySelector('.rec-st');
    const btns = f ? [...f.querySelectorAll('.rec-review .btn')] : [];
    btns.forEach(b => b.disabled = true);
    try{
      if(stl) stl.textContent = (t.a > 0.05 || t.b < t.dur - 0.05) ? 'Trimming…' : (how === 'post' && !/mp4/.test(t.blob.type) && !standalone() ? 'Converting to MP4…' : '');
      const file = await produce(t, how === 'post');
      if(how === 'download'){ await saveBlobAs(file, file.name); if(stl) stl.textContent = ''; }
      else if(how === 'keep'){
        if(!(S.ME && S.ME.pubkey) || S.GUEST) throw new Error('sign in to save to Files');
        if(stl) stl.textContent = 'Saving to Files…';
        await uploadEncFile(file, 'Recordings');
        if(stl) stl.textContent = 'Saved to Files → Recordings (encrypted — only you can open it).';
        toast('saved to Files → Recordings');
      } else {
        if(stl) stl.textContent = '';
        switchView('home'); compose({ files: [file] });
      }
    }catch(e){ if(stl) stl.textContent = ''; toast(String((e && e.message) || e)); }
    finally{ btns.forEach(b => { if(b.isConnected) b.disabled = false; }); }
  }

  /** Leaving the screen mid-recording keeps it recording (a browser tab can be filmed while you work in it). */
  return { render, start, stop, isRecording: () => st.phase !== 'idle', pickType };
};

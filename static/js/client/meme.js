/* Meme Builder — a small layered video editor inside the client.
 *
 * Shape: a PREVIEW stage you drag/resize layers on, a LAYERS list, and a TIMELINE where each layer is a
 * bar you slide (start) and stretch (duration). Editing is entirely local and instant; the only server
 * call is the final render (POST /client/meme/render), which composites the edit list with ffmpeg —
 * same reason every other media feature here renders server-side: it works identically on a phone.
 *
 * Layer sources are URLs. Local files are uploaded to Blossom first (uploadBlob), so the render service
 * only ever fetches things that already exist — no new upload surface, and the same blob can be reused
 * across layers without re-uploading.
 *
 * Mobile: the stage/timeline split stacks under 820px, drag and resize are pointer-events (so touch,
 * pen and mouse are one code path), and the timeline scrolls horizontally rather than squeezing. No
 * native confirm/prompt/alert anywhere — they wedge the Electron renderer's focus (see uiConfirm).
 */
(function(){
  'use strict';

  // Same contract every sub-module uses (see stats.js/news.js): wait for app.js to publish the bridge,
  // then take our helpers off it rather than off bare globals — app.js is an IIFE, so `toast`,
  // `uploadBlob` and friends are NOT global.
  let PC = null, toast, uploadBlob, selfProof, uiConfirm, ME;
  function boot(){
    PC = window.__PC;
    if(!PC) return setTimeout(boot, 50);
    ({ toast, uploadBlob, selfProof, uiConfirm } = PC);
    window.PCMeme = {
      render(){ ME = PC.ME; P = load(); render(); },
      unmount(){ if(_playT){ clearInterval(_playT); _playT=null; } },
      reset(){ P = blank(); sel=null; save(); render(); },
    };
  }

  const FX = [
    ['none','None'], ['fade','Fade in/out'], ['zoom','Ken Burns zoom'], ['shake','Shake'],
    ['pulse','Pulse'], ['spin','Spin'], ['glow','Glow'], ['blur','Blur'],
    ['grayscale','Grayscale'], ['sepia','Sepia'], ['invert','Invert'], ['flip','Mirror'],
  ];
  const PRESETS = [
    ['9:16', 720, 1280], ['1:1', 1080, 1080], ['16:9', 1280, 720], ['4:5', 864, 1080],
  ];

  let P = null;          // the project (edit list)
  let sel = null;        // selected layer id
  let _rendering = false;// a render is in flight — survives view repaints, unlike the button's disabled flag
  let _uid = 0;
  const nid = () => 'L' + (++_uid) + Math.random().toString(36).slice(2, 6);

  function blank(){
    return { w:720, h:1280, fps:30, bg:'#000000', duration:6, layers:[] };
  }
  // Persist across view switches / reloads so a half-built meme is not lost by tapping Home.
  // A silent failure here USED to mean the build quietly stopped persisting (localStorage full, or blocked in
  // private mode) and came back blank on the next load with no explanation. Say so ONCE, and point at the
  // Blossom save — which is the only copy that survives this browser anyway.
  let _saveWarned = false;
  function save(){
    try{ localStorage.setItem('pc_meme_project', JSON.stringify(P)); _saveWarned = false; }
    catch(err){
      if(!_saveWarned){ _saveWarned = true;
        try{ toast('⚠ can’t auto-save this build in the browser (storage full/blocked) — use 💾 Save to keep it'); }catch(_){ }
      }
    }
  }
  function load(){
    try{ const r=JSON.parse(localStorage.getItem('pc_meme_project')||'null');
      if(r && Array.isArray(r.layers)) return r; }catch(_){ }
    return blank();
  }

  const clamp = (v,lo,hi) => Math.max(lo, Math.min(hi, Number(v)||0));
  const projEnd = () => P.layers.reduce((m,l)=>Math.max(m, (+l.start||0)+(+l.dur||0)), 0) || P.duration;

  // ---------- the MASTER TIMELINE ----------
  // Media clips (image/video) form ONE ordered sequence that plays back-to-back — the way every video editor
  // works. Their `start` is DERIVED from that order, never hand-typed: drop a clip anywhere on the timeline
  // and the rest reflow around it (no gaps, no accidental overlaps). TEXT is excluded on purpose — a caption
  // is an overlay pinned ON the footage, so it keeps its own free start/duration.
  const mediaSeq = () => P.layers.filter(l=>l.type!=='text')
    .sort((a,b)=>((+a.start||0)-(+b.start||0)) || (P.layers.indexOf(a)-P.layers.indexOf(b)));
  function resequence(seq){ let t=0; (seq||mediaSeq()).forEach(l=>{ l.start=+t.toFixed(2); t+=(+l.dur||0); }); }
  // Where a clip dragged to `center` (seconds, its midpoint) belongs in the sequence of the OTHERS.
  function dropIndex(others, center){
    let idx=0, acc=0;
    for(const o of others){ const w=+o.dur||0; if(center > acc + w/2) idx++; acc+=w; }
    return Math.min(idx, others.length);
  }

  function addLayer(type, src, extra){
    if(P.layers.length >= 24){ toast('24 layers is the limit'); return null; }
    // APPEND to the end of the timeline, do not stack at t=0. Defaulting every new layer to start:0 made
    // a second image land exactly on top of the first — the timeline read as "everything overlaps", which
    // is not how a video editor behaves. Media appends after the last clip so drops play in sequence;
    // TEXT still starts at 0, because a caption is an overlay ON the footage, not another clip after it.
    const tail = type==='text' ? 0 : projEnd();
    const l = Object.assign({
      id: nid(), type, src: src||'', name: '',
      start: tail, dur: type==='text' ? 3 : 4, trim: 0,
      x: 0, y: 0, w: type==='text' ? 0 : P.w, h: type==='text' ? 0 : Math.round(P.h/2),
      opacity: 1, effect: 'none', volume: 1, mute: false,
      text: type==='text' ? 'top text' : '', size: 64, color:'#ffffff', stroke:'#000000',
    }, extra||{});
    if(type!=='text'){ l.y = Math.round((P.h - l.h)/2); }
    else { l.x = Math.round(P.w*0.08); l.y = Math.round(P.h*0.08); }
    P.layers.push(l); sel = l.id;
    if(type!=='text') resequence();   // join the master timeline exactly back-to-back (no drift from `tail`)
    save(); return l;
  }

  // ---------- rendering the UI ----------
  function view(){
    return `
    <div class="mb-wrap">
      <div class="mb-bar">
        <button class="btn btn-neon small" id="mb-add-media">🖼️ Add media</button>
        <button class="btn btn-cyan small" id="mb-add-text">🅣 Add text</button>
        <button class="btn btn-cyan small" id="mb-add-blossom">🌸 From Blossom</button>
        <button class="btn btn-cyan small" id="mb-save">💾 Save</button>
        <button class="btn btn-cyan small" id="mb-open">📂 Open</button>
        <select class="input mb-size" id="mb-size" aria-label="Canvas size">
          ${PRESETS.map(([n,w,h])=>`<option value="${w}x${h}" ${P.w===w&&P.h===h?'selected':''}>${n}</option>`).join('')}
        </select>
        <span class="mb-spacer"></span>
        <span class="muted small" id="mb-status"></span>
        <button class="btn btn-neon small" id="mb-render">🎬 Render</button>
      </div>

      <div class="mb-main">
        <div class="mb-stagewrap">
          <div class="mb-stage" id="mb-stage" style="aspect-ratio:${P.w}/${P.h};background:${P.bg}">
            ${P.layers.map(stageEl).join('')}
          </div>
          <div class="mb-playrow">
            <button class="btn btn-ghost small" id="mb-play">▶︎</button>
            <input type="range" id="mb-scrub" class="mb-scrub" min="0" max="${projEnd().toFixed(2)}" step="0.05" value="0">
            <span class="muted small" id="mb-time">0.0s / ${projEnd().toFixed(1)}s</span>
          </div>
        </div>

        <div class="mb-side">
          <div class="mb-inspector" id="mb-inspector">${inspector()}</div>
        </div>
      </div>

      ${P.layers.some(l=>l.type!=='text') ? '<div class="muted small mb-tlhint">Drag a clip along the timeline to reorder it — the rest reflow back-to-back. Drag its edges to trim.</div>' : ''}
      <div class="mb-timeline" id="mb-timeline">
        ${P.layers.length ? P.layers.map(trackEl).join('') : '<div class="muted small mb-empty">No layers yet — add media or text to start.</div>'}
      </div>
      <div id="mb-result"></div>
    </div>`;
  }

  function stageEl(l){
    const s = l.id===sel ? ' sel' : '';
    const pos = `left:${(l.x/P.w*100).toFixed(3)}%;top:${(l.y/P.h*100).toFixed(3)}%;`;
    if(l.type==='text'){
      return `<div class="mb-item mb-text${s}" data-id="${l.id}" style="${pos}font-size:${(l.size/P.h*100).toFixed(2)}cqh;color:${enc(l.color)};-webkit-text-stroke:.03em ${enc(l.stroke)};opacity:${l.opacity}">
        ${enc(l.text||' ')}<i class="mb-h"></i></div>`;
    }
    const size = `width:${(l.w/P.w*100).toFixed(3)}%;height:${(l.h/P.h*100).toFixed(3)}%;`;
    const inner = l.type==='video'
      ? `<video src="${enc(l.src)}" muted playsinline preload="metadata"></video>`
      : `<img src="${enc(l.src)}" alt="">`;
    return `<div class="mb-item mb-media${s}" data-id="${l.id}" style="${pos}${size}opacity:${l.opacity}">${inner}<i class="mb-h"></i></div>`;
  }

  function trackEl(l){
    const total = Math.max(projEnd(), 1);
    const left = (l.start/total*100), wid = Math.max(3, l.dur/total*100);
    const label = l.type==='text' ? ('🅣 ' + (l.text||'text')) : (l.type==='video'?'🎬 ':'🖼️ ') + (l.name || srcName(l.src));
    return `<div class="mb-track${l.id===sel?' sel':''}" data-id="${l.id}">
      <div class="mb-trackname">${enc(label.slice(0,28))}</div>
      <div class="mb-lane">
        <div class="mb-clip" data-id="${l.id}" style="left:${left.toFixed(3)}%;width:${wid.toFixed(3)}%">
          <i class="mb-grip mb-grip-l" data-grip="l"></i>
          <span>${l.dur.toFixed(1)}s</span>
          <i class="mb-grip mb-grip-r" data-grip="r"></i>
        </div>
      </div>
    </div>`;
  }

  function inspector(){
    const l = P.layers.find(x=>x.id===sel);
    if(!l) return `<div class="muted small">Select a layer to edit it.</div>`;
    const isText = l.type==='text';
    return `
      <div class="mb-insp-hd">
        <b>${isText?'Text':(l.type==='video'?'Video':'Image')} layer</b>
        <button class="btn btn-danger small" id="mb-del">🗑️ Delete</button>
      </div>
      ${isText ? `
        <label class="mb-f"><span>Text</span><textarea class="input" id="mb-f-text" rows="2">${enc(l.text)}</textarea></label>
        <label class="mb-f"><span>Size</span><input class="input" type="number" id="mb-f-size" min="8" max="400" value="${l.size}"></label>
        <div class="mb-frow">
          <label class="mb-f"><span>Colour</span><input type="color" id="mb-f-color" value="${enc(l.color)}"></label>
          <label class="mb-f"><span>Outline</span><input type="color" id="mb-f-stroke" value="${enc(l.stroke)}"></label>
        </div>` : `
        <div class="mb-frow">
          <label class="mb-f"><span>W</span><input class="input" type="number" id="mb-f-w" value="${Math.round(l.w)}"></label>
          <label class="mb-f"><span>H</span><input class="input" type="number" id="mb-f-h" value="${Math.round(l.h)}"></label>
        </div>
        <button class="btn btn-cyan small full" id="mb-fill">⛶ Fill the canvas</button>
        ${l.type==='video' ? `<label class="mb-f"><span>Trim from (s)</span><input class="input" type="number" id="mb-f-trim" min="0" step="0.1" value="${l.trim}"></label>
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-mute" ${l.mute?'checked':''}><span>Mute this clip</span></label>` : ''}`}
      <div class="mb-frow">
        <label class="mb-f"><span>Start (s)</span><input class="input" type="number" id="mb-f-start" min="0" step="0.1" value="${l.start}"></label>
        <label class="mb-f"><span>Length (s)</span><input class="input" type="number" id="mb-f-dur" min="0.1" step="0.1" value="${l.dur}"></label>
      </div>
      <label class="mb-f"><span>Effect</span><select class="input" id="mb-f-fx">
        ${FX.map(([v,n])=>`<option value="${v}" ${l.effect===v?'selected':''}>${n}</option>`).join('')}
      </select></label>
      <label class="mb-f"><span>Opacity</span><input type="range" id="mb-f-op" min="0.05" max="1" step="0.05" value="${l.opacity}"></label>
      <div class="mb-order">
        <button class="btn btn-cyan small" id="mb-back">⬇︎ Send back</button>
        <button class="btn btn-cyan small" id="mb-front">⬆︎ Bring front</button>
      </div>`;
  }

  const srcName = (u) => { try{ return decodeURIComponent(String(u).split('/').pop()).slice(0,18) || 'clip'; }catch(_){ return 'clip'; } };
  function enc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }


  // ---------- Blossom ----------
  // Your drive, as a source for layers. Media already on Blossom needs no upload and no re-encode — the
  // render service fetches the same URL — so picking from here is both faster and cheaper than re-adding
  // a file you already own.
  async function listBlobs(){
    const server = PC.mediaServer && PC.mediaServer();
    if(!server) throw new Error('no Blossom server configured');
    const r = await fetch(server + '/list/' + PC.ME.pubkey);
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }
  const blobUrl = (b) => b.url || ((PC.mediaServer && PC.mediaServer()) + '/' + b.sha256);

  async function pickBlossom(){
    let list = [];
    try{ list = await listBlobs(); }
    catch(err){ toast("couldn't read your Blossom drive: " + ((err&&err.message)||err)); return; }
    const media = list.filter(b => /^(image|video)\//.test(b.type||''));
    if(!media.length){ toast('no images or videos on your Blossom drive yet'); return; }
    const cell = (b) => {
      const u = blobUrl(b), v = /^video\//.test(b.type||'');
      return `<button class="mb-pick" data-u="${enc(u)}" data-v="${v?1:0}" title="${enc(b.type||'')}">`
        + (v ? `<video src="${enc(u)}" muted preload="metadata"></video><i class="mb-pickv">▶︎</i>`
             : `<img src="${enc(u)}" alt="" loading="lazy">`) + `</button>`;
    };
    PC.modal('<h3>🌸 Add from Blossom</h3><div class="mb-picker">' + media.slice(0,200).map(cell).join('') + '</div>', root=>{
      root.querySelectorAll('.mb-pick').forEach(b => b.onclick = () => {
        PC.closeModal();
        addLayer(b.dataset.v === '1' ? 'video' : 'image', b.dataset.u);
        render();
      });
    });
  }

  // Projects live on Blossom as a small JSON blob, so a build survives clearing this browser AND opens on
  // your other devices — localStorage alone is per-device and one "clear site data" from losing the lot.
  const PROJ_MARK = 'pcmeme-project';
  async function saveProject(){
    try{
      const doc = JSON.stringify(Object.assign({ [PROJ_MARK]: 1, savedAt: Math.floor(Date.now()/1000) }, P));
      const f = new File([doc], 'meme-project.json', { type: 'application/json' });
      const url = await uploadBlob(f);
      toast('project saved to Blossom');
      return url;
    }catch(err){ toast('save failed: ' + ((err&&err.message)||err)); }
  }
  async function openProject(){
    let list = [];
    try{ list = await listBlobs(); }
    catch(err){ toast("couldn't read your Blossom drive: " + ((err&&err.message)||err)); return; }
    // Blossom stores blobs by hash and does not preserve filenames, so identify projects by content:
    // JSON blobs are small, fetch the candidates and keep the ones carrying our marker.
    const cands = list.filter(b => /json/.test(b.type||'') && (b.size||0) < 400000).slice(0, 40);
    const found = [];
    for(const b of cands){
      try{
        const j = await fetch(blobUrl(b)).then(r=>r.json());
        if(j && j[PROJ_MARK] && Array.isArray(j.layers)) found.push({ b, j });
      }catch(_){ }
    }
    if(!found.length){ toast('no saved projects found on Blossom'); return; }
    found.sort((x,y)=>(y.j.savedAt||0)-(x.j.savedAt||0));
    const row = (f,i) => `<button class="btn btn-ghost full mb-projrow" data-i="${i}">`
      + `${f.j.layers.length} layer${f.j.layers.length===1?'':'s'} · ${f.j.w}×${f.j.h}`
      + (f.j.savedAt ? ' · ' + new Date(f.j.savedAt*1000).toLocaleString() : '') + '</button>';
    PC.modal('<h3>📂 Open a saved project</h3>' + found.map(row).join(''), root=>{
      root.querySelectorAll('.mb-projrow').forEach(btn => btn.onclick = async () => {
        const f = found[+btn.dataset.i];
        PC.closeModal();
        if(P.layers.length && !await uiConfirm('Replace the project you are working on?')) return;
        P = f.j; delete P[PROJ_MARK]; sel = null; save(); render();
      });
    });
  }

  // ---------- interaction ----------
  function repaint(what){
    const root = document.getElementById('feed'); if(!root) return;
    if(what==='inspector'){ const i=document.getElementById('mb-inspector'); if(i){ i.innerHTML=inspector(); bindInspector(root); } return; }
    if(what==='timeline'){ const t=document.getElementById('mb-timeline'); if(t){ t.innerHTML=P.layers.length?P.layers.map(trackEl).join(''):'<div class="muted small mb-empty">No layers yet — add media or text to start.</div>'; } return; }
    render();
  }

  function selectLayer(id){
    sel = id;
    document.querySelectorAll('.mb-item,.mb-track').forEach(e=>e.classList.toggle('sel', e.dataset.id===id));
    repaint('inspector');
  }

  // ONE pointer-events drag implementation for stage move/resize AND timeline slide/stretch. Pointer
  // events unify mouse/touch/pen, and setPointerCapture means a fast drag that leaves the element does
  // not strand the gesture (the bug you get with mousemove-on-element).
  function drag(e, onMove, onDone){
    const id = e.pointerId, t = e.currentTarget;
    try{ t.setPointerCapture(id); }catch(_){ }
    const move = (ev)=>{ if(ev.pointerId!==id) return; ev.preventDefault(); onMove(ev); };
    const up = (ev)=>{ if(ev.pointerId!==id) return;
      t.removeEventListener('pointermove', move); t.removeEventListener('pointerup', up);
      t.removeEventListener('pointercancel', up);
      try{ t.releasePointerCapture(id); }catch(_){ }
      if(onDone) onDone(); };
    t.addEventListener('pointermove', move); t.addEventListener('pointerup', up);
    t.addEventListener('pointercancel', up);
  }

  function bindStage(root){
    const stage = root.querySelector('#mb-stage'); if(!stage) return;
    stage.addEventListener('pointerdown', (e)=>{
      const item = e.target.closest('.mb-item'); if(!item) return;
      const l = P.layers.find(x=>x.id===item.dataset.id); if(!l) return;
      selectLayer(l.id);
      const rect = stage.getBoundingClientRect();
      const pxW = P.w/rect.width, pxH = P.h/rect.height;      // screen px -> project px
      const resizing = !!e.target.closest('.mb-h');
      const sx=e.clientX, sy=e.clientY, ox=l.x, oy=l.y, ow=l.w, oh=l.h, osz=l.size;
      e.preventDefault();
      drag(e, (ev)=>{
        const dx=(ev.clientX-sx)*pxW, dy=(ev.clientY-sy)*pxH;
        if(resizing){
          if(l.type==='text') l.size = clamp(osz + dy, 8, 400);
          else { l.w = clamp(ow+dx, 16, P.w*2); l.h = clamp(oh+dy, 16, P.h*2); }
        } else { l.x = Math.round(ox+dx); l.y = Math.round(oy+dy); }
        applyGeom(item, l);
      }, ()=>{ save(); repaint('inspector'); });
    });
  }

  function applyGeom(el, l){
    el.style.left = (l.x/P.w*100).toFixed(3)+'%';
    el.style.top  = (l.y/P.h*100).toFixed(3)+'%';
    if(l.type==='text') el.style.fontSize = (l.size/P.h*100).toFixed(2)+'cqh';
    else { el.style.width=(l.w/P.w*100).toFixed(3)+'%'; el.style.height=(l.h/P.h*100).toFixed(3)+'%'; }
  }

  function bindTimeline(root){
    const tl = root.querySelector('#mb-timeline'); if(!tl) return;
    tl.addEventListener('pointerdown', (e)=>{
      const clip = e.target.closest('.mb-clip'); if(!clip) return;
      const l = P.layers.find(x=>x.id===clip.dataset.id); if(!l) return;
      selectLayer(l.id);
      const lane = clip.parentElement, rect = lane.getBoundingClientRect();
      const total = Math.max(projEnd(), 1), perPx = total/rect.width;
      const grip = e.target.closest('.mb-grip'), side = grip && grip.dataset.grip;
      const sx=e.clientX, ost=l.start, odur=l.dur;
      e.preventDefault();
      drag(e, (ev)=>{
        const d=(ev.clientX-sx)*perPx;
        if(side==='l'){ const ns=clamp(ost+d, 0, ost+odur-0.2); l.dur=+(odur+(ost-ns)).toFixed(2); l.start=+ns.toFixed(2); }
        else if(side==='r'){ l.dur=+clamp(odur+d, 0.2, 120).toFixed(2); }
        else { l.start=+clamp(ost+d, 0, 120).toFixed(2); }
        clip.style.left=(l.start/total*100).toFixed(3)+'%';
        clip.style.width=Math.max(3, l.dur/total*100).toFixed(3)+'%';
        const sp=clip.querySelector('span'); if(sp) sp.textContent=l.dur.toFixed(1)+'s';
      }, ()=>{
        // Drop = commit to the MASTER TIMELINE. A media clip dragged anywhere REORDERS the sequence (it lands
        // where you dropped it and everything reflows back-to-back); a trim just closes the gap it left. Text
        // overlays keep the free position they were dragged to.
        if(l.type!=='text'){
          if(side) resequence();
          else {
            const others = mediaSeq().filter(x=>x!==l);
            const seq = others.slice();
            seq.splice(dropIndex(others, (+l.start||0) + (+l.dur||0)/2), 0, l);
            resequence(seq);
          }
          save(); render(); syncScrub(); return;   // full redraw so the reflowed clips paint in their new order
        }
        save(); repaint('inspector'); syncScrub();
      });
    });
  }

  function syncScrub(){
    const s=document.getElementById('mb-scrub'), t=document.getElementById('mb-time');
    if(s){ s.max=projEnd().toFixed(2); }
    if(t){ t.textContent=(+(s?s.value:0)).toFixed(1)+'s / '+projEnd().toFixed(1)+'s'; }
  }

  // Preview scrubbing: show only the layers alive at time t, and seek videos to their own local time.
  function seek(t){
    P.layers.forEach(l=>{
      const el=document.querySelector('.mb-item[data-id="'+l.id+'"]'); if(!el) return;
      const on = t>=l.start && t<=l.start+l.dur;
      el.style.display = on ? '' : 'none';
      const v=el.querySelector('video');
      if(v && on){ const local=(l.trim||0)+(t-l.start); if(Math.abs(v.currentTime-local)>0.25){ try{ v.currentTime=local; }catch(_){ } } }
    });
    const time=document.getElementById('mb-time');
    if(time) time.textContent=t.toFixed(1)+'s / '+projEnd().toFixed(1)+'s';
  }

  let _playT=null;
  function togglePlay(){
    const btn=document.getElementById('mb-play'), scrub=document.getElementById('mb-scrub');
    if(_playT){ clearInterval(_playT); _playT=null; if(btn) btn.textContent='▶︎';
      document.querySelectorAll('.mb-item video').forEach(v=>{ try{ v.pause(); }catch(_){ } }); return; }
    if(btn) btn.textContent='❚❚';
    let t=+(scrub?scrub.value:0);
    document.querySelectorAll('.mb-item video').forEach(v=>{ try{ v.play().catch(()=>{}); }catch(_){ } });
    _playT=setInterval(()=>{
      t+=0.1; if(t>projEnd()){ t=0; }
      if(scrub) scrub.value=t; seek(t);
    }, 100);
  }

  async function pickMedia(){
    const st=document.getElementById('mb-status');
    // Blossom picker OR a local file. Local files upload first so the render service can fetch them.
    const inp=document.createElement('input'); inp.type='file';
    inp.accept='image/*,video/*'; inp.multiple=true;
    inp.onchange=async()=>{
      for(const f of Array.from(inp.files||[])){
        try{
          if(st) st.textContent='uploading '+f.name+'…';
          const url=await uploadBlob(f);
          addLayer(f.type.startsWith('video')?'video':'image', url, { name:f.name.slice(0,24) });
          render();
        }catch(err){
          if(st) st.textContent='upload failed: '+((err&&err.message)||err);
          return;
        }
      }
      if(st) st.textContent='';
    };
    inp.click();
  }

  async function doRender(){
    if(!P.layers.length){ toast('add a layer first'); return; }
    // Guard on MODULE state, not just the button's disabled flag: any repaint/render() of the view replaces
    // that button with a fresh ENABLED one, so mid-render edits made it clickable again and each click
    // spawned another full server-side ffmpeg of the same project (they pile up and the UI sits on
    // "rendering…" forever). The server enforces this too (429), but don't even send the duplicate.
    if(_rendering){ toast('already rendering — hang on'); return; }
    _rendering = true;
    const st=document.getElementById('mb-status'), btn=document.getElementById('mb-render');
    const out=document.getElementById('mb-result');
    if(btn){ btn.disabled=true; }
    if(st) st.textContent='rendering…';
    try{
      const edit={ w:P.w, h:P.h, fps:P.fps, bg:P.bg, duration:projEnd(),
        layers:P.layers.map(l=>({ type:l.type, src:l.src, start:+l.start, dur:+l.dur, trim:+l.trim||0,
          x:Math.round(l.x), y:Math.round(l.y), w:Math.round(l.w), h:Math.round(l.h),
          opacity:+l.opacity, effect:l.effect, mute:!!l.mute, volume:+l.volume||1,
          text:l.text, size:+l.size, color:l.color, stroke:l.stroke })) };
      const auth=await selfProof();
      const r=await fetch('/client/meme/render',{ method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth, edit }) });
      if(!r.ok){
        let msg=''; try{ msg=(await r.json()).detail||''; }catch(_){ msg=await r.text().catch(()=>''); }
        throw new Error(msg||('render failed ('+r.status+')'));
      }
      const blob=await r.blob();
      if(st) st.textContent='';
      showResult(blob, out);
    }catch(err){
      if(st) st.textContent='';
      if(out) out.innerHTML='<div class="mb-err">⚠️ '+enc((err&&err.message)||err)+'</div>';
    }finally{ _rendering=false; const b=document.getElementById('mb-render'); if(b) b.disabled=false; }
  }

  function showResult(blob, out){
    const url=URL.createObjectURL(blob);
    out.innerHTML=`<div class="mb-result">
      <video src="${url}" controls playsinline class="mb-resvid"></video>
      <div class="mb-resacts">
        <button class="btn btn-neon small" id="mb-post">📤 Post to Nostr</button>
        <button class="btn btn-cyan small" id="mb-copy">🔗 Upload &amp; copy link</button>
        <a class="btn btn-ghost small" href="${url}" download="meme.mp4">⬇️ Download</a>
      </div>
      <div class="muted small" id="mb-reslink"></div>
    </div>`;
    const file=new File([blob], 'meme.mp4', { type:'video/mp4' });
    const linkEl=()=>document.getElementById('mb-reslink');
    const up=async()=>{
      const el=linkEl(); if(el) el.textContent='uploading to Blossom…';
      const u=await uploadBlob(file);
      if(el) el.innerHTML='<a href="'+enc(u)+'" target="_blank" rel="noopener">'+enc(u)+'</a>';
      return u;
    };
    const c=document.getElementById('mb-copy');
    if(c) c.onclick=async()=>{ try{ const u=await up(); await navigator.clipboard.writeText(u); toast('link copied'); }
      catch(err){ const el=linkEl(); if(el) el.textContent='upload failed: '+((err&&err.message)||err); } };
    const p=document.getElementById('mb-post');
    if(p) p.onclick=async()=>{ try{ const u=await up();
        if(PC.compose) PC.compose(u); else { await navigator.clipboard.writeText(u); toast("link copied — paste it into a post"); } }
      catch(err){ const el=linkEl(); if(el) el.textContent='upload failed: '+((err&&err.message)||err); } };
  }

  function bindInspector(root){
    const l=P.layers.find(x=>x.id===sel); if(!l) return;
    const on=(id,ev,fn)=>{ const e=root.querySelector('#'+id); if(e) e.addEventListener(ev,fn); };
    const num=(id,key,lo,hi)=>on(id,'input',(e)=>{ l[key]=clamp(e.target.value,lo,hi); save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) applyGeom(it,l);
      if(key==='start'||key==='dur') repaint('timeline'); });
    num('mb-f-w','w',16,4320); num('mb-f-h','h',16,4320);
    // Blow the layer up to the FULL project canvas and pin it to 0,0 — the common "make this the background /
    // full-bleed clip" move, which otherwise means typing the project's W and H and zeroing X/Y by hand.
    on('mb-fill','click',()=>{ l.x=0; l.y=0; l.w=P.w; l.h=P.h; save(); render(); });
    num('mb-f-start','start',0,120); num('mb-f-dur','dur',0.1,120); num('mb-f-trim','trim',0,600);
    num('mb-f-size','size',8,400);
    on('mb-f-text','input',(e)=>{ l.text=e.target.value; save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) it.childNodes[0].nodeValue=l.text;
      repaint('timeline'); });
    on('mb-f-color','input',(e)=>{ l.color=e.target.value; save(); repaint(); });
    on('mb-f-stroke','input',(e)=>{ l.stroke=e.target.value; save(); repaint(); });
    on('mb-f-fx','change',(e)=>{ l.effect=e.target.value; save(); });
    on('mb-f-mute','change',(e)=>{ l.mute=e.target.checked; save(); });
    on('mb-f-op','input',(e)=>{ l.opacity=clamp(e.target.value,0.05,1); save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) it.style.opacity=l.opacity; });
    on('mb-del','click',async()=>{
      if(!await uiConfirm('Delete this layer?')) return;
      P.layers=P.layers.filter(x=>x.id!==l.id); sel=null; save(); render();
    });
    on('mb-front','click',()=>{ const i=P.layers.indexOf(l); if(i>-1){ P.layers.splice(i,1); P.layers.push(l); save(); render(); } });
    on('mb-back','click',()=>{ const i=P.layers.indexOf(l); if(i>0){ P.layers.splice(i,1); P.layers.unshift(l); save(); render(); } });
  }

  function render(){
    const feed=document.getElementById('feed'); if(!feed) return;
    if(_playT){ clearInterval(_playT); _playT=null; }
    feed.innerHTML=view();
    const root=feed;
    bindStage(root); bindTimeline(root); bindInspector(root);
    const on=(id,ev,fn)=>{ const e=root.querySelector('#'+id); if(e) e.addEventListener(ev,fn); };
    on('mb-add-media','click',pickMedia);
    on('mb-add-text','click',()=>{ addLayer('text'); render(); });
    on('mb-add-blossom','click',pickBlossom);
    on('mb-save','click',saveProject);
    on('mb-open','click',openProject);
    on('mb-render','click',doRender);
    on('mb-play','click',togglePlay);
    on('mb-scrub','input',(e)=>seek(+e.target.value));
    on('mb-size','change',(e)=>{ const [w,h]=e.target.value.split('x').map(Number); P.w=w; P.h=h; save(); render(); });
    root.querySelectorAll('.mb-track').forEach(t=>t.addEventListener('click',(e)=>{
      if(!e.target.closest('.mb-clip')) selectLayer(t.dataset.id); }));
    seek(0);
  }

  boot();
})();

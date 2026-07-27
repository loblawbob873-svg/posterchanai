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
      // Persist on the way OUT too. Every edit already saves, but leaving the view is exactly when a
      // missed save becomes 'my project came back different', so make it unconditional.
      unmount(){ try{ stopPlay(false); }catch(_){ if(_playT){ clearInterval(_playT); _playT=null; } } try{ if(P) save(); }catch(_){ } },
      reset(){ P = blank(); sel=null; save(); render(); },
    };
  }

  const FX = [
    ['none','None'], ['fade','Fade in/out'], ['zoom','Ken Burns zoom'], ['shake','Shake'],
    ['pulse','Pulse'], ['spin','Spin'], ['glow','Glow'], ['blur','Blur'],
    ['grayscale','Grayscale'], ['sepia','Sepia'], ['invert','Invert'], ['flip','Mirror'],
  ];
  // Sound effects available per layer (AI-chat catalogue). Fetched once; empty until it resolves, so the
  // dropdown simply shows 'None' on a node with no sound assets rather than breaking.
  let SOUNDS = [];
  fetch('/client/meme/sounds').then(r=>r.json()).then(j=>{ SOUNDS=(j&&j.sounds)||[]; if(document.getElementById('mb-inspector')) repaint('inspector'); }).catch(()=>{});
  // The FULL effect catalogue — the same one the Effects studio + Telegram use (/client/effects), so the
  // Meme Builder never drifts from what the app supports. An image layer's "Meme effect" dropdown lists
  // all of these; picking one runs that effect ON the layer's image (glow, alive, nakedman, meme, sopranos,
  // diarrhea, …) server-side and swaps the layer's source to the resulting clip. Fetched once.
  let EFFECTS = [];
  fetch('/client/effects').then(r=>r.json()).then(j=>{
    const en=(j&&j.enhance)||[], fx=(j&&j.effects)||[];
    EFFECTS = en.concat(fx).map(e=>({ name:e.name, label:e.name, desc:e.desc||'' }));
  }).catch(()=>{});
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
      if(r && Array.isArray(r.layers)){ _healLayers(r); return r; } }catch(_){ }
    return blank();
  }
  // Repair coordinates saved by older builds. Text used to be positioned against a box that was up to 92%
  // of the canvas wide with its lines centred inside, so "drag it to the middle" wrote an x that was wildly
  // off (negative, or past the right edge) — and the render, which honours x literally, then drew the caption
  // hard against the frame edge. Pull any off-canvas caption back into view once, so an old project stops
  // rendering wrong through no fault of the user.
  function _healLayers(proj){
    if(proj._healed) return;            // ONCE per project — never touch a position again after this
    proj._healed = 1;
    const W=+proj.w||720, H=+proj.h||1280;
    (proj.layers||[]).forEach(l=>{
      if(l.type!=='text') return;
      // ONLY rescue a caption that is genuinely off the canvas (the old negative-x bug). Anything merely
      // NEAR an edge is a deliberate placement — the first version of this snapped those too, so a caption
      // saved at the bottom or right came back somewhere else entirely.
      if((+l.x||0) <= -8) l.x = Math.round(W*0.08);
      if((+l.y||0) <= -8) l.y = Math.round(H*0.08);
    });
  }

  const clamp = (v,lo,hi) => Math.max(lo, Math.min(hi, Number(v)||0));
  // A layer that occupies the CANVAS. Audio has no picture, so every place that reasoned "not text =
  // something to draw / something in the clip sequence" has to ask this instead.
  const _isVisual = (l) => l.type==='image' || l.type==='video';
  // AUDIO IS EXCLUDED from the project length, exactly like the renderer: dropping a three-minute song
  // onto a six-second meme must not turn it into a three-minute video. Music is truncated at the end
  // of the timeline instead.
  const projEnd = () => P.layers.filter(l=>l.type!=='audio')
    .reduce((m,l)=>Math.max(m, (+l.start||0)+(+l.dur||0)), 0) || P.duration;

  // ---------- the MASTER TIMELINE ----------
  // Media clips (image/video) form ONE ordered sequence that plays back-to-back — the way every video editor
  // works. Their `start` is DERIVED from that order, never hand-typed: drop a clip anywhere on the timeline
  // and the rest reflow around it (no gaps, no accidental overlaps). TEXT is excluded on purpose — a caption
  // is an overlay pinned ON the footage, so it keeps its own free start/duration.
  const mediaSeq = () => P.layers.filter(_isVisual)
    .sort((a,b)=>((+a.start||0)-(+b.start||0)) || (P.layers.indexOf(a)-P.layers.indexOf(b)));
  function resequence(seq){ let t=0; (seq||mediaSeq()).forEach(l=>{ l.start=+t.toFixed(2); t+=(+l.dur||0); }); }
  // Where a clip dragged to `center` (seconds, its midpoint) belongs in the sequence of the OTHERS.
  function dropIndex(others, center){
    let idx=0, acc=0;
    for(const o of others){ const w=+o.dur||0; if(center > acc + w/2) idx++; acc+=w; }
    return Math.min(idx, others.length);
  }

  // Rows read TOP-FIRST, like every editor: P.layers is draw order (last = on top), so the list is
  // reversed for display. Without this the row you saw at the top was actually the BOTTOM layer —
  // which is why a caption listed first rendered underneath the clip listed below it.
  // ...and the audio beds hang below everything, the way a soundtrack track sits under the video tracks
  // in any editor. They are not in _stageOrder at all (nothing to draw), so they are appended here.
  const _rowOrder = () => _stageOrder().slice().reverse().concat(P.layers.filter(l=>l.type==='audio'));

  // The RENDERER always composites captions last (meme_builder_service collects text layers and applies
  // their drawtext filters AFTER every overlay), so in the exported video text is ALWAYS on top and layer
  // order cannot change that. The preview used to draw in raw layer order, so it could show an image
  // covering a caption that the render would put underneath — you'd restack layers forever trying to fix
  // something that was only wrong on screen. Draw media first, then text, so the preview matches the export.
  // Captions default to CENTRED. Horizontal position was the single biggest source of 'the text is in
  // the wrong place': a hand-set x can never survive the browser/ffmpeg font difference, whereas ffmpeg
  // centring with (w-text_w)/2 is exact by construction. `undefined` (a layer made before this existed)
  // counts as centred too, so old projects fix themselves; an explicit '' means the user turned it off.
  // The caption's visual CENTRE in project pixels, measured from the preview element. Sent to the
  // renderer so it can place the text as centre - text_w/2 using ITS OWN width — the browser and
  // ffmpeg disagree on string width, and anchoring on the left edge bakes that disagreement into the
  // position (it is what produced x=-105 and captions landing left/right of where they were put).
  function _textCenterX(l){
    try{
      const el=document.querySelector('.mb-item[data-id="'+l.id+'"]');
      const st=document.getElementById('mb-stage');
      if(el && st){
        const r = el.getBoundingClientRect();
        // seek() hides layers outside the playhead's window (display:none), and a hidden element measures
        // ZERO — which produced a centre equal to the layer's left edge and put the caption off to one side.
        // Measure only when it is actually laid out; otherwise send nothing and let the renderer use x.
        if(!r.width) return null;
        const k = P.w / st.getBoundingClientRect().width;
        return Math.round((+l.x||0) + r.width * k / 2); }
    }catch(_){ }
    return null;
  }

  const _alignOf = (l) => (l.align || '');   // '' = obey the x you dragged it to; 'center' = let ffmpeg centre it

  const _stageOrder = () => P.layers.filter(_isVisual).concat(P.layers.filter(l=>l.type==='text'));

  function addLayer(type, src, extra){
    if(P.layers.length >= 24){ toast('24 layers is the limit'); return null; }
    // APPEND to the end of the timeline, do not stack at t=0. Defaulting every new layer to start:0 made
    // a second image land exactly on top of the first — the timeline read as "everything overlaps", which
    // is not how a video editor behaves. Media appends after the last clip so drops play in sequence;
    // TEXT still starts at 0, because a caption is an overlay ON the footage, not another clip after it.
    // Music defaults to the WHOLE timeline — that is what you want nine times out of ten when you drop a
    // song on a meme, and shortening it to one clip is a drag away. It never lengthens the project (the
    // renderer truncates it at the end), so "the whole timeline" is safe as a default even for a long track.
    const wasEnd = projEnd();
    const tail = (type==='text' || type==='audio') ? 0 : wasEnd;
    const l = Object.assign({
      id: nid(), type, src: src||'', name: '',
      start: tail, dur: type==='text' ? 3 : (type==='audio' ? Math.max(projEnd(), 1) : 4), trim: 0,
      x: 0, y: 0, w: type==='text' ? 0 : P.w, h: type==='text' ? 0 : Math.round(P.h/2),
      opacity: 1, effect: 'none', volume: 1, mute: false,
      text: type==='text' ? 'top text' : '', size: 64, color:'#ffffff', stroke:'#000000',
      align: '',
    }, extra||{});
    if(type==='audio'){
      l.x=0; l.y=0; l.w=0; l.h=0;
      l.volume = 0.6;      // sit UNDER the clips' own sound — amix here runs normalize=0, so 1.0 competes with it
      l.fade = true;       // truncation at the end of the timeline is a hard cut otherwise
    }
    else if(type!=='text'){ l.y = Math.round((P.h - l.h)/2); }
    else { l.x = Math.round(P.w*0.08); l.y = Math.round(P.h*0.08); }
    // P.layers order IS the draw order (later = on top). A caption must stay ABOVE the footage, so a new
    // MEDIA clip goes in below the text layers rather than on top of everything — otherwise adding a clip
    // after writing your caption silently buried the caption under it. Text still goes on top.
    if(type==='text'){ P.layers.push(l); }
    else if(type==='audio'){ P.layers.unshift(l); }   // draw order is meaningless for it; keep it out of the way
    else {
      const firstText = P.layers.findIndex(x=>x.type==='text');
      if(firstText < 0) P.layers.push(l); else P.layers.splice(firstText, 0, l);
    }
    sel = l.id;
    if(_isVisual(l)){
      resequence();   // join the master timeline exactly back-to-back (no drift from `tail`)
      // Keep a FULL-LENGTH music bed full-length. Adding a clip makes the meme longer, and a soundtrack
      // that was covering the whole thing would otherwise stop early — reported as "the music cuts out".
      // Only a track that spans exactly the old timeline is grown; one you deliberately trimmed is left alone.
      const end = projEnd();
      P.layers.forEach(a=>{ if(a.type==='audio' && !(+a.start||0) && Math.abs((+a.dur||0) - wasEnd) < 0.06)
        a.dur = +end.toFixed(2); });
    }
    save(); return l;
  }

  // ---------- rendering the UI ----------
  function view(){
    return `
    <div class="mb-wrap">
      <div class="mb-bar">
        <button class="btn btn-neon small" id="mb-add-media">🖼️ Add media</button>
        <button class="btn btn-cyan small" id="mb-add-text">🅣 Add text</button>
        <button class="btn btn-cyan small" id="mb-add-audio" title="Add a music track under the whole meme">🎵 Add music</button>
        <button class="btn btn-cyan small" id="mb-add-blossom">🌸 From Blossom</button>
        <button class="btn btn-cyan small" id="mb-save">💾 Save</button>
        <button class="btn btn-cyan small" id="mb-open">📂 Open</button>
        <button class="btn btn-cyan small" id="mb-arrange" title="Lay every clip back-to-back in its current order">⇄ Arrange</button>
        <button class="btn btn-danger small" id="mb-clear" title="Remove every layer and start a fresh build">🧹 Clear all</button>
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
            ${_stageOrder().map(stageEl).join('')}
          </div>
          <!-- Music beds have nothing to show on the stage, but the PREVIEW has to be able to hear them —
               otherwise you can only judge the mix by rendering. One hidden <audio> per track, driven by
               the same playhead as the video layers (see seekAudio). -->
          <div class="mb-audios" id="mb-audios" aria-hidden="true">
            ${P.layers.filter(l=>l.type==='audio').map(l=>`<audio data-id="${l.id}" src="${enc(l.src)}" preload="metadata"></audio>`).join('')}
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

      ${P.layers.some(_isVisual) ? '<div class="muted small mb-tlhint">Drag a clip along the timeline to reorder it — the rest reflow back-to-back. Drag its edges to trim.</div>' : ''}
      <div class="mb-timeline" id="mb-timeline">
        ${P.layers.length ? _rowOrder().map(trackEl).join('') : '<div class="muted small mb-empty">No layers yet — add media or text to start.</div>'}
      </div>
      <div id="mb-result"></div>
    </div>`;
  }

  function stageEl(l){
    const s = l.id===sel ? ' sel' : '';
    const pos = `left:${(l.x/P.w*100).toFixed(3)}%;top:${(l.y/P.h*100).toFixed(3)}%;`;
    if(l.type==='text'){
      // Centred captions span the full width and centre their text, mirroring drawtext's (w-text_w)/2.
      const cpos = _alignOf(l)==='center' ? `left:50%;top:${(l.y/P.h*100).toFixed(3)}%;` : pos;   // .centred shifts back by half its width
      return `<div class="mb-item mb-text${_alignOf(l)==='center'?' centred':''}${s}" data-id="${l.id}" style="${cpos}font-size:${(l.size/P.w*100).toFixed(3)}cqw;color:${enc(l.color)};-webkit-text-stroke:.03em ${enc(l.stroke)};opacity:${l.opacity}">
        ${enc(l.text||' ')}<i class="mb-h"></i></div>`;
    }
    const size = `width:${(l.w/P.w*100).toFixed(3)}%;height:${(l.h/P.h*100).toFixed(3)}%;`;
    // object-fit must mirror the renderer: 'cover' fills the box and crops, 'contain' letterboxes.
    const ofit = (l.fit==='cover') ? 'cover' : 'contain';
    // `#t=0.1` (a media fragment) makes the browser seek to 0.1s and DISPLAY that frame as a poster the
    // moment the layer mounts — otherwise `preload` alone decodes nothing and the clip shows blank until
    // you scrub/play (the "I have to render to see the effect" bug). preload=auto so the frame loads eagerly.
    const inner = l.type==='video'
      ? `<video src="${enc(l.src)}#t=0.1" muted playsinline preload="auto" style="object-fit:${ofit}"></video>`
      : `<img src="${enc(l.src)}" alt="" style="object-fit:${ofit}">`;
    return `<div class="mb-item mb-media${s}" data-id="${l.id}" style="${pos}${size}opacity:${l.opacity}">${inner}<i class="mb-h"></i></div>`;
  }

  function trackEl(l){
    const total = Math.max(projEnd(), 1);
    let left = (l.start/total*100), wid = Math.max(3, l.dur/total*100);
    // Music is the ONE layer that can run past the end of the timeline (the renderer truncates it), and
    // an unclamped bar is a real layout break, not a cosmetic one: a 3-minute song on a 6-second meme is
    // a 3000%-wide absolutely-positioned div in a lane with no overflow clipping. Clamp it to the lane and
    // mark it as cut, so the bar shows exactly the part that will actually be in the video.
    let cut = false;
    if(l.type==='audio' && left + wid > 100){
      left = Math.min(left, 97); wid = Math.max(3, 100 - left); cut = true;
    }
    // Show the CLIP ITSELF, not its filename — a hashed Blossom URL says nothing about what the clip is, so a
    // row of thumbnails is the only way to read the timeline at a glance. Text layers show their words (that
    // IS their content). The full name stays as the tooltip.
    const label = l.type==='text' ? ('🅣 ' + (l.text||'text')) : (l.name || srcName(l.src));
    const thumb = l.type==='text'
      ? `<span class="mb-ttxt">🅣 ${enc((l.text||'text').slice(0,16))}</span>`
      : (l.type==='audio'
        ? `<span class="mb-ttxt mb-taud">🎵 ${enc((l.name || srcName(l.src)).slice(0,16))}</span>`
        : l.type==='video'
          ? `<video class="mb-tthumb" src="${enc(l.src)}#t=0.1" muted playsinline preload="metadata"></video><i class="mb-tvid">▶︎</i>`
          : `<img class="mb-tthumb" src="${enc(l.src)}" alt="" loading="lazy">`);
    return `<div class="mb-track${l.id===sel?' sel':''}${l.type==='audio'?' mb-track-aud':''}" data-id="${l.id}">
      <div class="mb-trackname" title="${enc(label)}">${thumb}
        ${l.type==='audio' ? '' : `<span class="mb-zbtns">
          <button class="mb-z" data-z="front" data-id="${l.id}" title="Bring to front">⬆︎</button>
          <button class="mb-z" data-z="back" data-id="${l.id}" title="Send to back">⬇︎</button>
        </span>`}
      </div>
      <div class="mb-lane">
        <div class="mb-clip${cut?' mb-cut':''}" data-id="${l.id}" style="left:${left.toFixed(3)}%;width:${wid.toFixed(3)}%"${cut?` title="${l.dur.toFixed(1)}s of music — cut off at ${total.toFixed(1)}s"`:''}>
          <i class="mb-grip mb-grip-l" data-grip="l"></i>
          <span>${cut ? '✂ '+total.toFixed(1)+'s' : l.dur.toFixed(1)+'s'}</span>
          <i class="mb-grip mb-grip-r" data-grip="r"></i>
        </div>
      </div>
    </div>`;
  }

  function inspector(){
    const l = P.layers.find(x=>x.id===sel);
    if(!l) return `<div class="muted small">Select a layer to edit it.</div>`;
    const isText = l.type==='text';
    // Audio gets its OWN short panel and returns early: geometry, opacity, visual effects and the sound
    // catalogue are all meaningless for a track with no picture, and showing them just invites you to set
    // something the renderer will ignore.
    if(l.type==='audio'){
      const vol = (l.volume==null?0.6:+l.volume);
      return `
      <div class="mb-insp-hd">
        <b>🎵 Music layer</b>
        <button class="btn btn-danger small" id="mb-del">🗑️ Delete</button>
      </div>
      <div class="muted small mb-dbg">${enc(l.name || srcName(l.src))}</div>
      <button class="btn btn-cyan small full" id="mb-aud-all" title="Start at 0 and run to the end of the meme">⇔ Span the whole meme</button>
      <div class="mb-frow">
        <label class="mb-f"><span>Start (s)</span><input class="input" type="number" id="mb-f-start" min="0" step="0.1" value="${l.start}"></label>
        <label class="mb-f"><span>Length (s)</span><input class="input" type="number" id="mb-f-dur" min="0.1" step="0.1" value="${l.dur}"></label>
      </div>
      <label class="mb-f"><span>Skip into the song (s)</span><input class="input" type="number" id="mb-f-trim" min="0" step="0.5" value="${l.trim||0}"></label>
      <label class="mb-f"><span>Volume</span><input type="range" id="mb-f-avol" min="0" max="2" step="0.05" value="${vol}"></label>
      <label class="mb-f mb-check"><input type="checkbox" id="mb-f-afade" ${l.fade?'checked':''}><span>Fade in/out</span></label>
      <div class="muted small mb-dbg">Music never lengthens the meme — anything past ${projEnd().toFixed(1)}s is cut off.</div>`;
    }
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
        </div>
        <button class="btn btn-cyan small full" id="mb-center">⇔ Centre horizontally</button>
        <div class="muted small mb-dbg">x=${Math.round(l.x)} y=${Math.round(l.y)} size=${Math.round(l.size)} align=${_alignOf(l)||"free"} · canvas ${P.w}×${P.h}</div>` : `
        <div class="mb-frow">
          <label class="mb-f"><span>W</span><input class="input" type="number" id="mb-f-w" value="${Math.round(l.w)}"></label>
          <label class="mb-f"><span>H</span><input class="input" type="number" id="mb-f-h" value="${Math.round(l.h)}"></label>
        </div>
        <div class="mb-frow"><button class="btn btn-cyan small" id="mb-fit" title="Size the whole photo to the canvas — nothing is cut off">⛶ Fill the canvas</button><button class="btn btn-cyan small" id="mb-fill" title="Crop the edges so there are no bars">✂ Crop to fill</button></div>
        ${l.type==='video' ? trimWidget(l) + `
        <button class="btn btn-cyan small full" id="mb-prev-clip" title="Play just this clip in the preview above">▶︎ Preview clip</button>
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-mute" ${l.mute?'checked':''}><span>Mute this clip</span></label>` : ''}`}
      ${l.type==='video'
        ? `<div class="muted small mb-dbg">Drag the clip on the timeline below to set when it appears in the meme.</div>`
        : `<div class="mb-frow">
        <label class="mb-f"><span>Start (s)</span><input class="input" type="number" id="mb-f-start" min="0" step="0.1" value="${l.start}"></label>
        <label class="mb-f"><span>Length (s)</span><input class="input" type="number" id="mb-f-dur" min="0.1" step="0.1" value="${l.dur}"></label>
      </div>`}
      <label class="mb-f"><span>Effect</span><select class="input" id="mb-f-fx">
        ${FX.map(([v,n])=>`<option value="${v}" ${l.effect===v?'selected':''}>${n}</option>`).join('')}
      </select></label>
      ${(l.type==='image') && EFFECTS.length ? `<label class="mb-f"><span>Meme effect</span><select class="input" id="mb-f-meme">
        <option value="">— apply an effect to this image —</option>
        ${EFFECTS.map(e=>`<option value="${enc(e.name)}" title="${enc(e.desc||'')}">${enc(e.label||e.name)}</option>`).join('')}
      </select></label>
      <button class="btn btn-cyan small full" id="mb-prev-fx" title="Play just this layer in the preview above">▶︎ Preview effect</button>` : ''}
      <label class="mb-f"><span>Sound</span><select class="input" id="mb-f-snd">
        <option value="">None</option>
        ${SOUNDS.map(n=>`<option value="${enc(n)}" ${l.sound===n?'selected':''}>${enc(n)}</option>`).join('')}
      </select></label>
      ${l.sound ? `<label class="mb-f"><span>Sound volume</span><input type="range" id="mb-f-sndvol" min="0" max="3" step="0.1" value="${(l.soundVolume==null?1:l.soundVolume)}"></label>` : ''}
      <label class="mb-f"><span>Opacity</span><input type="range" id="mb-f-op" min="0.05" max="1" step="0.05" value="${l.opacity}"></label>
      <div class="mb-order">
        <button class="btn btn-cyan small" id="mb-back">⬇︎ Send back</button>
        <button class="btn btn-cyan small" id="mb-front">⬆︎ Bring front</button>
      </div>`;
  }

  const srcName = (u) => { try{ return decodeURIComponent(String(u).split('/').pop()).slice(0,18) || 'clip'; }catch(_){ return 'clip'; } };
  function enc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

  // Visual clip trimmer for a VIDEO layer — replaces the old "Trim from (s)" / "Length (s)" number boxes
  // you had to guess at. The source video IS the scrubber: the bright band is what plays, the dark ends are
  // trimmed off, and dragging a neon handle seeks the frame so you can SEE where you're cutting. It writes
  // l.trim (in-point) and l.dur (length = out-point − in-point); bindTrim() wires the drag. The natural
  // duration is read from the element's metadata, so the "what do I even type" problem goes away.
  function trimWidget(l){
    return `
      <div class="mb-f"><span>Clip — drag the ends to pick what plays</span>
        <div class="mb-trim" id="mb-trim">
          <video class="mb-trim-vid" src="${enc(l.src)}#t=${(+l.trim||0).toFixed(2)}" muted playsinline preload="metadata"></video>
          <div class="mb-trim-track" id="mb-trim-track">
            <div class="mb-trim-dim mb-trim-dim-l"></div>
            <div class="mb-trim-dim mb-trim-dim-r"></div>
            <div class="mb-trim-sel"></div>
            <div class="mb-trim-h mb-trim-in" title="Start of the clip"></div>
            <div class="mb-trim-h mb-trim-out" title="End of the clip"></div>
          </div>
        </div>
        <div class="mb-trim-lbl"><span class="mb-trim-tin">0:00</span><span class="mb-trim-len">${(+l.dur||0).toFixed(1)}s</span><span class="mb-trim-tout">0:00</span></div>
      </div>`;
  }


  // ---------- Blossom ----------
  // Your drive, as a source for layers AND as where saved projects live. Media already on Blossom needs
  // no upload and no re-encode — the render service fetches the same URL — so picking from here is both
  // faster and cheaper than re-adding a file you already own. (Media picking goes through the app's
  // shared picker below; this raw listing is what the project save/open scan needs.)
  async function listBlobs(){
    const server = PC.mediaServer && PC.mediaServer();
    if(!server) throw new Error('no Blossom server configured');
    const r = await fetch(server + '/list/' + PC.ME.pubkey);
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  }
  const blobUrl = (b) => b.url || ((PC.mediaServer && PC.mediaServer()) + '/' + b.sha256);

  // The app's own file picker, narrowed to media. It used to be a private grid here, which meant the
  // Meme Builder showed a FLAT drive while every other picker in the client showed your folders — and
  // a drive with a few hundred blobs is unusable flat. Reusing blossomPicker also inherits the folder
  // bar, the encrypted-blob hygiene and the server-side video thumbnails for free.
  function pickBlossom(){
    PC.blossomPicker(null, ({ url, type }) => {
      addLayer(/^video\//.test(type||'') ? 'video' : 'image', url);
      render();
    }, {
      title: '🌸 Add from Blossom',
      filter: b => /^(image|video)\//.test(b.type||''),
      empty: 'No images or videos on your Blossom drive yet — upload some in the Files tab.',
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
    if(what==='timeline'){ const t=document.getElementById('mb-timeline'); if(t){ t.innerHTML=P.layers.length?_rowOrder().map(trackEl).join(''):'<div class="muted small mb-empty">No layers yet — add media or text to start.</div>'; } return; }
    render();
  }

  function selectLayer(id){
    sel = id;
    document.querySelectorAll('.mb-item,.mb-track').forEach(e=>e.classList.toggle('sel', e.dataset.id===id));
    // Jump the playhead to where this layer actually appears. Selecting a clip whose slot is elsewhere on
    // the timeline used to leave the preview parked at the old time — showing a DIFFERENT clip (or nothing),
    // so you were positioning/trimming something you couldn't see. Skip while playing: yanking the playhead
    // mid-playback would fight the user. Land just INSIDE the clip so it's visible, not on its exact edge.
    if(!_playT){
      const l = P.layers.find(x=>x.id===id);
      if(l){
        const t = Math.min((+l.start||0) + Math.min(0.05, (+l.dur||0)/2), Math.max(0, projEnd()));
        const s = document.getElementById('mb-scrub');
        if(s) s.value = t.toFixed(2);
        seek(t);
      }
    }
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
        } else {
          // FREE positioning — put it exactly where you drag it. The clamp that used to live here bounded x
          // by the layer's `size` (text has no width in the model), which meant a wide caption hit the limit
          // almost immediately and simply would not move left any further. drawtext accepts any x/y, so the
          // editor should not invent a boundary the renderer does not have.
          l.x = Math.round(ox+dx); l.y = Math.round(oy+dy);
        }
        applyGeom(item, l);
      }, ()=>{ save(); repaint('inspector'); });
    });
  }

  function applyGeom(el, l){
    el.style.left = (l.x/P.w*100).toFixed(3)+'%';
    el.style.top  = (l.y/P.h*100).toFixed(3)+'%';
    if(l.type==='text') el.style.fontSize = (l.size/P.w*100).toFixed(3)+'cqw';
    else { el.style.width=(l.w/P.w*100).toFixed(3)+'%'; el.style.height=(l.h/P.h*100).toFixed(3)+'%'; }
  }

  function bindTimeline(root){
    const tl = root.querySelector('#mb-timeline'); if(!tl) return;
    // z-order right on the layer row, next to its thumbnail — that's where you're already looking when you
    // decide what should sit on top. Bound on the timeline (not the inspector) so it works without selecting
    // the layer first. stopPropagation: these live inside the row, which is also the drag surface.
    tl.addEventListener('click', (e)=>{
      const zb = e.target.closest('.mb-z'); if(!zb) return;
      e.preventDefault(); e.stopPropagation();
      const l = P.layers.find(x=>x.id===zb.dataset.id); if(!l) return;
      const i = P.layers.indexOf(l); if(i<0) return;
      // ONE step, not all the way to the extreme. Jumping straight to front/back meant putting a layer
      // just under the one above it took two moves (down to the very bottom, then back up) — swap with the
      // neighbour instead, which is what "move it under that one" actually means.
      const j = zb.dataset.z==='front' ? i+1 : i-1;
      if(j<0 || j>=P.layers.length) return;                 // already at the top/bottom
      P.layers[i]=P.layers[j]; P.layers[j]=l;
      save(); render();
    });
    tl.addEventListener('pointerdown', (e)=>{
      if(e.target.closest('.mb-z')) return;   // a z-order tap is not the start of a drag
      const clip = e.target.closest('.mb-clip'); if(!clip) return;
      const l = P.layers.find(x=>x.id===clip.dataset.id); if(!l) return;
      selectLayer(l.id);
      const lane = clip.parentElement, rect = lane.getBoundingClientRect();
      const total = Math.max(projEnd(), 1), perPx = total/rect.width;
      const grip = e.target.closest('.mb-grip'), side = grip && grip.dataset.grip;
      const sx=e.clientX, ost=l.start, odur=l.dur;
      let moved=false;   // a CLICK must never reshuffle the timeline — only an actual drag does
      e.preventDefault();
      drag(e, (ev)=>{
        if(Math.abs(ev.clientX-sx) > 3) moved=true;
        const d=(ev.clientX-sx)*perPx;
        if(side==='l'){ const ns=clamp(ost+d, 0, ost+odur-0.2); l.dur=+(odur+(ost-ns)).toFixed(2); l.start=+ns.toFixed(2); }
        else if(side==='r'){ l.dur=+clamp(odur+d, 0.2, 120).toFixed(2); }
        else { l.start=+clamp(ost+d, 0, 120).toFixed(2); }
        // Same clamp as trackEl — without it, stretching a music bar past the end of the meme blows the
        // lane out mid-drag (it only snapped back on drop, when the row is re-rendered).
        let dl=l.start/total*100, dw=Math.max(3, l.dur/total*100);
        if(l.type==='audio' && dl+dw>100){ dl=Math.min(dl,97); dw=Math.max(3,100-dl); }
        clip.style.left=dl.toFixed(3)+'%';
        clip.style.width=dw.toFixed(3)+'%';
        const sp=clip.querySelector('span'); if(sp) sp.textContent=l.dur.toFixed(1)+'s';
      }, ()=>{
        // Drop = commit to the MASTER TIMELINE. A media clip dragged anywhere REORDERS the sequence (it lands
        // where you dropped it and everything reflows back-to-back); a trim just closes the gap it left. Text
        // overlays keep the free position they were dragged to.
        if(l.type!=='text'){
          if(!moved){                 // a plain click: select + seek only, never touch the order
            l.start=ost; l.dur=odur;  // undo any sub-pixel drift so the clip cannot creep
            repaint('timeline'); repaint('inspector'); syncScrub(); return;
          }
          // Move ONLY the clip you dragged. Auto-resequencing every other clip on each drop meant nudging
          // one layer silently rewrote the timing of everything after it — you could never adjust a single
          // clip without disturbing the rest. Laying clips back-to-back is now an explicit action (⇄ Arrange).
          save(); repaint('timeline'); repaint('inspector'); syncScrub(); return;
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
    seekAudio(t);
    const time=document.getElementById('mb-time');
    if(time) time.textContent=t.toFixed(1)+'s / '+projEnd().toFixed(1)+'s';
  }

  // LIVE REFERENCES to the preview <audio> elements, not a DOM query. Replacing #feed's innerHTML detaches
  // them but does NOT stop them: a detached <audio> keeps playing until it is garbage collected, and by then
  // it can no longer be found by querySelector. That is silent for the video layers (they are muted) but
  // would leave music playing over the whole app after leaving the Meme Builder. Holding the elements means
  // stopPlay can always pause them, mounted or not.
  let _audioEls = [];
  const pauseAudio = () => _audioEls.forEach(a=>{ try{ a.pause(); }catch(_){ } });

  // The music beds, driven by the same playhead. Scrubbing only re-seeks them (silent); they actually
  // sound during playback. Preview volume is capped at 1 because that is all an <audio> element accepts —
  // the render honours the full 0-2 range, so a track boosted past 1 previews a little quieter than it exports.
  function seekAudio(t){
    const playing = !!_playT;
    P.layers.filter(l=>l.type==='audio').forEach(l=>{
      const a=_audioEls.find(x=>x.dataset.id===l.id); if(!a) return;
      const on = t>=(+l.start||0) && t<=(+l.start||0)+(+l.dur||0);
      a.volume = clamp(l.volume==null?0.6:l.volume, 0, 1);
      if(!on || !playing){ try{ a.pause(); }catch(_){ } }
      if(!on) return;
      const local=(+l.trim||0)+(t-(+l.start||0));
      if(Math.abs(a.currentTime-local)>0.3){ try{ a.currentTime=local; }catch(_){ } }
      if(playing && a.paused){ try{ a.play().catch(()=>{}); }catch(_){ } }
    });
  }

  let _playT=null;
  // One place that ends playback: clears the timer, pauses every video layer and restores the button.
  // `rewind` parks the playhead back at the start, which is what finishing a play-through should do.
  function stopPlay(rewind){
    if(_playT){ clearInterval(_playT); _playT=null; }
    document.querySelectorAll('.mb-item video').forEach(v=>{ try{ v.pause(); }catch(_){ } });
    pauseAudio();
    const b=document.getElementById('mb-play'); if(b) b.textContent='▶︎';
    if(rewind){ const s2=document.getElementById('mb-scrub'); if(s2) s2.value=0; seek(0); }
  }
  // endT (optional) — stop the preview at this time instead of the end of the whole meme. Used by
  // previewLayer() to play only one layer's slice ("Preview clip"/"Preview effect") so checking a trim or an
  // effect no longer means running the entire meme. The play-button binding calls togglePlay() with no arg.
  function togglePlay(endT){
    const btn=document.getElementById('mb-play'), scrub=document.getElementById('mb-scrub');
    if(_playT){ stopPlay(false); return; }
    const stopAt=(typeof endT==='number' && endT>0) ? Math.min(endT, projEnd()) : null;
    if(btn) btn.textContent='❚❚';
    let t=+(scrub?scrub.value:0);
    document.querySelectorAll('.mb-item video').forEach(v=>{ try{ v.play().catch(()=>{}); }catch(_){ } });
    // Start the music INSIDE the click handler. Kicking it off from the interval tick instead puts the
    // play() outside the user gesture, which is exactly what the browsers' autoplay policy blocks.
    _playT=1; seekAudio(t);
    _playT=setInterval(()=>{
      t+=0.1;
      // Leaving the Meme Builder replaces #feed wholesale and nothing calls unmount(), so the ticker used
      // to keep running over whatever view you switched to. Harmless while the only media was muted video;
      // with music it means a song playing over the rest of the app. Stop as soon as the stage is gone.
      if(!document.getElementById('mb-stage')){ stopPlay(false); return; }
      if(t > (stopAt!=null ? stopAt : projEnd())){  // STOP at the end — it used to wrap to 0 and loop forever.
        stopPlay(stopAt==null);   // full play rewinds to 0; a one-layer preview stays where it stopped.
        return;
      }
      if(scrub) scrub.value=t; seek(t);
    }, 100);
  }
  // Play only the selected layer's window in the preview, then stop. Lets you check a trimmed clip or an
  // applied effect right here, instead of pressing the big "🎬 Render" (which builds the whole finished meme).
  function previewLayer(l){
    if(!l || !document.getElementById('mb-stage')) return;
    if(_playT) stopPlay(false);
    const start=+l.start||0, end=start+Math.max(0.1,(+l.dur||0));
    const scrub=document.getElementById('mb-scrub');
    if(scrub) scrub.value=start.toFixed(2);
    seek(start);
    togglePlay(end);
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

  // Music: a local mp3/m4a/ogg/wav (uploaded to Blossom first, like every other layer source) or a track
  // already on your drive. One button, both paths — a separate "from Blossom" for audio would be a fourth
  // add-button in a bar that is already full on a phone.
  async function pickAudio(){
    const st=document.getElementById('mb-status');
    PC.modal(`<h3>🎵 Add music</h3>
      <button class="btn btn-neon full" id="mba-file">📁 Upload a file from this device</button>
      <button class="btn btn-cyan full" id="mba-blossom">🌸 Pick from my Blossom drive</button>`, root=>{
      root.querySelector('#mba-blossom').onclick=()=>{
        PC.closeModal();
        PC.blossomPicker(null, ({url})=>{ addLayer('audio', url); render(); }, {
          title: '🎵 Add music from Blossom',
          filter: b => /^audio\//.test(b.type||''),
          empty: 'No audio on your Blossom drive yet — upload some in the Files tab.',
        });
      };
      root.querySelector('#mba-file').onclick=()=>{
        PC.closeModal();
        const inp=document.createElement('input'); inp.type='file'; inp.accept='audio/*';
        inp.onchange=async()=>{
          const f=(inp.files||[])[0]; if(!f) return;
          try{
            if(st) st.textContent='uploading '+f.name+'…';
            const url=await uploadBlob(f);
            addLayer('audio', url, { name:f.name.slice(0,24) });
            if(st) st.textContent='';
            render();
          }catch(err){ if(st) st.textContent='upload failed: '+((err&&err.message)||err); }
        };
        inp.click();
      };
    });
  }

  // Apply a full effect (the app's whole effect engine — glow, alive, nakedman, meme, sopranos, …) TO an
  // image layer. The per-layer "Meme effect" dropdown is the SOURCE OF TRUTH (there is no separate effect
  // button): the server runs the effect on this layer's image and hands back the resulting clip, which we
  // swap in as the layer's source — so the effect is applied ON that image, exactly like the Effects
  // studio. Guarded like doRender so a double-tap can't fire two renders.
  let _fxBusy = false;
  async function applyMemeEffect(base, name){
    if(!name || !base || !base.src){ toast('add an image to this layer first'); return; }
    if(_fxBusy){ toast('still rendering the last effect — hang on'); return; }
    _fxBusy = true;
    const st=document.getElementById('mb-status');
    if(st) st.textContent='applying '+name+'…';
    try{
      const auth = await selfProof();
      const r = await fetch('/client/meme/apply-effect',{ method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth, url: base.src, effect: name }) });
      const j = await r.json().catch(()=>({}));
      if(!r.ok || !j.url){ throw new Error(j.detail || j.error || ('HTTP '+r.status)); }
      // The effect transforms the still into a clip — swap the layer's source IN PLACE (keep its box and
      // timeline slot), so the effect lands on this image. It becomes a video layer; duration follows the
      // effect clip so the whole thing plays.
      base.src = j.url;
      base.type = (j.is_video===false) ? 'image' : 'video';
      if(+j.dur>0) base.dur = +j.dur;
      base.name = String(name).slice(0,24);
      base.trim = 0;
      sel = base.id;
      save(); render();
      toast(name+' applied');
    }catch(err){ toast('effect failed: '+((err&&err.message)||err)); }
    finally{ _fxBusy = false; if(st) st.textContent=''; }
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
          opacity:+l.opacity, effect:l.effect, sound:l.sound||'', soundVolume:(l.soundVolume==null?1:+l.soundVolume), mute:!!l.mute,
          // NOT `+l.volume||1`: that turned a deliberate volume of 0 back into full volume.
          volume:(l.volume==null?1:+l.volume), fade:!!l.fade,
          text:l.text, size:+l.size, color:l.color, stroke:l.stroke, fit:l.fit||'contain', align:_alignOf(l), cx:(l.type==='text' ? _textCenterX(l) : null) })) };
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
        <a class="btn btn-neon small" href="${url}" download="meme.mp4">⬇️ Download</a>
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
        // compose() takes an OPTIONS OBJECT — passing the bare URL string destructured to nothing and
        // opened an empty composer with no link in it.
        if(PC.compose) PC.compose({ text:u }); else { await navigator.clipboard.writeText(u); toast("link copied — paste it into a post"); } }
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
    on('mb-fill','click',()=>{ l.x=0; l.y=0; l.w=P.w; l.h=P.h; l.fit='cover';
      save(); render(); toast('fills the frame — edges cropped'); });
    // The whole photo, scaled to fit inside the canvas. Can't do both: filling a different aspect ratio
    // always crops, and showing everything always leaves bars — so make it an explicit choice.
    on('mb-fit','click',()=>{ l.x=0; l.y=0; l.w=P.w; l.h=P.h; l.fit='contain';
      save(); render(); toast('whole photo — bars where the aspect differs'); });
    // Centre a caption: drawtext anchors the text's LEFT edge, so "centred" means x = (canvas - textWidth)/2.
    // Measure the preview element (it now hugs its text) and convert screen px -> project px.
    on('mb-center','click',()=>{
      // Flag it and let ffmpeg centre with (w-text_w)/2. Measuring the preview and computing a pixel x was
      // wrong: the browser wraps the caption at 92% and uses a different font, so the measured width (and
      // therefore x) did not match the single unwrapped line drawtext actually draws.
      l.align = (_alignOf(l)==='center') ? '' : 'center';
      save(); render(); toast(_alignOf(l)==='center' ? 'caption centred' : 'caption free-positioned');
    });
    num('mb-f-start','start',0,120); num('mb-f-dur','dur',0.1,120); num('mb-f-trim','trim',0,600);
    if(l.type==='video') bindTrim(root, l);
    on('mb-prev-clip','click',()=>previewLayer(l));   // play just this clip, not the whole meme
    on('mb-prev-fx','click',()=>previewLayer(l));      // same for an effect layer
    num('mb-f-size','size',8,400);
    on('mb-f-text','input',(e)=>{ l.text=e.target.value; save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) it.childNodes[0].nodeValue=l.text;
      repaint('timeline'); });
    on('mb-f-color','input',(e)=>{ l.color=e.target.value; save(); repaint(); });
    on('mb-f-stroke','input',(e)=>{ l.stroke=e.target.value; save(); repaint(); });
    on('mb-f-fx','change',(e)=>{ l.effect=e.target.value; save(); });
    // --- music layer ---
    on('mb-aud-all','click',()=>{ l.start=0; l.dur=+Math.max(projEnd(),0.1).toFixed(2); save(); render();
      toast('music spans the whole meme'); });
    on('mb-f-avol','input',(e)=>{ l.volume=clamp(e.target.value,0,2); save();
      const a=_audioEls.find(x=>x.dataset.id===l.id); if(a) a.volume=clamp(l.volume,0,1); });
    on('mb-f-afade','change',(e)=>{ l.fade=e.target.checked; save(); });
    on('mb-f-snd','change',(e)=>{ l.sound=e.target.value; save(); repaint('inspector'); toast(l.sound?('sound: '+l.sound):'sound removed'); });
    // The per-layer "Meme effect" dropdown is the SOURCE OF TRUTH for full effects (dancing man, shrug,
    // characters): picking one renders it server-side and overlays it ON this layer. Trigger dropdown —
    // reset to the placeholder after firing so it can be used again.
    on('mb-f-meme','change',(e)=>{ const nm=e.target.value; e.target.value=''; if(nm) applyMemeEffect(l, nm); });
    on('mb-f-sndvol','input',(e)=>{ l.soundVolume=clamp(e.target.value,0,3); save(); });
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

  // Wire the visual trimmer (trimWidget) for a video layer: two draggable handles over the source video set
  // the in-point (l.trim) and out-point (l.trim+l.dur). Dragging seeks the <video> so the frame you'll cut on
  // is on screen. Natural duration comes from the element's metadata — no guessing a number.
  function bindTrim(root, l){
    const wrap=root.querySelector('#mb-trim'); if(!wrap) return;
    const vid=wrap.querySelector('.mb-trim-vid');
    const track=wrap.querySelector('#mb-trim-track');
    const inH=wrap.querySelector('.mb-trim-in'), outH=wrap.querySelector('.mb-trim-out');
    const selEl=wrap.querySelector('.mb-trim-sel');
    const dimL=wrap.querySelector('.mb-trim-dim-l'), dimR=wrap.querySelector('.mb-trim-dim-r');
    const tin=root.querySelector('.mb-trim-tin'), tout=root.querySelector('.mb-trim-tout'), tlen=root.querySelector('.mb-trim-len');
    let D=0;   // natural duration of the source, once metadata loads
    const fmt=(s)=>{ s=Math.max(0,s||0); const m=Math.floor(s/60), ss=Math.floor(s%60); return m+':'+String(ss).padStart(2,'0'); };
    function paint(){
      if(!D) return;
      const inT=clamp(l.trim,0,D), outT=clamp((+l.trim||0)+(+l.dur||0),0,D);
      const a=inT/D*100, b=outT/D*100;
      inH.style.left=a+'%'; outH.style.left=b+'%';
      selEl.style.left=a+'%'; selEl.style.width=Math.max(0,b-a)+'%';
      dimL.style.width=a+'%'; dimR.style.left=b+'%'; dimR.style.width=Math.max(0,100-b)+'%';
      if(tin) tin.textContent=fmt(inT);
      if(tout) tout.textContent=fmt(outT);
      if(tlen) tlen.textContent=(outT-inT).toFixed(1)+'s';
    }
    function ready(){
      D=vid.duration||0;
      if(!D || !isFinite(D)) return;
      // A trim/length carried over from a different (or mis-measured) source can point past the end — pull it
      // back into range so the handles are always on the bar and the render can't ask ffmpeg for empty frames.
      if((+l.trim||0)>=D){ l.trim=0; }
      if((+l.trim||0)+(+l.dur||0)>D || !(+l.dur>0)){ l.dur=+(D-(+l.trim||0)).toFixed(2); save(); }
      paint();
    }
    if(vid.readyState>=1 && vid.duration) ready();
    else vid.addEventListener('loadedmetadata', ready, {once:true});

    function grab(handle, isIn){
      handle.addEventListener('pointerdown',(e)=>{
        if(!D) return;
        e.preventDefault();
        try{ handle.setPointerCapture(e.pointerId); }catch(_){}
        const rect=track.getBoundingClientRect();
        const move=(ev)=>{
          let t=clamp((ev.clientX-rect.left)/rect.width,0,1)*D;
          if(isIn){
            const out=(+l.trim||0)+(+l.dur||0);
            t=Math.max(0, Math.min(t, out-0.1));   // in-point stays left of the out-point
            l.trim=+t.toFixed(2); l.dur=+(out-t).toFixed(2);
          } else {
            t=Math.min(D, Math.max(t, (+l.trim||0)+0.1)); // out-point stays right of the in-point
            l.dur=+(t-(+l.trim||0)).toFixed(2);
          }
          try{ vid.currentTime=t; }catch(_){}   // show the frame under the handle
          paint();
        };
        const up=()=>{
          document.removeEventListener('pointermove',move);
          document.removeEventListener('pointerup',up);
          save();
          repaint('timeline');   // the clip bar + project length reflect the new duration
        };
        document.addEventListener('pointermove',move);
        document.addEventListener('pointerup',up);
      });
    }
    grab(inH, true); grab(outH, false);
  }

  function render(){
    const feed=document.getElementById('feed'); if(!feed) return;
    if(_playT){ clearInterval(_playT); _playT=null; }
    // Keep the playhead where it was. A rebuild used to snap back to t=0, so any edit (Fill, restack,
    // trim…) jumped the preview to whatever clip happens to be at zero — it looked like the app had
    // selected a different layer out of nowhere.
    const _prevT = (()=>{ const s=document.getElementById('mb-scrub'); return s ? +s.value||0 : null; })();
    pauseAudio();          // BEFORE the rebuild — after it, these elements are detached and unreachable
    feed.innerHTML=view();
    _audioEls = Array.from(feed.querySelectorAll('#mb-audios audio'));
    if(_prevT){ const s=feed.querySelector('#mb-scrub'); if(s) s.value=_prevT.toFixed(2); setTimeout(()=>seek(_prevT),0); }
    const root=feed;
    bindStage(root); bindTimeline(root); bindInspector(root);
    const on=(id,ev,fn)=>{ const e=root.querySelector('#'+id); if(e) e.addEventListener(ev,fn); };
    on('mb-add-media','click',pickMedia);
    on('mb-add-text','click',()=>{ addLayer('text'); render(); });
    on('mb-add-audio','click',pickAudio);
    on('mb-add-blossom','click',pickBlossom);
    on('mb-save','click',saveProject);
    on('mb-open','click',openProject);
    // Explicit "snap everything back-to-back", in the clips' current time order. This used to happen
    // automatically on every drop, which made adjusting one clip rewrite the whole timeline.
    on('mb-arrange','click',()=>{ resequence(); save(); render(); toast('clips laid back-to-back'); });
    // Start over. Only the LAYERS go — the canvas size/background you picked are settings, not content,
    // and having them reset too would mean re-choosing the preset after every clear.
    on('mb-clear','click',async ()=>{
      if(!P.layers.length){ toast('nothing to clear'); return; }
      if(!await uiConfirm(`Remove all ${P.layers.length} layer${P.layers.length===1?'':'s'}? This can’t be undone.`)) return;
      stopPlay(true);
      P.layers=[]; sel=null; save(); render();
      toast('all layers cleared');
    });
    on('mb-render','click',doRender);
    on('mb-play','click',()=>togglePlay());
    on('mb-scrub','input',(e)=>seek(+e.target.value));
    on('mb-size','change',(e)=>{ const [w,h]=e.target.value.split('x').map(Number); P.w=w; P.h=h; save(); render(); });
    root.querySelectorAll('.mb-track').forEach(t=>t.addEventListener('click',(e)=>{
      if(!e.target.closest('.mb-clip')) selectLayer(t.dataset.id); }));
    seek(0);
  }

  boot();
})();

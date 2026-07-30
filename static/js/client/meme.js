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
  let PC = null, toast, uploadBlob, selfProof, uiConfirm, uiPrompt, ME;
  function boot(){
    PC = window.__PC;
    if(!PC) return setTimeout(boot, 50);
    ({ toast, uploadBlob, selfProof, uiConfirm, uiPrompt } = PC);
    bindKeys();          // ONCE, on the document — see bindKeys for why not per render()
    window.PCMeme = {
      render(){ ME = PC.ME; P = load(); render(); },
      // Persist on the way OUT too. Every edit already saves, but leaving the view is exactly when a
      // missed save becomes 'my project came back different', so make it unconditional.
      unmount(){ try{ stopPlay(false); }catch(_){ if(_playT){ clearInterval(_playT); _playT=null; } } try{ if(P) save(); }catch(_){ } },
      reset(){ P = blank(); sel=null; save(); render(); },
      // Seed a layer from a URL — how a post gets "opened in" the Meme Builder. Mirrors what the
      // Blossom picker does (addLayer takes the url directly), but callable from outside the module.
      // ME/P are loaded first because this can arrive before render() has ever run for this session.
      // `from` = {id, pk} of the post this media came off, when the builder was opened FROM a post.
      // Kept on the project (so it survives a reload like everything else) and spent by showResult's
      // reply button. Last opened post wins on purpose: a sticky target would mean a post from an
      // abandoned build silently becomes the reply for the next one, which is the worse surprise.
      addMedia(url, type, from){
        if(!url) return false;
        try{
          ME = PC.ME; if(!P) P = load();
          addLayer(/^video\//.test(type||'') ? 'video' : 'image', url);
          if(from && from.id) P.replyTo = { id: from.id, pk: from.pk || '' };
          save(); render();
          return true;
        }catch(e){ return false; }
      },
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
  // The subset of effects that can be rendered onto a TRANSPARENT canvas (/client/meme/effects: the
  // dancing man, the shrug, the character overlays). Those are the ones that can stand on their own as a
  // LAYER — everything in EFFECTS above needs a base image to transform, which is why these are a
  // separate, shorter list and not a filter over that one. Filtered server-side to the assets that
  // actually resolve on this node, so the picker never offers a broken pick.
  let ALPHA_FX = [];
  fetch('/client/meme/effects').then(r=>r.json()).then(j=>{ ALPHA_FX=(j&&j.effects)||[]; }).catch(()=>{});
  const PRESETS = [
    ['9:16', 720, 1280], ['1:1', 1080, 1080], ['16:9', 1280, 720], ['4:5', 864, 1080],
  ];

  let P = null;          // the project (edit list)
  let sel = null;        // selected layer id
  let _rendering = false;// a render is in flight — survives view repaints, unlike the button's disabled flag
  let _uid = 0;
  const nid = () => 'L' + (++_uid) + Math.random().toString(36).slice(2, 6);

  function blank(){
    return { name:'', w:720, h:1280, fps:30, bg:'#000000', duration:6, layers:[] };
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

  // ---------- undo / redo ----------
  // Snapshots of the WHOLE project as JSON, not per-action inverses. P is already fully serializable
  // (it is exactly what localStorage holds and what Save writes to Blossom), so undo is "restore a
  // string" — there is no inverse operation to get wrong, and any feature added later is covered the
  // moment it calls snap(). Media is referenced by URL, so a snapshot is a few KB whatever is on the
  // timeline. This is the safety net that makes the destructive actions (delete, Clear all, applying
  // an effect over a layer's source, reshaping the canvas) safe to try at all.
  const HIST_MAX = 40;
  let _hist = [], _future = [], _burstTag = '', _burstAt = 0;
  // A COMPOUND edit (a template lays down two captions and re-boxes two clips) is one thing the user did,
  // so it must be one undo step. Take a snapshot, raise this, build, lower it — every snap() inside is a
  // no-op and the single outer snapshot is what Ctrl+Z returns to. See groupEdit.
  let _snapOff = 0;
  function groupEdit(fn){
    snap(); _snapOff++;
    try{ fn(); } finally { _snapOff--; }
  }
  // ALWAYS call before mutating — the stack holds the state you are leaving, not the one you arrive at.
  function snap(){
    if(_snapOff) return;
    try{
      const s = JSON.stringify(P);
      if(_hist.length && _hist[_hist.length-1] === s) return;   // nothing actually changed
      _hist.push(s);
      if(_hist.length > HIST_MAX) _hist.shift();
      _future.length = 0;            // a fresh edit forks the timeline; the old redo path is gone
      _burstTag = '';
      _syncHistBtns();
    }catch(_){ }
  }
  // Dragging fires per pointermove and typing per keystroke; ONE undo step per burst is what a person
  // means by "undo that". The same tag within 1.2s folds into the snapshot already taken.
  function snapBurst(tag){
    const now = Date.now();
    if(_burstTag === tag && (now - _burstAt) < 1200){ _burstAt = now; return; }
    snap(); _burstTag = tag; _burstAt = now;
  }
  // Drop the pending snapshot when the gesture turned out to change nothing — a TAP on a clip selects it
  // and a tap on the stage may not move a pixel, and a history entry identical to the present state makes
  // the next Ctrl+Z a visible no-op, which reads as "undo is broken". Safe to call unconditionally: a
  // gesture that did change something has a different string and keeps its snapshot.
  // Popping is always CORRECT whoever pushed it: an entry equal to the present state is a no-op undo step
  // by definition.
  function unsnapIfUnchanged(){
    try{ if(_hist.length && _hist[_hist.length-1] === JSON.stringify(P)) _hist.pop(); }catch(_){ }
    _syncHistBtns();
  }
  // Plenty of edits deliberately do NOT re-render the view (slider drags, arrow nudges, flip toggles — a
  // full rebuild would restart the video elements), so the ↶/↷ buttons cannot get their enabled state from
  // view() alone: they stayed greyed out after those edits even though Ctrl+Z worked. Update them directly.
  function _syncHistBtns(){
    const u = document.getElementById('mb-undo'), r = document.getElementById('mb-redo');
    if(u) u.disabled = !_hist.length;
    if(r) r.disabled = !_future.length;
  }
  function _restore(s){
    let next = null;
    try{ next = JSON.parse(s); }catch(_){ return; }
    if(!next || !Array.isArray(next.layers)) return;
    P = next;
    if(!P.layers.some(x => x.id === sel)) sel = null;   // the selected layer may not exist in this state
    save(); render();
  }
  function undo(){
    if(!_hist.length){ toast('nothing left to undo'); return; }
    try{ _future.push(JSON.stringify(P)); }catch(_){ }
    _burstTag = '';
    _restore(_hist.pop());
  }
  function redo(){
    if(!_future.length){ toast('nothing to redo'); return; }
    try{ _hist.push(JSON.stringify(P)); }catch(_){ }
    _burstTag = '';
    _restore(_future.pop());
  }

  // ---------- keyboard ----------
  // BARE keys here, not the app's Alt+<letter>: the global shortcuts are all Alt-modified (see SHORTCUTS
  // in app.js) so there is no collision, and an editor without Space/Delete/arrows/Ctrl+Z feels broken no
  // matter how good the mouse story is. Registered ONCE from boot() on the document — render() rebuilds
  // the entire view on nearly every edit, so a per-render listener would stack up a copy per repaint and
  // a single Delete would remove a dozen layers.
  const _typing = (t) => !!t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '') || t.isContentEditable);
  function bindKeys(){
    document.addEventListener('keydown', (e)=>{
      if(!P || !document.getElementById('mb-stage')) return;    // not in the Meme Builder
      if(_typing(e.target)) return;                             // they meant to type it
      if(e.altKey) return;                                      // Alt+<letter> belongs to the app
      // A modal (effect picker, Open, templates) or a uiConfirm is on top — its own keys win, and
      // Escape there must close IT, not deselect a layer behind it.
      if(document.querySelector('#modal-root .modal-bg, .uiconfirm-bg')) return;
      const k = e.key, ctrl = e.ctrlKey || e.metaKey;
      const l = P.layers.find(x => x.id === sel);
      if(ctrl){
        if(k === 'z' || k === 'Z'){ e.preventDefault(); (e.shiftKey ? redo : undo)(); return; }
        if(k === 'y' || k === 'Y'){ e.preventDefault(); redo(); return; }
        if(k === 'd' || k === 'D'){ e.preventDefault(); if(l) duplicateLayer(l); return; }
        return;                       // every other Ctrl combo stays the browser's (copy, find, reload)
      }
      // Space plays/pauses — EXCEPT on a focused button or link, where Space is how the keyboard presses it.
      // Rarely in the way: almost every toolbar action re-renders the view, which destroys the button it was
      // fired from and hands focus back to the body.
      if(k === ' ' || k === 'Spacebar'){
        if(/^(BUTTON|A)$/.test(e.target && e.target.tagName || '')) return;
        e.preventDefault(); togglePlay(); return;
      }
      if(k === 'Escape'){ if(sel){ e.preventDefault(); sel = null; render(); } return; }
      if(k === 'Delete' || k === 'Backspace'){
        if(!l) return;
        e.preventDefault();
        snap();
        P.layers = P.layers.filter(x => x.id !== l.id); sel = null; save(); render();
        toast('layer deleted — Ctrl+Z to undo');       // no confirm: undo is the better answer
        return;
      }
      if(k.indexOf('Arrow') === 0){
        if(!l || l.type === 'audio') return;           // a music bed has no position to nudge
        if(l.type === 'text' && _alignOf(l) === 'center' && (k === 'ArrowLeft' || k === 'ArrowRight')){
          toast('centred caption — switch off ⇔ Centre to move it sideways'); return;
        }
        e.preventDefault();
        const step = e.shiftKey ? 10 : 1;              // Shift = the coarse nudge, like every editor
        snapBurst('nudge:' + l.id);                    // BEFORE the mutation
        if(k === 'ArrowLeft' || k === 'ArrowRight') l.x = Math.round((+l.x||0) + (k === 'ArrowRight' ? step : -step));
        else l.y = Math.round((+l.y||0) + (k === 'ArrowDown' ? step : -step));
        save();
        // Move the element in place rather than repaint('inspector'): a repaint would rebuild the video
        // trim widget (restarting the clip) on every keypress, and x/y are not inspector inputs anyway.
        const it = document.querySelector('.mb-item[data-id="' + l.id + '"]');
        if(it) applyGeom(it, l);
        return;
      }
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
        // Measure only when it is actually laid out; a hidden layer falls through to the font measurement
        // below (NOT to null: with the renderer centring each line on cx, no cx means the lines are drawn
        // left-anchored instead — so a wrapped caption that happens to be outside the playhead's window at
        // render time would export left-aligned while the preview showed it centred).
        if(r.width){
          const k = P.w / st.getBoundingClientRect().width;
          return Math.round((+l.x||0) + r.width * k / 2);
        }
      }
    }catch(_){ }
    // Same canvas metrics the wrapper uses, in project pixels already — the widest line IS the box width.
    try{
      const size = +l.size || 64;
      const w = wrapText(l).split('\n').reduce((m, ln)=>Math.max(m, _measure(ln, size)), 0);
      if(w) return Math.round((+l.x||0) + w/2);
    }catch(_){ }
    return null;
  }

  const _alignOf = (l) => (l.align || '');   // '' = obey the x you dragged it to; 'center' = let ffmpeg centre it

  // ---------- caption word-wrap ----------
  // drawtext NEVER wraps, so a caption longer than the frame used to run straight off the edge of the
  // exported video with nothing on screen to warn you (the preview is white-space:pre and max-content
  // wide, so it overflowed the stage the same way and simply got clipped). The renderer already draws
  // ONE drawtext PER LINE of whatever text it is handed (see _line_dy in meme_builder_service), so the
  // wrapping belongs here — and it has to be computed ONCE and used for BOTH the preview and the render
  // payload, or the preview is lying again.
  //
  // Measured with a canvas 2D context in the SAME font/weight the preview and ffmpeg use, so the break
  // points are the ones drawtext would have chosen at that size.
  let _measCtx = null;
  function _measure(s, px){
    try{
      if(!_measCtx) _measCtx = document.createElement('canvas').getContext('2d');
      _measCtx.font = '700 ' + px + 'px PCMemeFont, "Liberation Sans", Helvetica, Arial, sans-serif';
      return _measCtx.measureText(s).width;
    }catch(_){ return String(s).length * px * 0.5; }   // no canvas → a rough guess beats not wrapping
  }
  // Percentage of the canvas width a caption may fill before it wraps. 92% leaves a margin on both
  // sides, which is what a caption wants; the inspector exposes it per layer.
  const _wrapPct = (l) => clamp(l.wrapPct == null ? 92 : l.wrapPct, 20, 100);
  // `wrap:false` opts a layer out entirely (a deliberate one-liner you want to overflow). Anything else —
  // including a layer saved before this existed — wraps: a caption that already fits is untouched by
  // wrapping, and one that doesn't was broken, so ON is the safe default for old projects too.
  function wrapText(l){
    const raw = String(l.text == null ? '' : l.text);
    if(l.wrap === false || !raw) return raw;
    const max = P.w * _wrapPct(l) / 100;
    const size = +l.size || 64;
    const out = [];
    raw.replace(/\r\n?/g, '\n').split('\n').forEach(para=>{
      let line = '';
      para.split(' ').forEach(word=>{
        if(!line && _measure(word, size) > max){
          // A single word wider than the frame (a URL, a keysmash) — break it by character, because
          // leaving it unwrapped is the exact bug this function exists to fix.
          let chunk = '';
          for(const ch of word){
            if(chunk && _measure(chunk + ch, size) > max){ out.push(chunk); chunk = ch; }
            else chunk += ch;
          }
          line = chunk;
          return;
        }
        const cand = line ? line + ' ' + word : word;
        if(line && _measure(cand, size) > max){ out.push(line); line = word; }
        else line = cand;
      });
      out.push(line);
    });
    return out.join('\n');
  }
  // A caption's preview markup: ONE inline-block span per line, separated by a real newline (the parent
  // is white-space:pre, so that is the line break). Per-line spans are what make the optional background
  // box match the export — drawtext's box=1 draws a box around EACH line's own ink, and a background on
  // the whole block would instead be one rectangle the width of the longest line. The box is painted with
  // box-shadow rather than padding, so it expands the paint area WITHOUT moving the text (which is exactly
  // what drawtext's boxborderw does).
  function _textInnerHTML(l){
    const size = +l.size || 64;
    const bx = l.box ? `background:${enc(_boxColor(l))};box-shadow:0 0 0 ${(Math.max(4, size/5)/P.w*100).toFixed(3)}cqw ${enc(_boxColor(l))};` : '';
    return wrapText(l).split('\n')
      .map(ln => `<span class="mb-tline" style="${bx}">${enc(ln || ' ')}</span>`).join('\n');
  }
  // The box colour as a CSS rgba(), from the same hex + alpha pair the renderer gets.
  function _boxColor(l){
    const hex = /^#[0-9a-fA-F]{6}$/.test(l.boxColor || '') ? l.boxColor : '#000000';
    const a = clamp(l.boxAlpha == null ? 0.55 : l.boxAlpha, 0, 1);
    return `rgba(${parseInt(hex.slice(1,3),16)},${parseInt(hex.slice(3,5),16)},${parseInt(hex.slice(5,7),16)},${a})`;
  }

  const _stageOrder = () => P.layers.filter(_isVisual).concat(P.layers.filter(l=>l.type==='text'));

  function addLayer(type, src, extra){
    if(P.layers.length >= 24){ toast('24 layers is the limit'); return null; }
    snap();
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
      flipH: false, flipV: false, rotate: 0,
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

  // Copy a layer into a new one sitting directly ABOVE it in the draw order. Everything carries over —
  // source, size, trim, effect, sound, opacity, flip/rotate and the timeline slot — because that is the
  // whole point: you built one layer up and want a second like it (two of the same character on screen, a
  // caption in the same font, the same clip again with a different trim). Only two things change:
  //   * a fresh id (the id IS the identity everywhere — selection, stage/timeline lookups, seek), and
  //   * a small nudge, since a pixel-perfect copy hides exactly behind the original and reads as
  //     "the button did nothing". A caption drops a line (its own font size); media shifts 4%/2% of the
  //     canvas. A centred caption is nudged in Y only — the renderer computes its x, so moving it does
  //     nothing and the two would land on top of each other in the export.
  // Insert at i+1 rather than the end so a duplicated clip cannot jump above a caption (text lives at the
  // tail of P.layers), and no re-timing happens: the copy overlaps the original until you drag it.
  function duplicateLayer(l){
    if(!l) return null;
    if(P.layers.length >= 24){ toast('24 layers is the limit'); return null; }
    snap();
    const c = Object.assign({}, l, { id: nid() });
    if(c.type === 'text'){
      if(_alignOf(c) !== 'center') c.x = clamp(c.x + Math.round(P.w*0.04), 0, Math.max(0, P.w-16));
      c.y = clamp(c.y + Math.round((+c.size||64) * 1.2), 0, Math.max(0, P.h-16));
    } else if(_isVisual(c)){
      c.x = clamp(c.x + Math.round(P.w*0.04), 0, Math.max(0, P.w-16));
      c.y = clamp(c.y + Math.round(P.h*0.02), 0, Math.max(0, P.h-16));
    }
    const i = P.layers.indexOf(l);
    P.layers.splice(i < 0 ? P.layers.length : i+1, 0, c);
    sel = c.id;
    save(); render();
    toast(c.type==='audio' ? 'track duplicated' : 'layer duplicated — drag it where you want it');
    return c;
  }

  // ---------- rendering the UI ----------
  function view(){
    return `
    <div class="mb-wrap">
      <!-- TWO rows, split by what they do: everything that ADDS a layer on the first, everything that acts
           on the PROJECT on the second. One row of eleven buttons wrapped into an unreadable block on a
           phone, and put "Clear all" next to "Add text". -->
      <div class="mb-bar">
        <button class="btn btn-neon small" id="mb-add-media">🖼️ Add media</button>
        <button class="btn btn-cyan small" id="mb-add-text">🅣 Add text</button>
        <button class="btn btn-cyan small" id="mb-add-audio" title="Add a music track under the whole meme">🎵 Add music</button>
        <button class="btn btn-cyan small" id="mb-add-effect" title="Add an effect (dancing man, shrug, a character) as its own layer you can drag, resize and time">✨ Add effect</button>
        <button class="btn btn-cyan small" id="mb-add-blossom">🌸 From Blossom</button>
        <button class="btn btn-cyan small" id="mb-tpl" title="Start from a ready-made layout — classic top/bottom captions, a two-panel split, a caption bar">📐 Templates</button>
      </div>
      <div class="mb-bar">
        <button class="btn btn-cyan small mb-icon" id="mb-undo" title="Undo (Ctrl+Z)" aria-label="Undo" ${_hist.length?'':'disabled'}>↶</button>
        <button class="btn btn-cyan small mb-icon" id="mb-redo" title="Redo (Ctrl+Shift+Z)" aria-label="Redo" ${_future.length?'':'disabled'}>↷</button>
        <button class="btn btn-cyan small" id="mb-proj" title="Save, open, rename or start a new project">📂 ${enc(P.name || 'Untitled')}</button>
        <button class="btn btn-cyan small" id="mb-arrange" title="Lay every clip back-to-back in its current order">⇄ Arrange</button>
        <select class="input mb-size" id="mb-size" aria-label="Canvas size">
          ${PRESETS.map(([n,w,h])=>`<option value="${w}x${h}" ${P.w===w&&P.h===h?'selected':''}>${n}</option>`).join('')}
          ${PRESETS.some(([n,w,h])=>P.w===w&&P.h===h) ? '' :
            `<option value="${P.w}x${P.h}" selected>${P.w}×${P.h}</option>`}
        </select>
        <label class="mb-bgpick" title="Canvas background — this is what shows AROUND a photo that doesn't fill the frame (the bars). It was always black with no way to change it.">
          <input type="color" id="mb-bg" value="${enc(P.bg)}" aria-label="Canvas background colour">
        </label>
        <span class="mb-spacer"></span>
        <span class="muted small" id="mb-status"></span>
        <button class="btn btn-neon small" id="mb-render">🎬 Render</button>
      </div>

      <div class="mb-main">
        <div class="mb-stagewrap">
          <div class="mb-stage" id="mb-stage" style="aspect-ratio:${P.w}/${P.h};background:${P.bg}">
            ${_stageOrder().map(stageEl).join('')}
            <!-- Snap guides: shown only while a drag is actually snapped to that line (see applySnaps). -->
            <i class="mb-guide mb-gv" id="mb-gv" style="display:none"></i>
            <i class="mb-guide mb-gh" id="mb-gh" style="display:none"></i>
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

      ${P.layers.some(_isVisual) ? '<div class="muted small mb-tlhint">Drag a clip along the timeline to move it, or its edges to trim. Tap the ruler to move the playhead.</div>' : ''}
      <div class="mb-timeline" id="mb-timeline">${timelineInner()}</div>
      <div id="mb-result"></div>
    </div>`;
  }

  // The timeline's scrolling CONTENT. Everything that has to line up with a clip lane — the ruler and the
  // playhead — lives in here rather than over the .mb-timeline box, because on a phone the box scrolls
  // horizontally: a playhead positioned against the outer element would slide away from the lanes it is
  // supposed to be marking the moment you scrolled.
  function timelineInner(){
    if(!P.layers.length) return '<div class="mb-tlinner"><div class="muted small mb-empty">No layers yet — add media or text to start.</div></div>';
    return `<div class="mb-tlinner" id="mb-tlinner">
      ${rulerEl()}
      ${_rowOrder().map(trackEl).join('')}
      <i class="mb-ph" id="mb-ph"><b></b></i>
    </div>`;
  }

  // A seconds ruler above the tracks. Built as a .mb-track so its lane is the SAME box as every clip lane
  // (same name-column width, same gap) — that is what makes a tick at 3s sit exactly above the clip that
  // starts at 3s, with no measuring. Tick spacing steps up with the project length so the labels never
  // collide: 1s up to 12s, then 2s, then 5s.
  function rulerEl(){
    const total = Math.max(projEnd(), 1);
    const step = total <= 12 ? 1 : (total <= 30 ? 2 : 5);
    const ticks = [];
    for(let t = 0; t <= total + 0.001; t += step){
      const pct = t/total*100;
      // The last label has to hang to the LEFT of its line or it is cut off by the edge of the timeline
      // (which on a phone is a scroll edge, so it is simply never readable).
      const lbl = pct > 98 ? ' class="mb-tickr"' : '';
      ticks.push(`<i class="mb-tick" style="left:${pct.toFixed(3)}%"><b${lbl}>${t % 1 ? t.toFixed(1) : t}s</b></i>`);
    }
    return `<div class="mb-track mb-rrow">
      <div class="mb-trackname mb-rname"></div>
      <div class="mb-lane mb-rlane" id="mb-rlane" title="Tap or drag to move the playhead">${ticks.join('')}</div>
    </div>`;
  }

  // Put the playhead over the lanes at time t. Position comes from a real lane's offset inside .mb-tlinner
  // rather than from the CSS name-column width, so it stays correct when that width changes at the 820px
  // breakpoint (and if it ever changes again).
  function paintPlayhead(t){
    const inner = document.getElementById('mb-tlinner'), ph = document.getElementById('mb-ph');
    if(!inner || !ph) return;
    const lane = inner.querySelector('.mb-lane');
    if(!lane){ ph.style.display='none'; return; }
    const total = Math.max(projEnd(), 1);
    const frac = clamp(t, 0, total) / total;
    ph.style.display = '';
    ph.style.left = (lane.offsetLeft + frac * lane.offsetWidth) + 'px';
  }

  // Tap or drag anywhere on the ruler to move the playhead — the standard way to scrub a timeline, and
  // until now the ONLY scrubber was the slider above the stage, in a different part of the page.
  function _rulerSeek(clientX){
    const lane = document.getElementById('mb-rlane'); if(!lane) return;
    const r = lane.getBoundingClientRect(); if(!r.width) return;
    const t = clamp((clientX - r.left) / r.width, 0, 1) * Math.max(projEnd(), 1);
    const s = document.getElementById('mb-scrub'); if(s) s.value = t.toFixed(2);
    seek(t);
  }

  function stageEl(l){
    const s = l.id===sel ? ' sel' : '';
    const pos = `left:${(l.x/P.w*100).toFixed(3)}%;top:${(l.y/P.h*100).toFixed(3)}%;`;
    if(l.type==='text'){
      // Centred captions span the full width and centre their text, mirroring drawtext's (w-text_w)/2.
      const cpos = _alignOf(l)==='center' ? `left:50%;top:${(l.y/P.h*100).toFixed(3)}%;` : pos;   // .centred shifts back by half its width
      // Drop shadow, matching the renderer's shadowx/shadowy (size/18, black at 65%).
      const sh = l.shadow ? `text-shadow:${(Math.max(2,l.size/18)/P.w*100).toFixed(3)}cqw ${(Math.max(2,l.size/18)/P.w*100).toFixed(3)}cqw 0 rgba(0,0,0,.65);` : '';
      return `<div class="mb-item mb-text${_alignOf(l)==='center'?' centred':''}${s}" data-id="${l.id}" style="${cpos}font-size:${(l.size/P.w*100).toFixed(3)}cqw;color:${enc(l.color)};-webkit-text-stroke:.03em ${enc(l.stroke)};${sh}opacity:${l.opacity}">${_textInnerHTML(l)}<i class="mb-h"></i></div>`;
    }
    const size = `width:${(l.w/P.w*100).toFixed(3)}%;height:${(l.h/P.h*100).toFixed(3)}%;`;
    // Mirror the renderer's mirror+rotate. Order matters and CSS applies transforms RIGHT to LEFT, so
    // `rotate(...) scaleX(-1) scaleY(-1)` runs flip-then-rotate — the same order as the ffmpeg chain
    // (hflip/vflip, then rotate). Put it on the IMG, not on .mb-item, so the layer's box and its resize
    // handle stay axis-aligned and still grab where you expect. .mb-item has no overflow:hidden, so the
    // corners spill out exactly as the renderer's rotw()/roth() growth allows.
    // object-fit must mirror the renderer: 'cover' fills the box and crops, 'contain' letterboxes.
    const ofit = (l.fit==='cover') ? 'cover' : 'contain';
    // `#t=0.1` (a media fragment) makes the browser seek to 0.1s and DISPLAY that frame as a poster the
    // moment the layer mounts — otherwise `preload` alone decodes nothing and the clip shows blank until
    // you scrub/play (the "I have to render to see the effect" bug). preload=auto so the frame loads eagerly.
    const inner = l.type==='video'
      ? `<video src="${enc(l.src)}#t=0.1" muted playsinline preload="auto" style="object-fit:${ofit}${_xformCss(l)}"></video>`
      : `<img src="${enc(l.src)}" alt="" style="object-fit:${ofit}${_xformCss(l)}">`;
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

  // CSS equivalent of the renderer's flip+rotate for this layer ('' when it is untouched).
  function _xformCss(l){
    const t=_xform(l); return t ? `;transform:${t};transform-origin:center` : '';
  }
  function _xform(l){
    const p=[]; const r=+l.rotate||0;
    if(r) p.push(`rotate(${r}deg)`);
    if(l.flipH) p.push('scaleX(-1)');
    if(l.flipV) p.push('scaleY(-1)');
    return p.join(' ');
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
        <span class="mb-insp-acts">
          <button class="btn btn-cyan small" id="mb-dup" title="Make a second copy of this track">⧉ Duplicate</button>
          <button class="btn btn-danger small" id="mb-del">🗑️ Delete</button>
        </span>
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
        <span class="mb-insp-acts">
          <button class="btn btn-cyan small" id="mb-dup" title="Copy this layer — same clip, size, effect, sound and timing — as a new layer just above it">⧉ Duplicate</button>
          <button class="btn btn-danger small" id="mb-del">🗑️ Delete</button>
        </span>
      </div>
      ${isText ? `
        <label class="mb-f"><span>Text</span><textarea class="input" id="mb-f-text" rows="2">${enc(l.text)}</textarea></label>
        <label class="mb-f"><span>Size</span><input class="input" type="number" id="mb-f-size" min="8" max="400" value="${l.size}"></label>
        <div class="mb-frow">
          <label class="mb-f"><span>Colour</span><input type="color" id="mb-f-color" value="${enc(l.color)}"></label>
          <label class="mb-f"><span>Outline</span><input type="color" id="mb-f-stroke" value="${enc(l.stroke)}"></label>
        </div>
        <button class="btn btn-cyan small full${_alignOf(l)==='center'?' on':''}" id="mb-center">⇔ Centre horizontally</button>
        ${alignGrid()}
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-wrap" ${l.wrap===false?'':'checked'}><span>Wrap long lines</span></label>
        ${l.wrap===false ? '' : `<label class="mb-f"><span>Wrap width <b>${_wrapPct(l)}%</b> of the frame</span><input type="range" id="mb-f-wrappct" min="20" max="100" step="1" value="${_wrapPct(l)}"></label>`}
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-box" ${l.box?'checked':''}><span>Background box</span></label>
        ${l.box ? `<div class="mb-frow">
          <label class="mb-f"><span>Box colour</span><input type="color" id="mb-f-boxcolor" value="${enc(/^#[0-9a-fA-F]{6}$/.test(l.boxColor||'')?l.boxColor:'#000000')}"></label>
          <label class="mb-f"><span>Box opacity</span><input type="range" id="mb-f-boxalpha" min="0.1" max="1" step="0.05" value="${l.boxAlpha==null?0.55:l.boxAlpha}"></label>
        </div>` : ''}
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-shadow" ${l.shadow?'checked':''}><span>Drop shadow</span></label>
        <div class="muted small mb-dbg">x=${Math.round(l.x)} y=${Math.round(l.y)} size=${Math.round(l.size)} align=${_alignOf(l)||"free"} · canvas ${P.w}×${P.h}</div>` : `
        <label class="mb-f"><span>Layer name</span><input class="input" id="mb-f-name" maxlength="24" placeholder="${enc(srcName(l.src))}" value="${enc(l.name||'')}"></label>
        <div class="mb-frow">
          <label class="mb-f"><span>W</span><input class="input" type="number" id="mb-f-w" value="${Math.round(l.w)}"></label>
          <label class="mb-f"><span>H</span><input class="input" type="number" id="mb-f-h" value="${Math.round(l.h)}"></label>
        </div>
        <div class="mb-frow"><button class="btn btn-cyan small" id="mb-fit" title="Show the whole photo inside the canvas. Bars appear wherever its shape differs from the canvas — they are the canvas background.">⛶ Whole photo (bars)</button><button class="btn btn-cyan small" id="mb-fill" title="Scale up until the canvas is full and crop the overflow — no bars, but the edges are cut off">✂ Fill &amp; crop</button></div>
        <button class="btn btn-cyan small full" id="mb-canvas-match" title="Reshape the CANVAS to this photo — the third option: no bars AND nothing cropped">⇲ Canvas to this photo</button>
        ${alignGrid()}
        ${l.origSrc ? `<button class="btn btn-cyan small full" id="mb-fx-revert" title="Put this layer's original picture back — the effect that replaced it is undone">↺ Undo the effect on this layer</button>` : ''}
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
      <div class="mb-frow"><button class="btn btn-cyan small${l.flipH?' on':''}" id="mb-fliph" title="Mirror left-to-right">⇄ Flip</button><button class="btn btn-cyan small${l.flipV?' on':''}" id="mb-flipv" title="Mirror top-to-bottom">⇅ Flip</button><button class="btn btn-cyan small" id="mb-rot0" title="Back to upright">⌾ 0°</button></div>
      <label class="mb-f"><span>Rotate <b id="mb-rot-val">${Math.round(+l.rotate||0)}°</b></span><input type="range" id="mb-f-rot" min="-180" max="180" step="1" value="${Math.round(+l.rotate||0)}"></label>
      <div class="mb-order">
        <button class="btn btn-cyan small" id="mb-back">⬇︎ Send back</button>
        <button class="btn btn-cyan small" id="mb-front">⬆︎ Bring front</button>
      </div>`;
  }

  // ---------- align to the canvas ----------
  // Nine one-tap positions. Dragging alone can never land a layer EXACTLY on an edge or dead centre, and
  // on a phone it is not close: the finger covers the layer you are placing. Same grid for media and for
  // captions, with one difference — the middle column of a caption sets the ffmpeg CENTRE flag instead of
  // computing an x, because (w-text_w)/2 is exact by construction and a measured pixel x is not.
  const _AGRID = [
    ['0','0','↖ top left'], ['0.5','0','↑ top centre'], ['1','0','↗ top right'],
    ['0','0.5','← left'],   ['0.5','0.5','⊙ centre'],   ['1','0.5','→ right'],
    ['0','1','↙ bottom left'], ['0.5','1','↓ bottom centre'], ['1','1','↘ bottom right'],
  ];
  function alignGrid(){
    return `<div class="mb-f"><span>Snap to the canvas</span><div class="mb-align">`
      + _AGRID.map(([h,v,t])=>`<button class="mb-ab" data-h="${h}" data-v="${v}" title="${enc(t)}">${enc(t.slice(0,1))}</button>`).join('')
      + `</div></div>`;
  }
  // The layer's size in PROJECT pixels. Media carries it (w/h); a caption's size is whatever its glyphs
  // measure, so it comes off the preview element — which is laid out only while the playhead is inside
  // the layer's window (seek() hides the rest), and selecting a layer seeks there, so by the time this
  // panel is on screen the measurement is real. The font-derived fallback keeps it sane if it isn't.
  function _layerBox(l){
    if(_isVisual(l)) return { w: +l.w||0, h: +l.h||0 };
    try{
      const el = document.querySelector('.mb-item[data-id="'+l.id+'"]');
      const st = document.getElementById('mb-stage');
      if(el && st){
        const r = el.getBoundingClientRect(), sr = st.getBoundingClientRect();
        if(r.width && sr.width) return { w: r.width * (P.w/sr.width), h: r.height * (P.h/sr.height) };
      }
    }catch(_){ }
    const size = +l.size||64, lines = wrapText(l).split('\n').length;
    return { w: Math.min(P.w, size * 0.55 * (l.text||'').length), h: size * lines };
  }
  function alignLayer(l, hx, vy){
    if(!l || l.type==='audio') return;
    snap();
    const b = _layerBox(l);
    if(hx != null){
      if(l.type==='text' && hx === 0.5) l.align = 'center';
      else { if(l.type==='text') l.align = ''; l.x = Math.round((P.w - b.w) * hx); }
    }
    if(vy != null) l.y = Math.round((P.h - b.h) * vy);
    save(); render();
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


  // ---------- templates ----------
  // A blank canvas and an "Add media" button is the hardest screen in the whole builder: the formats people
  // actually want (a caption top and bottom, a two-panel comparison, a caption bar over a clip) are all
  // several correct-in-a-particular-order steps away, and none of them are discoverable. A template is just
  // a prebuilt bit of the same project structure — no server call, nothing new for the renderer — so it is
  // pure ease of use. Each is ONE undo step (groupEdit), and each says up front what it needs.
  //
  // `need` is the number of media layers the layout arranges; a template that only adds captions needs 0
  // and works before you have added anything, so you can write the joke first and drop the picture in after.
  function _caption(text, yFrac, extra){
    const size = Math.max(18, Math.round(P.w / 9));
    const l = addLayer('text', '', Object.assign({ text, size, align: 'center',
      dur: +Math.max(projEnd(), 3).toFixed(2) }, extra || {}));
    // addLayer sets a text layer's x/y AFTER it merges `extra` (that is where the 8% default comes from),
    // so the position has to be written here rather than passed in.
    if(l) l.y = Math.round(P.h * yFrac);
    return l;
  }
  // Lay the first `n` clips out as panels: each gets a box, fills it (cropping), and runs the WHOLE
  // timeline — panels are side by side in space, not one after another in time, so their `start` is 0.
  function _panels(boxes){
    const seq = mediaSeq().slice(0, boxes.length);
    const dur = +Math.max.apply(null, seq.map(l => +l.dur || 0).concat([2])).toFixed(2);
    seq.forEach((l, i)=>{
      Object.assign(l, boxes[i], { fit: 'cover', start: 0, dur });
    });
    return seq.length;
  }
  const TEMPLATES = [
    { label: '🅣 Classic top &amp; bottom captions', need: 0,
      hint: 'Two big centred captions. Add the picture whenever you like — the captions sit over it.',
      apply(){ _caption('TOP TEXT', 0.04); _caption('BOTTOM TEXT', 0.84); } },
    { label: '⬒ Caption bar over the clip', need: 0,
      hint: 'A white bar across the top with black text — the “explain the joke above the picture” format.',
      apply(){ _caption('when you see it', 0.03, { color: '#000000', stroke: '#000000',
        box: true, boxColor: '#ffffff', boxAlpha: 1, size: Math.max(14, Math.round(P.w/16)) }); } },
    { label: '⬓ Two panels, top and bottom', need: 2,
      hint: 'Your first two clips stacked — the comparison format. Both play at once.',
      apply(){ const h = Math.round(P.h/2);
        _panels([{ x:0, y:0, w:P.w, h }, { x:0, y:h, w:P.w, h }]); } },
    { label: '◫ Two panels, side by side', need: 2,
      hint: 'Your first two clips left and right, both playing at once.',
      apply(){ const w = Math.round(P.w/2);
        _panels([{ x:0, y:0, w, h:P.h }, { x:w, y:0, w, h:P.h }]); } },
    { label: '⬛ Full-bleed clip + bottom caption', need: 1,
      hint: 'The first clip fills the frame (edges cropped) with one caption low over it.',
      apply(){ _panels([{ x:0, y:0, w:P.w, h:P.h }]); _caption('caption', 0.8); } },
  ];
  function pickTemplate(){
    const have = mediaSeq().length;
    const rows = TEMPLATES.map((t,i)=>{
      const short = t.need > have;
      return `<button class="btn btn-ghost full mb-tplrow" data-i="${i}" ${short?'disabled':''}>`
        + `<b>${t.label}</b><br><span class="muted small">${enc(t.hint)}`
        + (short ? ` — needs ${t.need} media layer${t.need===1?'':'s'}, you have ${have}` : '') + '</span></button>';
    }).join('');
    PC.modal(`<h3>📐 Start from a layout</h3>
      <div class="muted small" style="margin-bottom:8px">Everything a template makes is an ordinary layer —
      drag, retime and restyle it afterwards. ↶ undo puts it all back.</div>${rows}`, root=>{
      root.querySelectorAll('.mb-tplrow').forEach(btn=>btn.onclick=()=>{
        const t = TEMPLATES[+btn.dataset.i]; if(!t) return;
        PC.closeModal();
        groupEdit(()=>t.apply());
        save(); render();
        toast('layout applied — ↶ undo to drop it');
      });
    });
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
    // Ask for a NAME the first time. Saved projects used to be listed as "3 layers · 720×1280 · <date>",
    // which is unusable the moment you have two of them — you cannot tell which build is which without
    // opening it, and opening it replaces the one you are working on.
    if(!P.name){
      const n = await uiPrompt('Name this project', { value: '', placeholder: 'dog on a skateboard' });
      if(n == null) return;                                  // cancelled — do not save an unnamed blob
      P.name = String(n).slice(0, 60).trim();
      save(); render();
    }
    try{
      const doc = JSON.stringify(Object.assign({ [PROJ_MARK]: 1, savedAt: Math.floor(Date.now()/1000) }, P));
      const f = new File([doc], 'meme-project.json', { type: 'application/json' });
      const url = await uploadBlob(f);
      toast('“' + (P.name || 'project') + '” saved to Blossom');
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
      + `<b>${enc(f.j.name || 'Untitled')}</b><br><span class="muted small">`
      + `${f.j.layers.length} layer${f.j.layers.length===1?'':'s'} · ${f.j.w}×${f.j.h}`
      + (f.j.savedAt ? ' · ' + new Date(f.j.savedAt*1000).toLocaleString() : '') + '</span></button>';
    PC.modal('<h3>📂 Open a saved project</h3>' + found.map(row).join(''), root=>{
      root.querySelectorAll('.mb-projrow').forEach(btn => btn.onclick = async () => {
        const f = found[+btn.dataset.i];
        PC.closeModal();
        if(P.layers.length && !await uiConfirm('Replace the project you are working on? (↶ undo brings it back.)')) return;
        snap();                       // opening REPLACES the current build — undo has to reach back past it
        P = f.j; delete P[PROJ_MARK]; sel = null; _healLayers(P); save(); render();
      });
    });
  }

  // ---------- interaction ----------
  function repaint(what){
    const root = document.getElementById('feed'); if(!root) return;
    if(what==='inspector'){ const i=document.getElementById('mb-inspector'); if(i){ i.innerHTML=inspector(); bindInspector(root); } return; }
    if(what==='timeline'){ const t=document.getElementById('mb-timeline');
      if(t){ t.innerHTML=timelineInner();
        // The ruler's tick spacing and the playhead's position both depend on the project length, which a
        // trim just changed — repaint them with the rows rather than leaving a stale ruler behind.
        const s=document.getElementById('mb-scrub'); paintPlayhead(s?+s.value||0:0); }
      return; }
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

  // Under 1100px the inspector drops BELOW the stage and above the timeline (see the media query), so
  // tapping a clip in the timeline changed a panel that is off-screen upwards — on a phone that reads as
  // "tapping the clip did nothing". Bring it into view.
  //
  // Only ever called for a confirmed TAP, never from the pointerdown that starts a drag: scrolling the page
  // out from under a finger that is dragging a clip is worse than the problem it solves.
  function _revealInspector(){
    try{
      if(window.matchMedia && !window.matchMedia('(max-width:1100px)').matches) return;
      const i=document.getElementById('mb-inspector');
      if(i && i.scrollIntoView) i.scrollIntoView({ block:'nearest', behavior:'smooth' });
    }catch(_){ }
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
      const box = _layerBox(l);          // measured ONCE, at grab: it must not change mid-drag
      e.preventDefault();
      snap();                            // one snapshot per gesture, taken before the first move
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
          // …but pull it onto the obvious lines while it is near them. Holding Shift drags raw.
          if(!ev.shiftKey) applySnaps(l, box);
        }
        if(l.type==='text' && resizing) item.innerHTML=_textInnerHTML(l)+'<i class="mb-h"></i>';   // size changed the wrapping
        applyGeom(item, l);
      }, ()=>{ showGuides(null,null); unsnapIfUnchanged(); save(); repaint('inspector'); });
    });
  }

  // ---------- snapping ----------
  // Dragging can never land a layer exactly on an edge or on the centre line — and on a phone your finger
  // is over the thing you are placing, so "roughly centred" is the best you could do. Pull x/y onto the
  // canvas edges and centres while within a small distance, and SHOW the line you snapped to (a snap with
  // no feedback reads as the drag being fought). Shift bypasses it for genuinely free placement.
  function applySnaps(l, box){
    const tol = Math.max(6, P.w * 0.015);          // ~1.5% of the frame, floor of 6px for a tiny canvas
    const bw = box.w || 0, bh = box.h || 0;
    let gx = null, gy = null;
    // Horizontal: left edge, centre, right edge. A centred caption's x is ignored by the renderer, so it
    // is left out of the horizontal set (the ⇔ Centre flag is the exact version of that snap).
    if(!(l.type === 'text' && _alignOf(l) === 'center')){
      const cands = [[0, 0], [Math.round((P.w - bw)/2), P.w/2], [Math.round(P.w - bw), P.w]];
      for(const [x, line] of cands){
        if(Math.abs(l.x - x) <= tol){ l.x = x; gx = line; break; }
      }
    }
    const vc = [[0, 0], [Math.round((P.h - bh)/2), P.h/2], [Math.round(P.h - bh), P.h]];
    for(const [y, line] of vc){
      if(Math.abs(l.y - y) <= tol){ l.y = y; gy = line; break; }
    }
    showGuides(gx, gy);
  }
  // The two guide lines live in the stage and are moved/hidden rather than created per frame.
  function showGuides(x, y){
    const gv = document.getElementById('mb-gv'), gh = document.getElementById('mb-gh');
    if(gv){ if(x == null) gv.style.display='none'; else { gv.style.display=''; gv.style.left=(x/P.w*100).toFixed(3)+'%'; } }
    if(gh){ if(y == null) gh.style.display='none'; else { gh.style.display=''; gh.style.top=(y/P.h*100).toFixed(3)+'%'; } }
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
    // EVERY timeline gesture is delegated on #mb-timeline, which survives repaint('timeline') replacing its
    // children. Bound per row instead, selecting a layer by tapping its row silently stopped working after
    // the first trim or drag — the listeners went with the old innerHTML.
    tl.addEventListener('click', (e)=>{
      const zb = e.target.closest('.mb-z');
      if(!zb){
        // Tapping the row (its thumbnail/name, not its clip bar) selects that layer.
        const row = e.target.closest('.mb-track[data-id]');
        if(row && !e.target.closest('.mb-clip')){ selectLayer(row.dataset.id); _revealInspector(); }
        return;
      }
      e.preventDefault(); e.stopPropagation();
      const l = P.layers.find(x=>x.id===zb.dataset.id); if(!l) return;
      const i = P.layers.indexOf(l); if(i<0) return;
      // ONE step, not all the way to the extreme. Jumping straight to front/back meant putting a layer
      // just under the one above it took two moves (down to the very bottom, then back up) — swap with the
      // neighbour instead, which is what "move it under that one" actually means.
      const j = zb.dataset.z==='front' ? i+1 : i-1;
      if(j<0 || j>=P.layers.length) return;                 // already at the top/bottom
      snap();
      P.layers[i]=P.layers[j]; P.layers[j]=l;
      save(); render();
    });
    tl.addEventListener('pointerdown', (e)=>{
      if(e.target.closest('.mb-z')) return;   // a z-order tap is not the start of a drag
      if(e.target.closest('.mb-rlane') || e.target.closest('.mb-ph')){
        e.preventDefault();
        if(_playT) stopPlay(false);           // scrubbing during playback would fight the ticker
        _rulerSeek(e.clientX);
        drag(e, (ev)=>_rulerSeek(ev.clientX));
        return;
      }
      const clip = e.target.closest('.mb-clip'); if(!clip) return;
      const l = P.layers.find(x=>x.id===clip.dataset.id); if(!l) return;
      selectLayer(l.id);
      const lane = clip.parentElement, rect = lane.getBoundingClientRect();
      const total = Math.max(projEnd(), 1), perPx = total/rect.width;
      const grip = e.target.closest('.mb-grip'), side = grip && grip.dataset.grip;
      const sx=e.clientX, ost=l.start, odur=l.dur;
      let moved=false;   // a CLICK must never reshuffle the timeline — only an actual drag does
      e.preventDefault();
      snap();            // one snapshot per gesture; a click that changes nothing is deduped by snap()
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
            unsnapIfUnchanged();
            repaint('timeline'); repaint('inspector'); syncScrub(); _revealInspector(); return;
          }
          // Move ONLY the clip you dragged. Auto-resequencing every other clip on each drop meant nudging
          // one layer silently rewrote the timing of everything after it — you could never adjust a single
          // clip without disturbing the rest. Laying clips back-to-back is now an explicit action (⇄ Arrange).
          unsnapIfUnchanged(); save(); repaint('timeline'); repaint('inspector'); syncScrub(); return;
        }
        unsnapIfUnchanged(); save(); repaint('inspector'); syncScrub();
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
    paintPlayhead(t);
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

  // Upload local files and drop each one on the timeline as its own layer. Shared by the 🖼️ Add media
  // picker and by drag-and-drop, so a dropped file and a picked one land identically.
  async function addMediaFiles(files){
    const st=document.getElementById('mb-status');
    let added=0;
    for(const f of Array.from(files||[])){
      if(!/^(image|video)\//.test(f.type||'')) continue;   // audio has its own button (it spans the meme)
      try{
        if(st) st.textContent='uploading '+f.name+'…';
        const url=await uploadBlob(f);
        addLayer(f.type.startsWith('video')?'video':'image', url, { name:(f.name||'').slice(0,24) });
        added++;
        render();
      }catch(err){
        if(st) st.textContent='upload failed: '+((err&&err.message)||err);
        return added;
      }
    }
    if(st) st.textContent='';
    return added;
  }

  async function pickMedia(){
    // Blossom picker OR a local file. Local files upload first so the render service can fetch them.
    const inp=document.createElement('input'); inp.type='file';
    inp.accept='image/*,video/*'; inp.multiple=true;
    inp.onchange=()=>addMediaFiles(inp.files);
    inp.click();
  }

  // ---------- drag and drop ----------
  // Two different payloads arrive here and only one of them is a file:
  //   * from the DESKTOP (or a file manager) the drop carries dataTransfer.files — same as the picker.
  //   * from ANOTHER BROWSER TAB an image drag carries no file at all, only its URL (text/uri-list, or
  //     an <img src> inside text/html). That URL is on someone else's origin, so it can't just become a
  //     layer: the stage composites in a canvas, and a cross-origin image taints it (the render would
  //     fail at export), quite apart from the picture disappearing whenever that site takes it down.
  // So a dropped URL is FETCHED — through the node's image proxy, which is also what gets us past CORS —
  // and then uploaded like any other local file. Everything on the timeline ends up a Blossom URL.
  const _URL_RE=/^https?:\/\/\S+$/i;
  function _dropUrl(dt){
    const uri=(dt.getData('text/uri-list')||'').split('\n').map(s=>s.trim()).filter(s=>s && !s.startsWith('#'))[0];
    if(uri && _URL_RE.test(uri)) return uri;
    const html=dt.getData('text/html')||'';
    const m=/<img[^>]+src\s*=\s*["']([^"']+)["']/i.exec(html);
    if(m && _URL_RE.test(m[1])) return m[1];
    const txt=(dt.getData('text/plain')||'').trim();
    return _URL_RE.test(txt) ? txt : '';
  }
  async function addMediaUrl(u){
    const st=document.getElementById('mb-status');
    if(st) st.textContent='fetching the image…';
    let blob=null;
    try{ blob=await fetch('/client/proxy-image?url='+encodeURIComponent(u)).then(r=>r.ok?r.blob():null); }catch(_){}
    // Direct fetch as the fallback, exactly like the Effects studio: the proxy is the reliable path
    // (CORS + private-address guard), but a CORS-friendly host still works without it.
    if(!blob){ try{ blob=await fetch(u).then(r=>r.ok?r.blob():null); }catch(_){} }
    if(!blob || !/^(image|video)\//.test(blob.type||'')){
      if(st) st.textContent='';
      toast(blob ? 'that link isn’t an image or video' : 'could not fetch that image');
      return 0;
    }
    const base=(u.split(/[?#]/)[0].split('/').pop()||'dropped').slice(0,24);
    const ext=(blob.type.split('/')[1]||'jpg').split('+')[0];
    const name=/\.\w{2,4}$/.test(base) ? base : base+'.'+ext;
    return await addMediaFiles([new File([blob], name, { type:blob.type })]);
  }
  function bindDrop(root){
    const wrap=root.querySelector('.mb-wrap'); if(!wrap) return;
    // Only light up for a drag that actually carries media — dragging a text selection over the builder
    // shouldn't make it look like a drop target.
    const carries=dt=>!!dt && (Array.from(dt.types||[]).some(t=>t==='Files'||t==='text/uri-list'||t==='text/html'||t==='text/plain'));
    let depth=0;
    wrap.addEventListener('dragenter', e=>{ if(!carries(e.dataTransfer)) return; e.preventDefault(); depth++; wrap.classList.add('mb-dropping'); });
    wrap.addEventListener('dragover', e=>{ if(!carries(e.dataTransfer)) return; e.preventDefault(); try{ e.dataTransfer.dropEffect='copy'; }catch(_){} });
    // dragleave fires for every child the pointer crosses, so it's counted against dragenter rather
    // than clearing the highlight the first time the cursor passes over a layer box.
    wrap.addEventListener('dragleave', ()=>{ if(--depth<=0){ depth=0; wrap.classList.remove('mb-dropping'); } });
    wrap.addEventListener('drop', async e=>{
      const dt=e.dataTransfer; if(!carries(dt)) return;
      e.preventDefault(); depth=0; wrap.classList.remove('mb-dropping');
      const files=Array.from(dt.files||[]).filter(f=>/^(image|video)\//.test(f.type||''));
      let n=0;
      if(files.length) n=await addMediaFiles(files);
      else { const u=_dropUrl(dt); if(u) n=await addMediaUrl(u); else toast('drop an image, a video, or a link to one'); }
      if(n) toast(n===1 ? 'added as a new layer' : n+' layers added');
    });
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
      snap();
      // Remember what was here so "↺ Undo the effect on this layer" can put the picture back. This was a
      // ONE-WAY door: the original URL was overwritten, so the wrong pick out of a hundred-name dropdown
      // cost you the layer. Only recorded on the FIRST effect, so stacking effects still reverts all the
      // way to the photo you started from rather than to the previous effect's output.
      if(!base.origSrc){
        base.origSrc = base.src; base.origType = base.type;
        base.origName = base.name || ''; base.origDur = +base.dur || 0;
      }
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

  // Add an effect as its OWN layer — the other half of the story from applyMemeEffect above, which
  // transforms a layer's image and replaces it. Here the server renders the effect onto a transparent
  // canvas (VP9-alpha .webm), stores it to Blossom, and hands back a URL we hang on the timeline as an
  // ordinary video layer: so it composites over whatever is beneath it and gets the drag/resize handles,
  // the trim widget and the sound control for free, with no special layer type to teach the renderer.
  //
  // Shares _fxBusy with applyMemeEffect on purpose: both are the same heavy server render, and the
  // server's own per-user cooldown would 429 the second one anyway — better to say so before sending it.
  function pickEffect(){
    if(!ALPHA_FX.length){ toast('no add-as-layer effects available on this server'); return; }
    const rows = ALPHA_FX.map((e,i)=>`<button class="btn btn-ghost full" data-i="${i}" style="text-align:left">`
      + `${enc(e.label||e.name)}${e.audio?' <span class="muted small">🔊 with sound</span>':''}</button>`).join('');
    // Say WHERE it will land before the pick, because that depends on the selection: matching the
    // selected layer is what you want when you're dressing up a photo, and a bare "it appeared
    // somewhere" is the thing that makes an overlay feel broken.
    const base = P.layers.find(x=>x.id===sel && _isVisual(x));
    PC.modal(`<h3>✨ Add an effect layer</h3>
      <div class="muted small" style="margin-bottom:8px">Rendered on a transparent background, so it sits over
      whatever is beneath it. ${base ? 'It will be placed over the selected layer' : 'It will start at the beginning of the build'} —
      then drag, resize and re-time it like any other layer.</div>
      ${rows}`, root=>{
      root.querySelectorAll('button[data-i]').forEach(btn=>btn.onclick=async()=>{
        const e = ALPHA_FX[+btn.dataset.i]; if(!e) return;
        PC.closeModal();
        if(_fxBusy){ toast('still rendering the last effect — hang on'); return; }
        _fxBusy = true;
        const st=document.getElementById('mb-status');
        if(st) st.textContent='rendering '+(e.label||e.name)+'…';
        try{
          const auth = await selfProof();
          const r = await fetch('/client/meme/effect',{ method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ pubkey: ME.pubkey, auth, name: e.name }) });
          const j = await r.json().catch(()=>({}));
          if(!r.ok || !j.url){ throw new Error(j.detail || j.error || ('HTTP '+r.status)); }
          // NOT addLayer(): that appends media at the END of the timeline (right for a clip that plays
          // after the others, wrong for an overlay — it would render over nothing and look like the
          // effect never arrived). An overlay is timed to what it sits on instead.
          const nominal = (+j.dur>0) ? +j.dur : 4;
          const box = base ? { x:base.x, y:base.y, w:base.w, h:base.h }
                           : { x:0, y:Math.round((P.h - Math.round(P.h/2))/2), w:P.w, h:Math.round(P.h/2) };
          // The clip is SILENT (VP9 alpha can't carry an audio stream) — when the effect has a sound the
          // server returns its NAME, which rides the layer's existing `sound` field and mixes on render.
          const ov = Object.assign({ id: nid(), type:'video', src:j.url, name:(e.label||e.name).slice(0,24),
            start: base ? (+base.start||0) : 0,
            dur: base ? ((+base.dur>0) ? +base.dur : nominal) : nominal,
            trim:0, opacity:1, effect:'none', volume:1, mute:false, fit:'contain',
            flipH:false, flipV:false, rotate:0,
            sound:j.sound||'', soundVolume:1, text:'', size:64, color:'#ffffff', stroke:'#000000', align:'' }, box);
          // Draw ABOVE the layer it dresses up, but still BELOW any caption — a caption buried under an
          // overlay is the same bug addLayer guards against for ordinary media.
          let at = base ? P.layers.indexOf(base) + 1 : P.layers.length;
          const firstText = P.layers.findIndex(x=>x.type==='text');
          if(firstText>=0 && at>firstText) at = firstText;
          snap();
          P.layers.splice(at, 0, ov);
          sel = ov.id;                 // selected on arrival, so it can be dragged/resized immediately
          if(st) st.textContent='';
          save(); render();
          toast('effect layer added — drag or resize it');
        }catch(err){ if(st) st.textContent=''; toast('effect failed: '+((err&&err.message)||err)); }
        finally{ _fxBusy = false; }
      });
    });
  }

  // A render is one blocking POST that the server gives up on after 150s. "rendering…" with no clock and
  // no way out meant a slow job was indistinguishable from a hung one, and the only exit was leaving the
  // view (which does not stop the request). Now it counts up and the button becomes a cancel.
  let _renderAbort = null, _renderTick = null;
  function _stopRenderClock(){
    if(_renderTick){ clearInterval(_renderTick); _renderTick = null; }
  }
  // ffmpeg's stderr tail is the truth but not an explanation. Map the failures that are actually the edit's
  // fault onto the change that fixes them, and keep the raw text for anything unrecognised.
  function _renderErr(msg){
    const m = String(msg || '');
    if(/timed out/i.test(m)) return m;                    // the server already words this one well
    if(/No such file|not found|404|could not fetch/i.test(m))
      return 'a layer’s media could not be fetched — re-add that layer (its link may have expired)';
    if(/Invalid data found|moov atom|Invalid argument/i.test(m))
      return 'one of the clips is in a format this node can’t read — try re-adding it, or convert it first';
    if(/No space left/i.test(m)) return 'the server ran out of disk while rendering — tell the admin';
    return m || 'render failed';
  }

  async function doRender(){
    if(!P.layers.length){ toast('add a layer first'); return; }
    // Second tap while a render is in flight = CANCEL it (see the button's label below).
    if(_rendering && _renderAbort){ try{ _renderAbort.abort(); }catch(_){ } return; }
    // Guard on MODULE state, not just the button's disabled flag: any repaint/render() of the view replaces
    // that button with a fresh ENABLED one, so mid-render edits made it clickable again and each click
    // spawned another full server-side ffmpeg of the same project (they pile up and the UI sits on
    // "rendering…" forever). The server enforces this too (429), but don't even send the duplicate.
    if(_rendering){ toast('already rendering — hang on'); return; }
    _rendering = true;
    const st=document.getElementById('mb-status'), btn=document.getElementById('mb-render');
    const out=document.getElementById('mb-result');
    // NOT disabled: the button is how you cancel. Leaving it live is also what makes the guard on
    // _rendering (module state, not the button) load-bearing — see the check at the top.
    if(btn){ btn.textContent='✕ Cancel'; btn.classList.add('btn-danger'); }
    let secs=0;
    if(st) st.textContent='rendering… 0s';
    _stopRenderClock();
    _renderTick=setInterval(()=>{ secs++;
      const s2=document.getElementById('mb-status');
      if(!s2 || !document.getElementById('mb-stage')){ _stopRenderClock(); return; }
      s2.textContent='rendering… '+secs+'s';
    }, 1000);
    _renderAbort = (typeof AbortController!=='undefined') ? new AbortController() : null;
    try{
      const edit={ w:P.w, h:P.h, fps:P.fps, bg:P.bg, duration:projEnd(),
        layers:P.layers.map(l=>({ type:l.type, src:l.src, start:+l.start, dur:+l.dur, trim:+l.trim||0,
          x:Math.round(l.x), y:Math.round(l.y), w:Math.round(l.w), h:Math.round(l.h),
          opacity:+l.opacity, effect:l.effect, sound:l.sound||'', soundVolume:(l.soundVolume==null?1:+l.soundVolume), mute:!!l.mute,
          flipH:!!l.flipH, flipV:!!l.flipV, rotate:+l.rotate||0,
          // NOT `+l.volume||1`: that turned a deliberate volume of 0 back into full volume.
          volume:(l.volume==null?1:+l.volume), fade:!!l.fade,
          // The WRAPPED text, not the raw text: the renderer draws one line per newline it is given, and
          // wrapText is the same function the preview lays out, so what you saw is what gets drawn.
          text:(l.type==='text' ? wrapText(l) : l.text), size:+l.size, color:l.color, stroke:l.stroke,
          box:!!l.box, boxColor:l.boxColor||'#000000', boxAlpha:(l.boxAlpha==null?0.55:+l.boxAlpha), shadow:!!l.shadow,
          fit:l.fit||'contain', align:_alignOf(l), cx:(l.type==='text' ? _textCenterX(l) : null) })) };
      const auth=await selfProof();
      const r=await fetch('/client/meme/render',{ method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth, edit }),
        signal: _renderAbort ? _renderAbort.signal : undefined });
      if(!r.ok){
        let msg=''; try{ msg=(await r.json()).detail||''; }catch(_){ msg=await r.text().catch(()=>''); }
        throw new Error(msg||('render failed ('+r.status+')'));
      }
      const blob=await r.blob();
      if(st) st.textContent='';
      showResult(blob, out);
    }catch(err){
      const s3=document.getElementById('mb-status'); if(s3) s3.textContent='';
      const o2=document.getElementById('mb-result');
      if(err && err.name==='AbortError'){ toast('render cancelled'); if(o2) o2.innerHTML=''; }
      else if(o2) o2.innerHTML='<div class="mb-err">⚠️ '+enc(_renderErr((err&&err.message)||err))+'</div>';
    }finally{
      _rendering=false; _renderAbort=null; _stopRenderClock();
      const b=document.getElementById('mb-render');
      if(b){ b.textContent='🎬 Render'; b.classList.remove('btn-danger'); b.disabled=false; }
    }
  }

  // Who the "reply with it" button will answer, or null. Named so the button can say WHOSE post it
  // replies to: a project persists across reloads, so "↩️ Reply to the post" on its own would be asking
  // you to trust a target you can no longer see.
  function _replyTarget(){
    const to = P && P.replyTo;
    if(!to || !to.id) return null;
    const p = (PC.profOf && to.pk) ? (PC.profOf(to.pk) || {}) : {};
    const name = p.name || p.display_name || (PC.niceNip05 && PC.niceNip05(p.nip05)) || '';
    return { id: to.id, pk: to.pk || '', name };
  }

  // Change the canvas shape and carry the layers with it. Without the rescale, every layer keeps its
  // old pixel box on a differently-shaped canvas — captions land off-frame and photos stop covering
  // what they covered — which reads as "changing the size wrecked my build".
  function _resizeCanvas(w, h){
    w = Math.max(16, Math.round(w)/2*2|0) || P.w; h = Math.max(16, Math.round(h)/2*2|0) || P.h;
    if(w === P.w && h === P.h) return;
    snap();      // reshaping rewrites every layer's geometry — the one edit you most want to take back
    const rx = w/P.w, ry = h/P.h;
    P.layers.forEach(l=>{
      if(l.type==='audio') return;                 // no geometry
      l.x = Math.round((+l.x||0)*rx); l.y = Math.round((+l.y||0)*ry);
      if(l.type==='text'){ l.size = Math.max(8, Math.round((+l.size||48)*Math.min(rx,ry))); }
      else { l.w = Math.max(2, Math.round((+l.w||0)*rx)); l.h = Math.max(2, Math.round((+l.h||0)*ry)); }
    });
    P.w = w; P.h = h; save(); render();
  }

  // The canvas is BLACK by default and `contain` letterboxes anything whose shape differs from it, so
  // the bars people see are the canvas showing through a transparent pad — not a border drawn on the
  // photo, and not something the fit buttons can remove without cropping. Reshaping the canvas to the
  // photo is the third answer: whole photo, no bars, nothing cropped.
  function matchCanvasToLayer(l){
    const el = document.querySelector('.mb-item[data-id="'+l.id+'"] img, .mb-item[data-id="'+l.id+'"] video');
    const iw = el ? (el.naturalWidth || el.videoWidth || 0) : 0;
    const ih = el ? (el.naturalHeight || el.videoHeight || 0) : 0;
    if(!iw || !ih){ toast('still loading that media — try again in a second'); return; }
    // Cap the long edge like the presets do (1280): the canvas is the RENDER size, and a 2048-tall
    // project is a much slower ffmpeg for no visible gain on a phone.
    const cap = 1280, r = Math.min(1, cap/Math.max(iw, ih));
    snap();   // deduped against _resizeCanvas's own snapshot (same state, nothing changed between them)
    _resizeCanvas(iw*r, ih*r);
    l.x = 0; l.y = 0; l.w = P.w; l.h = P.h; l.fit = 'contain';
    sel = l.id; save(); render();
    toast('canvas matched — whole photo, no bars');
  }

  function showResult(blob, out){
    const url=URL.createObjectURL(blob);
    // Opened from a post (🎞️ Meme Builder on a note) → offer the reply right here, the way the Effects
    // studio does. Without it the only route back to the thread is copy-link, find the post, paste.
    const to=_replyTarget();
    const replyBtn = to ? `<button class="btn btn-neon small" id="mb-reply">↩️ Reply${to.name?' to '+enc(to.name):' to the post'}</button>` : '';
    out.innerHTML=`<div class="mb-result">
      <video src="${url}" controls playsinline class="mb-resvid"></video>
      <div class="mb-resacts">
        ${replyBtn}
        <button class="btn btn-neon small" id="mb-post">📤 Post to Nostr</button>
        <button class="btn btn-cyan small" id="mb-copy">🔗 Upload &amp; copy link</button>
        <button class="btn btn-cyan small" id="mb-again" title="Put this render back on the timeline as a clip, so you can build on top of it">🎞️ Use as a layer</button>
        <a class="btn btn-neon small" href="${url}" download="${enc((P.name||'meme').replace(/[^\w.-]+/g,'_').slice(0,40))}.mp4">⬇️ Download</a>
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
    // Upload once, then publish a kind-1 carrying the link with NIP-10 tags to the source post — the
    // same two steps (and the same eTags) as the Effects studio's sendEffectReply, so a meme reply
    // threads identically to an effect reply. Disable while it's in flight: publishing twice would put
    // two copies of the same meme under the post.
    const rb=document.getElementById('mb-reply');
    if(rb && to) rb.onclick=async()=>{
      if(rb.disabled) return;
      rb.disabled=true; const was=rb.textContent; rb.textContent='posting…';
      try{
        const u=await up();
        const r=await PC.publish(1, u, PC.eTags(to.id, to.pk));    // publish() toasts its own failure
        if(r && r.ok){ rb.textContent='✓ replied'; rb.classList.add('on'); toast('✓ reply posted'); }
        else { rb.disabled=false; rb.textContent=was; }
      }catch(err){ rb.disabled=false; rb.textContent=was;
        const el=linkEl(); if(el) el.textContent='reply failed: '+((err&&err.message)||err); }
    };
    // Feed the finished render back in as a clip. The AI view has had this since it was built
    // (ai-memefile), and not having it here meant "flatten what I've got and keep going" — the way you
    // build a meme with more than 24 layers, or bake a caption in before adding another — was
    // download-then-re-upload-by-hand. The URL has to be a Blossom one, exactly like any other layer
    // source, so the render is uploaded first.
    const ag=document.getElementById('mb-again');
    if(ag) ag.onclick=async()=>{
      if(ag.disabled) return;
      ag.disabled=true; const was=ag.textContent; ag.textContent='uploading…';
      try{
        const u=await up();
        addLayer('video', u, { name:(P.name||'meme').slice(0,24) });
        save(); render();
        toast('added as a new clip at the end of the timeline');
      }catch(err){ ag.disabled=false; ag.textContent=was;
        const el=linkEl(); if(el) el.textContent='upload failed: '+((err&&err.message)||err); }
    };
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
    // Typing in a number box is a BURST (one snapshot for the whole edit), and every one of these
    // handlers mutates, so the snapshot has to be taken before the assignment — see snapBurst.
    const num=(id,key,lo,hi)=>on(id,'input',(e)=>{ snapBurst(key+':'+l.id); l[key]=clamp(e.target.value,lo,hi); save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) applyGeom(it,l);
      if(key==='start'||key==='dur') repaint('timeline'); });
    num('mb-f-w','w',16,4320); num('mb-f-h','h',16,4320);
    // Blow the layer up to the FULL project canvas and pin it to 0,0 — the common "make this the background /
    // full-bleed clip" move, which otherwise means typing the project's W and H and zeroing X/Y by hand.
    on('mb-fill','click',()=>{ snap(); l.x=0; l.y=0; l.w=P.w; l.h=P.h; l.fit='cover';
      save(); render(); toast('fills the frame — edges cropped'); });
    // The whole photo, scaled to fit inside the canvas. Can't do both: filling a different aspect ratio
    // always crops, and showing everything always leaves bars — so make it an explicit choice.
    on('mb-canvas-match','click',()=>matchCanvasToLayer(l));
    on('mb-fit','click',()=>{ snap(); l.x=0; l.y=0; l.w=P.w; l.h=P.h; l.fit='contain';
      save(); render(); toast('whole photo — bars where the aspect differs'); });
    on('mb-f-name','input',(e)=>{ snapBurst('name:'+l.id); l.name=String(e.target.value||'').slice(0,24); save(); repaint('timeline'); });
    // Centre a caption: drawtext anchors the text's LEFT edge, so "centred" means x = (canvas - textWidth)/2.
    // Measure the preview element (it now hugs its text) and convert screen px -> project px.
    on('mb-center','click',()=>{
      // Flag it and let ffmpeg centre with (w-text_w)/2. Measuring the preview and computing a pixel x was
      // wrong: the browser wraps the caption at 92% and uses a different font, so the measured width (and
      // therefore x) did not match the single unwrapped line drawtext actually draws.
      snap();
      l.align = (_alignOf(l)==='center') ? '' : 'center';
      save(); render(); toast(_alignOf(l)==='center' ? 'caption centred' : 'caption free-positioned');
    });
    root.querySelectorAll('.mb-ab').forEach(b=>b.addEventListener('click',()=>{
      alignLayer(l, b.dataset.h==='' ? null : +b.dataset.h, b.dataset.v==='' ? null : +b.dataset.v);
    }));
    num('mb-f-start','start',0,120); num('mb-f-dur','dur',0.1,120); num('mb-f-trim','trim',0,600);
    if(l.type==='video') bindTrim(root, l);
    on('mb-prev-clip','click',()=>previewLayer(l));   // play just this clip, not the whole meme
    on('mb-prev-fx','click',()=>previewLayer(l));      // same for an effect layer
    // A caption's element is rebuilt (not just restyled) whenever anything that changes its WRAPPING or
    // its box changes — the markup is one span per wrapped line, so the lines have to be recomputed.
    const _paintText=()=>{ const it=root.querySelector('.mb-item[data-id="'+l.id+'"]');
      if(it) it.innerHTML=_textInnerHTML(l)+'<i class="mb-h"></i>'; };
    on('mb-f-size','input',(e)=>{ snapBurst('size:'+l.id); l.size=clamp(e.target.value,8,400); save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) applyGeom(it,l);
      _paintText(); });                       // size changes where the lines break
    on('mb-f-text','input',(e)=>{ snapBurst('text:'+l.id); l.text=e.target.value; save();
      _paintText(); repaint('timeline'); });
    on('mb-f-color','input',(e)=>{ snapBurst('color:'+l.id); l.color=e.target.value; save(); repaint(); });
    on('mb-f-stroke','input',(e)=>{ snapBurst('stroke:'+l.id); l.stroke=e.target.value; save(); repaint(); });
    on('mb-f-wrap','change',(e)=>{ snap(); l.wrap=!!e.target.checked; save(); repaint('inspector'); _paintText(); });
    on('mb-f-wrappct','input',(e)=>{ snapBurst('wrappct:'+l.id); l.wrapPct=clamp(e.target.value,20,100); save(); _paintText();
      const lb=e.target.parentElement && e.target.parentElement.querySelector('b'); if(lb) lb.textContent=_wrapPct(l)+'%'; });
    on('mb-f-box','change',(e)=>{ snap(); l.box=!!e.target.checked; save(); repaint('inspector'); _paintText(); });
    on('mb-f-boxcolor','input',(e)=>{ snapBurst('boxcolor:'+l.id); l.boxColor=e.target.value; save(); _paintText(); });
    on('mb-f-boxalpha','input',(e)=>{ snapBurst('boxalpha:'+l.id); l.boxAlpha=clamp(e.target.value,0.1,1); save(); _paintText(); });
    on('mb-f-shadow','change',(e)=>{ snap(); l.shadow=!!e.target.checked; save(); render(); });
    on('mb-f-fx','change',(e)=>{ snap(); l.effect=e.target.value; save(); });
    // Put the layer's ORIGINAL picture back. applyMemeEffect replaces the source in place, which used to be
    // a one-way door: the wrong pick out of a hundred-name list cost you the layer (Ctrl+Z covers it too now,
    // but not once you have made other edits on top).
    on('mb-fx-revert','click',()=>{
      if(!l.origSrc) return;
      snap();
      l.src=l.origSrc; l.type=l.origType||'image'; l.name=l.origName||''; l.trim=0;
      if(+l.origDur>0) l.dur=+l.origDur;
      delete l.origSrc; delete l.origType; delete l.origName; delete l.origDur;
      save(); render(); toast('effect undone — original picture is back');
    });
    // --- music layer ---
    on('mb-aud-all','click',()=>{ snap(); l.start=0; l.dur=+Math.max(projEnd(),0.1).toFixed(2); save(); render();
      toast('music spans the whole meme'); });
    on('mb-f-avol','input',(e)=>{ snapBurst('avol:'+l.id); l.volume=clamp(e.target.value,0,2); save();
      const a=_audioEls.find(x=>x.dataset.id===l.id); if(a) a.volume=clamp(l.volume,0,1); });
    on('mb-f-afade','change',(e)=>{ snap(); l.fade=e.target.checked; save(); });
    on('mb-f-snd','change',(e)=>{ snap(); l.sound=e.target.value; save(); repaint('inspector'); toast(l.sound?('sound: '+l.sound):'sound removed'); });
    // The per-layer "Meme effect" dropdown is the SOURCE OF TRUTH for full effects (dancing man, shrug,
    // characters): picking one renders it server-side and overlays it ON this layer. Trigger dropdown —
    // reset to the placeholder after firing so it can be used again.
    on('mb-f-meme','change',(e)=>{ const nm=e.target.value; e.target.value=''; if(nm) applyMemeEffect(l, nm); });
    on('mb-f-sndvol','input',(e)=>{ snapBurst('sndvol:'+l.id); l.soundVolume=clamp(e.target.value,0,3); save(); });
    on('mb-f-mute','change',(e)=>{ snap(); l.mute=e.target.checked; save(); });
    on('mb-f-op','input',(e)=>{ snapBurst('op:'+l.id); l.opacity=clamp(e.target.value,0.05,1); save();
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) it.style.opacity=l.opacity; });
    // Flip/rotate repaint the layer's transform IN PLACE rather than re-rendering the board: a full
    // re-render on every slider step would rebuild the <video> elements and restart them from frame 0.
    const _paintX=()=>{ const it=root.querySelector('.mb-item[data-id="'+l.id+'"]');
      const m=it && it.querySelector('img,video'); if(!m) return;
      m.style.transform=_xform(l); m.style.transformOrigin='center'; };
    on('mb-fliph','click',(e)=>{ snap(); l.flipH=!l.flipH; save(); e.currentTarget.classList.toggle('on',!!l.flipH); _paintX(); });
    on('mb-flipv','click',(e)=>{ snap(); l.flipV=!l.flipV; save(); e.currentTarget.classList.toggle('on',!!l.flipV); _paintX(); });
    on('mb-f-rot','input',(e)=>{ snapBurst('rot:'+l.id); l.rotate=clamp(e.target.value,-180,180); save();
      const v=root.querySelector('#mb-rot-val'); if(v) v.textContent=Math.round(l.rotate)+'°'; _paintX(); });
    on('mb-rot0','click',()=>{ snap(); l.rotate=0; l.flipH=false; l.flipV=false; save();
      const b=root.querySelector('#mb-f-rot'); if(b) b.value=0;
      const v=root.querySelector('#mb-rot-val'); if(v) v.textContent='0°'; _paintX(); });
    on('mb-dup','click',()=>duplicateLayer(l));
    // No confirm — Ctrl+Z (and the ↶ button) is a better answer than a dialog, and the dialog was the
    // only thing standing between you and a mis-tap on a phone anyway.
    on('mb-del','click',()=>{
      snap();
      P.layers=P.layers.filter(x=>x.id!==l.id); sel=null; save(); render();
      toast('layer deleted — ↶ undo to bring it back');
    });
    on('mb-front','click',()=>{ const i=P.layers.indexOf(l); if(i>-1){ snap(); P.layers.splice(i,1); P.layers.push(l); save(); render(); } });
    on('mb-back','click',()=>{ const i=P.layers.indexOf(l); if(i>0){ snap(); P.layers.splice(i,1); P.layers.unshift(l); save(); render(); } });
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
        snap();                  // one snapshot per trim gesture
        const up=()=>{
          document.removeEventListener('pointermove',move);
          document.removeEventListener('pointerup',up);
          unsnapIfUnchanged();
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
    bindStage(root); bindTimeline(root); bindInspector(root); bindDrop(root);
    const on=(id,ev,fn)=>{ const e=root.querySelector('#'+id); if(e) e.addEventListener(ev,fn); };
    on('mb-add-media','click',pickMedia);
    on('mb-add-text','click',()=>{ addLayer('text'); render(); });
    on('mb-add-audio','click',pickAudio);
    on('mb-add-effect','click',pickEffect);
    on('mb-add-blossom','click',pickBlossom);
    on('mb-tpl','click',pickTemplate);
    on('mb-undo','click',undo);
    on('mb-redo','click',redo);
    on('mb-proj','click',projectMenu);
    // Explicit "snap everything back-to-back", in the clips' current time order. This used to happen
    // automatically on every drop, which made adjusting one clip rewrite the whole timeline.
    on('mb-arrange','click',()=>{ snap(); resequence(); unsnapIfUnchanged(); save(); render(); toast('clips laid back-to-back'); });
    on('mb-render','click',doRender);
    on('mb-play','click',()=>togglePlay());
    on('mb-scrub','input',(e)=>seek(+e.target.value));
    on('mb-size','change',(e)=>{ const [w,h]=e.target.value.split('x').map(Number); _resizeCanvas(w,h); });
    // Repaint the stage directly rather than re-rendering the view: a full render() would replace the
    // colour input mid-drag and drop the picker.
    on('mb-bg','input',(e)=>{ snapBurst('bg'); P.bg=e.target.value||'#000000';
      const st=document.getElementById('mb-stage'); if(st) st.style.background=P.bg; save(); });
    // Row selection and ruler scrubbing are DELEGATED inside bindTimeline — they must survive
    // repaint('timeline'), which replaces every row.
    if(_prevT) paintPlayhead(_prevT); else seek(0);
  }

  // ---------- the project menu ----------
  // Save / Open / Rename / New / Clear behind one button. They were five buttons in a bar that already had
  // eleven, which on a phone wrapped into a wall where "Clear all" sat one thumb-width from "Add text".
  function projectMenu(){
    PC.modal(`<h3>📂 ${enc(P.name || 'Untitled')}</h3>
      <div class="muted small" style="margin-bottom:10px">${P.layers.length} layer${P.layers.length===1?'':'s'} · ${P.w}×${P.h} · ${projEnd().toFixed(1)}s</div>
      <button class="btn btn-neon full" id="mbp-save">💾 Save to my Blossom drive</button>
      <button class="btn btn-cyan full" id="mbp-open">📂 Open a saved project…</button>
      <button class="btn btn-cyan full" id="mbp-name">✏️ Rename this project</button>
      <button class="btn btn-cyan full" id="mbp-new">🆕 Start a new project</button>
      <button class="btn btn-danger full" id="mbp-clear">🧹 Remove every layer</button>`, root=>{
      const q = (id) => root.querySelector('#'+id);
      q('mbp-save').onclick = ()=>{ PC.closeModal(); saveProject(); };
      q('mbp-open').onclick = ()=>{ PC.closeModal(); openProject(); };
      q('mbp-name').onclick = async ()=>{
        PC.closeModal();
        const n = await uiPrompt('Name this project', { value: P.name || '', placeholder: 'dog on a skateboard' });
        if(n == null) return;
        snap(); P.name = String(n).slice(0, 60).trim(); save(); render();
      };
      // A NEW project resets the canvas and the name as well as the layers — that is the difference from
      // Clear, which keeps the shape you set up and only empties it. Undo covers both.
      q('mbp-new').onclick = async ()=>{
        PC.closeModal();
        if(P.layers.length && !await uiConfirm('Start a new project? The current one is replaced (↶ undo brings it back).')) return;
        snap(); stopPlay(true); P = blank(); sel = null; save(); render(); toast('new project');
      };
      q('mbp-clear').onclick = async ()=>{
        PC.closeModal();
        if(!P.layers.length){ toast('nothing to clear'); return; }
        if(!await uiConfirm(`Remove all ${P.layers.length} layer${P.layers.length===1?'':'s'}?`)) return;
        snap(); stopPlay(true);
        P.layers=[]; sel=null; save(); render();
        toast('all layers cleared — ↶ undo brings them back');
      };
    });
  }

  boot();
})();

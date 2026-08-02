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
      render(){ ME = PC.ME; P = load(); _fitNext = true; render(); },
      // Persist on the way OUT too. Every edit already saves, but leaving the view is exactly when a
      // missed save becomes 'my project came back different', so make it unconditional.
      unmount(){ try{ stopPlay(false); }catch(_){ if(_playT){ clearInterval(_playT); _playT=null; } } try{ if(P) save(); }catch(_){ } },
      reset(){ P = blank(); sel=null; _fitNext = true; save(); render(); },
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

  // ---------- ONE panel at a time ----------
  // The builder used to be one long column: two wrapped toolbars (~6 rows on a phone), the stage, the whole
  // inspector, then the timeline — ~1700px of scrolling, so the stage, the controls and the timeline were
  // never on screen together. Tapping a clip changed a panel that was off-screen upwards, which reads as
  // "tapping did nothing" (there was a scrollIntoView hack for exactly that). Sideways it was worse: the
  // toolbars alone ate a landscape phone.
  //
  // Now the stage is ALWAYS on screen and everything else is one of a few panes behind a tab strip. Desktop
  // shows every pane at once (it has the room) and hides the tabs — same markup, no second code path.
  let _tab = 'layer';                       // which pane the tabs are showing (mobile/tablet only)
  let _hasResult = false;                   // a finished render is sitting in #mb-result → offer its tab
  // Inspector sections. A layer panel is ~20 controls; collapsed by default it is the handful you actually
  // reach for. Module-level so the state survives repaint('inspector'), which happens on every drag end.
  const _sec = { place:true, time:false, look:false };
  // Read the groups' state back OFF THE DOM immediately before the panel is rebuilt, rather than tracking it
  // with a `toggle` listener. <details> fires `toggle` ASYNCHRONOUSLY, so a rebuild triggered in the same
  // task as the click — tap a group open, then tap a clip — ran before the event and rewrote the panel from
  // the stale flags, snapping every group back to its default. Reading the live `open` property has no such
  // race, and there is nothing to re-bind on each repaint.
  function _saveSecState(){
    document.querySelectorAll('.mb-sec').forEach(d=>{ if(d.dataset.sec in _sec) _sec[d.dataset.sec] = d.open; });
  }
  const _mobileLayout = () => { try{ return !window.matchMedia || window.matchMedia('(max-width:1100px)').matches; }catch(_){ return false; } };

  function _paintTabs(){
    const bar = document.getElementById('mb-tabs'); if(!bar) return;
    bar.innerHTML = tabsInner();
    bar.querySelectorAll('.mb-tab').forEach(b=>b.addEventListener('click',()=>_showTab(b.dataset.tab)));
  }
  // Switch panes without re-rendering: a full render() would rebuild the stage's <video> elements (and
  // restart them from frame 0) every time you looked at the timeline.
  function _showTab(name){
    if(name==='result' && !_hasResult) name='layer';
    _tab = name;
    document.querySelectorAll('.mb-pane').forEach(p=>p.classList.toggle('on', p.dataset.pane===name));
    document.querySelectorAll('.mb-tab').forEach(t=>t.classList.toggle('on', t.dataset.tab===name));
    _fitStage();                     // the pane that just opened is a different height → the stage resizes
    // The playhead is positioned from a real lane's measured offset, and a display:none lane measures 0 —
    // so a pane that was hidden the last time the playhead moved comes back with it parked at the far left,
    // over the track names. Re-place it against the lanes now that they have a box.
    if(name === 'timeline'){ const s=document.getElementById('mb-scrub'); paintPlayhead(s ? +s.value||0 : 0); }
  }

  // Fit the stage into whatever vertical space is left, in JS, because CSS cannot.
  // The stage's geometry contract is that its box IS the project's aspect ratio — every layer is positioned
  // in % of it, so a box that is not the project's shape puts every layer in the wrong place (that is why the
  // old rule sized from the HEIGHT and let aspect-ratio derive the width). `height:42vh` honoured that but
  // ignored what was left over, which is the whole problem in landscape: 42vh of a 390px-tall phone is a
  // 164px stage under 6 rows of toolbar. Measuring the free space and setting BOTH axes is exact, needs no
  // aspect-ratio fallback behaviour, and is the only thing that adapts to an orientation change.
  function _fitStage(){
    const st = document.getElementById('mb-stage'); if(!st || !P) return;
    const host = st.parentElement; if(!host) return;
    if(!_mobileLayout()){ st.style.height=''; st.style.width=''; return; }   // desktop keeps the CSS height
    const availH = host.clientHeight, availW = host.clientWidth;
    if(!availH || !availW) return;
    const ar = (+P.w||1) / (+P.h||1);
    let h = availH, w = h * ar;
    if(w > availW){ w = availW; h = w / ar; }
    // Any minimum has to scale the WHOLE box. Clamping one axis on its own is how the stage stops being
    // the project's shape — measured on a short landscape window, a 67px-wide 9:16 stage was floored to
    // 70px and every layer's %-position moved with it. Overflowing a tiny box is the lesser evil (and
    // .mb-fit clips it); a stage that lies about the frame is not.
    const MIN = 60, s = Math.max(1, MIN/h, MIN/w);
    st.style.height = Math.round(h*s) + 'px';
    st.style.width  = Math.round(w*s) + 'px';
  }
  // ONE listener for the life of the page (the module is an IIFE that boots once). It no-ops whenever the
  // builder is not on screen, so there is nothing to tear down when you leave the view.
  let _fitT = null;
  window.addEventListener('resize', ()=>{ if(_fitT) clearTimeout(_fitT); _fitT=setTimeout(_fitStage, 80); });
  window.addEventListener('orientationchange', ()=>setTimeout(_fitStage, 250));
  // …and watch the box itself, because most of what resizes it is not a window resize: opening a pane,
  // selecting a layer (the inspector is taller than "select a layer to edit it"), expanding an inspector
  // group, the on-screen keyboard. Without this the stage kept whatever size it was given at render time
  // and simply overflowed its box — measured at 455px tall inside a 178px slot, i.e. mostly off-screen.
  // Re-entrancy is not a risk: .mb-fit is `flex:1 1 0`, so its own box never depends on the stage inside it.
  let _fitRO = null;
  function _watchFit(){
    if(typeof ResizeObserver === 'undefined') return;
    const host = document.querySelector('.mb-fit'); if(!host) return;
    if(!_fitRO) _fitRO = new ResizeObserver(()=>{ if(_fitT) clearTimeout(_fitT); _fitT=setTimeout(_fitStage, 16); });
    _fitRO.disconnect();                 // render() replaces the element every time
    _fitRO.observe(host);
  }
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
    // Every mutation in the editor lands here, which is exactly why the full-length-bed rule is applied
    // here too: attaching it to the handful of call sites that happen to change the length today is how it
    // came to fire on "add a layer" and nothing else. A new way to re-time a clip now gets it for free.
    _syncMusicBeds();
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
      // ✂ the razor, on the bare S every NLE uses. Shift+S cuts the whole timeline. Premiere's Ctrl+K is
      // deliberately not offered as well — the Ctrl branch above hands every other combo back to the
      // browser on purpose, and Ctrl+K is its own search/address-bar shortcut in most of them.
      if(k === 's' || k === 'S'){ e.preventDefault(); (e.shiftKey ? cutAll : cutSelected)(); return; }
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

  // What the RENDER is told the project is. projEnd() deliberately ignores audio so a three-minute song
  // cannot stretch a six-second meme — the renderer truncates music instead. That is right for a bed and
  // wrong for a spoken LINE: cutting it at the end of the video means the sentence never finishes, which
  // is exactly "it added the voice but doesn't render it all".
  //
  // So the render length covers audio as well, while projEnd() stays as it is: the editing timeline, the
  // ruler and the reflow all keep behaving the way they do today, and only the exported length changes.
  // Music still can't run away with the project — a full-length bed tracks the timeline (_syncMusicBeds),
  // so it reaches exactly this far and no further; it is a line LONGER than the visuals that grows it.
  const renderEnd = () => P.layers.reduce(
    (m,l)=>Math.max(m, (+l.start||0)+(+l.dur||0)), 0) || projEnd();

  // Keep a FULL-LENGTH music bed full-length, whatever changed the length.
  //
  // This rule used to live inside addLayer(), so it only fired when a layer was ADDED — and every other
  // way the timeline can get shorter or longer (the inspector's Length field, dragging a clip's edge, the
  // speed control, an effect re-timing a clip, deleting a layer) left the bed at the length it happened to
  // have when it was dropped in. Because renderEnd() deliberately counts audio, that stale bed then PINNED
  // the export: you could shorten every clip in the project and the rendered video came back the length it
  // always was. "I keep changing the length of all the layers but the rendered never changes."
  //
  // Comparing against the project end as of the LAST save is what keeps "full-length" meaning full-length
  // rather than "long": a bed that spanned the old timeline is re-spanned to the new one, and a bed you
  // deliberately trimmed short — or a spoken line you deliberately ran PAST the visuals, which is the whole
  // reason renderEnd() counts audio — matches neither and is left exactly alone.
  //
  // The mark rides on P rather than in a module variable so that loading, importing, undo/redo and New
  // Project all carry (or correctly lack) it without five separate resets to keep in step. Absent = adopt
  // the current end and change nothing, which is right for a project we are seeing for the first time.
  function _syncMusicBeds(){
    const end = projEnd();
    const was = (P._spanEnd == null) ? end : +P._spanEnd;
    if(Math.abs(end - was) >= 0.005){
      P.layers.forEach(a=>{ if(a.type==='audio' && !(+a.start||0) && Math.abs((+a.dur||0) - was) < 0.06)
        a.dur = +end.toFixed(2); });
    }
    P._spanEnd = end;
  }

  // ---------- the MASTER TIMELINE ----------
  // Media clips (image/video) form ONE ordered sequence that plays back-to-back — the way every video editor
  // works. Their `start` is DERIVED from that order, never hand-typed: drop a clip anywhere on the timeline
  // and the rest reflow around it (no gaps, no accidental overlaps). TEXT is excluded on purpose — a caption
  // is an overlay pinned ON the footage, so it keeps its own free start/duration.
  const mediaSeq = () => P.layers.filter(_isVisual)
    .sort((a,b)=>((+a.start||0)-(+b.start||0)) || (P.layers.indexOf(a)-P.layers.indexOf(b)));
  // Crossfade length, in seconds, between consecutive clips. 0 = hard cuts (the default and what every
  // existing project has). This is a PROJECT setting rather than per-layer because a transition belongs to
  // the JOIN between two clips, and the timeline only has one join between any two neighbours.
  const _xfade = () => clamp(P.xfade == null ? 0 : P.xfade, 0, 2);
  // Lay the clips out back-to-back — or OVERLAPPING by the crossfade length, which is what makes the
  // dissolve possible at all: the renderer blends with alpha ramps on each clip's own stream, so the two
  // clips have to be on screen at the same time for there to be anything to blend. `xin`/`xout` are the
  // ramps, stamped here (and cleared when the crossfade is off) so the render never has to guess which
  // joins are transitions.
  function resequence(seq){
    const xf = _xfade();
    const list = seq || mediaSeq();
    let t = 0;
    list.forEach((l, i)=>{
      l.start = +t.toFixed(2);
      const d = +l.dur || 0;
      // Never advance by less than a token amount, or a clip shorter than the crossfade would put the
      // next one at (or before) its own start and the sequence would stop moving forward.
      t += (i < list.length - 1) ? Math.max(0.1, d - xf) : d;
      l.xin  = (xf > 0 && i > 0) ? xf : 0;
      l.xout = (xf > 0 && i < list.length - 1) ? xf : 0;
    });
  }
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
  // Playback speed of a VIDEO layer. `dur` is ALWAYS the layer's slot on the timeline, and the renderer
  // feeds it `dur x speed` seconds of source — so at any speed but 1, slot seconds and source seconds are
  // different units, and everything that converts between them (the trim handles, _setSpeed) has to say so.
  // Changing the speed in the editor rescales the slot to keep the trimmed FOOTAGE (see _setSpeed).
  const _speedOf = (l) => clamp(l.speed == null ? 1 : l.speed, 0.25, 4);
  // What the speed did to the clip's slot, in words. Speed is the one control here that changes a layer's
  // LENGTH as a side effect, so it has to say so — otherwise the clip bar shrinking on the timeline looks
  // like something else moved it.
  const _slotNote = (l) => {
    const sp = _speedOf(l);
    if(sp === 1) return '';
    return ` · ${(+l.dur||0).toFixed(1)}s slot for ${(((+l.dur||0)*sp)).toFixed(1)}s of footage`;
  };
  // Export format. Kept on the project so it survives a reload like every other choice here.
  const _fmt = () => (['mp4','gif','png'].indexOf(P.fmt) >= 0 ? P.fmt : 'mp4');

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
      // DEFAULTS ONLY — these run after `extra` is merged, so writing them unconditionally silently threw
      // away a caller's explicit choice. That is what happened to the voice-over: it asks for volume 1 and
      // no fade (a voice under a music bed at 0.6 is the thing you cannot make out) and got 0.6 + a fade.
      if(!extra || extra.volume == null) l.volume = 0.6;   // sit UNDER the clips' own sound — amix runs normalize=0
      if(!extra || extra.fade == null) l.fade = true;      // truncation at the end of the timeline is a hard cut otherwise
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
    if(_isVisual(l)) resequence();   // join the master timeline exactly back-to-back (no drift from `tail`)
    // Adding a clip makes the meme longer, and a soundtrack that was covering the whole thing would
    // otherwise stop early — reported as "the music cuts out". save() re-spans it (see _syncMusicBeds),
    // along with every other way the length changes; this used to be a copy of that rule living here,
    // which is precisely why only ADDING a layer ever kept the bed honest.
    save(); return l;
  }

  // An OVERLAY layer, as opposed to a CLIP in the master sequence. addLayer() is wrong for this: it appends
  // media at the END of the timeline and then resequences, so a sticker added to a 2-clip build landed after
  // both of them — on screen over nothing, which reads as "the sticker never arrived". An overlay starts at 0
  // and spans the build instead, and goes in BELOW the captions so it cannot bury one.
  function addOverlay(extra){
    if(P.layers.length >= 24){ toast('24 layers is the limit'); return null; }
    snap();
    const l = Object.assign({
      id: nid(), type:'image', src:'', name:'',
      start: 0, dur: +Math.max(projEnd(), 1).toFixed(2), trim: 0,
      x: 0, y: 0, w: P.w, h: Math.round(P.h/2),
      opacity: 1, effect: 'none', volume: 1, mute: false, fit: 'contain',
      flipH: false, flipV: false, rotate: 0, sound: '', soundVolume: 1,
      text: '', size: 64, color: '#ffffff', stroke: '#000000', align: '',
    }, extra || {});
    const firstText = P.layers.findIndex(x => x.type === 'text');
    P.layers.splice(firstText < 0 ? P.layers.length : firstText, 0, l);
    sel = l.id;
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

  // ---------- ✂ cut (split) ----------
  // Where the playhead is, in seconds. The scrubber IS the playhead — seek() is driven from it, and the
  // timeline ruler writes to it — so there is no second source of truth to keep in step.
  const _playhead = () => { const s = document.getElementById('mb-scrub'); return s ? (+s.value||0) : 0; };

  // The razor. A layer is cut IN TWO at the playhead: the left half stays this layer, the right half becomes
  // a new one starting exactly where the first now ends. It is the only way to take a piece out of the
  // MIDDLE of something (cut twice, delete what is between), to caption/speed up/restyle just half of a
  // take, or to end a music bed early — none of which the trim handles can do, because they only ever move
  // the two OUTER ends of a clip.
  //
  // Both halves are ordinary layers filling exactly the span the original filled, so nothing downstream has
  // to know a cut happened: the renderer, the save format, undo and the clip order are all untouched, and
  // no resequence is needed (the pieces are back-to-back by construction, so no other clip moves).
  //
  // The one thing that has to be right is where the second half starts INSIDE ITS SOURCE. A video's source
  // is walked at the clip's SPEED — the same conversion bindTrim does — so a 2× clip cut one second in
  // resumes two seconds into the footage; a song is walked at 1×; a still and a caption have no source
  // clock at all and simply take the rest of the slot.
  const MIN_PIECE = 0.1;                    // the shortest a piece may be (the Length field's own floor)
  const _canSplit = (l, t) => !!l && (t - (+l.start||0)) >= MIN_PIECE
                                  && ((+l.start||0) + (+l.dur||0) - t) >= MIN_PIECE;
  function splitLayer(l, t){
    if(!_canSplit(l, t)) return null;
    if(P.layers.length >= 24){ toast('24 layers is the limit — no room for the second half'); return null; }
    const st = +l.start||0, du = +l.dur||0;
    const left = +(t - st).toFixed(2);
    const c = Object.assign({}, l, { id: nid() });
    c.start = +(st + left).toFixed(2);
    c.dur   = +(du - left).toFixed(2);
    if(l.type === 'video')      c.trim = +((+l.trim||0) + left * _speedOf(l)).toFixed(3);
    else if(l.type === 'audio') c.trim = +((+l.trim||0) + left).toFixed(3);
    // A one-shot sound effect is pinned to the START of a layer, so leaving it on the second half would
    // fire it again at the cut — a sound the project never had.
    c.sound = '';
    // The cut is a HARD join. The crossfade ramps belong to the OUTER edges, where the neighbouring clips
    // still are: `c` inherits xout from the copy and `l` keeps xin, so only the two inner ramps are cleared.
    // (A music bed's "Fade in/out" is one flag for both ends and deliberately rides along unchanged — a bed
    // cut in half therefore fades at the join too, which is what unticking it on a half is for.)
    l.dur = left; l.xout = 0; c.xin = 0;
    const i = P.layers.indexOf(l);
    P.layers.splice(i < 0 ? P.layers.length : i + 1, 0, c);
    return c;
  }

  // ✂ on ONE layer — the selected one. The SECOND half is left selected, because cut-then-delete-the-tail
  // is the move you are nearly always making, and it is the piece you cannot otherwise get at by tapping
  // (both halves sit under the playhead at the moment of the cut).
  function cutSelected(){
    const l = P.layers.find(x=>x.id===sel);
    if(!l){ toast('select a layer first, then ✂ cuts it at the playhead'); return; }
    const t = _playhead();
    if(!_canSplit(l, t)){
      toast(`move the playhead into this ${l.type==='audio'?'track':'layer'} first — ✂ cuts where it stands`);
      return;
    }
    snap();
    const c = splitLayer(l, t);
    if(!c){ unsnapIfUnchanged(); return; }
    sel = c.id;
    save(); render();
    toast(`cut at ${t.toFixed(1)}s — the second half is selected`);
  }

  // ✂ across the WHOLE timeline: every layer the playhead is standing on, in ONE undo step. This is the
  // "cut here" of an editor — split everything at a moment, then move or delete either side as a piece.
  function cutAll(){
    const t = _playhead();
    const hits = P.layers.filter(l=>_canSplit(l, t));
    if(!hits.length){ toast('nothing under the playhead to cut'); return; }
    const room = 24 - P.layers.length;
    if(room <= 0){ toast('24 layers is the limit — no room for the second halves'); return; }
    const todo = hits.slice(0, room);
    groupEdit(()=>{ todo.forEach(l=>splitLayer(l, t)); });
    save(); render();
    toast(todo.length < hits.length
      ? `cut ${todo.length} of ${hits.length} layers — 24 layers is the limit`
      : `cut ${todo.length} layer${todo.length===1?'':'s'} at ${t.toFixed(1)}s`);
  }

  // ---------- rendering the UI ----------
  // What the Layer tab is called right now — "✎ Text" reads as a live thing you can go and edit, where a
  // permanent "Layer" gives no hint that tapping a clip did anything.
  function _selName(){
    const l = P.layers.find(x=>x.id===sel);
    if(!l) return 'Layer';
    return l.type==='text' ? 'Text' : l.type==='audio' ? 'Music' : l.type==='video' ? 'Video' : 'Image';
  }
  function tabsInner(){
    const t = [['layer','✎ '+_selName()], ['timeline','🎞 Timeline'], ['canvas','⚙︎ Canvas']];
    if(_hasResult) t.push(['result','🎬 Result']);
    return t.map(([k,lbl])=>`<button class="mb-tab${_tab===k?' on':''}" data-tab="${k}" role="tab"
      aria-selected="${_tab===k}">${enc(lbl)}</button>`).join('');
  }

  function view(){
    return `
    <div class="mb-wrap">
      <!-- ONE row, on every width. The two wrapped rows this replaces cost ~6 rows of a phone screen before
           you saw any of the meme, and everything that configures the PROJECT moved into the ⚙︎ Canvas pane.
           What is left is the three things you actually do: add, undo, render.
           There is ONE add button, not two. 🖼️ Media and ➕ More split the sources down a line only the
           person who wrote it could see — a sticker, a backing track and a layout were behind More, a photo
           and the Blossom drive behind Media — so finding anything meant opening one, closing it, and
           opening the other. They are one sheet now, still visibly grouped inside it. -->
      <div class="mb-bar">
        <div class="mb-barmain">
          <!-- The emoji is in its own span so a narrow phone can drop it and keep the WORD: a labelled
               button beats a pictogram nobody has to guess at. -->
          <button class="btn btn-neon small" id="mb-add-media" title="A photo, a clip, music, a sticker, an effect or a ready-made layout"><svg class="ic b-ic mb-e" aria-hidden="true"><use href="#i-image"></use></svg>Media</button>
          <button class="btn btn-cyan small" id="mb-add-text"><svg class="ic b-ic mb-e" aria-hidden="true"><use href="#i-text"></use></svg>Text</button>
          <button class="btn btn-cyan small mb-icon" id="mb-undo" title="Undo (Ctrl+Z)" aria-label="Undo" ${_hist.length?'':'disabled'}>↶</button>
          <button class="btn btn-cyan small mb-icon" id="mb-redo" title="Redo (Ctrl+Shift+Z)" aria-label="Redo" ${_future.length?'':'disabled'}>↷</button>
        </div>
        <span class="muted small mb-status" id="mb-status"></span>
        <div class="mb-barend">
          <!-- Render is the only control that must never scroll out of reach, so it is its own group. The
               export format moved to ⚙︎ Canvas: it is set once per project, and the button already SAYS what
               it will produce (📷 Still / 🎞️ GIF / 🎬 Render), which is what it was next to the format for. -->
          <button class="btn btn-neon small" id="mb-render">${_fmt()==='png'?'📷 Still':(_fmt()==='gif'?'🎞️ GIF':'🎬 Render')}</button>
        </div>
      </div>

      <div class="mb-body">
        <div class="mb-left">
          <!-- .mb-fit is the box the stage is fitted INTO (see _fitStage) — it owns the leftover height, the
               stage owns the project's aspect ratio, and neither has to know about the other. -->
          <div class="mb-fit">
            <div class="mb-stage" id="mb-stage" style="aspect-ratio:${P.w}/${P.h};background:${P.bg}">
              ${_stageOrder().map(stageEl).join('')}
              <!-- Snap guides: shown only while a drag is actually snapped to that line (see applySnaps). -->
              <i class="mb-guide mb-gv" id="mb-gv" style="display:none"></i>
              <i class="mb-guide mb-gh" id="mb-gh" style="display:none"></i>
            </div>
          </div>
          <!-- Music beds have nothing to show on the stage, but the PREVIEW has to be able to hear them —
               otherwise you can only judge the mix by rendering. One hidden <audio> per track, driven by
               the same playhead as the video layers (see seekAudio). -->
          <div class="mb-audios" id="mb-audios" aria-hidden="true">
            ${P.layers.filter(l=>l.type==='audio').map(l=>`<audio data-id="${l.id}" src="${enc(l.src)}" preload="metadata"></audio>`).join('')}
          </div>
          <div class="mb-playrow">
            <button class="btn btn-ghost small" id="mb-play" aria-label="Play"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg></button>
            <input type="range" id="mb-scrub" class="mb-scrub" min="0" max="${projEnd().toFixed(2)}" step="0.05" value="0">
            <span class="muted small" id="mb-time">0.0s / ${projEnd().toFixed(1)}s</span>
          </div>
        </div>

        <div class="mb-tabs" id="mb-tabs" role="tablist">${tabsInner()}</div>

        <div class="mb-pane${_tab==='layer'?' on':''}" data-pane="layer" id="mb-pane-layer" role="tabpanel">
          <div class="mb-inspector" id="mb-inspector">${inspector()}</div>
        </div>
        <div class="mb-pane${_tab==='timeline'?' on':''}" data-pane="timeline" id="mb-pane-timeline" role="tabpanel">
          ${timelinePane()}
        </div>
        <div class="mb-pane${_tab==='canvas'?' on':''}" data-pane="canvas" id="mb-pane-canvas" role="tabpanel">
          ${canvasPane()}
        </div>
        <div class="mb-pane${_tab==='result'?' on':''}" data-pane="result" id="mb-pane-result" role="tabpanel">
          <div id="mb-result"></div>
        </div>
      </div>
    </div>`;
  }

  // 🎞 Timeline pane — the clip lanes plus the two things you do TO the sequence (lay it out, dissolve
  // between clips) and the zoom. Arrange and the crossfade used to sit in the top toolbar and in a bar of
  // their own respectively; both are timeline edits, so they live with the timeline.
  function timelinePane(){
    const any = P.layers.some(_isVisual);
    return `${any ? `<div class="mb-tlbar">
        <button class="btn btn-cyan small" id="mb-arrange" title="Lay every clip back-to-back in its current order">⇄ Arrange</button>
        ${/* The razor for the WHOLE timeline. The per-layer ✂ lives in the layer panel next to Duplicate;
              this one is here because "cut here" is about the moment, not about a selection. */''}
        <button class="btn btn-cyan small" id="mb-cutall" title="Cut every layer standing under the playhead in two (Shift+S). Cut twice and delete the middle piece to take a section out of the meme.">✂ Cut here</button>
        <label class="mb-tlf" title="Dissolve between consecutive clips. ⇄ Arrange overlaps them by this much so there is something to blend.">⇄
          <select class="input" id="mb-xfade">
            ${[0,0.25,0.5,0.75,1,1.5].map(v=>`<option value="${v}" ${Math.abs(_xfade()-v)<0.01?'selected':''}>${v?v+'s':'cut'}</option>`).join('')}
          </select>
        </label>
        <span class="mb-spacer"></span>
        <span class="mb-zoom">
          <button class="mb-zb" id="mb-zoomout" title="Zoom out" aria-label="Zoom the timeline out">−</button>
          <b id="mb-zoomlbl">${_zoomLbl(_zoom())}</b>
          <button class="mb-zb" id="mb-zoomin" title="Zoom in — a long build is unreadable at 1×" aria-label="Zoom the timeline in">+</button>
          ${/* One click back to "show me the whole thing". A fixed px/second lane means a long build runs
                off the side, and hunting for how many times to press − to get it back is the tedious part. */''}
          <button class="mb-zb mb-zfit" id="mb-zoomfit" title="Fit the whole build in the timeline" aria-label="Fit the timeline to the build">⤢</button>
        </span>
        <span class="muted small mb-tlhint">Drag a clip to move it, or its edges to trim. Tap the ruler to move the playhead, then ✂ to cut there.</span>
      </div>` : ''}
      <div class="mb-timeline" id="mb-timeline">${timelineInner()}</div>`;
  }

  // ⚙︎ Canvas pane — the shape of the frame, what shows around a photo that doesn't fill it, and the
  // project itself. All of it was in the second toolbar, where a 42px colour swatch and a shape dropdown
  // sat one thumb-width from Render.
  function canvasPane(){
    const custom = !PRESETS.some(([n,w,h])=>P.w===w&&P.h===h);
    return `
      <div class="mb-secttl">Canvas shape</div>
      <div class="mb-sizes">
        ${PRESETS.map(([n,w,h])=>`<button class="mb-szb${(P.w===w&&P.h===h)?' on':''}" data-size="${w}x${h}"><b>${enc(n)}</b><i>${w}×${h}</i></button>`).join('')}
      </div>
      ${custom ? `<div class="muted small">Currently ${P.w}×${P.h} — a custom shape (⇲ Canvas to this photo). Picking one above rescales every layer to it.</div>` : ''}
      <label class="mb-bgrow" title="This is what shows AROUND a photo that doesn't fill the frame — the bars.">
        <input type="color" id="mb-bg" value="${enc(P.bg)}" aria-label="Canvas background colour">
        <span>Background — the bars around a photo that doesn’t fill the frame</span>
      </label>
      <div class="mb-secttl">Export</div>
      <!-- A meme is very often a PICTURE, and plenty of places still only take a GIF. -->
      <label class="mb-f"><span>Format — what 🎬 Render produces</span>
        <select class="input" id="mb-fmt" title="MP4 keeps the sound; GIF loops silently; Still is one frame at the playhead">
          <option value="mp4" ${_fmt()==='mp4'?'selected':''}>MP4 video (with sound)</option>
          <option value="gif" ${_fmt()==='gif'?'selected':''}>GIF (looping, silent)</option>
          <option value="png" ${_fmt()==='png'?'selected':''}>Still image (frame at the playhead)</option>
        </select></label>
      <div class="mb-secttl">This project</div>
      <div class="muted small mb-dbg">${enc(P.name || 'Untitled')} · ${P.layers.length} layer${P.layers.length===1?'':'s'} · ${P.w}×${P.h} · ${projEnd().toFixed(1)}s</div>
      <button class="btn btn-cyan small full" id="mb-proj">📂 Save, open, rename, start new…</button>`;
  }


  // The timeline's scrolling CONTENT. Everything that has to line up with a clip lane — the ruler and the
  // playhead — lives in here rather than over the .mb-timeline box, because on a phone the box scrolls
  // horizontally: a playhead positioned against the outer element would slide away from the lanes it is
  // supposed to be marking the moment you scrolled.
  // Timeline ZOOM. 1 = the whole project across the lane, which is all there was: a 60-second build put
  // every clip into a sliver you could neither read nor grab, and trimming by half a second was guesswork.
  // Implemented as the WIDTH of the scrolling content — every clip bar and ruler tick is positioned in %,
  // and the playhead measures a real lane, so they all scale from this one number with no other maths.
  // A LADDER rather than a multiplier, so the label is always a round number you can aim back at.
  // Below 1x matters now: the lane is a fixed px/second, so 1x is a SCALE, not "the whole project".
  // Without these a long build could not be seen end to end at all — you could only scroll it.
  const _ZOOMS = [0.125, 0.25, 0.5, 1, 2, 3, 4, 6, 8, 12];
  const ZOOM_MIN = 0.05, ZOOM_MAX = 12;
  const _zoom = () => clamp(P.zoom == null ? 1 : P.zoom, ZOOM_MIN, ZOOM_MAX);
  const _zoomLbl = (z) => (+(+z).toFixed(2)) + '\u00d7';
  function setZoom(z){
    const prev = _zoom();
    P.zoom = clamp(z, ZOOM_MIN, ZOOM_MAX);
    if(P.zoom === prev) return;
    save();
    const port = document.getElementById('mb-timeline');
    const s = document.getElementById('mb-scrub');
    repaint('timeline');          // rebuilds the lanes; the toolbar (and its listeners) survives
    const lbl = document.getElementById('mb-zoomlbl');
    if(lbl) lbl.textContent = _zoomLbl(P.zoom);
    // Keep the playhead on screen. Zooming in on a timeline and being left looking at second 0 while the
    // playhead is off to the right is the thing that makes editor zoom feel broken.
    if(port){
      const ph = document.getElementById('mb-ph');
      const x = ph ? parseFloat(ph.style.left || '0') : 0;
      port.scrollLeft = Math.max(0, x - port.clientWidth / 2);
    }
    if(s) syncScrub();
  }

  // How many SECONDS the timeline lane spans. Deliberately NOT projEnd(): a lane stretched to exactly the
  // project length is a scale that changes every time any clip's length changes, so shortening ONE clip
  // re-laid-out every OTHER bar — "it needs to stop changing layers when I adjust the length of another".
  //
  // The lane is now a fixed number of pixels PER SECOND (--mb-pps, times the zoom), and this is only how
  // far it runs. A second is the same width whatever else is on the timeline, so a bar's position and
  // width depend on that layer alone and nothing moves when a different layer is edited. The floor keeps
  // a two-second meme from collapsing to a stub of a lane, and the headroom leaves somewhere to drag a
  // clip TO — the trailing empty space grows and shrinks, which is the only thing an edit now moves.
  const TL_MIN_SPAN = 8, TL_TAIL = 2;
  const tlSpan = () => Math.max(TL_MIN_SPAN, projEnd() + TL_TAIL);
  // The zoom at which the WHOLE build just fits the timeline panel. Read back from the live element and
  // the same custom properties the lane is sized from, so it cannot drift from the CSS (or from whichever
  // breakpoint is in force). Returns null before the timeline exists — callers just do nothing.
  //
  // Applied only when ENTERING the builder and on an explicit ⤢. Never on an edit: re-fitting as you work
  // is precisely the rescale-on-every-change that made every other bar move when you re-timed one clip,
  // and it would undo the whole point of the fixed scale. Deliberately NOT on resize either — a phone
  // fires one every time the keyboard opens, so the scale would jump out from under a value being typed.
  function fitZoom(){
    const port = document.getElementById('mb-timeline');
    const inner = document.getElementById('mb-tlinner');
    if(!port || !inner) return null;
    const pcs = getComputedStyle(port), ics = getComputedStyle(inner);
    const avail = port.clientWidth - parseFloat(pcs.paddingLeft || 0) - parseFloat(pcs.paddingRight || 0);
    const nameW = parseFloat(ics.getPropertyValue('--mb-name-w')) || 72;
    const pps = parseFloat(ics.getPropertyValue('--mb-pps')) || 56;
    const lane = avail - nameW;
    if(!(lane > 40) || !(pps > 0)) return null;
    return clamp(lane / (tlSpan() * pps), ZOOM_MIN, ZOOM_MAX);
  }
  // Set by the entry points below, consumed once by render(). A flag rather than a call inside render()
  // because render() is the FULL rebuild that most edits trigger — fitting there would re-fit on every
  // delete, Fill, restack and template, which is the jitter this whole design removes.
  let _fitNext = true;
  function _applyFitIfPending(){
    if(!_fitNext) return;
    _fitNext = false;
    const z = fitZoom();
    // Only ever zoom OUT to fit on entry. Blowing a two-second meme up to 6x because it "fits" is not what
    // fit means to anyone — the point is that nothing is off-screen, not that everything is huge.
    if(z && z < _zoom() - 0.01) setZoom(z);
  }
  // The NARROWEST --mb-pps in client.css (the phone value). Only the ruler needs a number here, and only
  // to decide tick spacing — picking the smallest means labels that clear each other on a phone clear
  // each other everywhere, so this cannot go subtly wrong on one breakpoint. The LAYOUT never reads it:
  // widths are calc()ed against the property itself, so the real scale lives in exactly one place.
  const TL_PPS_MIN = 44;

  function timelineInner(){
    if(!P.layers.length) return '<div class="mb-tlinner"><div class="muted small mb-empty">No layers yet — add media or text to start.</div></div>';
    // calc() against the same custom properties the rows use, rather than a measured pixel count: the name
    // column narrows at the 820px breakpoint and --mb-pps drops on a phone, and letting CSS resolve both
    // means the lane cannot drift out of step with the column beside it on a resize.
    // The LANE gets an exact width rather than "whatever is left over" (flex:1 1 auto). Left over is what
    // made the scale depend on the port, so a short project stretched its seconds to fill the panel and a
    // long one squeezed them — the same rescale-on-every-edit from the other direction. Pinning it lets the
    // inner also carry min-width:100% (so the row still reaches across a wide panel) WITHOUT that extra
    // width leaking back into the scale: it becomes trailing space after the lane, not more seconds.
    const lane = `calc(var(--mb-pps) * ${(tlSpan()*_zoom()).toFixed(3)})`;
    const w = `--mb-lane-w:${lane};width:calc(var(--mb-name-w) + ${lane})`;
    return `<div class="mb-tlinner" id="mb-tlinner" style="${w}">
      ${rulerEl()}
      ${_rowOrder().map(trackEl).join('')}
      <i class="mb-ph" id="mb-ph"><b></b></i>
    </div>`;
  }

  // A seconds ruler above the tracks. Built as a .mb-track so its lane is the SAME box as every clip lane
  // (same name-column width, same gap) — that is what makes a tick at 3s sit exactly above the clip that
  // starts at 3s, with no measuring.
  function rulerEl(){
    const total = tlSpan();
    // Spacing follows the ZOOM alone. It used to be derived from the project length, because the lane was
    // stretched to fit it and a second was worth a different number of pixels in every project. Now a
    // second is a fixed width (--mb-pps), so how far apart two labels sit depends only on how far apart
    // their times are — and a ruler that re-spaces itself when a clip is trimmed is the same jitter the
    // fixed scale exists to remove. ~55px is the room a "12.5s" label needs.
    const z = Math.max(1, _zoom());
    const step = [0.1, 0.25, 0.5, 1, 2, 5, 10, 30].find(s => s * z * TL_PPS_MIN >= 55) || 60;
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
    const total = tlSpan();                 // the LANE's span, so the playhead tracks the same scale as the bars
    const frac = clamp(t, 0, total) / total;
    ph.style.display = '';
    ph.style.left = (lane.offsetLeft + frac * lane.offsetWidth) + 'px';
  }

  // Tap or drag anywhere on the ruler to move the playhead — the standard way to scrub a timeline, and
  // until now the ONLY scrubber was the slider above the stage, in a different part of the page.
  function _rulerSeek(clientX){
    const lane = document.getElementById('mb-rlane'); if(!lane) return;
    const r = lane.getBoundingClientRect(); if(!r.width) return;
    const t = clamp((clientX - r.left) / r.width, 0, 1) * tlSpan();
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
    const mk = _maskCss(l, ofit);
    const inner = l.type==='video'
      ? `<video src="${enc(l.src)}#t=0.1" muted playsinline preload="auto" style="object-fit:${ofit}${mk}${_xformCss(l)}"></video>`
      : `<img src="${enc(l.src)}" alt="" style="object-fit:${ofit}${mk}${_xformCss(l)}">`;
    return `<div class="mb-item mb-media${s}" data-id="${l.id}" style="${pos}${size}opacity:${l.opacity}">${inner}<i class="mb-h"></i></div>`;
  }

  function trackEl(l){
    const total = tlSpan();
    let left = (l.start/total*100), wid = Math.max(3, l.dur/total*100);
    // Music is the ONE layer that can run past the end of the lane, and an unclamped bar is a real layout
    // break, not a cosmetic one: a 3-minute song on a 6-second meme is a 3000%-wide absolutely-positioned
    // div in a lane with no overflow clipping. Clamp it to the lane so the row survives.
    let cut = false;
    if(l.type==='audio' && left + wid > 100){
      left = Math.min(left, 97); wid = Math.max(3, 100 - left); cut = true;
    }
    // …and the LABEL is about the meme, not the lane. The bar is clamped at the lane's span (projEnd + a
    // little headroom), but what matters to the user is where the VIDEO ends — and a bed running past that
    // is no longer "cut off": renderEnd() counts audio, so it makes the export longer instead. Saying the
    // opposite of what the renderer does is what hides a stale music bed holding the export at an old
    // length. See _syncMusicBeds.
    const _vEnd = projEnd(), _aEnd = (+l.start||0) + (+l.dur||0);
    const over = l.type==='audio' && _aEnd > _vEnd + 0.05;
    // Every row is [grip][tile][bar], in that order, whatever the layer is. The tile is the SAME 52x34 box
    // for all four types — a frame for a picture or clip, a glyph for text and music — because the previous
    // mix (media showed a thumbnail, text and music showed a truncated string) meant no two rows lined up
    // and the list read as four different widgets stacked up.
    const name = l.type==='text' ? (l.text||'text').replace(/\s+/g,' ').trim() : (l.name || srcName(l.src));
    // Is the bar too short to hold a name as well as its duration? wid is a % of the lane and the lane
    // grows with the zoom, so the zoom has to be in the test — otherwise a clip that is genuinely wide at
    // 8x would still be treated as a sliver and hide a name it has ample room for.
    const narrow = wid * _zoom() < 15;
    // Sprite icons, not text glyphs. ⇅ / ▶ / ♪ are exactly the characters a device is free not to have —
    // they render as ☐ (or worse, as a colour emoji that fights the theme) — and the row is the one place
    // in the builder where a missing glyph reads as a broken layer rather than a missing decoration.
    const ic = n => `<svg class="ic" aria-hidden="true"><use href="#i-${n}"></use></svg>`;
    const tile = l.type==='text'
      ? `<span class="mb-tile mb-tile-gl" aria-hidden="true">${ic('text')}</span>`
      : (l.type==='audio'
        ? `<span class="mb-tile mb-tile-gl mb-tile-aud" aria-hidden="true">${ic('music')}</span>`
        : l.type==='video'
          ? `<span class="mb-tile"><video src="${enc(l.src)}#t=0.1" muted playsinline preload="metadata"></video><i class="mb-tvid">${ic('play')}</i></span>`
          : `<span class="mb-tile"><img src="${enc(l.src)}" alt="" loading="lazy"></span>`);
    return `<div class="mb-track${l.id===sel?' sel':''}${l.type==='audio'?' mb-track-aud':''}" data-id="${l.id}">
      <div class="mb-trackname" title="${enc(name)}">
        ${/* Its OWN handle, not the whole row: the drag surface needs touch-action:none to be a drag at all
             on a phone, and putting that on the tile would eat vertical PAGE scrolling every time a finger
             happened to start there — an accidental restack instead of a scroll. Music has no stacking
             order, so its grip is present but INERT: removing the element instead would slide that row's
             tile 20px left of every other one, and a list whose rows don't share a left edge is the thing
             that reads as untidy no matter how good the rest of it looks. */''}
        <i class="mb-rgrip${l.type==='audio'?' mb-rgrip-off':''}"${l.type==='audio'?' aria-hidden="true"':' title="Drag to restack — what is drawn on top of what" aria-hidden="true"'}>${ic('menu')}</i>${tile}
      </div>
      <div class="mb-lane">
        <div class="mb-clip${cut?' mb-cut':''}${narrow?' mb-clip-sm':''}" data-id="${l.id}" style="left:${left.toFixed(3)}%;width:${wid.toFixed(3)}%"${over?` title="${l.dur.toFixed(1)}s of music — ${(_aEnd-_vEnd).toFixed(1)}s longer than the clips, so the video is ${_aEnd.toFixed(1)}s. “⇔ Span the whole meme” fits it to ${_vEnd.toFixed(1)}s."`:''}>
          <i class="mb-grip mb-grip-l" data-grip="l"></i>
          ${/* The NAME goes in the bar, not the left column. The bar is the widest thing in the row and was
               carrying one number; the column was 132px of truncated filename that collapsed to nothing on
               a phone, so two similar photos were two identical rows. A bar too short to hold both drops
               the name outright rather than ellipsising it to "b…" — the duration is the part you still
               need, and it is the part the grips would otherwise sit on top of. */''}
          ${narrow ? '' : `<span class="mb-cname">${enc(name)}</span>`}
          <span class="mb-cdur">${l.dur.toFixed(1)}s${over ? ' ⤍' : ''}</span>
          <i class="mb-grip mb-grip-r" data-grip="r"></i>
        </div>
      </div>
    </div>`;
  }

  // CSS equivalent of the renderer's ERASE MASK ('' when nothing is erased), so the stage shows the
  // real result instead of making you render to find out.
  //
  // `mask-size` mirrors `object-fit` — and that one line is why the preview matches the export. The mask
  // has the SAME aspect ratio as the source (it is painted in source space), so `contain` letterboxes it
  // by exactly the ratio `object-fit:contain` letterboxes the picture, and `cover` crops it by exactly
  // the ratio the picture is cropped. That is the browser's spelling of the renderer's shared
  // _fit_chain. A fixed `mask-size:100% 100%` would stretch the mask to the box and misalign the erase
  // on every layer whose shape differs from its box — which is most of them.
  //
  // It goes on the SAME element as the flip/rotate transform, so the erased region turns with the
  // picture — matching the renderer, which masks before those filters.
  //
  // A raster mask-image is read by its ALPHA (mask-mode: match-source), which is what we encode:
  // opaque = keep, transparent = erased.
  function _maskCss(l, ofit){
    if(!l || !l.mask) return '';
    // This lands in a CSS url() inside a style attribute, which enc() alone does NOT make safe: it
    // escapes a quote to &quot;, and the HTML parser hands the attribute VALUE back with a real quote
    // in it — closing url(" early and letting the rest of the string be read as CSS. A project is a
    // shareable Blossom document (Save/Open), so its fields are not automatically ours. Allow only a
    // plain http(s) or root-relative URL with nothing that can terminate the literal.
    if(!/^(https?:\/\/|\/)[^\s"'()\\<>]+$/i.test(String(l.mask))) return '';
    const u = `url("${enc(l.mask)}")`;
    return `;-webkit-mask-image:${u};mask-image:${u}`
         + `;-webkit-mask-size:${ofit};mask-size:${ofit}`
         + `;-webkit-mask-position:center;mask-position:center`
         + `;-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat`;
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
          <button class="btn btn-cyan small" id="mb-split" title="Cut this track in two where the playhead is (S) — the halves are separate tracks you can move, retime or delete on their own">✂ Cut</button>
          <button class="btn btn-cyan small" id="mb-dup" title="Make a second copy of this track">⧉ Duplicate</button>
          <button class="btn btn-danger small" id="mb-del"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>
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
    // The panel is ~20 controls for a video layer. Shown all at once it is a wall you scroll rather than
    // read — and on a phone it was most of the reason the builder felt unusable. Split it: the handful you
    // reach for on every layer stay at the top, the rest go into three named, collapsible groups whose
    // open/closed state is remembered (_sec) so the panel comes back the way you left it.
    return `
      <div class="mb-insp-hd">
        <b>${isText?'Text':(l.type==='video'?'Video':'Image')} layer</b>
        <span class="mb-insp-acts">
          <button class="btn btn-cyan small" id="mb-split" title="Cut this layer in two where the playhead is (S) — the halves are separate layers, so you can trim, restyle or delete either one. Cut twice and delete the middle to drop a piece out.">✂ Cut</button>
          <button class="btn btn-cyan small" id="mb-dup" title="Copy this layer — same clip, size, effect, sound and timing — as a new layer just above it">⧉ Duplicate</button>
          <button class="btn btn-danger small" id="mb-del"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>
        </span>
      </div>
      ${isText ? `
        <label class="mb-f"><span>Text</span><textarea class="input" id="mb-f-text" rows="2">${enc(l.text)}</textarea></label>
        <label class="mb-f"><span>Size</span><input class="input" type="number" id="mb-f-size" min="8" max="400" value="${l.size}"></label>
        <div class="mb-frow">
          <label class="mb-f"><span>Colour</span><input type="color" id="mb-f-color" value="${enc(l.color)}"></label>
          <label class="mb-f"><span>Outline</span><input type="color" id="mb-f-stroke" value="${enc(l.stroke)}"></label>
        </div>` : `
        ${l.type==='video' ? trimWidget(l) + `
        <button class="btn btn-cyan small full" id="mb-prev-clip" title="Play just this clip in the preview above"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg> Preview clip</button>` : ''}
        <div class="mb-frow"><button class="btn btn-cyan small" id="mb-fit" title="Show the whole photo inside the canvas. Bars appear wherever its shape differs from the canvas — they are the canvas background.">⛶ Whole photo (bars)</button><button class="btn btn-cyan small" id="mb-fill" title="Scale up until the canvas is full and crop the overflow — no bars, but the edges are cut off">✂ Fill &amp; crop</button></div>
        ${(l.type!=='image' && l.fxPose) ? `<button class="btn btn-cyan small full" id="mb-talk" title="Make this character say a line in one of your cloned voices. It is animated from the character's own artwork, so the pose stays exactly as it is."><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg>Make it talk</button>` : ''}
        ${l.type==='image' ? `<button class="btn btn-cyan small full" id="mb-nobg" title="Cut the subject out of this photo and drop the background, so the layers underneath show through. Same cut-out the removebackground command does. Undo with ↺ below."><svg class="ic b-ic" aria-hidden="true"><use href="#i-wand"></use></svg>Remove the background</button>
        <button class="btn btn-cyan small full" id="mb-talk" title="The face in this picture says a line in one of your cloned voices, with its mouth animated to the speech. Becomes a video layer; undo with ↺ below."><svg class="ic b-ic" aria-hidden="true"><use href="#i-mic"></use></svg>Make it talk</button>` : ''}
        <button class="btn btn-cyan small full" id="mb-erase" title="Rub parts of this layer out with your finger or the mouse. What you erase turns see-through, so the layers underneath show through it.">✂ Erase parts${l.mask?' (erased)':''}</button>
        ${l.mask ? `<button class="btn btn-cyan small full" id="mb-erase-clear" title="Put every erased part of this layer back">↺ Undo the erase</button>` : ''}
        ${l.origSrc ? `<button class="btn btn-cyan small full" id="mb-fx-revert" title="Put this layer's original picture back — the effect (or the background cut-out) that replaced it is undone">↺ Undo the effect on this layer</button>` : ''}`}

      <!-- Stacking order is one of the handful you reach for on EVERY layer, so it belongs up here with
           them, not buried at the bottom of a collapsed group. On a phone it was the only way to reorder
           at all — the row's ⬆︎/⬇︎ buttons come off the track there so the lane gets the width (see the
           ≤820px rules) — and "in the layer panel" meant: select the layer, open Look & sound, scroll to
           the end. Which is indistinguishable from not being there. -->
      <div class="mb-order">
        <button class="btn btn-cyan small" id="mb-back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg> Send back</button>
        <button class="btn btn-cyan small" id="mb-front"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg> Bring front</button>
      </div>

      ${_sect('place', '📐 Position &amp; size', (isText
        ? `<button class="btn btn-cyan small full${_alignOf(l)==='center'?' on':''}" id="mb-center">⇔ Centre horizontally</button>
           ${alignGrid()}
           <div class="muted small mb-dbg">x=${Math.round(l.x)} y=${Math.round(l.y)} size=${Math.round(l.size)} align=${_alignOf(l)||"free"} · canvas ${P.w}×${P.h}</div>`
        : `${alignGrid()}
           <div class="mb-frow">
             <label class="mb-f"><span>W</span><input class="input" type="number" id="mb-f-w" value="${Math.round(l.w)}"></label>
             <label class="mb-f"><span>H</span><input class="input" type="number" id="mb-f-h" value="${Math.round(l.h)}"></label>
           </div>
           <button class="btn btn-cyan small full" id="mb-canvas-match" title="Reshape the CANVAS to this photo — the third option: no bars AND nothing cropped">⇲ Canvas to this photo</button>
           <label class="mb-f"><span>Layer name</span><input class="input" id="mb-f-name" maxlength="24" placeholder="${enc(srcName(l.src))}" value="${enc(l.name||'')}"></label>`))}

      ${_sect('time', '⏱ Timing', `${l.type==='video'
        ? `<label class="mb-f"><span>Speed <b id="mb-spd-val">${_speedOf(l)}×</b><i class="mb-slot" id="mb-spd-slot">${_slotNote(l)}</i></span>
             <input type="range" id="mb-f-speed" min="0.25" max="4" step="0.05" value="${_speedOf(l)}"></label>
           <div class="mb-frow">${[0.5,1,2].map(v=>`<button class="btn btn-cyan small${_speedOf(l)===v?' on':''}" data-spd="${v}">${v}×</button>`).join('')}</div>
           <label class="mb-f mb-check"><input type="checkbox" id="mb-f-mute" ${l.mute?'checked':''}><span>Mute this clip</span></label>
           <div class="muted small mb-dbg">Drag the clip on the 🎞 Timeline to set when it appears in the meme.</div>`
        : `<div class="mb-frow">
             <label class="mb-f"><span>Start (s)</span><input class="input" type="number" id="mb-f-start" min="0" step="0.1" value="${l.start}"></label>
             <label class="mb-f"><span>Length (s)</span><input class="input" type="number" id="mb-f-dur" min="0.1" step="0.1" value="${l.dur}"></label>
           </div>`}`)}

      ${_sect('look', '🎨 Look &amp; sound', `
        <label class="mb-f"><span>Effect</span><select class="input" id="mb-f-fx">
          ${FX.map(([v,n])=>`<option value="${v}" ${l.effect===v?'selected':''}>${n}</option>`).join('')}
        </select></label>
        ${(l.type==='image') && EFFECTS.length ? `<label class="mb-f"><span>Meme effect</span><select class="input" id="mb-f-meme">
          <option value="">— apply an effect to this image —</option>
          ${EFFECTS.map(e=>`<option value="${enc(e.name)}" title="${enc(e.desc||'')}">${enc(e.label||e.name)}</option>`).join('')}
        </select></label>
        <button class="btn btn-cyan small full" id="mb-prev-fx" title="Play just this layer in the preview above"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg> Preview effect</button>` : ''}
        ${isText ? `
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-wrap" ${l.wrap===false?'':'checked'}><span>Wrap long lines</span></label>
        ${l.wrap===false ? '' : `<label class="mb-f"><span>Wrap width <b>${_wrapPct(l)}%</b> of the frame</span><input type="range" id="mb-f-wrappct" min="20" max="100" step="1" value="${_wrapPct(l)}"></label>`}
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-box" ${l.box?'checked':''}><span>Background box</span></label>
        ${l.box ? `<div class="mb-frow">
          <label class="mb-f"><span>Box colour</span><input type="color" id="mb-f-boxcolor" value="${enc(/^#[0-9a-fA-F]{6}$/.test(l.boxColor||'')?l.boxColor:'#000000')}"></label>
          <label class="mb-f"><span>Box opacity</span><input type="range" id="mb-f-boxalpha" min="0.1" max="1" step="0.05" value="${l.boxAlpha==null?0.55:l.boxAlpha}"></label>
        </div>` : ''}
        <label class="mb-f mb-check"><input type="checkbox" id="mb-f-shadow" ${l.shadow?'checked':''}><span>Drop shadow</span></label>` : ''}
        <label class="mb-f"><span>Sound</span><select class="input" id="mb-f-snd">
          <option value="">None</option>
          ${SOUNDS.map(n=>`<option value="${enc(n)}" ${l.sound===n?'selected':''}>${enc(n)}</option>`).join('')}
        </select></label>
        ${l.sound ? `<label class="mb-f"><span>Sound volume</span><input type="range" id="mb-f-sndvol" min="0" max="3" step="0.1" value="${(l.soundVolume==null?1:l.soundVolume)}"></label>` : ''}
        <label class="mb-f"><span>Opacity</span><input type="range" id="mb-f-op" min="0.05" max="1" step="0.05" value="${l.opacity}"></label>
        <div class="mb-frow"><button class="btn btn-cyan small${l.flipH?' on':''}" id="mb-fliph" title="Mirror left-to-right">⇄ Flip</button><button class="btn btn-cyan small${l.flipV?' on':''}" id="mb-flipv" title="Mirror top-to-bottom">⇅ Flip</button><button class="btn btn-cyan small" id="mb-rot0" title="Back to upright">⌾ 0°</button></div>
        <label class="mb-f"><span>Rotate <b id="mb-rot-val">${Math.round(+l.rotate||0)}°</b></span><input type="range" id="mb-f-rot" min="-180" max="180" step="1" value="${Math.round(+l.rotate||0)}"></label>`)}`;
  }

  // A collapsible inspector group. The title is already escaped by its caller (it carries &amp;), so it is
  // interpolated raw — everything user-supplied inside `body` went through enc() where it was built.
  function _sect(key, title, body){
    return `<details class="mb-sec" data-sec="${key}"${_sec[key]?' open':''}>
      <summary>${title}</summary>
      <div class="mb-secbody">${body}</div>
    </details>`;
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
    PC.modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-layout"></use></svg>Start from a layout</h3>
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
  // AUDIO belongs here too. It used to be image|video only, so a drive with mp3s on it opened as a
  // grid that simply did not contain them — the same drive showing different contents depending on
  // which button you arrived from, with nothing on screen to say why. Music has its own entry under
  // ➕ (with record-a-voice-over beside it), but nobody hunting for a track they already uploaded
  // should have to find that first. Route by the blob's type, exactly as the local-file path does.
  function pickBlossom(){
    PC.blossomPicker(null, ({ url, type }) => {
      addLayer(/^audio\//.test(type||'') ? 'audio' : /^video\//.test(type||'') ? 'video' : 'image', url);
      render();
    }, {
      title: '🌸 Add from Blossom',
      filter: b => /^(image|video|audio)\//.test(b.type||''),
      empty: 'Nothing on your Blossom drive yet — upload some in the Files tab.',
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
    PC.modal('<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Open a saved project</h3>' + found.map(row).join(''), root=>{
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
    if(what==='inspector'){ const i=document.getElementById('mb-inspector');
      if(i){ _saveSecState(); i.innerHTML=inspector(); bindInspector(root); } return; }
    if(what==='timeline'){ const t=document.getElementById('mb-timeline');
      if(t){ t.innerHTML=timelineInner();
        // The ruler's tick spacing and the playhead's position both depend on the project length, which a
        // trim just changed — repaint them with the rows rather than leaving a stale ruler behind.
        const s=document.getElementById('mb-scrub'); paintPlayhead(s?+s.value||0:0); }
      return; }
    render();
  }

  // `from` says where the tap came from, which decides whether the Layer pane takes over the panel area.
  // 'stage' → yes: the controls for what you just grabbed are the only thing you can want next. 'timeline'
  // → NO: swapping the timeline out from under the finger that is using it is the worse surprise, so the
  // Layer tab just relabels itself (✎ Video) and waits to be tapped.
  function selectLayer(id, from){
    sel = id;
    document.querySelectorAll('.mb-item,.mb-track').forEach(e=>e.classList.toggle('sel', e.dataset.id===id));
    _paintTabs();                                   // the Layer tab is named after the selection
    if(from === 'stage') _showTab('layer');
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
      selectLayer(l.id, 'stage');
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

  // Move a layer ONE place within its own stacking group, in the direction the ROW list reads
  // ('up' = towards the top of the list = later in draw order = drawn on top).
  //
  // Group, not raw P.layers order: _stageOrder draws every visual first and every caption after, because
  // the renderer composites drawtext last no matter what the array says. So a caption can only be
  // restacked among captions and a clip among clips — and a raw `P.layers[i] <-> P.layers[i±1]` swap
  // (what the ⬆︎/⬇︎ buttons used to do) was a silent NO-OP whenever the neighbour it grabbed was of the
  // other kind: the array changed, the filtered draw order didn't, and the button looked broken.
  // Audio isn't drawn at all, so it has no stacking to change.
  function restackOne(l, dir){
    if(!l || l.type==='audio') return false;
    const same = _isVisual(l) ? _isVisual : (x)=>x.type==='text';
    const group = P.layers.filter(same);
    const gi = group.indexOf(l);
    const other = group[dir==='up' ? gi+1 : gi-1];
    if(gi<0 || !other) return false;                        // already at the top/bottom of its group
    const a = P.layers.indexOf(l), b = P.layers.indexOf(other);
    P.layers[a]=other; P.layers[b]=l;
    return true;
  }

  function bindTimeline(root){
    const tl = root.querySelector('#mb-timeline'); if(!tl) return;
    // EVERY timeline gesture is delegated on #mb-timeline, which survives repaint('timeline') replacing its
    // children. Bound per row instead, selecting a layer by tapping its row silently stopped working after
    // the first trim or drag — the listeners went with the old innerHTML.
    //
    // The row used to carry its own ⬆︎/⬇︎ restack pair here, 22x16px stacked in the name column. They were
    // the most cramped thing in the builder, they were already hidden below 820px (the lane needs the
    // width), and they made desktop and mobile two different layouts. Restacking is unchanged and still
    // has two homes that work everywhere: DRAG the row by its ⇅ grip, or ⬇︎ Send back / ⬆︎ Bring front in
    // the layer panel.
    tl.addEventListener('click', (e)=>{
      // Tapping the row (its tile, not its clip bar) selects that layer.
      const row = e.target.closest('.mb-track[data-id]');
      if(row && !e.target.closest('.mb-clip')) selectLayer(row.dataset.id, 'timeline');
    });
    tl.addEventListener('pointerdown', (e)=>{
      // Drag a row by its ⇅ grip to restack it. Reordering by dragging the layer list is what everyone
      // tries first, and nothing listened for it — on a phone that mattered most, because the row's ⬆︎/⬇︎
      // buttons are hidden there (the lane needs the width) and the panel's pair lives in a DIFFERENT TAB,
      // so the timeline had no restack affordance at all. Pointer events → one implementation for mouse
      // and touch. The lane is left alone: that is the horizontal time-drag, a different gesture entirely.
      const gripEl = e.target.closest('.mb-rgrip');
      if(gripEl){
        const row = gripEl.closest('.mb-track[data-id]'); if(!row) return;
        const l = P.layers.find(x=>x.id===row.dataset.id); if(!l || l.type==='audio') return;
        e.preventDefault();
        selectLayer(l.id, 'timeline');
        // Step per ROW crossed, committed live: the rows genuinely reorder under your finger, which is the
        // feedback. Safe mid-drag because the pointer capture is on #mb-timeline itself, which survives
        // repaint('timeline') replacing its children (see the delegation note above).
        const rowH = Math.max(24, row.getBoundingClientRect().height + 6);   // + the .mb-tlinner gap
        let startY = e.clientY, steps = 0, moved = false;
        snap();                                             // one snapshot per gesture
        row.classList.add('mb-dragging');
        drag(e, (ev)=>{
          // Steps are derived from the ABSOLUTE travel, never accumulated per event: the row then tracks
          // the finger exactly and reversing is symmetric. Accumulating (anchor += rowH per step) leaves
          // the finger a fraction of a row past the anchor, so a few px of tremor in the other direction
          // undid the move you just made.
          const want = Math.round((ev.clientY - startY)/rowH);
          let changed = false;
          while(steps !== want){
            const up = want < steps;
            // At the end of its group, re-base so the overshoot isn't banked — otherwise dragging well
            // past the top and coming back does nothing until you have retraced every wasted row.
            if(!restackOne(l, up ? 'up' : 'down')){ startY = ev.clientY - steps*rowH; break; }
            steps += up ? -1 : 1; changed = true;
          }
          if(!changed) return;
          moved = true;
          repaint('timeline');
          const again = tl.querySelector('.mb-track[data-id="'+l.id+'"]');
          if(again) again.classList.add('mb-dragging');
        }, ()=>{
          const el = tl.querySelector('.mb-track[data-id="'+l.id+'"]');
          if(el) el.classList.remove('mb-dragging');
          if(moved){ save(); render(); }                    // render(): the PREVIEW stacking changed too
          else unsnapIfUnchanged();                         // a plain tap → selection only, no undo entry
        });
        return;
      }
      if(e.target.closest('.mb-rlane') || e.target.closest('.mb-ph')){
        e.preventDefault();
        if(_playT) stopPlay(false);           // scrubbing during playback would fight the ticker
        _rulerSeek(e.clientX);
        drag(e, (ev)=>_rulerSeek(ev.clientX));
        return;
      }
      const clip = e.target.closest('.mb-clip'); if(!clip) return;
      const l = P.layers.find(x=>x.id===clip.dataset.id); if(!l) return;
      selectLayer(l.id, 'timeline');
      const lane = clip.parentElement, rect = lane.getBoundingClientRect();
      const total = tlSpan(), perPx = total/rect.width;
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
            repaint('timeline'); repaint('inspector'); syncScrub(); return;
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
      // CROSSFADE in the preview. The renderer blends with alpha ramps on each clip's own stream; without
      // the same ramp here, an overlapping pair previews as the top clip simply covering the one beneath
      // (a hard cut) and the dissolve only appears in the export.
      if(on) el.style.opacity = (l.opacity==null?1:+l.opacity) * _rampAt(l, t - l.start);
      const v=el.querySelector('video');
      if(v){
        const playing = !!_playT;
        if(!on){
          // A hidden video must be PAUSED, not just display:none — it is audible now (below), and a
          // clip that has scrolled off the playhead would otherwise keep talking over the rest.
          if(!v.paused){ try{ v.pause(); }catch(_){ } }
        } else {
          // Local time runs at the clip's SPEED — the renderer feeds `speed x slot` seconds of source into
          // the slot, so the preview has to walk the source at the same rate or scrubbing shows a different
          // frame than the export.
          const sp=_speedOf(l);
          const local=(+l.trim||0)+(t-l.start)*sp;
          try{ if(v.playbackRate!==sp) v.playbackRate=sp; }catch(_){ }
          if(Math.abs(v.currentTime-local)>0.25){ try{ v.currentTime=local; }catch(_){ } }
          // HEAR the clip. Video layers used to be hardcoded `muted` here, so the preview was silent
          // while the export was not — the per-layer mute toggle and volume slider did nothing, and a
          // talking-face layer (whose whole content IS the audio) previewed as a silent still. Mirrors
          // seekAudio: the layer's own fields, capped at 1 because that is all a media element takes.
          const mute = !!l.mute;
          if(v.muted!==mute) v.muted = mute;
          const vol = clamp(l.volume==null?1:l.volume, 0, 1);
          if(Math.abs(v.volume-vol)>0.01) v.volume = vol;
          // RETRY the play every tick while the playhead is running. togglePlay fires play() once, in
          // the click, but a layer whose source was just swapped (an effect, "Make it talk") is a
          // BRAND NEW element that is still loading at that moment — its play() rejects and the clip
          // sits on its poster frame with no sound until you press play a second time. Retrying is
          // what makes the first press work; it is exactly what seekAudio already does for music.
          if(playing && v.paused){ try{ v.play().catch(()=>{}); }catch(_){ } }
          if(!playing && !v.paused){ try{ v.pause(); }catch(_){ } }
        }
      }
    });
    seekAudio(t);
    paintPlayhead(t);
    const time=document.getElementById('mb-time');
    if(time) time.textContent=t.toFixed(1)+'s / '+projEnd().toFixed(1)+'s';
  }

  // The crossfade ramp for a layer at `lt` seconds INTO its own slot: 0..1, mirroring the renderer's
  // `fade=t=in:alpha=1` / `fade=t=out:alpha=1` pair. Anything without ramps returns 1, so a project with no
  // crossfade is untouched.
  function _rampAt(l, lt){
    const dur = +l.dur || 0;
    let k = 1;
    const xi = +l.xin || 0, xo = +l.xout || 0;
    if(xi > 0.01 && lt < xi) k = Math.max(0, lt / xi);
    if(xo > 0.01 && lt > dur - xo) k = Math.min(k, Math.max(0, (dur - lt) / xo));
    return k;
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
    // Kick every clip off INSIDE the click, so the browser's autoplay policy sees a user gesture (the
    // clips carry sound now). Anything not loaded yet rejects here and is retried by seek() on the
    // next tick — see the retry in there for why the first press used to do nothing.
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
      // AUDIO is accepted here too. It has its own ➕ entry (with record-a-voice-over beside it), but a
      // dropped or picked mp3 used to be silently SKIPPED by this line — no layer, no error, nothing on
      // screen to say the file was ignored. It becomes an audio layer, exactly as that entry makes one.
      if(!/^(image|video|audio)\//.test(f.type||'')) continue;
      try{
        if(st) st.textContent='uploading '+f.name+'…';
        const url=await uploadBlob(f);
        addLayer(f.type.startsWith('audio')?'audio':f.type.startsWith('video')?'video':'image', url, { name:(f.name||'').slice(0,24) });
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

  // EVERYTHING you can add, on one sheet — what 🖼️ Media and ➕ More held between them. Two groups, and
  // the order is the answer to two different questions: the top three are WHERE a file comes from, the
  // rest are things the builder makes for you. Each keeps the line saying what it is, which is what the
  // toolbar buttons never had room for.
  //
  // "🎵 Music or a voice-over" is deliberately NOT one of these entries any more. It opened a THIRD sheet
  // whose own options were "from this device" / "from my Blossom drive" / "record" — and the first two are
  // the first two entries here, hitting the identical upload path (addMediaFiles takes audio, and
  // pickBlossom's filter already allows it). So music is where every other file is, and only the part that
  // was genuinely its own thing — recording one — is still a separate entry. That is one whole level of
  // sheet gone from the way to a backing track.
  function pickMedia(){
    PC.modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Add to the meme</h3>
      <div class="mb-addgrp">A picture, clip or track</div>
      <button class="btn btn-cyan full mb-addb" id="mbm-local"><b>📱 From this device</b><i>A photo, a video or a music track off your phone or computer</i></button>
      <button class="btn btn-cyan full mb-addb" id="mbm-blossom"><b>🌸 From my Blossom drive</b><i>A picture, a clip or a track you already uploaded</i></button>
      <button class="btn btn-cyan full mb-addb" id="mbm-ai"><b>🎨 Generate one with AI</b><i>Describe a picture and it lands on the timeline</i></button>
      <div class="mb-addgrp">Made here</div>
      <button class="btn btn-cyan full mb-addb" id="mba-sticker"><b>😀 Sticker</b><i>An emoji (or a custom one) as its own draggable layer</i></button>
      <button class="btn btn-cyan full mb-addb" id="mba-effect"><b>✨ Effect</b><i>The dancing man, the shrug, a character — drag, resize and time it</i></button>
      <button class="btn btn-cyan full mb-addb" id="mba-tpl"><b>📐 Ready-made layout</b><i>Top/bottom captions, a two-panel split, a caption bar</i></button>
      <button class="btn btn-cyan full mb-addb" id="mba-rec"><b>🎙️ Record a voice-over</b><i>Talk over the meme with your microphone</i></button>
      <button class="btn btn-cyan full mb-addb" id="mba-voice"><b>🗣️ Say it in a cloned voice</b><i>Type a line and one of your saved voices reads it</i></button>`, root=>{
      const go=(id,fn)=>{ const b=root.querySelector('#'+id); if(b) b.onclick=()=>{ PC.closeModal(); fn(); }; };
      go('mbm-local', pickLocalMedia);
      go('mbm-blossom', pickBlossom);
      go('mbm-ai', pickAiImage);
      // The emoji picker is a POPOVER and anchors to an element — the sheet's own button is gone by the
      // time it opens, so anchor it to the toolbar button, which is still there.
      go('mba-sticker', ()=>pickSticker(document.getElementById('mb-add-media')));
      go('mba-effect', pickEffect);
      go('mba-tpl', pickTemplate);
      go('mba-rec', recordVoice);
      go('mba-voice', pickClonedVoice);
    });
  }

  // A spoken line in one of your saved voices, as an ordinary audio layer.
  //
  // It BORROWS AI Chat's voice studio (PC.openVoiceStudio with an onTake) rather than growing a second
  // one here — the voice library, the recorder, the "this holds the GPU" notice and the mobile layout
  // are all non-trivial and already exist. Same reasoning as 🎨 Generate one with AI borrowing the
  // image studio: only the ENDING differs. The take arrives as a blob, goes to Blossom like every other
  // layer source (the renderer only ever fetches URLs), and lands on the timeline as audio.
  // How long a generated take actually is, from the browser rather than a guess. Resolves 0 if the
  // metadata never arrives, and the caller then falls back to addLayer's default rather than writing a
  // bogus duration onto the layer.
  function _clipSeconds(blob){
    return new Promise(res=>{
      let done=false; const fin=v=>{ if(done) return; done=true; try{ URL.revokeObjectURL(a.src); }catch(_){} res(v); };
      const a=document.createElement('audio');
      a.preload='metadata';
      a.onloadedmetadata=()=>fin(isFinite(a.duration)&&a.duration>0 ? a.duration : 0);
      a.onerror=()=>fin(0);
      setTimeout(()=>fin(0), 8000);      // never hang the add on a metadata read
      a.src=URL.createObjectURL(blob);
    });
  }

  function pickClonedVoice(){
    if(!PC.openVoiceStudio){ toast('voice cloning isn’t available on this build'); return; }
    PC.openVoiceStudio({
      useLabel: '➕ Add to the meme',
      onTake: async (blob, voiceName, text) => {
        const st = document.getElementById('mb-status');
        try{
          if(st) st.textContent = 'adding the voice line…';
          const name = (text || voiceName || 'voice').slice(0, 24);
          const url = await uploadBlob(new File([blob], name.replace(/[^\w .-]/g, '_') + '.wav',
                                                { type: 'audio/wav' }));
          // MEASURE the take. addLayer gives an audio layer `dur = the current project length`, which is
          // right for a music bed — a three-minute song must not stretch a six-second meme, so the
          // renderer truncates it. A spoken LINE is the opposite: cutting it at the end of the video
          // means the sentence never finishes, which is exactly "it don't render it all".
          const secs = await _clipSeconds(blob);
          if(!addLayer('audio', url, { name, dur: secs || undefined })) return;
          if(document.getElementById('mb-stage')) render();
          toast(secs ? `voice line added (${secs.toFixed(1)}s)` : 'voice line added as a layer');
        }catch(e){ toast('couldn’t add that: ' + ((e && e.message) || e)); }
        finally{ const s2 = document.getElementById('mb-status'); if(s2) s2.textContent = ''; }
      },
    });
  }
  // Where is the mouth? Resolves to a NORMALISED {x, y, w, angle, anime}, or null if cancelled.
  //
  // This control exists because detection cannot be trusted on the art people actually meme with.
  // InsightFace happily detects an anime face and then puts the mouth landmarks on the chin and a
  // cheek — a confident wrong answer, which is worse than none, and it is why "make it talk" did
  // nothing sensible on anime. The server still SEEDS the marker from detection (a photo needs no
  // interaction at all, you just press Use it), but the person looking at the picture gets the
  // final say. That also makes 3D renders, mascots, drawings and a face in a crowd all work,
  // none of which any face model here was trained for.
  //
  // "Photo vs drawing" is the same judgement and is asked here for the same reason: it picks the
  // RENDERER. A photograph has a real jaw to warp; flat art has an ink line that smears when you
  // move it, so its mouth is redrawn instead. See effects_service/talk.py.
  //
  // A CHARACTER POSE (jerry, carl, …) goes through this too. It briefly did not — the artwork is
  // fixed, so the detector's answer for it is fixed, and skipping the step looked like a kindness.
  // It is not: a fixed answer that is off is off on every single render with no way to correct it,
  // and "the mouth selector never shows" is what that feels like from the outside. The layer's own
  // src is the rendered CLIP, which an <img> cannot show and which is not what gets animated, so
  // the picture (and the detection seed) come from the pose's artwork instead — /meme/character.
  function pickMouth(src, character){
    const show = character ? '/client/meme/character/' + encodeURIComponent(character) : src;
    return new Promise(resolve => {
      let m = { x:0.5, y:0.62, w:0.12, angle:0, anime:false }, done = false;
      PC.modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-mic"></use></svg>Where is the mouth?</h3>
        <div class="muted small" style="margin-bottom:8px">Drag the marker onto the mouth, and the
        slider to match its width. I have put it where I think it is — on a photo that is usually right.</div>
        <div class="mb-mouth-wrap" id="mm-wrap"><img id="mm-img" src="${enc(show)}" alt="">
          <i class="mb-mouth-pin" id="mm-pin"></i></div>
        <label class="mb-f"><span>Mouth width</span>
          <input type="range" id="mm-w" min="3" max="45" step="1" value="12"></label>
        <div class="mb-frow" style="margin-top:6px">
          <button class="btn btn-cyan small" id="mm-photo">📷 Photo</button>
          <button class="btn btn-ghost small" id="mm-draw">🎨 Drawing / anime</button>
        </div>
        <div class="muted small" id="mm-hint" style="margin-top:6px"></div>
        <div style="display:flex;gap:8px;margin-top:12px">
          <button class="btn btn-ghost small" id="mm-cancel">Cancel</button>
          <button class="btn btn-neon small" id="mm-go">Use it</button>
        </div>`, root => {
        const wrap = root.querySelector('#mm-wrap'), img = root.querySelector('#mm-img');
        const pin = root.querySelector('#mm-pin'), rng = root.querySelector('#mm-w');
        const hint = root.querySelector('#mm-hint');
        // The marker is painted from offsetWidth/offsetHeight, NOT from the client rect. On a tablet
        // (and any desktop that isn't high-DPI) the app is scaled with `body{zoom}`, and the two are
        // in different spaces: getBoundingClientRect() reports VIEWPORT pixels, already multiplied by
        // the zoom, while a px in `style.left` on an element inside that body is a LAYOUT pixel. Sized
        // from the rect, the pin therefore landed at zoom× the fraction it was handed — at .67 it
        // crawled to two-thirds of the picture and stopped, so the right and bottom of the image could
        // not be reached at all, and a mouth lined up by eye was recorded ~1.5x too far across and too
        // wide (which is what "it doesn't align on anime" was: a photo is seeded by the detector and
        // never dragged, so only hand placement showed it). offsetWidth is a layout pixel, same as
        // style.left. The POINTER math below keeps the rect, where clientX and the rect do agree.
        const paint = () => {
          const w = img.offsetWidth, h = img.offsetHeight;
          if(!w) return;
          pin.style.left = (img.offsetLeft + m.x * w) + 'px';
          pin.style.top  = (img.offsetTop  + m.y * h) + 'px';
          pin.style.width = Math.max(6, m.w * w) + 'px';
          pin.style.transform = `translate(-50%,-50%) rotate(${m.angle}deg)`;
          root.querySelector('#mm-photo').className = 'btn small ' + (m.anime?'btn-ghost':'btn-cyan');
          root.querySelector('#mm-draw').className  = 'btn small ' + (m.anime?'btn-cyan':'btn-ghost');
          hint.textContent = m.anime
            ? 'Flat art: the mouth is redrawn each frame, the way anime does it.'
            : 'Photo: the real jaw is warped, so it keeps the face’s own detail.';
        };
        // One pointer path for mouse AND touch — the builder is used on phones at least as much.
        const put = ev => {
          const r = img.getBoundingClientRect(); if(!r.width) return;
          const p = ev.touches ? ev.touches[0] : ev;
          m.x = clamp((p.clientX - r.left) / r.width, 0, 1);
          m.y = clamp((p.clientY - r.top) / r.height, 0, 1);
          paint();
        };
        let dragging = false;
        // POINTER CAPTURE rather than a window-level pointerup: a drag that ends off the picture
        // still has to end, and a listener on `window` outlives a modal closed by tapping the
        // backdrop (which never reaches the Cancel handler that would have removed it).
        const down = e => { dragging = true;
          try{ wrap.setPointerCapture(e.pointerId); }catch(_){ }
          put(e); e.preventDefault(); };
        const move = e => { if(dragging){ put(e); e.preventDefault(); } };
        const up = e => { dragging = false;
          try{ wrap.releasePointerCapture(e.pointerId); }catch(_){ } };
        wrap.addEventListener('pointerdown', down);
        wrap.addEventListener('pointermove', move);
        wrap.addEventListener('pointerup', up);
        wrap.addEventListener('pointercancel', up);
        rng.oninput = () => { m.w = (+rng.value || 12) / 100; paint(); };
        root.querySelector('#mm-photo').onclick = () => { m.anime = false; paint(); };
        root.querySelector('#mm-draw').onclick  = () => { m.anime = true;  paint(); };
        img.onload = paint;
        // A dead Blossom URL would otherwise show an empty box with an invisible marker (paint()
        // bails on a zero-width image), i.e. a dialog you cannot use and cannot diagnose.
        img.onerror = () => { hint.textContent = 'That picture would not load — re-add the layer.'; };
        if(img.complete) paint();
        // SEED from the server's detector. Failure is not an error here — the whole point of the
        // control is that it works without one — so a dead request just leaves the default marker.
        (async () => {
          try{
            const auth = await selfProof();
            const r = await fetch('/client/meme/face',{ method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify(character ? { pubkey: ME.pubkey, auth, character }
                                             : { pubkey: ME.pubkey, auth, url: src }) });
            const j = await r.json().catch(()=>null);
            if(j && j.found){
              m = { x:+j.x||0.5, y:+j.y||0.62, w:+j.w||0.12, angle:+j.angle||0, anime:!!j.anime };
              // The server's photo-vs-drawing guess is only a guess — the two classes overlap, so a
              // heavily shaded illustration can land on the photo side. YOUR last answer beats it:
              // people tend to meme with one kind of art, so remembering converges immediately and
              // the picker never offers the same wrong default twice.
              try{
                const seen = localStorage.getItem('pc_meme_talk_mode');
                if(seen === 'anime' || seen === 'photo') m.anime = (seen === 'anime');
              }catch(_){ }
              rng.value = Math.round(clamp(m.w,0.03,0.45) * 100);
              paint();
            }
          }catch(_){ }
        })();
        const finish = v => { if(done) return; done = true; PC.closeModal(); resolve(v); };
        root.querySelector('#mm-cancel').onclick = () => finish(null);
        root.querySelector('#mm-go').onclick = () => {
          try{ localStorage.setItem('pc_meme_talk_mode', m.anime ? 'anime' : 'photo'); }catch(_){ }
          finish({ ...m });
        };
      });
    });
  }

  // ---------- ✂ erase part of a layer ----------
  // Rub bits of a layer out with a finger or a mouse. What you erase becomes TRANSPARENT, so whatever is
  // underneath shows through — the layers below it, or the canvas background.
  //
  // The stroke is painted in the layer's SOURCE space, not on the stage, and that is the design:
  //   * it survives everything done to the layer afterwards — resize, re-fit, flip, rotate, move —
  //     because the renderer seats the mask into the layer box with the SAME geometry as the picture
  //     (_fit_chain in meme_builder_service.py). Painting on the stage would bake in today's size.
  //   * the pointer maths stay trivial. The stage element carries the flip/rotate transform, so a stroke
  //     there would have to be un-rotated by hand; here the artwork is shown upright and untransformed,
  //     exactly like the mouth picker, and clientX and the rect agree.
  //
  // The mask is uploaded and referenced by URL like every other piece of layer media. Inline base64 is
  // the obvious alternative and is wrong twice over: snap() snapshots the WHOLE project 40 deep and
  // save() puts it in localStorage, so a handful of masks would blow up both.
  const MASK_EDGE = 1024;          // cap on the mask's long edge — see below
  // Undo REPLAYS strokes rather than stacking bitmaps. A 1024px mask is 4 MB as ImageData, so even a
  // shallow bitmap stack is tens of megabytes on a phone; a stroke is a few hundred bytes of points.
  function eraseParts(l){
    const isVid = l.type === 'video';
    PC.modal(`<h3>✂ Erase parts of this layer</h3>
      <div class="muted small" style="margin-bottom:8px">Rub out what you don’t want — it turns
        see-through, so whatever is under it shows through. ${isVid?'The whole clip is erased in the same place.':''}</div>
      <div class="mb-er-wrap" id="er-wrap">
        ${isVid ? `<video id="er-src" src="${enc(l.src)}#t=0.1" muted playsinline preload="auto"></video>`
                : `<img id="er-src" src="${enc(l.src)}" alt="">`}
        <canvas id="er-ov"></canvas>
      </div>
      <div class="mb-er-tools">
        <button class="btn btn-cyan small on" id="er-rub" title="Rub the picture out">✂ Erase</button>
        <button class="btn btn-ghost small" id="er-put" title="Paint an erased part back in">↺ Restore</button>
        <button class="btn btn-ghost small" id="er-undo" disabled title="Undo the last stroke">↶ Undo</button>
        <button class="btn btn-ghost small" id="er-all" title="Put the whole layer back">Clear all</button>
      </div>
      <label class="mb-f"><span>Brush size <b id="er-bv">18%</b></span>
        <input type="range" id="er-b" min="2" max="60" step="1" value="18"></label>
      <div class="muted small" id="er-hint">Loading the picture…</div>
      <div class="mb-frow" style="margin-top:12px">
        <button class="btn btn-ghost small" id="er-cancel">Cancel</button>
        <button class="btn btn-neon small" id="er-go" disabled>Apply</button>
      </div>`, root => {
      const $q = s => root.querySelector(s);
      const wrap = $q('#er-wrap'), art = $q('#er-src'), ov = $q('#er-ov');
      const hint = $q('#er-hint'), go = $q('#er-go'), undoBtn = $q('#er-undo');
      // msk is the real mask: opaque = keep, transparent = erased. Its ALPHA is the whole payload —
      // both consumers (CSS mask-image on the stage, alphaextract in ffmpeg) read alpha and ignore RGB.
      const msk = document.createElement('canvas');
      // `base` is the mask as it was when this opened, so re-opening CONTINUES the previous erase
      // instead of starting over — and so Undo can replay this session's strokes on top of it.
      let base = null, ops = [], mode = 'rub', drawing = false, last = null, cleared = false, busy = false;

      const brush = () => (+$q('#er-b').value || 18) / 100;
      const radius = () => Math.max(1, brush() * msk.width / 2);

      // One brush pass. `pts` may be the whole stroke (a replay) or the two points of the segment just
      // drawn (the live path) — round caps and joins make a polyline and its segments identical, so the
      // two agree pixel for pixel.
      function drawOp(c, mode, r, pts){
        c.globalCompositeOperation = mode === 'rub' ? 'destination-out' : 'source-over';
        c.strokeStyle = '#fff'; c.fillStyle = '#fff';
        c.lineWidth = r * 2; c.lineCap = 'round'; c.lineJoin = 'round';
        if(pts.length === 1){          // a TAP is a dot, not a zero-length line (which strokes nothing)
          c.beginPath(); c.arc(pts[0][0], pts[0][1], r, 0, Math.PI * 2); c.fill();
        } else {
          c.beginPath(); c.moveTo(pts[0][0], pts[0][1]);
          for(let i = 1; i < pts.length; i++) c.lineTo(pts[i][0], pts[i][1]);
          c.stroke();
        }
        c.globalCompositeOperation = 'source-over';
      }
      function syncBtns(){
        undoBtn.disabled = !ops.length;
        go.disabled = busy || (!ops.length && !cleared);
      }
      // FULL replay — undo, Clear all, and the initial paint. Deliberately NOT what a pointermove uses:
      // replaying every stroke per move is quadratic in the length of the drag, which on a phone is a
      // stutter that gets worse the longer you draw. A move extends the canvas by one segment instead.
      function rebuild(){
        const c = msk.getContext('2d');
        c.globalCompositeOperation = 'source-over';
        c.clearRect(0, 0, msk.width, msk.height);
        if(base) c.drawImage(base, 0, 0, msk.width, msk.height);
        else { c.fillStyle = '#fff'; c.fillRect(0, 0, msk.width, msk.height); }
        ops.forEach(op => drawOp(c, op.mode, op.r, op.pts));
        paintOv();
        syncBtns();
      }
      // The erased area is shown as a scrim rather than by hiding the pixels: you need to see WHAT you
      // rubbed out to judge the edge, and the true result is one Apply away on the stage behind.
      function paintOv(){
        const c = ov.getContext('2d');
        c.globalCompositeOperation = 'source-over';
        c.clearRect(0, 0, ov.width, ov.height);
        c.fillStyle = 'rgba(255,32,96,.45)';
        c.fillRect(0, 0, ov.width, ov.height);
        c.globalCompositeOperation = 'destination-out';   // keep the scrim ONLY where the mask is gone
        c.drawImage(msk, 0, 0, ov.width, ov.height);
        c.globalCompositeOperation = 'source-over';
      }
      // Pointer -> mask pixels. The RECT is right here (clientX and the rect are both viewport pixels,
      // so body{zoom} cancels out); it is only the drawing of elements that has to use layout pixels.
      const at = ev => {
        const r = art.getBoundingClientRect(); if(!r.width) return null;
        const p = ev.touches ? ev.touches[0] : ev;
        return [clamp((p.clientX - r.left) / r.width, 0, 1) * msk.width,
                clamp((p.clientY - r.top) / r.height, 0, 1) * msk.height];
      };
      const down = e => {
        if(!msk.width || busy) return;
        drawing = true;
        try{ wrap.setPointerCapture(e.pointerId); }catch(_){ }
        const p = at(e); if(!p) return;
        last = p; ops.push({ mode, r: radius(), pts: [p] });
        drawOp(msk.getContext('2d'), mode, radius(), [p]); paintOv(); syncBtns();
        e.preventDefault();
      };
      const move = e => {
        if(!drawing || !ops.length) return;
        const p = at(e); if(!p) return;
        // Skip sub-pixel jitter: a phone emits pointermove at 120Hz and every point is replayed on
        // every undo, so an unfiltered drag builds a stroke thousands of points long for no extra detail.
        if(last && Math.abs(p[0] - last[0]) < 1.5 && Math.abs(p[1] - last[1]) < 1.5) return;
        const op = ops[ops.length - 1];
        drawOp(msk.getContext('2d'), op.mode, op.r, [last, p]);   // just the new segment — see rebuild()
        op.pts.push(p); last = p; paintOv();
        e.preventDefault();
      };
      const up = e => { drawing = false; last = null;
        try{ wrap.releasePointerCapture(e.pointerId); }catch(_){ } };
      wrap.addEventListener('pointerdown', down);
      wrap.addEventListener('pointermove', move);
      wrap.addEventListener('pointerup', up);
      wrap.addEventListener('pointercancel', up);

      const setMode = m => { mode = m;
        $q('#er-rub').className = 'btn small ' + (m === 'rub' ? 'btn-cyan on' : 'btn-ghost');
        $q('#er-put').className = 'btn small ' + (m === 'put' ? 'btn-cyan on' : 'btn-ghost'); };
      $q('#er-rub').onclick = () => setMode('rub');
      $q('#er-put').onclick = () => setMode('put');
      $q('#er-undo').onclick = () => { if(ops.length){ ops.pop(); rebuild(); } };
      $q('#er-all').onclick = () => { ops = []; base = null; cleared = true; rebuild(); };
      $q('#er-b').oninput = e => { $q('#er-bv').textContent = (+e.target.value || 18) + '%'; };

      // Size the mask from the SOURCE's natural dimensions, capped: the mask only has to resolve the
      // brush edge, and a 12 MP phone photo would otherwise upload a 12 MP PNG per erase.
      function ready(nw, nh){
        if(!nw || !nh){ hint.textContent = 'That picture would not load — re-add the layer.'; return; }
        const k = Math.min(1, MASK_EDGE / Math.max(nw, nh));
        msk.width = Math.max(2, Math.round(nw * k)); msk.height = Math.max(2, Math.round(nh * k));
        ov.width = msk.width; ov.height = msk.height;
        hint.textContent = 'Drag over the picture to rub it out.';
        // Continue a previous erase. crossOrigin so the canvas stays EXPORTABLE — drawing a
        // cross-origin image without it taints the canvas and toBlob() then throws SecurityError,
        // which would only surface at Apply, after the work. A mask we cannot read is not fatal:
        // start clean and say so, rather than silently dropping the old erase without a word.
        if(l.mask){
          const mi = new Image(); mi.crossOrigin = 'anonymous';
          mi.onload = () => { base = mi; rebuild(); };
          mi.onerror = () => { hint.textContent = 'Could not re-open the earlier erase — starting fresh.';
                               cleared = true; rebuild(); };
          mi.src = l.mask;
        }
        rebuild();
      }
      if(isVid){
        art.addEventListener('loadedmetadata', () => ready(art.videoWidth, art.videoHeight), { once:true });
        art.addEventListener('error', () => ready(0, 0), { once:true });
      } else {
        art.onload = () => ready(art.naturalWidth, art.naturalHeight);
        art.onerror = () => ready(0, 0);
        if(art.complete && art.naturalWidth) ready(art.naturalWidth, art.naturalHeight);
      }

      $q('#er-cancel').onclick = () => PC.closeModal();
      // RE-RESOLVE the layer by id before writing to it. `l` was captured when the dialog opened, and an
      // upload is a real round trip; anything that reloads the project in between (re-entering the view
      // runs `P = load()`) rebuilds P.layers as NEW objects, so the captured one becomes an orphan and
      // the mask would be written to something no longer on the timeline — silently, with the toast
      // still saying it worked. The exact hazard applyMemeEffect and Make-it-talk both hit for real.
      const live = () => P.layers.find(x => x.id === l.id) || l;
      go.onclick = () => {
        if(busy) return;
        // Cleared back to nothing: drop the mask entirely rather than uploading an all-opaque PNG that
        // costs the renderer an extra input and a blend to change nothing.
        if(cleared && !ops.length){
          const cur = live();
          if(cur.mask){ snap(); cur.mask = ''; save(); render(); }
          PC.closeModal(); return;
        }
        busy = true; go.disabled = true; go.textContent = 'Saving…';
        try{
          msk.toBlob(async blob => {
            try{
              if(!blob) throw new Error('could not read the mask');
              const url = await uploadBlob(new File([blob], 'erase.png', { type:'image/png' }));
              snap(); live().mask = url; save(); render();
              PC.closeModal(); toast('erased — render to see it in the export');
            }catch(err){
              busy = false; go.disabled = false; go.textContent = 'Apply';
              toast('couldn’t save the erase: ' + ((err && err.message) || err));
            }
          }, 'image/png');
        }catch(err){
          busy = false; go.disabled = false; go.textContent = 'Apply';
          toast('couldn’t save the erase: ' + ((err && err.message) || err));
        }
      };
    });
  }

  // 🎨 The prompt sheet is AI Chat's OWN "Make an image" studio (PC.openGenStudio), borrowed with a
  // different destination: same style/mood/shot chips, same live preview, same mobile layout, and the
  // list can't drift between the two places. Only the ending differs — the result becomes a layer here
  // instead of a chat message.
  function pickAiImage(){
    if(!PC.openGenStudio){ toast('image generation isn’t available on this build'); return; }
    PC.openGenStudio('image', {
      over: { title:'Generate an image layer', go:'Generate', cmd:'',
              blurb:'Describe what you want to see. It arrives as a new layer on the timeline.' },
      onSubmit: ({ prompt }) => genImageLayer(prompt),
    });
  }
  // Shares _fxBusy with the effect renders on purpose: both are one heavy server job on the same node,
  // and a generation holds its GPU lock outright (the endpoint's own per-user cooldown would 429 the
  // second one anyway — better to say so before sending it).
  async function genImageLayer(prompt){
    prompt = (prompt||'').trim(); if(!prompt) return;
    if(_fxBusy){ toast('still working on the last one — hang on'); return; }
    _fxBusy = true;
    const st=document.getElementById('mb-status');
    if(st) st.textContent='generating your image… this can take a minute';
    try{
      const auth = await selfProof();
      const r = await fetch('/client/meme/generate-image',{ method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth, prompt }) });
      const j = await r.json().catch(()=>({}));
      if(!r.ok || !j.url){ throw new Error(j.detail || j.error || ('HTTP '+r.status)); }
      // An ordinary image layer, exactly like an uploaded photo — it is a Blossom URL either way, so
      // everything downstream (preview, drag/resize, effects, render) needs to know nothing about it.
      // addLayer returns null at the 24-layer limit (and says so itself) — don't then claim it landed.
      if(!addLayer('image', j.url, { name: prompt.slice(0,24) })) return;
      // A generation runs for a minute, which is long enough to wander off to another view — and
      // render() writes into #feed, so calling it from there would paint the builder over whatever
      // the user is now looking at. addLayer already save()d, so the layer is waiting either way.
      if(document.getElementById('mb-stage')) render();
      toast('image added as a new layer');
    }catch(err){ toast('generation failed: '+((err&&err.message)||err)); }
    // The status line is re-queried: render() above rebuilt the panel, so the captured node is stale.
    finally{ _fxBusy=false; const s2=document.getElementById('mb-status'); if(s2) s2.textContent=''; }
  }
  function pickLocalMedia(){
    // Local files upload to Blossom first, so the render service only ever fetches things that exist.
    const inp=document.createElement('input'); inp.type='file';
    inp.accept='image/*,video/*,audio/*'; inp.multiple=true;
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
  // What a URL says it is, by extension. Only the containers the builder can actually place.
  const _EXT_TYPE={ png:'image/png', jpg:'image/jpeg', jpeg:'image/jpeg', gif:'image/gif', webp:'image/webp',
                    avif:'image/avif', bmp:'image/bmp', svg:'image/svg+xml',
                    mp4:'video/mp4', webm:'video/webm', mov:'video/quicktime', m4v:'video/mp4',
                    mp3:'audio/mpeg', m4a:'audio/mp4', aac:'audio/aac', ogg:'audio/ogg',
                    oga:'audio/ogg', opus:'audio/ogg', wav:'audio/wav', flac:'audio/flac' };
  const _typeFromUrl=u=>{
    const m=/\.([a-z0-9]{2,5})(?:[?#]|$)/i.exec(String(u||''));
    return (m && _EXT_TYPE[m[1].toLowerCase()]) || '';
  };
  async function addMediaUrl(u){
    const st=document.getElementById('mb-status');
    if(st) st.textContent='fetching it…';
    let blob=null;
    try{ blob=await fetch('/client/proxy-image?url='+encodeURIComponent(u)).then(r=>r.ok?r.blob():null); }catch(_){}
    // Direct fetch as the fallback, exactly like the Effects studio: the proxy is the reliable path
    // (CORS + private-address guard), but a CORS-friendly host still works without it.
    if(!blob){ try{ blob=await fetch(u).then(r=>r.ok?r.blob():null); }catch(_){} }
    // The node's image proxy relabels ANY non-image body as `image/png` (chat._proxy_fetch), so the
    // blob's own type cannot be trusted to say what a dropped link actually is — a dropped .mp4 came
    // back "image/png" and landed as a broken image layer. Believe the URL's extension when it
    // disagrees, and only fall back to the reported type when the URL carries no extension.
    const type=_typeFromUrl(u) || blob && blob.type || '';
    if(!blob || !/^(image|video|audio)\//.test(type)){
      if(st) st.textContent='';
      toast(blob ? 'that link isn’t an image, video or audio file' : 'could not fetch that link');
      return 0;
    }
    const base=(u.split(/[?#]/)[0].split('/').pop()||'dropped').slice(0,24);
    const ext=(type.split('/')[1]||'jpg').split('+')[0];
    const name=/\.\w{2,4}$/.test(base) ? base : base+'.'+ext;
    return await addMediaFiles([new File([blob], name, { type })]);
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
      const files=Array.from(dt.files||[]).filter(f=>/^(image|video|audio)\//.test(f.type||''));
      let n=0;
      if(files.length) n=await addMediaFiles(files);
      else { const u=_dropUrl(dt); if(u) n=await addMediaUrl(u); else toast('drop an image, a video, a track, or a link to one'); }
      if(n) toast(n===1 ? 'added as a new layer' : n+' layers added');
    });
  }

  // Music: a local mp3/m4a/ogg/wav (uploaded to Blossom first, like every other layer source) or a track
  // already on your drive. One button, both paths — a separate "from Blossom" for audio would be a fourth
  // add-button in a bar that is already full on a phone.
  // ---------- stickers ----------
  // An emoji as a LAYER. Not as a text layer: the caption font is Liberation Sans (that is deliberate —
  // it is the font ffmpeg draws with), which has no emoji glyphs at all, so a 🔥 in a caption renders as a
  // tofu box in the export. Colour emoji through drawtext needs an emoji font AND a freetype built with
  // colour support, which is not something to depend on across three nodes.
  //
  // So the BROWSER draws it — it already has the system emoji font — onto a transparent canvas, and the PNG
  // goes to Blossom and onto the timeline as an ordinary image layer. A CUSTOM instance emoji is already an
  // image on our own host, so that one skips the canvas entirely and is used by URL.
  const STICKER_PX = 320;         // plenty for a 720-1080 canvas; a sticker is rarely more than a third wide
  function emojiToPngFile(ch){
    return new Promise((resolve, reject)=>{
      try{
        const c=document.createElement('canvas'); c.width=c.height=STICKER_PX;
        const g=c.getContext('2d');
        // No background fill — the transparency is the point, so it composites over whatever is beneath.
        g.textAlign='center'; g.textBaseline='middle';
        // A little headroom (0.8) so the taller emoji are not clipped by their own em box.
        g.font=Math.round(STICKER_PX*0.8)+'px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif';
        g.fillText(ch, STICKER_PX/2, STICKER_PX/2);
        c.toBlob(b=>{
          if(!b) return reject(new Error('could not draw that emoji'));
          resolve(new File([b], 'sticker.png', { type:'image/png' }));
        }, 'image/png');
      }catch(err){ reject(err); }
    });
  }
  function pickSticker(btn){
    if(!PC.openEmojiPopover){ toast('the emoji picker is unavailable'); return; }
    PC.openEmojiPopover(btn, async (val, close)=>{
      if(close) close();
      const st=document.getElementById('mb-status');
      try{
        if(st) st.textContent='adding the sticker…';
        let url='';
        const custom = /^:.+:$/.test(val) ? (PC.instEmojiUrl ? PC.instEmojiUrl(val) : '') : '';
        // A custom emoji is ALREADY a hosted image, on the absolute base this very client was served from
        // (see _emoji_base), so it goes on the timeline by URL — uploading a canvas copy would be a second
        // blob of the same picture. On a LAN-only deployment whose emoji host is not also its Blossom host,
        // the render's SSRF guard needs that name in Admin → Blossom → "Own media hosts" (media_own_hosts),
        // which is the same exemption every other own-host media source needs.
        if(custom) url = custom;
        else url = await uploadBlob(await emojiToPngFile(val));
        // A square box a third of the frame, in the middle, selected — so the very next gesture is dragging
        // it where you want it. addOverlay (not addLayer): a sticker decorates the meme, it is not another
        // clip in the sequence.
        const side=Math.round(Math.min(P.w, P.h)/3);
        const l=addOverlay({ src:url, name:String(val).slice(0,12), fit:'contain',
          w:side, h:side, x:Math.round((P.w-side)/2), y:Math.round((P.h-side)/2) });
        if(l){ save(); render(); toast('sticker added — drag or resize it'); }
        if(st) st.textContent='';
      }catch(err){ if(st) st.textContent=''; toast('could not add that sticker: '+((err&&err.message)||err)); }
    });
  }

  // ---------- voice-over ----------
  // Talk over the meme. Recording PLAYS THE PREVIEW at the same time (from the top), because the whole
  // point of a voice-over is to land your words on the pictures — recording in silence and then dragging
  // the clip around until it lines up is the workflow this replaces.
  //
  // The existing music beds are muted for the take: they come out of the same speakers the microphone is
  // pointed at, so leaving them up records the soundtrack a second time, quieter and slightly late.
  // MediaRecorder gives webm/opus, which ffmpeg reads like any other audio layer — the render path needs
  // nothing new. Never uses window.prompt/confirm (see uiConfirm).
  async function recordVoice(){
    if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder==='undefined'){
      toast('this browser can’t record audio — upload a file instead'); return;
    }
    let stream=null;
    try{ stream = await navigator.mediaDevices.getUserMedia({ audio:true }); }
    catch(err){ toast('microphone not available: '+((err&&err.message)||err)); return; }
    const mimes=['audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus','audio/mp4'];
    const mime=mimes.find(m=>{ try{ return MediaRecorder.isTypeSupported(m); }catch(_){ return false; } }) || '';
    let rec=null, chunks=[], t0=0, timer=null, done=false;
    const stopTracks=()=>{ try{ stream.getTracks().forEach(t=>t.stop()); }catch(_){ } };
    // Remember and restore the preview volumes rather than assuming they were all at the default.
    const wasVol=_audioEls.map(a=>({ a, v:a.volume }));
    PC.modal(`<h3>🎙️ Record a voice-over</h3>
      <div class="muted small" style="margin-bottom:10px">The meme plays from the start while you record, and
      the music is muted for the take so it isn’t recorded twice. Stop when you’re done — the take is added
      as its own audio track you can slide and trim like any other.</div>
      <div class="mb-rec"><b id="mbr-time">0.0s</b><span class="mb-recdot" id="mbr-dot"></span></div>
      <button class="btn btn-neon full" id="mbr-go">● Start recording</button>
      <button class="btn btn-cyan full" id="mbr-stop" disabled>■ Stop &amp; add</button>
      <button class="btn btn-ghost full" id="mbr-cancel">Cancel</button>`, root=>{
      const go=root.querySelector('#mbr-go'), stop=root.querySelector('#mbr-stop');
      const tEl=root.querySelector('#mbr-time'), dot=root.querySelector('#mbr-dot');
      const cleanup=()=>{ if(timer){ clearInterval(timer); timer=null; } stopTracks();
        wasVol.forEach(x=>{ try{ x.a.volume=x.v; }catch(_){ } }); };
      root.querySelector('#mbr-cancel').onclick=()=>{
        done=true;
        try{ if(rec && rec.state!=='inactive') rec.stop(); }catch(_){ }
        try{ stopPlay(true); }catch(_){ }
        cleanup(); PC.closeModal();
      };
      go.onclick=()=>{
        if(rec) return;
        try{ rec = mime ? new MediaRecorder(stream, { mimeType:mime }) : new MediaRecorder(stream); }
        catch(err){ toast('could not start recording: '+((err&&err.message)||err)); return; }
        rec.ondataavailable=(e)=>{ if(e.data && e.data.size) chunks.push(e.data); };
        rec.onstop=async()=>{
          cleanup();
          try{ stopPlay(true); }catch(_){ }
          if(done || !chunks.length){ PC.closeModal(); return; }
          const blob=new Blob(chunks, { type: chunks[0].type || mime || 'audio/webm' });
          const secs=(Date.now()-t0)/1000;
          PC.closeModal();
          const s2=document.getElementById('mb-status');
          if(s2) s2.textContent='uploading the voice-over…';
          try{
            const ext=/ogg/.test(blob.type)?'ogg':(/mp4/.test(blob.type)?'m4a':'webm');
            const url=await uploadBlob(new File([blob], 'voiceover.'+ext, { type:blob.type }));
            // Starts at 0 and lasts exactly as long as the take — a voice-over is timed to the picture,
            // so the default must NOT be addLayer's "span the whole meme" (that is right for music).
            // volume 1, not 0.6: a voice competing with the bed at 0.6 is the thing you can't hear.
            const l=addLayer('audio', url, { name:'voice-over', start:0,
              dur:+Math.max(0.3, Math.min(secs, 120)).toFixed(2), volume:1, fade:false });
            if(s2) s2.textContent='';
            if(l){ save(); render(); toast('voice-over added — slide or trim it like any track'); }
          }catch(err){ if(s2) s2.textContent=''; toast('upload failed: '+((err&&err.message)||err)); }
        };
        chunks=[]; t0=Date.now();
        try{ rec.start(); }catch(err){ toast('could not start recording: '+((err&&err.message)||err)); return; }
        // Silence the beds, then play from the top so the take lines up with the pictures.
        _audioEls.forEach(a=>{ try{ a.volume=0; }catch(_){ } });
        try{ stopPlay(true); togglePlay(); }catch(_){ }
        go.disabled=true; stop.disabled=false; if(dot) dot.classList.add('on');
        timer=setInterval(()=>{
          const s=(Date.now()-t0)/1000;
          if(tEl) tEl.textContent=s.toFixed(1)+'s';
          // Hard stop at the layer cap so a forgotten recording cannot become a 20-minute upload.
          if(s>=120){ try{ rec.stop(); }catch(_){ } }
        }, 100);
      };
      stop.onclick=()=>{ stop.disabled=true; try{ if(rec && rec.state!=='inactive') rec.stop(); }catch(_){ } };
    });
  }

  // Apply a full effect (the app's whole effect engine — glow, alive, nakedman, meme, sopranos, …) TO an
  // image layer. The per-layer "Meme effect" dropdown is the SOURCE OF TRUTH (there is no separate effect
  // button): the server runs the effect on this layer's image and hands back the resulting clip, which we
  // swap in as the layer's source — so the effect is applied ON that image, exactly like the Effects
  // studio. Guarded like doRender so a double-tap can't fire two renders.
  let _fxBusy = false;
  // `opts` renames the operation for the parts a person reads — the progress line, the toast and the
  // layer's name. Background removal goes through this same path (it is a server-side transform of the
  // layer's image that swaps its source in place, with the same one-way-door protection and the same
  // ↺ revert), but calling it "applying removebackground…" and naming the layer after a command would
  // be nonsense. Effects pass nothing and keep reading as effects.
  async function applyMemeEffect(base, name, opts){
    opts = opts || {};
    const label = opts.busy || ('applying '+name);
    const done = opts.done || '';
    const layerName = opts.layerName || '';
    if(!name || !base || !base.src){ toast('add an image to this layer first'); return; }
    if(_fxBusy){ toast('still rendering the last effect — hang on'); return; }
    _fxBusy = true;
    const st=document.getElementById('mb-status');
    if(st) st.textContent=label+'…';
    try{
      const auth = await selfProof();
      const r = await fetch('/client/meme/apply-effect',{ method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ pubkey: ME.pubkey, auth, url: base.src, effect: name }) });
      const j = await r.json().catch(()=>({}));
      if(!r.ok || !j.url){ throw new Error(j.detail || j.error || ('HTTP '+r.status)); }
      // The effect transforms the still into a clip — swap the layer's source IN PLACE (keep its box and
      // timeline slot), so the effect lands on this image. It becomes a video layer; duration follows the
      // effect clip so the whole thing plays.
      //
      // RE-RESOLVE the layer by ID first. `base` was captured before a server render that can run for a
      // minute, and anything that reloads the project in the meantime (re-entering the view runs
      // `P = load()`) rebuilds P.layers as new objects — mutating the captured one would then change
      // nothing at all, silently. Same hazard the Make-it-talk handler hit for real.
      base = P.layers.find(x => x.id === base.id) || base;
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
      // An erase was painted against the artwork that is being REPLACED here. On the new picture the
      // same mask covers something else, so it would rub out the wrong region on every render from now
      // on, with nothing on screen to explain why. Drop it and say so — a wrong erase you cannot see the
      // cause of is worse than one you have to redo.
      const _hadMask = !!base.mask; base.mask = '';
      const isVid = (j.is_video !== false);
      base.type = isVid ? 'video' : 'image';
      // Only a VIDEO result re-times the layer. ffprobe reports a duration for a still too (one frame,
      // ~0.04s), and taking it would shrink the slot to a frame — the layer would be in the project and
      // invisible in the export. A cut-out is the same picture with its background gone: same slot.
      if(isVid && +j.dur>0) base.dur = +j.dur;
      base.name = String(layerName || name).slice(0,24);
      base.trim = 0;
      sel = base.id;
      save(); render();
      toast((done || (name+' applied')) + (_hadMask ? ' — the erase was cleared, it was drawn on the old picture' : ''));
    }catch(err){ toast('failed: '+((err&&err.message)||err)); }
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
    PC.modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-ai"></use></svg>Add an effect layer</h3>
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
            // Remember WHICH character this layer is. The layer itself is a rendered transparent
            // clip, so nothing about it says "jerry" any more — and that name is what lets 🗣️ Make
            // it talk animate the pose's own artwork instead of trying to lip-sync a video.
            fxPose: e.pose ? e.name : '',
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
      const _scrub=document.getElementById('mb-scrub');
      const edit={ w:P.w, h:P.h, fps:P.fps, bg:P.bg, duration:renderEnd(),
        fmt:_fmt(),
        // A still is taken AT THE PLAYHEAD — the frame you are looking at is the frame you meant.
        still:(_fmt()==='png' ? +(_scrub?_scrub.value:0)||0 : 0),
        layers:P.layers.map(l=>({ type:l.type, src:l.src, start:+l.start, dur:+l.dur, trim:+l.trim||0,
          x:Math.round(l.x), y:Math.round(l.y), w:Math.round(l.w), h:Math.round(l.h),
          opacity:+l.opacity, effect:l.effect, sound:l.sound||'', soundVolume:(l.soundVolume==null?1:+l.soundVolume), mute:!!l.mute,
          flipH:!!l.flipH, flipV:!!l.flipV, rotate:+l.rotate||0,
          // NOT `+l.volume||1`: that turned a deliberate volume of 0 back into full volume.
          volume:(l.volume==null?1:+l.volume), fade:!!l.fade,
          speed:_speedOf(l), xin:+l.xin||0, xout:+l.xout||0,
          // The WRAPPED text, not the raw text: the renderer draws one line per newline it is given, and
          // wrapText is the same function the preview lays out, so what you saw is what gets drawn.
          text:(l.type==='text' ? wrapText(l) : l.text), size:+l.size, color:l.color, stroke:l.stroke,
          box:!!l.box, boxColor:l.boxColor||'#000000', boxAlpha:(l.boxAlpha==null?0.55:+l.boxAlpha), shadow:!!l.shadow,
          // The erase mask travels as a URL like `src` does; the server fetches it down the same guarded
          // path. This payload is an explicit WHITELIST, so a field missing here is a field the renderer
          // never sees — the erase would preview perfectly on the stage and be silently absent from
          // every export, which is indistinguishable from "the eraser is broken".
          mask:l.mask||'',
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
      else if(o2){ o2.innerHTML='<div class="mb-err">⚠️ '+enc(_renderErr((err&&err.message)||err))+'</div>'; _openResultPane(); }
    }finally{
      _rendering=false; _renderAbort=null; _stopRenderClock();
      const b=document.getElementById('mb-render');
      if(b){ b.textContent=(_fmt()==='png'?'📷 Still':(_fmt()==='gif'?'🎞️ GIF':'🎬 Render'));
        b.classList.remove('btn-danger'); b.disabled=false; }
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

  // The finished meme (or the error that stopped it) lands in its own pane, and the tab strip grows a
  // 🎬 Result tab to hold it. It used to be appended to the bottom of the page, which on a phone meant a
  // render you had to go looking for — and in the new fixed-height layout would be off-screen entirely.
  function _openResultPane(){
    _hasResult = true;
    _paintTabs();
    _showTab('result');
  }

  function showResult(blob, out){
    // Re-look-up the panel. `out` was captured before the render started, and ANY edit made while it ran
    // rebuilds the view — which detaches that element, so a finished meme would be written into a node
    // that is no longer on the page and silently vanish.
    out = document.getElementById('mb-result') || out;
    if(!out) return;
    _openResultPane();
    const url=URL.createObjectURL(blob);
    // Trust the BLOB's own type, not the format we asked for. A render can be forwarded to a peer node
    // (see _meme_lb_forward), and a peer still running older code answers with an MP4 whatever `fmt`
    // said — labelling that .gif would hand the user a file that will not open.
    const mime=String(blob.type||'video/mp4');
    const isImg=/^image\//.test(mime);
    const ext=mime.indexOf('gif')>=0 ? 'gif' : (mime.indexOf('png')>=0 ? 'png' : 'mp4');
    // Opened from a post (🎞️ Meme Builder on a note) → offer the reply right here, the way the Effects
    // studio does. Without it the only route back to the thread is copy-link, find the post, paste.
    const to=_replyTarget();
    const replyBtn = to ? `<button class="btn btn-neon small" id="mb-reply"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg>Reply${to.name?' to '+enc(to.name):' to the post'}</button>` : '';
    out.innerHTML=`<div class="mb-result">
      ${isImg ? `<img src="${url}" alt="" class="mb-resvid">`
              : `<video src="${url}" controls playsinline class="mb-resvid"></video>`}
      <div class="mb-resacts">
        ${replyBtn}
        <button class="btn btn-neon small" id="mb-post"><svg class="ic b-ic" aria-hidden="true"><use href="#i-send"></use></svg>Post to Nostr</button>
        <button class="btn btn-cyan small" id="mb-copy"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Upload &amp; copy link</button>
        <button class="btn btn-cyan small" id="mb-again" title="Put this render back on the timeline as a layer, so you can build on top of it">🎞️ Use as a layer</button>
        <a class="btn btn-neon small" href="${url}" download="${enc((P.name||'meme').replace(/[^\w.-]+/g,'_').slice(0,40))}.${ext}">⬇️ Download</a>
      </div>
      <div class="muted small" id="mb-reslink"></div>
    </div>`;
    const file=new File([blob], 'meme.'+ext, { type:mime });
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
        // A GIF/still comes back as an IMAGE layer — feeding a .png in as type 'video' would make the
        // renderer decode it with -ss/-t instead of -loop 1, and the layer would be one frame long.
        addLayer(isImg ? 'image' : 'video', u, { name:(P.name||'meme').slice(0,24) });
        save(); render();
        toast('added as a new layer at the end of the timeline');
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
    //
    // An EMPTY box is not the number zero. clamp() runs everything through `Number(v)||0`, so the instant
    // you select the contents of Width and hit delete — the normal way to retype a value — the layer was
    // set to the field's MINIMUM (16px, or 0.1s for a length) and saved. Click away, or let anything
    // rebuild the panel, and that minimum is what comes back: "I set a size and it resets". Ignore a box
    // that is empty or mid-way through a number, and on `change` (blur/Enter) put the layer's real value
    // back into a box left empty, so the field can never disagree with the layer it edits.
    const num=(id,key,lo,hi)=>{
      const el=root.querySelector('#'+id); if(!el) return;
      const write=(raw)=>{
        const s=String(raw==null?'':raw).trim();
        if(s==='' || !isFinite(Number(s))) return false;
        snapBurst(key+':'+l.id); l[key]=clamp(s,lo,hi); save();
        const it=root.querySelector('.mb-item[data-id="'+l.id+'"]'); if(it) applyGeom(it,l);
        if(key==='start'||key==='dur') repaint('timeline');
        return true;
      };
      el.addEventListener('input',(e)=>write(e.target.value));
      el.addEventListener('change',(e)=>{ if(!write(e.target.value)) e.target.value=l[key]; });
    };
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
    // 🪄 Remove the background — the same rembg cut-out the `removebackground` command does, run on this
    // layer's image and swapped in place, so the layers beneath show through. Not in the "Meme effect"
    // dropdown: that list is the effect catalogue, and a cut-out is a compositing tool you go looking for
    // by name, not something to find among a hundred entries. Keeps the layer's own name — it is still
    // the same picture. First run on a node downloads the ~170MB u2net model, hence the honest wait text.
    on('mb-nobg','click',()=>applyMemeEffect(l, 'removebackground', {
      busy: 'removing the background', done: '🪄 background removed — ↺ undo puts it back',
      layerName: l.name || srcName(l.src) }));
    // 🗣️ Make it talk — the face in this layer lip-syncs a line in one of YOUR CLONED VOICES.
    //
    // It BORROWS AI Chat's voice studio, exactly like "Add a voice line" above does: the library, the
    // recorder, the GPU-queue notice and the length estimate all live there, and a generation is the
    // node's GPU either way. Only the ENDING differs — the take is uploaded and then handed to
    // /client/meme/talk to animate this layer's face, instead of landing as its own audio layer.
    //
    // So the split is: speech = GPU, through the studio's own locked/load-balanced path; mouth = CPU,
    // through the meme render queue. Neither half reimplements the other's discipline.
    on('mb-talk','click',()=>{
      if(!PC.openVoiceStudio){ toast('voice cloning isn’t available on this build'); return; }
      // Place the mouth BEFORE spending a minute of GPU on the voice, not after: a marker you drag
      // is instant, and getting it wrong should cost a drag rather than another generation.
      // Hold the layer's ID, never the layer OBJECT. A voice generation runs for the better part of a
      // minute with a modal over the app, and any re-entry into this view runs `P = load()`, which
      // rebuilds P.layers as brand-new objects. A captured object is then an ORPHAN: mutating it
      // changes nothing the project can see, and P.layers.indexOf(it) is -1 — so `splice(-1 + 1, …)`
      // still inserted the voice at the front while the talking video went nowhere. What you got was
      // an audio layer and no animation: indistinguishable from the old "Add a voice line", which is
      // exactly how it was reported. Re-resolve by ID at take time instead.
      const lid = l.id;
      // A CHARACTER POSE places the mouth on the pose's ARTWORK rather than on the layer's source,
      // because the layer's source is the rendered clip and the artwork is what gets animated.
      const pose = (l.type !== 'image' && l.fxPose) ? l.fxPose : '';
      pickMouth(l.src, pose).then(mouth => {
        if(!mouth) return;                       // cancelled — no voice generated, nothing spent
        PC.openVoiceStudio({
        useLabel: '🗣️ Make the face say it',
        onTake: async (blob, voiceName, text) => {
          if(_fxBusy){ toast('still working on the last one — hang on'); return; }
          const cur = P.layers.find(x => x.id === lid);
          if(!cur || (!cur.src && !pose)){ toast('that layer is gone — nothing to make talk'); return; }
          _fxBusy = true;
          const st = document.getElementById('mb-status');
          if(st) st.textContent = 'animating the mouth…';
          try{
            // The renderer only ever fetches URLs, so the take goes to the user's own drive first —
            // the same route every other layer source takes.
            const name = (text || voiceName || 'talk').slice(0, 24);
            const audio = await uploadBlob(new File([blob], name.replace(/[^\w .-]/g,'_') + '.wav',
                                                    { type:'audio/wav' }));
            const auth = await selfProof();
            const r = await fetch('/client/meme/talk',{ method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify(pose
                ? { pubkey: ME.pubkey, auth, audio, character: pose, mouth }
                : { pubkey: ME.pubkey, auth, url: cur.src, audio, mouth }) });
            const j = await r.json().catch(()=>({}));
            if(!r.ok || !j.url) throw new Error(j.detail || j.error || ('HTTP '+r.status));
            // Swap the layer's source IN PLACE, keeping its box and timeline position — same one-way-
            // door protection and same ↺ revert as applyMemeEffect, because it is the same kind of edit.
            snap();
            if(!cur.origSrc){
              cur.origSrc = cur.src; cur.origType = cur.type;
              cur.origName = cur.name || ''; cur.origDur = +cur.dur || 0;
            }
            cur.src = j.url; cur.type = 'video'; cur.trim = 0;
            // Same reason as applyMemeEffect: the mask was drawn on the still, and this is a different
            // clip — often a different shape, since a talking take is rendered to its own frame.
            cur.mask = '';
            if(+j.dur > 0) cur.dur = +j.dur;
            cur.name = name;
            sel = cur.id;
            // A TRANSPARENT result is silent, and has to be: MP4 carries no alpha at all (a cut-out
            // rendered to MP4 comes back as a black rectangle with the subject on it), and an audio
            // stream inside a VP9-alpha WebM corrupts the alpha. So when the server kept the layer's
            // transparency it hands back a mute clip, and the spoken line goes on the timeline as its
            // own audio layer — aligned to this one, at full volume and with no fade, because it is a
            // VOICE, not a music bed.
            if(j.alpha){
              // Built from scratch, NOT copied off the video layer: a copy would inherit its speed,
              // crossfade ramps, effect and origSrc undo-state, none of which mean anything on an
              // audio layer and one of which (origSrc) would make ↺ offer to "restore" a photo onto
              // the voice. Volume 1 and no fade because this is a VOICE, not a music bed — the same
              // choice the voice-over path makes for the same reason.
              const spoken = {
                id: nid(), type:'audio', src: audio, name: (name + ' (voice)').slice(0, 24),
                start: +cur.start||0, dur: (+j.dur>0) ? +j.dur : (+cur.dur||3), trim: 0,
                x:0, y:0, w:0, h:0, opacity:1, effect:'none', volume:1, fade:false, mute:false,
                flipH:false, flipV:false, rotate:0,
                sound:'', soundVolume:1, text:'', size:64, color:'#ffffff', stroke:'#000000', align:'',
              };
              // `cur` came out of P.layers, so this index is real. Never splice on an indexOf that can
              // be -1: it silently prepends, which is how the orphaned-layer bug still managed to add
              // the voice while dropping the animation.
              const at = P.layers.indexOf(cur);
              if(P.layers.length >= 24) toast('24 layers is the limit — the voice could not be added');
              else P.layers.splice(at < 0 ? P.layers.length : at + 1, 0, spoken);
            }
            save(); render();
            toast(j.alpha ? '🗣️ it talks — the voice is its own layer, ↺ undoes the picture'
                          : '🗣️ it talks — ↺ undo puts the photo back');
          }catch(err){ toast('couldn’t make it talk: '+((err&&err.message)||err)); }
          finally{ _fxBusy = false; const s2=document.getElementById('mb-status'); if(s2) s2.textContent=''; }
        },
        });
      });
    });
    // ✂ Erase parts — a per-layer alpha mask, NOT a new source. That is why it sits beside the
    // background cut-out rather than replacing it: rembg swaps the layer's picture (and needs origSrc to
    // undo), while this leaves the source alone and is undone by dropping one field.
    on('mb-erase','click',()=>eraseParts(l));
    on('mb-erase-clear','click',()=>{
      if(!l.mask) return;
      snap(); l.mask=''; save(); render(); toast('erase undone — the whole layer is back');
    });
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
    // Speed. The preview mirrors it with playbackRate + a scaled local time (see seek), so slow-mo and 2×
    // are visible without rendering — the whole reason to have a slider rather than a number.
    // Changing the speed keeps the TRIMMED REGION and moves the slot, not the other way round: the footage
    // you picked with the handles is what you meant to show, so a 4s region at 2x becomes a 2s slot. Scaling
    // dur by the speed RATIO is what preserves that (trim stays put, out-point stays put).
    const _setSpeed=(v)=>{
      const prev=_speedOf(l);
      l.speed=clamp(v,0.25,4);
      const now=_speedOf(l);
      if(now!==prev && +l.dur>0) l.dur=+Math.max(0.1, Math.min(120, l.dur*prev/now)).toFixed(2);
      save();
      const sv=root.querySelector('#mb-spd-val'); if(sv) sv.textContent=_speedOf(l)+'×';
      const sn=root.querySelector('#mb-spd-slot'); if(sn) sn.textContent=_slotNote(l);
      root.querySelectorAll('[data-spd]').forEach(b=>b.classList.toggle('on', +b.dataset.spd===_speedOf(l)));
      const it=root.querySelector('.mb-item[data-id="'+l.id+'"] video');
      if(it){ try{ it.playbackRate=_speedOf(l); }catch(_){ } }
      // The SLOT changed length, so the clip bar and the project end did too. The trim handles do NOT move:
      // dur was scaled by the speed ratio, so trim + dur*speed — the out-point — is exactly where it was.
      repaint('timeline'); syncScrub();
    };
    on('mb-f-speed','input',(e)=>{ snapBurst('speed:'+l.id); _setSpeed(e.target.value); });
    root.querySelectorAll('[data-spd]').forEach(b=>b.addEventListener('click',()=>{
      snap(); _setSpeed(+b.dataset.spd);
      const sl=root.querySelector('#mb-f-speed'); if(sl) sl.value=_speedOf(l);
    }));
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
    on('mb-split','click',cutSelected);
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
    // The handles pick IN and OUT points in the SOURCE, but `dur` is the length of the layer's SLOT on the
    // timeline — and at any speed but 1 those are different numbers (a 4s region at 2x occupies a 2s slot).
    // So every conversion between the two goes through the speed. Without this, setting a speed made the
    // trim handles point somewhere other than the footage that actually played.
    const sp=()=>_speedOf(l);
    const outAt=()=>(+l.trim||0)+(+l.dur||0)*sp();     // source time of the out-point
    const fmt=(s)=>{ s=Math.max(0,s||0); const m=Math.floor(s/60), ss=Math.floor(s%60); return m+':'+String(ss).padStart(2,'0'); };
    function paint(){
      if(!D) return;
      const inT=clamp(l.trim,0,D), outT=clamp(outAt(),0,D);
      const a=inT/D*100, b=outT/D*100;
      inH.style.left=a+'%'; outH.style.left=b+'%';
      selEl.style.left=a+'%'; selEl.style.width=Math.max(0,b-a)+'%';
      dimL.style.width=a+'%'; dimR.style.left=b+'%'; dimR.style.width=Math.max(0,100-b)+'%';
      if(tin) tin.textContent=fmt(inT);
      if(tout) tout.textContent=fmt(outT);
      // The handles can only ever point INSIDE the source, so when the slot is longer than the footage
      // left after the in-point, (outT-inT) is the footage — not the slot. Say both, rather than showing
      // a number that contradicts the Length box right above it.
      if(tlen){
        const held = (+l.dur||0) - (outT-inT)/sp();
        tlen.textContent = (outT-inT).toFixed(1)+'s' + (held > 0.05 ? ` +${held.toFixed(1)}s held` : '');
      }
    }
    function ready(){
      D=vid.duration||0;
      if(!D || !isFinite(D)) return;
      // Repair ONLY what is genuinely unusable, and never a length the user chose.
      //
      // This used to also rewrite `dur` whenever the slot ran past the end of the source — which meant
      // typing a Length longer than the clip's own footage was silently undone, and SAVED that way, the
      // next time anything rebuilt the inspector. Renaming the project, adding a layer, an undo: all of
      // them rebuild, so the length "went back to what it was before" with no message and no undo entry,
      // and the export kept the old length however many times you retyped it.
      //
      // A longer slot is a legitimate edit: the preview has always held the last frame for the remainder,
      // and the renderer now pads the same way (see meme_builder_service, tpad=stop_mode=clone), so what
      // the timeline says is what comes out. An in-point past the end of the source, or a slot with no
      // length at all, are the only states that cannot be rendered at all — repair those.
      let fixed = false;
      if((+l.trim||0)>=D){ l.trim=0; fixed=true; }
      if(!(+l.dur>0)){ l.dur=+((D-(+l.trim||0))/sp()).toFixed(2); fixed=true; }
      if(fixed) save();
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
            const out=outAt();
            t=Math.max(0, Math.min(t, out-0.1));   // in-point stays left of the out-point
            l.trim=+t.toFixed(2); l.dur=+((out-t)/sp()).toFixed(2);
          } else {
            t=Math.min(D, Math.max(t, (+l.trim||0)+0.1)); // out-point stays right of the in-point
            l.dur=+((t-(+l.trim||0))/sp()).toFixed(2);
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
    _saveSecState();       // …same for which inspector groups are open (see _saveSecState)
    pauseAudio();          // BEFORE the rebuild — after it, these elements are detached and unreachable
    // A rebuild empties #mb-result, so the Result tab must go with it — otherwise it stays on the strip
    // and opens an empty pane. (showResult sets it again.)
    _hasResult = false;
    if(_tab === 'result') _tab = 'layer';
    // Full-height, non-scrolling layout for this view — same opt-in class the DM/AI/Translate views use.
    feed.classList.add('feed-meme');
    feed.innerHTML=view();
    _audioEls = Array.from(feed.querySelectorAll('#mb-audios audio'));
    if(_prevT){ const s=feed.querySelector('#mb-scrub'); if(s) s.value=_prevT.toFixed(2); setTimeout(()=>seek(_prevT),0); }
    const root=feed;
    bindStage(root); bindTimeline(root); bindInspector(root); bindDrop(root);
    // After the lanes exist (fitZoom measures them) and before the toolbar is wired, so the ⤢/± buttons
    // bound below already show the zoom that was applied. No-op unless something asked to fit.
    _applyFitIfPending();
    const on=(id,ev,fn)=>{ const e=root.querySelector('#'+id); if(e) e.addEventListener(ev,fn); };
    root.querySelectorAll('.mb-tab').forEach(b=>b.addEventListener('click',()=>_showTab(b.dataset.tab)));
    on('mb-add-media','click',pickMedia);
    on('mb-add-text','click',()=>{ addLayer('text'); render(); });
    root.querySelectorAll('.mb-szb').forEach(b=>b.addEventListener('click',()=>{
      const [w,h]=String(b.dataset.size||'').split('x').map(Number); if(w&&h) _resizeCanvas(w,h);
    }));
    on('mb-undo','click',undo);
    on('mb-redo','click',redo);
    on('mb-proj','click',projectMenu);
    on('mb-cutall','click',cutAll);
    // Explicit "snap everything back-to-back", in the clips' current time order. This used to happen
    // automatically on every drop, which made adjusting one clip rewrite the whole timeline.
    on('mb-arrange','click',()=>{ snap(); resequence(); unsnapIfUnchanged(); save(); render();
      toast(_xfade() ? `clips laid out with a ${_xfade()}s crossfade` : 'clips laid back-to-back'); });
    // Changing the crossfade RE-LAYS the clips, because the dissolve only exists if consecutive clips
    // overlap in time — setting a number that changed nothing on screen would read as a dead control.
    on('mb-xfade','change',(e)=>{
      groupEdit(()=>{ P.xfade = clamp(e.target.value, 0, 2); resequence(); });
      save(); render();
      toast(_xfade() ? `${_xfade()}s crossfade — clips now overlap` : 'hard cuts — clips laid back-to-back');
    });
    // Walk to the next ladder rung in that direction from wherever we are — indexOf is no good once Fit
    // has set an off-ladder zoom like 0.37x, which is exactly when you most want to step out of it.
    on('mb-zoomin','click',()=>setZoom(_ZOOMS.find(z=>z>_zoom()+1e-6) || ZOOM_MAX));
    on('mb-zoomout','click',()=>setZoom([..._ZOOMS].reverse().find(z=>z<_zoom()-1e-6) || ZOOM_MIN));
    on('mb-zoomfit','click',()=>{ const z=fitZoom(); if(z) setZoom(z); });
    on('mb-render','click',doRender);
    on('mb-play','click',()=>togglePlay());
    on('mb-scrub','input',(e)=>seek(+e.target.value));
    // Re-render the bar so the button says what it will now produce (📷 Still / 🎞️ GIF / 🎬 Render).
    on('mb-fmt','change',(e)=>{ P.fmt=e.target.value; save(); render(); });
    // Repaint the stage directly rather than re-rendering the view: a full render() would replace the
    // colour input mid-drag and drop the picker.
    on('mb-bg','input',(e)=>{ snapBurst('bg'); P.bg=e.target.value||'#000000';
      const st=document.getElementById('mb-stage'); if(st) st.style.background=P.bg; save(); });
    // Row selection and ruler scrubbing are DELEGATED inside bindTimeline — they must survive
    // repaint('timeline'), which replaces every row.
    if(_prevT) paintPlayhead(_prevT); else seek(0);
    // The stage is sized from the space actually left over (see _fitStage). Called straight away — reading
    // .mb-fit's clientHeight flushes layout, so the box is already real — and AGAIN on the next frame, for
    // the things that are not settled yet (a webfont, an <img> that changes a row's height). Not rAF alone:
    // that leaves the first paint dependent on a callback the browser is free to defer, and a stage that has
    // not been fitted is 2px wide.
    _watchFit();
    _fitStage();
    requestAnimationFrame(_fitStage);
  }

  // ---------- the project menu ----------
  // Save / Open / Rename / New / Clear behind one button. They were five buttons in a bar that already had
  // eleven, which on a phone wrapped into a wall where "Clear all" sat one thumb-width from "Add text".
  function projectMenu(){
    PC.modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-folder"></use></svg>${enc(P.name || 'Untitled')}</h3>
      <div class="muted small" style="margin-bottom:10px">${P.layers.length} layer${P.layers.length===1?'':'s'} · ${P.w}×${P.h} · ${projEnd().toFixed(1)}s</div>
      <button class="btn btn-neon full" id="mbp-save"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg>Save to my Blossom drive</button>
      <button class="btn btn-cyan full" id="mbp-open">📂 Open a saved project…</button>
      <button class="btn btn-cyan full" id="mbp-name"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>Rename this project</button>
      <button class="btn btn-cyan full" id="mbp-new">🆕 Start a new project</button>
      <button class="btn btn-danger full" id="mbp-clear"><svg class="ic b-ic" aria-hidden="true"><use href="#i-broom"></use></svg>Remove every layer</button>`, root=>{
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

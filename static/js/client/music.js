/* Music — the library screen (the track list and its paging, playlists bar, add-to-playlist,
 * the now-playing strip) drawn over the encrypted Music folder. Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_musicDeps`) and
 * builds this factory the first time the Music screen, or the Music folder in Files, is drawn. The
 * code below is BYTE-IDENTICAL to what it replaced in app.js apart from its reads of app.js's live
 * `let` bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets. `_updateMusicListBtns` (the player calls it on every track change) repaints a
 * list only this module draws, so it does nothing before the module exists.
 *
 * Stayed in app.js: the PLAYER itself (MusicPlayer, MusicOffline, the track URLs) — it is armed at
 * startup for the lock screen, headset and car controls, and must answer them with this screen never
 * opened — and the playlist state other screens read.
 */
window.PCMusicFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.GUEST, S.ME, S.VIEW, S._audioEl, S._blobHave, S._musicPl, S._musicPlRepaint, S._musicQ, S._musicSwept
  const {
    $, $$, FilesIdx, MusicOffline, MusicPlayer, PL, _clearNav, _feedScrollable, _hidePill,
    _musicExt, _musicRefreshedSet, _musicShareCurrent, _musicShareLoad, _musicShareView, _plTracks,
    _refreshBlobHave, _trackMeta, closeModal, delBlob, deleteBlobQuiet, enc, modal, musicEntries,
    musicTracks, openMusicFolder, renameBlob, saveEncrypted, toast, uiConfirm, uiPrompt,
  } = dep;

  /* The Music APP: a player, not a folder. Opening Music used to land you in Files → 🎵 Music — the
   * same screen you use to UPLOAD, complete with folder chips and a drop zone — which is a file
   * manager that happens to contain songs. This is the library as a playlist with transport on top;
   * uploading is one button away rather than the thing you arrive at.
   * It reuses _renderMusicList for the rows and the floating MusicPlayer for playback, so there is
   * one library, one queue, and one set of controls that cannot drift out of step. */
  function renderMusicApp(){
    const feed=$('#feed'); if(!feed) return;
    /* CLAIM THE VIEW. #feed is one element shared by every screen, and the live timeline appends to
     * it whenever VIEW is 'home' or 'global' — so rendering a player into it without changing VIEW
     * meant incoming social posts wrote straight over the library. ("new social posts take over
     * Music".) Every other non-timeline screen does exactly this; leaving VIEW alone was the bug. */
    S.VIEW='music'; _hidePill(); _clearNav();
    { const t=$('#view-title'); if(t) t.textContent='Music'; }
    _feedScrollable(feed);
    feed.innerHTML=`<div class="music-app">
      <div class="ma-now">
        <div class="ma-art"><svg class="ic" aria-hidden="true"><use href="#i-music"></use></svg></div>
        <div class="ma-meta"><b id="ma-title">Nothing playing</b><span class="muted small" id="ma-sub"></span></div>
        <div class="ma-ctl">
          <button class="btn btn-ghost small" id="ma-shuffle" title="Shuffle" aria-label="Shuffle"><svg class="ic b-ic" aria-hidden="true"><use href="#i-shuffle"></use></svg></button>
          <button class="btn btn-ghost small" id="ma-prev" title="Previous"><svg class="ic b-ic" aria-hidden="true"><use href="#i-prev"></use></svg></button>
          <button class="btn btn-neon" id="ma-play" title="Play / pause"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg></button>
          <button class="btn btn-ghost small" id="ma-next" title="Next"><svg class="ic b-ic" aria-hidden="true"><use href="#i-next"></use></svg></button>
        </div>
        <canvas class="ma-viz" id="ma-viz" aria-label="Audio spectrum"></canvas>
        <!-- The scrubber gets its OWN row rather than sharing the control line, so it can be the
             full width of the panel at every size. On a phone .ma-now wraps to two or three lines,
             and a bar sharing a row with four buttons is left a stub too short to aim with — the
             one thing a seek bar cannot afford to be. -->
        <div class="ma-seek-row">
          <span class="ma-t" id="ma-cur">0:00</span>
          <div class="mp-seek ma-seek" id="ma-seek" role="slider" tabindex="0"
               aria-label="Seek" aria-valuemin="0" aria-valuemax="100" aria-valuetext="not playing"><div class="mp-seek-fill"></div></div>
          <span class="ma-t" id="ma-dur">0:00</span>
        </div>
        <div class="ma-tools">
          <button class="btn btn-cyan small" id="ma-add" title="Add music"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Add music</button>
          <button class="btn btn-ghost small" id="ma-eq-toggle" aria-expanded="false" aria-controls="ma-eq"><span aria-hidden="true">≋</span> Equalizer</button>
        </div>
        <div class="ma-eq hidden" id="ma-eq">
          <div class="ma-eq-head"><b>Equalizer</b><span class="muted small">Saved on this device</span></div>
          <div class="ma-eq-presets" role="group" aria-label="Equalizer presets">
            <button class="ma-eq-preset" data-eq="flat">Flat</button><button class="ma-eq-preset" data-eq="bass">Bass</button><button class="ma-eq-preset" data-eq="vocal">Vocal</button><button class="ma-eq-preset" data-eq="bright">Bright</button>
          </div>
          <div class="ma-eq-bands">
            <label><span>Bass <output id="ma-eq-low-v">0 dB</output></span><input id="ma-eq-low" data-band="low" type="range" min="-12" max="12" step="1" value="0"></label>
            <label><span>Voice <output id="ma-eq-mid-v">0 dB</output></span><input id="ma-eq-mid" data-band="mid" type="range" min="-12" max="12" step="1" value="0"></label>
            <label><span>Treble <output id="ma-eq-high-v">0 dB</output></span><input id="ma-eq-high" data-band="high" type="range" min="-12" max="12" step="1" value="0"></label>
          </div>
        </div>
      </div>
      <div id="ma-plbar"></div>
      <input class="input ma-q" id="ma-q" type="search" autocomplete="off"
             placeholder="Search your library" aria-label="Search your library">
      <div class="music-list" id="ma-lib"><div class="spinner"></div></div></div>`;
    const lib=$('#ma-lib',feed);
    /* HYDRATE THE INDEX FIRST. The library lives in the files index, and this window can be the very
     * first thing opened in a session — openMusic() called loadLocal() before doing anything for
     * exactly that reason, and dropping it made a full library render as "No music yet". loadLocal
     * is synchronous (localStorage) so the first paint is already right on a returning device; pull()
     * is the network copy, and the repaint below is for a fresh one. */
    try{ FilesIdx.loadLocal(); }catch(_){}
    /* Paint from what we already know and DO NOT re-reconcile here. musicTracks() hides any track
     * the server's blob list does not mention, and forcing a fresh /list on entry emptied a library
     * that the Files → Music screen was listing perfectly well — the two fetches do not always agree,
     * and an empty player is a far worse answer than a track that turns out not to play (which says
     * so, out loud, via play()'s toast). */
    /* One repaint for both halves: the playlist bar decides WHICH tracks the list is drawing, so a
     * change to either has to redraw both or the chips and the rows disagree about what you picked. */
    const paint=()=>{
      const bar=$('#ma-plbar',feed);
      if(bar){ bar.innerHTML=_plBarHTML(); _bindPlBar(bar, paint); }
      // A playlist that has been deleted (or has not loaded yet) falls back to the whole library
      // rather than drawing an empty screen with no way to explain itself.
      // Shared with me / by me are musicshare.js views; the library search steps aside.
      if(_musicShareView()){
        const qi=$('#ma-q',feed); if(qi) qi.classList.add('hidden');
        PCMusicShare.renderView(S._musicPl, lib, {
          libraryChanged: () => {
            try{ _musicAppNow(); if(window.PCOS && PCOS.musicChanged) PCOS.musicChanged(); }catch(_){} },
          // Accepting a share selects it, so the answer lands on the playlist itself rather than on
          // a chip that may be scrolled off the end of the bar.
          open: key => { S._musicPl = key; paint(); } });
        _musicAppNow();
        return;
      }
      { const qi=$('#ma-q',feed); if(qi) qi.classList.remove('hidden'); }
      const sel = S._musicPl && window.PCPlaylists && PCPlaylists.get(S._musicPl);
      if(S._musicPl && !sel) S._musicPl = null;
      _renderMusicList(lib, null, S._musicQ, sel ? _plTracks(S._musicPl) : null);
      _musicAppNow();
    };
    S._musicPlRepaint = () => { if(document.getElementById('ma-lib')) paint(); };
    paint();
    // The library is encrypted per-user, so it can only be read once there is a signer. Repaint when
    // it lands; onChange covers an edit made on another device arriving over the live subscription.
    // Who shared music with me, behind the first paint, so the chip can say so.
    if(S.ME && !S.GUEST){
      _musicShareLoad().then(MS=>MS&&MS.loadIn()).then(()=>{ if(document.getElementById('ma-lib') && !_musicShareView()){
        const bar=$('#ma-plbar',feed); if(bar){ bar.innerHTML=_plBarHTML(); _bindPlBar(bar, paint); } } }).catch(()=>{});
    }
    if(window.PCPlaylists){
      PCPlaylists.load().then(()=>{ if(document.getElementById('ma-lib')) paint(); }).catch(()=>{});
      PCPlaylists.onChange(()=>{ if(document.getElementById('ma-lib')) paint(); });
    }
    // Safe to reconcile now: a track the server no longer has is MARKED, not hidden, so this can
    // annotate the library but never empty it. A failed fetch leaves _blobHave null and marks nothing.
    _refreshBlobHave().then(paint).catch(()=>{});
    /* …and once the index is trustworthy, clear out anything cached for a track that no longer
     * exists. Once per session, after a pull, never on the first paint from a cold cache. */
    // One signed drive read, shared by repaint and offline-cache cleanup.  Starting pull() in both
    // branches used to issue two /files-index requests (and decrypt the same index twice) because
    // _pullDone cannot become true until the first request finishes.
    const indexReady = FilesIdx.ensure();
    if(!S._musicSwept){
      S._musicSwept = true;
      indexReady.then(ok=>ok ? MusicOffline.sweep() : 0)
        .then(n=>{ if(n && document.getElementById('ma-lib')) paint(); })
        .catch(()=>{});
    }
    // …and once the index arrives from the relay, repaint. Never HIDES anything that was already
    // showing — paint() is a full re-render from a strictly better-informed index.
    indexReady.then(ok=>{ if(ok && document.getElementById('ma-lib')) paint(); }).catch(()=>{});
    const b=(sel,fn)=>{ const el=$(sel,feed); if(el) el.onclick=fn; };
    b('#ma-prev',()=>MusicPlayer.prev());
    b('#ma-next',()=>MusicPlayer.next());
    b('#ma-shuffle',()=>{ MusicPlayer.shuffle=!MusicPlayer.shuffle; MusicPlayer._render(); _musicAppNow(); });
    b('#ma-play',()=>{ if(MusicPlayer.cur) return MusicPlayer.toggle();
      const q=musicTracks(null); if(!q.length){ toast('no music yet — add some'); return; }
      MusicPlayer.refreshQueue(); MusicPlayer.play(q[0].sha); });
    b('#ma-add',()=>openMusicFolder());
    b('#ma-eq-toggle',()=>{
      const panel=$('#ma-eq',feed), toggle=$('#ma-eq-toggle',feed); if(!panel||!toggle) return;
      const open=panel.classList.toggle('hidden')===false;
      toggle.setAttribute('aria-expanded', open?'true':'false'); toggle.classList.toggle('on',open);
    });
    MusicPlayer.bindEqualizer(feed);
    /* The "⤒ Originals" one-pass repair is GONE. It existed for libraries uploaded while the Opus
     * transcode still ran: point it at your files and each one replaced its transcode in place,
     * playlists and all. Uploads have passed through untouched for a while now, so it only ever
     * applied to old libraries — and re-adding the files is the same repair with nothing that
     * deletes a blob. Its helpers (_musicBySrcName / _musicReplaceOriginals) and
     * PCPlaylists.replaceTrack went with it rather than being left as dead code. */
    // The same scrubber the floating widget uses — one implementation, so the two cannot drift.
    MusicPlayer.bindSeek($('#ma-seek',feed));
    MusicPlayer._tickApp();   // paint the position immediately: entering mid-track must not read 0:00
    // The app and floating widget share the analyser/audio graph. Opening Music in the middle of a
    // song must move the live spectrum here without creating a second AudioContext or restarting it.
    if(S._audioEl && !S._audioEl.paused) MusicPlayer._startViz();
    { const qi=$('#ma-q',feed);
      if(qi){ qi.value=S._musicQ;
        // Re-render the LIST only. Repainting the whole app would take the caret out of this box.
        qi.oninput=()=>{ S._musicQ=qi.value;
          if(_musicShareView()) return;
          const sel = S._musicPl && window.PCPlaylists && PCPlaylists.get(S._musicPl);
          _renderMusicList(lib, null, S._musicQ, sel ? _plTracks(S._musicPl) : null); _musicAppNow(); }; } }
    MusicPlayer.onChange=_musicAppNow;   // the floating player is the single source of truth
  }
  function _plBarHTML(){
    if(!PL()) return '';
    const ls = PL().all();
    const chip = (id,label,n)=>`<button class="ma-pl${S._musicPl===id?' on':''}" data-pl="${enc(id)}">${enc(label)}${n!=null?` <span class="ma-pln">${n}</span>`:''}</button>`;
    const MS = window.PCMusicShare, real = S._musicPl && !_musicShareView();
    return `<div class="ma-pls">
        ${chip('', '🎵 All music', null)}
        ${ls.map(p=>chip(p.id, p.name, p.tracks.length)).join('')}
        <button class="ma-pl ma-plnew" id="ma-plnew" title="New playlist">＋ New</button>
        ${MS ? MS.barHTML(S._musicPl, real) : ''}
        ${real ? `<span class="ma-plsp"></span>
          <button class="ma-pl" id="ma-plren" title="Rename this playlist">✎</button>
          <button class="ma-pl" id="ma-pldel" title="Delete this playlist">🗑</button>` : ''}
      </div>`;
  }
  function _bindPlBar(root, repaint){
    $$('.ma-pl[data-pl]', root).forEach(b=> b.onclick=()=>{
      const to = b.dataset.pl || null;
      /* Leaving a playlist hands the queue BACK. Playing inside one replaces MusicPlayer.queue with
       * that playlist; without this the queue stayed pinned to it, so ⏭ walked the playlist while
       * the whole library was on screen. Only when nothing is playing from the old view — pressing
       * a chip is navigation, and navigation must never interrupt the music. */
      const leaving = !to && S._musicPl && !(S._audioEl && !S._audioEl.paused);
      S._musicPl = to;
      MusicPlayer._pl = _musicShareView() ? null : to;   // two pickers, one selection (shares aren't playlists)
      // AFTER the selection moves: refreshQueue reads it, and before this it rebuilt from the playlist
      // being left.
      if(leaving) MusicPlayer.refreshQueue();
      repaint(); });
    { const sb=$('#ma-plshare', root); if(sb) sb.onclick=()=>_musicShareCurrent(); }
    { const nb=$('#ma-plnew', root); if(nb) nb.onclick=async()=>{
        const name = await uiPrompt('Name this playlist', { value: '', placeholder: 'New playlist' }); if(!name) return;
        const pl = await PL().create(name.trim());
        // create() returns null when the save did not happen — never navigate into one that is not
        // there, which is the whole reason it reports that rather than handing the object back.
        if(!pl){ toast('couldn’t save that playlist'); return; }
        S._musicPl = pl.id; repaint(); }; }
    { const rb=$('#ma-plren', root); if(rb) rb.onclick=async()=>{
        const cur = PL().get(S._musicPl); if(!cur) return;
        const name = await uiPrompt('Rename playlist', { value: cur.name, ok: 'Rename' }); if(!name) return;
        if(!await PL().rename(S._musicPl, name.trim())) toast('couldn’t rename that playlist');
        repaint(); }; }
    { const db=$('#ma-pldel', root); if(db) db.onclick=async()=>{
        const cur = PL().get(S._musicPl); if(!cur) return;
        // The TRACKS are not touched — a playlist is an order over the library, and deleting one has
        // never meant deleting music. Said out loud, because "delete" next to a list of songs reads
        // like it might.
        if(!await uiConfirm(`Delete “${cur.name}”? The songs stay in your library.`)) return;
        await PL().remove(S._musicPl); S._musicPl = null; repaint(); }; }
  }
  /* Add a track to a playlist — or to a new one. One prompt, because a chooser that cannot also
   * create is a dead end the first time anybody uses it. */
  async function _addToPlaylist(sha){
    if(!PL()){ toast('playlists are still loading'); return; }
    await PL().load();
    const ls = PL().all();
    const on = new Set(PL().playlistsWith(sha).map(p=>p.id));
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-music"></use></svg>Add to a playlist</h3>
      <div class="ma-plpick">${ls.length ? ls.map(p=>
        `<button class="btn ${on.has(p.id)?'btn-ghost':'btn-cyan'} full ma-plpickb" data-id="${enc(p.id)}" ${on.has(p.id)?'disabled':''}>
           ${enc(p.name)} <span class="muted small">${on.has(p.id)?'· already in it':`· ${p.tracks.length} track${p.tracks.length===1?'':'s'}`}</span>
         </button>`).join('') : '<div class="muted small">No playlists yet.</div>'}</div>
      <div class="row" style="justify-content:space-between;margin-top:12px">
        <button class="btn btn-neon small" id="ma-pladdnew">＋ New playlist</button>
        <button class="btn btn-ghost small" id="ma-pladdclose">Close</button>
      </div>`, root => {
      $('#ma-pladdclose', root).onclick = closeModal;
      $$('.ma-plpickb', root).forEach(b=> b.onclick=async()=>{
        b.disabled = true;
        const n = await PL().add(b.dataset.id, sha);
        closeModal();
        toast(n ? 'added to the playlist' : 'couldn’t add that track');
        S._musicPlRepaint();
      });
      $('#ma-pladdnew', root).onclick = async()=>{
        const name = await uiPrompt('Name this playlist', { value: '', placeholder: 'New playlist' }); if(!name) return;
        const pl = await PL().create(name.trim(), [sha]);
        closeModal();
        toast(pl ? 'playlist created' : 'couldn’t save that playlist');
        S._musicPlRepaint();
      };
    });
  }
  function _musicAppNow(){
    const t=document.getElementById('ma-title'); if(!t) return;
    const m=MusicPlayer.cur?_trackMeta(MusicPlayer.cur):null;
    t.textContent=(m&&m.name)||'Nothing playing';
    const sub=document.getElementById('ma-sub');
    const playing=S._audioEl && !S._audioEl.paused;
    if(sub) sub.textContent=MusicPlayer.cur ? (playing?'playing':'paused')+(MusicPlayer.shuffle?' · shuffle':'') : '';
    const sh=document.getElementById('ma-shuffle'); if(sh){ sh.classList.toggle('on', !!MusicPlayer.shuffle); sh.setAttribute('aria-pressed', MusicPlayer.shuffle?'true':'false'); }
    /* THE BUTTON HAS TO SAY WHICH IT IS. #ma-play was rendered once with a fixed ▶ and nothing ever
     * changed it, so the Music app's main control showed "play" while a track was playing — the one
     * piece of state a transport exists to report. (The floating widget rebuilds its whole innerHTML
     * on every _render and so has always been right; this view updates in place, which is why it
     * needs saying explicitly.) */
    { const pb=document.getElementById('ma-play');
      if(pb){ const u=pb.querySelector('use');
        if(u) u.setAttribute('href', playing?'#i-pause':'#i-play');
        pb.title = playing?'Pause':'Play';
        pb.setAttribute('aria-label', pb.title); } }
    try{ _updateMusicListBtns(); }catch(_){}
  }
  /* `only` — a restricted, ORDERED set of entries (a playlist). Passed rather than filtered inside,
   * because a playlist's order is its content: sorting it by date the way the library is sorted
   * would silently throw away the one thing the user arranged. */
  function _renderMusicList(grid, list, q, only){
    if(!grid) return;
    /* Which tracks are already on this device — read ONCE, then from memory.
     *
     * The list repaints on every keystroke of the search box, so this cannot be an IDB scan per
     * paint. The first paint of a session may not know yet; it draws without the marks and repaints
     * itself when the answer arrives, which is a flicker of a badge rather than a blocked render. */
    if(!MusicOffline._have){
      MusicOffline.have().then(()=>{ if(grid.isConnected) _renderMusicList(grid, list, q, only); }).catch(()=>{});
    }
    const all = only || musicEntries(list);
    const needle=String(q||'').trim().toLowerCase();
    const tracks=needle ? all.filter(t=>String(t.m.name||'').toLowerCase().includes(needle)) : all;
    const gone=tracks.filter(t=>t.missing).length;
    const live=tracks.length-gone;
    // Shuffle plays the whole LIBRARY, so it is gated on the library — not on whatever the current
    // search happens to match, which would disable it while you typed.
    const liveAll=all.filter(t=>!t.missing).length;
    grid.className='music-list';
    /* A header, so this reads as a music app rather than a folder that happens to hold songs. The
     * desktop opens this view as the Music WINDOW, and without one obvious action a full library
     * looked as inert as an empty one — "it loads the Music folder and nothing happens". */
    // How much of the library is on THIS device. Primed asynchronously the first time and then read
    // from memory, because this list re-renders on every keystroke of the search box.
    const offAll=all.filter(t=>t.offline).length;
    const wantable=all.filter(t=>!t.missing && !t.offline).map(t=>t.sha);
    const head = `<div class="music-head">
      <div class="music-head-primary">
        <button class="btn btn-neon small" id="mus-shuffle"${liveAll?'':' disabled'}>
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-shuffle"></use></svg>Shuffle</button>
        <button class="btn btn-ghost small" id="mus-refresh" title="Fetch the library again — songs added on another device appear here">
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg>Refresh</button>
        ${!only ? `<button class="btn btn-ghost small" id="mus-delall"${tracks.length?'':' disabled'}
          title="${needle ? 'Delete the songs matching this search — from your library, not just this view' : 'Delete every song in your library'}">${needle ? `Delete ${tracks.length} match${tracks.length===1?'':'es'}` : 'Delete All'}</button>` : ''}
      </div>
      <span class="music-count muted small" aria-live="polite">${needle
          ? `${tracks.length} of ${all.length} track${all.length===1?'':'s'}`
          : (tracks.length + ' track' + (tracks.length===1?'':'s'))}${
          gone ? ` · ${gone} missing from the server` : ''}${
          offAll ? ` · ${offAll} offline` : ''}</span>
      <div class="music-head-secondary">
        ${wantable.length ? `<button class="btn btn-ghost small" id="mus-getall" title="Keep every track on this device — they play with no network">
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Download ${wantable.length}</button>` : ''}
        ${gone ? `<button class="btn btn-ghost small" id="mus-tidy">Remove ${gone} missing</button>` : ''}
      </div></div>`;
    /* THE WHOLE LIBRARY USED TO BE ON SCREEN AT ONCE, and a big one made the entire app sluggish for
     * as long as the window was open — with nothing playing. Reported as "the music player on the
     * desktop slows everything down, window movement included; it's fast again once Music is closed",
     * in both Firefox and the packaged Windows app, which is what ruled out the visualiser and the
     * player's own animations (both are gated on actually playing).
     *
     * A track row is seven elements, four of them buttons carrying an inline <svg><use>, and each
     * <use> instantiates a shadow tree. At the 2422-track library the tidy-up code above was written
     * for that is roughly 17,000 nodes, permanently, in a document every style recalculation and
     * every layout of the whole page has to walk — which is why dragging an unrelated WINDOW got
     * slow. Nothing here was looping; the cost was the document itself.
     *
     * So one page is rendered and the rest waits behind a button. The rows are built for the whole
     * filtered set (they are strings — cheap to hold, and the search still filters everything, not
     * just what is drawn), and _musMore appends the next slice. */
    const rows = tracks.map(t=>`<div class="track${t.missing?' gone':''}" data-sha="${t.sha}">
        ${t.missing ? '<span class="track-play" aria-hidden="true">✕</span>'
                    : `<button class="track-play" data-sha="${t.sha}" aria-label="Play"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg></button>`}
        <span class="track-name">${enc(t.m.name||'track')}</span>
        <span class="track-meta">${t.missing ? 'not on the server — delete to tidy up'
                                             : '🔒 ' + (((t.m.size||0)/1048576)).toFixed(1) + 'MB'
                                               + (t.offline ? ' · offline' : '')}</span>
        ${t.missing ? '' : `<button class="track-keep${t.offline?' on':''}" data-sha="${t.sha}" title="${t.offline?'Kept on this device — tap to remove the offline copy':'Keep on this device (plays with no network)'}" aria-label="${t.offline?'Remove offline copy':'Keep offline'}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-${t.offline?'check':'download'}"></use></svg></button>`}
        ${t.missing ? '' : `<button class="track-add" data-sha="${t.sha}" title="Add to a playlist" aria-label="Add to a playlist"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg></button>`}
        ${t.missing ? '' : `<button class="track-dl" data-sha="${t.sha}" data-name="${enc((t.m.name||'track')+'.'+_musicExt(t.m))}" title="Save a copy to your files (decrypts first)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-share"></use></svg></button>`}
        ${t.missing ? '' : `<button class="track-ren" data-sha="${t.sha}" data-name="${enc(t.m.name||'')}" title="Rename" aria-label="Rename"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button>`}
        <button class="track-del" data-sha="${t.sha}" title="Delete"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>
      </div>`);
    const first = rows.slice(0, MUS_PAGE);
    grid._musRest = rows.slice(MUS_PAGE);
    grid.innerHTML = head + (rows.length
      ? first.join('') + (grid._musRest.length
          ? `<button class="mus-more">Show ${Math.min(MUS_PAGE, grid._musRest.length)} more (${grid._musRest.length} left)</button>`
          : '')
      : (needle ? `<div class="empty">Nothing in your library matches “${enc(needle)}”.</div>`
                : '<div class="empty">No music yet — drop audio files above. They\'re Opus-compressed + encrypted automatically, and only you can play them.</div>'));
    /* Clear out entries whose bytes the SERVER says it does not have. Only offered once the blob
     * list has actually been read (_blobHave non-null) — "we have not looked yet" must never be
     * mistaken for "it is gone", or this would delete a working library on a failed fetch. It only
     * drops INDEX entries; there is nothing on the server left to delete. */
    /* DELETE THE LIBRARY, OR WHATEVER THE SEARCH IS SHOWING.
     *
     * There was no way to do this at all: the Files grid hides encrypted music on purpose (it is
     * ciphertext with no useful name), and this screen only ever deleted ONE track at a time — which
     * for a few hundred songs is not a feature, it is a dare. Reported as exactly that.
     *
     * It deletes what is ON SCREEN, so a search narrows it and the button says so. The confirmation
     * names the count and does not pretend to be reversible: the bytes go from the server.
     *
     * The index save is BATCHED and checkpointed every 25, because forget() otherwise re-uploads the
     * whole encrypted index per track — the same reason the bulk import batches — and a crash
     * halfway then keeps the progress instead of losing it. The verdict comes from endBatch, not
     * from having asked: a tidy that never reached the server once reported success and the entries
     * were all back on the next load, twice, for 2422 tracks. */
    /* NOT INSIDE A PLAYLIST. There, the same button reads as "empty this playlist" and would delete
     * the songs themselves — beside a Delete-playlist control that deliberately does not. The
     * comment on that one already says why: "delete" next to a list of songs reads like it might. */
    { const da=$('#mus-delall',grid);
      if(da) da.onclick=async ()=>{
        const doomed = tracks.map(t=>t.sha);
        if(!doomed.length) return;
        const what = needle ? `the ${doomed.length} song${doomed.length>1?'s':''} matching “${needle}”`
                            : `all ${doomed.length} song${doomed.length>1?'s':''} in your library`;
        if(!await uiConfirm(`Delete ${what}?\n\nThe files are removed from the server. This cannot be `
                          + `undone, and any copy kept on this device goes too.`)) return;
        da.disabled = true;
        const was = da.textContent;
        FilesIdx.beginBatch();
        let done = 0, failed = 0;
        for(const sha of doomed){
          if(!await deleteBlobQuiet(sha)) failed++;
          if(++done % 25 === 0){
            await FilesIdx.endBatch(); FilesIdx.beginBatch();
            da.textContent = `Deleting ${done}/${doomed.length}…`;
          }
        }
        const saved = await FilesIdx.endBatch();
        // A playlist that still names a deleted track draws as a gap, and "delete my music" that
        // leaves the names behind is not what anybody meant. One save per playlist that changes.
        try{ if(PL()) await PL().pruneTracks(doomed); }catch(_){}
        da.disabled = false; da.textContent = was;
        /* THE BYTES ARE ALREADY GONE BY HERE, so "unchanged" would be the opposite of the truth.
         *
         * The loop above sent a Blossom DELETE for every track; `saved` is only about whether the
         * LIBRARY RECORD was written back. A failed save leaves entries pointing at bytes that no
         * longer exist — the songs are gone and the list still shows them — and telling somebody
         * their library is untouched at that moment is how they find out days later. */
        toast(failed ? `deleted ${doomed.length - failed}, ${failed} could not be removed`
                     : !saved ? `deleted ${doomed.length}, but the library list could not be saved `
                                + `— it will fix itself on the next save; do not reload yet`
                              : `deleted ${doomed.length} song${doomed.length>1?'s':''}`);
        try{ await FilesIdx.pull(); }catch(_){}
        _renderMusicList(grid, list, q, only);
        try{ _musicAppNow(); }catch(_){}
        try{ if(window.PCOS && PCOS.musicChanged) PCOS.musicChanged(); }catch(_){}
      }; }
    { const td=$('#mus-tidy',grid);
      if(td) td.onclick=async ()=>{
        const dead=musicEntries(list).filter(t=>t.missing).map(t=>t.sha);
        if(!dead.length || !S._blobHave) return;
        if(!await uiConfirm(`Remove ${dead.length} track${dead.length>1?'s':''} from your library? `
                          + `The files are already gone from the server — this only clears the leftover `
                          + `entries. Anything still playable is untouched.`)) return;
        FilesIdx.beginBatch();
        dead.forEach(sha=>{ try{ FilesIdx.forget(sha); }catch(_){} });
        /* Report what the SERVER did, not what we asked it to do. This said "removed N" no matter
         * what, because _save() swallowed every failure — so a tidy that never reached the server
         * looked done, and the entries were all back on the next load. Twice, over two days, for
         * 2422 tracks. _save's own toast gives the reason; this one is only the verdict. */
        const saved = await FilesIdx.endBatch();
        toast(saved ? `removed ${dead.length} missing track${dead.length>1?'s':''}`
                    : `not saved — your library on the server is unchanged`);
        _renderMusicList(grid, list, q, only);
        try{ _musicAppNow(); }catch(_){}
      }; }
    { const sh=$('#mus-shuffle',grid);
      /* Shuffle what is ON SCREEN. With a playlist selected this used to reach past it to the whole
       * library — `musicTracks(null)` + refreshQueue(), both of which ignore the current view — so
       * picking a playlist and pressing shuffle played something that was not in it. Whatever `only`
       * is, that is the set the user is looking at and the set they meant. */
      if(sh) sh.onclick=()=>{
        const q = (only && only.length) ? only.filter(t=>!t.missing) : musicTracks(null);
        if(!q.length) return;
        MusicPlayer.shuffle=true;
        if(only && only.length) MusicPlayer.queue = q.map(t=>t.sha);
        else MusicPlayer.refreshQueue();
        // force: the random pick can be the track already playing, and without it that PAUSES.
        MusicPlayer.play(MusicPlayer.queue[Math.floor(Math.random()*MusicPlayer.queue.length)], {force:true});
        sh.classList.add('on'); }; }
    /* Pressing play inside a playlist makes the PLAYLIST the queue, in its order — otherwise
     * ⏭ walks the whole library from wherever that track happens to sit in it, which is not what
     * "play this playlist" means anywhere else. */
    /* ONE listener for every row, however many rows there are.
     *
     * These were bound per button — four `$$('.track-*').forEach(b => b.onclick = …)` passes over the
     * whole library on every render, and the library re-renders on every keystroke of the search box.
     * At the 2422 tracks the tidy-up code above was written for that is ~10,000 closures created and
     * attached per paint, on top of the rows themselves. Delegation is also what lets the list be
     * PAGED below: appended rows work with nothing to re-wire.
     *
     * Rebound (not accumulated) on each render: `grid.onclick` is a single slot, so re-rendering
     * cannot leave a second handler behind the way addEventListener would. */
    grid.onclick = async (ev) => {
      const b = ev.target && ev.target.closest && ev.target.closest(
        '.track-play,.track-keep,.track-add,.track-dl,.track-ren,.track-del,.mus-more');
      if(!b || !grid.contains(b)) return;
      if(b.classList.contains('mus-more')){ _musMore(grid, b); return; }
      const sha = b.dataset.sha;
      if(b.classList.contains('track-play')){
        /* Pressing play inside a playlist makes the PLAYLIST the queue, in its order — otherwise
         * ⏭ walks the whole library from wherever that track happens to sit in it, which is not what
         * "play this playlist" means anywhere else. */
        /* The playlist becomes the queue; shuffle stays whatever the person set. It used to be
         * switched OFF here, silently, so turning shuffle on and then tapping a song in a playlist
         * played the rest in order while the button still looked like it did something. */
        if(only && only.length){ MusicPlayer.queue = only.filter(t=>!t.missing).map(t=>t.sha); }
        MusicPlayer.play(sha); return;
      }
      if(b.classList.contains('track-dl')){ saveEncrypted(sha, b.dataset.name); return; }
      if(b.classList.contains('track-add')){ _addToPlaylist(sha); return; }
      if(b.classList.contains('track-ren')){ renameBlob(sha, b.dataset.name); return; }
      if(b.classList.contains('track-del')){ delBlob(sha); return; }
      /* KEEP ON THIS DEVICE. Toggling one track, and the whole library at once.
       *
       * The button reports per track as it lands rather than after the lot: downloading a few hundred
       * songs is minutes of work, and a control that says nothing for minutes is one nobody trusts —
       * they press it again, or decide it is broken. Removing is instant and never touches the server,
       * so it is safe to undo. */
      if(b.classList.contains('track-keep')){
        const kept=(await MusicOffline.have()).has(sha);
        b.disabled=true;
        if(kept){ await MusicOffline.drop(sha); toast('offline copy removed'); }
        else {
          b.classList.add('working');
          const r=await MusicOffline.keep([sha]);
          toast(r.ok ? 'kept on this device' : 'could not download that track');
        }
        b.disabled=false;
        _renderMusicList(grid, list, q, only);
      }
    };
    /* KEEP ON THIS DEVICE. Toggling one track, and the whole library at once.
     *
     * The button reports per track as it lands rather than after the lot: downloading a few hundred
     * songs is minutes of work, and a control that says nothing for minutes is one nobody trusts —
     * they press it again, or decide it is broken. Removing is instant and never touches the server,
     * so it is safe to undo. */
    /* GO AND LOOK AGAIN.
     *
     * The library is pulled from the relay ONCE per session (`if(!FilesIdx._pullDone)` in
     * renderMusicApp), which is right for a device working alone and wrong the moment two are in
     * play: upload a folder on a laptop and the phone shows the library it read when it started,
     * with no way to reach the new songs — not to play them and not to download them. Reported
     * exactly that way, mid-upload.
     *
     * Manual, not automatic on entry: re-reconciling on every visit is what once emptied a library
     * that was listing perfectly well (see paint() above), and a button that goes and looks when you
     * ask is both cheaper and easier to trust than a poll. It clears _pullDone deliberately — a
     * refresh that returns the copy it already has is not a refresh. */
    { const rf=$('#mus-refresh',grid);
      if(rf) rf.onclick=async ()=>{
        const label=rf.innerHTML; rf.disabled=true; rf.textContent='refreshing…';
        try{
          FilesIdx._pullDone=false;
          await FilesIdx.pull();
          await _refreshBlobHave();
          await MusicOffline.have(true);   // …and re-read what is downloaded, on THIS device
        }catch(_){ }
        rf.disabled=false; rf.innerHTML=label;
        _renderMusicList(grid, null, q, _musicRefreshedSet(only));
        try{ _musicAppNow(); }catch(_){}
      }; }
    { const ga=$('#mus-getall',grid);
      if(ga) ga.onclick=async ()=>{
        // `all`, not the library: inside a playlist the button is labelled from the playlist, and a
      // button that caches something other than the number printed on it is a trap.
      const want=all.filter(t=>!t.missing && !t.offline).map(t=>t.sha);
        if(!want.length) return;
        ga.disabled=true; const label=ga.innerHTML;
        const r=await MusicOffline.keep(want, s=>{ ga.textContent=`${s.done} / ${s.total}…`; });
        ga.disabled=false; ga.innerHTML=label;
        toast(r.ok===r.total ? `${r.ok} track${r.ok===1?'':'s'} kept on this device`
                             : `kept ${r.ok} of ${r.total} — the rest failed to download`);
        _renderMusicList(grid, list, q, only);
      }; }
    // A track is stored as Opus ciphertext, so "download" means decrypt-then-save — same path the
    // file grid's lock cards use. Without this the only way out of the Music folder was the player.
    _updateMusicListBtns(grid);
  }
  /* Reveal the next page of rows by APPENDING them.
   *
   * Not by re-rendering with a bigger slice: that rebuilds every row already on screen, so reaching
   * the end of a large library would cost the same quadratic pile of DOM work this paging exists to
   * avoid. The rows the button needs are stashed on the grid by _renderMusicList; they are strings,
   * not nodes, so holding them costs nothing until they are asked for. */
  const MUS_PAGE = 120;
  function _musMore(grid, btn){
    const rest = grid._musRest || [];
    const next = rest.splice(0, MUS_PAGE);
    if(!next.length){ btn.remove(); return; }
    btn.insertAdjacentHTML('beforebegin', next.join(''));
    if(!rest.length) btn.remove();
    else btn.textContent = `Show ${Math.min(MUS_PAGE, rest.length)} more (${rest.length} left)`;
    _updateMusicListBtns(grid);
  }
  /* Mark the row that is playing.
   *
   * Scoped to the LIST, not the document: this ran `$$('.track-play')` with no root on every render
   * and on every play/pause, which walks the entire page — the same "cost proportional to the whole
   * document" that made a big library slow everything down.
   *
   * And it sets a CLASS rather than textContent. The button contains an inline <svg>, so assigning
   * text to it deleted the icon and replaced it with a bare ▶/⏸ glyph — every row the pointer had
   * ever touched lost its icon permanently, which is why the list looked like two different designs
   * once you had played something. The stylesheet swaps the glyph. */
  function _updateMusicListBtns(root){
    /* EVERY list, when no particular one is named.
     *
     * There can be two on screen at once — `#ma-lib` in the Music app and `#bl-grid` when Files is
     * showing the Music folder — and in desktop mode both can be open in their own windows. The
     * un-rooted callers (MusicPlayer._render, _musicAppNow) mean "wherever this is drawn", so a
     * `querySelector` fallback picks whichever comes first in the document and leaves the other one
     * stuck: its rows never gain the marker, and a row that was playing keeps it forever. */
    const scopes = root ? [root] : $$('.music-list');
    const playing = S._audioEl && !S._audioEl.paused;
    for(const scope of scopes){
      if(!scope) continue;
      $$('.track-play', scope).forEach(b =>
        b.classList.toggle('playing', playing && b.dataset.sha === MusicPlayer.cur));
    }
  }

  return {
    _renderMusicList, _updateMusicListBtns, renderMusicApp,
  };
};

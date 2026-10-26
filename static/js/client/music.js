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

/* THE LIBRARY AS A MUSIC APP LISTS IT: Songs, Albums, Artists. DOM-free and dependency-free, so
 * tests/client/test_music_library_groups.py runs exactly this under node.
 *
 * An entry is the library's own ({sha, m, missing, offline}); `m.tg` is the tag index musiclib/the scan wrote
 * into the encrypted files index -- t title, a artist, al album, aa album artist, n track, d disc, y year,
 * p "the file carries a picture", oc the online cover id ('-' = looked up, none). A track with no index yet
 * (or no tags) is still a song: it groups under "Unknown album" / "Unknown artist", never dropped.
 *
 * Album identity is the ALBUM ARTIST (else the artist) plus the album name, both NORMALISED (case and
 * whitespace): "Abbey Road" and "abbey  road " are one album, and a compilation tagged "Various Artists"
 * stays one album however many artists sing on it. */
window.PCMusicGroups = (function(){
  const UNKNOWN_ALBUM = 'Unknown album', UNKNOWN_ARTIST = 'Unknown artist';
  const norm = s => String(s == null ? '' : s).normalize('NFC').toLowerCase().replace(/\s+/g, ' ').trim();
  const num = v => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : null; };
  function tags(e){
    const m = (e && e.m) || {}, g = (m.tg && typeof m.tg === 'object') ? m.tg : {};
    const title = String(g.t || m.name || 'track');
    const artist = String(g.a || '').trim(), albumArtist = String(g.aa || '').trim();
    return { title, artist, album: String(g.al || '').trim(), albumArtist, by: albumArtist || artist,
             track: num(g.n), disc: num(g.d), year: /^\d{4}$/.test(String(g.y || '')) ? String(g.y) : '',
             pic: !!g.p, oc: g.oc || '', indexed: !!(g.v >= 1) };
  }
  function albumKey(t){ return norm(t.by) + '\u0001' + norm(t.album); }
  function artistKey(t){ return norm(t.by); }
  // Disc, then track, then title -- a track with no number after the numbered ones, alphabetical among themselves.
  function byPosition(a, b){
    const A = tags(a), B = tags(b);
    const d = (A.disc || 1) - (B.disc || 1); if(d) return d;
    if(A.track && B.track && A.track !== B.track) return A.track - B.track;
    if(!!A.track !== !!B.track) return A.track ? -1 : 1;
    return A.title.localeCompare(B.title, undefined, { sensitivity: 'base', numeric: true });
  }
  const cmp = (x, y) => String(x).localeCompare(String(y), undefined, { sensitivity: 'base', numeric: true });
  function spelling(tracks, pick){
    const n = new Map(); let best = '', most = 0;
    for(const e of tracks){ const s = String(pick(tags(e)) || '').trim(); if(!s) continue;
      const c = (n.get(s) || 0) + 1; n.set(s, c); if(c > most){ most = c; best = s; } }
    return best;
  }
  function albums(entries){
    const map = new Map();
    for(const e of entries || []){
      const t = tags(e), key = albumKey(t);
      let a = map.get(key);
      if(!a){ a = { key, album: t.album || UNKNOWN_ALBUM, artist: t.by || (t.album ? UNKNOWN_ARTIST : ''),
                    year: '', unknown: !t.album, tracks: [], pic: false, oc: '' }; map.set(key, a); }
      a.tracks.push(e);
      if(t.year && (!a.year || t.year < a.year)) a.year = t.year;
      if(t.pic) a.pic = true;
      if(t.oc && !a.oc) a.oc = t.oc;
    }
    const out = [...map.values()];
    out.forEach(a => {
      a.tracks.sort(byPosition);
      // The name is how MOST of its tracks spell it (ties: the album's first track) -- not whichever file happened to
      // come first, which put one badly typed tag ("neon  skyline") on the whole album.
      if(!a.unknown){ a.album = spelling(a.tracks, t => t.album) || a.album; a.artist = spelling(a.tracks, t => t.by) || a.artist; }
    });
    // Unknown albums last; otherwise by artist, then album -- the order a shelf is in.
    return out.sort((x, y) => (x.unknown - y.unknown) || cmp(x.artist, y.artist) || cmp(x.album, y.album));
  }
  function artists(entries){
    const map = new Map();
    for(const e of entries || []){
      const t = tags(e), key = artistKey(t);
      let a = map.get(key);
      if(!a){ a = { key, name: t.by || UNKNOWN_ARTIST, unknown: !t.by, tracks: [] }; map.set(key, a); }
      a.tracks.push(e);
    }
    const out = [...map.values()];
    out.forEach(a => {
      if(!a.unknown) a.name = spelling(a.tracks, t => t.by) || a.name;
      a.albums = albums(a.tracks);
      a.tracks = a.albums.flatMap(al => al.tracks);       // an artist's songs read album by album, in order
    });
    return out.sort((x, y) => (x.unknown - y.unknown) || cmp(x.name, y.name));
  }
  /* Songs: 'title' | 'artist' | 'album' | 'added' (newest first -- the library's own order). */
  function sortSongs(entries, by){
    const list = (entries || []).slice();
    if(by === 'title') return list.sort((a, b) => cmp(tags(a).title, tags(b).title));
    // Normalised, as albums() groups them: "Abbey Road" and "abbey  road " are one album, so sorting them as two
    // would interleave its tracks with nothing between them but a typing difference.
    if(by === 'artist') return list.sort((a, b) => { const A = tags(a), B = tags(b);
      return (!A.by - !B.by) || cmp(norm(A.by), norm(B.by)) || cmp(norm(A.album), norm(B.album)) || byPosition(a, b); });
    if(by === 'album') return list.sort((a, b) => { const A = tags(a), B = tags(b);
      return (!A.album - !B.album) || cmp(norm(A.album), norm(B.album)) || cmp(norm(A.by), norm(B.by)) || byPosition(a, b); });
    return list.sort((a, b) => ((b.m && b.m.ts) || 0) - ((a.m && a.m.ts) || 0));
  }
  // One search box, three tabs: a song matches on title, artist or album; an album on its name or artist.
  function matchSong(e, q){ const n = norm(q); if(!n) return true; const t = tags(e);
    return [t.title, t.artist, t.albumArtist, t.album, (e.m && e.m.name) || ''].some(s => norm(s).includes(n)); }
  function matchAlbum(a, q){ const n = norm(q); return !n || norm(a.album).includes(n) || norm(a.artist).includes(n); }
  function matchArtist(a, q){ const n = norm(q); return !n || norm(a.name).includes(n); }
  return { tags, albums, artists, sortSongs, matchSong, matchAlbum, matchArtist, albumKey, norm, byPosition,
           UNKNOWN_ALBUM, UNKNOWN_ARTIST };
})();

/* ALBUM COVERS, CACHED ON THIS DEVICE. A cover is a 320px JPEG thumbnail in IndexedDB, keyed by the album -- never
 * in the files index (one document re-saved whole; a few hundred covers would make every save megabytes) and never
 * uploaded (an extra blob in nobody's index is exactly what the Blossom orphan reclaim exists to delete). Where it
 * comes from: the file's own picture (read by the tag scan or at upload), or the opt-in online lookup through this
 * node. A fresh device re-derives covers album by album, one track each -- not the whole library.
 * Object URLs are capped (oldest revoked): a grid scrolled through 500 albums must not hold 500 blobs for ever. */
window.PCMusicArt = (function(){
  const urls = new Map(), MAX_URLS = 240;
  let dbp = null;
  const db = () => dbp || (dbp = new Promise(res => {
    try{ const r = indexedDB.open('pc-music-art', 1);
      r.onupgradeneeded = () => { try{ r.result.createObjectStore('art'); }catch(_){ } };
      r.onsuccess = () => res(r.result); r.onerror = () => res(null); r.onblocked = () => res(null);
    }catch(_){ res(null); }
  }));
  const req = (q) => new Promise(res => { q.onsuccess = () => res(q.result); q.onerror = () => res(null); });
  function keep(key, url){
    urls.delete(key); urls.set(key, url);
    while(urls.size > MAX_URLS){ const [k, u] = urls.entries().next().value; urls.delete(k); try{ URL.revokeObjectURL(u); }catch(_){ } }
    return url;
  }
  async function get(key){
    if(urls.has(key)){ const u = urls.get(key); urls.delete(key); urls.set(key, u); return u; }
    const d = await db(); if(!d) return '';
    let blob = null;
    try{ blob = await req(d.transaction('art').objectStore('art').get(key)); }catch(_){ blob = null; }
    if(!blob || urls.has(key)) return urls.get(key) || '';
    return keep(key, URL.createObjectURL(blob));
  }
  async function has(key){
    if(urls.has(key)) return true;
    const d = await db(); if(!d) return false;
    try{ return !!(await req(d.transaction('art').objectStore('art').count(key))); }catch(_){ return false; }
  }
  async function put(key, blob){
    const d = await db(); if(!d || !blob) return false;
    const ok = await new Promise(res => { try{ const tx = d.transaction('art', 'readwrite'); tx.objectStore('art').put(blob, key);
      tx.oncomplete = () => res(true); tx.onerror = () => res(false); tx.onabort = () => res(false); }catch(_){ res(false); } });
    if(ok){ const old = urls.get(key); urls.delete(key); if(old){ try{ URL.revokeObjectURL(old); }catch(_){ } } }
    return ok;
  }
  // Square, centre-cropped, small: a grid tile, not a poster. null for anything that does not decode.
  async function thumb(blob){
    try{
      const bmp = await createImageBitmap(blob), side = 320, c = document.createElement('canvas');
      c.width = side; c.height = side;
      const s = Math.min(bmp.width, bmp.height); if(!s) return null;
      c.getContext('2d').drawImage(bmp, (bmp.width - s) / 2, (bmp.height - s) / 2, s, s, 0, 0, side, side);
      try{ bmp.close && bmp.close(); }catch(_){ }
      return await new Promise(r => c.toBlob(r, 'image/jpeg', 0.85));
    }catch(_){ return null; }
  }
  async function fromPicture(key, pic){
    if(!pic || !pic.data || !pic.data.length) return false;
    const t = await thumb(new Blob([pic.data], { type: pic.mime || 'image/jpeg' }));
    return t ? put(key, t) : false;
  }
  // The upload path (musiclib.uploadMusicTrack) hands over raw tags; the album key is the library's own.
  function fromTags(t){
    const G = window.PCMusicGroups, T = window.PCAudioTags;
    if(!G || !T || !T.index) return Promise.resolve(false);
    return fromPicture(G.albumKey(G.tags({ m: { tg: T.index(t) } })), t && t.picture);
  }
  return { get, has, put, thumb, fromPicture, fromTags, _urls: urls };
})();

window.PCMusicFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.GUEST, S.ME, S.VIEW, S._audioEl, S._blobHave, S._musicPl, S._musicPlRepaint, S._musicQ, S._musicSwept
  const {
    $, $$, FilesIdx, MusicOffline, MusicPlayer, PL, _clearNav, _feedScrollable, _hidePill,
    _musicExt, _musicRefreshedSet, _musicShareCurrent, _musicShareLoad, _musicShareView, _plTracks,
    _refreshBlobHave, _trackMeta, closeModal, delBlob, deleteBlobQuiet, enc, modal, musicEntries,
    musicTracks, openMusicFolder, renameBlob, saveEncrypted, toast, uiConfirm, uiPrompt,
    _withModule, trackPlainBlob, ensureAiSession, authFetch, saveClientPrefsNostr, touchPref,
  } = dep;
  const G = window.PCMusicGroups, Art = window.PCMusicArt;

  /* The Music APP: a player, not a folder. Opening Music used to land you in Files → 🎵 Music — the
   * same screen you use to UPLOAD, complete with folder chips and a drop zone — which is a file
   * manager that happens to contain songs. This is the library as a playlist with transport on top;
   * uploading is one button away rather than the thing you arrive at.
   * It reuses _renderMusicList for the rows and the floating MusicPlayer for playback, so there is
   * one library, one queue, and one set of controls that cannot drift out of step. */
  let _plUnwatch = null;
  function renderMusicApp(){
    const feed=$('#feed'); if(!feed) return;
    /* CLAIM THE VIEW. #feed is one element shared by every screen, and the live timeline appends to
     * it whenever VIEW is 'home' or 'global' — so rendering a player into it without changing VIEW
     * meant incoming social posts wrote straight over the library. ("new social posts take over
     * Music".) Every other non-timeline screen does exactly this; leaving VIEW alone was the bug. */
    S.VIEW='music'; _hidePill(); _clearNav();
    { const t=$('#view-title'); if(t) t.textContent='Music'; }
    _feedScrollable(feed);
    /* A PLAYER, LAID OUT LIKE ONE. The now-playing card above the library carries the cover, title, artist,
     * scrubber and transport; tapping the cover opens NOW PLAYING -- the same card filling the screen, cover
     * large on top and the transport at the bottom where a thumb is. One set of elements in two layouts
     * (`.ma-full` on .music-app), never a second copy of the controls to drift out of step. */
    feed.innerHTML=`<div class="music-app${_maFull?' ma-full':''}">
      <div class="ma-now">
        <button class="ma-art" id="ma-art" type="button" title="Now playing" aria-label="Open Now playing">${MusicPlayer.artHtml(MusicPlayer.cur,'ma-cover')}</button>
        <div class="ma-meta"><b id="ma-title">Nothing playing</b><span class="ma-artist" id="ma-artist"></span><span class="muted small" id="ma-sub"></span></div>
        <button class="btn btn-ghost small ma-collapse" id="ma-collapse" type="button" title="Back to your library" aria-label="Close Now playing"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-down"></use></svg></button>
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
          <button class="btn btn-ghost small" id="ma-eq-toggle" aria-expanded="false" aria-controls="ma-eq"><svg class="ic b-ic" aria-hidden="true"><use href="#i-sliders"></use></svg>Equalizer</button>
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
      <div class="ma-browse" id="ma-browse">
        <div class="ma-tabs" role="tablist" aria-label="Browse your library">
          <button type="button" role="tab" class="ma-tab" id="ma-tab-songs" data-tab="songs">Songs</button>
          <button type="button" role="tab" class="ma-tab" id="ma-tab-albums" data-tab="albums">Albums</button>
          <button type="button" role="tab" class="ma-tab" id="ma-tab-artists" data-tab="artists">Artists</button>
        </div>
        <select class="input ma-sort" id="ma-sort" aria-label="Sort songs">
          <option value="added">Recently added</option><option value="title">Title</option>
          <option value="artist">Artist</option><option value="album">Album</option>
        </select>
        <button type="button" class="btn btn-ghost small ma-libopts" id="ma-libopts" title="Library options" aria-label="Library options"><svg class="ic b-ic" aria-hidden="true"><use href="#i-more"></use></svg></button>
      </div>
      <div class="ma-scan hidden" id="ma-scan" role="status" aria-live="polite"><span class="ma-scan-t" id="ma-scan-t"></span><span class="ma-scan-bar" aria-hidden="true"><i id="ma-scan-fill"></i></span></div>
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
        _maBrowseBar(feed, false);
        PCMusicShare.renderView(S._musicPl, lib, {
          libraryChanged: () => {
            try{ _musicAppNow(); if(window.PCOS && PCOS.musicChanged) PCOS.musicChanged(); }catch(_){} },
          // Accepting a share selects it, so the answer lands on the playlist itself rather than on
          // a chip that may be scrolled off the end of the bar.
          open: key => { S._musicPl = key; paint(); },
          // A removed share is gone from the bar: back to the whole library, with the bar redrawn.
          closed: () => { S._musicPl = null; paint(); } });
        _musicAppNow();
        return;
      }
      { const qi=$('#ma-q',feed); if(qi) qi.classList.remove('hidden'); }
      const sel = S._musicPl && window.PCPlaylists && PCPlaylists.get(S._musicPl);
      if(S._musicPl && !sel) S._musicPl = null;
      // A playlist is its own ORDER and stays the plain list it always was; the whole library is browsed by tab.
      _maBrowseBar(feed, !sel);
      if(sel) _renderMusicList(lib, null, S._musicQ, _plTracks(S._musicPl));
      else _maPaintLibrary(lib);
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
      /* ONE WATCHER, replaced on every open. Each render used to ADD one and none was ever removed: every closed
       * Music window stayed in memory through it (its whole screen, ~160 nodes and 21 listeners per open,
       * measured) and every playlist change repainted all of the dead copies. In the APK every desktop window
       * lives in ONE WebView, so that is what Android's low-memory killer eventually took -- "android tablet
       * keeps restarting in the middle of using it, desktop mode" (2026-10-15). */
      if(_plUnwatch){ try{ _plUnwatch(); }catch(_){ } }
      _plUnwatch = PCPlaylists.onChange(()=>{ if(lib.isConnected) paint(); else if(_plUnwatch){ _plUnwatch(); _plUnwatch=null; } });
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
    b('#ma-art',()=>_maSetFull(!_maFull));
    b('#ma-collapse',()=>_maSetFull(false));
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
          if(sel) _renderMusicList(lib, null, S._musicQ, _plTracks(S._musicPl));
          else _maPaintLibrary(lib);
          _musicAppNow(); }; } }
    _maBindBrowse(feed, lib);
    // The tag index fills itself in the background once the library index is here (resumable: a track is
    // read once, ever, and the record lives in the encrypted files index).
    indexReady.then(ok=>{ if(ok) Lib.start(); }).catch(()=>{});
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
  /* NOW PLAYING, opened from the cover and closed by its chevron -- or by Android's Back (app.js asks
   * PCMusicFull before it walks the view history), so Back leaves the big player for the library and not
   * the whole Music screen. Kept for the session: reopening Music returns to whichever you were looking at. */
  let _maFull=false;
  function _maSetFull(on){
    _maFull=!!on;
    const app=document.querySelector('#feed .music-app');
    if(app) app.classList.toggle('ma-full', _maFull);
    const a=document.getElementById('ma-art');
    if(a){ a.setAttribute('aria-expanded', _maFull?'true':'false'); a.setAttribute('aria-label', _maFull?'Now playing':'Open Now playing'); }
    try{ MusicPlayer._startViz && S._audioEl && !S._audioEl.paused && MusicPlayer._startViz(); }catch(_){}
  }
  /* Android's Back asks `open()` before walking the view history: true while there is something INSIDE Music to
   * close -- the full player first, else an album or artist page (which returns to its tab, or to the artist). */
  const _fullOpen = () => _maFull && !!document.querySelector('#feed .music-app.ma-full');
  window.PCMusicFull = {
    open: () => _fullOpen() || (!!_maNav.page && !!document.getElementById('ma-page')),
    close: () => { if(_fullOpen()) _maSetFull(false); else if(_maNav.page) _maBack(); },
  };
  function _musicAppNow(){
    const t=document.getElementById('ma-title'); if(!t) return;
    const inf=MusicPlayer.cur?MusicPlayer.info(MusicPlayer.cur):null;
    t.textContent=(inf&&inf.title)||'Nothing playing';
    { const ar=document.getElementById('ma-artist');
      if(ar){ const by=inf?[inf.artist,inf.album].filter(Boolean).join(' · '):'';
        ar.textContent=by; ar.classList.toggle('hidden', !by); } }
    /* The cover, redrawn only when it is a different picture -- this runs on every state change, and a
     * fresh <img> on each one would flash. */
    { const a=document.getElementById('ma-art');
      const sig=inf?(MusicPlayer.cur+'|'+inf.cover):'';
      if(a && a.dataset.sig!==sig){ a.dataset.sig=sig; a.innerHTML=MusicPlayer.artHtml(MusicPlayer.cur,'ma-cover'); }
      if(a) a.setAttribute('aria-expanded', _maFull?'true':'false'); }
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
  /* ===== SONGS / ALBUMS / ARTISTS ==========================================================================
   * The whole library is browsed by tab; a playlist (or a share) stays the plain ordered list it always was.
   * The tab and the Songs sort are per DEVICE (localStorage) -- a phone and a desktop are browsed differently.
   * An album or artist PAGE is a step inside Music: its Back (and Android's, through PCMusicFull) returns to the
   * tab it came from, or from an album opened on an artist's page, to that artist. */
  const _ls = { get(k, d){ try{ const v = localStorage.getItem(k); return v == null ? d : v; }catch(_){ return d; } },
                set(k, v){ try{ localStorage.setItem(k, v); }catch(_){ } } };
  const MA_TABS = ['songs', 'albums', 'artists'], MA_SORTS = ['added', 'title', 'artist', 'album'];
  let _maTab = MA_TABS.includes(_ls.get('pc_music_tab', 'songs')) ? _ls.get('pc_music_tab', 'songs') : 'songs';
  let _maSort = MA_SORTS.includes(_ls.get('pc_music_sort', 'added')) ? _ls.get('pc_music_sort', 'added') : 'added';
  let _maNav = { page: null };          // {page:'album'|'artist', key, from?:<nav>}
  let _maShown = [];                    // the albums/artists currently drawn, by the index their buttons carry
  const MA_GRID_PAGE = 60, MA_ROW_PAGE = 120;
  // A colour per album for its generated cover, fixed by the album -- the player does the same per track.
  const _maHue = key => { let h = 0; for(const c of String(key)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; };

  function _maEnsureCss(){
    if(document.getElementById('pc-music-lib-css')) return;
    const l = document.createElement('link');
    l.id = 'pc-music-lib-css'; l.rel = 'stylesheet';
    l.href = '/static/css/music-library.css' + (window.__VER ? ('?v=' + encodeURIComponent(window.__VER)) : '');
    document.head.appendChild(l);
  }
  function _maBrowseBar(feed, on){
    const bar = $('#ma-browse', feed); if(!bar) return;
    _maEnsureCss();
    bar.classList.toggle('hidden', !on);
    // Browsing the library: on a phone the screen scrolls as ONE page (music-library.css), so the now-playing card
    // scrolls away and the albums get the screen instead of a strip under it. A class, not :has() on the app.
    { const app = bar.closest('.music-app'); if(app) app.classList.toggle('ma-browsing', !!on); }
    $$('.ma-tab', bar).forEach(t => { const sel = t.dataset.tab === _maTab && !_maNav.page;
      t.classList.toggle('on', t.dataset.tab === _maTab); t.setAttribute('aria-selected', sel ? 'true' : 'false'); });
    const so = $('#ma-sort', bar);
    if(so){ so.value = _maSort; so.classList.toggle('hidden', !(on && _maTab === 'songs' && !_maNav.page)); }
    const qi = $('#ma-q', feed);
    if(qi) qi.placeholder = !on ? 'Search your library'
      : _maNav.page === 'album' ? 'Search this album' : _maNav.page === 'artist' ? 'Search this artist'
      : _maTab === 'albums' ? 'Search albums' : _maTab === 'artists' ? 'Search artists' : 'Search songs';
  }
  function _maRepaint(){
    const lib = document.getElementById('ma-lib'), feed = document.getElementById('feed');
    if(!lib || !feed || !lib.isConnected) return;
    const sel = S._musicPl && window.PCPlaylists && PCPlaylists.get(S._musicPl);
    if(sel || _musicShareView()) return;
    _maBrowseBar(feed, true); _maPaintLibrary(lib);
  }
  function _maGo(nav){ _maNav = nav || { page: null };
    const feed = document.getElementById('feed'); if(feed) feed.scrollTop = 0;
    _maRepaint(); }
  function _maBack(){ _maGo(_maNav.from || { page: null }); }
  function _maBindBrowse(feed, lib){
    $$('.ma-tab[data-tab]', feed).forEach(t => t.onclick = () => {
      _maTab = t.dataset.tab; _ls.set('pc_music_tab', _maTab); _maGo({ page: null }); });
    { const so = $('#ma-sort', feed); if(so) so.onchange = () => {
        _maSort = MA_SORTS.includes(so.value) ? so.value : 'added'; _ls.set('pc_music_sort', _maSort); _maRepaint(); }; }
    { const lo = $('#ma-libopts', feed); if(lo) lo.onclick = () => _maOptions(); }
    Lib.show();
  }
  function _maPaintLibrary(lib){
    if(!lib) return;
    const q = S._musicQ || '', entries = musicEntries(null);
    if(_maNav.page === 'album'){
      const al = G.albums(entries).find(a => a.key === _maNav.key);
      if(al) return _maAlbumPage(lib, al, q);
      _maNav = { page: null };
    }
    if(_maNav.page === 'artist'){
      const ar = G.artists(entries).find(a => a.key === _maNav.key);
      if(ar) return _maArtistPage(lib, ar, q);
      _maNav = { page: null };
    }
    if(_maTab === 'albums') return _maAlbumsGrid(lib, G.albums(entries).filter(a => G.matchAlbum(a, q)), q);
    if(_maTab === 'artists') return _maArtistsList(lib, G.artists(entries).filter(a => G.matchArtist(a, q)), q);
    lib.onclick = null;
    _renderMusicList(lib, null, q, null, { sort: _maSort });
  }
  function _maArtHtml(key, cls){
    return `<span class="pc-art ph ${cls}" style="--art-h:${_maHue(key)}" data-akey="${enc(key)}" data-art="placeholder">`
      + `<svg class="ic" aria-hidden="true"><use href="#i-music"></use></svg></span>`;
  }
  /* Covers are filled in as they scroll into view, never all at once: a 500-album grid builds 60 cards, and each
   * card asks the device cache for its picture only when it is about to be seen. */
  let _maIO = null;
  function _maFillArt(root){
    const arts = $$('.pc-art[data-akey]:not([data-filled])', root);
    if(!arts.length) return;
    const fill = el => { el.dataset.filled = '1';
      Art.get(el.dataset.akey).then(u => { if(!u || !el.isConnected) return;
        el.classList.remove('ph'); el.dataset.art = 'cover';
        el.innerHTML = `<img class="pc-art-img" src="${enc(u)}" alt="" decoding="async" draggable="false">`; }).catch(() => {}); };
    if(typeof IntersectionObserver === 'undefined'){ arts.forEach(fill); return; }
    if(!_maIO) _maIO = new IntersectionObserver(es => es.forEach(e => { if(e.isIntersecting){ _maIO.unobserve(e.target); fill(e.target); } }),
                                                { rootMargin: '300px' });
    arts.forEach(a => _maIO.observe(a));
  }
  function _maCard(a, i){
    const sub = [a.artist, a.year].filter(Boolean).join(' · ');
    return `<button type="button" class="ma-card" data-i="${i}" aria-label="${enc(a.album + (a.artist ? ', ' + a.artist : ''))}">`
      + _maArtHtml(a.key, 'ma-card-art')
      + `<span class="ma-card-t">${enc(a.album)}</span><span class="ma-card-s">${enc(sub || ' ')}</span></button>`;
  }
  function _maEmpty(q, what){
    return `<div class="empty">${q ? `No ${what} match “${enc(q)}”.` : `No ${what} yet — add some music.`}</div>`;
  }
  function _maAlbumsGrid(lib, albums, q){
    lib.className = 'ma-lib-browse';
    _maShown = albums;
    const first = albums.slice(0, MA_GRID_PAGE), rest = albums.length - first.length;
    lib.innerHTML = albums.length
      ? `<div class="ma-count muted small" aria-live="polite">${albums.length} album${albums.length === 1 ? '' : 's'}</div>`
        + `<div class="ma-grid" id="ma-albums">${first.map(_maCard).join('')}</div>`
        + (rest ? `<button type="button" class="mus-more ma-more" id="ma-more">Show ${Math.min(MA_GRID_PAGE, rest)} more (${rest} left)</button>` : '')
      : _maEmpty(q, 'albums');
    lib.onclick = ev => {
      const more = ev.target.closest && ev.target.closest('#ma-more');
      if(more){ const grid = $('#ma-albums', lib), have = grid.children.length;
        grid.insertAdjacentHTML('beforeend', _maShown.slice(have, have + MA_GRID_PAGE).map((a, k) => _maCard(a, have + k)).join(''));
        const left = _maShown.length - grid.children.length;
        if(left > 0) more.textContent = `Show ${Math.min(MA_GRID_PAGE, left)} more (${left} left)`; else more.remove();
        _maFillArt(lib); return; }
      const c = ev.target.closest && ev.target.closest('.ma-card'); if(!c || !lib.contains(c)) return;
      const a = _maShown[+c.dataset.i]; if(a) _maGo({ page: 'album', key: a.key });
    };
    _maFillArt(lib);
  }
  function _maArtistRow(a, i){
    const al = a.albums.filter(x => !x.unknown).length, n = a.tracks.length;
    const art = (a.albums.find(x => x.pic || x.oc) || a.albums[0] || { key: a.key }).key;
    return `<button type="button" class="ma-arow" data-i="${i}">${_maArtHtml(art, 'ma-artist-art')}`
      + `<span class="ma-artist-m"><span class="ma-artist-n">${enc(a.name)}</span>`
      + `<span class="ma-artist-s muted small">${al ? `${al} album${al === 1 ? '' : 's'} · ` : ''}${n} song${n === 1 ? '' : 's'}</span></span></button>`;
  }
  function _maArtistsList(lib, artists, q){
    lib.className = 'ma-lib-browse';
    _maShown = artists;
    const first = artists.slice(0, MA_ROW_PAGE), rest = artists.length - first.length;
    lib.innerHTML = artists.length
      ? `<div class="ma-count muted small" aria-live="polite">${artists.length} artist${artists.length === 1 ? '' : 's'}</div>`
        + `<div class="ma-artists" id="ma-artists">${first.map(_maArtistRow).join('')}</div>`
        + (rest ? `<button type="button" class="mus-more ma-more" id="ma-more">Show ${Math.min(MA_ROW_PAGE, rest)} more (${rest} left)</button>` : '')
      : _maEmpty(q, 'artists');
    lib.onclick = ev => {
      const more = ev.target.closest && ev.target.closest('#ma-more');
      if(more){ const list = $('#ma-artists', lib), have = list.children.length;
        list.insertAdjacentHTML('beforeend', _maShown.slice(have, have + MA_ROW_PAGE).map((a, k) => _maArtistRow(a, have + k)).join(''));
        const left = _maShown.length - list.children.length;
        if(left > 0) more.textContent = `Show ${Math.min(MA_ROW_PAGE, left)} more (${left} left)`; else more.remove();
        _maFillArt(lib); return; }
      const r = ev.target.closest && ev.target.closest('.ma-arow'); if(!r || !lib.contains(r)) return;
      const a = _maShown[+r.dataset.i]; if(a) _maGo({ page: 'artist', key: a.key });
    };
    _maFillArt(lib);
  }
  // Play an album (or an artist) from its page: that set, in its order, becomes the queue.
  function _maPlaySet(tracks, shuffle){
    const q = (tracks || []).filter(t => !t.missing).map(t => t.sha);
    if(!q.length){ toast('nothing here can be played'); return; }
    MusicPlayer._pl = null;
    MusicPlayer.shuffle = !!shuffle;
    MusicPlayer.queue = q;
    if(shuffle) MusicPlayer.shufflePlay(); else MusicPlayer.play(q[0], { force: true });
    try{ MusicPlayer._render(); _musicAppNow(); }catch(_){ }
  }
  function _maPageHead(kind, key, title, sub, meta, backLabel){
    return `<div class="ma-page-head">
        <button type="button" class="btn btn-ghost small ma-back" id="ma-back" aria-label="Back to ${enc(backLabel)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chevron-left"></use></svg><span>${enc(backLabel)}</span></button>
        <div class="ma-page-top">${_maArtHtml(key, 'ma-page-art')}
          <div class="ma-page-meta"><b class="ma-page-t">${enc(title)}</b>${sub ? `<span class="ma-page-s">${enc(sub)}</span>` : ''}<span class="muted small ma-page-x">${enc(meta)}</span></div></div>
        <div class="ma-page-acts">
          <button type="button" class="btn btn-neon ma-page-play" id="ma-${kind}-play"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg><span>Play</span></button>
          <button type="button" class="btn btn-ghost ma-page-shuffle" id="ma-${kind}-shuffle"><svg class="ic b-ic" aria-hidden="true"><use href="#i-shuffle"></use></svg><span>Shuffle</span></button>
        </div></div>`;
  }
  const _maBackLabel = () => _maNav.from && _maNav.from.page === 'artist' ? 'Artist' : _maTab === 'artists' ? 'Artists' : _maTab === 'songs' ? 'Songs' : 'Albums';
  function _maAlbumPage(lib, al, q){
    lib.className = 'ma-lib-browse'; lib.onclick = null;
    const n = al.tracks.length, meta = [al.year, `${n} song${n === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
    lib.innerHTML = `<div class="ma-page" id="ma-page" data-kind="album">${_maPageHead('alb', al.key, al.album, al.artist, meta, _maBackLabel())}`
      + `<div class="music-list" id="ma-pagelist"></div></div>`;
    $('#ma-back', lib).onclick = _maBack;
    $('#ma-alb-play', lib).onclick = () => _maPlaySet(al.tracks, false);
    $('#ma-alb-shuffle', lib).onclick = () => _maPlaySet(al.tracks, true);
    _renderMusicList($('#ma-pagelist', lib), null, q, al.tracks, { compact: true, numbered: true });
    _maFillArt(lib);
  }
  function _maArtistPage(lib, ar, q){
    lib.className = 'ma-lib-browse';
    const albums = ar.albums.filter(a => !a.unknown), n = ar.tracks.length;
    const meta = [albums.length ? `${albums.length} album${albums.length === 1 ? '' : 's'}` : '', `${n} song${n === 1 ? '' : 's'}`].filter(Boolean).join(' · ');
    const art = (ar.albums.find(x => x.pic || x.oc) || ar.albums[0] || { key: ar.key }).key;
    _maShown = albums;
    lib.innerHTML = `<div class="ma-page" id="ma-page" data-kind="artist">${_maPageHead('art', art, ar.name, '', meta, _maBackLabel())}`
      + (albums.length ? `<h4 class="ma-sec">Albums</h4><div class="ma-grid ma-grid-sm" id="ma-albums">${albums.map(_maCard).join('')}</div>` : '')
      + `<h4 class="ma-sec">Songs</h4><div class="music-list" id="ma-pagelist"></div></div>`;
    $('#ma-back', lib).onclick = _maBack;
    $('#ma-art-play', lib).onclick = () => _maPlaySet(ar.tracks, false);
    $('#ma-art-shuffle', lib).onclick = () => _maPlaySet(ar.tracks, true);
    lib.onclick = ev => {
      const c = ev.target.closest && ev.target.closest('.ma-card'); if(!c || !lib.contains(c)) return;
      const a = _maShown[+c.dataset.i]; if(a) _maGo({ page: 'album', key: a.key, from: { page: 'artist', key: ar.key } });
    };
    _renderMusicList($('#ma-pagelist', lib), null, q, ar.tracks, { compact: true });
    _maFillArt(lib);
  }
  /* LIBRARY OPTIONS -- one switch today: the opt-in online cover lookup. Saved like every client pref (this device,
   * then the account's pcai:client-prefs, so it follows to the next device); read live by the scan. */
  const _coverOn = () => !!(window.ClientSettings && ClientSettings.get('musicCoverLookup', false));
  function _maOptions(){
    const on = _coverOn();
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-music"></use></svg>Library</h3>
      <label class="ma-opt" for="ma-cover-lookup">
        <input type="checkbox" id="ma-cover-lookup"${on ? ' checked' : ''}>
        <span><b>Look up missing album covers online</b>
          <span class="muted small ma-opt-why">For albums whose files carry no cover, this server asks MusicBrainz and the Cover Art
          Archive (through its Tor-first proxy). They learn the artist and album names it asks about — nothing else about
          your library. Off until you turn it on.</span></span></label>
      <div class="row" style="justify-content:flex-end;margin-top:12px">
        <button type="button" class="btn btn-ghost small" id="ma-opt-close">Close</button></div>`, root => {
      $('#ma-opt-close', root).onclick = closeModal;
      const box = $('#ma-cover-lookup', root);
      box.onchange = () => {
        const v = !!box.checked;
        try{ ClientSettings.set('musicCoverLookup', v); }catch(_){ }
        try{ touchPref && touchPref('musicCoverLookup'); }catch(_){ }
        try{ const p = saveClientPrefsNostr && saveClientPrefsNostr({ musicCoverLookup: v }); if(p && p.catch) p.catch(() => {}); }catch(_){ }
        toast(v ? 'album covers will be looked up online' : 'online cover lookup is off');
        Lib.prefChanged();
      };
    });
  }

  /* ===== THE TAG INDEX, FILLED IN THE BACKGROUND ===========================================================
   * Library tracks are encrypted blobs: to learn a song's album it has to be fetched (or read from this device)
   * and decrypted, so it is done ONCE per track, and the record (`tg`, PCAudioTags.index) is written into the
   * encrypted files index -- the next device inherits it. One track at a time, a pause between, never while the
   * player is loading a track and never while the page is hidden; a track that fails is skipped (and retried next
   * session), never the end of the scan. Saved in checkpoints of 10, so a reload resumes where it was.
   * Then two cover passes: albums whose files carry a picture but this DEVICE has no thumbnail (one track per album),
   * and -- only when the person turned it on -- the online lookup for albums with no picture at all. */
  const SCAN_GAP = 150, SCAN_CHECKPOINT = 10;
  const Lib = window.PCMusicLibrary = {
    st: { running: false, phase: '', done: 0, total: 0, failed: new Set(), asked: new Set(), runs: 0 },
    pendingTags(){
      return musicEntries(null).filter(e => !e.missing && !(e.m.tg && e.m.tg.v >= 1) && !this.st.failed.has(e.sha));
    },
    async idle(){
      for(;;){
        const hidden = document.hidden, busy = !!MusicPlayer._loading;
        if(!hidden && !busy) return;
        await new Promise(r => setTimeout(r, hidden ? 500 : 120));
      }
    },
    show(){
      const box = document.getElementById('ma-scan'); if(!box) return;
      const st = this.st, on = st.running && st.total > 0;
      box.classList.toggle('hidden', !on);
      if(!on) return;
      const t = document.getElementById('ma-scan-t'), f = document.getElementById('ma-scan-fill');
      const what = st.phase === 'tags' ? 'Reading tags' : st.phase === 'covers' ? 'Finding album covers' : 'Looking up covers online';
      if(t) t.textContent = `${what}: ${st.done} of ${st.total}`;
      if(f) f.style.width = Math.round(100 * st.done / Math.max(1, st.total)) + '%';
    },
    async start(){
      if(this.st.running || S.GUEST || !S.ME) return;
      this.st.running = true; this.st.runs++;
      try{
        const T = await _withModule('audiotags.js', 'PCAudioTags');
        if(T && T.index){ await this.tagPass(T); await this.coverPass(T); }
        if(_coverOn()) await this.onlinePass();
      }catch(_){ }
      finally{ this.st.running = false; this.st.phase = ''; this.st.total = 0; this.show(); }
    },
    prefChanged(){ if(_coverOn() && !this.st.running) this.start(); },
    async tagPass(T){
      const list = this.pendingTags(); if(!list.length) return;
      const st = this.st; st.phase = 'tags'; st.done = 0; st.total = list.length; this.show();
      FilesIdx.beginBatch();
      let since = 0;
      try{
        for(const e of list){
          await this.idle();
          let tg = null, pic = null;
          try{ const t = await T.readBlob(await trackPlainBlob(e.sha)); tg = T.index(t); pic = t.picture; }
          catch(_){ st.failed.add(e.sha); }
          if(tg){
            FilesIdx.setFile(e.sha, { tg });
            if(pic){ const key = G.albumKey(G.tags({ m: Object.assign({}, e.m, { tg }) }));
              try{ if(!(await Art.has(key))) await Art.fromPicture(key, pic); }catch(_){ } }
          }
          st.done++; this.show();
          if(++since >= SCAN_CHECKPOINT){ since = 0; await FilesIdx.endBatch(); FilesIdx.beginBatch(); _maRepaint(); }
          await new Promise(r => setTimeout(r, SCAN_GAP));
        }
      }finally{ await FilesIdx.endBatch(); _maRepaint(); }
    },
    // Albums whose files carry a picture that THIS device has no thumbnail for: one track per album.
    async coverPass(T){
      const want = [];
      for(const a of G.albums(musicEntries(null).filter(e => !e.missing))){
        if(!a.pic || (await Art.has(a.key))) continue;
        const t = a.tracks.find(x => x.m.tg && x.m.tg.p && !this.st.failed.has(x.sha)); if(t) want.push([a, t]);
      }
      if(!want.length) return;
      const st = this.st; st.phase = 'covers'; st.done = 0; st.total = want.length; this.show();
      for(const [a, t] of want){
        await this.idle();
        try{ const tags = await T.readBlob(await trackPlainBlob(t.sha)); await Art.fromPicture(a.key, tags.picture); }
        catch(_){ st.failed.add(t.sha); }
        st.done++; this.show();
        await new Promise(r => setTimeout(r, SCAN_GAP));
      }
      _maRepaint();
    },
    /* OPT-IN. Asks THIS node (GET /api/music/cover), which asks MusicBrainz + the Cover Art Archive. An album is
     * asked about once: the answer -- a cover id, or '-' for "there is none" -- is written into every one of its
     * tracks' records, so no device asks again. "Could not ask" (503, network) writes NOTHING: it is not "no cover",
     * and the next session asks again. */
    async onlinePass(){
      const albums = G.albums(musicEntries(null).filter(e => !e.missing));
      const ask = [], fetchIds = [];
      for(const a of albums){
        if(a.unknown || !a.artist || a.pic) continue;
        if(!a.oc){ if(!this.st.asked.has(a.key)) ask.push(a); continue; }
        if(a.oc !== '-' && !(await Art.has(a.key))) fetchIds.push(a);
      }
      if(!ask.length && !fetchIds.length) return;
      const st = this.st; st.phase = 'online'; st.done = 0; st.total = ask.length + fetchIds.length; this.show();
      try{ await ensureAiSession(); }catch(_){ }
      FilesIdx.beginBatch();
      try{
        for(const a of ask){
          if(!_coverOn()) break;
          await this.idle();
          this.st.asked.add(a.key);
          // A POST with a body: the names somebody listens to never go in a URL (access logs write URLs).
          const r = await this.request('/api/music/cover', { artist: a.artist, album: a.album });
          if(r.id || r.none){
            const oc = r.id || '-';
            a.tracks.forEach(t => FilesIdx.setFile(t.sha, { tg: Object.assign({ v: 1 }, t.m.tg || {}, { oc }) }));
            if(r.id) await this.fetchCover(a.key, r.id);
          }
          st.done++; this.show();
        }
        for(const a of fetchIds){
          if(!_coverOn()) break;
          await this.idle();
          await this.fetchCover(a.key, a.oc);
          st.done++; this.show();
        }
      }finally{ await FilesIdx.endBatch(); _maRepaint(); }
    },
    async fetchCover(key, id){
      if(!/^[0-9a-f-]{36}$/i.test(String(id))) return false;
      const r = await this.request('/api/music/cover/' + encodeURIComponent(id));
      if(!r.blob) return false;
      const th = await Art.thumb(r.blob);
      return th ? Art.put(key, th) : false;
    },
    // {id} for a lookup (JSON), {blob} for an image; {none} = 404 (there is no cover); {unavailable} = anything else.
    async request(path, json){
      try{
        const r = await authFetch(path, json ? { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                 body: JSON.stringify(json) } : {});
        if(r.status === 200){
          if(json){ const j = await r.json(); return /^[0-9a-f-]{36}$/i.test(String(j && j.id)) ? { id: j.id } : { unavailable: true }; }
          return { blob: await r.blob() };
        }
        if(r.status === 404) return { none: true };
        return { unavailable: true };
      }catch(_){ return { unavailable: true }; }
    },
  };

  /* `only` — a restricted, ORDERED set of entries (a playlist). Passed rather than filtered inside,
   * because a playlist's order is its content: sorting it by date the way the library is sorted
   * would silently throw away the one thing the user arranged. */
  function _renderMusicList(grid, list, q, only, opts){
    if(!grid) return;
    /* Which tracks are already on this device — read ONCE, then from memory.
     *
     * The list repaints on every keystroke of the search box, so this cannot be an IDB scan per
     * paint. The first paint of a session may not know yet; it draws without the marks and repaints
     * itself when the answer arrives, which is a flicker of a badge rather than a blocked render. */
    if(!MusicOffline._have){
      MusicOffline.have().then(()=>{ if(grid.isConnected) _renderMusicList(grid, list, q, only, opts); }).catch(()=>{});
    }
    /* `opts`: {sort} orders the whole library (the Songs tab); {compact} is the list under an album or artist page,
     * whose own Play/Shuffle stand above it; {numbered} shows each song's track number (an album's own order). */
    opts = opts || {};
    const all = only || (opts.sort ? G.sortSongs(musicEntries(list), opts.sort) : musicEntries(list));
    const needle=String(q||'').trim().toLowerCase();
    // A song is found by its title, artist or album as well as its file name: that is what people search for.
    const tracks=needle ? all.filter(t=>G.matchSong(t, needle)) : all;
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
      ${opts.compact ? '' : `<div class="music-head-primary">
        <button class="btn btn-neon small" id="mus-shuffle"${liveAll?'':' disabled'}>
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-shuffle"></use></svg>Shuffle</button>
        <button class="btn btn-ghost small" id="mus-refresh" title="Fetch the library again — songs added on another device appear here">
          <svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg>Refresh</button>
        ${!only ? `<button class="btn btn-ghost small" id="mus-delall"${tracks.length?'':' disabled'}
          title="${needle ? 'Delete the songs matching this search — from your library, not just this view' : 'Delete every song in your library'}">${needle ? `Delete ${tracks.length} match${tracks.length===1?'':'es'}` : 'Delete All'}</button>` : ''}
      </div>`}
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
    // A song reads as its TITLE and artist once the tag index knows them; the file name until then.
    const rows = tracks.map(t=>{ const tg=G.tags(t), by=opts.compact && only && tg.album ? tg.artist : [tg.artist, tg.album].filter(Boolean).join(' · ');
      return `<div class="track${t.missing?' gone':''}" data-sha="${t.sha}">
        ${t.missing ? '<span class="track-play" aria-hidden="true">✕</span>'
                    : `<button class="track-play" data-sha="${t.sha}" aria-label="Play ${enc(tg.title)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-play"></use></svg></button>`}
        <span class="track-name">${opts.numbered && tg.track ? `<span class="track-no">${tg.track}</span>` : ''}${enc(tg.title)}</span>
        <span class="track-meta">${t.missing ? 'not on the server — delete to tidy up'
                                             : (by ? `<span class="track-by">${enc(by)}</span> · ` : '') + '🔒 ' + (((t.m.size||0)/1048576)).toFixed(1) + 'MB'
                                               + (t.offline ? ' · offline' : '')}</span>
        ${t.missing ? '' : `<button class="track-keep${t.offline?' on':''}" data-sha="${t.sha}" title="${t.offline?'Kept on this device — tap to remove the offline copy':'Keep on this device (plays with no network)'}" aria-label="${t.offline?'Remove offline copy':'Keep offline'}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-${t.offline?'check':'download'}"></use></svg></button>`}
        ${t.missing ? '' : `<button class="track-add" data-sha="${t.sha}" title="Add to a playlist" aria-label="Add to a playlist"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg></button>`}
        ${t.missing ? '' : `<button class="track-dl" data-sha="${t.sha}" data-name="${enc((t.m.name||'track')+'.'+_musicExt(t.m))}" title="Save a copy to your files (decrypts first)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-share"></use></svg></button>`}
        ${t.missing ? '' : `<button class="track-ren" data-sha="${t.sha}" data-name="${enc(t.m.name||'')}" title="Rename" aria-label="Rename"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button>`}
        <button class="track-del" data-sha="${t.sha}" title="Delete"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>
      </div>`; });
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
        _renderMusicList(grid, list, q, only, opts);
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
        _renderMusicList(grid, list, q, only, opts);
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
        MusicPlayer.shufflePlay();   // a fresh shuffled round of exactly this queue
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
        _renderMusicList(grid, list, q, only, opts);
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
        _renderMusicList(grid, null, q, _musicRefreshedSet(only), opts);
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
        _renderMusicList(grid, list, q, only, opts);
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

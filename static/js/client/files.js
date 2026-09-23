/* Files — the Explorer over the encrypted Blossom drive, synced folders, This Computer and the
 * trash; drive check and the admin blob views; previews, Open with…, the Office editor session and
 * Code round trips; encrypted uploads and the upload queue. Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_filesDeps`) and
 * builds this factory the first time one is called — opening Files or Office, opening a file from
 * another screen, an encrypted upload. The code below is BYTE-IDENTICAL to what it replaced in
 * app.js apart from its reads of app.js's live `let` bindings, which the parser rewrote to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets. Two listeners that used
 * to be bound at boot are bound when the module is built: the tree-open flags read from
 * localStorage, and `pc:sync-folders` (it refreshes a list only this module draws).
 *
 * Stayed in app.js: the drive index itself (FilesIdx), the navigation state other screens set
 * before opening Files (_filesTab, _filesFolder, _syncRoot, …), the selection sets, the upload
 * flag, the column/sort/icon helpers (thumbnails everywhere draw with _fxFileGlyph), the preview/open-with classifiers Mail shares,
 * and PosterChanOS's `pc-open` hook, which must be registered at boot.
 */
window.PCFilesFactory = function(dep){
  const _S = dep.state;   // live app.js bindings: _S.CFG, _S.IS_ADMIN, _S.LOGO, _S.ME, _S.VIEW, _S._badmSort, _S._blobHave, _S._blobSizes, _S._filesAdminPk, _S._filesFolder, _S._filesGridList, _S._filesQ, _S._filesTab, _S._fxMobileSource, _S._hostOn, _S._syncPath, _S._syncRoot, _S._uploadBatchAuth, _S._uploading, _S._vodNameMap, _S.signer
  const {
    $, $$, FilesIdx, NT, _CODE_MAX, _FX_COLS, _aesDecrypt, _b64u8, _bindThumbFallback,
    _blossomDenied, _contentIV, _filesDeleted, _filesSel, _fmtBytes, _fxBlobKey, _fxBlobName,
    _fxBytes, _fxColsHTML, _fxCompare, _fxEncIcon, _fxFileGlyph, _fxFolderIcon, _fxIcon,
    _fxSearchKey, _fxSetSort, _fxSort, _fxType, _fxView, _fxWhen, _handlersFor, _hostFs,
    _instanceBase, _keepBytes, _keptToast, _looksAudio, _masterDecrypt, _masterEncrypt,
    _musicHasSrc, _musicShareLoad, _officeable, _openFileName, _previewable, _refreshBlobHave,
    _renderMusicList, _selEl, _shaFromUrl, _signUploadBatch, _standalone, _streamFetch, _trackUrls,
    _withModule, blobThumb, closeModal, copyUrl, copyValue, delBlob, deleteBlobQuiet,
    doPurgeBlossom, downloadBlobFile, downloadName, enc, ensureAiSession, extOfBlob, fileFromBytes,
    fileLabel, mediaServer, mimeForName, modal, openMenuPopover, renameBlob, renderAiFiles,
    renderProfileView, requestBlossomAccess, saveBlobAs, saveEncrypted, sha256hex, sign,
    switchView, thumbUrl, toast, trackUrl, uiConfirm, uiPrompt, uploadBlob, uploadMusicTrack,
  } = dep;
  async function renderBlossom(){
    const feed=$('#feed');
    /* AND REPAINT WHEN THE DRIVE ACTUALLY ARRIVES.
     *
     * Nothing did. The index is pulled once per session and the ONLY thing that redrew this screen
     * afterwards was the manual Refresh button — so opening Files before the pull finished left the
     * built-in default on screen until the user clicked something, which is precisely what was
     * reported ("i had to click again to see all my blossom folders"). Saying "Loading your
     * folders…" without ever replacing it would just be an honest hang.
     *
     * Fires at most once: `ensure()` sets `_pullDone`, so the redraw cannot re-enter this branch.
     * Guarded on still being on this screen, because the pull outlives the view that started it. */
    if(!FilesIdx._pullDone){
      const _wasTab=_S._filesTab;
      Promise.resolve().then(()=>FilesIdx.ensure()).catch(()=>{}).then(()=>{
        if(_S.VIEW==='blossom' && _S._filesTab===_wasTab) renderBlossom();
      });
    }
    /* One Explorer-style navigation tree replaces the duplicate row of source tabs. */
    feed.innerHTML='<div id="files-pane"></div>';
    const pane=$('#files-pane',feed);
    // A remembered tab can name one that is no longer drawn (the AI tab, with no server) — the same
    // reason switchView re-checks a view the nav has stopped offering.
    if(_standalone() && _S._filesTab==='ai') _S._filesTab='public';
    // A remembered "This Computer" on a build with no filesystem names a tab that is not drawn.
    if(_S._filesTab==='computer' && !_hostFs()){ _S._filesTab='public'; _S._hostOn=false; }
    if(_S._filesTab==='admin'||_S._filesTab==='ai'){
      pane.innerHTML='<div class="fx-explorer"><div class="fx-side">'+_fxSideHTML()
        +'</div><div class="fx-main" id="files-special"></div></div>';
      _fxBindSide(pane);
      const special=$('#files-special',pane);
      return _S._filesTab==='admin'?renderBlossomAdmin(special):renderAiFiles(special);
    }
    if(_S._filesTab==='computer') return _renderHostRoot(pane);
    return renderPublicFiles(pane);
  }
  // Admin tab: per-user storage overview. Tap a row → review that user's files; tap avatar/name → profile.
  async function renderBlossomAdmin(pane){
    if(!_S.IS_ADMIN){ pane.innerHTML='<div class="empty">Admins only.</div>'; return; }
    if(_S._filesAdminPk) return renderBlossomAdminUser(pane, _S._filesAdminPk);
    pane.innerHTML='<div class="spinner"></div>';
    let users=[], total=0, err='';
    try{
      const auth=await sign(27235,'blossom-usage',[['p',_S.ME.pubkey]]);   // content 'blossom-usage' binds the admin proof to THIS action (server checks it)
      const r=await fetch('/client/admin-blossom-usage',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({auth:btoa(JSON.stringify(auth))})}).then(r=>r.json());
      if(r&&r.ok){ users=r.users||[]; total=r.total||0; } else err=(r&&r.error)||'failed';
    }catch(_){ err='request failed'; }
    if(err){ pane.innerHTML='<div class="empty">'+enc(err)+'</div>'; return; }
    if(!users.length){ pane.innerHTML='<div class="empty">No Blossom uploads on this server yet.</div>'; return; }
    const miss=users.map(u=>u.pubkey).filter(pk=>!Store.haveProfile(pk)).slice(0,300);
    if(miss.length){ try{ (await Relay.query([{authors:miss,kinds:[0],limit:miss.length}])).forEach(e=>Store.saveProfile(e)); }catch(_){} }
    /* A TABLE, not cards — "it's listed in Cards and it's useless the way it's designed". The
     * admin's question is comparative (who is using the space), and a comparison needs columns
     * that sort. Same visual family as the Files explorer; the drill-in below is unchanged. */
    _S._badmSort = _S._badmSort || { k:'size', d:-1 };
    const draw = () => {
      const k=_S._badmSort.k, d=_S._badmSort.d;
      const rows=[...users].sort((x,y)=>{
        const vx = k==='name' ? (((Store.profile(x.pubkey)||{}).name)||x.npub).toLowerCase()
                              : (x[k]||0);
        const vy = k==='name' ? (((Store.profile(y.pubkey)||{}).name)||y.npub).toLowerCase()
                              : (y[k]||0);
        return (vx<vy?-1:vx>vy?1:0)*d;
      });
      const arrow = c => _S._badmSort.k===c ? (_S._badmSort.d<0?' ▾':' ▴') : '';
      pane.innerHTML=`<div class="muted small" style="padding:8px 6px 4px">${users.length} uploader${users.length===1?'':'s'} · ${_fmtBytes(total)} stored · tap a column to sort, a row to review files</div>
        <div style="overflow-x:auto"><table class="badm-tbl" style="width:100%;border-collapse:collapse">
          <thead><tr>
            <th data-s="name" style="text-align:left;cursor:pointer;padding:6px">User${arrow('name')}</th>
            <th data-s="count" style="text-align:right;cursor:pointer;padding:6px">Files${arrow('count')}</th>
            <th data-s="size" style="text-align:right;cursor:pointer;padding:6px">Stored${arrow('size')}</th>
            <th style="padding:6px"></th>
          </tr></thead>
          <tbody>${rows.map(u=>{ const p2=Store.profile(u.pubkey)||{}; const nm=p2.name||p2.display_name||(u.npub.slice(0,12)+'…');
            return `<tr class="badm-row" data-pk="${u.pubkey}" style="cursor:pointer;border-top:1px solid var(--border,#333)">
              <td style="padding:6px;display:flex;align-items:center;gap:8px;min-width:0">
                <img src="${enc(p2.picture||_S.LOGO)}" class="badm-prof" data-pk="${u.pubkey}" style="width:24px;height:24px;border-radius:50%;flex:none" onerror="this.src='${_S.LOGO}'">
                <span style="min-width:0;overflow:hidden;text-overflow:ellipsis"><b>${enc(nm)}</b> <span class="muted small">${enc(u.npub.slice(0,14))}…</span></span></td>
              <td style="padding:6px;text-align:right">${u.count}</td>
              <td style="padding:6px;text-align:right"><b>${_fmtBytes(u.size)}</b></td>
              <td style="padding:6px;text-align:right"><span class="muted small">review ›</span></td>
            </tr>`; }).join('')}</tbody>
        </table></div>`;
      $$('th[data-s]',pane).forEach(th=> th.onclick=()=>{ const k2=th.dataset.s;
        _S._badmSort = { k:k2, d: _S._badmSort.k===k2 ? -_S._badmSort.d : (k2==='name'?1:-1) }; draw(); });
      $$('.badm-prof',pane).forEach(el=> el.onclick=(e)=>{ e.stopPropagation(); renderProfileView(el.dataset.pk); });
      $$('.badm-row',pane).forEach(el=> el.onclick=()=>{ _S._filesAdminPk=el.dataset.pk; renderBlossom(); });
    };
    draw();
  }
  // Admin drill-in: a moderation grid of ONE user's public blobs from THIS node's built-in Blossom server
  // (CFG.blossom_url — the SAME store the usage overview is computed from, not the admin's own configured
  // mediaServer()). Tiles load DOWNSCALED ?thumb=1 previews and link to the full blob in a new tab; reuses
  // the existing purge for deletion.
  async function renderBlossomAdminUser(pane, pk){
    const server=(_S.CFG.blossom_url||mediaServer()||'').replace(/\/$/,'');
    const p=Store.profile(pk)||{}; const nm=p.name||p.display_name||(NT().nip19.npubEncode(pk).slice(0,14)+'…');
    pane.innerHTML=`<div class="row" style="align-items:center;gap:8px;padding:6px 4px">
        <button class="btn btn-ghost small" id="badm-back">‹ Back</button>
        <img src="${enc(p.picture||_S.LOGO)}" class="badm-prof" data-pk="${pk}" style="width:28px;height:28px;border-radius:50%;cursor:pointer" onerror="this.src='${_S.LOGO}'">
        <b class="badm-prof" data-pk="${pk}" style="cursor:pointer">${enc(nm)}</b>
        <span style="flex:1"></span>
        <button class="btn small danger" id="badm-purge"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Purge all</button>
      </div><div class="files-grid" id="badm-grid"><div class="spinner"></div></div>`;
    $('#badm-back',pane).onclick=()=>{ _S._filesAdminPk=null; renderBlossom(); };
    $$('.badm-prof',pane).forEach(el=> el.onclick=()=>renderProfileView(el.dataset.pk));
    // Purge reuses the existing flow; on Cancel it returns without deleting — re-render in place (keep
    // _filesAdminPk) so a cancel doesn't bounce the admin out of the drill-in.
    $('#badm-purge',pane).onclick=async()=>{ await doPurgeBlossom(pk); renderBlossom(); };
    const g=$('#badm-grid',pane);
    if(!server){ g.innerHTML='<div class="empty">Blossom server not configured.</div>'; return; }
    let list=null;
    try{ const r=await fetch(server+'/list/'+pk); if(r.ok) list=await r.json(); }catch(_){}
    if(!list){ g.innerHTML='<div class="empty">Couldn’t load this user’s files.</div>'; return; }
    if(!list.length){ g.innerHTML='<div class="empty">No files.</div>'; return; }
    g.innerHTML = list.map(b=>{
      const full=server+'/'+b.sha256, thumb=thumbUrl(full), t=(b.type||'').toLowerCase(), sz=_fmtBytes(b.size||0);
      const isImg=t.startsWith('image/'), isVid=t.startsWith('video/');
      let inner;
      if(isImg||isVid) inner=`<img src="${enc(thumb)}" loading="lazy" style="width:100%;height:100%;object-fit:cover" onerror="this.style.display='none'">`+(isVid?`<span style="position:absolute;top:4px;left:4px;font-size:14px;text-shadow:0 0 3px #000">▶</span>`:'');
      else inner=`<div style="display:flex;align-items:center;justify-content:center;height:100%;font-size:24px">📄</div>`;
      return `<a href="${enc(full)}" target="_blank" rel="noopener" title="${enc(t)} · ${sz}" style="position:relative;aspect-ratio:1;display:block;border-radius:8px;overflow:hidden;background:var(--panel,#16161c);border:1px solid var(--border,#333)">${inner}<span style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.6);color:#fff;font-size:10px;padding:1px 4px">${sz}</span></a>`;
    }).join('');
  }
  /* ONE details row, for both sources. The drive's blobs and a synced folder's manifest entries have
   * nothing in common as data — one is addressed by hash and the other by path — but as a ROW they
   * are the same four columns, so they are drawn by the same function. Two copies of this template
   * would drift, and the way they drift is the header lining up with one of them and not the other.
   *
   * Every class a handler selects on is passed in by the caller (`acts`, `box`) rather than decided
   * here: this function knows about layout and nothing else.
   * Extracted by name in scripts/check_files_explorer.py, which lays real rows out under the real
   * stylesheet — keep it free of dependencies beyond enc(). */
  function _fxDetailsRow(o){
    const cls = 'file-card row' + (o.enc ? ' enc' : '') + (o.dir ? ' isdir' : '') + (o.selected ? ' selected' : '');
    const attrs = (o.sha ? ` data-sha="${enc(o.sha)}"` : '') + (o.dir ? ` data-dir="${enc(o.name)}"` : '')
                + (o.draggable ? ' draggable="true"' : '');
    const inner = `<span class="fx-ic"${o.thumb || ''}>${o.icon || '📎'}</span>`
                + `<span class="fname" title="${enc(o.title || o.name)}">${enc(o.name)}</span>`;
    /* `o.data` is the file's whole dataset, verbatim, so the row's LINK can be opened the same way
     * the tile's is. It used to carry only a sha and a mime and lean on an Open button beside it;
     * clicking the name is the door now, and a door needs the name and the url too. */
    const name = o.href
      ? `<a href="${enc(o.href)}" class="fx-name${o.encOpen ? ' enc-open' : ''}"${o.sha && o.encOpen ? ` data-sha="${enc(o.sha)}"` : ''}${o.mime !== undefined ? ` data-mime="${enc(o.mime||'')}"` : ''}${o.data || ''}${o.encOpen ? '' : ' target="_blank"'}>${inner}</a>`
      : `<span class="fx-name">${inner}</span>`;
    return `<div class="${cls}"${attrs}>${o.box || ''}${name}`
      + `<span class="fx-size">${enc(o.size)}</span>`
      + `<span class="fx-type">${enc(o.type)}</span>`
      + `<span class="fx-mod">${enc(o.when)}</span>`
      + `<span class="fc-acts">${o.acts || ''}</span>`
      + (o.acts ? `<details class="fx-mobile-actions"><summary aria-label="File actions"><span class="fx-more-dots" aria-hidden="true"></span></summary><span class="fc-acts">${o.acts}</span></details>` : '')
      + `</div>`;
  }
  function _fxBindCols(grid){
    $$('.fx-col', grid).forEach(b => b.onclick = () => { _fxSetSort(b.dataset.sort); renderBlossom(); });
  }
  /* Breadcrumbs + the tiles/details switch. A crumb's target is a string the one handler below can
   * parse: `b:<folder>` for the drive, `s:<pairkey>/<subdir>` for a synced folder — one place that
   * knows how to get anywhere, rather than a click handler per crumb. */
  /* `canBack` is PASSED, not read from module state. This function is lifted out of app.js by name
   * and evaluated on its own by scripts/check_files_explorer.py — that is what stops the check
   * measuring a copy of the markup that has drifted from the real one — so it must not close over
   * anything that only exists at runtime. Reading `_fxHist` here made the whole check SKIP with
   * "the page never rendered", which is a check that cannot run rather than one that passes. */
  function _fxBarHTML(crumbs, canBack, canNewFolder){
    const v = _fxView();
    const s = _fxSort();
    /* BACK AND UP — the two controls every file manager has and this one did not.
     *
     * Browsing was breadcrumbs only, which can go UP but never BACK: walking into a folder, into a
     * synced folder, then wanting the previous place meant re-reading the trail and picking the
     * right crumb. `Up` is the last crumb's parent (disabled at the root, rather than absent, so
     * the toolbar does not reflow as you navigate); `Back` is a history this pane keeps for itself,
     * because the app's own history is view-level and a folder is not a view. */
    const up = crumbs.length > 1 ? crumbs[crumbs.length - 2].to : '';
    return `<div class="fx-bar">
      <div class="fx-nav">
        <button class="fx-nb fx-locations-open" id="fx-locations-open" title="Locations" aria-label="Locations" aria-controls="fx-locations-panel" aria-expanded="false"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg></button>
        <button class="fx-nb" id="fx-back" title="Back" aria-label="Back"${canBack ? '' : ' disabled'}>‹</button>
        <button class="fx-nb" id="fx-up" title="Up one folder" aria-label="Up one folder"${up || crumbs.length > 1 ? '' : ' disabled'} data-to="${enc(up)}">↑</button>
      </div><nav class="fx-crumbs">${crumbs.map((c, i) => (i ? '<span class="fx-sep">›</span>' : '')
        + `<button class="fx-crumb${i === crumbs.length-1 ? ' on' : ''}" data-crumb="${enc(c.to)}"`
        + `${i === crumbs.length-1 ? ' disabled' : ''}>${enc(c.label)}</button>`).join('')}</nav>
      <input class="input fx-find" id="fx-find" type="search" autocomplete="off"
             placeholder="🔍 Search files" aria-label="Search files" value="${enc(_S._filesQ)}">
      <div class="fx-views">
        ${canNewFolder ? `<button class="fx-newfolder" id="bl-newfolder" title="New folder"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg><span>New folder</span></button>` : ''}
        ${canNewFolder ? `<button class="fx-newfolder" id="bl-newdoc" title="New document"><svg class="ic b-ic" aria-hidden="true"><use href="#i-note"></use></svg><span>New document</span></button>` : ''}
        <label class="fx-sort-wrap"><span>Sort by</span><select class="fx-sort" id="fx-sort" aria-label="Sort by">${_FX_COLS.map(([k,l])=>`<option value="${k}"${s.by===k?' selected':''}>${l}</option>`).join('')}</select></label>
        <button class="fx-sort-dir" id="fx-sort-dir" title="Reverse sort" aria-label="Reverse sort">${s.dir===1?'▲':'▼'}</button>
        <button class="fx-vw${v==='tiles'?' on':''}" data-view="tiles" title="Tiles" aria-label="Tiles"><svg class="ic b-ic" aria-hidden="true"><use href="#i-grid"></use></svg></button>
        <button class="fx-vw${v==='details'?' on':''}" data-view="details" title="Details" aria-label="Details"><svg class="ic b-ic" aria-hidden="true"><use href="#i-bars"></use></svg></button>
      </div></div>`;
  }
  function _fxBindBar(pane){
    { const open=$('#fx-locations-open',pane), explorer=open&&open.closest('.fx-explorer');
      if(open && explorer) open.onclick=()=>{
        explorer.classList.add('fx-locations-on'); open.setAttribute('aria-expanded','true');
        const close=$('#fx-locations-close',explorer); if(close) close.focus();
      }; }
    /* SEARCH. The drive outgrew browsing the moment Folder Sync started filing thousands of files
     * into it, and a folder is only findable if you remember which one you put it in.
     *
     * It matches on the NAME, which is the one thing both kinds of file have: a plain blob's name
     * comes from the files index, and an ENCRYPTED one's is in that index too — the ciphertext on
     * the server has no name at all, so a server-side search could never see them. That is why this
     * filters the already-decrypted index in the client rather than asking Blossom to do it.
     *
     * The value is re-read from the input on every keystroke and the LIST alone is redrawn: a full
     * renderBlossom() would rebuild the toolbar and take the caret with it. */
    { const f = $('#fx-find', pane);
      if(f){
        /* Repaint through renderBlossom(), not _renderFilesGrid directly.
         *
         * Two reasons, both of which made the first version simply not work. _renderFilesGrid
         * dereferences its `list` immediately, so passing null threw a TypeError out of the input
         * handler on EVERY keystroke and the box was dead. And this same toolbar is mounted inside a
         * SYNCED folder, whose listing comes from a manifest and a different renderer — calling the
         * blob grid there would have replaced the folder's contents with Blossom drive blobs while
         * the breadcrumbs still named the folder.
         *
         * renderBlossom() picks the right renderer and already has the list. It rebuilds the
         * toolbar, so the caret is restored explicitly rather than by avoiding the redraw. */
        let _findT = null;
        f.oninput = () => {
          _S._filesQ = f.value;
          _filesSelClear();          // never leave a selection pointing at rows the query hid
          clearTimeout(_findT);
          // Coalesced: a keystroke on a big drive re-filters thousands of entries, and doing that
          // per character is what makes a search box feel heavy.
          _findT = setTimeout(async () => {
            /* Re-filter the list we ALREADY have. renderBlossom() re-fetches the whole Blossom
             * /list — thousands of entries over the network — so debouncing it still meant a fetch
             * per word typed, which is what "it searches every time I write a word" is. The blob
             * list does not change because somebody typed; only which of it is shown does.
             *
             * A SYNCED folder still goes the long way: its listing comes from a manifest and a
             * different renderer, and there is no cached array here to re-filter. */
            const g = document.getElementById('bl-grid');
            /* A query belongs to FILE MANAGER, not to the location that happened to be open when
             * it was typed.  Always take the full render path while searching so Blossom, synced
             * folders and this computer are aggregated into one result list. */
            if(_S._filesQ.trim()){
              const at = f.selectionStart;
              await renderBlossom();
              const nf = document.getElementById('fx-find');
              if(nf){ nf.focus(); try{ nf.setSelectionRange(at, at); }catch(_){} }
              return;
            }
            // Re-filter what we already have, through the SAME renderer renderBlossom would pick —
            // a Music folder drawn by _renderFilesGrid is a different screen, not a filtered one.
            if(!_S._syncRoot && g && _S._filesGridList){
              if(_S._filesFolder === 'Music') _renderMusicList(g, _S._filesGridList, _S._filesQ);
              else _renderFilesGrid(g, _S._filesGridList);
              return;
            }
            const at = f.selectionStart;
            await renderBlossom();
            const nf = document.getElementById('fx-find');
            if(nf){ nf.focus(); try{ nf.setSelectionRange(at, at); }catch(_){} }
          }, 220);
        };
        // Escape clears rather than closing anything — there is nothing to close, and a search box
        // you cannot empty without selecting the text is a small daily annoyance.
        f.onkeydown = e => { if(e.key === 'Escape'){ e.stopPropagation(); f.value=''; f.oninput(); } };
      } }
    $$('.fx-vw', pane).forEach(b => b.onclick = () => {
      ClientSettings.set('filesView', b.dataset.view);
      /* The drive landing page is a folder dashboard, so tiles/details cannot change its shape.
       * Leaving the controls live there made both buttons appear broken on a phone. A view choice
       * now means "show my files this way": enter All files first, then paint the selected view. */
      if(!_S._hostOn && !_S._syncRoot && _S._filesFolder === null) _S._filesFolder = '';
      renderBlossom();
    });
    { const sort=$('#fx-sort',pane); if(sort) sort.onchange=()=>{
        const cur=_fxSort();
        ClientSettings.set('filesSort',{by:sort.value,dir:(sort.value==='name'||sort.value==='type')?1:-1});
        if(cur.by===sort.value) ClientSettings.set('filesSort',cur);
        renderBlossom();
      };
      const dir=$('#fx-sort-dir',pane); if(dir) dir.onclick=()=>{ const cur=_fxSort();
        ClientSettings.set('filesSort',{by:cur.by,dir:-cur.dir}); renderBlossom(); }; }
    { const nf=$('#bl-newfolder',pane); if(nf) nf.onclick=_newFolderModal; }
    { const nd=$('#bl-newdoc',pane); if(nd) nd.onclick=_newDocumentModal; }
    /* ONE ROUTER FOR EVERY WAY OF MOVING. A crumb, Up and Back all mean "go to this place", and the
     * place is one of three sources (drive folder, synced folder, this computer). A second copy of
     * this switch is how two of them start disagreeing about what `up` means. */
    const _fxGo = (to, remember) => {
      to = to || '';
      if(remember !== false){ const here = _fxWhere(); if(here !== to) _fxHist.push(here); }
      if(_fxHist.length > 40) _fxHist.shift();
      _fxRoute(to);
      renderBlossom();
    };
    { const bb = $('#fx-back', pane);
      if(bb) bb.onclick = () => { const to = _fxHist.pop(); if(to == null) return; _fxRoute(to); renderBlossom(); }; }
    { const ub = $('#fx-up', pane);
      if(ub) ub.onclick = () => _fxGo(ub.dataset.to || ''); }
    $$('.fx-crumb[data-crumb]', pane).forEach(b => b.onclick = () => _fxGo(b.dataset.crumb || ''));
    /* WHERE a `to` string points, applied. The three prefixes are the three sources Files shows;
     * `f:` (or anything else) is a drive folder, which is why the fallback is not an error. */
    function _fxRoute(to){
      to = to || '';
      if(to.charAt(0) === 'h'){
        /* `h:<abs path>` — THIS COMPUTER. The host source shares this toolbar, so it has to share
         * its crumb router; a second one is how two breadcrumb trails start disagreeing about what
         * "up" means. */
        const H3 = _hostFs(); if(H3) H3.enter(to.slice(2));
        _S._hostOn = true; _S._filesTab = 'computer'; _S._fxMobileSource = 'computer';
      }
      else if(to.charAt(0) === 's'){ const rest = to.slice(2); const cut = rest.indexOf('/');
        _S._syncRoot = cut < 0 ? rest : rest.slice(0, cut); _S._syncPath = cut < 0 ? '' : rest.slice(cut+1);
        _syncSel.clear(); _syncSelOn = false; _S._fxMobileSource = 'synced'; }
      else { _S._syncRoot = ''; _S._syncPath = ''; _S._filesFolder = to.slice(2); _S._hostOn = false; _S._fxMobileSource = 'blossom'; }
    }
  }
  const _fxMatch = (nm) => { const q=_S._filesQ.trim().toLowerCase();
    return !q || String(nm||'').toLowerCase().includes(q); };
  const _FILES_PAGE = 60;
  let _filesShown = _FILES_PAGE, _filesShownFolder = null;
  let _fxSearchSeq = 0;
  /* GOING TO A FOLDER IS ONE ACTION, AND IT WAS WRITTEN TWICE.
   *
   * The sidebar chip did `_fxRemember(); …; _fxMobileSource='blossom'; _filesFolder=…` and the HOME
   * TILE did the same thing minus those two. `_fxMobileSource` is what puts `mobile-on` on a pane,
   * i.e. WHICH PANE IS VISIBLE on a narrow layout — so the tile moved the state and left the screen
   * showing home. Reported as "once I am in home, I can't click to any other folder from Blossom or
   * Synced folders", and on a phone that is the whole file manager: nothing happens, nothing logs,
   * and the app looks frozen. `_fxRemember` was missing too, so Back had nothing to return to.
   *
   * Two call sites for one action drift the moment either is edited. There is one now. */
  function _fxOpenFolder(name){
    _fxRemember(); _S._syncRoot=''; _S._syncPath=''; _S._hostOn=false;
    _S._filesTab='public'; _S._fxMobileSource='blossom'; _S._filesFolder=name; renderBlossom();
  }
  function _fxOpenSynced(key){
    _fxRemember(); _S._syncRoot=key; _S._syncPath=''; _S._hostOn=false;
    _S._filesTab='public'; _S._fxMobileSource='synced'; renderBlossom();
  }
  function _fxOpenComputer(){
    _fxRemember(); _S._fxMobileSource='computer'; _openHostFiles();
  }
  let _fxBlossomOpen = localStorage.getItem('pc.files.tree.blossom') !== '0';
  let _fxSyncedOpen = localStorage.getItem('pc.files.tree.synced') !== '0';
  let _fxComputerOpen = localStorage.getItem('pc.files.tree.computer') !== '0';
  /* ONE opener for both surfaces. The sidebar chip and the home-screen tile are two ways to the
   * same place, and two copies of "where does this start" is how they end up starting somewhere
   * different. */
  function _openHostFiles(goHome=false){
    _S._syncRoot=''; _S._syncPath=''; _S._filesFolder=null; _S._hostOn=true; _S._filesTab='computer'; _S._fxMobileSource='computer';
    const H2 = _hostFs();
    /* The source heading resumes the last folder, but the child explicitly labelled Home must
     * actually return home. Previously both called the same preserve-path branch, leaving a child
     * folder on screen while the tree claimed Home was selected. Wait for root discovery before
     * painting so an explicit Home click cannot briefly repaint that stale child. */
    if(H2 && (goHome || !H2.at())){
      H2.roots().then(rs => {
        const home = (rs || []).find(x => x.kind === 'home') || (rs || [])[0];
        H2.enter(home ? home.path : '/');
        if(_S.VIEW==='blossom' && _S._filesTab==='computer' && _S._hostOn) renderBlossom();
      }, () => { if(_S.VIEW==='blossom' && _S._filesTab==='computer' && _S._hostOn) renderBlossom(); });
      return;
    }
    renderBlossom();
  }
  /* WHERE FILES HAS BEEN, for the Back button. Its own stack rather than the app's history: that
   * one is view-level, and walking between folders never leaves the Files view — pushing a history
   * entry per folder would make the app's Back walk folders instead of screens, which is the same
   * mistake the thread view made. Bounded, because a long browse is not a thing to keep for ever. */
  let _fxHist = [];
  /* Push where we are, unless we are already there. Called by every navigation; the crumb/Up router
   * does it itself so the two cannot double-push. */
  function _fxRemember(){
    try{ const here = _fxWhere(); if(_fxHist[_fxHist.length-1] !== here) _fxHist.push(here);
         if(_fxHist.length > 40) _fxHist.shift(); }catch(_){}
  }
  /* The current location as a `to` string, in the same grammar the crumbs use. */
  function _fxWhere(){
    if(_S._hostOn){ const H = _hostFs(); return 'h:' + ((H && H.at()) || ''); }
    if(_S._syncRoot) return 's:' + _S._syncRoot + (_S._syncPath ? '/' + _S._syncPath : '');
    return 'f:' + (_S._filesFolder || '');
  }
  /* The engine's own name for the trash, in the scope that RENDERS it. app.js is several closures,
   * not one: declared in the Files-sidebar closure further down it was not defined here at all, and
   * the symptom is the folder listing stuck on its spinner for ever — the render is async, so the
   * ReferenceError became a rejected promise and nothing on screen or in any log said a word. */
  const TRASH_DIR = '.pc-trash';
  /* What listTrash last answered for `_syncRoot`, so walking in and out of the trash does not
   * re-read the device every time. Cleared (set to null) by anything that changes what is in there. */
  let _trashCache = null;             // {key, rows:[{at,to,size,mtime}]} or null
  /* Selection for the BULK delete. Keyed on FULL paths, so ticks survive navigating and searching
   * within the folder; cleared when the root changes or the basket is acted on. Built for the
   * conflict-storm cleanup: search "conflict", Select all shown, one delete — the storm published
   * hundreds of copy ENTRIES the source machine never even held, and removing them one confirm at a
   * time is nobody's afternoon. */
  let _syncSel = new Set(), _syncSelOn = false;
  let _syncPairs = null;              // [{key,n,updated_at}] · null = not asked yet · 'error' = asked and failed
  const _syncManifests = new Map();   // pair key -> {at, paths:{path:{sha,size,mtime,deletedAt}}}
  const _SYNC_TTL = 60000;   // how long a decrypted manifest is reused while walking its subfolders

  /* Draw what this device already knows without asking the signer. A package update exposed a bad
   * split: sync.js had the device-local mappings (and often an already-fetched account list), while
   * Files kept a second null cache and therefore showed NO synced folders until its own Load button
   * was pressed. The folders were never gone; the file manager declined to read the cache beside it.
   * Local mappings win only as presence information; an account result supplies the real counts. */
  function _adoptSyncPairs(){
    const S = window.PCSync;
    if(!S) return false;
    const before = JSON.stringify(_syncPairs);
    let remote = null, local = [];
    try{ remote = S.acct && S.acct(); }catch(_){}
    try{ local = (S.folders && S.folders() || []).map(f => ({ key:String(f.key || f.name || '').trim(), n:null, local:true })).filter(f=>f.key); }catch(_){}
    if(Array.isArray(remote)){
      const by = new Map(remote.filter(f=>f&&f.key).map(f=>[String(f.key),Object.assign({},f)]));
      local.forEach(f=>{ if(!by.has(f.key)) by.set(f.key,f); });
      _syncPairs = [...by.values()];
    }else if(local.length){
      _syncPairs = local;                 // never hide a folder mapped on this very device
    }else if(remote === 'error') _syncPairs = 'error';
    return before !== JSON.stringify(_syncPairs);
  }
  if(!window.__pcFilesSyncFoldersBound){
    window.__pcFilesSyncFoldersBound = true;
    window.addEventListener('pc:sync-folders', () => {
      if(_adoptSyncPairs() && _S.VIEW==='blossom' && _S._filesTab==='public') renderBlossom();
    });
  }

  /* Which folders this ACCOUNT syncs. The fetch, the TTL and the cache all live in sync.js — the
   * Folder Sync screen asks the same question to offer an unmapped folder back to a device that lost
   * its mapping, and two caches would mean two answers and two requests per visit. A 503 there means
   * the relay did not answer, which is NOT "you have no synced folders": that distinction is made on
   * the server and carried through here as 'error'. */
  async function _ensureSyncPairs(){
    // No instance means no manifest endpoint at all — the bundled desktop/APK build runs against
    // relays alone, and asking would 404 on every render.
    if(_standalone() || !_S.ME || !_S.ME.pubkey) { _syncPairs = []; return false; }
    const S = window.PCSync;
    if(!S || !S.accountFolders){ _syncPairs = []; return false; }
    const changed = await S.accountFolders();
    /* NULL STAYS NULL. This mapped it to [], which is the one value that means "you sync nothing"
     * — and `_fxSyncedHTML` renders that as NOTHING AT ALL, so the whole section vanished while the
     * answer was still on its way, indistinguishable from having no synced folders. Its "looking…"
     * branch could never fire, because the only value that would reach it was being erased here.
     *
     * Whose problem that is depends entirely on the SIGNER, which is why it read as an Amber bug:
     * `accountFolders` awaits `PC.signAuth('sync-folders')`, and with a local key or a NIP-07
     * extension that resolves in a millisecond, so the empty window is too short to see. Over NIP-46
     * it is a round trip to a phone that has to display a prompt and be tapped — seconds at best,
     * and forever if the notification is missed. Same code, same account, and Files showed synced
     * folders with the extension and none with Amber. Nothing here times out on its own: the signer's
     * own 120s ceiling rejects, `accountFolders` catches it into 'error', and the section then says
     * "couldn't be loaded just now" — which is the honest end state, and was also unreachable. */
    _syncPairs = S.acct();
    _adoptSyncPairs();
    return changed;
  }
  function _fxSyncedHTML(){
    if(_standalone()) return '';
    const p = _syncPairs;
    let body = '';
    if(p === 'error') body = '<div class="muted small fx-secnote">Couldn’t be loaded just now</div>';
    else if(!Array.isArray(p)) body = '<button class="btn btn-ghost small" data-load-sync-folders>Load synced folders</button>'
      + '<div class="muted small fx-secnote">Uses your signer only when you ask.</div>';
    else if(!p.length) body = '<div class="muted small fx-secnote">No synced folders</div>';
    else body = p.map(f =>
      `<span class="fx-syncwrap"><button class="folder-chip syncroot${_S._syncRoot===f.key?' active':''}" data-synckey="${enc(f.key)}"
         title="${enc(f.key)}${Number.isFinite(f.n)?` — ${f.n} file${f.n===1?'':'s'}`:' — synced on this device'}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg>${enc(f.key)}${Number.isFinite(f.n)?`<span class="fx-n">${f.n}</span>`:''}</button>`
      /* Removing a folder from every device leaves its shared record behind for ever — keyed on the
         NAME, so the pair goes on existing with all its history and any device that pairs that name
         later inherits it. There was no way to clear it from anywhere in the app. */
      + `<button class="fx-syncx" data-syncforget="${enc(f.key)}" title="Forget “${enc(f.key)}” — remove this folder's shared record">✕</button></span>`).join('')
      + _fxDeletedHTML();
    return `<section class="fx-tree-node"><button class="fx-tree-head${_S._syncRoot?' active':''}${_S._fxMobileSource==='synced'?' mobile-on':''}" data-fxtoggle="synced" aria-expanded="${_fxSyncedOpen?'true':'false'}"><span class="chev">${_fxSyncedOpen?'▾':'▸'}</span><svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg><b>Synced Folders</b></button><div class="fx-tree-children${_fxSyncedOpen?'':' hidden'}" data-fxtree="synced">${body}</div></section>`;
  }
  // The manifest for one pair, decrypted by sync.js's store (NIP-44 to the user's own key — this
  // node cannot read it, and neither can this code without the signer).
  async function _syncManifest(key, force){
    // Cached only briefly: walking in and out of subdirectories must not re-read and re-decrypt the
    // whole manifest each time, but a folder that finished syncing while this screen was open should
    // not stay wrong until reload either.
    const hit = _syncManifests.get(key);
    if(!force && hit && (Date.now() - hit.at) < _SYNC_TTL) return hit.paths;
    const S = window.PCSync;
    if(!S || !S.docs || !S.docs.state) throw new Error('folder sync is not loaded on this build');
    /* ONE RECORD PER FILE. The record set IS the folder — no per-device views, no merge; the
     * transport throws rather than answering {} when the server cannot be asked. */
    const got = await S.docs.state(key);
    const paths = (got && got.state) || {};
    _syncManifests.set(key, { at: Date.now(), paths });
    return paths;
  }
  /* One directory's worth of a manifest — the immediate children of `dir` only. The manifest is a
   * flat map of full paths, so the folders are implied by them and have to be re-derived here; a
   * directory's size and date are the sum and the newest of what is inside it, which is what a file
   * manager shows and what makes the columns meaningful on a folder row. */
  /* EVERY LIVE PATH UNDER `dir`, AT ANY DEPTH — what a search actually means.
   *
   * `_syncEntries` answers "what is IN this folder", which is right for browsing and wrong for
   * searching: a synced folder files thousands of paths into a tree, and a search that only looks at
   * the current directory answers "no results" for a file that is plainly there. Reported exactly
   * that way — searching a folder root for "conflict" while the copies sat in sub-folders.
   *
   * Each hit carries the directory it lives in, because a result you cannot locate is half an
   * answer. */
  function _syncSearch(paths, dir, match){
    const pre = dir ? dir + '/' : '';
    const out = [];
    for(const p in paths){
      const e = paths[p];
      if(!e || e.deletedAt) continue;
      if(pre && p.indexOf(pre) !== 0) continue;
      const rest = p.slice(pre.length);
      /* THE TRASH IS EXCLUDED AT ANY DEPTH, not just at the top.
       *
       * Browsing only ever looks one level down, so checking the first segment is enough there. A
       * SEARCH walks the whole subtree, and `.pc-trash/<date>/…` holds the deleted copies — offering
       * them as results makes a deleted file look present, and re-adding one from there is exactly
       * how a deletion gets undone. */
      if(!rest || ('/' + rest + '/').indexOf('/.pc-trash/') >= 0) continue;
      const cut = rest.lastIndexOf('/');
      const name = cut < 0 ? rest : rest.slice(cut + 1);
      if(!match(name)) continue;
      out.push({ path: p, name, where: cut < 0 ? '' : rest.slice(0, cut),
                 size: +e.size || 0, mtime: +e.mtime || 0, sha: e.sha, chunks: e.chunks });
    }
    return out;
  }
  function _syncEntries(paths, dir){
    const pre = dir ? dir + '/' : '';
    const dirs = new Map(), files = [];
    for(const p in paths){
      const e = paths[p];
      if(!e || e.deletedAt) continue;                       // a tombstone is not a file
      if(pre && p.indexOf(pre) !== 0) continue;
      const rest = p.slice(pre.length);
      if(!rest || rest.split('/')[0] === '.pc-trash') continue;   // the folder's own trash is not content
      const cut = rest.indexOf('/');
      // `chunks` travels with the row: a file stored in pieces has NO `sha` of its own, and a screen
      // that only knows about `sha` offers a Download button that can never work (it is also what
      // every file over 16 MB looks like — the common case, not the exotic one).
      if(cut < 0){ files.push({ path:p, name:rest, size:+e.size||0, mtime:+e.mtime||0, sha:e.sha,
                                chunks:e.chunks }); continue; }
      const nm = rest.slice(0, cut);
      const d = dirs.get(nm) || { name:nm, dir:true, n:0, size:0, mtime:0 };
      d.n++; d.size += +e.size || 0; d.mtime = Math.max(d.mtime, +e.mtime || 0);
      dirs.set(nm, d);
    }
    return { dirs:[...dirs.values()], files };
  }
  function _syncSortKey(it, by){
    if(by === 'size') return it.size || 0;
    if(by === 'modified') return it.mtime || 0;
    if(by === 'type') return it.dir ? '' : ((String(it.name).match(/\.([A-Za-z0-9]{1,8})$/) || [])[1] || '');
    return String(it.name || '').toLowerCase();
  }
  /* ---- IS MY DRIVE ACTUALLY THERE? ------------------------------------------------------------
   *
   * The Files grid draws what the INDEX says you have. The server holds what is actually stored. Most
   * of the time those agree, and when they do not, nothing says so: a file whose bytes are gone looks
   * completely normal until the day somebody opens it.
   *
   * So this compares them, and reports:
   *   · entries whose bytes the server no longer holds — the ones that will fail when opened;
   *   · entries this device cannot decrypt, which is a KEY problem and not a storage one, and the
   *     fix for each is the other's mistake;
   *   · how much is stored that your index does not name — reported, never offered for deletion,
   *     because folder sync, the manifests and the music library keep their own records and none of
   *     them appear in this index. An "orphan" here is usually somebody else's bookkeeping.
   *
   * READ-ONLY. The one action it offers is clearing INDEX entries whose bytes are gone, which
   * deletes nothing from the server — there is nothing there to delete.
   */
  /* IS THIS BLOB THERE? — asked properly, which `_blobAlreadyStored` does NOT do.
   *
   * That one answers a different question: "may I skip this upload?" — and it deliberately says NO
   * for a blob that is present but carries an expiry stamp, because re-uploading is what clears the
   * stamp. Correct for an upload, and completely wrong as evidence of loss: used here it reports a
   * file that is sitting on the server as gone, and this drives a repair that removes index entries
   * on every device for ninety days.
   *
   * Three answers, and the middle one is why this exists: `true` there, `false` NOT there, `null` I
   * could not ask. A rate limiter, a 500, a redirect and a dead socket are all `null`.
   */
  /* WHICH OWNED BYTES BELONG TO NOTHING — the set a storage reclaim may touch.
   *
   * Deleting a synced folder clears its RECORDS and leaks every BYTE: the blobs are keep-flagged
   * (age-exempt, deliberately) and no bookkeeping names them any more. Measured: 137.5 GB owned by
   * one account the day both its folder pairs were deleted. This is the missing other half of that
   * delete, and it is deliberately conservative three ways: only `keep` blobs (post media is never
   * keep, so old posts' images are structurally untouchable); only blobs the drive index does not
   * name (music, notes attachments and drive files all live there); only blobs no synced folder
   * references — entries, every chunk, and the sealed path-list blobs the manifests live in.
   * PURE, so the suite runs it under node. */
  function _reclaimableBlobs(list, indexShas, refIds){
    const out = [];
    /* TOO FRESH TO JUDGE IS NOT AN ORPHAN. A first seed uploads bytes continuously and publishes
     * their records in checkpoints, so at any instant a few hundred blobs exist whose records are
     * seconds away — and a drive check run mid-seed read exactly that gap as "belongs to nothing"
     * (measured: 34 in-flight uploads offered for deletion DURING the folder's first sync). An
     * orphan is only an orphan once it has been unreferenced for longer than any sweep can run. */
    const FRESH_MS = 24 * 3600 * 1000;
    const now = Date.now();
    for(const b of (list || [])){
      if(!b || !b.keep || !b.sha256) continue;
      if(indexShas.has(b.sha256)) continue;
      if(refIds.has(b.sha256)) continue;
      const up = (+b.uploaded || 0) * 1000;
      if(up && (now - up) < FRESH_MS){ out.fresh = (out.fresh || 0) + 1; continue; }
      out.push(b);
    }
    return out;
  }
  /* Every blob id any synced folder still references. `null` means "could not read them all" — and
   * a reclaim with a partial reference set is a delete order for whatever the unread folder holds,
   * so the caller must treat null as NO. */
  async function _syncRefIds(){
    try{
      const S = window.PCSync;
      if(!S || !S.acct || !S.accountFolders) return null;
      /* FORCED AND AWAITED. `acct()` legitimately answers null while the folder list's signer
       * round trip is still in flight ("NULL STAYS NULL" — the sidebar needed that), and the first
       * build of this read that null as "no folders", killed the offer, and printed "Nothing to
       * reclaim" over 137 GB of reclaimable bytes. The offer depends on this enumeration, so it
       * waits for a fresh one and treats anything but a real list as "could not read". */
      await S.accountFolders(true);
      const pairs = S.acct();
      if(!Array.isArray(pairs)) return null;
      const ids = new Set();
      for(const p2 of pairs){
        /* Throws when the server cannot be asked — and an unreadable folder means the whole
         * enumeration is unusable, because a reference this could not see is a blob the reclaim
         * would offer to delete. TOMBSTONES COUNT TOO: they keep their addresses so the
         * account-wide Restore can put the file back, and reclaiming those bytes would quietly
         * turn every Restore button into a 404. */
        const got = await S.docs.state(p2.key);
        const st = (got && got.state) || {};
        for(const path in st){
          const e = st[path]; if(!e) continue;
          if(e.sha) ids.add(e.sha);
          if(e.ps) ids.add(e.ps);
          for(const c of (e.chunks || [])) ids.add(c);
        }
      }
      /* Shared-music copies are keep-flagged and in no index — exactly the reclaim set. Unreadable
       * shares, like an unreadable folder, mean no offer at all. */
      {
        // Loaded, never skipped: absent, every shared copy would be offered for deletion.
        const MS = await _musicShareLoad().catch(() => null);
        if(!MS) return null;
        const shared = await MS.refIds();
        if(!shared) return null;
        for(const sh of shared) ids.add(sh);
      }
      return ids;
    }catch(_){ return null; }
  }

  async function _blobPresent(sha){
    try{
      // The same cache-buster as _blobAlreadyStored, for the same reason: a proxy that caches an
      // immutable 200 turns a deleted blob into a phantom the drive check then vouches for.
      const r = await fetch(mediaServer() + '/' + sha + '?probe=' + Date.now(),
                            { method:'HEAD', cache:'no-store' });
      if(r && (r.status === 200 || r.status === 206)) return true;
      if(r && (r.status === 404 || r.status === 410)) return false;
      return null;
    }catch(_){ return null; }
  }

  async function driveCheck(btn){
    const was = btn ? btn.textContent : '';
    if(btn){ btn.disabled = true; btn.textContent = 'checking…'; }
    try{
      /* THE SERVER'S OWN LIST, read fresh. Not `_blobHave`, which is whatever the last screen
       * happened to leave behind — and on the drive home that is often nothing at all, which would
       * make this report every file in the index as missing. */
      let list = null;
      try{
        const r = await fetch(mediaServer() + '/list/' + _S.ME.pubkey, { cache:'no-store' });
        if(r.ok) list = await r.json();
      }catch(_){ }
      if(!Array.isArray(list)){ toast('could not read your drive from the server — nothing was changed'); return; }
      const have = new Set(list.map(b => b.sha256));
      const named = new Set();
      const files = FilesIdx._norm().files;
      const missing = [], undecryptable = [];
      for(const sha in files){
        const m = files[sha] || {};
        named.add(sha);
        if(!have.has(sha)){ missing.push({ sha, name: m.name || sha.slice(0, 8) }); continue; }
        /* An encrypted entry whose key this device does not have is not a missing file. Only the
         * wrapped key is checked — reading every blob to find out would be the whole drive. */
        if(m.enc && !FilesIdx._mkWrapped && !FilesIdx.mk) undecryptable.push(m.name || sha.slice(0, 8));
      }
      /* CONFIRM EACH ONE AGAINST THE SERVER ITSELF before calling it dead.
       *
       * `/list` is one answer from one endpoint, and this repair is not a local one: forget() writes
       * a TOMBSTONE, which strips that sha out of any index this account pulls for the next ninety
       * days, on every device. A list that answers `200 []` — a re-pointed instance, a node whose
       * ownership rows were lost, a proxy with an opinion — would otherwise offer to "clear 4000
       * dead entries" and destroy the names, folders and encrypted-folder membership of a drive
       * whose bytes are all still there.
       *
       * A HEAD per candidate is the second opinion, and it is bounded: only the entries the listing
       * already doubts are asked about, and an unknown answer counts as PRESENT. */
      const reallyGone = [];
      let unsure = 0;
      for(const x of missing.slice(0, 500)){
        const there = await _blobPresent(x.sha);
        if(there === false) reallyGone.push(x);
        else if(there === null) unsure++;
      }
      const unconfirmed = missing.length - Math.min(missing.length, 500);

      let otherBytes = 0, otherN = 0;
      for(const b of list) if(!named.has(b.sha256)){ otherN++; otherBytes += (b.size || 0); }

      /* WHICH SERVER THIS VERDICT IS ABOUT — the missing half of every number below it.
       *
       * `mediaServer()` is ONE current server and the index keeps no per-entry host, so an account
       * that has ever been pointed somewhere else has entries whose bytes are on the OLD host. Both
       * the listing and the HEAD then answer 404 while the file is perfectly safe where it was
       * uploaded, and the report reads as data loss. Naming the server is what makes that possible
       * to notice, and it costs one line. */
      const lines = [`<div><b>${Object.keys(files).length}</b> file(s) in your index · `
                     + `<b>${have.size}</b> stored on the server</div>`,
                     `<div class="dim small">checked against <code>${enc(mediaServer())}</code></div>`];
      if(unsure){
        lines.push(`<div>${unsure} could not be checked a second time — the server did not answer. `
          + `Left alone.</div>`);
      }
      /* Gated on what the SECOND opinion confirmed, not on what the listing doubted. A listing
       * that doubts 500 entries the HEADs then all find present is a listing having a bad minute —
       * rendering "0 entries whose stored copy is gone" over an empty list would be this screen's
       * own version of crying wolf. Say instead that the two answers disagreed. */
      const vindicated = Math.min(missing.length, 500) - reallyGone.length - unsure;
      if(vindicated > 0 && !reallyGone.length){
        lines.push(`<div>${vindicated} entr${vindicated === 1 ? 'y was' : 'ies were'} doubted by the `
          + `server's listing but found present when asked directly. Nothing to do.</div>`);
      }
      if(reallyGone.length || unconfirmed){
        /* SAY WHAT WAS MEASURED, WHICH IS NOT "YOUR FILES ARE GONE".
         *
         * The index is a set of POINTERS at a stored copy. This check asked the server about those
         * pointers, and nothing else — it has never looked at a disk. "497 file(s) the server no
         * longer has" was read, entirely reasonably, as a report about the files themselves, on a
         * drive whose owner could see every one of them sitting on their desktop. The stored copy
         * going away is what actually happened (clearing the store does exactly this, and only the
         * synced folders are re-uploaded afterwards), and the entry that outlives it is a name
         * pointing at nothing. */
        lines.push(`<div class="nt-warn">⚠ <b>${reallyGone.length}</b> entr${reallyGone.length === 1 ? 'y' : 'ies'}`
          + ` whose stored copy is no longer on this server`
          + (unconfirmed ? ` (${unconfirmed} more not checked individually)` : '') + `:`
          + `<div class="muted small">${reallyGone.slice(0, 12).map(x => enc(x.name) + ' <code>' + enc(x.sha.slice(0, 16)) + '</code>').join('<br>')}`
          + (reallyGone.length > 12 ? ` and ${reallyGone.length - 12} more` : '')
          + `</div><div class="muted small" style="margin-top:6px">This is about the copy stored `
          + `here, not about your devices — the files themselves may well still be on them. `
          + `Bytes are addressed by content, so an entry comes back to life by itself if the same `
          + `file is uploaded again from anywhere.</div></div>`);
      }
      if(undecryptable.length){
        lines.push(`<div class="nt-warn">⚠ <b>${undecryptable.length}</b> encrypted file(s) this `
          + `device has no key for. That is a key problem, not a storage one — the bytes are there.</div>`);
      }
      let reclaim = [];
      if(otherN){
        /* Can any of the unnamed bytes be RECLAIMED? Only with the complete reference picture: the
         * drive index (already in hand) plus every synced folder's ids. A null reference set means
         * a folder could not be fully read, and the offer simply does not appear — a reclaim that
         * guesses is the folder-sync wipe wearing a storage hat. */
        /* NEVER OFFER DELETION WHILE A SYNC RUNS. The fresh-blob guard already protects the
         * bytes; this protects the PERSON — a reclaim button during a seed is a decision nobody
         * should be handed mid-flight. */
        if(window.PCSync && PCSync.busyNow && PCSync.busyNow()){
          lines.push(`<div class="muted small">${otherN} blob(s), ${_fxBytes(otherBytes)}, are stored `
            + `but not named by this index. A sync is running — reclaim is disabled until it `
            + `finishes.</div>`);
          r.innerHTML = lines.join('');
          return;
        }
        const refs = await _syncRefIds();
        /* THE INDEX'S OWN CONTAINER IS LOAD-BEARING AND INVISIBLE. The drive index lives in an
         * encrypted blob (`indexSha`) that is keep-flagged, named by no ledger, and deliberately
         * hidden from the grid — i.e. it matches the reclaim set PERFECTLY, and offering it means
         * offering the user their own drive's spine. Every index sha this session has seen is
         * excluded; superseded ones from older sessions are genuinely reclaimable and the server
         * TTL-stamps them anyway. */
        if(refs){
          try{
            for(const sh of (FilesIdx._indexShas || [])) refs.add(sh);
            if(FilesIdx._lastIndexSha) refs.add(FilesIdx._lastIndexSha);
          }catch(_){}
          reclaim = _reclaimableBlobs(list, named, refs);
        }
        const gb = reclaim.reduce((n, b) => n + (b.size || 0), 0);
        /* THREE sentences for three truths, never one for two: an offer, a genuine nothing, and
         * "the folders could not be read so nothing is OFFERED" — which is not a verdict about the
         * bytes. Printing "Nothing to reclaim" for the third hid 137 GB behind a signer blip. */
        lines.push(`<div class="muted small">${otherN} blob(s), ${_fxBytes(otherBytes)}, are stored `
          + `but not named by this index — folder sync and the rest of your account keep their own `
          + `records here.`
          + (reclaim.fresh ? ` ${reclaim.fresh} arrived within the last day and are too fresh to `
              + `judge — a sync may still be publishing their records.` : ``)
          + (reclaim.length
            ? ` <b>${reclaim.length}</b> of them (${_fxBytes(gb)}) belong to nothing any more — `
              + `usually a deleted synced folder's leftovers.`
            : (refs ? ` Nothing to reclaim.`
                    : ` Your synced folders couldn\u2019t be read just now, so nothing is offered `
                      + `for reclaim \u2014 run the check again in a moment.`)) + `</div>`);
        if(reclaim.length){
          lines.push(`<div class="row" style="margin-top:6px"><button class="btn btn-red small" `
            + `id="fx-ck-reclaim">Reclaim ${_fxBytes(gb)} (${reclaim.length} blobs)</button></div>`);
        }
      }
      /* "nothing was found missing" and "nothing could be asked" are not the same sentence —
       * the same rule the admin store scan follows. `unsure` entries were doubted by the listing
       * and the HEAD got no answer, so a clean bill of health over them would be a guess. */
      if(!reallyGone.length && !unconfirmed && !unsure && !undecryptable.length)
        lines.push('<div>Everything checks out.</div>');

      modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Drive check</h3>`
        + lines.join('')
        + `<div class="row" style="margin-top:12px">`
        + (reallyGone.length ? `<button class="btn btn-red small" id="fx-ck-clear">Clear ${reallyGone.length} dead entr${reallyGone.length===1?'y':'ies'}</button>` : '')
        + `<button class="btn btn-ghost small" id="fx-ck-history">Review retained folder lists…</button></div>`,
        r => {
          const hist = $('#fx-ck-history', r);
          if(hist) hist.onclick = () => _showFilesHistory(hist);
          const rc = $('#fx-ck-reclaim', r);
          if(rc) rc.onclick = async () => {
            const gb = reclaim.reduce((n, b) => n + (b.size || 0), 0);
            if(!await uiConfirm('Delete ' + reclaim.length + ' stored blob'
              + (reclaim.length === 1 ? '' : 's') + ' (' + _fxBytes(gb) + ')?\n\nThese bytes belong '
              + 'to no synced folder and no file in your drive \u2014 typically what a deleted '
              + 'synced folder left behind. Re-adding a folder later simply re-uploads what it '
              + 'needs.', { ok:'Delete ' + _fxBytes(gb), danger:true })) return;
            rc.disabled = true;
            let done = 0, failedN = 0;
            for(const b of reclaim){
              rc.textContent = 'reclaiming\u2026 ' + (++done) + '/' + reclaim.length;
              try{ if(!await deleteBlobQuiet(b.sha256)) failedN++; }catch(_){ failedN++; }
            }
            toast(failedN ? ('reclaimed ' + (reclaim.length - failedN) + ' \u2014 ' + failedN
                             + ' could not be deleted; run the check again')
                          : ('reclaimed ' + _fxBytes(gb)));
            closeModal();
          };
          const cl = $('#fx-ck-clear', r);
          if(cl) cl.onclick = async () => {
            /* A SHORT LIST IS A DELETE ORDER — the same rule the phone book and folder sync use, and
             * for a stronger reason here: forget() TOMBSTONES the sha, so this is not a local tidy,
             * it strips those entries out of every device's index for ninety days. Clearing more
             * than survives means the drive is not missing files, the LISTING is wrong. */
            const keep = Object.keys(FilesIdx._norm().files).length - reallyGone.length;
            if(reallyGone.length >= 20 && reallyGone.length > keep){
              toast('refused: that would clear ' + reallyGone.length + ' of '
                    + (keep + reallyGone.length) + ' entries — the listing is more likely wrong '
                    + 'than your drive');
              return;
            }
            if(!await uiConfirm(`Clear ${reallyGone.length} entr${reallyGone.length===1?'y':'ies'} `
                + `from your file list?\n\nThe bytes are already gone from the server — this only `
                + `removes the names. It cannot be undone, and it applies to every device.`)) return;
            cl.disabled = true;
            /* INDEX ONLY. There is nothing on the server to delete — that is what "missing" means —
             * so this is a bookkeeping repair, and it is batched for the same reason every other
             * bulk edit here is: one save, not one per entry. */
            FilesIdx.beginBatch();
            try{ for(const x of reallyGone){ try{ FilesIdx.forget(x.sha); }catch(_){} } }
            finally{
              const saved = await FilesIdx.endBatch();
              /* The entries are already out of the index and tombstoned by here, and `_retryLater`
               * is armed — so "unchanged" would be the opposite of the truth, exactly as it was for
               * the music delete. */
              toast(saved ? `cleared ${reallyGone.length} dead entr${reallyGone.length===1?'y':'ies'}`
                          : `cleared ${reallyGone.length} here — the list could not be saved yet and `
                            + `will retry; do not reload`);
              closeModal(); renderBlossom();
            }
          };
        });
    }catch(e){
      toast('could not check your drive: ' + ((e && e.message) || e));
    }finally{ if(btn){ btn.disabled = false; btn.textContent = was; } }
  }
  async function _blobAlreadyStored(sha){
    try{
      /* THE PROBE MUST OUTRUN EVERY CACHE, and `cache:'no-store'` only speaks to the browser's. A
       * proxy in front of the store caches these content-addressed URLs as immutable — correct for
       * bytes that exist, and a LIE once they are deleted: after the store was wiped, the cache
       * answered 200 for 1,475 blobs that were gone, the seed skipped uploading them, and every
       * other device then hit "the store does not have these bytes". A unique query string makes
       * every cache miss; only the store itself may answer an existence question. */
      const r = await fetch(mediaServer() + '/' + sha + '?probe=' + Date.now(),
                            { method:'HEAD', cache:'no-store' });
      if(!r || !r.ok) return false;
      /* PRESENT IS NOT ENOUGH — it must not be on its way out. Skipping the upload also skips the
       * server's save path, and that save is what clears an expiry when a blob becomes referenced
       * again. A blob carrying one is scheduled for deletion (a superseded manifest, an index blob
       * out of backup retention), so recording a manifest entry against it would point every device
       * at bytes due to vanish. Upload instead: the write clears the stamp, which is exactly the
       * "a fresh reference clears the TTL" rule the reclaim path depends on. */
      if(r.headers && r.headers.get('X-Expires-At')) return false;
      return true;
    }catch(_){ return false; }
  }
  // Fetch + decrypt one synced blob. The same two steps PC.syncBlobs.get does — shared rather than
  // written twice, because "which key decrypts a sync blob" must have exactly one answer.
  /* One re-check of the drive key per session, at the moment something fails to decrypt.
   *
   * pull() already adopts the account's key when the local one differs — but only when a pull
   * happens, which is once at startup. A device that was fine at startup and then meets bytes it
   * cannot open never asks again, so it stays locked out for the whole session: "this device cannot
   * decrypt your folder list", every sweep, while the key that opens it is in the drive index.
   *
   * Once, because a key that is still wrong after re-reading the authority is not going to be fixed
   * by reading it again, and a retry per file would turn one bad key into thousands of requests. */
  let _mkRefreshed = false;
  async function _refreshDriveKey(){
    if(_mkRefreshed) return false;
    _mkRefreshed = true;
    try{
      // Force a genuine re-read: ensure() short-circuits on `_pullOk`, and this exists precisely
      // because the index we already hold has a key that does not work.
      FilesIdx.mk = null; FilesIdx._pullOk = false;
      await FilesIdx.pull();
      return true;
    }catch(_){ return false; }
  }
  async function _syncBlobBytes(sha, onWireProgress){
    /* A DOWNLOAD MUST MISS NEGATIVE CACHES JUST LIKE THE EXISTENCE PROBE ABOVE.
     *
     * A freshly uploaded blob can briefly sit behind a cached 404 at a proxy/edge.  The HEAD probe
     * already carries a nonce, but the GET did not, so a receiver could be told "unavailable" for
     * bytes the store actually held. Folder Sync remembers a deterministic 404 to avoid burning
     * bandwidth forever; that made one stale cache answer persist as "incoming copy could not be
     * fetched" and the unresolved record then appeared as hundreds of conflicts.  Retry only
     * response failures that can be transient, with a fresh URL each time.  A successful response
     * still goes through the content hash and AES-GCM checks below.
     */
    let r = null;
    for(let attempt = 0; attempt < 3; attempt++){
      try{
        r = await fetch(mediaServer() + '/' + sha + '?sync=' + Date.now() + '-' + attempt,
                        { cache:'no-store' });
      }catch(e){
        if(attempt === 2) throw e;
        await new Promise(done => setTimeout(done, 250 * (attempt + 1)));
        continue;
      }
      if(r.ok || !([404, 408, 425, 429, 500, 502, 503, 504].includes(r.status)) || attempt === 2) break;
      try{ if(r.body && r.body.cancel) await r.body.cancel(); }catch(_){}
      await new Promise(done => setTimeout(done, 250 * (attempt + 1)));
    }
    if(!r.ok) throw new Error('blob ' + String(sha).slice(0,8) + ' unavailable (' + r.status + ')');
    /* KEEP THE STALL CLOCK TIED TO BYTES, NOT TO WHOLE BLOBS.
     *
     * Folder-sync chunks written by a desktop are 16 MB. On a weak mobile link that can take many
     * minutes, and the old code said nothing until arrayBuffer() had received every byte. The
     * watchdog therefore declared a healthy transfer dead (and the card sat at one percentage)
     * even while the radio was moving data. Read the response stream so every network read proves
     * liveness. We still join one encrypted chunk for its SHA-256/AES-GCM check; memory remains
     * bounded by the chunk size, exactly as before.
     */
    let bytes;
    if(r.body && typeof r.body.getReader === 'function'){
      const rd = r.body.getReader();
      const declared = +(r.headers.get('content-length') || 0);
      let direct = declared > 0 ? new Uint8Array(declared) : null;
      let parts = direct ? null : [];
      let n = 0;
      for(;;){
        const x = await rd.read();
        if(x.done) break;
        if(x.value && x.value.length){
          /* Content-Length is normally present, so fill one allocation directly. Besides avoiding
           * an unnecessary second 16 MB copy, this matters on Android where the decrypted chunk,
           * WebView bridge buffer and filesystem write can briefly coexist. If a proxy omitted or
           * lied about the length, fall back to joining the pieces and let the content hash decide
           * whether the response is complete. */
          if(direct && n + x.value.length <= direct.length){
            direct.set(x.value, n);
          } else {
            if(direct){ parts = [direct.subarray(0, n)]; direct = null; }
            parts.push(x.value);
          }
          n += x.value.length;
          try{ if(onWireProgress) onWireProgress(n, declared); }catch(_){}
        }
      }
      if(direct){
        bytes = n === direct.length ? direct : direct.subarray(0, n);
      } else {
        bytes = new Uint8Array(n);
        let at = 0;
        for(const p of parts){ bytes.set(p, at); at += p.length; }
      }
    } else {
      bytes = new Uint8Array(await r.arrayBuffer());
      try{ if(onWireProgress) onWireProgress(bytes.length, bytes.length); }catch(_){}
    }
    /* WHAT ARRIVED, BEFORE WHAT IT MEANS. AES-GCM answers one word — OperationError — for two
     * completely different failures: bytes that arrived damaged (a proxy stream cut short, a bad
     * hop) and bytes that are perfect but sealed with a key this device does not hold. The store is
     * content-addressed, so one hash settles it: a mismatch is a TRANSFER problem and must say so
     * (and must never be remembered against the copy — the copy is fine); a match that still fails
     * to decrypt is a KEY problem, which retrying the network cannot fix. Reported as a bunch of
     * bare "OperationError"s on a laptop, which is neither sentence. */
    const got = await sha256hex(bytes.buffer);
    if(got !== sha){
      throw new Error('the server answered with damaged or partial bytes for '
                      + String(sha).slice(0,8) + ' (' + bytes.length + ' bytes hashing to '
                      + got.slice(0,8) + ') — a transfer problem, this will be retried');
    }
    try{
      return await _masterDecrypt(await FilesIdx._ensureMK(), bytes);
    }catch(e){
      // AES-GCM rejects the WHOLE message when the key is wrong, so this is not a damaged file —
      // it is the wrong key. Ask the drive index what the account's key actually is, and try once more.
      if(!await _refreshDriveKey()){
        throw new Error('this device\u2019s drive key does not open ' + String(sha).slice(0,8)
                        + ' \u2014 the bytes are intact but were sealed with a different key.'
                        + ' On the device that HAS this file, press \u201cSend them again\u201d on'
                        + ' the folder\u2019s card \u2014 it re-uploads under the current key');
      }
      try{ return await _masterDecrypt(await FilesIdx._ensureMK(), bytes); }
      catch(_){
        throw new Error('this device\u2019s drive key does not open ' + String(sha).slice(0,8)
                        + ' \u2014 the bytes are intact but were sealed with a different key'
                        + ' (the account\u2019s current key was re-read and does not open it either).'
                        + ' On the device that HAS this file, press \u201cSend them again\u201d on'
                        + ' the folder\u2019s card \u2014 it re-uploads under the current key');
      }
    }
  }
  /* The file's bytes, whole or in pieces, as a Blob.
   *
   * A Blob rather than a Uint8Array on purpose: a chunked file is one the renderer could not hold in
   * the first place (that is why it was chunked), and a Blob built from its parts is backed by the
   * browser's own storage instead of the JS heap. Concatenating into one array here would reintroduce
   * exactly the ceiling chunking exists to remove. */
  /* A BLOB URL CARRIES THE BLOB'S TYPE, AND AN UNTYPED BLOB IS A FILE NOTHING CAN OPEN.
   *
   * Every blob on the drive is stored `application/octet-stream` -- it is encrypted, so that is the
   * only honest thing to store -- and these Blobs were built with no type at all. A `blob:` URL then
   * names bytes of unknown kind: `background-image` never loads it (reported as "0 thumbnails loaded
   * in File Manager"), and a <video> handed one shows a black box whose controls have nothing to
   * control ("playing video in blossom ... is black, buttons hidden", "can't play .webm in file
   * manager"). Three reports, one missing argument. preview.js already learned this and says so at
   * `mimeFor`; `fileFromBytes` above states the rule for this file -- "everything that reconstructs
   * a file from a decrypted buffer should go through this" -- and this function was the exception.
   *
   * The NAME is the only evidence available: the stored mime is octet-stream by design, and sniffing
   * the bytes would be a decoder this client does not need. An unknown extension yields no type,
   * which is exactly what happened before, so a caller with no name is no worse off than it was. */
  async function _syncFileBlob(sha, chunks, name){
    const type = mimeForName(name || '');
    const opts = type ? { type } : undefined;
    if(chunks && chunks.length){
      const parts = [];
      for(const c of chunks) parts.push(await _syncBlobBytes(c));
      return new Blob(parts, opts);
    }
    return new Blob([await _syncBlobBytes(sha)], opts);
  }
  /* OPEN A FILE FROM A SYNCED FOLDER IN POSTERCHAN CODE.
   *
   * The bytes come the same way Download gets them (`_syncFileBlob` — Blossom by sha or chunk list,
   * decrypted with the drive key), and the buffer carries a `sync` descriptor instead of a drive
   * sha, so saving writes back into the FOLDER rather than onto the drive. That distinction is the
   * whole point: a synced file edited here has to reach every device, and a copy quietly landing on
   * the drive instead would look like the edit worked and change nothing anywhere else. */
  async function openSyncCodeFile(d){
    try{
      const chunks = d.chunks ? String(d.chunks).split(',').filter(Boolean) : null;
      if(!d.sha && !(chunks && chunks.length)){ toast('this file has no stored copy yet'); return; }
      toast('decrypting…');
      const blob = await _syncFileBlob(d.sha, chunks);
      if(blob.size > _CODE_MAX) throw new Error('that file is too big to edit here');
      const bytes = new Uint8Array(await blob.arrayBuffer());
      if(bytes.indexOf(0) !== -1) throw new Error('that looks like a binary file');
      const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
      const code = await _withModule('code.js', 'PCCode');
      if(!(code && code.openBlob)) throw new Error('the editor did not load');
      code.openBlob({ sha: d.sha || ('sync:' + d.path), name: d.name || 'document',
                        mime: mimeForName(d.name || ''), enc: '0',
                        sync: { key: _S._syncRoot, path: d.path || '' }, text });
      switchView('code');
    }catch(e){ toast('could not open: ' + ((e && e.message) || e)); }
  }

  async function _syncDownload(btn, sha, name, chunks){
    if(!sha && !(chunks && chunks.length)){ toast('this file has no stored copy yet'); return; }
    // innerHTML, not textContent: the button IS an <svg> sprite reference, so textContent reads as ''
    // and writing to it deletes the icon — restoring the empty string afterwards leaves a blank
    // button for the rest of the session.
    const was = btn ? btn.innerHTML : '';
    try{
      if(btn){ btn.disabled = true; btn.innerHTML = '…'; }
      toast('decrypting…');
      await saveBlobAs(await _syncFileBlob(sha, chunks), name || 'file');
    }catch(e){ toast('download failed: ' + ((e && e.message) || e)); }
    finally{ if(btn){ btn.disabled = false; btn.innerHTML = was; } }
  }

  /* The sidebar: the drive's own folders, then the synced ones. Built by both renderers so the two
   * sources sit in one tree — which is the whole idea of showing them here rather than on a separate
   * screen. Identical markup to before for the drive half; the chips are still
   * .folder-chip[data-folder] and every handler they had still finds them. */
  function _fxSideHTML(){
    const folders = FilesIdx.folders();
    /* A DRIVE THAT HAS NOT LOADED IS NOT A DRIVE WITH ONE FOLDER IN IT.
     *
     * FilesIdx starts life holding its own default — `folders: ['Music']` — and this sidebar drew
     * that default as though it were the answer. Nothing repaints when the real index lands either
     * (only the manual Refresh button does), so Files opened showing Music and Posts and STAYED
     * that way until the user clicked something else: "webui showing Music and posts only in
     * Files", which reads as every other folder having been deleted. Nothing was: the index simply
     * had not arrived.
     *
     * Guarded on the DEFAULT, not on `_pullDone` alone — a warm client holding a cached list has
     * something real to show and must keep showing it while it refreshes behind. This only
     * suppresses the case where the list IS the built-in default and we have not yet been told
     * otherwise. See the cache-first rule in CLAUDE.md: never dress an empty answer as an answer. */
    const _idxUnknown = !FilesIdx._pullDone && folders.length <= 1;
    return `<div class="fx-side-mobile-head" id="fx-locations-panel"><b>Locations</b><button class="fx-nb" id="fx-locations-close" aria-label="Close locations"><svg class="ic b-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div><div class="fx-tree"><section class="fx-tree-node">
      <button class="fx-tree-head${(!_S._hostOn&&!_S._syncRoot)?' active':''}${_S._fxMobileSource==='blossom'?' mobile-on':''}" data-fxtoggle="blossom" aria-expanded="${_fxBlossomOpen?'true':'false'}"><span class="chev">${_fxBlossomOpen?'▾':'▸'}</span><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg><b>Files</b></button>
      <div class="fx-tree-children${_fxBlossomOpen?'':' hidden'}" data-fxtree="blossom"><div class="folder-bar">
        <button class="folder-chip${(!_S._syncRoot&&_S._filesFolder==='')?' active':''}" data-folder=""><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>All</button>
        ${_idxUnknown ? '<span class="muted small" id="fx-folders-loading">Loading your folders…</span>'
          : folders.map(f=>`<button class="folder-chip${(!_S._syncRoot&&_S._filesFolder===f)?' active':''}" data-folder="${enc(f)}">${_fxFolderIcon(f)}${enc(f)}</button>`).join('')}
      </div></div></section>` + _fxSyncedHTML() + _fxHostHTML()
      + `${_standalone()?'':`<button class="fx-tree-head${_S._filesTab==='ai'?' active':''}" data-files-mode="ai"><svg class="ic b-ic" aria-hidden="true"><use href="#i-ai"></use></svg><b>AI Chat files</b></button>`}`
      + `${_S.IS_ADMIN?`<button class="fx-tree-head${_S._filesTab==='admin'?' active':''}" data-files-mode="admin"><svg class="ic b-ic" aria-hidden="true"><use href="#i-shield"></use></svg><b>Storage admin</b></button>`:''}</div>`;
  }
  /* "This computer", beside the drive's folders and the synced ones — one tree, three sources,
   * which is the whole reason they share a screen instead of having three. */
  function _fxHostHTML(){
    if(!_hostFs()) return '';
    /* `.fx-sec` — the SAME section wrapper the synced folders use, not a class of this feature's
     * own. An invented one has no stylesheet behind it, so the heading renders as unstyled body
     * text in a sidebar where every other heading is a small cyan caption: it looks like a bug in
     * the theme rather than a section nobody wrote CSS for. */
    return `<section class="fx-tree-node"><button class="fx-tree-head${_S._hostOn?' active':''}${_S._fxMobileSource==='computer'?' mobile-on':''}" data-fxtoggle="computer" aria-expanded="${_fxComputerOpen?'true':'false'}"><span class="chev">${_fxComputerOpen?'▾':'▸'}</span><svg class="ic b-ic" aria-hidden="true"><use href="#i-monitor"></use></svg><b>My Computer</b></button><div class="fx-tree-children${_fxComputerOpen?'':' hidden'}" data-fxtree="computer"><button class="folder-chip${_S._hostOn ? ' active' : ''}" data-host="1" title="Browse this machine's own files"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Home</button></div></section>`;
  }
  /* \u267b DELETED ON EVERY DEVICE — the account-wide undo, beside the per-device trash. Entries
   * the record marks deleted BUT whose address was retained (executors keep sha/chunks on
   * tombstones now) can be republished live; every device then re-fetches from the store. Old-era
   * tombstones without an address are named as unrestorable rather than hidden. */
  let _fxDelOpen = false;
  function _fxDeletedHTML(){
    if(!_S._syncRoot) return '';
    const m = _syncManifests.get(_S._syncRoot);
    const paths = m && m.paths; if(!paths) return '';
    const rows = [], dead = [];
    for(const p in paths){
      const e = paths[p];
      if(!e || !e.deletedAt) continue;
      if(('/' + p + '/').indexOf('/.pc-trash/') >= 0) continue;
      if(e.sha || (e.chunks && e.chunks.length)) rows.push(p); else dead.push(p);
    }
    if(!rows.length && !dead.length) return '';
    /* THE HEADER COUNTS THE DELETIONS, NOT THE RESTORABLE ONES. It showed `rows.length`, which
       excludes every tombstone that kept no address — so a folder holding 107 deletions announced
       "3", read as the panel being broken or the deletions having gone somewhere else. The total is
       the honest number; the split between what this panel can undo and what it cannot belongs
       INSIDE, where it can be explained. */
    const total = rows.length + dead.length;
    /* THE TRASH. Singular, and this is it.
     *
     * It used to be called "Deleted on every device" because it was one of TWO — the other being a
     * `.pc-trash` directory inside every synced folder, on every device, holding a different set of
     * files with no list anywhere that covered both. That is what people actually experienced as
     * the failure: "phone already has 109 files in trash wtf", a tablet with 226, another with 19,
     * and nothing that answered "what did I delete". The per-device trash is gone; a deletion now
     * removes the local file, and only once the store has confirmed it can give the bytes back.
     * So this list IS the trash, for the whole account, and it is named that. */
    return `<div class="fx-trash">
      <button class="fx-trash-hd" id="fx-del-toggle"><svg class="ic b-ic" aria-hidden="true"><use href="#i-restore"></use></svg>Trash
        <span class="fx-n">${total}</span><span class="chev">${_fxDelOpen?'\u25be':'\u25b8'}</span></button>
      <div class="fx-trash-body${_fxDelOpen?'':' hidden'}">
        <div class="muted small">Everything deleted from your synced folders, on any device. ${rows.length} of ${total} can be put back from here \u2014 their bytes are still in the store, so restoring republishes them and every device brings the file back.</div>
        ${rows.slice(0, 50).map(p2 => `<div class="fx-trash-row"><span title="${enc(p2)}">${enc(p2.split('/').pop())}</span>
          <span class="muted small" title="${enc(p2)}">${enc(p2.split('/').slice(0,-1).join('/'))}</span>
          <button class="mini" data-undelete="${enc(p2)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-restore"></use></svg>Restore</button></div>`).join('')}
        ${rows.length > 50 ? `<div class="muted small">\u2026and ${rows.length - 50} more \u2014 Restore all covers every one</div>` : ''}
        ${rows.length > 1 ? `<button class="mini fx-trash-all" id="fx-del-restoreall"><svg class="ic b-ic" aria-hidden="true"><use href="#i-restore"></use></svg>Restore all ${rows.length} everywhere</button>` : ''}
        ${dead.length ? `<div class="muted small">The other ${dead.length} kept no storage address, so nothing here knows which bytes they were. They are not lost: on a device that still HAS those files, open Folder sync and press \u201cPut them back everywhere\u201d on the folder\u2019s card \u2014 it uploads from that copy.</div>` : ''}
      </div>
    </div>`;
  }
  /* \ud83d\uddd1 THIS DEVICE'S TRASH for the synced folder being browsed. The trash is per-device
   * by design (it never joins the shared record), so this section only appears where a filesystem
   * bridge exists AND this device actually maps the pair being viewed — a phone browsing a folder
   * it doesn't sync sees nothing, honestly. Grouped by the dated folders the engine writes;
   * restore reuses PCSync.restoreTrash (the card's exact loop: never overwrite, per-op timeouts). */
  /* The trash is a FOLDER now (see the synced-folder listing), not a panel of dates.
   *
   * What was here rendered a collapsed block of dated groups with a Restore button per date: you
   * could see that "2026-08-19 · 107 files" existed and nothing whatever about what those files
   * were, which is the only question somebody deciding what to do with 107 deleted files is asking.
   * A folder answers it with the machinery Files already has — names, sizes, dates, sub-folders,
   * search and the sort columns — so the block is gone rather than improved. Its cache went with
   * it: `_trashCache` lives in the closure that RENDERS the listing, which is a different one, and
   * reaching across for the old variable was a ReferenceError that showed up only as a folder stuck
   * on its spinner (the render is async, so the throw became a rejected promise nobody saw). */
  /* Dropping a file onto a folder chip moves it. Lives in its own function because the sidebar can
   * repaint on its own — when the synced-folder list lands a moment after first paint — and a chip
   * redrawn without this is a drop target that silently stops accepting drops. */
  function _fxBindChipDrop(root){
    $$('.folder-chip[data-folder]', root || document).forEach(chip=>{
      chip.ondragover=e=>{ e.preventDefault(); chip.classList.add('drop'); };
      chip.ondragleave=()=>chip.classList.remove('drop');
      chip.ondrop=e=>{ e.preventDefault(); chip.classList.remove('drop'); const sha=e.dataTransfer&&e.dataTransfer.getData('text/sha'); if(sha){ FilesIdx.move(sha, chip.dataset.folder); toast('moved to '+(chip.dataset.folder||'All')); renderBlossom(); } };
    });
  }
  function _fxBindSide(root){
    const r = root || document;
    { const close=$('#fx-locations-close',r), explorer=(close&&close.closest('.fx-explorer'))
        || (r.closest&&r.closest('.fx-explorer')) || $('.fx-explorer',r);
      const shut=()=>{ if(!explorer)return; explorer.classList.remove('fx-locations-on');
        const open=$('#fx-locations-open',explorer); if(open){ open.setAttribute('aria-expanded','false'); open.focus(); } };
      if(close && explorer) close.onclick=shut;
      if(explorer) explorer.addEventListener('keydown',e=>{
        if(e.key==='Escape' && explorer.classList.contains('fx-locations-on')){
          e.preventDefault(); e.stopPropagation(); shut();
        }
      });
      /* The phone drawer's dark surround is a box-shadow, not a DOM backdrop. The narrow exposed
       * strip therefore belongs to .fx-main underneath it; without a capture guard, tapping there
       * opens whichever file happens to be under your finger and leaves Locations open. Treat any
       * tap outside .fx-side as the conventional backdrop dismissal before file handlers see it. */
      if(explorer) explorer.addEventListener('click',e=>{
        const mobile=!!(window.matchMedia&&matchMedia('(max-width:820px)').matches);
        if(!mobile||!explorer.classList.contains('fx-locations-on')||e.target.closest('.fx-side'))return;
        shut();e.preventDefault();e.stopPropagation();
      },true); }
    $$('[data-files-mode]',r).forEach(b=>b.onclick=()=>{
      _S._filesAdminPk=null; _S._filesTab=b.dataset.filesMode; renderBlossom();
    });
    /* A FOLDER'S ACTIONS BELONG TO THE FOLDER, not to the list of places you can go.
     *
     * "Delete “<name>”" was a chip in the sidebar's folder LIST: a destructive button sitting among
     * the navigation chips, in a column 220px wide, so on any folder with a real name the label ran
     * out of the panel and the trash icon was clipped. Reported in exactly those words — "the delete
     * button doesn't even fit in the space", "the icon is cut off", "why would you put a delete
     * button there". It also put Delete one mis-click from the folder above it while navigating.
     *
     * Every file manager answers this the same way and so does this one now: right-click (or
     * long-press) the folder. Nothing is deleted without the same confirmation as before, and the
     * files themselves are never deleted — they move to All. */
    $$('.folder-chip[data-folder]', r).forEach(chip => {
      const name = chip.dataset.folder || '';
      if(!name || name === 'Music') return;      // built in; there is nothing to remove
      chip.title = name + ' — right-click for folder actions';
      chip.oncontextmenu = async e => {
        e.preventDefault(); e.stopPropagation();
        if(await uiConfirm('Delete folder “' + name + '”? Its files move to All — the files themselves aren\'t deleted.')){
          FilesIdx.removeFolder(name);
          if(_S._filesFolder === name) _S._filesFolder = '';
          renderBlossom();
        }
      };
    });
    /* A HEADING IN THE SIDEBAR IS A PLACE, NOT A DISCLOSURE TRIANGLE.
     * These three used to only collapse their tree on a desktop, so clicking "Blossom", "Synced
     * Folders" or "My Computer" from anywhere else in Files opened a list and left you standing
     * where you were — reported three separate times as "clicking X does nothing", because from
     * the outside a repaint of the same screen and a dead button are the same event. The phone had
     * grown its own answer for two of the three (and "Blossom" landed on HOME rather than on
     * Blossom). One rule now, both layouts: a heading takes you to that source and opens its tree;
     * clicking the heading of the source you are ALREADY showing is what collapses it. */
    $$('[data-fxtoggle]',r).forEach(b=>b.onclick=()=>{
      const which=b.dataset.fxtoggle;
      const mobile = !!(window.matchMedia && matchMedia('(max-width:820px)').matches);
      const openNow = which==='blossom' ? _fxBlossomOpen : (which==='synced' ? _fxSyncedOpen : _fxComputerOpen);
      /* "At" a source means standing on its ROOT, not merely somewhere inside it. Judged only by
       * which source is showing, pressing "Blossom" from inside a folder took the collapse branch
       * and left you in that folder — the original complaint, one level down. */
      const atSource = which==='blossom' ? (!_S._hostOn && !_S._syncRoot && _S._filesFolder==='')
                     : which==='synced'  ? (!!_S._syncRoot && !_S._syncPath)
                     : which==='computer' ? !!_S._hostOn : false;
      const remember = (open)=>localStorage.setItem('pc.files.tree.'+which,open?'1':'0');
      if(which!=='blossom' && which!=='synced' && which!=='computer') return;
      if(!atSource || !openNow){
        remember(true);
        if(which==='blossom'){ _fxBlossomOpen=true; _fxOpenFolder(''); return; }
        if(which==='computer'){ _fxComputerOpen=true; _fxOpenComputer(); return; }
        _fxSyncedOpen=true;
        /* Synced Folders is the one heading with no landing folder of its own, so it lands on the
         * first pair. With none loaded yet it still has to show something rather than nothing. */
        const first = Array.isArray(_syncPairs) && _syncPairs.length ? _syncPairs[0].key : '';
        if(first){ _fxOpenSynced(first); return; }
        _S._fxMobileSource='synced'; renderBlossom(); return;
      }
      if(which==='blossom')_fxBlossomOpen=!_fxBlossomOpen;
      else if(which==='synced')_fxSyncedOpen=!_fxSyncedOpen;
      else _fxComputerOpen=!_fxComputerOpen;
      const open = which==='blossom' ? _fxBlossomOpen : (which==='synced' ? _fxSyncedOpen : _fxComputerOpen);
      remember(open);
      if(mobile){
        b.setAttribute('aria-expanded',open?'true':'false');
        const tree=$(`[data-fxtree="${which}"]`,r); if(tree)tree.classList.toggle('hidden',!open);
        const chev=b.querySelector('.chev'); if(chev)chev.textContent=open?'\u25be':'\u25b8';
      }else renderBlossom();
    });
    /* EVERY move remembers where it came from, or Back only undoes the ones made from the crumbs —
     * which is the half of browsing nobody uses. */
    $$('.folder-chip[data-folder]', r).forEach(b=> b.onclick=()=>_fxOpenFolder(b.dataset.folder));
    $$('.folder-chip[data-synckey]', r).forEach(b=> b.onclick=()=>_fxOpenSynced(b.dataset.synckey));
    $$('.folder-chip[data-host]', r).forEach(b=> b.onclick=()=>_openHostFiles(true));
    /* The trash's own bindings live with the LISTING now that it is a folder, not here beside the
     * chips — a set of handlers left behind for markup that no longer renders is how a dead control
     * survives a redesign and quietly does nothing. */
    { const dt = $('#fx-del-toggle', r); if(dt) dt.onclick = () => { _fxDelOpen = !_fxDelOpen; renderBlossom(); };
      const un = async (paths) => {
        const S2 = window.PCSync;
        if(!S2 || !S2.edit || !S2.edit.restoreMany){ toast('this build can\u2019t restore account-wide yet'); return; }
        if(!await uiConfirm('Restore ' + paths.length + ' file' + (paths.length===1?'':'s')
             + ' on every device that syncs \u201c' + _S._syncRoot + '\u201d?\n\nEach device downloads '
             + 'its copy back from the store on its next sweep.')) return;
        try{
          const r2 = await S2.edit.restoreMany(_S._syncRoot, paths);
          toast('restored ' + ((r2 && r2.restored) || 0) + ' everywhere'
                + (r2 && r2.unaddressed ? ' \u00b7 ' + r2.unaddressed + ' kept no address' : ''));
          _syncManifests.delete(_S._syncRoot); renderBlossom();
        }catch(e){ toast('nothing was restored: ' + ((e && e.message) || e)); }
      };
      $$('[data-undelete]', r).forEach(b => b.onclick = () => un([b.dataset.undelete]));
      { const da = $('#fx-del-restoreall', r); if(da) da.onclick = () => {
          const m2 = _syncManifests.get(_S._syncRoot); const out = [];
          if(m2 && m2.paths) for(const p2 in m2.paths){ const e2 = m2.paths[p2];
            if(e2 && e2.deletedAt && (e2.sha || (e2.chunks && e2.chunks.length))
               && ('/' + p2 + '/').indexOf('/.pc-trash/') < 0) out.push(p2); }
          if(out.length) un(out); }; } }
    $$('.fx-syncx[data-syncforget]', r).forEach(b => b.onclick = async (e) => {
      e.stopPropagation();
      const key = b.dataset.syncforget;
      const rec = Array.isArray(_syncPairs) ? _syncPairs.find(x => x && x.key === key) : null;
      const n = rec ? rec.n : 0;
      /* SAY WHAT IT DOES AND WHAT IT DOES NOT. It removes the shared record; it does not touch a
         file on any disk. The one situation it is wrong in is a folder something is still syncing,
         because to that device an empty manifest reads as "deleted elsewhere" — so that is the
         sentence in the dialog, not a footnote. */
      const ok = await uiConfirm('Forget “' + key + '”?\n\nThis removes the shared record for '
        + 'this folder — the list of files your devices agree on' + (n ? ' (' + n + ' live)' : '')
        + '. No file is deleted anywhere.\n\nDo this only when NO device is still syncing this '
        + 'folder. A device that is would see an empty record and offer to delete its copy.');
      if(!ok) return;
      try{
        const S = window.PCSync;
        if(!S || !S.edit || !S.edit.forget) throw new Error('this build cannot forget a folder');
        const out = await S.edit.forget(key);
        /* Say which is which. "8,000 entries cleared" under a folder showing "0 files" reads as a
           contradiction and was reported as one; the live count and the deletion markers are
           different things and the markers are the reason the folder was stuck. */
        const n = (out && out.removed) || 0, lv = (out && out.live) || 0, tb = (out && out.tombstones) || 0;
        toast(!n ? ('“' + key + '” had nothing left to clear')
                 : ('forgot “' + key + '” — ' + lv.toLocaleString() + ' live file'
                    + (lv === 1 ? '' : 's') + ' and ' + tb.toLocaleString() + ' deletion marker'
                    + (tb === 1 ? '' : 's') + ' cleared. No file was deleted.'));
        if(_S._syncRoot === key){ _S._syncRoot = ''; _S._syncPath = ''; }
        _syncManifests.delete(key);
        await S.accountFolders(true);
        renderBlossom();
      }catch(err){ toast('could not forget it: ' + ((err && err.message) || err)); }
    });
    _fxBindChipDrop(r);
    /* Opening Files must never summon a remote signer. Discovery is encrypted, so make it an
     * explicit action instead of a background side effect of drawing the sidebar. */
    { const loadSync=$('[data-load-sync-folders]',r); if(loadSync) loadSync.onclick=async()=>{
      loadSync.disabled=true; loadSync.textContent='Waiting for signer…';
      try{ await _ensureSyncPairs(); renderBlossom(); }
      catch(e){ loadSync.disabled=false; loadSync.textContent='Try again'; toast(String(e&&e.message||e)); }
    }; }
  }
  function _fxCrumbs(){
    const home = { label:'Files', to:'b:' };
    if(_S._syncRoot){
      const out = [home, { label:'🔄 ' + _S._syncRoot, to:'s:' + _S._syncRoot }];
      let acc = '';
      for(const seg of String(_S._syncPath||'').split('/').filter(Boolean)){
        acc = acc ? acc + '/' + seg : seg;
        out.push({ label:seg, to:'s:' + _S._syncRoot + '/' + acc });
      }
      return out;
    }
    if(!_S._filesFolder) return [home];
    const ic = _S._filesFolder==='Music' ? '🎵 ' : (FilesIdx.isEncFolder(_S._filesFolder) ? '🔒 ' : '📁 ');
    return [home, { label: ic + _S._filesFolder, to:'b:' + _S._filesFolder }];
  }

  /* Browsing a synced folder. The rows come from the manifest — the same document the sync engine
   * agrees on — so this shows what your DEVICES agreed the folder contains, which is why it works on
   * a phone that syncs nothing and in a browser that cannot sync at all.
   *
   * No thumbnails, on purpose: every blob here is AES-GCM ciphertext under the drive's master key, so
   * a preview costs a full download and a decrypt per file. A folder of 4000 photos would do that
   * 4000 times to draw one screen. */
  /* THUMBNAILS FOR A SYNCED FOLDER.
   *
   * Every one of these bytes is an encrypted blob: there is no thumbnail on the server and there
   * cannot be one, because the server cannot read the picture. So a preview means fetching the whole
   * blob and decrypting it HERE — which is affordable for what is on screen and ruinous for a folder
   * of six thousand photos. Hence all four limits below, none of which is optional:
   *
   *   lazy        only what has actually been scrolled into view (IntersectionObserver)
   *   bounded     at most _THUMB_PAR at once, or a fast scroll opens hundreds of parallel fetches
   *   small only  a full-size photo is the whole file; past _THUMB_MAX it is not worth the bytes
   *   revoked     object URLs are LRU-capped and revoked, or browsing a folder leaks every picture
   *               it drew until the tab is closed
   */
  const _THUMB_MAX = 12 * 1024 * 1024;
  const _THUMB_PAR = 3;
  const _THUMB_KEEP = 120;
  const _thumbs = new Map();          // sha -> object URL (insertion-ordered = LRU)
  let _thumbBusy = 0;
  const _THUMB_EXT = /^(jpg|jpeg|png|gif|webp|avif|bmp)$/i;

  function _thumbRemember(sha, url){
    _thumbs.set(sha, url);
    while(_thumbs.size > _THUMB_KEEP){
      const oldest = _thumbs.keys().next().value;
      try{ URL.revokeObjectURL(_thumbs.get(oldest)); }catch(_){}
      _thumbs.delete(oldest);
    }
  }
  async function _thumbFor(sha, chunks, name){
    const key = sha || ('chunks:' + (chunks || []).join(','));
    if(_thumbs.has(key)) return _thumbs.get(key);
    while(_thumbBusy >= _THUMB_PAR) await new Promise(r=>setTimeout(r, 120));
    _thumbBusy++;
    try{
      const blob = await _syncFileBlob(chunks && chunks.length ? '' : sha,
                                       chunks && chunks.length ? chunks : null, name);
      const url = URL.createObjectURL(blob);
      _thumbRemember(key, url);
      return url;
    } finally { _thumbBusy--; }
  }
  let _thumbObs = null;
  function _bindThumbs(grid){
    try{ if(_thumbObs) _thumbObs.disconnect(); }catch(_){}
    if(!window.IntersectionObserver) return;     // no observer → no previews, never eager ones
    _thumbObs = new IntersectionObserver((entries)=>{
      for(const en of entries){
        if(!en.isIntersecting) continue;
        const el = en.target, sha = el.dataset.thumb;
        const chunks = (el.dataset.thumbChunks || '').split(',').filter(Boolean);
        _thumbObs.unobserve(el);
        if(!sha && !chunks.length) continue;
        _thumbFor(sha, chunks, el.dataset.thumbName || '').then(url=>{
          // The grid is rebuilt on every navigation, so a decrypt that lands after the user has
          // moved on must not paint into a card that is no longer on the page.
          if(!el.isConnected) return;
          el.style.backgroundImage = 'url("' + url + '")';
          el.classList.add('has-thumb');
        }).catch(()=>{});
      }
    }, { rootMargin: '200px' });
    $$('[data-thumb]', grid).forEach(el=>_thumbObs.observe(el));
  }

  /* An edit to the shared manifest, made from here. Everything that can go wrong with one is the
   * same: it is a network write that other people's machines will act on, so it says what it did, it
   * drops the cached manifest (the view must never redraw from the copy it just invalidated) and it
   * asks for the folder counts again, since the number beside the folder is now wrong. */
  /* Every folder a synced folder's manifest implies, sorted -- a manifest has no folders of its own,
   * only paths, so a folder exists exactly while something live is inside it. */
  function _syncFolderList(paths){
    const out = new Set();
    for(const p in (paths || {})){
      const e = paths[p]; if(!e || e.deletedAt) continue;
      const parts = p.split('/'); parts.pop();
      for(let i = 1; i <= parts.length; i++) out.add(parts.slice(0, i).join('/'));
    }
    return [...out].sort((a, b) => a.localeCompare(b));
  }
  async function _syncEdit(what, run){
    const key = _S._syncRoot;
    try{
      const r = await run();
      _syncManifests.delete(key);
      try{ if(window.PCSync && PCSync.accountFolders) await PCSync.accountFolders(true); }catch(_){}
      if(_S.VIEW==='blossom' && _S._filesTab==='public' && _S._syncRoot===key) renderBlossom();
      return r;
    }catch(e){
      const m = (e && e.message) || String(e);
      // A refusal is not a failure to report quietly: the collapse guard and the "already exists"
      // check both come back this way, and both mean nothing was changed anywhere.
      toast(what + ' failed: ' + m);
      _syncManifests.delete(key);
      throw e;
    }
  }
  /* Uploading INTO a synced folder, one file at a time.
   *
   * Sequential on purpose, like the drive's uploader: each file is encrypted and hashed in this
   * renderer, and running several at once is what turns a phone's WebView into a memory kill. The
   * manifest is written per file rather than once at the end, so an interrupted batch leaves the
   * files that DID land in the folder instead of losing all of them. */
  let _sfUploading = false;
  async function _syncUploadFiles(files){
    files = (files||[]).filter(Boolean);
    if(!files.length || _sfUploading) return;
    const key = _S._syncRoot, dir = _S._syncPath;     // capture: navigating mid-upload must not misfile
    /* REPLACING IS NOT ADDING, and it happens to every device. An upload onto a path that already
     * exists is a change, so the copies on the other machines are overwritten on their next sweep —
     * the same thing that happens when you edit the file on a device, except that here nobody has
     * SEEN the file being replaced. Asked once for the whole batch rather than per file.
     *
     * The manifest is READ when it is not already cached, rather than warning only when the folder
     * happens to have been browsed recently: this prompt is the one thing between a drag-and-drop
     * and somebody's file being replaced on four machines. */
    let known = null;
    try{ known = { paths: await _syncManifest(key) }; }catch(_){ known = _syncManifests.get(key) || null; }
    if(known && known.paths){
      const clash = files.filter(f => { const e = known.paths[(dir ? dir + '/' : '') + f.name];
                                        return e && !e.deletedAt; }).map(f => f.name);
      if(clash.length){
        const list = clash.slice(0, 5).join(', ') + (clash.length > 5 ? ' and ' + (clash.length-5) + ' more' : '');
        if(!await uiConfirm('“' + key + '” already has ' + list + '.\n\nUploading replaces '
                            + (clash.length===1?'it':'them') + ' on every device that syncs this folder.')) return;
      }
    }
    const q = $('#sf-queue');
    if(q) q.innerHTML = files.map((f,i)=>`<div class="up-item"><span class="up-name">${enc(f.name)}</span><span class="up-stat" id="sf-stat-${i}">queued</span></div>`).join('');
    const stat = (i, s) => { const el = $('#sf-stat-'+i); if(el) el.textContent = s; };
    _sfUploading = true;
    let ok = 0, failed = 0;
    /* uploadMany, not a loop of upload(): each manifest write is a fresh encrypted copy of the WHOLE
     * document, which for a folder with thousands of paths is megabytes — once per file, a fifty-photo
     * drop moves more manifest than photos. It checkpoints instead, so an interrupted batch still
     * leaves the files that landed. A row reaches 'added' when its ENTRY is stored, not when its bytes
     * are, which is why the status can sit at 100% for a moment before it turns over. */
    try{
      const r = await PCSync.edit.uploadMany(key, dir, files, {
        onFile: (i, text) => stat(i, text),
        onProgress: (i, done, total) => stat(i, total ? Math.round(done/total*100) + '%' : 'uploading…'),
      });
      ok = r.ok; failed = r.failed;
    }catch(e){ toast('upload failed: ' + ((e && e.message) || e)); }
    finally { _sfUploading = false; }   // a flag left set is an uploader that never works again
    _syncManifests.delete(key);
    try{ if(window.PCSync && PCSync.accountFolders) await PCSync.accountFolders(true); }catch(_){}
    toast(ok ? (ok + ' file' + (ok===1?'':'s') + ' added — your devices pick them up on their next sync'
                + (failed ? ', ' + failed + ' failed' : ''))
             : 'nothing was added');
    if(_S.VIEW==='blossom' && _S._filesTab==='public' && _S._syncRoot===key) renderBlossom();
  }
  async function _renderSyncedRoot(pane){
    const details = _fxView()==='details';
    /* A client whose sync.js predates PCSync.edit is a REAL case, not a paranoid one: this app is a
     * service-worker PWA and an APK, so app.js and sync.js can be different ages on the same device.
     * Without the check the buttons draw and every one of them is a TypeError. */
    const canEdit = !!(window.PCSync && PCSync.edit);
    /* The uploader sits OUTSIDE the grid, so it is there whether the folder is empty, full, or
     * unreadable — an empty synced folder is exactly the one you most want to put something in. */
    const head = !canEdit ? '' : `<div class="drop-zone" id="sf-drop"><input type="file" id="sf-file" multiple hidden>
        <div class="dz-inner"><span class="dz-ic"><svg class="ic b-ic" aria-hidden="true"><use href="#i-upload"></use></svg></span>
          Drop files here, or <button class="btn btn-cyan small" id="sf-pick">choose files</button>
          <div class="muted small">→ 🔄 ${enc(_S._syncRoot)}${_S._syncPath?' / '+enc(_S._syncPath):''} · added to every device that syncs this folder</div></div>
        <div class="up-queue" id="sf-queue"></div></div>`;
    pane.innerHTML = '<div class="fx-explorer">'
      + '<div class="fx-side">' + _fxSideHTML() + '</div>'
      + '<div class="fx-main">' + _fxBarHTML(_fxCrumbs(), _fxHist.length > 0) + head
      /* NOT `nosel` any more: a synced file can be picked one at a time, so this grid has the same
       * checkbox column the drive's does. Left as `nosel` the header kept 5 cells while every row
       * grew to 6, which puts every heading over the wrong column — caught by
       * scripts/check_files_explorer.py's headings-misaligned assertion. */
      + '<div class="files-grid' + (details?' details':'') + '" id="bl-grid"><div class="spinner"></div></div>'
      + '</div></div>';
    _fxBindSide(pane); _fxBindBar(pane);
    {
      const input = $('#sf-file', pane), drop = $('#sf-drop', pane), pick = $('#sf-pick', pane);
      if(pick && input) pick.onclick = () => input.click();
      if(input) input.onchange = () => { const fs=[...input.files]; input.value=''; _syncUploadFiles(fs); };
      if(drop){
        drop.ondragover = e => { if(e.dataTransfer && [...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); drop.classList.add('over'); } };
        drop.ondragleave = () => drop.classList.remove('over');
        // Files only — no webkitGetAsEntry recursion here. A dropped FOLDER would have to create a
        // subtree in the manifest, and a half-walked directory tree is a half-created folder on every
        // device; the drive's uploader can take that risk because nothing it writes leaves this account.
        drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over');
          const fs = [...((e.dataTransfer && e.dataTransfer.files)||[])];
          if(fs.length) _syncUploadFiles(fs);
          else toast('drop files rather than a folder — folders sync from a device');
        };
      }
    }
    const grid = $('#bl-grid', pane); if(!grid) return;

    let paths=null, err='';
    try{ paths = await _syncManifest(_S._syncRoot); }
    catch(e){ err = (e && e.message) || String(e); }
    if(_S.VIEW!=='blossom' || _S._filesTab!=='public' || !_S._syncRoot) return;   // navigated away while it loaded
    if(paths===null){
      grid.innerHTML = '<div class="empty">Couldn’t read “'+enc(_S._syncRoot)+'” ('+enc(err)+').<br>'
        + '<span class="muted small">Your files are safe — this is the shared list of them, not the files.</span></div>';
      return;
    }
    /* THE TRASH IS A FOLDER, BROWSED LIKE ANY OTHER.
     *
     * It used to be a collapsed block of DATES with a Restore button per date — you could see that
     * "2026-08-19 · 107 files" existed and nothing about what they were, which is precisely the
     * question somebody standing in front of 107 deleted files needs answered before they decide
     * anything. Now `.pc-trash` is a directory row in the folder it belongs to, and entering it
     * gives the ordinary listing: names, sizes, dates, sub-folders, the search box, the sort
     * columns. The rows are read from the DEVICE (listTrash), not the manifest, because the trash is
     * per-device and deliberately absent from the shared list of files.
     *
     * `_syncEntries` needs no special case: its "the folder's own trash is not content" rule only
     * fires at the ROOT, so once `_syncPath` is inside .pc-trash the ordinary grouping applies. */
    const _inTrash = String(_S._syncPath || '').split('/')[0] === TRASH_DIR;
    let _trashRows = null;
    if(_inTrash || !_S._syncPath){
      const row = (window.PCSync && window.PCSync.folders)
        ? (window.PCSync.folders() || []).find(f => (f.key || f.name) === _S._syncRoot) : null;
      if(row && window.pcFs && window.pcFs.listTrash){
        if(!_trashCache || _trashCache.key !== _S._syncRoot){
          try{ _trashCache = { key: _S._syncRoot, rows: await window.pcFs.listTrash(row.id) || [] }; }
          catch(_){ _trashCache = { key: _S._syncRoot, rows: [] }; }
        }
        _trashRows = _trashCache.rows || [];
      }
    }
    if(_inTrash){
      /* Shaped like a manifest so every renderer below is unchanged. `at` is the real path on this
       * device and is what a restore or a purge is given. */
      paths = {};
      for(const r of (_trashRows || [])) paths[r.at] = { size:+r.size||0, mtime:+r.mtime||0, trash:r };
    }
    const { dirs, files } = _syncEntries(paths, _S._syncPath);
    /* THE DOOR. A directory row at the folder's root, so the trash is reached the way every other
     * folder is instead of through a control somewhere else on the page. Named for what it is
     * called on disk — a person who goes looking in Explorer finds the same name. */
    if(!_S._syncPath && _trashRows && _trashRows.length){
      dirs.push({ name: TRASH_DIR, dir: true, trashdir: true, n: _trashRows.length,
                  size: _trashRows.reduce((a, r) => a + (+r.size || 0), 0),
                  mtime: _trashRows.reduce((a, r) => Math.max(a, +r.mtime || 0), 0) });
    }
    // FOLDERS ALWAYS FIRST, whatever the sort — the one place a file manager overrides the column
    // you clicked, and every one of them does it. Within each group the chosen sort applies.
    const cmp = _fxCompare(_syncSortKey);
    dirs.sort(cmp); files.sort(cmp);
    /* The same search box, over a SYNCED folder. Folder Sync is most of why the drive needed one —
     * it files thousands of paths in — and a synced folder's names live in its manifest, so this is
     * the same client-side name match the Blossom grid does. Sub-folders drop out of a query for the
     * same reason files leave their folder: you are looking for a file, not a place. */
    const _q = _S._filesQ.trim();
    // A query searches the whole subtree, not just the directory you happen to be standing in.
    const items = _q ? _syncSearch(paths, _S._syncPath, _fxMatch).sort(cmp) : dirs.concat(files);
    if(!items.length){
      grid.innerHTML = '<div class="empty">' + (_q
        ? ('Nothing under ' + (_S._syncPath ? '“'+enc(_S._syncPath)+'”' : '“'+enc(_S._syncRoot)+'”')
           + ' matches “' + enc(_q) + '” — including sub-folders.')
        : (_S._syncPath?('“'+enc(_S._syncPath)+'” is empty.'):('Nothing in “'+enc(_S._syncRoot)+'” yet — sync a device and it appears here.'))) + '</div>';
      return;
    }
    const rowFor = (it) => {
      const ext = it.dir ? '' : ((String(it.name).match(/\.([A-Za-z0-9]{1,8})$/)||[])[1]||'').toLowerCase();
      const icon = it.trashdir ? '🗑️' : (it.dir ? _fxFileGlyph('folder') : _fxIcon(ext, ''));
      const type = it.dir ? (it.n + ' item' + (it.n===1?'':'s'))
                : (it.where ? (_fxType(ext) + ' · in ' + it.where) : _fxType(ext));
      const canThumb = !it.dir && (it.sha || (it.chunks && it.chunks.length))
                    && _THUMB_EXT.test(ext) && (it.size||0) <= _THUMB_MAX;
      const thumbAttrs = canThumb
        ? ` data-thumb="${enc(it.sha||'')}" data-thumb-name="${enc(it.name||'')}"${it.chunks?` data-thumb-chunks="${enc(it.chunks.join(','))}"`:''}` : '';
      // The FULL path is what an edit needs — a manifest has no folders, only paths — and a directory
      // row has none of its own, so it is rebuilt from where we are standing.
      const full = it.dir ? ((_S._syncPath ? _S._syncPath + '/' : '') + it.name) : it.path;
      /* INSIDE THE TRASH THE VERBS ARE DIFFERENT, and every one of the ordinary ones would be wrong:
       * Download offers bytes the manifest has no address for, Rename renames a dead copy, and
       * "Delete on every device" would publish a deletion for a file that is already deleted. Two
       * verbs belong here — put it back, or destroy this copy — and the second says so plainly
       * because it is the only button in Files that cannot be undone. */
      if(_inTrash){
        const at = it.dir ? '' : it.path;
        const tact = it.dir ? '' :
          `<button class="tr-back" data-at="${enc(at)}" title="Put this file back where it came from"><svg class="ic b-ic" aria-hidden="true"><use href="#i-restore"></use></svg></button>`
          + `<button class="tr-gone" data-at="${enc(at)}" data-name="${enc(it.name)}" title="Delete this copy permanently"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>`;
        const tnav = it.dir ? ` data-dir="${enc(it.name)}"` : '';
        if(details) return _fxDetailsRow({ dir:!!it.dir, name:it.name, icon:icon, size:_fxBytes(it.size),
          type:type, when:_fxWhen(it.mtime), acts:tact });
        return `<div class="file-card${it.dir?' isdir':''}"${tnav}>
          <div class="file-icon">${icon}<span>${enc(it.dir?'folder':(ext||'file'))}</span></div>
          <div class="meta"><span class="fname" title="${enc(it.name)}">${enc(fileLabel(it.name, ext, it.size))}</span>${tact?`<span class="fc-acts">${tact}</span>`:''}</div></div>`;
      }
      const edits = !canEdit ? ''
        : `<button class="rnsync" data-path="${enc(full)}" data-name="${enc(it.name)}" title="Rename${it.dir?' this folder everywhere':''}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button>`
          + `<button class="rmsync" data-path="${enc(full)}" data-name="${enc(it.name)}"${it.dir?' data-dir="1"':''} title="Delete${it.dir?' this folder':''} on every device"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>`;
      const act = (it.dir ? ''
        : `<button class="dlsync" data-sha="${enc(it.sha||'')}"${it.chunks?` data-chunks="${enc(it.chunks.join(','))}"`:''} data-name="${enc(it.name)}" data-path="${enc(it.path||'')}" title="Download (decrypts first)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg></button>`
          + `<button class="keepsync" data-sha="${enc(it.sha||'')}"${it.chunks?` data-chunks="${enc(it.chunks.join(','))}"`:''} data-name="${enc(it.name)}" title="Save a copy to your drive"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg></button>`
          )
        + edits;
      const nav = it.dir ? ` data-dir="${enc(it.name)}"` : '';
      /* ONE FILE AT A TIME, the way every file manager does it. The synced view had `Select all`
       * and `Select none` and nothing between them — you could not pick a single file. The drive
       * already had a per-card checkbox; this is the same control, the same class, the same
       * grammar (having a selection IS select mode), keyed on the PATH because that is what a
       * manifest edit takes. Folders are excluded: the actions the bar offers are per-file. */
      /* A FOLDER GETS THE CELL BUT NOT THE CHECKBOX. The bar's action is per-file, so a folder is
       * not selectable — but the details view is a GRID, and a row with one fewer cell than the
       * header shifts every heading by a column. The placeholder holds the column open. */
      const sbox = it.dir ? '<span class="selbox-gap"></span>' :
        `<input type="checkbox" class="selbox syncbox" data-path="${enc(it.path||'')}"${_syncSel.has(it.path)?' checked':''} title="Select">`;
      const selc = (!it.dir && _syncSel.has(it.path)) ? ' selected' : '';
      if(details) return _fxDetailsRow({ dir:!!it.dir, name:it.name, icon:icon, thumb:thumbAttrs, box:sbox,
        selected:!!selc, size:_fxBytes(it.size), type:type, when:_fxWhen(it.mtime), acts:act });
      return `<div class="file-card${it.dir?' isdir':''}${selc}"${nav}>${sbox}
        <div class="file-icon"${thumbAttrs}>${icon}<span>${enc(it.dir?'folder':(ext||'file'))}</span></div>
        <div class="meta"><span class="fname" title="${enc(it.name)}">${enc(fileLabel(it.name, ext, it.size))}</span>${act?`<span class="fc-acts">${act}</span>`:''}</div></div>`;
    };
    const fileItems = items.filter(it => !it.dir);
    /* THE SAME BAR THE DRIVE USES — one selection grammar for all of Files. The synced view had
     * its own Select/Done mode toggle with unicode glyphs beside the drive's persistent
     * Select-all / Select-none / count / actions bar, and two grammars for one screen reads as
     * broken ("the select button is inconsistent with the entire blossom UI"). Same classes, same
     * order, same icons; only the delete's LABEL differs, because here it means every device. */
    /* Search results can come from any depth below the current folder, so their manifest path is
     * authoritative. Rebuilding it from the current folder plus basename makes two different
     * subtrees look unselected even after every visible result was selected. */
    const _ssAll = !!fileItems.length && fileItems.every(it => _syncSel.has(it.path));
    /* NO SELECT-AND-DELETE-EVERYWHERE BAR IN THE TRASH. Its one action publishes a deletion, and
     * every file here is already deleted — pressing it would tell the other devices to delete files
     * they have already deleted, which is how a wave of stale tombstones starts. */
    const selbar = (canEdit && !_inTrash) ? `<div class="sync-selbar">
        <button class="btn btn-ghost small" id="ss-all" aria-pressed="${_ssAll?'true':'false'}">${_ssAll?'\u2611 Deselect all':'\u2610 Select all'}${fileItems.length?' ('+fileItems.length+')':''}</button>
        <button class="btn btn-ghost small" id="ss-none"${_syncSel.size?'':' disabled'}><svg class="ic b-ic" aria-hidden="true"><use href="#i-close"></use></svg>Select none</button>
        <span class="muted small" id="ss-count" style="margin:0 4px">${_syncSel.size?_syncSel.size+' selected':'none selected'}</span>
        <button class="btn btn-ghost small" id="ss-move"${_syncSel.size ? '' : ' disabled'} aria-haspopup="menu"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Move to…</button>
        <button class="btn btn-neon small" id="ss-del"${_syncSel.size ? '' : ' disabled'} style="color:var(--danger)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete on every device</button>
      </div>` : '';
    const trashbar = _inTrash ? `<div class="sync-selbar">
        <span class="muted small" style="margin-right:auto">${items.filter(i=>!i.dir).length} file${items.filter(i=>!i.dir).length===1?'':'s'} moved aside on this device \u2014 nothing here is on your other devices' screens.</span>
        <button class="btn btn-ghost small" id="tr-reconcile"><svg class="ic b-ic" aria-hidden="true"><use href="#i-restore"></use></svg>Reconcile Trash</button>
      </div>` : '';
    grid.innerHTML = selbar + trashbar + (details ? _fxColsHTML(true) : '') + items.map(rowFor).join('');
    if(details) _fxBindCols(grid);
    /* The two verbs. Both go through PCSync so the sweep that follows is told what happened — a
     * restore that says nothing is re-derived as "this copy is the deleted version" and undone. */
    if(_inTrash){
      const S = window.PCSync;
      const row = (S && S.folders) ? (S.folders() || []).find(f => (f.key || f.name) === _S._syncRoot) : null;
      $$('.tr-back', grid).forEach(b => b.onclick = async (e) => {
        e.stopPropagation();
        if(!row || !S.restoreTrash) return;
        b.disabled = true;
        try{ await S.restoreTrash(row.id, [b.dataset.at]); }
        finally{ _trashCache = null; renderBlossom(); }
      });
      $$('.tr-gone', grid).forEach(b => b.onclick = async (e) => {
        e.stopPropagation();
        if(!row || !window.pcFs || !window.pcFs.purgeTrash){
          toast('this build can\u2019t delete a single trashed file'); return; }
        if(!await __PC.uiConfirm('Permanently delete “' + b.dataset.name + '”?\n\nThis is the last '
             + 'copy on this device — it is already gone from your other devices. It cannot be '
             + 'undone.', { ok: 'Delete permanently' })) return;
        b.disabled = true;
        try{ const r = await window.pcFs.purgeTrash(row.id, [b.dataset.at]);
             toast((r && r.removed) ? 'deleted' : 'nothing was removed'); }
        catch(err){ toast('failed: ' + ((err && err.message) || err)); }
        finally{ _trashCache = null; renderBlossom(); }
      });
      const rc = $('#tr-reconcile', grid);
      if(rc) rc.onclick = async () => { if(!row || !S.reconcileTrash) return;
        rc.disabled = true;
        try{ await S.reconcileTrash(row.id); } finally{ _trashCache = null; renderBlossom(); } };
    }
    /* Toggling ONE file repaints that card and the bar, not the whole view — a re-render would
     * lose the scroll position on every click, which is what makes a picker feel broken. */
    $$('.syncbox', grid).forEach(cb=> cb.onclick=(e)=>{ e.stopPropagation();
      const p2 = cb.dataset.path || '';
      if(cb.checked) _syncSel.add(p2); else _syncSel.delete(p2);
      const card = cb.closest('.file-card'); if(card) card.classList.toggle('selected', cb.checked);
      grid.classList.toggle('selmode', _syncSel.size > 0);
      _syncSelOn = _syncSel.size > 0;
      const c = $('#ss-count', grid); if(c) c.textContent = _syncSel.size ? _syncSel.size + ' selected' : 'none selected';
      const d2 = $('#ss-del', grid); if(d2) d2.disabled = !_syncSel.size;
      const m2 = $('#ss-move', grid); if(m2) m2.disabled = !_syncSel.size;
      const n2 = $('#ss-none', grid); if(n2) n2.disabled = !_syncSel.size;
      const a2 = $('#ss-all', grid); if(a2){
        const every = !!fileItems.length && fileItems.every(it => _syncSel.has(it.path));
        a2.setAttribute('aria-pressed', every ? 'true' : 'false');
        a2.textContent = (every ? '\u2611 Deselect all' : '\u2610 Select all')
          + (fileItems.length ? ' (' + fileItems.length + ')' : '');
      }
    });
    /* selmode follows the DRIVE's grammar: having a selection IS the mode — no toggle. */
    _syncSelOn = _syncSel.size > 0;
    if(_syncSelOn) grid.classList.add('selmode'); else grid.classList.remove('selmode');
    {
      const all = $('#ss-all', grid);
      if(all) all.onclick = () => {
        const allIn = fileItems.length && fileItems.every(it => _syncSel.has(it.path));
        if(allIn) fileItems.forEach(it => _syncSel.delete(it.path));
        else fileItems.forEach(it => _syncSel.add(it.path));
        renderBlossom(); };
      const none = $('#ss-none', grid);
      if(none) none.onclick = () => { _syncSel.clear(); renderBlossom(); };
      const del = $('#ss-del', grid);
      if(del) del.onclick = async () => {
        const doomed = [..._syncSel];
        if(!doomed.length) return;
        /* One confirmation that says the reach and the safety net; one publish for the lot. */
        if(!await uiConfirm('Delete ' + doomed.length + ' file' + (doomed.length === 1 ? '' : 's')
          + ' on every device that syncs \u201c' + _S._syncRoot + '\u201d?\n\nEach device moves its copy '
          + 'into .pc-trash \u2014 nothing is erased outright. Entries whose bytes were never stored '
          + 'simply stop being asked for.', { ok:'Delete ' + doomed.length, danger:true })) return;
        del.disabled = true; del.textContent = 'deleting\u2026';
        try{
          const r = await PCSync.edit.removeMany(_S._syncRoot, doomed);
          const n = (r && r.removed) || 0;
          /* SAY WHAT WILL ACTUALLY HAPPEN, INCLUDING THE ASKING. Past 20 files a sweep no longer
             removes anything unattended — that floor is what stopped a wave of stale tombstones
             emptying three devices in turn — so "every device applies it on its next sweep" is
             wrong for exactly the bulk deletions people do here (searching "conflict" and clearing
             the lot). Reported as "chose delete all, says it will be picked up on next sweep, no
             files disappearing": the deletion WAS published, and every device was waiting to be
             asked. */
          toast(n >= 20
            ? 'marked ' + n + ' deleted \u2014 that is a bulk deletion, so each device asks before '
              + 'removing them: open Folder sync there and press Sync now'
            : 'marked ' + n + ' deleted \u2014 every device applies it on its next sweep');
          _syncSel.clear(); _syncSelOn = false;
          renderBlossom();
        }catch(e){
          /* innerHTML, not textContent — the label IS a sprite <use>, and textContent both drops
             the icon and (as it did here) invites an emoji to be typed in its place. Same trap as
             _syncDownload's button, one screen away. */
          del.disabled = false;
          del.innerHTML = '<svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>'
                        + 'Delete on every device';
          toast('nothing was deleted: ' + ((e && e.message) || e));
        }
      };
      /* MOVE TO… — every folder of this synced folder, from the manifest already on screen, plus a
       * new one. A move is a rename of the paths, so it is one write and every device carries it out
       * on its next sweep; nothing is downloaded or uploaded. */
      const mv = $('#ss-move', grid);
      if(mv) mv.onclick = () => {
        const moving = [..._syncSel];
        if(!moving.length) return;
        const folders = _syncFolderList(paths).filter(d => !moving.some(m => d === m || d.startsWith(m + '/')));
        const here = _S._syncPath || '';
        const opts = [['\u0000', '\u2302 ' + _S._syncRoot + ' (top level)']]
          .concat(folders.map(d => [d, '\ud83d\udcc1 ' + d]))
          .concat([['\u0001', '\u2795 New folder\u2026']]);
        openMenuPopover(mv, opts, async (v) => {
          let dir = v === '\u0000' ? '' : v;
          if(v === '\u0001'){
            const typed = await uiPrompt('Name of the new folder (use / for a subfolder)',
                                         { value: here ? here + '/' : '', ok:'Move here' });
            if(typed === null) return;
            dir = String(typed).trim().replace(/^\/+|\/+$/g, '');
            if(!dir){ toast('type a folder name'); return; }
          }
          mv.disabled = true;
          try{
            await _syncEdit('move', () => PCSync.edit.move(_S._syncRoot, moving, dir));
            toast('moved ' + moving.length + ' file' + (moving.length === 1 ? '' : 's') + ' to '
                  + (dir || _S._syncRoot) + ' \u2014 every device applies it on its next sweep');
            _syncSel.clear(); _syncSelOn = false;
            renderBlossom();
          }catch(_){ mv.disabled = false; }
        });
      };
      if(_syncSelOn){
        // Row click toggles the tick instead of downloading; the tick lives on the row class.
        const rowsOf = (path) => $$('.dlsync', grid).filter(b => b.dataset.path === path)
                                  .map(b => b.closest('.file-card, .fx-row'));
        const paintSel = () => {
          $$('.dlsync', grid).forEach(b => {
            const row = b.closest('.file-card, .fx-row');
            if(row) row.classList.toggle('selrow', _syncSel.has(b.dataset.path));
          });
          const c = $('#ss-count', grid); if(c) c.textContent = _syncSel.size + ' selected';
          const d = $('#ss-del', grid); if(d) d.disabled = !_syncSel.size;
          const m = $('#ss-move', grid); if(m) m.disabled = !_syncSel.size;
        };
        $$('.dlsync', grid).forEach(b => {
          const row = b.closest('.file-card, .fx-row');
          if(!row) return;
          row.onclick = (e) => {
            if(e.target.closest('.fc-acts') && !e.target.closest('.dlsync')) return;
            e.preventDefault(); e.stopPropagation();
            const p2 = b.dataset.path;
            if(_syncSel.has(p2)) _syncSel.delete(p2); else _syncSel.add(p2);
            paintSel();
          };
        });
        paintSel();
      }
    }
    $$('.file-card[data-dir]', grid).forEach(c=> c.onclick=(e)=>{
      if(e.target.closest('.fc-acts')) return;
      _fxRemember();
      _S._syncPath = _S._syncPath ? _S._syncPath + '/' + c.dataset.dir : c.dataset.dir; renderBlossom();
    });
    // Clicking a FILE downloads it, the same as pressing its ⬇. A row you can click that does
    // nothing is the thing that makes a list feel broken, and a synced file has no URL to open —
    // the bytes are ciphertext, so opening one IS decrypting and saving it.
    $$('.file-card:not(.isdir)', grid).forEach(c=> c.onclick=(e)=>{
      if(e.target.closest('.fc-acts') || e.target.closest('.selbox')) return;
      const b = c.querySelector('.dlsync'); if(!b) return;
      if(_previewable(_openFileName(b.dataset), b.dataset.mime)){
        e.preventDefault(); e.stopPropagation(); openPreviewFile(b.dataset, {sync:true}); return;
      }
      /* Ask when something of ours can open it, and keep Download as the last choice — that is what
       * this click did before, and a synced file has no URL to open any other way. */
      const hs = _handlersFor(b.dataset, { sync:true });
      if(!hs.length){ b.click(); return; }
      hs.push({ id:'dl', icon:'⬇', label:'Download', hint:'Decrypts and saves it to this device',
                run:()=>b.click() });
      _openWithSheet(b.dataset.name||'this file', hs);
    });
    const _chunksOf = (b) => (b.dataset.chunks ? b.dataset.chunks.split(',').filter(Boolean) : null);
    $$('.dlsync', grid).forEach(b=> b.onclick=(e)=>{ e.preventDefault(); e.stopPropagation();
      _syncDownload(b, b.dataset.sha, b.dataset.name, _chunksOf(b)); });

    /* RENAME. The path is what changes, so a folder is renamed by renaming everything under it — one
     * write, one confirmation, and the devices move that many files. The box is seeded with the leaf
     * only: this is a rename, and offering the whole path invites someone to retype a directory by
     * hand and quietly move the file somewhere else. */
    $$('.rnsync', grid).forEach(b=> b.onclick=async (e)=>{ e.preventDefault(); e.stopPropagation();
      const from = b.dataset.path, was = b.dataset.name || '';
      const to = await uiPrompt('Rename “' + was + '” — this renames it on every device that syncs “' + _S._syncRoot + '”.',
                                { value: was, ok:'Rename' });
      if(to === null) return;
      const leaf = String(to).trim();
      if(!leaf || leaf === was) return;
      if(leaf.includes('/')){ toast('a name cannot contain “/” — drag it on a device to move it'); return; }
      const cut = from.lastIndexOf('/');
      const parent = cut < 0 ? '' : from.slice(0, cut + 1);            // '' at the folder's root
      b.disabled = true;
      try{ await _syncEdit('rename', () => PCSync.edit.rename(_S._syncRoot, from, parent + leaf)); }
      catch(_){ b.disabled = false; }
    });
    /* DELETE. This is the one action on this screen that reaches other people's machines and takes
     * something away, so it names the number of files and it says where they go — every device moves
     * its copy into `.pc-trash/<date>/`, which is what makes this recoverable rather than final. */
    $$('.rmsync', grid).forEach(b=> b.onclick=async (e)=>{ e.preventDefault(); e.stopPropagation();
      const path = b.dataset.path, name = b.dataset.name || path, isDir = b.dataset.dir === '1';
      /* COUNTED NOW, not when the row was drawn. This screen can sit open while another device fills
       * the folder, and the number is what the user is agreeing to: a stale "3" over a live 403 is
       * how somebody approves deleting three files and loses four hundred — and `removed` accounting
       * for the shrink is exactly what carries that past the server's collapse guard unquestioned.
       * The same number goes to remove() as `expect`, which refuses if it has grown since. */
      let n = 1;
      try{ n = await PCSync.edit.count(_S._syncRoot, path); }
      catch(err){ toast('couldn’t read the folder: ' + ((err && err.message) || err)); return; }
      if(!n){ toast('that is already gone'); renderBlossom(); return; }
      const what = isDir ? ('“' + name + '” and the ' + n + ' file' + (n===1?'':'s') + ' in it') : ('“' + name + '”');
      if(!await uiConfirm('Delete ' + what + ' from “' + _S._syncRoot + '”?\n\nEvery device that syncs this '
                          + 'folder moves its copy to .pc-trash on its next sync — nothing is erased.')) return;
      b.disabled = true;
      try{ await _syncEdit('delete', () => PCSync.edit.remove(_S._syncRoot, path, n)); }
      catch(_){ b.disabled = false; }
    });
    /* "Save a copy to your drive" — the same _keepBytes every other save in the app uses, so a photo
     * kept from a synced folder lands in Posts and a track lands in the music library, exactly as it
     * would from anywhere else. */
    $$('.keepsync', grid).forEach(b=> b.onclick=async (e)=>{ e.preventDefault(); e.stopPropagation();
      const sha=b.dataset.sha, name=b.dataset.name||'file', chunks=_chunksOf(b);
      if(!sha && !(chunks && chunks.length)){ toast('this file has no stored copy yet'); return; }
      b.disabled=true;
      try{
        const bytes=await _syncFileBlob(sha, chunks);
        // fileFromBytes, not `new File(...)`: a synced blob is ciphertext with no type of its own,
        // so the NAME is the only thing that still knows what it is. Without it the copy is stored
        // untyped and the drive draws it as a generic 📎 — and a track would miss the music library.
        // exact: this is an ARCHIVE of a file that already exists — the drive must get the bytes
        // that were synced, not a re-encoded approximation of them under a different hash.
        const r=await _keepBytes(fileFromBytes(bytes, name), '', {exact:true});
        _keptToast(r);
      }catch(err){ toast('couldn’t save: '+((err&&err.message)||err)); }
      finally{ b.disabled=false; }
    });
    _bindThumbs(grid);
  }

  // Public tab — your Blossom blobs, organised into client-side folders. Drag-drop + folders + grid.
  /* The drive's home: folders, and how many files are in each.
   *
   * Deliberately does NOT fetch /list. The counts come from the index that is already in memory, so
   * opening Files is now instant instead of a round trip plus a card for every blob on the account —
   * which on a drive of any size was seconds of work to draw something nobody had asked for yet.
   * "All files" is still one click away; it is just no longer what you land on.
   */
  let _fxCountsRev=-1, _fxCountsCache={};
  function _fxFolderCounts(){
    if(_fxCountsRev===FilesIdx._rev) return _fxCountsCache;
    const out = {};
    const files = (FilesIdx._norm().files) || {};
    for(const sha in files){ const f = files[sha].folder || ''; out[f] = (out[f] || 0) + 1; }
    _fxCountsRev=FilesIdx._rev; _fxCountsCache=out; return out;
  }
  /* Disaster recovery lives under Drive check, not in the ordinary Files toolbar. A healthy drive
   * should never ask its owner to think about retained metadata generations; the check is where a
   * mismatch is diagnosed and therefore the only context in which this control makes sense. */
  async function _showFilesHistory(trigger){
    const old=trigger&&trigger.textContent;
    if(trigger){ trigger.disabled=true; trigger.textContent='Waiting for signer…'; }
    try{
      const rows=await FilesIdx.history();
      if(!rows.length){ if(trigger){ trigger.disabled=false; trigger.textContent=old; } toast('No older folder-list versions are stored yet.'); return; }
      modal(`<h3>Recover folder list</h3><p class="muted small">The server retains five earlier versions of your folder names and file metadata. Restoring one does not delete or re-upload any Blossom bytes, and the current version is retained so this can be undone.</p>
        <div class="fx-history-list">${rows.map(x=>`<button class="btn btn-ghost fx-history-row" data-slot="${Number(x.slot)}"><b>${x.n==null?'older version':(Number(x.n).toLocaleString()+' files')}</b><span class="muted small">${enc(new Date(Number(x.created_at||0)*1000).toLocaleString())}</span></button>`).join('')}</div>
        <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn btn-ghost" id="fx-history-cancel">Cancel</button></div>`, root=>{
        const cancel=$('#fx-history-cancel',root); if(cancel) cancel.onclick=closeModal;
        $$('.fx-history-row',root).forEach(b=>b.onclick=async()=>{
          const label=(b.querySelector('b')||{}).textContent||'this version';
          if(!await uiConfirm('Restore '+label+'?\n\nOnly folder names and file metadata change. Your current list is retained as another backup, and no stored files are deleted.')) return;
          closeModal(); toast('restoring folder list…');
          try{ await FilesIdx.restore(b.dataset.slot); _S._filesFolder=null; renderBlossom(); toast('folder list restored'); }
          catch(e){ toast('could not restore: '+String(e&&e.message||e)); }
        });
      });
    }catch(e){ if(trigger){ trigger.disabled=false; trigger.textContent='Try recovery again'; } toast('could not load folder-list history: '+String(e&&e.message||e)); }
  }
  function _renderDriveHome(pane){
    const counts = _fxFolderCounts();
    const known = Object.keys(counts).reduce((n, k) => n + counts[k], 0);
    const tile = (icon, label, sub, attr) =>
      `<button class="fx-home-tile" ${attr}>
         <span class="fx-home-ic">${icon}</span>
         <span class="fx-home-name">${enc(label)}</span>
         <span class="fx-home-sub muted small">${enc(sub)}</span>
       </button>`;
    const n = (k) => { const c = counts[k] || 0; return c ? (c + ' file' + (c === 1 ? '' : 's')) : 'empty'; };
    const folders = FilesIdx.folders().map(f =>
      tile(_fxFolderIcon(f), f, n(f), 'data-folder="' + enc(f) + '"')).join('');
    /* Synced folders are listed here too, because from the user's side they are simply more folders —
     * the fact that one is a Blossom folder and the other a sync manifest is our problem, not theirs.
     * `_syncPairs` may still be loading; the sidebar says so and this shelf just fills in on repaint. */
    const pairs = Array.isArray(_syncPairs) ? _syncPairs : [];
    /* A device-local mapping deliberately carries n:null until the account manifest has been
     * fetched.  String concatenation turned that honest unknown into the broken-looking
     * "null files" on the Files home screen.  Match the sidebar: show a count only when one was
     * actually supplied, and otherwise say why the folder is present. */
    const synced = pairs.map(f => {
      const count = Number.isFinite(f.n)
        ? (f.n + ' file' + (f.n === 1 ? '' : 's'))
        : 'synced on this device';
      return tile('🔄', f.key, count, 'data-synckey="' + enc(f.key) + '"');
    }).join('');
    const grid = $('#bl-grid', pane); if(!grid) return;
    /* HOW MUCH OF THE DRIVE YOU ARE USING, said plainly and at the top. The number was reachable
     * only by opening All files and adding up tiles, which for a drive that Folder Sync files
     * thousands of files into is not reachable at all. Summed from the SERVER's blob list, not the
     * index: the index knows what you named things, the server knows what is actually stored — and
     * that difference is the whole point of a storage figure. Reads "—" until /list has answered,
     * rather than a confident 0 B. */
    // Gated on the SIZES, not on _blobHave: the two are filled by different paths (renderPublicFiles
    // sets _blobHave alone), so keying the figure on the wrong one printed a confident "0 B stored"
    // on a drive with gigabytes in it — the exact thing an em dash is here to avoid.
    const haveSizes = _S._blobSizes.size > 0;
    const used = haveSizes ? _fxBytes([..._S._blobSizes.values()].reduce((a,b)=>a+b, 0)) : '—';
    const usedLine = `<div class="fx-used"><b>${enc(used)}</b> stored`
      + (_S._blobHave ? ` · ${_S._blobHave.size} file${_S._blobHave.size===1?'':'s'}` : ' · counting…')
      + ` <button class="btn btn-ghost small fx-refresh" title="Refresh encrypted drive metadata using your signer.">Refresh</button>`
      + ` <button class="btn btn-ghost small fx-check" title="Check every file in your drive against what the server actually holds. Changes nothing.">Check my drive</button></div>`;
    grid.innerHTML = '<div class="fx-home">'
      + usedLine
      + folders
      /* Synced roots already live in the source strip immediately above this landing page. Keep
       * the larger desktop shortcuts, but group them so mobile can avoid drawing the same folder
       * twice — once in the top strip and again as a giant button in the middle. */
      + (synced ? '<div class="fx-home-synced"><div class="fx-home-sec">Synced folders</div>' + synced + '</div>' : '')
      + '<div class="fx-home-sec">Everything</div>'
      + tile('🗂', 'All files', known ? ('at least ' + known + ' known') : 'browse the whole drive', 'data-folder=""')
      /* The machine's own disk, on the landing screen as well as in the sidebar — this is where
       * somebody arrives, and on PosterChanOS it is the source they reach for first. Absent where
       * there is no filesystem to browse. */
      + (_hostFs() ? '<div class="fx-home-sec">This computer</div>'
                   + tile('💻', 'Files on this computer', 'browse this machine', 'data-hosthome="1"') : '')
      + '</div>';
    { const cb = $('.fx-check', pane); if(cb) cb.onclick = () => driveCheck(cb); }
    { const rb = $('.fx-refresh', pane); if(rb) rb.onclick = async()=>{
      rb.disabled=true; rb.textContent='Waiting for signer…';
      try{ await FilesIdx.ensure(); await _ensureSyncPairs(); renderBlossom(); }
      catch(e){ rb.disabled=false; rb.textContent='Try again'; toast(String(e&&e.message||e)); }
    }; }
    $$('.fx-home-tile[data-folder]', pane).forEach(b => b.onclick = () => _fxOpenFolder(b.dataset.folder));
    $$('.fx-home-tile[data-synckey]', pane).forEach(b => b.onclick = () => _fxOpenSynced(b.dataset.synckey));
    $$('.fx-home-tile[data-hosthome]', pane).forEach(b => b.onclick = ()=>_openHostFiles());
  }

  /* OPENING A FILE ON THIS COMPUTER IN ONE OF OUR APPS — one function per app, called by the Files
   * chooser below AND by `pc-open` from a terminal (_openFromCommandLine). Two callers that each
   * carried their own copy of "how Office opens a local document" would drift the first time either
   * was edited, which is how the host chooser lost Office in the first place. Each one resolves when
   * the app owns the file and THROWS with the reason otherwise; the caller decides how to say it. */
  async function _hostOpenPreview(path, name, mime){
    const nm = name || String(path).split('/').pop() || path, type = mime || mimeForName(nm) || '';
    /* MEDIA IS STREAMED, EVERYTHING ELSE IS READ.
     *
     * `pcHost.read` pulls the WHOLE file through the bridge -- a synchronous read in the
     * desktop's main process, then the bytes into this heap. For a picture or a PDF that is
     * fine and it is what has always happened. For a video it is why one was a black box
     * with dead controls ("playing video ... is black, buttons hidden") and why a big one
     * would not open at all ("can't play .webm in file manager"): a Blob URL cannot be
     * range-requested, so the player can neither seek nor start before the last byte, and
     * past the bridge's ceiling the read is refused outright.
     *
     * `pcHost.fileUrl` is an address main.js serves with Accept-Ranges. Absent on a build
     * without the handler, and on the web, where this whole branch is unreachable -- so the
     * fallback is the path that was here before, not a failure. */
    const streamable = /^(video|audio)\//i.test(type)
                    || /\.(mp4|m4v|mov|webm|mkv|ogv|avi|3gp|mp3|m4a|aac|ogg|oga|opus|wav|flac)$/i.test(nm);
    const P = await _withModule('preview.js', 'PCPreview');
    if(streamable && window.pcHost && pcHost.fileUrl){
      if(!P || !P.open({ name:nm, mime:type, url:pcHost.fileUrl(path) }))
        throw new Error('nothing here can show that file');
      return true;
    }
    toast('opening…');
    const bytes = await window.pcHost.read(path, 256 * 1024 * 1024);
    const blob = new Blob([bytes], { type });
    if(!P || !P.open({ name:nm, mime:type || blob.type || '', blob }))
      throw new Error('nothing here can show that file');
    return true;
  }
  /* Same session machinery as a document on the drive; only the writer differs, and it goes back
   * as BYTES through `writeBytes`, because round-tripping a zip container through a string quietly
   * destroys it. `mtime` is the compare-and-swap guard for the first save. */
  async function _hostOpenOffice(path, name, mime, mtime){
    if(!(window.pcHost && pcHost.read)) throw new Error('this build cannot read a file on this computer');
    let openedMtime = Number(mtime) || 0;
    const bytes = await pcHost.read(path, 128 * 1024 * 1024);
    const nm = name || String(path).split('/').pop() || 'document';
    const f = fileFromBytes(bytes, nm, mime || mimeForName(nm) || '');
    await _officeSession(f, async (updated) => {
      if(!(pcHost.writeBytes)) throw new Error('this build cannot save back to this computer');
      const info=await pcHost.writeBytes(path,new Uint8Array(await updated.arrayBuffer()),openedMtime||0);
      if(info&&info.mtime)openedMtime=info.mtime;
    });
    return true;
  }
  /* Keep the entire lazy-open transaction guarded: a missing/stale packaged code.js used to reject
   * into the event loop, leaving Files on one side of the desktop with no editor and no
   * explanation. Do not switch views until Code confirms it owns a live buffer; a refused/binary
   * file remains safely where it was. */
  async function _hostOpenCode(path){
    const code = await _withModule('code.js', 'PCCode');
    if(!code || typeof code.openHostFile!=='function') throw new Error('the editor did not load');
    if(!(await code.openHostFile({ path }))) throw new Error('PosterChan Code would not open it');
    switchView('code');
    return true;
  }
  /* Files, on THIS COMPUTER, at `dir` — the crumb router's `h:` branch, as one call. */
  function _hostOpenFolder(dir){
    const H2 = _hostFs();
    if(!H2) throw new Error('this build has no access to this computer’s files');
    _fxRemember();
    _S._syncRoot=''; _S._syncPath=''; _S._filesFolder=null;
    H2.enter(dir);
    _S._hostOn = true; _S._filesTab = 'computer'; _S._fxMobileSource = 'computer';
    if(_S.VIEW === 'blossom') renderBlossom(); else switchView('blossom');
    return true;
  }
  /* `pc-open` (os/bin/pc-open → desktop/opener.js → here). `auto` picks what a click in Files
   * would: a picture/video/PDF opens in Preview, a document in Office, anything else in Code — the
   * editor is the safe fallback for every regular file — and a folder opens in Files. Asking for an
   * app that cannot take the file is an ERROR the terminal reports, never a silent substitute:
   * somebody who typed `pc-office notes.txt` wants to be told, not handed an editor. */
  const _PC_OPEN_APPS = { preview:'Preview', office:'Office', code:'Code', files:'Files' };
  async function _openFromCommandLine(req){
    const want = String((req && req.app) || 'auto');
    const items = Array.isArray(req && req.items) ? req.items : [];
    const out = [];
    for(const it of items){
      const path = String((it && it.path) || '');
      const name = path.split('/').pop() || path;
      const mime = mimeForName(name) || '';
      let app = want;
      if(app === 'auto'){
        app = it.kind === 'dir' ? 'files'
            : _previewable(name, mime) ? 'preview'
            : (_officeable(name, mime) && window.pcHost && pcHost.read) ? 'office'
            : 'code';
      }
      try{
        if(app === 'files'){
          const parent = path.slice(0, path.lastIndexOf('/')) || '/';
          _hostOpenFolder(it.kind === 'dir' ? path : parent);
        }
        else if(it.kind === 'dir') throw new Error('Is a directory (use --files)');
        else if(app === 'preview'){
          if(!_previewable(name, mime)) throw new Error('Preview shows pictures, video, audio and PDFs — not this');
          await _hostOpenPreview(path, name, mime);
        }
        else if(app === 'office'){
          if(!_officeable(name, mime)) throw new Error('PosterChan Office does not open this kind of file');
          await _hostOpenOffice(path, name, mime, Number(it.mtime) || 0);
        }
        else if(app === 'code') await _hostOpenCode(path);
        else throw new Error('unknown app: ' + app);
        out.push({ path, ok:true, app:_PC_OPEN_APPS[app] || app });
      }catch(e){
        out.push({ path, ok:false, app:_PC_OPEN_APPS[app] || app, why:String((e && e.message) || e) });
      }
    }
    return out;
  }
  /* The machine's own disk. The module draws it; this hands it the things that belong to the Files
   * screen — the sort comparator, the view mode, the byte formatter and the app's own prompts — so
   * a folder on this disk is sorted and shaped exactly like a folder on the drive. */
  async function _renderHostRoot(pane){
    const H2 = _hostFs();
    if(!H2){ _S._hostOn = false; return renderBlossom(); }
    /* The SAME shell every other source uses — `.fx-explorer` with a sidebar and a main pane. A
     * second layout here is how one screen ends up looking like two. */
    pane.innerHTML = '<div class="fx-explorer">'
      + '<div class="fx-side">' + _fxSideHTML() + '</div>'
      + '<div class="fx-main"><div id="host-pane"><div class="spinner"></div></div></div></div>';
    _fxBindSide(pane);
    /* THE APP'S OWN EXPLORER PARTS, HANDED OVER — not described, not re-implemented.
     *
     * The module used to draw its own `hf-bar`, `hf-crumbs`, `fx-tiles` and an `fx-details` table,
     * and MEASURED against the stylesheet not one of those class names exists: eleven classes, zero
     * rules between them. So this pane rendered as raw unstyled HTML — a bare table and bare
     * buttons — inside an explorer shell where every other source is a proper file grid. Reported,
     * fairly, as "the Local files implementation is complete ass and looks ugly".
     *
     * The fix is not a second stylesheet. It is passing in the four things that actually draw a
     * file list here — the toolbar, the sortable column header, one details row, and the type icon
     * — so the machine's disk is drawn by the same code as the drive and cannot look different
     * from it again. */
    await H2.render($('#host-pane', pane), {
      view: _fxView(),
      cmp: (keyOf) => _fxCompare(keyOf),
      fmtBytes: _fmtBytes,
      fmtDate: _fxWhen,
      bar: _fxBarHTML,
      cols: _fxColsHTML,
      row: _fxDetailsRow,
      icon: _fxIcon,
      folderIcon: () => _fxFileGlyph('folder'),
      typeName: _fxType,
      bindBar: () => _fxBindBar(pane),
      bindCols: _fxBindCols,
      query: () => _S._filesQ,
      shareFile: _shareHostFile,
      /* WHAT POSTERCHAN CODE CAN OPEN, answered by the same function the drive and the synced
       * folders use. hostfiles.js must not grow a second opinion about what "a text file" is. */
      /* Code is the safe fallback for every regular file.  Unknown and binary files may not be
       * pleasant to edit, but hiding the editor entirely made .conf files, PDFs and extensionless
       * project files impossible to inspect from the machine picker. */
      openable: () => true,
      openFile: _openHostFile,
      toast, prompt: uiPrompt, confirm: uiConfirm,
      menu: openMenuPopover, copy: copyValue,
    });
  }

  /* One of this computer's files, opened: Preview for what it can show, else the chooser. A named
   * function (it was an inline option of the Files pane) so a desktop SEARCH result opens a local
   * file exactly as a click in Files → This Computer does — see hostOpen. */
  async function _openHostFile(path, name, openHere, mime){
    {
        if(_previewable(name || path, mime)){
          try{ await _hostOpenPreview(path, name, mime); }
          catch(e){ toast('could not open that: ' + ((e && e.message) || e)); }
          return;
        }
        /* OFFICE WAS MISSING FROM THIS CHOOSER ENTIRELY. A document on the drive and a document in
         * a synced folder both had an Office button; a document on THIS COMPUTER offered only Code
         * (which refuses a .odt as binary) and "hand it to the machine" — so from Home, clicking an
         * .odt could not open it in the office suite this OS ships with. Same session machinery as
         * the other two; only the writer differs (see _hostOpenOffice). */
        const officeChoice = (_officeable(name || path, mime) && window.pcHost && pcHost.read) ? [{
          id:'office', icon:'📝', label:'PosterChan Office',
          hint:'Edit it here — saves straight back to this computer',
          run:async() => {
            try{ await _hostOpenOffice(path, name, mime, Number(openHere&&openHere.mtime)||0); }
            catch(err){ toast('could not open in Office: ' + ((err && err.message) || err)); }
          } }] : [];
        _openWithSheet(name || path, officeChoice.concat([{
        id:'code', icon:'&lt;/&gt;', label:'PosterChan Code',
        hint:'Edit it here — saves straight back to this computer',
        run:async() => {
          /* The chooser closes before running this, so every failure is caught HERE and said out
           * loud — see _hostOpenCode for why Files stays put until Code owns a live buffer. */
          try{ await _hostOpenCode(path); }
          catch(err){ toast('could not open in Code: ' + ((err && err.message) || err)); }
        } },
        /* Last on the list, and never absent: this is what clicking the file did before the editor
         * existed, and for most files it is still the answer. */
        { id:'host', icon:'🖥', label:'This computer',
          hint:'Hand it to whatever this machine opens that with',
          run:() => { if(openHere) openHere(); } }]));
    }
  }

  /* A DELIBERATE PUBLIC COPY from this machine into Blossom. The bridge read is bounded because it
   * crosses into the renderer as one buffer; uploadBlob also hashes and sends one buffer, so
   * pretending this path streams would only move the memory spike somewhere less visible. The
   * original filename and bytes are retained (`noCompress`), and `folder:'Shared'` indexes the
   * result under one predictable public folder before its URL reaches the clipboard. */
  async function _shareHostFile(entry){
    if(!entry || entry.dir) throw new Error('select one file to share');
    const cap = 256 * 1024 * 1024;
    if((+entry.size || 0) > cap)
      throw new Error('this file is over 256 MB; local sharing needs the streaming uploader first');
    const H = window.pcHost;
    if(!H || typeof H.read !== 'function') throw new Error('this build cannot read local files');
    _uploadBadge('Sharing ' + (entry.name || 'file') + ' with Blossom…');
    try{
      const bytes = await H.read(entry.path, cap);
      const file = new File([bytes], entry.name || 'file', { type:'application/octet-stream' });
      const url = await uploadBlob(file, { folder:'Shared', noCompress:true });
      await copyValue(url, 'shared to Blossom — URL copied', 'Shared URL:');
      _uploadBadge('Shared to Blossom — URL copied', true);
      return url;
    }catch(e){
      _uploadBadge(null);
      if(typeof _blossomDenied === 'function' && _blossomDenied(e)){
        requestBlossomAccess();
        throw new Error('no Blossom upload access yet — requested it from the admin');
      }
      throw e;
    }
  }

  let _filesRenderLoadedKey='';
  /* SEARCH THE FILE MANAGER, NOT ONE DRAWER.
   *
   * Each source keeps its own storage contract, so aggregation happens here and remains read-only:
   * Blossom names come from the encrypted client index, synced names from their manifests, and the
   * device uses the bounded native search bridge.  A hit routes into its real source; it is never a
   * synthetic fourth filesystem. */
  async function _renderFilesEverywhere(pane){
    const q = _S._filesQ.trim(), seq = ++_fxSearchSeq;
    pane.innerHTML = '<div class="fx-explorer"><div class="fx-side">' + _fxSideHTML() + '</div>'
      + '<div class="fx-main">' + _fxBarHTML([{label:'Search everywhere',to:_fxWhere()}], _fxHist.length > 0, false)
      + '<div class="fx-search-status muted small">Searching Blossom, Synced Folders and My Computer…</div>'
      + '<div class="fx-search-results" id="fx-search-results"><div class="spinner"></div></div></div></div>';
    _fxBindSide(pane); _fxBindBar(pane);
    const blossom = [], synced = [];
    let local = [], drive = Array.isArray(_S._filesGridList) ? _S._filesGridList : null;
    const server = mediaServer();
    const jobs = [];
    if(!drive && server){
      jobs.push(fetch(server.replace(/\/$/,'') + '/list/' + _S.ME.pubkey, {cache:'no-store'})
        .then(r => { if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .then(rows => { if(Array.isArray(rows)) drive = _S._filesGridList = rows; }).catch(()=>{}));
    }
    _adoptSyncPairs();
    const pairs = Array.isArray(_syncPairs) ? _syncPairs.slice() : [];
    jobs.push(...pairs.map(pair => _syncManifest(pair.key).then(paths => {
      for(const path in (paths || {})){
        const e=paths[path]; if(!e || e.deletedAt || !_fxMatch(path.split('/').pop())) continue;
        synced.push({ source:'synced', root:pair.key, path, name:path.split('/').pop(),
          folder:path.split('/').slice(0,-1).join('/'), size:+e.size||0, modified:+e.mtime||0 });
      }
    }).catch(()=>{})));
    if(window.pcHost && typeof pcHost.search === 'function') jobs.push(
      pcHost.search(q, {limit:200}).then(rows => { if(Array.isArray(rows)) local=rows; }).catch(()=>{}));
    await Promise.allSettled(jobs);
    if(seq !== _fxSearchSeq || _S._filesQ.trim() !== q || !pane.isConnected) return;
    for(const b of (drive || [])){
      if(!b || !b.sha256) continue;
      const name=_fxBlobName(b); if(!_fxMatch(name)) continue;
      blossom.push({ source:'blossom', sha:b.sha256, name:name||b.sha256, folder:FilesIdx.folderOf(b.sha256)||'',
        size:+b.size||0, modified:+b.uploaded||0 });
    }
    const rows = blossom.concat(synced, local.map(e => Object.assign({source:'computer'},e)));
    const resultCmp = _fxCompare(_fxSearchKey);
    // Match each source's ordinary listing: folders stay above files, then the selected column and
    // direction apply. Previously search always forced Name A-Z while displaying the sort control.
    rows.sort((a,b) => (!!a.dir !== !!b.dir) ? (a.dir ? -1 : 1) : resultCmp(a,b));
    const results=$('#fx-search-results',pane), status=$('.fx-search-status',pane);
    if(status) status.textContent=rows.length+' result'+(rows.length===1?'':'s')+' across all locations';
    if(!results) return;
    const sourceName = {blossom:'Blossom',synced:'Synced Folders',computer:'My Computer'};
    results.innerHTML = rows.length ? rows.map((r,i)=>{
      const where=r.source==='synced' ? (r.root+(r.folder?' / '+r.folder:''))
        : r.source==='computer' ? String(r.path||'') : (r.folder||'All files');
      return `<button class="fx-search-hit" data-hit="${i}"><span class="fx-search-source">${enc(sourceName[r.source]||r.source)}</span>`
        + `<span class="fx-search-name">${_fxIcon('',r.mime||'')}${enc(r.name||r.path||'file')}</span>`
        + `<span class="fx-search-path">${enc(where)}</span><span class="fx-search-size">${r.dir?'Folder':_fxBytes(r.size)}</span>`
        + `<span class="fx-search-date">${enc(_fxWhen(r.created||r.modified||r.mtime))}</span></button>`;
    }).join('') : `<div class="empty">Nothing in Blossom, Synced Folders or My Computer matches “${enc(q)}”.</div>`;
    $$('.fx-search-hit',results).forEach(b=>b.onclick=()=>{
      const r=rows[+b.dataset.hit]; if(!r) return;
      _fxRemember(); _S._filesQ='';
      if(r.source==='blossom'){ _S._hostOn=false; _S._syncRoot=''; _S._syncPath=''; _S._filesFolder=r.folder||''; _S._fxMobileSource='blossom'; }
      else if(r.source==='synced'){ _S._hostOn=false; _S._syncRoot=r.root; _S._syncPath=r.folder||''; _S._fxMobileSource='synced'; }
      else { const H=_hostFs(); if(H){ const p=String(r.path||'');
          /* Do not derive a native parent by slicing separators here. `/file` has its separator at
           * zero and `C:\\file` needs to retain the root slash; both used to route back into an
           * invalid file path after choosing a unified-search hit. The host module already owns
           * cross-platform path ancestry for breadcrumbs and Up, so search uses the same rule. */
          H.enter(r.dir?p:(H.parentPath(p)||p)); } _S._hostOn=true; _S._syncRoot=''; _S._filesFolder=null; _S._fxMobileSource='computer'; }
      renderBlossom();
    });
  }
  async function renderPublicFiles(pane){
    const server=mediaServer();
    if(!server){ pane.innerHTML='<div class="empty">Blossom server not configured.</div>'; return; }
    _adoptSyncPairs();
    /* One visit can repaint several times while permission, remote-index and synced-folder probes
     * settle. Re-parsing a large local index for each repaint freezes Electron's only UI thread.
     * Other callers keep loadLocal's ordinary fresh-read semantics; only this repaint loop is
     * deduplicated, and changing accounts gives it a different key. */
    const renderKey=FilesIdx._key();
    if(_filesRenderLoadedKey!==renderKey){ FilesIdx.loadLocal(); _filesRenderLoadedKey=renderKey; }
    if(_S._filesQ.trim()) return _renderFilesEverywhere(pane);
    // Remote drive metadata is encrypted. The explicit Refresh control requests it; opening Files
    // itself stays instant and never waits on a signer.
    /* A synced folder is a different SOURCE, not a different folder of the drive: its list comes from
     * the sync manifest, not from Blossom's /list. Branch BEFORE the upload probe and the listing —
     * neither is anything to do with it, and both are a round trip. */
    if(_S._hostOn) return _renderHostRoot(pane);
    if(_S._syncRoot) return _renderSyncedRoot(pane);
    pane.innerHTML='<div class="spinner"></div>';
    // Anything that throws below leaves this spinner on screen forever unless it is caught, and
    // "a spinner that never stops" tells the user nothing and tells us less. Surface it instead.
    /* Optimistic controls are intentional: no signature or network request happens until the user
     * chooses files. The real PUT reports denial and offers/request access on that explicit action. */
    const canUp=true;
    const head = canUp
      ? `<div class="drop-zone" id="bl-drop"><input type="file" id="bl-file" multiple ${_S._filesFolder==='Music'?'accept="audio/*,.mp3,.m4a,.m4b,.aac,.flac,.wav,.ogg,.oga,.opus,.wma,.aif,.aiff,.mka,.ape,.dsf"':''} hidden><input type="file" id="bl-folder" webkitdirectory hidden>
          <div class="dz-inner"><span class="dz-ic">⬆</span> Drop files/folders here, or <button class="btn btn-cyan small" id="bl-pick">choose files</button> <button class="btn btn-neon small" id="bl-pickfolder"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>choose folder</button>
          <div class="muted small">→ ${_S._filesFolder?((FilesIdx.isEncFolder(_S._filesFolder)?'🔒 ':'📁 ')+enc(_S._filesFolder)):'All files'} · uploaded one at a time${_S._filesFolder==='Music'?' · non-audio skipped':(FilesIdx.isEncFolder(_S._filesFolder)?' · encrypted on this device':'')}</div></div>
          <div class="up-queue" id="bl-queue"></div></div>`
      : `<div class="blossom-locked glass"><b>🔒 Upload access needed</b>
           <p class="muted small">You don't have permission to upload files to this server yet. Request access and the admin can grant it from Admin → Users.</p>
           <button class="btn btn-cyan" id="bl-request"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Request upload access</button></div>`;
    /* EXPLORER LAYOUT. The folder list is a left PANE and everything else a right one, which is the
     * shape every file manager has settled on because it is the one that scales: a row of chips wraps
     * into three lines the moment you have eight folders, and then the files start below the fold.
     * Same markup, same handlers — the chips are still .folder-chip[data-folder] — so nothing about
     * uploading, selecting or deleting changes; only where they sit. Collapses back to a single
     * column on a phone, where a 220px sidebar would leave nothing for the files. */
    pane.innerHTML = '<div class="fx-explorer">'
      + '<div class="fx-side">' + _fxSideHTML() + '</div>'
      + '<div class="fx-main">' + _fxBarHTML(_fxCrumbs(), _fxHist.length > 0, true) + head
      + '<div class="files-selbar" id="bl-selbar"></div>'
      + '<div class="files-grid" id="bl-grid"><div class="spinner"></div></div></div></div>';
    _fxBindSide(pane); _fxBindBar(pane);
    if(canUp){
      const fileInput=$('#bl-file',pane), folderInput=$('#bl-folder',pane), drop=$('#bl-drop',pane);
      $('#bl-pick',pane).onclick=()=>fileInput.click();
      { const fb=$('#bl-pickfolder',pane); if(fb) fb.onclick=()=>folderInput&&folderInput.click(); }
      fileInput.onchange=()=>{ const fs=[...fileInput.files]; fileInput.value=''; uploadFilesSeq(fs); };
      if(folderInput){ try{ folderInput.webkitdirectory=true; folderInput.setAttribute('webkitdirectory',''); folderInput.setAttribute('directory',''); }catch(_){}
        folderInput.onchange=()=>{ const fs=[...folderInput.files]; folderInput.value=''; uploadFilesSeq(fs); }; }
      drop.ondragover=e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); drop.classList.add('over'); } };
      drop.ondragleave=()=>drop.classList.remove('over');
      drop.ondrop=e=>{ e.preventDefault(); drop.classList.remove('over');
        const dt=e.dataTransfer, items=dt&&dt.items, entries=[];
        // capture FileSystem entries SYNCHRONOUSLY (invalid after the event) so dropped FOLDERS recurse
        if(items&&items.length&&items[0].webkitGetAsEntry){ for(let i=0;i<items.length;i++){ const en=items[i].webkitGetAsEntry(); if(en) entries.push(en); } }
        if(entries.length){ _walkEntries(entries).then(fs=>{ if(fs.length) uploadFilesSeq(fs); }); }
        else { const fs=[...((dt&&dt.files)||[])]; if(fs.length) uploadFilesSeq(fs); }
      };
    } else { const rb=$('#bl-request',pane); if(rb) rb.onclick=()=>requestBlossomAccess(rb); }
    /* HOME: folder tiles in the grid, and NOTHING fetched. It used to return before this whole
     * function ran, which took the drop zone with it — so on the one screen that says "your folders"
     * there was no way to put anything in one. The uploader is built above, so it is here now;
     * only the /list and the file grid are skipped, which is what made the landing instant. */
    if(_S._filesFolder === null){ _renderDriveHome(pane); return; }
    /* STALE-WHILE-REVALIDATE. The blob listing is public metadata and already cached in this
     * session; folder changes only filter it through the encrypted local index. Paint that copy
     * immediately, then refresh it. A media host or CORS proxy that stops answering must not turn
     * a drive we just displayed into an infinite spinner. */
    const cachedList=Array.isArray(_S._filesGridList)?_S._filesGridList:null;
    if(cachedList){
      try{ if(_S._filesFolder==='Music') _renderMusicList($('#bl-grid',pane),cachedList,_S._filesQ);
        else _renderFilesGrid($('#bl-grid',pane),cachedList); }catch(_){}
    }
    let list=null;
    const ctl=typeof AbortController!=='undefined'?new AbortController():null;
    const listTimer=ctl?setTimeout(()=>ctl.abort(),12000):null;
    try{ const r=await fetch(server+'/list/'+_S.ME.pubkey,
      Object.assign({cache:'no-store'},ctl?{signal:ctl.signal}:{}));
      if(!r.ok) throw new Error('HTTP '+r.status); list=await r.json();
      if(!Array.isArray(list)) throw new Error('invalid response'); }
    catch(e){
      /* Keep a successfully painted cached list. With no copy, replace the spinner with a useful
       * bounded failure and a retry button—never leave animation pretending work is continuing. */
      if(!cachedList){ const g=$('#bl-grid',pane); if(g){
        g.innerHTML='<div class="empty">Couldn\'t load files from '+enc(server)+' ('+enc(e&&e.name==='AbortError'?'timed out':e.message)+'). <button class="btn btn-ghost small" id="bl-list-retry">Retry</button></div>';
        const retry=$('#bl-list-retry',g);if(retry)retry.onclick=()=>renderBlossom();
      } }
    }finally{ if(listTimer)clearTimeout(listTimer); }
    if(list!==null){
      /* Blossom list responses need not repeat an absolute URL. Build the canonical blob address
       * once so thumbnails, Preview, copy and download all consume a complete entry. */
      list = list.filter(b=>b && b.sha256).map(b => Object.assign({}, b, {
        url:b.url || (server.replace(/\/$/,'') + '/' + b.sha256)
      }));
      _S._blobHave=new Set(list.map(b=>b.sha256));   // reuse this fetch for the music player's existence check
      // …and the SIZES, which the drive home totals. Filled here as well as in _refreshBlobHave
      // because this is the fetch the Files screen actually makes; keying the figure on the other
      // one is what printed "0 B stored" on a full drive.
      _S._blobSizes=new Map(list.map(b=>[b.sha256, Number(b.size)||0]));
      _backfillPostFolder(list);
      // Guarded: a throw in the grid renderer used to escape renderPublicFiles and leave the grid
      // spinner spinning with no error anywhere the user could see.
      try{
        // Cache the list HERE, for both renderers. It used to be set only inside _renderFilesGrid, so
        // in the Music folder it held the previous folder's list (or nothing) — and the search box's
        // fast path, which exists precisely to re-filter without a network round trip, either drew the
        // wrong folder or fell through to a full Blossom /list PER KEYSTROKE. That is the "it searches
        // again after every character I type" on a drive with thousands of blobs.
        _S._filesGridList = list;
        if(_S._filesFolder==='Music') _renderMusicList($('#bl-grid',pane), list, _S._filesQ); else _renderFilesGrid($('#bl-grid',pane), list);
      }catch(e){
        console.error('blossom: grid render failed', e);
        const g=$('#bl-grid',pane); if(g) g.innerHTML='<div class="empty">Couldn\'t draw your files ('+enc(e.message||'error')+'). Your files are safe — reload to try again.</div>';
      } }
    // Label saved-stream recordings ("Past streams") so they don't show as anonymous video blobs. This is
    // a cosmetic cross-reference of the user's own VOD list — fetch it ONCE (cached), in the BACKGROUND,
    // and re-render the grid when it lands. Never block the drive's first paint on this (a slow/hung
    // /api/streams/vods must not leave the drive blank), and never re-fetch on every folder switch/move.
    if(list!==null && _S._filesFolder!=='Music'){
      _ensureVodNames().then(changed=>{
        if(!changed || _S.VIEW!=='blossom' || _S._filesTab!=='public' || _S._filesFolder==='Music') return;
        // Apply the freshly-fetched labels onto the CURRENT grid IN PLACE — no re-render, so this can't
        // revert an in-window blob add/remove, flash a spinner, or wipe the upload-progress queue.
        const grid=document.getElementById('bl-grid'); if(!grid) return;
        for(const sha in _S._vodNameMap){
          if((FilesIdx.meta(sha)||{}).name) continue;                      // a user-named file keeps its own name
          const card=grid.querySelector('.file-card[data-sha="'+sha+'"]'); if(!card) continue;
          const span=card.querySelector('.meta > span'); if(span){ span.textContent=_S._vodNameMap[sha].slice(0,18); span.title=_S._vodNameMap[sha]; }
          // ...and the download button, or a recording would still save under its hash after the
          // label said "Stream <date>".
          const dl=card.querySelector('.dlbtn');
          if(dl && dl.dataset.name) dl.dataset.name=downloadName({sha256:sha, url:dl.dataset.url}, _S._vodNameMap[sha], '');
        }
      });
    }
  }
  let _vodNamesInflight=false; // a fetch is in progress — blocks a concurrent second fetch from a fast re-render
  let _vodNamesAt=0;           // last-fetch time. TTL-cached so we DON'T refetch every folder-switch, but DO pick up a VOD recorded later this session and recover from an early auth-race failure
  async function _ensureVodNames(){
    if(_vodNamesInflight) return false;
    if(_vodNamesAt && (Date.now()-_vodNamesAt < 60000)) return false;   // cached & fresh → nothing new to apply
    _vodNamesInflight=true; _vodNamesAt=Date.now();                     // stamp up-front → at most ONE attempt per 60s (success OR failure) — no refetch storm on a persistently-failing endpoint
    let changed=false;
    try{ const vr=await _streamFetch('/api/streams/vods');
      if(vr && vr.ok){ const vj=await vr.json(); const m={};
        for(const v of (vj.vods||[])){ if(!v.sha256) continue; const t=+v.started_at||0; const d=new Date(t*1000);
          // With a real start time → "Stream <date> <time>"; without one, fall back to a short sha so two
          // timestamp-less recordings don't collapse to an identical bare "Stream" label.
          m[v.sha256]='Stream '+((!t||isNaN(d.getTime())) ? v.sha256.slice(0,6) : (d.toLocaleDateString()+' '+d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}))); }
        _S._vodNameMap=m; changed=true; } }
    catch(_){}
    _vodNamesInflight=false;
    return changed;            // failure keeps the stamp → next retry after the 60s TTL (bounded), labels just wait
  }
  let _postFolderBackfilled=false;
  /* Older composer builds uploaded media successfully but did not put it in the Files index.  The
   * blob therefore appears under All while Posts looks almost empty. Recover metadata from the
   * account's already-cached social history: only URLs that name a blob this account actually owns
   * are admitted, and an existing user-chosen folder always wins. */
  function _backfillPostFolder(list){
    if(_postFolderBackfilled) return 0;
    _postFolderBackfilled=true;
    const have=new Map((list||[]).filter(b=>b&&b.sha256).map(b=>[String(b.sha256).toLowerCase(),b]));
    let evs=[]; try{ evs=Store.query([{authors:[_S.ME.pubkey],kinds:[1,20,30023,30402,34235,34236],limit:10000}])||[]; }catch(_){}
    const found=new Map();
    for(const e of evs){
      const bits=[String(e.content||'')];
      for(const t of (e.tags||[])) if(['imeta','url','image','thumb'].includes(t[0])) bits.push(t.slice(1).join(' '));
      for(const bit of bits) for(const u of (bit.match(/https?:\/\/[^\s<>"']+/g)||[])){
        const m=u.match(/(?:^|\/)([0-9a-f]{64})(?:\.[a-z0-9]{1,10})?(?:[?#]|$)/i);
        if(m && have.has(m[1].toLowerCase())) found.set(m[1].toLowerCase(),u);
      }
    }
    const add=[...found].filter(([sha])=>!FilesIdx.meta(sha));
    if(!add.length) return 0;
    FilesIdx.beginBatch();
    try{
      if(!FilesIdx.folders().includes('Posts')) FilesIdx.addFolder('Posts');
      for(const [sha,u] of add){ const b=have.get(sha)||{}; let name=b.name||'';
        if(!name) try{ name=decodeURIComponent(new URL(u).pathname.split('/').pop()||''); }catch(_){}
        FilesIdx.setFile(sha,{name:name||('post-'+sha.slice(0,8)),folder:'Posts',mime:b.type||'',size:+b.size||0,ts:+b.created_at||Math.floor(Date.now()/1000)});
      }
    }finally{ FilesIdx.endBatch().catch(()=>{}); }
    return add.length;
  }
  function _filesSelClear(){ _filesSel.clear(); }
  // The visible, selectable shas for the CURRENT folder+page — "Select all" must mean what's on
  // screen, never the whole drive (3000+ blobs, where a mis-tap would be catastrophic).
  function _filesVisibleShas(grid){
    return [...grid.querySelectorAll('.file-card[data-sha]')].map(c=>c.dataset.sha);
  }
  function _filesSelBar(grid, list){
    const bar=grid.parentNode && grid.parentNode.querySelector('#bl-selbar'); if(!bar) return;
    const vis=_filesVisibleShas(grid), n=_filesSel.size;
    grid.classList.toggle('selmode', n>0);   // drives the card cursor (tap = select, not open)
    const allSel = vis.length && vis.every(sha=>_filesSel.has(sha));
    bar.innerHTML = `<button class="btn btn-ghost small" id="bl-selall">${allSel?'☑':'☐'} Select all${vis.length?' ('+vis.length+')':''}</button>`
      + `<button class="btn btn-ghost small" id="bl-selnone"${n?'':' disabled'}><svg class="ic b-ic" aria-hidden="true"><use href="#i-close"></use></svg>Select none</button>`
      + `<span class="muted small" style="margin:0 4px">${n?(n+' selected'):'none selected'}</span>`
      + `<button class="btn btn-cyan small" id="bl-seldl"${n?'':' disabled'}><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Download</button>`
      + `<button class="btn btn-cyan small" id="bl-selmove"${n?'':' disabled'}><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Move</button>`
      + `<button class="btn btn-neon small" id="bl-seldel"${n?'':' disabled'} style="color:var(--danger)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>`;
    bar.querySelector('#bl-selall').onclick=()=>{ if(allSel) vis.forEach(sha=>_filesSel.delete(sha)); else vis.forEach(sha=>_filesSel.add(sha)); _renderFilesGrid(grid, list); };
    bar.querySelector('#bl-selnone').onclick=()=>{ _filesSelClear(); _renderFilesGrid(grid, list); };
    bar.querySelector('#bl-seldl').onclick=e=>_filesMassDownload(e.currentTarget, grid, list);
    bar.querySelector('#bl-selmove').onclick=e=>_filesMassMove(e.currentTarget, grid, list);
    bar.querySelector('#bl-seldel').onclick=e=>_filesMassDelete(e.currentTarget, grid, list);
  }
  // Delete every selected blob. ONE confirm for the whole batch (not one per file), and it names the
  // count — a per-file prompt for 40 files is worse than no prompt, people click through it.
  // Sequential, not Promise.all: a Blossom server answering 40 parallel signed DELETEs is a great way
  // to get rate-limited half-way and leave the index disagreeing with the server.
  async function _filesMassDelete(btn, grid, list){
    const shas=[..._filesSel]; if(!shas.length) return;
    if(!await uiConfirm('Delete '+shas.length+' file'+(shas.length>1?'s':'')+'? This removes the bytes from the server and cannot be undone.')) return;
    const server=mediaServer(); let ok=0, fail=0;
    btn.disabled=true;
    for(let i=0;i<shas.length;i++){
      const sha=shas[i]; btn.textContent='deleting '+(i+1)+'/'+shas.length+'…';
      try{
        const auth=await sign(24242,'Delete blob',[['t','delete'],['x',sha],['expiration',String(Math.floor(Date.now()/1000)+3600)]]);
        const res=await fetch(server+'/'+sha,{ method:'DELETE', headers:{'Authorization':'Nostr '+btoa(JSON.stringify(auth))} });
        // 404 = already gone server-side, but still drop it from the index (same rule as delBlob).
        if(res.ok || res.status===404){ FilesIdx.forget(sha); _filesDeleted.add(sha); delete _trackUrls[sha]; ok++; }
        else fail++;
      }catch(_){ fail++; }
    }
    _filesSelClear();
    toast(fail? ('deleted '+ok+', '+fail+' failed') : ('deleted '+ok+' file'+(ok>1?'s':'')));
    renderBlossom();
  }
  // Move every selected file into one folder. Index-only (no bytes move), so this is fast and safe.
  function _filesMassMove(btn, grid, list){
    const shas=[..._filesSel]; if(!shas.length) return;
    const opts=[['__all','🗂 All']].concat(FilesIdx.folders().map(f=>[f,(f==='Music'?'🎵 ':'📁 ')+f]));
    openMenuPopover(btn, opts, v=>{
      shas.forEach(sha=>FilesIdx.move(sha, v==='__all'?'':v));
      _filesSelClear(); toast('moved '+shas.length+' file'+(shas.length>1?'s':'')); renderBlossom();
    });
  }
  // Download every selected file, one at a time. Browsers throttle/block a burst of simultaneous
  // downloads, so this paces them and skips ENCRYPTED files (those need the decrypt path, which
  // prompts per file — offering them here would silently save unreadable ciphertext).
  async function _filesMassDownload(btn, grid, list){
    const shas=[..._filesSel]; if(!shas.length) return;
    const byId={}; (list||[]).forEach(b=>byId[b.sha256]=b);
    let n=0, skipped=0;
    btn.disabled=true;
    for(const sha of shas){
      const b=byId[sha]; if(!b){ skipped++; continue; }
      const m=FilesIdx.meta(sha)||{};
      if(m.enc){ skipped++; continue; }                       // ciphertext — use the per-file ⬇ (decrypts)
      const nm=m.name||b.name||_S._vodNameMap[sha]||'';
      const ext=extOfBlob(b, m.name?m:{name:nm, mime:m.mime});
      n++; btn.textContent='downloading '+n+'/'+shas.length+'…';
      try{ await downloadBlobFile(b.url, downloadName(b, nm, ext)); }catch(_){}
      await new Promise(r=>setTimeout(r, 400));               // pace — a burst gets blocked as a popup storm
    }
    btn.disabled=false; btn.textContent='⬇ Download';
    toast(skipped? ('downloaded '+n+' · skipped '+skipped+' (encrypted — use the per-file ⬇)') : ('downloaded '+n));
  }

  /* Open an Office document through the node's built-in CODE server.  The working copy is always
   * assembled in this browser: encrypted drive files are decrypted here, and no key is sent to the
   * WOPI host. Saving pulls CODE's current bytes back through the ordinary drive upload pipeline. */
  /* OPEN A FILE FROM Files → Blossom IN POSTERCHAN CODE.
   *
   * Same shape as openOfficeFile below — fetch, decrypt if it is one of ours, hand it over — except
   * the editor is ours and takes text rather than a WOPI session. Binary is refused by looking at
   * the BYTES rather than trusting the name: a .txt that is actually a zip would otherwise open as
   * mojibake and be saved back as a broken file. */
  async function openCodeFile(d){
    try{
      toast(d.enc === '1' ? 'decrypting…' : 'opening…');
      let blob;
      if(d.enc === '1'){
        const u = await encFileUrl(d.sha, d.mime || mimeForName(d.name));
        blob = await fetch(u).then(r => r.blob());
      }else{
        const r = await fetch(d.url); if(!r.ok) throw new Error('file HTTP ' + r.status);
        blob = await r.blob();
      }
      if(blob.size > _CODE_MAX) throw new Error('that file is too big to edit here');
      const bytes = new Uint8Array(await blob.arrayBuffer());
      // A NUL byte is the oldest and most reliable "this is not text" there is.
      if(bytes.indexOf(0) !== -1) throw new Error('that looks like a binary file');
      const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
      const code = await _withModule('code.js', 'PCCode');
      if(!(code && code.openBlob)) throw new Error('the editor did not load');
      code.openBlob({ sha: d.sha, name: d.name || 'document', mime: d.mime || blob.type,
                        enc: d.enc || '0', text });
      switchView('code');
    }catch(err){ toast('could not open: ' + ((err && err.message) || err)); }
  }

  /* SAVE A CODE BUFFER BACK TO Files. Content addressing means an edit is a NEW blob, so this
   * mirrors the office save exactly: upload, re-point the drive index at the new hash under the same
   * name and folder, and forget the old entry — the old blob itself stays on Blossom, recoverable.
   * Lives here rather than in code.js because the index, the encryption and the folder all do. */
  async function saveBlobDoc(desc, text){
    if(!desc || !desc.sha) throw new Error('nothing to save');
    const name = desc.name || 'document';
    const mime = desc.mime || mimeForName(name) || 'text/plain';
    const updated = fileFromBytes(new TextEncoder().encode(text), name, mime);
    /* A SYNCED FILE GOES BACK TO THE FOLDER, ON EVERY DEVICE — not onto the drive. Same call the
     * folder's own uploader makes, so it is one write path with one set of guards; `replace` is
     * explicit because this path already exists by definition and overwriting it is the whole
     * intent. Returns no drive sha: there is no drive entry to re-point. */
    if(desc.sync && desc.sync.key){
      if(!(window.PCSync && PCSync.edit && PCSync.edit.uploadMany))
        throw new Error('this build cannot write to a synced folder');
      const full = String(desc.sync.path || name);
      const cut = full.lastIndexOf('/');
      const dir = cut < 0 ? '' : full.slice(0, cut);
      const r = await PCSync.edit.uploadMany(desc.sync.key, dir, [updated], { replace: true });
      if(r && r.failed && r.failed.length) throw new Error('the folder refused the write');
      try{ _syncManifests.delete(desc.sync.key); }catch(_){}
      try{ if(_S.VIEW === 'blossom' && _S._syncRoot === desc.sync.key) renderBlossom(); }catch(_){}
      return '';
    }
    const old = FilesIdx.meta(desc.sha) || {}, folder = old.folder || FilesIdx.folderOf(desc.sha) || '';
    let newSha = '';
    if(desc.enc === '1') newSha = await uploadEncFile(updated, folder, null);
    else {
      const url = await uploadBlob(updated, { noCompress: true });
      newSha = _shaFromUrl(url);
      FilesIdx.setFile(newSha, { name, folder, mime, size: updated.size,
                                 ts: Math.floor(Date.now() / 1000) });
    }
    if(newSha && newSha !== desc.sha) FilesIdx.forget(desc.sha);
    try{ if(_S.VIEW === 'blossom') renderBlossom(); }catch(_){}
    return newSha;
  }

  /* OPEN WITH… — one door, and the choice is made where you can see the file's name.
   *
   * Two small icon buttons per tile was the wrong shape twice over: they are easy to miss, they
   * crowd a card that is meant to be an icon and a name, and they force the decision before you
   * have said "open" at all. A file manager asks AFTER: you open a thing, and if more than one
   * program handles it you are asked which.
   *
   * `handlers` is a list of {id,label,hint,run}. With one handler this opens it and shows nothing —
   * a chooser with a single choice is a dialog that wastes a click. */
  function _openWithSheet(name, handlers){
    /* An advertised handler without an action is worse than no handler: it draws a convincing row,
     * closes the chooser when tapped, then throws `run is not a function` into the event loop. Keep
     * malformed/lazy integration entries out, and route both synchronous and async launch failures
     * to one visible verdict. */
    handlers = (handlers || []).filter(h=>h && typeof h.run==='function');
    if(!handlers.length){ toast('nothing here can open that file'); return; }
    const run = h => { try{
      const pending=h.run();
      if(pending && typeof pending.catch==='function') pending.catch(e=>toast('could not open: '+((e&&e.message)||e)));
    }catch(e){ toast('could not open: '+((e&&e.message)||e)); } };
    if(handlers.length === 1){ run(handlers[0]); return; }
    modal(`<h3 class="cmp-hd">Open “${enc(name)}”<button class="modal-x" id="ow-x" title="Close" aria-label="Close">&#215;</button></h3>
      <div class="openwith">${handlers.map(h =>
        `<button class="ow-opt" data-ow="${enc(h.id)}"><span class="ow-ic">${h.icon}</span>
           <span class="ow-t"><b>${enc(h.label)}</b><i>${enc(h.hint||'')}</i></span></button>`).join('')}</div>`,
      root => {
        { const x = $('#ow-x', root); if(x) x.onclick = () => closeModal(); }
        $$('.ow-opt', root).forEach(b => b.onclick = () => {
          const h = handlers.find(x => x.id === b.dataset.ow);
          closeModal();                       // close FIRST: the handler opens a sheet of its own
          if(h) run(h);
        });
      });
  }
  /* BYTES, FROM WHICHEVER OF THE THREE SOURCES THIS FILE CAME FROM. Preview renders from a Blob and
   * fetches nothing itself, which is what lets it show an ENCRYPTED file without that file's
   * plaintext ever going near the network — and what makes it work on a build with no instance. */
  async function _previewBytes(d, opts){
    if(opts && opts.sync) return _syncFileBlob(d.sha, d.chunks ? d.chunks.split(',').filter(Boolean) : null);
    if(d.enc === '1'){
      const u = await encFileUrl(d.sha, d.mime || mimeForName(d.name));
      return fetch(u).then(r => r.blob());
    }
    const r = await fetch(d.url);
    if(!r.ok) throw new Error('file HTTP ' + r.status);
    return r.blob();
  }
  async function openPreviewFile(d, opts){
    try{
      toast(d.enc === '1' ? 'decrypting…' : 'opening…');
      const blob = await _previewBytes(d, opts);
      /* Keep lazy module loading inside this try/catch. The callback form returns before preview.js
       * loads, so a missing packaged asset or an exception from P.open becomes an unhandled promise
       * and Files appears to open a black/nothing screen. Every source funnels through here. */
      const P = await _withModule('preview.js', 'PCPreview');
      if(!P || typeof P.open!=='function') throw new Error('the preview viewer did not load');
      if(!P.open({ name: d.name || 'file', mime: d.mime || blob.type || '', blob }))
        toast('nothing here can show that file');
    }catch(err){ toast('could not open that: ' + ((err && err.message) || err)); }
  }

  /* THE EDITOR ITSELF, WRITTEN ONCE. The drive and a synced folder fetch their bytes differently
   * and write them back differently; everything in between — the WOPI session, the iframe, the
   * launch form, Save and Close — is the same, and two copies of it is two places to leak a token
   * or leave a session open. `saveBack(updatedFile)` is the only part that differs. */
  function _sameBytes(a, b){
    if(!a || !b) return false;
    const x = new Uint8Array(a), y = new Uint8Array(b);
    if(x.length !== y.length) return false;
    for(let i = 0; i < x.length; i++) if(x[i] !== y[i]) return false;
    return true;
  }
  async function _officeSession(file, saveBack){
    // Capture the launcher before opening the editor changes VIEW. Files keeps its
    // selected source and folder in memory, so returning through switchView restores it.
    const returnToFiles = typeof _S.VIEW !== 'undefined' && _S.VIEW === 'blossom';
    let session=null;
    /* THE INSTANCE, EXPLICITLY. A bundled app (desktop `app://posterchan`, the APK's
     * `https://localhost`) has no server on its own origin, so a bare `/client/office/...` resolves
     * against the BUNDLE and fails — and every failure in this function said the same six words,
     * "office unavailable", whatever went wrong. */
    const B = _instanceBase();
    try{
      /* Do not let a missing packaged-app instance turn these into relative requests. On desktop
       * that means app://posterchan/client/office; in the APK it means https://localhost/client/office.
       * Neither is the node that advertised Office, and retrying there only produces a white/empty
       * editor after the file bytes have already been read. */
      if(!/^https?:\/\//i.test(B))
        throw new Error('connect this app to your PosterChan instance before opening Office');
      const fd=new FormData(); fd.append('file',file,file.name); fd.append('mode','edit');
      /* WHERE THE EDITOR SHOULD POST BACK TO — this page, which is not the instance in either
       * packaged app (`app://posterchan` on the desktop, `capacitor://localhost` on Android). The
       * server checks it against the shells it already trusts and ignores anything else, so this is
       * a statement of fact, not a permission. Without it Collabora addresses every host message to
       * the instance, the browser drops all of them, and `askEditorToSave` waits out its full
       * timeout on every Save, Save As and PDF export. */
      try{ if(location && location.origin && location.origin !== 'null') fd.append('origin', location.origin); }catch(_){ }
      let r;
      try{ await ensureAiSession();r=await window.__PC.authFetch(B + '/client/office/session',{method:'POST',body:fd}); }
      catch(e){ throw new Error('could not reach ' + (B || 'this node') + ' — ' + ((e&&e.message)||e)); }
      if(!r.ok){
        const said=(await r.json().catch(()=>null)||{}).detail||'';
        // 404 here means the router is not mounted; 502 means CODE itself is not running behind
        // /office-code. Those send you to completely different places.
        throw new Error(said || (r.status===404 ? 'this node has no office editor installed'
                               : r.status===502 ? 'the office editor is not running on this node'
                               : 'office HTTP '+r.status));
      }
      session=await r.json();
      const frameName='pc-office-'+session.id;
      /* THE EDITOR'S BODY, BUILT ONCE and mounted either in a desktop WINDOW or in a modal. A
       * document editor is an application, not a dialog: on the windowed desktop it must minimise,
       * maximise, sit behind another window and be dragged between monitors like everything else.
       * `modal()` can do none of that — it is one centred box with a backdrop. */
      const bodyHTML = `<div class="office-head"><h3>📝 ${enc(file.name)}</h3><span class="muted small">Changes are temporary until you tap Save.</span></div>
        <iframe class="office-frame" name="${frameName}" title="Office editor"></iframe>
        <form class="office-launch" method="post" action="${enc(session.editor_url)}" target="${frameName}">
          <input type="hidden" name="access_token" value="${enc(session.token)}"><input type="hidden" name="access_token_ttl" value="${session.expires*1000}"></form>
        <div class="row office-actions"><button class="btn btn-ghost" id="office-close">Close</button><button class="btn btn-ghost" id="office-pdf" aria-label="Save as PDF"><span class="office-long">Save as </span>PDF…</button><button class="btn btn-ghost" id="office-saveas">Save As…</button><button class="btn btn-neon" id="office-save">Save</button></div>`;
      const drop=async()=>{ try{ await fetch(B + '/client/office/session/'+session.id+'?access_token='+encodeURIComponent(session.token),{method:'DELETE'}); }catch(_){} };
      /* `wire` is handed how to shut whatever it was mounted in, so the Save and Close buttons do
       * not have to know which of the two they are living in. */
      /* SAVE HAD TO **ASK** THE EDITOR TO SAVE, AND IT NEVER DID.
       * The button waited 700ms and then downloaded the session document — but the only thing that
       * ever writes that document is Collabora's own PutFile, which it sends when IT decides to
       * (an autosave tick, or closing). Click Save promptly after typing and the fetch returned the
       * bytes the session started with, so the upload succeeded, the drive index moved to a new
       * hash, the toast said "document saved" and the file was unchanged. Reported as "clicking
       * save when opening a blossom file did nothing" — and it did the same on a synced folder and
       * on Download, since all three share this one handler.
       * The WOPI post-message channel is the fix: `Action_Save` with Notify, then wait for the
       * editor's `Action_Save_Resp` before reading. The wait is bounded and FALLS BACK to the old
       * behaviour, because an editor that never answers must still save whatever it has written. */
      const askEditorToSave = (root) => new Promise(resolve => {
        const frame = $('.office-frame', root);
        const win = frame && frame.contentWindow;
        if(!win) return resolve(false);
        let settled = false;
        const finish = (ok) => {
          if(settled) return; settled = true;
          try{ window.removeEventListener('message', onMsg); }catch(_){}
          clearTimeout(timer); resolve(ok);
        };
        const onMsg = (ev) => {
          if(frame.contentWindow && ev.source !== frame.contentWindow) return;
          let d = ev.data;
          if(typeof d === 'string'){ try{ d = JSON.parse(d); }catch(_){ return; } }
          if(!d || typeof d !== 'object') return;
          if(d.MessageId === 'Action_Save_Resp') finish(true);
        };
        window.addEventListener('message', onMsg);
        const post = (MessageId, Values) => {
          try{ win.postMessage(JSON.stringify({MessageId, SendTime:Date.now(), Values:Values||{}}), '*'); }
          catch(_){}
        };
        /* Collabora ignores host messages until it has been told the host is listening. It is sent
         * again here rather than only on load, because a window can be re-wired after a repaint. */
        post('Host_PostmessageReady');
        post('Action_Save', {Notify:true, ExtendedData:'', DontTerminateEdit:true, DontSaveIfUnmodified:false});
        const timer = setTimeout(()=>finish(false), 8000);
      });
      let origBytes = null;
      try{ origBytes = await file.arrayBuffer(); }catch(_){ origBytes = null; }
      const wire = (root, shut) => {
        $('.office-launch',root).submit();
        $('#office-close',root).onclick=async()=>{ await drop(); shut(); };
        /* Ours, not Collabora's — see the comment on `session_export` in app/routers/office.py for
         * why its own File > Download as > PDF cannot work inside this app. `saveBlobAs` is the one
         * path that saves a file in a browser, the desktop shell and the APK alike. */
        const pdfBtn = $('#office-pdf',root);
        if(pdfBtn) pdfBtn.onclick = async e => {
          const b=e.currentTarget, label=b.innerHTML; b.disabled=true; b.textContent='Converting\u2026';
          try{
            await askEditorToSave(root);
            await new Promise(res=>setTimeout(res,700));
            const rr=await fetch(B + '/client/office/session/'+session.id+'/export/pdf?access_token='+encodeURIComponent(session.token));
            if(!rr.ok) throw new Error('HTTP '+rr.status);
            const blob=await rr.blob();
            if(!blob.size) throw new Error('the converter returned nothing');
            await _officeSaveCopy(blob, (file.name||'document').replace(/\.[^.]+$/,'') + '.pdf');
          }catch(err){ toast('could not save a PDF: '+((err&&err.message)||err)); }
          b.disabled=false; b.innerHTML=label;
        };
        const saveAsBtn=$('#office-saveas',root);
        if(saveAsBtn) saveAsBtn.onclick=async e=>{
          const b=e.currentTarget;b.disabled=true;b.textContent='Saving…';
          try{
            await askEditorToSave(root); await new Promise(res=>setTimeout(res,700));
            const rr=await fetch(B+'/client/office/session/'+session.id+'/contents?access_token='+encodeURIComponent(session.token));
            if(!rr.ok)throw new Error('saved document HTTP '+rr.status);
            await _officeSaveCopy(await rr.blob(),file.name||'document');
          }catch(err){toast('Save As failed: '+((err&&err.message)||err));}
          b.disabled=false;b.textContent='Save As…';
        };
        $('#office-save',root).onclick=async e=>{
          const b=e.currentTarget; b.disabled=true; b.textContent='Saving\u2026';
          try{
            await askEditorToSave(root);
            // Its PutFile lands just after the acknowledgement; a short settle covers both paths.
            await new Promise(res=>setTimeout(res,700));
            const rr=await fetch(B + '/client/office/session/'+session.id+'/contents?access_token='+encodeURIComponent(session.token));
            if(!rr.ok) throw new Error('saved document HTTP '+rr.status);
            const bytes = await rr.arrayBuffer();
            /* AND SAY SO WHEN THERE IS NOTHING TO SAVE. Uploading an unchanged document is not
             * harmless here — it mints a second blob and re-points the drive index at it — and a
             * "document saved" toast over bytes that never changed is precisely how the missing
             * save handshake stayed invisible. Identical bytes now report themselves. */
            /* SAVE SAVES. IT DOES NOT CLOSE.
             *
             * This ended in `drop()` + `shut()`, so every Save deleted the server-side session and
             * shut the window -- reported as "saving an office document should not close it too".
             * Nothing about writing the bytes back requires ending the edit, and a person who saves
             * mid-document is saying the opposite: they intend to carry on. Closing IS still
             * available, on the Close button beside this one, and that path still drops the session.
             *
             * `origBytes` moves to what was just written, so a second Save on an untouched document
             * correctly reports "no changes" rather than minting another blob -- the check exists
             * because an unchanged upload is not harmless here: it re-points the drive index at a
             * new hash. */
            if(_sameBytes(bytes, origBytes)){
              toast('no changes to save'); b.disabled=false; b.textContent='Save'; return;
            }
            await saveBack(fileFromBytes(bytes,file.name,file.type));
            origBytes = bytes;
            toast('document saved'); b.disabled=false; b.textContent='Save';
          }catch(err){ toast('save failed: '+(err.message||err)); b.disabled=false; b.textContent='Save'; }
        };
      };
      /* ON THE WINDOWED DESKTOP IT IS A WINDOW. Same shape webxdc already uses for a mini app:
       * openDoc with noFeed, mounted by hand into the window's own slot. noFeed is load-bearing —
       * without it the window joins the shared-feed hand-off, so clicking any OTHER window pulls
       * the timeline out of this one and repaints it, and a repaint around a live iframe reloads
       * the editor and loses whatever was typed. The window OWNS the session: closing it drops the
       * server-side document, or an editor closed by its ✕ leaks a session for the whole TTL. */
      if(window.PCOS && PCOS.isOn() && PCOS.openDoc){
        const w = PCOS.openDoc('office:'+session.id, file.name, 'i-note', () => {}, true);
        if(w && PCOS.documentWindow) PCOS.documentWindow(w);
        const host = w && (w.slot || w.body);
        if(host){
          host.classList.add('office-win');
          host.innerHTML = bodyHTML;
          let shut = () => { try{ PCOS.closeDoc && PCOS.closeDoc('office:'+session.id); }catch(_){} };
          if(w) w.onClose = () => { drop(); };
          wire(host, shut);
          return;
        }
        // No slot to mount into: fall through to the modal rather than leave an empty window.
        try{ PCOS.closeDoc && PCOS.closeDoc('office:'+session.id); }catch(_){}
      }
      /* THE WEB CLIENT GETS THE WHOLE VIEW, NOT A DIALOG.
       *
       * Reported as "office is loading in a tiny ass window in the webui, classic mode, it should
       * be the entire right side like concord". A document editor is an application: it is the
       * thing you are doing, for as long as you are doing it. `modal()` is one centred box with a
       * backdrop — right for a picker, wrong for a word processor, and on a laptop it left the
       * editor in a small panel with the timeline greyed out behind it.
       *
       * Concord is the shape to copy: it owns `#feed`, which IS the right side. The desktop keeps
       * its real window above; the modal stays as the last resort for a client with no feed to
       * take (an embedded surface, a shell that has not painted yet). */
      {
        const feed = document.getElementById('feed');
        if(feed){
          try{ switchView('office'); }catch(_){ }
          feed.classList.add('feed-office');
          feed.innerHTML = '';
          const host = document.createElement('div');
          host.className = 'office-win office-view';
          host.innerHTML = bodyHTML;
          feed.appendChild(host);
          wire(host, () => {
            if(returnToFiles) switchView('blossom');
            else renderOfficeHome();
          });
          return;
        }
      }
      modal(bodyHTML, root=>{
          /* `modal()` caps its box at 720px and the office frame asks for far more inside that, so
           * without this the iframe is clipped to 720px of a modal that then scrolls — "a tiny ass
           * window that is white": a small viewport showing the top-left corner of a spreadsheet. */
          root.classList.add('office-modal');
          wire(root, closeModal);
        });
    }catch(err){ toast('office unavailable: '+((err&&err.message)||err)); }
  }

  async function _officeStoreDrive(blob,name){
    const file=fileFromBytes(await blob.arrayBuffer(),name,blob.type||mimeForName(name)||'application/octet-stream');
    const stored={}; const url=await uploadBlob(file,{hashOut:stored,noCompress:true});
    const sha=stored.sha||_shaFromUrl(url); if(!sha)throw new Error('upload completed without a content hash');
    FilesIdx.setFile(sha,{name,folder:'',mime:file.type,size:file.size,ts:Math.floor(Date.now()/1000)});
    _rememberUploadedBlob(sha,url,file); return sha;
  }
  function _officeSaveCopy(blob,name){
    /* THE SAME CHOOSER AS EVERYTHING ELSE. This grew its own markup — `.openwith-list` /
     * `.openwith-row`, no icons, no way out but the backdrop — beside `_openWithSheet`'s
     * `.openwith` / `.ow-opt`, which every other "which one of these?" in the app uses. Reported as
     * "very ugly", and it was: two patterns for one question, and the plain one on the screen you
     * reach by saving work. Same classes, same header shape, same ✕ — no new CSS. */
    const dests = [
      ['drive','🗂','Files / Blossom','Your PosterChan drive'],
      ...(_S._syncRoot?[['sync','🔄','Synced Folder','The folder currently open in Files']]:[]),
      ['local','💻','This computer','Choose a local destination'],
    ];
    return new Promise((resolve,reject)=>modal(
      `<h3 class="cmp-hd">Save “${enc(name)}”<button class="modal-x" id="os-x" title="Close" aria-label="Close">&#215;</button></h3>
       <div class="openwith">${dests.map(([id,icon,label,hint])=>
         `<button class="ow-opt" data-office-dest="${enc(id)}"><span class="ow-ic">${icon}</span>
            <span class="ow-t"><b>${enc(label)}</b><i>${enc(hint)}</i></span></button>`).join('')}</div>`,root=>{
      /* A CHOOSER THAT IS DISMISSED MUST STILL SETTLE — reported as "convert to pdf button is
       * stuck". `Save as PDF…` disables its button, awaits this promise and re-enables afterwards;
       * dismissing this sheet by the BACKDROP or Escape called `closeModal()` and resolved nothing,
       * so the await never returned and the button sat on "Converting…" for the life of the page.
       * The ✕ had the same hole. Watching for the node to leave the document covers every way out
       * at once — including a `closeModal()` from somewhere else entirely — where handling each
       * one separately is how the backdrop got missed in the first place.
       *
       * Cancelling is not an error: resolve with nothing rather than rejecting into a "could not
       * save a PDF" toast for a deliberate choice. */
      let settled = false;
      const done = v => { if(settled) return; settled = true; resolve(v); };
      const fail = e => { if(settled) return; settled = true; reject(e); };
      { const x=$('#os-x',root); if(x) x.onclick=()=>{ closeModal(); done(null); }; }
      try{
        const host = $('#modal-root');
        if(host && window.MutationObserver){
          const mo = new MutationObserver(()=>{ if(!root.isConnected){ mo.disconnect(); done(null); } });
          mo.observe(host, {childList:true, subtree:true});
        }
      }catch(_){ }
      $$('[data-office-dest]',root).forEach(btn=>btn.onclick=async()=>{
        try{
          const dest=btn.dataset.officeDest;
          if(dest==='drive')await _officeStoreDrive(blob,name);
          else if(dest==='sync'){
            if(!(window.PCSync&&PCSync.edit&&PCSync.edit.uploadMany))throw new Error('this build cannot write to a synced folder');
            const r=await PCSync.edit.uploadMany(_S._syncRoot,_S._syncPath||'',[fileFromBytes(await blob.arrayBuffer(),name,blob.type)],{replace:false});
            if(r&&r.failed&&r.failed.length)throw new Error('the folder refused the write');
            _syncManifests.delete(_S._syncRoot);
          }else if(window.pcHost&&pcHost.saveFile){
            const out=await pcHost.saveFile(name,new Uint8Array(await blob.arrayBuffer())); if(!out)return;
          }else await saveBlobAs(blob,name);
          closeModal();toast('saved '+name);done(dest);
        }catch(e){closeModal();fail(e);}
      });
    }));
  }

  function renderOfficeHome(){
    const feed=$('#feed');
    feed.innerHTML=`<section class="office-home"><h2>PosterChan Office</h2><p class="muted">Create a document or open one from any of your files.</p>
      <div class="office-home-actions">${_DOC_KINDS.map(([k,label])=>`<button class="btn btn-neon" data-office-new="${k}">New ${label}</button>`).join('')}
      <button class="btn btn-ghost" id="office-open-files">Open from Files</button>
      ${window.pcHost&&pcHost.pickFile?'<button class="btn btn-ghost" id="office-open-local">Open from this computer</button>':''}</div></section>`;
    $$('[data-office-new]',feed).forEach(b=>b.onclick=()=>_newDocumentModal(b.dataset.officeNew));
    $('#office-open-files',feed).onclick=()=>switchView('blossom');
    const local=$('#office-open-local',feed); if(local)local.onclick=async()=>{
      try{
        const p=await pcHost.pickFile({title:'Open in PosterChan Office',max:64*1024*1024}); if(!p)return;
        const file=fileFromBytes(p.data,p.name,(p.type&&p.type!=='application/octet-stream')?p.type:mimeForName(p.name));
        let openedMtime=p.mtime||0;
        await _officeSession(file,async updated=>{
          if(!p.path||!pcHost.writeBytes)throw new Error('this build cannot save back to that file');
          const info=await pcHost.writeBytes(p.path,new Uint8Array(await updated.arrayBuffer()),openedMtime);
          if(info&&info.mtime)openedMtime=info.mtime;
        });
      }catch(e){toast('could not open in Office: '+((e&&e.message)||e));}
    };
  }

  /* CREATE A DOCUMENT, not only open one that already exists.
   *
   * `openOfficeFile` takes a file off the drive and hands it to CODE, and there was no path in the
   * app that MADE a file — so starting a spreadsheet required already having a spreadsheet. The
   * blank document comes from the server (`/client/office/blank/<kind>`, built with the stdlib zip
   * module so it costs no dependency) and is then stored and opened through the EXISTING paths:
   * the same upload the file picker uses, honouring the folder's encryption, and the same
   * openOfficeFile below. Nothing about saving, encrypting or re-indexing is duplicated here. */
  const _DOC_KINDS = [['text','Document','odt'], ['spreadsheet','Spreadsheet','ods'],
                      ['presentation','Presentation','odp']];
  async function _createOfficeDocument(name, kind){
    const B = _instanceBase();
    await ensureAiSession();
    const r = await window.__PC.authFetch(B + '/client/office/blank/' + encodeURIComponent(kind));
    if(!r.ok) throw new Error('could not create the document (HTTP ' + r.status + ')');
    const ext = r.headers.get('X-Document-Extension') || 'odt';
    const blob = await r.blob();
    /* A name is a FILENAME: a slash would make the drive index disagree with the folder chips. */
    const base = String(name || '').replace(/[\\/]+/g, '-').trim().slice(0, 60) || 'Untitled';
    const fname = base.toLowerCase().endsWith('.' + ext) ? base : base + '.' + ext;
    const file = fileFromBytes(await blob.arrayBuffer(), fname, blob.type);
    const folder = _S._filesFolder || '';
    if(FilesIdx.isEncFolder(folder)){
      const sha = await uploadEncFile(file, folder, null);
      _rememberUploadedBlob(sha, '', file);
      return { sha, name: fname, mime: blob.type, enc: '1' };
    }
    const stored = {};
    const url = await uploadBlob(file, { hashOut: stored, noCompress: true });
    const sha = stored.sha || _shaFromUrl(url);
    if(!sha) throw new Error('upload completed without a content hash');
    FilesIdx.setFile(sha, { name: fname, folder, mime: blob.type, size: file.size,
                            ts: Math.floor(Date.now()/1000) });
    _rememberUploadedBlob(sha, url, file);
    return { sha, name: fname, mime: blob.type, enc: '0', url };
  }
  function _newDocumentModal(preselect){
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-note"></use></svg>New document</h3>
      <label class="fld">Name<input class="input" id="nd-name" placeholder="Untitled" maxlength="60"></label>
      <div class="fld">Type
        ${_DOC_KINDS.map(([k,label],i)=>`<label class="nf-opt"><input type="radio" name="nd-kind" value="${k}"${(preselect?k===preselect:!i)?' checked':''}> <b>${label}</b></label>`).join('')}
      </div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px"><button class="btn btn-ghost small" id="nd-cancel">Cancel</button><button class="btn btn-neon small" id="nd-create">Create</button></div>`,
      root=>{
        const nm=$('#nd-name',root); if(nm) nm.focus();
        const c=$('#nd-cancel',root); if(c) c.onclick=closeModal;
        const go=async ()=>{
          const btn=$('#nd-create',root); if(btn){btn.disabled=true;btn.textContent='Creating…';}
          const kind=(($('input[name="nd-kind"]:checked',root)||{}).value)||'text';
          try{
            toast('creating document…');
            const d=await _createOfficeDocument((nm&&nm.value)||'', kind);
            closeModal(); renderBlossom(); await openOfficeFile(d);
          }catch(e){ if(btn){btn.disabled=false;btn.textContent='Create';} toast('could not create it: '+((e&&e.message)||e)); }
        };
        const g=$('#nd-create',root); if(g) g.onclick=go;
        if(nm) nm.addEventListener('keydown',e=>{ if(e.key==='Enter') go(); });
      });
  }
  async function openOfficeFile(d){
    try{
      toast(d.enc==='1'?'decrypting document…':'opening office…');
      let blob;
      if(d.enc==='1'){
        const u=await encFileUrl(d.sha, d.mime||mimeForName(d.name));
        blob=await fetch(u).then(r=>r.blob());
      }else{
        const r=await fetch(d.url); if(!r.ok) throw new Error('file HTTP '+r.status); blob=await r.blob();
      }
      const file=fileFromBytes(await blob.arrayBuffer(), d.name||'document', d.mime||blob.type);
      await _officeSession(file, async (updated)=>{
        const old=FilesIdx.meta(d.sha)||{}, folder=old.folder||FilesIdx.folderOf(d.sha)||'';
        let newSha='';
        if(d.enc==='1') newSha=await uploadEncFile(updated,folder,null);
        else { const url=await uploadBlob(updated,{noCompress:true}); newSha=_shaFromUrl(url);
          FilesIdx.setFile(newSha,{name:file.name,folder,mime:updated.type||mimeForName(file.name),size:updated.size,ts:Math.floor(Date.now()/1000)}); }
        if(newSha && newSha!==d.sha) FilesIdx.forget(d.sha); // old blob remains recoverable on Blossom
        renderBlossom();
      });
    }catch(err){ toast('office unavailable: '+((err&&err.message)||err)); }
  }

  /* THE SAME EDITOR, ON A FILE IN A SYNCED FOLDER. Bytes come the way Download gets them
   * (`_syncFileBlob` — Blossom by sha or chunk list, decrypted with the drive key) and Save goes
   * back through the FOLDER's own writer, so the edit reaches every device rather than landing on
   * the drive as a copy. There was no Office button in a synced folder at all. */
  async function openSyncOfficeFile(d){
    const key=_S._syncRoot, full=d.path||d.name||'';
    try{
      const chunks = d.chunks ? String(d.chunks).split(',').filter(Boolean) : null;
      if(!d.sha && !(chunks && chunks.length)){ toast('this file has no stored copy yet'); return; }
      toast('decrypting document…');
      const blob=await _syncFileBlob(d.sha, chunks);
      const name=d.name||'document';
      const file=fileFromBytes(await blob.arrayBuffer(), name, mimeForName(name)||blob.type);
      await _officeSession(file, async (updated)=>{
        if(!(window.PCSync && PCSync.edit && PCSync.edit.uploadMany))
          throw new Error('this build cannot write to a synced folder');
        const cut=full.lastIndexOf('/');
        const dir=cut<0?'':full.slice(0,cut);
        const r=await PCSync.edit.uploadMany(key, dir, [updated], { replace:true });
        if(r && r.failed && r.failed.length) throw new Error('the folder refused the write');
        try{ _syncManifests.delete(key); }catch(_){}
        if(_S.VIEW==='blossom' && _S._syncRoot===key) renderBlossom();
      });
    }catch(err){ toast('office unavailable: '+((err&&err.message)||err)); }
  }

  function _renderFilesGrid(grid, list){
    if(!grid) return;
    _S._filesGridList = list;
    // hide encrypted MUSIC ciphertext from the normal grid (it lives in the Music folder's track list);
    // encrypted files in other folders DO show, as lock cards that decrypt in-browser on open.
    // Forget tombstones the server has already stopped listing — keeps the set from growing and lets
    // a re-uploaded (same-hash) file come back.
    if(_filesDeleted.size){ const live=new Set(list.map(b=>b.sha256)); [..._filesDeleted].forEach(sha=>{ if(!live.has(sha)) _filesDeleted.delete(sha); }); }
    const inFolder = list.filter(b=>{
      if(_filesDeleted.has(b.sha256)) return false;                            // deleted this session (see _filesDeleted)
      // EVERY index blob, not just the newest. Each edit (upload/delete/move/rename) re-encrypts the
      // whole index and uploads it as a NEW blob, and the superseded one is deliberately kept as the
      // only standalone backup of every filename+folder. Hiding only _lastIndexSha meant that after a
      // delete the FRESH index blob was hidden but the one it replaced popped into the grid — an
      // unnamed ~671KB tile appearing exactly where the deleted file had been, which reads as
      // "the file didn't delete". They are all bookkeeping; none of them is a user file.
      if(b.sha256===FilesIdx._lastIndexSha || FilesIdx._indexShas.has(b.sha256)) return false;
      const m=FilesIdx.meta(b.sha256);
      if(!m && /octet-stream/.test(b.type||'')) return false;                  // stale index blobs / unnamed binaries (the "OCTET-STE" noise)
      if(m && m.enc && FilesIdx.folderOf(b.sha256)==='Music') return false;    // music ciphertext → Music list only
      /* The folder still applies while searching. Ignoring it made every folder chip draw the same
       * whole-drive results, so navigation went inert until the box was cleared — and "All files" is
       * already the way to search everything, one click away and always visible. */
      if(_S._filesFolder!=='' && FilesIdx.folderOf(b.sha256)!==_S._filesFolder) return false;
      /* Searching looks at the WHOLE drive, not just the folder you happen to be standing in — "find
       * my tax return" is the question, and needing to guess the folder first is the problem it is
       * there to solve. The crumb still says where you are; the results say where they came from. */
      return _fxMatch((m && m.name) || '');
    }).sort(_fxCompare(_fxBlobKey));   // default is NEWEST first — else a fresh upload/VOD is buried at the END of a
                                       // big (3000+) oldest-first Blossom /list, past the 60-per-page window (the
                                       // "my recording isn't in the drive" bug). See _fxSort().
    if(_filesShownFolder!==_S._filesFolder){ _filesShownFolder=_S._filesFolder; _filesShown=_FILES_PAGE; _filesSelClear(); }   // reset paging AND selection on folder change (never act on off-screen files)
    const _shown = inFolder.slice(0, _filesShown), _more = inFolder.length - _shown.length;
    /* TILES or DETAILS — the same filtered, sorted array, drawn two ways. Every class the handlers
     * below select on (.file-card, .selbox, .del, .copy, .dlbtn, .dlenc, .movebtn, .enc-open) is
     * present in BOTH, so selecting, deleting, moving, dragging and downloading are the same code in
     * either view. That is the whole reason the details view is markup and CSS and not a second
     * implementation of the drive. */
    const details = _fxView()==='details';
    grid.className = 'files-grid' + (details ? ' details' : '') + (grid.classList.contains('selmode') ? ' selmode' : '');
    grid.innerHTML = inFolder.length ? ((details ? _fxColsHTML() : '')
      + `<div class="files-page-count" style="grid-column:1/-1" aria-live="polite">Showing ${_shown.length} of ${inFolder.length}</div>`
      + _shown.map(b=>{
      // Name: ours (Files index) → the server's stored upload name → a VOD label. The last two are why
      // a file uploaded from another device/client isn't an anonymous "412KB" tile any more.
      const m=FilesIdx.meta(b.sha256)||{}; const nm=m.name||b.name||_S._vodNameMap[b.sha256]||'';
      const ext=extOfBlob(b, m.name?m:{name:nm, mime:m.mime});
      const dlName=downloadName(b, nm, ext);
      const sel=_filesSel.has(b.sha256)?' selected':'';
      const box=`<input type="checkbox" class="selbox" data-sha="${b.sha256}"${_filesSel.has(b.sha256)?' checked':''} title="Select">`;
      const del=`<button class="del" data-sha="${b.sha256}" aria-label="Delete"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button>`;
      const move=`<button class="movebtn" data-sha="${b.sha256}" title="Move to folder"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg></button>`;
      /* Rename. The name lives in the FILES INDEX, not in the blob — a Blossom server keys on the
       * hash and knows nothing about what the file is called — so this is an index edit, and the
       * bytes are never touched or re-uploaded. That is also why it works for a file somebody else
       * uploaded and why it costs nothing for a 4GB video. */
      const ren=`<button class="renbtn" data-sha="${b.sha256}" data-name="${enc(nm||dlName)}" title="Rename"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button>`;
      const dl=m.enc
        ? `<button class="dlbtn dlenc" data-sha="${b.sha256}" data-name="${enc(dlName)}" title="Download (decrypts first)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg></button>`
        : `<button class="dlbtn" data-url="${enc(b.url)}" data-name="${enc(dlName)}" title="Download ${enc(dlName)}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg></button>`;
      if(details) return _fxDetailsRow({
        sha:b.sha256, draggable:true, selected:_filesSel.has(b.sha256), enc:!!m.enc, box:box,
        href: m.enc ? '#' : b.url, encOpen: !!m.enc, mime: m.enc ? undefined : (b.type||''),
        data: ` data-sha="${b.sha256}" data-url="${enc(b.url)}" data-name="${enc(nm||dlName)}"`
            + ` data-mime="${enc(m.mime||b.type||'')}" data-enc="${m.enc?'1':'0'}"`,
        icon: m.enc ? _fxEncIcon(ext, m.mime) : _fxIcon(ext, (b.type && !/octet-stream/i.test(b.type)) ? b.type : (m.mime||b.type)), name: nm || (m.enc ? 'encrypted' : dlName), title: nm || dlName,
        size:_fxBytes(b.size), type:(m.enc?'🔒 ':'')+_fxType(ext), when:_fxWhen(b.uploaded),
        acts: (m.enc ? '' : `<button class="copy" data-url="${enc(b.url)}" title="Copy URL">⧉</button>`) + dl + ren + move + del,
      });
      /* THE OPENERS, ON THE TILE ITSELF.
       *
       * These lived only in the DETAILS row, and the default view is tiles — so on the screen
       * almost everybody is looking at there was no way to open anything in Code, and Office was
       * reachable only by clicking the tile, which nothing says. Reported exactly that way: "why no
       * way to open any blossom file in posterchan code or office yet". Same buttons, same
       * dataset, same handlers as the details row; only the place they are drawn is new. */
      if(m.enc){   // encrypted file — lock card; opening decrypts in-browser (never exposes the ciphertext URL)
        return `<div class="file-card enc${sel}" draggable="true" data-sha="${b.sha256}"><a href="#" class="enc-open" data-sha="${b.sha256}" data-name="${enc(nm||dlName)}" data-mime="${enc(m.mime||'')}" data-enc="1"><div class="file-icon">${_fxEncIcon(ext, m.mime)}<span>${enc(ext||'enc')}</span></div></a>
          ${box}${del}
          <div class="meta"><span class="fname" title="${enc(nm)}">${nm?enc(fileLabel(nm,ext,b.size)):'encrypted'}</span><span class="fc-acts">${dl}${ren}${move}</span></div></div>`;
      }
      return `<div class="file-card${sel}" draggable="true" data-sha="${b.sha256}"><a href="${enc(b.url)}" data-url="${enc(b.url)}" data-name="${enc(nm||dlName)}" data-sha="${b.sha256}" data-mime="${enc(b.type||'')}" data-enc="0" target="_blank">${blobThumb(b, ext)}</a>
        ${box}
        <button class="copy" data-url="${enc(b.url)}" title="Copy URL">⧉</button>${del}
        <div class="meta"><span class="fname" title="${enc(nm||dlName)}">${enc(fileLabel(nm,ext,b.size))}</span><span class="fc-acts">${dl}${ren}${move}</span></div></div>`;
    }).join('') + (_more>0 ? `<button class="btn btn-ghost bl-more" data-id="bl-more" style="grid-column:1/-1;justify-self:center;margin:10px 0"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-down"></use></svg>Load ${Math.min(_more,_FILES_PAGE)} more · ${_more} left</button>` : '')) : (_S._filesQ.trim()
        ? '<div class="empty">Nothing'+(_S._filesFolder?(' in '+enc(_S._filesFolder)):' on your drive')
          +' matches “'+enc(_S._filesQ.trim())+'”.</div>'
        : '<div class="empty">No files'+(_S._filesFolder?(' in '+enc(_S._filesFolder)):'')+' yet — drop some above.</div>');
    if(details) _fxBindCols(grid);
    { const mb=$('.bl-more',grid); if(mb){
      mb.onclick=()=>{ _filesShown+=_FILES_PAGE; _renderFilesGrid(grid, list); };
      /* The page button remains available to keyboard and assistive-tech users. For ordinary
       * scrolling, advance automatically before the sentinel reaches the viewport: a folder with
       * 1,972 indexed files must not look like a 60-file folder merely because its paging control
       * is several screens below the last visible row. One page per intersection keeps initial DOM
       * work bounded; scrolling further progressively reveals the whole folder. */
      if(window.IntersectionObserver){
        const observer=new IntersectionObserver(entries=>{ if(entries.some(x=>x.isIntersecting)){
          observer.disconnect(); if(mb.isConnected)mb.click();
        } },{root:mb.closest('.fx-main'),rootMargin:'600px 0px'});
        observer.observe(mb);
      }
    } }
    /* CLICKING THE FILE OPENS IT — there is no Open button any more.
     *
     * ONE binding for every door on this screen, tile and details row alike, because they are the
     * same element: `.file-card > a`. Three separate handlers used to be assigned to it (encrypted,
     * office, and an Open button's), and two of them were `a.onclick = …` on the SAME anchor, where
     * the last assignment silently wins and the earlier one is simply gone. So the decision is made
     * ONCE, here, from the file itself.
     *
     * What a click did before is always the LAST entry on the list rather than a thing this replaces:
     * a plain blob still opens in a tab, an encrypted one still decrypts in the browser. And a file
     * nothing of ours can open keeps its old one-click behaviour exactly — no sheet, no extra step. */
    $$('.file-card[data-sha] > a', grid).forEach(a=> a.onclick=async e=>{
      const d=a.dataset, hs=_handlersFor(d, {});
      const encd = d.enc==='1';
      if(_previewable(_openFileName(d), d.mime)){
        e.preventDefault(); e.stopPropagation(); openPreviewFile(d, {}); return;
      }
      if(!hs.length && !encd) return;              // an ordinary link with nothing to choose: let it work
      e.preventDefault(); e.stopPropagation();
      const plain = encd
        ? { id:'plain', icon:'🔓', label:'Decrypt and open', hint:'Hands the plaintext to this browser',
            run:async()=>{ try{ toast('decrypting…'); window.open(await trackUrl(d.sha),'_blank'); }
                           catch(err){ toast('decrypt failed: '+((err&&err.message)||'')); } } }
        : { id:'plain', icon:'🌐', label:'Open in a new tab', hint:'However this browser handles it',
            run:()=>{ try{ window.open(d.url,'_blank'); }catch(_){} } };
      if(!hs.length){ await plain.run(); return; } // encrypted, nothing of ours: decrypt, as before
      hs.push(plain);
      _openWithSheet(d.name||'this file', hs);
    });
    _bindThumbFallback(grid);
    // Encrypted files can't be downloaded by URL (that would save the ciphertext) — decrypt in the
    // browser first, then save the plaintext under its real name.
    $$('.dlenc',grid).forEach(b=> b.onclick=e=>{ e.preventDefault(); e.stopPropagation(); saveEncrypted(b.dataset.sha, b.dataset.name); });
    $$('.del',grid).forEach(b=> b.onclick=()=>delBlob(b.dataset.sha));
    $$('.copy',grid).forEach(b=> b.onclick=()=>copyUrl(b.dataset.url));
    // :not(.dlenc) — encrypted files have their own handler below (decrypt first, never fetch the URL)
    $$('.dlbtn:not(.dlenc)',grid).forEach(b=> b.onclick=e=>{ e.preventDefault(); e.stopPropagation(); downloadBlobFile(b.dataset.url, b.dataset.name); });
    $$('.movebtn',grid).forEach(b=> b.onclick=(e)=>_moveMenu(e.currentTarget, b.dataset.sha));
    $$('.renbtn',grid).forEach(b=> b.onclick=(e)=>{ e.preventDefault(); e.stopPropagation(); renameBlob(b.dataset.sha, b.dataset.name); });
    $$('.file-card',grid).forEach(card=> card.ondragstart=e=>{ if(e.dataTransfer) e.dataTransfer.setData('text/sha', card.dataset.sha); });
    // Checkbox toggles selection without opening the file (the card's <a> would otherwise swallow it).
    $$('.selbox',grid).forEach(cb=> cb.onclick=e=>{ e.stopPropagation();
      const sha=cb.dataset.sha; if(cb.checked) _filesSel.add(sha); else _filesSel.delete(sha);
      const card=cb.closest('.file-card'); if(card) card.classList.toggle('selected', cb.checked);
      _filesSelBar(grid, list); });
    _filesSelBar(grid, list);
    _fxBindChipDrop();
    // Re-attach the keyboard cursor. _selEl() re-finds the row by key and re-adds .sel, but only when
    // something asks it to — so after "Load more" redraws the grid the highlight vanished until the next
    // keypress, even though the selection was still live (the next Enter loaded another page just fine).
    try{ _selEl(); }catch(_){ }
  }
  function _moveMenu(anchor, sha){
    const opts=[['__all','🗂 All']].concat(FilesIdx.folders().map(f=>[f,(f==='Music'?'🎵 ':'📁 ')+f]));
    openMenuPopover(anchor, opts, v=>{ FilesIdx.move(sha, v==='__all'?'':v); toast('moved'); renderBlossom(); });
  }
  // Upload a batch ONE AT A TIME (sequential), into the current folder, with a per-file progress queue.
  // In the Music folder, audio files go through the compress→encrypt pipeline; everything else uploads
  // straight to Blossom.
  // Recurse dropped folders → a flat File[] (FileSystem entries captured synchronously from the drop).
  async function _walkEntries(entries){
    const out=[];
    async function walk(entry){
      if(entry.isFile){ await new Promise(res=>entry.file(f=>{
        /* FileSystemEntry keeps the directory path on the ENTRY, not on the File it returns.
         * `webkitRelativePath` is therefore empty for a dropped directory even though it is set for
         * the equivalent <input webkitdirectory> selection. Carry the path beside that read-only
         * browser property or dropping Pictures/Trips/a.jpg at Home silently files a.jpg in All. */
        const rel=String(entry.fullPath||'').replace(/^\/+|\/+$/g,'');
        if(rel){ try{ Object.defineProperty(f,'_pcRelativePath',{value:rel,configurable:true}); }
          catch(_){ try{ f._pcRelativePath=rel; }catch(__){} } }
        out.push(f); res();
      }, ()=>res())); }
      else if(entry.isDirectory){ const reader=entry.createReader();
        await new Promise(res=>{ const read=()=>reader.readEntries(async ents=>{ if(!ents.length){ res(); return; } for(const en of ents){ await walk(en); } read(); }, ()=>res()); read(); }); }
    }
    for(const en of entries){ await walk(en); }
    return out;
  }
  // Persistent floating upload progress (appended to <html> so it survives view changes) + a stop button.
  // Generic drag (mouse + touch) for a fixed-position widget; `noDrag` = selector to ignore (buttons etc.)
  function _makeDraggable(el, handle, noDrag){
    if(!el||!handle) return; let sx,sy,ox,oy,on=false;
    const move=e=>{ if(!on) return; const p=e.touches?e.touches[0]:e; el.style.right='auto'; el.style.bottom='auto'; el.style.left=Math.max(0,Math.min(innerWidth-50,ox+p.clientX-sx))+'px'; el.style.top=Math.max(0,Math.min(innerHeight-40,oy+p.clientY-sy))+'px'; if(e.cancelable)e.preventDefault(); };
    const up=()=>{ on=false; removeEventListener('mousemove',move); removeEventListener('mouseup',up); removeEventListener('touchmove',move); removeEventListener('touchend',up); };
    const down=e=>{ if(e.target.closest('button'+(noDrag?(','+noDrag):''))) return; const p=e.touches?e.touches[0]:e; const r=el.getBoundingClientRect(); on=true; sx=p.clientX; sy=p.clientY; ox=r.left; oy=r.top; el.style.right='auto'; el.style.bottom='auto'; el.style.left=ox+'px'; el.style.top=oy+'px'; addEventListener('mousemove',move); addEventListener('mouseup',up); addEventListener('touchmove',move,{passive:false}); addEventListener('touchend',up); };
    handle.onmousedown=down; handle.ontouchstart=down;
  }
  let _uploadCancel=false, _uploadBadgeT=0;
  function _uploadBadge(text, done){
    clearTimeout(_uploadBadgeT);   // a new update cancels a pending auto-remove (so it won't wipe a fresh upload)
    let b=document.getElementById('upload-badge');
    if(text===null){ if(b) b.remove(); return; }
    if(!b){ b=document.createElement('div'); b.id='upload-badge'; document.documentElement.appendChild(b); _makeDraggable(b, b, '.upbadge-x'); }
    b.className='upbadge'+(done?' done':'');
    b.innerHTML=`<span class="upbadge-ic">${done?'✅':'⬆'}</span><span class="upbadge-txt">${enc(text)}</span><span class="upbadge-x" title="${done?'dismiss':'stop'}">✕</span>`;
    const x=b.querySelector('.upbadge-x'); if(x) x.onclick=()=>{ if(done){ _uploadBadge(null); } else { _uploadCancel=true; const t=b.querySelector('.upbadge-txt'); if(t) t.textContent='stopping…'; } };
    if(done) _uploadBadgeT=setTimeout(()=>_uploadBadge(null), 12000);
  }
  // New-folder dialog: name + Encrypted/Public. An encrypted folder AES-encrypts everything dropped in
  // it (client-side, under your master key) before upload — Blossom only ever sees ciphertext.
  function _newFolderModal(){
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-folder"></use></svg>New folder</h3>
      <label class="fld">Name<input class="input" id="nf-name" placeholder="Folder name" maxlength="40"></label>
      <div class="fld">Contents
        <label class="nf-opt"><input type="radio" name="nf-enc" value="0" checked> 🌐 <b>Public</b><span class="muted small"> — files upload as-is, shareable by URL</span></label>
        <label class="nf-opt"><input type="radio" name="nf-enc" value="1"> 🔒 <b>Encrypted</b><span class="muted small"> — encrypted on this device; only you can open them</span></label>
      </div>
      <div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px"><button class="btn btn-ghost small" id="nf-cancel">Cancel</button><button class="btn btn-neon small" id="nf-create">Create</button></div>`,
      root=>{
        const nm=$('#nf-name',root); if(nm) nm.focus();
        const c=$('#nf-cancel',root); if(c) c.onclick=closeModal;
        const go=()=>{ const name=((nm&&nm.value)||'').trim().slice(0,40); if(!name){ toast('enter a name'); return; }
          const isEnc=(($('input[name="nf-enc"]:checked',root)||{}).value==='1');
          if(FilesIdx.addFolder(name, isEnc)){ _S._filesFolder=name; closeModal(); renderBlossom(); } else toast('folder exists'); };
        const g=$('#nf-create',root); if(g) g.onclick=go;
        if(nm) nm.addEventListener('keydown',e=>{ if(e.key==='Enter') go(); });
      });
  }
  // Encrypt ANY file with the master key (IV from content → identical input dedups), upload the
  // ciphertext (noMirror), and record it like a music track so trackUrl() can decrypt it on open.
  async function uploadEncFile(file, folder, statEl){
    if(!_S.signer.nip44enc) throw new Error("signer can't encrypt (needs NIP-44)");
    const setS=t=>{ if(statEl) statEl.textContent=t; };
    const mk=await FilesIdx._ensureMK();
    const buf=new Uint8Array(await file.arrayBuffer());
    setS('encrypting…');
    const blob=await _masterEncrypt(mk, buf, await _contentIV(buf));
    setS('uploading…');
    // Encrypted drive content is a COPY OF A FILE — never media to prepare. See syncBlobs.put.
    const url=await uploadBlob(new File([blob],(file.name||'file')+'.enc',{type:'application/octet-stream'}), {noMirror:true, keep:true, noCompress:true});
    const sha=_shaFromUrl(url); if(!sha) throw new Error('upload returned no hash');
    FilesIdx.setFile(sha,{name:file.name||'file', folder, mime:file.type||'application/octet-stream', enc:true, mk:true, size:buf.length, ts:Math.floor(Date.now()/1000)});
    return sha;   // notes.js needs it: a Notes attachment is referenced from the note by its hash
  }
  // Decrypt any master-key blob to an object URL — the generic half of trackUrl(), for images and
  // documents rather than audio. Its own small LRU: the music player pins the playing track in
  // _trackUrls and a note full of pictures would evict it.
  const _encUrls={}; const _encOrder=[]; const _encPending={};
  async function encFileUrl(sha, mimeHint){
    if(_encUrls[sha]) return _encUrls[sha];
    // SHARE the work in flight. The cache above is only populated after the fetch+decrypt resolves,
    // so two callers asking for the same blob at the same time — which is the normal case, a note
    // rendering a picture inline while the attachment strip renders its thumbnail — each downloaded
    // and decrypted the whole file, and the second object URL overwrote (and leaked) the first.
    if(_encPending[sha]) return _encPending[sha];
    const p = _encFileUrl(sha, mimeHint).finally(()=>{ delete _encPending[sha]; });
    _encPending[sha]=p; return p;
  }
  /* THE DRIVE HAS TWO ENCRYPTION SCHEMES, AND READING ONLY THE NEWER ONE LOOKS EXACTLY LIKE A WRONG KEY.
   *
   *   v2 (`meta.mk`)     — the master key, IV prepended to the blob.
   *   v1 (`meta.keyenc`) — a key PER FILE, itself NIP-44-encrypted to the owner.
   *
   * `trackUrl` has always branched on that; `_encFileUrl` — the generic path behind Notes attachments,
   * the wallpaper and its picker — did not, and ran every blob through `_masterDecrypt`. On a v1 file
   * that throws AES-GCM's `OperationError`, whose message is "The operation failed for an
   * operation-specific reason": indistinguishable from the wrong-key case below, which is what the
   * retry then spent its effort on — re-fetching a key that was never the problem and decrypting
   * with the same wrong SCHEME again. Reported as a desktop background that would neither preview
   * nor apply, while the identical bytes played fine in the music player.
   *
   * One decryptor, used by both callers, because two of them is how they drifted in the first place. */
  async function _driveDecrypt(m, bytes, indexed){
    /* NOT EVERY FILE IN THE DRIVE IS ENCRYPTED, and decrypting one that isn't fails identically to a
     * wrong key. The index carries `enc` and the Files explorer branches on it everywhere — a plain
     * file keeps its public URL and a normal icon, a sealed one gets the lock card. This reader did
     * not, so an ordinary image (dragged into a folder, uploaded to the public list) came back as
     * AES-GCM's `OperationError`, which is the SAME message a wrong key produces. That is what a
     * desktop background that would neither preview nor apply actually was: a picture that needed no
     * decrypting at all, being decrypted.
     *
     * `enc` may only be trusted when we actually HAVE the index record: with no record the flag is
     * absent rather than false, and those two must not be confused — a Notes attachment reaches here
     * with a synthesised `{mime}` from the note itself and IS encrypted, so reading its missing flag
     * as "plaintext" would hand back ciphertext and draw a broken image with nothing said.
     *
     * `indexed` is passed in rather than sniffed off the object. The first version inferred it from
     * `m.name !== undefined`, and the index does not promise a name — os.js's own `backgrounds()`
     * falls back to the sha for exactly that reason. So a real, unencrypted record that happened to
     * carry no name skipped this branch and went on to be decrypted, which is the same failure over
     * again on the same screen. */
    if(indexed && m && !m.enc && !m.mk && !m.keyenc) return bytes;
    if(m && m.keyenc && !m.mk){
      const {k,iv}=JSON.parse(await _S.signer.nip44dec(_S.ME.pubkey, m.keyenc));
      return await _aesDecrypt(bytes, _b64u8(k), _b64u8(iv));
    }
    return await _masterDecrypt(await FilesIdx._ensureMK(), bytes);
  }
  async function _encFileUrl(sha, mimeHint){
    // mimeHint: the drive index is not the only record of a blob any more — a Notes attachment also
    // stores its own name/mime on the note. If a bulk import was interrupted before the index was
    // flushed, meta() is empty, and an object URL typed application/octet-stream does NOT render in
    // an <img>. The note's own copy of the type keeps the picture showing.
    let rec=FilesIdx.meta(sha);
    let m=rec || (mimeHint ? {mime:mimeHint} : null);
    const r=await fetch(mediaServer()+'/'+sha); if(!r.ok) throw new Error('blob HTTP '+r.status);
    const blob=new Uint8Array(await r.arrayBuffer());
    let plain;
    try{
      plain=await _driveDecrypt(m, blob, !!rec);
    }catch(e){
      /* AES-GCM failing is "wrong key", not "bad file" — WebCrypto reports it as OperationError,
       * "The operation failed for an operation-specific reason", which reads like corruption and
       * sent this hunt in the wrong direction for a day. A device can hold the wrong key (it once
       * minted its own; see _ensureMK), so before believing the file is unreadable, re-read the key
       * from the server, which is the authority, and try once more. Doing it HERE is what heals a
       * device that is already stuck, without the user having to do anything. */
      try{ await FilesIdx.pull(); }catch(_){ }
      // The pull refreshes the INDEX as well as the key, so re-read the file's record: a blob whose
      // meta was missing (hence no scheme to pick) may have just got it back.
      rec=FilesIdx.meta(sha) || rec;
      m=rec || m;
      try{
        plain=await _driveDecrypt(m, blob, !!rec);
      }catch(e2){
        /* SAY WHICH SCHEME FAILED. Every path here ends in `OperationError`, whose text is the same
         * whether the key is wrong, the bytes are not ciphertext, or the file was never encrypted —
         * so the bare message sent this hunt down the key-healing path twice. Naming the scheme and
         * the size makes the next report diagnose itself. */
        const scheme = (m && m.mk) ? 'master key' : (m && m.keyenc) ? 'per-file key'
                     : (m && m.name !== undefined) ? 'stored as plain (enc flag off)' : 'no index entry';
        const err = new Error((e2 && e2.message || e2) + ` [${scheme}, ${blob.length} bytes]`);
        err.cause = e2;
        throw err;
      }
    }
    const u=URL.createObjectURL(new Blob([plain],{type:(m&&m.mime)||'application/octet-stream'}));
    _encUrls[sha]=u; _encOrder.push(sha);
    _encEvict();
    return u;
  }
  const ENC_LRU = 40;
  function _encEvict(){
    if(_encOrder.length <= ENC_LRU) return;
    // What is still PAINTING. Revoking a URL that is breaks the picture it belongs to — a note with
    // more images than the LRU holds used to lose the ones you had scrolled past, which reads as
    // "the attachment is gone". Collected ONCE per eviction: asking per candidate walked the whole
    // live document.images for every entry, and since an in-use entry goes back on the queue rather
    // than being dropped, that cost grew with every decrypt in the session.
    const live = new Set();
    for(const im of document.images) if(im.src) live.add(im.src);
    let guard = _encOrder.length;
    while(_encOrder.length > ENC_LRU && guard-- > 0){
      const old = _encOrder.shift();
      const u = _encUrls[old];
      if(!u) continue;
      if(live.has(u)){ _encOrder.push(old); continue; }    // still on screen → to the back of the queue
      URL.revokeObjectURL(u); delete _encUrls[old];
    }
  }
  /* Where a selected directory lands in the drive.
   *
   * A directory chosen from Files home/All has to remain a directory. The old importer always
   * removed the first component of `webkitRelativePath`, so choosing `Pictures/a.jpg` at home
   * uploaded the bytes but indexed `a.jpg` in All. The chooser closed, no Pictures folder appeared,
   * and the operation looked as if it had disappeared. Inside an existing drive folder we still
   * discard that outer chooser name: selecting `Camera` while standing in `Photos` means import its
   * contents into Photos, with Camera's real subdirectories preserved below it.
   */
  function _uploadTargetFolder(current, relative){
    const rel=String(relative||'').replace(/^\/+|\/+$/g,'');
    if(!rel.includes('/')) return current;
    const parts=rel.split('/').filter(Boolean), selected=parts.shift()||'';
    const below=parts.slice(0,-1).join('/');
    if(current) return below ? (current+'/'+below) : current;
    return below ? (selected+'/'+below) : selected;
  }
  /* A successful PUT is already proof that this blob exists. Do not make its visibility depend on
   * the follow-up /list request being fast or immediately consistent: renderPublicFiles paints this
   * cache first, then replaces it with the server's authoritative listing when that arrives. This is
   * especially important for a folder chosen from Home, where no listing has been fetched yet. */
  function _rememberUploadedBlob(sha, url, file){
    if(!sha) return;
    const row={sha256:sha, url:url||mediaServer().replace(/\/$/,'')+'/'+sha,
      name:(file&&file.name)||'', type:(file&&file.type)||'', size:+(file&&file.size)||0,
      uploaded:Math.floor(Date.now()/1000)};
    const old=Array.isArray(_S._filesGridList)?_S._filesGridList:[];
    _S._filesGridList=old.filter(b=>b&&b.sha256!==sha).concat(row);
    if(_S._blobHave) _S._blobHave.add(sha);
    _S._blobSizes.set(sha,row.size);
  }
  async function uploadFilesSeq(files){
    files=files.filter(Boolean); if(!files.length) return;
    /* The per-file loop is sequential, but the picker and drop zone can start this function again
     * while its first invocation is awaiting a PUT.  Those invocations share cancellation state,
     * batch authorization and the index batch: one completion can clear the other authorization
     * and Stop cancels both.  Keep the operation single-flight and tell the user to retry rather
     * than accepting a second selection whose ownership cannot be isolated safely. */
    if(_S._uploading){ toast('Another upload is still running — wait for it to finish, then try again.'); return; }
    const folder=_S._filesFolder, music=folder==='Music';   // capture: navigating mid-upload won't misfile
    // A FOLDER upload (webkitdirectory / dropped dir) carries webkitRelativePath like "Top/sub/pic.jpg".
    // Preserve that structure instead of flattening everything into one folder: each file lands in a
    // folder derived from its subpath. Capture the paths NOW — compressImage() below returns a fresh File
    // that has NO webkitRelativePath, and the batch may reorder-replace `files`, so read them up front and
    // index-align. At Files home/All, the chosen top-level directory becomes a real drive folder; inside
    // an existing folder, only its subdirectories are nested there. Only the plain path uses it; Music /
    // encrypted folders keep their single-folder behavior.
    const _relPaths=files.map(f=>(f&&(f.webkitRelativePath||f._pcRelativePath))||'');
    const _subFolder=(i)=>_uploadTargetFolder(folder,_relPaths[i]);
    // FAIL-CLOSED: never upload into a NAMED folder before the index has loaded. If the folder's
    // encrypted flag isn't known yet, uploading would silently take the PLAINTEXT path and put a
    // world-readable blob on Blossom (the leaked-file bug). Refuse until we know the folder's status.
    const _importsFolder=_relPaths.some(p=>String(p||'').includes('/'));
    if(!music && (folder || _importsFolder) && !FilesIdx._pullDone){ toast('One sec — still loading your folders. Try that again in a moment.'); return; }
    _uploadCancel=false;
    _S._uploading++;
    /* Resolve security PER FILE. A drop can contain several directories, and from Home one of them
       may target an existing encrypted tree while another is public. One batch-wide `encFolder`
       derived from the current screen (`null` on Home) sent the former to public Blossom as plain
       bytes. Descendants inherit encryption through isEncFolder(). */
    const _targetFolders=files.map((_,i)=>_subFolder(i));
    const _targetEncrypted=_targetFolders.map(tf=>!music && FilesIdx.isEncFolder(tf));
    const encFolder=_targetEncrypted.some(Boolean);
    const big=files.length>20;   // a folder import → compact summary, not 2000 DOM rows
    const q=$('#bl-queue');
    if(q) q.innerHTML = big ? `<div class="up-summary" id="up-sum">Preparing ${files.length} files…</div>`
      : files.map((f,i)=>`<div class="up-item"><span class="up-name">${enc(f.name)}</span><span class="up-stat" id="up-stat-${i}">queued</span></div>`).join('');
    FilesIdx.beginBatch();   // collapse the index save (a 2000-file import must NOT re-save the index per file)
    // Pre-sign the whole batch: ONE signer prompt instead of one per file (see _signUploadBatch). Encrypted
    // folders are skipped — their bytes are produced per file during the upload, so their hashes aren't
    // knowable up front. Best-effort: on any failure we simply fall back to per-file signing.
    let _batchPrepped=null;
    if(!encFolder && files.length>1){ try{ _batchPrepped=await _signUploadBatch(files); }catch(_){ _batchPrepped=null; } }
    if(_batchPrepped && _batchPrepped.length===files.length) files=_batchPrepped;   // upload the exact bytes we hashed
    // Register the subfolders this upload will populate — setFile only TAGS a file; folders() is a
    // separate registry, so an unregistered folder wouldn't appear in the Files list. Deduped up front
    // (a handful per import, not per file), plain uploads only.
    if(!music){
      const _seen=new Set(FilesIdx.folders());
      /* Encrypted targets too: `Private/photos` inherits encryption from `Private` (isEncFolder walks
       * ancestors), but the folder LIST is only what is registered — skipping them imported every file
       * into a subfolder no chip, tile or picker could show. */
      for(let i=0;i<files.length;i++){ const tf=_targetFolders[i]; if(tf && !_seen.has(tf)){ _seen.add(tf); try{ FilesIdx.addFolder(tf, false, true); }catch(_){} } }
    }
    /* "skipped" was one bucket covering two very different outcomes — a file REJECTED as non-audio
     * and a file ALREADY imported — and in a batch of more than 20 there are no per-file rows, so all
     * anyone saw was "⏭ 1 skipped" with no reason at all. Counted apart, and the first reason is
     * carried into the summary, because a skip you cannot explain is indistinguishable from a bug. */
    let done=0, ok=0, skip=0, dup=0, fail=0, why='';
    for(let i=0;i<files.length;i++){
      if(_uploadCancel) break;
      const stat=big?null:$('#up-stat-'+i);
      try{
        if(music && i===0){
          // Learn what the server ACTUALLY holds before the first dedup decision, so a stale index
          // entry cannot refuse an upload. Best effort — a failure leaves _blobHave null, which the
          // check reads as "unknown" and falls back to the old behaviour.
          try{ await _refreshBlobHave(); }catch(_){}
        }
        if(music){
          if(!_looksAudio(files[i])){ skip++;
            const seen = files[i].type || 'no file type';
            if(!why) why = `“${files[i].name}” was not recognised as audio (${seen})`;
            console.warn('[music] rejected', files[i].name, 'type=', files[i].type, 'size=', files[i].size);
            if(stat) stat.textContent='skipped — not audio (' + seen + ')'; }
          else if(_musicHasSrc(files[i])){ dup++;
            if(!why) why = `“${files[i].name}” is already in your library`;
            if(stat){ stat.textContent='already imported ✓'; stat.className='up-stat ok'; } }   // resume
          else { await uploadMusicTrack(files[i], stat); ok++; if(stat){ stat.textContent='✓'; stat.className='up-stat ok'; }
            if(++done%25===0){ await FilesIdx.endBatch(); FilesIdx.beginBatch(); } }   // checkpoint so a crash keeps progress
        } else if(_targetEncrypted[i]){
          const sha=await uploadEncFile(files[i], _targetFolders[i], stat);
          /* The PUT succeeded and uploadEncFile indexed the plaintext name/type. Mirror the public
           * optimistic listing too: an eventually-consistent /list response must not make a
           * completed encrypted folder import invisible until the next manual Refresh. */
          _rememberUploadedBlob(sha,'',files[i]);
          ok++; if(stat){ stat.textContent='🔒'; stat.className='up-stat ok'; }
          if(++done%25===0){ await FilesIdx.endBatch(); FilesIdx.beginBatch(); }
        } else {
          if(stat) stat.textContent='uploading…';
          const stored={};
          const url=await uploadBlob(files[i],{hashOut:stored}); const sha=stored.sha||_shaFromUrl(url);
          if(!sha) throw new Error('upload completed without a content hash');
          FilesIdx.setFile(sha, {name:files[i].name, folder:_targetFolders[i], mime:files[i].type||'', size:files[i].size, ts:Math.floor(Date.now()/1000)});
          _rememberUploadedBlob(sha,url,files[i]);
          ok++; if(stat){ stat.textContent='✓'; stat.className='up-stat ok'; }
          if(++done%25===0){ await FilesIdx.endBatch(); FilesIdx.beginBatch(); }
        }
      }catch(e){ fail++; if(_blossomDenied(e)) requestBlossomAccess(); if(stat){ stat.textContent='✗'; stat.className='up-stat err'; stat.title=e.message||'failed'; } }
      const prog=`${i+1} / ${files.length} · ✓${ok}${dup?' ↺'+dup:''}${skip?' ⏭'+skip:''}${fail?' ✗'+fail:''}`;
      _uploadBadge('Uploading '+prog);   // persists across views
      if(big){ const s=$('#up-sum'); if(s) s.textContent='Uploading… '+prog; }
    }
    /* Bytes on Blossom are only half of a folder upload. The encrypted index is what records their
     * names and target folders, and endBatch deliberately reports whether that write reached the
     * server. Do not announce durable completion when it did not; _save keeps the dirty index and
     * schedules its retry, while the optimistic blob cache below still makes the uploads visible
     * on this device immediately. */
    let indexSaved=false, indexErr='';
    try{ indexSaved=!!(await FilesIdx.endBatch()); }
    catch(e){ indexErr=String((e&&e.message)||e||'index save failed'); }
    _S._uploadBatchAuth=null;   // the batch auth never outlives its batch (it commits only to THESE hashes)
    _S._uploading=Math.max(0, _S._uploading-1);
    /* "Uploaded — folder list waiting to save" was true and useless. It is the sentence somebody
     * reads when their file has reached the server and their folder is still empty, and it names no
     * cause, suggests no action, and reads as if the save is merely queued when it may have been
     * REFUSED. Measured on a live node: eight uploads, eight relay refusals of the index write, and
     * nothing anywhere said so. Whatever the save knows about why, say it here. */
    let _why = '';
    try{ _why = (typeof Relay!=='undefined' && Relay._authBail) ? Relay._authBail : (indexErr||''); }catch(_){ _why = indexErr||''; }
    const completion=_uploadCancel?'Stopped':(indexSaved?'Done'
      :('Uploaded — folder list NOT saved'+(_why?': '+_why:'')));
    const summary=`${completion} — ✓ ${ok} added${dup?(' · ↺ '+dup+' already there'):''}${skip?(' · ⏭ '+skip+' skipped'):''}${fail?(' · ✗ '+fail+' failed'):''}`
      + (!indexSaved && indexErr ? ` — ${indexErr}` : '')
      + ((skip||dup) && why ? ` — ${why}` : '');
    _uploadBadge(summary, true);   // self-removes after 12s (timer lives in _uploadBadge)
    if(big&&q){ const s=$('#up-sum'); if(s) s.textContent=summary; }
    toast(summary);
    // endBatch() has committed the folder registry and file rows, so repaint from that exact state.
    // Deferring this used to leave a completed directory import invisible for an arbitrary 700ms;
    // it could remain invisible indefinitely when the page was backgrounded before the timer ran.
    if(_S.VIEW==='blossom') renderBlossom();
  }

  return {
    _blobAlreadyStored, _driveDecrypt, _filesSelBar, _officeSession, _openFromCommandLine,
    _openHostFile, _openWithSheet, _syncBlobBytes, _walkEntries, encFileUrl, openCodeFile,
    openOfficeFile, openPreviewFile, openSyncCodeFile, openSyncOfficeFile, renderBlossom,
    renderOfficeHome, saveBlobDoc, uploadEncFile,
  };
};

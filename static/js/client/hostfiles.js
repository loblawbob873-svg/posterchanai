/* THE COMPUTER'S OWN DISK, in the Files screen.
 *
 * Files already browses two sources — the encrypted drive on Blossom, and a synced folder's
 * manifest — and on PosterChanOS there is an obvious third: the machine you are sitting at. This
 * is that source. It is a MODULE rather than another branch inside app.js's Files renderer for two
 * reasons: that renderer is long and shared, and this has to be absent everywhere the bridge is
 * (a browser tab has no filesystem), which is easier to be honest about from outside it.
 *
 * IT IS NOT A SECOND EXPLORER. The sort order, the tiles-vs-details switch and the comparator are
 * app.js's and are passed in — a second set of rules for "which way is this folder sorted" is how
 * two screens that look the same start disagreeing.
 *
 * WHAT IT WILL NOT DO: it does not upload anything anywhere by itself. A file on this disk is on
 * this disk; putting a copy on the encrypted drive is a deliberate act with its own button, because
 * the two have completely different privacy properties and a file manager that blurs them is one
 * that eventually puts somebody's tax return on a relay.
 */
(function(root){
  'use strict';

  const HOST = () => root.pcHost || null;
  const available = () => !!HOST();

  const H = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  /* ── the pure half, which is what the tests run ─────────────────────────────────────────────── */

  /** The column value the explorer's comparator asks for. Folders sort as folders, never by size. */
  function keyOf(entry, col){
    const e = entry || {};
    if(col === 'size') return e.dir ? -1 : Number(e.size || 0);
    if(col === 'modified') return Number(e.created || e.mtime || 0);
    if(col === 'type') return e.dir ? '' : (String(e.name || '').split('.').pop() || '').toLowerCase();
    return String(e.name || '');
  }

  /* FOLDERS FIRST, ALWAYS, and then whatever the person chose. Every file manager does this and the
   * reason is navigation: the folders are the thing you are moving THROUGH, and interleaving them
   * with files by date makes a directory of a thousand items unusable. It is applied on top of the
   * shared comparator rather than inside it, so the drive and a synced folder keep their own
   * ordering — neither of them has folders in the list at all. */
  function order(entries, cmp, opts){
    const o = opts || {};
    const rows = (entries || []).filter(e => o.hidden ? true : !e.hidden);
    return rows.slice().sort((a, b) => {
      if(!!a.dir !== !!b.dir) return a.dir ? -1 : 1;
      return cmp ? cmp(a, b) : String(a.name).localeCompare(String(b.name));
    });
  }

  /* THE PATH AS A ROW OF BUTTONS. Split so every ancestor is clickable — the way back up is the
   * single most used control in a file manager, and a text field is not it. */
  function crumbs(p){
    const s = String(p || '');
    /* Windows drive paths are not slash paths. Treating `C:\\Users\\me` as one segment produced a
     * breadcrumb with no usable parent, which became a dead end when a protected junction refused
     * to open. Keep the platform's separator and make the drive itself the root crumb. */
    const win = /^([A-Za-z]:)[\\/]/.exec(s);
    if(win){
      const rootPath = win[1] + '\\';
      const parts = s.slice(win[0].length).split(/[\\/]+/).filter(Boolean);
      const out = [{ label: win[1], path: rootPath }];
      let acc = rootPath;
      for(const seg of parts){ acc += (acc.endsWith('\\') ? '' : '\\') + seg; out.push({ label:seg, path:acc }); }
      return out;
    }
    if(!s || s === '/') return [{ label: '/', path: '/' }];
    const parts = s.split('/').filter(Boolean);
    const out = [{ label: '/', path: '/' }];
    let acc = '';
    for(const seg of parts){ acc += '/' + seg; out.push({ label: seg, path: acc }); }
    return out;
  }

  function parentPath(p){
    const cs = crumbs(p);
    return cs.length > 1 ? cs[cs.length - 2].path : null;
  }

  /* This control toggles, so its announced action must toggle too. On touch there is no checkbox
   * hover state to explain why a button still labelled “Select all” will actually clear the set. */
  function allSelected(rows, selected){
    const paths=(rows||[]).map(r=>String(r&&r.path||'')).filter(Boolean);
    return !!paths.length && paths.every(p=>selected&&selected.has(p));
  }

  /* A HOME-RELATIVE LABEL, because `/home/npub1fdtthaq…/Documents` is unreadable and the leading
   * two thirds of it never change. The full path is still what every operation uses. */
  function pretty(p, home){
    const s = String(p || ''), h = String(home || '');
    if(h && (s === h || s.startsWith(h + '/'))) return '~' + s.slice(h.length);
    return s;
  }

  /* WHAT A DELETE IS ABOUT TO DO, in words, before it does it. A file manager's delete is the one
   * action people want stated precisely — and this one is reversible, which is the most important
   * part of the sentence and the part a generic "Are you sure?" leaves out. */
  function deletePrompt(rows){
    const n = (rows || []).length;
    if(!n) return '';
    const dirs = rows.filter(r => r.dir).length;
    const what = n === 1
      ? '“' + rows[0].name + '”'
      : n + ' items' + (dirs ? ' (' + dirs + ' folder' + (dirs === 1 ? '' : 's') + ')' : '');
    return 'Move ' + what + ' to the trash?\n\nThey go to this computer\'s own bin, so you can put '
         + 'them back from any file manager on it.';
  }

  const cssEsc = (v) => (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape(v) : String(v).replace(/["\\]/g, '\\$&');
  /* What the clipboard holds, in words, on the button that will act on it. */
  function pasteLabel(clip){
    const n = (clip && clip.paths || []).length;
    return (clip && clip.move ? 'Move ' : 'Paste ') + (n === 1 ? '1 item' : n + ' items') + ' here';
  }
  function clipNote(clip){
    const n = (clip && clip.paths || []).length;
    return (n === 1 ? '1 item' : n + ' items') + (clip && clip.move ? ' cut' : ' copied')
         + ' — open a folder and paste (Ctrl+V)';
  }
  function doneNote(n, move, dest){
    const leaf = String(dest || '').split(/[\\/]/).filter(Boolean).pop() || String(dest || '/');
    return (move ? 'moved ' : 'copied ') + (n === 1 ? '1 item' : n + ' items') + ' to ' + leaf;
  }

  /* THE KEYBOARD, the way every file manager has it. One listener for the page, acting on the pane
   * this module drew last -- and only while that pane is on screen, nothing editable has the caret,
   * no menu or dialog is up, and (on the desktop) its window is the focused one. Ctrl+C in a note
   * must copy text, not files. */
  function _editable(t){
    return !!(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '')));
  }
  function _onKey(e){
    const pane = _pane, a = _act;
    if(!pane || !a || !pane.isConnected || e.defaultPrevented) return;
    if(_editable(e.target) || _editable(document.activeElement)) return;
    if(!pane.getClientRects().length) return;                       // hidden: another view is up
    const win = pane.closest && pane.closest('.osw');
    if(win && !win.classList.contains('focused')) return;
    if(document.querySelector('.menu-pop, .modal-bg, .ui-dialog')) return;
    const k = String(e.key || '').toLowerCase(), mod = e.ctrlKey || e.metaKey;
    let run = null;
    if(mod && !e.shiftKey && !e.altKey){
      if(k === 'c') run = a.copy; else if(k === 'x') run = a.cut;
      else if(k === 'v') run = _clipboard ? () => a.paste() : null;
      else if(k === 'a') run = a.selectAll;
    } else if(!mod && !e.altKey){
      if(k === 'delete') run = _sel.size ? a.trash : null;
      else if(k === 'f2') run = _sel.size === 1 ? a.rename : null;
      else if(k === 'escape') run = (_sel.size || _clipboard) ? a.clear : null;
    }
    if(!run) return;
    e.preventDefault();
    run();
  }
  function _bindKeys(){
    if(_keysBound || typeof document === 'undefined' || !document.addEventListener) return;
    _keysBound = true;
    document.addEventListener('keydown', _onKey);
  }

  const API = { available, keyOf, order, crumbs, parentPath, pretty, deletePrompt, extOf, barCrumbs,
                allSelected, pasteLabel, clipNote, doneNote, H };

  /* ── the visible half ────────────────────────────────────────────────────────────────────────
   *
   * Deliberately thin, and everything it needs from the Files screen is HANDED to it — the sort
   * comparator, the view mode, the byte formatter, the prompts. Reaching back into app.js for those
   * is how a module ends up depending on a private name that gets renamed.
   */
  let _path = '', _sel = new Set(), _hidden = false, _home = '', _clipboard = null;
  /* The pane last drawn and the actions it offers, for the keyboard. One listener for the life of
   * the page (see _onKey) acting on whatever this module most recently rendered. */
  let _pane = null, _act = null, _keysBound = false;

  const state = () => ({ path: _path, hidden: _hidden });
  const at = () => _path;
  const enter = (p) => { _path = String(p || ''); _sel = new Set(); };
  const leave = () => { _path = ''; _sel = new Set(); };

  async function roots(){
    const h = HOST(); if(!h) return [];
    try{
      const r = await h.roots();
      const home = (r || []).find(x => x.kind === 'home');
      if(home) _home = home.path;
      return r || [];
    }catch(_){ return []; }
  }

  /** Read a directory. Throws upward — the caller says so on screen rather than drawing "empty". */
  async function read(p){
    const h = HOST(); if(!h) throw new Error('this build has no filesystem');
    return h.list(p || _path);
  }

  /* THE EXTENSION, for the type column and the icon. A directory has none and must not be given
   * one — `Documents` is not a `DOCUMENTS file`. */
  function extOf(e){
    if(!e || e.dir) return '';
    const n = String(e.name || '');
    const dot = n.lastIndexOf('.');
    return dot > 0 ? n.slice(dot + 1).toLowerCase() : '';
  }

  /* THE CRUMBS THE SHARED TOOLBAR TAKES: `{label, to}` with an `h:` target, which is the prefix the
   * Files screen's one crumb router resolves to this source. The trail is shortened from the LEFT
   * when it is long, because the useful end of a path is the end — a crumb bar that wraps to three
   * lines on `/home/user/Pictures/2026/August/raw` pushes the file list off the screen. */
  function barCrumbs(p, home){
    const rows = crumbs(p).map(c => ({ label: c.label, to: 'h:' + c.path }));
    /* The home directory is one crumb reading `~`, not five reading `/ home npub1… `, which is the
     * same reason `pretty()` exists. Everything above home stays reachable through it. */
    const h = String(home || '');
    if(h){
      const hi = rows.findIndex(c => c.to === 'h:' + h);
      if(hi > 0) return [{ label: '~', to: 'h:' + h }].concat(rows.slice(hi + 1));
    }
    if(rows.length > 6) return [rows[0], { label: '…', to: rows[rows.length - 5].to }]
      .concat(rows.slice(rows.length - 4));
    return rows;
  }

  /* ONE ROW OR ONE TILE, drawn by the FILES SCREEN'S OWN builders — `ui.row` is `_fxDetailsRow` and
   * `ui.icon` is `_fxIcon`, the same two the drive uses. Nothing here invents a class name.
   *
   * That is the whole of this rewrite. The previous version drew `hf-bar`, `hf-crumbs`, `hf-grid`,
   * `hf-row`, `hf-acts`, `fx-tiles`, `fx-tile`, `fx-ico`, `fx-nm`, `fx-sub` and an `fx-details`
   * table — ELEVEN class names, and measured against client.css not one of them has a single rule.
   * So this pane was unstyled HTML inside a styled explorer: a bare table, bare buttons, no grid.
   * A file manager that looks like a broken web page is not a file manager anybody trusts with a
   * delete button. */
  function rowsHTML(entries, ui){
    const u = ui || {};
    const fmt = u.fmtBytes || ((n) => String(n));
    const when = u.fmtDate || ((t) => t ? new Date(t).toLocaleString() : '');
    const icon = u.icon || (() => '📎');
    const folderIcon = u.folderIcon || (() => '📁');
    const typeName = u.typeName || ((e) => (e ? e.toUpperCase() + ' file' : 'File'));
    const details = u.view === 'details';
    if(!entries.length) return '<div class="empty">This folder is empty.</div>';

    /* A PICTURE ON THIS COMPUTER CAN SHOW ITSELF.
     *
     * The drive's tiles have had thumbnails for a long time; This Computer never did -- reported as
     * "0 thumbnails loaded in File Manager", which on this pane was not a failure but an absence:
     * there was no thumbnail code here at all.
     *
     * It costs nothing now. `pcHost.fileUrl` addresses a local file over the shell's own scheme
     * (main.js `serveHostFile`), so the tile just points an <img> at it -- no read through the IPC
     * bridge, no bytes in this renderer's heap, and the browser decodes and scales it. Only IMAGES,
     * and only small ones: a thumbnail is worth a decode, and a 40MB RAW file is not.
     *
     * Absent on the web and in the APK, where `pcHost` does not exist -- those keep the glyph they
     * have always had, which is why this is an attribute and not a second code path. */
    const THUMB_EXT = /^(?:png|jpe?g|jfif|gif|webp|avif|bmp|ico)$/i;
    const THUMB_MAX = 12 * 1024 * 1024;
    const thumbAttr = (e, ext) => {
      try{
        if(e.dir || e.broken || !THUMB_EXT.test(String(ext || ''))) return '';
        if(Number(e.size) > THUMB_MAX) return '';
        const url = window.pcHost && pcHost.fileUrl ? pcHost.fileUrl(e.path) : '';
        return url ? ` style="background-image:url('${H(url)}')" data-thumb-host="1"` : '';
      }catch(_){ return ''; }
    };
    const cells = entries.map(e => {
      const ext = extOf(e);
      const sel = _sel.has(e.path);
      /* A FOLDER IS NOT A FILE TYPE. It gets the folder glyph, no size and the word "Folder" —
       * `_fxIcon` would answer 📎 for it, and a size column reading "0 B" beside a directory is a
       * statement about the directory's contents that is not true. */
      const ic = e.dir ? folderIcon() : (e.broken ? '⚠️' : icon(ext, e.mime || ''));
      if(details) return (u.row || (() => ''))({
        dir: e.dir, selected: sel, name: e.name + (e.link ? ' ↗' : ''), title: e.path,
        icon: ic, size: e.dir ? '' : fmt(e.size), type: e.dir ? 'Folder' : typeName(ext),
        when: when(e.created || e.mtime),
        box: `<button class="selbox hf-select" type="button" aria-label="${sel ? 'Deselect' : 'Select'} ${H(e.name)}"
          aria-pressed="${sel ? 'true' : 'false'}">${sel ? '✓' : ''}</button>`,
        acts: '',
      });
      /* TILES use the drive's own `.file-card` + `.file-icon` + `.meta` shape. The `data-p`/`data-d`
       * attributes are this source's own and are what the handlers below select on — the drive keys
       * its cards on a hash, and a path is not one. */
      return `<div class="file-card${sel ? ' selected' : ''}${e.dir ? ' isdir' : ''}"
           data-p="${H(e.path)}" data-d="${e.dir ? '1' : ''}" title="${H(e.path)}">
        <button class="selbox hf-select" type="button" aria-label="${sel ? 'Deselect' : 'Select'} ${H(e.name)}"
          aria-pressed="${sel ? 'true' : 'false'}">${sel ? '✓' : ''}</button>
        <div class="file-icon"${thumbAttr(e, ext)}>${ic}<span>${H(e.dir ? 'folder' : (ext || ''))}</span></div>
        <div class="meta"><span class="fname" title="${H(e.name)}">${H(e.name)}</span>
          <span class="fc-acts">${e.dir ? '' : H(fmt(e.size))}</span></div></div>`;
    }).join('');

    /* These rows DO have a selection control.  Passing false used to omit its header cell while
     * leaving the button in every row, shifting Name under the checkbox and making the first
     * (apparently empty) column consume most of a narrow host pane. */
    return (details ? (u.cols ? u.cols(true) : '') : '') + cells;
  }

  /* THE ROWS A DETAILS VIEW DRAWS carry `data-p` too, and `_fxDetailsRow` has no slot for it — it
   * is the drive's row and keys on a hash. Rather than widen that shared builder (which every other
   * source would then carry an unused attribute for), the path is stamped on after the fact, in
   * order: the rows are generated from the same array, one per entry. */
  function stampPaths(grid, entries){
    const rows = [...grid.querySelectorAll('.file-card')];
    if(rows.length !== entries.length) return;      // a header or an empty state — leave it alone
    entries.forEach((e, i) => {
      rows[i].dataset.p = e.path;
      if(e.dir) rows[i].dataset.d = '1';
    });
  }

  /** Draw the whole source into `pane`. `ui` carries what belongs to the Files screen. */
  async function render(pane, ui){
    if(!pane) return;
    const u = ui || {};
    /* Used by the FILE CLICK HANDLER below, so it belongs in render's scope. This was accidentally
     * declared inside rowsHTML(); the list painted normally, then every regular file click threw
     * `openable is not defined` before Video/Preview/Code/host opening could run. */
    const openable = u.openable || (() => false);
    let listing = null, err = '';
    try{ listing = await read(_path); }
    catch(e){ err = String((e && e.message) || e); }
    /* NAVIGATED AWAY WHILE IT READ. A directory on a sleeping USB disk takes seconds, and painting
     * its contents into a pane that is now showing something else is how a file manager shows you
     * the wrong folder's files under the right folder's name. */
    if(!pane.isConnected) return;
    if(err){
      const upPath = parentPath(_path);
      const bar = u.bar ? u.bar(barCrumbs(_path, _home)) : '';
      pane.innerHTML = bar + `<div class="fx-actions">
          <button class="btn btn-ghost small hf-error-up"${upPath ? '' : ' disabled'}>Up</button>
        </div><div class="empty">Couldn’t read ${H(pretty(_path, _home))} — ${H(err)}</div>`;
      if(u.bindBar) u.bindBar();
      const up = pane.querySelector('.hf-error-up');
      if(up && upPath) up.onclick = () => { enter(upPath); render(pane, ui); };
      return;
    }
    const details = u.view === 'details';
    /* THE SEARCH BOX IS THE SHARED ONE, so it has to filter something here or it is a control that
     * looks live and does nothing on one tab out of three. It matches the NAME, like the drive's. */
    const q = String((u.query && u.query()) || '').trim().toLowerCase();
    let rows = order(listing.entries, u.cmp && u.cmp(keyOf), { hidden: _hidden });
    if(q) rows = rows.filter(e => String(e.name || '').toLowerCase().includes(q));

    const bar = u.bar ? u.bar(barCrumbs(_path, _home)) : '';
    const everySelected = allSelected(rows, _sel);
    const oneSelected = _sel.size === 1
      ? rows.find(e => e.path === [..._sel][0] && !e.dir) : null;
    pane.innerHTML = bar
      + `<div class="fx-actions">
           <button class="btn btn-ghost small hf-up"${listing.parent ? '' : ' disabled'}>Up</button>
           <button class="btn btn-ghost small hf-new">New folder</button>
           <button class="btn btn-ghost small hf-all" aria-pressed="${everySelected?'true':'false'}">${everySelected?'Deselect all':'Select all'}${rows.length ? ' (' + rows.length + ')' : ''}</button>
           <button class="btn btn-ghost small hf-none"${_sel.size ? '' : ' disabled'}>Select none</button>
           <button class="btn btn-ghost small hf-more" aria-label="More" title="Show or hide dotfiles">⋯</button>
           ${_clipboard ? `<button class="btn btn-cyan small hf-paste">${pasteLabel(_clipboard)}</button>
             <button class="btn btn-ghost small hf-unclip" aria-label="Cancel" title="Forget what was cut or copied">✕</button>` : ''}
           <span class="spacer"></span>
           ${_sel.size ? `<span class="muted small">${_sel.size} selected</span>
             ${oneSelected && typeof u.shareFile === 'function'
               ? '<button class="btn btn-cyan small hf-share">Save to Files</button>' : ''}
             <button class="btn btn-ghost small hf-acts" aria-haspopup="menu">Actions ▾</button>` : ''}
         </div>
         <div class="files-grid${details ? ' details' : ''}" id="hf-grid">${
           q && !rows.length
             ? `<div class="empty">Nothing in ${H(pretty(_path, _home))} matches “${H(q)}”.</div>`
             : rowsHTML(rows, u)}</div>`;

    const $ = (s) => pane.querySelector(s);
    const $$ = (s) => [...pane.querySelectorAll(s)];
    const again = () => render(pane, ui);
    const grid = $('#hf-grid');
    if(details && grid) stampPaths(grid, rows);
    /* The shared toolbar's own controls — the crumbs, the search box, tiles-vs-details. Bound by the
     * Files screen, because every one of them changes state that lives there. */
    if(u.bindBar) u.bindBar();
    if(details && u.bindCols && grid) u.bindCols(grid);

    if(listing.parent){ const up = $('.hf-up'); if(up) up.onclick = () => { enter(listing.parent); again(); }; }
    const byPath = new Map(rows.map(r => [r.path, r]));
    const say = (e) => u.toast(String((e && e.message) || e));
    const chosen = () => [..._sel].map(x => byPath.get(x)).filter(Boolean);

    /* EVERY FILE OPERATION, ONCE. The toolbar, the right-click menu and the keyboard all call these,
     * so the three cannot drift into three slightly different ideas of what "paste" does. They used
     * to exist only as toolbar buttons that appeared AFTER a selection, which needs Ctrl+click or
     * the small tick box -- reported as "I don't see move for My Computer files", about a Cut and a
     * Paste that were there all along and could not be found. */
    const act = {
      cut: () => remember(true),
      copy: () => remember(false),
      paste: (into) => pasteInto(into || _path),
      moveTo: () => sendTo(true),
      copyTo: () => sendTo(false),
      newFolder: async () => {
        let name = '';
        try{ name = await u.prompt('Name for the new folder', { ok: 'Create' }); }catch(_){ return; }
        if(!name) return;
        try{ await HOST().mkdir(_path, name); }catch(e){ say(e); }
        again();
      },
      selectAll: () => { rows.forEach(r => _sel.add(r.path)); again(); },
      clear: () => {
        if(_sel.size){ _sel = new Set(); again(); return true; }
        if(_clipboard){ _clipboard = null; again(); return true; }
        return false;
      },
      rename: async () => {
        if(_sel.size !== 1) return;
        const r = byPath.get([..._sel][0]); if(!r) return;
        let to = '';
        try{ to = await u.prompt('Rename “' + r.name + '”', { value: r.name, ok: 'Rename' }); }
        catch(_){ return; }
        if(!to || to === r.name) return;
        try{ await HOST().rename(r.path, to); }
        catch(e){ say(e); }
        _sel = new Set();
        again();
      },
      trash: async () => {
        const list = chosen(); if(!list.length) return;
        let ok = false;
        try{ ok = await u.confirm(deletePrompt(list), { ok: 'Move to trash', danger: true }); }
        catch(_){ ok = false; }
        if(!ok) return;
        for(const r of list){
          try{ await HOST().trash(r.path); }
          catch(e){ u.toast(r.name + ': ' + String((e && e.message) || e)); }
        }
        _sel = new Set();
        again();
      },
      copyPath: async () => {
        const list = chosen(); if(!list.length) return;
        const text = list.map(r => r.path).join('\n');
        if(typeof u.copy === 'function') return u.copy(text, list.length === 1 ? 'path copied' : 'paths copied');
        try{ await navigator.clipboard.writeText(text); u.toast('path copied'); }catch(e){ say(e); }
      },
    };
    function remember(move){
      if(!_sel.size) return;
      _clipboard = { paths: [..._sel], move: !!move };
      _sel = new Set();
      u.toast(clipNote(_clipboard));
      again();
    }
    async function pasteInto(dest){
      if(!_clipboard) return;
      const clip = _clipboard;
      try{
        await HOST().transfer(clip.paths, dest, clip.move);
        /* A CUT IS SPENT BY ITS PASTE; A COPY IS NOT. The copied files are still where they were,
         * so pasting them into a second folder is a perfectly ordinary thing to do. */
        if(clip.move) _clipboard = null;
        u.toast(doneNote(clip.paths.length, clip.move, dest));
      }catch(e){ say(e); }
      again();
    }
    /* MOVE TO… / COPY TO… pick the destination in the machine's own folder chooser, so moving a
     * file somewhere does not mean first finding that folder in this pane. A build without the
     * chooser falls back to the clipboard, which gets there in two steps instead of one. */
    async function sendTo(move){
      const list = chosen(); if(!list.length) return;
      const h = HOST();
      if(!h.pickDirectory){
        remember(move);
        u.toast('Open the folder they should go to, then press Paste');
        return;
      }
      let dest = null;
      try{ dest = await h.pickDirectory({ title: move ? 'Move to…' : 'Copy to…', defaultPath: _path }); }
      catch(e){ say(e); return; }
      if(!dest) return;
      try{
        await h.transfer(list.map(r => r.path), dest, move);
        u.toast(doneNote(list.length, move, dest));
        _sel = new Set();
      }catch(e){ say(e); }
      again();
    }
    _pane = pane; _act = act;
    _bindKeys();

    const pick = (m) => { const f = act[m] || (m === 'open' && menuOpen) || (m === 'pasteInto' && menuPasteInto)
                                 || (m === 'selectNone' && (() => { _sel = new Set(); again(); }))
                                 || (m === 'dotfiles' && (() => { _hidden = !_hidden; again(); })); if(f) f(); };
    /* THE ONE LIST OF THINGS YOU CAN DO TO A SELECTION, shown by right-click and by Actions ▾ alike. */
    const itemsFor = (one) => [
      ...(one ? [['open', 'Open']] : []),
      ['cut', 'Cut'], ['copy', 'Copy'],
      ...(one && one.dir && _clipboard ? [['pasteInto', 'Paste into “' + one.name + '”']] : []),
      ['moveTo', 'Move to…'], ['copyTo', 'Copy to…'],
      ...(one ? [['rename', 'Rename']] : []),
      ['copyPath', one ? 'Copy path' : 'Copy paths'],
      ['trash', 'Move to trash', 'danger'],
    ];
    { const more = $('.hf-more'); if(more) more.onclick = () => {
        if(typeof u.menu !== 'function') return;
        u.menu(more, [['dotfiles', _hidden ? 'Hide dotfiles' : 'Show dotfiles']], pick);
      }; }
    { const allBtn = $('.hf-all'); if(allBtn) allBtn.onclick = () => {
        const paths = rows.map(r => r.path);
        if(allSelected(rows, _sel)) paths.forEach(p => _sel.delete(p)); else paths.forEach(p => _sel.add(p));
        again();
      }; }
    { const noneBtn = $('.hf-none'); if(noneBtn) noneBtn.onclick = () => { _sel = new Set(); again(); }; }
    const paste = $('.hf-paste');
    if(paste) paste.onclick = () => { paste.disabled = true; act.paste(); };
    { const unclip = $('.hf-unclip'); if(unclip) unclip.onclick = () => { _clipboard = null; again(); }; }
    $('.hf-new').onclick = act.newFolder;

    /* RIGHT-CLICK IS A MENU, as it is in every file manager. It used to toggle the selection, which
     * nobody expects and which left the operations with no second way in. */
    const menuAt = (ev, items) => {
      if(typeof u.menu !== 'function' || !items.length) return false;
      const a = document.createElement('span');
      a.style.cssText = 'position:fixed;width:1px;height:1px;left:' + ev.clientX + 'px;top:' + ev.clientY + 'px';
      document.documentElement.appendChild(a);
      try{ u.menu(a, items, pick); }
      finally{ a.remove(); }
      return true;
    };
    let menuOpen = null, menuPasteInto = null;
    if(grid) grid.oncontextmenu = (ev) => {
      if(ev.target.closest && ev.target.closest('.file-card[data-p]')) return;
      ev.preventDefault();
      menuAt(ev, [
        ...(_clipboard ? [['paste', pasteLabel(_clipboard)]] : []),
        ['newFolder', 'New folder'],
        ['selectAll', 'Select all'],
      ]);
    };
    $$('#hf-grid .file-card[data-p]').forEach(el => {
      const p = el.dataset.p;
      const select = el.querySelector('.hf-select');
      if(select) select.onclick = (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        if(_sel.has(p)) _sel.delete(p); else _sel.add(p);
        again();
      };
      el.onclick = (ev) => {
        /* A MODIFIER SELECTS, A PLAIN CLICK OPENS. On a folder "open" means walk into it; on a file
         * it means hand it to whatever this machine opens that kind of file with. */
        if(ev.ctrlKey || ev.metaKey || ev.shiftKey || _sel.size){
          ev.preventDefault();
          if(_sel.has(p)) _sel.delete(p); else _sel.add(p);
          again();
          return;
        }
        if(el.dataset.d){ enter(p); again(); return; }
        /* CLICKING THE FILE IS THE OPEN. When PosterChan Code can edit it you are asked which —
         * and handing it to the machine stays on that list, because that is what this click has
         * always done and it is still the right answer for most files. */
        const openHere = () => HOST().open(p).then(r => { if(r && r.ok === false) u.toast(r.why); },
                                                   e => u.toast(String((e && e.message) || e)));
        const meta = byPath.get(p) || {};
        openHere.mtime = Number(meta.mtime) || 0;
        const nm = meta.name || p;
        /* CLICKING THE FILE IS THE OPEN. When PosterChan Code can edit it you are asked which —
         * and `openHere` goes on that list, because handing it to the machine is what this click
         * has always done and is still the right answer for most files. The bridge call is built
         * HERE rather than in the caller: this is the only file that knows about the bridge. */
        const mime = meta.mime || '';
        if(u.openFile && openable(nm, mime)){
          ev.preventDefault(); u.openFile(p, nm, openHere, mime); return;
        }
        openHere();
      };
      el.oncontextmenu = (ev) => {
        ev.preventDefault();
        /* The menu acts on the SELECTION, so a right-click on something outside it makes it the
         * selection first -- the way every file manager behaves, and the only reading under which
         * "Cut" in that menu cannot surprise you by cutting three other files. */
        if(!_sel.has(p)){ _sel = new Set([p]); markSelected(); }
        menuOpen = () => el.onclick({ ctrlKey:false, metaKey:false, shiftKey:false, preventDefault(){} , _viaMenu:true });
        menuPasteInto = () => act.paste(p);
        const shown = menuAt(ev, itemsFor(_sel.size === 1 ? byPath.get(p) : null));
        if(!shown) again();
      };
      /* DRAG A FILE ONTO A FOLDER TO MOVE IT; hold Ctrl to copy. Dragging something outside the
       * selection drags just that thing, so a stray drag never carries files you forgot you had
       * ticked. */
      el.draggable = true;
      el.ondragstart = (ev) => {
        const paths = _sel.has(p) ? [..._sel] : [p];
        try{
          ev.dataTransfer.setData('application/x-pc-host-paths', JSON.stringify(paths));
          ev.dataTransfer.effectAllowed = 'copyMove';
        }catch(_){ }
      };
      if(el.dataset.d){
        const carries = (ev) => { try{ return [...ev.dataTransfer.types].includes('application/x-pc-host-paths'); }
                                  catch(_){ return false; } };
        el.ondragover = (ev) => {
          if(!carries(ev)) return;
          ev.preventDefault();
          try{ ev.dataTransfer.dropEffect = (ev.ctrlKey || ev.altKey) ? 'copy' : 'move'; }catch(_){ }
          el.classList.add('drop-target');
        };
        el.ondragleave = () => el.classList.remove('drop-target');
        el.ondrop = async (ev) => {
          el.classList.remove('drop-target');
          if(!carries(ev)) return;
          ev.preventDefault();
          let paths = [];
          try{ paths = JSON.parse(ev.dataTransfer.getData('application/x-pc-host-paths')) || []; }catch(_){ }
          paths = paths.filter(x => x && x !== p);
          if(!paths.length) return;
          const move = !(ev.ctrlKey || ev.altKey);
          try{
            await HOST().transfer(paths, p, move);
            u.toast(doneNote(paths.length, move, p));
            _sel = new Set();
          }catch(e){ say(e); }
          again();
        };
      }
    });
    /* A selection drawn in place, for the right-click case: repainting the whole pane under the
     * pointer would detach the card the menu is about to be anchored beside. */
    function markSelected(){
      $$('#hf-grid .file-card[data-p]').forEach(c => c.classList.toggle('selected', _sel.has(c.dataset.p)));
    }

    if(_sel.size){
      const actsBtn = $('.hf-acts');
      if(actsBtn) actsBtn.onclick = () => {
        const one = _sel.size === 1 ? byPath.get([..._sel][0]) : null;
        if(one){ const card = pane.querySelector('.file-card[data-p="' + cssEsc(one.path) + '"]');
                 menuOpen = card ? () => card.onclick({ ctrlKey:false, metaKey:false, shiftKey:false, preventDefault(){} }) : null;
                 menuPasteInto = () => act.paste(one.path); }
        if(typeof u.menu === 'function') u.menu(actsBtn, itemsFor(one), pick);
      };
      const sh = $('.hf-share');
      if(sh && oneSelected) sh.onclick = async () => {
        sh.disabled = true;
        try{ await u.shareFile(oneSelected); }
        catch(e){ u.toast(String((e && e.message) || e)); }
        finally{ sh.disabled = false; }
      };
    }
  }

  /* ONE FILE'S CONTENTS, for PosterChan Code. Thin on purpose: every guard (the size ceiling, the
   * NUL-byte check, the atomic rename, the mtime compare-and-swap) is in desktop/hostfs.js, because
   * a bridge must not be talked out of them by whoever is calling. */
  async function readText(p){
    const h = HOST(); if(!h || !h.readText) throw new Error('this build cannot open a local file');
    return h.readText(p);
  }
  async function writeText(p, text, mtime){
    const h = HOST(); if(!h || !h.writeText) throw new Error('this build cannot save a local file');
    return h.writeText(p, text, mtime || 0);
  }

  Object.assign(API, { render, rowsHTML, roots, read, readText, writeText, enter, leave, at, state,
                       home: () => _home, selection: () => [..._sel] });
  root.PCHostFiles = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);

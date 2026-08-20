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
    if(col === 'modified') return Number(e.mtime || 0);
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
    if(!s || s === '/') return [{ label: '/', path: '/' }];
    const parts = s.split('/').filter(Boolean);
    const out = [{ label: '/', path: '/' }];
    let acc = '';
    for(const seg of parts){ acc += '/' + seg; out.push({ label: seg, path: acc }); }
    return out;
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

  const API = { available, keyOf, order, crumbs, pretty, deletePrompt, H };

  /* ── the visible half ────────────────────────────────────────────────────────────────────────
   *
   * Deliberately thin, and everything it needs from the Files screen is HANDED to it — the sort
   * comparator, the view mode, the byte formatter, the prompts. Reaching back into app.js for those
   * is how a module ends up depending on a private name that gets renamed.
   */
  let _path = '', _sel = new Set(), _hidden = false, _home = '';

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

  function rowsHTML(entries, ui){
    const u = ui || {};
    const fmt = u.fmtBytes || ((n) => String(n));
    const when = u.fmtDate || ((t) => t ? new Date(t).toLocaleString() : '');
    if(!entries.length) return '<div class="empty">This folder is empty.</div>';
    if(u.view === 'details'){
      return `<table class="fx-details"><tbody>` + entries.map(e =>
        `<tr class="hf-row${_sel.has(e.path) ? ' sel' : ''}" data-p="${H(e.path)}" data-d="${e.dir ? '1' : ''}">
           <td class="hf-n">${e.dir ? '📁' : (e.broken ? '⚠️' : '📄')} ${H(e.name)}${e.link ? ' ↗' : ''}</td>
           <td class="hf-s">${e.dir ? '' : H(fmt(e.size))}</td>
           <td class="hf-t">${H(when(e.mtime))}</td>
         </tr>`).join('') + `</tbody></table>`;
    }
    return `<div class="fx-tiles">` + entries.map(e =>
      `<button class="fx-tile hf-row${_sel.has(e.path) ? ' sel' : ''}" data-p="${H(e.path)}"
               data-d="${e.dir ? '1' : ''}" title="${H(e.path)}">
         <span class="fx-ico">${e.dir ? '📁' : (e.broken ? '⚠️' : '📄')}</span>
         <span class="fx-nm">${H(e.name)}</span>
         <span class="fx-sub">${e.dir ? 'folder' : H(fmt(e.size))}</span>
       </button>`).join('') + `</div>`;
  }

  /** Draw the whole source into `pane`. `ui` carries what belongs to the Files screen. */
  async function render(pane, ui){
    if(!pane) return;
    const u = ui || {};
    let listing = null, err = '';
    try{ listing = await read(_path); }
    catch(e){ err = String((e && e.message) || e); }
    /* NAVIGATED AWAY WHILE IT READ. A directory on a sleeping USB disk takes seconds, and painting
     * its contents into a pane that is now showing something else is how a file manager shows you
     * the wrong folder's files under the right folder's name. */
    if(!pane.isConnected) return;
    if(err){
      pane.innerHTML = `<div class="empty">Couldn’t read ${H(pretty(_path, _home))} — ${H(err)}</div>`;
      return;
    }
    const rows = order(listing.entries, u.cmp && u.cmp(keyOf), { hidden: _hidden });
    pane.innerHTML =
      `<div class="hf-bar">
         <button class="btn btn-ghost small hf-up"${listing.parent ? '' : ' disabled'}>↑ Up</button>
         <div class="hf-crumbs">${crumbs(_path).map(c =>
           `<button class="hf-crumb" data-p="${H(c.path)}">${H(c.label)}</button>`).join('<span>/</span>')}</div>
         <span class="spacer"></span>
         <button class="btn btn-ghost small hf-new">New folder</button>
         <button class="btn btn-ghost small hf-hidden">${_hidden ? 'Hide dotfiles' : 'Show dotfiles'}</button>
       </div>
       <div class="hf-acts${_sel.size ? '' : ' hidden'}">
         <span class="muted small">${_sel.size} selected</span>
         <button class="btn btn-ghost small hf-rename"${_sel.size === 1 ? '' : ' disabled'}>Rename</button>
         <button class="btn btn-ghost small hf-del">Move to trash</button>
       </div>
       <div class="hf-grid">${rowsHTML(rows, u)}</div>`;

    const $ = (s) => pane.querySelector(s);
    const $$ = (s) => [...pane.querySelectorAll(s)];
    const again = () => render(pane, ui);

    if(listing.parent) $('.hf-up').onclick = () => { enter(listing.parent); again(); };
    $$('.hf-crumb').forEach(b => b.onclick = () => { enter(b.dataset.p); again(); });
    $('.hf-hidden').onclick = () => { _hidden = !_hidden; again(); };
    $('.hf-new').onclick = async () => {
      let name = '';
      try{ name = await u.prompt('Name for the new folder', { ok: 'Create' }); }catch(_){ return; }
      if(!name) return;
      try{ await HOST().mkdir(_path, name); }catch(e){ u.toast(String((e && e.message) || e)); }
      again();
    };

    const byPath = new Map(rows.map(r => [r.path, r]));
    $$('.hf-row').forEach(el => {
      const p = el.dataset.p;
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
        HOST().open(p).then(r => { if(r && r.ok === false) u.toast(r.why); },
                            e => u.toast(String((e && e.message) || e)));
      };
      el.oncontextmenu = (ev) => {
        ev.preventDefault();
        if(_sel.has(p)) _sel.delete(p); else _sel.add(p);
        again();
      };
      void byPath.get(p);
    });

    if(_sel.size){
      $('.hf-del').onclick = async () => {
        const chosen = [...(_sel)].map(x => byPath.get(x)).filter(Boolean);
        let ok = false;
        try{ ok = await u.confirm(deletePrompt(chosen), { ok: 'Move to trash', danger: true }); }
        catch(_){ ok = false; }
        if(!ok) return;
        for(const r of chosen){
          try{ await HOST().trash(r.path); }
          catch(e){ u.toast(r.name + ': ' + String((e && e.message) || e)); }
        }
        _sel = new Set();
        again();
      };
      const rn = $('.hf-rename');
      if(_sel.size === 1 && rn) rn.onclick = async () => {
        const r = byPath.get([..._sel][0]); if(!r) return;
        let to = '';
        try{ to = await u.prompt('Rename “' + r.name + '”', { value: r.name, ok: 'Rename' }); }
        catch(_){ return; }
        if(!to || to === r.name) return;
        try{ await HOST().rename(r.path, to); }
        catch(e){ u.toast(String((e && e.message) || e)); }
        _sel = new Set();
        again();
      };
    }
  }

  Object.assign(API, { render, roots, read, enter, leave, at, state,
                       home: () => _home, selection: () => [..._sel] });
  root.PCHostFiles = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);

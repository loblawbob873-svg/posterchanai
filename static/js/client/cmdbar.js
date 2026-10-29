/* THE COMMAND BAR (Super+Space on PosterChanOS, Ctrl+Space / Ctrl+K on the web desktop): one box that finds apps,
 * programs, notes, files and actions, and runs them -- the desktop's Spotlight.
 *
 * It draws; os.js decides. The sources are os.js's own search sources as DATA (_searchData: the same rows the Search
 * window shows), the actions are os.js's own functions, and "Search Nostr" / "Ask AI" hand the query to the screens
 * that already do those. So nothing here can find something the desktop search would not, or run something the
 * desktop could not.
 *
 * Every source fills in on its own clock (the notebook decrypts, the disk walk runs in another process); a newer query
 * owns the list, so a slow answer to an old one is dropped. Loaded on demand from os.js -- never at boot. */
(function(){
  'use strict';
  if(window.PCCmdbar) return;
  const enc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  const LABELS = { actions:'Actions', apps:'Apps', notes:'Notes', files:'Files', local:'This computer', more:'More' };
  let box = null, api = null, seq = 0, cur = -1, timer = 0, lastFocus = null;

  /* Styles ride the module (it never loads at boot, and neither should they). Built on the theme variables, so the
   * cyberpunk default glows and every other theme gets its own colours. Phones: a full-width sheet at the top. */
  const CSS = `
.pc-cmdbar{position:fixed;inset:0;z-index:2147483000;display:flex;justify-content:center;align-items:flex-start;font-family:Inter,system-ui,-apple-system,'Segoe UI',sans-serif;color:var(--text,#edeefa)}
.pc-cmdbar .cmd-back{position:absolute;inset:0;background:rgba(5,5,12,.55);backdrop-filter:blur(3px)}
.pc-cmdbar .cmd-panel{position:relative;margin-top:12vh;width:min(640px,calc(100vw - 32px));max-height:min(560px,76vh);
  display:flex;flex-direction:column;background:var(--panel2,#1a1a2a);border:1px solid rgba(var(--accent-rgb,60,232,255),.55);
  border-radius:var(--r,14px);box-shadow:var(--glow,0 0 18px -2px rgba(60,232,255,.45)),0 24px 60px rgba(0,0,0,.55);overflow:hidden;
  animation:cmdIn .14s ease-out}
@keyframes cmdIn{from{opacity:0;transform:translateY(-8px) scale(.98)}to{opacity:1;transform:none}}
.pc-cmdbar .cmd-head{display:flex;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line,rgba(125,210,255,.16))}
.pc-cmdbar .cmd-glyph{display:grid;place-items:center;color:var(--neon,#3ce8ff);filter:drop-shadow(0 0 6px rgba(var(--accent-rgb,60,232,255),.7))}
.pc-cmdbar .cmd-glyph svg{width:20px;height:20px}
.pc-cmdbar .cmd-q{flex:1;min-width:0;background:transparent!important;border:0!important;outline:0!important;box-shadow:none!important;padding:4px 0!important;margin:0;-webkit-appearance:none;appearance:none;color:var(--text,#edeefa);font:500 17px Inter,system-ui,sans-serif}
.pc-cmdbar .cmd-q::-webkit-search-cancel-button{display:none}
.pc-cmdbar .cmd-q::placeholder{color:var(--muted,#9fa1c6)}
.pc-cmdbar kbd{font:600 11px Inter,sans-serif;color:var(--muted,#9fa1c6);border:1px solid var(--line,rgba(125,210,255,.16));border-radius:6px;padding:2px 6px}
.pc-cmdbar .cmd-res{overflow-y:auto;padding:6px 0 8px;overscroll-behavior:contain}
.pc-cmdbar .cmd-sec-t{font:700 10.5px Orbitron,Inter,sans-serif;letter-spacing:.12em;text-transform:uppercase;color:var(--neon2,#ff5cf0);padding:10px 16px 4px}
.pc-cmdbar .cmd-row{display:flex;align-items:center;gap:12px;width:100%;padding:8px 16px;background:none;border:0;border-left:2px solid transparent;
  color:var(--text,#edeefa);text-align:left;cursor:pointer;font:inherit}
.pc-cmdbar .cmd-row.on{background:rgba(var(--accent-rgb,60,232,255),.12);border-left-color:var(--neon,#3ce8ff)}
.pc-cmdbar .cmd-ico{flex:0 0 26px;height:26px;display:grid;place-items:center;font-size:18px}
.pc-cmdbar .cmd-ico svg,.pc-cmdbar .cmd-ico img{width:22px;height:22px}
.pc-cmdbar .cmd-t{display:flex;flex-direction:column;min-width:0;flex:1}
.pc-cmdbar .cmd-t b{font-weight:600;font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pc-cmdbar .cmd-t i{font-style:normal;font-size:12px;color:var(--muted,#9fa1c6);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pc-cmdbar .cmd-empty{padding:18px 16px}
@media (max-width:640px){
  .pc-cmdbar .cmd-panel{margin-top:0;width:100vw;max-height:88vh;border-radius:0 0 var(--r,14px) var(--r,14px);border-top:0}
  .pc-cmdbar .cmd-row{padding:12px 16px}
  .pc-cmdbar .cmd-esc{display:none}
}`;
  function ensureCss(){
    if(document.getElementById('pc-cmdbar-css')) return;
    const st = document.createElement('style'); st.id = 'pc-cmdbar-css'; st.textContent = CSS; document.head.appendChild(st);
  }

  function close(){
    if(!box) return;
    box.remove(); box = null; clearTimeout(timer);
    try{ if(lastFocus && lastFocus.isConnected) lastFocus.focus({ preventScroll:true }); }catch(_){ }
  }
  function items(){ return box ? [...box.querySelectorAll('.cmd-row')] : []; }
  function mark(i){
    const rows = items(); if(!rows.length){ cur = -1; return; }
    cur = (i + rows.length) % rows.length;
    rows.forEach((r, n) => { r.classList.toggle('on', n === cur); r.setAttribute('aria-selected', n === cur ? 'true' : 'false'); });
    try{ rows[cur].scrollIntoView({ block:'nearest' }); }catch(_){ }
    box.querySelector('.cmd-q').setAttribute('aria-activedescendant', rows[cur].id);
  }
  function run(row){
    const fn = row && row._run; if(!fn) return;
    close();
    try{ const r = fn(); if(r && typeof r.catch === 'function') r.catch(e => api && api.say && api.say(e)); }
    catch(e){ api && api.say && api.say(e); }
  }
  function rowEl(r, i){
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'cmd-row'; b.id = 'cmd-row-' + (++rowEl.n); b.setAttribute('role', 'option');
    b.innerHTML = `<span class="cmd-ico">${r.icon || ''}</span><span class="cmd-t"><b>${enc(r.title)}</b>${r.sub ? `<i>${enc(r.sub)}</i>` : ''}</span>`
      + (r.hint ? `<kbd class="cmd-hint">${enc(r.hint)}</kbd>` : '');
    b._run = r.run;
    b.addEventListener('mousedown', e => e.preventDefault());   // keep the caret in the box
    b.onclick = () => run(b);
    b.onmousemove = () => { const n = items().indexOf(b); if(n !== cur) mark(n); };
    return b;
  }
  rowEl.n = 0;
  function section(key){
    let sec = box.querySelector(`.cmd-sec[data-source="${key}"]`);
    if(!sec){
      sec = document.createElement('section'); sec.className = 'cmd-sec'; sec.dataset.source = key;
      sec.innerHTML = `<div class="cmd-sec-t">${enc(LABELS[key] || key)}</div><div class="cmd-list" role="group"></div>`;
      // Sections keep a fixed order whatever order they answer in.
      const order = ['actions', 'apps', 'notes', 'files', 'local', 'more'];
      const after = [...box.querySelectorAll('.cmd-sec')].find(s => order.indexOf(s.dataset.source) > order.indexOf(key));
      box.querySelector('.cmd-res').insertBefore(sec, after || null);
    }
    return sec;
  }
  function fill(key, rows){
    if(!box) return;
    const sec = section(key), list = sec.querySelector('.cmd-list');
    list.replaceChildren(...rows.map(rowEl));
    sec.hidden = !rows.length;
    const empty = box.querySelector('.cmd-empty');
    if(empty) empty.hidden = items().length > 0;
    if(cur < 0 || cur >= items().length) mark(0); else mark(cur);
  }
  function match(a, ql){
    return !ql || String(a.title).toLowerCase().includes(ql) || (a.keywords || []).some(k => String(k).toLowerCase().includes(ql));
  }
  function query(q){
    const my = ++seq, ql = q.trim().toLowerCase();
    box.querySelector('.cmd-res').replaceChildren();
    const empty = document.createElement('div'); empty.className = 'cmd-empty muted small';
    empty.textContent = q.trim() ? 'Nothing here matches yet…' : 'Type to find apps, files, notes and actions.';
    box.querySelector('.cmd-res').appendChild(empty);
    cur = -1;
    fill('actions', (api.actions || []).filter(a => match(a, ql)).slice(0, q.trim() ? 6 : 8));
    if(!q.trim()) return;
    for(const key of api.plan || []){
      let src = null;
      try{ src = api.data(key, q.trim()); }catch(_){ src = null; }
      if(!src) continue;
      Promise.resolve().then(src).then(rows => rows || [], () => []).then(rows => { if(my === seq) fill(key, rows); });
    }
    const more = [];
    if(api.nostr) more.push({ icon:'🔎', title:`Search Nostr for “${q.trim()}”`, sub:'Posts, people and hashtags', run:() => api.nostr(q.trim()) });
    if(api.ai) more.push({ icon:'✨', title:`Ask AI: “${q.trim()}”`, sub:'Opens AI chat with this question', run:() => api.ai(q.trim()) });
    fill('more', more);
  }
  function open(opts){
    api = opts || {};
    if(box){ box.querySelector('.cmd-q').focus(); return; }
    lastFocus = document.activeElement;
    ensureCss();
    box = document.createElement('div');
    box.className = 'pc-cmdbar'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'Command bar');
    box.innerHTML = `<div class="cmd-back"></div><div class="cmd-panel">
      <div class="cmd-head"><span class="cmd-glyph"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg></span><input class="cmd-q" type="search" role="combobox" aria-expanded="true"
        aria-controls="cmd-res" placeholder="Search apps, files, notes — or type a command" autocomplete="off" spellcheck="false">
        <kbd class="cmd-esc">Esc</kbd></div>
      <div class="cmd-res" id="cmd-res" role="listbox"></div></div>`;
    document.documentElement.appendChild(box);
    const q = box.querySelector('.cmd-q');
    box.querySelector('.cmd-back').onclick = close;
    q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => box && query(q.value), 90); });
    q.addEventListener('keydown', e => {
      if(e.key === 'Escape'){ e.preventDefault(); e.stopPropagation(); close(); }
      else if(e.key === 'ArrowDown' || (e.key === 'Tab' && !e.shiftKey)){ e.preventDefault(); mark(cur + 1); }
      else if(e.key === 'ArrowUp' || (e.key === 'Tab' && e.shiftKey)){ e.preventDefault(); mark(cur - 1); }
      else if(e.key === 'Enter'){
        e.preventDefault();
        // An Enter typed before the debounce fired runs against the text actually in the box.
        if(timer && box && q.value.trim() && !items().length){ clearTimeout(timer); query(q.value); }
        const rows = items(); run(rows[cur >= 0 ? cur : 0]);
      }
    });
    query('');
    q.focus();
    requestAnimationFrame(() => { try{ q.focus(); }catch(_){ } });
  }
  window.PCCmdbar = { open, close, isOpen: () => !!box };
})();

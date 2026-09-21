/* Torrents — RSS subscriptions and per-file selection.
 *
 * WHY THIS IS A SEPARATE FILE THAT ATTACHES ITSELF.
 *
 * The Torrents view is rendered by app.js (`renderTorrents`), which is ~40k lines and is edited by
 * other work at the same time as this. So instead of growing that function, this module watches for
 * the view's own markup and ENHANCES it: one button in the tab strip's action row, one button per
 * torrent row, and a richer Add dialog on the button app.js already draws. Everything it adds is
 * idempotent and marked, so the 2-second progress repaint — which rebuilds those rows — costs a
 * button re-insert and nothing else. If the view ever moves into a file of its own, the two
 * `openFeeds()` / `openFiles()` entry points are the whole surface and can simply be called.
 *
 * THE TWO FEATURES:
 *
 *   Feeds   — subscribe to a torrent RSS feed (showRSS, nyaa, a Jackett/torznab endpoint) with
 *             optional title filters, so a feed does not download a whole category. "Check" is a
 *             PREVIEW by default: it shows what the filter would take without taking it, which is
 *             the only way a filter is writable before the fact rather than after.
 *
 *   Files   — choose which files inside a torrent to download. A season pack, a discography or a
 *             game with six language packs is mostly a download nobody asked for. Deselecting sets
 *             libtorrent priority 0: nothing is deleted, and re-selecting resumes rather than
 *             restarts. With a MAGNET there is no file list until the metadata arrives, so the
 *             picker says so and waits instead of showing an empty list that reads like a fault.
 */
(function(){
  'use strict';
  const PC = () => window.__PC || {};
  const enc = s => (PC().enc ? PC().enc(String(s==null?'':s))
                             : String(s==null?'':s).replace(/[&<>"']/g, c =>
                               ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])));
  const toast = m => { const t=PC().toast; if(t) t(m); };
  /* Our OWN byte formatter, deliberately. `_fmtBytes` lives inside app.js's closure and is not on
   * window.__PC — reaching for it is the documented `PC._fmtBytes is not a function` trap, and it
   * would throw inside a modal that is otherwise fine. */
  function fmtBytes(n){ n=Number(n)||0; const u=['B','KB','MB','GB','TB']; let i=0;
    while(n>=1024&&i<u.length-1){ n/=1024; i++; } return n.toFixed(n<10&&i>0?1:0)+' '+u[i]; }
  function ago(ts){ ts=Number(ts)||0; if(!ts) return 'never';
    const s=Math.max(0,Math.floor(Date.now()/1000-ts));
    if(s<60) return 'just now'; if(s<3600) return Math.floor(s/60)+'m ago';
    if(s<86400) return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago'; }

  // ------------------------------------------------------------------ API
  async function api(path, opts){
    try{ if(PC().ensureAiSession) await PC().ensureAiSession(); }catch(_){}
    const f = PC().authFetch || ((u,o)=>fetch(u,{credentials:'include',...(o||{})}));
    const r = await f('/api/torrent'+path, { ...(opts||{}),
      headers:{ 'Content-Type':'application/json', ...((opts&&opts.headers)||{}) } });
    if(!r.ok){
      const e=new Error('http '+r.status); e.status=r.status;
      // FastAPI's `detail` is a string for an HTTPException and a LIST OF OBJECTS for a 422;
      // pasting the latter into a toast reads "[object Object]" and tells nobody anything.
      try{ const d=(await r.json()).detail;
           e.detail = Array.isArray(d) ? d.map(x=>[(x.loc||[]).slice(-1)[0],x.msg].filter(Boolean).join(': ')).join('; ')
                    : (typeof d==='string' ? d : (d?JSON.stringify(d):'')); }catch(_){}
      throw e;
    }
    return r.json();
  }
  const errText = err => (err && (err.detail || err.message)) || 'something went wrong';

  /* The action endpoints address a torrent by its POSITION in the list, not by hash, and that list
   * reshuffles whenever anything is added or removed while this view polls every two seconds. So
   * resolve the hash to a CURRENT number at the moment of use — a number captured at render time
   * eventually names a different torrent. (Same rule app.js's `_torNum` follows.) */
  async function numFor(hash){
    const j = await api('/list');
    const hit = ((j&&j.torrents)||[]).find(t=>t.info_hash===hash);
    if(!hit){ const e=new Error('that torrent is no longer in the list'); e.detail=e.message; throw e; }
    return hit.num;
  }

  // ------------------------------------------------------------------ style (self-contained)
  const CSS = `
  .tmx-modal{max-width:720px}
  .tmx-files{max-height:48vh;overflow:auto;margin:10px 0;border:1px solid var(--line,#2a2a36);border-radius:8px}
  .tmx-f{display:flex;gap:8px;align-items:center;padding:6px 9px;border-bottom:1px solid var(--line,#2a2a36)}
  .tmx-f:last-child{border-bottom:none}
  .tmx-f input{flex:none;margin:0}
  .tmx-f .p{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
  .tmx-f .s{flex:none;font-size:12px;opacity:.7;font-variant-numeric:tabular-nums}
  .tmx-f.off .p{opacity:.45;text-decoration:line-through}
  .tmx-feed{padding:9px;border:1px solid var(--line,#2a2a36);border-radius:8px;margin-bottom:9px}
  .tmx-feed.off{opacity:.6}
  .tmx-feed .u{font-size:12px;opacity:.7;word-break:break-all}
  .tmx-feed .st{font-size:12px;opacity:.75;margin-top:4px}
  .tmx-feed .err{color:var(--danger,#e5484d)}
  .tmx-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
  .tmx-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  @media(max-width:640px){.tmx-grid{grid-template-columns:1fr}}
  .tmx-modal input[type=text],.tmx-modal input[type=url]{width:100%;box-sizing:border-box}
  .tmx-hint{font-size:12px;opacity:.7;margin:2px 0 8px}
  `;
  function style(){
    if(document.getElementById('tmx-style')) return;
    const s=document.createElement('style'); s.id='tmx-style'; s.textContent=CSS;
    document.head.appendChild(s);
  }

  // ------------------------------------------------------------------ file picker
  let _filesPoll = 0;
  function stopFilesPoll(){ if(_filesPoll){ clearInterval(_filesPoll); _filesPoll=0; } }

  /* Open the per-file picker for a torrent.
   *
   * `waitForMeta` is the add-time path: a magnet has no file list for its first few seconds, and an
   * empty list shown without explanation is indistinguishable from a torrent with no files. So the
   * picker says which it is and keeps asking, rather than showing nothing and stopping. */
  async function openFiles(hash, opts){
    style();
    const wait = !!(opts && opts.waitForMeta);
    const modal = PC().modal, close = PC().closeModal;
    if(!modal){ toast('this build has no modal host'); return; }
    let root = null;
    modal(`<h3>🗂 Choose files</h3>
      <div id="tmx-fbody"><div class="spinner"></div></div>`, box => { root = box; });
    if(root) root.classList.add('tmx-modal');
    const body = () => root && root.querySelector('#tmx-fbody');

    let num = null, files = [], pending = null, gone = false;
    const closed = () => !document.body.contains(root);

    async function load(){
      if(closed()){ gone=true; stopFilesPoll(); return; }
      try{
        if(num===null) num = await numFor(hash);
        const j = await api('/files/'+num);
        files = (j&&j.files)||[]; pending = (j&&j.pending_selection)||null;
      }catch(err){
        stopFilesPoll();
        const b=body(); if(b) b.innerHTML = `<div class="empty">Couldn't read that torrent's files.<br>
          <span class="muted small">${enc(errText(err))}</span></div>`;
        return;
      }
      draw();
    }

    function draw(){
      const b=body(); if(!b) return;
      if(!files.length){
        stopFilesPoll();
        b.innerHTML = `<div class="empty">${wait
          ? 'Fetching this torrent’s file list…<br><span class="muted small">A magnet carries no file list &mdash; it arrives from the swarm, usually within a few seconds.'
            + (pending && pending.length ? ' Your earlier selection is saved and will be applied the moment it does.' : '')
            + '</span>'
          : 'No file list yet.<br><span class="muted small">This torrent is still fetching its metadata.</span>'}</div>
          <div class="tmx-row"><button class="btn btn-ghost small" id="tmx-frefresh">Check again</button></div>`;
        const r=b.querySelector('#tmx-frefresh'); if(r) r.onclick=load;
        if(wait && !_filesPoll && !gone) _filesPoll=setInterval(load, 2500);
        return;
      }
      stopFilesPoll();
      const total = files.reduce((s,f)=>s+(Number(f.size)||0),0);
      const rows = files.map(f=>{
        const pct = Math.round((Number(f.progress)||0)*100);
        return `<label class="tmx-f${f.wanted?'':' off'}" data-i="${f.index}">
          <input type="checkbox" ${f.wanted?'checked':''}>
          <span class="p" title="${enc(f.path)}">${enc(f.path)}</span>
          <span class="s">${enc(fmtBytes(f.size))}${pct?' · '+pct+'%':''}</span></label>`;
      }).join('');
      b.innerHTML = `<div class="tmx-hint">Unticked files are not downloaded (libtorrent priority 0).
        Nothing is deleted &mdash; ticking one back on resumes it.</div>
        <input type="text" id="tmx-ffilter" placeholder="Filter this list…">
        <div class="tmx-files">${rows}</div>
        <div class="tmx-hint" id="tmx-fsum"></div>
        <div class="tmx-row">
          <button class="btn btn-ghost small" id="tmx-fall">Select all</button>
          <button class="btn btn-ghost small" id="tmx-fnone">Select none</button>
          <span style="flex:1"></span>
          <button class="btn btn-ghost small" id="tmx-fcancel">Close</button>
          <button class="btn btn-neon small" id="tmx-fsave">Save selection</button>
        </div>`;
      const boxes = () => Array.from(b.querySelectorAll('.tmx-f input'));
      function sum(){
        const picked = boxes().filter(x=>x.checked).map(x=>+x.closest('.tmx-f').dataset.i);
        const size = files.filter(f=>picked.indexOf(f.index)>=0).reduce((s,f)=>s+(Number(f.size)||0),0);
        const el=b.querySelector('#tmx-fsum');
        // The size of the CHOICE, not of the torrent. "12 of 40 files · 4.1 GB of 22 GB" is the one
        // number this dialog exists to change, and it has to move while you tick.
        if(el) el.textContent = `${picked.length} of ${files.length} file${files.length===1?'':'s'} · `
          + `${fmtBytes(size)} of ${fmtBytes(total)}`;
        return {picked, size};
      }
      b.querySelectorAll('.tmx-f').forEach(row=>{
        const cb=row.querySelector('input');
        cb.onchange=()=>{ row.classList.toggle('off', !cb.checked); sum(); };
      });
      b.querySelector('#tmx-fall').onclick=()=>{ boxes().forEach(x=>{ x.checked=true;
        x.closest('.tmx-f').classList.remove('off'); }); sum(); };
      b.querySelector('#tmx-fnone').onclick=()=>{ boxes().forEach(x=>{ x.checked=false;
        x.closest('.tmx-f').classList.add('off'); }); sum(); };
      const ff=b.querySelector('#tmx-ffilter');
      ff.oninput=()=>{ const q=ff.value.trim().toLowerCase();
        b.querySelectorAll('.tmx-f').forEach(row=>{
          row.style.display = !q || row.querySelector('.p').textContent.toLowerCase().indexOf(q)>=0 ? '' : 'none'; }); };
      b.querySelector('#tmx-fcancel').onclick=()=>{ stopFilesPoll(); if(close) close(); };
      const save=b.querySelector('#tmx-fsave');
      save.onclick=async()=>{
        const picked = sum().picked;
        /* Refused rather than sent: every file at priority 0 leaves a torrent that can never finish
         * and says nothing about why. Pause is the control for "stop this download". */
        if(!picked.length){ toast('select at least one file — use Pause to stop the whole torrent'); return; }
        save.disabled=true;
        try{
          num = await numFor(hash);
          const n = files.length;
          const j = await api('/files',{method:'POST',body:JSON.stringify({num, files:picked})});
          files = (j&&j.files)||files;
          toast(picked.length===n ? 'downloading all files'
                                  : 'downloading '+picked.length+' of '+n+' files');
          stopFilesPoll(); if(close) close();
        }catch(err){ toast('could not save that: '+errText(err)); save.disabled=false; }
      };
      sum();
    }

    await load();
  }

  // ------------------------------------------------------------------ feeds
  async function openFeeds(){
    style();
    const modal = PC().modal, close = PC().closeModal, uiConfirm = PC().uiConfirm;
    if(!modal){ toast('this build has no modal host'); return; }
    let root=null;
    modal(`<h3>📡 Torrent feeds</h3><div id="tmx-feedbody"><div class="spinner"></div></div>`,
          box => { root = box; });
    if(root) root.classList.add('tmx-modal');
    const body = () => root && root.querySelector('#tmx-feedbody');

    async function load(){
      let j=null;
      try{ j = await api('/feeds'); }
      catch(err){
        const b=body(); if(b) b.innerHTML = `<div class="empty">${err.status===503
          ? 'This server has no torrent client enabled.'
          : 'Couldn’t read the feed list.<br><span class="muted small">'+enc(errText(err))+'</span>'}</div>`;
        return;
      }
      draw(j);
    }

    function feedHtml(f){
      const status = f.last_error
        ? `<span class="err">last check failed: ${enc(f.last_error)}</span>`
        : (!f.primed
            ? `first check will learn this feed’s backlog and add nothing`
            : `checked ${enc(ago(f.last_check))} · ${f.added_total} added so far`);
      return `<div class="tmx-feed${f.enabled?'':' off'}" data-id="${enc(f.id)}">
        <div><strong>${enc(f.title||f.url)}</strong>${f.enabled?'':' <span class="muted small">(paused)</span>'}</div>
        <div class="u">${enc(f.url)}</div>
        ${(f.include||f.exclude) ? `<div class="st">only: <code>${enc(f.include||'anything')}</code>`
            + (f.exclude?` · never: <code>${enc(f.exclude)}</code>`:'') + `</div>` : ''}
        <div class="st">${status}</div>
        <div class="tmx-row">
          <button class="btn btn-ghost small" data-a="check">Check now</button>
          <button class="btn btn-ghost small" data-a="edit">Filters</button>
          <button class="btn btn-ghost small" data-a="toggle">${f.enabled?'Pause':'Resume'}</button>
          <button class="btn btn-ghost small" data-a="del" style="color:var(--danger,#e5484d)">Remove</button>
        </div>
        <div class="st" data-role="out"></div></div>`;
    }

    function draw(j){
      const b=body(); if(!b) return;
      const feeds=(j&&j.feeds)||[];
      /* Naming the switch is the whole point of this line. A node that has feeds and is not polling
       * them looks exactly like a node whose feeds match nothing — and the difference is a checkbox
       * in a tab the person reading this may not be able to open. */
      const off = !(j&&j.enabled) ? `<div class="tmx-hint">⚠ Automatic polling is <strong>off</strong> on this
        server, so nothing downloads on a timer. &ldquo;Check now&rdquo; still works. An admin turns it on in
        <strong>Admin &rarr; Network &rarr; Torrent RSS Feeds</strong>.</div>` : '';
      b.innerHTML = off
        + (feeds.length ? feeds.map(feedHtml).join('')
                        : `<div class="empty">No feeds yet.<br><span class="muted small">Paste a torrent RSS
                           URL below &mdash; showRSS, nyaa, a Jackett/torznab endpoint, a tracker’s
                           personal feed.</span></div>`)
        + `<hr><div><strong>Add a feed</strong></div>
           <div class="tmx-hint">Polled every ${enc(j.interval_minutes)} minutes, at most
             ${enc(j.max_per_poll)} new torrent(s) per feed each time. The FIRST check adds nothing:
             it learns what the feed already holds, so subscribing can’t start its whole history.</div>
           <input type="url" id="tmx-nurl" placeholder="https://…/rss">
           <div class="tmx-grid" style="margin-top:7px">
             <input type="text" id="tmx-ntitle" placeholder="Name (optional)">
             <input type="text" id="tmx-ninc" placeholder="Only titles containing… (comma separated)">
           </div>
           <div class="tmx-grid" style="margin-top:7px">
             <input type="text" id="tmx-nexc" placeholder="Never titles containing…">
             <button class="btn btn-neon small" id="tmx-nadd">Subscribe</button>
           </div>
           <div class="tmx-hint">A term wrapped in slashes is a regular expression:
             <code>/S0[12]E\\d\\d/</code>. Leave &ldquo;only&rdquo; empty to take everything the feed lists.</div>`;

      b.querySelectorAll('.tmx-feed').forEach(card=>{
        const id=card.dataset.id, out=card.querySelector('[data-role=out]');
        const f=feeds.find(x=>x.id===id)||{};
        card.querySelectorAll('[data-a]').forEach(btn=> btn.onclick=async()=>{
          const a=btn.dataset.a;
          if(a==='del'){
            if(uiConfirm && !await uiConfirm('Unsubscribe from this feed? Torrents it already added are kept.')) return;
            try{ await api('/feeds/'+encodeURIComponent(id),{method:'DELETE'}); toast('unsubscribed'); load(); }
            catch(err){ toast('could not remove that: '+errText(err)); }
            return;
          }
          if(a==='toggle'){
            try{ await api('/feeds/'+encodeURIComponent(id),{method:'POST',
                   body:JSON.stringify({enabled:!f.enabled})}); load(); }
            catch(err){ toast('could not change that: '+errText(err)); }
            return;
          }
          if(a==='edit'){ editFeed(f, load); return; }
          // Check = a PREVIEW. It never adds and never marks anything seen, so a filter can be
          // tried before it is trusted.
          btn.disabled=true; out.textContent='checking…'; out.classList.remove('err');
          try{
            const r = await api('/feeds/'+encodeURIComponent(id)+'/check',{method:'POST'});
            if(r.error){ out.className='st err'; out.textContent='failed: '+r.error; }
            else{
              out.className='st';
              out.innerHTML = `${r.items} item(s) in the feed, <strong>${r.matched}</strong> match your filters`
                + (r.skipped?` · ${r.skipped} already seen`:'')
                + (r.titles && r.titles.length
                    ? `<br><span class="muted small">${r.titles.slice(0,6).map(enc).join('<br>')}</span>` : '');
            }
          }catch(err){ out.className='st err'; out.textContent='failed: '+errText(err); }
          btn.disabled=false;
        });
      });

      const add=b.querySelector('#tmx-nadd');
      if(add) add.onclick=async()=>{
        const url=(b.querySelector('#tmx-nurl').value||'').trim();
        if(!url){ toast('paste a feed URL'); return; }
        add.disabled=true;
        try{
          await api('/feeds',{method:'POST',body:JSON.stringify({
            url, title:(b.querySelector('#tmx-ntitle').value||'').trim(),
            include:(b.querySelector('#tmx-ninc').value||'').trim(),
            exclude:(b.querySelector('#tmx-nexc').value||'').trim(), enabled:true })});
          toast('subscribed'); load();
        }catch(err){ toast('could not add that feed: '+errText(err)); add.disabled=false; }
      };
    }

    function editFeed(f, done){
      const modal2=PC().modal, close2=PC().closeModal;
      let r2=null;
      modal2(`<h3>Filters — ${enc(f.title||f.url)}</h3>
        <div class="tmx-hint">Matched against the release TITLE. Comma separated; a term in slashes is a
          regular expression. &ldquo;Never&rdquo; wins over &ldquo;only&rdquo;.</div>
        <label class="tmx-hint">Name</label><input type="text" id="tmx-eti" value="${enc(f.title||'')}">
        <label class="tmx-hint">Only titles containing</label><input type="text" id="tmx-einc" value="${enc(f.include||'')}">
        <label class="tmx-hint">Never titles containing</label><input type="text" id="tmx-eexc" value="${enc(f.exclude||'')}">
        <div class="tmx-row"><span style="flex:1"></span>
          <button class="btn btn-ghost small" id="tmx-ecancel">Cancel</button>
          <button class="btn btn-neon small" id="tmx-esave">Save</button></div>`, box=>{ r2=box; });
      if(r2) r2.classList.add('tmx-modal');
      const q=s=>r2&&r2.querySelector(s);
      if(q('#tmx-ecancel')) q('#tmx-ecancel').onclick=()=>{ if(close2) close2(); openFeeds(); };
      if(q('#tmx-esave')) q('#tmx-esave').onclick=async()=>{
        try{
          await api('/feeds/'+encodeURIComponent(f.id),{method:'POST',body:JSON.stringify({
            title:q('#tmx-eti').value, include:q('#tmx-einc').value, exclude:q('#tmx-eexc').value })});
          toast('saved'); if(close2) close2(); openFeeds();
        }catch(err){ toast('could not save that: '+errText(err)); }
      };
    }

    await load();
  }

  // ------------------------------------------------------------------ add dialog
  async function openAdd(){
    style();
    const modal=PC().modal, close=PC().closeModal;
    if(!modal){ toast('this build has no modal host'); return; }
    let root=null;
    modal(`<h3>🧲 Add a torrent</h3>
      <input type="text" id="tmx-aq" placeholder="magnet:?xt=… or a link to a .torrent">
      <label class="tmx-hint" style="display:flex;gap:7px;align-items:center;margin-top:9px">
        <input type="checkbox" id="tmx-apick"> Choose which files to download
      </label>
      <div class="tmx-hint">With a magnet the file list arrives from the swarm a few seconds after
        adding, so the picker opens and waits for it.</div>
      <div class="tmx-row"><span style="flex:1"></span>
        <button class="btn btn-ghost small" id="tmx-acancel">Cancel</button>
        <button class="btn btn-neon small" id="tmx-aadd">Add</button></div>`, box=>{ root=box; });
    if(root) root.classList.add('tmx-modal');
    const q=s=>root&&root.querySelector(s);
    const input=q('#tmx-aq'); if(input) setTimeout(()=>{ try{ input.focus(); }catch(_){} }, 30);
    if(q('#tmx-acancel')) q('#tmx-acancel').onclick=()=>{ if(close) close(); };
    const go=async()=>{
      const v=(q('#tmx-aq').value||'').trim(); if(!v){ toast('paste a magnet or .torrent link'); return; }
      const pick=!!(q('#tmx-apick')&&q('#tmx-apick').checked);
      const btn=q('#tmx-aadd'); if(btn) btn.disabled=true;
      let j=null;
      try{
        // `torrent_url`, not `url` — a .torrent link sent as `url` is silently ignored and the
        // request adds nothing.
        j = await api('/add',{method:'POST',body:JSON.stringify(
          v.toLowerCase().startsWith('magnet:') ? {magnet:v} : {torrent_url:v})});
        toast('added');
      }catch(err){ toast('could not add that: '+errText(err)); if(btn) btn.disabled=false; return; }
      if(close) close();
      refresh();
      if(pick && j && j.info_hash) openFiles(j.info_hash, {waitForMeta:true});
    };
    if(q('#tmx-aadd')) q('#tmx-aadd').onclick=go;
    if(input) input.onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); go(); } };
  }

  function refresh(){
    // Nudge app.js's own poller by clicking the Refresh button it already draws; if it is not
    // there, the 2s poll picks the new torrent up on its own.
    const r=document.getElementById('tm-refresh'); if(r) r.click();
  }

  // ------------------------------------------------------------------ attach
  /* Idempotent. Called from a MutationObserver, so it must be cheap when there is nothing to do and
   * must never mutate anything it has already marked — otherwise it triggers itself for ever. */
  function attach(){
    const tabs = document.querySelector('#feed .tor-tabs');
    if(!tabs) return;
    const downloads = !!document.getElementById('tm-list');

    if(downloads && !tabs.querySelector('#tmx-feeds')){
      const b=document.createElement('button');
      b.className='btn btn-ghost small tor-act'; b.id='tmx-feeds';
      b.textContent='📡 Feeds'; b.title='Subscribe to torrent RSS feeds';
      b.onclick=openFeeds;
      // Into the strip's action GROUP (app.js wraps them in `.tor-acts`), so on a phone the whole
      // group wraps onto its own line as one unit instead of pushing Add torrent off the screen.
      const first=tabs.querySelector('.tor-act');
      if(first) first.parentNode.insertBefore(b, first);
      else (tabs.querySelector('.tor-acts')||tabs).appendChild(b);
    }

    // Richer Add dialog on the button app.js already draws. Re-applied on every repaint because
    // app.js reassigns `.onclick` when it re-renders the view.
    const add=document.getElementById('tm-add');
    if(add && add.dataset.tmx!=='1'){ add.dataset.tmx='1'; add.onclick=openAdd; }

    if(!downloads) return;
    document.querySelectorAll('#tm-list .tm-item').forEach(row=>{
      const acts=row.querySelector('.tm-acts');
      if(!acts || acts.querySelector('.tmx-fbtn')) return;
      const hash=row.dataset.h; if(!hash) return;
      const b=document.createElement('button');
      b.className='btn btn-ghost small tmx-fbtn'; b.textContent='🗂 Files';
      b.title='Choose which files to download';
      b.onclick=()=>openFiles(hash, {});
      acts.insertBefore(b, acts.firstChild);
    });
  }

  let _pending=false;
  function schedule(){
    if(_pending) return; _pending=true;
    requestAnimationFrame(()=>{ _pending=false; try{ attach(); }catch(e){ console.warn('[torrents]', e); } });
  }

  function boot(){
    const feed=document.getElementById('feed');
    if(!feed){ setTimeout(boot, 500); return; }
    try{ new MutationObserver(schedule).observe(feed, {childList:true, subtree:true}); }
    catch(e){ console.warn('[torrents] observer failed', e); }
    schedule();
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.PCTorrents = { openFeeds, openFiles, openAdd, attach, fmtBytes };
})();

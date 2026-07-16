/* #news — the built-in RSS News reader for the Nostr client. Kept OUT of app.js (own file, like the
 * games) to avoid bloating the core. Uses app.js's shared surface window.__PC + window.Relay/Store.
 * Feeds are fetched SERVER-SIDE (/api/rss — proxy→direct, shared cache, structured JSON). Your feed
 * list AND read state persist as your own kind-30078 Nostr events (d=pcai:news-feeds / pcai:news-read),
 * cached in localStorage for instant load and hydrated from the relay — so they sync across devices.
 * app.js dispatches the view via window.PCNews.render(). */
(function(){
  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, publish, toast, LOGO, compose, authFetch, ensureAiSession } = PC;
    const Relay = window.Relay;
    // Feed content is untrusted — only allow http(s) URLs as href/src (blocks javascript:/data: scheme XSS).
    const safeUrl = u => /^https?:\/\//i.test(u||'') ? u : '';

    const FEEDS_D = 'pcai:news-feeds', READ_D = 'pcai:news-read', READ_CAP = 2000, MAX_ITEMS = 2000;
    const DEFAULTS = [
      { url:'https://feeds.bbci.co.uk/news/rss.xml', name:'BBC News' },
      { url:'https://hnrss.org/frontpage', name:'Hacker News' },
      { url:'https://www.theverge.com/rss/index.xml', name:'The Verge' },
      { url:'https://feeds.arstechnica.com/arstechnica/index', name:'Ars Technica' },
    ];

    let _feeds = null;        // [{url,name}]
    let _active = 'all';      // 'all' or a feed url
    let _items = [];          // fetched, merged, sorted
    let _obs = null;          // IntersectionObserver for mark-read-on-scroll
    let _loading = false;
    let _gen = 0;             // render token — a newer render/feed-switch supersedes an in-flight older one
    let _loaded = false;      // true once News has fetched items this session (guards the global-unread recompute)
    const inView = () => window.__PC.VIEW === 'news';

    // ---- read state: a capped, insertion-ordered set of item ids (bounded so the event can't grow) ----
    let _readIds = [], _readSet = new Set(), _readSaveT = null;
    function _loadReadLocal(){ try{ _readIds = JSON.parse(localStorage.getItem('pc_news_read')||'[]')||[]; }catch(_){ _readIds=[]; } _readSet = new Set(_readIds); }
    function isRead(id){ return _readSet.has(id); }
    // Unread badge = articles NEWER than the last time you opened News (the standard RSS "new since last
    // visit" model — NOT "every item you haven't scrolled past", which on a 2000-item feed just sticks at
    // 99+). Computed only from the 'all' view. The first visit sets the baseline (nothing pre-existing is
    // "new" → 0), and opening News advances the baseline so the badge clears.
    function _seenTs(){ try{ return +(localStorage.getItem('pc_news_seen_ts') || 0) || 0; }catch(_){ return 0; } }
    function updateBadge(){
      _paintBadge(_persistedBadge());                    // instant: show the last-known count
      if(!_loaded || _active !== 'all') return;          // only the 'all' view sees the whole feed
      const seen = _seenTs();
      let newest = 0, cnt = 0;
      for(const it of _items){ const t = it.ts || 0; if(t > newest) newest = t; if(seen && t > seen) cnt++; }
      try{ localStorage.setItem('pc_news_unread', String(cnt)); localStorage.setItem('pc_news_seen_ts', String(newest)); }catch(_){}
      _paintBadge(cnt);
    }
    function _persistedBadge(){ try{ return +(localStorage.getItem('pc_news_unread') || 0) || 0; }catch(_){ return 0; } }
    function _paintBadge(n){
      const txt = n > 99 ? '99+' : String(n);
      document.querySelectorAll('.news-badge').forEach(b=>{ b.textContent = n ? txt : ''; b.style.display = n ? '' : 'none'; });
    }
    function markAllRead(){
      for(const it of _items){ markRead(it.id); }        // grey the visible cards
      let newest = 0; for(const it of _items){ if((it.ts || 0) > newest) newest = it.ts || 0; }
      try{ localStorage.setItem('pc_news_seen_ts', String(newest)); localStorage.setItem('pc_news_unread', '0'); }catch(_){}
      _paintBadge(0);
      renderList();
      try{ toast('marked all read'); }catch(_){}
    }
    function markRead(id){
      if(!id || _readSet.has(id)) return;
      _readSet.add(id); _readIds.push(id);
      while(_readIds.length > READ_CAP){ _readSet.delete(_readIds.shift()); }
      try{ localStorage.setItem('pc_news_read', JSON.stringify(_readIds)); }catch(_){}
      clearTimeout(_readSaveT);
      _readSaveT = setTimeout(()=>{ try{ publish(30078, JSON.stringify(_readIds), [['d', READ_D]]); }catch(_){} }, 5000);
    }

    // ---- feed list: local cache first, then hydrate from the relay ----
    function _feedsLocal(){ try{ const c=JSON.parse(localStorage.getItem('pc_news_feeds')||'null'); if(Array.isArray(c)&&c.length) return c; }catch(_){} return null; }
    async function loadState(){
      _loadReadLocal();
      _feeds = _feedsLocal() || DEFAULTS.slice();
      // hydrate both lists from Nostr (may override local)
      try{
        const evs = await Relay.query([{ authors:[window.__PC.ME.pubkey], kinds:[30078], '#d':[FEEDS_D, READ_D] }]);
        for(const e of (evs||[])){
          const d = (e.tags.find(t=>t[0]==='d')||[])[1];
          if(d===FEEDS_D){ try{ const c=JSON.parse(e.content||'[]'); if(Array.isArray(c)&&c.length) _feeds=c; }catch(_){} }
          else if(d===READ_D){ try{ const c=JSON.parse(e.content||'[]'); if(Array.isArray(c)){ for(const id of c){ if(!_readSet.has(id)){ _readSet.add(id); _readIds.push(id);} } while(_readIds.length>READ_CAP){ _readSet.delete(_readIds.shift()); } } }catch(_){} }
        }
      }catch(_){}
      try{ localStorage.setItem('pc_news_feeds', JSON.stringify(_feeds)); }catch(_){}
    }
    async function saveFeeds(){ try{ localStorage.setItem('pc_news_feeds', JSON.stringify(_feeds)); }catch(_){} try{ await publish(30078, JSON.stringify(_feeds), [['d', FEEDS_D]]); }catch(_){} }

    // ---- OPML import/export (Miniflux + any reader export/import via the universal OPML format) ----
    function parseOpml(text){
      const out=[];
      try{
        const doc=new DOMParser().parseFromString(text, 'application/xml');
        doc.querySelectorAll('outline[xmlUrl]').forEach(o=>{
          const url=(o.getAttribute('xmlUrl')||'').trim();
          const name=(o.getAttribute('title')||o.getAttribute('text')||'').trim() || (url.split('/')[2]||url);
          if(/^https?:\/\//i.test(url)) out.push({ url, name });
        });
      }catch(_){}
      return out;
    }
    function buildOpml(){
      const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
      const rows=_feeds.map(f=>`    <outline type="rss" text="${esc(f.name)}" title="${esc(f.name)}" xmlUrl="${esc(f.url)}"/>`).join('\n');
      return `<?xml version="1.0" encoding="UTF-8"?>\n<opml version="1.0">\n  <head><title>PosterChan News feeds</title></head>\n  <body>\n${rows}\n  </body>\n</opml>\n`;
    }
    async function importOpml(text){
      const found=parseOpml(text); if(!found.length){ toast('no feeds found in that OPML'); return; }
      const have=new Set(_feeds.map(f=>f.url)); let added=0;
      for(const f of found){ if(!have.has(f.url)){ _feeds.push(f); have.add(f.url); added++; } }
      if(added){ await saveFeeds(); toast('imported '+added+' feed'+(added>1?'s':'')); closeNews(); renderNews(); }
      else toast('all already added');
    }
    function exportOpml(){
      const blob=new Blob([buildOpml()], { type:'text/x-opml' });
      const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='posterchan-feeds.opml';
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(a.href), 2000);
    }

    // ---- fetch ----
    // Returns the fetched items (does NOT assign _items) — renderNews assigns only after its generation
    // check, so a slow superseded fetch can't clobber a newer feed's _items (share/summarize/mark-read).
    async function fetchActive(){
      _loading = true;
      try{
        let items = [];
        if(_active==='all'){
          const all=_feeds.map(f=>f.url);
          if(all.length){
            // CHUNK the feed list (a big imported OPML would blow the server's URL-length limit / 30-feed cap).
            const chunks=[]; for(let i=0;i<all.length;i+=12) chunks.push(all.slice(i,i+12));
            const nameOf = u => (_feeds.find(f=>f.url===u)||{}).name || (u.split('/')[2]||u);
            const results = await Promise.all(chunks.map(c=>
              fetch('/api/rss/feeds?urls='+encodeURIComponent(c.join(','))).then(r=>r.json()).catch(()=>({feeds:[]}))));
            for(const r of results) for(const fd of (r.feeds||[])) for(const it of (fd.items||[])) items.push({ ...it, feed:fd.url, feedName: nameOf(fd.url) });
          }
        } else {
          const fd = _feeds.find(f=>f.url===_active) || { url:_active, name:_active };
          const r = await fetch('/api/rss/feed?url='+encodeURIComponent(_active)).then(r=>r.json()).catch(()=>({items:[]}));
          items = (r.items||[]).map(it=>({ ...it, feed:fd.url, feedName: fd.name }));
        }
        items.sort((a,b)=> (b.ts||0)-(a.ts||0));
        if(items.length > MAX_ITEMS) items = items.slice(0, MAX_ITEMS);   // retention: keep newest 2000, drop older
        return items;
      } finally { _loading = false; }
    }

    // ---- rendering ----
    function _ago(ts){ if(!ts) return ''; const s=Math.max(1, Math.floor(Date.now()/1000)-ts);
      if(s<3600) return Math.floor(s/60)+'m'; if(s<86400) return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'d'; }
    function _card(it, i){
      const read = isRead(it.id);
      const img=safeUrl(it.image), href=safeUrl(it.link);
      return `<div class="news-card${read?' read':''}" data-i="${i}" data-id="${enc(it.id)}">
        ${img?`<img class="news-thumb" src="${enc(img)}" loading="lazy" onerror="this.remove()">`:''}
        <div class="news-body">
          <a class="news-title" href="${enc(href||'#')}" target="_blank" rel="noopener">${enc(it.title)}</a>
          <div class="news-meta">${enc(it.feedName||'')}${it.ts?' · '+_ago(it.ts)+' ago':''}</div>
          ${it.snippet?`<div class="news-snip">${enc(it.snippet)}</div>`:''}
          <div class="news-acts">
            <button class="btn btn-ghost small news-share" data-i="${i}">↗ Share</button>
            ${window.PC_NOSTR_ONLY ? '' : `<button class="btn btn-ghost small news-sum" data-i="${i}">✨ Summarize</button>`}
          </div>
        </div></div>`;
    }
    function _chips(){
      const chip = (v,l)=>`<button class="news-chip${_active===v?' on':''}" data-feed="${enc(v)}">${enc(l)}</button>`;
      return `<div class="news-chips">${chip('all','All')}${_feeds.map(f=>chip(f.url, f.name)).join('')}
        <button class="news-chip news-readall" title="Mark all read">✓✓</button>
        <button class="news-chip news-add" title="Manage feeds">＋</button></div>`;
    }
    function renderList(){
      const list = $('#news-list'); if(!list) return;
      if(_loading && !_items.length){ list.innerHTML='<div class="spinner"></div>'; return; }
      list.innerHTML = _items.length ? _items.map(_card).join('')
        : '<div class="empty">No articles — add a feed with ＋ or try again.</div>';
      // wire actions
      $$('.news-share', list).forEach(b=> b.onclick=(e)=>{ e.stopPropagation(); const it=_items[+b.dataset.i]; if(it) compose({ text: it.title + '\n\n' + it.link }); });
      $$('.news-sum', list).forEach(b=> b.onclick=(e)=>{ e.stopPropagation(); summarize(_items[+b.dataset.i], b); });
      // mark-read once a card has scrolled ABOVE the top of the SCROLL CONTAINER (you read PAST it) — NOT
      // merely on screen at first render (that would grey the newest headlines). root MUST be #feed (the
      // overflow-y:auto scroller): with the default viewport root a card scrolled out #feed's top is still
      // in the viewport behind the top bar, so its rect.bottom never reaches 0 and nothing marks read.
      if(_obs) _obs.disconnect();
      // #feed is the scroll container normally; in embed mode CSS forces it overflow:visible and the WINDOW
      // scrolls instead — use the viewport root (null) there so mark-read still fires.
      const scroller = document.body.classList.contains('embed') ? null : ($('#feed') || null);
      _obs = new IntersectionObserver(ents=>{ for(const en of ents){
        const top = en.rootBounds ? en.rootBounds.top : 0;
        if(!en.isIntersecting && en.boundingClientRect.bottom <= top){
          const el=en.target; const it=_items[+el.dataset.i];
          if(it && !isRead(it.id)){ markRead(it.id); el.classList.add('read'); }   // grey card; badge is time-based, not read-based
          _obs.unobserve(el);
        }
      } }, { root: scroller, threshold:0 });
      $$('.news-card', list).forEach(el=> _obs.observe(el));
    }
    async function renderNews(){
      const gen = ++_gen;                       // supersede any older in-flight render
      const feed = $('#feed');
      feed.innerHTML = `<div class="news-wrap"><div id="news-head"></div><div id="news-list"><div class="spinner"></div></div></div>`;
      if(!_feeds) await loadState();
      if(gen!==_gen || !inView()) return;       // navigated away / superseded during loadState
      const head = $('#news-head'); if(!head) return;
      head.innerHTML = _chips();
      $$('.news-chip', feed).forEach(b=> b.onclick=()=>{ if(b.classList.contains('news-add')){ manageFeeds(); return; } if(b.classList.contains('news-readall')){ markAllRead(); return; } _active=b.dataset.feed; renderNews(); });
      const items = await fetchActive();
      if(gen!==_gen || !inView()) return;       // navigated away / a newer feed-switch won the race
      _items = items;                           // assign ONLY as the winning render (stale fetch is discarded)
      _loaded = true;
      renderList();
      updateBadge();
    }

    // ---- summarize (server LLM) ----
    async function summarize(it, btn){
      if(!it) return; const orig = btn.textContent; btn.disabled=true; btn.textContent='…';
      try{
        try{ if(ensureAiSession) await ensureAiSession(); }catch(_){}   // populate the bearer (APK needs it for authed endpoints)
        const r = await authFetch('/api/news/summarize?url='+encodeURIComponent(it.link)).then(r=>r.json());
        const txt = (r && (r.summary || r.text)) || 'No summary available.';
        modalNews(`<h3>✨ ${enc(it.title)}</h3><div class="news-summary">${enc(txt).replace(/\n/g,'<br>')}</div>
          <div class="row" style="margin-top:12px"><button class="btn btn-cyan" id="news-sum-share">↗ Share summary</button>
          <a class="btn btn-ghost" href="${enc(safeUrl(it.link)||'#')}" target="_blank" rel="noopener">Open article</a></div>`, root=>{
          const s=$('#news-sum-share',root); if(s) s.onclick=()=>compose({ text: txt.trim()+'\n\n'+it.link });
        });
      }catch(e){ toast('summarize failed'); }
      finally{ btn.disabled=false; btn.textContent=orig; }
    }

    // ---- manage feeds (add / remove) ----
    function manageFeeds(){
      const rows = _feeds.map((f,i)=>`<div class="news-mrow"><span>${enc(f.name)}</span><button class="btn btn-ghost small news-del" data-i="${i}">Remove</button></div>`).join('') || '<div class="muted small">No feeds yet.</div>';
      modalNews(`<h3>📰 Manage feeds</h3><div class="news-mlist">${rows}</div>
        <label class="fld">Add a feed (RSS/Atom URL)<input class="input" id="news-newurl" placeholder="https://example.com/rss"></label>
        <label class="fld">Name (optional)<input class="input" id="news-newname" placeholder="Example"></label>
        <div class="row" style="margin-top:10px"><button class="btn btn-cyan" id="news-addbtn">Add feed</button></div>
        <div class="news-msep">Import / export (OPML — works with Miniflux &amp; any reader)</div>
        <div class="row" style="gap:8px;flex-wrap:wrap">
          <button class="btn btn-ghost small" id="news-import">⬆ Import OPML file</button>
          <button class="btn btn-ghost small" id="news-export">⬇ Export OPML</button>
          <input type="file" id="news-opml-file" accept=".opml,.xml,text/xml,application/xml" style="display:none">
        </div>
        <details style="margin-top:8px"><summary class="muted small">…or paste OPML</summary>
          <textarea class="input" id="news-opml-text" rows="4" placeholder="&lt;opml&gt;…&lt;/opml&gt;" style="margin-top:6px"></textarea>
          <button class="btn btn-ghost small" id="news-import-text" style="margin-top:6px">Import pasted OPML</button>
        </details>`, root=>{
        $$('.news-del',root).forEach(b=> b.onclick=async()=>{ const i=+b.dataset.i; const rm=_feeds[i]; _feeds.splice(i,1); if(rm && _active===rm.url) _active='all'; await saveFeeds(); renderNews(); closeNews(); manageFeeds(); });
        $('#news-addbtn',root).onclick=async()=>{
          let url=($('#news-newurl',root).value||'').trim(); if(!url) return;
          if(!/^https?:\/\//i.test(url)) url='https://'+url;
          const name=($('#news-newname',root).value||'').trim() || (url.split('/')[2]||url);
          if(_feeds.some(f=>f.url===url)){ toast('already added'); return; }
          _feeds.push({ url, name }); await saveFeeds(); closeNews(); toast('feed added'); renderNews();
        };
        $('#news-export',root).onclick=exportOpml;
        $('#news-import',root).onclick=()=>$('#news-opml-file',root).click();
        $('#news-opml-file',root).onchange=e=>{ const f=e.target.files&&e.target.files[0]; if(!f) return; const r=new FileReader(); r.onload=()=>importOpml(String(r.result||'')); r.readAsText(f); };
        $('#news-import-text',root).onclick=()=>importOpml(($('#news-opml-text',root).value||''));
      });
    }

    // tiny modal (self-contained — doesn't depend on app.js's modal internals)
    let _nMod=null;
    function modalNews(html, onOpen){ closeNews();
      _nMod=document.createElement('div'); _nMod.className='news-modal-bg';
      _nMod.innerHTML=`<div class="news-modal">${html}<button class="news-modal-x">✕</button></div>`;
      _nMod.onclick=e=>{ if(e.target===_nMod) closeNews(); };
      document.body.appendChild(_nMod);
      _nMod.querySelector('.news-modal-x').onclick=closeNews;
      if(onOpen) try{ onOpen(_nMod); }catch(_){}
    }
    function closeNews(){ if(_nMod){ _nMod.remove(); _nMod=null; } }

    window.PCNews = { render: renderNews, updateBadge };
    // One-time migration: the old badge counted scroll-unread (stuck at 99+). If there's no "seen" baseline
    // yet, clear that stale count so the new "new since last visit" model starts clean.
    try{
      if(!localStorage.getItem('pc_news_seen_ts')) localStorage.setItem('pc_news_unread', '0');
      const n = _persistedBadge(); if(n > 0) _paintBadge(n);   // paint last known count before News is opened
    }catch(_){}
  }
  init();
})();

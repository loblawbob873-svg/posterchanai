/* #websearch — Web Search: this node's SearXNG instance, with a front end of our own.
 *
 * Kept OUT of app.js (own file, like News and Notes) and driven from app.js's renderView via
 * window.PCWebSearch.render(). Everything server-side lives behind /api/websearch/* — the SearXNG
 * proxy, the reader, and the two LLM calls.
 *
 * Four things this has that a plain SearXNG page does not:
 *   1. Save to Notes   — a result, a page, a summary or the overview, into the encrypted notebook.
 *   2. Share           — straight into the composer, so a find becomes a post.
 *   3. Summarize a link — one result's page, read and summarized on the node.
 *   4. AI overview     — the Google/Bing answer-box, over the top results, WITH citations.
 *
 * Two things about the shape of this screen are deliberate:
 *
 *   The whole search lives in MODULE state, not in the DOM. #feed is one element every view shares
 *   and app.js blanks it on entry, so a view that keeps its results in the page loses them the
 *   moment you glance at Messages. Leaving and coming back repaints query, filters, results,
 *   overview, summaries, the open article and both scroll positions from `S` — no refetch, no
 *   "where was I".
 *
 *   A result opens IN the app (the reader), not in a browser tab. A tab is a one-way door on a
 *   phone: coming back is the OS's business, and in a PWA/APK it often means a cold restart of this
 *   page — i.e. the results are gone. The reader is a sub-screen with a Back that returns to the
 *   exact scroll offset. The original is always one tap away, for the pages the reader can't parse.
 */
(function(){
  const TIME_LABELS = [['','Any time'],['day','Past day'],['week','Past week'],['month','Past month'],['year','Past year']];
  const CATS = [['general','Web'],['news','News'],['images','Images'],['videos','Videos'],
                ['science','Science'],['it','Tech'],['files','Files']];

  // ---- the state that outlives the view ----------------------------------------------------
  const S = {
    q:'', category:'general', time:'',              // the controls
    key:'',                                          // query the results below belong to
    results:[], answers:[], suggestions:[], error:null,
    page:1, more:true, loading:false, gen:0,
    overview:null,                                   // {overview, sources} — for `key`
    ovLoading:false, ovError:'',
    summaries:{},                                    // url → summary text (cheap re-open)
    scroll:0,                                        // results scroll offset
    reader:null,                                     // {url,title,content,error,loading,scroll}
  };

  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, $$, enc, toast, compose, authFetch, ensureAiSession, modal, closeModal } = PC;

    const inView = () => window.__PC.VIEW === 'websearch';
    /* Should this screen offer the AI buttons at all?
     *
     * Not just PC_NOSTR_ONLY: the server gates /summarize and /overview on the same `can_ai` flag as
     * chat, so a user without it was being shown a ✨ button on every result that could only ever
     * answer "AI access not enabled". The AI view itself checks the flag and shows a request-access
     * screen instead; this is the one place that advertised AI to people who cannot use it.
     *
     * `null` = not asked yet, and that reads as ON: the session probe is one request and a first
     * paint should not wait on it. When the answer comes back negative the buttons disappear on the
     * next paint (renderAiState below repaints once, if it changed anything). */
    let _canAi = null;
    const aiOff  = () => !!window.PC_NOSTR_ONLY || _canAi === false;
    async function refreshAiState(){
      try{
        const a = await ensureAiSession();
        const can = !!(a && (a.can_ai || a.is_admin));
        if(_canAi === can) return;
        // Don't flip on a transient failure — `error` means the login didn't complete, not "denied".
        if(!can && a && a.error) return;
        _canAi = can;
        if(inView()) paint();
      }catch(_){}
    }
    const safeUrl = u => /^https?:\/\//i.test(u||'') ? u : '';
    const host = u => { try{ return new URL(u).hostname.replace(/^www\./,''); }catch(_){ return ''; } };
    // #feed is the scroll container, except in embed mode where CSS hands scrolling to the window.
    const scroller = () => document.body.classList.contains('embed')
      ? (document.scrollingElement || document.documentElement) : $('#feed');

    // ---- server calls ------------------------------------------------------------------------
    // Every one of these is authed. ensureAiSession() populates the bearer token the APK needs (the
    // web page has a cookie either way) — cached after the first call, so this is free thereafter.
    async function api(path, opts){
      try{ await ensureAiSession(); }catch(_){}
      const r = await authFetch(path, opts);
      let body = null;
      try{ body = await r.json(); }catch(_){}
      if(!r.ok){
        const detail = (body && (body.detail || body.error)) || '';
        throw new Error(detail || ('HTTP ' + r.status));
      }
      return body || {};
    }
    const jsonPost = (path, obj) => api(path, { method:'POST', headers:{'Content-Type':'application/json'},
                                                body: JSON.stringify(obj) });

    // ---- search ------------------------------------------------------------------------------
    async function runSearch(append){
      const q = (S.q||'').trim();
      if(!q) return;
      const gen = ++S.gen;
      S.loading = true;
      if(!append){
        S.page = 1; S.results = []; S.answers = []; S.suggestions = []; S.error = null;
        S.more = true; S.scroll = 0;
        // A new query invalidates the old overview — showing yesterday's answer above today's
        // results is worse than showing none.
        if(S.key !== queryKey()){ S.overview = null; S.ovError = ''; }
        S.key = queryKey();
      }
      paint();
      try{
        const p = new URLSearchParams({ q, category:S.category, time_range:S.time, page:String(S.page) });
        const r = await api('/api/websearch/search?' + p.toString());
        if(gen !== S.gen) return;                       // a newer search won
        const got = r.results || [];
        S.results = append ? S.results.concat(got) : got;
        S.answers = r.answers || [];
        S.suggestions = r.suggestions || [];
        S.error = r.error || null;
        S.more = got.length > 0;                        // an empty page is the end of the road
      }catch(e){
        if(gen !== S.gen) return;
        S.error = (e && e.message) || 'search failed';
        S.more = false;
      }finally{
        if(gen === S.gen){ S.loading = false; paint(); }
      }
    }
    const queryKey = () => [S.q.trim(), S.category, S.time].join('|');

    function submit(q){
      const el = $('#ws-q');
      S.q = (q != null ? q : (el ? el.value : S.q)) || '';
      if(el && q != null) el.value = S.q;
      if(!S.q.trim()){ toast('type something to search'); return; }
      // Hand the keyboard BACK. Every key handler in the app bails out while focus is in an <input>
      // (rightly — you are typing), so leaving the caret in the search box after a search means j/k,
      // Enter and the card letters all type into it instead of walking the results. It also drops the
      // on-screen keyboard on a phone, which was covering the first two results.
      try{ if(el) el.blur(); }catch(_){}
      S.reader = null;
      runSearch(false);
    }

    // ---- the AI overview ---------------------------------------------------------------------
    async function loadOverview(){
      if(S.ovLoading || !S.results.length) return;
      const key = queryKey();          // what this overview will be ABOUT
      S.ovLoading = true; S.ovError = '';
      paintOverview();
      try{
        const r = await jsonPost('/api/websearch/overview',
                                 { q:S.q.trim(), category:S.category, time_range:S.time });
        // Searched something else while it was thinking? Drop it. An overview shown under results it
        // did not read is worse than no overview — it looks like an answer to the new query.
        if(queryKey() !== key) return;
        S.overview = r; S.key = key;
      }catch(e){
        S.ovError = (e && e.message) || 'could not summarize these results';
      }finally{
        S.ovLoading = false; paintOverview();
      }
    }

    // ---- summarize one link ------------------------------------------------------------------
    async function summarize(url, title, btn){
      const cached = S.summaries[url];
      if(cached) return showSummary(url, title, cached);
      if(btn){ btn.disabled = true; btn.dataset.lbl = btn.textContent; btn.textContent = '…'; }
      try{
        const r = await jsonPost('/api/websearch/summarize', { url });
        S.summaries[url] = r.summary || '';
        showSummary(url, r.title || title, S.summaries[url]);
      }catch(e){
        toast('summarize failed: ' + ((e && e.message) || 'error'));
      }finally{
        if(btn){ btn.disabled = false; btn.textContent = btn.dataset.lbl || '✨ Summarize'; }
      }
    }
    function showSummary(url, title, text){
      modal(`<h3>✨ ${enc(title || host(url))}</h3>
        <div class="ws-sumtext">${enc(text).replace(/\n/g,'<br>')}</div>
        <div class="ws-modacts">
          <button class="btn btn-cyan small" id="ws-sum-share">↗ Share</button>
          <button class="btn btn-ghost small" id="ws-sum-note">📓 Save to Notes</button>
          <a class="btn btn-ghost small" href="${enc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">↗ Open original</a>
        </div>`, root=>{
        $('#ws-sum-share', root).onclick = ()=>{ closeModal(); compose({ text: text.trim() + '\n\n' + url }); };
        $('#ws-sum-note', root).onclick = (e)=> saveNote(e.currentTarget, {
          title: 'Summary — ' + (title || host(url)), body: text.trim() + '\n\n' + url, tags:['web-search'] });
      });
    }

    // ---- Save to Notes -----------------------------------------------------------------------
    // The notebook is encrypted to the user's own key, so this is the one "keep it" that survives a
    // link rotting: the TEXT is saved, not a pointer to it.
    async function saveNote(btn, note){
      if(!window.PCNotes || !window.PCNotes.save){ toast('Notes is not loaded'); return; }
      const lbl = btn ? btn.textContent : '';
      if(btn){ btn.disabled = true; btn.textContent = 'saving…'; }
      try{
        const r = await window.PCNotes.save(note);
        toast(r.queued ? '📓 saved to Notes — will sync when you are back online' : '📓 saved to Notes');
        if(btn){ btn.textContent = '✓ in Notes'; btn.disabled = false; }
      }catch(e){
        toast('could not save to Notes: ' + ((e && e.message) || 'error'));
        if(btn){ btn.disabled = false; btn.textContent = lbl || '📓 Save to Notes'; }
      }
    }

    // ---- the reader --------------------------------------------------------------------------
    // Opening a result shows the PAGE. The extracted text is fetched only if the reader is actually
    // switched to Reader (or Summarize asks for it) — in page mode that request buys nothing, and it
    // is a second full fetch of the same URL through this node.
    async function openReader(r){
      S.scroll = scrollTop();                              // remember where the results were
      S.reader = { url:r.url, title:r.title, mode:'page', content:'', error:'', loading:false, scroll:0 };
      // In the bundled apps the frame needs its ticket BEFORE it is painted, or the first load 401s
      // and the user sees an empty page they then have to back out of. One request per 15 minutes.
      if(bundled()){
        try{ await ensureTicket(); }catch(_){}
        if(!S.reader || S.reader.url !== r.url) return;    // they moved on while we asked
      }
      paint();
    }
    async function loadText(){
      const r = S.reader;
      if(!r || r.content || r.loading) return;
      r.loading = true; paint();
      try{
        const out = await api('/api/websearch/read?url=' + encodeURIComponent(r.url));
        if(!S.reader || S.reader.url !== r.url) return;     // they backed out / opened another
        S.reader.content = out.content || '';
        S.reader.error = out.error || (out.content ? '' : 'nothing readable on that page');
      }catch(e){
        if(S.reader && S.reader.url === r.url) S.reader.error = (e && e.message) || 'could not read that page';
      }finally{
        if(S.reader && S.reader.url === r.url){ S.reader.loading = false; paint(); }
      }
    }
    function toggleMode(){
      const r = S.reader; if(!r) return;
      r.mode = (r.mode === 'text') ? 'page' : 'text';
      r.scroll = 0;                       // the two modes are different documents; don't cross the offsets
      paint();
      if(r.mode === 'text') loadText();
    }
    /* An image result, in the app's own lightbox — with the whole page of results as its pager, so
     * ←/→ walk the search the way they walk a post's gallery. */
    function openImage(i){
      const r = S.results[i]; if(!r) return;
      const shots = S.results
        .map(x => ({ src: safeUrl(x.img_src || x.thumbnail), title: x.title, url: x.url }))
        .filter(x => x.src);
      const at = Math.max(0, shots.findIndex(x => x.url === r.url));
      if(PC.openLightbox){
        PC.openLightbox(shots[at] ? shots[at].src : safeUrl(r.img_src || r.thumbnail), null,
                        { items: shots.map(x => ({ src:x.src, kind:null })), i: at });
        return;
      }
      // No lightbox on this build (an older bundled client): the page it came from is the next best
      // thing, and it is still IN the app.
      openReader(r);
    }
    function closeReader(){
      if(!S.reader) return false;
      S.reader = null;
      paint();
      return true;
    }

    // ---- scroll position ---------------------------------------------------------------------
    const scrollTop = () => { const s = scroller(); return s ? s.scrollTop : 0; };
    // Replacing #feed's contents resets its scrollTop to 0, and THAT fires a scroll event — which,
    // recorded, would overwrite the very offset the repaint is about to restore. So the recorder is
    // deaf while a paint is in flight. (Defensive: scroll events are dispatched asynchronously, so
    // whether one lands before or after the restoring frame is not ours to decide.)
    let _painting = false;
    function restoreScroll(px){
      const s = scroller();
      if(!s){ _painting = false; return; }
      // After the paint, not during it: the list has to exist before it can be scrolled.
      requestAnimationFrame(()=>{
        try{ s.scrollTop = px || 0; }catch(_){}
        // …and one frame later again, because the browser can still be settling the new layout.
        requestAnimationFrame(()=>{ try{ if(px) s.scrollTop = px; }catch(_){} _painting = false; });
      });
    }
    /* Escape closes the open page, exactly as the Android back button does.
     *
     * The card cursor (j/k, S/N/U, Enter) is app.js's — `.ws-card` is registered in its row list and
     * its per-card key table, so these results behave like every other card list rather than growing
     * a second keyboard model. What app.js has no way to know about is the reader, which is a
     * sub-screen inside the view: without this, Esc on an open page does nothing and the only way
     * back is the mouse.
     */
    document.addEventListener('keydown', e => {
      if(e.key !== 'Escape' || !inView() || !S.reader) return;
      if(e.defaultPrevented) return;                       // something above already claimed it
      if(document.body.classList.contains('modal-open')) return;
      const t = e.target;
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || ''))) return;
      e.preventDefault();
      closeReader();
    });
    // Remember where you are, live — the results' offset and the reader's own are kept separately,
    // so opening an article and coming back returns to the result you opened it from.
    (function watchScroll(){
      const s0 = $('#feed');
      const on = () => { if(!inView() || _painting) return;
                         if(S.reader) S.reader.scroll = scrollTop(); else S.scroll = scrollTop(); };
      if(s0) s0.addEventListener('scroll', on, { passive:true });
      window.addEventListener('scroll', on, { passive:true });   // embed mode
    })();

    // ---- rendering ---------------------------------------------------------------------------
    function head(){
      const cats = CATS.map(([v,l]) =>
        `<button type="button" class="ws-chip${S.category===v?' on':''}" data-cat="${v}">${enc(l)}</button>`).join('');
      const times = TIME_LABELS.map(([v,l]) =>
        `<option value="${v}"${S.time===v?' selected':''}>${enc(l)}</option>`).join('');
      return `<div class="ws-bar">
        <form class="ws-form" id="ws-form" autocomplete="off">
          <svg class="ic ws-ic" aria-hidden="true"><use href="#i-search"></use></svg>
          <input class="input ws-input" id="ws-q" type="search" enterkeyhint="search"
                 placeholder="Search the web" value="${enc(S.q)}">
          <button type="submit" class="btn btn-cyan small ws-go">Search</button>
        </form>
        <div class="ws-filters">
          <div class="ws-chips">${cats}</div>
          <select class="input ws-time" id="ws-time" aria-label="Time range">${times}</select>
        </div>
      </div>`;
    }

    function resultCard(r, i){
      const url = safeUrl(r.url);
      const thumb = safeUrl(r.thumbnail);
      // No "Read here" button: the title — and the card — already open the page, and a fourth action
      // per result wrapped onto a second row on a phone, which is most of what made a page of results
      // read as a wall of buttons.
      const acts = `<div class="ws-acts">
          <button class="btn btn-ghost small ws-share" data-i="${i}">↗ Share</button>
          <button class="btn btn-ghost small ws-note" data-i="${i}">📓 Notes</button>
          ${aiOff() ? '' : `<button class="btn btn-ghost small ws-sum" data-i="${i}">✨ Summarize</button>`}
        </div>`;
      return `<article class="ws-card" data-i="${i}" data-id="${enc(url)}">
        ${thumb ? `<img class="ws-thumb" src="${enc(thumb)}" alt="" loading="lazy" onerror="this.remove()">` : ''}
        <div class="ws-body">
          <div class="ws-src">${enc(host(url) || r.engine || '')}${r.published ? ' · ' + enc(String(r.published).slice(0,10)) : ''}</div>
          <a class="ws-title" href="${enc(url||'#')}" target="_blank" rel="noopener noreferrer" data-i="${i}">${enc(r.title||url)}</a>
          ${r.content ? `<div class="ws-snip">${enc(r.content)}</div>` : ''}
          ${acts}
        </div></article>`;
    }

    /* An image result opens IN the app too — the app's own lightbox (pager, zoom, swipe, Esc), the
     * same one every other picture here opens in, rather than a browser tab. The tile stays a real
     * <a> to the source page so ctrl/⌘/middle-click still works and the status bar shows where it
     * goes; the plain click is intercepted.
     *
     * ⧉ opens the PAGE the image is on, in the frame — the two are different things you might want,
     * and a picture with no way back to its context is half a search result. */
    function imageCard(r, i){
      const src = safeUrl(r.thumbnail || r.img_src);
      if(!src) return '';
      return `<div class="ws-img" data-i="${i}">
        <a class="ws-imga" href="${enc(safeUrl(r.url)||'#')}" target="_blank" rel="noopener noreferrer"
           title="${enc(r.title||'')}" data-i="${i}">
          <img src="${enc(src)}" alt="${enc(r.title||'')}" loading="lazy" onerror="this.closest('.ws-img').remove()">
          <span class="ws-imglbl">${enc(host(r.url) || '')}</span></a>
        <button class="ws-imgpg" data-i="${i}" title="Open the page this image is on"
                aria-label="Open the page this image is on">⧉</button>
      </div>`;
    }

    function overviewCard(){
      if(aiOff()) return '';
      if(!S.results.length) return '';
      if(S.ovLoading) return `<div class="ws-ov"><div class="ws-ovhd">✨ AI overview</div><div class="spinner"></div></div>`;
      if(S.overview && S.key === queryKey()){
        const text = enc(S.overview.overview || '')
          // [1] / [2][3] → links to the source they cite. Escaped FIRST, so this only ever matches
          // the model's own bracket-digits, never markup from a page title.
          .replace(/\[(\d{1,2})\]/g, (m, n) => `<a class="ws-cite" href="#ws-src-${n}" data-src="${n}">[${n}]</a>`)
          .replace(/\n/g, '<br>');
        const srcs = (S.overview.sources||[]).map(s =>
          `<li id="ws-src-${s.n}"><a href="${enc(safeUrl(s.url)||'#')}" target="_blank" rel="noopener noreferrer">${enc(s.title||s.url)}</a>
            <span class="ws-srchost">${enc(host(s.url))}</span></li>`).join('');
        return `<div class="ws-ov">
          <div class="ws-ovhd">✨ AI overview <span class="muted small">· from the top results</span></div>
          <div class="ws-ovtext">${text}</div>
          <ol class="ws-srcs">${srcs}</ol>
          <div class="ws-acts">
            <button class="btn btn-ghost small" id="ws-ov-share">↗ Share</button>
            <button class="btn btn-ghost small" id="ws-ov-note">📓 Save to Notes</button>
            <button class="btn btn-ghost small" id="ws-ov-again">↻ Redo</button>
          </div></div>`;
      }
      const err = S.ovError ? `<div class="ws-err small">${enc(S.ovError)}</div>` : '';
      return `<div class="ws-ov ws-ov-cta">
        <button class="btn btn-cyan small" id="ws-ov-go">✨ Summarize these results</button>
        <span class="muted small">Answers your search from the top pages, with sources.</span>${err}</div>`;
    }
    function paintOverview(){
      const slot = $('#ws-ov-slot'); if(!slot) return;
      slot.innerHTML = overviewCard();
      wireOverview(slot);
    }
    function wireOverview(root){
      const go = $('#ws-ov-go', root); if(go) go.onclick = loadOverview;
      const again = $('#ws-ov-again', root);
      if(again) again.onclick = ()=>{ S.overview = null; paintOverview(); loadOverview(); };
      const sh = $('#ws-ov-share', root);
      if(sh) sh.onclick = ()=>{
        const o = S.overview || {};
        const cites = (o.sources||[]).slice(0,3).map(s=>s.url).join('\n');
        compose({ text: (o.overview||'').trim() + '\n\n' + cites });
      };
      const nt = $('#ws-ov-note', root);
      if(nt) nt.onclick = (e)=>{
        const o = S.overview || {};
        const body = (o.overview||'').trim() + '\n\n' +
          (o.sources||[]).map(s=>`[${s.n}] ${s.title}\n${s.url}`).join('\n');
        saveNote(e.currentTarget, { title:'Web search — ' + S.q.trim(), body, tags:['web-search'] });
      };
      // A citation jumps to its source rather than navigating: the results are one scroll away and
      // an href="#…" inside a PWA rewrites the address bar for nothing.
      $$('.ws-cite', root).forEach(a => a.onclick = (e)=>{
        e.preventDefault();
        const li = $('#ws-src-' + a.dataset.src, root);
        if(li){ li.scrollIntoView({ block:'nearest', behavior:'smooth' }); li.classList.add('hit');
                setTimeout(()=>li.classList.remove('hit'), 1200); }
      });
    }

    function results(){
      if(S.error && !S.results.length)
        return `<div class="ws-err">${enc(S.error)}</div>`;
      if(S.loading && !S.results.length) return '<div class="spinner"></div>';
      if(!S.q.trim())
        return `<div class="empty">Search the web from here — then save what you find to Notes, share it,
                or have the AI read it for you.</div>`;
      if(!S.results.length) return '<div class="empty">No results. Try different words, or another tab.</div>';

      const answers = S.answers.map(a=>`<div class="ws-answer">${enc(a)}</div>`).join('');
      const list = S.category === 'images'
        ? `<div class="ws-grid">${S.results.map(imageCard).join('')}</div>`
        : S.results.map(resultCard).join('');
      const sugg = S.suggestions.length
        ? `<div class="ws-sugg"><span class="muted small">Related:</span>${
            S.suggestions.map(s=>`<button class="ws-chip ws-sg" data-q="${enc(s)}">${enc(s)}</button>`).join('')}</div>`
        : '';
      const more = S.more
        ? `<button class="btn btn-ghost ws-more" id="ws-more"${S.loading?' disabled':''}>${S.loading?'loading…':'More results'}</button>`
        : '';
      return `${answers}<div id="ws-ov-slot">${overviewCard()}</div>${list}${sugg}${more}`;
    }

    /* The opened result.
     *
     * TWO modes, and PAGE is the default: clicking a search result should give you the page, laid
     * out the way its author laid it out. The extracted-text mode is still there behind "Reader",
     * because it is the better answer for a wall of ads around four paragraphs — but it is not what
     * "open this result" means, and a screen full of stripped paragraphs reads as broken.
     *
     * The page is framed from OUR origin (/api/websearch/page) rather than pointed straight at the
     * site, because most sites refuse to be framed at all (X-Frame-Options / frame-ancestors). The
     * endpoint strips everything that executes and serves a no-script CSP, so the frame lays itself
     * out and does nothing else.
     */
    /* The frame's key.
     *
     * On the WEB the frame is same-origin and the session cookie rides along, so nothing goes in the
     * URL at all — which matters, because the URL is what nginx and Cloudflare write to their logs.
     * Only the BUNDLED shells (app://posterchan, the APK WebView) need a value, and what they get is
     * a ticket: 15 minutes, this endpoint only, useless for anything else. The session JWT never
     * goes near a query string.
     */
    let _ticket = { v:'', exp:0 };
    const bundled = () => { try{ const b = PC.apiBase && PC.apiBase(); return !!b && b !== location.origin; }
                            catch(_){ return false; } };
    async function ensureTicket(){
      if(!bundled()) return '';
      if(_ticket.v && Date.now() < _ticket.exp) return _ticket.v;
      try{
        const r = await jsonPost('/api/websearch/ticket', {});
        _ticket = { v: r.ticket || '', exp: Date.now() + Math.max(60, (r.expires_in || 900) - 60) * 1000 };
      }catch(_){ _ticket = { v:'', exp:0 }; }
      return _ticket.v;
    }
    /* A VIDEO result PLAYS, rather than showing the site's script-only page.
     *
     * YouTube's watch page is an application: with scripts off it is a blank rectangle, and no amount
     * of proxying changes that — a player IS its scripts. But these sites all publish an EMBED meant
     * to be framed by other people, so that is what a video result gets, straight from the site.
     * (-nocookie for YouTube: the same URL every privacy-minded embed uses.)
     */
    function embedUrl(u){
      try{
        const x = new URL(u);
        const h = x.hostname.replace(/^www\./, '');
        if(h === 'youtube.com' || h === 'm.youtube.com'){
          const v = x.searchParams.get('v');
          if(v) return 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(v);
          const m = x.pathname.match(/^\/(?:shorts|embed|live)\/([\w-]+)/);
          if(m) return 'https://www.youtube-nocookie.com/embed/' + m[1];
        }
        if(h === 'youtu.be'){
          const id = x.pathname.slice(1).split('/')[0];
          if(id) return 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(id);
        }
        if(h === 'vimeo.com'){
          const id = (x.pathname.match(/\/(\d+)/) || [])[1];
          if(id) return 'https://player.vimeo.com/video/' + id;
        }
        if(h === 'odysee.com') return u.replace('odysee.com/', 'odysee.com/$/embed/');
      }catch(_){}
      return '';
    }
    function pageUrl(u){
      // ABSOLUTE against the instance, never root-relative. The bundled desktop app and the APK serve
      // this page from app://posterchan (or the WebView's own origin) and rewrite fetch() through a
      // shim — but an <iframe src> is a NAVIGATION, which the shim never sees, so a root-relative URL
      // resolves against the bundle and the frame comes up blank ("Blocked script execution in
      // 'app://posterchan/api/websearch/page…'" on Windows).
      let base = '';
      try{ base = (PC.apiBase && PC.apiBase()) || ''; }catch(_){}
      let s = base + '/api/websearch/page?url=' + encodeURIComponent(u);
      if(_ticket.v && Date.now() < _ticket.exp) s += '&t=' + encodeURIComponent(_ticket.v);
      return s;
    }
    function readerView(){
      const r = S.reader;
      const url = safeUrl(r.url);
      const isPage = r.mode !== 'text';
      const embed = isPage ? embedUrl(r.url) : '';
      const body = embed
        // The site's OWN player, framed straight from the site — NOT through our proxy, which strips
        // the scripts a player is made of. This is the ordinary thing any page embedding a video
        // does, and it is why a video result plays instead of showing a script-only shell.
        ? `<iframe class="ws-frame" id="ws-frame" src="${enc(embed)}" allowfullscreen
                   referrerpolicy="no-referrer"
                   allow="accelerometer; autoplay; clipboard-write; encrypted-media; picture-in-picture; fullscreen"
                   title="${enc(r.title || host(url))}"></iframe>`
        : isPage
        /* allow-same-origin is BACK, and it is not a loosening: without it the frame gets an opaque
         * origin, and a page's own fonts and CORS-fetched images are then refused ("blocked by CORS
         * policy … from origin 'null'" — measured on apple.com), so the page renders half-dressed.
         * What actually keeps this safe is the response's CSP: `default-src 'none'` with no
         * script-src at all, plus every script, handler and form stripped server-side. The document
         * is inert markup; giving inert markup our origin buys it nothing. */
        ? `<iframe class="ws-frame" id="ws-frame" src="${enc(pageUrl(r.url))}"
                   sandbox="allow-same-origin allow-popups allow-popups-to-escape-sandbox"
                   referrerpolicy="no-referrer" title="${enc(r.title || host(url))}"></iframe>`
        : (r.loading ? '<div class="spinner"></div>'
           : r.error ? `<div class="ws-err">${enc(r.error)} — the page is still there, open it in a tab.</div>`
           : `<div class="ws-rtext">${r.content.split(/\n+/).filter(l=>l.trim()).map(l=>`<p>${enc(l)}</p>`).join('')}</div>`);
      return `<div class="ws-reader${isPage ? ' ws-reader-page' : ''}">
        <div class="ws-rbar">
          <button class="btn btn-ghost small ws-back" id="ws-back">← Results</button>
          <div class="ws-rsrc" title="${enc(url)}">${enc(host(url))}</div>
          <div class="ws-rbar-acts">
            <button class="btn btn-ghost small" id="ws-mode">${isPage ? '📄 Reader' : '🖼 Page'}</button>
            <a class="btn btn-ghost small" href="${enc(url||'#')}" target="_blank" rel="noopener noreferrer">↗ Open</a>
          </div>
        </div>
        ${isPage ? '' : `<h2 class="ws-rtitle">${enc(r.title || host(url))}</h2>`}
        <div class="ws-acts ws-racts">
          <button class="btn btn-ghost small" id="ws-r-share">↗ Share</button>
          <button class="btn btn-ghost small" id="ws-r-note">📓 Notes</button>
          ${aiOff() ? '' : `<button class="btn btn-ghost small" id="ws-r-sum">✨ Summarize</button>`}
        </div>
        ${body}
      </div>`;
    }

    function paint(){
      const feed = $('#feed'); if(!feed) return;
      _painting = true;                       // ignore the scroll events this repaint is about to cause
      if(S.reader){
        feed.innerHTML = `<div class="ws-wrap">${readerView()}</div>`;
        wireReader(feed);
        restoreScroll(S.reader.scroll);
        return;
      }
      feed.innerHTML = `<div class="ws-wrap">${head()}<div class="ws-results" id="ws-results">${results()}</div></div>`;
      wire(feed);
      restoreScroll(S.scroll);
    }

    function wire(root){
      const form = $('#ws-form', root);
      if(form) form.onsubmit = e => { e.preventDefault(); submit(); };
      $$('.ws-chip[data-cat]', root).forEach(b => b.onclick = ()=>{
        if(S.category === b.dataset.cat) return;
        S.category = b.dataset.cat;
        if(S.q.trim()) submit(S.q); else paint();
      });
      const t = $('#ws-time', root);
      if(t) t.onchange = ()=>{ S.time = t.value; if(S.q.trim()) submit(S.q); };
      $$('.ws-sg', root).forEach(b => b.onclick = ()=> submit(b.dataset.q));
      const more = $('#ws-more', root);
      if(more) more.onclick = ()=>{ if(S.loading) return; S.page += 1; runSearch(true); };

      const at = i => S.results[+i];
      // A plain left click reads it here; ctrl/cmd/middle-click keeps the browser's own behaviour,
      // because the title IS a real link and people expect one to open in a tab.
      $$('.ws-title', root).forEach(a => a.onclick = (e)=>{
        if(e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
        e.preventDefault(); const r = at(a.dataset.i); if(r) openReader(r);
      });
      // The whole card opens the result — the actions stop the click themselves, and the title is a
      // real link so ctrl/⌘/middle-click still gets a tab.
      $$('.ws-card', root).forEach(card => card.onclick = (e)=>{
        if(e.target.closest('a, button')) return;
        const r = at(card.dataset.i); if(r) openReader(r);
      });
      // Images: the picture in the app's lightbox (with the whole page of results as its pager), the
      // ⧉ button for the page it came from. Modified clicks are left alone — that is the browser's.
      $$('.ws-imga', root).forEach(a => a.onclick = (e)=>{
        if(e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
        e.preventDefault();
        openImage(+a.dataset.i);
      });
      $$('.ws-imgpg', root).forEach(b => b.onclick = (e)=>{
        e.preventDefault(); e.stopPropagation();
        const r = at(b.dataset.i); if(r) openReader(r);
      });
      $$('.ws-share', root).forEach(b => b.onclick = ()=>{ const r = at(b.dataset.i);
        if(r) compose({ text: (r.title||'') + '\n\n' + r.url }); });
      $$('.ws-note', root).forEach(b => b.onclick = ()=>{ const r = at(b.dataset.i); if(!r) return;
        saveNote(b, { title:r.title || host(r.url), tags:['web-search'],
                      body: (r.content ? r.content + '\n\n' : '') + r.url }); });
      $$('.ws-sum', root).forEach(b => b.onclick = ()=>{ const r = at(b.dataset.i);
        if(r) summarize(r.url, r.title, b); });

      wireOverview(root);
    }

    function wireReader(root){
      const r = S.reader;
      const back = $('#ws-back', root); if(back) back.onclick = closeReader;
      const md = $('#ws-mode', root); if(md) md.onclick = toggleMode;
      const sh = $('#ws-r-share', root);
      if(sh) sh.onclick = ()=> compose({ text: (r.title||'') + '\n\n' + r.url });
      const nt = $('#ws-r-note', root);
      // Saving from PAGE mode still saves the page's TEXT, not a bookmark — that is the difference
      // between a saved article and a link that 404s next year — so fetch it if it isn't here yet.
      if(nt) nt.onclick = async ()=>{
        if(!r.content && !r.error) await loadText();
        saveNote(nt, { title: r.title || host(r.url), tags:['web-search'],
                       body: ((S.reader && S.reader.content) ? S.reader.content.slice(0, 40000) + '\n\n' : '') + r.url });
      };
      const sm = $('#ws-r-sum', root);
      if(sm) sm.onclick = ()=> summarize(r.url, r.title, sm);
    }

    // ---- the view ----------------------------------------------------------------------------
    function render(){
      paint();
      refreshAiState();     // cached after the first call; hides the ✨ buttons if this user lacks can_ai
      // Focus the box on a FIRST visit only (an empty screen with a keyboard up is the point); never
      // on a return trip, where it would pop the phone keyboard over results you came back to read.
      if(!S.q.trim() && !S.results.length){
        const el = $('#ws-q');
        if(el && window.matchMedia && !window.matchMedia('(max-width:820px)').matches) try{ el.focus(); }catch(_){}
      }
    }

    window.PCWebSearch = {
      render,
      // The Android/Electron back button walks the view stack; the reader is a sub-screen INSIDE
      // this view, so it has to be closed first or Back leaves Web Search with the article still up.
      readerOpen: () => !!S.reader,
      closeReader,
      // Someone searched from elsewhere (the sidebar box, a command) — land here with it running.
      search(q){ S.q = q || ''; S.reader = null; if(window.__PC.switchView) window.__PC.switchView('websearch');
                 runSearch(false); },
    };
  }
  init();
})();

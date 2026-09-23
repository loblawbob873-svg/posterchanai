/* Discover's own screens — long-form articles (reader, comments, editor), torrents (NIP-35 list and
 * the local client), search results, hashtag feeds, Trending, and the in-app Admin frame. Split out
 * of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_discoverDeps`) and
 * builds this factory the first time one of these screens opens or a search runs. The code below is
 * BYTE-IDENTICAL to what it replaced in app.js apart from its reads of app.js's live `let` bindings,
 * which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact identifier
 * offsets. `_sendAdminToken` (called on every login) only talks to an admin frame this module
 * creates, so it does nothing before the module exists. Trending's remembered tab is read from localStorage when the module is built.
 *
 * Stayed in app.js: what other lists draw with (articleCard, articleAddr, torrentCard, _fmtBytes,
 * artTime, _dedupAddr/_matchAddr), bindSearch (bound at boot; it calls runSearch here), NIP-05
 * verification, and the desktop right column (topics, notifications), which loads at boot.
 */
window.PCDiscoverFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.BOOKMARKS, S.FOLLOWS, S.GUEST, S.IS_ADMIN, S.LOGO, S.ME, S.VIEW, S._aiAuth, S._aiToken
  const {
    $, $$, InstEmoji, NT, ZAP_ICON, _backOut, _bindTimelineHeader, _capFeedDom, _clearNav, _dedupAddr,
    _feedScrollable, _fmtBytes, _guestPrompt, _hidePill, _hold, _insertAt, _isDesktopApp,
    _matchAddr, _navTopHtml, _navUrl, _timelineHeaderHtml, _tlNotes, _webLink, applyEmojis, artTime, articleAddr,
    articleCard, cleanupInlineStream, clearSentinel, closeModal, compose, copyValue,
    decorateProfiles, doZap, emojiName, enc, ensureAiSession, feedNoteHtml, fetchNotes, hydrate,
    hydrateCounts, hydrateLinkCards, hydratePolls, invalidateCounts, isMutedView, linkify,
    loadSentinel, mdToHtml, mediaParts, mentionTags, modal, needProfile, niceNip05, nip05Resolve,
    noteHtml, npubOf, observeProfiles, openLightbox, openRepo, openStream, openThread, profOf,
    publish, renderProfileView, renderView, repoCard, safePk, streamCard, switchView, timeAgo,
    toast, toggleBookmark, torrentCard, uiConfirm, uiPrompt, uploadBlob,
  } = dep;
  async function renderArticles(){
    const feed=$('#feed');
    feed.innerHTML=`<div class="art-top"><button class="btn btn-neon small" id="art-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>Write article</button></div><div id="art-drafts"></div><div id="art-list"><div class="spinner"></div></div>`;
    $('#art-new').onclick=()=>renderArticleEditor();
    let evs=[], drafts=[];
    try{ evs=await Relay.query([{ kinds:[30023], limit:80 }]); }catch(_){}
    try{ drafts=await Relay.query([{ kinds:[30024], authors:[S.ME.pubkey], limit:50 }]); }catch(_){}   // my NIP-23 drafts
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    drafts.forEach(e=>Store.saveEvent(e));
    if(S.VIEW!=='articles') return;
    // Drafts (your unpublished kind-30024) — resume or delete.
    const db=$('#art-drafts');
    if(db){
      const dd=_dedupAddr(drafts).sort((a,b)=>(b.created_at||0)-(a.created_at||0));
      db.innerHTML = dd.length ? '<div class="search-section-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-draft"></use></svg>Drafts</div>'+dd.map(d=>{
        const t=(d.tags.find(x=>x[0]==='title')||[])[1]||'(untitled)';
        const slug=(d.tags.find(x=>x[0]==='d')||[])[1]||'';
        return `<div class="draft-art" data-id="${d.id}" data-slug="${enc(slug)}"><span class="da-title">📝 ${enc(t)}</span><span class="spacer"></span><button class="btn btn-ghost small da-edit">Resume</button><button class="btn btn-ghost small da-del" style="color:var(--danger)" aria-label="Delete"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></div>`;
      }).join('') : '';
      $$('.draft-art',db).forEach(c=>{
        c.querySelector('.da-edit').onclick=()=>{ const e=Store.get(c.dataset.id); if(e) renderArticleEditor(e); };
        c.querySelector('.da-del').onclick=async()=>{ if(!await uiConfirm('Delete this draft?'))return; await _deleteArticleDraft(c.dataset.slug); c.remove(); toast('draft deleted'); };
      });
    }
    const arts=_dedupAddr(evs).sort((a,b)=>artTime(b)-artTime(a));
    const list=$('#art-list'); if(!list) return;
    list.innerHTML = arts.length ? arts.map(articleCard).join('') : '<div class="empty">No articles yet. Tap “Write article” to publish the first one.</div>';
    decorateProfiles();
    $$('.article-card',list).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const a=Store.get(c.dataset.id); if(a) openArticle(a); });
    if(arts.length) _fillArticleCommentCounts(arts, list);
  }
  // Fill the 💬 comment count on the listed article cards with ONE query (not one per card), counting
  // events by the article ROOT scope (#A / legacy #a) they carry. Best-effort, purely additive.
  async function _fillArticleCommentCounts(arts, list){
    const addrs=[...new Set(arts.map(articleAddr))]; if(!addrs.length) return;
    let cs=[]; try{ cs=await Relay.query([{ kinds:[1,1111], '#A':addrs, limit:500 }, { kinds:[1,1111], '#a':addrs, limit:500 }]); }catch(_){}
    const counts=new Map(), seen=new Set();
    for(const c of cs){ if(seen.has(c.id)) continue; seen.add(c.id);
      const a=(c.tags||[]).find(t=>(t[0]==='A'||t[0]==='a') && addrs.includes(t[1]));
      if(a) counts.set(a[1], (counts.get(a[1])||0)+1); }
    if(S.VIEW!=='articles' || !list) return;
    list.querySelectorAll('.art-cc').forEach(el=>{ const n=counts.get(el.dataset.addr)||0; if(n) el.textContent=` · 💬 ${n}`; });
  }
  /* AN ARTICLE OPENS AS A DOCUMENT, NOT OVER WHATEVER SCREEN IT WAS CLICKED IN.
   *
   * Reported from Social: an article card inside a post painted the reader into the Social window's
   * feed -- so the timeline's next redraw could land on top of it -- and it pushed no history, so
   * Back skipped past it (and its own button always went to the Articles list, wherever the reader
   * had come from). On the desktop it now goes through openThread, which gives it a window of its
   * own (`doc:post:<id>`) exactly as a post gets; renderThread hands a long-form event back here
   * with `inPlace`. Everywhere it gets a real address (its naddr), so Back and a reload both work. */
  function openArticle(e, opts){
    const inPlace = !!(opts && opts.inPlace);
    if(!inPlace && !S._routing && window.PCOS && PCOS.isOn() && e && e.id){
      try{ Store.saveEvent(e); }catch(_){}
      return openThread(e.id);
    }
    /* The address goes in BEFORE the view changes: _navUrl stamps the entry being LEFT with the
     * current view, so setting 'article' first labelled the timeline's own entry "article" and Back
     * popped straight back into the article. Arriving through the post window (inPlace), openThread
     * has already pushed an nevent for this same document, so that entry is REPLACED, not stacked. */
    try{
      const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
      _navUrl('/'+NT().nip19.naddrEncode({ kind:e.kind, pubkey:e.pubkey, identifier:d }), inPlace);
    }catch(_){}
    S.VIEW='article'; _clearNav(); $('#view-title').textContent='Article';
    const feed=_feedScrollable(); const p=profOf(e.pubkey); needProfile(e.pubkey);
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||'';
    const mine=e.pubkey===S.ME.pubkey;
    feed.innerHTML=_navTopHtml('art-back', 'Back')+`<div class="article-view">
      ${img?_hold(`<img class="av-banner" src="${enc(img)}" onerror="this.remove()">`, img, 'image', 'av-banner'):''}
      <h1 class="av-title">${enc(title)}</h1>
      <div class="av-by"><img class="art-av" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(artTime(e))}</span></div>
      <div class="av-actions">
        <button class="act actb ${S.BOOKMARKS.has(e.id)?'on':''}" id="av-bm" title="bookmark"><svg class="ic b-ic" aria-hidden="true"><use href="#i-bookmark"></use></svg></button>
        <button class="act actz" id="av-zap" title="zap">${ZAP_ICON}</button>
        ${mine?`<button class="act" id="av-edit" title="edit"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg></button>`:''}
        ${mine?`<button class="act" id="av-del" title="delete" style="color:var(--danger)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg></button>`:''}
        <button class="act" id="av-copy" title="copy link"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg></button>
      </div>
      <div class="markdown av-body">${mdToHtml(e.content)}</div>
      <div class="av-comments">
        <div class="av-comments-hd"><span class="search-section-title">Comments</span>
          <button class="btn btn-neon small" id="av-comment"><svg class="ic b-ic" aria-hidden="true"><use href="#i-chat"></use></svg>Write a comment</button></div>
        <div id="av-comment-list"><div class="spinner"></div></div>
      </div>
    </div>`;
    $('#art-back').onclick=()=>_backOut('post:'+e.id, 'articles');
    $('#av-bm').onclick=ev=>toggleBookmark(e.id, ev.currentTarget);
    $('#av-zap').onclick=()=>doZap(e.id, e.pubkey);
    { const ed=$('#av-edit'); if(ed) ed.onclick=()=>renderArticleEditor(e); }
    { const dl=$('#av-del'); if(dl) dl.onclick=()=>deleteArticle(e); }
    $('#av-copy').onclick=()=>{ let _lk=e.id, _m='id copied';
      try{ _lk=_webLink(NT().nip19.naddrEncode({ identifier:(e.tags.find(t=>t[0]==='d')||[])[1]||'', pubkey:e.pubkey, kind:30023 })); _m='article link copied'; }catch(_){}
      copyValue(_lk, _m, 'Link to this article:'); };
    { const cb=$('#av-comment'); if(cb) cb.onclick=()=>{ if(S.GUEST){ _guestPrompt(); return; } compose({articleComment:e}); }; }
    feed.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    feed.querySelectorAll('.markdown img').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    decorateProfiles();
    loadArticleComments(e);
  }
  // One threaded comment (+ its nested replies, recursively). Custom card (not noteCard) so the Reply
  // button posts a NIP-22 reply THREADED under this comment, not a plain kind-1.
  function _acCard(c, depth){
    const p=profOf(c.pubkey); needProfile(c.pubkey);
    const name=p.name||p.display_name||(npubOf(c.pubkey).slice(0,12)+'…');
    const handle=niceNip05(p.nip05)||('@'+npubOf(c.pubkey).slice(4,12));
    const mp=mediaParts(c.content, c);
    const kids=(c._kids||[]).map(k=>_acCard(k, depth+1)).join('');
    return `<div class="ac-item"${depth?` style="margin-left:${Math.min(depth,5)*14}px"`:''}>
      <div class="ac-hd"><img class="ac-av" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span class="name" data-prof="${c.pubkey}">${emojiName(c.pubkey,name)}</span><span class="vchk" data-pk="${c.pubkey}"></span><span class="handle">${enc(handle)}</span><span class="time">${timeAgo(c.created_at)}</span></div>
      ${mp.mediaFirst?mp.gallery:''}<div class="ac-body">${applyEmojis(linkify(mp.text), c)}</div>${mp.mediaFirst?'':mp.gallery}
      <div class="ac-act"><button class="btn btn-ghost small ac-reply" data-id="${c.id}"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg>Reply</button></div>
      ${kids}</div>`;
  }
  async function loadArticleComments(e){
    const addr=articleAddr(e);
    // #A = the NIP-22 ROOT scope → catches top-level AND nested replies (nested carry `A`=article but
    // `e`=parent, so `#a` alone would miss them). `#a` too for legacy/top-level. Pool dedups by id.
    let cs=[]; try{ cs=await Relay.query([{ kinds:[1,1111], '#A':[addr], limit:200 }, { kinds:[1,1111], '#a':[addr], limit:200 }]); }catch(_){}
    cs.forEach(x=>{ Store.saveEvent(x); needProfile(x.pubkey); });
    if(S.VIEW!=='article') return;                         // navigated away while loading
    const box=$('#av-comment-list'); if(!box) return;
    cs=cs.filter(x=>!isMutedView(x));
    // Build the reply tree: a comment nests under another comment IN this set that it e-tags; otherwise
    // it's top-level (its parent is the article). Guard against self/cyclic parents.
    const byId=new Map(cs.map(c=>[c.id,c])); cs.forEach(c=>c._kids=[]);
    const roots=[];
    for(const c of cs){
      const pid=(c.tags||[]).filter(t=>t[0]==='e'&&t[1]&&t[1]!==c.id).map(t=>t[1]).find(id=>byId.has(id));
      if(pid) byId.get(pid)._kids.push(c); else roots.push(c);
    }
    const sortRec=a=>{ a.sort((x,y)=>x.created_at-y.created_at); a.forEach(c=>sortRec(c._kids)); };   // oldest-first
    sortRec(roots);
    box.innerHTML = roots.length ? roots.map(c=>_acCard(c,0)).join('') : '<div class="empty">No comments yet — be the first to reply.</div>';
    box.querySelectorAll('.ac-reply').forEach(b=> b.onclick=()=>{ if(S.GUEST){ _guestPrompt(); return; } const c=byId.get(b.dataset.id); if(c) compose({articleComment:e, articleParent:c}); });
    box.querySelectorAll('[data-prof]').forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    box.querySelectorAll('.ac-item img:not(.ac-av)').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    decorateProfiles();
  }
  // Article drafts are NIP-23 **kind-30024** (draft long-form) events — same shape as a published
  // 30023 but a draft, so they live on your relay, sync across devices/clients, and you own them.
  // "Save draft" publishes/updates the 30024; publishing the article (30023) deletes the draft.
  let _aeDraftT=null;
  function _slugFor(title){ return ((title||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60) || 'draft') + '-' + Math.random().toString(36).slice(2,7); }
  async function _saveArticleDraft(slug, a){
    const tags=[['d',slug],['title',a.title||'']];
    if(a.summary) tags.push(['summary',a.summary]);
    if(a.image) tags.push(['image',a.image]);
    return await publish(30024, a.body||'', tags);   // NIP-23 draft long-form
  }
  async function _deleteArticleDraft(slug){ if(!slug) return; try{ await publish(5, 'draft published', [['a', `30024:${S.ME.pubkey}:${slug}`]]); }catch(_){} }
  // On publish, the draft's slug may differ from the published slug (drafted across sessions / title
  // changed after the first autosave). Delete the exact slug AND any draft with the same title, so a
  // published article never leaves an orphaned draft behind.
  async function _deletePublishedDrafts(slug, title){
    const coords=new Set(); if(slug) coords.add(`30024:${S.ME.pubkey}:${slug}`);
    try{
      const drafts=await Relay.query([{ kinds:[30024], authors:[S.ME.pubkey], limit:50 }]);
      for(const d of (drafts||[])){
        const dt=(d.tags.find(t=>t[0]==='d')||[])[1]; if(!dt) continue;
        const ti=((d.tags.find(t=>t[0]==='title')||[])[1]||'').trim();
        if(dt===slug || (title && ti===title.trim())) coords.add(`30024:${S.ME.pubkey}:${dt}`);
      }
    }catch(_){}
    if(coords.size){ try{ await publish(5, 'draft published', [...coords].map(c=>['a',c])); }catch(_){} }
  }
  function renderArticleEditor(existing){
    S.VIEW='article'; _clearNav(); $('#view-title').textContent=existing?'Edit article':'Write article';
    const feed=$('#feed'); const g=(k)=>existing?((existing.tags.find(t=>t[0]===k)||[])[1]||''):'';
    feed.innerHTML=`<div class="article-editor">
      <button class="btn btn-ghost small" id="ae-back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg>Cancel</button>
      <label class="fld">Title<input class="input" id="ae-title" placeholder="Article title" value="${enc(g('title'))}"></label>
      <label class="fld">Summary<input class="input" id="ae-sum" placeholder="One-line summary (optional)" value="${enc(g('summary'))}"></label>
      <label class="fld">Header image<input class="input" id="ae-img" placeholder="https://… (optional)" value="${enc(g('image'))}"></label>
      <div class="row" style="margin:-6px 0 2px"><button type="button" class="btn btn-ghost small" id="ae-img-up"><svg class="ic b-ic" aria-hidden="true"><use href="#i-image"></use></svg>Upload header image</button></div>
      <input type="file" id="ae-img-file" accept="image/*" hidden>
      <div class="cmp-tabs ae-tabs"><button class="cmp-tab active" data-t="write">Write</button><button class="cmp-tab" data-t="preview"><svg class="ic b-ic" aria-hidden="true"><use href="#i-eye"></use></svg>Preview</button></div>
      <div class="row cmp-tools"><button class="btn btn-ghost small" id="ae-insert"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg>Insert image</button><input type="file" id="ae-body-file" accept="image/*" multiple hidden><span class="spacer"></span><span class="muted small">Markdown</span></div>
      <!-- Side by side on a desktop, one at a time behind the tabs above on a phone. Both panes exist
           in the DOM either way — the tabs only change which is SHOWN, so switching costs nothing and
           the preview never has to be rebuilt from scratch. -->
      <div class="ae-split" id="ae-split">
        <div class="ae-pane">
          <div class="ae-pane-hd">Write</div>
          <textarea id="ae-body" class="article-body" placeholder="Write your article in markdown…">${enc(existing?existing.content:'')}</textarea>
        </div>
        <div class="ae-pane">
          <div class="ae-pane-hd">Preview</div>
          <div id="ae-preview" class="markdown article-preview"></div>
        </div>
      </div>
      <div class="row"><span class="muted small" id="ae-status"></span><span class="spacer"></span><button type="button" class="btn btn-ghost small" id="ae-draft"><svg class="ic b-ic" aria-hidden="true"><use href="#i-cloud"></use></svg>Save draft</button><button class="btn btn-neon" id="ae-pub">Publish ▶</button></div>
    </div>`;
    $('#ae-back').onclick=()=>switchView('articles');
    const body=$('#ae-body');
    // Slug (d-tag): reused when editing a published article OR resuming a draft, so saving updates
    // the SAME 30024 (not a duplicate). Generated on first save for a brand-new article.
    let _aeSlug = g('d') || null;
    const _grabArticle=()=>({title:$('#ae-title').value, summary:$('#ae-sum').value, image:$('#ae-img').value, body:body.value});
    async function _doSaveDraft(announce){
      const a=_grabArticle();
      if(!(a.title||a.body||a.image||a.summary)){ if(announce) toast('nothing to save yet'); return; }
      if(!_aeSlug) _aeSlug=_slugFor(a.title);
      if($('#ae-status')) $('#ae-status').textContent='saving draft…';
      try{ await _saveArticleDraft(_aeSlug, a); if($('#ae-status')) $('#ae-status').textContent='✓ draft saved'; if(announce) toast('draft saved (in Articles)'); }
      catch(e){ if($('#ae-status')) $('#ae-status').textContent='draft save failed'; }
    }
    { const d=$('#ae-draft'); if(d) d.onclick=()=>_doSaveDraft(true); }
    // Gentle auto-save to a 30024 so work survives a refresh (cleared when you publish).
    body.addEventListener('input', ()=>{ clearTimeout(_aeDraftT); _aeDraftT=setTimeout(()=>_doSaveDraft(false), 4000); });
    /* LIVE PREVIEW. Same custom-emoji pass as the post composer, so a :shortcode: looks in the
     * preview the way it will look published.
     *
     * Debounced rather than per-keystroke: mdToHtml + the emoji pass over a long article is real work,
     * and running it on every character is how a writing surface starts dropping keys. 120ms is under
     * the gap between words, so it reads as live while never running mid-burst. */
    const prev=$('#ae-preview'), split=$('#ae-split');
    let _pvT=null;
    const renderPreview=()=>{
      try{ prev.innerHTML=InstEmoji.render(mdToHtml(body.value))||'<div class="muted small">Nothing to preview yet — start writing on the left.</div>'; }
      catch(_){ prev.innerHTML='<div class="muted small">Couldn’t render that markdown.</div>'; }
    };
    const queuePreview=()=>{ clearTimeout(_pvT); _pvT=setTimeout(renderPreview, 120); };
    body.addEventListener('input', queuePreview);
    renderPreview();
    /* The tabs are the PHONE's control — CSS hides them above 820px, where both panes are on screen
     * and switching would mean nothing. Kept in the DOM at every width so the class they toggle is
     * the single thing deciding which pane shows. */
    $$('.cmp-tab',feed).forEach(b=> b.onclick=()=>{
      $$('.cmp-tab',feed).forEach(x=>x.classList.toggle('active',x===b));
      const pv=b.dataset.t==='preview';
      split.classList.toggle('show-preview', pv);
      if(pv) renderPreview();                 // catch up if the debounce had not fired yet
      else body.focus();
    });
    /* Scroll the preview WITH the text, proportionally. Both panes scroll independently, so without
     * this the two halves drift apart the moment an article is longer than the pane and side-by-side
     * stops being side-by-side. `_lock` breaks the feedback loop: setting scrollTop fires scroll on
     * the other pane, which would set this one back. */
    let _lock=false;
    const sync=(from,to)=>()=>{
      if(_lock || split.classList.contains('show-preview')) return;
      const fr=from.scrollHeight-from.clientHeight, tr=to.scrollHeight-to.clientHeight;
      if(fr<=0 || tr<=0) return;
      _lock=true; to.scrollTop=(from.scrollTop/fr)*tr;
      requestAnimationFrame(()=>{ _lock=false; });
    };
    body.addEventListener('scroll', sync(body, prev), { passive:true });
    prev.addEventListener('scroll', sync(prev, body), { passive:true });
    $('#ae-img-up').onclick=()=>$('#ae-img-file').click();
    $('#ae-img-file').onchange=async ev=>{ const f=ev.target.files[0]; if(!f)return; $('#ae-status').textContent='uploading image…'; try{ $('#ae-img').value=await uploadBlob(f,{folder:'Posts'}); $('#ae-status').textContent='image uploaded'; }catch(err){ $('#ae-status').textContent='upload failed: '+err.message; } };
    $('#ae-insert').onclick=()=>$('#ae-body-file').click();
    $('#ae-body-file').onchange=async ev=>{ const files=[...ev.target.files]; for(let i=0;i<files.length;i++){ $('#ae-status').textContent=`uploading ${i+1}/${files.length}…`; try{ const url=await uploadBlob(files[i],{folder:'Posts'}); _insertAt(body, `\n![](${url})\n`); }catch(err){ $('#ae-status').textContent='upload failed: '+err.message; return; } } $('#ae-status').textContent=''; ev.target.value=''; };
    $('#ae-pub').onclick=()=>publishArticle({ title:$('#ae-title').value.trim(), summary:$('#ae-sum').value.trim(), image:$('#ae-img').value.trim(), body:body.value, d:_aeSlug });
  }
  async function deleteArticle(e){
    // NIP-09: a kind-5 deletion referencing the article by event id AND addressable coordinate.
    // It broadcasts to all upstream relays (deletions are broadcastable), so they remove it too.
    if(!await uiConfirm('Delete this article? This asks every relay (NIP-09) to remove it.')) return;
    const slug=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const tags=[['e',e.id]]; if(slug) tags.push(['a',`30023:${e.pubkey}:${slug}`]);
    try{ const r=await publish(5, 'deleted', tags);   // failure toast by publish()
      if(r && r.ok){ toast('deletion requested'); switchView('articles'); } }
    catch(err){ toast('delete failed: '+(err.message||'')); }
  }
  async function publishArticle({title, summary, image, body, d}){
    if(!title){ toast('add a title'); return; }
    if(!body.trim()){ toast('write something first'); return; }
    const slug = d || ((title.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60) || 'post') + '-' + Math.random().toString(36).slice(2,7));
    const tags=[['d',slug],['title',title],['published_at',String(Math.floor(Date.now()/1000))]];
    if(summary) tags.push(['summary',summary]);
    if(image) tags.push(['image',image]);
    mentionTags(body).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
    $('#ae-status') && ($('#ae-status').textContent='publishing…');
    try{ const r=await publish(30023, body, tags); if(r && r.ok===false){ toast('relay: '+(r.msg||'rejected')); if($('#ae-status'))$('#ae-status').textContent=''; } else { _deletePublishedDrafts(slug, title); toast('article published'); switchView('articles'); } }
    catch(e){ toast('publish failed: '+e.message); }
  }

  // ---------- torrents (NIP-35, kind 2003) ----------
  // A magnet's infohash may be hex (40/64) OR RFC4648 base32 (32 chars) — older clients still emit base32,
  // and _magnet() only accepts hex, so a base32 one would publish an event whose magnet button is dead.
  // Convert instead of rejecting.
  function _b32ToHex(s){
    const A='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; let bits=0, val=0, out='';
    for(const ch of String(s).toUpperCase()){ const i=A.indexOf(ch); if(i<0) return ''; val=(val<<5)|i; bits+=5;
      if(bits>=8){ bits-=8; out+=((val>>bits)&0xff).toString(16).padStart(2,'0'); } }
    return out;
  }
  function _parseMagnet(m){
    const out={ ih:'', name:'', trackers:[] };
    const xt=/xt=urn:btih:([a-z0-9]+)/i.exec(m||''); if(!xt) return out;
    let ih=xt[1];
    if(/^[a-z2-7]{32}$/i.test(ih) && !/^[0-9a-f]{32}$/i.test(ih)) ih=_b32ToHex(ih);   // base32 → hex
    out.ih=/^([0-9a-f]{40}|[0-9a-f]{64})$/i.test(ih) ? ih.toLowerCase() : '';
    const dn=/[?&]dn=([^&]+)/i.exec(m||''); if(dn){ try{ out.name=decodeURIComponent(dn[1].replace(/\+/g,' ')); }catch(_){ out.name=dn[1]; } }
    for(const t of (m||'').matchAll(/[?&]tr=([^&]+)/gi)){ try{ out.trackers.push(decodeURIComponent(t[1])); }catch(_){ out.trackers.push(t[1]); } }
    return out;
  }
  // Publish a NIP-35 torrent (kind 2003) — the same shape torrentCard/_magnet already read, so a published
  // one renders in this list exactly like the ones already there.
  function addTorrent(){
    if(S.GUEST){ _guestPrompt(); return; }
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-magnet"></use></svg>Add a torrent</h3>
      <p class="muted small">Published to Nostr as a NIP-35 torrent (kind 2003), so it shows up here and in any other NIP-35 client.</p>
      <label class="fld">Magnet link<textarea id="tor-mag" rows="3" placeholder="magnet:?xt=urn:btih:…"></textarea></label>
      <label class="fld">Title<input class="input" id="tor-title" maxlength="200" placeholder="Name of the release"></label>
      <label class="fld">Description<textarea id="tor-desc" rows="3" placeholder="Optional"></textarea></label>
      <label class="fld">Tags<input class="input" id="tor-tags" placeholder="comma-separated, e.g. linux, iso"></label>
      <div class="muted small" id="tor-status"></div>
      <div class="row" style="justify-content:flex-end;gap:8px"><button class="btn btn-ghost" id="tor-cancel">Cancel</button><button class="btn btn-neon" id="tor-go">Publish</button></div>`, root=>{
      const mag=$('#tor-mag',root), ti=$('#tor-title',root), st=$('#tor-status',root);
      // Autofill the title from the magnet's display name — one less thing to retype.
      mag.addEventListener('input', ()=>{ const p=_parseMagnet(mag.value);
        st.textContent = mag.value.trim() && !p.ih ? 'That magnet has no usable infohash.' : '';
        if(p.name && !ti.value) ti.value=p.name; });
      $('#tor-cancel',root).onclick=closeModal;
      $('#tor-go',root).onclick=async()=>{
        const p=_parseMagnet(mag.value);
        if(!p.ih){ st.textContent='Paste a magnet link with a valid btih infohash.'; return; }
        const title=(ti.value||'').trim()||p.name||'Untitled torrent';
        const go=$('#tor-go',root); go.disabled=true; st.textContent='publishing…';
        const tags=[['title',title], ['x',p.ih]];
        p.trackers.forEach(tr=>tags.push(['tracker',tr]));
        (($('#tor-tags',root).value||'').split(',').map(s=>s.trim()).filter(Boolean)).forEach(t=>tags.push(['t',t]));
        try{
          const r=await publish(2003, ($('#tor-desc',root).value||'').trim(), tags);
          if(r && r.ok){ closeModal(); toast('🧲 torrent published'); if(S.VIEW==='torrents') renderView(true); return; }
          st.textContent='';   // publish() raises its own failure toast
        }catch(e){ st.textContent='publish failed: '+((e&&e.message)||e); }
        go.disabled=false;
      };
    });
  }
  /* ---------- Discover → Torrents ----------
   * Two tabs. DOWNLOADS is the manager for this node's own torrent client — the same client the
   * `torrent` chat command drives, so a download started by typing at the AI turns up here and can
   * be paused or removed with a button instead of another command. NOSTR is the NIP-35 feed of
   * torrents other people have published, which is what this whole view used to be.
   * Downloads is the default: the thing you came to manage is more useful than a stranger's list. */
  let _torTab = 'dl';
  let _torPollT = 0;

  async function _torApi(path, opts){
    try{ await ensureAiSession(); }catch(_){}   // the APK has no cookie; the bearer token is the auth
    const r = await fetch('/api/torrent'+path, { credentials:'include', ...(opts||{}),
      headers:{ 'Content-Type':'application/json', ...((opts&&opts.headers)||{}),
                ...(S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}) } });
    if(!r.ok){
      const e=new Error('http '+r.status); e.status=r.status;
      // FastAPI returns `detail` as a STRING for an HTTPException and as a LIST OF OBJECTS for a
      // 422 — pasting the latter into a toast reads "could not pause that: [object Object]", which
      // told the user nothing and me nothing either. Flatten it to the messages.
      try{
        const d=(await r.json()).detail;
        e.detail = Array.isArray(d)
          ? d.map(x=>[(x.loc||[]).slice(-1)[0], x.msg].filter(Boolean).join(': ')).join('; ')
          : (typeof d==='string' ? d : (d ? JSON.stringify(d) : ''));
      }catch(_){}
      throw e;
    }
    return r.json();
  }

  const _TOR_STATE = { downloading:'downloading', seeding:'seeding', finished:'done',
                       checking:'checking', 'checking files':'checking', 'downloading metadata':'metadata',
                       queued:'queued', allocating:'allocating', paused:'paused', error:'error' };

  function _torRow(t){
    const pct = Math.max(0, Math.min(100, (Number(t.progress)||0) * (Number(t.progress)<=1 ? 100 : 1)));
    const st = t.is_paused ? 'paused' : (_TOR_STATE[String(t.state||'').toLowerCase()] || String(t.state||''));
    const done = !!t.is_finished;
    return `<div class="tm-item${done?' done':''}${t.is_paused?' paused':''}" data-h="${enc(t.info_hash)}">
      <div class="tm-head">
        <span class="tm-name" title="${enc(t.name||'')}">${enc(t.name||'(metadata…)')}</span>
        <span class="tm-state">${enc(st)}</span>
      </div>
      <div class="tm-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="tm-meta">
        <span>${pct.toFixed(pct<10?1:0)}%</span>
        <span>${_fmtBytes(t.downloaded)} / ${_fmtBytes(t.size)}</span>
        <span title="download">↓ ${_fmtBytes(t.download_rate)}/s</span>
        <span title="upload">↑ ${_fmtBytes(t.upload_rate)}/s</span>
        <span title="seeds / peers">${Number(t.seeders)||0}⇡ ${Number(t.peers)||0}⇢</span>
      </div>
      <div class="tm-acts">
        <button class="btn btn-ghost small tm-toggle">${t.is_paused?'▶ Resume':'⏸ Pause'}</button>
        <button class="btn btn-ghost small tm-del" style="color:var(--danger)">✕ Remove</button>
      </div></div>`;
  }

  /* The action endpoints address a torrent by its POSITION in the list, not by hash. A position is
   * only true until something is added or removed, and this list refreshes every two seconds — so
   * resolve the hash to a current number at the moment of the click. Reusing a number captured at
   * render time would, for Remove-with-files, eventually delete the wrong torrent's downloads. */
  async function _torNum(hash){
    const j = await _torApi('/list');
    const hit = ((j&&j.torrents)||[]).find(t=>t.info_hash===hash);
    if(!hit) { const e=new Error('that torrent is no longer in the list'); e.detail=e.message; throw e; }
    return hit.num;
  }

  function _torBindRows(box){
    $$('.tm-item',box).forEach(row=>{
      const h=row.dataset.h;
      const tg=row.querySelector('.tm-toggle');
      if(tg) tg.onclick=async()=>{
        const resume=/Resume/.test(tg.textContent);
        tg.disabled=true;
        try{ await _torApi(resume?'/resume':'/pause',{method:'POST',body:JSON.stringify({num:await _torNum(h)})}); }
        catch(err){ toast('could not '+(resume?'resume':'pause')+' that: '+(err.detail||err.message)); }
        finally{ tg.disabled=false; _torRefresh(true); }
      };
      const dl=row.querySelector('.tm-del');
      if(dl) dl.onclick=async()=>{
        const name=(row.querySelector('.tm-name')||{}).textContent||'this torrent';
        // Two questions, because they are two different losses: one frees the slot, the other
        // deletes what you already downloaded and cannot be undone.
        if(!await uiConfirm(`Remove “${name}” from the list?`)) return;
        const wipe=await uiConfirm('Delete the downloaded files too? Cancel keeps them on disk.');
        try{ await _torApi('/remove',{method:'POST',
               body:JSON.stringify({num:await _torNum(h), delete_files:!!wipe})});
             toast('removed'); }
        catch(err){ toast('could not remove that: '+(err.detail||err.message)); }
        _torRefresh(true);
      };
    });
  }

  async function _torRefresh(force){
    const box=$('#tm-list'); if(!box || S.VIEW!=='torrents' || _torTab!=='dl') return;
    let j=null;
    try{ j=await _torApi('/list'); }
    catch(err){
      if(S.VIEW!=='torrents') return;
      // 503 is "no torrent client on this node", which is a SETTING, not a fault — say which.
      box.innerHTML = err.status===503
        ? `<div class="empty">This server has no torrent client enabled.<br><span class="muted small">An admin turns it on in Admin → Tools.</span></div>`
        : (err.status===401 || err.status===403
            ? '<div class="empty">You don\'t have access to the torrent client on this server.</div>'
            : `<div class="empty">Couldn\'t reach the torrent client.<br><span class="muted small">${enc(err.detail||err.message)}</span></div>`);
      return;
    }
    if(S.VIEW!=='torrents' || _torTab!=='dl') return;
    const list=(j&&j.torrents)||[];
    // Repaint in place while the list is unchanged in SHAPE, so a click target does not move under
    // a finger every two seconds; rebuild only when torrents are added or removed.
    const sig=list.map(t=>t.info_hash).join(',');
    if(!force && box.dataset.sig===sig){
      list.forEach(t=>{ const row=box.querySelector(`.tm-item[data-h="${CSS.escape(t.info_hash)}"]`);
        if(row){ const tmp=document.createElement('div'); tmp.innerHTML=_torRow(t);
                 row.replaceWith(tmp.firstElementChild); } });
      _torBindRows(box);
      return;
    }
    box.dataset.sig=sig;
    box.innerHTML = list.length ? list.map(_torRow).join('')
      : '<div class="empty">Nothing downloading.<br><span class="muted small">Add a magnet link above, or start one from AI Chat with <code>torrent &lt;magnet&gt;</code>.</span></div>';
    _torBindRows(box);
  }

  function _torStopPoll(){ if(_torPollT){ clearInterval(_torPollT); _torPollT=0; } }
  function _torStartPoll(){
    _torStopPoll();
    // Self-cancelling: leaving the view (or the tab) kills it, so a forgotten interval cannot keep
    // polling a server every two seconds for the rest of the session.
    _torPollT=setInterval(()=>{
      if(S.VIEW!=='torrents' || _torTab!=='dl' || !document.getElementById('tm-list')){ _torStopPoll(); return; }
      /* NOT on the desktop app. Chromium reports `hidden` for a window that is merely COVERED by
       * another one (native occlusion), so putting any other window in front froze the progress bars
       * for as long as it stayed there — reported as "torrents on desktop, not updating progress if
       * not focused". A covered window is still an app someone is running, and a download you are
       * watching in another window is the whole reason to leave that view open. The desktop shell
       * already sets `backgroundThrottling: false`, so the timer really does keep its 2s rate.
       *
       * The check stays for a BROWSER TAB, where hidden means hidden and polling a server twice a
       * second for a page nobody can see is exactly the waste it was added to prevent. */
      if(document.visibilityState==='hidden' && !_isDesktopApp()) return;
      _torRefresh(false);
    }, 2000);
  }

  async function _torAddPrompt(){
    const v=await uiPrompt('Add a torrent', { placeholder:'magnet:?xt=… or a link to a .torrent' });
    const q=String(v||'').trim(); if(!q) return;
    toast('adding…');
    try{
      // The field is `torrent_url`, not `url` — a .torrent link sent as `url` is simply ignored and
      // the request adds nothing.
      await _torApi('/add',{method:'POST',body:JSON.stringify(
        q.startsWith('magnet:') ? { magnet:q } : { torrent_url:q })});
      toast('added');
      _torRefresh(true);
    }catch(err){ toast('could not add that: '+(err.detail||err.message)); }
  }

  async function renderTorrents(){
    const feed=$('#feed');
    /* The tab strip carries the tab's own action, right-aligned. It used to sit on a row of its own
     * below the tabs — a lone pill floating over the list, which read as something left behind
     * rather than a control belonging to the view. One header row, and the strip is already sticky. */
    const act = _torTab==='dl'
      ? `<button class="btn btn-neon small tor-act" id="tm-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-magnet"></use></svg>Add torrent</button>
         <button class="btn btn-ghost small icon-only tor-act" id="tm-refresh" title="Refresh now" aria-label="Refresh now"><svg class="ic b-ic" aria-hidden="true"><use href="#i-refresh"></use></svg></button>`
      : (S.GUEST || _torTab!=='nostr' ? '' : `<button class="btn btn-neon small tor-act" id="tor-add"><svg class="ic b-ic" aria-hidden="true"><use href="#i-magnet"></use></svg>Publish</button>`);
    const tabs=`<div class="notif-tabs tor-tabs">
        <button class="ntab${_torTab==='dl'?' on':''}" data-tt="dl">⬇ Downloads</button>
        <button class="ntab${_torTab==='nyaa'?' on':''}" data-tt="nyaa">🌸 Nyaa</button>
        <button class="ntab${_torTab==='tgx'?' on':''}" data-tt="tgx">🌌 TGX</button>
        <button class="ntab${_torTab==='nostr'?' on':''}" data-tt="nostr">🧲 Nostr</button>
        <span class="tor-sp"></span><span class="tor-acts">${act}</span></div>`;
    const bind=()=>{ $$('.tor-tabs .ntab',feed).forEach(b=> b.onclick=()=>{
      if(_torTab===b.dataset.tt) return; _torTab=b.dataset.tt; _torStopPoll(); renderTorrents(); }); };
    if(_torTab==='dl'){
      feed.innerHTML = tabs + '<div class="tm-list" id="tm-list"><div class="spinner"></div></div>';
      bind();
      { const a=$('#tm-add',feed); if(a) a.onclick=_torAddPrompt; }
      { const r=$('#tm-refresh',feed); if(r) r.onclick=()=>_torRefresh(true); }
      await _torRefresh(true);
      _torStartPoll();
      return;
    }
    _torStopPoll();
    if(_torTab==='nyaa' || _torTab==='tgx'){
      feed.innerHTML = tabs + _torBrowseShell(_torTab);
      bind();
      _torBrowseBind(feed, _torTab);
      return;
    }
    feed.innerHTML = tabs + '<div class="spinner"></div>';
    bind();
    await _renderTorrentsNostr(feed, tabs, bind);
  }

  /* NYAA and TGX — the same two sites the AI chat's `nyaa` and `torrents` commands read, through the
   * endpoints those commands' data already comes from (/api/torrent/nyaa, /catalog, /search). They
   * run on THIS node, through its Tor proxy, never from the browser.
   *
   * What a tab last showed lives in module state, not the DOM: #feed is shared by every view and is
   * blanked on entry, so going to Downloads to watch a torrent start and coming back finds the same
   * query and the same list, with no refetch. A response is only painted if it is still the latest
   * request for that tab — a slow search must not land on top of the one typed after it.
   *
   * Download adds the magnet to this node's client (the Downloads tab) and STAYS here: the button
   * turns into "✓ Added". Somebody browsing a list usually wants more than one thing from it, and
   * being thrown to another tab after every click would make that a chore. */
  const _TGX_CATS = [['movies','Movies'],['tv','TV'],['music','Music'],['anime','Anime']];
  const _torBrowse = {
    nyaa: { q:'', items:null, err:'', seq:0, added:Object.create(null) },
    tgx:  { q:'', cat:'movies', items:null, err:'', seq:0, added:Object.create(null) },
  };
  function _torBrowseShell(src){
    const st=_torBrowse[src];
    const cats = src==='tgx' ? `<div class="tb-cats" role="tablist">${_TGX_CATS.map(([k,l])=>
      `<button class="btn small ${!st.q && st.cat===k ? 'btn-neon' : 'btn-ghost'} tb-cat" data-cat="${k}">${l}</button>`).join('')}</div>` : '';
    return `<form class="tb-bar" id="tb-form" autocomplete="off">
        <input class="input" type="search" id="tb-q" enterkeyhint="search" value="${enc(st.q)}"
          placeholder="${src==='nyaa' ? 'Search nyaa.si' : 'Search TorrentGalaxy'}" aria-label="${src==='nyaa' ? 'Search nyaa.si' : 'Search TorrentGalaxy'}">
        <button class="btn btn-neon small" type="submit">Search</button>
      </form>${cats}
      <div class="tb-head muted small" id="tb-head"></div>
      <div class="tm-list" id="tb-list"><div class="spinner"></div></div>`;
  }
  function _torBrowseRow(src, t, i){
    const st=_torBrowse[src], added=!!st.added[t.magnet];
    return `<div class="tm-item tb-item" data-i="${i}">
      <div class="tm-head"><span class="tm-name tb-name" title="${enc(t.title)}">${enc(t.title)}</span></div>
      <div class="tm-meta">
        <span>${enc(t.size||'')}</span>
        <span title="seeders">⇡ ${Number(t.seeders)||0}</span>
        <span title="leechers">⇣ ${Number(t.leechers)||0}</span>
      </div>
      <div class="tm-acts">
        <button class="btn ${added?'btn-ghost':'btn-neon'} small tb-get"${added?' disabled':''}>${added?'✓ Added':'<svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Download'}</button>
        <button class="btn btn-ghost small tb-copy">⧉ Magnet</button>
      </div></div>`;
  }
  function _torBrowsePaint(feed, src){
    const st=_torBrowse[src], list=$('#tb-list',feed), head=$('#tb-head',feed);
    if(!list) return;
    const what = st.q ? `Results for “${st.q}”`
               : (src==='nyaa' ? 'Newest on nyaa.si' : 'Top '+((_TGX_CATS.find(c=>c[0]===st.cat)||[])[1]||'')+' on TorrentGalaxy');
    if(head) head.textContent = st.items ? what : '';
    if(st.err){ list.innerHTML=`<div class="empty">${enc(st.err)}</div>`; return; }
    if(!st.items){ list.innerHTML='<div class="spinner"></div>'; return; }
    list.innerHTML = st.items.length ? st.items.map((t,i)=>_torBrowseRow(src,t,i)).join('')
      : `<div class="empty">Nothing found${st.q?' for “'+enc(st.q)+'”':''}.</div>`;
    $$('.tb-item',list).forEach(row=>{
      const t=st.items[+row.dataset.i]; if(!t) return;
      row.querySelector('.tb-copy').onclick=()=>copyValue(t.magnet, 'magnet copied', 'Magnet link:');
      const get=row.querySelector('.tb-get');
      get.onclick=async()=>{
        if(st.added[t.magnet]) return;
        get.disabled=true;
        try{
          await _torApi('/add',{method:'POST',body:JSON.stringify({magnet:t.magnet})});
          st.added[t.magnet]=true;
          get.className='btn btn-ghost small tb-get'; get.textContent='✓ Added';
          toast('added to Downloads');
        }catch(err){
          get.disabled=false;
          toast(err.status===503 ? 'this server has no torrent client enabled'
                                 : 'could not add that: '+(err.detail||err.message));
        }
      };
    });
  }
  async function _torBrowseLoad(feed, src){
    const st=_torBrowse[src], seq=++st.seq;
    st.items=null; st.err=''; _torBrowsePaint(feed, src);
    const path = src==='nyaa' ? '/nyaa?limit=75'+(st.q?'&q='+encodeURIComponent(st.q):'')
               : (st.q ? '/search?limit=50&q='+encodeURIComponent(st.q) : '/catalog?limit=50&category='+st.cat);
    let items=null, err='';
    try{ const j=await _torApi(path); items=(j&&j.items)||[]; }
    catch(e){
      err = e.status===401 || e.status===403 ? 'You don\'t have access to torrents on this server.'
          : (e.detail || e.message || 'Could not reach '+(src==='nyaa'?'nyaa.si':'TorrentGalaxy')+'.');
    }
    if(seq!==st.seq) return;                          // a newer request owns this tab now
    st.items=items; st.err=err;
    if(S.VIEW==='torrents' && _torTab===src) _torBrowsePaint(feed, src);
  }
  function _torBrowseBind(feed, src){
    const st=_torBrowse[src];
    const form=$('#tb-form',feed), q=$('#tb-q',feed);
    if(form) form.onsubmit=ev=>{
      ev.preventDefault();
      const v=(q.value||'').trim();
      if(v===st.q && st.items && !st.err) return;
      st.q=v;
      // Searching TGX leaves the category row; an empty search goes back to it.
      $$('.tb-cat',feed).forEach(b=> b.className='btn small '+(!st.q && st.cat===b.dataset.cat ? 'btn-neon' : 'btn-ghost')+' tb-cat');
      _torBrowseLoad(feed, src);
    };
    $$('.tb-cat',feed).forEach(b=> b.onclick=()=>{
      if(!st.q && st.cat===b.dataset.cat && st.items && !st.err) return;
      st.cat=b.dataset.cat; st.q=''; if(q) q.value='';
      $$('.tb-cat',feed).forEach(x=> x.className='btn small '+(x===b ? 'btn-neon' : 'btn-ghost')+' tb-cat');
      _torBrowseLoad(feed, src);
    });
    if(st.items || st.err) _torBrowsePaint(feed, src);
    else _torBrowseLoad(feed, src);
  }

  async function _renderTorrentsNostr(feed, tabs, bind){
    let evs=[]; try{ evs=await Relay.query([{ kinds:[2003], limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(S.VIEW!=='torrents' || _torTab!=='nostr') return;
    const tors=evs.sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = tabs + (tors.length ? tors.map(torrentCard).join('') : '<div class="empty">No torrents found on the relay yet (NIP-35 · kind 2003).</div>');
    bind();
    { const ab=$('#tor-add',feed); if(ab) ab.onclick=addTorrent; }
    decorateProfiles();
    $$('.tor-card .name[data-prof]',feed).forEach(n=> n.onclick=()=>renderProfileView(n.dataset.prof));
    $$('.tor-copy',feed).forEach(b=> b.onclick=()=> copyValue(b.dataset.magnet, 'magnet copied', 'Magnet link:'));
    /* "Download here" hands the magnet to THIS node's torrent client and shows you the manager. The
     * magnet link beside it still exists for handing the torrent to your own app; before, that was
     * the only thing on offer, which on a phone means an app that may not be installed. */
    $$('.tor-get',feed).forEach(b=> b.onclick=async()=>{
      b.disabled=true;
      try{
        await _torApi('/add',{method:'POST',body:JSON.stringify({magnet:b.dataset.magnet})});
        toast('added — opening Downloads');
        _torTab='dl'; renderTorrents();
      }catch(err){
        b.disabled=false;
        toast(err.status===503 ? 'this server has no torrent client enabled'
                               : 'could not add that: '+(err.detail||err.message));
      }
    });
  }
  // In-app Admin: the standalone admin panel embedded in the client (same-origin, the Nostr-login
  // cookie authorizes it). Admins only.
  // Persistent admin iframe: created ONCE (hidden) and kept alive — opening Admin just reveals it.
  // The iframe is shown only after it has loaded (opacity 0→1 on onload, spinner until then), so you
  // never see a blank/half-rendered frame. Preloaded at startup (see _preloadAdmin) so the first
  // open from home/global is instant instead of timeline→spinner→blank-iframe→content (the flicker).
  /* Handing the admin panel its credential.
   *
   * The panel is a page from the instance, framed here. An iframe's DOCUMENT load carries only cookies,
   * so as long as the page was what got authorised, the panel needed a cookie — and in a bundled app
   * that cookie is cross-site (SameSite=None → Secure → HTTPS only), so against a .onion, which is
   * plain HTTP by design, no cookie could ever reach it. The panel simply could not work there.
   *
   * So the page asks US for the token instead, over postMessage. Nothing is in the URL, so no secret
   * lands in history, a Referer or a log; the reply goes to the frame's EXACT origin, which we know
   * because we built its src. Scheme-agnostic, so an onion instance works like any other.
   */
  let _adminFrameEl=null, _adminBridge=false;
  function _adminOrigin(){
    try{ return new URL(_adminFrameEl.src, location.href).origin; }catch(_){ return ''; }
  }
  function _sendAdminToken(){
    if(!_adminFrameEl || !S._aiToken) return;
    const o=_adminOrigin(); if(!o) return;
    try{ _adminFrameEl.contentWindow.postMessage({ type:'pc-admin-token', token:S._aiToken }, o); }catch(_){ }
  }
  function _bindAdminTokenBridge(){
    if(_adminBridge) return; _adminBridge=true;
    window.addEventListener('message', e=>{
      if(!_adminFrameEl || e.source!==_adminFrameEl.contentWindow) return;
      if(!e.data || e.data.type!=='pc-admin-hello') return;
      // It may have loaded before we had a token; _setAiToken pushes it when one arrives.
      _sendAdminToken();
    });
  }
  function _ensureAdminHost(){
    let host=document.getElementById('admin-host');
    if(!host){
      host=document.createElement('div'); host.id='admin-host'; host.style.display='none';
      host.innerHTML='<div class="spinner"></div>';
      // In the bundled app the iframe src is NOT rewritten by the fetch shim, so a bare '/admin' resolves to
      // https://localhost/admin → Capacitor's SPA fallback serves index.html → the admin frame showed the
      // main timeline. Load it from the server (cross-origin, cookie is SameSite=None so it authenticates;
      // /admin sets no X-Frame-Options so framing is allowed). PWA: __PC_API_BASE__ undefined → plain '/admin'.
      const ifr=document.createElement('iframe'); ifr.className='admin-frame'; ifr.src=(window.__PC_API_BASE__||'')+'/admin?t='+Date.now(); ifr.title='Admin'; ifr.style.opacity='0';
      // The frame is CROSS-ORIGIN (it comes from the instance, we are app://posterchan or the PWA), and
      // `clipboard-write` defaults to a `self` allowlist — so without this delegation every Copy button
      // in the panel has its writeText() rejected, which is what killed the .onion address copy in the
      // Windows app. The panel also carries its own execCommand fallback (admin.js copyToClipboard),
      // because a cleartext instance has no navigator.clipboard to permit in the first place.
      ifr.allow='clipboard-write';
      ifr.addEventListener('load', ()=>{ ifr.dataset.loaded='1'; ifr.style.opacity='1'; const sp=host.querySelector('.spinner'); if(sp) sp.remove(); });
      _adminFrameEl=ifr;
      _bindAdminTokenBridge();
      host.appendChild(ifr);
      (document.querySelector('.main')||document.body).appendChild(host);
    }
    return host;
  }
  /* Load /admin hidden so the first open is instant. NOT called at startup, and that now matters: the
   * frame asks us for a token as soon as it loads, and if we have none yet its own gate concludes
   * (after 2.5s) that nobody is signed in and paints "Admin sign-in needed" over itself. Today the
   * frame is only created from _adminFrame(), i.e. AFTER ensureAiSession() has produced a token, so
   * the answer is always ready. Wire this into startup and that stops being true — push the token on
   * arrival (see _setAiToken) and have the page re-check, or it will preload itself into a dead end. */
  function _preloadAdmin(){ _ensureAdminHost(); }
  /* The admin panel is still an IFRAME of the instance's own /admin page — but the PAGE is no longer
   * what gets authorised. It arrives unauthenticated (it holds no data, only fields) and we hand it
   * the bearer token over postMessage; its own requests carry it. That is what removed the cookie,
   * and with it the reason the panel could not work against a .onion: an iframe document load carries
   * only cookies, a bundled app's cookie is cross-site, and cross-site needs Secure, which needs
   * HTTPS. See _bindAdminTokenBridge above and static/js/admin-auth.js. */
  function _adminFrame(feed){
    // The iframe is created + loaded ONCE (post-auth, see _ensureAdminHost / _preloadAdmin) and kept
    // alive — re-entering admin just REVEALS it, never reloads it. (Reloading on every enter made the
    // panel slow + flickery and re-ran all its fetches.) After a deploy, a full page refresh picks up
    // new admin CSS/JS.
    const host=_ensureAdminHost();
    /* The host is a SIBLING overlay of the feed, and on the desktop the feed has been moved into a
     * window — so the host has to follow it there. Left parked in .main it renders underneath the
     * z-index:300 desktop: invisible, unclickable, and the window it was opened from just goes
     * blank, because the line below hides the feed. Re-parented on every open rather than once, so
     * it tracks the feed between windows and back to the classic layout. */
    const home = feed.parentElement;
    if(home && host.parentElement !== home) home.appendChild(host);
    feed.style.display='none';   // hide the feed; the persistent iframe fills the main area
    host.style.display='block';
    const ifr=host.querySelector('iframe');
    if(ifr && ifr.dataset.loaded==='1') ifr.style.opacity='1';   // already loaded → show instantly
  }
  function renderAdmin(opts){
    const feed=$('#feed');
    if(!S.IS_ADMIN){ feed.innerHTML='<div class="empty">Admins only.</div>'; return; }
    // /admin needs the session cookie nostr-login sets. If it's already established, render the
    // iframe SYNCHRONOUSLY (no await → no window for a re-render to clobber it). Otherwise show a
    // spinner and render when it resolves.
    if(S._aiAuth && S._aiAuth.is_admin){ _adminFrame(feed); return; }
    feed.innerHTML='<div class="spinner"></div>';
    const author=S.ME && S.ME.pubkey;
    ensureAiSession(opts).then(a=>{
      if(S.VIEW!=='admin' || !S.ME || S.ME.pubkey!==author) return;
      if(a && a.is_admin) _adminFrame(feed);
      else feed.innerHTML='<div class="empty">Admin session unavailable — log in with your admin Nostr key.</div>';
    }).catch(e=>{
      if(S.VIEW!=='admin' || !S.ME || S.ME.pubkey!==author) return;
      const why=(e && e.message)||'Could not establish your admin session';
      const extensionUnavailable=/receiving end does not exist|could not establish connection|extension context|context invalidated/i.test(why);
      feed.style.display='';
      const host=document.getElementById('admin-host'); if(host) host.style.display='none';
      feed.innerHTML='<div class="empty"><h2>'+(extensionUnavailable?'Signer extension unavailable':'Admin sign-in failed')+'</h2><p>'
        +(extensionUnavailable?'Open the PosterChan signer extension and try again. If it still cannot connect, disable and re-enable the extension in Firefox’s Add-ons Manager, then reload this page. Keep your existing pairing.':enc(why))
        +'</p><button class="btn btn-cyan" id="admin-session-retry">Retry admin sign-in</button></div>';
      const retry=$('#admin-session-retry'); if(retry) retry.onclick=()=>renderAdmin({force:true});
    });
  }
  /* `user@server` → that fediverse account's Nostr identity on this node (ActivityPub lookup; 404
   * on a node without it). Relative on purpose: the bundled apps' fetch shim sends /api/* to the
   * instance, exactly as it does for every other /api call here. */
  async function _fediLookup(acct){
    try{
      try{ await ensureAiSession(); }catch(_){}
      const f = (window.__PC && window.__PC.authFetch) || ((u,o)=>fetch(u,{credentials:'include',...(o||{})}));
      const r = await f('/api/activitypub/lookup?acct=' + encodeURIComponent(acct));
      if(!r.ok) return '';
      const j = await r.json();
      return (j && /^[0-9a-f]{64}$/.test(j.pubkey||'')) ? j.pubkey : '';
    }catch(_){ return ''; }
  }
  /* `opts` is the desktop's taskbar search (os.js `desktopSearch`), which shows this computer's own
   * results -- apps, Notes, the drive, files on the disk -- beside Nostr's, in the order System
   * Settings → Search chose. They arrive as two live elements: `head` goes above the Nostr results
   * and `tail` below them. They are ELEMENTS, not HTML, because each fills itself in as its own
   * answer lands (Notes decrypts, the disk walk returns) and carries its own click handlers, so every
   * repaint here MOVES them rather than rebuilding them. `nostr:false` is "Nostr search is switched
   * off": nothing is asked of any relay and the npub/nevent/NIP-05 jumps (Nostr lookups too) are
   * skipped. Every other caller passes nothing and gets exactly what it always did. */
  async function runSearch(q, opts){
    opts=opts||{};
    const head=opts.head||null, tail=opts.tail||null;
    S.VIEW='search'; _clearNav(); $('#view-title').textContent='Search';
    const feed=_feedScrollable();
    const place=(html)=>{ feed.innerHTML=html; if(head) feed.prepend(head); if(tail) feed.append(tail); };
    if(opts.nostr===false){ place(''); if(!head && !tail) feed.innerHTML='<div class="empty">Every search source is switched off — System Settings → Search.</div>'; return; }
    place('<div class="spinner"></div>');
    // People naturally type a handle as "@name@domain" — strip the leading @ so it matches the NIP-05
    // resolver below (else it falls through to full-text search for the literal string and finds nothing).
    q=q.replace(/^@+/, '').trim(); if(!q) return;
    // 1. direct npub/hex -> jump to that profile
    const pk=safePk(q); if(pk){ return renderProfileView(pk); }
    // 1b. note/nevent (optionally nostr:-prefixed) -> open that note's thread
    if(/^(?:nostr:)?(?:note1|nevent1)[0-9a-z]{20,}$/i.test(q)){
      try{ const d=NT().nip19.decode(q.replace(/^nostr:/i,'')); const id=d.type==='note'?d.data:(d.data&&d.data.id); if(id) return openThread(id); }catch(_){}
    }
    // 2. NIP-05 address (name@domain) -> resolve to a pubkey -> jump
    if(/^[\w.\-+]+@[\w.\-]+\.[a-z]{2,}$/i.test(q)){
      const rp=await nip05Resolve(q.toLowerCase());
      if(rp){ return renderProfileView(rp); }
      /* NOT A NOSTR ADDRESS? IT MAY BE A FEDIVERSE ONE. `alice@mastodon.social` fails NIP-05 (that
       * server has no nostr.json), and this node -- when its ActivityPub server is on -- can find the
       * account and give it the same Nostr identity the fediverse bridge gives everyone there. Its
       * profile then opens like any other, and following it is an ActivityPub Follow. A node without
       * the feature answers 404 and the search simply carries on as before. */
      const fedi = await _fediLookup(q);
      if(fedi){ return renderProfileView(fedi); }
    }
    // 3. posts via NIP-50 full-text (relay indexes note content); profiles by name/nip05 over the
    //    locally-cached profile set (the relay's FTS doesn't cover kind-0, so we match what we know).
    // Posts via NIP-50 FTS, and the Discover kinds (articles/streams/torrents/repos) fetched + filtered
    // client-side (FTS doesn't index them) — run in parallel.
    const ql=q.toLowerCase();
    /* WAIT FOR A SOCKET THAT CAN ANSWER FIRST. A REQ written to a socket that is not OPEN is dropped
     * (relay.js `_send`), and a ZOMBIE — one the proxy idle-closed while the browser still reports it
     * OPEN — accepts the REQ and answers nothing at all. Either way `query` resolves empty on its 6s
     * timer, and this screen then says "No matching posts" about a search the relay would have
     * answered: reported as "0 results for half-life, then closing it and redoing it a few times
     * showed results". Measured against this relay while fixing it: 39 matching notes, every time,
     * once a live socket exists. renderProfileView and flushEvents already wait like this. */
    try{ if(Relay.ready) await Relay.ready(4000); }catch(_){}
    if(S.VIEW!=='search') return;
    let [postEvs, addrEvs, profEvs] = await Promise.all([
      Relay.query([{ kinds:[1], search:q, limit:40 }]).catch(()=>[]),
      Relay.query([{ kinds:[30023,30311,2003,30617], limit:240 }]).catch(()=>[]),
      // Also ask the relay for matching PROFILES (NIP-50 kind-0 search) — otherwise "Profiles" only ever
      // shows what THIS device has already cached, so a profile you haven't seen (or lost on a cache
      // clear) never appears. Cached below so the local name/nip05 filter picks them up too.
      Relay.query([{ kinds:[0], search:q, limit:20 }]).catch(()=>[]),
    ]);
    /* THE SECOND SEARCH, DONE FOR YOU — this is "I have to search twice to see results".
     *
     * Waiting for a live socket above is not enough. The FIRST search of a session goes out while the
     * client is still taking the timeline's opening flood down the same connection, and the NIP-50
     * reply misses `query`'s timer. Measured against the live relay, 10 searches back to back from one
     * booted session: #1 came back unanswered with 0 posts, #2-#10 returned 40 every time. Nothing was
     * wrong with the query or the relay — it was early.
     *
     * The app already KNOWS this happened: `complete === false` is "no relay EOSE'd", which is
     * precisely the state the retry button was offered for. Offering a button is asking the user to do
     * by hand the one thing the code is certain is worth doing, so it does it itself — once, briefly
     * delayed to let the opening flood drain. The button stays for the case where the retry ALSO comes
     * back unanswered, which is a genuinely unreachable relay rather than a busy moment.
     *
     * Only ever REPLACES the result when the retry did better: an answered reply (even an empty one is
     * a real "nothing matches") beats an unanswered one, and a longer list beats a shorter. So this can
     * turn a wrong empty screen into results and can never turn results into an empty screen. */
    /* A single retry still missed in 1/5 cold live sessions while every warm/profile query in the
     * same session worked. Give the opening relay flood two bounded chances to drain. This is not
     * an endless retry loop: after three total attempts the honest manual Retry state remains. */
    for(let retry=0; postEvs && postEvs.complete === false && retry<2; retry++){
      await new Promise(r => setTimeout(r, 900 * (retry + 1)));
      if(S.VIEW!=='search') return;
      try{ if(Relay.ready) await Relay.ready(4000); }catch(_){}
      if(S.VIEW!=='search') return;
      try{
        const again = await Relay.query([{ kinds:[1], search:q, limit:40 }]);
        if(again && (again.complete !== false || again.length > postEvs.length)) postEvs = again;
      }catch(_){}
      if(S.VIEW!=='search') return;
    }
    postEvs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    addrEvs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    (profEvs||[]).forEach(e=>{ Store.saveProfile(e); });
    if(S.VIEW!=='search') return;
    const arts =_dedupAddr(addrEvs.filter(e=>e.kind===30023 && _matchAddr(e,ql))).sort((a,b)=>artTime(b)-artTime(a)).slice(0,12);
    const strms=_dedupAddr(addrEvs.filter(e=>e.kind===30311 && _matchAddr(e,ql))).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const tors =addrEvs.filter(e=>e.kind===2003 && _matchAddr(e,ql)).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const repos=_dedupAddr(addrEvs.filter(e=>e.kind===30617 && _matchAddr(e,ql))).sort((a,b)=>b.created_at-a.created_at).slice(0,12);
    const profs=Store.profileList().filter(p=>(((p.meta.name||'')+(p.meta.display_name||'')+(p.meta.nip05||'')).toLowerCase().includes(ql))).slice(0,12);
    let html='';
    if(profs.length){ html+='<div class="search-section-title">Profiles</div>'; for(const p of profs){ const m=p.meta; html+=`<div class="psearch" data-prof="${p.pubkey}"><img src="${enc(m.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'"><div><b>${emojiName(p.pubkey,m.name||m.display_name||'anon')}</b><div class="muted small">${enc(niceNip05(m.nip05)||(m.about||'').slice(0,60))}</div></div></div>`; } }
    if(arts.length){  html+='<div class="search-section-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-article"></use></svg>Articles</div>'+arts.map(articleCard).join(''); }
    if(strms.length){ html+='<div class="search-section-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-stream"></use></svg>Streams</div><div class="stream-grid">'+strms.map(streamCard).join('')+'</div>'; }
    if(tors.length){  html+='<div class="search-section-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-magnet"></use></svg>Torrents</div>'+tors.map(torrentCard).join(''); }
    if(repos.length){ html+='<div class="search-section-title"><svg class="ic b-ic" aria-hidden="true"><use href="#i-git"></use></svg>Git Repos</div><div class="repo-grid">'+repos.map(repoCard).join('')+'</div>'; }
    const posts=postEvs.sort((a,b)=>b.created_at-a.created_at);
    html+='<div class="search-section-title">Posts</div>';
    /* AN UNANSWERED SEARCH IS NOT AN EMPTY ONE. `complete` is false when the relays never EOSE'd —
     * a timeout, a socket still connecting, nothing live to ask — and telling somebody who typed a
     * word that there is nothing to find is the one answer that sends them away. Offer the retry
     * instead; it is what they were doing by hand. */
    const answered = postEvs.complete !== false;
    html+= posts.length ? `<div id="search-posts">${posts.map(feedNoteHtml).join('')}</div>`
         : (answered ? '<div class="empty">No matching posts.</div>'
                     : `<div class="empty">Your relays didn’t answer in time — this is not "nothing found".<br>
                          <button class="btn btn-cyan small" id="search-retry" style="margin-top:10px">Search again</button></div>`);
    place(html); hydrate(feed);
    { const rb=$('#search-retry',feed); if(rb) rb.onclick=()=>runSearch(q, opts); }
    $$('[data-prof]',feed).forEach(el=> el.onclick=()=>renderProfileView(el.dataset.prof));
    // Discover result cards → open the stream player.
    $$('.article-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const a=Store.get(c.dataset.id); if(a) openArticle(a); });
    $$('.stream-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const x=Store.get(c.dataset.id); if(x) openStream(x); });
    $$('.tor-copy',feed).forEach(b=> b.onclick=()=> copyValue(b.dataset.magnet, 'magnet copied', 'Magnet link:'));
    $$('.repo-clone',feed).forEach(b=> b.onclick=ev=>{ ev.stopPropagation(); copyValue(b.dataset.clone, 'clone URL copied', 'Clone URL:'); });
    $$('.repo-card a[href]',feed).forEach(a=> a.onclick=ev=>ev.stopPropagation());
    $$('.repo-card',feed).forEach(c=> c.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(c.dataset.pk); return; } const e=Store.get(c.dataset.id); if(e) openRepo(e); });
    // pagination cursor for scroll-back through more search hits
    _search = { q, loading:false, done:posts.length<40, oldest: posts.length ? posts[posts.length-1].created_at : 0 };
  }
  // scroll-back for NIP-50 search results (appends older matching posts under #search-posts)
  let _search = { q:'', oldest:0, loading:false, done:false };
  async function loadOlderSearch(){
    if(_search.loading || _search.done || !_search.q || !_search.oldest) return;
    const cont=$('#search-posts'); if(!cont){ _search.done=true; return; }
    _search.loading=true; const q=_search.q; const feed=$('#feed'); loadSentinel(feed);
    const until=_search.oldest;
    let evs=[], answered=true;
    try{ evs=await Relay.query([{ kinds:[1], search:q, until:until-1, limit:30 }]); answered = evs.complete !== false; }
    catch(_){ evs=[]; answered=false; }
    clearSentinel(feed);
    if(S.VIEW!=='search' || _search.q!==q){ _search.loading=false; return; }
    evs.sort((a,b)=>b.created_at-a.created_at);
    let minTs=until; const frag=document.createDocumentFragment();
    for(const ev of evs){
      Store.saveEvent(ev); needProfile(ev.pubkey);
      if(ev.created_at<minTs) minTs=ev.created_at;
      if(cont.querySelector('.note[data-id="'+ev.id+'"]')) continue;
      const div=document.createElement('div'); div.innerHTML=feedNoteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node);
    }
    invalidateCounts();
    if(frag.childElementCount){ cont.appendChild(frag); _capFeedDom(feed, cont); decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); }
    if(minTs<_search.oldest) _search.oldest=minTs;
    // Only an ANSWERED page can end the results. A timeout latching `done` is how a search stops
    // paging for the rest of its life on one slow moment — the same rule the timeline's `complete`
    // check exists for.
    if(answered && (!evs.length || minTs>=until)) _search.done=true;
    _search.loading=false;
  }
  // a feed of every post carrying a hashtag (NIP-12 `t` filter), with scroll-back pagination
  let _hashtag={ tag:'', oldest:0, loading:false, done:false };
  async function renderHashtag(tag){
    tag=String(tag||'').toLowerCase().replace(/^#/,''); if(!tag) return;
    S.VIEW='hashtag'; _hidePill(); _clearNav(); $('#view-title').textContent='#'+tag;
    cleanupInlineStream();
    const feed=$('#feed');
    _feedScrollable(feed);   // a normal scrolling view — clear every full-height modifier
    feed.innerHTML='<div class="spinner"></div>';
    // The relay's #t filter is case-SENSITIVE, but trending lowercases tags AND counts inline #hashtags —
    // so a post tagged "LillyPhillips" (or one that only writes #LillyPhillips in its text) trended yet the
    // exact-lowercase #t query returned nothing. Also pull a content SEARCH, then keep only posts that
    // genuinely use the tag: a case-insensitive `t` tag OR an inline #tag in the text (matches trending).
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], '#t':[tag], limit:60 }, { kinds:[1], search:tag, limit:80 }]); }catch(_){}
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(S.VIEW!=='hashtag') return;
    const _t=tag.replace(/[^a-z0-9_]/g,''), _rx=new RegExp('(^|\\s)#'+_t+'\\b','i');
    const posts=evs.filter(e=>e.kind===1 && ((e.tags||[]).some(t=>t[0]==='t'&&String(t[1]||'').toLowerCase().replace(/^#/,'')===tag) || _rx.test(e.content||'')))
                   .sort((a,b)=>b.created_at-a.created_at);
    feed.innerHTML = `<div class="search-section-title"># ${enc(tag)}</div>` +
      (posts.length ? `<div id="hashtag-posts">${posts.map(noteHtml).join('')}</div>` : `<div class="empty">No posts found for #${enc(tag)} yet.</div>`);
    hydrate(feed);
    _hashtag={ tag, loading:false, done:posts.length<60, oldest: posts.length?posts[posts.length-1].created_at:0 };
  }
  async function loadOlderHashtag(){
    if(_hashtag.loading || _hashtag.done || !_hashtag.tag || !_hashtag.oldest) return;
    const cont=$('#hashtag-posts'); if(!cont){ _hashtag.done=true; return; }
    _hashtag.loading=true; const tag=_hashtag.tag; const feed=$('#feed'); loadSentinel(feed);
    const until=_hashtag.oldest;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], '#t':[tag], until:until-1, limit:40 }]); }catch(_){}
    clearSentinel(feed);
    if(S.VIEW!=='hashtag' || _hashtag.tag!==tag){ _hashtag.loading=false; return; }
    evs.sort((a,b)=>b.created_at-a.created_at);
    let minTs=until; const frag=document.createDocumentFragment();
    for(const ev of evs){ Store.saveEvent(ev); needProfile(ev.pubkey); if(ev.created_at<minTs) minTs=ev.created_at;
      if(cont.querySelector('.note[data-id="'+ev.id+'"]')) continue;
      const div=document.createElement('div'); div.innerHTML=feedNoteHtml(ev); const node=div.firstElementChild; if(node) frag.appendChild(node); }
    invalidateCounts();
    if(frag.childElementCount){ cont.appendChild(frag); _capFeedDom(feed, cont); decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); }
    if(minTs<_hashtag.oldest) _hashtag.oldest=minTs;
    if(!evs.length || minTs>=until) _hashtag.done=true;
    _hashtag.loading=false;
  }
  // ---------- Trending (centre column): Hot + From-follows, as a real feed ------------------
  // Engagement ranking used to be a seven-row digest in the right rail. It's a timeline tab now, so it
  // gets the whole column: FULL note cards (reply/react/zap/quote inline, media, link cards) and
  // scroll-back that widens the ranking window instead of dead-ending at seven rows — and it works on a
  // phone, which the desktop-only rail never did.
  //   Hot          = every reaction/repost (kinds 6,7) on the relay, tallied per note.
  //   From follows = the same tally restricted to reactors you FOLLOW ("what my network is into").
  // Windows: start at 24h and double on each dry spell up to 30d, so scrolling keeps finding
  // older-but-hot posts rather than stopping.
  const TR_WIN0=24*3600, TR_WIN_MAX=30*24*3600, TR_PAGE=20, TR_QUERY_LIMIT=3000;
  let _tr={ tab:'hot', win:TR_WIN0, loading:false, done:false, exhausted:false, unreachable:false, shown:new Set(), queue:[], gen:0 };
  try{ const t=localStorage.getItem('trTab'); if(t==='hot'||t==='follows') _tr.tab=t; }catch(_){}
  // Rank notes by how many reactions/reposts point at them inside `windowSec`.
  // Returns [[noteId, count, icon],…] desc. `follows` restricts the REACTORS to the people you follow.
  /* WAIT FOR A SOCKET THAT CAN ANSWER, and say whether one did.
   *
   * A REQ written to a CONNECTING socket is silently dropped (relay.js `_send`), and the moment this
   * view is most likely to be opened against one is the moment somebody has just logged in and is
   * looking around. Reported as "logged in, nothing under Trending" — and it STAYS nothing, because
   * an empty answer widens the window, and a few widenings later `exhausted` latches and the view
   * gives up for the rest of the visit. renderProfileView and flushEvents already wait like this.
   *
   * `complete` is the other half. It is true only when every relay we asked sent an EOSE, so a
   * timeout — or no live socket at all — is "the relays never spoke", which says nothing about
   * whether anything is trending. Latching `exhausted` on that answer is what turns one slow moment
   * into an empty screen with no way back. The flag rides on the returned array, the way
   * Relay.query's own does. */
  async function trRank(windowSec, follows){
    const since=Math.floor(Date.now()/1000)-windowSec;
    const me=(S.ME&&S.ME.pubkey)||'';   // '' for a logged-out guest — FOLLOWS is empty there anyway
    const f={ kinds:[6,7], since, limit:TR_QUERY_LIMIT };
    if(follows){ const authors=[...S.FOLLOWS].filter(p=>p!==me); if(!authors.length){ const none=[]; none.complete=true; return none; } f.authors=authors; }
    let live=true; try{ if(Relay.ready) live = await Relay.ready(4000); }catch(_){ live=true; }
    let evs=[], complete=false;
    try{ evs=await Relay.query([f]); complete = live && evs.complete !== false; }catch(_){ evs=[]; complete=false; }
    const tally={}, icon={};
    for(const e of evs){
      if(follows && e.pubkey===me) continue;   // your own likes aren't "from your follows"
      // NIP-25: for kinds 6/7 the target is the LAST e tag, not the reply-marked one.
      const id=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]; if(!id) continue;
      tally[id]=(tally[id]||0)+1; if(e.kind===6) icon[id]='🔁'; else if(!icon[id]) icon[id]='❤️';
    }
    const rows = Object.entries(tally).sort((a,b)=>b[1]-a[1]).map(([id,c])=>[id,c,icon[id]||'❤️']);
    rows.complete = complete;
    return rows;
  }
  // One ranked row = a heat line + the ordinary feed card, so every interaction the timeline has works
  // here for free. '' when the note isn't renderable (not on this relay, not a kind 1, muted).
  function trItemHtml(id, count, icon){
    const ev=Store.get(id); if(!ev||ev.kind!==1||isMutedView(ev)) return '';
    const heat = _tr.tab==='follows' ? `${icon} ${count} from people you follow` : `🔥 ${count} reaction${count===1?'':'s'}`;
    return `<div class="tr-item"><div class="tr-heat">${heat}</div>${feedNoteHtml(ev)}</div>`;
  }
  function _trBarHtml(){
    const t=(k,label)=>`<button class="tr-tab${_tr.tab===k?' on':''}" data-tr="${k}" role="tab" aria-selected="${_tr.tab===k}">${label}</button>`;
    return `<div class="tr-bar" role="tablist">${t('hot','🔥 Hot')}${t('follows','🫂 From follows')}</div>`;
  }
  // Append the next page of ranked notes to #tl-notes. Returns how many cards were actually added.
  // Every await is followed by a generation check: a tab switch (or leaving the view) mid-query must
  // DROP its rows rather than paint them into the list the user is now looking at — the same hazard the
  // old rail solved with rbListEl(), and the reason both are gated instead of just re-querying the DOM.
  async function trLoadPage(){
    const gen=_tr.gen;
    let added=0, guard=0;
    while(added===0 && guard++<8){
      if(!_tr.queue.length){
        if(_tr.exhausted){ _tr.done=true; break; }   // already ranked the widest window and it gave nothing new
        const ranked=await trRank(_tr.win, _tr.tab==='follows');
        if(_tr.gen!==gen || S.VIEW!=='trending') return 0;
        _tr.queue=ranked.filter(x=>!_tr.shown.has(x[0]));
        /* AN UNANSWERED QUERY IS NOT AN EMPTY MONTH. Widening the window and latching `exhausted` on
         * it spends every window against a socket that is not talking yet, and then the view is done
         * for the visit. Stop instead, remember why, and leave the windows where they are — the next
         * entry (or a scroll) asks again, by which time the socket is up. */
        if(ranked.complete === false){ _tr.unreachable = true; break; }
        _tr.unreachable = false;
        if(_tr.win>=TR_WIN_MAX) _tr.exhausted=true; else _tr.win=Math.min(_tr.win*2, TR_WIN_MAX);
        if(!_tr.queue.length) continue;   // nothing new in this window → widen and try again
      }
      // Over-fetch candidates: a ranked id whose note isn't on this relay (or is muted) renders nothing,
      // so taking exactly TR_PAGE ids would yield short pages. Only CONSUMED ids leave the queue.
      const pick=_tr.queue.slice(0, TR_PAGE*3);
      await fetchNotes(pick.map(x=>x[0]));
      const feed=$('#feed');
      if(_tr.gen!==gen || S.VIEW!=='trending' || !feed) return 0;
      const box=_tlNotes(feed), frag=document.createDocumentFragment();
      let used=0;
      for(const [id,c,ic] of pick){
        used++; _tr.shown.add(id);
        if(box.querySelector('.note[data-id="'+id+'"]')) continue;   // a note can be hot in two windows
        const html=trItemHtml(id,c,ic); if(!html) continue;
        const d=document.createElement('div'); d.innerHTML=html; const node=d.firstElementChild; if(node) frag.appendChild(node);
        if(frag.childElementCount>=TR_PAGE) break;
      }
      _tr.queue=_tr.queue.slice(used);
      added=frag.childElementCount;
      if(added){
        // Clear the placeholder spinner / empty line AND the scroll-back sentinel first: loadSentinel
        // parks its spinner at the END of #tl-notes, so appending over it would drop the new page BELOW
        // a still-spinning loader.
        clearSentinel(feed);
        const sp=box.querySelector('.spinner'); if(sp) sp.remove();
        const em=box.querySelector('.empty'); if(em) em.remove();
        box.appendChild(frag);
        _capFeedDom(feed, box);
        invalidateCounts();
        decorateProfiles(); hydrateLinkCards(feed); hydrateCounts(); hydratePolls(feed); observeProfiles(feed);
      }
    }
    return added;
  }
  function renderTrending(){
    const feed=$('#feed'); if(!feed) return;
    _feedScrollable(feed);   // a normal scrolling list view
    _tr.gen++;   // invalidate any in-flight page from the previous entry / the other tab
    _tr.win=TR_WIN0; _tr.done=false; _tr.exhausted=false; _tr.unreachable=false; _tr.shown=new Set(); _tr.queue=[]; _tr.loading=true;
    // Same header as Home/Nostrverse (composer + tabs) and the same #tl-notes box, so the inline
    // composer, the ＋ FAB and loadSentinel() all behave exactly as they do on the other two tabs.
    feed.innerHTML = _timelineHeaderHtml() + _trBarHtml() + '<div id="tl-notes"><div class="spinner"></div></div>'
      + (S.ME && !S.GUEST ? '<button class="tl-fab" id="tl-fab" title="New post" aria-label="New post"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg></button>' : '');
    _bindTimelineHeader(feed);
    { const fb=$('#tl-fab',feed); if(fb) fb.onclick=()=>compose(); }
    $$('.tr-tab',feed).forEach(b=> b.onclick=()=>{ const t=b.dataset.tr; if(t===_tr.tab) return;
      _tr.tab=t; try{ localStorage.setItem('trTab',t); }catch(_){} renderTrending(); });
    feed.scrollTop=0;
    const gen=_tr.gen;
    trLoadPage().then(n=>{
      if(_tr.gen!==gen || S.VIEW!=='trending') return;
      _tr.loading=false;
      if(n) return;
      const box=_tlNotes($('#feed')); if(!box || box.querySelector('.note')) return;
      // "The relays didn't answer" and "nothing is trending" are different screens, and only one of
      // them is worth waiting on. The retry re-enters the view, which re-arms the windows too.
      box.innerHTML = _tr.unreachable
        ? `<div class="empty">Couldn’t reach your relays just now — nothing has been ranked yet.<br>
             <button class="btn btn-cyan small" id="tr-retry" style="margin-top:10px">Try again</button></div>`
        : `<div class="empty">${_tr.tab==='follows'
            ? (S.FOLLOWS.size ? 'Nothing from your follows yet — they haven’t liked or boosted anything this month.' : 'Follow people to see what they’re into.')
            : 'Nothing trending yet. Check back once there are a few reactions about.'}</div>`;
      { const rb=$('#tr-retry'); if(rb) rb.onclick=()=>renderTrending(); }
    });
  }
  async function loadMoreTrending(){
    if(_tr.loading || _tr.done || S.VIEW!=='trending') return;
    _tr.loading=true;
    const feed=$('#feed'); if(feed) loadSentinel(feed);
    try{ await trLoadPage(); }
    finally{ const f=$('#feed'); if(f) clearSentinel(f); _tr.loading=false; }
  }

  return {
    _sendAdminToken, loadMoreTrending, loadOlderHashtag, loadOlderSearch, openArticle, renderAdmin,
    renderArticles, renderHashtag, renderTorrents, renderTrending, runSearch,
  };
};

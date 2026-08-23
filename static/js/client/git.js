/* Git (NIP-34) — repo discovery, the repo view, issues/patches, and the self-hosted GRASP browser.
 *
 * THE FIRST SLICE OFF app.js, which is ~30k lines in a single IIFE. The point of the exercise is
 * that a module can be read, tested and changed without holding the rest of the client in your
 * head; the point of THIS file is to establish the pattern the rest will follow.
 *
 * A FACTORY, NOT A GLOBAL GRAB. app.js calls `PCGitFactory(deps)` and passes in exactly what this
 * code needs. Three reasons, and the third is why it beats reaching for `window.__PC`:
 *
 *   1. The extracted code is BYTE-IDENTICAL to what it replaced, apart from the live-state rewrites
 *      below. Nothing was re-indented, renamed or "tidied", so `git log -p` shows a move rather than
 *      a rewrite, and a bug found here is a bug that was already here.
 *   2. The dependency list is explicit and checkable. It came from a real parser walking scopes —
 *      an earlier hand-rolled regex pass reported 13 dependencies for this block and the true number
 *      is 40, because a `//` inside a template literal (every URL) ate the rest of its line.
 *   3. `__PC` is assigned at the very END of app.js, long after this file is parsed. A module that
 *      captured helpers off it at load time would capture undefined; one that reached for it per
 *      call would work but would hide which 40 things this file actually depends on.
 *
 * LIVE STATE COMES THROUGH `S`. Five of the dependencies — CFG, ME, VIEW, GUEST, LOGO — are `let`
 * bindings that app.js REASSIGNS (CFG is replaced wholesale once /client/config lands, ME on every
 * login). Passing their values would freeze this module at boot, so app.js passes a getter object
 * and the 57 references to them read `S.ME`, `S.VIEW` and so on. Those 57 rewrites were made by the
 * parser at exact identifier offsets, not by search-and-replace, so a `ME` inside a string or a
 * property name could not be caught by mistake.
 *
 * WHAT IT HANDS BACK is the five names the rest of app.js calls into. `_ISSUES_REPO` is NOT one of
 * them: it is a plain constant read from app.js too, and a value cannot be shimmed the way a
 * function can, so it stays declared over there and is passed in.
 */
window.PCGitFactory = function(dep){
  'use strict';
  const S = dep.state;                       // live: S.CFG, S.ME, S.VIEW, S.GUEST, S.LOGO
  const {
    $, $$, NT, _ISSUES_REPO, _blossomDenied, _clearNav, _dedupAddr, _fmtBytes, _guestPrompt,
    _mdUrl, _navUrl, _serverOrigin, _webLink, closeModal, copyValue, decorateProfiles, enc,
    attachMentionAutocomplete, imetaTagsFor, mdToHtml, mediaParts, mentionTags, modal, needProfile, openLightbox, openMenuPopover,
    openThread, profOf, publish, renderProfileView, requestBlossomAccess, sign, switchView,
    timeAgo, toast, uiConfirm, uiPrompt, uploadBlob,
  } = dep;

  // ---------- git repos (NIP-34, kind 30617 repository announcements) ----------
  // Everything a repo can be searched by: its name, slug, description, clone/web URLs — and the
  // owner, since "who published it" is how you look for a repo you didn't name yourself.
  function _repoHaystack(e){
    const p=profOf(e.pubkey)||{};
    const t=k=>e.tags.filter(x=>x[0]===k).map(x=>x.slice(1).join(' ')).join(' ');
    return [t('name'),t('d'),t('description'),t('clone'),t('web'),
            p.name||'',p.display_name||'',p.nip05||''].join(' ').toLowerCase();
  }
  // ---- keyboard / vim navigation for the Git views (repos list + repo detail) ----
  // One document-level handler, bound once and gated on VIEW, so the Git views are fully drivable
  // from the keyboard: j/k move, o/Enter open, g/G jump, / search, n new, 1-5 switch detail tabs,
  // h/Esc back. It NEVER hijacks keys while a text field is focused (except Enter/Esc in the search).
  let _kbSel=-1, _gitKbBound=false;
  // Which slice of the relay's repos the list shows. YOUR repos are the default: the Git view is a
  // workspace before it is a directory, and the relay carries every announcement the web of trust has
  // ever federated in — so "all" buries the three repos you actually push to under other people's.
  // Falls back to 'all' when you're signed out or own none, otherwise the default view is empty.
  // Sticky for the session (not persisted), so switching to 'all' survives leaving and re-entering.
  let _repoScope='mine';
  /* ⭐ STARS — a NIP-51 bookmark set (kind 30003, d:'git-repos') of 30617 `a` coordinates, the
   * idiomatic Nostr list any list-aware client can read. Replaceable, so the two standing rules for
   * replaceable lists apply with no exceptions: NEVER write after a failed read (an empty read
   * written back is the follows-wipe), and serialize writes (a chain, like Budget's). `_stars`
   * stays null until a read SUCCEEDS — and a null set means the star buttons are read-only. */
  const STARS_D='git-repos';
  /* TWO SOURCES, ONE UNION — measured, not assumed: a real event on this relay shows gitworkshop
   * writing repo bookmarks into the STANDARD NIP-51 bookmarks list (kind 10003, a-tags of 30617
   * coordinates), while ours live in a 30003 set. Starred shows the union; WRITES only ever touch
   * our own set — a write into somebody's 10003 is one failed read away from wiping the rest of
   * their bookmarks, which is the replaceable-list lesson this codebase keeps paying for. */
  let _stars=null, _starsMine=null, _starsBk=new Set(), _starChain=Promise.resolve();
  let _starsGw=new Set(), _stars10=new Set(), _gwCur=null;   // gitworkshop's set: coords, 10003-only coords, raw newest
  let _starsRx=new Map();   // repo-star REACTIONS (gitworkshop's actual format): coord -> {id, on}
  async function _loadStars(){
    if(!S.ME || !S.ME.pubkey) return;
    try{
      const evs=await Relay.query([{ kinds:[30003], authors:[S.ME.pubkey], '#d':[STARS_D, 'git-repo-bookmark'], limit:10 },
                                   { kinds:[10003], authors:[S.ME.pubkey], limit:5 },
                                   { kinds:[7], authors:[S.ME.pubkey], limit:500 }]);
      const pick=(kind,d)=> (evs||[]).filter(e=>e.kind===kind && (d===undefined || (((e.tags||[]).find(t=>t[0]==='d')||[])[1]||'')===d))
                                     .sort((a,b)=>b.created_at-a.created_at)[0];
      const coords=(ev)=> ev?(ev.tags||[]).filter(t=>t[0]==='a'&&/^30617:/.test(t[1]||'')).map(t=>t[1]):[];
      _starsMine=new Set(coords(pick(30003, STARS_D)));
      /* gitworkshop's set (kind 30003, d:'git-repo-bookmark' — measured off a real write relay)
       * is READ AND WRITTEN: stars made here must show on the ngit site too, and its site-side ⭐
       * publishes nothing unless that tab can sign, so ours is the reliable direction. The raw
       * newest version is kept for read-modify-write (carry its tags, edit one coordinate).
       * Kind 10003 (general bookmarks) stays read-only — that list belongs to other features. */
      _gwCur=pick(30003, 'git-repo-bookmark')||null;
      _starsGw=new Set(coords(_gwCur));
      _stars10=new Set(coords(pick(10003, undefined)));
      /* MEASURED against the live site (2026-08-18, instrumented window.nostr): gitworkshop's Star
       * button signs a KIND-7 REACTION a-tagging the 30617 — not a list. Three of the user's "lost"
       * stars were sitting in the store as '+' reactions the whole time. Newest reaction per repo
       * wins ('-' or a deletion is an unstar); the event id is kept so OUR unstar can publish the
       * NIP-09 delete gitworkshop itself honours. */
      _starsRx=new Map();
      for(const ev of (evs||[]).filter(e=>e.kind===7).sort((a,b)=>a.created_at-b.created_at)){
        for(const t of (ev.tags||[])){
          if(t[0]!=='a' || !/^30617:/.test(t[1]||'')) continue;
          _starsRx.set(t[1], { id: ev.id, on: (ev.content||'+') !== '-' });
        }
      }
      const rxOn=[..._starsRx.entries()].filter(([,v])=>v.on).map(([a])=>a);
      _starsBk=new Set([..._stars10, ..._starsGw, ...rxOn]);
      _stars=new Set([..._starsMine, ..._starsBk]);
    }catch(_){ /* _stars stays as it was — possibly null, which keeps the buttons read-only */ }
  }
  function _starred(e){ return !!(_stars && _stars.has(_repoAddr(e))); }
  function toggleStar(e){
    if(!S.ME || !S.ME.pubkey){ _guestPrompt(); return Promise.resolve(false); }
    if(_stars===null || _starsMine===null){ toast('still loading your starred list — try again in a second'); return Promise.resolve(false); }
    const addr=_repoAddr(e), on=!_stars.has(addr);
    if(!on){
      // A reaction-star (gitworkshop's format, ours to delete — the user signed it): NIP-09 it.
      const rx=_starsRx.get(addr);
      if(rx && rx.on){ _starsRx.set(addr, { id: rx.id, on:false });
        _starChain=_starChain.catch(()=>{}).then(()=>publish(5, 'unstarred', [['e', rx.id],['k','7']]).catch(()=>{})); }
    }
    if(!on && !_starsMine.has(addr) && !_starsGw.has(addr) && !(_starsRx.get(addr)||{}).on && _stars10.has(addr)){
      // A general 10003 bookmark made in another app: that list belongs to other features here, so
      // we will not write it — and therefore cannot remove from it.
      toast('this star lives in your Nostr bookmarks (made in another client) — remove it there');
      return Promise.resolve(true);
    }
    if(on){ _starsMine.add(addr); _starsGw.add(addr); }
    else { _starsMine.delete(addr); _starsGw.delete(addr); }
    const _rxOn=()=>[..._starsRx.entries()].filter(([,v])=>v.on).map(([a])=>a);
    _starsBk=new Set([..._stars10, ..._starsGw, ..._rxOn()]);
    _stars=new Set([..._starsMine, ..._starsBk]);
    _starChain=_starChain.catch(()=>{}).then(async()=>{
      const tags=[['d',STARS_D],['title','Git repos']].concat([..._starsMine].map(a=>['a',a]));
      const r=await publish(30003, '', tags);
      if(r && r.ok===false){ if(on){ _starsMine.delete(addr); _starsGw.delete(addr); }
        else { _starsMine.add(addr); _starsGw.add(addr); }
        _starsBk=new Set([..._stars10, ..._starsGw, ..._rxOn()]);
        _stars=new Set([..._starsMine, ..._starsBk]);
        toast('the relay didn’t store your star — nothing changed'); return; }
      /* MIRROR INTO GITWORKSHOP'S SET, read-modify-write: carry every non-a/non-d tag of the newest
       * version we read, replace only the coordinates. Publishing a fresh list when none existed is
       * the point (the ngit site reads it); a failed mirror keeps the star here and retries on the
       * next toggle — never a rollback, ours is the source of truth. */
      try{
        const carry=(_gwCur&&_gwCur.tags||[]).filter(t=>t&&t[0]!=='a'&&t[0]!=='d'&&t[0]!=='client');
        const gtags=[['d','git-repo-bookmark']].concat(carry).concat([..._starsGw].map(a=>['a',a]));
        const g=await publish(30003, (_gwCur&&_gwCur.content)||'', gtags);
        if(g && g.ok!==false) _gwCur={ content:(_gwCur&&_gwCur.content)||'', tags:gtags, created_at:Math.floor(Date.now()/1000) };
      }catch(_){}
    });
    return Promise.resolve(on);
  }
  // Where a repo's code actually lives — the clone URL's host. In "All repos" this is the single most
  // useful fact about a stranger's announcement (a relay federates in repos hosted anywhere), and it's
  // what separates "I can browse this here" from "this is a pointer to someone else's server".
  function _repoHostname(e){
    const c=(e.tags.find(t=>t[0]==='clone')||[])[1]||'';
    try{
      const u=new URL(c);
      // http(s) only. Other schemes in the wild (htree://<npub>/<repo>, nostr://…) put an npub or a
      // relay token where the host goes, and rendering that as a "host" is just a wall of base32.
      if(u.protocol!=='http:' && u.protocol!=='https:') return '';
      return u.hostname.replace(/^www\./,'');
    }catch(_){ return ''; }
  }
  /* NO GIT HOST HERE MEANS NOTHING IS HOSTED HERE. The old fallback to `self.location.origin` meant
   * a node with no git host still claimed every repo whose clone URL happened to share its
   * hostname — and in a bundled app it compared against `app://posterchan`, so nothing ever
   * matched. Both directions were wrong; the question only has an answer when there IS a host. */
  function _repoHostedHere(e){
    const h=_repoHostname(e); if(!h) return false;
    const base=_gitHostBase(); if(!base) return false;
    try{ return h===new URL(base).hostname; }catch(_){ return false; }
  }
  /* Does this clone URL have the shape the browse routes need — `…/<owner npub|hex>/<id>.git`? */
  function _graspShaped(cloneUrl){
    try{
      const sg=new URL(cloneUrl).pathname.split('/').filter(Boolean);
      const gi=sg.findIndex(s=>s.endsWith('.git'));
      return gi>0 && (/^npub1/.test(sg[gi-1])||/^[0-9a-fA-F]{64}$/.test(sg[gi-1]));
    }catch(_){ return false; }
  }
  /* CAN THIS NODE BROWSE THIS REPO'S FILES — the question the repo view has to answer before it
   * offers a Files tab, a Commits tab and a branch switcher.
   *
   * It used to be the SHAPE alone, and the shape is not the question. Every GRASP forge on nostr
   * uses `…/<npub>/<repo>.git` — relay.ngit.dev, gitnostr.com, git.gittr.space, git.shakespeare.diy,
   * pyramid.fiatjaf.com — and they are the majority of what the relay lists. So opening any of them
   * asked THIS node's git host for a repo it has never held: measured against poster.place, /refs
   * and /tree answer 404 in 20ms ("branches unavailable", "Couldn't list files", "Couldn't read the
   * commit history") while /readme spends 8-9 SECONDS timing out against a host that is not a forge
   * — nine seconds of spinner ending in a page with nothing on it, which is what "clicking on a git
   * repo gets stuck and never loads" is. Worse than useless: had our host held an unrelated repo at
   * the same `<npub>/<id>`, we would have shown ITS files under a stranger's name.
   *
   * So a repo is browsable here only when it is hosted here. Everything else keeps its README, its
   * issues, its patches and its clone URL — the parts that are really about the repo and not about
   * where it happens to live. */
  function _repoBrowsableHere(e){
    const c=(e&&e.tags||[]).find(t=>t[0]==='clone');
    const url=(c||[])[1]||'';
    return _graspShaped(url) && _repoHostedHere(e);
  }
  // Yours = you announced it, OR you're in its maintainers tag — the same set that can push to it.
  function _repoIsMine(e){
    if(!S.ME||!S.ME.pubkey) return false;
    if(e.pubkey===S.ME.pubkey) return true;
    return (e.tags||[]).some(t=>t[0]==='maintainers' && t.slice(1).includes(S.ME.pubkey));
  }
  function _kbCards(feed){ return $$('.repo-card',feed); }
  let _repoEvents = new Map();
  function _kbPaint(feed){
    const cards=_kbCards(feed);
    cards.forEach((c,i)=>c.classList.toggle('kb-sel', i===_kbSel));
    const c=cards[_kbSel]; if(c) c.scrollIntoView({block:'nearest'});
  }
  function _kbMove(feed,d){
    const n=_kbCards(feed).length; if(!n) return;
    _kbSel = _kbSel<0 ? (d>0?0:n-1) : Math.max(0, Math.min(n-1, _kbSel+d));
    _kbPaint(feed);
  }
  // Columns in the RESPONSIVE repo grid = the number of cards sharing the first row's offsetTop. j/k
  // (and ↑/↓) move a whole ROW by this stride, h/l (←/→) move one card — so movement matches the 2-D
  // grid instead of marching through flat DOM order (which made every key look like it went one way).
  function _kbCols(feed){
    const c=_kbCards(feed); if(c.length<2) return 1;
    // Cards on the first ROW share ~the same offsetTop. Use a TOLERANCE, not strict ===: the desktop
    // (Electron) app scales the page with body{zoom}, which makes same-row offsetTop values differ by a
    // sub-pixel rounding under Chromium — strict equality then saw "1 column" and every key moved a
    // single card (the "arrows go one direction" bug in the Windows app that Firefox, no zoom, dodged).
    const top=c[0].offsetTop, tol=Math.max(6,(c[0].offsetHeight||0)/2); let n=1;
    for(let i=1;i<c.length;i++){ if(Math.abs(c[i].offsetTop-top)<tol) n++; else break; }
    return Math.max(1,n);
  }
  function _typingInField(el){
    if(!el) return false;
    const t=(el.tagName||'').toLowerCase();
    return t==='input' || t==='textarea' || t==='select' || el.isContentEditable;
  }
  function _gitKeydown(ev){
    if(S.VIEW!=='repos' && S.VIEW!=='repo') return;
    // Don't act while a dialog/overlay is open over the Git view (a modal, confirm, lightbox, picker).
    if(document.querySelector('.modal-bg,.uiconfirm-bg,.lightbox,.emoji-pop,.menu-pop,.pop-backdrop')) return;
    if(ev.altKey||ev.ctrlKey||ev.metaKey) return;   // leave OS/browser chords alone
    const feed=$('#feed'); if(!feed) return;
    // ----- repos LIST -----
    if(S.VIEW==='repos'){
      const q=$('#repo-q',feed);
      if(_typingInField(document.activeElement)){
        // In the search box: Enter jumps to the results, Escape (input's own handler) clears. Type freely.
        if(ev.key==='Enter'){ ev.preventDefault(); if(_kbCards(feed).length){ _kbSel=0; _kbPaint(feed); } if(q) q.blur(); }
        return;
      }
      switch(ev.key){
        case 'j': case 'ArrowDown': ev.preventDefault(); _kbMove(feed, _kbCols(feed)); break;   // down a row
        case 'k': case 'ArrowUp':   ev.preventDefault(); _kbMove(feed, -_kbCols(feed)); break;   // up a row
        case 'l': case 'ArrowRight':ev.preventDefault(); _kbMove(feed, 1); break;                // next card
        case 'h': case 'ArrowLeft': ev.preventDefault(); _kbMove(feed, -1); break;               // prev card
        case 'g': ev.preventDefault(); _kbSel=0; _kbPaint(feed); break;
        case 'G': ev.preventDefault(); _kbSel=_kbCards(feed).length-1; _kbPaint(feed); break;
        case 'o': case 'Enter': { ev.preventDefault(); const c=_kbCards(feed)[_kbSel]; if(c){ const e=_repoEvents.get(c.dataset.id)||Store.get(c.dataset.id); if(e) openRepo(e); } break; }
        case '/': ev.preventDefault(); if(q){ q.focus(); if(q.select) q.select(); } break;
        case 'n': ev.preventDefault(); publishRepo(); break;
        case 'Escape': if(_kbSel>=0){ _kbSel=-1; _kbPaint(feed); } break;
      }
      return;
    }
    // ----- repo DETAIL -----
    if(_typingInField(document.activeElement)) return;   // don't fight the issue/edit forms
    const tabs=$$('.rv-tab',feed);
    switch(ev.key){
      case 'h': case 'Escape': ev.preventDefault(); switchView('repos'); break;
      case '1': case '2': case '3': case '4': case '5': {
        const i=(+ev.key)-1; if(tabs[i]){ ev.preventDefault(); tabs[i].click(); } break;
      }
    }
  }
  function _gitKbBind(){
    if(_gitKbBound) return;
    _gitKbBound=true;
    // CAPTURE phase: run before any other handler / the shell, so the desktop (Electron) app can't
    // swallow j/k/arrows before they reach us — the reason they worked in Firefox but not the app.
    document.addEventListener('keydown', _gitKeydown, true);
  }
  async function renderRepos(){
    const feed=$('#feed'); feed.innerHTML='<div class="spinner"></div>';
    _kbSel=-1; _gitKbBind();   // reset keyboard selection on (re)entry; bind the vim-style handler once
    // 80 was fine for a grid you scroll. Once it is a SEARCHABLE list the cap becomes a correctness
    // problem: searching a truncated set reports "no match" for a repo that is really on the relay.
    // Ask for the relay's MAXIMUM (store.py clamps to 5000) rather than omitting the field: a filter
    // with no limit is read as `limit or 500`, so leaving it out is the 500 cap, not the absence of one.
    let evs=[]; try{ evs=await Relay.query([{ kinds:[30617], limit:5000 }]); }catch(_){}
    /* Stars are re-read on EVERY entry, not latched for the page: a star made in another app (the
     * ngit website) or on another device landed on the relay and this view kept answering from the
     * set it loaded at first open — "i starred a repo on ngit again and still does not appear".
     * The first load still blocks (a Starred chip that flashes empty is worse); after that the
     * refresh happens on entry and repaints only if something changed. */
    if(_stars===null) await _loadStars();
    else _loadStars().then(()=>{ try{ if(S.VIEW==='repos') paint(); }catch(_){} });
    evs.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); });
    if(S.VIEW!=='repos') return;
    const repos=_dedupAddr(evs).sort((a,b)=>b.created_at-a.created_at);
    _repoEvents = new Map(repos.map(e=>[e.id,e]));
    const mine=repos.filter(_repoIsMine);
    if(!mine.length) _repoScope='all';           // never open on an empty view
    if(_repoScope==='starred' && (!_stars || !_stars.size)) _repoScope = mine.length?'mine':'all';
    const scoped=()=>_repoScope==='mine'?mine:(_repoScope==='starred'?repos.filter(_starred):repos);
    const grid=r=>`<div class="repo-grid">${r.map(repoCard).join('')}</div>`;
    feed.innerHTML = `<div class="art-top repo-top">
        ${_gitHostBase()?`<button class="btn btn-neon small" id="repo-create"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Create repo</button>`:''}
        <button class="btn ${_gitHostBase()?'btn-ghost':'btn-neon'} small" id="repo-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>Announce a repo</button>
        ${mine.length?`<div class="repo-scope" role="tablist">
          <button class="repo-sc${_repoScope==='mine'?' on':''}" data-scope="mine" role="tab">Mine</button>
          <button class="repo-sc${_repoScope==='starred'?' on':''}" data-scope="starred" role="tab">\u2b50 Starred</button>
          <button class="repo-sc${_repoScope==='all'?' on':''}" data-scope="all" role="tab">All repos</button>
        </div>`:''}
        ${repos.length>1?`<input class="input repo-search" id="repo-q" type="search" autocomplete="off" placeholder="🔍 Search repos — name, owner, description…">`:''}
      </div>
      <div id="repo-results"></div>`;
    $('#repo-new').onclick=()=>publishRepo();
    { const cb=$('#repo-create'); if(cb) cb.onclick=()=>createRepo(); }
    // Card wiring is re-applied after every filter render, since filtering replaces the cards.
    const wire=()=>{
      decorateProfiles();
      $$('.repo-card .name[data-prof]',feed).forEach(n=> n.onclick=ev=>{ ev.stopPropagation(); renderProfileView(n.dataset.prof); });
      $$('.repo-clone',feed).forEach(b=> b.onclick=ev=>{ ev.stopPropagation(); copyValue(b.dataset.clone, 'clone URL copied', 'Clone URL:'); });
      $$('.repo-share',feed).forEach(b=> b.onclick=ev=>{ ev.stopPropagation(); copyValue(b.dataset.share, 'project link copied', 'Project link:'); });
      $$('.repo-card a[href]',feed).forEach(a=> a.onclick=ev=>ev.stopPropagation());   // ↗ Open must not also open the detail
      $$('.repo-star',feed).forEach(b=> b.onclick=async ev=>{ ev.stopPropagation();
        const e=Store.get(b.dataset.id); if(!e) return;
        const on=await toggleStar(e);
        b.textContent=_starred(e)?'\u2b50':'\u2606';
        b.title=_starred(e)?'Unstar':'Star';
        if(_repoScope==='starred') paint();     // unstarring while looking at Starred removes the card
      });
      $$('.repo-card',feed).forEach(c=> c.onclick=ev=>{ ev.stopPropagation(); const e=_repoEvents.get(c.dataset.id)||Store.get(c.dataset.id); if(e) openRepo(e); else toast('this repository is no longer available — refresh Git'); });
    };
    const q=$('#repo-q',feed);
    // ONE renderer for both the scope chips and the search box: they filter the same list, and two
    // renderers would let a scope switch quietly drop the active search (or vice versa).
    // EVERY space-separated term must match, so "posterchan ai" narrows rather than widens.
    // Re-rendering the grid (instead of hiding cards) keeps the empty state honest.
    const paint=()=>{
      const base=scoped();
      const terms=((q&&q.value)||'').toLowerCase().split(/\s+/).filter(Boolean);
      const hits=terms.length ? base.filter(e=>{ const h=_repoHaystack(e); return terms.every(t=>h.includes(t)); }) : base;
      $('#repo-results',feed).innerHTML = hits.length ? grid(hits)
        : (terms.length ? `<div class="empty">No repo matches “${enc(q.value)}”${_repoScope==='mine'?' in your repos — try “All repos”.':'.'}</div>`
           : `<div class="empty">No git repos found on the relay yet (NIP-34 · kind 30617). ${_gitHostBase()?'Create one ↑':'Announce yours ↑'}</div>`);
      if(q) q.placeholder=`🔍 Search ${base.length} repo${base.length===1?'':'s'} — name, owner, description…`;
      wire();
    };
    paint();
    $$('.repo-sc',feed).forEach(b=> b.onclick=()=>{
      _repoScope=b.dataset.scope;
      $$('.repo-sc',feed).forEach(x=>x.classList.toggle('on', x.dataset.scope===_repoScope));
      _kbSel=-1;                       // the card under the keyboard cursor is gone; don't keep its index
      paint();
    });
    if(q){
      q.oninput=paint;
      q.onkeydown=ev=>{ if(ev.key==='Escape'){ q.value=''; paint(); } };
    }
  }
  // The GRASP git host base for creating a repo from THIS node: the operator's public_base if set, else
  // (on a git-PROXY node — e.g. the one serving /client — that has no local base) this client's own
  // origin + /git. '' when the node neither hosts nor proxies git → the Create button stays hidden.
  /* WHERE THIS NODE'S OWN GIT HOST LIVES — and the fallback must be the INSTANCE, not the page.
   *
   * `self.location.origin` is right in a browser tab and WRONG in every packaged build: the desktop
   * app and the APK serve the client from their own bundle, so the origin is `app://posterchan`.
   * That made this answer `app://posterchan/git`, which is nobody's git host — so `_repoHostedHere`
   * said no to the node's OWN repos (the "hosted here" badge never appeared in the app) and
   * `createRepo` built `app://posterchan/git/<npub>/<id>.git` as a clone URL and signed a NIP-98
   * token for it. `_serverOrigin()` is the instance the app is actually talking to, and it is empty
   * with no instance — which is the truthful answer there, since a node that is not there hosts
   * nothing. */
  function _gitHostBase(){
    if(S.CFG.git_host_base) return String(S.CFG.git_host_base).replace(/\/+$/,'');
    if(S.CFG.git_create_available){ try{ const o=_serverOrigin(); if(o) return o.replace(/\/+$/,'')+'/git'; }catch(_){ } }
    return '';
  }
  // Slugify a repo id to the git host's allowlist (^[a-z0-9][a-z0-9._-]{0,99}$) so a bad id is rejected
  // client-side and the create clone URL always matches what the host will accept.
  function _repoSlug(s){
    s=(s||'').trim().toLowerCase().replace(/\.git$/,'').replace(/[^a-z0-9._-]+/g,'-').replace(/^[-.]+/,'').replace(/-+/g,'-');
    return s.slice(0,100);
  }
  // First-commit quick start (like GitHub/Gitea show on an empty repo): the exact git commands for the
  // first push, with the real clone URL filled in. Reused by the post-create dialog + an empty repo view.
  function _repoQuickStartHtml(clone, repoId){
    const blk=(label,cmds)=>`<div class="qs-block"><div class="qs-head"><span class="muted small">${enc(label)}</span><button class="btn btn-ghost small qs-copy" data-cmd="${enc(cmds)}">⧉ Copy</button></div><pre class="qs-pre">${enc(cmds)}</pre></div>`;
    const first=`echo "# ${repoId}" >> README.md\ngit init\ngit add README.md\ngit commit -m "first commit"\ngit branch -M master\ngit remote add origin ${clone}\ngit push -u origin master`;
    const exist=`git remote add origin ${clone}\ngit branch -M master\ngit push -u origin master`;
    return `<div class="qs">
      <div class="qs-clone"><span class="muted small">Clone URL</span> <code class="rv-clone-url">${enc(clone)}</code> <button class="btn btn-neon small qs-copy" data-cmd="${enc(clone)}">⧉ Copy</button></div>
      <p class="muted small">Pushing is authorized by a maintainer's Nostr signature (your key) — use an <b>ngit</b>-aware client, or the built-in web editor to make the first commit right here.</p>
      ${blk('…create a new repository on the command line', first)}
      ${blk('…or push an existing repository', exist)}
    </div>`;
  }
  function _wireQuickStart(root){
    $$('.qs-copy',root).forEach(b=> b.onclick=()=> copyValue(b.dataset.cmd, 'copied', 'Copy this:'));
  }
  function _showRepoQuickStart(clone, repoId, ev){
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-send"></use></svg>${enc(repoId)} — get started</h3>${_repoQuickStartHtml(clone, repoId)}
      <div class="set-actions"><button class="btn btn-neon small" id="qs-open">Open repo →</button><button class="btn btn-ghost small" id="qs-done">Done</button></div>`,
      root=>{
        _wireQuickStart(root);
        $('#qs-done',root).onclick=closeModal;
        $('#qs-open',root).onclick=()=>{ closeModal(); if(ev) openRepo(ev); else switchView('repos'); };
      });
  }
  // Create a NEW self-hosted repo on THIS node, then announce it (public) + show the first-commit
  // tutorial. Provision is NIP-98-signed by the owner and re-verified by the git host (owner+allowlist).
  function createRepo(){
    if(S.GUEST || !S.ME){ _guestPrompt(); return; }
    if(!_gitHostBase()){ toast('this node has no git host configured'); return; }
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-git"></use></svg>Create a repo on ${enc(S.CFG.name||'this node')}</h3>
      <p class="muted small">Provisions an empty repo hosted here you can <code>git push</code> to, then announces it (NIP-34).</p>
      <label class="fld">Repo id <span class="muted small">(letters, digits, . _ - — e.g. my-app)</span><input class="input" id="cr-d" placeholder="my-app"></label>
      <label class="fld">Name<input class="input" id="cr-name" placeholder="My App"></label>
      <label class="fld">Description<textarea class="input" id="cr-desc" rows="2"></textarea></label>
      <label class="fld fld-check"><input type="checkbox" id="cr-private"> <span>Private — only you (+ readers) can clone; not announced</span></label>
      <div class="set-actions"><button class="btn btn-neon small" id="cr-go">Create</button><button class="btn btn-ghost small" id="cr-cancel">Cancel</button></div>
      <div class="muted small" id="cr-status"></div>`,
      root=>{
        $('#cr-d',root).focus();
        $('#cr-cancel',root).onclick=closeModal;
        root.addEventListener('keydown',ev=>{ if(ev.key==='Enter' && (ev.target.tagName||'').toLowerCase()!=='textarea'){ ev.preventDefault(); $('#cr-go',root).click(); } });
        $('#cr-go',root).onclick=()=>_doCreateRepo(root);
      });
  }
  async function _doCreateRepo(root){
    const st=$('#cr-status',root);
    const v=id=>($('#'+id,root).value||'').trim();
    const d=_repoSlug(v('cr-d')), name=v('cr-name'), desc=v('cr-desc');
    const priv=!!(($('#cr-private',root)||{}).checked);
    if(!d){ st.textContent='A repo id is required (letters, digits, . _ -).'; return; }
    const base=_gitHostBase();
    let npub; try{ npub=NT().nip19.npubEncode(S.ME.pubkey); }catch(_){ st.textContent='no key to own the repo.'; return; }
    const clone=`${base}/${npub}/${d}.git`;
    st.textContent='signing…';
    let auth;
    try{ auth='Nostr '+btoa(JSON.stringify(await sign(27235,'',[['u',clone+'/create'],['method','POST']]))); }
    catch(err){ st.textContent='couldn’t sign: '+((err&&err.message)||err); return; }
    st.textContent='creating on the host…';
    let j={};
    try{ j=await fetch('/client/git/create',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:clone, name, description:desc, private:priv, auth})}).then(r=>r.json()); }
    catch(_){ st.textContent='the git host didn’t answer.'; return; }
    if(!j || !j.ok){ st.textContent='host: '+((j&&j.error)||'create failed'); return; }
    const cloneUrl=j.clone||clone;
    let ev=null;
    if(!priv && Array.isArray(j.announce_tags_30617) && j.announce_tags_30617.length){
      st.textContent='announcing…';
      try{ const r=await publish(30617,'',j.announce_tags_30617);
        if(r && r.ok===false){ st.textContent='created ✓ but announce was rejected: '+(r.msg||''); return; } }
      catch(e){ st.textContent='created ✓ but announce failed: '+((e&&e.message)||e); return; }
      ev={ kind:30617, pubkey:S.ME.pubkey, created_at:Math.floor(Date.now()/1000), tags:j.announce_tags_30617, content:'', id:'' };
    }
    closeModal();
    toast(priv?'private repo created':'repo created + announced');
    _showRepoQuickStart(cloneUrl, d, ev);   // the first-commit tutorial, like other git platforms
  }
  // Delete a repo the CURRENT USER OWNS: NIP-09-delete its 30617 announcement (+ 30618 state), then
  // remove the hosted bare repo (owner NIP-98). Owner-only + confirmed — destructive, no undo.
  async function deleteRepo(e){
    if(!S.ME || !e || e.pubkey!==S.ME.pubkey){ toast('only the repo owner can delete it'); return; }
    const name=_repoTag(e,'name')||_repoTag(e,'d')||'this repo';
    if(!await uiConfirm(`Delete “${name}”?\n\nThis removes the hosted repository AND its announcement, and can’t be undone.`, {ok:'Delete', danger:true})) return;
    const cloneUrl=(e.tags.find(t=>t[0]==='clone')||[])[1]||'';
    // 1) NIP-09 delete the announcement (30617) + any state (30618) — the client signed them, so this
    //    removes it from Discover for everyone (the relay applies + federates the kind-5).
    try{ await publish(5,'',[['e',e.id],['a',_repoAddr(e)]]); }catch(_){}
    // 2) Remove the hosted bare repo (owner NIP-98). No-op for a repo hosted on an external forge.
    if(cloneUrl){
      try{
        const auth='Nostr '+btoa(JSON.stringify(await sign(27235,'',[['u',cloneUrl.replace(/\/+$/,'')+'/delete'],['method','POST']])));
        const j=await fetch('/client/git/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:cloneUrl, auth})}).then(r=>r.json());
        if(j && j.ok===false && !/not a self-hosted/i.test(j.error||'')){ toast('announcement removed; host: '+(j.error||'delete failed')); }
      }catch(_){ }
    }
    toast('repo deleted'); switchView('repos');
  }
  // The tags this form OWNS. Everything else already on an announcement is carried over VERBATIM when
  // editing, because 30617 is replaceable and a re-publish overwrites the whole event: emitting only the
  // fields shown here would silently drop `maintainers` (which holds the hosting node's operator key —
  // the signer of the 30618 state witness, and half the push ACL), `relays` (the advertised push
  // endpoint) and NIP-34's `r`/euc. Editing a description must not quietly break pushing.
  const _REPO_OWN_TAGS=new Set(['d','name','description','clone','web','alt']);
  // Publish a NIP-34 repo announcement (kind 30617) signed by the user, so it shows here + in other
  // Nostr git clients (gitworkshop, ngit, …). d-tag = repo id (replaceable per identifier) — which is
  // also what makes this the EDIT path: re-publishing with the same d replaces the announcement.
  function publishRepo(existing){
    const editing=!!(existing&&Array.isArray(existing.tags));
    if(editing && (!S.ME || existing.pubkey!==S.ME.pubkey)){
      // A replaceable event is keyed by (kind, pubkey, d) — signing someone else's repo with your key
      // doesn't edit it, it mints a SECOND announcement of the same repo under you.
      toast('only the repo owner can edit its details'); return;
    }
    const tag=k=>editing?((existing.tags.find(t=>t[0]===k)||[])[1]||''):'';
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-git"></use></svg>${editing?'Edit repo details':'Announce a git repo'}</h3>
      <p class="muted small">${editing
        ? 'Republishes this repo’s NIP-34 announcement (kind 30617) with your changes. Its maintainers and relays are kept exactly as they are.'
        : 'Publishes a NIP-34 repo announcement (kind 30617) signed by your key.'}</p>
      <label class="fld">Repo id <span class="muted small">${editing?'(fixed — it identifies the repo)':'(short slug, e.g. posterchanai)'}</span><input class="input" id="rp-d" value="${enc(tag('d'))}" placeholder="my-app"${editing?' readonly':''}></label>
      <label class="fld">Name<input class="input" id="rp-name" value="${enc(tag('name'))}" placeholder="My App"></label>
      <label class="fld">Description<textarea class="input rp-ta" id="rp-desc" rows="3" placeholder="What this project is, in a line or two.">${enc(tag('description'))}</textarea></label>
      <label class="fld">Clone URL<input class="input" id="rp-clone" value="${enc(tag('clone'))}" placeholder="https://git.example.com/me/my-app.git"></label>
      <label class="fld">Web URL<input class="input" id="rp-web" value="${enc(tag('web'))}" placeholder="https://git.example.com/me/my-app"></label>
      <div class="set-actions"><button class="btn btn-neon small" id="rp-pub">${editing?'Save changes':'Publish'}</button><button class="btn btn-ghost small" id="rp-cancel">Cancel</button></div>
      <div class="muted small" id="rp-status"></div>`,
      root=>{
        $('#rp-cancel',root).onclick=closeModal;
        { const f=$(editing?'#rp-name':'#rp-d',root); if(f) f.focus(); }
        $('#rp-pub',root).onclick=async()=>{
          const v=id=>($('#'+id,root).value||'').trim();
          const d=v('rp-d'); const st=$('#rp-status',root);
          if(!d){ st.textContent='Repo id is required.'; return; }
          const tags=[['d',d]];
          // A NIP-34 tag can carry SEVERAL values (["clone", url, url2]) and this form shows only the
          // first, so an untouched field re-emits the ORIGINAL tag rather than a one-value rewrite of
          // it — otherwise editing the description alone would quietly drop a repo's second clone URL.
          const put=(k,val)=>{
            const o=editing?existing.tags.find(t=>t[0]===k):null;
            if(o && (o[1]||'')===val) tags.push(o.slice());
            else if(val) tags.push([k,val]);
          };
          put('name',v('rp-name')); put('description',v('rp-desc'));
          put('clone',v('rp-clone')); put('web',v('rp-web'));
          tags.push(['alt',`git repository: ${v('rp-name')||d}`]);
          if(editing) existing.tags.forEach(t=>{ if(Array.isArray(t)&&t.length&&!_REPO_OWN_TAGS.has(t[0])) tags.push(t.slice()); });
          st.textContent=editing?'saving…':'publishing…';
          try{ const r=await publish(30617,'',tags);
            if(r && r.ok===false){ st.textContent='relay: '+(r.msg||'rejected'); return; }
            closeModal();
            if(editing){
              toast('repo details saved');
              // Re-open on the event we just signed — publish() saved it, and Store.query collapses a
              // replaceable kind to its latest version, so this is the real signed event (id + sig),
              // not a hand-built stand-in. Painting it now beats waiting on a relay round-trip.
              const fresh=(Store.query([{kinds:[30617],authors:[S.ME.pubkey],'#d':[d],limit:1}])||[])[0];
              openRepo(fresh || {...existing, id:'', created_at:Math.floor(Date.now()/1000), tags});
            } else { toast('repo announced'); switchView('repos'); }
          }catch(e){ st.textContent='failed: '+((e&&e.message)||e); }
        };
      });
  }
  function repoCard(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const name=(e.tags.find(t=>t[0]==='name')||[])[1]||(e.tags.find(t=>t[0]==='d')||[])[1]||'(unnamed repo)';
    const desc=(e.tags.find(t=>t[0]==='description')||[])[1]||'';
    const clone=(e.tags.find(t=>t[0]==='clone')||[]).slice(1).filter(Boolean);
    const web=(e.tags.find(t=>t[0]==='web')||[]).slice(1).filter(Boolean);
    const wurl=_mdUrl(web[0]||'');   // scheme-allowlist (http/https only) — a relay-supplied javascript: href must never become clickable
    const share=_repoShareUrl(e);
    return `<article class="repo-card" data-id="${e.id}" data-pk="${e.pubkey}">
      <div class="repo-card-hd"><span class="repo-card-ico">🌱</span><span class="repo-card-name">${enc(name)}</span></div>
      <div class="repo-card-desc">${desc?enc(desc.slice(0,150)):'<span class="muted">git repository</span>'}</div>
      <div class="repo-card-by"><img class="repo-card-av" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'" data-prof="${e.pubkey}"><span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span>${
        _repoHostname(e)?`<span class="repo-host${_repoHostedHere(e)?' here':''}" title="${enc(_repoHostname(e))}">${enc(_repoHostname(e))}</span>`:''}</div>
      <div class="repo-card-acts"><button class="btn btn-ghost small repo-star" data-id="${e.id}" title="${_starred(e)?'Unstar':'Star'}">${_starred(e)?'\u2b50':'\u2606'}</button>${clone.length?`<button class="btn btn-ghost small repo-clone" data-clone="${enc(clone[0])}">⧉ Clone</button>`:''}${share?`<button class="btn btn-ghost small repo-share" data-share="${enc(share)}" onclick="event.stopPropagation()"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Link</button>`:''}${_repoWebExternal(wurl)?`<a class="btn btn-ghost small" href="${enc(wurl)}" target="_blank" rel="noopener" onclick="event.stopPropagation()"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Web</a>`:''}</div>
    </article>`;
  }
  // ---------- NIP-34 repo detail (README + issues + patches) ----------
  // Addressable coordinate a repo's collaboration events (issues 1621 / patches 1617) point at via `a`.
  function _repoAddr(e){ const d=(e.tags.find(t=>t[0]==='d')||[])[1]||''; return `30617:${e.pubkey}:${d}`; }
  function _repoTag(e,k){ return (e.tags.find(t=>t[0]===k)||[])[1]||''; }
  // Shareable web link to a repo = poster.place/<naddr> for its 30617 coordinate. Opens the repo view
  // directly (openNaddr routes kind-30617 → openRepo) for anyone, logged in or not — this is what
  // "share this project" hands out. (A repo's own `web` tag is usually the generic client URL.)
  // Where 🐛 Report a bug files to when the server doesn't name one (CFG.issues_repo). This is the
  // PosterChanAI repo's own 30617 coordinate — a fork overrides it rather than editing this line.
  
  function _repoNaddr(e){
    try{ const relays=[S.CFG&&S.CFG.relay_url].filter(Boolean);
      return NT().nip19.naddrEncode({identifier:_repoTag(e,'d'), pubkey:e.pubkey, kind:30617, relays}); }
    catch(_){ return ''; }
  }
  function _repoShareUrl(e){ const n=_repoNaddr(e); return n?_webLink(n):''; }
  // Everyone responsible for a repo: its 30617 author plus the `maintainers` tag — NIP-34 puts them as
  // extra VALUES on one tag (["maintainers", pk, pk, …]), not one tag each, which is why this slices.
  function _repoPeople(e){
    const out=[], seen=new Set();
    const add=pk=>{ if(/^[0-9a-f]{64}$/i.test(pk||'') && !seen.has(pk)){ seen.add(pk); out.push(pk); } };
    add(e&&e.pubkey);
    ((e&&e.tags)||[]).filter(t=>t[0]==='maintainers').forEach(t=>t.slice(1).forEach(add));
    return out;
  }
  // True only when a repo's `web` tag is a REAL external forge (GitHub/Gitea/…), not our own origin —
  // "Web" on poster.place/client just reopens the generic app, so we hide it there in favour of Share.
  function _repoWebExternal(wurl){
    if(!wurl) return false;
    try{ return new URL(wurl, location.href).origin !== _serverOrigin(); }catch(_){ return false; }
  }
  // A collaboration event's human title: prefer a `subject` tag (NIP-34), else the first non-empty line.
  function _collabTitle(ev){
    const s=_repoTag(ev,'subject'); if(s) return s;
    const ln=((ev.content||'').split('\n').find(l=>l.trim())||'').trim();
    return ln || '(no title)';
  }
  function _collabRow(ev, state, canAct){
    const p=profOf(ev.pubkey); needProfile(ev.pubkey);
    const ico=ev.kind===1617?'🩹':'🐛';
    const title=_collabTitle(ev).slice(0,200);
    const body=(ev.content||'').trim();
    // The row preview is PLAIN TEXT, so an attached blob URL would otherwise eat all 240 chars of it
    // with an unreadable sha256. mediaParts is the shared "lift the media out of the text" primitive
    // the other previews use (narratePost, _cardText), so the strip and the 📎 count come from ONE
    // definition of what media is. Counting its items rather than our own `imeta` tags also counts an
    // issue filed from any OTHER nostr client (no imeta) — and a non-image blob, which never gets one.
    const mp=mediaParts(body);
    const clean=(mp.text||'').replace(/\s{2,}/g,' ').trim();
    const nAtt=mp.items.length;
    const preview = clean && clean.slice(0,240)!==title ? clean.slice(0,240) : '';
    return `<div class="collab-row" data-id="${ev.id}" data-pk="${ev.pubkey}">
      <div class="collab-title"><span class="collab-ico">${ico}</span>${enc(title)}${nAtt?`<span class="collab-att" title="${nAtt} attachment${nAtt>1?'s':''}">📎${nAtt>1?nAtt:''}</span>`:''}${state?`<span class="collab-state st-${enc(state)}">${_ST_BADGE[state]||state}</span>`:''}</div>
      ${preview?`<div class="collab-body">${enc(preview)}${clean.length>240?'…':''}</div>`:''}
      <div class="collab-meta"><img class="collab-av" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'" data-prof="${ev.pubkey}"><span class="name" data-prof="${ev.pubkey}">${enc(p.name||p.display_name||'anon')}</span><span class="muted small">· ${timeAgo(ev.created_at)}</span>${canAct?`<span class="spacer"></span>${
        (state==='closed'||state==='resolved')
          ? `<button class="cf-act" data-id="${ev.id}" data-kind="1630" title="Reopen"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg>Reopen</button>`
          : `<button class="cf-act" data-id="${ev.id}" data-kind="1631" title="Mark resolved"><svg class="ic b-ic" aria-hidden="true"><use href="#i-check"></use></svg>Resolve</button>`
            +`<button class="cf-act" data-id="${ev.id}" data-kind="1632" title="Close"><svg class="ic b-ic" aria-hidden="true"><use href="#i-live"></use></svg>Close</button>`
      }`:''}</div>
    </div>`;
  }
  // ---------- self-hosted (GRASP) repo state ----------
  // ONE object holds what every panel of an open repo needs: which repo, which ref is selected, and
  // whether this user may write. The panels read it instead of closing over a cloneUrl, so changing
  // branch re-renders all of them consistently instead of only whichever one knew about the change.
  let _rv=null;
  // Writers = the repo's own maintainer ACL from its NIP-34 announcement (owner ∪ `maintainers`) — the
  // SAME set the git host enforces on the signed request. This only decides whether to draw the Edit
  // buttons; the host re-checks every write against the relay, so a forged UI can't gain anything.
  function _rvCanWrite(ev){
    if(S.GUEST || !S.ME || !ev) return false;
    if(ev.pubkey===S.ME.pubkey) return true;
    return (ev.tags||[]).some(t=>t[0]==='maintainers' && t.slice(1).includes(S.ME.pubkey));
  }
  // May THIS user commit to what is currently selected? Maintainer AND a branch (a tag is a snapshot;
  // the host refuses a commit onto one, so offering an Edit button there would only ever fail).
  function _rvMayEdit(){ return !!(_rv && _rv.canWrite && !_rv.isTag); }
  function _rvUrl(route, params){
    const q=new URLSearchParams({url:_rv.cloneUrl, ref:_rv.ref, ...(params||{})});
    return `/client/git/${route}?${q}`;
  }
  async function _rvJson(route, params){
    try{ return await fetch(_rvUrl(route,params)).then(r=>r.json()); }catch(_){ return {ok:false}; }
  }
  /* Which tab each repo was last read on, keyed by its naddr.
   *
   * A repo's tabs are panels inside one view, not screens — putting each in the history would make
   * Back out of an issue a walk through README/Files/Commits. But the tab IS where the reader was:
   * an issue is opened from the Issues tab and nowhere else, so coming back to the README is coming
   * back to a screen they never chose. Restored only on a BACK press (`opts.restore`), never when
   * the repo is opened fresh from the list — arriving somewhere starts at its front page. */
  const _rvTab = Object.create(null);
  function openRepo(e, opts){
    if(!e) return;
    // Put the repo in history. It was never a history entry, so Back from an issue popped straight PAST
    // it to whatever came before — the "no way back from an issue" dead end. The naddr doubles as the
    // shareable/reloadable URL: routeFromPath → openNaddr already routes a 30617 back to openRepo.
    { const n=_repoNaddr(e); if(n) _navUrl('/'+n); }
    S.VIEW='repo'; _clearNav(); _gitKbBind(); $('#view-title').textContent='Repo';
    const feed=$('#feed'); const p=profOf(e.pubkey); needProfile(e.pubkey);
    const name=_repoTag(e,'name')||_repoTag(e,'d')||'(unnamed repo)';
    const desc=_repoTag(e,'description');
    const clone=(e.tags.find(t=>t[0]==='clone')||[]).slice(1).filter(Boolean);
    const web=(e.tags.find(t=>t[0]==='web')||[]).slice(1).filter(Boolean);
    const wurl=_mdUrl(web[0]||'');
    const readmeSrc=clone[0]||web[0]||'';   // clone URL preferred (points straight at the forge)
    const cloneUrl=clone[0]||'';
    const shareUrl=_repoShareUrl(e);        // poster.place/<naddr> — the link to hand out for this project
    const isOwner=!!(S.ME && e.pubkey===S.ME.pubkey);   // only the owner sees Delete (host re-checks the signature)
    // Files browser only for a self-hosted (Nostr-owned) repo — the clone path has an npub/hex owner
    // before <id>.git; a plain forge clone URL (GitHub/Gitea) has no readable file API here.
    const isGrasp=_repoBrowsableHere(e);
    // Hosted somewhere else, but on a forge that speaks the same URL shape. The distinction matters
    // to the README panel below: it is fetching across the internet, not off this node.
    const isForeignGrasp=!isGrasp && _graspShaped(cloneUrl);
    _rv = isGrasp ? {ev:e, cloneUrl, ref:'HEAD', refName:'', refs:null, canWrite:_rvCanWrite(e),
                     filesLoaded:false, commitsLoaded:false, path:''} : null;
    feed.innerHTML=`<div class="repo-view">
      <button class="btn btn-ghost small" id="repo-back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg>Repos</button>
      <div class="rv-head">
        <div class="rv-headrow">
          <img class="rv-avatar" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'" data-prof="${e.pubkey}">
          <div class="rv-headmain">
            <h1 class="rv-title"><svg class="ic h-ic" aria-hidden="true"><use href="#i-git"></use></svg>${enc(name)}</h1>
            <div class="rv-by"><span class="muted small">maintained by</span> <span class="name" data-prof="${e.pubkey}">${enc(p.name||p.display_name||'anon')}</span></div>
          </div>
        </div>
        ${desc?`<div class="rv-desc">${enc(desc)}</div>`:''}
        ${cloneUrl?`<div class="rv-clone">
          <span class="rv-clone-ico"><svg class="ic b-ic" aria-hidden="true"><use href="#i-branch"></use></svg></span>
          <code class="rv-clone-url">${enc(cloneUrl)}</code>
          <div class="rv-acts">
            <button class="btn btn-neon small repo-clone" data-clone="${enc(cloneUrl)}" title="Copy clone URL">⧉ Copy</button>
            ${shareUrl?`<button class="btn btn-ghost small rv-share" data-share="${enc(shareUrl)}" title="Copy a shareable link to this project"><svg class="ic b-ic" aria-hidden="true"><use href="#i-share"></use></svg>Share</button>`:''}
            ${_repoWebExternal(wurl)?`<a class="btn btn-ghost small" href="${enc(wurl)}" target="_blank" rel="noopener"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Web</a>`:''}
            ${isOwner?`<button class="btn btn-ghost small rv-edit" title="Edit this repository’s name and description (owner only)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>Edit</button>`:''}
            ${isOwner?`<button class="btn btn-ghost small rv-delete" title="Delete this repository (owner only)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>`:''}
          </div>
        </div>`:`<div class="rv-clone"><div class="rv-acts">${shareUrl?`<button class="btn btn-neon small rv-share" data-share="${enc(shareUrl)}" title="Copy a shareable link to this project"><svg class="ic b-ic" aria-hidden="true"><use href="#i-share"></use></svg>Share</button>`:''}${_repoWebExternal(wurl)?`<a class="btn btn-ghost small" href="${enc(wurl)}" target="_blank" rel="noopener"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Open web</a>`:''}${isOwner?`<button class="btn btn-ghost small rv-edit" title="Edit this repository’s name and description (owner only)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>Edit</button>`:''}${isOwner?`<button class="btn btn-ghost small rv-delete" title="Delete this repository (owner only)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>`:''}</div></div>`}
        ${isGrasp?`<div class="rv-refbar">
          <button class="btn btn-ghost small rv-refbtn" id="rv-refpick" title="Switch branch or tag"><svg class="ic b-ic" aria-hidden="true"><use href="#i-branch"></use></svg><span id="rv-refname">default</span> ▾</button>
          <span class="muted small" id="rv-refnote"></span>
        </div>`:''}
      </div>
      <div class="rv-tabs" role="tablist">
        <button class="rv-tab active" data-tab="readme"><svg class="ic b-ic" aria-hidden="true"><use href="#i-article"></use></svg>README</button>
        ${isGrasp?`<button class="rv-tab" data-tab="files"><svg class="ic b-ic" aria-hidden="true"><use href="#i-folder"></use></svg>Files</button>`:''}
        ${isGrasp?`<button class="rv-tab" data-tab="commits"><svg class="ic b-ic" aria-hidden="true"><use href="#i-clock"></use></svg>Commits</button>`:''}
        <button class="rv-tab" data-tab="issues"><svg class="ic b-ic" aria-hidden="true"><use href="#i-bug"></use></svg>Issues <span class="rv-count" id="rv-c-issues"></span></button>
        <button class="rv-tab" data-tab="patches"><svg class="ic b-ic" aria-hidden="true"><use href="#i-bandage"></use></svg>Patches <span class="rv-count" id="rv-c-patches"></span></button>
      </div>
      <div class="rv-panel" data-panel="readme">
        <!-- A SPINNER THAT SAYS WHAT IT IS WAITING FOR. Reading a README off another forge is a
             round trip across the internet that this node bounds at 8s and, for a GRASP host that
             is not a forge, MEASURED at 8-9 seconds every time. Eight silent seconds under a
             spinner is indistinguishable from a page that never loads. -->
        <div class="markdown rv-readme" id="rv-readme"><div class="spinner"></div>${
          _repoHostname(e) && !isGrasp
            ? `<div class="muted small" style="text-align:center">reading the README from ${enc(_repoHostname(e))}…</div>`
            : ''}</div>
      </div>
      ${isGrasp?`<div class="rv-panel" data-panel="files" hidden><div class="fb" id="rv-files"><div class="spinner"></div></div></div>`:''}
      ${isGrasp?`<div class="rv-panel" data-panel="commits" hidden><div class="fb" id="rv-commits"><div class="spinner"></div></div></div>`:''}
      <div class="rv-panel" data-panel="issues" hidden>
        <div class="rv-collab-hd"><span class="search-section-title">Issues</span><button class="btn btn-neon small" id="rv-newissue"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New issue</button></div>
        <div class="rv-collab" id="rv-issues"><div class="spinner"></div></div>
      </div>
      <div class="rv-panel" data-panel="patches" hidden>
        <div class="rv-collab-hd"><span class="search-section-title">Patches</span></div>
        <div class="rv-collab" id="rv-patches"><div class="spinner"></div></div>
      </div>
    </div>`;
    $('#repo-back',feed).onclick=()=>switchView('repos');
    $$('[data-prof]',feed).forEach(el=> el.onclick=ev=>{ ev.stopPropagation(); renderProfileView(el.dataset.prof); });
    { const cb=$('.repo-clone',feed); if(cb) cb.onclick=()=> copyValue(cb.dataset.clone, 'clone URL copied', 'Clone URL:'); }
    { const sb=$('.rv-share',feed); if(sb) sb.onclick=()=> copyValue(sb.dataset.share, 'project link copied — share it anywhere', 'Project link:'); }
    { const eb=$('.rv-edit',feed); if(eb) eb.onclick=()=>publishRepo(e); }
    { const xb=$('.rv-delete',feed); if(xb) xb.onclick=()=>deleteRepo(e); }
    { const ni=$('#rv-newissue',feed); if(ni) ni.onclick=()=>newRepoIssue(e); }
    // Tabs: swap the visible panel. README loads eagerly; issues/patches were already fetched below;
    // Files/Commits are lazy-loaded on first open (a git round-trip each).
    const _naddr=_repoNaddr(e);
    $$('.rv-tab',feed).forEach(tb=> tb.onclick=()=>{
      if(_naddr) _rvTab[_naddr]=tb.dataset.tab;   // where this reader is in this repo — see _rvTab
      $$('.rv-tab',feed).forEach(x=>x.classList.toggle('active',x===tb));
      $$('.rv-panel',feed).forEach(pn=> pn.hidden = pn.dataset.panel!==tb.dataset.tab);
      if(!_rv) return;
      if(tb.dataset.tab==='files' && !_rv.filesLoaded){ _rv.filesLoaded=true; _loadRepoFiles(feed, ''); }
      if(tb.dataset.tab==='commits' && !_rv.commitsLoaded){ _rv.commitsLoaded=true; _loadRepoCommits(feed); }
    });
    // Back out of an issue lands here: re-press the tab it was left on, which also runs that tab's
    // lazy loader (Files/Commits are a git round trip each and load on first open).
    if(opts && opts.restore && _naddr && _rvTab[_naddr] && _rvTab[_naddr]!=='readme'){
      const tb=$(`.rv-tab[data-tab="${_rvTab[_naddr]}"]`, feed); if(tb) tb.click();
    }
    if(isGrasp) _rvLoadRefs(feed);
    // README — best-effort forge fetch; the server renders nothing, we render its markdown safely.
    (async()=>{
      const box=$('#rv-readme',feed); if(!box) return;
      if(!readmeSrc){ box.innerHTML=`<div class="muted small">No clone/web URL on this repo.</div>`; return; }
      try{
        const r=await fetch('/client/git/readme?url='+encodeURIComponent(readmeSrc));
        const j=await r.json();
        if(S.VIEW!=='repo') return;
        if(j && j.ok && j.markdown){ box.innerHTML=mdToHtml(j.markdown);
          box.querySelectorAll('img').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src)); }
        else if(_rv && _rv.cloneUrl){
          // Self-hosted repo with no README — usually a freshly-created EMPTY repo. Show the first-commit
          // quick start right here (like GitHub/Gitea), so the repo tells you how to make the first push.
          box.innerHTML=`<div class="rv-empty"><h2 class="rv-empty-h"><svg class="ic h-ic" aria-hidden="true"><use href="#i-send"></use></svg>Quick start</h2>${_repoQuickStartHtml(_rv.cloneUrl, _repoTag(e,'d')||'repo')}</div>`;
          _wireQuickStart(box);
        }
        else box.innerHTML=`<div class="muted small">No README found${
            _repoHostname(e)?` on ${enc(_repoHostname(e))}`:''}${
            wurl?` — <a href="${enc(wurl)}" target="_blank" rel="noopener">open the repo</a>`:''}.</div>${
          isForeignGrasp&&cloneUrl?`<div class="muted small" style="margin-top:8px">This repo is hosted on
            <b>${enc(_repoHostname(e))}</b>, so its files and history are not browsable here. Clone it:
            <code>${enc(cloneUrl)}</code></div>`:''}`;
      }catch(_){ if(S.VIEW==='repo') box.innerHTML=`<div class="muted small">Couldn’t load the README${wurl?` — <a href="${enc(wurl)}" target="_blank" rel="noopener">open the repo</a>`:''}.</div>`; }
    })();
    // Issues (1621) + patches (1617) reference the repo via an `a` tag = the repo coordinate.
    const addr=_repoAddr(e);
    _loadRepoCollab(feed, '#rv-issues', 1621, addr, 'No issues yet.', e);
    _loadRepoCollab(feed, '#rv-patches', 1617, addr, 'No patches yet.', e);
    decorateProfiles();
  }
  // ---------- NIP-34 issue/patch STATUS (1630 open / 1631 resolved / 1632 closed / 1633 draft) ----
  // Spec: a status carries an `e` tag to the issue marked "root", p-tags the repo owner + the root
  // author, and "the most recent Status event (by created_at) from either the issue/patch author or a
  // maintainer is considered valid" — so authority is that union, not the owner alone.
  const _ST_KINDS = [1630, 1631, 1632, 1633];
  const _ST_BADGE = {open:'🟢 Open', resolved:'✅ Resolved', closed:'🔴 Closed', draft:'⚪ Draft'};
  const _ST_META = {1630:['open','🟢 Open'], 1631:['resolved','✅ Resolved'],
                    1632:['closed','🔴 Closed'], 1633:['draft','⚪ Draft']};
  // Newest authoritative status per issue id. `authority` = issue author ∪ repo maintainers.
  function _statusMap(statusEvs, issues, people){
    const owner = new Set(people || []);
    const byIssue = new Map(issues.map(e => [e.id, e]));
    const best = new Map();
    for (const st of statusEvs || []) {
      if (!_ST_META[st.kind]) continue;
      const target = (st.tags.find(t => t[0]==='e' && t[3]==='root') || st.tags.find(t => t[0]==='e') || [])[1];
      const issue = target && byIssue.get(target);
      if (!issue) continue;
      if (st.pubkey !== issue.pubkey && !owner.has(st.pubkey)) continue;   // not authorised to set state
      const cur = best.get(target);
      if (!cur || st.created_at > cur.created_at) best.set(target, st);
    }
    return best;
  }
  function _canSetStatus(issue, people){
    const me = S.ME && S.ME.pubkey; if(!me || S.GUEST) return false;
    return issue.pubkey === me || (people || []).includes(me);
  }
  // Publish a status. `a` is optional per spec but recommended — it lets a client find every status
  // for a repo without first knowing the issue ids.
  async function _setIssueStatus(repo, issue, kind, note){
    const tags = [['e', issue.id, S.CFG.relay_url || '', 'root'], ['k', String(issue.kind || 1621)]];
    const addr = _repoAddr(repo); if (addr) tags.push(['a', addr]);
    const seen = new Set([S.ME && S.ME.pubkey]);
    for (const pk of [issue.pubkey, ..._repoPeople(repo)]) {
      if (pk && !seen.has(pk)) { seen.add(pk); tags.push(['p', pk]); }
    }
    return await publish(kind, note || '', tags);
  }

  const _collabFilter = { '#rv-issues':'open', '#rv-patches':'open' };   // per-panel Open/Closed/All
  async function _loadRepoCollab(feed, sel, kind, addr, emptyMsg, repo){
    let evs=[]; try{ evs=await Relay.query([{ kinds:[kind], '#a':[addr], limit:100 }]); }catch(_){}
    evs.forEach(ev=>{ Store.saveEvent(ev); needProfile(ev.pubkey); });
    if(S.VIEW!=='repo') return;
    const box=$(sel,feed); if(!box) return;
    evs.sort((a,b)=>b.created_at-a.created_at);
    // Statuses in ONE query keyed on the issue ids we just fetched — not per-issue, which would be
    // 100 round-trips to paint a list.
    let stEvs=[];
    if(evs.length){
      try{ stEvs=await Relay.query([{ kinds:_ST_KINDS, '#e':evs.map(e=>e.id).slice(0,200), limit:500 }]); }catch(_){}
    }
    const people=_repoPeople(repo||_rv&&_rv.repo);
    const st=_statusMap(stEvs, evs, people);
    const stateOf = e => { const m=st.get(e.id); return m ? _ST_META[m.kind][0] : 'open'; };   // no status = open (spec default)
    const counts={ open:0, closed:0 };
    evs.forEach(e=>{ const k=stateOf(e); counts[k==='open'||k==='draft'?'open':'closed']++; });
    const mode=_collabFilter[sel]||'open';
    const shown=evs.filter(e=>{ const k=stateOf(e);
      return mode==='all' ? true : mode==='open' ? (k==='open'||k==='draft') : (k==='closed'||k==='resolved'); });
    const bar=`<div class="collab-filter">${[['open',`Open ${counts.open}`],['closed',`Closed ${counts.closed}`],['all','All']]
      .map(([k,l])=>`<button class="cf-tab${k===mode?' on':''}" data-cf="${k}">${enc(l)}</button>`).join('')}</div>`;
    box.innerHTML = bar + (shown.length
      ? shown.map(e=>_collabRow(e, stateOf(e), _canSetStatus(e, people))).join('')
      : `<div class="rv-empty muted small">${mode==='open'?'No open items.':emptyMsg}</div>`);
    $$('.cf-tab',box).forEach(b=> b.onclick=ev=>{ ev.stopPropagation(); _collabFilter[sel]=b.dataset.cf;
      _loadRepoCollab(feed, sel, kind, addr, emptyMsg, repo); });
    // Close / Reopen, gated on the spec's authority rule.
    $$('.cf-act',box).forEach(b=> b.onclick=async ev=>{
      ev.stopPropagation();
      const issue=evs.find(x=>x.id===b.dataset.id); if(!issue) return;
      const k=+b.dataset.kind; b.disabled=true; b.textContent='…';
      try{ const r=await _setIssueStatus(repo||_rv&&_rv.repo, issue, k, b.dataset.note||'');
        if(r && r.ok===false){ toast('relay: '+(r.msg||'rejected')); b.disabled=false; }
        else { toast(k===1632?'issue closed':k===1631?'marked resolved':'issue reopened');
               _loadRepoCollab(feed, sel, kind, addr, emptyMsg, repo); }
      }catch(err){ toast('failed: '+((err&&err.message)||err)); b.disabled=false; }
    });
    const cid = sel==='#rv-issues'?'#rv-c-issues':sel==='#rv-patches'?'#rv-c-patches':'';
    if(cid){ const c=$(cid,feed); if(c) c.textContent = counts.open?String(counts.open):''; }
    // openThread, not renderThread: renderThread swaps the view WITHOUT pushing a URL, so an issue
    // opened this way had no history entry and Back could not return to the repo.
    $$('.collab-row',box).forEach(r=> r.onclick=ev=>{ if(ev.target.closest('[data-prof]')){ renderProfileView(r.dataset.pk); return; } openThread(r.dataset.id); });
    $$('[data-prof]',box).forEach(el=> el.onclick=ev=>{ ev.stopPropagation(); renderProfileView(el.dataset.prof); });
    decorateProfiles();
  }
  // Attach files to an issue body. Same three moves as the post composer — uploadBlob → the BARE url in
  // the body → imetaTagsFor at publish — so an issue rides the app's real Blossom path (image
  // compression, batch-signed auth, NIP-96 fallback) instead of a bespoke one that would drift from it.
  // Deliberately NOT the article editor's `![](url)` markdown: a 1621 renders through noteCard →
  // mediaParts (plain text + lifted media), not a markdown renderer, so the syntax would show up
  // literally as `![]()` beside the picture. A bare URL embeds here and still reads fine in the clients
  // that DO render the body as markdown.
  async function _issueAttach(files, ta, st){
    files=[...(files||[])].filter(Boolean); if(!files.length) return;
    for(let i=0;i<files.length;i++){
      st.textContent=`uploading ${i+1}/${files.length}…`;
      try{
        const f=files[i], url=await uploadBlob(f, {folder:'Posts'});
        // Media renders itself; anything else degrades to a plain link (__blobFallback), and a bare
        // /<sha256> tells the reader nothing about what they'd be opening — so it keeps its filename.
        const label=/^(image|video)\//.test(f.type||'') ? '' : ((f.name||'').replace(/\s+/g,' ').trim()+' ');
        ta.value += (ta.value && !ta.value.endsWith('\n') ? '\n' : '') + label + url;
      }catch(err){
        // A brand-new user has no Blossom permission yet. The composer turns that 403 into a
        // request-access flow rather than a dead error; an issue must not be the one place it dead-ends.
        if(_blossomDenied(err)){ requestBlossomAccess(); st.textContent='🔒 No upload access — requested it from the admin.'; }
        else st.textContent='upload failed: '+((err&&err.message)||err);
        return;
      }
    }
    st.textContent=''; ta.focus();
  }
  // Publish a NIP-34 issue (kind 1621) against a repo: `a` tag → repo coordinate + a `subject` tag.
  function newRepoIssue(repo){
    if(S.GUEST){ _guestPrompt(); return; }
    const addr=_repoAddr(repo);
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-bug"></use></svg>New issue</h3>
      <p class="muted small">Publishes a NIP-34 issue (kind 1621) signed by your key, against <b>${enc(_repoTag(repo,'name')||_repoTag(repo,'d')||'this repo')}</b>.</p>
      <label class="fld">Subject<input class="input" id="ri-subj" placeholder="Short summary"></label>
      <label class="fld">Description<textarea class="input" id="ri-body" rows="5" placeholder="Describe the issue… (markdown)"></textarea></label>
      <div class="row cmp-tools"><button type="button" class="btn btn-ghost small" id="ri-attach"><svg class="ic b-ic" aria-hidden="true"><use href="#i-paperclip"></use></svg>Attach</button><input type="file" id="ri-file" multiple hidden><span class="spacer"></span><span class="muted small">or paste / drop a screenshot</span></div>
      <div class="set-actions"><button class="btn btn-neon small" id="ri-pub">Publish</button><button class="btn btn-ghost small" id="ri-cancel">Cancel</button></div>
      <div class="muted small" id="ri-status"></div>`,
      root=>{
        const ta=$('#ri-body',root), st=$('#ri-status',root);
        // Bug reports are social Nostr events too. Reuse the normal composer autocomplete so typing
        // `@verita` shows the same people picker, and keep the resulting p-tags below so the chosen
        // person is actually notified rather than merely appearing as text in the issue body.
        attachMentionAutocomplete(ta);
        $('#ri-cancel',root).onclick=closeModal;
        $('#ri-attach',root).onclick=()=>$('#ri-file',root).click();
        $('#ri-file',root).onchange=async e=>{ await _issueAttach(e.target.files, ta, st); e.target.value=''; };
        // Paste is the path that actually matters for a bug report: a screenshot is on the clipboard,
        // not on disk, and making the user save it to a file first is most of the friction.
        ta.addEventListener('paste', e=>{
          const f=[...((e.clipboardData&&e.clipboardData.files)||[])];
          if(f.length){ e.preventDefault(); _issueAttach(f, ta, st); }
        });
        root.addEventListener('dragover',e=>{ if(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files')){ e.preventDefault(); root.classList.add('cmp-drop'); } });
        root.addEventListener('dragleave',e=>{ if(e.target===root) root.classList.remove('cmp-drop'); });
        root.addEventListener('drop',async e=>{
          if(!(e.dataTransfer&&[...(e.dataTransfer.types||[])].includes('Files'))) return;
          e.preventDefault(); root.classList.remove('cmp-drop');
          await _issueAttach(e.dataTransfer.files, ta, st);
        });
        $('#ri-pub',root).onclick=async()=>{
          // EVERYTHING in one try. Tag building used to sit outside it — `_repoPeople(repo)` and
          // `imetaTagsFor(body)` both run real logic over untrusted content, and a throw there rejected
          // this async handler with nothing caught: no message in #ri-status, no toast, the modal frozen
          // mid-publish. On a phone that also means no bottom nav (body.modal-open hides it), i.e. an app
          // you have to kill. Inside the try it becomes a line of red text you can read and report.
          const pub=$('#ri-pub',root);
          try{
            const subj=($('#ri-subj',root).value||'').trim();
            const body=ta.value.trim();
            if(!subj){ st.textContent='A subject is required.'; return; }
            const tags=[['a', addr], ['subject', subj]];
            // Address the owner + maintainers. Without a `p` tag a 1621 notifies NOBODY — not our
            // notifications, not push, not any other nostr client — so an issue could sit unread forever;
            // that is the actual reason filing one felt like shouting into a void. It also covers REPLIES
            // for free: NIP-10 replyTags() carries a parent's `p` tags forward, so everyone on the issue
            // stays on the thread without a second mechanism watching for them.
            _repoPeople(repo).forEach(pk=>{ if(pk!==(S.ME&&S.ME.pubkey)) tags.push(['p',pk]); });
            mentionTags(body).forEach(t=>{ if(!tags.some(x=>x[0]==='p'&&x[1]===t[1])) tags.push(t); });
            imetaTagsFor(body).forEach(t=>tags.push(t));   // NIP-92 media metadata, same as a post
            st.textContent='publishing…';
            if(pub) pub.disabled=true;                  // one issue per press, not one per impatient tap
            const r=await publish(1621, body, tags);
            if(r && r.ok===false){ st.textContent='relay: '+(r.msg||'rejected'); return; }
            toast('issue published');
            closeModal();
            // AFTER the modal is gone, and in its own guard: openRepo re-renders the whole view, and a
            // throw in there used to take out the success path too — the issue was published but the UI
            // looked broken, which is indistinguishable from a failed publish.
            try{ if(S.VIEW==='repo') openRepo(repo); }
            catch(err2){ toast('published — reopen the repo to see it'); }
          }catch(err){
            st.textContent='failed: '+((err&&err.message)||err);
          }finally{
            // Always give the button back. Without this a relay hiccup left the only way forward disabled.
            const b=$('#ri-pub',root); if(b) b.disabled=false;
          }
        };
      });
  }
  // ---------- Branch / tag switcher (self-hosted GRASP repos) ----------
  // Every browse route takes a ref, but nothing told the UI which refs EXIST, so the browser was stuck
  // on one branch. One /refs read fills the picker and names the real default branch.
  async function _rvLoadRefs(feed){
    const j=await _rvJson('refs');
    if(!_rv || S.VIEW!=='repo') return;
    const btn=$('#rv-refpick',feed), lbl=$('#rv-refname',feed), note=$('#rv-refnote',feed);
    if(!j || !j.ok){ if(note) note.textContent='branches unavailable'; return; }
    _rv.refs=j;
    if(_rv.ref==='HEAD'){ _rv.refName=j.default||''; }
    if(lbl) lbl.textContent=_rv.refName||j.default||'HEAD';
    const nb=(j.branches||[]).length, nt=(j.tags||[]).length;
    if(note) note.textContent=`${nb} branch${nb===1?'':'es'}${nt?` · ${nt} tag${nt===1?'':'s'}`:''}`;
    if(btn) btn.onclick=()=>{
      const items=[]
        .concat((j.branches||[]).map(b=>[ 'b:'+b.name, `⎇ ${b.name}` ]))
        .concat((j.tags||[]).map(t=>[ 't:'+t.name, `🏷 ${t.name}` ]));
      if(!items.length){ toast('this repo has no branches yet'); return; }
      openMenuPopover(btn, items, pick=>{
        const name=pick.slice(2), isTag=pick[0]==='t';
        _rvSetRef(feed, isTag ? 'refs/tags/'+name : name, (isTag?'🏷 ':'')+name, isTag);
      });
    };
  }
  // Switching ref invalidates BOTH lazy panels — reload whichever is on screen now and let the other
  // reload when it's next opened, so the two can never show different revisions of the same repo.
  function _rvSetRef(feed, ref, label, isTag){
    if(!_rv) return;
    // A tag is a snapshot, not a branch — the host refuses a commit onto one, so don't offer editing.
    _rv.ref=ref; _rv.refName=label||ref; _rv.path=''; _rv.isTag=!!isTag;
    const lbl=$('#rv-refname',feed); if(lbl) lbl.textContent=_rv.refName;
    const active=($('.rv-tab.active',feed)||{}).dataset||{};
    _rv.filesLoaded=false; _rv.commitsLoaded=false;
    if(active.tab==='files'){ _rv.filesLoaded=true; _loadRepoFiles(feed,''); }
    else if(active.tab==='commits'){ _rv.commitsLoaded=true; _loadRepoCommits(feed); }
  }
  // ---------- Commits (self-hosted GRASP repos) ----------
  async function _loadRepoCommits(feed, path){
    const box=$('#rv-commits',feed); if(!box || !_rv) return;
    box.innerHTML='<div class="spinner"></div>';
    const j=await _rvJson('log', Object.assign({limit:'100'}, path?{path}:{}));
    if(S.VIEW!=='repo' || !_rv) return;
    if(!j||!j.ok){ box.innerHTML='<div class="rv-empty muted small">Couldn’t read the commit history.</div>'; return; }
    const cs=j.commits||[];
    const scope=path?`<div class="rv-scope muted small">🕘 history of <code>${enc(path)}</code> · <a class="fb-crumb" role="button" tabindex="0" id="cm-allhist">show all commits</a></div>`:'';
    if(!cs.length){ box.innerHTML=scope+'<div class="rv-empty muted small">No commits yet.</div>'; }
    else box.innerHTML=scope+`<div class="cm-list">${cs.map(c=>`<div class="cm-row" data-sha="${enc(c.sha||'')}" title="View this commit’s changes">
        <div class="cm-main">
          <div class="cm-subj">${enc(c.subject||'(no message)')}</div>
          <div class="cm-meta"><span class="cm-by">${enc(c.author||'unknown')}</span>
            <span class="cm-when" title="${enc(new Date((c.at||0)*1000).toLocaleString())}">${enc(c.at?timeAgo(c.at):'')}</span></div>
        </div>
        <button class="cm-sha" data-sha="${enc(c.sha||'')}" title="copy full sha">${enc(c.short||'')}</button>
      </div>`).join('')}</div>
      <div class="muted small" style="padding:10px 2px">${cs.length} most recent commit${cs.length===1?'':'s'} on ${enc(_rv.refName||_rv.ref)}</div>`;
    { const a=$('#cm-allhist',box); if(a){ a.onclick=()=>_loadRepoCommits(feed); a.onkeydown=ev=>{ if(ev.key==='Enter'||ev.key===' '){ ev.preventDefault(); a.click(); } }; } }
    // Copying the sha must not also open the diff — the button is inside the clickable row.
    $$('.cm-sha',box).forEach(b=> b.onclick=ev=>{
      ev.stopPropagation();
      copyValue(b.dataset.sha, 'commit sha copied', 'Commit:');
    });
    $$('.cm-row',box).forEach(r=> r.onclick=()=>_openRepoCommit(feed, r.dataset.sha));
  }
  // ---------- One commit's changes (the diff view) ----------
  // Renders a unified diff per file. The host bounds the patch it sends and flags `truncated`, so this
  // never has to defend against a multi-megabyte commit itself.
  function _diffBody(patch){
    const lines=(patch||'').split('\n');
    // Drop git's file header (diff --git / index / mode / ---,+++) by cutting to the first hunk rather
    // than filtering by prefix: a REMOVED line that itself began with "-- " renders as "--- " and a
    // prefix filter would silently eat it.
    let i=lines.findIndex(l=>l.startsWith('@@'));
    if(i<0){
      const bin=lines.find(l=>/^Binary files /.test(l));
      return `<div class="dl dl-meta">${enc(bin||'(no textual changes)')}</div>`;
    }
    let oldNo=0, newNo=0, out=[];
    for(; i<lines.length; i++){
      const l=lines[i];
      if(l.startsWith('@@')){
        const m=/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(l);
        if(m){ oldNo=+m[1]; newNo=+m[2]; }
        out.push(`<div class="dl dl-hunk"><span class="dn"></span><span class="dn"></span><span class="dt">${enc(l)}</span></div>`);
        continue;
      }
      const c=l[0];
      if(c==='\\'){ out.push(`<div class="dl dl-meta"><span class="dn"></span><span class="dn"></span><span class="dt">${enc(l)}</span></div>`); continue; }
      const cls = c==='+'?'dl-add' : c==='-'?'dl-del' : 'dl-ctx';
      const on = c==='+' ? '' : String(oldNo++);
      const nn = c==='-' ? '' : String(newNo++);
      out.push(`<div class="dl ${cls}"><span class="dn">${on}</span><span class="dn">${nn}</span><span class="dt">${enc(l||' ')}</span></div>`);
    }
    return out.join('');
  }
  async function _openRepoCommit(feed, sha){
    const box=$('#rv-commits',feed); if(!box || !sha || !_rv) return;
    box.innerHTML='<div class="spinner"></div>';
    let j={}; try{ j=await fetch(`/client/git/commit?url=${encodeURIComponent(_rv.cloneUrl)}&sha=${encodeURIComponent(sha)}`).then(r=>r.json()); }catch(_){}
    if(S.VIEW!=='repo' || !_rv) return;
    if(!j||!j.ok){ box.innerHTML='<div class="rv-empty muted small">Couldn’t load that commit.</div>'; _loadRepoCommits(feed); return; }
    const files=j.files||[];
    // Big diffs stay collapsed: opening a 4000-line file dump is a choice, not a default.
    const BIG=400;
    const filesHtml=files.map((f,ix)=>{
      const n=(f.patch||'').split('\n').length;
      const open = (!f.binary && n<=BIG) ? ' open' : '';
      return `<details class="df"${open}><summary class="df-hd">
          <span class="df-path">${enc(f.path||'')}</span>
          <span class="df-stat">${f.binary?'<span class="df-bin">binary</span>':`<span class="df-add">+${f.additions|0}</span><span class="df-del">−${f.deletions|0}</span>`}</span>
        </summary><div class="df-body" data-ix="${ix}">${f.binary?'<div class="dl dl-meta">Binary file — download it to inspect.</div>':_diffBody(f.patch)}</div></details>`;
    }).join('');
    box.innerHTML=`<div class="cmv">
      <div class="cmv-top"><button class="btn btn-ghost small" id="cmv-back"><svg class="ic b-ic" aria-hidden="true"><use href="#i-arrow-left"></use></svg>Commits</button>
        <button class="btn btn-ghost small" id="cmv-copy" title="Copy the full sha"><code>${enc(j.short||'')}</code> ⧉</button></div>
      <h3 class="cmv-subj">${enc(j.subject||'(no message)')}</h3>
      ${j.body?`<pre class="cmv-body">${enc(j.body)}</pre>`:''}
      <div class="cmv-meta muted small">${enc(j.author||'unknown')} · ${enc(j.at?new Date(j.at*1000).toLocaleString():'')}
        · <span class="df-add">+${j.additions|0}</span> <span class="df-del">−${j.deletions|0}</span>
        · ${j.file_count|0} file${(j.file_count|0)===1?'':'s'}${(j.parents||[]).length>1?' · merge':''}</div>
      ${j.truncated?'<div class="rv-scope muted small">⚠️ This diff is large and has been shortened — clone the repo to see all of it.</div>':''}
      ${filesHtml||'<div class="rv-empty muted small">This commit changed nothing.</div>'}
    </div>`;
    $('#cmv-back',box).onclick=()=>_loadRepoCommits(feed);
    $('#cmv-copy',box).onclick=()=> copyValue(j.sha||sha, 'commit sha copied', 'Commit:');
  }
  // ---------- Files browser (self-hosted GRASP repos) ----------
  async function _loadRepoFiles(feed, path){
    const box=$('#rv-files',feed); if(!box || !_rv) return;
    _rv.path=path||'';
    box.innerHTML='<div class="spinner"></div>';
    const j=await _rvJson('tree', {path:path||''});
    if(S.VIEW!=='repo' || !_rv) return;
    if(!j||!j.ok){ box.innerHTML='<div class="rv-empty muted small">Couldn’t list files.</div>'; return; }
    const parts=(path||'').split('/').filter(Boolean);
    // role/tabindex because these are href-less <a>s: without them Tab skipped the breadcrumbs entirely,
    // so there was no keyboard way back UP a directory once you had descended.
    const crumb=(p,label)=>`<a class="fb-crumb" role="button" tabindex="0" data-p="${enc(p)}">${enc(label)}</a>`;
    const crumbs=[crumb('','🏠 root')].concat(parts.map((seg,i)=>crumb(parts.slice(0,i+1).join('/'),seg))).join('<span class="fb-sep">/</span>');
    const rows=(j.entries||[]).map(en=>{
      const ico=en.type==='tree'?'📁':'📄';
      const sz=en.type==='blob'?`<span class="fb-size">${_fmtBytes(en.size)}</span>`:'';
      // Last commit that touched this entry — the message + relative date every forge shows. The
      // host only scans recent history, so an untouched-for-ages file legitimately has none.
      const c=en.commit;
      const msg=c?`<span class="fb-cmsg" title="${enc(c.subject||'')} — ${enc(c.author||'')}">${enc(c.subject||'')}</span>`:'<span class="fb-cmsg"></span>';
      const when=c&&c.at?`<span class="fb-cwhen" title="${enc(new Date(c.at*1000).toLocaleString())}">${enc(timeAgo(c.at))}</span>`:'<span class="fb-cwhen"></span>';
      return `<div class="fb-row" data-type="${en.type}" data-path="${enc(en.path)}"><span class="fb-ico">${ico}</span><span class="fb-name">${enc(en.name)}</span>${msg}${when}${sz}</div>`;
    }).join('');
    // Tip-commit bar, like the one above a GitHub/Gitea file list.
    const h=j.head;
    const headBar=h?`<div class="fb-headbar"><span class="fb-hsha">${enc(h.short||'')}</span>
      <span class="fb-hmsg">${enc(h.subject||'')}</span>
      <span class="fb-hby">${enc(h.author||'')}</span>
      <span class="fb-hwhen" title="${enc(new Date((h.at||0)*1000).toLocaleString())}">${enc(h.at?timeAgo(h.at):'')}</span></div>`:'';
    const tools = _rvMayEdit()
      ? `<div class="fb-tools"><button class="btn btn-ghost small" id="fb-new"><svg class="ic b-ic" aria-hidden="true"><use href="#i-plus"></use></svg>New file</button></div>` : '';
    box.innerHTML=`<div class="fb-crumbs">${crumbs}</div>${tools}${headBar}<div class="fb-list">${rows||'<div class="muted small" style="padding:14px">empty directory</div>'}</div><div id="rv-fileview"></div>`;
    $$('.fb-crumb',box).forEach(a=>{
      a.onclick=()=>_loadRepoFiles(feed,a.dataset.p);
      a.onkeydown=ev=>{ if(ev.key==='Enter'||ev.key===' '){ ev.preventDefault(); a.click(); } };
    });
    $$('.fb-row',box).forEach(r=> r.onclick=()=>{
      if(r.dataset.type==='tree') _loadRepoFiles(feed,r.dataset.path);
      else _viewRepoFile(feed,r.dataset.path);
    });
    { const nb=$('#fb-new',box); if(nb) nb.onclick=async()=>{
        const dir=(path||'');
        const name=await uiPrompt('New file path', {value:dir?dir+'/':'', placeholder:'docs/notes.md'});
        if(name===null) return;
        const p=(name||'').trim().replace(/^\/+/,'');
        if(!p){ toast('a path is required'); return; }
        _editRepoFile(feed, p, '', {isNew:true});
      }; }
  }
  // ---------- One file: view / download / edit / history ----------
  async function _viewRepoFile(feed, path){
    const fv=$('#rv-fileview',feed); if(!fv || !_rv) return;
    fv.innerHTML='<div class="spinner"></div>'; fv.scrollIntoView({block:'nearest'});
    const j=await _rvJson('blob', {path});
    if(S.VIEW!=='repo' || !_rv) return;
    const name=path.split('/').pop();
    if(!j||!j.ok){ fv.innerHTML='<div class="rv-empty muted small">Couldn’t open the file.</div>'; return; }
    // "Download" is a plain link to the streaming endpoint, so the browser (and the app's WebView) uses
    // its own save flow — fetching the bytes into JS just to re-offer them would break on big files.
    const dl=_rvUrl('download',{path});
    const acts=`<span class="fb-fvacts">
        <a class="btn btn-ghost small" href="${enc(dl)}" download="${enc(name)}" title="Download this file"><svg class="ic b-ic" aria-hidden="true"><use href="#i-download"></use></svg>Download</a>
        <button class="btn btn-ghost small" id="fv-hist" title="Commits that touched this file"><svg class="ic b-ic" aria-hidden="true"><use href="#i-clock"></use></svg>History</button>
        ${(_rvMayEdit() && !j.binary)?`<button class="btn btn-neon small" id="fv-edit"><svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>Edit</button>`:''}
        ${_rvMayEdit()?`<button class="btn btn-ghost small" id="fv-del" style="color:var(--danger,#e0245e)"><svg class="ic b-ic" aria-hidden="true"><use href="#i-trash"></use></svg>Delete</button>`:''}
      </span>`;
    const hd=`<div class="fb-fvhd">📄 <span class="fb-fvname">${enc(name)}</span>${acts}</div>`;
    if(j.binary){
      fv.innerHTML=`<div class="fb-fileview">${hd}<div class="muted small" style="padding:14px">Binary file · ${_fmtBytes(j.size||0)} — download it or clone the repo to inspect it.</div></div>`;
    }else{
      const isMd=/\.(md|markdown)$/i.test(name);
      const body=isMd?`<div class="markdown">${mdToHtml(j.text||'')}</div>`:`<pre class="fb-code">${enc(j.text||'')}</pre>`;
      fv.innerHTML=`<div class="fb-fileview">${hd}${body}</div>`;
      if(isMd) fv.querySelectorAll('img').forEach(im=> im.onclick=()=>openLightbox(im.currentSrc||im.src));
    }
    { const h=$('#fv-hist',fv); if(h) h.onclick=()=>{
        $$('.rv-tab',feed).forEach(x=>x.classList.toggle('active',x.dataset.tab==='commits'));
        $$('.rv-panel',feed).forEach(pn=> pn.hidden = pn.dataset.panel!=='commits');
        _rv.commitsLoaded=true; _loadRepoCommits(feed, path);
      }; }
    { const e=$('#fv-edit',fv); if(e) e.onclick=()=>_editRepoFile(feed, path, j.text||'', {}); }
    { const d=$('#fv-del',fv); if(d) d.onclick=async()=>{
        if(!await uiConfirm(`Delete ${name} from ${_rv.refName||_rv.ref}?`)) return;
        await _commitRepoFile(feed, {path, delete:true, base:'', message:'delete '+path});
      }; }
  }
  // The editor. A plain monospace textarea on purpose: it has to work identically in the PWA, the
  // desktop shell and on a phone keyboard, and a code-editor library would be a large dependency the
  // relay-only client deliberately does without.
  async function _editRepoFile(feed, path, text, opts){
    const fv=$('#rv-fileview',feed); if(!fv || !_rv) return;
    const o=opts||{};
    const name=path.split('/').pop();
    // Pin the branch tip as it is NOW: the commit is sent with it as `base`, so if someone else pushes
    // while this editor is open the write is refused (409) instead of quietly reverting their work. It
    // must be the tip of the whole ref — the file listing's header commit is only the newest commit that
    // touched the directory being listed, which for a subdirectory is usually much older.
    _rv.base='';
    try{ const t=await _rvJson('log',{limit:'1'});
      if(t && t.ok && (t.commits||[]).length) _rv.base=t.commits[0].sha||''; }catch(_){}
    if(S.VIEW!=='repo' || !_rv) return;
    fv.innerHTML=`<div class="fb-fileview fb-editing">
      <div class="fb-fvhd">✏️ <span class="fb-fvname">${enc(path)}</span>
        <span class="muted small">on ${enc(_rv.refName||_rv.ref)}</span></div>
      <div class="fb-edit">
        <textarea class="input fb-editor" id="fe-text" spellcheck="false" autocapitalize="off" autocorrect="off">${enc(text||'')}</textarea>
        <input class="input" id="fe-msg" maxlength="200" placeholder="${o.isNew?`create ${enc(name)}`:`update ${enc(name)}`}">
        <div class="fb-editacts">
          <span class="muted small" id="fe-status">Signed with your Nostr key — the same authority as a push.</span>
          <span class="fb-editbtns">
            <button class="btn btn-ghost small" id="fe-cancel">Cancel</button>
            <button class="btn btn-neon small" id="fe-save"><svg class="ic b-ic" aria-hidden="true"><use href="#i-check"></use></svg>Commit</button>
          </span>
        </div>
      </div></div>`;
    fv.scrollIntoView({block:'nearest'});
    const ta=$('#fe-text',fv);
    // Tab inserts a tab instead of leaving the box — in a code editor that is what the key means.
    ta.addEventListener('keydown',ev=>{
      if(ev.key==='Tab'){ ev.preventDefault();
        const s=ta.selectionStart, e=ta.selectionEnd;
        ta.value=ta.value.slice(0,s)+'\t'+ta.value.slice(e); ta.selectionStart=ta.selectionEnd=s+1; }
      if((ev.ctrlKey||ev.metaKey)&&ev.key==='Enter'){ ev.preventDefault(); $('#fe-save',fv).click(); }
    });
    setTimeout(()=>{ try{ ta.focus(); }catch(_){} },30);
    $('#fe-cancel',fv).onclick=()=>{ if(o.isNew) fv.innerHTML=''; else _viewRepoFile(feed, path); };
    $('#fe-save',fv).onclick=()=>_commitRepoFile(feed, {path, content:ta.value,
      message:($('#fe-msg',fv).value||'').trim() || ((o.isNew?'create ':'update ')+path)});
  }
  // The write itself. Authorization is a NIP-98 (kind-27235) event signed by the user and bound to THIS
  // repo's write route; the git host re-verifies it against the repo's NIP-34 maintainer list, so the
  // web editor has exactly the authority of `git push` and no more. On success the host hands back the
  // kind-30618 tags naming the new tip and we publish them signed by the user — that event is what
  // authorizes the NEXT push, so skipping it would leave the repo's signed state behind reality.
  async function _commitRepoFile(feed, body){
    if(S.GUEST || !S.ME){ _guestPrompt(); return; }
    if(!_rv) return;
    const st=$('#fe-status',feed) || null;
    const say=t=>{ if(st) st.textContent=t; else toast(t); };
    say('signing…');
    let auth;
    try{
      const u=_rv.cloneUrl.replace(/\/+$/,'')+'/edit';
      auth='Nostr '+btoa(JSON.stringify(await sign(27235,'',[['u',u],['method','POST']])));
    }catch(err){ say('couldn’t sign: '+((err&&err.message)||err)); return; }
    say('committing…');
    let j={};
    try{
      j=await fetch('/client/git/edit',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:_rv.cloneUrl, ref:_rv.ref==='HEAD'?(_rv.refName||'HEAD'):_rv.ref,
          path:body.path, content:body.content||'', message:body.message||'',
          // `base` guards an EDIT (opened minutes ago); a delete was just confirmed, so it relies on
          // the host's own read-then-update-ref CAS instead of a stale editor-session base.
          base:(body.base!==undefined?body.base:(_rv.base||'')), delete:!!body.delete, auth})}).then(r=>r.json());
    }catch(err){ say('the server didn’t answer'); return; }
    if(!j || !j.ok){
      const msg = j && (j.detail||j.error) || 'commit failed';
      say(j && j.error==='stale' ? 'The branch moved — reopen the file and re-apply your change.' : msg);
      return;
    }
    if(Array.isArray(j.state_tags_30618) && j.state_tags_30618.length){
      try{ await publish(30618,'',j.state_tags_30618); }
      catch(_){ /* the commit landed; a failed state publish is reported by the next push, not here */ }
    }
    toast(j.unchanged?'no changes to commit':('committed '+(j.short||'')));
    // Re-render from the server rather than from what we just typed, so what's on screen is what landed.
    _rv.commitsLoaded=false;
    await _loadRepoFiles(feed, _rv.path||'');
    if(!body.delete) _viewRepoFile(feed, body.path);
  }
  return { repoCard, openRepo, renderRepos, newRepoIssue, _repoTag };
};

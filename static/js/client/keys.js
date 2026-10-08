/* keys.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_keysMod()`) at the
 * point where this code used to sit — its top-level statements register the global keyboard listeners
 * (shortcuts, vim movement, keyboard scrolling), which have to be live from the first keystroke, in
 * the same order as before, not after a lazy load. The code below is app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCKeysFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.GUEST, S.VIEW, S._vimPane
  const {
    $, VideoMount, _FOCUSABLE, _guestPrompt, compose, effectPost, enc, modal, openThread,
    switchView, toast,
  } = dep;

  // ---------- keyboard shortcuts ----------
  // Alt+<letter> throughout, rather than the bare letters some clients use: this app has a composer sitting
  // in the timeline and search boxes in half the views, so an unmodified "m" would be a keystroke the user
  // meant to type. Alt is also what the sidebar's own access keys would be, so it reads as "the app's key".
  const SHORTCUTS = [
    ['p', '@compose',      'New post'],
    ['l', '@react',        'React to the selected post'],
    ['h', 'home',          'Home'],
    ['i', 'ai',            'PosterChan AI'],
    ['n', 'notifications', 'Notifications'],
    ['b', 'bookmarks',     'Bookmarks'],
    ['m', 'messages',      'Messages'],
    ['g', '@games',        'Games'],          // a group in the sidebar, not a view — see _runShortcut
    ['e', 'global',        'Nostrverse'],
    ['t', 'trending',      'Trending'],
    ['s', 'streams',       'Streams'],
    ['a', 'articles',      'Articles'],
    ['d', 'drafts',        'Drafts'],
    ['f', 'blossom',       'Files'],
    ['c', 'chat',          'Chat'],
    ['w', 'news',          'News'],
    ['q', 'websearch',     'Web Search'],
    ['y', 'calendar',      'Calendar'],
    ['k', 'markets',       'Markets'],
    ['r', 'meme',          'Meme Builder'],
    ['u', 'stats',         'Server Stats'],
    ['$', 'budget',        'Budget'],
    [',', 'settings',      'Settings'],
    ['/', '@help',         'This list'],
  ];
  function _runShortcut(target){
    if(target==='@games'){
      // Games are a sidebar GROUP, not a view, so there is nothing to switch to — expand the group (and on
      // a phone open the sidebar, or it would expand something that isn't on screen).
      const sub=$('#games-sub'), chev=$('#games-chev');
      ClientSettings.set('gamesOpen', true);
      if(sub) sub.classList.remove('collapsed');
      if(chev) chev.textContent='▾';
      const sb=$('#sidebar'); if(sb && sb.classList.contains('open')===false) sb.classList.add('open');
      if(sub && sub.scrollIntoView) sub.scrollIntoView({block:'nearest'});
      return;
    }
    if(target==='@compose'){
      // The one shortcut that isn't a view. Guests get the same nudge the compose button gives them
      // rather than an empty modal they cannot post from.
      if(S.GUEST){ _guestPrompt(); return; }
      compose(); return;
    }
    if(target==='@react'){ _postAction('react'); return; }   // Alt+L reacts in EITHER mode
    if(target==='@help'){ _shortcutHelp(); return; }
    // Alt+I when the AI view is ALREADY up means "put me back in the box" (you pressed Escape to reach a
    // shortcut and now want to keep typing) — NOT a re-render, which remounts the chat and would throw the
    // half-written draft away. That makes Escape ⇄ Alt+I a real round trip.
    if(target==='ai' && S.VIEW==='ai'){ const t=$('#ai-input'); if(t){ try{ t.focus(); }catch(_){ } return; } }
    switchView(target);
    // Entering it fresh from the KEYBOARD puts the caret where you were going to type anyway — the same
    // courtesy opening a DM from the keyboard already gets. Deliberately not inside switchView: a TAP on
    // the sidebar must NOT throw up the on-screen keyboard, and that is the only other way in.
    if(target==='ai') _focusSoon('#ai-input');
  }
  // Perform one of the selected post's actions by name. Clicking its own button, as everywhere else, so
  // the guest guard / already-reposted check / counts / toasts all come along.
  function _postAction(name){
    const el=_selEl();
    if(!el){ toast('select a post first — ↑/↓ or j/k'); return; }
    if(name==='effect'){
      if(window.PC_NOSTR_ONLY) return;
      const n=_rowNote(el); if(n) effectPost(n.id, n.pk);
      return;
    }
    const b=el.querySelector('.act[data-a="'+name+'"]');
    if(b) b.click();
  }
  // Post actions on Alt, for the keys that are NOT already views: Alt+Q quote, Alt+Z tip (and Alt+L react,
  // which is in SHORTCUTS and works in both modes). Alt+R/B/E deliberately stay Meme / Bookmarks /
  // Nostrverse — taking them cost three view shortcuts and bought nothing, because bare r, b and e do not
  // collide with hjklgG and still act on the post in Vim mode. `l` was the only real collision.
  const _VIM_ALT_POST = { q:'quote', z:'tip' };
  function _shortcutHelp(){
    const POSTLBL={ reply:'Reply to the selected post', repost:'Boost the selected post',
                    quote:'Quote the selected post', tip:'Tip the selected post', effect:'Effect on the selected post' };
    const row=([k,,label])=>{
      // In Vim mode Alt+<r/b/q/z/e> act on the post instead of switching view — say so here rather than
      // listing a binding that no longer does what it claims.
      const over=_vimOn() && _VIM_ALT_POST[k];
      const lbl=over ? POSTLBL[_VIM_ALT_POST[k]] : label;
      return `<div class="ks-row"><kbd>Alt</kbd><span class="ks-plus">+</span><kbd>${enc(k.toUpperCase())}</kbd><span class="ks-lbl">${enc(lbl)}</span></div>`;
    };
    // One helper instead of repeating the same .map() for every block — there are a dozen of them now.
    const sec=(title, rows)=> !rows.length ? '' : `<div class="ks-sec">${enc(title)}</div><div class="ks-grid">`
      + rows.map(([k,l])=>`<div class="ks-row"><kbd>${enc(k)}</kbd><span class="ks-lbl">${enc(l)}</span></div>`).join('')
      + '</div>';
    modal('<h3>⌨️ Keyboard shortcuts</h3><div class="ks-grid">'+SHORTCUTS.map(row).join('')+'</div>'
      +(_vimOn() ? sec('Vim movement', [['j','Down'],['k','Up'],['h','Left / nav rail'],['l','Right / notifications'],
                                        ['gg','Top'],['G','Bottom'],['j / k','Also change a focused dropdown'],
                                        ['k','At the top row — into this view\u2019s action bar'],
                                        ['h / l','Walk that bar; j drops back to the list']]) : '')
      +sec('Anywhere', [['/','Search'],['Alt+Enter','This view’s main action (Go Live, ＋ New, Write article…)'],
                        ['Alt+←','Back out of what you opened'],['Esc','Leave a text box / close a dialog'],
                        ['[  ]','Previous / next tab']])
      +sec('Menus and pickers', [['↑ ↓ ← →','Move'+(_vimOn()?' (or h j k l)':'')],['Enter','Choose'],['Esc','Close']])
      +sec('On the selected post', [['R','Reply'],['B','Boost (repost)'],['Q','Quote'],[_vimOn()?'F':'L','React'],
                                    ['Z','Tip'],['E','Effect'],['V','Play its video · open its image or link'],
                                    ['Enter','Open thread'],['Esc','Deselect']])
      +sec('On a selected file', [['O','Open'],['C','Copy the URL'],['M','Move to a folder'],['D','Delete'],
                                  ['Enter','Open'],['↓ Load more','Is a row too — Enter keeps paging']])
      +sec('On a selected news item', [['S','Share (also Markets)'],['U','Summarize'],['Enter','Open the article']])
      +sec('On a selected web-search result', [['S','Share'],['N','Save to Notes'],['U','Summarize'],
                                              ['Enter','Open the page'],['Esc','Back to results']])
      +sec('Viewing an image', [['C','Copy'],['S','Save'],['B','Save to Files'],['← / →','Previous / next'],
                                ['Esc','Close']])
      +sec('Games', [['A – Z','Guess a letter (Hangman)'],['1 – 9','Play that square / drop that column'],
                     ['Type a move','Chess']])
      +(window.PC_NOSTR_ONLY ? '' : sec('In AI Chat',
        [['Alt+I','Open it / jump back into the message box'],['Esc','Leave the box — lands on the newest reply'],
         ['↑ / ↓','Your previous messages'],['Page Up/Dn','Scroll the conversation while typing'],
         ['Alt+Enter','Reach 🏠 Home · ＋ New · Agents'],['Enter','Send'],['Shift+Enter','New line']]
        .concat(_vimOn() ? [['k','At the top message — into the toolbar (then h / l)']] : [])))
      +'<div class="muted small ks-foot">Arrow keys step through rows; Page Up/Down, Space and Home/End scroll — '
      +'though Space plays/pauses a video once you have tabbed onto it.</div>');
  }
  // Which letter an Alt chord means. `e.key` alone is not enough: Android's keymap gives most letters NO
  // character while Alt is held (Generic.kcm: `ctrl, alt, meta: none`), so the tablet/APK sees key values
  // like "Unidentified" — or the symbol layer's character — and every Alt shortcut fell on the floor there.
  // `e.code` is the PHYSICAL key and survives that, so it is the fallback. The layout's own character is
  // still tried first (a layout that really does put a letter there wins); the fallback is also what keeps
  // Option+E on a Mac — a dead key, "é" — landing on Nostrverse rather than nothing.
  const _CODE_CHAR = { Comma:',', Slash:'/' };
  function _altChars(e){
    const out=[];
    const k=(e.key||'').toLowerCase();
    if(k.length===1) out.push(k);
    const c=e.code||'';
    const m=/^Key([A-Z])$/.exec(c);
    const fromCode = m ? m[1].toLowerCase() : _CODE_CHAR[c];
    if(fromCode && fromCode!==out[0]) out.push(fromCode);
    return out;
  }
  (function(){
    const MAP=new Map(SHORTCUTS.map(([k,v])=>[k,v]));
    document.addEventListener('keydown', e=>{
      if(!e.altKey || e.ctrlKey || e.metaKey) return;
      if(e.defaultPrevented) return;   // an overlay handled it in capture (and may already have closed itself)
      // …or a Vim-mode post action. Q and Z are not view shortcuts at all, so gating purely on the view
      // map dropped Alt+Q / Alt+Z on the floor.
      const bound = (c) => MAP.has(c) || (_vimOn() && !!_VIM_ALT_POST[c]);
      const k = _altChars(e).find(bound);
      if(!k) return;
      const t=e.target;
      // Alt+letter inside a text field is a real editing shortcut on some platforms (macOS word-jumps),
      // so leave a focused field alone — same rule the scroll keys use.
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||''))) return;
      if(document.body.classList.contains('modal-open') || document.querySelector('.lightbox')) return;
      if(_vimOn() && _VIM_ALT_POST[k]){ e.preventDefault(); _postAction(_VIM_ALT_POST[k]); return; }
      e.preventDefault();
      _runShortcut(MAP.get(k));
    });
  })();

  // `/` focuses search — what vim, and every other site with a search box, has trained everyone to expect.
  // There are TWO search inputs (sidebar on desktop, topbar on mobile) with CSS showing exactly one at any
  // width, so take whichever is actually on screen rather than picking one and being wrong at a breakpoint.
  // Not gated on !shiftKey: on plenty of layouts `/` IS a shifted key.
  function _searchBox(){
    for(const sel of ['#nav-search-input','#search-input']){
      const el=document.querySelector(sel);
      if(el && (el.offsetParent!==null || getComputedStyle(el).position==='fixed')) return el;
    }
    return null;
  }
  (function(){
    document.addEventListener('keydown', e=>{
      if(e.key!=='/' || e.altKey || e.ctrlKey || e.metaKey) return;
      if(e.defaultPrevented) return;   // an overlay handled it in capture (and may already have closed itself)
      const t=e.target;
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||''))) return;
      if(document.body.classList.contains('modal-open')) return;
      if(document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      const box=_searchBox(); if(!box) return;                   // nothing to focus — leave the key alone
      e.preventDefault();                                        // ...or the "/" lands in the box
      try{ box.focus({preventScroll:true}); }catch(_){ try{ box.focus(); }catch(__){} }
      try{ box.select(); }catch(_){ }                            // a second `/` replaces the last query
    });
  })();

  // The page must be able to RECEIVE keys before it has been clicked. On a fresh launch (most visibly in
  // the desktop shell, where the window opens with the menu bar/chrome holding focus) document.activeElement
  // is not in the page, so keydown never reaches these handlers and every shortcut looked dead until you
  // clicked something. body needs tabindex to be focusable at all — -1 keeps it out of the Tab order.
  (function(){
    const grab=()=>{ try{
      const ae=document.activeElement;
      if(ae && ae!==document.body && ae!==document.documentElement) return;   // something real has focus
      document.body.tabIndex=-1; document.body.focus({preventScroll:true});
    }catch(_){ } };
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', grab, {once:true});
    else grab();
    window.addEventListener('focus', grab);   // re-focusing the window must not need a click either
  })();

  // ---------- keyboard post selection ----------
  // Arrow/Page keys move a SELECTION through the timeline, so the action keys below have something to act
  // on. The actions deliberately click the note's own .act button rather than calling doRepost/compose
  // directly: that row already carries the guest guard, the already-reposted check, the counts and the
  // toasts, and a second path into them would be a second place for those to drift.
  let _selId=null, _selRef=null;   // key survives a re-render; the reference covers cards that carry no id
  // The rows a keyboard selection can land on. Posts in a feed or a thread; CONVERSATIONS in Messages —
  // the same gesture, so it is the same code rather than a second selection model bolted on beside it.
  // First list with any rows on screen wins.
  const _ROW_SEL = [
    // A THREAD first: every post is wrapped in .thread-node[data-tid], whereas the card inside is not
    // guaranteed to carry data-id (a repost whose original is still being fetched renders a placeholder
    // with data-orig instead). Keying on the wrapper is why the cursor no longer stops partway down.
    '#feed .thread-node[data-tid]',
    '#feed article.note[data-id]',       // timeline
    '#feed .notif[data-open]',           // notifications (the updater row has no data-open — not selectable)
    // Every other card view — Streams, Articles, Pics. Deliberately the SAME
    // list the app already uses to mean "a card" (see the long-press handler), so a new card type is picked
    // up here without a second place to remember.
    '#feed .draft-card[data-draft], #feed .draft-art[data-id]',   // Drafts (post + article)
    '#feed .news-card',                  // News (its own keys — see _CARD_KEYS)
    '#feed .ws-card',                    // Web Search results (its own keys — see _CARD_KEYS)
    // Files (a GRID — see _rowStride). "↓ Load more" is a row too: it sits at the END of the same grid,
    // and without it the cursor stopped dead on the last tile — Tab could not save you either, because
    // while a row is selected Tab is scoped to THAT row's own controls.
    '#feed .file-card, #feed .bl-more',
    '#feed .stream-card, #feed .article-card, #feed .channel-card, #feed .fc-card, #feed .pic-card, #feed .repo-card',
    // Budget: a bill row and a plan CARD are both rows. `[data-bill]` deliberately excludes the plan's
    // own header (it carries .bg-row for styling but no data-bill), so the cursor doesn't stop twice on
    // the same plan. Their letters live in _CARD_KEYS; Tab still reaches the ✅/☰ inside a selected row.
    '#feed .bg-row[data-bill], #feed .bg-plan[data-cat]',
    // AI Chat's splash — the starter cards you are greeted with, plus the `help` chip under them. They
    // were real buttons, so Tab reached them, but only one at a time: twenty cards meant twenty presses
    // and no cursor to show where you were. As rows they get the arrows, j/k, and h/l across the grid.
    // All three card grids share one `repeat(auto-fit,minmax(190px,1fr))` template, so the column count
    // measured off the first one is right for the others too (and is 1 on a phone).
    '#feed .ai-welcome .aw-card, #feed .ai-welcome .ai-cmd',
    // AI Chat's transcript. A reply is a row like any other, and its bubble is full of things worth
    // reaching — the guided cards, the command chips, Copy/Reply/Post on generated media, 🔊 Read aloud.
    // None of them were reachable: the view had no selection model at all, so the cursor never entered
    // the transcript and Tab (which is scoped to a selected row) had nothing to scope to.
    '#feed .ai-msgs .ai-msg',
    '#dm-list .dm-peer[data-peer]',      // messages
    // The DETAIL views you open INTO. The list that got you here was navigable, but what it opened was
    // not: Markets' tickers, a chat room's messages. Same rows, same keys.
    '#feed .mkts-card',                  // Markets
    '#ch-msgs .chat-msg[data-mid]',      // an open chat room
    // An open git repo: the file list and the commit list. Both are plain vertical lists, so they get
    // the arrows, j/k and gg/G with no stride. A .fb-row already knows how to open itself (a directory
    // descends, a file opens), which is exactly what Enter on a row does.
    '#rv-files .fb-row, #rv-commits .cm-row',
  ];
  function _noteEls(){
    // HIDDEN rows are not rows. The splash's Agents card is display:none unless you have node access, and
    // the cursor stopping on something invisible reads as the arrows having died. Same test the nav/rail
    // panes already use. A list whose rows are all hidden correctly falls through to the next one.
    for(const sel of _ROW_SEL){ const els=[...document.querySelectorAll(sel)].filter(_vimShown); if(els.length) return els; }
    return [];
  }
  const _rowKey = (el) => el && (el.dataset.tid || el.dataset.draft || el.dataset.id || el.dataset.peer || el.dataset.open || '');
  // The post inside a selected row. A thread row is a .thread-node WRAPPER, so the id/pubkey live on the
  // card within it, not on the row itself.
  function _rowNote(el){
    const art = el.matches('article.note') ? el : el.querySelector('article.note');
    if(!art) return null;
    const id = art.dataset.id || el.dataset.tid || '';
    return id ? { id, pk: art.dataset.pk || '' } : null;
  }
  function _selEl(){
    // Prefer the live element: not every card view gives its cards an id (some listings), and those
    // cannot be found again by key. Fall back to the key so a keyed row survives the feed re-rendering
    // under it — which is what happens constantly on a live timeline.
    if(_selRef && _selRef.isConnected){ _selRef.classList.add('sel'); return _selRef; }
    if(!_selId) return null;
    const el=_noteEls().find(n=>_rowKey(n)===_selId);
    if(el){ _selRef=el; el.classList.add('sel'); }
    return el||null;
  }
  function _selectNote(el){
    // Moving the cursor off a row abandons any button inside it that Tab had focused — otherwise focus
    // would stay behind on the row you just left.
    { const a2=document.activeElement, prev=_selRef;
      if(prev && a2 && a2!==prev && prev.contains(a2) && prev!==el){
        try{ a2.blur(); document.body.focus({preventScroll:true}); }catch(_){ }
      } }
    // A CARD that has focus (cards are Tab stops; a click on an image focuses one) travels WITH the selection.
    // Left behind, the key handler sees "focus is on another row" and every post key does nothing -- and Enter
    // opens the focused card, not the highlighted one (code review).
    const cardFocused=(()=>{ const a3=document.activeElement; return !!(a3 && a3!==el && a3.matches && a3.matches('article.note[data-id]')); })();
    document.querySelectorAll('.sel').forEach(n=>n.classList.remove('sel'));
    if(!el){ _selId=null; _selRef=null; return; }
    _selId=_rowKey(el); _selRef=el; el.classList.add('sel');
    if(cardFocused){
      try{ if(el.matches && el.matches('article.note[data-id]')) el.focus({preventScroll:true});
           else { document.activeElement.blur(); document.body.focus({preventScroll:true}); } }catch(_){ } }
    el.scrollIntoView({block:'nearest'});
  }
  // The note at the top of what you are looking at — where a selection should START, and where it should
  // land again after a page jump.
  function _topNote(){
    const sc=_keyScroller()||document.getElementById('feed'); if(!sc) return null;
    const top=sc.getBoundingClientRect().top;
    return _noteEls().find(n=>n.getBoundingClientRect().bottom > top+4) || null;
  }
  // How many rows sit on one LINE. Files is a grid, so ↑/↓ there should jump a row and ←/→ move one item;
  // a list simply reports 1 and behaves as before. Measured from the elements themselves so it adapts to
  // the column count the layout actually chose at this width.
  function _rowStride(els){
    if(!els || els.length<2) return 1;
    const top=els[0].offsetTop; let n=0;
    for(const el of els){ if(el.offsetTop!==top) break; n++; }
    return Math.max(1, n);
  }
  function _moveSel(dir){
    const els=_rows(); if(!els.length) return false;
    const cur=_selEl();
    if(!cur){ _selectNote(_topNote()||els[0]); return true; }
    const i=els.indexOf(cur);
    if(i<0){ _selectNote(_topNote()||els[0]); return true; }
    const j=i+dir;
    if(j<0 || j>=els.length) return false;   // at an end → fall through to a normal scroll
    _selectNote(els[j]);
    return true;
  }
  // Focus an element that is about to be rendered. Bounded so a view that never produces it (a failed DM
  // load) cannot leave a timer running.
  function _focusSoon(sel, tries){
    tries = tries || 12;
    const t=setInterval(()=>{
      const el=document.querySelector(sel);
      if(el){ clearInterval(t); try{ el.focus({preventScroll:true}); }catch(_){ try{ el.focus(); }catch(__){} } }
      else if(--tries<=0) clearInterval(t);
    }, 50);
  }
  // Escape out of the message box, back to the conversation list, so the arrow keys work again — without
  // it the keyboard path was one-way: you could open a chat but never leave it without the mouse.
  // #ai-input is in the same list for the same reason, and it matters MORE there: the AI view's compose box
  // is where you spend the whole session, and every Alt shortcut, Page Up/Down and the vim keys all bail on
  // a focused text field — so with no way out, reaching any of them meant picking up the mouse.
  // The search boxes are in here too, so `/` in and Escape out is a round trip like every other field.
  const _ESC_FIELDS = new Set(['dm-in','grp-input','tl-cmp-ta','ai-input','nav-search-input','search-input',
                               'ch-input']);   // a chat room's box was the one message field still missing
  document.addEventListener('keydown', e=>{
    if(e.key!=='Escape') return;
    const t=e.target;
    // Escape off a view's action bar. Without this the bar was a dead end for Escape — it only clears a
    // SELECTION, and moving into the bar clears that by design — so the one key that means "get me out of
    // here" everywhere else did nothing exactly where you had just arrived. AI Chat goes back to the
    // compose box (where you live in that view), closing the loop: type → Escape → the transcript → k,k →
    // the bar → Escape → typing. Every other view drops back onto the list it sits above.
    if(_actionBarOf(t)){
      e.preventDefault(); e.stopPropagation();
      const box=t.closest('#feed .ai-bar') ? document.getElementById('ai-input') : null;
      try{ t.blur(); }catch(_){ }
      if(box){ try{ box.focus({preventScroll:true}); }catch(_){ try{ box.focus(); }catch(__){} } }
      else {
        try{ document.body.focus({preventScroll:true}); }catch(_){ }
        const rows=_noteEls(); if(rows.length) _selectNote(rows[0]);
      }
      return;
    }
    if(!t || !_ESC_FIELDS.has(t.id)) return;
    e.preventDefault(); e.stopPropagation();
    try{ t.blur(); }catch(_){ }
    try{ document.body.focus({preventScroll:true}); }catch(_){ }
    // Leaving the timeline composer hands the cursor back to the FIRST POST rather than to nothing, so
    // you resume scrolling from a visible position instead of an empty selection — and flag it, so the
    // k you press next carries on up the timeline instead of dropping you back in the box.
    if(t.id==='tl-cmp-ta'){
      _cmpLeft=true;
      try{ const rows=_noteEls(); if(rows.length) _selectNote(rows[0]); }catch(_){ }
    }
    // AI Chat runs oldest→newest and you are always looking at the BOTTOM of it, so leaving the compose
    // box hands the cursor to the LAST message — the reply that just arrived. Landing on nothing meant the
    // next j jumped to the top of the whole conversation, which is why the buttons on a freshly generated
    // effect were so hard to get to.
    else if(t.id==='ai-input'){
      try{ const rows=_noteEls(); if(rows.length) _selectNote(rows[rows.length-1]); }catch(_){ }
    }
  }, true);

  // ---------- vim movement (opt-in: Settings → "Vim keys") ----------
  // Three columns, one cursor. h/l cross between the nav rail, the feed and the notifications rail; j/k
  // move within whichever column holds the cursor. In a GRID (Files) h/l step through the grid first and
  // only cross columns at its edge — "eventually", as asked.
  let _gPending=0;           // `gg` = top; timestamp of a lone g
  function _vimOn(){ return !!ClientSettings.get('vimKeys', false); }
  const _vimShown = (el) => el.offsetParent!==null || getComputedStyle(el).position==='fixed';
  function _paneRows(pane){
    if(pane==='nav')  return [...document.querySelectorAll('.sidebar .nav-item')].filter(_vimShown);
    if(pane==='rail') return [...document.querySelectorAll('#rb-list .notif')].filter(_vimShown);
    return _noteEls();
  }
  const _rows = () => _paneRows(S._vimPane);
  // Move the cursor to another column. Refuses when that column has nothing on screen (a phone hides both
  // rails), so h/l simply do nothing there rather than losing the selection into an invisible pane.
  function _vimPaneTo(pane){
    const rows=_paneRows(pane);
    if(!rows.length) return false;
    S._vimPane=pane;
    const cur=_selEl();
    _selectNote(rows.includes(cur) ? cur : rows[0]);
    return true;
  }
  (function(){
    document.addEventListener('keydown', e=>{
      if(!_vimOn()) return;
      if(e.altKey||e.ctrlKey||e.metaKey) return;
      const t=e.target;
      // j/k drive a focused <select> the way ↑/↓ already do. Dropdowns are the one control a vim user
      // cannot move with hjkl — the Blossom picker's folder chooser is a select, so changing directory
      // meant reaching for the arrows. Only j/k: h/l would fight the caret in a combobox.
      if(t && t.tagName==='SELECT' && (e.key==='j' || e.key==='k')){
        e.preventDefault();
        const n=Math.max(0, Math.min(t.options.length-1, t.selectedIndex + (e.key==='j'?1:-1)));
        if(n!==t.selectedIndex){ t.selectedIndex=n; t.dispatchEvent(new Event('change',{bubbles:true})); }
        return;
      }
      // Focus is ON the view's action bar (reached by k from the top row, see the k branch below): it is a
      // ROW, not a pane, so h/l walk its buttons and j drops back into the list. This sits BEFORE the
      // text-field guard on purpose — AI Chat's conversation picker is a <select>, which that guard turns
      // away. j/k on a select is already claimed above (it changes the value, as on every other dropdown),
      // so only h/l reach here from one; from a button all four do.
      const _bar = _actionBarOf(t);
      if(_bar && e.key.length===1 && 'hjkl'.includes(e.key)){
        e.preventDefault(); e.stopPropagation();
        if(e.key==='k') return;            // nothing above the bar — stop rather than wrap to the bottom
        if(e.key==='j'){                   // back down to the list, at its first row
          try{ t.blur(); }catch(_){ }
          const rows=_noteEls(); if(rows.length) _selectNote(rows[0]);
          else { try{ document.body.focus({preventScroll:true}); }catch(_){ } }
          return;
        }
        const btns=[...(_bar.querySelectorAll(_FOCUSABLE))].filter(_vimShown);
        const n=btns.indexOf(t) + (e.key==='l' ? 1 : -1);
        if(n>=0 && n<btns.length){ try{ btns[n].focus({preventScroll:true}); }catch(_){ try{ btns[n].focus(); }catch(__){} } }
        return;
      }
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||''))) return;
      if(document.body.classList.contains('modal-open') || document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      const k=e.key;
      if(!'hjklgG'.includes(k) || k.length!==1) return;
      // Leaving a pane whose rows are gone (view switched under us) resets to the feed.
      if(!_paneRows(S._vimPane).length) S._vimPane='feed';
      const rows=_rows();
      if(!rows.length){
        // An EMPTY list still has its action bar, and that is exactly when you want it: "no streams live
        // right now" is the moment you reach for 🔴 Go Live. The k-at-the-top-row path below can never
        // fire here because there is no row to be at the top of, so k did nothing at all.
        if(k==='k' && _focusActionBar()){ e.preventDefault(); e.stopPropagation(); }
        return;
      }
      const stride=(S._vimPane==='feed') ? _rowStride(rows) : 1;
      const cur=_selEl();
      const i=rows.indexOf(cur);
      const take=(n)=>{ e.preventDefault(); e.stopPropagation();
        if(n>0) _cmpLeft=false;    // landed anywhere but the top row → going back up to the box is armed again
        _selectNote(rows[Math.max(0,Math.min(rows.length-1,n))]); };
      // scrollIntoView({block:'nearest'}) moves the MINIMUM distance, so selecting the first post leaves
      // everything above it — the composer, the tab bar — still off screen: you could reach the first row
      // but never the top of the view. gg/G (and k held at the first row) drive the scroller explicitly.
      const _edge=(top)=>{ if(S._vimPane!=='feed') return; const sc=_keyScroller()||document.getElementById('feed');
        if(sc) sc.scrollTop = top ? 0 : sc.scrollHeight; };
      if(k==='G'){ take(rows.length-1); _edge(false); _gPending=0; return; }
      if(k==='g'){
        // No time limit on the second g. A 700ms window meant a slower double-tap silently did nothing,
        // and vim itself does not race you either: a pending g simply waits, and ANY other key clears it
        // (see below), so it cannot get stuck armed.
        if(_gPending){ _gPending=0; take(0); _edge(true); }
        else { _gPending=1; e.preventDefault(); e.stopPropagation(); }
        return;
      }
      _gPending=0;
      if(k==='j'){ take(i<0?0:i+stride); return; }
      if(k==='k'){
        // NO SELECTION is not "already at the first post". `i<0` fell into the branch below, so the very
        // first k in a view — and every k straight after Escaping out of the composer, which clears the
        // selection — went directly into the composer instead of moving the cursor. That is the "locked
        // in the new post box" loop: Escape, press k to carry on scrolling, and you are back inside it.
        if(i<0){ take(0); _edge(true); return; }
        if(i===0){
          take(0); _edge(true);
          // …and one more k from the first post lands IN the composer. Revealing it was not enough: you
          // could see the box but still had to reach for the mouse to write. Escape hands focus back.
          // Not when you have JUST left it, though: k is also how you scroll up, so re-entering on the
          // next press made leaving impossible. One j (or any move away) arms it again.
          if(!_cmpLeft && _focusComposer()) return;
          // Views with no composer up here have an ACTION BAR instead — Streams' 🔴 Go Live, Articles'
          // Write article, AI Chat's toolbar — so k reaches that. Deliberately NOT gated on _cmpLeft: that
          // flag exists because k is also scroll-up, so re-entering the timeline composer right after
          // leaving it would trap you. A bar has no such trap (j is its exit, and nothing sits above it),
          // and _cmpLeft left over from the timeline would otherwise make the bar unreachable in a view it
          // has nothing to do with.
          _focusActionBar();
          return;
        }
        take(i-stride); return;
      }
      // Horizontal. Inside a grid, step through it until the edge; then cross columns.
      const atLeft  = i<0 || stride<2 || (i%stride)===0;
      const atRight = i<0 || stride<2 || ((i+1)%stride)===0 || i===rows.length-1;
      if(k==='h'){
        if(!atLeft){ take(i-1); return; }
        e.preventDefault(); e.stopPropagation();
        _vimPaneTo(S._vimPane==='rail' ? 'feed' : 'nav');
        return;
      }
      if(k==='l'){
        if(!atRight){ take(i+1); return; }
        e.preventDefault(); e.stopPropagation();
        _vimPaneTo(S._vimPane==='nav' ? 'feed' : 'rail');
        return;
      }
    }, true);   // CAPTURE — vim movement must win over the single-letter post actions (l = react)
  })();

  // Set when you Escape out of the timeline composer, so the next k does not walk straight back in. Any
  // move away (j) clears it — going up to the box on purpose still works, it just is not automatic.
  let _cmpLeft=false;
  // A view's ACTION BAR (_ACTION_BAR: Go Live, Write article, Add torrent, AI Chat's toolbar, Files'
  // drop zone), treated as the row ABOVE the first card — the same relationship the timeline composer has
  // to the first post, so k at the top of the list moves into it. Alt+Enter already reached these, but the
  // vim path could reach every row and NONE of the buttons over them, which in Streams is the whole point
  // of the view: you could scroll the stream list but never get to 🔴 Go Live.
  function _actionBarOf(el){
    if(!el || !el.closest) return null;
    for(const sel of _ACTION_BAR){ const b=el.closest(sel); if(b) return b; }
    return null;
  }
  function _focusActionBar(){
    const b=_viewAction();                 // the first focusable in whichever bar this view has
    if(!b) return false;
    _selectNote(null);                     // the cursor is in the bar now, not on a row
    try{ b.focus({preventScroll:true}); }catch(_){ try{ b.focus(); }catch(__){} }
    return true;
  }

  // The timeline's inline composer, treated as the row ABOVE the first post: k at the top moves into it.
  // Only where it exists (home/global/trending) and is on screen — elsewhere k just stops at the top.
  function _focusComposer(){
    const ta=document.getElementById('tl-cmp-ta');
    if(!ta || !(ta.offsetParent!==null)) return false;
    if(document.activeElement===ta) return true;
    _selectNote(null);                     // the cursor is in the composer now, not on a post
    try{ ta.focus({preventScroll:true}); }catch(_){ try{ ta.focus(); }catch(__){} }
    return true;
  }

  // Tab moves INTO the selected row's buttons, and cycles among them. A draft card's Edit / Delete / Send
  // (and a post's action row) are ordinary buttons, so Tab already reached them — but only after walking
  // everything else on the page first, which is useless when a cursor is already sitting on the row you
  // mean. Scoped while a row is selected; Escape drops back out to row selection.
  (function(){
    document.addEventListener('keydown', e=>{
      if(e.key!=='Tab' || e.ctrlKey || e.metaKey || e.altKey) return;
      if(document.body.classList.contains('modal-open')) return;   // the modal trap owns Tab there
      if(document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      const row=_selEl(); if(!row) return;
      // Only claim Tab when the cursor is actually AT the row — on it, inside it, or nowhere yet. A
      // selected row used to grab Tab no matter where focus was, so once Alt+Enter had put you on the AI
      // toolbar, Tab yanked you back into the selected message instead of walking 🏠 → ＋ New → Agents.
      { const a=document.activeElement;
        const atRow = !a || a===document.body || a===document.documentElement || a===row || row.contains(a);
        if(!atRow) return; }
      const els=[...row.querySelectorAll(_FOCUSABLE)].filter(el=>el.offsetParent!==null);
      if(!els.length) return;
      const i=els.indexOf(document.activeElement);
      e.preventDefault();
      const n = i<0 ? (e.shiftKey ? els.length-1 : 0)
                    : (e.shiftKey ? (i<=0 ? els.length-1 : i-1) : (i>=els.length-1 ? 0 : i+1));
      try{ els[n].focus({preventScroll:true}); }catch(_){ try{ els[n].focus(); }catch(__){} }
    }, true);
  })();

  // Games: type your move. Chess already took typed moves (it has a move box); the rest were tap-only,
  // which is worst in Hangman — a word game you could not type a letter into. Every board already renders
  // its moves as elements carrying the value, so a keypress just presses the right one: a–z guesses a
  // letter, 1–9 plays that Tic-Tac-Toe cell (the cells are NUMBERED on screen for exactly this) or drops
  // into that Connect Four column. Blackjack and Hold'em are left alone — their actions are ordinary
  // buttons in a row, which Tab already handles.
  // Nothing here can collide with the post keys or hjkl: both of those bail when no row is selected, and
  // a game view has no rows.
  (function(){
    document.addEventListener('keydown', e=>{
      if(e.altKey||e.ctrlKey||e.metaKey) return;
      const t=e.target;
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||''))) return;
      if(document.body.classList.contains('modal-open')) return;
      if(document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      if(e.defaultPrevented) return;   // an overlay handled it in capture (and may already have closed itself)
      const k=(e.key||'').toLowerCase();
      let b=null;
      if(/^[a-z]$/.test(k)) b=document.querySelector(`#feed .hm-key[data-l="${k}"]:not([disabled])`);
      else if(/^[1-9]$/.test(k)){
        b=document.querySelector(`#feed .ttt-cell.empty[data-i="${+k-1}"]`)
          || document.querySelector(`#feed .c4-cell[data-c="${+k-1}"]`);
      }
      if(!b || b.offsetParent===null) return;
      e.preventDefault();
      b.click();
    });
  })();

  // Alt+← goes BACK out of whatever you opened. Every detail view — an article, a chat
  // room, a stream, a DM — puts a "←" button in its header, and every one of them
  // was mouse-only: you could open the thing from the keyboard and then had no way out of it.
  // An explicit list rather than [id$="-back"], because that would also match #follow-all-back, which
  // FOLLOWS EVERYONE BACK. le-back / ae-back are left out too: those read "← Cancel" and abandon an edit
  // in progress, which is not something a navigation key should do by accident.
  const _BACK_BTNS = ['#fc-back','#ch-back','#comm-back','#art-back','#st-back','#li-back','#dm-back',
                      '#grp-back','#badm-back','#th-back','#repo-back'];
  function _backBtn(){
    for(const sel of _BACK_BTNS){
      const b=document.querySelector(sel);
      if(b && b.offsetParent!==null) return b;
    }
    return null;
  }
  (function(){
    document.addEventListener('keydown', e=>{
      if(!(e.key==='ArrowLeft' || e.code==='ArrowLeft')) return;   // e.code too — see the Alt+Enter note
      if(!e.altKey || e.ctrlKey || e.metaKey) return;
      if(e.defaultPrevented) return;   // an overlay handled it in capture (and may already have closed itself)
      if(document.body.classList.contains('modal-open')) return;
      if(document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      const b=_backBtn(); if(!b) return;      // nothing open → leave Alt+← to the browser's own history
      e.preventDefault();
      b.click();
    });
  })();

  // Alt+Enter reaches the button a view puts ABOVE its content — Go Live in Streams, Write article, Add
  // torrent, Sell something, Announce a repo. Tab could not get there: while a card is selected Tab is
  // scoped to that card, and with nothing selected it walked the whole sidebar first. Same shape in every
  // view, so one key does the lot.
  // ...and AI Chat's toolbar (🏠 Home, the conversation picker, ＋ New, Agents, 🔊, 🗑️), which had the same
  // problem: real buttons, but only after Tab had walked the whole sidebar. Landing on it puts plain Tab
  // on the rest of the row, since nothing is selected there.
  const _ACTION_BAR = ['#feed .streams-top', '#feed .art-top', '#feed .ai-bar',
                       '#feed .drop-zone'];   // Files — "choose files" is this view's main action
  function _viewAction(){
    for(const sel of _ACTION_BAR){
      for(const bar of document.querySelectorAll(sel)){
        const b=[...bar.querySelectorAll(_FOCUSABLE)].find(el=>el.offsetParent!==null);
        if(b) return b;
      }
    }
    return null;
  }
  (function(){
    document.addEventListener('keydown', e=>{
      // e.code as well as e.key, for the same reason _altChars exists: on an Android hardware keyboard the
      // Alt layer can report key:"Unidentified" (see the Generic.kcm note), which is why this stayed dead
      // on the tablet while working on a desktop.
      if(!(e.key==='Enter' || e.code==='Enter' || e.code==='NumpadEnter')) return;
      if(!e.altKey || e.ctrlKey || e.metaKey) return;
      if(e.defaultPrevented) return;   // an overlay handled it in capture (and may already have closed itself)
      if(document.body.classList.contains('modal-open')) return;
      if(document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      // Deliberately NO text-field guard. The other Alt keys have one because Alt+<letter> and Alt+←/→ are
      // real editing shortcuts on some platforms (macOS word-jumps), but Alt+Enter is not an editing
      // shortcut anywhere — and the guard was the whole bug here: AI Chat's action bar (🏠 Home, ＋ New,
      // Agents, 🔊, 🗑️) is unreachable exactly when you are typing, which in that view is always.
      const b=_viewAction(); if(!b) return;
      e.preventDefault();
      // First press moves the cursor there (so you can see what you are about to do); pressing it again
      // — or Enter, which the button handles itself — runs it. Nothing fires by surprise.
      if(document.activeElement===b) b.click();
      else { try{ b.focus({preventScroll:true}); }catch(_){ try{ b.focus(); }catch(__){} } }
    });
  })();

  // [ and ] cycle the TAB BAR of whatever view is open — profile's Notes/Replies/Media/Articles/Streams,
  // the timeline's Home/Nostrverse/Trending, notification filters, the Files tabs. Generic rather than
  // profile-only because they are all just a row of buttons with one marked current; the marker is `on`
  // in some bars and `active` in others, so both are accepted.
  const _TAB_SEL = ['#feed .prof-tabs .prof-tab', '#feed .tl-tabs .tltab', '#feed .notif-tabs .ntab',
                    '#feed .files-tabs .ftab', '#feed .np-tabs .np-tab',
                    '#feed .rv-tabs .rv-tab',        // an open repo: README / Files / Commits / Issues / Patches
                    '#feed .us-tabs .us-tab'];       // Settings / User Settings
  function _tabGroup(){
    for(const sel of _TAB_SEL){
      const els=[...document.querySelectorAll(sel)].filter(el=>el.offsetParent!==null);
      if(els.length>1) return els;
    }
    return null;
  }
  function _cycleTab(dir){
    const els=_tabGroup(); if(!els) return false;
    let i=els.findIndex(el=>el.classList.contains('active')||el.classList.contains('on'));
    if(i<0) i=0;
    els[(i+dir+els.length)%els.length].click();
    return true;
  }
  (function(){
    document.addEventListener('keydown', e=>{
      if(e.altKey||e.ctrlKey||e.metaKey) return;
      if(e.key!=='[' && e.key!==']') return;
      if(e.defaultPrevented) return;   // an overlay handled it in capture (and may already have closed itself)
      const t=e.target;
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||''))) return;
      if(document.body.classList.contains('modal-open') || document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      if(_cycleTab(e.key===']' ? 1 : -1)){ e.preventDefault(); _selectNote(null); }   // new tab, new list
    });
  })();

  // Act on the selected post by pressing its own button. Bare letters (no Alt): they are what every other
  // client uses, and the text-field guard already keeps them out of anything being typed into.
  // `l` is react — until Vim keys are on, where l is MOVEMENT and react moves to f (favourite). The Alt+/
  // sheet is generated from this, so whichever is live is what the sheet shows.
  const _postKeys = () => _vimOn() ? { r:'reply', b:'repost', q:'quote', f:'react', z:'tip' }
                                   : { r:'reply', b:'repost', q:'quote', l:'react', z:'tip' };
  // Cards that carry their OWN buttons rather than the post action row, so they are matched by class
  // instead of data-a: which letter presses which of the card's controls. One table because all three
  // worked the same way already, and adding a fourth card type should not mean a fourth copy of it.
  // Pressing the card's real button means its guards come along — `d` still goes through delBlob's
  // "Delete this blob?" confirm exactly as clicking ✕ does, and Markets' S is the same Share the News
  // card already used, so the letter means one thing everywhere.
  const _CARD_KEYS = [
    ['.file-card', { o:'a', c:'.copy', m:'.movebtn', d:'.del' }],
    ['.news-card', { s:'.news-post', u:'.news-sum' }],
    // Web Search. S and U are News' letters for the same two actions; N is Save to Notes (the
    // card has no other n-word control, and it is the action this screen is FOR).
    ['.ws-card', { s:'.ws-share', u:'.ws-sum', n:'.ws-note' }],
    ['.mkts-card', { s:'.mkts-post' }],
    // Budget. `p` is the one you press all day (pay / un-pay), so it gets the letter even though the
    // GLOBAL p is "New post" — global shortcuts are Alt+p, so there is no collision. querySelector
    // finds the plan header's own ✅/☰ rather than an item's, because the header comes first.
    ['.bg-row[data-bill]', { p:'.bg-check', m:'.bg-more' }],
    ['.bg-plan[data-cat]', { p:'.bg-check', m:'.bg-more', a:'.bg-additem' }],
  ];
  (function(){
    document.addEventListener('keydown', e=>{
      if(e.altKey||e.ctrlKey||e.metaKey) return;
      // Somebody above already claimed this keystroke. Checking the overlay classes is NOT enough: the
      // confirm dialog handles Enter in the CAPTURE phase and removes itself there, so by the time this
      // bubble-phase handler runs the .uiconfirm-bg it would have matched on is already gone. That is why
      // deleting a file with `d` then confirming with Enter also OPENED the file — the very same Enter
      // fell through to the Enter branch below, which presses a file card's <a>.
      if(e.defaultPrevented) return;
      const t=e.target;
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName||''))) return;
      if(document.body.classList.contains('modal-open') || document.querySelector('.lightbox,.uiconfirm-bg,.emoji-pop,.menu-pop')) return;
      const k=(e.key||'').toLowerCase();
      if(k==='escape'){
        if(_selId){
          e.preventDefault();
          // If Tab had moved focus onto one of the row's buttons, the first Escape steps back out to the
          // row; a second clears the selection. Otherwise a single Escape would do both at once.
          const row=_selEl();
          if(row && row.contains(document.activeElement) && document.activeElement!==row){
            try{ document.activeElement.blur(); document.body.focus({preventScroll:true}); }catch(_){ }
          } else _selectNote(null);
        }
        return;
      }
      const el=_selEl(); if(!el) return;      // nothing selected → these keys mean nothing yet
      // Once Tab has moved focus onto one of the row's own controls, that control owns the keyboard:
      // Enter must press the focused button, not re-open the row, and r/b/q must not fire underneath it.
      { const a2=document.activeElement;
        if(a2 && a2!==el && el.contains(a2)) return;
        // Post cards are Tab stops too (cards.js). A card that HAS FOCUS is the one the keyboard is on,
        // even if the arrow-key selection is still on another row — acting on the selected one would
        // open, reply to or like a post other than the one a screen reader just read out.
        if(a2 && a2!==el && a2.matches && a2.matches('article.note[data-id]')) return;
        // And Enter on any other FOCUSED control is that control's press — a toolbar button, the account
        // card — never "open the selected row" (preventDefault here cancelled the button's own click).
        if(e.key==='Enter' && a2 && a2!==el && a2!==document.body && a2.matches &&
           a2.matches('button,a[href],summary,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="option"],[role="checkbox"],[role="switch"]')) return; }
      // 🎬 Effect lives in the post's ☰ menu rather than the action row, so it has no button to click —
      // call it directly, behind the same PC_NOSTR_ONLY gate the menu entry uses (a Nostr-only node has
      // no AI backend to render one).
      if(k==='e' && !window.PC_NOSTR_ONLY){
        const n=_rowNote(el);
        if(n){ e.preventDefault(); effectPost(n.id, n.pk); }
        return;
      }
      // v plays/pauses the video in the selected row. NOT Space: Space already pages the feed (and
      // Shift+Space pages back), and since arrowing through a timeline always leaves a row selected,
      // taking it would hijack scrolling on exactly the posts you are most likely to be scrolling past.
      // Works on anything that holds a video — a post, a thread node, a chat or AI message.
      if(k==='v'){
        const vid=el.querySelector('video');
        if(vid){ e.preventDefault(); if(vid.paused) { const p=vid.play(); if(p&&p.catch) p.catch(()=>{}); } else vid.pause(); return; }
        // Data saver renders "▶️ tap to load video" instead of the real element — press that, then v again.
        const ph=el.querySelector('.vid-hold');
        if(ph){ e.preventDefault(); ph.click(); return; }
        // No video → the post's IMAGE. Exactly the selector the feed's own click handler uses, and pressed
        // by clicking it, so the lightbox opens through the same path and still gets its gallery group
        // (←/→ page through a multi-image post). A link CARD's preview is .lc-img and deliberately not in
        // this list, so a link share still opens its link rather than its thumbnail.
        const im=el.querySelector('.txt img, .note-preview img, .media-row img, .media-grid img, .mc-item img');
        if(im){ e.preventDefault(); im.click(); return; }
        // …and under data saver the image is a "tap to load" placeholder rather than an <img>.
        const ih=el.querySelector('.img-hold');
        if(ih){ e.preventDefault(); ih.click(); return; }
        // Otherwise open the post's link. href^="http" is what separates a REAL outbound link from the
        // app's own navigation: hashtags, mentions and quoted-note links are all href="#" and handled in
        // JS, so they can never be opened by mistake here. Clicking the anchor rather than window.open
        // keeps target/rel and counts as a user gesture, so the popup blocker allows it.
        const a=el.querySelector('a[href^="http"]');
        if(a){ e.preventDefault(); a.click(); }
        return;
      }
      if(e.key==='Enter'){
        e.preventDefault();
        // A post opens its thread; anything else (a conversation, a notification) is just clicked — that
        // is what openDm and the notification row handler are already wired to.
        const tid = el.dataset.tid || (el.matches('article.note') ? el.dataset.id : '');
        if(tid){ openThread(tid); return; }
        // A file card is a grid tile with no click handler of its own — the OPEN is its <a>, so Enter has
        // to press that or it looked like Enter did nothing in the file manager.
        if(el.matches('.file-card')){ const a=el.querySelector('a'); if(a){ a.click(); return; } }
        // A budget row's own click does nothing (its handler only fires for [data-act] children), so a
        // bare el.click() would make Enter look dead. Enter means "open this" everywhere else, so open
        // the row's ☰ menu — edit/skip/delete all live behind it, and `p` already covers paying.
        if(el.matches('.bg-row[data-bill], .bg-plan[data-cat]')){
          const mb=el.querySelector('.bg-more'); if(mb){ mb.click(); return; } }
        el.click();
        // Opening a conversation from the keyboard should leave you able to TYPE — otherwise focus is
        // still on the list and the first thing you write goes nowhere. Only on the KEYBOARD path: doing
        // it for a tap too would throw up the on-screen keyboard every time a phone user opens a chat.
        // The thread renders asynchronously, so poll briefly for the input rather than assuming it is there.
        if(el.matches('.dm-peer')) _focusSoon('#dm-in');
        // Same for the AI view reached the vim way — h into the nav rail, j/k, Enter. That path never goes
        // through _runShortcut, so it needs its own hand-off or the caret is left behind on the rail.
        else if(el.matches('.nav-item[data-view="ai"]')) _focusSoon('#ai-input');
        return;
      }
      for(const [sel,map] of _CARD_KEYS){
        if(!el.matches(sel)) continue;
        const csel=map[k]; if(!csel) return;  // this card type owns its letters; don't fall through
        const cb=el.querySelector(csel);
        if(!cb) return;                       // e.g. an encrypted file (no ⧉ Copy), or a Nostr-only node
        e.preventDefault(); cb.click();       //      where News has no Summarize
        return;
      }
      const a=_postKeys()[k]; if(!a) return;
      const btn=el.querySelector('.act[data-a="'+a+'"]');
      if(!btn) return;                        // e.g. a poll card, which carries only reply + menu
      e.preventDefault();
      btn.click();
    });
  })();

  // ---------- keyboard scrolling ----------
  // The timeline is a scrollable DIV (#feed), not the document — the app is a fixed-height flex layout, so
  // <body> never scrolls. A div with no tabindex cannot take keyboard focus, so ↓/↑/PageDown/PageUp/Space/
  // Home/End landed on <body>, which has nothing to scroll, and the feed sat still. Nothing was swallowing
  // the keys; they simply had nowhere to act. Route them to the feed instead.
  // Which element the scroll keys should move. Usually #feed, but Messages / Chat / AI set overflow:hidden
  // on it and scroll in their own pane — skipping those was why the keys did nothing in Messages. Ordered
  // by what the user is actually reading: an open conversation beats the conversation list beside it.
  // An ARRAY, queried one selector at a time: a combined querySelectorAll returns DOM order, not selector
  // order, so the conversation LIST (which comes first in the markup) would win over the open thread.
  const _KEY_PANES = ['.dm-msgs', '.ai-msgs', '.chatroom-msgs', '.dm-list'];
  function _keyScroller(){
    const f=document.getElementById('feed');
    if(!f) return null;
    const over=el=>{ const oy=getComputedStyle(el).overflowY; return oy==='auto'||oy==='scroll'; };
    const can=el=>el && el.scrollHeight > el.clientHeight+4;
    const shown=el=>el.offsetParent!==null || getComputedStyle(el).position==='fixed';
    if(getComputedStyle(f).overflowY!=='hidden' && can(f)) return f;
    // Named panes first, so the ambiguous cases resolve the way a reader expects — in Messages both the
    // conversation LIST and the open thread scroll, and the thread is what you are reading.
    for(const sel of _KEY_PANES){
      for(const el of document.querySelectorAll(sel)){ if(shown(el) && can(el)) return el; }
    }
    // Otherwise find it GENERICALLY. Enumerating every view that owns its own pane (News, Markets,
    // Torrents, Repos…) would just be a list to forget to update — walk out from the feed instead
    // and take the biggest visible scrollable box. Node-bounded: a scroller sits near the top of a view's
    // subtree, and a keypress must not walk a thousand list rows to find one.
    let best=null, bestH=0, budget=400;
    const q=[...f.children];
    while(q.length && budget-- > 0){
      const el=q.shift();
      if(!el || el.nodeType!==1 || !shown(el)) continue;
      if(over(el) && can(el)){ if(el.clientHeight>bestH){ bestH=el.clientHeight; best=el; } continue; }
      for(const c of el.children) q.push(c);
    }
    return best;
  }
  (function(){
    const KEYS=new Set(['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','PageDown','PageUp','Home','End',' ','Spacebar']);
    const LINE=48;   // roughly a browser's own arrow-key step
    document.addEventListener('keydown', e=>{
      if(e.ctrlKey||e.metaKey||e.altKey) return;          // Ctrl+Home etc. stay the browser's
      if(!KEYS.has(e.key)) return;
      const t=e.target, tag=(t && t.tagName)||'';
      // Never take a key off something being typed in, or off a select (arrows change its value).
      if(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(tag))) return;
      // A focused BUTTON/link must NOT block scrolling — browsers scroll the page happily with one focused,
      // and clicking a sidebar item leaves focus on exactly that button, which is precisely when you reach
      // for Page Down. (Excluding buttons outright is why the keys died after clicking "Social" but worked
      // when the same view was opened with Alt+E, where focus never moved.) Space is the one exception:
      // it ACTIVATES a focused button, so that keystroke still belongs to the control.
      // VIDEO/AUDIO are in here for the same reason as BUTTON: Space is the player's OWN play/pause when
      // one has focus, so paging the feed instead meant tabbing to a video and then being unable to start
      // it. Tab already reaches it — the row-scoped Tab walks the selected row's controls.
      if((e.key===' '||e.key==='Spacebar') && /^(BUTTON|A|SUMMARY|OPTION|VIDEO|AUDIO)$/.test(tag)) return;
      // A modal, the lightbox and the emoji/menu popovers own the keyboard while they are up — the lightbox
      // in particular pages through images with the arrows.
      if(document.body.classList.contains('modal-open')) return;
      if(document.querySelector('.lightbox,.menu-pop,.emoji-pop')) return;
      // SELECTION FIRST, before asking whether anything scrolls. A short thread fits on screen, so there is
      // no scroller — and bailing here meant the arrows did nothing there, which in turn meant nothing was
      // ever selected and r/b/q were dead too.
      if(e.key==='ArrowDown'||e.key==='ArrowUp'||e.key==='ArrowLeft'||e.key==='ArrowRight'){
        const sideways=(e.key==='ArrowLeft'||e.key==='ArrowRight');
        const stride=_rowStride(_noteEls());
        // ←/→ only mean something in a GRID (Files). In a list they would just duplicate ↑/↓ while taking
        // the keys away from anything that scrolls horizontally, so leave them to the browser there.
        if(sideways && stride<2) return;
        const step=sideways ? 1 : stride;
        if(_moveSel((e.key==='ArrowDown'||e.key==='ArrowRight') ? step : -step)){ e.preventDefault(); return; }
        if(sideways) return;
      }
      const f=_keyScroller();
      if(!f) return;                                       // nothing to scroll — don't swallow the key
      const page=Math.max(120, f.clientHeight-64);
      let dy=0;
      if(e.key==='ArrowDown') dy=LINE;
      else if(e.key==='ArrowUp') dy=-LINE;
      else if(e.key==='PageDown') dy=page;
      else if(e.key==='PageUp') dy=-page;
      else if(e.key===' '||e.key==='Spacebar') dy=e.shiftKey?-page:page;   // Space pages, Shift+Space back
      else if(e.key==='Home'||e.key==='End'){
        e.preventDefault();
        // Instant, like the browser's own Home/End on a page — and a smooth animation here can be
        // suppressed outright (prefers-reduced-motion), which would read as the key doing nothing.
        f.scrollTo({top: e.key==='Home'?0:f.scrollHeight, behavior:'auto'});
        { const cur=_selEl();
          if(S._vimPane==='feed' && cur && !cur.matches('.dm-peer')){ const els=_noteEls(); if(els.length) _selectNote(e.key==='Home'?els[0]:els[els.length-1]); } }
        return;
      }
      if(!dy) return;
      e.preventDefault();
      // (Arrow → selection was handled above; reaching here means there was no post to step to, so this is
      //  a plain pixel scroll — settings, files, the tail of a list.)
      f.scrollBy({top:dy, behavior:'auto'});   // auto, not smooth: held-down arrows must not queue up
      // A PAGE jump leaves the selection off-screen; move it to whatever is now at the top, so the action
      // keys stay pointed at what you are actually looking at. Page keys ONLY: an arrow that reached the
      // end of the list also lands here, and re-syncing there dragged the cursor back to the topmost
      // visible row — which reads as the selection being stuck partway down the thread.
      if(e.key==='PageDown'||e.key==='PageUp'||e.key===' '||e.key==='Spacebar'){
        const cur=_selEl();
        // Feed pane only: paging must not drag a cursor that is sitting in the nav or notifications
        // column onto a post, and in Messages the scroller is the thread, not the list beside it.
        if(S._vimPane==='feed' && cur && !cur.matches('.dm-peer')){ const n=_topNote(); if(n) _selectNote(n); }
      }
    });
  })();
  // Every image/video in the gallery `im` belongs to, so the lightbox can step through a multi-image post
  // with the arrow keys / on-screen arrows / swipe, like every other client. null when there's nothing to
  // page through (a lone attachment, or an image sitting inline in the text).
  function _lbGroup(im){
    const box = im && im.closest && im.closest('.media-car, .media-row, .media-grid');
    if(!box) return null;
    const els=[...box.querySelectorAll('img,video')];
    if(els.length<2) return null;
    const i=els.indexOf(im);
    // data-vsrc for an UNMOUNTED video (VideoMount only attaches a src to what's on screen) — without it a
    // lazily-mounted attachment stepped to in the lightbox would open with an empty src.
    return { items: els.map(el=>({ src: el.currentSrc||el.src||VideoMount.url(el), kind: el.tagName==='VIDEO'?'video':null })), i: i<0?0:i };
  }


  return {
    _lbGroup, _selEl, _selectNote, _vimOn,
  };
};

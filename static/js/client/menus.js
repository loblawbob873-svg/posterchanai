/* The popovers and the post ⋯ menu — the emoji picker and its autocomplete, reactions, the menu
 * popover every screen opens, translate/rebroadcast/raw-event, the post card screenshot, read
 * aloud and summarise. Split out of app.js.
 *
 * Loaded on first use: app.js keeps a small block of entry points (search for `_menusDeps`) and
 * builds this factory the first time one of these is opened. The press-and-hold read-aloud gesture
 * is still bound at boot in app.js — it must answer a long press before anything here has loaded —
 * and `_narrateAudio` (the AI module reads it as live state) and postImageUrl stayed there too.
 *
 * `stopNarration` runs on every view change, so it is one of the entry points that does nothing
 * until the module exists — leaving a screen must never fetch this file.
 *
 * The code below is BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets.
 */
window.PCMenusFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.BOOKMARKS, S.IS_ADMIN, S.ME, S.MUTED_THREADS, S.PINNED, S._narrateAudio, S._narrateWatch
  const {
    $, $$, InstEmoji, NT, REACTION_EMOJIS, _AC_MAX, _AC_RE, _blossomDenied, _emojiBtn,
    _emojiRecent, _emojiRemember, _ltNorm, _placePop, _popKeys, _rootIdOf, _scalePop, _webLink,
    _withModule, closeModal, compose, copyValue, decorateCounts, doBlock, doDelete, eTags,
    effectPost, enc, fetchEvent, invalidateCounts, isMutedAuthor, launchEffectStudio, linkify,
    mediaParts, memeBuildPost, modal, myReaction, myReactionIds, needProfile, niceNip05, npubOf,
    postImageUrl, profOf, publish, repostWithWarning, requestBlossomAccess, switchView, toast,
    toggleBookmark, toggleMute, toggleMuteThread, togglePin, uiPrompt, uploadBlob,
  } = dep;
  function attachEmojiAutocomplete(ta){
    if(!ta || ta.dataset.emAc) return; ta.dataset.emAc='1';
    let box=null, items=[], sel=0, from=-1;
    const close=()=>{ if(box){ box.remove(); box=null; } items=[]; sel=0; from=-1; };
    const place=()=>{
      if(!box) return;
      const r=ta.getBoundingClientRect(), M=8, w=Math.min(300, Math.max(200, r.width));
      box.style.width=w+'px';
      let left=Math.max(M, Math.min(r.left, window.innerWidth-M-w));
      const h=box.offsetHeight;
      // ABOVE the box by default: on a phone the on-screen keyboard owns everything below the caret.
      let top=r.top-h-6; if(top<M) top=Math.min(r.bottom+6, window.innerHeight-M-h);
      box.style.left=left+'px'; box.style.top=Math.max(M, top)+'px';
    };
    const draw=()=>{
      if(!items.length){ close(); return; }
      if(!box){ box=document.createElement('div'); box.className='em-ac'; document.documentElement.appendChild(box); }
      box.innerHTML=items.map((e,i)=>
        `<button type="button" class="em-ac-row${i===sel?' on':''}" data-i="${i}">`
        +`<img src="${enc(e.t||e.u)}" alt="" loading="lazy"><span>:${enc(e.s)}:</span></button>`).join('');
      $$('.em-ac-row',box).forEach(b=> b.onmousedown=ev=>{ ev.preventDefault(); take(+b.dataset.i); });
      place();
    };
    const take=i=>{
      const e=items[i]; if(!e) return;
      const c=ta.selectionStart||0, tail=ta.value.slice(c);
      ta.value=ta.value.slice(0, from)+':'+e.s+': '+tail;
      const at=from+e.s.length+3;
      ta.selectionStart=ta.selectionEnd=at; ta.focus();
      _emojiRemember(':'+e.s+':');
      close();
      ta.dispatchEvent(new Event('input',{bubbles:true}));   // composers autosave/grow on input
    };
    const scan=()=>{
      const c=ta.selectionStart||0, m=_AC_RE.exec(ta.value.slice(0,c));
      if(!m){ close(); return; }
      from=c-m[2].length;
      const q=m[3].toLowerCase();
      InstEmoji.load().then(list=>{
        if(!list.length || from<0) { close(); return; }
        if((ta.selectionStart||0)!==c) return;              // caret moved on while we loaded
        const starts=[], has=[];
        for(const e of list){
          const s=e.s.toLowerCase();
          if(s.startsWith(q)) starts.push(e); else if(s.includes(q)) has.push(e);
          if(starts.length>=_AC_MAX) break;
        }
        items=starts.concat(has).slice(0,_AC_MAX); sel=0; draw();
      });
    };
    ta.addEventListener('input', scan);
    ta.addEventListener('click', scan);
    ta.addEventListener('blur', ()=>setTimeout(close, 160));   // let a click on a row land first
    // CAPTURE on the document: the composers bind Enter (post/send) on the textarea itself, and the
    // suggestion list has to take that key first while it's open.
    document.addEventListener('keydown', e=>{
      if(!box || e.target!==ta) return;
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){
        e.preventDefault(); e.stopPropagation();
        sel=(sel+(e.key==='ArrowDown'?1:items.length-1))%items.length; draw();
      } else if(e.key==='Enter'||e.key==='Tab'){
        e.preventDefault(); e.stopPropagation(); take(sel);
      } else if(e.key==='Escape'){ e.preventDefault(); e.stopPropagation(); close(); }
    }, true);
    window.addEventListener('resize', ()=>{ if(box) place(); });
  }
  function openEmojiPopover(anchorBtn, onPick, opts){
    if(openEmojiPopover.closeActive)openEmojiPopover.closeActive();
    opts=opts||{};
    document.querySelectorAll('.emoji-pop,.pop-backdrop').forEach(p=>p.remove());   // never stack pickers
    const pop=document.createElement('div'); pop.className='emoji-pop';
    pop.innerHTML=`<div class="ep-head" hidden><input class="ep-q" type="search" placeholder="search emoji…" autocomplete="off" spellcheck="false"><div class="ep-tabs"></div></div><div class="ep-grid"></div>`;
    const grid=$('.ep-grid',pop), head=$('.ep-head',pop), q=$('.ep-q',pop), tabs=$('.ep-tabs',pop);
    // Render in CHUNKS and top up on scroll. An instance can have thousands of emoji; building that
    // many buttons up front freezes a phone for seconds and downloads every thumbnail at once.
    const CHUNK=120; let _items=[], _n=0;
    const _more=()=>{
      if(_n>=_items.length) return;
      const slice=_items.slice(_n, _n+CHUNK); _n+=slice.length;
      grid.insertAdjacentHTML('beforeend', slice.map(_emojiBtn).join(''));
      _wire();
      // Top up until the box is full, but never more than a few chunks: if the grid measures 0 (not
      // laid out yet) this test is always true, and unbounded it would build every emoji at once.
      if(_n < CHUNK*3 && grid.scrollHeight<=grid.clientHeight+8) _more();
    };
    const _show=items=>{ _items=items; _n=0; grid.innerHTML=''; _more();
      if(!items.length) grid.innerHTML='<div class="ep-empty">no match</div>';
      // Tabs and search can grow Recent; re-anchor after every replacement render.
      _placePop(pop, anchorBtn, opts); };
    grid.addEventListener('scroll',()=>{ if(grid.scrollTop+grid.clientHeight > grid.scrollHeight-160) _more(); });
    // TABS, like Pleroma's picker: recents, the built-in unicode set, then one per instance pack —
    // 3336 emoji in one endless scroll is unusable, and packs are how an operator organises them.
    const _recents=()=>{
      const by={}; InstEmoji.list.forEach(e=>{ by[e.s]=e; });
      return _emojiRecent().map(x=> x.startsWith(':') ? (by[x.slice(1,-1)]||null) : x).filter(Boolean);
    };
    const _tabItems=t=> t==='recent' ? _recents()
                      : t==='std'    ? REACTION_EMOJIS
                                     : InstEmoji.list.filter(e=>e.p===t);
    let _tab='std';
    const _setTab=(t, keepQuery)=>{
      _tab=t;
      $$('.ep-tab',pop).forEach(b=>b.classList.toggle('on', b.dataset.tab===t));
      if(!keepQuery && q) q.value='';
      grid.scrollTop=0; _show(_tabItems(t));
    };
    const _buildTabs=()=>{
      const packs=[...new Set(InstEmoji.list.map(e=>e.p))];
      const first={}; InstEmoji.list.forEach(e=>{ if(!first[e.p]) first[e.p]=e; });
      const chip=(id,label,icon,img)=>`<button type="button" class="ep-tab" data-tab="${enc(id)}" title="${enc(label)}">`
        + (img?`<img src="${enc(img)}" alt="" loading="lazy">`:enc(icon))+`<span>${enc(label)}</span></button>`;
      const out=[];
      if(_recents().length) out.push(chip('recent','Recent','🕘'));
      out.push(chip('std','Emoji','😀'));
      packs.forEach(p=>out.push(chip(p, p==='_'?'Custom':p, '', (first[p]||{}).t)));
      tabs.innerHTML=out.join('');
      $$('.ep-tab',pop).forEach(b=>{
        // Finish the pointer gesture before resizing/reanchoring the grid. Moving on mousedown
        // can retarget mouseup/click to the page, where the outside-click handler closes us.
        b.onmousedown=ev=>ev.preventDefault();
        b.onclick=()=>_setTab(b.dataset.tab);
      });
    };
    document.documentElement.appendChild(pop);   // <html>, not <body>: body has zoom:.85 on desktop,
    _placePop(pop, anchorBtn, opts);                    // which throws off fixed-position math for a body child
    let _detachKeys=()=>{};
    // Give the popover itself the focus. Without it a picker was only keyboard-navigable when whatever had
    // focus happened to be harmless: _popKeys ignores keys aimed at a text field (so you can keep typing
    // while one is open), and the boxes these hang off — AI Chat's compose, the post composer — hold the
    // caret practically all the time. So the menu opened and then swallowed every arrow key.
    // Focusing the POP rather than its first item keeps the .kb cursor as the single indicator instead of
    // racing a browser focus ring.
    const _prevFocus = document.activeElement;
    pop.tabIndex = -1;
    try{ pop.focus({preventScroll:true}); }catch(_){ }
    let closed=false,armTimer;
    const close=()=>{
      if(closed)return;closed=true;clearTimeout(armTimer);
      if(openEmojiPopover.closeActive===close)openEmojiPopover.closeActive=null;
      // Hand focus back where it came from, so opening a menu mid-sentence does not cost you the caret.
      // Only when the popover still owns it — an item's action may have moved focus deliberately.
      const mine = pop.contains(document.activeElement) || document.activeElement===document.body;
      _detachKeys(); pop.remove(); document.querySelectorAll('.pop-backdrop').forEach(b=>b.remove()); document.removeEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.removeEventListener('scroll',close); document.removeEventListener('scroll',onScroll,true); window.removeEventListener('resize',close);
      if(mine && _prevFocus && _prevFocus.isConnected){ try{ _prevFocus.focus({preventScroll:true}); }catch(_){ } }
    };
    openEmojiPopover.closeActive=close;
    const onScroll=e=>{ if(!pop.contains(e.target)) close(); };
    if(opts.anchored){ document.addEventListener('scroll',onScroll,true); window.addEventListener('resize',close); }
    const onDoc=e=>{ if(!pop.contains(e.target) && !(anchorBtn && anchorBtn.contains(e.target))) close(); };
    armTimer=setTimeout(()=>{ if(closed)return;document.addEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.addEventListener('scroll',close,{once:true}); },0);
    // mousedown + preventDefault keeps the textarea focused so insert-at-cursor works. Buttons arrive
    // in chunks, so wiring is per-button and idempotent rather than one pass over the grid.
    function _wire(){
      $$('[data-e]:not([data-w])',pop).forEach(b=>{ b.dataset.w='1';
        b.onmousedown=ev=>{ ev.preventDefault(); _emojiRemember(b.dataset.e); onPick(b.dataset.e, close); }; });
    }
    _wire();
    // Arrows/hjkl drive the grid; everything else goes to the search box so you can type into it.
    _detachKeys=_popKeys(pop, '[data-e]', b=>{ _emojiRemember(b.dataset.e); onPick(b.dataset.e, close); }, close,
                         {inText:['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Escape','Home','End']});
    // Paint the built-in set instantly, then add the tab bar once the instance packs land.
    _show(REACTION_EMOJIS);
    if(!opts.unicodeOnly) InstEmoji.load().then(list=>{
      if(!pop.isConnected) return;
      if(!list.length) return;             // no custom emoji here → no search box, no tabs, as before
      head.hidden=false;
      _buildTabs();
      _setTab(_recents().length ? 'recent' : 'std');
    });
    // Search looks across EVERY pack (that's the point of a search box with thousands of emoji);
    // clearing it drops back to the tab you were on.
    if(q) q.oninput=()=>{
      const s=q.value.trim().toLowerCase().replace(/:/g,'');
      if(!s){ _setTab(_tab, true); return; }
      $$('.ep-tab',pop).forEach(b=>b.classList.remove('on'));
      grid.scrollTop=0;
      _show(InstEmoji.list.filter(e=>e.s.toLowerCase().includes(s)));
    };
    return close;
  }
  // Tapping the react button when you have ALREADY reacted takes the reaction back (NIP-09 delete of
  // your kind-7) instead of the old dead-end "already reacted" toast — which left no way to undo a
  // mis-tap at all, on Nostr or on the fediverse the reaction was written back to.
  async function unReact(id){
    const ids=myReactionIds(id); if(!ids.length){ toast('already reacted'); return; }
    const tags=ids.map(r=>['e', r]); tags.push(['k','7']);   // NIP-09: k = kind being deleted
    const r=await publish(5, '', tags, {quiet:true});        // '' content: nothing to say about a reaction
    // Only forget it locally once the relay took the tombstone — otherwise the button would go back to
    // "not reacted" while the reaction is still live (and still on the fediverse).
    if(!(r && r.ok)){ toast('couldn’t reach the relay — the reaction was NOT removed'); return; }
    ids.forEach(x=>{ try{ Store.removeEvent(x); }catch(_){} });
    invalidateCounts(); decorateCounts(); toast('reaction removed');
  }
  function pickEmoji(id,pk,btn){
    if(myReaction(id)){ unReact(id); return; }
    openEmojiPopover(btn, (emoji, close)=>{ close(); publish(7,emoji,eTags(id,pk)).then(r=>{ if(r&&r.ok){ toast('reacted '+emoji); decorateCounts(); } }); });   // failure toast by publish() (which also rolls the reaction back)
  }
  // Generic "☰ more" popover anchored under a button. items = [action, label, optional css class];
  // onPick(action) fires after the menu closes. Shared by the post menu and the profile menu.
  function openMenuPopover(anchorBtn, items, onPick){
    if(typeof openEmojiPopover==='function' && openEmojiPopover.closeActive)openEmojiPopover.closeActive();
    document.querySelectorAll('.menu-pop,.emoji-pop,.pop-backdrop').forEach(p=>p.remove());   // never stack popovers
    const pop=document.createElement('div'); pop.className='menu-pop';
    /* AN ITEM MAY BE A <label>, AND THAT IS WHAT KEEPS A FILE PICKER WORKING FROM A MENU.
     *
     * Opening the native chooser needs a trusted click on the <input type=file>. Doing it as
     * `input.click()` from an item's handler does not survive: `close()` below removes the menu
     * first, and Firefox then treats the programmatic click as untrusted — the chooser never
     * opens, silently. That is why the post composer's paperclip stopped offering a menu at all on
     * the web, which took "attach from Files" with it.
     *
     * A label bound to the input needs no script: the browser opens the chooser as the label's own
     * default action. So an item may carry `{htmlFor:'<input id>'}` and is rendered as one. */
    pop.innerHTML=items.map(([a,label,cls,extra])=> extra && extra.htmlFor
      ? `<label for="${enc(extra.htmlFor)}" data-m="${a}"${cls?` class="${cls}"`:''}>${enc(label)}</label>`
      : `<button data-m="${a}"${cls?` class="${cls}"`:''}>${enc(label)}</button>`).join('');
    _scalePop(pop);                              // desktop scale, BEFORE insertion (see _scalePop)
    document.documentElement.appendChild(pop);   // <html>, not <body>: body has zoom:.85 on desktop,
    _placePop(pop, anchorBtn);                    // which throws off fixed-position math for a body child
    let _detachKeys=()=>{};
    // Give the popover itself the focus. Without it a picker was only keyboard-navigable when whatever had
    // focus happened to be harmless: _popKeys ignores keys aimed at a text field (so you can keep typing
    // while one is open), and the boxes these hang off — AI Chat's compose, the post composer — hold the
    // caret practically all the time. So the menu opened and then swallowed every arrow key.
    // Focusing the POP rather than its first item keeps the .kb cursor as the single indicator instead of
    // racing a browser focus ring.
    const _prevFocus = document.activeElement;
    pop.tabIndex = -1;
    try{ pop.focus({preventScroll:true}); }catch(_){ }
    const close=()=>{
      // Hand focus back where it came from, so opening a menu mid-sentence does not cost you the caret.
      // Only when the popover still owns it — an item's action may have moved focus deliberately.
      const mine = pop.contains(document.activeElement) || document.activeElement===document.body;
      _detachKeys(); pop.remove(); document.querySelectorAll('.pop-backdrop').forEach(b=>b.remove()); document.removeEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.removeEventListener('scroll',close);
      if(mine && _prevFocus && _prevFocus.isConnected){ try{ _prevFocus.focus({preventScroll:true}); }catch(_){ } }
    };
    const onDoc=e=>{ if(!pop.contains(e.target) && !anchorBtn.contains(e.target)) close(); };
    setTimeout(()=>{ document.addEventListener('click',onDoc,true); const f=$('#feed'); if(f) f.addEventListener('scroll',close,{once:true}); },0);
    $$('[data-m]',pop).forEach(b=> b.onclick=()=>{
      /* A label's job IS the click — closing first would detach it before the browser ran its
       * default action, which is the whole failure this exists to avoid. Let it happen, then tidy
       * up; there is nothing for onPick to do that the input's own change handler does not. */
      if(b.tagName === 'LABEL'){ setTimeout(close, 0); return; }
      close(); onPick(b.dataset.m); });
    _detachKeys=_popKeys(pop, '[data-m]', b=>{ close(); onPick(b.dataset.m); }, close);
    return close;
  }
  // Translate the DRAFT in a compose box into a chosen language (reply / quote / new post all use
  // compose(), so this covers all three). Pops a language picker, then replaces the draft text.
  async function composeTranslate(ta, btn){
    const text=(ta.value||'').trim();
    if(!text){ toast('write something first'); return; }
    // Thai kept near the top so it's visible without scrolling the picker (the popover caps at ~12
    // rows before it scrolls — Thai lower in the list read as "missing").
    const langs=['English','Thai','Chinese','Spanish','French','German','Italian','Portuguese','Tagalog','Cebuano','Swahili','Japanese','Korean','Hindi','Arabic','Russian','Indonesian'];
    const items=langs.map(n=>[n,'🌐 '+n]).concat([['__other','✏️ Other…']]);
    openMenuPopover(btn, items, async name=>{
      let to=name;
      if(name==='__other'){ to=(await uiPrompt('Translate to which language?')||'').trim(); if(!to) return; }
      const old=ta.value; ta.value='translating…'; ta.disabled=true;
      try{
        const r=await fetch('/client/translate',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ text, to }) });
        const j=await r.json().catch(()=>({}));
        ta.disabled=false;
        if(r.ok && j.text){
          if(_ltNorm(j.text)===_ltNorm(text)){ ta.value=old; toast('no change — already '+to+'? (or just sounds/emoji)'); }
          else { ta.value=j.text; ta.focus(); ta.dispatchEvent(new Event('input')); toast('translated → '+to); }
        } else { ta.value=old; toast(j.error||'translation unavailable'); }
      }catch(e){ ta.disabled=false; ta.value=old; toast('translate failed'); }
    });
  }
  // the per-post "☰ more" menu — holds the secondary actions (bookmark / copy id / pin / delete /
  // block) so the action row stays a clean 5 across.
  // 📡 Rebroadcast: re-publish the full signed event to the built-in relay, which fans it back out to
  // the upstream public relays (the relay's paced outbox) — useful to re-propagate a post that didn't
  // reach the wider network. Works for any post; the event must be the complete signed event.
  async function rebroadcastPost(id){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev || !ev.sig){ toast('post not loaded'); return; }
    toast('📡 rebroadcasting…');
    try{ const r=await Relay.publish(ev); toast(r&&r.ok ? '📡 rebroadcast to relays' : ('relay: '+((r&&r.msg)||'rejected'))); }
    catch(_){ toast('rebroadcast failed'); }
  }
  function openPostMenu(id, pk, art, anchorBtn){
    const mine = pk===S.ME.pubkey;
    const items=[['bookmark', S.BOOKMARKS.has(id)?'🔖 Remove bookmark':'🔖 Bookmark'], ['copyid','🔗 Copy link']];
    if(mine) items.push(['delete','🗑️ Delete','danger']);   // near the top so it's reachable on a crowded menu
    if(!window.PC_NOSTR_ONLY) items.push(['translate','🌐 Translate']);   // uses the node's AI backend
    if(!window.PC_NOSTR_ONLY) items.push(['summary','📝 Summary']);       // AI summary of the post/thread
    if(!window.PC_NOSTR_ONLY) items.push(['narrate','🔊 Read Aloud']);    // TTS the post (author + content)
    if(!window.PC_NOSTR_ONLY) items.push(['effect','🎬 Effect']);         // apply an effect to the post's image
    items.push(['memebuild','🎞️ Meme Builder']);   // drop the post's media in as a layer to edit/compose
    if(!window.PC_NOSTR_ONLY) items.push(['screenshot','📸 Screenshot']); // render the post as a clean card → Blossom link
    items.push(['rawjson','🧾 Raw event (JSON)']);   // the signed event exactly as it is on the relay
    if(mine) items.push(['pin', S.PINNED.has(id)?'📌 Unpin from profile':'📌 Pin to profile']);
    if(mine){ const ev=Store.get(id); const tagged=!!(ev && ev.tags.some(t=>t[0]==='content-warning'));
      // Only offer it when the post isn't already warned (a re-posted copy already carries the tag).
      if(!tagged) items.push(['nsfw','🔞 Re-post with NSFW warning']); }
    if(mine) items.push(['rebroadcast','📡 Rebroadcast to relays']);   // re-propagate your own post (moved down)
    // Mute the whole CONVERSATION (NIP-51 `e` tag) — offered on your own posts too, since a thread of
    // yours going noisy is exactly when you want it.
    { const _r=_rootIdOf(Store.get(id))||id;
      items.push(['mutethread', S.MUTED_THREADS.has(_r)?'🔔 Unmute conversation':'🔕 Mute conversation']); }
    if(!mine) items.push(['mute', isMutedAuthor(pk)?'🔊 Unmute author':'🔇 Mute author']);   // personal NIP-51 mute (any user)
    if(S.IS_ADMIN && !mine) items.push(['block','🚫 Block author','danger']);
    openMenuPopover(anchorBtn, items, a=>{
      if(a==='bookmark'){ toggleBookmark(id, null).then(()=>{ if(anchorBtn) anchorBtn.classList.toggle('on', S.BOOKMARKS.has(id)); }); return; }
      if(a==='copyid'){ let _lk=id, _m='id copied';
        try{ _lk=_webLink(NT().nip19.neventEncode({id})); _m='link copied'; }
        catch(_){ try{ _lk=_webLink(NT().nip19.noteEncode(id)); _m='link copied'; }catch(__){} }
        copyValue(_lk, _m, 'Link to this post:'); return; }
      if(a==='rebroadcast') return rebroadcastPost(id);
      if(a==='translate') return translatePost(id);
      if(a==='summary') return summarizePost(id);
      if(a==='narrate') return narratePost(id, pk);
      if(a==='effect') return effectPost(id, pk);
      if(a==='memebuild') return memeBuildPost(id, pk);
      if(a==='screenshot') return screenshotPost(id);
      if(a==='rawjson') return showRawEvent(id);
      if(a==='pin') return togglePin(id);
      if(a==='nsfw') return repostWithWarning(id);
      if(a==='delete') return doDelete(id, art);
      if(a==='mutethread') return toggleMuteThread(_rootIdOf(Store.get(id))||id);
      if(a==='mute') return toggleMute(pk);
      if(a==='block') return doBlock(pk);
    });
  }
  // 🧾 Raw event — the signed object exactly as it sits on the relay. Nostr is a protocol of plain JSON and
  // everything interesting about a post that the card cannot show is in it: which event it replies to, who
  // it p-tags, the content warning, the client that made it, the proof-of-work nonce, the signature. There
  // was no way to see any of that here, so "what does this post actually contain" meant opening another
  // client. Rendered rather than dumped — the header fields are decoded into human terms above a
  // pretty-printed, token-coloured body.
  const _KIND_NAMES = {0:'profile metadata', 1:'short text note', 3:'follow list', 4:'encrypted DM (legacy)',
    5:'deletion request', 6:'repost', 7:'reaction', 9:'group chat message', 16:'generic repost',
    40:'channel create', 42:'channel message', 1018:'poll response', 1063:'file metadata', 1068:'poll',
    1111:'comment', 1311:'live chat message', 1984:'report', 9735:'zap receipt', 10000:'mute list',
    10002:'relay list', 10063:'blossom server list', 13194:'wallet info', 14:'direct message',
    24133:'nostr connect', 25050:'call signalling', 30000:'follow set', 30008:'profile badges',
    30023:'long-form article', 30078:'application data', 30311:'live event', 30402:'classified listing',
    30617:'git repo announcement', 30618:'git repo state', 30818:'wiki article', 31922:'calendar event',
    34550:'community definition'};

  async function showRawEvent(id){
    let ev = Store.get(id);
    if(!ev){ ev = await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    const json = _prettyEvent(ev);
    let when=''; try{ when = new Date(ev.created_at*1000).toLocaleString(); }catch(_){ }
    let npub=ev.pubkey; try{ npub = NT().nip19.npubEncode(ev.pubkey); }catch(_){ }
    let nevent=ev.id; try{ nevent = NT().nip19.neventEncode({ id:ev.id, author:ev.pubkey }); }catch(_){ }
    const kindName = _KIND_NAMES[ev.kind] || '';
    modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-article"></use></svg>Raw event</h3>
      <div class="rawev-meta">
        <span>kind</span><b>${ev.kind}${kindName?` <i>${enc(kindName)}</i>`:''}</b>
        <span>created</span><b>${enc(when)} <i>${ev.created_at}</i></b>
        <span>tags</span><b>${(ev.tags||[]).length}</b>
        <span>author</span><b class="rawev-mono">${enc(npub)}</b>
        <span>event</span><b class="rawev-mono">${enc(nevent)}</b>
      </div>
      <pre class="rawev-json">${_jsonHtml(json)}</pre>
      <div class="row rawev-acts">
        <button class="btn btn-cyan small" id="rawev-copyid"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy nevent</button>
        <button class="btn btn-neon small" id="rawev-copy"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy JSON</button>
        <button class="btn btn-cyan small" id="rawev-close">Close</button>
      </div>`, root=>{
      const cp=root.querySelector('#rawev-copy');
      if(cp) cp.onclick=()=> copyValue(json, '📋 JSON copied', 'Raw event JSON:');
      const ci=root.querySelector('#rawev-copyid');
      if(ci) ci.onclick=()=> copyValue(nevent, '🔗 nevent copied', 'nevent:');
      const cl=root.querySelector('#rawev-close'); if(cl) cl.onclick=closeModal;
    });
  }

  // Pretty-print the event with each TAG on ONE line. Plain JSON.stringify(…,2) puts every element of every
  // tag on a line of its own, so a normal reply — four or five tags — becomes twenty lines of one word each
  // and the shape of the thing you came to read is gone. Tags are the interesting part of a nostr event and
  // they are short arrays of strings; inline is how every other tool shows them.
  //
  // Safe as a text substitution: JSON.stringify escapes a newline inside a string as `\n` (two characters),
  // so a REAL newline followed by exactly two spaces and `]` can only be the pretty-printer closing a
  // depth-1 array — and `tags` is the only one a nostr event has. A non-match leaves the plain output.
  function _prettyEvent(ev){
    const json = JSON.stringify(ev, null, 2);
    const tags = ev && ev.tags;
    if(!Array.isArray(tags) || !tags.length) return json;
    const inline = '[\n' + tags.map(t=>'    '+JSON.stringify(t)).join(',\n') + '\n  ]';
    return json.replace(/"tags": \[[\s\S]*?\n {2}\]/, () => '"tags": ' + inline);
  }

  // Colourise pretty-printed JSON. The tokeniser runs on the RAW string and every emitted fragment — matched
  // or not — goes through enc() on its way out, so an event whose content is `<script>` (or a tag value that
  // looks like markup) is escaped exactly as it would be anywhere else. Escaping FIRST would not work: enc
  // turns `"` into `&quot;`, and the string-literal rule would then match nothing.
  function _jsonHtml(s){
    const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;
    let out='', last=0, m;
    while((m = re.exec(s))){
      out += enc(s.slice(last, m.index));
      last = re.lastIndex;
      if(m[1]) out += m[2] ? `<span class="jk">${enc(m[1])}</span>${enc(m[2])}` : `<span class="js">${enc(m[1])}</span>`;
      else if(m[3]) out += `<span class="jl">${enc(m[3])}</span>`;
      else out += `<span class="jn">${enc(m[4])}</span>`;
    }
    return out + enc(s.slice(last));
  }

  // 📸 Screenshot: render the post as a clean tweet-style CARD (just the post)
  // server-side from the note's own fields — reliable + instance-branded, no live-SPA capture/timing.
  // The card PNG is uploaded to Blossom and its link copied (image-on-clipboard is unreliable after a
  // multi-second async op; a text link copies fine).
  // Clean plain text for the screenshot card: a flat card can't render Nostr embeds, so resolve
  // nostr: mentions to @names and replace quote/embed refs (nevent/note) with the quoted post's
  // text when we have it cached (else strip the raw bech32 — the gibberish token looked broken).
  function _cardText(ev){
    let t=(mediaParts(ev.content).text||ev.content||'');
    t=t.replace(/nostr:(npub1[023456789acdefghjklmnpqrstuvwxyz]{58}|nprofile1[0-9a-z]+)/gi,(m,b)=>{
      try{ const d=NT().nip19.decode(b); const pk=d.type==='npub'?d.data:(d.data&&d.data.pubkey);
        if(pk){ const pr=profOf(pk); const nm=pr&&(pr.name||pr.display_name); return '@'+(nm||(NT().nip19.npubEncode(pk).slice(4,12)+'…')); } }catch(_){}
      return '';
    });
    t=t.replace(/\s*nostr:(nevent1[0-9a-z]+|note1[0-9a-z]+)/gi,(m,b)=>{
      try{ const d=NT().nip19.decode(b); const eid=d.type==='note'?d.data:(d.data&&d.data.id); const o=eid&&Store.get(eid);
        if(o){ const op=profOf(o.pubkey); const onm=(op&&(op.name||op.display_name))||'anon';
          const ot=(mediaParts(o.content).text||o.content||'').replace(/nostr:[0-9a-z]+/gi,'').replace(/\s+/g,' ').trim().slice(0,160);
          return `\n\n↩ ${onm}: “${ot}”`; } }catch(_){}
      return '';
    });
    t=t.replace(/\s*nostr:naddr1[0-9a-z]+/gi,'');
    return t.replace(/\n{3,}/g,'\n\n').trim();
  }
  async function screenshotPost(id){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    const p=profOf(ev.pubkey);
    const name=p.name||p.display_name||'';
    const handle=niceNip05(p.nip05)||('@'+npubOf(ev.pubkey).slice(4,12)+'…');
    const text=_cardText(ev);
    let timestamp=''; try{ timestamp=new Date(ev.created_at*1000).toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}); }catch(_){}
    toast('📸 rendering…');
    try{
      // Pass the avatar + first-image URLs; the SERVER fetches them (the client can't read most of
      // them as bytes — cross-origin CORS — which is why the card was missing the avatar).
      const r=await fetch('/client/screenshot',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        name, handle, text, timestamp, avatar_url: p.picture||'', image_url: postImageUrl(ev)||'' }) });
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.image){ toast('screenshot failed: '+(j.error||('http '+r.status))); return; }
      const bin=Uint8Array.from(atob(j.image), c=>c.charCodeAt(0));
      const file=new File([bin], 'post.png', { type:'image/png' });
      toast('📤 uploading…');
      const link=await uploadBlob(file);
      // Best-effort and SILENT: the modal below shows the link and has its own Copy button, so a
      // shell with no clipboard must not toast a failure here on top of a successful upload.
      try{ if(window.pcClip && window.pcClip.write) await window.pcClip.write(link);
           else if(navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(link); }catch(_){}
      modal(`<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-camera"></use></svg>Post card</h3><img src="${enc(link)}" style="max-width:100%;max-height:54vh;border-radius:10px;display:block;margin:0 auto">`+
        `<div class="muted small" style="margin-top:10px;word-break:break-all">${enc(link)}</div>`+
        `<div class="row ss-acts"><button class="btn btn-cyan small" id="ss-fx"><svg class="ic b-ic" aria-hidden="true"><use href="#i-film"></use></svg>Effect</button><button class="btn btn-cyan small" id="ss-meme"><svg class="ic b-ic" aria-hidden="true"><use href="#i-film"></use></svg>Meme Builder</button><button class="btn btn-cyan small" id="ss-note"><svg class="ic b-ic" aria-hidden="true"><use href="#i-note"></use></svg>Save to Notes</button><button class="btn btn-neon small" id="ss-copy"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Copy link</button><a class="btn btn-cyan small" href="${enc(link)}" target="_blank" rel="noopener"><svg class="ic b-ic" aria-hidden="true"><use href="#i-link"></use></svg>Open</a><button class="btn btn-cyan small" id="ss-close">Close</button></div>`,
        root=>{
          const cp=root.querySelector('#ss-copy'); if(cp) cp.onclick=()=> copyValue(link, '📋 link copied', 'Link:');
          // 🎬 Effect: run the card PNG through the Effects studio; no reply target → its 🚀 Post button
          // publishes a fresh timeline post, exactly like an effect made from a regular image.
          const fx=root.querySelector('#ss-fx'); if(fx) fx.onclick=()=>{ closeModal(); launchEffectStudio(link, null); };
          // 🎞️ Meme Builder: the card is already a Blossom URL, which is exactly what a layer source is —
          // so it can go straight in as one (caption it, put it on a background, add a clip after it).
          // Same seed-after-the-view-renders ordering as memeBuildPost, and the same reply target, so the
          // finished meme can still answer the post the card was made from.
          const mb=root.querySelector('#ss-meme');
          if(mb) mb.onclick=()=>{
            closeModal(); switchView('meme');
            _withModule('meme.js','PCMeme').then(m=>{
              const ok=m&&m.addMedia&&m.addMedia(link,'image/png',{id,pk:ev.pubkey});
              toast(ok ? '🎞️ card added to the Meme Builder' : 'could not add that card');
            }).catch(()=>toast('could not open the Meme Builder'));
          };
          /* 📓 Save to Notes: the card, the post's own words and a link back, as ONE private note.
           *
           * The PNG goes in as an ENCRYPTED attachment rather than as the Blossom URL that is right
           * there: a note that linked to the blob would go blank the day that blob ages out or the
           * phone loses signal, and it would leave the picture readable by a server that is not
           * supposed to be able to read this library at all. The public link is kept in the text,
           * where it is a reference and not the content. */
          const nb=root.querySelector('#ss-note');
          if(nb) nb.onclick=async()=>{
            if(!window.PCNotes || !window.PCNotes.save){ toast('Notes is not loaded'); return; }
            nb.disabled=true; const was=nb.innerHTML; nb.textContent='Saving…';
            try{
              let ent=''; try{ ent=NT().nip19.neventEncode({ id, author: ev.pubkey }); }catch(_){ }
              const lines=[];
              if(text) lines.push(text.trim());
              lines.push('');
              lines.push(`— ${name || handle}${timestamp ? ' · ' + timestamp : ''}`);
              if(ent) lines.push(`[the original post](${_webLink(ent)})`);
              lines.push(`[card image](${link})`);
              const r=await window.PCNotes.save({
                title: (name ? name + ' — ' : '') + (text ? text.trim().split('\n')[0].slice(0,70) : 'post'),
                body: lines.join('\n') + '\n',
                tags: ['saved-post'],
                files: [file],
              });
              closeModal();
              toast(r.queued ? '📓 saved to Notes — will sync when you are back online'
                             : '📓 saved to Notes');
            }catch(e){
              nb.disabled=false; nb.innerHTML=was;
              toast('could not save to Notes: '+((e&&e.message)||'error'));
            }
          };
          const cl=root.querySelector('#ss-close'); if(cl) cl.onclick=closeModal;
        });
      toast('📸 card ready — link copied');
    }catch(e){
      if(typeof _blossomDenied==='function' && _blossomDenied(e)){ requestBlossomAccess(); toast('🔒 No upload access — requested it from the admin.'); }
      else toast('screenshot failed: '+((e&&e.message)||e));
    }
  }
  // Translate a post in-place via the node's AI backend. Only edits the DOM (the stored event is
  // untouched), so switching views / refreshing restores the original — exactly as asked.
  async function translatePost(id){
    const ev=Store.get(id); if(!ev){ toast('post not loaded'); return; }
    const src=(mediaParts(ev.content).text || ev.content || '').trim();
    if(!src){ toast('nothing to translate'); return; }
    const nodes=$$('.note[data-id="'+id+'"] > .body > .txt');
    if(!nodes.length){ toast('open the timeline to translate this'); return; }
    nodes.forEach(n=>{ if(!n.dataset.orig) n.dataset.orig=n.innerHTML; n.style.opacity='.5'; });
    try{
      const r=await fetch('/client/translate',{ method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ text:src, to:(navigator.language||'en') }) });
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.text){ toast(j.error||'translation unavailable'); nodes.forEach(n=>n.style.opacity=''); return; }
      // normalized compare: /client/translate collapses whitespace on its unchanged (already-target) return
      if(_ltNorm(j.text)===_ltNorm(src)){ nodes.forEach(n=>n.style.opacity=''); toast('nothing to translate — looks already in your language (or just sounds/emoji)'); return; }
      nodes.forEach(n=>{ n.style.opacity='';
        n.innerHTML=linkify(j.text)+'<div class="muted small tr-tag">🌐 translated · refresh to restore</div>'; });
    }catch(_){ toast('translate failed'); nodes.forEach(n=>n.style.opacity=''); }
  }
  /* STOPPING IT IS PART OF THE FEATURE, and there was no way to. Once a post started reading, the
     only control was long-pressing a DIFFERENT post (which starts another one) — reported as "I
     should be able to stop a post from being read aloud while it's playing (maybe by scrolling
     away?)". So: a chip that says what is playing and stops it, scrolling the post out of view
     stops it (the reader's own suggestion, and the honest signal that you have moved on), and
     leaving the screen stops it. Idempotent — every path calls this one function. */
  function stopNarration(why){
    try{ if(S._narrateAudio){ S._narrateAudio.pause(); S._narrateAudio.src=''; } }catch(_){}
    S._narrateAudio=null;
    try{ if(S._narrateWatch){ S._narrateWatch.disconnect(); } }catch(_){}
    S._narrateWatch=null;
    const chip=$('#narrate-chip'); if(chip) chip.remove();
    if(why) toast(why);
  }
  function _narrateChip(name, note){
    const old=$('#narrate-chip'); if(old) old.remove();
    const b=document.createElement('button');
    b.id='narrate-chip'; b.className='narrate-chip';
    b.innerHTML='<svg class="ic b-ic" aria-hidden="true"><use href="#i-stop"></use></svg>Stop reading <span class="muted">\u00b7 '+enc(name)+'</span>';
    b.onclick=()=>stopNarration();
    document.body.appendChild(b);
    /* SCROLLED AWAY IS STOPPED. An observer on the post itself rather than a scroll handler: the
       timeline is virtualised and re-drawn constantly, and a threshold on the element is the only
       thing that stays true through that. If the card is gone from the DOM entirely there is
       nothing to observe and the chip is the way out. */
    try{
      if(note && 'IntersectionObserver' in window){
        S._narrateWatch=new IntersectionObserver((es)=>{
          for(const e of es) if(!e.isIntersecting) stopNarration('stopped reading — you scrolled away');
        }, { threshold: 0 });
        S._narrateWatch.observe(note);
      }
    }catch(_){}
  }
  async function narratePost(id, pk){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    pk = pk || ev.pubkey;
    let body=(mediaParts(ev.content).text || ev.content || '');
    body=body.replace(/https?:\/\/\S+/gi,' ').replace(/\b(?:nostr|wss?):\S+/gi,' ')
             .replace(/\bwww\.\S+/gi,' ')   // scheme-less URLs (www.x.com)
             .replace(/\b[a-z0-9-]+\.(?:com|net|org|io|gg|tv|xyz|co|app|me|info|dev|news|social|place|lol|sh|gov|edu)\b\S*/gi,' ')   // bare domains
             .replace(/#[\p{L}\p{N}_]+/gu,' ').replace(/\s+/g,' ').trim();
    if(!body){ toast('nothing to read aloud'); return; }
    const who=profOf(pk)||{}; const name=((who.display_name||who.name||'someone')+'').replace(/[#@_]/g,' ').replace(/\s+/g,' ').trim()||'someone';
    stopNarration();                     // one at a time, and the old chip/observer go with it
    toast('🔊 reading aloud…');
    try{
      const r=await fetch('/client/narrate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ text:`${name}. ${body}`.slice(0,2000) })});
      const j=await r.json().catch(()=>({}));
      if(!r.ok || !j.audio){ toast(j.error||'narration unavailable'); return; }
      S._narrateAudio=new Audio('data:audio/mp3;base64,'+j.audio);
      S._narrateAudio.onended=()=>stopNarration();
      _narrateChip(name, document.querySelector('.note[data-id="'+id+'"]'));
      S._narrateAudio.play().catch(()=>{   // autoplay blocked (e.g. fired from a long-press timer) — play on the next tap
        toast('tap anywhere to play 🔊');
        const go=()=>{ document.removeEventListener('click',go); document.removeEventListener('touchend',go); try{ S._narrateAudio && S._narrateAudio.play(); }catch(_){} };
        document.addEventListener('click', go, {once:true});
        document.addEventListener('touchend', go, {once:true});
      });
    }catch(_){ toast('narration failed'); }
  }
  // Summarize the post (and its surrounding thread) via the node's AI backend, shown in a modal.
  async function summarizePost(id){
    let ev=Store.get(id); if(!ev){ ev=await fetchEvent(id); if(ev) Store.saveEvent(ev); }
    if(!ev){ toast('post not loaded'); return; }
    modal('<h3><svg class="ic h-ic" aria-hidden="true"><use href="#i-article"></use></svg>Summary</h3><div id="sum-body" style="max-height:60vh;overflow:auto;line-height:1.55;white-space:pre-wrap;font-size:15px;overflow-wrap:anywhere"><div class="spinner"></div></div>'+
          '<div class="row" style="justify-content:flex-end;gap:8px;margin-top:14px"><button class="btn btn-neon small" id="sum-post" disabled><svg class="ic b-ic" aria-hidden="true"><use href="#i-send"></use></svg>Post summary</button><button class="btn btn-ghost small" id="sum-close">Close</button></div>',
      root=>{ const c=root.querySelector('#sum-close'); if(c) c.onclick=closeModal; });
    const named=e=>{ const p=profOf(e.pubkey); const nm=p.name||p.display_name||npubOf(e.pubkey).slice(0,12); return nm+': '+((mediaParts(e.content).text||e.content||'').trim()); };
    try{
      const seen=new Set([ev.id]);
      // walk up the reply chain for context (capped), oldest first
      const chain=[ev]; let cur=ev, hops=0;
      while(cur && hops<6){
        const es=(cur.tags||[]).filter(t=>t[0]==='e');
        const pid=((es.find(t=>t[3]==='reply')||es.find(t=>t[3]==='root')||es[es.length-1])||[])[1];
        if(!pid || seen.has(pid)) break;
        let p=Store.get(pid); if(!p){ p=await fetchEvent(pid); if(p) Store.saveEvent(p); }
        if(!p) break; chain.unshift(p); seen.add(pid); cur=p; hops++;
      }
      let replies=[];
      try{ replies=(await Relay.query([{ kinds:[1], '#e':[id], limit:100 }])).filter(r=>r.id!==id && !seen.has(r.id)); }catch(_){}
      replies.sort((a,b)=>a.created_at-b.created_at);
      [...chain, ...replies].forEach(e=>needProfile(e.pubkey));
      const text=[...chain.map(named), ...replies.map(named)].join('\n\n').slice(0,8000);
      const r=await fetch('/client/summarize',{ method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ text }) });
      const j=await r.json().catch(()=>({}));
      const body=$('#sum-body');
      if(body) body.innerHTML=(r.ok && j.text) ? linkify(j.text) : ('<div class="muted">'+enc(j.error||'summary unavailable')+'</div>');
      if(r.ok && j.text){ const pb=$('#sum-post'); if(pb){ pb.disabled=false; pb.onclick=()=>{ closeModal(); compose({text: j.text}); }; } }   // share the summary as a new note
    }catch(_){ const body=$('#sum-body'); if(body) body.innerHTML='<div class="muted">summary failed</div>'; }
  }

  return {
    attachEmojiAutocomplete, composeTranslate, narratePost, openEmojiPopover, openMenuPopover,
    openPostMenu, pickEmoji, stopNarration, translatePost,
  };
};

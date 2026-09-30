/* rail.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built by app.js at boot (`_railMod()`), so every
 * entry point answers synchronously. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCRailFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.VIEW, S._rbLoaded, S._updApplying
  const {
    _notifMatch, _updNotifHtml, applyUpdate, bumpNotif, decorateProfiles, enc, needProfile,
    notifGrouped, notifHtml, notifList, renderHashtag, zapSender,
  } = dep;

  // ---------- right column: Topics + Notifications (desktop) ----------
  // Two sections: the Topics chip cloud and the notifications list. Hot and "From follows" USED to live here as
  // a seven-row digest behind a segmented control; they're the centre column's Trending tab now (see
  // renderTrending) — a full feed with real note cards, which is also what finally makes them reachable
  // on a phone, where this rail is display:none.
  // The rightbar EXISTS in the DOM even on mobile (CSS display:none ≤820px), so a bare querySelector
  // isn't enough — gate on it being VISIBLE, else we run its heavy trending-tag query for a sidebar the
  // user can't see (pure waste on a slow phone link).
  function _rightbarShown(){ const rb=document.querySelector('.rightbar'); return !!rb && getComputedStyle(rb).display!=='none'; }
  async function loadRightbar(){
    if(!_rightbarShown()) return;   // display:none on mobile OR body.rb-off → skip; but build in a backgrounded desktop tab (no document.hidden gate)
    S._rbLoaded=true;                 // only once it's genuinely visible — _syncRightbar retries otherwise
    loadTopics();                   // Topics = trending hashtags (last 24h) + curated shortcuts
    bumpNotif();                    // ...and its unread count: bumpNotif only fires on ARRIVING
                                    // events, so a rail built afterwards would show a bare heading despite unread
    loadNotifs();
  }
  // Routine update (timer): refresh the chip cloud and re-paint notifications from the in-memory store.
  function refreshRightbar(){
    if(document.hidden || !_rightbarShown()) return;   // skip the periodic refresh while backgrounded or on mobile (hidden rightbar)
    // Only auto-refresh the (heavy: 300-event tag tally) rightbar while the user is on a feed it belongs
    // to. Re-tallying trending tags every interval while reading a Community/Profile/Files view was
    // needless CPU + relay load for content that isn't even being looked at.
    if(S.VIEW!=='home' && S.VIEW!=='global' && S.VIEW!=='trending') return;
    loadTopics();
    loadNotifs();   // free: reads the in-memory notification store, no relay query
  }
  // Curated hashtag shortcuts — friendly entry points into popular communities. They now PAD the one
  // Topics cloud rather than owning a section of their own: on a quiet relay the live tally can come
  // back with two chips, and these keep the block from looking broken.
  const DISCOVER_TAGS = ['foodstr', 'asknostr', 'AI', 'Bitcoin', 'nostr', 'art', 'news', 'memes'];
  const TOPIC_TRENDING=10, TOPIC_MAX=14;
  // ONE topics cloud = trending hashtags (tallied over the last 24h from explicit `t` tags AND inline
  // #hashtags in recent notes, ranked by how many distinct posts used each) followed by whatever
  // curated tags aren't already trending. Both render a clickable chip → a #tag feed. This replaces
  // the old stacked "Trending" + "Discover" pair, which spent a third of the column on two chip
  // clouds that a reader can't meaningfully tell apart.
  async function loadTopics(){
    const el=document.getElementById('rb-topics'); if(!el) return;
    const since=Math.floor(Date.now()/1000)-24*3600;
    let evs=[]; try{ evs=await Relay.query([{ kinds:[1], since, limit:300 }]); }catch(_){}   // 300 recent posts is plenty for the tag tally; 600 doubled the relay serialize + client regex cost
    const tally={};
    for(const e of evs){
      const seen=new Set();
      for(const t of (e.tags||[])){ if(t[0]==='t' && t[1]){ const g=String(t[1]).toLowerCase().replace(/^#/,''); if(/^[a-z0-9_]{2,30}$/.test(g)) seen.add(g); } }
      for(const m of (e.content||'').matchAll(/(?:^|\s)#([a-z0-9_]{2,30})\b/gi)) seen.add(m[1].toLowerCase());
      for(const g of seen) tally[g]=(tally[g]||0)+1;
    }
    const top=Object.entries(tally).filter(([,c])=>c>=2).sort((a,b)=>b[1]-a[1]).slice(0,TOPIC_TRENDING);
    const live=new Set(top.map(([g])=>g));
    // The label is its OWN span so it can ellipsize inside the fixed grid cell (see #rb-topics CSS) —
    // a bare text node can't, and one long tag was enough to leave half a row empty.
    const chips=top.map(([g,c])=>`<button class="tag-chip" data-tag="${enc(g)}" title="#${enc(g)}"><span class="tg">#${enc(g)}</span><span class="tag-n">${c}</span></button>`);
    for(const t of DISCOVER_TAGS){
      if(chips.length>=TOPIC_MAX) break;
      const g=t.toLowerCase(); if(live.has(g)) continue;   // never show a tag twice, once ranked and once curated
      chips.push(`<button class="tag-chip disc" data-tag="${enc(g)}" title="#${enc(t)}"><span class="tg">#${enc(t)}</span></button>`);
    }
    el.innerHTML=`<div class="tag-cloud">${chips.join('')}</div>`;
    el.querySelectorAll('.tag-chip').forEach(b=> b.onclick=()=>renderHashtag(b.dataset.tag));
  }
  async function fetchNotes(ids){
    const miss=ids.filter(id=>!Store.get(id)); if(!miss.length) return;
    try{ const notes=await Relay.query([{ ids:miss }]); notes.forEach(e=>{ Store.saveEvent(e); needProfile(e.pubkey); }); }catch(_){}
  }
  // ---- Notifications in the rail, actionable -----------------------------------------------
  // Reuses notifList/_notifMatch/notifGrouped/notifHtml verbatim — the same rows the Notifications
  // view builds — so the rail can't drift from it the way a second copy of that markup would. Reads
  // the in-memory notification store, so it costs no relay query.
  // Five rows. Ten (tried when Hot/Follows moved out to the Trending tab) overflowed the column: a
  // reply/mention row carries a whole quoted post, so ten of those push "Get the app" + the GitHub link
  // off the bottom. Five fits whatever KIND of notifications happen to be on top.
  const RB_NOTIF_ROWS=5;
  // Same coalescing as renderNotificationsSoon, for the same reason: the rail renders the SAME rows —
  // embedded post, media and all — so on a tablet (where the rail IS shown next to the Notifications
  // view) every arriving event rebuilt both copies of every video at once.
  let _rbNotifRT=null;
  function loadNotifsSoon(){
    if(_rbNotifRT) return;
    _rbNotifRT=setTimeout(()=>{ _rbNotifRT=null; try{ loadNotifs(); }catch(_){} }, 300);
  }
  function loadNotifs(){
    if(_rbNotifRT){ clearTimeout(_rbNotifRT); _rbNotifRT=null; }
    const el=document.getElementById('rb-list'); if(!el) return;
    const upd=_updNotifHtml('upd-notif-rb');
    // The rail is deliberately 5 rows tall. The updater takes one of them rather than making it 6 —
    // the row count is what keeps the column from pushing "Get the app" off the bottom.
    const all=notifGrouped(notifList().filter(_notifMatch)).slice(0, upd ? RB_NOTIF_ROWS-1 : RB_NOTIF_ROWS);
    if(!all.length && !upd){ el.innerHTML='<div class="muted small">No notifications yet.</div>'; return; }
    el.innerHTML=upd+all.map(notifHtml).join('');
    { const un=el.querySelector('#upd-notif-rb'); if(un && !S._updApplying) un.onclick=applyUpdate; }
    // Quick actions are APPENDED to the rendered row rather than spliced into notifHtml's string:
    // the row already carries the resolved target in data-open (which differs by kind — a reply opens
    // itself, a reaction opens the post it reacted to), so we just read it back off the DOM.
    el.querySelectorAll('.notif[data-open]:not(.upd-notif)').forEach(n=>{
      const id=n.dataset.open, ev=Store.get(id); if(!ev) return;   // target not on this relay → no actions
      const body=n.lastElementChild; if(!body) return;
      const bar=document.createElement('div'); bar.className='rbq-bar';
      // U+FE0E (text presentation) on both glyphs. They already share one class and one set of rules, but
      // ↩ U+21A9 has an emoji form and the system font was drawing it as the colour ↩️ — so Reply came out
      // as a coloured badge next to a flat ♥. The selector pins both to the flat text glyph.
      bar.innerHTML=`<button class="rbq" data-q="react" data-id="${id}" data-pk="${ev.pubkey}" title="React"><svg class="ic x-ic" aria-hidden="true"><use href="#i-heart"></use></svg></button>`
                   +`<button class="rbq" data-q="reply" data-id="${id}" data-pk="${ev.pubkey}" title="Reply"><svg class="ic b-ic" aria-hidden="true"><use href="#i-reply"></use></svg></button>`;
      body.appendChild(bar);
    });
    all.forEach(e=>{ if(e.type==='group') e.events.forEach(x=>needProfile(x.pubkey)); else needProfile(e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey); });
    decorateProfiles();
  }

  return {
    _rightbarShown, fetchNotes, loadNotifs, loadNotifsSoon, loadRightbar, refreshRightbar,
  };
};

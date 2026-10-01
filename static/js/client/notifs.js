/* notifs.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_notifsMod()`) at the
 * point where this code used to run — the notification subscription starts at login, the Android and
 * desktop notification routes land here at boot, and the top-level statements below (the follower pins
 * and the one-time epoch repair) run when the factory is built, in the order they always did. The code
 * below is app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCNotifsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.GUEST, S.LOGO, S.ME, S.VIEW, S._aiToken, S._apkUpdate, S._newBuild, S._notifEpoch
  const {
    $, $$, FOLLOWERS, _fetchTimeout, _instanceBase, _notifCtxId, _notifRouteViaDesktop,
    _rightbarShown, _standalone, _tipNote, applySobLive, emojiName, enc, ensureAiSession, fmtSats,
    invalidateCounts, isMutedAuthor, isReply, loadNotifs, loadNotifsSoon, needProfile,
    notificationAllowed, notificationSound, openThread, osNotify, profOf, reactDisp, reminderAlert,
    renderNotificationsSoon, seenNotif, switchView, zapAmount, zapSender,
  } = dep;

  // ---------- notifications ----------
  let _notifReady=false;
  // A follow is a kind-3 (the follower's WHOLE contact list), republished every time they
  // follow/unfollow ANYONE — so each republish looked like a brand-new "followed you". Record the
  // FIRST time we see each follower and key the notification off that stable time, so a known
  // follower re-saving their list never re-pings or re-lights the badge.
  let _followSeen={}; try{ _followSeen=JSON.parse(localStorage.getItem('pc_follow_seen')||'{}')||{}; }catch(_){ _followSeen={}; }
  let _followSeeded=false;   // true once the follower seed (below) has run — before that, notifList must NOT persist pins
  let _followReady=false;    // true after the follows (kind-3) sub reaches EOSE — only then is a kind-3 a genuinely NEW follow
  // One-time repair of installs poisoned by the old clamp: they persisted pins at the contact-list SAVE
  // time, so existing users carry a pile of long-time followers pinned as if they'd just followed.
  // Clamping every stored pin to the epoch says exactly "not newer than when we started tracking".
  if(!localStorage.getItem('pc_notif_epoch_migrated')){
    let _ch=false; for(const k in _followSeen){ if(!(_followSeen[k]>0) || _followSeen[k]>S._notifEpoch){ _followSeen[k]=S._notifEpoch; _ch=true; } }
    if(_ch) try{ localStorage.setItem('pc_follow_seen', JSON.stringify(_followSeen)); }catch(_){}
    try{ localStorage.setItem('pc_notif_epoch_migrated','1'); }catch(_){}
  }
  // Treat a 0/falsy stored value as UNSET (old builds persisted 0 when seenNotif.last was 0 → "55 years ago"),
  // so it gets repaired to a real time on next access instead of sticking.
  function _followTs(pk, fallback){ if(!_followSeen[pk]){ _followSeen[pk]=fallback||Math.floor(Date.now()/1000); try{ localStorage.setItem('pc_follow_seen', JSON.stringify(_followSeen)); }catch(_){} } return _followSeen[pk]; }
  // Pin a follower we're meeting from HISTORY (the seed, the relay's pre-EOSE backlog, the Store cache) —
  // never at their kind-3's created_at, which is just their last contact-list SAVE. The epoch floor holds
  // them below the line permanently, however many times they re-save.
  function _followTsOld(pk, created){ return _followTs(pk, Math.min(created, S._notifEpoch)); }
  // Unpinned kind-3 (seed missed them / storage cleared) gets the same conservative time, so the ORDER is
  // right even when nothing has been recorded yet — the pin is a cache, not the thing correctness rests on.
  function _notifTs(e){
    if(e.kind!==3) return e.created_at;
    return _followSeen[e.pubkey] || Math.min(e.created_at, S._notifEpoch);
  }
  async function watchNotifications(){
    /* FORWARD ONLY. Another monitor's read can arrive (a `storage` event, app.js) before this runs, and
     * re-reading storage here then put the marker BACK -- that monitor's bell relit for what it had
     * already read. A read marker never moves backwards. */
    seenNotif.last = Math.max(seenNotif.last || 0, +(localStorage.getItem('pc_notif_seen')||0));
    // Seed the known-follower set from the FULL current follower list BEFORE going live. kind-3 is the
    // follower's whole contact list, republished on every edit — so without a comprehensive seed, a
    // fresh client / cleared storage / a follower beyond the live sub's 150-cap re-pings as a "new
    // follow" every time anyone edits their list. We mark every existing follower as already-seen (their
    // _followSeen time = last-checked), so only a pubkey we've NEVER recorded — a genuinely new follower
    // arriving live — pings/badges. (the recurring "follow spam from people who followed long ago")
    // Run the seed in the BACKGROUND so it never delays the live mention/zap/DM subscription below — the
    // retry can take a few seconds on a laggy link. Until it completes, _followSeeded stays false and the
    // kind-3 badge-bump is HELD (a follow only touches the badge, never a ping), so a slow/failed seed
    // can't resurface old followers; the only cost is not badging a brand-new follow in the first ~second.
    (async()=>{ try{
      // Retry the seed on a laggy link (Thailand→US): a single REQ can EOSE empty before the relay serves
      // the follower kind-3s, leaving _followSeen unpopulated → every old follower re-saving their contact
      // list post-EOSE re-pings as a brand-new "followed you". Retry with backoff until it returns (same
      // fix as restoreMediaServer). Empty after all tries → genuinely no followers (or offline) → no harm.
      // Wait for a live socket before the first attempt — the retry loop below was the band-aid for a REQ
      // being silently dropped onto a still-CONNECTING socket (relay.js `_send`); this fixes the cause, so
      // the retries now only cover genuine relay lag.
      try{ await Relay.ready(); }catch(_){}
      // Retry until the read is COMPLETE, not merely non-empty. query() sets `complete` only when every
      // relay EOSE'd; a timeout resolves with whatever arrived. Stopping at the first non-empty answer
      // accepted a PARTIAL follower list as the seed — and every follower missing from it was then
      // "never recorded", so the next time they re-saved their contact list (~25 of them do daily) the
      // live sub scored it genuine=true and pinned it at NOW: an old follower resurfacing as a brand-new
      // "followed you", with a badge. Keep the best (longest) answer across attempts.
      let followers=[], seedComplete=false;
      for(let attempt=0; attempt<4 && !seedComplete; attempt++){
        if(attempt>0) await new Promise(r=>setTimeout(r, 600*attempt));
        try{
          const r = (await Relay.query([{ kinds:[3], '#p':[S.ME.pubkey], limit:1000 }]))||[];
          if(r.length >= followers.length) followers = r;
          if(r.complete) seedComplete = true;
        }catch(_){}
      }
      let changed=false;
      for(const e of followers){ FOLLOWERS.add(e.pubkey);
        // Pin an existing follower at/below the STABLE epoch so a later re-save can't float them to the
        // top. Anyone in this seed already followed us before we started tracking, so their kind-3's
        // created_at (their last contact-list save) must never be taken as "when they followed you".
        if(!_followSeen[e.pubkey]){ _followSeen[e.pubkey]= Math.min(e.created_at, S._notifEpoch); changed=true; } }
      if(changed){ try{ localStorage.setItem('pc_follow_seen', JSON.stringify(_followSeen)); }catch(_){} }
      // Seeded ONLY on a complete read that returned followers. A partial or empty answer must not be
      // claimed as the baseline: everything absent from it would score as a never-seen follower and get
      // pinned at its re-save time. (Harmless when you genuinely have no followers — there's no kind-3
      // to mis-pin.)
      _followSeeded = seedComplete && followers.length>0;
      renderNotificationsSoon();   // reflect the seeded ordering once the async seed lands
    }catch(_){}
      // Sub B — follows (kind-3), subscribed only AFTER the seed above, so every kind-3 is judged against a
      // populated _followSeen: an existing follower re-saving their contact list is firstTime=false and
      // never resurfaces; only a pubkey we've NEVER recorded, arriving after THIS sub's own EOSE, badges as
      // a new follow. Its own _followReady (not the mentions sub's EOSE) is the backlog→live boundary.
      Relay.subscribe([{ '#p':[S.ME.pubkey], kinds:[3], limit:150 }], {
        onEvent: ev => { if(ev.pubkey===S.ME.pubkey) return; if(Store.saveEvent(ev)){ needProfile(ev.pubkey);
          FOLLOWERS.add(ev.pubkey);
          const firstTime = !_followSeen[ev.pubkey];
          // "Never recorded" only means "genuinely new" if the seed actually populated _followSeen. When
          // the seed failed, or was capped (limit:1000) on a big account, firstTime just means "we never
          // looked" — pinning those at their re-save time is precisely what produced the flood. Treat them
          // as history instead, and record a genuine new follow at the time WE observed it rather than at
          // the follower's contact-list save time.
          const genuine = _followReady && _followSeeded && firstTime;
          const ts = genuine ? _followTs(ev.pubkey, Math.floor(Date.now()/1000))
                             : _followTsOld(ev.pubkey, ev.created_at);
          if(genuine && ts>seenNotif.last) bumpNotif();   // only a post-EOSE, never-seen follower badges
          renderNotificationsSoon(); } },
        onEose: ()=>{ _followReady=true; renderNotificationsSoon(); }
      });
    })();
    // Sub A — mentions/reposts/reactions/zaps/reports/chat/comments. Subscribed IMMEDIATELY, never gated on
    // the follower seed, so live mentions/zaps aren't delayed by the seed's laggy-link retry.
    Relay.subscribe([{ '#p':[S.ME.pubkey], _include_quotes:true, kinds:[1,6,7,9735,1984,1111,1621,1617], limit:150 }], {   // 42=chat, 1111=community comments, 1621/1617=NIP-34 issue/patch on your repo
      onEvent: ev => { if(ev.pubkey===S.ME.pubkey) return; if(Store.saveEvent(ev)){ invalidateCounts(); applySobLive(ev); needProfile(ev.kind===9735?(zapSender(ev)||ev.pubkey):ev.pubkey);
        if(ev.created_at>seenNotif.last){ bumpNotif(); if(_notifReady) notifPing(ev); }
        renderNotificationsSoon(); } },
      onEose: ()=>{ _notifReady=true; if(S.VIEW==='notifications') renderNotificationsSoon(); else bumpNotif(); }   // show unseen count on load; ping LIVE ones
    });
  }
  function _quotesMe(ev){
    return ev.kind===1 && (ev.tags||[]).some(t=>Array.isArray(t) && t[0]==='q'
      && typeof t[1]==='string' && /^[0-9a-f]{64}$/.test(t[1]) && t[3]===S.ME.pubkey);
  }
  function _notifForMe(ev){
    return (ev.tags||[]).some(t=>Array.isArray(t) && t[0]==='p' && t[1]===S.ME.pubkey) || _quotesMe(ev);
  }
  function notifPing(ev){
    // NEVER toast/OS-notify for follows. kind-3 is a whole contact list, republished on every edit, so
    // an OLD follower constantly looks "new" — detecting genuine new follows reliably is impossible
    // (the relay can't be trusted to have every follower's current list). Follows still show in the
    // Notifications list (Follows tab); they just don't interrupt. Kills the recurring follow spam.
    if(ev.kind===3) return;
    const notificationType=ev.kind===7?'likes':ev.kind===6?'reposts':ev.kind===9735||_tipNote(ev)?'zaps':
      ev.kind===1111||isReply(ev)?'replies':_quotesMe(ev)?'quotes':'mentions';
    if(!notificationAllowed(notificationType))return;
    const fromPk = ev.kind===9735?(zapSender(ev)||ev.pubkey):ev.pubkey;
    if(isMutedAuthor(fromPk)) return;   // no toast / OS notification for a muted author
    const p=profOf(fromPk); const who=p.name||p.display_name||'someone';
    const _tn=_tipNote(ev);
    const what = ev.kind===9735?`⚡ zapped you ${fmtSats(zapAmount(ev))} sats`
      : _tn?`${_tn.icon} tipped you${_tn.amt?' '+enc(_tn.amt)+' '+enc(_tn.unit):''}`
      : ev.kind===3?'🫂 followed you'
      : ev.kind===1984?'🚩 reported you'
      : ev.kind===7?`reacted ${reactDisp(ev)}`
      : ev.kind===6?'reposted you'
      : ev.kind===1111?'replied to you'
      : ev.kind===1621?'🐛 opened an issue on your repo'
      : ev.kind===1617?'🩹 sent a patch to your repo'
      : _quotesMe(ev)?'quoted your post' : isReply(ev)?'replied to you' : 'mentioned you';
    notifToast(`🔔 <b>${emojiName(fromPk, who)}</b> ${what}`, p.picture, null, notificationType);   // render the sender's custom :emoji: in the toast
    const target=_notifCtxId(ev)||ev.id;
    osNotify('PosterChan', `${who} ${what}`, { icon:p.picture||S.LOGO,
                                               tag:'nostr-'+ev.id, notificationType,
                                               route:target?'post:'+target:'notifications',
                                               onClick:()=>target?openThread(target):switchView('notifications') });
  }
  // `html` is trusted markup (callers build names via emojiName + enc their content) — do NOT re-escape it.
  /* ONE DESKTOP ANNOUNCES. PosterChanOS runs one renderer per monitor (and every popped-out window is
   * a document of its own), and each of them receives the same event — so every arrival used to pop a
   * card AND play the chime once per monitor ("each monitor does things separately"). Only the primary
   * desktop surface announces; the others still COUNT (the bell is painted from notifUnread, which the
   * shared read marker keeps in step). Outside PosterChanOS there is one page and this is always true. */
  function announcesArrivals(){
    try{ return !(window.pcShell && window.pcShell.backgroundOwner === false); }catch(_){ return true; }
  }
  function notifToast(html, pic, onClick, notificationType){
    if(!notificationAllowed(notificationType))return;
    if(!announcesArrivals())return;
    notificationSound();
    // On the desktop these become Windows-style cards in the bottom-right corner instead (with the
    // arrival chime). Routed here rather than detected again in os.js: this function is already the
    // ONE place a live notification, DM or new email announces itself, so the two cannot disagree
    // about what arrived. onClick names where the card should GO — email belongs in Messages, not in
    // the Notifications view this used to send everything to.
    const go = onClick || (() => { if(!_notifRouteViaDesktop('notifications')) switchView('notifications'); });
    try{ if(window.PCOS && PCOS.isOn() && PCOS.osToast){ PCOS.osToast(html, pic, go, notificationType); return; } }catch(_){}
    const t=document.createElement('div'); t.className='toast notif-toast';
    t.innerHTML=`<img src="${enc(pic||S.LOGO)}" onerror="this.src='${S.LOGO}'"><span>${html}</span>`;
    t.onclick=()=>{ go(); t.remove(); };
    $('#toast-root').appendChild(t); setTimeout(()=>t.remove(),5000);
  }
  // Reminder history is not a Nostr event. Keep it separate from Store, scoped to both the
  // account and instance (numeric reminder IDs belong to one backend). The backend is authoritative
  // for missed deliveries; this bounded cache also works offline and across native popup windows.
  const _reminderLoads=new Map();
  let _reminderEpochOwner='', _reminderEpochAt=0;
  function _reminderOwner(){ return S.ME&&S.ME.pubkey ? _instanceBase()+':'+S.ME.pubkey : ''; }
  function _reminderHistoryDays(owner=_reminderOwner()){
    try{const days=Number(localStorage.getItem('pc_reminder_history_days:'+owner));
      if(Number.isInteger(days)&&days>=1&&days<=365)return days;
    }catch(_){}
    return 7;
  }
  function _reminderRows(owner=_reminderOwner()){
    if(!owner)return [];
    try{const rows=JSON.parse(localStorage.getItem('pc_reminder_history:'+owner)||'[]');
      // Apply retention on every read, including offline startup and a long-lived desktop.
      // A server-side history limit cannot expire rows already stored by an older client.
      const cutoff=Date.now()/1000-_reminderHistoryDays(owner)*86400;
      return Array.isArray(rows)?rows.filter(x=>x&&x.type==='reminder'&&typeof x.id==='string'&&Number.isFinite(x.created_at)&&x.created_at>=cutoff).slice(0,200):[];
    }catch(_){return [];}
  }
  function _rememberReminder(data,owner=_reminderOwner(),live=false){
    if(!owner||owner!==_reminderOwner()||!data)return false;
    const rid=String(data.reminder_id||'');
    const due=String(data.due_at||'');
    // Older servers have no stable occurrence metadata: still show their alert, but don't invent
    // a durable identity from wall-clock arrival time and duplicate it on reconnect.
    if(!rid||!due||!Number.isFinite(Date.parse(due)))return false;
    const id='reminder:'+rid+':'+new Date(due).toISOString();
    const rows=_reminderRows(owner), previous=rows.find(x=>x.id===id);
    const row={type:'reminder',id,alerted:live||!!(previous&&previous.alerted),created_at:Math.floor(Date.parse(data.delivered_at||due)/1000),
      content:String(data.content||'Reminder').slice(0,10000),route:data.route==='calendar'?'calendar':'notifications'};
    if(!Number.isFinite(row.created_at)||row.created_at<Date.now()/1000-_reminderHistoryDays(owner)*86400)return false;
    const next=[row,...rows.filter(x=>x.id!==id)].sort((a,b)=>b.created_at-a.created_at).slice(0,200);
    try{localStorage.setItem('pc_reminder_history:'+owner,JSON.stringify(next));}catch(_){}
    return live ? !(previous&&previous.alerted) : !previous;
  }
  function _remindersChanged(){
    try{bumpNotif();renderNotificationsSoon();loadNotifs();}catch(_){}
  }
  async function hydrateReminderNotifications(){
    const owner=_reminderOwner();if(!owner||S.GUEST||_standalone())return;
    if(_reminderEpochOwner!==owner){_reminderEpochOwner=owner;_reminderEpochAt=Date.now();_reminderLoads.delete(owner);}
    const epoch=_reminderEpochAt;
    const current=()=>owner===_reminderOwner()&&_reminderEpochOwner===owner&&_reminderEpochAt===epoch&&_reminderLoads.get(owner)===state;
    const old=_reminderLoads.get(owner);
    if(old&&(old.pending||Date.now()-old.at<30000))return;
    const state={pending:true,at:Date.now()};_reminderLoads.set(owner,state);
    try{
      await ensureAiSession();if(!current())return;
      const r=await _fetchTimeout('/api/auth/reminder-notifications',{credentials:'include',
        headers:S._aiToken?{'Authorization':'Bearer '+S._aiToken}:{}},10000);
      if(!r.ok)throw new Error('reminder history unavailable');
      const data=await r.json();if(!current())return;
      if(!data||!Array.isArray(data.items))throw new Error('invalid reminder history');
      const days=Number(data.history_days);
      if(Number.isInteger(days)&&days>=1&&days<=365){
        try{localStorage.setItem('pc_reminder_history_days:'+owner,String(days));}catch(_){}
      }
      for(const row of data.items.slice(0,200)){
        _rememberReminder(row,owner);
        // The AI conversation socket closes outside AI. Polling is the quiet fallback: alert only
        // occurrences delivered while this account was active, never old startup history imports.
        if(Date.parse(row.delivered_at||row.due_at)>=epoch)reminderAlert(row.content,row);
      }
    }catch(_){/* Keep cached history; the next poll/open retries without discarding it. */}
    finally{
      // Cached reminders also age out while offline. Repaint while this load remains pending
      // so a notification view asking for its rows cannot start a recursive request.
      if(current())_remindersChanged();
      state.pending=false;
    }
  }
  function notifList(){
    // THIS list is the gate that decides what a notification even is — subscribing to a kind and giving it
    // a row renderer is not enough, because everything still has to survive this filter. 1621/1617 (NIP-34
    // issue/patch on a repo you maintain) were added to the subscription and the renderer but not here, so
    // they were fetched, toasted live, and then dropped from the list that actually renders.
    const evs=Store.all().filter(e=>[1,6,7,9735,3,1984,1621,1617,1111].includes(e.kind) && e.pubkey!==S.ME.pubkey && !isMutedAuthor(e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey) && _notifForMe(e)
      // A reaction or repost with no `e` tag says "someone liked something" and can't say what. The row
      // has nothing to open, and the handler's `ref||e.id` fallback opened the REACTION as a thread,
      // which renders as an empty one. Drop them here so a malformed event from any source — our fedi
      // bridge produced one — can't put an unusable row in the list.
      && !((e.kind===7 || e.kind===6) && !e.tags.some(t=>t[0]==='e'&&t[1])));
    // PIN each follower's notification time on FIRST sight (persisted) BEFORE sorting — otherwise a
    // re-saved contact list (a NEW kind-3 with a fresh created_at) keeps sorting to the top and re-shows
    // an old follower as "followed you" over and over. _followTs records once; _notifTs then reads it.
    // Everything in the Store is history (a genuinely new follow was already pinned at its real time by the
    // live subscription), so pin it as history — never at the re-save time. ONLY after a seed that actually
    // read the follower list, else an early/cold render would persist a pin we can't yet stand behind.
    if(_followSeeded) for(const e of evs){ if(e.kind===3) _followTsOld(e.pubkey, e.created_at); }
    evs.push(..._reminderRows());
    evs.sort((a,b)=>_notifTs(b)-_notifTs(a));
    // dedupe follows by author — a follower re-saving their contact list shouldn't show "followed you" repeatedly
    const seen3=new Set(); const out=[];
    for(const e of evs){ if(e.kind===3){ if(seen3.has(e.pubkey)) continue; seen3.add(e.pubkey); } out.push(e); }
    return out.slice(0,2000);
  }
  // Follows DO light the bell now. The extra `ts > _notifEpoch` test (rather than just dropping the old
  // `kind!==3` exclusion) is what makes that safe: history followers are pinned at/below the epoch, and on
  // a fresh install seenNotif.last is 0 — so a bare `ts > seenNotif.last` would count every existing
  // follower as unread and open the app with a badge of hundreds. Only a follow recorded AFTER we started
  // tracking is genuinely unread.
  // WHAT "UNREAD" MEANS, in ONE place — because a FOURTH surface (the desktop's tray bell) computed its
  // own answer and got it wrong in a way that could only ever go quiet. It read `notifItems(60).length -
  // <count when the centre was last opened>`, i.e. the length of a list that is SLICED to 60: any account
  // past 60 notifications has notifItems(60).length === 60 for ever, so the moment the centre was opened
  // once the subtraction was 60-60 and the bell never lit again however much arrived. It lit on the first
  // login only because nothing had been opened yet. A count is not a read-marker — unread is a comparison
  // against seenNotif.last, and it is this function on every surface now.
  function notifUnread(){ return notifList().filter(e=>{
      const ts=_notifTs(e);
      if(ts<=seenNotif.last) return false;
      return e.kind===3 ? ts>S._notifEpoch : true;
    // Count the update toward the badge from the SAME condition renderNotifications() draws the row from
    // (_newBuild || _apkUpdate) — NOT the separate _updBadge, which cleared on view and left the badge
    // showing +1 with no matching row in the rail ("a number with no notification"). Now they can't disagree:
    // the badge shows the update iff the row is there, and it clears when you actually apply the update.
    }).length + ((S._newBuild||S._apkUpdate)?1:0); }
  function bumpNotif(){ const n=notifUnread();
    // The rail's Notifications heading is painted from the SAME count as the sidebar bell and the mobile bar —
    // one computation, three surfaces, so they can't disagree about whether something is unread.
    $$('#notif-badge,#notif-badge-m,#rb-notif-badge').forEach(b=>{ if(n){b.textContent=n>99?'99+':n;b.classList.remove('hidden');}else b.classList.add('hidden');});
    // …and the desktop's tray bell is the fourth. It has nothing to subscribe to, and the taskbar only
    // repaints on a window focus, the 30s clock tick and an arrival TOAST — follows and everything that
    // lands before _notifReady toast nothing, so the bell could be minutes behind the badge beside it.
    try{ if(window.PCOS && PCOS.notifChanged) PCOS.notifChanged(); }catch(_){}
    // Keep the rail's notification LIST live too, not just its badge: if one lands while you're
    // looking at it, re-render now instead of leaving it stale until the 150s refresh. In-memory read (no
    // relay query); gated so it doesn't churn during the initial load burst or when the rail is hidden (mobile).
    if(_notifReady && _rightbarShown()) loadNotifsSoon(); }

  return {
    _notifTs, _quotesMe, _rememberReminder, _reminderOwner, _remindersChanged, bumpNotif,
    hydrateReminderNotifications, notifList, notifToast, notifUnread, watchNotifications,
  };
};

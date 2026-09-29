/* notifview.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built by app.js at boot (`_notifviewMod()`), so every
 * entry point answers synchronously. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCNotifViewFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.LOGO, S.ME, S.VIEW, S._apkUpdate, S._desktopUpdate, S._newBuild, S._notifEpoch, S._updApplying, S._updBadge
  const {
    $, $$, NT, _SHORTCODE_STRIP, _notifTs, _quotesMe, _repoTag, _tipNote, applyEmojis, applyUpdate,
    emojiName, enc, fmtSats, hydrateReminderNotifications, isReply, needEvent, needProfile,
    notifList, openOsNotificationRoute, openThread, profOf, quotedDiv, reactDisp,
    renderProfileView, replyParentId, seenNotif, timeAgo, zapAmount, zapSender,
  } = dep;

  let _notifShown = 25;   // paginate: render a page at a time, "Load more" reveals the next
  let _notifFilter = 'all';
  // The initial mixed-kind subscription is capped at 150. Rare kinds (especially zap receipts and
  // Monero tip notes) can have zero rows in that window even though years of history exist. Keep a
  // filter-owned cursor so its tab can ask the relay directly instead of requiring 25 already-loaded
  // zaps before the first "Load more" button can exist.
  let _notifZapUntil = 0, _notifZapDone = false;
  const _NOTIF_TABS = [['all','All'],['mentions','@ Mentions'],['reactions','♥ Reactions'],['zaps','⚡ Zaps'],['follows','🫂 Follows'],['reports','🚩 Reports'],['reminders','Reminders']];
  function _notifMatch(e){
    if(e.type==='reminder')return _notifFilter==='all'||_notifFilter==='reminders';
    switch(_notifFilter){
      case 'reminders': return false;
      case 'mentions': return (e.kind===1 && !_tipNote(e)) || e.kind===1111 || e.kind===1621 || e.kind===1617;   // incl. chat + community replies + git issues/patches; a tip note belongs in Zaps, not here
      case 'reactions': return e.kind===7||e.kind===6;
      case 'zaps': return e.kind===9735 || !!_tipNote(e);   // Lightning zaps + BCH/Monero address tips share the ⚡ tab
      case 'follows': return e.kind===3;
      case 'reports': return e.kind===1984;
      // "All" carries only follows we actually WATCHED arrive (pinned after _notifEpoch, the same test
      // bumpNotif uses for the badge). Everyone who was already following when this install started is
      // history — a follower list, not a notification — and belongs in the 🫂 Follows tab. Without this,
      // a fresh install (new APK, cleared storage) pins each of them at their last contact-list save, so
      // the ones who saved recently still read as brand-new follows even once the ordering is right.
      default: return !(e.kind===3 && _notifTs(e)<=S._notifEpoch);
    }
  }
  let _notifScrollTop = false;   // next notifications render lands at the top (fresh tab entry only)
  /* A FRESH ENTRY into Notifications: collapse pagination back to one page and land at the TOP.
   * #feed is one scroll container shared by every view, and nothing reset it on a view switch, so
   * opening Notifications kept whatever offset the previous view had left — you arrived mid-list.
   * One-shot (mirrors _dmScrollTop): the LIVE re-renders that fire as relay events arrive must NOT
   * yank you back to the top while you're reading. */
  function _notifFreshEntry(){ _notifShown = 25; _notifScrollTop = true; }
  // Mark everything up to now as seen and drop the unread badge on all three surfaces (sidebar bell,
  // mobile bar, rail heading). Shared by the full Notifications view and the rail so
  // the two can't disagree about what "read" means.
  function markNotifsRead(){
    seenNotif.last = Math.floor(Date.now()/1000); localStorage.setItem('pc_notif_seen', seenNotif.last);
    S._updBadge=false;   // clears the one-shot update badge too (no phantom permanent +1)
    $$('#notif-badge,#notif-badge-m,#rb-notif-badge').forEach(b=>b.classList.add('hidden'));
    // The desktop's tray bell reads the same unread count, so it clears here too — opening the
    // Notifications app in a window has to empty the bell, not only the badge inside that window.
    try{ if(window.PCOS && PCOS.notifChanged) PCOS.notifChanged(); }catch(_){}
  }
  // The updater row, shared by the Notifications VIEW and the right-column rail — the same prompt in both
  // places, so a desktop reader who lives on the timeline is not told to go and find it. Distinct ids
  // because both can be on screen at once (the rail is visible while viewing Notifications).
  function _updNotifHtml(id){
    if(!(S._newBuild || S._apkUpdate)) return '';
    const body = S._updApplying ? ' <span class="muted small">applying the new version</span>'
      : (S._desktopUpdate ? ' \u2014 a new desktop build is ready<div class="muted small">tap to open the download page</div>'
      /* NO ZAPSTORE BRANCH ANY MORE. `_fromZapstore` and the `_installer` it read were deleted when
         Android moved to direct signed upgrades, and this one call site was left behind — so with
         an APK update pending this threw ReferenceError, and it threw INSIDE the notifications
         render. Both callers (the view and the right rail) build their HTML through here, so the
         whole screen died: reported as "notifications is slow" and separately as
         "_fromZapstore is not defined when it start". They were the same bug. */
      : (S._apkUpdate ? ' — a new PosterChan app version is ready<div class="muted small">tap to download &amp; install the update</div>'
                    : ' — a new version of the app is ready<div class="muted small">tap to reload &amp; update</div>'));
    return `<div class="notif upd-notif" id="${id}"><span class="ic">🔄</span>`
         + `<div><b>${S._updApplying?'Updating…':'Update available'}</b>${body}</div></div>`;
  }
  // Coalesce EVENT-DRIVEN redraws. renderNotifications() is a whole-innerHTML rebuild of up to 25 rows,
  // each embedding the full referenced post (quotedDiv: text, images and video), and it was called once per
  // arriving event — so the initial subscription burst alone rebuilt the view ~150 times, tearing down and
  // recreating every media element mid-load each time. On the Android tablet that read as videos
  // "reloading over and over, never showing a preview" and then killed the WebView's render process. One
  // render per burst instead; anything the USER does (tab, filter, load more) still redraws immediately.
  let _notifRT=null;
  function renderNotificationsSoon(){
    if(S.VIEW!=='notifications' || _notifRT) return;
    _notifRT=setTimeout(()=>{ _notifRT=null; if(S.VIEW==='notifications') try{ renderNotifications(); }catch(_){} }, 300);
  }
  /* What the next render WOULD produce, as a short string: the filter, how many rows, whether the
   * updater row is up, and each row's identity plus whether its context post has arrived yet.
   *
   * Notifications re-render on every arriving event, every profile that lands, and every context
   * post that resolves — and the render is one `feed.innerHTML = …`, which destroys and rebuilds
   * every node in the list. A row's context is `quotedDiv`, i.e. the real media gallery, so each of
   * those rebuilds tears down and re-creates live <video> elements: the flashing. Most of those
   * renders change nothing that is visible, so comparing first turns them into no-ops. */
  let _notifSig = '';
  function _notifSigOf(list, upd){
    const part = e => {
      let id = e.id || '';
      if(e.type === 'group') id = 'g' + e.events.length + ':' + ((e.events[0]||{}).id || '');
      let ctx = '';
      try{ const c = _notifCtxId(e); ctx = c ? (Store.get(c) ? '+' : '-') : ''; }catch(_){}
      return id + ctx;
    };
    return `${_notifFilter}|${_notifShown}|${upd ? 1 : 0}|${list.map(part).join(',')}`;
  }

  function renderNotifications(){
    hydrateReminderNotifications();
    if(_notifRT){ clearTimeout(_notifRT); _notifRT=null; }   // a direct render satisfies any pending one
    const feed=$('#feed');
    const all=notifGrouped(notifList().filter(_notifMatch));
    const list=all.slice(0, _notifShown);
    const tabs=`<div class="notif-tabs">${_NOTIF_TABS.map(([k,l])=>`<button class="ntab${k===_notifFilter?' on':''}" data-nf="${k}">${enc(l)}</button>`).join('')}</div>`;
    // In-app updater: pinned above the list when a new build is ready to install.
    const upd = _updNotifHtml('upd-notif');
    // Nothing visible changed AND the list is still on screen → leave the DOM alone. #feed is shared
    // and blanked when a view is entered, so the tab strip's presence is what proves our rows are
    // still there; without that check the guard would skip the first render after coming back.
    const sig = _notifSigOf(list, upd);
    if(sig === _notifSig && feed.querySelector('.notif-tabs')){ markNotifsRead(); return; }
    _notifSig = sig;
    const loadedMore=all.length>_notifShown;
    const relayMore=_notifFilter==='zaps' && !_notifZapDone;
    const moreHtml=(loadedMore||relayMore)
      ? `<button class="btn btn-ghost full" id="notif-more">${loadedMore
          ? `Load ${Math.min(25,all.length-_notifShown)} more (${all.length-_notifShown})`
          : 'Load older tips and zaps'}</button>` : '';
    feed.innerHTML = tabs + upd + (all.length
      ? list.map(notifHtml).join('') + moreHtml
      : (upd ? moreHtml : '<div class="empty">No notifications here.</div>'+moreHtml));
    if(_notifScrollTop){ _notifScrollTop=false; feed.scrollTop=0; }
    { const un=$('#upd-notif',feed); if(un && !S._updApplying) un.onclick=applyUpdate; }
    $$('.ntab',feed).forEach(b=> b.onclick=()=>{ _notifFilter=b.dataset.nf; _notifShown=25; renderNotifications(); });
    list.forEach(e=>{ if(e.type==='reminder')return; if(e.type==='group') e.events.forEach(x=>needProfile(x.pubkey)); else needProfile(e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey); });
    markNotifsRead();
    // row opens the post; avatar opens the sender's profile (stop the row handler firing too). EXCLUDE the
    // updater row (.upd-notif) — it keeps its own applyUpdate handler and has no post/profile to open.
    /* THE ROW MUST STOP THE EVENT. #feed carries a DELEGATED `[data-open]` handler and a row has
     * `data-open`, so one click ran both — two openThread in one tick, which the desktop's window
     * dedupe cannot catch (it reads a compositor snapshot a tick late). On a REPLY they are not even
     * the same post: the row embeds the parent as a quote card whose `.quoted` carries the PARENT's
     * id, so two different windows opened. The avatar binding below always stopped propagation. */
    feed.querySelectorAll('.notif:not(.upd-notif)').forEach(n=> n.onclick=(ev)=>{ ev.stopPropagation(); n.dataset.route ? openOsNotificationRoute(n.dataset.route) : n.dataset.prof ? renderProfileView(n.dataset.prof) : openThread(n.dataset.open); });
    feed.querySelectorAll('.notif-av').forEach(a=> a.onclick=(ev)=>{ ev.stopPropagation(); renderProfileView(a.dataset.pk); });
    const more=$('#notif-more'); if(more) more.onclick=async ()=>{
      _notifShown+=25;
      // Reaching the end of what's loaded → fetch OLDER notifications from the relay (paginate back
      // in time with `until`), so notifications aren't capped at the initial window.
      if(_notifFilter==='zaps' && _notifShown >= all.length-5){
        more.textContent='Loading older tips and zaps…'; more.disabled=true;
        const localOldest=all.length ? Math.min(...all.map(e=>e.created_at||0).filter(Boolean)) : 0;
        const until=(_notifZapUntil || localOldest || Math.floor(Date.now()/1000)+1)-1;
        try{
          // Lightning receipts are kind 9735. PosterChan's Monero/BCH receipts are kind-1 tip notes;
          // query their t-tags explicitly so ordinary mentions cannot consume this history page.
          const older=await Relay.query([
            { '#p':[S.ME.pubkey], kinds:[9735], until, limit:100 },
            { '#p':[S.ME.pubkey], '#t':['monerotip','bchtip'], kinds:[1], until, limit:100 }
          ]);
          let floor=0;
          for(const e of older||[]){ if(!floor||e.created_at<floor) floor=e.created_at;
            if(e.pubkey!==S.ME.pubkey) Store.saveEvent(e); }
          if(floor) _notifZapUntil=floor;
          if(!(older&&older.length)) _notifZapDone=true;
          // A page can be entirely duplicate relay fan-out, not end-of-history; the timestamp cursor still
          // advances and the next press continues backward rather than latching history closed.
        }catch(_){}
      } else if(_notifShown >= all.length-5 && all.length){
        more.textContent='Loading older…'; more.disabled=true;
        const oldest=all[all.length-1].created_at;
        try{
          const older=await Relay.query([{ '#p':[S.ME.pubkey], _include_quotes:true, kinds:[1,6,7,9735,1111,1621,1617], until: oldest-1, limit:100 }]);
          older.forEach(e=>{ if(e.pubkey!==S.ME.pubkey) Store.saveEvent(e); });
        }catch(_){}
      }
      renderNotifications();
    };
  }
  // Collapse reactions/reposts on the SAME post into one row ("X and N others reacted"); everything
  // else stays an individual notification.
  function notifGrouped(list){
    const groups=new Map(); const out=[];
    for(const e of list){
      if(e.kind===7||e.kind===6){
        const tgt=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]||e.id;
        const key=e.kind+':'+tgt;
        let g=groups.get(key);
        if(!g){ g={type:'group', kind:e.kind, tgt, events:[], created_at:e.created_at}; groups.set(key,g); out.push(g); }
        g.events.push(e); if(e.created_at>g.created_at) g.created_at=e.created_at;
      } else out.push(e);
    }
    // Sort by the SAME key notifList() used — _notifTs, not raw created_at. This re-sort ran after it and
    // silently undid the whole pinning system: a kind-3's created_at is the follower's last contact-list
    // SAVE, so all ~31 followers who re-saved today floated straight back to the top of Notifications,
    // above real mentions, each row displaying its (correct, old) pinned time via _notifTs. That is the
    // follow spam — the badge fix never touched it, because the bug was downstream of the ordering.
    // Groups (reactions/reposts on one post) legitimately sort by their newest event's created_at.
    const ord = x => (x && x.type==='group') ? x.created_at : _notifTs(x);
    return out.sort((a,b)=>ord(b)-ord(a));
  }
  // Clean a notification's content for a compact one-line preview: turn npub/nprofile mentions into @name
  // (resolved from the profile, else a short @npub…) and collapse note/nevent/naddr refs to a 🔗 — so a
  // reply/mention preview doesn't show a wall of raw npubs. Resolve BEFORE the caller slices to 80 chars.
  function _notifPreview(content){
    let s=String(content||'');
    // Collapse note/nevent/naddr refs FIRST (on the raw content), so a bech32-like token that happens to
    // sit inside a resolved @display-name isn't later mangled into a stray 🔗.
    s=s.replace(/(?:nostr:)?(n(?:ote|event|addr)1[0-9a-z]{20,})/gi, '🔗');
    return s.replace(/(?:nostr:)?(n(?:pub|profile)1[0-9a-z]{20,})/gi, (m, ent)=>{
      try{ const d=NT().nip19.decode(ent); const pk=d.type==='npub'?d.data:(d.data&&d.data.pubkey);
        if(!pk) return '@…'; needProfile(pk); const p=Store.profile(pk); const nm=p&&(p.name||p.display_name);
        // Strip the mentioned user's own :shortcodes: — we can't render THEIR emoji here (applyEmojis only
        // has the notification event's tags), so leaving them would show as literal :code: text.
        return '@'+String(nm || (NT().nip19.npubEncode(pk).slice(0,10)+'…')).replace(_SHORTCODE_STRIP,'').replace(/\s+/g,' ').trim(); }catch(_){ return '@…'; }
    });
  }
  // Which of YOUR posts a notification is about. For a reply/mention it's the parent they answered; for a
  // reaction/repost/zap it's the referenced post. Follows and reports aren't about a post at all.
  function _notifCtxId(e){
    if(e.kind===3 || e.kind===1984) return '';
    if(e.kind===1 || e.kind===1111) return replyParentId(e)||'';
    return (e.tags.filter(t=>t[0]==='e').pop()||[])[1]||'';
  }
  // A one-line preview of that post, so "someone reacted ♥ to your post" says WHICH post. Renders from
  // cache; anything not cached is requested and patched in by patchLoaded when it lands.
  function _notifCtxHtml(id){
    const o=Store.get(id);
    if(!o) return `<div class="notif-ctx" data-nctx="${enc(id)}"><span class="muted small">…</span></div>`;
    // Reuse quotedDiv: the SAME embedded-post card used for quotes and the reply preview — author header,
    // full text and the real media gallery. Hand-rolling a trimmed version here is what made notifications
    // unreadable, and it drifts from the card everywhere else in the app.
    return `<div class="notif-ctx">${quotedDiv(o)}</div>`;
  }
  function _notifCtx(e){
    const id=_notifCtxId(e); if(!id) return '';
    if(!Store.get(id)) needEvent(id);
    return _notifCtxHtml(id);
  }
  // The actor's own words belong on their OWN line — inlined after "replied:" they ran together with
  // the name and verb into one long unreadable string, and the 80-char cut landed mid-sentence.
  function _notifSaid(e){
    const t=(_notifPreview(e.content) || '').trim();
    if(!t) return '';
    return `<div class="notif-said">${applyEmojis(enc(t.slice(0,220)), e)}${t.length>220?'…':''}</div>`;
  }
  function notifHtml(e){
    if(e.type==='reminder')return `<div class="notif reminder-notif" data-route="${e.route==='calendar'?'calendar':'notifications'}"><div class="notif-body"><b>${e.route==='calendar'?'Calendar reminder':'Reminder'}</b><div class="notif-said">${enc(e.content)}</div><span class="muted small">${enc(new Date(e.created_at*1000).toLocaleString())}</span></div></div>`;
    if(e.type==='group'){
      const first=e.events[0];
      // Group by PERSON, not by event: one user reacting twice (two emojis on the same post) was
      // counted as two actors and rendered "Alice and 1 other" — where the other one was Alice.
      const seen=new Set(), actors=[];
      for(const ev of e.events){ if(!seen.has(ev.pubkey)){ seen.add(ev.pubkey); actors.push(ev.pubkey); } }
      const rawName=(pk)=>{ const q=profOf(pk); return q.name||q.display_name||'someone'; };
      const linkName=(pk)=>`<span class="name" data-prof="${pk}">${emojiName(pk, rawName(pk))}</span>`;
      const verb = e.kind===6?'reposted your note':`reacted ${reactDisp(first)} to your post`;
      // Name everyone up to three; only collapse from the fourth on. Naming just one and saying
      // "and 1 other" hid a name to save no space — this way "and 1 other" can never appear, and
      // whoever is collapsed is still named in the tooltip.
      const named=actors.length<=3?actors:actors.slice(0,2), rest=actors.length-named.length;
      const parts=named.map(linkName);
      let who = rest>0 ? parts.join(', ')
              : parts.length>1 ? parts.slice(0,-1).join(', ')+' and '+parts[parts.length-1]
              : parts[0];
      if(rest>0){
        const restNames=actors.slice(named.length).map(pk=>rawName(pk)).join(', ');
        who += ` <span class="muted" title="${enc(restNames)}">and ${rest} other${rest>1?'s':''}</span>`;
      }
      // Stack up to three avatars so the row shows at a glance that several people acted.
      const avs = actors.slice(0,3).map(pk=>
        `<img class="notif-av" data-pk="${pk}" src="${enc(profOf(pk).picture||S.LOGO)}" title="${enc(rawName(pk))}" onerror="this.src='${S.LOGO}'">`).join('');
      const avWrap = actors.length>1 ? `<span class="notif-avs">${avs}</span>` : avs;
      return `<div class="notif ${e.kind===6?'rt':'like'}" data-open="${enc(e.tgt)}"><span class="ic">${e.kind===6?'↻':'♥'}</span>${avWrap}<div><b>${who}</b> ${verb}${_notifCtx(first)}<div class="muted small">${timeAgo(_notifTs(e))}</div></div></div>`;
    }
    const fromPk = e.kind===9735?(zapSender(e)||e.pubkey):e.pubkey;
    const p=profOf(fromPk); const av=p.picture||S.LOGO;
    // What to open on click: for a reply/mention (kind-1) or a chat reply (kind-42) open the
    // notification event ITSELF — for kind-1 the thread view centers their reply with your post above
    // it; for kind-42 the chat redirect scrolls to THEIR message (its last e-tag is the parent, i.e.
    // your message, which would be the wrong target). For a reaction/repost/zap, open the post they
    // acted on (the last referenced e-tag).
    const ref=(e.tags.filter(t=>t[0]==='e').pop()||[])[1]||'';
    // 1111 belongs in the first group: it IS their reply, so open it. It was falling through to
    // `ref` — the last lowercase `e` tag — which on a NIP-22 comment is the PARENT, so tapping a
    // community/thread mention landed on somebody else's older comment with nothing of theirs to
    // reply to. Exactly the trap the kind-42 note below describes.
    const tgt = (e.kind===1 || e.kind===1111 || e.kind===1621 || e.kind===1617) ? e.id : (ref||e.id);
    let cls,ic,txt;
    if(e.kind===9735){cls='zap';ic='⚡';txt=`zapped you <b>${fmtSats(zapAmount(e))} sats</b>`;}
    else if(e.kind===3){cls='follow';ic='🫂';txt='followed you';}
    else if(e.kind===1984){cls='report';ic='🚩';const tg=e.tags.find(t=>t[0]==='p'&&t[1]===S.ME.pubkey)||e.tags.find(t=>t[0]==='e');const ty=(tg&&tg[2])||(e.tags.find(t=>t[0]==='report')||[])[1]||'other';txt=`reported you <b>${enc(ty)}</b>${e.content?': '+enc(_notifPreview(e.content).slice(0,80)):''}`;}
    else if(e.kind===7){cls='like';ic='♥';txt=`reacted ${reactDisp(e)} to your post`;}
    else if(e.kind===6){cls='rt';ic='↻';txt='reposted your note';}
    else if(e.kind===1111){cls='reply';ic='💬';txt='replied'+_notifSaid(e);}
    // NIP-34 collaboration on a repo you own/maintain. The `subject` tag IS the title, so show it
    // instead of _notifSaid's content preview — an issue body's first line is rarely the headline.
    else if(e.kind===1621||e.kind===1617){cls='reply';ic=e.kind===1617?'🩹':'🐛';const _s=_repoTag(e,'subject');txt=(e.kind===1617?'sent a patch':'opened an issue')+(_s?': <b>'+enc(_s.slice(0,80))+'</b>':_notifSaid(e));}
    else if(_tipNote(e)){const _tn=_tipNote(e);cls='zap';ic=_tn.icon;txt=`tipped you${_tn.amt?` <b>${enc(_tn.amt)} ${enc(_tn.unit)}</b>`:''}`;}
    else if(_quotesMe(e)){cls='mention';ic='❝';txt='quoted your post'+_notifSaid(e);}
    else if(isReply(e)){cls='reply';ic='💬';txt='replied'+_notifSaid(e);}
    else {cls='mention';ic='@';txt='mentioned you'+_notifSaid(e);}
    // follows/reports have no thread → the row opens the sender's profile (data-prof); others open the post.
    const isProf = e.kind===3||e.kind===1984;
    return `<div class="notif ${cls}" ${isProf?`data-prof="${fromPk}"`:`data-open="${tgt}"`}><span class="ic">${ic}</span><img class="notif-av" data-pk="${fromPk}" src="${enc(av)}" onerror="this.src='${S.LOGO}'"><div><div class="notif-hd"><b class="name" data-prof="${fromPk}">${emojiName(fromPk,p.name||p.display_name||'anon')}</b> ${txt}</div>${_notifCtx(e)}<div class="muted small">${timeAgo(_notifTs(e))}</div></div></div>`;
  }


  return {
    _notifCtxHtml, _notifCtxId, _notifFreshEntry, _notifMatch, _updNotifHtml, markNotifsRead,
    notifGrouped, notifHtml, renderNotifications, renderNotificationsSoon,
  };
};

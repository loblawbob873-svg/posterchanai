/* Post cards — how an event becomes a card: the body renderer for every kind (notes, polls,
 * articles, communities, channels, webxdc), the reply and quote context, the action row and the
 * counts under it, media (the data-saver placeholders, the dimension cache, the carousel and the
 * video mount), custom emoji, and the batched fetches that fill a card in — missing events,
 * addressable events, reactions, reposts and zaps. Split out of app.js.
 *
 * It ships with the page rather than loading on demand: the timeline, Notifications, threads,
 * profiles, Discover, Mail and the lightbox all call `noteHtml`/`feedNoteHtml`/`mediaParts`/
 * `buildCounts` SYNCHRONOUSLY while they build a screen, so waiting for a fetch at the point of
 * call is not an option. app.js keeps one entry point per name and builds the factory the first
 * time one is called.
 *
 * The code below is BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets. Stayed in app.js: the four `document` listeners this code needs bound AT
 * BOOT (`load`/`loadedmetadata`, which teach the dimension cache, and the media carousel's
 * scroll/click), and the small consts the rest of app.js reads as VALUES rather than calls —
 * BLOBF, _DIM_GUESS, _isMediaUrl, _SHORTCODE_STRIP, eTagRelays, the ghost counters and the
 * addressable-event cache.
 */
window.PCCardsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.BLUR_NSFW, S.BOOKMARKS, S.LOGO, S.ME, S.NO_IMAGES, S.PINNED, S._adT, S._filesGridList, S._navPushed, S._routing
  const {
    $, $$, BLOBF, Drafts, NT, QUOTE_ICON, REACT_ICON, REPLY_ICON, RT_ICON, ZAP_ICON, _DIM_GUESS,
    _adCache, _adQ, _advertises, _animOff, _celebrateOf, _filesSel, _filesSelBar, _ghosts,
    _isMediaUrl, _isProofSig, _isTxid, _lbGroup, _noteNode, _notePaymentXmr, _notifCtxHtml,
    _postEffectsOn, _repostAction, _repostDeleted, _tipNote, _webLink, articleCard, bchOf, compose,
    copyValue, decorateProfiles, doBlock, doDelete, doRepost, doTip, doXmrTip, doZap, enc, fmtSats,
    isBchAddr, isReply, isSensitive, isXmrAddr, linkCardHtml, linkify, needProfile, niceNip05,
    npubOf, observeCelebrations, openArticle, openLightbox, openPostMenu, openRepo, openStream,
    openThread, pickEmoji, profOf, publish, renderHashtag, renderProfileView, renderThread,
    renderView, safePk, timeAgo, toast, toggleBookmark, togglePin, translatePost, uiConfirm,
    uiPrompt, webxdcCardHtml, webxdcFileCard, xmrForNote, xmrTipBadge, zapAmount,
  } = dep;
  /* EVERY CARD KIND IS CAUGHT HERE, not just kind 1 — and that is what stops one bad event taking a
   * whole SCREEN with it.
   *
   * noteCard() has had its own try/catch for a long time, so a malformed kind-1 costs exactly one
   * card. Nothing else did: a poll with no `option` tags, an article whose content is not what
   * articleCard expects, a channel whose kind-40 content is not JSON — each of those threw straight
   * out of here, and out of whatever was building the list.
   *
   * On a PROFILE that is the whole view. `listFor('notes')` maps over the author's events and its
   * result is assigned to #prof-list, so a throw means the list is never filled AND every binding
   * after it — the tabs, ⋯, "Copy npub", the follow stats — is never made. The report reads as three
   * unrelated bugs ("no posts", "the hamburger menu isn't showing", "copying the npub does nothing")
   * with no error on screen, because the header above it rendered perfectly.
   *
   * The timeline has the same shape through _noteNode, and so does search, the thread view and the
   * notification rail. One guard here covers all of them. */
  function noteHtml(ev){
    try{ return _noteHtml(ev); }
    catch(e){
      if((_noteCardErrs=(_noteCardErrs||0)+1) <= 3) try{ console.error('[noteHtml] kind', ev&&ev.kind, (ev&&ev.id)||'?', e); }catch(_){}
      const why = (e && (e.message || e.name)) ? String(e.message || e.name).slice(0,140) : '';
      return `<article class="note" data-id="${(ev&&ev.id)||''}" data-pk="${(ev&&ev.pubkey)||''}"><div class="body"><div class="txt muted small">⚠ couldn't render this post${why?' — '+enc(why):''}</div></div></article>`;
    }
  }
  /* THE ONE REPOST HEADER — built here so the three places that draw one cannot drift apart.
   *
   * TWO REPORTS, one line of markup. **"someone reposted"** was on EVERY repost in a measured feed
   * (4 of 4): the name was baked in as escaped text, so when the reposter's kind-0 finally arrived
   * `decorateProfiles` had nothing to patch — it fills `.name[data-prof]`, and this had neither.
   * The placeholder is still 'someone', but it is a placeholder now rather than a verdict.
   *
   * And **the card showed only the original's timestamp** — "when a repost is displayed, only the
   * time of the original post is shown, but that AND when it was reposted [should be]". A repost is
   * two events with two times, and the one that decides where it sits in your feed is the repost's.
   * The original's stays on the card below, where it belongs to the post it describes. */
  function _repostTag(pk, ts){
    const nm = enc((profOf(pk).name) || 'someone');
    const when = ts ? `<span class="rt-when" title="${enc(new Date(ts*1000).toLocaleString())}">${enc(timeAgo(ts))}</span>` : '';
    return `<div class="repost-tag">${RT_ICON} <span class="name" data-prof="${enc(pk)}">${nm}</span> reposted${when}</div>`;
  }
  function _noteHtml(ev){
    if(_repostDeleted(ev))return '';
    if (ev.kind===6){  // repost
      let inner=null; try{ inner=JSON.parse(ev.content); }catch(_){}
      if(inner && inner.id) Store.saveEvent(inner);
      const origId=(ev.tags.find(t=>t[0]==='e')||[])[1];
      const orig = inner || Store.get(origId);
      needProfile(ev.pubkey);
      if(orig){ needProfile(orig.pubkey); return noteCard(orig, _repostTag(ev.pubkey, ev.created_at)).replace('<article ',`<article data-repost-id="${enc(ev.id)}" `); }
      needEvent(origId);   // fetch the original; flushEvents patches this placeholder in place
      // The reposter's pubkey and the repost's time ride on the placeholder, because patchLoaded
      // rebuilds the header from it and has no other way back to the kind-6.
      return `<article data-repost-id="${enc(ev.id)}" class="note" data-orig="${enc(origId||'')}" data-rtpk="${enc(ev.pubkey)}" data-rtts="${ev.created_at}"><div class="body">${_repostTag(ev.pubkey, ev.created_at)}<div class="muted small">loading post…</div></div></article>`;
    }
    /* A mini app posted as NIP-94 file metadata — which is how Ditto publishes them and how the
     * Half-Life port is published. Without this the client has NO renderer for kind 1063 at all: the
     * post opens as a bare thread with the app nowhere in it ("half life loads a social reply").
     * Only when it really carries an app; a 1063 for an ordinary file is left to the generic path. */
    if (ev.kind===1063 && webxdcCardHtml(ev)) return webxdcFileCard(ev);
    if (ev.kind===1068) return pollCard(ev);   // NIP-88 poll
    if (ev.kind===30023) return articleCard(ev);   // NIP-23 long-form article → reader card
    return noteCard(ev);
  }
  // Timeline renderer: the Home/Global feeds show replies (like Nostr/fediverse), NOT just top-level
  // posts. A reply renders WITH its "↩ replying to" parent context so it reads in-context instead of as
  // an orphaned card. Thread + profile views render their own context, so they keep calling noteHtml.
  /* THE CARD IS BUILT FIRST, and the label is only ever wrapped around one that exists.
   *
   * "↩ REPLYING TO alice" with nothing under it, stacked down the whole screen, is the ghost timeline
   * people report from the Android app. The label is built from the reply's OWN tags, so it renders
   * whatever else fails — which is what makes the failure look like the feed being empty rather than
   * like an error. Reversing the order means the label cannot outlive its post: no card, no label,
   * and the missing post is then an ordinary missing row that the next draw fills in.
   *
   * noteCard() catches its own errors and always answers with a card, so `!card` is not a case that
   * should ever be reachable — which is exactly why it is worth handling here rather than assuming. */
  function feedNoteHtml(ev){
    const card = noteHtml(ev) || '';   // noteHtml catches its own renderers — see its guard
    if(!card) return '';
    return isReply(ev)
      ? `<div class="reply-pair">${replyContextHtml(ev)}${card}</div>`
      : card;
  }
  /* `measure` is off on the live-prepend path. Reading offsetHeight forces a synchronous layout, and
   * a busy feed prepends several times a minute on the very device this is written for — so the
   * cheap half (is the <article> THERE?) runs every time, and the half that costs a layout runs on a
   * full draw, which has just rebuilt the list anyway. */
  function _healGhostPairs(box, measure){
    if(!box || !box.querySelectorAll || !box.isConnected) return 0;
    /* THE THIRD GHOST, and the one that produced this screen for real: a card that is in the DOM, at
     * its full height, and TRANSPARENT. Neither probe below can see it — it has an <article> and it
     * has height — which is why the two of them shipped, were verified against a planted ghost, and
     * changed nothing on the phone.
     *
     * There is exactly one thing in this client that can make a whole feed transparent at once, and
     * it is not a property of any post: `body.anim-off` left on while the page is being looked at.
     * So the check is that state, not a per-card style read — O(1), no layout, and it works on a
     * timeline with no replies on it at all, which the per-pair loop below cannot do. Both real
     * causes are fixed (client.css no longer freezes an entry animation; _animOff is driven by the
     * native resume signal too), so reaching this is a signal gap nobody has found yet — hence the
     * count, so the next report names it instead of restarting this hunt. */
    if(!document.hidden && document.body.classList.contains('anim-off')){
      _animOff(false); _ghosts.frozen++; _ghosts.at = Date.now();
      try{ console.warn('[timeline] cleared a stale anim-off on a visible page — see _animOff'); }catch(_){}
    }
    const pairs = box.querySelectorAll('.reply-pair');
    if(!pairs.length) return 0;
    let n = 0;
    for(const p of pairs){
      const card = p.querySelector('article.note');
      if(!card){
        // data-key IS the event id for a reply (only kind 6 keys on something else, and a repost is
        // never a reply-pair), so the post can be rebuilt from the cache it was drawn from.
        const ev = Store.get(p.dataset.key || '');
        const node = ev ? _noteNode(ev) : null;
        if(node){ p.replaceWith(node); _ghosts.missing++; }
        else { p.remove(); _ghosts.dropped++; if(p.dataset.key) needEvent(p.dataset.key); }
        n++; continue;
      }
      /* Zero-height is only meaningful once the pair itself has been laid out — a feed that is
       * display:none (the view moved on) measures every child at zero and would "heal" the lot. */
      if(measure && p.offsetHeight > 0 && card.offsetHeight === 0){
        const was = card.style.display;
        card.style.display = 'none'; void card.offsetHeight; card.style.display = was || '';
        _ghosts.blank++; n++;
      }
    }
    if(n){
      _ghosts.at = Date.now();
      try{ console.warn('[timeline] recovered', n, 'ghost reply card(s)', JSON.stringify(_ghosts)); }catch(_){}
    }
    return n;
  }
  // ---------- NIP-88 polls: kind-1068 poll, kind-1018 responses ----------
  const _myPollVotes = {};   // pollId -> Set(optionId)
  function pollCard(ev){
    const p=profOf(ev.pubkey); needProfile(ev.pubkey);
    const name=p.name||p.display_name||(npubOf(ev.pubkey).slice(0,12)+'…');
    const handle=niceNip05(p.nip05)||('@'+npubOf(ev.pubkey).slice(4,12));
    const opts=ev.tags.filter(t=>t[0]==='option'&&t[1]).map(t=>({id:t[1],label:t[2]||t[1]}));
    const multi=((ev.tags.find(t=>t[0]==='polltype')||[])[1]==='multiplechoice');
    const endsAt=parseInt((ev.tags.find(t=>t[0]==='endsAt')||[])[1]||'0',10);
    const ended=endsAt && endsAt<Math.floor(Date.now()/1000);
    const optHtml=opts.map(o=>`<button class="poll-opt" data-poll="${ev.id}" data-opt="${enc(o.id)}"${ended?' disabled':''}><span class="poll-bar"></span><span class="poll-label">${enc(o.label)}</span><span class="poll-pct"></span></button>`).join('');
    return `<article class="note poll" data-id="${ev.id}" data-pk="${ev.pubkey}">
      <img class="av" src="${enc(p.picture||S.LOGO)}" onerror="this.src='${S.LOGO}'">
      <div class="body">
        <div class="hd"><span class="name" data-prof="${ev.pubkey}">${emojiName(ev.pubkey,name)}</span><span class="vchk"></span>
          <span class="handle">${enc(handle)}</span><span class="time">${timeAgo(ev.created_at)}</span></div>
        <div class="poll-q">📊 ${linkify(ev.content||'')}</div>
        <div class="poll-opts">${optHtml}</div>
        <div class="poll-foot muted small">${multi?'Multiple choice':'Single choice'}${ended?' · ended':''} · <span class="poll-total">…</span></div>
        <div class="acts"><button class="act" data-a="reply" title="reply">${REPLY_ICON} <span class="n"></span></button>
          <button class="act actm" data-a="menu" title="more"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button></div>
      </div></article>`;
  }
  async function hydratePolls(scope){
    for(const card of $$('.note.poll:not([data-poll-done])', scope||document)){
      card.setAttribute('data-poll-done','1');
      const pid=card.dataset.id;
      // Data saver: skip the per-poll vote tally (a kinds:[1018] '#e' query, limit 1000 — one of the
      // heaviest per-card fetches). Show the poll + let the user vote; results load when data saver is off.
      if(S.NO_IMAGES){ const tot=card.querySelector('.poll-total'); if(tot) tot.textContent='poll results hidden · data saver'; continue; }
      let votes=[]; try{ votes=await Relay.query([{ kinds:[1018], '#e':[pid], limit:1000 }]); }catch(_){}
      const latest=new Map();
      for(const v of votes.sort((a,b)=>a.created_at-b.created_at)) latest.set(v.pubkey, v);
      const counts={}; let total=0; const mine=_myPollVotes[pid]||new Set();
      for(const v of latest.values()){
        const chosen=[...new Set(v.tags.filter(t=>t[0]==='response').map(t=>t[1]))];
        if(v.pubkey===S.ME.pubkey) chosen.forEach(o=>mine.add(o));
        chosen.forEach(o=>{ counts[o]=(counts[o]||0)+1; total++; });
      }
      _myPollVotes[pid]=mine;
      card.querySelectorAll('.poll-opt').forEach(b=>{
        const c=counts[b.dataset.opt]||0, pct=total?Math.round(c*100/total):0;
        const bar=b.querySelector('.poll-bar'); if(bar) bar.style.width=pct+'%';
        const pc=b.querySelector('.poll-pct'); if(pc) pc.textContent=pct+'% ('+c+')';
        b.classList.toggle('voted', mine.has(b.dataset.opt));
      });
      const tot=card.querySelector('.poll-total'); if(tot) tot.textContent=total+' vote'+(total===1?'':'s');
    }
  }
  // A vote IN FLIGHT is a vote. The "already voted" guard is only written after the relay answers,
  // so every click while the first publish was pending signed another identical 1018 - measured on
  // server1: 8 identical responses from one account in 10s, 4 in 4s from another.
  const _pollVoting = new Set();
  async function votePoll(pollId, optId){
    if((_myPollVotes[pollId]||new Set()).has(optId)){ toast('already voted'); return; }
    const key=pollId+'\n'+optId;
    if(_pollVoting.has(key)) return;
    _pollVoting.add(key);
    try{ await _votePollNow(pollId, optId); }finally{ _pollVoting.delete(key); }
  }
  async function _votePollNow(pollId, optId){
    try{
      const r=await publish(1018, '', [['e', pollId], ['response', optId]]);   // failure toast by publish()
      if(!(r && r.ok)) return;   // relay didn't store the vote → don't mark it voted (else the guard blocks a retry)
      (_myPollVotes[pollId]=_myPollVotes[pollId]||new Set()).add(optId);
      toast('✓ voted');
      const card=$(`.note.poll[data-id="${pollId}"]`); if(card){ card.removeAttribute('data-poll-done'); hydratePolls(card.parentNode||document); }
    }catch(e){ toast('vote failed'); }
  }
  // ---- Media dimensions: the feed's layout stability ---------------------------------------------
  // Every <img>/<video> in a note carries a SIZE HINT so the browser can reserve its box before a byte
  // arrives. Without one, a media post is 2px tall until it decodes and then snaps to its cap (300px in
  // .media-row, 420 in a carousel, 720 for a lone image), shoving everything below it down — the "jumpy
  // mess". It is worst on a phone, where the short viewport means nearly every image is loading="lazy"
  // and so pops in one after another as you scroll.
  //
  // The hint is the aspect RATIO and the natural WIDTH, as inline custom properties (--arn / --nw), which
  // client.css turns into `width: min(100%, natural, cap x ratio)` — see the layout-stable media block
  // there for why that particular expression, and _dimAttrs below for why width/height ATTRIBUTES (the
  // obvious answer) reserve nothing at all.
  //
  // Three sources, best first:
  //   1. NIP-92 `imeta dim=WxH` on the note (this app writes it — see imetaTagsFor — and so do most
  //      clients that upload via Blossom/nip96).
  //   2. A measurement of any image we have ALREADY loaded once, persisted per-device.
  //   3. Nothing → a provisional 16:10 box, corrected the moment it loads (and cached, so the second
  //      sighting is exact). One settle on first sight beats a pop on every sight.
  const MediaDims = (()=>{
    const K='pc-mdim-v1', MAX=2400;
    let map=new Map(), dirty=false;
    try{ const raw=localStorage.getItem(K); if(raw) map=new Map(Object.entries(JSON.parse(raw))); }catch(_){}
    // Persist lazily: a busy feed measures dozens of images in a burst, and a localStorage write per
    // image is a synchronous main-thread stall on exactly the frames we are trying to keep smooth.
    let flushT=null;
    function flush(){
      flushT=null; if(!dirty) return; dirty=false;
      try{
        // Bounded, newest-kept: Map preserves insertion order, and `set` on an existing key does NOT
        // move it, so re-seeing an old image doesn't refresh it. Good enough — this is a size hint
        // cache, and dropping one costs a single first-sight settle.
        if(map.size>MAX) map=new Map([...map].slice(-MAX));
        localStorage.setItem(K, JSON.stringify(Object.fromEntries(map)));
      }catch(_){}   // quota / private mode: the in-memory map still works for this session
    }
    function keyOf(url){
      // Strip the query/fragment: the same blob is served with #t=0.1 appended for video posters and
      // with cache-busting queries, and all of those share one intrinsic size.
      const u=String(url||''); const i=u.search(/[?#]/); return i<0?u:u.slice(0,i);
    }
    // Both ends validate. `set` is fed by imeta tags off untrusted notes, and `get` reads back JSON that
    // some earlier version (or a poisoned localStorage) wrote — and both numbers land in an HTML attribute
    // and a CSS value, so a string that isn't a number has no business getting that far. 20000 is well
    // past any real photo and keeps the ratio out of the range where toFixed(6) rounds it to 0.
    const sane=(n)=>Number.isFinite(n) && n>0 && n<=20000;
    return {
      get(url){
        const d=map.get(keyOf(url));
        return (Array.isArray(d) && sane(+d[0]) && sane(+d[1])) ? [+d[0], +d[1]] : null;
      },
      set(url, w, h){
        w=+w; h=+h;
        if(!sane(w) || !sane(h)) return;
        const k=keyOf(url); if(!k || map.get(k)) return;
        if(/^blob:/i.test(k)) return;   // session-scoped object URL — a key that is dead before it is read back
        map.set(k, [w,h]); dirty=true;
        if(!flushT) flushT=setTimeout(flush, 2000);
      },
      // NIP-92 imeta → {url: [w,h]} for one event. Tags are ["imeta","url <u>","dim WxH",…].
      seed(ev){
        for(const t of ((ev&&ev.tags)||[])){
          if(t[0]!=='imeta') continue;
          let u='', d='';
          for(const part of t.slice(1)){
            const s=String(part||'');
            if(s.startsWith('url ')) u=s.slice(4).trim();
            else if(s.startsWith('dim ')) d=s.slice(4).trim();
          }
          const m=/^(\d+)x(\d+)$/.exec(d);
          if(u && m) this.set(u, +m[1], +m[2]);
        }
      },
    };
  })();
                                  // first-sight correction is as small as it can be in either direction.
  function _dimAttrs(url){
    const d=MediaDims.get(url), g=!d, [w,h]=d||_DIM_GUESS;
    return ` width="${w}" height="${h}" style="--arn:${(w/h).toFixed(6)};--nw:${w}"${g?' data-dim="guess"':''}`;
  }
  // Rewrite a provisional hint from the real decoded size, once, and remember it for next time.
  //
  // Gated on data-dim="guess", which is narrower than it looks and deliberately so. The listener below is
  // document-wide, so it also sees every avatar, every inline custom emoji and every UI icon — learning
  // from those would fill the cache with 48x48 entries nothing ever reserves and schedule a localStorage
  // write for each. "guess" is exactly the set that (a) needs correcting and (b) is worth remembering:
  // media we sized from imeta is already right, and media with no hint at all is on the fallback path.
  function _dimLearn(el, w, h){
    if(!(w>0&&h>0)) return;
    if(!el.dataset || el.dataset.dim!=='guess') return;
    delete el.dataset.dim;
    // data-dimkey: what to REMEMBER this measurement under, when that isn't the src. An encrypted DM
    // attachment is played from a blob: URL minted this session — remembering the size under that key
    // teaches us nothing and fills the cache with dead entries — so those elements carry the stable
    // encrypted-reference URL instead, and the next time that attachment is opened its box is right
    // on the first paint.
    MediaDims.set(el.dataset.dimkey || el.currentSrc || el.src, w, h);
    el.setAttribute('width', w); el.setAttribute('height', h);
    el.style.setProperty('--arn', (w/h).toFixed(6));
    el.style.setProperty('--nw', String(w));
  }
  function _phold(encUrl, kind, cls, onerr){   // the placeholder itself
    const v = kind==='video';
    return `<span class="img-hold${v?' vid-hold':''}" data-src="${encUrl}" data-kind="${v?'video':'image'}"${cls?` data-cls="${cls}"`:''}${onerr?` data-onerr="${enc(onerr)}"`:''} role="button" tabindex="0">${v?'▶️ tap to load video':'🖼️ tap to load image'}</span>`;
  }
  // Constructed media (feed / inline text): placeholder in data saver, else a real <img>/<video>.
  // `encUrl` is ALREADY html-encoded; `cls` = optional layout class ("m"); `onerr` = optional onerror.
  function _media(encUrl, kind, cls, onerr){
    if(S.NO_IMAGES) return _phold(encUrl, kind, cls, onerr);
    const c = cls?` class="${cls}"`:''; const oe = onerr?` onerror="${onerr}"`:'';
    // preload="none" fetched NOTHING, so a timeline video was a blank grey box until you pressed play
    // ("all the videos are missing a preview"). "metadata" pulls just the header — not the media — and
    // the #t=0.1 fragment makes the browser seek to that frame and paint it, which is what actually
    // produces the poster (metadata alone still renders black in several browsers). Skipped when the URL
    // already carries a fragment, so we never rewrite someone else's. Data saver never gets here (_phold).
    // width/height reserve the box before the bytes land — see MediaDims. `decoding="async"` keeps a
    // big image's decode off the main thread so it can't drop a frame mid-scroll on a phone.
    const dim = _dimAttrs(encUrl);
    // Videos carry their URL in data-vsrc and are MOUNTED LAZILY by VideoMount (below) — see the note
    // there for why a <video src> that is merely present, not playing, is expensive enough to kill the
    // Android WebView. The poster behaviour above is unchanged; it just happens on mount instead of on
    // parse, so only the videos actually on screen ever hold a media player.
    if(kind==='video') return `<video${c} data-vsrc="${encUrl}"${dim} controls preload="none" playsinline></video>`;
    return `<img${c} src="${encUrl}"${dim} loading="lazy" decoding="async"${oe}>`;
  }
  // ---- VideoMount: only the videos you can SEE hold a media player -------------------------------
  // A <video> with a src is not markup, it is a live decoder: the WebView allocates a MediaCodec/media
  // player per element the moment it starts pulling metadata, and Android's pool of those is small and
  // process-wide. Every surface that renders notes puts an unbounded number of them on screen at once —
  // and Notifications is the worst of them, because each row embeds the FULL referenced post (quotedDiv,
  // media gallery and all), the right rail renders the same rows AGAIN, and renderNotifications is a
  // whole-innerHTML rebuild that fired on every arriving event. Each rebuild threw away every <video>
  // mid-fetch and built a new one, which is exactly the report: three videos "reloading over and over,
  // never showing a preview", ending in "PosterChan hit a display error" — MainActivity's
  // onRenderProcessGone toast, i.e. the render process had died outright.
  //
  // So a video's src is attached when it comes near the viewport and RELEASED when it leaves (or when the
  // node is dropped from the DOM by a re-render) — removeAttribute('src') + load() is what actually hands
  // the decoder back; letting a detached node fall out of scope leaves the player alive until GC, which is
  // far too late during a rebuild burst. A video the user is actually PLAYING is never unmounted, and one
  // that is remounted resumes where it was.
  const VideoMount = (function(){
    // The viewport is the real limit — this is only a backstop against a pathological page, so it is set
    // above anything that genuinely fits on screen at once (a tablet showing the Notifications view AND
    // the rail is the densest case). Too low and a video you CAN see would sit blank.
    const MAX_MOUNTED = 8;
    /* How long a video that has not managed to paint its first frame yet is left alone after it
     * scrolls away. This is the whole of the "no video previews over Tor" bug.
     *
     * The poster frame is a NETWORK FETCH: metadata (the moov atom, which for a non-faststart MP4 is
     * at the far end of the file) and then a seek. Measured on real timeline videos, direct vs through
     * a tor SOCKS proxy in headless Chrome: 319ms → 2347ms, 356ms → 5135ms, 1099ms → 38569ms. Nothing
     * is wrong with the video or with tor; every round trip simply costs 10-30x more.
     *
     * Aborting that fetch keeps NOTHING. Measured: unmount at 1.5s leaves readyState back at 0, and
     * the remount pays the full 5367ms again — the cancelled bytes are not resumed and not reused.
     * So on a feed you are scrolling, an unmount-on-leave beats the fetch every time and no video EVER
     * paints: permanent black boxes, on exactly the connection where the user can least tell why.
     * Direct, the fetch wins in 300ms and nobody ever saw this.
     *
     * A load in flight is therefore not idle — it is the preview arriving — and it is left alone for a
     * while. The CAP is unaffected (see mount(): a cap eviction forces, because a hard resource limit
     * is not a policy) and so is DOM removal, so the decoder pressure this whole module exists to
     * control is unchanged: never more than MAX_MOUNTED, ever. */
    const FIRST_FRAME_GRACE = 20000;
    const mounted = new Set();
    const visible = new WeakSet();          // last known intersection state, for the grace timer
    const _url = el => el.dataset.vsrc || '';
    const _dist = el => { const r=el.getBoundingClientRect(); const c=(innerHeight||800)/2; return Math.abs(r.top+r.height/2-c); };
    const _painted = el => el.readyState >= 2;               // HAVE_CURRENT_DATA — a frame exists
    /* ONE VIDEO AT A TIME, AND IT STOPS WHEN YOU SCROLL PAST IT.
     *
     * Two reports, one cause: "if I'm scrolling and two posts have videos, they both play audio" and
     * "if I scroll past the video, the audio should stop". Nothing ever paused a second video when a
     * first was playing, and `unmount` deliberately refused to touch a playing one — the comment on
     * that line said "audio keeps going off-screen", which was a choice and is the bug.
     *
     * `play` does NOT bubble, so this listens in the CAPTURE phase on the document: that reaches
     * every video on every surface (timeline, thread, profile, search, notifications, community,
     * bookmarks…) including ones this module never mounted, without a dozen render paths having to
     * remember to opt in. Same reasoning as `add()` watching the whole document.
     *
     * FULLSCREEN AND PICTURE-IN-PICTURE ARE EXEMPT FROM THE SCROLL RULE, and that exemption is not
     * cosmetic: a fullscreen video's element is still laid out where it was in the feed, so the
     * observer reports it as off-screen the moment anything scrolls underneath — and pausing what
     * somebody is watching full-screen because a background list moved would be a far worse bug than
     * the one being fixed. */
    const _playing = el => el && !el.paused && !el.ended;
    const _exempt = el => {
      try{ if(document.pictureInPictureElement === el) return true; }catch(_){}
      try{ if(document.fullscreenElement && document.fullscreenElement.contains(el)) return true; }catch(_){}
      try{ if(el.webkitDisplayingFullscreen) return true; }catch(_){}   // iOS has its own idea
      return false;
    };
    document.addEventListener('play', (e) => {
      const el = e.target;
      if(!el || el.tagName !== 'VIDEO') return;
      document.querySelectorAll('video').forEach(v => {
        if(v !== el && _playing(v)) { try{ v.pause(); }catch(_){} }
      });
    }, true);

    function unmount(el, force){
      if(!el.dataset.vmount) return;
      /* A PLAYING VIDEO IS PAUSED FIRST, then freed like any other. It used to be left alone
       * entirely, which is what kept audio running from a post scrolled far off the screen. */
      if(_playing(el)){
        if(_exempt(el)) return;
        try{ el.pause(); }catch(_){}
      }
      if(!force && el.isConnected && !_painted(el)){
        const waited = Date.now() - (+el.dataset.vmt || 0);
        if(waited < FIRST_FRAME_GRACE){
          // Come back when the grace is up: the observer will not fire again for something that has
          // already left the viewport, so without this the element would stay mounted indefinitely.
          clearTimeout(el._vgrace);
          el._vgrace = setTimeout(()=>{ if(!visible.has(el)) unmount(el, true); },
                                  FIRST_FRAME_GRACE - waited);
          return;
        }
      }
      clearTimeout(el._vgrace);
      delete el.dataset.vmount; delete el.dataset.vmt; mounted.delete(el);
      try{ if(el.currentTime>0.2) el.dataset.vpos=String(el.currentTime); }catch(_){}
      try{ el.pause(); }catch(_){}
      el.removeAttribute('src'); el.preload='none';
      try{ el.load(); }catch(_){}                                 // THE line that frees the decoder
    }
    function mount(el){
      if(el.dataset.vmount || !_url(el)) return;
      // Over the cap: free the furthest-away mounted video that is willing to go (a PLAYING one refuses,
      // so walk the list rather than giving up on the first refusal). Nothing to free and everything on
      // screen is closer than we are → stay unmounted; the observer will call again when that changes.
      if(mounted.size>=MAX_MOUNTED){
        for(const far of [...mounted].sort((a,b)=>_dist(b)-_dist(a))){
          if(_dist(far)<=_dist(el)) break;
          // FORCED: the cap is a hard limit on live decoders, not a preference. Letting a still-loading
          // video refuse here would starve the one the user is actually looking at.
          unmount(far, true); if(mounted.size<MAX_MOUNTED) break;
        }
        if(mounted.size>=MAX_MOUNTED) return;
      }
      el.dataset.vmount='1'; el.dataset.vmt=String(Date.now()); mounted.add(el);
      el.preload='metadata';
      const u=_url(el), pos=+(el.dataset.vpos||0);
      // #t= seeks the browser to a frame and PAINTS it — metadata alone still renders black in several
      // browsers, which is the "all the videos are missing a preview" bug. Resume point when we have one.
      el.src = u.includes('#') ? u : u + '#t=' + (pos>0.2 ? pos.toFixed(2) : '0.1');
    }
    const io = ('IntersectionObserver' in window) ? new IntersectionObserver(ents=>{
      for(const e of ents){
        if(e.isIntersecting){ visible.add(e.target); mount(e.target); }
        else { visible.delete(e.target); unmount(e.target); }
      }
    }, { rootMargin:'300px 0px' }) : null;
    // No IntersectionObserver (nothing current, but the whole feature hangs off it) → mount on sight, which
    // is precisely the old behaviour. Degrading to "no video ever gets a src" would be the worse bug.
    const watch = el => io ? io.observe(el) : mount(el);
    // Watch the WHOLE document rather than asking each render path to opt in. There are a dozen surfaces
    // that emit note media (timeline, thread, profile, search, notifications view AND rail, community,
    // bookmarks, …) and a new one that forgot the call would silently reintroduce this.
    function add(n){
      if(!n || n.nodeType!==1) return;
      if(n.tagName==='VIDEO'){ if(n.dataset.vsrc) watch(n); return; }
      if(n.querySelectorAll) n.querySelectorAll('video[data-vsrc]').forEach(watch);
    }
    function drop(n){
      if(!n || n.nodeType!==1) return;
      if(n.tagName==='VIDEO'){ if(n.dataset.vsrc) _release(n); return; }
      if(n.querySelectorAll) n.querySelectorAll('video[data-vsrc]').forEach(v=>_release(v));
    }
    // Detached by a re-render → free the decoder rather than waiting for GC. Deferred by a turn and
    // re-checked, because a MOVE (insertBefore of a node already in the tree — what the timeline's
    // reconcile does) is reported as a remove followed by an add, and killing those would restart every
    // visible video on every reconcile: the churn this exists to stop.
    function _release(el){
      setTimeout(()=>{
        if(el.isConnected) return;                                // it was a move, not a removal
        if(io) io.unobserve(el);
        // A pending first-frame grace dies with the node — it is keyed on the element still being in
        // the document, and a timer that outlives it would fire against a detached video.
        clearTimeout(el._vgrace); visible.delete(el);
        mounted.delete(el); delete el.dataset.vmount; delete el.dataset.vmt;
        try{ el.pause(); }catch(_){}
        el.removeAttribute('src'); try{ el.load(); }catch(_){}
      }, 0);
    }
    new MutationObserver(muts=>{
      for(const m of muts){ for(const n of m.addedNodes) add(n); for(const n of m.removedNodes) drop(n); }
    }).observe(document.documentElement, { childList:true, subtree:true });
    add(document.body || document.documentElement);   // anything already parsed in (the observer only sees later inserts)
    // A video the user pressed play on must survive scrolling away, and must not be evicted to make room
    // for one scrolling in.
    document.addEventListener('play', e=>{ const el=e.target; if(el && el.tagName==='VIDEO' && el.dataset.vsrc) mounted.add(el); }, true);
    return { url:_url };
  })();
  // Wrap an ALREADY-built content <img>/<video> (article / gallery / marketplace / stream / link cards):
  // placeholder in data saver, else the original html untouched. `cls` = layout class to restore on tap.
  // Carry the ORIGINAL element's onerror into the placeholder. Dropping it meant a tapped image lost
  // the recovery/cleanup its non-data-saver twin gets — __blobFallback's authenticated fetch→blob (the
  // APK's cross-origin /api images), __aiMediaRetry's blossom-not-ready retry, and the plain
  // hide-the-broken-thumb handlers. The whole point of the placeholder is to defer the download, not
  // to render a DIFFERENT element once you ask for it.
  function _holdOnerr(html){ const m=/\sonerror="([^"]*)"/i.exec(html||''); return m ? m[1] : ''; }
  function _hold(html, url, kind, cls){ return S.NO_IMAGES ? _phold(enc(url), kind, cls, _holdOnerr(html)) : html; }
  // `ev`, when given, is the note the text came from: its NIP-92 imeta tags carry each attachment's real
  // dimensions, so seeding them HERE — before any _media() call below — is what lets the very first paint
  // reserve the true box instead of a guess. Callers that only want `.text` can keep omitting it.
  function mediaParts(raw, ev){
    if(ev) MediaDims.seed(ev);
    const media=[];
    // Media is LIFTED OUT of the text and rendered as its own row, so whatever text remains would always
    // sit above it — a card post ("<image>\n\n<link>") showed its link ABOVE the picture. When the content
    // LEADS with media, that ordering is backwards: honour the author's order and put the row first.
    const _lead=(raw||'').trim().match(/^(https?:\/\/[^\s<]+)/);
    const mediaFirst=!!(_lead && _isMediaUrl(_lead[1].replace(/[)\].,!?]+$/,'')));
    const text=(raw||'').replace(/(https?:\/\/[^\s<]+)/g,(url)=>{
      const u=url.replace(/[)\].,!?]+$/,''); const tail=url.slice(u.length); const E=enc(u);
      if(/\.(jpe?g|png|gif|webp|avif)(\?|#|$)/i.test(u)){ media.push(_media(E)); return tail; }
      if(/\.(mp4|webm|mov|m4v)(\?|#|$)/i.test(u)){ media.push(_media(E,'video')); return tail; }
      if(/\/[0-9a-f]{64}(\?|#|$)/i.test(u)){ media.push(_media(E, null, null, BLOBF)); return tail; }
      return url;  // non-media URL: leave for linkify
    });
    // One attachment keeps the plain row. TWO OR MORE become a swipeable carousel (scroll-snap gives us
    // native touch/trackpad swipe for free) so a 6-image post doesn't turn into a wall of thumbnails.
    // `items` = the bare <img>/<video> html, no wrapper — callers that re-grid the media (the profile
    // Media tab) use it directly instead of regex-stripping the wrapper off `gallery`, which silently
    // broke the moment that wrapper stopped always being <div class="media-row">.
    if(!media.length) return { text, gallery:'', items:media, mediaFirst };
    if(media.length===1) return { text, gallery:`<div class="media-row">${media[0]}</div>`, items:media, mediaFirst };
    const n=media.length;
    return { text, items:media, mediaFirst, gallery:`<div class="media-car" data-n="${n}">`
      +`<div class="mc-track">${media.map(m=>`<div class="mc-item">${m}</div>`).join('')}</div>`
      +`<button class="mc-nav mc-prev" aria-label="Previous" disabled>‹</button>`
      +`<button class="mc-nav mc-next" aria-label="Next">›</button>`
      +`<span class="mc-count">1/${n}</span>`
      +`<div class="mc-dots">${media.map((_,i)=>`<i${i?'':' class="on"'}></i>`).join('')}</div>`
      +`</div>` };
  }
  // ---------- media carousel ----------
  // Bound ONCE by delegation. Carousels are emitted as HTML strings by mediaParts and land in the
  // timeline, threads, profiles, bookmarks, search and article views — per-render binding would have to
  // be duplicated into every one of those paths AND re-run on each live timeline redraw.
  function _mcSync(track){
    const car=track.closest('.media-car'); if(!car) return;
    const n=track.children.length; if(!n) return;
    const idx=Math.max(0, Math.min(n-1, Math.round(track.scrollLeft/Math.max(1,track.clientWidth))));
    const cnt=car.querySelector('.mc-count'); if(cnt) cnt.textContent=(idx+1)+'/'+n;
    car.querySelectorAll('.mc-dots i').forEach((d,k)=> d.classList.toggle('on', k===idx));
    const p=car.querySelector('.mc-prev'), nx=car.querySelector('.mc-next');
    if(p) p.disabled = idx<=0;
    if(nx) nx.disabled = idx>=n-1;
  }
  // ---- NIP-30 custom emoji ----------------------------------------------------------------------
  // Fediverse-bridged notes (and reactions) carry ["emoji", shortcode, url] tags; render the actual
  // image in place of the bare ":shortcode:" text (otherwise it reads like inline code). Restricted
  // to shortcodes the event actually declares, so it can't mangle an unrelated ":foo:" in a URL.
  function emojiTagMap(ev){ const m={}; for(const t of ((ev&&ev.tags)||[])){ if(t[0]==='emoji'&&t[1]&&t[2]) m[t[1]]=t[2]; } return m; }
  // Render a display NAME with its NIP-30 custom emoji (fediverse-bridged names are full of :shortcodes:
  // like :hellokitty_headbang:). Uses the author's OWN kind-0 emoji map (Store keeps it on the profile);
  // HTML-escapes first, then swaps shortcodes for <img>. Plain escaped text when there's no custom emoji.
  const _SC_RE=/:([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):/;
  const _emojiRefetched=new Set();
  // The author's kind-0 emoji map may be MISSING because we cached their profile before emoji-tag support
  // OR they (a fedi puppet) republished their kind-0 with emoji tags AFTER we cached it — and needProfile
  // skips already-cached pubkeys. So when a name HAS shortcodes but we have no map, force ONE fresh kind-0
  // fetch (bypassing the cache skip); saveProfile backfills/updates the emoji, then re-decorate the names.
  function _refetchEmojiProfile(pk){
    if(!pk || _emojiRefetched.has(pk)) return; _emojiRefetched.add(pk);
    Relay.query([{ authors:[pk], kinds:[0], limit:1 }]).then(evs=>{
      if(evs && evs.length){ Store.saveProfile(evs.sort((a,b)=>b.created_at-a.created_at)[0]);
        if(Store.profileEmojis(pk)) try{ decorateProfiles(); }catch(_){} }
    }).catch(()=>{});
  }
  function emojiName(pk, name){
    const safe=enc(name||'');
    const map=(Store.profileEmojis&&Store.profileEmojis(pk));
    if(!map){ if(_SC_RE.test(name||'')) _refetchEmojiProfile(pk); return safe; }   // has shortcodes but no map → refetch once
    return safe.replace(/:([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):/g,(m,sc)=>
      map[sc]?`<img class="emoji-inline" src="${enc(map[sc])}" alt="${enc(m)}" title="${enc(m)}" loading="lazy">`:m);
  }
  // ---------- instance custom emoji (NIP-30) ----------
  // The operator's emoji packs (Admin → Emoji, served by /client/emojis). Loaded ONCE,
  // lazily, on the first picker open — a real pack is thousands of entries, so it is never part of
  // startup. Typing/picking a :shortcode: is all the user does; `tagsFor` turns it into the NIP-30
  // ["emoji", code, url] tags that make it render in EVERY client, not just this one.
  const InstEmoji = {
    list:[], map:{}, loaded:false, _p:null,
    SC_RE:/:[A-Za-z0-9_+\-]+:/,
    CACHE_KEY:'pc_emoji_index', CACHE_TTL:3600,   // stale after an hour → refreshed in the BACKGROUND
                                                  // (the cached copy is always used immediately)
    // The INDEX is cached in localStorage (its own key — not the ClientSettings blob, which is
    // rewritten on every setting change). That covers all three shells identically: web PWA, the
    // Electron desktop app (which loads /client live) and the APK (whose service worker is
    // deliberately media-only, so it would never cache this JSON). The IMAGES need nothing extra —
    // they are <img> requests, which sw.js already stores cache-first in MEDIA_CACHE, cross-origin
    // included (the emoji route sends CORS + long max-age, so the APK caches them too).
    _fromCache(){
      try{
        const raw=localStorage.getItem(this.CACHE_KEY); if(!raw) return null;
        const c=JSON.parse(raw);
        return (c && Array.isArray(c.emojis) && c.base) ? c : null;
      }catch(_){ return null; }
    },
    _apply(base, emojis){
      this.list=(emojis||[]).map(e=>({s:e.s, p:e.p, u:`${base}/${e.p}/${e.f}`, t:`${base}/${e.p}/${e.f}?t=1`}));
      this.map={}; this.list.forEach(e=>{ this.map[e.s]=e.u; });
      this.loaded=true; return this.list;
    },
    _fetch(){
      // The server sends {base, emojis:[{s,p,f}]} — one base for thousands of entries, so the
      // payload stays ~200 KB (30 KB gzipped) instead of repeating a full URL per emoji.
      return fetch('/client/emojis').then(r=>r.json()).then(j=>{
        const base=(j&&j.base)||'/client/emoji', emojis=(j&&j.emojis)||[];
        try{ localStorage.setItem(this.CACHE_KEY, JSON.stringify({at:Math.floor(Date.now()/1000), base, emojis})); }
        catch(_){ }   // quota / private mode → run uncached
        return this._apply(base, emojis);
      });
    },
    load(){
      if(this.loaded) return Promise.resolve(this.list);
      if(this._p) return this._p;
      const c=this._fromCache();
      if(c){
        this._apply(c.base, c.emojis);                       // instant: no network on the picker's open
        if((Math.floor(Date.now()/1000)-(c.at||0)) > this.CACHE_TTL) this._fetch().catch(()=>{});
        return Promise.resolve(this.list);                   // …and refresh in the background when stale
      }
      this._p = this._fetch().catch(()=>{ this.loaded=true; return []; });
      return this._p;
    },
    // Render :shortcodes: in text that has no event (and therefore no NIP-30 tags) yet — the composer
    // PREVIEW. Everything already posted renders through applyEmojis with the event's own tags; this
    // is only for a draft, where the instance map IS the source of truth.
    render(htmlStr){
      if(!this.loaded || !htmlStr) return htmlStr;
      return String(htmlStr).replace(/<[^>]*>|:([A-Za-z0-9_+\-]+):/g,(m,sc)=>
        sc ? (this.map[sc] ? `<img class="emoji-inline" src="${enc(this.map[sc])}" alt="${enc(m)}" title="${enc(m)}" loading="lazy">` : m) : m);
    },
    // ["emoji", shortcode, url] for every KNOWN shortcode in `content`. Unknown ones are left as
    // plain text (they may be someone else's emoji, quoted) and a tag is never duplicated.
    tagsFor(content, tags){
      if(!this.loaded || !content) return tags;
      const out=(tags||[]).slice(); const have=new Set(out.filter(t=>t&&t[0]==='emoji').map(t=>t[1]));
      (String(content).match(/:([A-Za-z0-9_+\-]+):/g)||[]).forEach(m=>{
        const sc=m.slice(1,-1);
        if(this.map[sc] && !have.has(sc)){ have.add(sc); out.push(['emoji', sc, this.map[sc]]); }
      });
      return out;
    },
  };
  function applyEmojis(htmlStr, ev){
    const map=emojiTagMap(ev); if(!Object.keys(map).length) return htmlStr;
    // Alternate the regex so it CONSUMES whole HTML tags untouched, then matches a :shortcode: only in
    // text — otherwise a shortcode that appears inside an <a href="…:x:…"> attribute would be replaced
    // mid-tag and corrupt the markup. Shortcode charset allows a trailing @host (remote/federated
    // custom emoji, e.g. :blobcat@host:) to match the server's NIP-30 tags.
    return (htmlStr||'').replace(/<[^>]*>|:([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):/g,(m,sc)=>
      sc ? (map[sc] ? `<img class="emoji-inline" src="${enc(map[sc])}" alt="${enc(m)}" title="${enc(m)}" loading="lazy">` : m) : m);
  }
  // Display form of a kind-7 reaction's emoji: an <img> for a NIP-30 custom emoji, else escaped text.
  function reactDisp(e){
    let c=(e&&e.content)||''; if(c==='+'||c==='') return '❤️'; if(c==='-') return '👎';
    if(/^:[^:\s]+:$/.test(c)){ const nm=c.slice(1,-1); const t=((e.tags)||[]).find(x=>x[0]==='emoji'&&x[1]===nm&&x[2]);
      if(t) return `<img class="emoji-inline" src="${enc(t[2])}" alt="${enc(c)}" title="${enc(c)}" loading="lazy">`; }
    return enc(c);
  }
  // Inner HTML of the NIP-36 content-warning reveal overlay (used by noteCard's blurred template).
  function _cwRevealInner(reason){ return `🔞 Sensitive content${reason?' — '+enc(reason):''}<span class="cw-show">Show</span>`; }
  // Mark one of YOUR OWN posts NSFW after the fact. Nostr events are immutable, so the only way to
  // get a warning that EVERY client honours (NIP-36) is to re-post: publish an identical copy carrying
  // the content-warning tag, then delete the original. The copy is a NEW event — its engagement
  // (likes/replies/zaps) starts fresh — so this is destructive and confirmed first.
  async function repostWithWarning(id){
    const ev=Store.get(id); if(!ev){ toast('post not loaded'); return; }
    if(ev.pubkey!==S.ME.pubkey){ toast('you can only do this to your own posts'); return; }
    if(!await uiConfirm('Re-post this with an NSFW warning?\n\nNostr posts can’t be edited, so this DELETES the original and publishes a fresh copy with a content-warning that every client blurs. The new post won’t carry over the original’s likes, replies or zaps.')) return;
    const reason=(await uiPrompt('Content warning reason (optional):')||'').trim();
    try{
      // Re-use the original content + tags (mentions, reply/quote refs, imeta, hashtags), dropping any
      // existing content-warning, and append ours. Keep the same kind so polls/community posts survive.
      const tags=(ev.tags||[]).filter(t=>t[0]!=='content-warning').map(t=>t.slice());
      tags.push(['content-warning', reason]);
      // noQueue: this is a two-step post-then-delete. A queued replacement that lands an hour later, after
      // the delete step was skipped, would leave the original and the warned copy both live.
      const r=await publish(ev.kind||1, ev.content||'', tags, {quiet:true, noQueue:true});   // we show our own specific messages
      // Only delete the original once the REPLACEMENT actually landed — otherwise a relay blip on the new
      // copy would delete the post and lose it entirely (there's no retry queue).
      if(!r || !r.ok){ toast('couldn’t post the warned copy — original kept, try again'); return; }
      if(r.ev) Store.saveEvent(r.ev);
      const d=await publish(5, 'replaced with a content-warning version', [['e', id]], {quiet:true});   // delete the original
      if(!d || !d.ok){ toast('warned copy posted, but couldn’t delete the original — delete it manually'); renderView(true); return; }
      toast('🔞 re-posted with warning');
      renderView(true);
    }catch(e){ toast('failed: '+((e&&e.message)||e)); }
  }
  /* THE ACTION ROW — reply, repost, quote, react, tip, more.
   *
   * ONE implementation, because a card without it is a post nobody can answer, boost, zap or
   * bookmark, and that failure is invisible in review: the card looks finished. The mini-app card
   * (kind 1063) was rendered by hand and simply had no `.acts` at all, so a game shared as a post
   * could not be replied to. Everything it needs comes off the event, so any card that renders a
   * `.note` with `data-id` can call it — the delegated click handler keys on nothing else. */
  function actsRow(ev){
    const counts = countsFor(ev.id);
    const liked = myReaction(ev.id);
    const hasNoteXmr = isXmrAddr(xmrForNote(ev)) || _advertises(ev.pubkey,'monero');
    const hasNoteBch = isBchAddr(bchOf(profOf(ev.pubkey))) || _advertises(ev.pubkey,'bitcoincash');
    const _rtAct = _repostAction(ev.id, counts.iRt);
    return `<div class="acts">
          <button class="act" data-a="reply" title="reply">${REPLY_ICON} <span class="n">${counts.replies?fmtSats(counts.replies):''}</span></button>
          <button class="act rt ${counts.iRt?'on':''}${_rtAct.pending?' rt-unconfirmed':''}" data-a="repost" title="${_rtAct.label}" aria-label="${_rtAct.label}">${RT_ICON} <span class="n">${counts.reposts?fmtSats(counts.reposts):''}</span></button>
          <button class="act actq" data-a="quote" title="quote post">${QUOTE_ICON}</button>
          <button class="act ${liked?'on':''}" data-a="react" title="${liked?'remove your reaction':'react'}"><span class="react-ic">${liked||REACT_ICON}</span> <span class="n">${counts.reactions?fmtSats(counts.reactions):''}</span></button>
          <button class="act actz ${(counts.zaps||counts.tipN)?'on':''}" data-a="tip" title="tip — Lightning${hasNoteXmr?', Monero':''}${hasNoteBch?', Bitcoin Cash':''}"><span class="tipbolt">${ZAP_ICON}${hasNoteXmr?`<sup class="xmr-mark">ɱ</sup>`:''}${hasNoteBch?`<sup class="bch-mark">🟢</sup>`:''}</span> <span class="n">${enc(tipCountLabel(counts))}</span></button>
          <button class="act actm ${S.BOOKMARKS.has(ev.id)?'on':''}" data-a="menu" title="more"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button>
        </div>`;
  }
  let _noteCardErrs = 0;
  function noteCard(ev, prefix=''){
   try{
    const p = profOf(ev.pubkey); if(!S.NO_IMAGES) needProfile(ev.pubkey);   // data saver: observeProfiles fetches lazily as the card nears view
    const mp = mediaParts(ev.content, ev);
    // Wall-of-text guard: clamp very long posts with a "Show more" toggle so the feed stays scannable.
    const bodyTxt = stripQuoteRef(mp.text, ev);
    const longTxt = !!bodyTxt && (bodyTxt.length > 480 || (bodyTxt.match(/\n/g)||[]).length > 10);
    const name = p.name||p.display_name||(npubOf(ev.pubkey).slice(0,12)+'…');
    // Data saver: avatars are the biggest remaining image cost in the feed (dozens per page). Point them
    // at the single, already-cached local LOGO so a feed page pulls ~no avatar bytes over a throttled
    // link. Content media is already tap-to-load; this closes the other half.
    const av = S.NO_IMAGES ? S.LOGO : (p.picture || S.LOGO);
    const handle = niceNip05(p.nip05) || ('@'+npubOf(ev.pubkey).slice(4,12));
    const counts = countsFor(ev.id);
    const mine = ev.pubkey===S.ME.pubkey;
    // NIP-36 content warning: blur the body + media behind a reveal button.
    const cwTag = ev.tags.find(t=>t[0]==='content-warning');
    const cw = S.BLUR_NSFW && (!!cwTag || isSensitive(ev));   // content-warning OR #nsfw tag; honour the toggle
    const cwReason = cwTag ? String(cwTag[1]||'').trim() : (cw ? 'NSFW' : '');
    const noteXmr = xmrForNote(ev), hasNoteXmr = isXmrAddr(noteXmr);   // resolve ONCE; stash on the card so the tip handler still has it if the note is later evicted from Store
    // 🎉 congrats / 🌅 gm from the post's own text; 😭 from other people's reactions. Text wins when both
    // apply, so a "congrats!" that someone sobbed at still reads as the celebration it is.
    const _celeb = _celebrateOf(bodyTxt) || ((_postEffectsOn() && counts.sob) ? 'sob' : '');
    return `<article class="note" data-id="${ev.id}" data-pk="${ev.pubkey}"${hasNoteXmr?` data-xmr="${enc(noteXmr)}"${_notePaymentXmr(ev)?' data-xmr-note="1"':''}`:''}${_celeb?` data-celebrate="${_celeb}"`:''}>
      <img class="av" src="${enc(av)}" onerror="this.src='${S.LOGO}'">
      <div class="body">${prefix}
        <div class="hd"><span class="name" data-prof="${ev.pubkey}">${emojiName(ev.pubkey,name)}</span><span class="vchk"></span>
          <span class="handle">${enc(handle)}</span><span class="time">${timeAgo(ev.created_at)}</span>${S.PINNED.has(ev.id)?'<span class="pin-badge" title="Pinned to your profile">📌</span>':''}${(window.Outbox&&Outbox.has(ev.id))?'<span class="pending-badge" data-pending="'+enc(ev.id)+'" title="Waiting to send — tap to send now or discard">Pending</span>':''}</div>
        ${cw?`<div class="cw-wrap cw-on"><div class="cw-reveal" onclick="event.stopPropagation();var w=this.parentElement;w.classList.remove('cw-on');this.remove();">${_cwRevealInner(cwReason)}</div><div class="cw-inner">`:''}
        ${mp.mediaFirst?mp.gallery:''}
        <div class="txt${longTxt?' clamp':''}">${applyEmojis(linkify(bodyTxt), ev)}</div>
        ${longTxt?`<button class="txt-more" onclick="event.stopPropagation();var t=this.previousElementSibling;t.classList.toggle('clamp');this.textContent=t.classList.contains('clamp')?'Show more ↓':'Show less ↑';">Show more ↓</button>`:''}
        ${xmrTipBadge(ev)}
        ${mp.mediaFirst?'':mp.gallery}
        ${linkCardHtml(mp.text)}
        ${webxdcCardHtml(ev)}
        ${quoteHtml(ev)}
        ${cw?`</div></div>`:''}
        ${actsRow(ev)}
      </div></article>`;
   }catch(e){
     // One bad global (a missing NostrTools, a half-booted app) makes EVERY card take this branch, and a
     // silent catch turns that into "all my posts say couldn't render" with an empty console — undebuggable
     // from the outside. Log the first few, once per page, with the id so the failure can be reproduced.
     if((_noteCardErrs=(_noteCardErrs||0)+1) <= 3) try{ console.error('[noteCard] render failed', (ev&&ev.id)||'?', e); }catch(_){}
     // The message goes ON the card, not just in the console. Whoever sees this is usually not the
     // person who can open devtools — a screenshot of "⚠ couldn't render this post" alone is
     // unactionable, the same line with "NT is not defined" on it diagnoses itself.
     const why = (e && (e.message || e.name)) ? String(e.message || e.name).slice(0,140) : '';
     return `<article class="note" data-id="${(ev&&ev.id)||''}" data-pk="${(ev&&ev.pubkey)||''}"><div class="body"><div class="txt muted small">⚠ couldn't render this post${why?' — '+enc(why):''}</div></div></article>`;
   }
  }
  // A NIP-18 quote post carries both a `q` tag (rendered by quoteHtml) AND usually the same
  // nostr:nevent inline — strip the inline one so the quoted note doesn't embed twice.
  function stripQuoteRef(text, ev){
    const q=(((ev&&ev.tags)||[]).find(t=>t&&t[0]==='q')||[])[1]; if(!q) return text;
    return (text||'').replace(/(?:nostr:)?(?:nevent1|note1)[0-9a-z]{20,}/gi, m=>{
      try{ const d=NT().nip19.decode(m.replace(/^nostr:/i,'')); const id=d.type==='note'?d.data:(d.data&&d.data.id); return id===q?'':m; }catch(_){ return m; }
    }).replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
  }
  // Open an addressable event (naddr): articles (k30023) open in the reader, else as a thread.
  async function openNaddr(pk, d, kind){
    kind=parseInt(kind,10)||0;
    const filt={ authors:[pk], kinds:[kind] }; if(d) filt['#d']=[d];
    let evs=[]; try{ evs=await Relay.query([filt]); }catch(_){}
    const ev=evs.sort((a,b)=>b.created_at-a.created_at)[0];
    if(!ev){ toast('referenced post not found on the relay'); return; }
    Store.saveEvent(ev); needProfile(ev.pubkey);
    // A stream link must open the PLAYER. Without this a shared stream naddr fell through to renderThread and
    // rendered a live broadcast as an ordinary post — the link "didn't point to the stream".
    if(kind===30023) openArticle(ev);
    else if(kind===30311) openStream(ev);
    else if(kind===30617) openRepo(ev, { restore:S._routing });   // a shared git repo (NIP-34) opens the repo view, not a thread — and a BACK press re-opens the tab it was left on
    else renderThread(ev.id);
  }
  function quoteHtml(ev){
    const q=(ev.tags.find(t=>t[0]==='q')||[])[1]; if(!q) return '';
    const mc=q.match(/^(\d+):([0-9a-f]{64}):(.*)$/i);   // addressable quote: kind:pubkey:dtag (NIP-18/22)
    if(mc){ const key=`${+mc[1]}:${mc[2]}:${mc[3]}`; const c=_adCache.get(key);
      if(c) return addrDiv(c);
      needAddr(+mc[1], mc[2], mc[3]);
      return `<div class="quoted muted small" data-naload="${enc(key)}">📄 quoted post loading…</div>`; }
    if(!/^[0-9a-f]{64}$/i.test(q)) return '';            // not a valid event ref → don't render junk
    const o=Store.get(q);
    if(!o){
      // Data saver: don't eagerly fetch the quoted note — tapping opens it (data-open → openThread).
      if(S.NO_IMAGES) return `<div class="quoted muted small" data-open="${enc(q)}">❝ quoted post — tap to load</div>`;
      needEvent(q); return `<div class="quoted muted small" data-qload="${enc(q)}">quoted post loading…</div>`;
    }
    return quotedDiv(o);
  }
  function quotedDiv(o){ const p=profOf(o.pubkey); needProfile(o.pubkey);
    const name = p.name||p.display_name||(npubOf(o.pubkey).slice(0,12)+'…');
    const av = S.NO_IMAGES ? S.LOGO : (p.picture || S.LOGO);   // data saver: hold quoted/reply-context avatars too
    const handle = niceNip05(p.nip05) || ('@'+npubOf(o.pubkey).slice(4,12));
    const mp = mediaParts(o.content, o);
    /* A quote is a second rendering of the SOURCE event, so its warning belongs to that event too.
     * Checking the outer note would expose warned media whenever a safe post quoted it; wrapping all
     * quotes would hide ordinary media. Apply the same NIP-36/hashtag gate as noteCard, locally. */
    const cwTag=(o.tags||[]).find(t=>t[0]==='content-warning');
    const cw=S.BLUR_NSFW && (!!cwTag || isSensitive(o));
    const cwReason=cwTag ? String(cwTag[1]||'').trim() : (cw?'NSFW':'');
    return `<div class="quoted" data-open="${o.id}">
      <div class="hd"><img class="qav" src="${enc(av)}" onerror="this.src='${S.LOGO}'"><span class="name" data-prof="${o.pubkey}">${emojiName(o.pubkey,name)}</span><span class="vchk" data-pk="${o.pubkey}"></span><span class="handle">${enc(handle)}</span><span class="time">${timeAgo(o.created_at)}</span></div>
      ${cw?`<div class="cw-wrap cw-on"><div class="cw-reveal" onclick="event.stopPropagation();var w=this.parentElement;w.classList.remove('cw-on');this.remove();">${_cwRevealInner(cwReason)}</div><div class="cw-inner">`:''}
      ${mp.mediaFirst?mp.gallery:''}
      <div class="txt">${applyEmojis(linkify(stripQuoteRef(mp.text, o)), o)}</div>
      ${mp.mediaFirst?'':mp.gallery}
      ${cw?`</div></div>`:''}</div>`; }
  // NIP-10 parent e-tag of a reply: the explicit `reply` marker, else `root`, else the last e-tag.
  // Returns the WHOLE tag so its relay hint (t[2]) can be used to fetch an off-relay parent.
  function replyParentTag(ev){
    const es=(ev.tags||[]).filter(t=>t[0]==='e'&&t[1]);
    return es.find(t=>t[3]==='reply')||es.find(t=>t[3]==='root')||es[es.length-1]||null;
  }
  function replyParentId(ev){ const t=replyParentTag(ev); return t?t[1]:null; }
  // Compact "↩ replying to <name>" LABEL shown above a reply — NOT the parent's full card. Rendering the
  // whole parent inline duplicated it all over a busy feed: a reply's parent is often itself a shown reply
  // or a popular post that many people reply to, so its card repeated dozens of times ("duplicate replies").
  // The reply card itself opens the full thread on tap. Name the parent author from the cached parent, else
  // the reply's last p-tag; a not-yet-cached parent is fetched so a later redraw can name it.
  function replyContextHtml(ev){
    const pid=replyParentId(ev); if(!pid) return '';
    const o=Store.get(pid);
    const parentTag=replyParentTag(ev),pk=(o&&o.pubkey)||(ev.kind===1111&&parentTag&&parentTag[3])||((ev.tags.filter(t=>t[0]==='p'&&t[1]).slice(-1)[0]||[])[1]);
    if(!o) needEvent(pid);
    if(!pk) return `<div class="reply-ctx"><span class="reply-ctx-lbl">↩ reply</span></div>`;
    const p=profOf(pk); needProfile(pk); const nm=p.name||p.display_name;
    // Parent name is a .name[data-prof] span: renders custom emoji now (emojiName) and gets filled/patched
    // by decorateProfiles once the author's kind-0 loads — so bridged :shortcode: usernames show as images.
    const inner = nm ? emojiName(pk,nm) : (npubOf(pk).slice(0,12)+'…');
    return `<div class="reply-ctx"><span class="reply-ctx-lbl">↩ replying to <span class="name" data-prof="${pk}">${inner}</span></span></div>`;
  }
  const _evQ=new Set(); let _evT=null; const _evTries=new Map();
  function needEvent(id){ if(id&&/^[0-9a-f]{64}$/i.test(id)&&!Store.get(id)){ _evQ.add(id); if(!_evT)_evT=setTimeout(flushEvents,150);} }
  /* Fetch the events other cards REFER to — a repost's original, a reply's parent, a quote.
   *
   * IT RETRIES WHAT DID NOT COME BACK, and that is the whole of this function's difficulty. A card
   * whose referenced event never arrives renders as its own shell: the "↩ replying to …" header
   * (which is built from the reply's OWN tags and therefore always works) above a body that stays
   * "loading post…". That is exactly the reported symptom — "no posts are shown but you see REPLYING
   * TO" — and it is what one lost query does, because nothing ever asked again.
   *
   * The query is lost for entirely ordinary reasons: a socket the OS froze while the app was in the
   * background, a relay that answered nothing before the timeout, a reconnect landing mid-flight. On
   * a desktop something usually repaints and re-queues; on a tablet left on one screen, nothing does,
   * so the feed sits there half-drawn until it is scrolled or reloaded — "it eventually fixes
   * itself", which is what a missing retry looks like from the outside.
   *
   * BOUNDED, because "not on any relay we are connected to" is a real and common answer: a few
   * attempts with a widening gap, then that id is left alone. `_evTries` is cleared for anything that
   * lands and capped, so a long session of scrolling a busy feed cannot grow it without limit.
   *
   * ONLY AN ANSWER SPENDS THAT BUDGET, and getting that wrong is what made a backgrounded phone come
   * back to a screen of shells. A frozen socket does not refuse the query, it swallows it: relay.js
   * drops a REQ written to anything that is not OPEN, and a socket the OS thawed reads OPEN while
   * being dead, so either way nobody EOSEs and `query` resolves empty on its 6s timer. That is
   * indistinguishable from "no relay has it" unless you look at `complete` — and read as the latter,
   * ~40s in a pocket burns all four attempts and the id is then abandoned for the LIFE OF THE PAGE.
   * Measured: a redraw on resume re-queues it and gets exactly one more shot, which the still-thawing
   * socket also eats, and after that nothing ever asks again. Hence two counters: misses the relays
   * actually answered (permanent once spent), and attempts nobody answered — bounded far more
   * generously, since none of them is evidence, and re-armed wholesale by `_reaskMissing()`. */
  const _EV_TRIES_MAX = 4;
  const _EV_STALL_MAX = 8;
  const _evStalls = new Map();
  async function flushEvents(){
    _evT=null; const ids=[..._evQ]; _evQ.clear(); if(!ids.length) return;
    let evs=[], live=true, threw=false;
    // Ask a socket that can actually answer. renderThread already waits like this before its first
    // query (a REQ into a CONNECTING socket is silently dropped); this is the same wait, instant when
    // we are connected, and it also reconnects a zombie so the query below has somewhere to land.
    try{ if(Relay.ready) live = await Relay.ready(4000); }catch(_){ live=true; }
    // `query` can reject (no relay is up at all) — unhandled, that killed the whole flush and left
    // every id in this batch unasked AND unqueued.
    try{ evs=await Relay.query([{ids}]); }catch(_){ evs=[]; threw=true; }
    // `complete` is true only when every relay we ASKED sent an EOSE. Anything else — a timeout, a
    // rejection, no live socket to ask in the first place — means the relays never spoke, which says
    // nothing about whether the event exists.
    const answered = live && !threw && evs.complete !== false;
    const got=new Set();
    for(const e of evs){ got.add(e.id); _evTries.delete(e.id); _evStalls.delete(e.id); Store.saveEvent(e); needProfile(e.pubkey); patchLoaded(e); }
    decorateProfiles();
    const missing=ids.filter(id=>!got.has(id) && !Store.get(id));
    if(!missing.length) return;
    let worst=0;
    for(const id of missing){
      if((_evTries.get(id)||0) >= _EV_TRIES_MAX) continue;   // answered that many times and not there; stop asking
      const map = answered ? _evTries : _evStalls, max = answered ? _EV_TRIES_MAX : _EV_STALL_MAX;
      const n=(map.get(id)||0)+1;
      if(n>max) continue;
      map.set(id,n); _evQ.add(id); if(n>worst) worst=n;
    }
    if(_evTries.size>2000) _evTries.clear();
    if(_evStalls.size>2000) _evStalls.clear();
    // Widening gap, so a feed full of unreachable references does not become a query loop.
    if(_evQ.size && !_evT) _evT=setTimeout(flushEvents, Math.min(15000, 900*Math.pow(2, worst-1)));
  }
  /* Ask again for every reference still rendered as a placeholder.
   *
   * The moment the app comes BACK is the one moment when "we asked and got nothing" is worth
   * revisiting, because the reason we got nothing — a socket the OS froze — has just gone away. And
   * nothing else revisits it: `_drawTimeline` reconciles by KEY and reuses the cards already on
   * screen, so a repaint does not re-run the `needEvent()` that built them. A card that gave up while
   * the phone was in a pocket therefore stays a shell until the page is reloaded, which is exactly
   * "come back to the app and every post is a placeholder".
   *
   * The selectors are `patchLoaded`'s, deliberately: those are the placeholders it knows how to fill
   * in, so everything asked for here has somewhere to land. Throttled, because a flapping relay fires
   * onReconnect repeatedly and this clears the give-up state each time. */
  let _lastReask = 0;
  function _reaskMissing(){
    if(Date.now() - _lastReask < 15000) return;
    _lastReask = Date.now();
    _evTries.clear(); _evStalls.clear();
    let n=0;
    document.querySelectorAll('.note[data-orig],[data-qload],[data-nctx]').forEach(el=>{
      if(n>=200) return;                                   // the timeline caps at 200 cards; don't build a huge REQ
      const d=el.dataset, id=d.orig||d.qload||d.nctx;
      if(id && !Store.get(id)){ n++; needEvent(id); }
    });
  }
  // Patch repost/quote placeholders in place when their referenced event loads — NO full feed
  // re-render (that flashed the whole screen on the busy global feed).
  function patchLoaded(e){
    $$(`.note[data-orig="${e.id}"]`).forEach(el=>{
      const div=document.createElement('div'); div.innerHTML=noteCard(e, el.dataset.rtpk?_repostTag(el.dataset.rtpk,+el.dataset.rtts||0):'');
      if(div.firstElementChild){if(el.dataset.repostId)div.firstElementChild.dataset.repostId=el.dataset.repostId;el.replaceWith(div.firstElementChild);}
    });
    $$(`[data-nctx="${e.id}"]`).forEach(el=>{   // notification context: fill the preview once the post lands
      const div=document.createElement('div'); div.innerHTML=_notifCtxHtml(e.id);
      if(div.firstElementChild) el.replaceWith(div.firstElementChild); else el.remove();
    });
    $$(`[data-qload="${e.id}"]`).forEach(el=>{
      const div=document.createElement('div'); div.innerHTML=quotedDiv(e);
      if(div.firstElementChild) el.replaceWith(div.firstElementChild);
    });
  }
  function needAddr(kind, pubkey, d){
    const key=`${kind}:${pubkey}:${d}`;
    if(_adCache.has(key)) return; _adQ.set(key, {kind, pubkey, d, key});
    if(!S._adT) S._adT=setTimeout(flushAddrs, 150);
  }
  async function flushAddrs(){
    S._adT=null; const items=[..._adQ.values()]; _adQ.clear();
    for(const it of items){
      try{
        const evs=await Relay.query([{ kinds:[it.kind], authors:[it.pubkey], '#d':[it.d], limit:1 }]);
        const e=(evs||[]).sort((a,b)=>(b.created_at||0)-(a.created_at||0))[0];
        if(e){ Store.saveEvent(e); _adCache.set(it.key, e); needProfile(e.pubkey);
          $$('[data-naload]').forEach(el=>{ if(el.dataset.naload!==it.key) return;
            const div=document.createElement('div'); div.innerHTML=addrDiv(e);
            if(div.firstElementChild) el.replaceWith(div.firstElementChild); }); }
      }catch(_){}
    }
    decorateProfiles();
  }
  // Preview card for an addressable event (NIP-23 article etc.) — clickable via the .naddrlink handler.
  function addrDiv(e){
    const p=profOf(e.pubkey); needProfile(e.pubkey);
    const d=(e.tags.find(t=>t[0]==='d')||[])[1]||'';
    const title=(e.tags.find(t=>t[0]==='title')||[])[1]||'(untitled)';
    const summary=(e.tags.find(t=>t[0]==='summary')||[])[1]||'';
    const img=(e.tags.find(t=>t[0]==='image')||[])[1]||'';
    const name=p.name||p.display_name||(npubOf(e.pubkey).slice(0,12)+'…');
    return `<div class="quoted naddrlink" data-pk="${enc(e.pubkey)}" data-d="${enc(d)}" data-k="${enc(String(e.kind))}">
      ${img?_hold(`<img class="m" src="${enc(img)}" loading="lazy">`, img, 'image', 'm'):''}
      <div class="hd"><span class="name">📄 ${e.kind===30023?'Article':'Post'} · ${enc(name)}</span></div>
      <div class="txt"><b>${enc(title)}</b>${summary?`<br><span class="muted small">${enc(summary)}</span>`:''}</div></div>`;
  }

  // A 😭 REACTION makes the post rain sobs (see _playCelebration). Unlike the congrats/gm celebrations
  // this is driven by other people's reactions rather than the post's own text, so it appears when the
  // kind-7s load and disappears if they are removed — the note re-renders off these counts either way.
  // Fediverse-bridged reactions arrive as the `:sob:` shortcode rather than the codepoint, so match both.
  const _SOB_EMOJI = ['😭', '😢', '🥲', '😿'];
  // The suffix MUST start with "_" — a bare `[a-z0-9]+` tail made `cry` swallow `:cryptobro:` (and any
  // other custom emoji that merely STARTS with "cry"), which the test caught.
  const _SOB_CODES = /^:(sob|cry|crying|cry_face|loudly_crying_face|tear|tears)(_[a-z0-9]+)?:$/i;
  function _isSob(content){
    const s = (content || '').trim();
    if(!s) return false;
    return _SOB_CODES.test(s) || _SOB_EMOJI.some(e => s.includes(e));
  }
  // Apply the sob celebration LIVE. data-celebrate is otherwise only set when a note RENDERS, so a 😭
  // that arrived while you were looking at the post did nothing until the feed happened to re-render —
  // which reads as the feature being broken. Called from every kind-7 ingest path.
  function applySobLive(ev){
    try{
      if(!_postEffectsOn()) return;                  // post effects disabled — no live sob overlay
      if(!ev || ev.kind!==7 || !_isSob(ev.content)) return;
      let id=null;                                   // NIP-25: the reacted post is the LAST e tag
      for(let i=ev.tags.length-1;i>=0;i--) if(ev.tags[i][0]==='e'){ id=ev.tags[i][1]; break; }
      if(!id) return;
      const el=document.querySelector(`.note[data-id="${CSS.escape(id)}"]`);
      if(!el || el.dataset.celebrate) return;        // absent, or already celebrating something
      el.dataset.celebrate='sob';
      el._celebNext=0;                               // let the sweep fire it on its next tick
      observeCelebrations();                         // start the sweep if nothing was celebrating yet
    }catch(_){}
  }
  // reaction/repost counts — built ONCE per render pass (single scan of the store) instead of
  // re-scanning the whole store for every rendered note (was O(notes × store)).
  let CIDX = null;
  function invalidateCounts(){ CIDX = null; }
  let _cidxErrs = 0;
  function buildCounts(){
    const c = { replies:{}, reactions:{}, reposts:{}, zaps:{}, zapN:{}, myRt:new Set(), myReact:{}, myReactIds:{}, sob:{}, tips:{}, tipN:{} };
    const lastE = e => { const tg=(e&&e.tags)||[]; for(let i=tg.length-1;i>=0;i--) if(tg[i]&&tg[i][0]==='e') return tg[i][1]; return null; };
    // This index is what EVERY note card asks for (countsFor/myReaction), and CIDX is only assigned at
    // the end — so anything that throws in here doesn't cost one post's like count, it costs the whole
    // timeline, on every render, until the offending event leaves the cache. Store now normalises tags
    // at the boundary; per-event containment is the second lock, so a shape nobody has thought of yet
    // can only ever lose ITS OWN counts. Same reason ME is checked rather than assumed: a guest reading
    // the public feed has no key, and `ME.pubkey` on a kind-7 would take out every card for them too.
    for(const e of Store.all()){
     try{
      const id = lastE(e); if(!id) continue;
      if(e.kind===1||e.kind===1111){
        // An ADDRESS tip (Monero `t:monerotip` / BCH `t:bchtip`) is a kind-1 carrying an `e` tag, so it
        // used to be counted as a REPLY and shown nowhere as a tip — you'd receive 0.00022 XMR and the
        // note would just gain a phantom reply. It isn't a zap receipt either (no kind 9735, no sats:
        // the payment happens wallet-to-wallet off Nostr), so it needs its own tally, per unit.
        const tip=e.kind===1?_tipNote(e):null;
        if(tip){
          const n=parseFloat(tip.amt)||0;
          (c.tips[id]=c.tips[id]||{})[tip.unit]=(c.tips[id][tip.unit]||0)+n;
          c.tipN[id]=(c.tipN[id]||0)+1;
        } else c.replies[id]=(c.replies[id]||0)+1;
      }
      // myReact holds the DISPLAY HTML (reactDisp), not the raw content — the react button injects it as
      // innerHTML, so a NIP-30 custom emoji has to become its <img> here or the button reads ":shortcode:".
      // myReactIds keeps the EVENT ids too — un-reacting is a NIP-09 delete of them, and it collects
      // ALL of mine on a post (another client may have left several) so one tap clears the lot.
      else if(e.kind===7){ c.reactions[id]=(c.reactions[id]||0)+1;
        if(S.ME && e.pubkey===S.ME.pubkey){ c.myReact[id]=reactDisp(e); (c.myReactIds[id]=c.myReactIds[id]||[]).push(e.id); }
        if(_isSob(e.content)) c.sob[id]=(c.sob[id]||0)+1; }
      else if((e.kind===6||e.kind===16)&&!_repostDeleted(e)){ c.reposts[id]=(c.reposts[id]||0)+1; if(S.ME && e.pubkey===S.ME.pubkey){c.myRt.add(id);} }
      else if(e.kind===9735){ const sats=zapAmount(e); if(sats){ c.zaps[id]=(c.zaps[id]||0)+sats; c.zapN[id]=(c.zapN[id]||0)+1; } }
     }catch(err){
      // Bounded and NOISY on purpose. Containment is what keeps the timeline alive, but a catch that
      // says nothing is how the original took a week to find: the feed was visibly broken and the
      // console was empty. Name the event so the next one is reproducible from a screenshot.
      if((_cidxErrs=(_cidxErrs||0)+1) <= 3) try{ console.error('[buildCounts] skipped event', (e&&e.id)||'?', err); }catch(_){}
     }
    }
    CIDX = c;
  }
  function countsFor(id){ if(!CIDX) buildCounts(); return { replies:CIDX.replies[id]||0, reactions:CIDX.reactions[id]||0, reposts:CIDX.reposts[id]||0, zaps:CIDX.zaps[id]||0, zapN:CIDX.zapN[id]||0, iRt:CIDX.myRt.has(id), sob:CIDX.sob[id]||0, tips:CIDX.tips[id]||null, tipN:CIDX.tipN[id]||0 }; }
  /* Amount label for an address tip: XMR/BCH are decimal, not sats, so fmtSats can't render them
     (0.00022 would round to "0"). Trim trailing zeros so a tip reads as the sender typed it. */
  function fmtTipAmt(n){ return (Number(n)||0).toFixed(8).replace(/0+$/,'').replace(/\.$/,'') || '0'; }
  /* What the tip button shows: Lightning sats and any address tips, side by side — they're different
     currencies and summing them would be a lie. Empty string when the note has neither. */
  function tipCountLabel(counts){
    const parts=[];
    if(counts.zaps) parts.push(fmtSats(counts.zaps));
    const t=counts.tips;
    /* A TIP WITH NO AMOUNT IS STILL A TIP. `amount_xmr` is optional on the note — an older client,
       or somebody who paid without telling us how much — and keying the label on a truthy amount
       made those tips invisible: `tipN` counted them, the button lit up, and the label was empty,
       so a post read as tipped-but-for-nothing. Show the mark without a number instead; claiming an
       amount we were not told is the one thing worse than showing none. */
    if(t){
      if(t.XMR) parts.push('ɱ'+fmtTipAmt(t.XMR));
      if(t.BCH) parts.push('🟢'+fmtTipAmt(t.BCH));
      if(!parts.length && counts.tipN) parts.push('ɱ' in t || t.XMR === 0 ? 'ɱ' : '🟢');
    }
    return parts.join(' ');
  }
  function myReaction(id){ if(!CIDX) buildCounts(); return CIDX.myReact[id]||null; }
  function myReactionIds(id){ if(!CIDX) buildCounts(); return CIDX.myReactIds[id]||[]; }
  // (reaction display: '+' shows as ❤️, a custom emoji as its image — see reactDisp/buildCounts)

  // ---------- interactions ----------
  /* A STRAY SPACE MUST NOT THROW YOUR PLACE IN THE TIMELINE AWAY.
   *
   * Reported as "space bar sometimes skips down the social page", and the "sometimes" is the whole
   * clue. `.feed` is the scroll container (`overflow-y:auto`) and it is not focusable, so a space
   * scrolls whatever scrollable ancestor the FOCUSED element happens to have: click a post — a
   * plain div — and focus is inside the feed, so space pages it down; click nothing and focus is on
   * body, whose document does not scroll, so space does nothing. Same key, two behaviours, decided
   * by whatever you last touched. In a timeline a page-jump loses your position, and there is no
   * way back to it.
   *
   * So space does nothing here — EXCEPT where it already means something, and those exceptions are
   * not negotiable: it types in a field, and it ACTIVATES a focused control, which is how the
   * keyboard operates a button. Suppressing either of those would trade one bug for a worse one. */
  /* A WAY BACK, FROM ANYWHERE, WITHOUT A SIDEBAR.
   *
   * Reported three times in a row and each time it read as a different bug: "opened a profile from
   * social and now can't go back to social", "added contact in texts and no way to go back to
   * texts, I am stuck in Contacts", "clicking on Messages, opening a convo, click on avatar, you go
   * to a profile page, problem is no way back to messages".
   *
   * They are one thing. A desktop WINDOW has no sidebar and no browser chrome, and the screens that
   * navigate inside it — a profile, a thread, Contacts — set the client's VIEW directly rather than
   * going through switchView, so the window still calls itself Messages while showing a profile.
   * Pressing the app's icon focuses that window and hands the profile straight back.
   *
   * The app has a real history (`_navPushed`, popstate) and Android's back button already walks it.
   * Nothing on a desktop did. Alt+Left is the binding every browser and file manager uses, so it
   * needs no explaining, and it works in a window, on the web and in the desktop shell alike. */
  function _bindAltLeftGoesBack(){
    document.addEventListener('keydown', e => {
      if(e.key !== 'ArrowLeft' || !e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target;
      try{
        const tag = String((t && t.tagName) || '').toLowerCase();
        if(t && (t.isContentEditable || tag === 'input' || tag === 'textarea' || tag === 'select')) return;
      }catch(_){ return; }
      if(!(typeof S._navPushed !== 'undefined' && S._navPushed > 0)) return;   // nothing of ours to pop
      e.preventDefault();
      try{ history.back(); }catch(_){ }
    }, false);
  }

  function _bindSpaceDoesNotJump(){
    document.addEventListener('keydown', e => {
      if(e.key !== ' ' && e.key !== 'Spacebar') return;
      if(e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if(!t || t.isContentEditable) return;
      const tag = String(t.tagName || '').toLowerCase();
      if(tag === 'input' || tag === 'textarea' || tag === 'select') return;
      /* Space is the keyboard's "press this". Anything that can be pressed keeps it. */
      try{
        if(t.closest && t.closest('button, a[href], summary, label, [role="button"], [role="checkbox"], [role="menuitem"], [contenteditable=""], [contenteditable="true"]')) return;
      }catch(_){ return; }
      /* Only inside a scrollable feed — elsewhere on the page the browser's default is harmless and
         somebody may be relying on it. */
      try{ if(!(t.closest && t.closest('.feed'))) return; }catch(_){ return; }
      e.preventDefault();
    }, false);
  }

  function bindFeedActions(){
    _bindSpaceDoesNotJump();
    _bindAltLeftGoesBack();
    $('#feed').addEventListener('click', async (e)=>{
      if(e.target.closest('.yt-embed')) return;  // YouTube facade → handled by the player loader; don't lightbox the thumb
      const mn=e.target.closest('.mention'); if(mn){ e.preventDefault(); const pk=safePk(mn.dataset.np); if(pk) renderProfileView(pk); return; }
      const evl=e.target.closest('.evlink'); if(evl){ e.preventDefault(); openThread(evl.dataset.ev); return; }
      const ht=e.target.closest('.hashtag'); if(ht){ e.preventDefault(); renderHashtag(ht.dataset.tag); return; }
      const po=e.target.closest('.poll-opt'); if(po && !po.disabled){ e.preventDefault(); votePoll(po.dataset.poll, po.dataset.opt); return; }
      const na=e.target.closest('.naddrlink'); if(na){ e.preventDefault(); openNaddr(na.dataset.pk, na.dataset.d, na.dataset.k); return; }
      // Files grid: thumbnails load ?thumb=1, so open the parent link's FULL url in the lightbox
      // (images) — videos/docs fall through to their <a> (new tab / download).
      const fa=e.target.closest('.file-card a'); if(fa){ const fm=fa.dataset.mime||'';
        // SELECTION MODE: once anything in the drive is ticked, a tap on a card TOGGLES it instead of
        // opening it — the same rule Finder/Explorer/Drive use. Without this, one stray tap on a
        // thumbnail while picking 30 files opened the lightbox and the run was lost. Only applies to
        // the drive grid (a card with a checkbox); AI-files cards have none and open as before.
        const _selCard=fa.closest('.file-card');
        if(_selCard && _filesSel.size && _selCard.querySelector('.selbox')){
          e.preventDefault(); e.stopPropagation();
          const sha=_selCard.dataset.sha;
          if(_filesSel.has(sha)) _filesSel.delete(sha); else _filesSel.add(sha);
          const cb=_selCard.querySelector('.selbox'); if(cb) cb.checked=_filesSel.has(sha);
          _selCard.classList.toggle('selected', _filesSel.has(sha));
          const g=document.getElementById('bl-grid'); if(g) _filesSelBar(g, S._filesGridList);
          return;
        }
        if(/^video\//.test(fm)){ e.preventDefault(); openLightbox(fa.getAttribute('href'), 'video'); }
        else if(/^audio\//.test(fm)){ e.preventDefault(); openLightbox(fa.getAttribute('href'), 'audio'); }
        else if(/^image\//.test(fm) || fa.querySelector('img')){ e.preventDefault(); openLightbox(fa.getAttribute('href')); }
        return; }   // docs: fall through to the link (download / new tab)
      const im=e.target.closest('.txt img, .note-preview img, .media-row img, .media-grid img, .mc-item img'); if(im){ e.preventDefault(); openLightbox(im.currentSrc||im.src, null, _lbGroup(im)); return; }
      const xv=e.target.closest('.xt-verify'); if(xv){ e.stopPropagation();   // Monero tip: copy a ready-to-run verify command for the viewer's own wallet
        const tx=xv.dataset.xtxid||'', ad=xv.dataset.xaddr||'', pf=xv.dataset.xproof||'';
        if(!_isTxid(tx) || !isXmrAddr(ad) || !_isProofSig(pf)){ toast('this proof looks malformed — not copying'); return; }   // re-validate: never build a wallet command from untrusted tag data
        const cmd=`check_tx_proof ${tx} ${ad} "" ${pf}`;
        copyValue(cmd, 'verify command copied — paste it into your Monero wallet', 'Paste this into your Monero wallet:'); return; }
      const av=e.target.closest('.av'); if(av){ const n=e.target.closest('.note'); if(n){ renderProfileView(n.dataset.pk); return; } }
      const prof=e.target.closest('[data-prof]'); if(prof){ renderProfileView(prof.dataset.prof); return; }
      const q=e.target.closest('[data-open]'); if(q){ openThread(q.dataset.open); return; }
      // Article cards (kind-30023) that appear inline in the timeline: open the reader (or the author's
      // profile when the name is tapped). Mirrors the Articles-list handler; the .note path below
      // doesn't match them.
      const artc=e.target.closest('.article-card'); if(artc){ if(e.target.closest('[data-prof]')){ renderProfileView(artc.dataset.pk); return; } const a=Store.get(artc.dataset.id); if(a) openArticle(a); return; }
      // Git repo cards (kind-30617) surfaced in Discover/search → open the REPO DETAIL (README/issues/
      // patches), NOT the generic .note thread. A repo-card is also class="note" for styling, so without
      // this it fell through to renderThread below and opened the announcement as a thread. Let the author
      // name (data-prof) and the Copy-clone / ↗ Open controls keep their own handlers.
      const rc=e.target.closest('.repo-card'); if(rc){ if(e.target.closest('[data-prof]')){ renderProfileView(rc.dataset.pk); return; } if(e.target.closest('a,button')) return; const x=Store.get(rc.dataset.id); if(x) openRepo(x); return; }
      // Draft + scheduled cards are class="note" for the STYLING only — they hold a local draft or a
      // queued post, never a relay event, so they carry no `data-id`. The generic body-click below
      // therefore called renderThread(undefined), which looked your own unsent post up on the relay and
      // answered "Post not found on the relay." Tapping a draft opens it for editing, which is the only
      // thing the card is for; a scheduled post keeps its own buttons. Same trap the article,
      // channel and repo cards above each had to be dug out of individually.
      const dc=e.target.closest('.draft-card');
      if(dc){
        if(e.target.closest('a,button,input,textarea,select,label')) return;   // Edit / Delete / Send own their clicks
        if(dc.classList.contains('sched-card')) return;                        // queued: Edit/Cancel only
        // Same rule the note path uses below: a drag-SELECT is someone copying their own text, not a tap.
        if(window.getSelection && String(window.getSelection()).length>0) return;
        const d=Drafts.get(dc.dataset.draft);
        if(d) compose({reply:d.reply, replyPk:d.replyPk, quote:d.quote, draftId:dc.dataset.draft,
                       text:d.text, cw:d.cw, cwReason:d.cwReason});
        return;
      }
      const btn=e.target.closest('.act');
      const art=e.target.closest('.note');
      // Click anywhere else on the card body opens the post's thread, so the user doesn't have to
      // aim for the timestamp. Skip clicks on attachments / links / form controls (images already
      // returned above as a lightbox; video & co. must keep their own controls), and skip when the
      // user just drag-SELECTED text (so highlight-to-copy works instead of opening the thread).
      const hasSelection = window.getSelection && String(window.getSelection()).length>0;
      // Excluding the media CONTAINERS (.media-row/.media-grid/.media-car) made their empty space dead.
      // .media-row is a flex row and its images are `width:auto` — a portrait or narrow photo sits at the
      // left and leaves the rest of the full-width row bare, so clicking the RIGHT of a post with an image
      // hit the container, matched the exclusion, and did nothing at all. Exclude the media ITSELF instead:
      // an <img> already returned further up (lightbox), video/audio keep their controls via the tag names,
      // .mc-nav is a <button>, and the carousel dots are the one control that needs naming.
      // `art.dataset.id` guard: a .note-styled card with no event id must do NOTHING, not send the user
      // to a thread view that can only report the post missing. That error is indistinguishable from a
      // real relay problem, which is what made this look like data loss rather than a dead click.
      /* openThread, NEVER renderThread. renderThread swaps the view and pushes NOTHING, so tapping a
       * post in the feed — the single commonest navigation in the app — left no history entry at all.
       * Back then popped whatever entry happened to be underneath, which was the last post opened
       * through a path that DID push (a notification, a quote card): "click post, make a comment,
       * click the back button on my phone and it takes me back to some other random post instead of
       * my home feed", exactly, and it stayed true through every fix aimed at the push/replace rule
       * because this tap never reached that rule. openThread decides push-vs-replace and is the only
       * way into a thread from a click. */
      if(!btn){ if(art && art.dataset.id && !hasSelection && !e.target.closest('a,video,audio,button,input,textarea,select,label,.mc-dots,.link-card')) openThread(art.dataset.id); return; }
      if(!art) return;   // .act outside a note (article/stream view) binds its own handler
      const id=art.dataset.id; const pk=art.dataset.pk;
      const a=btn.dataset.a;
      if(a==='react') return pickEmoji(id,pk,btn);
      if(a==='repost') return doRepost(id,pk,btn);
      if(a==='quote') return compose({quote:id});
      if(a==='reply') return compose({reply:id, replyPk:pk});
      if(a==='delete') return doDelete(id,art);
      if(a==='tip') return doTip(id,pk,art.dataset.xmr,art.dataset.xmrNote==='1');
      if(a==='zap') return doZap(id,pk);
      if(a==='xmrtip') return doXmrTip(id,pk,art.dataset.xmr,art.dataset.xmrNote==='1');
      if(a==='bookmark') return toggleBookmark(id,btn);
      if(a==='copyid'){ let _lk=id, _m='id copied';
        try{ _lk=_webLink(NT().nip19.neventEncode({id})); _m='link copied'; }
        catch(_){ try{ _lk=_webLink(NT().nip19.noteEncode(id)); _m='link copied'; }catch(__){} }
        copyValue(_lk, _m, 'Link to this post:'); return; }
      if(a==='translate') return translatePost(id);
      if(a==='pin') return togglePin(id);
      if(a==='block') return doBlock(pk);
      if(a==='menu') return openPostMenu(id, pk, art, btn);
    });
  }
  /* Permanent delegation: #dm-msgs survives ordinary refreshes, but a restored/handoff thread can
   * enter through the reuse path without ever executing the fresh-render binding. The image exists
   * and decodes in that state, yet tapping it does nothing. One document listener follows every
   * replacement/restoration and cannot accumulate per conversation. */
  function bindDmMediaActions(){
    document.addEventListener('click', e=>{
      const im=e.target&&e.target.closest&&e.target.closest('#dm-msgs img:not(.emoji-inline)');
      if(!im)return;
      e.preventDefault();e.stopPropagation();
      openLightbox(im.currentSrc||im.src);
    },true);
  }

  return {
    _dimAttrs, _dimLearn, _healGhostPairs, _hold, _mcSync, _media, _reaskMissing, actsRow, addrDiv,
    applyEmojis, applySobLive, bindDmMediaActions, bindFeedActions, countsFor, emojiName,
    feedNoteHtml, hydratePolls, invalidateCounts, mediaParts, myReaction, myReactionIds, needAddr,
    needEvent, noteHtml, openNaddr, quotedDiv, reactDisp, replyParentId, repostWithWarning,
    get MediaDims(){ return MediaDims; },
    get VideoMount(){ return VideoMount; },
    get InstEmoji(){ return InstEmoji; },
  };
};

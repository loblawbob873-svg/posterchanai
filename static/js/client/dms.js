/* dms.js — split out of app.js by scripts/split_client_module.mjs.
 *
 * Loaded by its own <script> tag BEFORE app.js, and built synchronously by app.js (`_dmsMod()`) at the
 * point where this code used to sit — incoming DMs are subscribed at login, and a push, a notification
 * or the Android launcher lands on Messages at boot, so none of it may wait on a network load. DmCache
 * is returned as an object and reached from app.js through `_lzProxy`. The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live `let` bindings were rewritten to
 * `S.<name>` (getters/setters on `dep.state`) at exact identifier offsets, and everything else it uses
 * arrives through `dep`. tests/test_client_module_deps.py proves every name resolves.
 */
window.PCDmsFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.GUEST, S.LOGO, S.ME, S.NO_IMAGES, S.VIEW, S._cordDirectOwner, S._dmDone, S._dmLoaded, S._dmTotal, S._dmUnread, S.dmActive, S.signer
  const {
    $, $$, DISCOVERY_RELAYS, bumpNotif, Nip46, _blossomDenied, _capPlugin, _notePublishedWraps,
    _scheduleDmRefresh, _shaFromUrl, _withModule, _wrapTried, defaultRelays, dmEncOn, dmPeers,
    emojiName, enc, isMutedAuthor, mediaServer, needProfile, normalizeRelay, notifToast,
    notificationAllowed, osNotify, profOf, publish, refToPk, renderDmThread, renderMessages,
    requestBlossomAccess, switchView, toast, uploadBlob, uploadSharedEnc, userRelays,
  } = dep;

  // ---------- DMs: NIP-17 gift-wrapped (modern, local-key) + NIP-04 (legacy, read-compat) ----------
  function _dmScrollState(el){
    if(!el)return null;
    const above=Math.max(0,Number(el.scrollHeight)-Number(el.scrollTop)-Number(el.clientHeight));
    return {pinned:above<80,aboveBottom:above};
  }
  function _restoreDmScroll(el,state){
    if(!el||!state)return false;
    const max=Math.max(0,Number(el.scrollHeight)-Number(el.clientHeight));
    el.scrollTop=state.pinned!==false?max:Math.max(0,max-Math.max(0,Number(state.aboveBottom)||0));
    return true;
  }
  // A remote signer (Amber) decrypts each message on the PHONE, so restoring a big history genuinely takes a
  // while. Say so, with a count — otherwise a half-filled DM list just looks broken.
  let _dmProg='', _dmBase='', _dmProgAt=0, _dmProgWatch=0;
  /* WHY IT IS NOT MOVING, MEASURED, ON THE SCREEN THAT IS STUCK.
   *
   * "tablet stuck after 1/400" has four different causes that are identical from the outside — the
   * signer is slow, the socket is dead, the queue is starved, or nothing was ever asked — and the
   * device this happens on is the one where opening a console is hardest. PC.dmStats() answers it in
   * a browser; this is the same numbers where the counter already is, because a tablet cannot use
   * the console and a report of "it says 1/400" cannot be diagnosed from here.
   *
   * Only after 15 seconds of no advance: every message is two round trips to a phone, so pausing is
   * normal and this must not become permanent furniture. It repaints every 5s while the stall lasts,
   * so `since the relay spoke` is a live number rather than a stale one. */
  function _dmStalled(){
    if(!_dmBase) return;
    let why = '';
    try{
      const n = Nip46;
      if(n && n.remotePk){
        const live = n._live().length;
        const heard = n._rxAt ? Math.round((Date.now() - n._rxAt) / 1000) + 's ago'
                              : 'never';
        /* WHICH RELAYS, by name. A request goes to every socket this session holds — and if the
         * signer is not listening on any of them, a kind-24133 is fanned out to nobody and
         * DESTROYED (it is ephemeral; nothing stores it). That failure looks exactly like a slow
         * signer from here: the relay ACKs our publish, so "relay last spoke" stays fresh, and the
         * reply never comes because the request never reached anyone. The only thing that tells the
         * two apart is which room we are shouting into, so the line says it. */
        const hosts = (n._urls || []).map(u => { try{ return new URL(u).host; }catch(_){ return u; } });
        /* WHO THIS CLIENT IS, and WHICH SIGNER it is talking to. Without these two, a report of
         * "stuck" cannot be matched against what the relay actually carries: every client is an
         * anonymous ephemeral key from the outside, so "only two clients are exchanging and both are
         * answered" cannot be turned into "…and yours is not one of them". They are public keys —
         * the app key is ephemeral by construction and the signer's is the identity it signs as —
         * so there is nothing here to leak. A pairing that names the WRONG signer is silent in
         * exactly this way: the phone drops the request (no session for that app), the relay still
         * ACKs the publish, and this end waits out the ceiling. */
        const me8 = (n.appPk || '').slice(0, 8) || '?';
        const sg8 = (n.remotePk || '').slice(0, 8) || '?';
        why = ` — signer: ${n._pending.size} sent and unanswered, ${n._inflight + n._queue.length}`
            + ` queued, relay last spoke ${heard}` + (live ? '' : ', NO SOCKET')
            + `; via ${hosts.join(', ') || 'nothing'} (${live}/${hosts.length} open)`
            + `; me ${me8} → signer ${sg8}`;
      }
    }catch(_){}
    _dmProg = _dmBase + (why || ' — waiting on your signer');
    try{ _dmProgress(); }catch(_){}
    clearTimeout(_dmProgWatch);
    _dmProgWatch = setTimeout(_dmStalled, 5000);
  }
  function _dmProgress(done, total){
    if(arguments.length){
      _dmProg = (done < total) ? `🔓 decrypting your messages… ${done}/${total}` : '';
      _dmProgAt = Date.now();
      /* A COUNTER THAT STOPS IS NOT A COUNTER THAT FAILED, AND IT LOOKS IDENTICAL.
       *
       * Every message is two round trips to a phone, so this line advances in bursts and pausing is
       * normal. It was reported as "decrypting messages stuck at 70/400 — this is not moving", and
       * from the screen that is the only available reading: the number is the whole interface. Say
       * what the pause IS once it has gone on longer than any healthy burst, and keep the number, so
       * "slow" and "wedged" stop looking the same. Cleared by the next advance. */
      clearTimeout(_dmProgWatch);
      _dmBase = _dmProg;
      if(_dmProg) _dmProgWatch = setTimeout(_dmStalled, 15000);
    }
    const wrap=document.querySelector('#dm-list'); if(!wrap) return;
    let el=document.getElementById('dm-progress');
    if(!_dmProg){ if(el) el.remove(); return; }
    if(!el){ el=document.createElement('div'); el.id='dm-progress'; el.className='muted small'; el.style.padding='8px 10px'; wrap.prepend(el); }
    el.textContent=_dmProg;
  }
  let _dmWatching=false, _dmHistoryDrain=null, _dmHistoryReady=false, _dmHistoryPauseUntil=0;
  /* _dmHistoryHeld: wraps already looked up in DmCache and missed, waiting for a signer that may be
   * called. Kept OUT of the queue until it may — re-queued with every burst of arrivals, each one
   * paid another IndexedDB read per pass, so a 2000-wrap replay against a remote signer re-scanned
   * the whole held set again and again (review finding). */
  const _dmHistoryQueue=new Map(), _dmHistoryFailures=new Map(), _dmHistoryHeld=new Map();
  let _dmHistoryWake=null;
  // The shared-cache wait spares a REMOTE signer; a local key decrypts a miss in the worker for free.
  const _dmSignerOk=()=>(_dmHistoryReady || S.signer?.mode==='local') && Date.now()>=_dmHistoryPauseUntil;
  function _queueDmHistory(wraps){
    for(const ev of wraps){
      if(!ev?.id || _wrapTried.has(ev.id) || _dmHistoryHeld.has(ev.id)) continue;
      if((_dmHistoryFailures.get(ev.id)?.at || 0)>Date.now()) continue;
      _dmHistoryQueue.set(ev.id,ev);
    }
    if(!_dmHistoryDrain && _dmHistoryHeld.size && _dmSignerOk()){
      // The signer may be asked now: held wraps go back IN FRONT, in their order.
      const rest=[..._dmHistoryQueue]; _dmHistoryQueue.clear();
      for(const [id,ev] of [..._dmHistoryHeld, ...rest]) _dmHistoryQueue.set(id,ev);
      _dmHistoryHeld.clear();
    }
    if(!_dmHistoryDrain && _dmHistoryQueue.size){
      // External signers may be the same phone serving several clients. Leave room for live work.
      const slots=S.signer?.mode==='local'?6:2;
      /* WHAT WAITS IS THE SIGNER, NEVER THE CACHE.
       *
       * Reported as "Messages quickly loads a batch of old messages and takes forever to load the
       * latest". Two gates used to stop the WHOLE queue: history waited for pullShared (two relay
       * reads, a blob and a re-write of every record), and one wrap that did not come back as tried
       * paused everything for 30s — with nothing to resume it until the 60s watcher. A cache hit costs
       * no signer call, so neither gate has a reason to hold it. And a Concord invite (`k 3313`) never
       * enters `_wrapTried` at all, so the drain read every one as a failed signer: MEASURED, one
       * invite at the head of the queue held a 2002-message history at 7 for over 40 seconds, while
       * the same history without it painted in under 2. */
      const drain=async()=>{
        while(_dmHistoryQueue.size){
          const [id,ev]=_dmHistoryQueue.entries().next().value;
          _dmHistoryQueue.delete(id);
          const cord=ev.tags?.some(t=>t[0]==='k'&&t[1]==='3313');
          if(!cord && !_dmSignerOk() && !(await DmCache.get(id))){ _dmHistoryHeld.set(id,ev); continue; }
          try{ await ingestWrap(ev,false); }catch(_){}
          if(cord || _wrapTried.has(id)) _dmHistoryFailures.delete(id);
          else{
            // A failed signer must not be asked to try every remaining historical message. A local
            // key is not a signer that can be down: its throw is about that one wrap.
            if(S.signer?.mode!=='local') _dmHistoryPauseUntil=Date.now()+30000;
            const attempts=(_dmHistoryFailures.get(id)?.attempts || 0)+1;
            _dmHistoryFailures.set(id,{attempts,at:Date.now()+Math.min(300000,60000*2**Math.min(attempts-1,3))});
          }
        }
      };
      // Each worker advances independently; one stalled decrypt must not hold up the entire batch.
      _dmHistoryDrain=Promise.all(Array.from({length:slots},drain)).finally(()=>{
        _dmHistoryDrain=null;
        // Arrivals after the workers stopped have not been looked at; held wraps wait for the signer.
        if(_dmHistoryQueue.size || (_dmHistoryHeld.size && _dmSignerOk())) _queueDmHistory([]);
        else if(_dmHistoryHeld.size && _dmHistoryReady && !_dmHistoryWake){
          // One wake-up for the end of the pause — not one per call made during it.
          _dmHistoryWake=setTimeout(()=>{ _dmHistoryWake=null; _queueDmHistory([]); },
                                    Math.max(0,_dmHistoryPauseUntil-Date.now())+50);
        }
      });
    }
    return _dmHistoryDrain;
  }
  function _watchDMs(modern){
    if(_dmWatching) return;
    _dmWatching=true;
    // Relay subscriptions dedup even failed decryptions. Retry cached, unread wraps independently
    // so a temporary signer failure cannot hide a server notification for the rest of the session.
    setInterval(()=>{
      if(modern) _queueDmHistory(Store.byKind(1059));
      if(!S._dmLoaded) ensureDMs();
    },60000);
    // live sub for legacy DMs (since now is fine — kind-4 timestamps are real)
    const since=Math.floor(Date.now()/1000)-60;
    Relay.subscribe([{ kinds:[4], '#p':[S.ME.pubkey], since }, { kinds:[4], authors:[S.ME.pubkey], since }], {
      onEvent: ev => { Store.saveEvent(ev); if(ingestDM(ev) && ev.pubkey!==S.ME.pubkey && !isMutedAuthor(ev.pubkey)){ S._dmUnread++; bumpDm(); _dmNotify(ev.pubkey); }
        _scheduleDmRefresh(); }   // debounced: never rebuilds per-message (would thrash + drop the mobile overlay)
    });
    // NIP-17 gift wraps carry RANDOMIZED past timestamps, so a `since` filter would drop them —
    // subscribe with no `since` and let Store dedup skip the ones we've already unwrapped.
    if(modern){
      // Gift wraps carry RANDOMIZED past timestamps, so we can't `since`-filter the live sub — the
      // relay replays the WHOLE history on connect. Treat everything up to the initial EOSE as
      // backlog (ingest silently, NO notification); only wraps arriving AFTER EOSE are genuinely new.
      // Without this every historical DM fired a notification on login — the "flooded on login" bug.
      let _dmLive = false;
      Relay.subscribe([{ kinds:[1059], '#p':[S.ME.pubkey] }], {
        /* THE STORE'S DEDUP MUST NOT GATE THE UNWRAP. `saveEvent` answers false for an event the
         * Store already holds — which is true the moment this wrap has been SEEN, not when it has
         * been read. So a wrap whose first unwrap failed (a remote signer that timed out, a crypto
         * worker busy verifying the feed) was stored, skipped on every redelivery, and never tried
         * again: the message is invisible for the rest of the session. Reported as "Messages -> DM,
         * it did not load the new message from the user".
         *
         * `ingestWrap` owns this decision and always did: `_wrapTried` returns immediately for one
         * already in flight or done — so nothing is decrypted twice — and it DELETES that entry
         * when the unwrap throws, precisely so a redelivery can retry. Gating it here overrode the
         * retry it was designed to allow. */
        onEvent: async ev => { if(!_dmLive){ Store.saveEvent(ev); _queueDmHistory([ev]); return; } Store.saveEvent(ev); await ingestWrap(ev, _dmLive); },
        onEose: () => { _dmLive = true; }
      });
    }
  }
  async function ensureDMs(){
    if(S._dmLoaded || !S.ME?.pubkey) return; S._dmLoaded=true;
    const modern = !!(S.signer && S.signer.nip17unwrap);   // gift wraps need the local secret key
    _watchDMs(modern); // Live delivery must not wait for cache downloads or historical decryption.
    /* Replayed history's SIGNER work waits for the shared cache; what this device has already read
     * does not (see _queueDmHistory). Post-EOSE arrivals remain live throughout that download, so
     * fixing signer pressure does not bring back the missing-DM startup gap. */
    if(modern) _queueDmHistory(Store.byKind(1059));
    if(modern) try{ const n = await DmCache.pullShared();
                    if(n) console.info('[dm] ' + n + ' messages from the shared cache'); }catch(_){}
    _dmHistoryReady=true;
    _queueDmHistory([]);
    Store.byKind(4).forEach(ingestDM);                 // show cached legacy DMs instantly
    if(modern) _queueDmHistory(Store.byKind(1059));   // unwrap cached gift wraps (async)
    if(S.VIEW==='messages') renderMessages();
    /* THE HISTORY READ MUST WAIT FOR A SOCKET THAT CAN ANSWER — `await Relay.ready()`.
     *
     * A REQ written to a CONNECTING socket is silently dropped (relay.js `_send`), and this view is
     * the one that is opened INTO that window by construction: a launcher tile lands the moment
     * `pc-app-ready` fires, which is the same turn `connectRelays()` was called in — the socket
     * cannot be open yet. Reported as "when I click on Messages from the android launcher, it does
     * not load messages for me": the right screen, painted correctly, with an empty conversation
     * list and nothing in any log. The timeline, the profile, the notification flush and Trending
     * all already wait here; Messages never did.
     *
     * The LIVE subscriptions above are deliberately started first and are NOT gated on this: they
     * re-arm themselves when a socket opens (`Conn` re-sends a live sub's REQ on connect), so a
     * message arriving during the gap is still delivered. It is only the one-shot history read that
     * has nothing to re-arm it.
     *
     * Not ready in time is "I could not ask", never "you have no messages", so `_dmLoaded` is put
     * back and the read is left to be retried — by hydrateUser's post-onReady call, and by the
     * 60-second watcher in `_watchDMs` as the backstop. */
    if(!(await Relay.ready(8000).catch(()=>false))){
      S._dmLoaded=false;
      console.warn('[dm] no relay socket yet; history read deferred');
      return;
    }
    const filt=[{ kinds:[4], '#p':[S.ME.pubkey], limit:300 }, { kinds:[4], authors:[S.ME.pubkey], limit:300 }];
    if(modern) filt.push({ kinds:[1059], '#p':[S.ME.pubkey], limit:400 });
    let evs;
    try{ evs=await Relay.query(filt); }
    catch(error){ S._dmLoaded=false; console.warn('[dm] history read failed; live subscription remains active',error); return; }
    evs.forEach(e=>Store.saveEvent(e));
    evs.filter(e=>e.kind!==1059).forEach(ingestDM);
    // Unwrap the gift wraps NEWEST-FIRST and CONCURRENTLY. This loop used to `await` one wrap at a time,
    // which is invisible with a local key (the worker decrypts instantly) but brutal with a remote signer:
    // each wrap costs TWO round-trips to a phone, so a 500-message history was ~1000 serial round-trips —
    // tens of minutes, during which you see only the handful it has got through ("still only the same 6 DMs").
    // Queue a bounded batch at a time, newest-first; _scheduleDmRefresh paints messages as they land.
    /* NOT COUNTED HERE ANY MORE — ingestWrap counts itself, and it has to.
     *
     * This loop only ever knew about ONE of the three places wraps are decrypted. The cached pass
     * above it (`Store.byKind(1059)`, fired and not awaited) and the backfill drain below it were
     * both invisible to the number on screen, and they share the same six transport slots — so the
     * counter could sit still for a minute while the signer was working flat out on wraps this
     * loop had never heard of, or run to 400/400 while hundreds were still going. Once a wrap that
     * another pass already took returns instantly (_wrapTried), the two came apart completely.
     *
     * A progress number that is not measuring the work is worse than none: "70/400, this is not
     * moving" is then a true statement about the counter and says nothing about the restore. */
    const wraps=evs.filter(e=>e.kind===1059).sort((a,b)=>b.created_at-a.created_at);
    await _queueDmHistory(wraps);
    /* …and publish what this device just worked out, so the next one does not repeat it. Fire and
     * forget: it is a courtesy to the other devices, never something this screen waits for. */
    if(modern) DmCache.pushShared().catch(()=>{});
    // DRAIN THE REST OF THE HISTORY. The bulk query above caps at 400 wraps and the "unlimited" live
    // sub below is not actually unlimited: a filter with NO limit is capped by the relay (ours
    // defaults to 500), so a long history is silently truncated to the newest few hundred and older
    // conversations arrive half-empty or not at all. Page BACKWARDS with `until` until a page comes
    // back short. Runs in the background so the thread still opens instantly, and is bounded so a
    // huge inbox can't spin.
    if(modern) (async()=>{
      let until = wraps.length ? Math.min(...wraps.map(w=>w.created_at)) - 1 : Math.floor(Date.now()/1000);
      for(let page=0; page<40; page++){
        let batch=[];
        try{ batch = await Relay.query([{ kinds:[1059], '#p':[S.ME.pubkey], limit:500, until }]); }catch(_){ break; }
        if(!batch || !batch.length) break;
        const fresh = batch.filter(e=>Store.saveEvent(e));
        if(fresh.length){ await _queueDmHistory(fresh); _scheduleDmRefresh(); }
        const oldest = batch.reduce((a,e)=>Math.min(a, e.created_at||0), Infinity);
        if(!isFinite(oldest) || oldest > until) break;   // relay ignored `until` → stop rather than loop
        // INCLUSIVE cursor: several wraps can share a second, and `oldest - 1` skipped any that the
        // page limit cut off at that exact timestamp. Re-fetching the boundary is free (Store dedups),
        // and "no fresh events in a whole page" is the termination condition instead.
        until = oldest;
        if(!fresh.length) break;                         // nothing new → end of history (or a tie loop)
        if(batch.length < 500) break;                    // short page = end of history
      }
    })();

    // Persistent unread badge (like Notifications): count incoming DMs newer than the last time the
    // Messages view was opened, so DMs received while away still alert — not just live ones.
    recountDmUnread();
    if(S.VIEW==='messages') renderMessages();
  }
  // Unwrap a NIP-17 gift wrap (kind 1059) → its inner kind-14 chat rumor (already plaintext). `live`
  // bumps the unread badge for incoming (non-self) messages. (`_wrapTried` and the `_dmDone`/`_dmTotal`
  // counter are declared in app.js, just above this block — PC.dmStats reads them too.)
  function _dmTick(){
    if(S._dmDone >= S._dmTotal){ _dmProgress(S._dmTotal, S._dmTotal); return; }
    // Every 5, and always the first — a burst of instant skips must not hide the real ones behind it.
    if(S._dmDone % 5 === 0 || S._dmDone <= 1) _dmProgress(S._dmDone, S._dmTotal);
  }
  /* THE DM CACHE — one signer call per session instead of two per message, for ever.
   *
   * MEASURED, on the relay, while three clients were "stuck": 2769 kind-24133 in 110 seconds, with
   * requests and replies 1:1 in both directions (663 of 664, 655 of 655). Nothing was dropped and
   * nothing was deaf — the phone answered everything. It is THROUGHPUT: one message is two decrypts,
   * so a 400-message history is 800 round trips, and the client threw every result away on reload,
   * so all three devices paid it again on every visit. Reported as "stuck at 1/400", "not decrypting
   * anything", "worse than amber".
   *
   * So the plaintext is kept. The trade nobody should make is keeping it in the clear — these are
   * private messages sitting in a browser profile — and the trade nobody has to make is asking the
   * signer per message. The encrypted drive already solved exactly this: ONE key, wrapped to the
   * user, unwrapped once per session, everything else local AES. Same shape here.
   *
   *   `pcai:dmkey`  a kind-30078 doc, NIP-44 to the user's own key, holding 32 random bytes. One
   *                 nip44dec per session. PINNED in both `_isPinned` (store.js) and `_CARRY_D` —
   *                 every private doc here has missed one of those at least once, silently.
   *   the cache     IndexedDB `pc-dm-v1`, one record per gift-wrap id, AES-GCM under that key with
   *                 a fresh IV each. A gift wrap is immutable, so a hit is never stale and there is
   *                 no invalidation to get wrong.
   *
   * IT IS A CACHE AND IT FAILS AS ONE. Every path returns null rather than throwing: no key, no
   * IndexedDB (a private window has none), a corrupt record — all fall through to the signer, which
   * is exactly today's behaviour. What it must NEVER do is mint a SECOND key: a silent relay or an
   * undecryptable doc would publish a new one over the old, and every cached message on every device
   * becomes unreadable. Both are refused, the way the agent-task doc and the desktop layout refuse
   * them.
   *
   * The key SYNCS (it is an ordinary private doc); the cache itself is per device, so a second
   * device pays the round trips once and then never again. Mirroring the decrypted history ITSELF
   * across devices is the next step and deliberately not started here: it is the Files shape (an
   * index doc plus an encrypted Blossom blob, because a 30078 cannot hold it — NIP-44 stops at
   * 65535 bytes), and it puts a second copy of the most private thing in the app on the relay. That
   * is worth doing carefully rather than on the same afternoon as the cache. */
  const DmCache = {
    D: 'pcai:dmkey', _dbP: null, _key: null, _keyP: null, _offUntil: 0, _puts: 0,
    _dead(){ return Date.now() < this._offUntil; },
    /* One failure must not become one failure PER MESSAGE. 400 wraps calling get() while the signer
     * is down would be 400 more requests at the thing that is already struggling. */
    _fail(){ this._offUntil = Date.now() + 60000; this._keyP = null; return null; },
    _idb(){
      if(this._dbP) return this._dbP;
      this._dbP = new Promise((res, rej) => {
        let r; try{ r = indexedDB.open('pc-dm-v1', 1); }catch(e){ return rej(e); }
        r.onupgradeneeded = () => { try{ r.result.createObjectStore('msgs'); }catch(_){} };
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error || new Error('indexeddb'));
      });
      return this._dbP;
    },
    // Per ACCOUNT, in the key: one device, two identities, and neither may read the other's cache.
    _k(id){ return ((S.ME && S.ME.pubkey) || 'anon').slice(0, 16) + ':' + id; },
    async _mk(){
      if(this._key) return this._key;
      if(this._dead()) return null;
      if(!this._keyP) this._keyP = this._loadKey();
      try{ return await this._keyP; }catch(_){ return this._fail(); }
    },
    async _loadKey(){
      if(!S.ME || !S.ME.pubkey || !S.signer || !S.signer.nip44dec) throw new Error('no signer');
      /* THE LOCAL COPY FIRST. The doc is pinned in the Store, so offline it is already here; asking the
       * relays first made the Messages screen wait ~12s for an answer it held all along. */
      const flt = { authors:[S.ME.pubkey], kinds:[30078], '#d':[this.D], limit:1 };
      const newest = list => (list || []).filter(e => e && e.content).sort((x, y) => y.created_at - x.created_at)[0] || null;
      let ev = null, sawRelay = false;
      try{ ev = newest(typeof Store !== 'undefined' && Store.query ? Store.query([flt]) : []); }catch(_){ ev = null; }
      for(let a = 0; a < 2 && !ev; a++){
        if(a) await new Promise(r => setTimeout(r, 400));
        try{
          const evs = await Relay.query([flt]);
          /* AN ANSWER IS A COMPLETE QUERY, NOT A RESOLVED ONE. Relay.query resolves [] when it times
           * out (`complete === false`), and reading that as "you have no key" minted a replacement —
           * on a flaky link even published it — leaving every other device's cache unreadable. */
          if((evs && evs.length) || (evs && evs.complete === true)) sawRelay = true;
          ev = newest(evs);
        }catch(_){}
      }
      // "Nobody answered" is not "you have no key" — minting one here is how every cached message on
      // every other device would become unreadable.
      if(!ev && !sawRelay) throw new Error('relays silent');
      let hex = '';
      if(ev){
        if(typeof ev.content!=='string' || !ev.content) throw new Error('DM cache key event is empty or corrupt');
        hex = String(await S.signer.nip44dec(S.ME.pubkey, ev.content) || '').trim();
        if(!/^[0-9a-f]{64}$/i.test(hex)) throw new Error('key doc is not a key');
      }else{
        const b = new Uint8Array(32); crypto.getRandomValues(b);
        hex = [...b].map(x => x.toString(16).padStart(2, '0')).join('');
        const ct = await S.signer.nip44enc(S.ME.pubkey, hex);
        const r = await publish(30078, ct, [['d', this.D]], { quiet:true });
        if(!(r && r.ok)) throw new Error('the key could not be stored');
      }
      const raw = new Uint8Array(hex.match(/../g).map(h => parseInt(h, 16)));
      this._key = await crypto.subtle.importKey('raw', raw, { name:'AES-GCM' }, false,
                                                ['encrypt', 'decrypt']);
      return this._key;
    },
    /* THE SAME DEVICE KEY, LENT. Email kept offline (mail.js MailCache) is as private as a DM and has
     * the same answer: AES-GCM under this one key, never plaintext in the profile. Null = could not. */
    async seal(obj){
      try{
        const key = await this._mk(); if(!key) return null;
        const iv = new Uint8Array(12); crypto.getRandomValues(iv);
        const ct = await crypto.subtle.encrypt({ name:'AES-GCM', iv }, key, new TextEncoder().encode(JSON.stringify(obj)));
        return { iv, ct:new Uint8Array(ct) };
      }catch(_){ return null; }
    },
    async open(rec){
      try{
        if(!rec || !rec.iv || !rec.ct) return null;
        const key = await this._mk(); if(!key) return null;
        const pt = await crypto.subtle.decrypt({ name:'AES-GCM', iv:new Uint8Array(rec.iv) }, key, new Uint8Array(rec.ct));
        return JSON.parse(new TextDecoder().decode(pt));
      }catch(_){ return null; }
    },
    async get(id){
      if(this._dead()) return null;
      try{
        const key = await this._mk(); if(!key) return null;
        const db = await this._idb();
        const rec = await new Promise((res, rej) => {
          const q = db.transaction('msgs', 'readonly').objectStore('msgs').get(this._k(id));
          q.onsuccess = () => res(q.result || null); q.onerror = () => rej(q.error);
        });
        if(!rec || !rec.iv || !rec.ct) return null;
        const pt = await crypto.subtle.decrypt({ name:'AES-GCM', iv:new Uint8Array(rec.iv) },
                                               key, new Uint8Array(rec.ct));
        const r = JSON.parse(new TextDecoder().decode(pt));
        return (r && (r.kind === 14 || r.kind === 15)) ? r : null;
      }catch(_){ return null; }
    },
    async put(id, rumor){
      if(this._dead()) return;
      try{
        const key = await this._mk(); if(!key) return;
        const db = await this._idb();
        const iv = new Uint8Array(12); crypto.getRandomValues(iv);
        const ct = await crypto.subtle.encrypt({ name:'AES-GCM', iv }, key,
                                               new TextEncoder().encode(JSON.stringify(rumor)));
        await new Promise((res, rej) => {
          const q = db.transaction('msgs', 'readwrite').objectStore('msgs')
                      .put({ iv:[...iv], ct:[...new Uint8Array(ct)] }, this._k(id));
          q.onsuccess = () => res(); q.onerror = () => rej(q.error);
        });
        /* A BOUND, checked cheaply. This grows with the user's own history and each record is a few
         * hundred bytes, so it is small for years — but "small for years" is not a limit, and the
         * browser enforcing a quota instead would take the OTHER caches down with it. Emptying a
         * cache costs one re-decrypt pass and nothing else, which is why the crude answer is right. */
        if(++this._puts % 500 === 0) this._capacity(db);
      }catch(_){}
    },
    _capacity(db){
      try{
        const st = db.transaction('msgs', 'readwrite').objectStore('msgs');
        const c = st.count();
        c.onsuccess = () => { if((c.result || 0) > 50000) try{ st.clear(); }catch(_){} };
      }catch(_){}
    },
    /* ---- ONE DEVICE DOES THE WORK, THE REST READ IT -------------------------------------------
     *
     * The per-device cache fixes the SECOND visit. It does nothing for the first, and with three
     * clients that is three separate 900-round-trip passes at one phone — measured at ~7.7
     * request/reply pairs a second in total, shared between them. "tablet was fast and good,
     * windows slowly showing DMs, firefox 1/400": one signer, three queues, all correct and all
     * unbearable.
     *
     * So the cache itself syncs, the way Files does: ONE encrypted blob on Blossom plus a small
     * pointer document. A device that has never decrypted anything reads the pointer, fetches the
     * blob and unwraps it with the key it already has — a relay read, an HTTP GET and ZERO signer
     * round trips for the whole history.
     *
     * A 30078 could not hold it (NIP-44 stops at 65535 bytes and this is hundreds of messages), and
     * a blob is what this codebase already uses at that size — the files index and the folder-sync
     * manifest both do exactly this.
     *
     * The POINTER is plaintext {sha, n, at}: the blob it names is AES-GCM under the same key nobody
     * but this user can unwrap, and the only thing the pointer itself discloses is how many messages
     * are cached — which the relay already knows, because it is holding the gift wraps. Keeping it
     * plaintext saves a signer round trip on the one path whose entire purpose is not to need one. */
    SHARE_D: 'pcai:dmcache', _sharedPulled: false, _pushedAt: 0,

    async pullShared(){
      if(this._sharedPulled || this._dead()) return 0;
      this._sharedPulled = true;
      try{
        const key = await this._mk(); if(!key) return 0;
        const evs = await Relay.query([{ authors:[S.ME.pubkey], kinds:[30078], '#d':[this.SHARE_D], limit:1 }]);
        const ev = (evs || []).sort((a, b) => b.created_at - a.created_at)[0];
        if(!ev) return 0;
        let ptr = null; try{ ptr = JSON.parse(ev.content || '{}'); }catch(_){ return 0; }
        const sha = String((ptr && ptr.sha) || '');
        if(!/^[0-9a-f]{64}$/i.test(sha)) return 0;
        const srv = mediaServer(); if(!srv) return 0;
        const r = await fetch(srv + '/' + sha);
        if(!r.ok) return 0;
        const buf = new Uint8Array(await r.arrayBuffer());
        if(buf.length < 13) return 0;
        const pt = await crypto.subtle.decrypt({ name:'AES-GCM', iv: buf.slice(0, 12) },
                                               key, buf.slice(12));
        const map = JSON.parse(new TextDecoder().decode(pt));
        const db = await this._idb();
        /* SKIP WHAT THIS DEVICE ALREADY HOLDS. Every session re-wrote the WHOLE shared history, one
         * encrypt and one transaction per record, and every message this device had not read yet
         * waited behind it — measured, the newest DM painted 2.2s after the cached ones over a slow
         * relay. A record keyed by a gift-wrap id is immutable, so a held key is already the answer. */
        let have = new Set();
        try{
          const pre = this._k('');
          const keys = await new Promise((res, rej) => {
            const q = db.transaction('msgs', 'readonly').objectStore('msgs')
                        .getAllKeys(IDBKeyRange.bound(pre, pre + '\uffff'));
            q.onsuccess = () => res(q.result || []); q.onerror = () => rej(q.error);
          });
          have = new Set(keys.map(k => String(k).slice(pre.length)));
        }catch(_){}
        let n = 0;
        for(const id in map){
          const rumor = map[id];
          if(!rumor || (rumor.kind !== 14 && rumor.kind !== 15)) continue;
          if(!have.has(id)) await this.put(id, rumor);   // re-encrypted per record, same key
          n++;
        }
        return n;
      }catch(_){ return 0; }
    },

    /* Publish what this device holds. Called after a pass finishes, at most every few minutes.
     *
     * REFUSES TO SHRINK. The pointer carries `n`, so a device whose cache is smaller than what is
     * already published does not replace it — the same rule the drive index and the folder-sync
     * manifest have, for the same reason: an empty or partial read must never become the new truth
     * for every other device. pullShared() runs first on every client, so the normal state is that
     * what we hold is a superset of what is published. */
    async pushShared(){
      if(this._dead()) return false;
      if(Date.now() - this._pushedAt < 300000) return false;
      try{
        const key = await this._mk(); if(!key) return false;
        const srv = mediaServer(); if(!srv) return false;
        const db = await this._idb();
        const pre = ((S.ME && S.ME.pubkey) || 'anon').slice(0, 16) + ':';
        const map = {};
        let held = 0;
        /* YOU CANNOT AWAIT INSIDE A CURSOR WALK, and this did.
         *
         * An IndexedDB transaction stays alive only while it has a pending request. The moment an
         * `async` onsuccess yields — here on `crypto.subtle.decrypt` — there is none, so the
         * transaction AUTO-COMMITS; when the await resolves, `c.continue()` is being called on a
         * cursor whose transaction has finished and it throws
         * `Failed to execute 'continue' on 'IDBCursor'`.
         *
         * Every part of what follows is quiet. The throw happens inside an async handler nobody
         * awaits, so it is not an error anyone catches — it is an UNHANDLED REJECTION, which this
         * app turns into a toast reading "action failed" (see the listener near healNav). pushShared
         * runs on startup, so a user with anything in the DM cache got that on every single launch,
         * with no clue what action was meant. The loop also stopped at the first record, so the
         * shared cache was never actually pushed — a silent feature outage wearing a scary toast.
         *
         * The fix is the standard shape: COLLECT synchronously while the transaction is alive, then
         * do the async work after it has closed. Nothing is held that was not already in memory as
         * a decrypted map a moment later. */
        const rawRecs = [];
        await new Promise((res, rej) => {
          const q = db.transaction('msgs', 'readonly').objectStore('msgs').openCursor();
          q.onsuccess = () => {                       // NOT async — see above
            const c = q.result;
            if(!c){ res(); return; }
            if(String(c.key).startsWith(pre)) rawRecs.push({ k: String(c.key), rec: c.value });
            c.continue();
          };
          q.onerror = () => rej(q.error);
        });
        for(const { k, rec } of rawRecs){
          try{
            const pt = await crypto.subtle.decrypt({ name:'AES-GCM', iv:new Uint8Array(rec.iv) },
                                                   key, new Uint8Array(rec.ct));
            map[k.slice(pre.length)] = JSON.parse(new TextDecoder().decode(pt));
            held++;
          }catch(_){}
        }
        if(!held) return false;
        // What is already out there? Never replace more than we hold.
        let have = 0, oldSha = '';
        try{
          const evs = await Relay.query([{ authors:[S.ME.pubkey], kinds:[30078], '#d':[this.SHARE_D], limit:1 }]);
          const ev = (evs || []).sort((a, b) => b.created_at - a.created_at)[0];
          if(ev){ const p = JSON.parse(ev.content || '{}'); have = Number(p && p.n) || 0;
                  oldSha = /^[0-9a-f]{64}$/i.test(String(p && p.sha || '')) ? String(p.sha).toLowerCase() : ''; }
        }catch(_){ return false; }          // could not ask → do not write
        /* ONLY WHEN THERE IS SOMETHING NEW, AND NOT FROM EVERY WINDOW. This used to push whenever it
         * held at least as much as was published — i.e. also when NOTHING had changed — and every
         * push is a fresh encryption (new IV) of the whole cache: a brand-new ~1.5MB blob, uploaded
         * `keep` and never let go of. The five-minute limit lived on the page, and on PosterChanOS
         * every app window is a page. Measured on one account: ~400 copies, 730MB, in two days.
         * Now: strictly more than is published, and at most every 30 minutes across ALL of this
         * browser's windows unless a lot has arrived. */
        if(held <= have) return false;
        const stampKey = 'pc_dmcache_push_' + String(S.ME.pubkey).slice(0, 16);
        let last = 0; try{ last = Number(localStorage.getItem(stampKey)) || 0; }catch(_){ }
        if(Date.now() - last < 1800000 && held - have < 50) return false;
        try{ localStorage.setItem(stampKey, String(Date.now())); }catch(_){ }
        const iv = new Uint8Array(12); crypto.getRandomValues(iv);
        const ct = new Uint8Array(await crypto.subtle.encrypt({ name:'AES-GCM', iv }, key,
                                    new TextEncoder().encode(JSON.stringify(map))));
        const body = new Uint8Array(12 + ct.length); body.set(iv, 0); body.set(ct, 12);
        const url = await uploadBlob(new File([body], 'dmcache.enc',
                                              { type:'application/octet-stream' }), { keep:true, noCompress:true });
        const sha = _shaFromUrl(url);
        if(!sha) return false;
        await publish(30078, JSON.stringify({ sha, n: held, at: Math.floor(Date.now()/1000) }),
                      [['d', this.SHARE_D]], { quiet:true });
        this._pushedAt = Date.now();
        /* LET GO OF THE COPY THIS ONE REPLACES. It is a CACHE — every message in it is still a gift
         * wrap on the relays — so a device that was mid-read of the old copy loses nothing but a
         * faster first load (pullShared answers 0 and the history is decrypted as before). Only
         * after the new pointer is published, and only this account's reference: the bytes go
         * with their last owner. */
        if(oldSha && oldSha !== String(sha).toLowerCase()){
          try{ const P = window.__PC; if(P && P.deleteBlobQuiet) await P.deleteBlobQuiet(oldSha); }catch(_){ }
        }
        return true;
      }catch(_){ return false; }
    },

    forget(){
      this._key = null; this._keyP = null; this._dbP = null; this._offUntil = 0;
      this._sharedPulled = false; this._pushedAt = 0;
      try{ indexedDB.deleteDatabase('pc-dm-v1'); }catch(_){}
    },
  };

  // Armada/0xChat encrypted NIP-17 attachments. Metadata is inside the decrypted rumor: kind 14
  // uses NIP-94 `imeta` fields, while kind 15 puts the same fields in top-level tags. Never trust
  // Blossom's Content-Type here: encrypted blobs commonly retain image/png and are ciphertext.
  function _dmKeyBytes(s){
    s=String(s||'').trim();
    if(/^[0-9a-f]+$/i.test(s) && !(s.length&1)) return new Uint8Array(s.match(/../g).map(x=>parseInt(x,16)));
    try{ const b=atob(s.replace(/-/g,'+').replace(/_/g,'/')); return Uint8Array.from(b,x=>x.charCodeAt(0)); }catch(_){ return null; }
  }
  function _dmEncOf(fields){
    if(!fields['encryption-algorithm']) return null;
    return { algorithm:String(fields['encryption-algorithm']).toLowerCase(),
      key:String(fields['decryption-key']||''), nonce:String(fields['decryption-nonce']||''),
      ox:/^[0-9a-f]{64}$/i.test(fields.ox||'')?String(fields.ox).toLowerCase():'' };
  }
  function _dmAttachmentMeta(r){
    const out=[];
    if(!r || !Array.isArray(r.tags)) return out;
    if(r.kind===15){
      const f={}; for(const t of r.tags){ if(t&&t[0]&&t[1]!=null && f[t[0]]==null) f[t[0]]=t[1]; }
      if(/^https?:\/\//i.test(r.content||'')) out.push({url:r.content,mime:f['file-type']||f.m||'',name:f.name||'',enc:_dmEncOf(f)});
      return out;
    }
    for(const t of r.tags){
      if(!t || t[0]!=='imeta') continue;
      const f={}; for(const p of t.slice(1)){ const n=String(p).indexOf(' '); if(n>0) f[p.slice(0,n)]=p.slice(n+1); }
      if(/^https?:\/\//i.test(f.url||'')) out.push({url:f.url,mime:f.m||'',name:f.name||'',enc:_dmEncOf(f)});
    }
    return out;
  }
  const _dmAttUrls=new Map();
  async function _dmDecryptAttachment(a){
    if(!a || !a.enc) throw new Error('not encrypted');
    const key=_dmKeyBytes(a.enc.key), iv=_dmKeyBytes(a.enc.nonce);
    if(a.enc.algorithm!=='aes-gcm' || !key || key.length!==32 || !iv || !iv.length) throw new Error('unsupported encryption');
    const r=await fetch(a.url,{cache:'force-cache'}); if(!r.ok) throw new Error('attachment http '+r.status);
    const declared=Number(r.headers.get('content-length')||0); if(declared>64*1024*1024) throw new Error('attachment too large');
    const ct=await r.arrayBuffer(); if(ct.byteLength>64*1024*1024) throw new Error('attachment too large');
    const pt=await crypto.subtle.decrypt({name:'AES-GCM',iv},await crypto.subtle.importKey('raw',key,{name:'AES-GCM'},false,['decrypt']),ct);
    if(a.enc.ox){ const h=new Uint8Array(await crypto.subtle.digest('SHA-256',pt));
      const hex=[...h].map(x=>x.toString(16).padStart(2,'0')).join(''); if(hex!==a.enc.ox) throw new Error('attachment hash mismatch'); }
    return new Blob([pt],{type:a.mime||'application/octet-stream'});
  }
  function _dmFind(mid){ for(const arr of dmPeers.values()){ const m=arr.find(x=>x.id===mid); if(m)return m; } return null; }
  async function _decorateDmFileAtts(root){
    if(!root)return;
    for(const el of root.querySelectorAll('[data-dm-file]:not([data-ready])')){
      el.dataset.ready='1'; const m=_dmFind(el.dataset.dmFile), a=m&&m.atts&&m.atts[Number(el.dataset.ai)||0]; if(!a)continue;
      try{
        // Armada uses the same kind-14/kind-15 attachment shapes for plaintext and encrypted files.
        // Encryption is present only when `encryption-algorithm` is declared; routing every file
        // through the decryptor made an ordinary link fail with the false message "Attachment could
        // not be decrypted". Keep declared encryption fail-closed, but use a validated plaintext URL
        // directly when no encryption metadata exists.
        let u=a.url;
        if(a.enc){
          u=_dmAttUrls.get(m.id+':'+el.dataset.ai);
          if(!u){ const b=await _dmDecryptAttachment(a); u=URL.createObjectURL(b); _dmAttUrls.set(m.id+':'+el.dataset.ai,u);
            if(_dmAttUrls.size>64){ const k=_dmAttUrls.keys().next().value; URL.revokeObjectURL(_dmAttUrls.get(k)); _dmAttUrls.delete(k); } }
        }
        const mime=String(a.mime||'');
        if(/^image\//i.test(mime)) el.outerHTML=`<img class="dm-file-media" src="${enc(u)}" alt="${enc(a.name||'attachment')}">`;
        else if(/^video\//i.test(mime)) el.outerHTML=`<video class="dm-file-media" src="${enc(u)}" controls playsinline></video>`;
        else el.outerHTML=`<a class="dm-file-att" href="${enc(u)}" target="_blank" rel="noopener noreferrer">📎 ${enc(a.name||'Open attachment')}</a>`;
      }catch(err){ el.textContent='🔒 Attachment could not be decrypted'; el.classList.add('bad'); }
    }
  }

  function cordDirectContext(){
    const account=S.ME?.pubkey, identity=S.signer;
    if(!account || !identity?.nip44dec || !identity?.nip44enc) throw new Error('Sign in to use Concord invitations');
    return {pubkey:account,isCurrent:()=>S.ME?.pubkey===account && S.signer===identity,
      verify:async events=>{const checked=await Relay.worker.call('verifyBatch',{events});const ids=new Set(checked.filter(r=>r.valid).map(r=>r.id));return events.filter(e=>ids.has(e.id));},
      decrypt:(peer,text)=>identity.nip44dec(peer,text),encrypt:(peer,text)=>identity.nip44enc(peer,text),
      sign:template=>identity.signEvent(template),
      wrapSeal:(seal,recipient,metadata)=>Relay.worker.call('giftwrapSeal',{seal,recipient,...metadata}).then(r=>r.wrap)};
  }
  async function cordDirectModule(){
    await _withModule('cord-reader.js','PosterCordReader');
    const api=await _withModule('cord-direct-invites.js','PCCordDirectInvites');
    if(!api) throw new Error('Concord invitations could not load');
    return api;
  }
  async function cordInviteLinksModule(){
    await _withModule('cord-reader.js','PosterCordReader');
    await _withModule('cord-protocol.js','PosterCord');
    const api=await _withModule('cord-invite-links.js','PCCordInviteLinks');
    if(!api)throw new Error('Concord link management could not load');
    return api;
  }
  async function sendCordDirectInvite(bundle,recipientRef,options){
    const base=cordDirectContext(),context={...base,isCurrent:()=>base.isCurrent()&&(!options?.isCurrent||options.isCurrent())},recipient=refToPk(String(recipientRef||'').trim());
    if(!context.isCurrent())throw new Error('Invitation permission changed');
    if(!recipient)throw new Error('Select an account or paste its npub');
    const api=await cordDirectModule();
    if(!context.isCurrent())throw new Error('Invitation permission changed');
    const delivery=await dmInboxRelays(recipient);
    if(!context.isCurrent())throw new Error('The signed-in account changed');
    if(!delivery.relays.length)throw new Error('No receiving relays found for this account');
    const result=await api.create(bundle,recipient,context);
    if(!context.isCurrent())throw new Error('The signed-in account changed');
    const sent=await Relay.publishTo(delivery.relays,result.wrap,{includeManaged:true,detailed:true,max:8});
    if(!context.isCurrent())throw new Error('The signed-in account changed');
    if(!sent?.ok)throw new Error(sent?.uncertain?'Invitation delivery is not confirmed':'No relay accepted the invitation');
    return result.wrap.id;
  }
  const _cordDirectBusy=new Set();
  async function _ingestCordDirectWrap(wrap,live=false){
    const context=cordDirectContext(),key=context.pubkey+':'+wrap?.id;
    if(_cordDirectBusy.has(key)||_cordDirectBusy.size>=32)return false;
    _cordDirectBusy.add(key);
    try{
      const api=await cordDirectModule(),added=await api.park(wrap,context);
      if(added && context.isCurrent()){
        window.dispatchEvent(new CustomEvent('pc-concord-direct-invites'));
        if(live)toast('New Concord invitation — open Concord to review it');
      }
      return true;
    }finally{_cordDirectBusy.delete(key);}
  }
  let _cordDirectClose=null;   // `_cordDirectOwner` is app.js's (boot resets it when the inbox fails)
  async function _startCordDirectInbox(){
    const context=cordDirectContext();
    if(S._cordDirectOwner===context.pubkey)return;
    if(_cordDirectClose)_cordDirectClose();
    _cordDirectClose=null;S._cordDirectOwner=context.pubkey;
    const filters=[{kinds:[1059],'#p':[context.pubkey],'#k':['3313'],limit:32}];
    let live=false;
    const handlers={onEvent:event=>{if(context.isCurrent())_ingestCordDirectWrap(event,live).catch(()=>{});},onEose:()=>{live=true;}};
    const pool=Relay.subscribe(filters,handlers);let external=null;
    _cordDirectClose=()=>{Relay.close(pool);if(external)external();};
    const delivery=await dmInboxRelays(context.pubkey);
    if(!context.isCurrent())return;
    if(delivery.relays.length && Relay.subscribeFrom)external=Relay.subscribeFrom(delivery.relays,filters,{...handlers,live:true,max:8});
  }

  async function ingestWrap(ev, live){
    if(!S.signer || !S.signer.nip17unwrap) return false;
    if(!ev || !ev.id || _wrapTried.has(ev.id)) return false;
    if(ev.tags?.some(t=>t[0]==='k'&&t[1]==='3313'))return _ingestCordDirectWrap(ev,live);
    _wrapTried.add(ev.id);
    S._dmTotal++;
    /* THE CACHE FIRST, and that is the whole point of it: a hit costs no signer round trip at all,
     * where a miss costs two. Never fatal — see DmCache. */
    let rumor = await DmCache.get(ev.id);
    if(!rumor){
      try{ rumor=await S.signer.nip17unwrap(ev); }
      catch(_){ _wrapTried.delete(ev.id); S._dmDone++; _dmTick(); return false; }
      if(rumor && (rumor.kind === 14 || rumor.kind === 15)) DmCache.put(ev.id, rumor);
    }
    S._dmDone++; _dmTick();
    if(rumor?.kind===3313){try{return await _ingestCordDirectWrap(ev,live);}catch(_){_wrapTried.delete(ev.id);return false;}}
    if(!rumor || (rumor.kind!==14 && rumor.kind!==15) || rumor.content==null) return false;
    const mine = rumor.pubkey===S.ME.pubkey;
    const peer = mine ? (rumor.tags.find(t=>t[0]==='p')||[])[1] : rumor.pubkey;
    if(!peer) return false; needProfile(peer);
    if(!dmPeers.has(peer)) dmPeers.set(peer, []);
    const arr=dmPeers.get(peer); if(arr.find(m=>m.id===ev.id)) return false;
    const atts=_dmAttachmentMeta(rumor);
    arr.push({ id:ev.id, mine, text:rumor.kind===15?'':rumor.content, t:rumor.created_at, nip17:true, atts,
               /* The peer wrap and our self-copy have different outer ids. WebXDC must use the
                  shared INNER rumor id or the two recipients derive different game sessions. */
               xdcMessageId:rumor.id||ev.id, tags:(rumor.tags||[]).map(t=>t.slice()),
               em:(rumor.tags||[]).filter(t=>t[0]==='emoji') }); arr.sort((a,b)=>a.t-b.t);
    // COUNT on freshness, not on `live`. `live` is a network signal (the sub's EOSE) and it can never
    // arrive — a relay in the user's list that is down/DNS-dead is still counted in the pool, so the
    // "everyone EOSE'd" test is never met and every incoming DM was classified as login backlog: no
    // count, no badge, ever. (relay.js now backstops that, but the badge must not depend on it at all.)
    // Newer than the last time Messages was opened IS the definition of unread — the same test
    // recountDmUnread and the per-peer unread dot already use, so all three now agree.
    const _seen = Number(ClientSettings.get('dmSeen', 0)) || 0;
    // A NOTE TO SELF counts too. The server sends system notifications (agent run finished, uptime
    // alerts) from the node's operator key — which on a single-admin install IS your key, so they
    // arrive as you→you. Skipping every `mine` message meant those published, decrypted, and notified
    // nobody. This does NOT badge what you compose here: sendDm ingests its own `toSelf` wrap up
    // front, so the relay's echo is deduped above before it can reach this. What's left is the
    // arriving ones — server notifications, and your own notes from another device.
    const selfNote = mine && peer === S.ME.pubkey;
    if((!mine || selfNote) && !isMutedAuthor(peer) && (rumor.created_at||0) > _seen){
      S._dmUnread++; bumpDm();
      // The interrupting toast/OS notification stays gated on `live`, so restoring a backlog on login
      // doesn't fire a burst of them. It names the SENDER only, never the message — so it says the same
      // thing whether or not "Hide DM previews until opened" is on.
      if(live) _dmNotify(selfNote ? null : peer, selfNote, selfNote ? rumor.content : '');
    }
    _scheduleDmRefresh();
    return true;
  }
  // ---------- NIP-17 DM relay list (kind 10050) — discovery + outbox delivery ----------
  // The relays where WE receive gift-wrapped DMs: our own list when enabled, else the built-in relay.
  // Other clients (0xchat/Amethyst/Coracle) read our kind-10050 to know where to deliver DMs to us.
  // With no instance at all, CFG.relay_url is undefined and this returned [] — an EMPTY inbox list,
  // which is the one answer that must never be published (nobody could deliver us a DM again). Fall
  // back to the same defaults connectRelays() dialled, so the list always names somewhere real.
  function myInboxRelays(){
    let list = ClientSettings.get('relaysEnabled') ? userRelays() : [];
    if(!list.length) list = (S.CFG && S.CFG.relay_url) ? [S.CFG.relay_url] : defaultRelays().filter(Boolean);
    return [...new Set(list.map(u=>normalizeRelay(u)).filter(Boolean))];
  }
  // Publish our kind-10050 DM-inbox list so other clients can discover where to gift-wrap-DM us. Called
  // LAZILY — the first time the user actually uses DMs (opens Messages / sends a DM), NOT on login: a
  // silent login-time write surprised users ("why did it change my relay list just for logging in?").
  //
  /* A DM RELAY LIST SOMEBODY ALREADY HAS IS NEVER EDITED. THIS ONLY EVER CREATES A FIRST ONE.
   *
   * "npub14w4q… is saying we changed his DM relays" -- and we had. His list (0xchat, keychat, nostr21,
   * signed 2026-05-29 by another client) lived on his own relays; this function read the existing list
   * from OUR pool alone, which never held it, "merged" our relay into nothing and published a list
   * naming ONLY wss://poster.place/relay -- then pushed it to the indexers, so every client that
   * delivers NIP-17 DMs by the book sent his DMs to a relay he never chose. A replaceable list is the
   * user's whole decision; one empty read is not evidence there is no decision.
   *
   * So: look on our pool, the indexers AND the user's own relays (their kind-10002 + the relays set
   * here). ANY list found, from any client, is left exactly as it is -- even adding our relay to it
   * is a change nobody asked for. A first list is published only when that search FOUND NOTHING and at
   * least two relays actually ANSWERED; "could not ask" leaves it for a later session. */
  let _dmInboxEnsured = false;
  async function ensureDmInboxList(){
    if(_dmInboxEnsured || S.GUEST) return; _dmInboxEnsured = true;   // once per session, on first DM use
    try{
      const want = myInboxRelays(); if(!want.length) return;
      const me = S.ME.pubkey;
      const mine = evs => (evs||[]).filter(e=>e && e.pubkey===me && (e.kind===10050 || e.kind===10002));
      const local = mine(await Relay.query([{ authors:[me], kinds:[10050,10002], limit:2 }]));
      if(local.some(e=>e.kind===10050)) return;                         // they already have one: hands off
      const own = local.filter(e=>e.kind===10002).flatMap(e=>e.tags.filter(t=>t[0]==='r'&&t[1]).map(t=>t[1]));
      const ask = [...new Set([...DISCOVERY_RELAYS, ...own, ...want].map(u=>normalizeRelay(u)).filter(Boolean))];
      const report = {};
      let ext = mine(await Relay.queryFrom(ask, [{ authors:[me], kinds:[10050], limit:1 }],
        {purpose:'own dm inbox check', report, allowBlocked:true, max:ask.length}));
      if(ext.length){ try{ const v=await Relay.worker.call('verifyBatch',{events:ext});
        const ok=new Set(v.filter(r=>r.valid).map(r=>r.id)); ext=ext.filter(e=>ok.has(e.id)); }catch(_){ } }
      if(ext.some(e=>e.kind===10050)) return;                           // found elsewhere: hands off
      if((report.ok||[]).length < 2){ _dmInboxEnsured = false; return; } // nobody answered: not evidence
      const made = await publish(10050, '', want.map(u=>['relay', u]));
      /* AND PUT IT WHERE THE PEOPLE WHO NEED IT ACTUALLY LOOK. `publish` reaches our own pool; every
       * other client resolves a DM inbox from the relay-list INDEXERS. Backgrounded and best-effort. */
      if(made && made.ev) Relay.publishTo(DISCOVERY_RELAYS, made.ev, {max:DISCOVERY_RELAYS.length})
        .catch(()=>{});
    }catch(_){ _dmInboxEnsured = false; }   // let a later DM-use retry after a transient failure
  }
  // (DISCOVERY_RELAYS — the indexers asked for a stranger's inbox list — is declared in app.js, just
  // above this block: own-profile discovery and payment-target discovery use it too.)
  // A peer's DM-inbox relays (their kind-10050), lazily fetched + cached (1h TTL); falls back to their
  // NIP-65 read relays (kind 10002). Tries our pool first (has it for WoT members), then external
  // discovery relays for strangers (our WoT-only relay never stored those). Looked up ONLY when
  // sending to a not-yet-cached peer — never per message or per render — so it adds no steady-state CPU.
  const _inboxCache = new Map();   // pubkey -> { relays:[...], ts }
  const _INBOX_TTL = 3600*1000;
  function _pick10050(evs, pk){ const ev=evs.filter(e=>e&&e.kind===10050&&e.pubkey===pk).sort((a,b)=>b.created_at-a.created_at)[0];
    return ev?ev.tags.filter(t=>t[0]==='relay'&&t[1]).map(t=>normalizeRelay(t[1])).filter(Boolean):[]; }
  function _pick10002(evs, pk){ const ev=evs.filter(e=>e&&e.kind===10002&&e.pubkey===pk).sort((a,b)=>b.created_at-a.created_at)[0];
    return ev?ev.tags.filter(t=>t[0]==='r'&&t[1]&&(t.length<3||t[2]==='read')).map(t=>normalizeRelay(t[1])).filter(Boolean):[]; }
  /* "COULD NOT ASK" IS NEVER "HAS NO INBOX", and this path was the last place in the client still
   * conflating them. The whole lookup sat in `catch(_){}`, the empty result was cached for an HOUR,
   * and the sender was told "recipient has no DM inbox relays" — a confident statement about
   * somebody else's account, made after reaching nobody, and then repeated to the next person who
   * tried. With three of four discovery relays down that is exactly what happened.
   *
   * `answered` is the difference, and it comes from `queryFrom`'s own report: `ok` lists the relays
   * that actually replied. Nothing is cached unless somebody answered, so the next attempt looks
   * again instead of inheriting a verdict nobody earned. */
  async function dmInboxRelays(pk){
    const c=_inboxCache.get(pk); const now=Date.now();
    if(c && (now-c.ts)<_INBOX_TTL) return { relays:c.relays, answered:true };
    let relays=[], answered=false;
    try{
      const evs=await Relay.query([{ authors:[pk], kinds:[10050,10002], limit:2 }]);
      relays=_pick10050(evs, pk); if(!relays.length) relays=_pick10002(evs, pk);
      /* Our own relay holding nothing is not evidence: it is WoT-only, so a stranger's relay list
       * was never stored here. Only FINDING something makes the local read an answer. */
      if(relays.length) answered=true;
      if(!relays.length){
        // Stranger (not in our WoT) → ask external discovery relays. They're untrusted, so VERIFY
        // signatures before trusting a relay list — a forged one would misroute the (encrypted) wrap.
        const report={};
        /* `allowBlocked` on purpose: relay.damus.io is on the pool's blocked-host list (it is a
         * firehose we do not want in the shared pool) and it is one of the few relays that reliably
         * holds these lists. This read is one author, two kinds, limit 2 — bounded and cheap. */
        let ext=await Relay.queryFrom(DISCOVERY_RELAYS, [{ authors:[pk], kinds:[10050,10002], limit:2 }],
                                      {purpose:'dm inbox discovery', report, allowBlocked:true,
                                       max:DISCOVERY_RELAYS.length});
        if((report.ok||[]).length) answered=true;
        if(ext.length){ try{ const v=await Relay.worker.call('verifyBatch',{events:ext});
          const ok=new Set(v.filter(r=>r.valid).map(r=>r.id)); ext=ext.filter(e=>ok.has(e.id)); }catch(_){ ext=[]; } }
        relays=_pick10050(ext, pk); if(!relays.length) relays=_pick10002(ext, pk);
      }
    }catch(_){ answered=false; }
    relays=[...new Set(relays)];
    if(answered) _inboxCache.set(pk, { relays, ts:now });   // never cache a verdict nobody gave
    return { relays, answered };
  }
  /* A background history/profile refresh may rebuild #dm-list while a remote signer or relay is
   * still handling Send. `dmActive` remains correct, but mobile navigation is controlled by the
   * rebuilt list's `has-active` class, so losing only that class looks like the conversation closed.
   * Never resurrect a thread after Back/navigation: repair the chrome only while this peer is still
   * the explicitly active conversation. */
  function _keepDmOpen(pk){
    if(S.VIEW!=='messages' || S.dmActive!==pk) return;
    const list=$('#dm-list'); if(list) list.classList.add('has-active');
    $$('.dm-peer',list||document).forEach(e=>e.classList.toggle('active',e.dataset.peer===pk));
    if(!$('#dm-in') && $('#dm-thread')) renderDmThread(pk);
  }
  // Send a DM: NIP-17 gift wraps for local-key users, legacy NIP-04 for NIP-07 (no exposed secret).
  /* THE LOCAL ECHO, for when the wrap could not be ingested. Deliberately minimal: it is the same
   * shape `ingestWrap` pushes, keyed on the SAME outer id, so the relay's copy of our own message
   * de-duplicates against it (`arr.find(m=>m.id===ev.id)`) instead of appearing twice. */
  function _dmEcho(pk, text, id){
    if(!pk || !id) return false;
    if(!dmPeers.has(pk)) dmPeers.set(pk, []);
    const arr = dmPeers.get(pk);
    if(arr.find(m => m.id === id)) return false;
    arr.push({ id, mine:true, text:String(text||''), t:Math.floor(Date.now()/1000), nip17:true, atts:[] });
    arr.sort((a,b)=>(a.t||0)-(b.t||0));
    try{ _scheduleDmRefresh(); }catch(_){ }
    return true;
  }

  async function sendDm(pk, text){
    if(S.signer && S.signer.nip17wrap){
      const { toPeer, toSelf } = await S.signer.nip17wrap(pk, text);
      /* TELL THE NATIVE LAYER WE PUBLISHED THESE, before either is on a relay.
       *
       * NIP-17 writes a SELF-COPY wrap so this account's other devices see what it sent, and that
       * copy is p-tagged to US — so the push watcher, which cannot decrypt a gift wrap and whose
       * "don't notify the author" test sees only an ephemeral key, pushes us a notification for our
       * own outgoing message: "evey time I send a DM i get a push notification". The device that
       * published it is the one thing that knows, so it says so, keyed on the wrap's own id — an
       * exact match, where suppressing DMs for a few seconds after a send would silence a real one
       * that happened to arrive in that gap. */
      /* TOLD TWICE, AND THE SECOND ONE IS THE ONE THAT COVERS THE REST OF YOUR DEVICES.
       *
       * The plugin call is this device saying "I published these", which is exact and needs no
       * server — but a device only knows what IT sent. A DM sent from the desktop still pushed the
       * phone, because the phone published nothing; and Web Push has no equivalent map at all, so
       * every browser device was told regardless. Reported again as "if I send a DM, i do not want
       * a push notification saying that somebdy sent a DM — it was me!".
       *
       * So the ACCOUNT is told too, and the push is never sent rather than sent and then dropped.
       * Both are kept: the device-local one still works with no instance at all, and it is the
       * faster of the two when the sender and the receiver are the same phone. */
      const _wrapIds=[toSelf&&toSelf.id,toPeer&&toPeer.id].filter(Boolean);
      try{ const P=_capPlugin('PosterChanPush','notePublished');
           if(P)void P.notePublished({ids:_wrapIds}); }catch(_){ }
      void _notePublishedWraps(_wrapIds);
      Store.saveEvent(toSelf);
      /* THE MESSAGE YOU JUST SENT MUST BE IN THE THREAD. Reported as "i send dm to user, then the
       * conversation goes blank": the pane renders `dmPeers.get(pk)`, so if our own copy does not
       * land there the thread paints EMPTY — and on a conversation with no history that is a blank
       * screen where the message should be.
       *
       * `ingestWrap` has several honest ways to decline: the unwrap can throw (a remote signer that
       * timed out, a worker that is busy), the id can already be in `_wrapTried`, the rumor can come
       * back the wrong kind. Every one of them returned false into a call that ignored the answer.
       *
       * So the answer is used. The wrap is still the real record — this only guarantees the echo,
       * with the SAME outer id, so the copy that arrives from the relay is recognised as a duplicate
       * rather than shown twice. */
      const echoed = await ingestWrap(toSelf, false);   // show our own message right away
      if(!echoed) _dmEcho(pk, text, toSelf && toSelf.id);
      _keepDmOpen(pk);
      const r1=await Relay.publish(toPeer), r2=await Relay.publish(toSelf);
      _keepDmOpen(pk);
      /* WRITTEN OFFLINE, SENT WHEN BACK. The message was already in the thread (the echo above), so a
       * publish that could not reach a relay left something that LOOKED sent and never would be. Both
       * wraps go to the Outbox (signed already; a resend is a no-op) — only when the relays could not be
       * reached: a relay that ANSWERED no has refused it, and retrying a refusal sends nothing. */
      const unreached = r => !!r && r.ok === false && !r.noQueue && (r.msg === 'offline' || navigator.onLine === false);
      if(unreached(r1) && window.Outbox && Outbox.canQueue(1059)){
        Outbox.add(toPeer, S.ME.pubkey); if(unreached(r2)) Outbox.add(toSelf, S.ME.pubkey);
        toast('you’re offline — the message will be sent when you’re back online');
        return;
      }
      // ingestWrap already schedules the visible thread's in-place refresh. Rebuilding all of
      // Messages here replaced the mobile overlay/composer and could drop the sender back on the
      // conversation list immediately after Send.
      // NIP-17 outbox delivery (backgrounded): push the wrap to the RECIPIENT's own DM-inbox relays
      // (kind 10050) so clients that don't read our relay (0xchat/Amethyst) receive it. publishTo skips
      // relays already in our pool + is bounded, so it's a no-op when the peer reads our relay. We only
      // warn when NOTHING accepted it — our relay rejects wraps to a non-WoT recipient (expected for an
      // external user), which is fine once their own inbox relay has taken it.
      dmInboxRelays(pk).then(({relays:inbox, answered})=>{
        if(!inbox.length){
          /* TWO DIFFERENT SENTENCES, because they are two different situations and only one of them
           * is about the recipient. Telling somebody "they have no inbox" when we simply could not
           * reach a single indexer is how a working account gets written off as unreachable. */
          if(r1 && r1.ok===false) toast(answered
            ? 'message not delivered — recipient has no DM inbox relays'
            : 'message not delivered — could not look up their DM inbox; try again');
          return;
        }
        Relay.publishTo(inbox, toPeer).then(n=>{ if(r1 && r1.ok===false && !n) toast('message not delivered — no inbox relay accepted it'); });
      }).catch(()=>{});
    } else {
      const ct=await S.signer.nip04enc(pk, text); await publish(4, ct, [['p',pk]]);
      _keepDmOpen(pk);
    }
  }
  function bumpDm(){ $$('#dm-badge,#dm-badge-m').forEach(b=>{ if(S._dmUnread){ b.textContent=S._dmUnread>99?'99+':S._dmUnread; b.classList.remove('hidden'); } else b.classList.add('hidden'); });
    // The BELL counts unread DMs too ("i see no notification bell when DM's come in"), so it repaints
    // whenever this count moves -- an arrival, or reading them in Messages.
    try{ if(bumpNotif) bumpNotif(); }catch(_){ } }
  // Startup count of what's unread. Notes to SELF count here for the same reason they do in
  // ingestWrap — that's how the server delivers notifications — and this is the path that catches one
  // that arrived while the app was CLOSED, which is the whole point of notifying at all.
  /* READ ON ONE MONITOR IS READ ON ALL -- for DMs too. "monitor 1 has 0 notifications, monitor 2 has
   * 8": each PosterChanOS monitor is its own page, `dmSeen` lives in shared storage, and the count was
   * only ever recomputed when a DM ARRIVED -- so reading your messages on one monitor left every other
   * bell counting them. pc_notif_seen has had this listener since notifications went per-monitor; DMs
   * joined the bell later and never got theirs. A `storage` event fires only in the OTHER pages. */
  try{ window.addEventListener('storage', e => {
    if(e.key !== 'pc_nostr_settings') return;
    let was = 0, now = 0;
    try{ was = (JSON.parse(e.oldValue || '{}') || {}).dmSeen || 0; }catch(_){}
    try{ now = (JSON.parse(e.newValue || '{}') || {}).dmSeen || 0; }catch(_){}
    if(was !== now) try{ recountDmUnread(); }catch(_){}
  }); }catch(_){}
  /* Unread DMs that arrived AFTER `since` (seconds). The bell counts these, not every unread DM: opening
     Notifications has to empty the bell ("still says 13" -- "i opened messages and it cleared": the 13
     were DMs, which only Messages marked read). The Messages badge keeps the full count. */
  function dmUnreadSince(since){ const seen=Math.max(Number(ClientSettings.get('dmSeen',0))||0, Number(since)||0); let n=0;
    for(const [pk,arr] of dmPeers){ if(isMutedAuthor(pk)) continue;
      const selfThread = pk===S.ME.pubkey;
      for(const m of arr){ if((!m.mine || selfThread) && (m.t||0)>seen) n++; } }
    return n; }
  try{ window.PCdmUnreadSince = dmUnreadSince; }catch(_){}
  function recountDmUnread(){ const seen=ClientSettings.get('dmSeen',0); let n=0;
    for(const [pk,arr] of dmPeers){ if(isMutedAuthor(pk)) continue;
      const selfThread = pk===S.ME.pubkey;
      for(const m of arr){ if((!m.mine || selfThread) && (m.t||0)>seen) n++; } } S._dmUnread=n; bumpDm(); }
  function _dmNotify(fromPk, selfNote, text){
    if(!notificationAllowed('dm'))return;
    /* A NOTE TO SELF IS HOW THE SERVER TALKS TO ITS ADMIN (a user applying for a name, an agent run
     * finishing, an uptime alert) — on a single-admin node the operator key IS the admin's. It used to
     * say "New notification — saved to your notes to self", which describes the DELIVERY and nothing
     * about the event: an access application read like something the admin had saved ("it's nothing
     * about a user requesting permission"). So it says what the message says: its first line. With
     * "Hide DM previews until opened" on it still names nothing — but it no longer claims a save. */
    if(selfNote){
      const line = String(text || '').split('\n').map(x => x.trim()).find(Boolean) || '';
      const said = line.replace(/nostr:(npub|nprofile)1[02-9ac-hj-np-z]{20,}/g, 'someone').slice(0, 140);
      const body = (!said || ClientSettings.get('hideDmPreview', false)) ? 'Open Messages to read it' : said;
      notifToast('🔔 <b>Notification</b> — ' + enc(body), S.LOGO);
      osNotify('🔔 Notification', body, { tag:'pc-dm', type:'dm', route:'messages' });
      return;
    }
    const p=fromPk?profOf(fromPk):{}; const who=p.name||p.display_name||'someone';
    notifToast(`✉ <b>${fromPk?emojiName(fromPk,who):enc(who)}</b> sent you a message`, p.picture);   // in-app toast (no OS permission needed)
    /* `tag:'pc-dm'` IS SHARED WITH THE SERVER'S PUSH ON PURPOSE — it is what makes this ONE
     * notification instead of two. The push cannot decrypt a gift wrap, so it says "Someone sent you
     * a message"; this one has, so it names them. Same tag, so Android replaces rather than stacks.
     * `type:'dm'` is the other half: the native plugin records that a live client spoke for this DM,
     * and a generic push arriving just afterwards is dropped rather than overwriting the better
     * wording (or re-posting a message already read). See ClientNotified. */
    osNotify('✉ New message', `${who} sent you a DM`, { tag:'pc-dm', type:'dm', icon:p.picture||S.LOGO, route:'messages',
                                                        onClick:()=>switchView('messages') });
  }
  // Index DMs WITHOUT decrypting (decryption is CPU-heavy ECDH+AES in the worker; decrypting all
  // 200 on load jams the worker and stalls timeline verification). Decrypt lazily on view.
  function ingestDM(ev){
    const mine = ev.pubkey===S.ME.pubkey;
    // Only DMs that involve ME. The relay stores other WoT members' kind-4 DMs and the client
    // caches them (Store.byKind(4)) — without this guard they'd show as "couldn't decrypt".
    const toMe = (ev.tags||[]).some(t=>t[0]==='p' && t[1]===S.ME.pubkey);
    if(!mine && !toMe) return false;
    const peer = mine ? (ev.tags.find(t=>t[0]==='p')||[])[1] : ev.pubkey;
    if(!peer) return false; needProfile(peer);
    if(!dmPeers.has(peer)) dmPeers.set(peer, []);
    const arr=dmPeers.get(peer); if(arr.find(m=>m.id===ev.id)) return false;
    arr.push({ id:ev.id, mine, ev, text:null, t:ev.created_at, xdcMessageId:ev.id,
               tags:(ev.tags||[]).map(t=>t.slice()) }); arr.sort((a,b)=>a.t-b.t);
    _scheduleDmRefresh();
    return true;
  }
  async function decryptMsg(peer, m){
    if(m.text!=null) return m.text;
    try{ m.text=await S.signer.nip04dec(peer, m.ev.content); }catch(_){ m.text='🔒 (couldn\'t decrypt)'; }
    return m.text;
  }
  // Shared image-URL test for compose preview strips. Extension-only (matches the linkify/media embed
  // regex), so an event-id / non-image 64-hex link is never shown as a deletable thumbnail. uploadBlob and
  // blossomPicker append the file extension, so pasted/attached images still match; a rare extensionless
  // unknown-MIME blob just doesn't get a preview (it still sends fine as a text URL).
  function isImageUrl(u){ return /\.(jpe?g|png|gif|webp|avif)(\?|#|$)/i.test(u); }
  // Wire a compose <textarea> for image PASTE-to-attach + a live, removable thumbnail strip. Shared by
  // both DM entry points. Returns sync() to call after programmatic value changes (e.g. send clears the box).
  /* `opts.enc` marks a composer whose attachments obey the DM 🔒 (the two DM boxes). It is a flag
     rather than a global check because this same helper backs composers where encrypting would be
     wrong — a public post encrypted to nobody is just a broken image. */
  function wireImgAttach(inp, strip, opts){
    if(!inp) return ()=>{};
    const encHere = () => !!(opts && opts.enc) && dmEncOn();
    // Remove the URL only where it stands as a WHOLE token (+ its leading space), so it can't clobber a
    // longer URL it's a prefix of (e.g. x.png vs x.png?thumb=1) and doesn't collapse unrelated whitespace.
    const removeOne=u=>{ const esc=u.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
      inp.value=inp.value.replace(new RegExp('[ \\t]*'+esc+'(?=\\s|$)'), ''); };
    const sync=()=>{ if(!strip) return; const imgs=(String(inp.value||'').match(/https?:\/\/\S+/g)||[]).filter(isImageUrl);
      if(!imgs.length){ strip.hidden=true; strip.innerHTML=''; return; }
      strip.hidden=false;
      strip.innerHTML=imgs.map(u=> S.NO_IMAGES
        ? `<span class="dm-att ds" data-url="${enc(u)}">🖼 image<button class="dm-att-x" title="remove"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></span>`
        : `<span class="dm-att" data-url="${enc(u)}"><img src="${enc(u)}" alt="Attachment" loading="lazy" onerror="this.closest('.dm-att')&&this.closest('.dm-att').remove()"><button class="dm-att-x" title="remove"><svg class="ic x-ic" aria-hidden="true"><use href="#i-close"></use></svg></button></span>`).join('');
      strip.querySelectorAll('.dm-att-x').forEach(b=> b.onclick=()=>{ removeOne(b.closest('.dm-att').dataset.url); sync(); inp.focus(); }); };
    inp.addEventListener('input', sync);
    inp.addEventListener('paste', async e=>{
      const files=[...(e.clipboardData&&e.clipboardData.items||[])].filter(it=>it.kind==='file').map(it=>it.getAsFile()).filter(Boolean);
      if(!files.length) return; e.preventDefault();
      for(const f of files){ const _e=encHere(); toast(_e?'encrypting pasted image…':'uploading pasted image…');
        try{ const url=_e ? await uploadSharedEnc(f) : await uploadBlob(f); inp.value+=(inp.value&&!/\s$/.test(inp.value)?' ':'')+url; }
        catch(err){ if(typeof _blossomDenied==='function'&&_blossomDenied(err)){ requestBlossomAccess(); toast('🔒 No upload access — requested it from the admin.'); } else toast('upload failed: '+((err&&err.message)||err)); } }
      sync(); inp.focus();
    });
    return sync;
  }
  // Messenger-style timestamps: a time for today, a weekday within the week, else a date. Keeps the
  // list scannable — "14:32" next to a name is the single biggest thing that made it read as a
  // conversation list rather than a flat directory.
  function _dmClock(ts){ try{ return new Date(ts*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }catch(_){ return ''; } }
  function _dmWhen(ts){
    if(!ts) return '';
    const d=new Date(ts*1000), now=new Date();
    const sameDay=(a,b)=>a.toDateString()===b.toDateString();
    if(sameDay(d,now)) return _dmClock(ts);
    const y=new Date(now); y.setDate(y.getDate()-1);
    if(sameDay(d,y)) return 'Yesterday';
    if((now-d)/86400000 < 7) return d.toLocaleDateString([], {weekday:'short'});
    return d.toLocaleDateString([], {day:'numeric', month:'short'});
  }
  function _dmDayLabel(ts){
    const d=new Date(ts*1000), now=new Date();
    const sameDay=(a,b)=>a.toDateString()===b.toDateString();
    if(sameDay(d,now)) return 'Today';
    const y=new Date(now); y.setDate(y.getDate()-1);
    if(sameDay(d,y)) return 'Yesterday';
    return d.toLocaleDateString([], {day:'numeric', month:'long', year: d.getFullYear()===now.getFullYear()?undefined:'numeric'});
  }


  return {
    DmCache, _decorateDmFileAtts, _dmClock, _dmDayLabel, _dmProgress, _dmScrollState, _dmWhen,
    _restoreDmScroll, _startCordDirectInbox, bumpDm, cordDirectContext, cordDirectModule,
    cordInviteLinksModule, decryptMsg, ensureDMs, ensureDmInboxList, ingestDM, recountDmUnread,
    sendCordDirectInvite, sendDm, wireImgAttach,
  };
};

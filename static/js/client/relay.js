/* WebSocket client for Nostr relays. By default it talks to the single built-in WoT relay and
 * TRUSTS its events (they're verified server-side on write) — re-verifying the global firehose
 * client-side pegged a CPU core. When the user opts into their OWN relays (Settings), the pool
 * connects to those INSTEAD, and since they're no longer the trusted built-in relay every
 * incoming event is signature-verified in the crypto worker (off the main thread) before it
 * reaches the app. The public API (connect/subscribe/query/publish/close/worker) is identical
 * whether one relay or several are connected — the app code doesn't change. */
(function(){
  // ---- worker RPC (crypto: sign / keygen / nip04 / verify) ----
  class WorkerRPC {
    constructor(url){
      this.w = new Worker(url); this.seq = 0; this.pending = new Map();
      this.w.onmessage = (e) => {
        const { id, ok, data, error } = e.data;
        const p = this.pending.get(id); if (!p) return;
        this.pending.delete(id); ok ? p.res(data) : p.rej(new Error(error || 'worker error'));
      };
    }
    call(op, args){ return new Promise((res,rej)=>{ const id = ++this.seq; this.pending.set(id,{res,rej}); this.w.postMessage({ id, op, args }); }); }
  }
  const worker = new WorkerRPC('/static/js/client/signer-worker.js?v=' + (self.__VER || ''));

  /* A RELAY URL HAS TO BE ABSOLUTE AND ws(s):, and this is not pedantry — `new WebSocket(x)`
     RESOLVES A RELATIVE STRING AGAINST THE PAGE. So a blank, half-typed or path-shaped entry does
     not fail: it opens a socket to the app's own address, which serves no WebSocket, and the
     rejection looks exactly like a relay being down. Measured in production as 35 refused upgrades
     to /client in twenty minutes — one client retrying for ever behind a backoff, against a URL that
     could never work, while the relay list showed nothing wrong.

     Matched with a REGEX rather than `new URL()`, deliberately. `URL` is a global, and a global is
     a dependency: this file runs in a browser, in an APK WebView, in a desktop shell and inside a
     `vm` sandbox in the tests — and the sandbox did not provide it, so the first version threw
     ReferenceError on every relay, refused all of them, and hung seven tests. A predicate this small
     should not be able to fail because of what is missing around it. The host must be non-empty, or
     a bare "wss://" would pass. */
  var _WSURL = /^wss?:\/\/[^\s/?#]+/i;
  function _isRelayUrl(u){
    return _WSURL.test(String(u || '').trim());
  }
  /* Deployment-denied relay hosts. These two consistently refuse/time out and have appeared through
     legacy saved rooms, user relay migrations and external reads. Check at every constructor path so
     no persisted spelling can bypass a higher-level list cleanup. */
  var _BLOCKED_RELAY_HOST = /^wss?:\/\/(?:relay\.ditto\.pub|relay\.damus\.io)(?::\d+)?(?:[\/?#]|$)/i;
  function _isBlockedRelay(u){ return _BLOCKED_RELAY_HOST.test(String(u||'').trim()); }

  // ---- one socket to one relay; reports up to the pool ----
  class Conn {
    constructor(url, pool, trusted){
      this.url = url; this.pool = pool; this.trusted = trusted;
      this.ws = null; this.status = 'init'; this._backoff = 600; this._rt = null;
      this._open();
    }
    _open(){
      /* NOT `_retry()`: a malformed URL cannot start working, and retrying one is how a typo becomes
         an endless stream of rejected connections nobody can trace back to it. Said once, out loud,
         because the alternative is a relay that is simply always 'off' for no stated reason. */
      if(!_isRelayUrl(this.url)||_isBlockedRelay(this.url)){
        if(!this._badUrl){
          this._badUrl = true;
          try{ console.warn('[relay] unavailable or invalid relay URL ignored:', this.url); }catch(_){}
        }
        this._setStatus('off');
        return;
      }
      try { this.ws = new WebSocket(this.url); this._openingAt = Date.now(); }
      catch(e){ this._openingAt = 0; this._setStatus('off'); this._retry(); return; }
      this._setStatus('connecting');
      this.ws.onopen = () => {
        this._openingAt = 0;
        this._backoff = 600;                       // reset reconnect backoff on a good connection
        this._lastRx = Date.now();
        this._setStatus('ok');
        // Re-arm LIVE subscriptions, and give any sub this socket has never been ASKED its first REQ
        // — including a one-shot query(). Those two are different things: re-REQ'ing a one-shot sub
        // after a drop would re-deliver its backlog, but a socket that was still CONNECTING when the
        // REQ went out was never sent it at all (_send drops on readyState !== 1), so it can only sit
        // there contributing nothing and then be waited on by the EOSE gate below. That is the cold
        // start — open the app, the first queries fire before the sockets finish connecting, and every
        // one of them returns empty at its 6s timeout.
        for (const [id, s] of this.pool._subs){
          if (s.live || !s.sent.has(this.url)){ this._send(['REQ', id, ...s.filters]); s.sent.add(this.url); }
        }
        this.pool._connReady(this);
        this._startHeartbeat();
      };
      this.ws.onclose = () => { this._stopHeartbeat(); this._setStatus('off'); this.pool._connGone(this); this._retry(); };
      this.ws.onerror = () => { try{ this.ws.close(); }catch(_){} };
      this.ws.onmessage = (e) => { this._lastRx = Date.now(); this.pool._onMessage(this, e.data); };
    }
    // Null handlers (so a stale onclose can't ALSO fire _retry and race an in-flight _open), close,
    // drop the ref, and stop the beat. One place, reused by destroy()/wake()/the zombie branch.
    _teardownSocket(){
      this._stopHeartbeat();
      this._openingAt = 0;
      if (this.ws){ this.ws.onclose = this.ws.onerror = this.ws.onmessage = null; try{ this.ws.close(); }catch(_){} this.ws = null; }
    }
    // Keep the socket alive AND detect a zombie. On a high-latency / proxied link (e.g. Cloudflare in
    // front, client on another continent) an idle WebSocket with no traffic gets idle-closed by the
    // proxy but the browser still reports it "open": events stop flowing and the next query() times out
    // → the "have to reload the page" symptom. Every 25s (under the ~60s nginx / ~100s CF idle windows),
    // ONLY when the socket has been idle (an actively-receiving connection's own traffic already keeps
    // the proxy open), send a cheap no-op REQ+CLOSE — which keeps the proxy from closing the WS and
    // draws an EOSE that refreshes _lastRx. If our TRUSTED relay (which always answers) then goes silent
    // for ~3 beats, the socket is a zombie → reconnect (via _retry, so a persistently-silent relay backs
    // off instead of re-churning every 75s). Skipped while backgrounded — the OS freezes the socket and
    // throttles this timer past the threshold; wake() (visibilitychange) re-establishes on focus.
    _startHeartbeat(){
      this._stopHeartbeat();
      this._hb = setInterval(() => {
        if (!this.ws || this.ws.readyState !== 1) return;
        if (typeof document !== 'undefined' && document.hidden) return;
        const idle = Date.now() - (this._lastRx || 0);
        // Zombie detection. The idle ping below MUST draw an EOSE, which refreshes _lastRx — so on a LIVE
        // link (even a slow/throttled one) idle stays low because the ping keeps getting answered. Only a
        // frozen socket (a proxy idle-closed it but the browser still reports "open") lets idle grow
        // unanswered. Reconnect at 40s (was 75s) so a stalled quiet feed recovers in well under a minute,
        // WITHOUT false-positiving a slow-but-alive link (which keeps answering the ping). Trusted relay
        // only — the one guaranteed to answer. (A manual pull-to-refresh recovers instantly via reviveStale.)
        if (this.trusted && this._lastRx && idle > 40000){
          this._teardownSocket(); this._setStatus('off'); this._retry();
          return;
        }
        if (idle > 20000){
          // Filter on a non-existent id → guaranteed 0 events + an immediate EOSE (a bare {limit:0} would
          // be read as 500 by the relay — `limit or 500` — and dump events every beat).
          try{ this._send(['REQ', '_hb', { ids: ['0000000000000000000000000000000000000000000000000000000000000000'] }]); this._send(['CLOSE', '_hb']); }catch(_){}
        }
      }, 25000);
    }
    _stopHeartbeat(){ if (this._hb){ clearInterval(this._hb); this._hb = null; } }
    _setStatus(s){ this.status = s; this.pool._recomputeStatus(); }
    _retry(){ clearTimeout(this._rt); const cap = this.trusted ? 2500 : 8000;   // our built-in relay is always up → reconnect fast, never wait 8s
              const d = this._backoff || 600; this._backoff = Math.min(d*1.7, cap);
              this._rt = setTimeout(()=>this._open(), d + Math.random()*300); }
    // Answers whether it actually went out, so the pool can count the relays a publish reached —
    // which is what makes "every one of them refused" a question with an answer. See publish().
    _send(arr){ if (this.ws && this.ws.readyState === 1){ this.ws.send(JSON.stringify(arr)); return true; }
                return false; }
    destroy(){ this.status = 'closed'; clearTimeout(this._rt); this._teardownSocket(); }
  }

  const Relay = {
    worker,
    status: 'init',
    onStatus: null,
    onReady: null,            // fired once when the first socket opens (run initial queries here)
    onReconnect: null,        // fired on each LATER (re)open — re-run one-shot queries that onopen
                              // can't re-arm (live subs auto-re-arm; query() subs are dropped on a drop)
    url: null,                // primary relay (first configured) — used as the display/zap relay
    _conns: new Map(),        // url -> Conn
    _subs: new Map(),         // subId -> {filters, onEvent, onEose, live, seen:Set, eosed:Set, sent:Set}
    _okWaiters: new Map(),    // eventId -> { settle(fn) }
    _countWaiters: new Map(), // countId -> resolve(n)  (NIP-45 COUNT)
    _negWaiters: new Map(),   // negId -> { onMsg(hex), onErr(reason) }  (NIP-77 negentropy)
    _verify: false,           // true when connected to user relays (untrusted -> verify sigs)
    _vq: [], _vt: null,       // verify queue for untrusted events
    _ready: false,
    _queryFromActive: new Set(),
    _queryFromCooldown: new Map(),
    _queryFromPurposeCooldown: new Map(),
    _queryFromStops: new Set(),
    _queryFromBlockedHosts: new Set(['relay.ditto.pub','relay.damus.io']),

    /* Ephemeral external reads belong to the screen that requested them. A route change closes all
       of them synchronously; per-call AbortSignals remain useful for finer owners inside a view. */
    abortQueries(){ for(const stop of [...this._queryFromStops])try{stop();}catch(_){} },

    // Connect to an explicit set of relays. verify=true makes the pool signature-verify every
    // incoming event (used for user-supplied relays); verify=false trusts them (built-in WoT relay).
    configure({ urls, verify } = {}){
      urls = [...new Set((urls||[]).filter(u=>u&&!_isBlockedRelay(u)))];
      this._verify = !!verify;
      this.url = urls[0] || null;
      // drop connections no longer wanted
      for (const [u, c] of this._conns){ if (!urls.includes(u)){ c.destroy(); this._conns.delete(u); this._connGone(c); } }
      // open the new ones (trusted = NOT verify mode)
      for (const u of urls){ if (!this._conns.has(u)) this._conns.set(u, new Conn(u, this, !this._verify)); }
      if (!this._conns.size) this._setStatus('off');
    },
    // back-compat single-relay entry point (built-in WoT relay, trusted)
    connect(url){ this.configure({ urls: url ? [url] : [], verify: false }); },
    // Every relay this pool talks to. `publish()` already broadcasts to all of them; this is for
    // telling ANOTHER device (the password extension) where else the same events can be read, so a
    // single relay being down is not a single point of failure for it.
    urls(){ return [...this._conns.keys()]; },

    _watchers: [],
    /* Subscribe to connection changes; returns an unsubscribe.
     *
     * A LIST, not a second `onStatus`: that is a single slot app.js owns, and it drives the offline
     * banner, the outbox flush and the relay-change carry — so a second consumer assigning it takes
     * all three over and nothing says so.
     *
     * Fired from _recomputeStatus, which runs on every PER-RELAY change rather than only when the
     * aggregate flips. That distinction is the whole value for a per-relay display: a pool of five
     * losing four of them never changes `status` (some socket is still 'ok' throughout), so a widget
     * listening for the aggregate would show all five as fine right up until the last one died. */
    watch(fn){
      if (typeof fn !== 'function') return () => {};
      this._watchers.push(fn);
      return () => { const i = this._watchers.indexOf(fn); if (i >= 0) this._watchers.splice(i, 1); };
    },
    /* A SNAPSHOT of every socket, for display. Deliberately not the Conn objects: those own a live
     * socket and two timers, and a caller that got hold of one could close the pool's connection by
     * accident. `idle` is ms since anything last arrived — the heartbeat answers its own ping every
     * 25s, so on a healthy link it stays low, and a trusted socket that reaches 40s reconnects
     * itself. null means nothing has ever arrived (still connecting, or never did). */
    conns(){
      return [...this._conns.values()].map(c => ({
        url: c.url,
        status: c.status,
        trusted: !!c.trusted,
        open: !!(c.ws && c.ws.readyState === 1),
        idle: c._lastRx ? Date.now() - c._lastRx : null,
      }));
    },

    // Resolve once a socket is actually OPEN (or `ms` elapses). Call BEFORE a burst of one-shot reads on a
    // cold start: on first launch (esp. the APK, radio waking) the socket is still CONNECTING, and firing
    // REQs into it just drops them (Conn._send needs readyState OPEN) and eats the full query timeout —
    // the "15s profile load + 0 followers on first open, fine after reload". Instant when already connected.
    // Resolves TRUE once a socket is actually OPEN, or FALSE if `ms` elapsed first (still connecting /
    // offline). The caller uses the boolean to decide whether its reads hit a live socket — if not, it
    // marks itself un-hydrated so onReady/onReconnect can reload it when the socket finally comes up.
    ready(ms=3000){
      return new Promise(resolve => {
        // "Live" = OPEN and not a zombie. A zombie (proxy idle-closed / PWA resumed, but the browser still
        // reports the socket OPEN) has readyState 1 yet delivers nothing — a bare readyState check treats it
        // as ready and the caller's reads then time out into it (the "0 followers on the web too" case).
        // Treat a trusted socket silent >30s as NOT live (matches reviveStale's zombie threshold).
        const live = () => {
          for (const c of this._conns.values()){
            if (!c.ws || c.ws.readyState !== 1) continue;
            if (c.trusted && c._lastRx && Date.now() - c._lastRx > 30000) continue;   // zombie
            return true;
          }
          return false;
        };
        if (live()) return resolve(true);
        if (!this._conns.size) return resolve(false);
        try{ this.reviveStale(); }catch(_){}   // reconnect a dead/zombie socket so live() can become true
        const t0 = Date.now();
        const iv = setInterval(() => {
          if (live()){ clearInterval(iv); resolve(true); }
          else if (Date.now() - t0 >= ms){ clearInterval(iv); resolve(false); }
        }, 50);
      });
    },

    // Force a fresh connection on every relay — call when the app returns to the foreground. A mobile
    // PWA's WebSocket is frozen while backgrounded and very often comes back DEAD-but-still-"open"
    // (zombie): the client thinks it's connected, no events flow, and the next query times out against a
    // dead socket → the "relay timeout when loading" symptom. Tearing the socket down and reopening
    // (our relay reconnects in ~0.1s, re-arming live subs) refreshes the feed instantly on focus.
    wake(){
      for (const c of this._conns.values()){
        c._teardownSocket();   // nulls handlers + stops the prior heartbeat before reopening
        clearTimeout(c._rt); c._backoff = 600; try{ c._open(); }catch(_){}
      }
    },

    _setStatus(s){ if (s === this.status) return; this.status = s; if (this.onStatus) this.onStatus(s); },
    _recomputeStatus(){
      const sts = [...this._conns.values()].map(c=>c.status);
      this._setStatus(sts.some(x=>x==='ok') ? 'ok' : sts.some(x=>x==='connecting') ? 'connecting' : 'off');
      // A COPY: this fires from Conn._setStatus, which runs inside configure()'s loop over _conns —
      // a watcher that reconfigures the pool would otherwise mutate the list being iterated. One
      // throwing watcher must not cost the others their notification either.
      for (const fn of this._watchers.slice()){
        try { fn(this.status); } catch(e){ console.warn(e); }
      }
    },
    _connReady(){
      if (!this._ready){ this._ready = true; if (this.onReady) try { this.onReady(); } catch(e){ console.warn(e); } }
      else if (this.onReconnect){ try { this.onReconnect(); } catch(e){ console.warn(e); } }   // reconnect: re-hydrate one-shot data
    },

    /* Has every relay we ACTUALLY ASKED answered?
     *
     * This used to be `sub.eosed.size >= this._conns.size`, and the denominator was wrong in the one
     * case that matters: a relay that is down stays in `_conns` for the whole session (it is in a
     * reconnect backoff, which is what makes it come back), and a REQ is silently dropped for any
     * socket that is not OPEN (Conn._send). So one unreachable relay in the user's list made this
     * threshold UNREACHABLE — `query()` then ran to its full 6s timeout on every single fetch, for as
     * long as that relay stayed down. Measured with `wss://offchain.pub/` (one of our own suggested
     * defaults) unreachable: every deferred fill in the app — repost originals, notification previews,
     * quoted notes, thread parents, older timeline pages — cost 6 seconds, and an older page that came
     * back empty on the timer latched the feed as finished. Live subs hid it behind their 12s backstop;
     * one-shot queries have no backstop, only the timeout.
     *
     * `sent` is the honest denominator: the sockets this sub's REQ was really written to. Empty means
     * nothing was asked (every socket still connecting) — the timer has to settle that one, and the
     * onopen path above sends the REQ when a socket arrives so it can still answer in time. */
    _eoseDone(sub){
      if (!sub.sent || !sub.sent.size) return false;
      for (const u of sub.sent) if (!sub.eosed.has(u)) return false;
      return true;
    },
    // A socket that dropped cannot answer a REQ it was sent, so it must leave the denominator or every
    // pending sub waits out its timeout for it. Re-check the gate after: this may be the last one.
    _connGone(conn){
      for (const sub of this._subs.values()){
        if (!sub.sent || !sub.sent.delete(conn.url)) continue;
        if (sub.onEose && this._eoseDone(sub)) this._fireEose(sub);
      }
    },

    _send(arr){ let n = 0; for (const c of this._conns.values()) if (c._send(arr)) n++; return n; },

    /* `tags` is REQUIRED by NIP-01, so this rewrites nothing well-formed — but a relay is untrusted
     * input and the signature check cannot cover this: our OWN relay is `trusted`, so its events skip
     * verification entirely. One event with no `tags` array is enough to throw inside whatever walks
     * it, and the callers that take a result STRAIGHT from here without going through the Store are
     * the replaceable-list loaders that hydrate a signed-in session — FOLLOWS, MUTED, PINNED,
     * BOOKMARKS in app.js all do `ev.tags.filter(...)` on a raw query result. An exception there does
     * not cost one card, it costs the follow list. (Store.saveEvent normalises for everything that
     * DOES reach the cache; this is the same guarantee for everything that never does — which is why
     * it lives at both ingest points, the pool below and the one-shot external read in queryFrom.)
     * Safe before signature verification: a well-formed event is left untouched, and a tag-less one
     * could never have verified anyway — its id is a hash over the tags. Adding `[]` cannot make an
     * invalid event verify, only keep an already-doomed one from throwing on the way to being dropped.
     *
     * DELIBERATELY narrower than store.js's _normEvent, which ALSO drops non-array tag entries. Only
     * the top-level shape can throw (`ev.tags.filter` on undefined); a junk ENTRY like `['e','x']`
     * flattened to a string merely fails the `t[1]` test and is ignored. The Store normalises harder
     * because it keeps events for weeks and feeds the count index; this is a wire guard, and touching
     * less of an unverified event is the point. If one of these ever grows a rule, ask whether the
     * other needs it — they are two normalisers and that is how they drift. */
    _normTags(ev){ if (ev && !Array.isArray(ev.tags)) ev.tags = []; return ev; },

    _onMessage(conn, raw){
      let m; try { m = JSON.parse(raw); } catch(_){ return; }
      const typ = m[0];
      if (typ === 'EVENT'){
        /* A trusted relay echoing the exact signed id is delivery evidence even if its separate OK
         * frame was lost during a phone radio handoff. Settle the active publish before looking up
         * the subscription: the echo can arrive on any live REQ, and delivery must not depend on
         * which screen happens to be open. This also lets an Outbox retry clear a stale Pending
         * badge for a post other people already received. Untrusted relays do not get this shortcut;
         * their EVENT still has to pass verification below. */
        const wireEv = m[2];
        if(conn.trusted && wireEv && wireEv.id){
          const ack = this._okWaiters.get(wireEv.id);
          if(ack){ this._okWaiters.delete(wireEv.id); ack.settle({ ok:true, msg:'relay echo' }); }
          try{ if(window.Outbox && Outbox.has(wireEv.id)) Outbox.remove(wireEv.id); }catch(_){}
        }
        const sub = this._subs.get(m[1]); if (!sub || !sub.onEvent) return;
        const ev = this._normTags(m[2]); if (!ev || sub.seen.has(ev.id)) return;   // dedup across relays
        if (conn.trusted){ this._seenAdd(sub, ev.id); sub.onEvent(ev); }
        else { this._vq.push({ ev, sub }); if (!this._vt) this._vt = setTimeout(()=>this._flush(), 40); }
      } else if (typ === 'EOSE' || typ === 'CLOSED'){
        const sub = this._subs.get(m[1]); if (!sub) return;
        sub.eosed.add(conn.url);
        if (sub.onEose && this._eoseDone(sub)){
          // for untrusted relays, drain pending verifications so the last events aren't lost
          const fire = () => this._fireEose(sub);
          this._vq.length ? this._flush().then(fire) : fire();
        }
      } else if (typ === 'OK'){
        const w = this._okWaiters.get(m[1]);
        if (w && m[2]){ this._okWaiters.delete(m[1]); w.settle({ ok: true, msg: m[3]||'' }); }   // first accept wins
        /* A REFUSAL IS AN ANSWER, and it was being thrown away.
         *
         * Only an acceptance settled the waiter, so `["OK", id, false, "invalid: bad id or
         * signature"]` fell on the floor and publish() reported `timeout` eight seconds later. Every
         * caller downstream then treats a relay that REFUSED the event exactly like a relay that
         * never spoke: the Outbox retries it for a week, the user sees "Pending" for ever, and the
         * one sentence that says why — which the relay sent immediately — is never shown to anybody.
         * That is what turned a wrongly-signed quote post into an unexplainable stuck post.
         *
         * "First accept wins" is still the rule: a refusal from one relay must not settle a publish
         * another might accept. So the reason is REMEMBERED, and settles the promise only once every
         * relay we sent to has refused — or, failing that, it is what the timeout reports instead of
         * the word "timeout". */
        else if (w){
          w.no = (w.no || 0) + 1;
          w.why = m[3] || 'refused';
          if (w.sent && w.no >= w.sent){ this._okWaiters.delete(m[1]); w.settle({ ok:false, msg:w.why }); }
        }
      } else if (typ === 'COUNT'){
        const w = this._countWaiters.get(m[1]); if (w) w((m[2] && m[2].count) || 0);   // NIP-45 reply
      } else if (typ === 'NEG-MSG'){
        const w = this._negWaiters.get(m[1]); if (w) w.onMsg(m[2]);                    // NIP-77 round
      } else if (typ === 'NEG-ERR'){
        const w = this._negWaiters.get(m[1]); if (w) w.onErr(m[2] || 'error');
      }
    },
    async _flush(){
      if (this._vt){ clearTimeout(this._vt); this._vt = null; }
      const batch = this._vq.splice(0, this._vq.length);
      if (!batch.length) return;
      try {
        const results = await worker.call('verifyBatch', { events: batch.map(b=>b.ev) });
        const valid = new Set(results.filter(r=>r.valid).map(r=>r.id));
        for (const b of batch){ if (valid.has(b.ev.id) && b.sub.onEvent && !b.sub.seen.has(b.ev.id)){ this._seenAdd(b.sub, b.ev.id); b.sub.onEvent(b.ev); } }
      } catch(e){ console.warn('verify batch failed', e); }
    },
    // Bound a sub's dedup Set: a live sub stays open for the whole session, so an uncapped `seen` grows
    // without limit (one entry per event ever delivered) → a slow memory leak in the always-open PWA/APK.
    // A Set keeps insertion order, so on overflow we evict the oldest ids. The 5000 cap dwarfs any relay's
    // in-flight/dedup window, so dedup for recent events stays correct — only long-gone ids are dropped.
    _seenAdd(sub, id){
      sub.seen.add(id);
      if (sub.seen.size > 5000){ const it = sub.seen.values(); for (let n = sub.seen.size - 5000; n > 0; n--){ sub.seen.delete(it.next().value); } }
    },

    // filters: array of filter objects. Returns subId. live=true keeps it open for new events.
    subscribe(filters, { onEvent, onEose, live=true } = {}){
      const id = 'sub' + Math.random().toString(36).slice(2,9);
      const sub = { filters, onEvent, onEose, live, seen: new Set(), eosed: new Set(), sent: new Set() };
      this._subs.set(id, sub);
      // Record WHICH sockets took the REQ, not merely that we tried — see _eoseDone.
      for (const c of this._conns.values()){ if (c.ws && c.ws.readyState === 1){ c._send(['REQ', id, ...filters]); sub.sent.add(c.url); } }
      // EOSE BACKSTOP. Below, onEose fires once every relay we ASKED has EOSE'd (_eoseDone), and a
      // relay that goes down leaves that count — but one that is UP and simply never answers this
      // filter does not, and callers use EOSE as the backlog→live boundary (`_dmLive`, `_notifReady`,
      // `_followReady`), where never firing freezes them in "this is all history" mode forever — which
      // is how the Messages counter died. Fire it late rather than never. Live subs only: query() has
      // its own timeout and needs `complete` to stay honest.
      if (live && onEose) sub._eoseTimer = setTimeout(()=>{ this._fireEose(sub); }, 12000);
      return id;
    },
    /* Resolve only after this subscription's REQ was written to one of the requested managed
     * sockets. Merely having a Conn in _conns is not readiness: during reconnect it exists with a
     * closed/connecting websocket and subscribe() quite correctly records no URL in `sent`. */
    waitForSubscription(subId, urls, timeout=8000){
      const wanted=new Set((urls||[]).filter(Boolean));
      return new Promise(resolve=>{
        let done=false,iv=null;
        const finish=ok=>{if(done)return;done=true;if(iv)clearInterval(iv);clearTimeout(tm);resolve(!!ok);};
        const check=()=>{const sub=this._subs.get(subId);if(!sub)return finish(false);for(const url of sub.sent)if(wanted.has(url))return finish(true);};
        const tm=setTimeout(()=>finish(false),timeout);iv=setInterval(check,25);check();
      });
    },
    // Deliver a sub's onEose exactly once (whoever gets there first: the last relay's EOSE, or the
    // backstop timer above).
    _fireEose(sub){
      if (!sub || !sub.onEose) return;
      clearTimeout(sub._eoseTimer); sub._eoseTimer = null;
      const cb = sub.onEose; sub.onEose = null; cb();
    },
    close(subId){
      const sub = this._subs.get(subId);
      if (!sub) return;
      clearTimeout(sub._eoseTimer);
      this._send(['CLOSE', subId]); this._subs.delete(subId);
    },
    // Reconnect any socket that looks dead/stale (readyState not open, or nothing received in a while).
    // Called when a query gets NO answer at all — the classic zombie-socket-after-resume case — so the
    // connection self-heals on the FIRST failed query instead of waiting for a resume event or the
    // heartbeat's ~75s zombie check. Only touches sockets that are actually stale (never a busy one).
    reviveStale(){
      for (const c of this._conns.values()){
        const st = c.ws ? c.ws.readyState : 3;                 // no socket → treat as CLOSED
        /* CONNECTING is only evidence of progress for a bounded time. Android WebView can leave a
         * WebSocket handshake in readyState 0 forever after a radio handoff: no open, no error,
         * therefore no Conn retry. The old unconditional `continue` made the client say Offline
         * indefinitely while the relay was healthy. Give a real handshake ten seconds, then try a
         * fresh network path just like a dead socket. */
        const stuck = st === 0 && c._openingAt && Date.now() - c._openingAt > 10000;
        if (st === 0 && !stuck) continue;
        const dead = (st === 2 || st === 3);                   // CLOSING / CLOSED
        const zombie = (st === 1 && c._lastRx && Date.now() - c._lastRx > 30000);  // OPEN but silent
        if (dead || zombie || stuck){
          c._teardownSocket(); clearTimeout(c._rt); c._backoff = 600; try{ c._open(); }catch(_){}
        }
      }
    },
    // Interaction-driven keepalive + recovery, for a mobile PWA where setInterval (the heartbeat) is
    // throttled while the user passively reads. On each call: send a cheap keepalive ping on the trusted
    // socket (refreshes it → defeats a proxy idle-close, PREVENTING a freeze) and, ONLY if that ping draws
    // no reply within a grace window, reconnect (recovers a genuinely frozen zombie). Unlike reviveStale's
    // _lastRx-age test, this can't tear down a merely-quiet-but-healthy socket — a healthy socket answers
    // the ping, so _lastRx advances and no reconnect happens. A dead/closed socket reopens immediately.
    pokeAlive(){
      for (const c of this._conns.values()){
        // Only PROBE an OPEN, trusted socket. A closed/dead socket already has a backoff reconnect
        // scheduled (onclose → _retry); force-reopening it here would cancel that backoff and, on repeated
        // pokes while scrolling, hammer a down relay — so leave dead sockets to _retry. Untrusted relays
        // aren't guaranteed to answer the ping, so they'd false-positive; skip them too.
        if (!c.trusted || !c.ws || c.ws.readyState !== 1) continue;
        const pokeAt = Date.now();
        try{ c._send(['REQ', '_hb', { ids: ['0000000000000000000000000000000000000000000000000000000000000000'] }]); c._send(['CLOSE', '_hb']); }catch(_){}
        setTimeout(() => {
          // Received NOTHING since the poke (the EOSE would set _lastRx >= pokeAt) → genuinely frozen →
          // reconnect. Comparing against pokeAt (not the prior _lastRx) avoids a same-millisecond false
          // positive on a fast link, and a fresh _lastRx from an interim reconnect naturally exempts it.
          if (c.ws && c.ws.readyState === 1 && (c._lastRx || 0) < pokeAt){
            c._teardownSocket(); c._setStatus('off'); c._retry();
          }
        }, 10000);
      }
    },
    // one-shot query across all relays -> resolves with a deduped array after every relay EOSEs
    query(filters, timeout=6000){
      return new Promise((res)=>{
        const got = []; let done = false;
        const settle = (viaTimeout) => {
          this.close(id);
          // No EOSE from ANY relay within the window → the socket is likely a zombie (frozen by a
          // proxy/resume). Kick a reconnect so the retry + the next query succeed.
          if (viaTimeout && !got.length) { try{ this.reviveStale(); }catch(_){} }
          // `complete` = every relay EOSE'd, so the set is the whole answer. False means we gave up on the
          // timer instead, and the result may be PARTIAL — a REQ fired at a still-CONNECTING socket is
          // silently dropped by _send and never draws an EOSE. Non-enumerable so spreads/JSON ignore it.
          try{ Object.defineProperty(got, 'complete', { value: !viaTimeout, enumerable: false, configurable: true }); }catch(_){}
          res(got);
        };
        const finish = (viaTimeout) => { if (done) return; done = true;
          // Drain pending signature verifications before resolving, or events already received from an
          // untrusted relay are thrown away on this path (the EOSE path above already drains them).
          if (viaTimeout && this._vq.length) this._flush().then(() => settle(true), () => settle(true));
          else settle(viaTimeout); };
        const id = this.subscribe(filters, {
          live: false,
          onEvent: ev => got.push(ev),    // pool already deduped by id before delivery
          onEose: () => finish(false)
        });
        setTimeout(() => finish(true), timeout);
      });
    },
    // NIP-45 COUNT: ask the relay for a COUNT(*) instead of fetching the events. Resolves with the
    // highest count any relay reports (the local relay answers fast). Used for follower/following
    // tallies so opening a profile doesn't pull 1000 full contact-list events.
    count(filters, timeout=4000){
      return new Promise((res)=>{
        const id = 'cnt' + Math.random().toString(36).slice(2,9);
        let best = 0, done = false, settle = null;
        const finish = () => { if (done) return; done = true; clearTimeout(settle); this._countWaiters.delete(id); res(best); };
        this._countWaiters.set(id, n => {
          if (n > best) best = n;
          if (!settle) settle = setTimeout(finish, 300);   // got a reply → resolve ~now (300ms grace for other relays), not after the full timeout
        });
        this._send(['COUNT', id, ...filters]);
        setTimeout(finish, timeout);   // hard fallback if no relay answers at all
      });
    },
    // NIP-77 negentropy delta sync: reconcile ONE filter with the TRUSTED relay (range-based set
    // reconciliation, see negentropy.js) and resolve with a Set of event ids the relay HAS that we
    // DON'T — the caller then REQs only those. REJECTS on NEG-ERR / unsupported / timeout / no Negentropy
    // so callers fall back to a plain REQ (this can never lose data). Single-filter (NEG is per-filter).
    negSync(filters, { timeout = 15000 } = {}){
      return new Promise((res, rej) => {
        if (typeof window.Negentropy === 'undefined') return rej(new Error('neg: unavailable'));
        const filter = Array.isArray(filters) ? filters[0] : filters;
        const conn = [...this._conns.values()].find(c => c.trusted && c.ws && c.ws.readyState === 1);
        if (!conn) return rej(new Error('neg: no trusted relay'));
        const items = Store.query([filter]).map(e => ({ ts: e.created_at, id: e.id }));
        const ng = new window.Negentropy(items);
        const id = 'neg' + Math.random().toString(36).slice(2, 9);
        let done = false;
        const tm = setTimeout(() => finish(rej, new Error('neg: timeout')), timeout);
        const finish = (fn, arg) => { if (done) return; done = true; clearTimeout(tm); this._negWaiters.delete(id); try{ conn._send(['NEG-CLOSE', id]); }catch(_){} fn(arg); };
        this._negWaiters.set(id, {
          onMsg: async (hex) => { try{ const { nextMsg } = await ng.reconcile(hex);
            if (nextMsg) conn._send(['NEG-MSG', id, nextMsg]); else finish(res, ng.need); }
            catch(e){ finish(rej, e); } },
          onErr: (reason) => finish(rej, new Error('neg: ' + reason))
        });
        ng.initiate().then(msg => { if (!done) conn._send(['NEG-OPEN', id, filter, msg]); })
          .catch(e => finish(rej, e));
      });
    },
    // publish to every connected relay; resolves on the first relay that accepts (OK true)
    async publish(event, timeout=8000){
      /* A post is an interaction, hence a reliable recovery trigger. On a resumed phone the pool
       * may say off/connecting even though the relay is healthy. ready() repairs dead, zombie and
       * overlong-CONNECTING sockets before the acknowledgement timeout starts. */
      if(!await this.ready(Math.min(3000, timeout))) return { ok:false, msg:'offline' };
      return new Promise((res)=>{
        let settled = false;
        const w = { settle: (r)=>{ if(!settled){ settled = true; clearTimeout(t); this._okWaiters.delete(event.id); res(r); } } };
        // The reason, when there was one: a refused publish must not be reported as silence.
        const t = setTimeout(()=>{ if(!settled){ settled = true; this._okWaiters.delete(event.id);
                                                res({ ok:false, msg: w.why || 'timeout' }); } }, timeout);
        this._okWaiters.set(event.id, w);
        // How many relays were actually written to, so "every one of them refused" is answerable.
        w.sent = this._send(['EVENT', event]) || 0;
      });
    },
    /* Fire-and-forget publish to ONE relay. For the webxdc realtime channel (ephemeral kind 20932),
     * which is the only thing here that sends tens of events a SECOND.
     *
     * NOT THE POOL, deliberately. publish() fans out to every configured relay, so a game running at
     * 30 packets a second would be 30 × however many relays somebody has — a flood aimed at strangers'
     * infrastructure, which will get the user rate-limited or banned, to no purpose: the peers are on
     * this instance's relay, which is where the other player is subscribed. Federating a packet whose
     * whole meaning expires in 50ms is the definition of waste.
     *
     * And no OK waiter: an acknowledgement that arrives after the packet is stale tells nobody
     * anything, and one waiter per packet is a Map that grows at 30 entries a second. */
    /* ONE relay, but it must be a relay that is actually UP.
     *
     * This used to be `_conns.get(url || this.url) || [...values][0]`, and both halves were wrong
     * together: `this.url` is simply urls[0], and the fallback only fired when the map had NO entry
     * at all — which never happens, because a relay that is down or reconnecting KEEPS its entry for
     * the whole session. So while the first relay was reconnecting, every packet was dropped with
     * the rest of the pool connected and nothing said, and two players whose relay lists began with
     * different hosts could never see each other's movement at all.
     *
     * Preference order: the relay the caller named, then the pool's primary (`this.url` — this
     * instance's own relay, where the other player is), then ANY open socket. Never a dropped packet
     * while a live socket exists. */
    publishFast(event, url){
      const up = (c) => !!(c && c.ws && c.ws.readyState === 1);
      let c = this._conns.get(url || this.url);
      if (!up(c)) c = [...this._conns.values()].find(up);
      if (!up(c)) return false;
      c._send(['EVENT', event]);
      return true;
    },
    /* SENDING TO ONE RELAY WHILE LISTENING ON ALL OF THEM IS WHY TWO PLAYERS NEVER MEET.
     *
     * A subscription is placed on the WHOLE pool; publishFast picks a SINGLE socket (the caller's,
     * else the primary, else any). So a packet is heard only if the sender's one relay happens to be
     * in the receiver's pool. Two people whose lists merely START with different hosts — one on the
     * instance's relay, one who enabled their own — broadcast into relays the other never reads, and
     * both screens look perfectly healthy while nothing arrives. Kind 20932 is EPHEMERAL: no relay
     * stores it and no relay forwards it onward, so there is no second chance.
     *
     * Bounded on purpose. The single-relay rule existed because a moving player sends 20-30 packets
     * a second and fanning those across somebody's ten-relay pool is real upstream traffic; `max`
     * keeps that at a small multiple while making the common two-or-three-relay case simply work.
     * The primary goes first so the relay both default clients share is never the one dropped.
     * Returns how many sockets took it — 0 means the packet went nowhere, which the caller must not
     * discard silently. */
    publishFastAll(event, { max = 4 } = {}){
      const up = (c) => !!(c && c.ws && c.ws.readyState === 1);
      const seen = new Set();
      const order = [];
      const primary = this._conns.get(this.url);
      if (up(primary)) { order.push(primary); seen.add(primary); }
      for (const c of this._conns.values()){
        if (order.length >= max) break;
        if (up(c) && !seen.has(c)){ order.push(c); seen.add(c); }
      }
      let n = 0;
      for (const c of order){ try{ c._send(['EVENT', event]); n++; }catch(_){} }
      return n;
    },
    /* Send once to an OPEN managed socket that is actually one of `urls`. Room-scoped realtime
     * traffic must not fall back to an unrelated account relay: Armada is listening on the room's
     * relay set, not whichever socket happens to be first in ours. */
    publishFastTo(urls,event){
      for(const url of (urls||[])){
        const c=this._conns.get(url);
        if(c&&c.ws&&c.ws.readyState===1&&c._send(['EVENT',event]))return true;
      }
      return false;
    },
    // One-shot publish to EXTERNAL relays NOT in the pool — e.g. a DM recipient's NIP-17 inbox relays
    // (their kind-10050) so gift-wrapped DMs reach clients like 0xchat/Amethyst that don't read our
    // relay. Opens a short-lived socket per URL, sends the EVENT, waits for its OK, then closes — no
    // reconnect, no pool membership, deduped + capped fan-out so a send can't spike CPU/sockets.
    // Resolves with the number of relays that accepted. Skips relays already in the pool (publish()
    // covered them) and is a no-op when there are none.
    publishTo(urls, event, { timeout=5000, max=4 } = {}){
      const targets = [...new Set((urls||[]).filter(u=>u&&!_isBlockedRelay(u)))].filter(u => !this._conns.has(u)).slice(0, max);
      if (!targets.length) return Promise.resolve(0);
      return Promise.all(targets.map(u => new Promise(resolve => {
        let ws, done = false, tm;
        const fin = (ok) => { if (done) return; done = true; clearTimeout(tm);
          if (ws){ try{ ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; ws.close(); }catch(_){} }
          resolve(ok ? 1 : 0); };
        try { ws = new WebSocket(u); } catch(_){ return fin(false); }
        tm = setTimeout(()=>fin(false), timeout);
        ws.onopen = () => { try{ ws.send(JSON.stringify(['EVENT', event])); }catch(_){ fin(false); } };
        ws.onmessage = (e) => { let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
          if (m[0] === 'OK' && m[1] === event.id) fin(!!m[2]); };
        ws.onerror = () => fin(false);
        ws.onclose = () => fin(false);
      }))).then(rs => rs.reduce((a,b)=>a+b,0));
    },
    /* A temporary LIVE subscription to one or more relays outside the user's normal pool.
     * Remote Desktop address discovery uses this for a peer's advertised relay: publishing the
     * invite there is only half a signaling path; answers and ICE must be heard there too. External
     * events are signature-verified before delivery and sockets are closed with the returned
     * function, so discovering an IP never silently changes the user's saved relay list. */
    subscribeFrom(urls, filters, { onEvent, timeout=60000, max=4 } = {}){
      const targets=[...new Set((urls||[]).filter(u=>u&&!_isBlockedRelay(u)))].filter(u=>!this._conns.has(u)).slice(0,max);
      const sockets=[]; let closed=false,tm=null,readyDone=false,readyResolve;
      /* Callers that bridge a realtime protocol must not report "joined" before an external
       * socket has actually sent its REQ.  The old API returned its closer immediately, while the
       * websocket was still connecting; ioquake then sent host-election packets into the gap.  A
       * function is still returned for compatibility, with a readiness promise attached. */
      const ready=new Promise(resolve=>{readyResolve=resolve;});
      const markReady=ok=>{if(!readyDone){readyDone=true;readyResolve(!!ok);}};
      const stop=()=>{ if(closed)return;closed=true;if(tm)clearTimeout(tm);sockets.forEach(ws=>{try{ws.close();}catch(_){}}); };
      stop.ready=ready;
      stop.hasTargets=targets.length>0;
      stop.publish=event=>{let sent=0;for(const ws of sockets){try{if(!closed&&ws.readyState===1){ws.send(JSON.stringify(['EVENT',event]));sent++;}}catch(_){}}return sent;};
      if(!targets.length)markReady(true); // every requested URL is already in the managed pool
      if(timeout>0) tm=setTimeout(stop,timeout);
      targets.forEach((u,n)=>{
        let ws; const id='xf'+Math.random().toString(36).slice(2,9)+n;
        try{ws=new WebSocket(u);sockets.push(ws);}catch(_){return;}
        ws.onopen=()=>{try{ws.send(JSON.stringify(['REQ',id,...filters]));markReady(true);}catch(_){}};
        ws.onmessage=async e=>{let m;try{m=JSON.parse(e.data);}catch(_){return;}
          if(closed||m[0]!=='EVENT'||m[1]!==id||!m[2])return;
          try{const rs=await worker.call('verifyBatch',{events:[m[2]]});
            if(!closed&&rs&&rs[0]&&rs[0].valid&&onEvent)onEvent(this._normTags(m[2]));}catch(_){} };
      });
      /* Do not let a failed external relay leave a realtime join pending for a full minute. */
      if(targets.length)setTimeout(()=>markReady(false),8000);
      return stop;
    },
    // One-shot AUTHENTICATED publish to a single EXTERNAL relay (e.g. a NIP-29 group relay like
    // groups.0xchat.com that requires NIP-42 AUTH to write). Opens a short-lived socket, sends the
    // EVENT optimistically, and if the relay issues an ["AUTH", challenge] it calls signAuth(challenge)
    // (which must resolve to a signed kind-22242 event) and replays the EVENT once authed. Resolves
    // { ok, msg } like publish(). signAuth runs in the app (it owns the active signer — extension /
    // NIP-46 / local key), so relay.js stays signer-agnostic.
    publishAuthed(url, event, signAuth, { timeout = 9000 } = {}){
      if(_isBlockedRelay(url))return Promise.resolve({ok:false,msg:'relay unavailable'});
      return new Promise(resolve => {
        let ws, done = false, tm, rtm, pgm, authSent = false, authedEventId = null, authResent = false, lastReject = '';
        const fin = (r) => { if (done) return; done = true; clearTimeout(tm); clearTimeout(rtm); clearTimeout(pgm);
          if (ws){ try{ ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; ws.close(); }catch(_){} }
          resolve(r); };
        const sendEvent = () => { try{ ws.send(JSON.stringify(['EVENT', event])); }catch(_){ fin({ ok:false, msg:'send failed' }); } };
        const resendAfterAuth = () => { if (authResent) return; authResent = true; clearTimeout(rtm); sendEvent(); };
        try { ws = new WebSocket(url); } catch(_){ return fin({ ok:false, msg:'connect failed' }); }
        // On timeout, report the relay's last rejection reason if it gave one (e.g. "blocked: not a
        // member") rather than a generic "timeout" — far more actionable for the user.
        tm = setTimeout(()=>fin({ ok:false, msg: lastReject || 'timeout' }), timeout);
        ws.onopen = () => sendEvent();   // optimistic — relay may accept without auth, else it'll challenge
        ws.onmessage = async (e) => {
          let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
          if (m[0] === 'AUTH' && m[1] && !authSent){            // NIP-42 challenge → sign & answer
            authSent = true; clearTimeout(pgm);                 // a challenge arrived → don't give up early on the pre-auth reject

            let a; try{ a = await signAuth(m[1]); }catch(_){ return fin({ ok:false, msg:'auth sign failed' }); }
            if (!a || !a.id){ return fin({ ok:false, msg:'auth declined' }); }   // signer resolved empty (declined) instead of throwing
            authedEventId = a.id;
            try{ ws.send(JSON.stringify(['AUTH', a])); }catch(_){ return fin({ ok:false, msg:'auth send failed' }); }
            // Resend the EVENT once the relay ACCEPTS our AUTH (strict relays); also fall back after a
            // short delay for relays that authenticate silently (accept AUTH without OK-ing the 22242).
            rtm = setTimeout(resendAfterAuth, 1200);
            return;
          }
          if (m[0] === 'OK'){
            if (m[1] === event.id){ if (m[2]) return fin({ ok:true, msg:m[3]||'' });
              lastReject = m[3] || 'rejected';
              // pre-auth reject → the relay MAY still send an AUTH challenge; wait briefly, but if none
              // comes (relay rejected for a non-auth reason like non-membership), fail fast with the reason
              // instead of stalling the full timeout.
              if (!authSent){ if (!pgm) pgm = setTimeout(()=>fin({ ok:false, msg: lastReject }), 1500); return; }
              return fin({ ok:false, msg: lastReject }); }       // rejected even after auth → real failure
            if (m[1] === authedEventId){ if (m[2]) return resendAfterAuth();   // auth accepted → send the real event now
              return fin({ ok:false, msg:'auth rejected: '+(m[3]||'') }); }
          }
        };
        ws.onerror = () => fin({ ok:false, msg: lastReject || 'socket error' });
        ws.onclose = () => fin({ ok:false, msg: lastReject || 'closed' });
      });
    },
    // One-shot READ from EXTERNAL relays NOT in the pool — e.g. discovery/indexer relays to find a
    // non-WoT peer's NIP-17 inbox list (kind 10050), which our WoT-only relay never stored. Same
    // bounded ephemeral-socket pattern as publishTo: REQ, collect until EOSE/timeout, close. Events
    // are UNVERIFIED here (untrusted relays) — the caller must verify signatures before trusting them.
    queryFrom(urls, filters, { timeout=4000, max=4, exact=false, signal=null, purpose='external read', minInterval=0, allowBlocked=false, failureCooldown=30000 } = {}){
      /* Most external discovery reads should avoid duplicating a connected pool socket. Some
       * protocols bind truth to one named relay (notably NIP-29), so exact=true deliberately opens
       * that relay even when it is also present in the shared pool. */
      const blocked=u=>{try{return this._queryFromBlockedHosts.has(new URL(String(u)).hostname.toLowerCase());}catch(_){return true;}},
        now=Date.now(),targets = [...new Set((urls||[]).filter(Boolean))]
        .filter(u => (allowBlocked || !blocked(u)) && (exact || !this._conns.has(u)) && !this._queryFromActive.has(u) && Number(this._queryFromCooldown.get(u)||0)<=now && (minInterval<=0 || Number(this._queryFromPurposeCooldown.get(u+'\0'+purpose)||0)<=now))
        .slice(0, max);
      if (!targets.length || (signal && signal.aborted)) return Promise.resolve([]);
      const subId = 'qf' + Math.random().toString(36).slice(2,9);
      return Promise.all(targets.map(u => new Promise(resolve => {
        let ws, done = false, tm; const got = [];
        const abort = () => fin('abort');
        const stop = () => fin('abort');
        const fin = (outcome='failure') => { if (done) return; done = true; clearTimeout(tm);
          if (signal) signal.removeEventListener('abort', abort);
          Relay._queryFromStops.delete(stop);Relay._queryFromActive.delete(u);
          if(outcome==='failure'){
            Relay._queryFromCooldown.set(u,Date.now()+Math.max(30000,Number(failureCooldown)||0));
            try{console.warn('[relay queryFrom]',purpose,u,'failed; cooling down');}catch(_){}
          }else if(outcome==='success'){
            Relay._queryFromCooldown.delete(u);
            if(minInterval>0)Relay._queryFromPurposeCooldown.set(u+'\0'+purpose,Date.now()+minInterval);
          }
          if (ws){ try{ ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null; ws.close(); }catch(_){} }
          resolve(got); };
        if (signal){ if (signal.aborted) return fin('abort'); signal.addEventListener('abort', abort, {once:true}); }
        Relay._queryFromActive.add(u);Relay._queryFromStops.add(stop);
        try { ws = new WebSocket(u); } catch(_){ return fin('failure'); }
        tm = setTimeout(fin, timeout);
        ws.onopen = () => { try{ ws.send(JSON.stringify(['REQ', subId, ...filters])); }catch(_){ fin(); } };
        ws.onmessage = (e) => { let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
          if (m[0] === 'EVENT' && m[1] === subId && m[2]) got.push(Relay._normTags(m[2]));
          else if ((m[0] === 'EOSE' || m[0] === 'CLOSED') && m[1] === subId) fin('success'); };
        ws.onerror = () => fin('failure');
        ws.onclose = () => fin('failure');
      }))).then(rs => rs.flat());
    }
  };

  window.Relay = Relay;
})();

/* The two signers that are not this device's own key — NIP-55 (an Android signer app, reached over
 * intents) and NIP-46 (a remote signer over a relay: bunker:// and nostrconnect://) — and the other
 * side of NIP-46, the SERVICE this app runs FOR other clients: the apps it signs for, their
 * permissions, the sockets, and the native background service that keeps them answered. Split out
 * of app.js.
 *
 * It ships with the page rather than loading on demand: every signature a remote-signer account
 * takes goes through one of these, `Nip46.revive()` and `Nip46Signer.revive()` run at startup, and
 * app.js reads all three as objects rather than calling entry points. app.js keeps the three NAMES,
 * each bound to a Proxy onto the object in here (see `_lzProxy`), and builds the factory the first
 * time one of them is touched.
 *
 * The code below is BYTE-IDENTICAL to what it replaced apart from its reads of app.js's live `let`
 * bindings, which the parser rewrote to `S.<name>` (getters/setters on `dep.state`) at exact
 * identifier offsets.
 *
 * Stayed in app.js: `makeSigner`, which picks between these, a NIP-07 extension and the local key;
 * the guard that refuses a nip44 operation a signer cannot do; publish/_publishNow; the sign-in
 * screens that drive a connection; and the signer SETTINGS screen, which is in settings.js.
 */
window.PCSignerFactory = function(dep){
  const S = dep.state;   // live app.js bindings: S.CFG, S.ME
  const {
    NT, _capPlugin, _eventId, _instanceBase, _ncRelays, _renderSignerApps, toast, uiConfirm,
  } = dep;

  // ---------- NIP-55: on-device Android signer (Amber) ----------
  // The native counterpart of NIP-46. Same guarantee — the key never enters this app — but the exchange
  // is an Android intent to a signer installed on the SAME phone instead of a round trip through a relay,
  // so it works offline, needs no bunker URI to paste, and has no pairing to expire. Only exists inside
  // the APK: the Nip55 Capacitor plugin is what talks to the OS, and it is absent in a browser.
  const Nip55 = {
    available: false, signers: [], pkg: '', npub: '',
    _p(){ try{ return (window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.Nip55) || null; }catch(_){ return null; } },
    async probe(){
      const p = this._p(); if(!p) return false;
      try{ const r = await p.isAvailable(); this.available = !!(r && r.available); this.signers = (r && r.signers) || []; }
      catch(_){ this.available = false; }
      // Seed the package when exactly ONE signer is installed. The silent (content-resolver) path needs a
      // package name, and the only other source is the `package` extra on a result — which not every signer
      // sets. Without this seed a signer that omits it would foreground itself for every single reaction
      // and every DM decrypted. Unambiguous by construction: one app claims the scheme, so we address it.
      if(this.available && !this.pkg && this.signers.length === 1) this.pkg = this.signers[0].package || '';
      return this.available;
    },
    // Android delivers activity results ONE at a time, and Capacitor tracks a single "last plugin call" for
    // them — so two overlapping intent requests would resolve against each other's call and hand back the
    // wrong plaintext. Decrypting a screenful of DMs fires exactly that pattern, so every request goes
    // through one queue. The silent resolver path would tolerate parallelism, but the caller cannot know
    // which path a request will take until it is already running.
    _q: Promise.resolve(),
    _serial(fn){
      const run = this._q.then(fn, fn);
      this._q = run.then(()=>{}, ()=>{});   // keep the chain alive after a rejection
      return run;
    },
    // Every permission we will ever need, asked for ONCE at login. A signer that remembers the grant can
    // then answer later calls silently (the plugin's content-resolver path) instead of jumping to the
    // foreground for every reaction and every DM we decrypt.
    _perms(){
      return JSON.stringify(['sign_event','nip04_encrypt','nip04_decrypt','nip44_encrypt','nip44_decrypt']
        .map(t => ({ type: t })));
    },
    _req(type, opts){
      return this._serial(async () => {
        const p = this._p(); if(!p) throw new Error('signer bridge unavailable');
        const r = await p.request(Object.assign({ type, pkg: this.pkg, currentUser: this.npub }, opts||{}));
        // Carry the signer's OWN words when it gave any. Ours says "no key on this device" for a
        // phone that has not been handed a key yet, which is a fixable state the user can only act
        // on if they hear it — flattened to "declined" it reads as the signer being broken.
        if(!r || r.rejected) throw new Error((r && r.error) || 'the signer declined');
        if(r.package && !this.pkg) this.pkg = r.package;   // learn the package → unlocks the silent path
        return r;
      });
    },
    async getPublicKey(){
      const r = await this._req('get_public_key', { permissions: this._perms() });
      const v = (r.result||'').trim();
      if(!v) throw new Error('the signer returned no key');
      const hex = /^npub1/i.test(v) ? NT().nip19.decode(v).data : v;
      this.npub = /^npub1/i.test(v) ? v : NT().nip19.npubEncode(hex);
      return hex;
    },
    async signEvent(tpl){
      const r = await this._req('sign_event', { content: JSON.stringify(tpl), id: String(Date.now()) });
      // Prefer the signer's own signed event — it is authoritative about created_at/id. Older signers
      // return only the signature, so rebuild the event around it (the id is a pure function of the
      // template, so this cannot disagree unless the signer altered the template).
      if(r.event){ try{ return JSON.parse(r.event); }catch(_){} }
      if(!r.result) throw new Error('the signer returned no signature');
      const ev = Object.assign({}, tpl); ev.id = await _eventId(ev); ev.sig = r.result; return ev;
    },
    async _crypt(type, peer, payload){
      const r = await this._req(type, { content: payload, pubkey: peer });
      if(r.result == null) throw new Error('the signer returned nothing');
      return r.result;
    },
    nip04enc(peer, text){ return this._crypt('nip04_encrypt', peer, text); },
    nip04dec(peer, ct){ return this._crypt('nip04_decrypt', peer, ct); },
    nip44enc(peer, text){ return this._crypt('nip44_encrypt', peer, text); },
    nip44dec(peer, ct){ return this._crypt('nip44_decrypt', peer, ct); },
  };

  // ---------- NIP-46 remote signer (Amber / nsecbunker) ----------
  // The user's secret key lives in the remote signer. We hold an EPHEMERAL "app key" (in the
  // worker) purely to encrypt/sign the NIP-46 transport (kind-24133 events over the signer's relay);
  // every user-facing sign/encrypt is forwarded to the signer, which prompts the user to approve.
  /* How far back the response subscription looks, in seconds.
   *
   * This was 5, and 5 is a CLOCK-SKEW BUDGET, not a freshness window. The signer stamps its reply
   * with the PHONE's clock, and the relay applies `since` server-side — so a phone running half a
   * minute behind the desktop had its connect ack dropped before it ever reached us. Amber says it
   * paired, the browser sits on "waiting for the signer to approve…" forever, and nothing anywhere
   * reports an error, because from our side no event arrived. Phones drift; a signer is by
   * definition a different machine with a different clock, so the window has to be big enough for
   * one that was never NTP-synced.
   *
   * Generous but still bounded. Nothing old can be replayed into a pairing: beginNostrConnect
   * generates a FRESH app key, so no 24133 addressed to it can predate this attempt — and on resume
   * every reply is matched by request id, where a stale one matches nothing. */
  const NIP46_SINCE_SKEW = 900;

  /* When an unanswered interactive request is published AGAIN, in ms after the one before it.
   *
   * A ladder rather than a fixed interval, because the two things being waited for have completely
   * different shapes. A ZOMBIE socket is discovered on the first rung (see the deaf check in _rpc)
   * and fixed immediately. A SIGNER that is redialling comes back at a moment nobody here can know,
   * so the early rungs are close together — a coarse interval means the request lands seconds after
   * the signer returned and then waits out the whole gap for the next try, which is exactly what a
   * 20s second rung did (measured: 26s to sign, where 16s was available).
   *
   * Bounded at four. Each rung is a duplicate request, and a signer that asks a human to approve
   * every one of them can show a prompt for each — the event is byte-identical so approving any of
   * them gives the same signature, but four is where "make sure it lands" stops being worth it. */
  const _RESEND_AT = [6000, 10000, 20000, 30000];

  const Nip46 = {
    relay:null, appSk:null, appPk:null, remotePk:null, userPk:null,
    /* The encryption this SESSION writes with, settled once at pairing and then never changed.
     * NIP-46 began on NIP-04 and the current spec is NIP-44, so both are in the wild — Amber reads
     * either, a modern bunker may read only NIP-44, an old one only NIP-04. Defaults to nip04
     * because that is what every signer still reads, and because a session paired before this
     * existed has no recorded value. */
    _enc:'nip04',
    _lastDec:null,        // scheme the last decodable message ARRIVED in (diagnostic + connect ack)
    _encOk:false,         // the peer decrypted something we sent → our scheme is right
    _pending:new Map(), _subId:null, _onEvent:null,
    /* Every relay socket this session holds — not one. See _openAll. */
    _socks:[],
    /* The relay URLs this session WANTS open, which is not the same list as the sockets it has.
     *
     * A socket carries its own url on `_pcUrl`, and that was the only record of it — so the moment a
     * socket closed, the url went with it and nothing could reopen what had just been lost. That is
     * survivable while a close is followed by an immediate successful retry and fatal the moment one
     * isn't (see _scheduleReopen). Kept per SESSION, cleared only by reset(). */
    _urls:[], _boff:{}, _rtimer:{}, _revivedAt:0, _generation:0, _opening:{}, _openingCancel:{},
    _live(){ return (this._socks||[]).filter(w => w && w.readyState === 1); },
    /* A REPLY THAT WAS IN FLIGHT WHEN THE LAST SOCKET DIED IS NEVER ARRIVING.
     *
     * Kind 24133 is EPHEMERAL — this relay stores nothing and fans out only to whoever is listening
     * at that instant. So once every socket is gone, a request already sent and unanswered cannot be
     * answered: there is no copy anywhere, and the reply has nowhere to land. It nevertheless sat in
     * `_pending` holding a lane slot until its ceiling — 120s for a signature, 45s for a decrypt.
     *
     * The interactive lane is TWO slots wide, so two dead requests block every signature for two
     * minutes. That is a node restart in one sentence, and it was reported as all of its symptoms at
     * once: "every time you restart, desktop takes a long time to recover and post", "queue builds
     * up", "i just seen signer request timed out", and — because a publish gives the relay 8s before
     * the post is filed — "i have a bunch of drafts now".
     *
     * Only when NO socket survives. Requests are fanned out to every relay this session holds, so
     * while one is still up the reply may yet arrive on it and failing would be wrong.
     *
     * The error is deliberately the retryable wording `_send` already knows: it reconnects through
     * `_ensure` and re-sends, which is what the user was doing by hand by pressing Post again. */
    _failPending(why){
      if(!this._pending || !this._pending.size) return;
      const dead=[...this._pending.entries()];
      this._pending.clear();
      for(const [,p] of dead){ try{ p.rej(new Error(why)); }catch(_){} }
      try{ console.warn('[nip46] signer relay lost — released ' + dead.length + ' in-flight request(s)'); }catch(_){}
    },
    _want(url){ if(url && this._urls.indexOf(url) < 0) this._urls.push(url); },
    reset(){ this._wantOpen=false; this._generation++;
      // Settle and retire attempts from the old account before another session can dial.
      Object.values(this._openingCancel||{}).forEach(cancel=>{try{cancel();}catch(_){}});
      this._opening={};this._openingCancel={};
      this._enc='nip04'; this._lastDec=null; this._encOk=false; this._encSeen=null;
      (this._socks||[]).forEach(w=>{ try{ w.onclose=w.onerror=w.onmessage=null; w.close(); }catch(_){} });
      Object.keys(this._rtimer||{}).forEach(u=>{ try{ clearTimeout(this._rtimer[u]); }catch(_){} });
      this._socks=[]; this._urls=[]; this._boff={}; this._rtimer={}; this.relay=null; this._subId=null;
      this._pending.forEach(p=>{ try{ p.rej(new Error('signer disconnected')); }catch(_){} }); this._pending.clear();
      // Fail the queued work too — otherwise jobs waiting for a slot hang on a socket that's gone.
      const q=(this._queue||[]).concat(this._queueP||[]);
      this._queue=[]; this._queueP=[]; this._inflight=0; this._inflightP=0;
      q.forEach(j=>{ try{ j.rej(new Error('signer disconnected')); }catch(_){} });
      this._onEvent=null; },
    // NIP-46 transport is NIP-04 by default, but some signers reply with NIP-44 — try each scheme
    // through to a valid JSON payload (a wrong scheme may return garbage rather than throw).
    async _decode(peer, ct){
      /* `?iv=` is NIP-04's own marker — its payload is `<base64>?iv=<base64>`, where NIP-44's is a
       * single base64 blob whose first decoded byte is the version. So try the scheme the ciphertext
       * announces FIRST, then the other one anyway: a signer that ignores what we sent must still be
       * readable. (Reading a NIP-04 payload as NIP-44 is what produces "Unsupported NIP-44 version:
       * 150" in a signer's log — 150 is just the first byte of AES ciphertext, different every time.)
       *
       * READING is tolerant; WRITING is not. We always send NIP-04, which every signer in the wild
       * reads. Mirroring the signer's scheme on the outbound side was tried and REVERTED: it makes
       * every future signature depend on correctly inferring a scheme from one earlier message, and
       * when that inference is wrong nothing reports it — the request is simply never decrypted, so
       * replying and posting stop working with no error anywhere. That is a bad trade against a
       * problem the signer end can fix by detecting `?iv=`, which is what NIP-04 marks itself with. */
      const ops = /\?iv=/.test(String(ct || '')) ? ['nip04dec','nip44dec'] : ['nip44dec','nip04dec'];
      for(const op of ops){
        try{
          const pt = JSON.parse((await Relay.worker.call(op,{ peer, ct })).pt);
          this._lastDec = (op === 'nip04dec') ? 'nip04' : 'nip44';
          return pt;
        }catch(_){}
      }
      return null;
    },
    // load (or reuse) the ephemeral app key into the worker
    async _ensureAppKey(sk){
      const generation=this._generation;
      const check=()=>{if(generation!==this._generation)throw Object.assign(new Error('signer disconnected'),{cancelledSession:true});};
      const g = sk ? { sk } : await Relay.worker.call('genKey', {});
      check();
      const r = await Relay.worker.call('setKey', { sk: g.sk });
      check();
      this.appSk = g.sk; this.appPk = r.pubkey; return this.appPk;
    },
    // The response subscription, sent on every socket the moment it opens.
    _req(ws){
      ws.send(JSON.stringify(['REQ', this._subId,
        { kinds:[24133], '#p':[this.appPk], since: Math.floor(Date.now()/1000)-NIP46_SINCE_SKEW }]));
    },
    /* Wire one open socket into the session: subscribe, listen, and reconnect if it drops.
     *
     * A remote signer is contacted only when signing, so a relay will idle-drop us between
     * signatures — reconnecting (and re-subscribing) is what lets the next sign go through without
     * forcing a re-pair. Only while `_wantOpen`, so reset() genuinely disconnects. */
    _adopt(ws, url){
      if(this._socks.indexOf(ws) < 0) this._socks.push(ws);
      this._want(url);
      this._boff[url] = 0;                          // a good connection resets that relay's backoff
      if(!this.relay) this.relay = url;             // the first to open names the session's relay
      this._subId = this._subId || ('n46'+Math.random().toString(36).slice(2,8));
      this._req(ws);
      /* EVERY OUTSTANDING REQUEST RIDES THE FRESH SOCKET. A kind-24133 is ephemeral — a request
       * published just before the relay went down was fanned out to nobody and DESTROYED, and the
       * two lanes recover from that at two very different speeds: interactive requests have the
       * re-send ladder, but the bulk lane deliberately has none ("no duplicate storm"), so a bulk
       * decrypt destroyed by a relay RESTART sat on its slot for the full 45s ceiling and then
       * _send's retry spent more — measured 51.7s for a fresh decrypt queued behind 30 of them
       * (scripts/check_nip46_bulk_lane.py), while sign_event answered in 4ms. That split is the
       * report verbatim: "blossom waiting forever for signer wtf … I could post fine" — everything
       * Blossom needs (the drive master key, manifests) is a bulk nip44_decrypt.
       *
       * Re-sending HERE is not a timer and not a ladder: it fires exactly when a socket reopened,
       * which is the only moment the destroyed-copy theory is both likely and fixable. At most
       * _cap+_capP requests are ever pending, the events are byte-identical (duplicate replies are
       * dropped by request id), and a signer that prompts per request can at worst prompt twice for
       * an action that would otherwise silently die. */
      if(this._pending && this._pending.size){
        for(const p of this._pending.values()){
          if(p && p.signed) try{ ws.send(JSON.stringify(['EVENT', p.signed])); }catch(_){}
        }
      }
      ws.onerror = null;
      ws.onmessage = (e)=>this._recv(e.data);
      ws.onclose = ()=>{
        this._socks = this._socks.filter(w => w !== ws);
        /* Nothing can answer a request that was outstanding on the LAST socket — see _failPending.
           CONNECTING counts as a socket: `_openAll` opens every relay the session knows and a dead
           one closing while a good one is still dialling would otherwise look like "all gone" and
           kill the connect request that pairing is waiting on. Same `readyState <= 1` test
           `_scheduleReopen` uses to decide a url is already covered. */
        if(!(this._socks||[]).some(w => w && w.readyState <= 1))
          this._failPending('lost the signer relay — retrying');
        this._scheduleReopen(url);
      };
      ws._pcUrl = url;
    },
    /* Reconnect that KEEPS TRYING — the one-attempt version is why a signed-in desktop woke up
     * unable to sign.
     *
     * This was a single `setTimeout(…, 2000)` whose failure was swallowed by `.catch(()=>{})`, and
     * a machine coming back from sleep is precisely the case it cannot survive: every socket closes
     * on suspend, the one retry fires two seconds into the resume — before the wifi has associated,
     * before DNS answers — and fails. Nothing is scheduled after it, nothing is logged, and the
     * session is left `_wantOpen` with no sockets for the rest of the page's life. The next thing
     * the user clicks throws `signer not connected` from _rpc, and reloading the page is the only
     * cure, which is exactly how it was reported. The phone signer was never involved.
     *
     * So: exponential backoff, capped, and it never stops while the session wants the relay. The cap
     * is 20s rather than the pool's 8s because nothing here is waiting on the socket to draw a
     * screen — it only has to be up before the next signature, and _ensure() closes even that gap. */
    _scheduleReopen(url){
      if(!this._wantOpen || !url) return;
      if(this._socks.some(w => w._pcUrl === url && w.readyState <= 1)) return;   // open or connecting
      if(this._rtimer[url]) return;                                              // already scheduled
      const d = this._boff[url] ? Math.min(this._boff[url] * 1.7, 20000) : 1500;
      this._boff[url] = d;
      this._rtimer[url] = setTimeout(()=>{
        this._rtimer[url] = 0;
        if(!this._wantOpen) return;
        if(this._socks.some(w => w._pcUrl === url && w.readyState <= 1)) return;
        this._openRelay(url).catch(e=>{ if(!e.cancelledSession)this._scheduleReopen(url); });
      }, d);
    },
    /* Reopen NOW — the page came back from sleep / the network returned.
     *
     * Two jobs, and the second is the one a backoff cannot do: it cancels the pending backoff so a
     * relay that has been failing for a while is retried immediately, and it tears down sockets the
     * browser still calls OPEN. A suspended machine routinely comes back with a zombie socket —
     * readyState 1, delivering nothing — and a request published into one does not fail, it waits
     * out the full 120s ceiling while the signer is never asked. Same reasoning as Relay.wake(),
     * and at most a couple of sockets. */
    revive(){
      if(!this._wantOpen || !this._urls.length) return;
      if(Date.now() - this._revivedAt < 3000) return;
      this._revivedAt = Date.now();
      (this._socks||[]).forEach(w=>{ try{ w.onclose=w.onerror=w.onmessage=null; w.close(); }catch(_){} });
      this._socks = [];
      this._urls.forEach(url=>{
        try{ clearTimeout(this._rtimer[url]); }catch(_){}
        this._rtimer[url] = 0; this._boff[url] = 0;
        this._openRelay(url).catch(e=>{ if(!e.cancelledSession)this._scheduleReopen(url); });
      });
    },
    /* A live socket before a request goes out, or the truth about why there isn't one.
     *
     * _rpc used to throw `signer not connected` the instant `_live()` was empty, which reads to the
     * user as "your signer is broken" when what actually happened is that this page's socket died
     * while the machine was asleep and the reconnect is seconds away. Reconnecting HERE is what
     * makes the first click after a resume work rather than being the thing that discovers the
     * problem — and it does not depend on any wake event arriving, which on the desktop build is not
     * guaranteed at all (Chromium reports no visibility change for a window that was never hidden,
     * and `online` does not fire when the interface never went down). */
    async _ensure(ms){
      if(this._live().length) return true;
      if(!this._wantOpen || !this._urls.length) return false;
      this._urls.forEach(url=>{
        try{ clearTimeout(this._rtimer[url]); }catch(_){}
        this._rtimer[url] = 0; this._boff[url] = 0;
        if(!this._socks.some(w => w._pcUrl === url && w.readyState <= 1))
          this._openRelay(url).catch(e=>{ if(!e.cancelledSession)this._scheduleReopen(url); });
      });
      const t0 = Date.now(), cap = ms || 9000;
      while(Date.now() - t0 < cap){
        if(this._live().length) return true;
        await new Promise(r=>setTimeout(r, 120));
      }
      return !!this._live().length;
    },
    // open a socket to the signer's relay + subscribe for responses addressed to our app key
    _openRelay(relay){
      this._wantOpen=true;
      this._want(relay);
      if(this._live().some(w=>w._pcUrl===relay))return Promise.resolve(relay);
      // Initial pairing, resume and retries share the same attempt. A still-connecting
      // socket is not in _socks yet, so checking only that list creates duplicate listeners.
      if(this._opening[relay])return this._opening[relay];
      const generation=this._generation;
      let resolve, reject, ws, timer, done=false;
      const promise=new Promise((res,rej)=>{resolve=res;reject=rej;});
      this._opening[relay]=promise;
      const retire=()=>{
        if(ws)try{ws.onopen=ws.onclose=ws.onerror=ws.onmessage=null;ws.close();}catch(_){}
      };
      const finish=(ok,value)=>{
        if(done)return;done=true;clearTimeout(timer);
        if(this._opening[relay]===promise){delete this._opening[relay];delete this._openingCancel[relay];}
        if(!ok)retire();
        (ok?resolve:reject)(value);
      };
      this._openingCancel[relay]=()=>finish(false,Object.assign(new Error('signer disconnected'),{cancelledSession:true}));
      try{ws=new WebSocket(relay);}
      catch(_){finish(false,new Error('cannot reach signer relay'));return promise;}
      ws.onopen=()=>{
        if(done || generation!==this._generation || !this._wantOpen){retire();return;}
        this._adopt(ws,relay);finish(true,relay);
      };
      ws.onmessage=e=>{if(generation===this._generation)this._recv(e.data);};
      ws.onerror=ws.onclose=()=>finish(false,new Error('cannot reach signer relay'));
      timer=setTimeout(()=>finish(false,new Error('signer relay timed out')),20000);
      return promise;
    },
    /* Open EVERY relay this session knows, and keep them all.
     *
     * This raced them and kept only the first to open, and that is wrong in the one way that matters:
     * the signer is not on all of them. A bunker:// URI lists the relays the signer is LISTENING on,
     * while a resumed session adds this instance's own relay as a fallback — so "fastest socket wins"
     * routinely picked our relay, which is nearby and always up, and sent every request to a room the
     * signer is not in. Nothing errors: the request is published, the relay accepts it, and the app
     * waits out its 120s ceiling. Reported as "existing sessions say waiting for signer and nothing
     * shows up in Amber", and it is also why a dead stored relay used to look identical.
     *
     * So a request goes to ALL of them (see _rpc) and a reply is taken from whichever answers —
     * every one is matched by request id, so a duplicate from a second relay settles nothing twice.
     * This resolves on the FIRST socket to open, because a login must not wait for the slowest, and
     * the others join as they arrive. It rejects only when every one of them has failed. */
    _openAll(list, ms){
      const urls=[...new Set((Array.isArray(list)?list:[list]).filter(Boolean))];
      if(!urls.length)return Promise.reject(new Error('no signer relay configured'));
      this._wantOpen=true;
      const generation=this._generation;
      return new Promise((res,rej)=>{
        let opened=false,dead=0;
        const fail=error=>{
          if(opened)return;
          if(error && error.cancelledSession){clearTimeout(timer);rej(error);return;}
          if(++dead<urls.length)return;
          clearTimeout(timer);
          rej(new Error('no signer relay is reachable right now — try again in a minute'));
        };
        const timer=setTimeout(()=>{if(!opened)rej(new Error('signer relays timed out'));},ms||20000);
        urls.forEach(url=>{
          this._openRelay(url).then(()=>{
            if(generation!==this._generation)return fail(Object.assign(new Error('signer disconnected'),{cancelledSession:true}));
            if(!opened){opened=true;clearTimeout(timer);res(url);}
          },e=>{
            fail(e);
            if(generation===this._generation && !e.cancelledSession)this._scheduleReopen(url);
          });
        });
      });
    },
    async _recv(raw){
      /* WHEN ANYTHING LAST ARRIVED, stamped before the message is even parsed — an `OK`, an `EOSE`,
       * a `NOTICE`, anything. It is not about this session's content; it is the only evidence a page
       * has that its socket is REAL. A resumed machine's socket routinely reports `readyState 1`
       * while delivering nothing, and every send() into it succeeds. See the deaf check in _rpc. */
      this._rxAt = Date.now();
      let m; try{ m=JSON.parse(raw); }catch(_){ return; }
      if(m[0]!=='EVENT' || m[1]!==this._subId) return;
      const ev=m[2]; if(!ev || ev.kind!==24133) return;
      const payload=await this._decode(ev.pubkey, ev.content); if(!payload) return;
      // A payload carrying one of OUR request ids means the peer read what we sent, whatever it
      // replied with. That — not the reply's own encoding — is what confirms our outbound scheme,
      // and it counts even for an auth_url, which never settles the pending promise. `_encSeen` is
      // the scheme THAT request went out in, which is the part a two-probe pairing needs to know.
      if(payload && payload.id && this._pending.has(payload.id)){
        this._encOk = true; this._encSeen = (this._pending.get(payload.id)||{}).enc || this._encSeen;
      }
      if(this._onEvent) try{ this._onEvent(ev, payload); }catch(_){}   // nostrconnect handshake hook
      // the signer needs the user to approve in-app → open the deep link / approval URL
      if(payload.result==='auth_url' || (payload.error && /^https?:\/\//i.test(payload.error||''))){
        try{ window.open(payload.error,'_blank'); }catch(_){} return;
      }
      const p=this._pending.get(payload.id);
      if(p){ this._pending.delete(payload.id); payload.error ? p.rej(new Error(payload.error)) : p.res(payload.result); }
    },
    // Every request here is answered by a phone, over a relay. Restoring a DM history fires one unwrap per
    // gift-wrap — and each unwrap is TWO decryptions — so a few hundred messages meant a few hundred
    // simultaneous requests at Amber, which drops/rate-limits them; the failures were swallowed and the DMs
    // simply never appeared ("most of my DMs didn't restore"). With a local key this never showed, because the
    // worker decrypts instantly.
    //
    // So the transport is a QUEUE: a few in flight at a time, each retried once. Slower, but it finishes.
    // 6 in flight: enough to actually chew through a DM history (each message = 2 decryptions), while still
    // far short of the hundreds-at-once that made Amber drop requests.
    /* TWO LANES, not one queue with a priority end — because jumping the queue does not help if
     * every SLOT is taken.
     *
     * The interactive job went to the front of the same 6-slot queue, and a stalled transport is
     * exactly when that is not enough: a DM restore fills all six slots with decrypts, each of which
     * now waits out the full 120s ceiling (twice — _send retries once) before releasing its slot.
     * The sign_event is first in line and still cannot start for minutes, which is reported as
     * "waiting for your signer…" and a draft that never sends, with the signer itself perfectly
     * healthy and never even asked. So signing has its own slots that bulk work can never occupy. */
    /* TWELVE, raised from six once the signer could take it — and the reason is measured, not
     * hopeful. Six was chosen against a signer that answers on ONE thread and drops what it cannot
     * keep up with; ours now answers on a pool of three with the crypto in C, and the relay shows
     * requests and replies at 1:1 (635/642 over a minute) with nothing dropped at any point today.
     * With six in flight the first pass of a 472-message history is 944 round trips at ~4 pairs a
     * second — four minutes of a progress bar — and the limit is the round trip, not the phone.
     *
     * Not "hundreds", which is what made Amber drop requests and is why a cap exists at all. If a
     * signer ever starts dropping again the symptom is the counter stalling while `relay last spoke`
     * stays fresh, and this is the first number to put back. */
    _cap: 12, _capP: 2, _inflight: 0, _inflightP: 0, _queue: [], _queueP: [],
    _pump(){
      while(this._inflightP < this._capP && this._queueP.length){
        const job=this._queueP.shift();
        this._inflightP++;
        job.run().then(job.res, job.rej).finally(()=>{ this._inflightP--; this._pump(); });
      }
      /* THE BULK LANE YIELDS TO THE USER. While anything interactive is outstanding — a post, a
       * follow, a reaction — no NEW background decrypt is started; the ones already in flight
       * finish and the restore resumes a moment later.
       *
       * Separate lanes stopped the composer QUEUEING behind a DM restore; they did not stop it
       * COMPETING with one. Twelve decrypts in flight is twelve more publishes on the same socket
       * and twelve more replies to parse on the same main thread, and a publish gives the relay 8
       * seconds to answer before the post is filed as Pending. Reported the moment the restore got
       * fast enough to saturate: "messages is great now, now sending posts goes into pending".
       *
       * A restore is background work by definition — it is catching up on things already said. */
      const userWaiting = this._inflightP > 0 || this._queueP.length > 0;
      while(!userWaiting && this._inflight < this._cap && this._queue.length){
        const job=this._queue.shift();
        this._inflight++;
        job.run().then(job.res, job.rej).finally(()=>{ this._inflight--; this._pump(); });
      }
    },
    // Requests the USER is waiting on take the reserved lane. Restoring a DM history pushes hundreds of
    // decrypt jobs at the bulk one; a sign_event sharing their slots blocks the composer for MINUTES —
    // you hit Post and nothing happens. Interactive work (signing, and the connect handshake) is separate.
    _PRIORITY: new Set(['sign_event', 'connect', 'get_public_key']),
    _send(method, params, opts){
      return new Promise((res, rej)=>{
        const run=async()=>{
          try{ return await this._rpc(method, params, opts); }
          catch(e){
            // A user REFUSAL is final — retrying just re-prompts them. Match only clear user-denial phrasing;
            // NOT bare 'unauthorized'/'not authorized', which also appear in transient relay errors
            // (NIP-42 'restricted: unauthorized', bunker session re-establish) that SHOULD retry.
            const m=String((e&&e.message)||e).toLowerCase();
            // Only a USER refusal is final. Bare 'denied' over-matches transient transport errors
            // ('access denied', 'permission denied', 'connection denied') that SHOULD retry.
            if(m.includes('rejected by user') || m.includes('user rejected') || m.includes('user declined')
               || m.includes('denied by user') || m.includes('request denied')) throw e;
            await new Promise(r=>setTimeout(r, 600));
            return await this._rpc(method, params, opts);
          }
        };
        const job={ run, res, rej };
        if(this._PRIORITY.has(method)) this._queueP.push(job); else this._queue.push(job);
        this._pump();
      });
    },
    /* Settle the encryption ONCE, at pairing, by trying it — because NIP-46 has no capability
     * negotiation to ask with.
     *
     * NIP-44 first: it is what the current spec says, and continuing to open in NIP-04 forever makes
     * every modern signer somebody else's problem to fix. NIP-04 second, because that is what the
     * long tail actually reads. Whichever the signer answers is recorded on the SESSION and used for
     * every later request, so the scheme never changes mid-session.
     *
     * This is deliberately NOT the inference that was reverted (see _decode). That version re-derived
     * the outbound scheme from whatever arrived last, so one odd message silently broke every
     * subsequent signature — for good, and with no error. Here the probe happens at the one moment
     * the user is watching a pairing screen, a wrong guess costs 12 seconds rather than a session,
     * and `_encOk` confirms on the peer having READ us rather than on what it chose to write back.
     *
     * The second attempt is ADDED, never a replacement: the first stays outstanding at the full
     * ceiling. Approving a connect is a human act on a phone — Amber puts a dialog in front of
     * someone who may be several seconds from picking the handset up — and a probe that retired its
     * own request after twelve of them threw away the approval when it finally came, then sat
     * waiting for an answer to a request nobody had been asked about. Whichever attempt is answered
     * settles the scheme; a signer that reads both simply answers the first.
     *
     * Quiet, and no retry: "waiting for your signer…" during a probe would be a lie, and _send's
     * retry-once turns each probe into two connects (two prompts) for a signer that is merely slow.
     * An auth_url counts as read — it proves the request was decrypted. */
    async _negotiateConnect(remote, secret){
      this._encOk = false; this._encSeen = null;
      const tries = [], errs = [];
      const fire = enc => {
        const p = this._rpc('connect', [remote, secret], { enc, quiet: true })
                      .then(r => ({ enc, r }), e => { errs.push(e); throw e; });
        tries.push(p);
        return p;
      };
      fire('nip44');
      // …and if that is still silent after 12s, ask again in NIP-04, keeping the first alive.
      const silent = await Promise.race([
        tries[0].then(() => false, () => false),
        new Promise(r => setTimeout(() => r(true), 12000)),
      ]);
      if(silent && !this._encOk) fire('nip04');
      try{
        const w = await Promise.any(tries);
        this._enc = w.enc;
        return w.r;
      }catch(_){
        // Nothing was ANSWERED — but if the signer read one of them (an auth_url, or an approval
        // still pending on the phone), that scheme is settled and the caller's own get_public_key
        // waits on the human at full length.
        if(this._encOk){ this._enc = this._encSeen || 'nip04'; return null; }
        const refusal = errs.find(e => !/timed out/i.test(String((e && e.message) || e)));
        throw refusal || errs[0] || new Error('the signer did not answer in either encryption scheme');
      }
    },
    async _rpc(method, params, opts){
      if(!this.remotePk) throw new Error('signer not connected');
      /* Reconnect rather than report. `_live()` being empty means THIS page lost its relay socket —
       * almost always because the machine slept — and says nothing about the signer, which is why
       * "signer not connected" was the wrong sentence to put in front of the user. See _ensure. */
      if(!this._live().length && !(await this._ensure(9000)))
        throw new Error('cannot reach the signer relay — check your connection');
      const id='r'+Math.random().toString(36).slice(2,10);
      let enc=(opts && opts.enc) || this._enc || 'nip04';
      const body=JSON.stringify({ id, method, params });
      /* NIP-44 refuses a plaintext over 65535 bytes; NIP-04 has no ceiling at all.
       *
       * That is a property of the ENVELOPE, not of this request, and it bites here because the
       * envelope wraps whole documents: a `nip44_encrypt` of a Notes library or a budget, or a
       * `sign_event` of a large event, is routinely past 64KB. Sent as NIP-44 it does not fail at
       * the signer or on the relay — it throws in our own worker before anything is published
       * ("invalid plaintext size: must be between 1 and 65535 bytes"), so the action simply dies.
       * Every session used to be NIP-04, which is why this only appeared once the scheme was
       * negotiated.
       *
       * So oversize falls back to NIP-04 for that one request. A signer that reads only NIP-44
       * cannot be sent a request this size by ANY route, so there is nothing to lose; Amber and
       * every dual-scheme signer read it. The session's own scheme is left alone. */
      if(enc === 'nip44' && new TextEncoder().encode(body).length > 65535){
        enc = 'nip04';
        try{ console.warn('[nip46] ' + method + ' is over NIP-44\'s 64KB ceiling — sent as NIP-04'); }catch(_){}
      }
      const ct=(await Relay.worker.call(enc+'enc',{peer:this.remotePk, text:body})).ct;
      const tpl={ kind:24133, content:ct, tags:[['p',this.remotePk]], created_at:Math.floor(Date.now()/1000), pubkey:this.appPk };
      const signed=await Relay.worker.call('sign',{ event:tpl });
      return new Promise((res,rej)=>{
        /* Which scheme this request went out in, kept WITH the request. Two connect probes can be
         * outstanding at once during pairing, so a bare "something was read" flag cannot say which
         * of them the signer decrypted — and that is the whole answer the probe is asking for. */
        this._pending.set(id,{res,rej,enc,signed});
        // To EVERY relay this session holds. The signer listens on the ones its bunker link named,
        // which is not necessarily the one that opened first — see _openAll.
        const live=this._live(); let sent=0; const sentAt=Date.now();
        for(const w of live){ try{ w.send(JSON.stringify(['EVENT', signed])); sent++; }catch(_){} }
        // The socket died between _ensure and here. Kick the reconnect so _send's one retry (600ms
        // later, through _rpc → _ensure) has something to land on instead of failing the same way.
        if(!sent){ this._pending.delete(id); (this._urls||[]).forEach(u=>this._scheduleReopen(u));
                   return rej(new Error('lost the signer relay — retrying')); }
        /* Say something LONG before the ceiling. The 120s is deliberate — approving on a phone is a
         * physical act — but two minutes of a button that does nothing is indistinguishable from a
         * broken one, and that is exactly how it was reported ("click send, nothing happens").
         * A single nudge at 4s, only while the request is still outstanding. */
        /* 120s is for a HUMAN — it is how long approving a signature on a phone can take. Nothing
         * in the bulk lane is waiting on a person: a decrypt on a reachable signer answers in well
         * under a second, so a bulk request still outstanding at 45s is not slow, it is LOST — the
         * relay fanned it out to nobody (kind 24133 is ephemeral) and no copy exists anywhere.
         *
         * Giving those the human's ceiling is what made a DM restore stop dead: six slots, each
         * held for two minutes and then RETRIED for another two by _send, so a handful of dropped
         * requests stalls the whole queue for the best part of ten minutes. Reported as "decrypting
         * messages stuck at 80/400" — the counter was not stuck, it was waiting out ceilings that
         * had nothing to do with a person. Failing at 45s hands the slot back and lets the retry
         * (which reconnects first, via _ensure) do its job. */
        const ceiling=(opts && opts.timeout)
                    || (this._PRIORITY.has(method) ? 120000 : 45000);
        /* ONE nudge, and only for something a human is actually waiting on.
         *
         * Every request nudged, so a stalled transport during a DM restore covered the screen in
         * identical "waiting for your signer…" toasts — hundreds of decrypts, each with its own 4s
         * timer, none of them anything the user asked for. Reported as "a bunch of waiting for
         * signer toasters" while trying to post. The nudge exists to tell you a BUTTON you pressed
         * is still working, so it is limited to the interactive lane and throttled to one per 8s. */
        if(!(opts && opts.quiet) && this._PRIORITY.has(method))
          setTimeout(()=>{
            if(!this._pending.has(id)) return;
            if(Date.now() - (this._lastNudge||0) < 8000) return;
            this._lastNudge = Date.now();
            try{ toast('waiting for your signer…'); }catch(_){}
          }, 4000);
        setTimeout(()=>{ if(this._pending.has(id)){ this._pending.delete(id); rej(new Error('signer request timed out')); } }, ceiling);
        /* A KIND-24133 IS EPHEMERAL: IF NOBODY IS SUBSCRIBED AT THAT INSTANT, THE REQUEST IS GONE.
         *
         * Our own relay says so in as many words (`elif kind == 24133` in nostr_relay/server.py):
         * nothing is stored, it is only fanned out to whoever is listening RIGHT NOW. So a signer
         * whose socket is redialling — a phone that just came back from doze, a NAT that dropped the
         * mapping — does not receive a late copy. The request simply never happened, and this end
         * waits out the full 120s ceiling for an answer that cannot come. Clicking Post a second
         * time is what fixed it in the field, which is the tell: the SECOND request landed.
         *
         * So do that here instead of making the user do it: while the request is still outstanding,
         * publish the SAME signed event again — first making sure this end has a socket at all.
         *
         * A `ping` gate was tried here and is WRONG, which is worth recording because it is the
         * obvious design. The idea was to re-send only when the signer is provably not listening,
         * so that a signer which is merely slow (a human reading an approval dialog) is never
         * prompted twice. But the signer that dropped the request answers the ping the moment it
         * redials — that is the whole scenario — so the probe reports "alive", the re-send is
         * suppressed, and the request that was destroyed stays destroyed. Measured exactly that way
         * in scripts/check_nip46_reconnect.py's `signer-away` case: three pings answered, the
         * sign_event never. Liveness now says nothing about a request sent a moment ago.
         *
         * The cost is honest and small: a signer that asks a human to approve EVERY request can
         * show a second prompt for one action. The event is byte-identical (same template, same
         * created_at) so approving either produces the same signature and the duplicate reply is
         * dropped by request id. A second prompt is a nuisance; a post that never sends is the app
         * being broken.
         *
         * Interactive requests only. Bulk decrypts have the queue and their own retry, and a DM
         * restore re-sending per message would be hundreds of duplicate requests. */
        if(method !== 'connect'){
          let resent = 0;
          const interactive = this._PRIORITY.has(method);
          const again = async ()=>{
            if(!this._pending.has(id) || resent >= 4) return;
            /* THE DEAF CHECK IS ABOUT THE TRANSPORT, SO EVERY LANE GETS IT; the RE-SEND is about
             * one request, so only the interactive lane does.
             *
             * A zombie socket is not a property of the request that happened to notice it. During a
             * DM restore the only requests in flight are bulk decrypts — nothing interactive is
             * outstanding to do the noticing — so gating the check on the lane meant the one
             * situation with hundreds of requests stalled against a dead socket was the one
             * situation nothing checked. `revive()` is debounced to one reconnect per 3s however
             * many requests call it, which is what makes this safe to do 400 times at once. */
            if(!interactive){
              if(!(this._rxAt >= sentAt)) try{ this.revive(); }catch(_){}
              return;                     // no duplicate storm: _send's own retry re-sends this one
            }
            resent++;
            /* DEAF: nothing at all has arrived on any socket since this request went out — not even
             * the relay's own `OK` for the event we just published, which is the fastest and most
             * specific evidence available that the socket is a zombie rather than the signer being
             * away. `_ensure` cannot see this: a zombie is `readyState 1`, so it counts as live and
             * every send() into it succeeds. `revive()` is the only thing that resolves it, by
             * tearing the socket down and dialling again.
             *
             * MEASURED, on the real client through a proxy that goes silent without closing
             * (check_nip46_reconnect.py's `zombie-socket`): waiting for the second rung of the
             * ladder to doubt the socket recovered in 29s, and the deaf check does it in ~6s. The
             * later rungs revive unconditionally, because by then the socket has proved nothing. */
            const deaf = !(this._rxAt >= sentAt);
            if(deaf || resent >= 2) try{ this.revive(); }catch(_){}
            try{ await this._ensure(4000); }catch(_){}
            if(!this._pending.has(id)) return;
            for(const w of this._live()){ try{ w.send(JSON.stringify(['EVENT', signed])); }catch(_){} }
            setTimeout(again, _RESEND_AT[resent] || 30000);
          };
          setTimeout(again, _RESEND_AT[0]);
        }
      });
    },
    // bunker://<remote-signer-pubkey>?relay=wss://…&secret=…  (Amber gives you this string)
    async connectBunker(uri){
      const mm=String(uri||'').trim().match(/^bunker:\/\/([0-9a-fA-F]{64})\??(.*)$/);
      if(!mm) throw new Error('not a bunker:// link');
      const remote=mm[1].toLowerCase(); const qs=new URLSearchParams(mm[2]||'');
      const relays=qs.getAll('relay'); const secret=qs.get('secret')||'';
      if(!relays.length) throw new Error('bunker link is missing its relay');
      /* OPEN every relay the bunker link names, not just relays[0] and not just the fastest. A
       * bunker:// URL routinely carries four and the signer is listening on all of them, so opening
       * only the first made a login fail whenever that one relay was down — with three healthy ones
       * sitting unused in the same string we had just parsed.
       *
       * ALL of them are stored on the session, too. Keeping only the winner threw away the signer's
       * own answer to "where can I be reached", and left a later session with one relay that may be
       * the one that has since died.
       *
       * Starting from reset(), because a pairing gets a FRESH app key: any socket left over from an
       * earlier session (or an earlier attempt at this one) is subscribed for a pubkey no reply will
       * carry, so it can only publish — which is how one connect went out twice. */
      this.reset();
      await this._ensureAppKey(); await this._openAll(relays); this.remotePk=remote;
      await this._negotiateConnect(remote, secret);
      const userPk=await this._send('get_public_key',[]);
      this.userPk=userPk;
      return { userPk, session:{ mode:'nip46', sk:this.appSk, relay:this.relay, relays, remotePk:remote, userPk, enc:this._enc } };
    },
    /* The client URL a signer is shown, or '' when there is nothing a signer could make sense of.
     * Web and PWA give an https origin; the desktop bundle gives `app://posterchan` and the APK gives
     * `https://localhost`, neither of which identifies this client to anyone. See beginNostrConnect. */
    _clientUrl(){
      const cands=[(typeof location!=='undefined' && location.origin) || '', _instanceBase()];
      for(const u of cands){
        if(/^https?:\/\//i.test(u) && !/^https?:\/\/localhost(?::|\/|$)/i.test(u)) return u;
      }
      return '';
    },
    // nostrconnect://<app-pubkey>?relay=…&secret=…  (WE present this; the signer connects to us)
    //
    // `relays` is a LIST and we take the first one that actually opens. It used to be a single hard-coded
    // relay, which made signing in with Amber a single point of failure: when relay.nsec.app went down
    // (HTTP 502) nobody could log in with a remote signer, in any browser or the app — the login just said
    // "cannot reach signer relay" and there was nothing the user could do.
    async beginNostrConnect(relays, name){
      const list=(Array.isArray(relays)?relays:[relays]).filter(Boolean);
      this.reset();                 // fresh app key → any earlier socket can publish but never hear
      await this._ensureAppKey();
      await this._openAll(list);
      const relay=this.relay;   // the first to open — the one relay the QR can point the signer at
      const secret=Math.random().toString(36).slice(2,12);
      // Permissions we request up front. Amber prompts per-action so an empty list still works,
      // but iOS signers like Clave PRE-authorize from this list and deny anything not in it
      // ("No permission"). List every op/kind the client signs so the first connect grants them all.
      /* EVERY KIND THIS CLIENT SIGNS — and an omission is a feature that silently cannot be used.
       *
       * Our OWN signer enforces this list exactly (`allowed()` asks `g.indexOf('sign_event:'+kind)`),
       * so a kind missing here is answered "no permission" with no prompt and no way for the person
       * to grant it. That is what "why is my damn draft not signing!" was: replies became NIP-22
       * comments (kind 1111 — see replyKindFor) and 1111 was not on this list, so every reply to an
       * ordinary note was refused by the signer the user had paired.
       *
       * KEPT IN STEP BY A TEST, not by memory: tests/client/test_the_signer_is_asked_for_every_kind
       * _we_sign.py reads the kinds out of this file's own publish()/sign() calls and fails if one
       * is not here. The list grew from 16 to 35 the first time that ran, which is the measure of
       * how well "remember to add it" was working.
       *
       * The cost is a longer pairing URI and therefore a denser QR (384 → 777 encoded characters).
       * That is the right trade: a QR that pairs quickly and then refuses half the app is worse than
       * one that takes a moment longer to scan. */
      const kinds=[0,1,3,4,5,6,7,13,1018,1059,1068,1111,1311,1621,2003,9734,10000,10002,10003,
                   10050,10063,10096,10133,13303,20013,20014,22242,24242,27235,30003,30023,30024,
                   30078,30311,30388,30617,30618,31923,33302];
      const perms=['get_public_key','nip04_encrypt','nip04_decrypt','nip44_encrypt','nip44_decrypt']
        .concat(kinds.map(k=>'sign_event:'+k)).join(',');
      /* `url` goes in ONLY if it is an http(s) origin, and is omitted otherwise.
       *
       * The desktop app loads its bundle over the privileged `app://posterchan` scheme, so
       * `location.origin` there is literally "app://posterchan" — a real tuple origin to Chromium
       * (main.js registers the scheme `standard: true`) and not a URL any signer can parse. Amber
       * answers the whole pairing with "invalid nostr connect URI", which names the URI rather than
       * the one optional field in it that is wrong, so it reads as our QR being broken. It only ever
       * happened on the desktop client, because that is the only build whose origin is not http(s).
       *
       * `url` is optional in NIP-46, so the fallback is the instance we actually talk to, and then
       * nothing at all. An omitted optional field is always safer than one no signer can read. */
      const origin=this._clientUrl();
      /* TWO SPELLINGS OF THE SAME PAIRING, and the difference is only how much a camera has to read.
       *
       * `uri` is the full one — the tap-to-open link and the copyable text. `qrUri` drops `perms`,
       * which is 66% of the bytes and the reason the symbol was version 18 (89x89 modules). Without
       * it the same pairing is version 8 (49x49), so at any given size on screen every module is
       * nearly twice as wide.
       *
       * That is the variable that actually decides whether a scan works. Measured with a fake camera
       * (scripts/check_qr_scan.py): a SHARP frame decodes down to one pixel per module, but softness
       * — a webcam that has not focused — is fatal below about four. Modules are the only term in
       * that ratio we control.
       *
       * Nothing is lost by leaving `perms` out of the QR. It is optional in NIP-46 and advisory to
       * the signer: OUR signer ignores it entirely (an app that declares nothing is granted
       * everything, because scanning the QR is the consent), and Amber prompts per action regardless.
       * Only a signer that PRE-authorises from the list is affected, and it still gets the full URI
       * by tap or paste — the pubkey, relay and secret are identical, so both routes join the same
       * session. A pairing a camera cannot read is worth nothing to anybody. */
      const uri=`nostrconnect://${this.appPk}?relay=${encodeURIComponent(relay)}&secret=${secret}`
        +`&perms=${encodeURIComponent(perms)}&name=${encodeURIComponent(name||'PosterChan')}`
        +(origin?`&url=${encodeURIComponent(origin)}`:'');
      const qrUri=`nostrconnect://${this.appPk}?relay=${encodeURIComponent(relay)}&secret=${secret}`
        +`&name=${encodeURIComponent(name||'PosterChan')}`;
      const done=new Promise((res,rej)=>{
        const to=setTimeout(()=>{ this._onEvent=null; rej(new Error('timed out waiting for the signer')); }, 180000);
        this._onEvent=async (ev, payload)=>{
          if(!payload.result || payload.result==='auth_url') return;   // wait for the connect ack
          this.remotePk=ev.pubkey; this._onEvent=null; clearTimeout(to);
          /* The signer opened the conversation here, so there is nothing to probe: it has told us
           * what it speaks by what it just sent. Recorded ONCE, from the connect ack specifically —
           * not from whatever happens to arrive later. */
          this._enc = this._lastDec || 'nip04';
          try{ const pk=await this._send('get_public_key',[]); this.userPk=pk;
            res({ userPk:pk, session:{ mode:'nip46', sk:this.appSk, relay, relays:[relay], remotePk:this.remotePk, userPk:pk, enc:this._enc } }); }
          catch(e){ rej(e); }
        };
      });
      return { uri, qrUri, done };
    },
    async resume(s){
      const generation=this._generation;
      const current=()=>generation===this._generation;
      const check=()=>{if(!current())throw Object.assign(new Error('signer disconnected'),{cancelledSession:true});};
      /* Reconnect to the relays this session was PAIRED on, and to this node's own as well.
       *
       * The signer is on the paired ones and nowhere else — ours is here only because a relay can
       * die between sessions (relay.poster.place sat on an expired certificate for months, and
       * relay.nsec.app answers 502) and a session pinned to a dead relay signs into a socket nobody
       * is listening on, with a 120s timeout for feedback. Which is why they are all OPENED and each
       * request goes to all of them (see _openAll): choosing between them is the mistake — picking
       * the fastest picks ours, and ours is exactly the one the signer is not on. */
      await this._ensureAppKey(s.sk);check();
      // A session paired before this existed has no `enc` — nip04 is what it was using, so that is
      // the only safe default. Never re-negotiate on resume: the signer already knows this session.
      this._enc = (s.enc === 'nip44') ? 'nip44' : 'nip04';
      // `relays` is what newer pairings store (a bunker link names several); `relay` is the single
      // one older sessions kept, and dropping it would strand every session paired before today.
      const paired = (Array.isArray(s.relays) && s.relays.length ? s.relays : [s.relay]).filter(Boolean);
      const relays = paired.concat(_ncRelays()).filter(Boolean);
      this.remotePk=s.remotePk; this.userPk=s.userPk||null;
      /* A SOCKET THAT WILL NOT OPEN RIGHT NOW IS NOT A LOST LOGIN.
       *
       * This used to throw when no relay opened, and the caller reads a throw from `resume` as "the
       * session is unusable" and starts a read-only GUEST. On a machine that has just booted that
       * is the normal case, not the exceptional one: sway execs the shell and the first socket is
       * attempted a second or two later, before there is a route. Reported as "i rebooted laptop and
       * am not signed in anymore" -- with the phone's signer answering a ping throughout, because
       * the phone was never the problem.
       *
       * The identity does not depend on the socket. `userPk` is IN the saved session, so there is
       * nothing to ask anybody: the login is known, and `_ensure()` exists precisely to dial before
       * the first request that needs a signature (see its comment about the machine having been
       * asleep). Failing here threw away a login over a connection that was seconds away.
       *
       * A session with NO userPk is different and still fails: that one genuinely has to ask the
       * signer who it is, and it cannot without a socket. */
      /* THE SAVED IDENTITY MAKES RECONNECT A BACKGROUND OPERATION.
       * Waiting here blocks startApp(), which means a disconnected signer used to prevent the
       * desktop, Files and even this machine's native PTY from mounting. The public key is already
       * in the saved session; only a future SIGNING operation needs the socket, and _ensure() dials
       * before one. Start the dial now for low latency, but never put the local OS behind it. */
      if(this.userPk){
        this._openAll(relays).catch(e=>{
          if(!current() || e.cancelledSession)return;
          return this._openRelay(paired[0] || s.relay).catch(e2=>{
            if(current() && !e2.cancelledSession)console.warn('signer relay not up yet — keeping the login:',e2);
          });
        });
        return this.userPk;
      }
      try{ await this._openAll(relays); }
      catch(e){ check();if(e.cancelledSession)throw e;await this._openRelay(paired[0] || s.relay); }
      check();
      if(!this.userPk){const pk=await this._send('get_public_key',[]);check();this.userPk=pk;}
      return this.userPk;
    },
    // signer interface — every user op is forwarded to the remote signer
    /* A REMOTE SIGNER CAN CHANGE WHICH KEY IT OFFERS, AND SILENCE IS THE WORST WAY TO FIND OUT.
     *
     * `userPk` is read once and cached, which is right — it is asked for at connect time and a round
     * trip per signature would be absurd. But the signer is a whole other device with its own account
     * switcher: a phone that signs for this desktop and is then switched to a second account keeps
     * answering, with a DIFFERENT key. Reported 2026-09-18: "I tried on my phone but I don't think
     * the signer worked, my other account didn't work on desktop until I switched back." Nothing on
     * either screen said why, because nothing looked.
     *
     * The signed event carries the pubkey that signed it, so the check is free and needs no extra
     * request. Refusing is right — publishing it would post from an identity the user did not choose,
     * under a session that still believes it is someone else. */
    async signEvent(tpl){
      const ev = JSON.parse(await this._send('sign_event',[JSON.stringify(tpl)]));
      const want = this.userPk || '';
      if(want && ev && ev.pubkey && ev.pubkey !== want){
        const err = new Error('signer changed account');
        err.signerPubkeyMismatch = { expected: want, got: ev.pubkey };
        try{ toast('Your signer is offering a different account than this session. '
                   + 'Switch it back, or sign in again as that account.'); }catch(_){}
        throw err;
      }
      return ev;
    },
    nip04enc(peer, text){ return this._send('nip04_encrypt',[peer, text]); },
    nip04dec(peer, ct){ return this._send('nip04_decrypt',[peer, ct]); },
    nip44enc(peer, text){ return this._send('nip44_encrypt',[peer, text]); },
    nip44dec(peer, ct){ return this._send('nip44_decrypt',[peer, ct]); },
  };

  // ---------- NIP-46 SIGNER side: "scan a QR to log in another device" (Primal-style) ----------
  // When you're logged in here with a LOCAL key (the worker holds your nsec), THIS device can act
  // as the remote signer for another machine: scan its nostrconnect:// QR, ack the connection, then
  // answer its get_public_key / sign_event / nipNN_(en|de)crypt requests — signing with your key,
  // which never leaves this device. The mirror image of the Nip46 *client* above.
  /* THIS DEVICE AS A REMOTE SIGNER — the Amber side of NIP-46, for apps you sign in from here.
   *
   * MULTIPLE APPS AT ONCE. This held ONE pairing and `start()` began with `stop()`, so linking a
   * second app silently killed the first: the earlier one's requests went out to a relay nobody was
   * listening on any more and it sat waiting for a signer that had, from its point of view, simply
   * stopped existing. Sessions are a Map keyed by the app's own pubkey now, and one subscription
   * serves all of them — the filter is `#p: <our pubkey>`, which was never per-app to begin with,
   * so routing is a lookup on `ev.pubkey` rather than a comparison against the single current peer.
   *
   * PAIRINGS ARE DEVICE-LOCAL AND DELIBERATELY NOT SYNCED. They live in localStorage under this
   * account's pubkey, not in a kind-30078 document like Notes or the desktop layout. Two reasons,
   * and the second is the important one: a pairing is an agreement between an APP and THIS DEVICE,
   * so carrying it to your laptop would have both devices answering the same request and racing to
   * publish two signatures; and a replaceable document read back empty from an unreachable relay
   * would take every pairing with it on the next write, which is the wipe this codebase has already
   * paid for more than once. Losing pairings when you clear site data is the correct, legible
   * failure — you scan the QR again.
   *
   * PERMISSIONS ARE ENFORCED WHEN THE APP DECLARES THEM. `perms` in the nostrconnect URI is what the
   * app says it needs; it is stored per session and checked on every request, so an app that asked
   * for `sign_event:1` cannot later sign a kind 5 deletion. An app that declares NOTHING is granted
   * everything, because scanning its QR is the consent and refusing a client that predates the
   * parameter would simply break it — but one that names its needs is held to them.
   */
  const Nip46Signer = {
    socks: new Map(),        // relay url -> WebSocket. One per relay, however many apps share it.
    sessions: new Map(),     // client pubkey -> {relay, secret, name, perms, created, last}
    _subId: null,
    active: false,
    /* Set once the NATIVE service confirms it is up (packaged Android only). While it is, this half
     * holds no sockets at all — see _standDown. */
    nativeOn: false,

    /* ---- handing the job to the process, on the one platform that can take it ------------------
     *
     * THE DIVISION OF LABOUR, and it is the whole fix for "I have to wake the phone for events to
     * actually send from desktop": THIS half owns PAIRING, the native service owns STEADY STATE.
     *
     * Pairing happens with the screen on — someone is looking at a QR code — so the page is the right
     * place for it: it does the scan, the consent and the unsolicited ACK, exactly as it always has.
     * Steady state is the opposite: it happens with the screen off, which is precisely when Chromium
     * throttles this page's timers to about one a minute and a dropped socket stops being redialled.
     * A foreground service has no such policy, so once a pairing exists it is handed over.
     *
     * They must never BOTH be listening — two signers answering one request is two events published
     * for it — so the handover is explicit and one-way: publish, confirm the service is really up,
     * and only then close these sockets. If the service does not confirm, this half carries on as it
     * always did, which is also exactly what happens in a browser and on the desktop app, where there
     * is no service to hand anything to. */
    _nativePlugin(){
      try{ const p = _capPlugin('Signer', 'sync'); return (p && p.sync) ? p : null; }catch(_){ return null; }
    },
    /* Publish the pairings and ask the service to match them. Answers whether it took the job.
     * `secret` is deliberately NOT sent: it is only ever needed for the pairing ACK, which this half
     * sends before handing over, so the service has no use for it and no reason to store it. */
    /* GIVE THE SERVICE ITS KEY BEFORE ASKING IT TO DO ANYTHING.
     *
     * `SignerRelayService` cannot sign without a Keystore-sealed key, and until now the only thing
     * that ever stored one was the "Sign for other apps on this phone" switch — a different feature,
     * in a different settings section, that nobody pairing a laptop by QR has any reason to touch.
     * Without it the service starts, finds no key, closes every socket and returns; `connected`
     * stays 0, the hand-over is refused for ever, and the PAGE signs — which is full speed while the
     * app is on screen and about one request a minute behind it.
     *
     * Local keys only: with an extension or a remote signer this device has no secret to hand over,
     * and arming with nothing would produce a service that answers every request with a failure.
     * It does NOT expose this phone to other apps as a NIP-55 signer — that stays its own switch
     * (SignerPlugin.arm vs enable). */
    async _armNative(){
      try{
        if(!S.ME || S.ME.mode !== 'local') return false;
        const p = _capPlugin('Signer', 'arm');
        if(!p || !p.arm) return false;                 // an APK older than this: nothing to arm
        /* "ALREADY HOLDS ONE" HAS TO MEAN "HOLDS THIS ACCOUNT'S", and it did not.
         *
         * Switching accounts reloads the page with a new session and never clears the Keystore, so
         * a phone that had armed account A kept A's secret and this returned true for B. That was
         * nearly inert while only the NIP-46 signer used it; the background sweep now DEPENDS on it,
         * so B's unattended sweep would sign its Blossom auth and its manifest proof as A, and try
         * to unwrap B's drive key with A's secret — a 403 at best, the wrong identity at worst, and
         * nothing on either path says a word. `status()` reports the stored pubkey; compare it. */
        const st = await p.status().catch(()=>null);
        if(st && st.have && st.pubkey && S.ME.pubkey && st.pubkey === S.ME.pubkey) return true;
        const sess = Session.load();
        const sec = sess && sess.sk;
        if(!sec) return false;
        await p.arm({ sec });
        return true;
      }catch(_){ return false; }
    },
    async _pushNative(){
      const p = this._nativePlugin();
      if(!p){ this.nativeOn = false; return false; }
      // …and it must happen before the FIRST offer, or the first offer is refused for a reason no
      // retry can fix.
      try{ await this._armNative(); }catch(_){}
      try{
        const list = this.list().map(s => ({ pk:s.pk, relay:s.relay, name:s.name,
                                             perms:(s.perms||[]).join(','), enc:s.enc||'', last:s.last||0 }));
        const r = await p.sync({ sessions: JSON.stringify(list), enabled: !!this.active });
        /* A HAND-OVER RECEIPT IS A SOCKET, NOT A FLAG.
         *
         * The caller stands this half DOWN on a true answer here — it closes its own relay sockets,
         * because the pairings are somebody else's job now. `running` alone cannot carry that: the
         * plugin's `kick()` is startService, so the flag is read before the service has necessarily
         * touched it, and even once set the sockets are opened later still on its work thread. Taking
         * `running` as the receipt closed the only half that WAS listening, and the desktop then sat
         * on "waiting for your signer…" with the app open on screen — reported exactly that way.
         *
         * So the receipt is `connected`: at least one relay socket actually held. Until then this
         * half keeps answering, which is the safe direction — the worst case is that both halves are
         * subscribed for a moment and the desktop gets its signature twice as fast. */
        this.nativeOn = !!(r && r.running && Number(r.connected || 0) > 0);
      }catch(_){ this.nativeOn = false; }
      return this.nativeOn;
    },
    /* Close this half's sockets, keeping the session list for the UI to draw. NOT `stop()`: the
     * pairings are still live, they are simply somebody else's job now. */
    _standDown(){
      this.socks.forEach(ws => { try{ ws.onclose = null; ws.close(); }catch(_){} });
      this.socks.clear();
      /* And cancel any reconnect already in flight. Nulling `onclose` stops a NEW one being
       * scheduled; one that was scheduled before the hand-over would still fire, reopen a socket,
       * and put both halves back on the relay — the double-answer (two signatures published for one
       * request) that the whole hand-over protocol exists to prevent. */
      Object.keys(this._rtimer||{}).forEach(u=>{ try{ clearTimeout(this._rtimer[u]); }catch(_){} });
      this._rtimer = {}; this._boff = {};
    },

    _key(){ return 'pc_signer_v1_' + ((S.ME && S.ME.pubkey) || 'anon'); },
    _load(){
      try{
        const raw = localStorage.getItem(this._key());
        const arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr : [];
      }catch(_){ return []; }
    },
    _persist(){
      try{
        const out = [];
        this.sessions.forEach((v, k) => out.push(Object.assign({ pk: k }, v)));
        localStorage.setItem(this._key(), JSON.stringify(out));
      }catch(_){}                   // a full quota must not take down a working signer
    },
    _stats: new Map(),   // pk -> {n, dup, lastM} — per-app request tally, this page's lifetime
    list(){
      const out = [];
      this.sessions.forEach((v, k) => out.push(Object.assign({ pk: k },
        v, { stats: this._stats.get(k) || null })));
      return out.sort((a, b) => (b.last || b.created || 0) - (a.last || a.created || 0));
    },

    /* Grants, parsed from the URI's `perms`. Kept as a plain array of strings so it survives
     * JSON round-tripping through localStorage — a Set does not. */
    _grants(qs){
      const raw = (qs.get('perms') || '').trim();
      if(!raw) return null;                       // declared nothing → everything (see the header)
      return raw.split(',').map(s => s.trim()).filter(Boolean);
    },
    _allowed(sess, method, params){
      const g = sess && sess.perms;
      if(!g) return true;                          // no declaration → no restriction
      // Always answerable: the handshake itself, a liveness check, and the pubkey — which is public
      // by definition and which every client asks for immediately after connecting.
      if(method === 'connect' || method === 'ping' || method === 'get_public_key') return true;
      if(g.indexOf(method) >= 0) return true;
      if(method === 'sign_event'){
        let kind = null;
        try{
          let tpl = params && params[0];
          if(typeof tpl === 'string') tpl = JSON.parse(tpl);
          kind = tpl && tpl.kind;
        }catch(_){}
        if(kind !== null && kind !== undefined && g.indexOf('sign_event:' + kind) >= 0) return true;
        // Existing pairings predate NIP-78's AUTH requirement. A grant to store private kind-30078
        // data necessarily includes the ephemeral kind-22242 proof needed to access that data.
        if(Number(kind)===22242 && g.indexOf('sign_event:30078') >= 0) return true;
      }
      return false;
    },

    async start(uri, onStatus){
      const m = String(uri||'').trim().match(/^nostrconnect:\/\/([0-9a-f]{64})\??(.*)$/i);
      if(!m) throw new Error('that QR is not a nostrconnect login link');
      const clientPk = m[1].toLowerCase();
      const qs = new URLSearchParams(m[2]||'');
      /* THIS INSTANCE'S RELAY BY DEFAULT, ANOTHER ONLY IF YOU SAY SO.
       *
       * The relay in a QR is chosen by whoever printed the QR, and this side dials it — so a code
       * from anywhere could aim the half of the app that holds the key at a stranger's relay, which
       * learns the device's IP from the connection alone, before any pairing is approved. That is
       * why it is not simply obeyed.
       *
       * But refusing outright was too blunt, and broke the thing this is for: jumble.social and
       * Coracle print perfectly good nostrconnect codes naming THEIR relay, and a signer that only
       * works with its own instance is not a signer other apps can use. Being usable by other apps
       * is the entire point of the feature.
       *
       * So the rule is consent, not ownership. Ours is the silent default — a PosterChan QR always
       * names exactly this relay, so the common path asks nothing. Anything else names the host and
       * asks once, and the answer decides. Silently substituting our relay would be the worst of
       * both: the pairing would be made against a relay the other app is not listening on, both
       * halves would behave perfectly, and it would wait for ever. */
      const qrRelay = qs.getAll('relay')[0];
      if(!qrRelay) throw new Error('that QR is missing its relay');
      // Declared BEFORE the relay question, which names it. `const` is in a temporal dead zone until
      // its declaration, so reading it above would throw ReferenceError on exactly the pairing this
      // prompt exists to allow.
      const name = qs.get('name') || 'the app';
      const ourRelay = (S.CFG && S.CFG.relay_url) || '';
      const _norm = (u) => String(u||'').trim().replace(/\/+$/,'').toLowerCase();
      if(ourRelay && _norm(qrRelay) !== _norm(ourRelay)){
        let where = qrRelay;
        try{ where = new URL(qrRelay).host; }catch(_){}
        const ok = await uiConfirm(
          '“' + name + '” wants to be signed in through ' + where + ', which is not your instance’s '
          + 'relay. Your key stays on this device either way, but this device will connect to that '
          + 'relay. Allow it?', { ok:'Allow', cancel:'Cancel' });
        if(!ok) throw new Error('you declined the relay that QR asked for');
      }
      const relay = qrRelay;
      const sess = { relay, secret: qs.get('secret')||'', name, url: qs.get('url')||'',
                     perms: this._grants(qs), created: Math.floor(Date.now()/1000), last: 0 };
      this.active = true;
      // Registered BEFORE the socket opens: the subscription is shared, so an ack we publish can be
      // answered before `_open` resolves, and `_recv` must already be able to find the session.
      this.sessions.set(clientPk, sess);
      this._persist();
      try{
        await this._open(relay);
        // Unsolicited connect ACK — tells the client our pubkey (this event's author) and echoes the
        // secret, which is exactly what its nostrconnect handshake waits for.
        await this._send(clientPk, { id:'c'+Math.random().toString(36).slice(2,8), result: sess.secret });
      }catch(e){
        this.sessions.delete(clientPk); this._persist(); this._sync();
        throw e;
      }
      this._sync();
      /* Hand this pairing to the service now that the ACK is out. Awaited rather than fired off, so
       * that by the time the caller says "logged in" the half that will actually be answering is the
       * one holding the socket — a gap here is a window where the phone looks paired and nothing is
       * listening. A failure leaves this half connected, which is the pre-existing behaviour. */
      try{ if(await this._pushNative()) this._standDown(); }catch(_){}
      onStatus && onStatus(name);
      return name;
    },

    /* Reopen everything this device was already signing for. Called on sign-in, not on scan — a
     * pairing that survives a reload is the whole point of persisting it, and without this the
     * paired app looks alive right up until you refresh the page. */
    async resume(){
      const saved = this._load();
      if(!saved.length) return 0;
      this.active = true;
      saved.forEach(s => { const pk = s.pk; if(pk){ const c = Object.assign({}, s); delete c.pk;
                                                    this.sessions.set(pk, c); } });
      /* Ask the service FIRST, and open nothing if it takes the job. Opening sockets and closing them
       * again a moment later is not merely wasteful: it is a REQ per relay whose replies race the
       * service's, which is the double-answer this split exists to prevent. */
      let native = false;
      try{ native = await this._pushNative(); }catch(_){}
      if(!native){
        const relays = [...new Set(this.list().map(s => s.relay).filter(Boolean))];
        await Promise.all(relays.map(r => this._open(r).catch(()=>{})));
        this._offerNative();          // …and keep offering: see below
      }
      this._sync();
      return this.sessions.size;
    },

    /* KEEP OFFERING THE JOB UNTIL THE SERVICE TAKES IT — because the one moment it was offered is
     * the one moment it is guaranteed to refuse.
     *
     * The receipt for a hand-over is `connected`: at least one relay socket actually held by the
     * service. That is the right receipt (a flag is not a socket). But the offer is made from
     * `resume()`, and `kick()` is `startService` — the service then has to go foreground, read its
     * prefs and open a socket on its work thread, all of it AFTER the plugin call has returned. So
     * the first answer is false almost every time, the page opens its own sockets, and there was
     * nothing anywhere to ask again. The page stays the signer for the entire session.
     *
     * That is not a subtle degradation. A page IS the throttled half: Chromium holds a hidden
     * WebView's timers to about one a minute, so signing runs at full speed while the app is on
     * screen and falls off a cliff the moment it is not — which is precisely how it was reported
     * ("foreground instantly solves the DM problem fast"), and precisely the failure the native
     * service was written to remove. Measured on the relay at the time: 642 replies in 55 seconds
     * with the app in front, from a phone that had supposedly handed the job over hours earlier.
     *
     * A LADDER, not a timer: four tries over about a minute, stopping the moment the service takes
     * it (or the pairings go). `_standDown` only ever runs on a TRUE receipt, so the invariant that
     * matters — never two signers answering one request — is untouched. */
    _offerAt: [2500, 6000, 15000, 40000],
    _offering: false,
    _offerNative(){
      if(this._offering || this.nativeOn || !this.active) return;
      this._offering = true;
      let i = 0;
      const tick = async () => {
        if(!this.active || this.nativeOn){ this._offering = false; return; }
        let ok = false;
        try{ ok = await this._pushNative(); }catch(_){}
        if(ok){ this._standDown(); this._sync(); this._offering = false; return; }
        if(i < this._offerAt.length) setTimeout(tick, this._offerAt[i++]);
        else this._offering = false;
      };
      setTimeout(tick, this._offerAt[i++]);
    },

    revoke(clientPk){
      this.sessions.delete(String(clientPk||''));
      this._persist();
      // Close a relay nothing needs any more; keep the ones still carrying a session.
      const keep = new Set(this.list().map(s => s.relay));
      this.socks.forEach((ws, url) => {
        if(keep.has(url)) return;
        try{ ws.onclose = null; ws.close(); }catch(_){}
        this.socks.delete(url);
      });
      if(!this.sessions.size) this.active = false;
      /* Tell the service too, or a revoked app carries on being answered by the half that is awake —
       * the page would show it signed out while the phone kept signing for it. */
      this._pushNative().catch(()=>{});
      this._sync();
    },

    revokeAll(){
      this.sessions.clear();
      this._persist();
      this.active = false;
      this.socks.forEach(ws => { try{ ws.onclose = null; ws.close(); }catch(_){} });
      this.socks.clear();
      /* Push the EMPTY session set to the native service too. Clearing browser storage alone would
       * leave Android answering requests for pairings the Signer screen says were revoked. */
      this._pushNative().catch(()=>{});
      this._sync();
    },

    _open(relay){
      const have = this.socks.get(relay);
      if(have && have.readyState === 1) return Promise.resolve();
      return new Promise((res,rej)=>{
        let done=false; const ws=new WebSocket(relay); this.socks.set(relay, ws);
        ws.onopen=()=>{ this._subId = this._subId || ('ns'+Math.random().toString(36).slice(2,8));
          /* NIP46_SINCE_SKEW, not the 5 seconds this had — for the reason written at that constant,
           * which applies to THIS side just as much and was only ever fixed on the other one. The
           * two ends of a QR pairing are two machines with two clocks by definition, the relay
           * applies `since` server-side, and the desktop stamps its requests with ITS clock: a
           * desktop a minute behind this phone had every request dropped before it arrived. The
           * phone says "now logged in", the desktop sits on "waiting for the signer to approve…"
           * until it times out, and nothing anywhere raises an error, because from this side nothing
           * ever came. scripts/check_qr_device_login.py pairs two real browsers with a skewed clock. */
          ws.send(JSON.stringify(['REQ', this._subId, { kinds:[24133], '#p':[S.ME.pubkey], since: Math.floor(Date.now()/1000)-NIP46_SINCE_SKEW }]));
          if(!done){ done=true; res(); } };
        ws.onmessage=(e)=>this._recv(e.data);
        ws.onerror=()=>{ if(!done){ done=true; rej(new Error('cannot reach the relay in the QR')); } };
        ws.onclose=()=>{
          if(this.socks.get(relay) === ws) this.socks.delete(relay);
          // Reconnect only while something still needs THIS relay. A revoked pairing must not keep
          // a socket alive for ever, and a stopped signer must genuinely stop.
          if(!this.active) return;
          if(!this.list().some(s => s.relay === relay)) return;
          this._reopen(relay);
        };
        setTimeout(()=>{ if(!done){ done=true; rej(new Error('relay timed out')); } }, 20000);
      });
    },
    /* Keep trying, with backoff, for as long as a pairing still needs this relay.
     *
     * The single `setTimeout(…, 2000)` this replaces gave up after ONE failed attempt, and the case
     * it cannot survive is a machine resuming from sleep: every socket closes on suspend, the retry
     * fires two seconds into the resume before the network is back, and this device silently stops
     * being a signer for every app it is paired with. Nothing is logged here and nothing is visible
     * there — the other end simply waits out its 120s ceiling on every signature. The client half's
     * _scheduleReopen is the mirror of this and exists for the same reason. */
    _boff:{}, _rtimer:{},
    _reopen(relay){
      if(!this.active || this.nativeOn || !relay) return;
      if(!this.list().some(s => s.relay === relay)) return;
      if(this.socks.get(relay)) return;                 // open or connecting
      if(this._rtimer[relay]) return;
      const d = this._boff[relay] ? Math.min(this._boff[relay] * 1.7, 20000) : 1500;
      this._boff[relay] = d;
      this._rtimer[relay] = setTimeout(()=>{
        this._rtimer[relay] = 0;
        if(!this.active || this.socks.get(relay)) return;
        this._open(relay).then(()=>{ this._boff[relay] = 0; }, ()=>{ this._reopen(relay); });
      }, d);
    },
    /* The page came back from sleep — reopen every relay a pairing needs, now, and drop sockets the
     * browser still calls OPEN (a resumed machine's socket is routinely a zombie: readyState 1,
     * delivering nothing, so requests arrive nowhere and no error is raised on either side).
     * No-op while the native service owns the pairings — it has its own socket and its own ping. */
    revive(){
      if(!this.active) return;
      // Coming back to the foreground is a fresh chance for the service to take the job: it may have
      // been started by the OS, or by this app, since the last offer was refused.
      if(!this.nativeOn){ this._offerNative(); }
      if(this.nativeOn) return;
      if(Date.now() - (this._revivedAt||0) < 3000) return;
      this._revivedAt = Date.now();
      const relays = [...new Set(this.list().map(s => s.relay).filter(Boolean))];
      this.socks.forEach(ws => { try{ ws.onclose = ws.onerror = ws.onmessage = null; ws.close(); }catch(_){} });
      this.socks.clear();
      relays.forEach(r=>{
        try{ clearTimeout(this._rtimer[r]); }catch(_){}
        this._rtimer[r] = 0; this._boff[r] = 0;
        this._open(r).catch(()=>{ this._reopen(r); });
      });
    },

    /* ---- the OTHER direction: a link the app connects TO ---------------------------------------
     *
     * WHY BOTH EXIST. `nostrconnect://` is the flow this signer was built around: the APP publishes a
     * QR, this phone scans it and dials out. That is what jumble.social and primal.net show, and it
     * works. But a large part of the ecosystem does the reverse — nostrudel's "login with a signer"
     * screen has one field, and its placeholder is `bunker://<pubkey>?relay=wss://…`. There is no QR
     * to scan there, so a signer that only speaks nostrconnect cannot log into it at all. Handing it
     * anything else produces a bech32 error from the client rather than an explanation ("unknown
     * letter b" — a hex key being decoded as an npub), which is how this looked like a broken signer
     * rather than a missing feature.
     *
     * It costs almost nothing to support, because the subscription is already the right one: this
     * signer subscribes to every kind-24133 addressed to it, and a bunker connect is exactly that
     * from a pubkey it has not met yet. So the only new rule is when to accept a stranger — and the
     * answer is "when it presents the secret we just minted", which IS the credential in this flow.
     *
     * The window is deliberately short and the link is not stored. A bunker secret is a bearer token
     * printed on screen; leaving one live for the life of the session would mean anyone who saw it
     * over your shoulder could attach an app days later. */
    _pending: null,
    BUNKER_WINDOW: 10 * 60 * 1000,

    /* Mint a link. The relay is THIS INSTANCE'S, the same default the QR flow uses — the app being
     * connected has no say here, which is the point: with nostrconnect the relay comes from somebody
     * else's QR, and with this one it does not. */
    async bunkerUri(){
      if(!S.ME || S.ME.mode !== 'local') throw new Error('this device does not hold a key to sign with');
      const relay = (S.CFG && S.CFG.relay_url) || '';
      if(!relay) throw new Error('this instance has no relay to be reached on');
      const b = new Uint8Array(16); crypto.getRandomValues(b);
      const secret = Array.from(b, x => x.toString(16).padStart(2,'0')).join('');
      this._pending = { secret, relay, at: Date.now() };
      this.active = true;
      await this._open(relay);
      return 'bunker://' + S.ME.pubkey + '?relay=' + encodeURIComponent(relay) + '&secret=' + secret;
    },

    /* An app we have never met just said hello. Accept it ONLY as a `connect` carrying the live
     * secret; anything else from a stranger is dropped without a reply, because answering would
     * confirm to an unpaired peer that this key is listening here. */
    _acceptBunker(ev, req){
      const p = this._pending;
      if(!p) return null;
      if(req.method !== 'connect') return null;
      if(Date.now() - p.at > this.BUNKER_WINDOW){ this._pending = null; return null; }
      const params = req.params || [];
      // NIP-46 `connect` is [remote_signer_pubkey, secret, perms]. The secret is checked wherever it
      // landed rather than only at index 1: clients disagree about whether the first argument is
      // present, and a signer that reads one position silently rejects the ones that do not match it.
      if(!params.some(x => typeof x === 'string' && x === p.secret)) return null;

      const perms = this._grants(new URLSearchParams(
        'perms=' + encodeURIComponent(String(params[2] || ''))));
      const sess = { relay: p.relay, secret: p.secret, name: 'an app', perms,
                     enc: this._lastEnc || '', created: Math.floor(Date.now()/1000),
                     last: Math.floor(Date.now()/1000) };
      this.sessions.set(ev.pubkey, sess);
      this._persist();
      // One link, one app: leaving it live would let a second app attach off the same screenshot.
      this._pending = null;
      this._pushNative().then(ok => { if(ok) this._standDown(); }).catch(()=>{});
      this._sync();
      return sess;
    },

    /* Reads BOTH schemes, and records which one this peer speaks.
     *
     * `?iv=` is NIP-04's own marker, so it decides the order rather than being discovered by a
     * failed decrypt — the same rule the client half uses. */
    async _decode(clientPk, ct){
      const ops = /\?iv=/.test(String(ct||'')) ? ['nip04dec','nip44dec'] : ['nip44dec','nip04dec'];
      for(const op of ops){
        try{
          const pt = JSON.parse((await Relay.worker.call(op,{ peer:clientPk, ct })).pt);
          const sess = this.sessions.get(clientPk);
          /* Recorded even with NO session, because the bunker flow decodes a stranger's `connect`
           * before there is one to record it on — and the reply to that very message has to go back
           * in the scheme it arrived in. */
          this._lastEnc = (op === 'nip04dec') ? 'nip04' : 'nip44';
          if(sess) sess.enc = this._lastEnc;
          return pt;
        }catch(_){}
      }
      return null;
    },
    /* ANSWER IN THE SCHEME THE PEER SPEAKS, and default to NIP-44.
     *
     * This always encrypted with NIP-04, which is how "the app shows up in my signer's list but the
     * site never logs in" happens: we mint the session (so OUR side looks paired) and send an ack the
     * other end cannot read. NIP-46 moved to NIP-44 and a current client — jumble.social, Coracle —
     * may implement only that, so a NIP-04 reply is silence with extra steps.
     *
     * The unsolicited ACK has nothing to learn from yet, so it goes out NIP-44: it is what the spec
     * says today, and our own client reads both. Every later reply uses whatever that peer's request
     * actually arrived in, which is the only evidence that cannot be wrong. */
    async _send(clientPk, payload){
      const sess=this.sessions.get(clientPk);
      const op=((sess && sess.enc) === 'nip04') ? 'nip04enc' : 'nip44enc';
      const ct=(await Relay.worker.call(op,{ peer:clientPk, text:JSON.stringify(payload) })).ct;
      const tpl={ kind:24133, content:ct, tags:[['p',clientPk]], created_at:Math.floor(Date.now()/1000), pubkey:S.ME.pubkey };
      const signed=await Relay.worker.call('sign',{ event:tpl });
      // To the socket that carries this session's relay.
      const ws=sess && this.socks.get(sess.relay);
      try{ if(ws && ws.readyState===1) ws.send(JSON.stringify(['EVENT', signed])); }catch(_){}
    },
    async _recv(raw){
      let m; try{ m=JSON.parse(raw); }catch(_){ return; }
      if(m[0]!=='EVENT' || m[1]!==this._subId) return;
      const ev=m[2]; if(!ev || ev.kind!==24133) return;
      let sess=this.sessions.get(ev.pubkey);
      /* Decoded BEFORE the "do we know this app" check, because with a bunker link outstanding the
       * answer can be "not yet": the whole point of that flow is that the app introduces itself. Any
       * other unknown peer still costs one failed decrypt and is dropped by _acceptBunker. */
      const req=await this._decode(ev.pubkey, ev.content); if(!req || !req.id || !req.method) return;
      if(!sess){
        sess = this._acceptBunker(ev, req);
        if(!sess) return;                     // not an app we are signing for
      }else if(this.nativeOn){
        /* THE SERVICE OWNS STEADY STATE, so this half answers nothing it is not uniquely able to
         * answer. It matters because minting a bunker link REOPENS this socket while the service
         * still holds its own: without this line both halves would receive every request from the
         * already-paired apps and both would reply, publishing two signed events for one request —
         * exactly what the confirmed one-way handover exists to prevent. A stranger's `connect`
         * above is the one thing only this half can do, because only it holds the pending secret. */
        return;
      }
      /* `last` is bookkeeping, NOT a reason to redraw. Sending one DM is several requests — encrypt,
       * wrap, sign — and repainting the list on each of them made the Settings card flicker while
       * the user was looking at it. Persisted at most once a minute, too: the timestamp is only ever
       * read to render "last used", so writing localStorage per request bought nothing. */
      sess.last=Math.floor(Date.now()/1000);
      if(!this._lastSaved || sess.last - this._lastSaved > 60){ this._lastSaved = sess.last; this._persist(); }
      /* PER-APP TALLY, in memory only. A paired app stuck in a loop (measured: the same two small
       * decrypts every ~20s, for hours) is invisible from the signer's side — every request looks
       * legitimate one at a time. Counting per app, and counting REPEATS (same method + same
       * params fingerprint as that app's previous ask) is what lets the pairings screen name the
       * looping device instead of the whole signer feeling slow. */
      { const st=this._stats.get(ev.pubkey) || { n:0, dup:0, lastM:'', _fp:'' };
        const fp=req.method+'|'+String(JSON.stringify(req.params||[])).length+'|'+String(JSON.stringify(req.params||[])).slice(0,64);
        if(fp === st._fp) st.dup++;
        st._fp=fp; st.n++; st.lastM=req.method;
        this._stats.set(ev.pubkey, st); }
      let result=null, error=null;
      if(!this._allowed(sess, req.method, req.params||[])){
        /* NAME THE KIND, AND SAY WHAT FIXES IT.
         *
         * This said only "sign_event was not in what this app asked for", which is true and
         * unusable: it does not say WHICH kind, and it does not say that a pairing's grants are
         * fixed at pairing time so the only way to widen them is to pair again. Reported as "why is
         * my damn draft not signing!" — a reply, which became a NIP-22 comment (kind 1111) after
         * that app had already been paired with a list that could not contain it. */
        let _k=null;
        try{ let t=(req.params||[])[0]; if(typeof t==='string') t=JSON.parse(t); _k=t&&t.kind; }catch(_){ }
        error='not permitted: '+req.method+(_k===null||_k===undefined?'':(' (kind '+_k+')'))
              +' was not in what this app asked for. Pair it again to grant it.';
      }else{
        try{ result=await this._handle(req.method, req.params||[]); }
        catch(e){ error=String((e&&e.message)||e); }
      }
      await this._send(ev.pubkey, error ? { id:req.id, result:'', error } : { id:req.id, result });
      // NO _sync() here. The set of paired apps has not changed — only how recently one of them
      // asked for something — and a repaint per request is what the flicker was.
    },
    async _handle(method, params){
      switch(method){
        case 'connect':        return 'ack';
        case 'ping':           return 'pong';
        case 'get_public_key': return S.ME.pubkey;
        case 'sign_event': {
          let tpl=params[0]; if(typeof tpl==='string') tpl=JSON.parse(tpl);
          tpl.pubkey=S.ME.pubkey; if(!tpl.created_at) tpl.created_at=Math.floor(Date.now()/1000);
          return JSON.stringify(await Relay.worker.call('sign',{ event:tpl }));
        }
        case 'nip04_encrypt':  return (await Relay.worker.call('nip04enc',{ peer:params[0], text:params[1] })).ct;
        case 'nip04_decrypt':  return (await Relay.worker.call('nip04dec',{ peer:params[0], ct:params[1] })).pt;
        case 'nip44_encrypt':  return (await Relay.worker.call('nip44enc',{ peer:params[0], text:params[1] })).ct;
        case 'nip44_decrypt':  return (await Relay.worker.call('nip44dec',{ peer:params[0], ct:params[1] })).pt;
        default: throw new Error('unsupported method: '+method);
      }
    },
    /* Repaint whatever is showing the pairings. Deliberately NOT a native call: see
     * `_signerBackgroundHint`, which explains why this reuses StayAwakeService rather than starting
     * a foreground service of its own. */
    _sync(){ try{ _renderSignerApps(); }catch(_){} },
    stop(){
      this.active=false;
      this.socks.forEach(ws => { try{ ws.onclose=null; ws.close(); }catch(_){} });
      Object.keys(this._rtimer||{}).forEach(u=>{ try{ clearTimeout(this._rtimer[u]); }catch(_){} });
      this._rtimer={}; this._boff={};
      this.socks.clear(); this.sessions.clear(); this._subId=null;
      this._persist();
      // `active:false` above is what tells the service to shut down rather than reload.
      this._pushNative().catch(()=>{});
      this._sync();
    },
  };

  return {

    get Nip55(){ return Nip55; },
    get Nip46(){ return Nip46; },
    get Nip46Signer(){ return Nip46Signer; },
  };
};

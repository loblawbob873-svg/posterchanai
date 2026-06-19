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

  // ---- one socket to one relay; reports up to the pool ----
  class Conn {
    constructor(url, pool, trusted){
      this.url = url; this.pool = pool; this.trusted = trusted;
      this.ws = null; this.status = 'init'; this._backoff = 600; this._rt = null;
      this._open();
    }
    _open(){
      try { this.ws = new WebSocket(this.url); } catch(e){ this._setStatus('off'); this._retry(); return; }
      this._setStatus('connecting');
      this.ws.onopen = () => {
        this._backoff = 600;                       // reset reconnect backoff on a good connection
        this._setStatus('ok');
        // re-arm only LIVE subscriptions; one-shot query() subs (live:false) must not be re-REQ'd.
        for (const [id, s] of this.pool._subs) if (s.live) this._send(['REQ', id, ...s.filters]);
        this.pool._connReady(this);
      };
      this.ws.onclose = () => { this._setStatus('off'); this._retry(); };
      this.ws.onerror = () => { try{ this.ws.close(); }catch(_){} };
      this.ws.onmessage = (e) => this.pool._onMessage(this, e.data);
    }
    _setStatus(s){ this.status = s; this.pool._recomputeStatus(); }
    _retry(){ clearTimeout(this._rt); const d = this._backoff || 600; this._backoff = Math.min(d*1.7, 8000);
              this._rt = setTimeout(()=>this._open(), d + Math.random()*300); }
    _send(arr){ if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(arr)); }
    destroy(){ this.status = 'closed'; clearTimeout(this._rt);
               if (this.ws){ this.ws.onclose = this.ws.onerror = this.ws.onmessage = null; try{ this.ws.close(); }catch(_){} this.ws = null; } }
  }

  const Relay = {
    worker,
    status: 'init',
    onStatus: null,
    onReady: null,            // fired once when the first socket opens (run initial queries here)
    url: null,                // primary relay (first configured) — used as the display/zap relay
    _conns: new Map(),        // url -> Conn
    _subs: new Map(),         // subId -> {filters, onEvent, onEose, live, seen:Set, eosed:Set}
    _okWaiters: new Map(),    // eventId -> { settle(fn) }
    _verify: false,           // true when connected to user relays (untrusted -> verify sigs)
    _vq: [], _vt: null,       // verify queue for untrusted events
    _ready: false,

    // Connect to an explicit set of relays. verify=true makes the pool signature-verify every
    // incoming event (used for user-supplied relays); verify=false trusts them (built-in WoT relay).
    configure({ urls, verify } = {}){
      urls = [...new Set((urls||[]).filter(Boolean))];
      this._verify = !!verify;
      this.url = urls[0] || null;
      // drop connections no longer wanted
      for (const [u, c] of this._conns){ if (!urls.includes(u)){ c.destroy(); this._conns.delete(u); } }
      // open the new ones (trusted = NOT verify mode)
      for (const u of urls){ if (!this._conns.has(u)) this._conns.set(u, new Conn(u, this, !this._verify)); }
      if (!this._conns.size) this._setStatus('off');
    },
    // back-compat single-relay entry point (built-in WoT relay, trusted)
    connect(url){ this.configure({ urls: url ? [url] : [], verify: false }); },

    _setStatus(s){ if (s === this.status) return; this.status = s; if (this.onStatus) this.onStatus(s); },
    _recomputeStatus(){
      const sts = [...this._conns.values()].map(c=>c.status);
      this._setStatus(sts.some(x=>x==='ok') ? 'ok' : sts.some(x=>x==='connecting') ? 'connecting' : 'off');
    },
    _connReady(){ if (!this._ready){ this._ready = true; if (this.onReady) try { this.onReady(); } catch(e){ console.warn(e); } } },

    _send(arr){ for (const c of this._conns.values()) c._send(arr); },

    _onMessage(conn, raw){
      let m; try { m = JSON.parse(raw); } catch(_){ return; }
      const typ = m[0];
      if (typ === 'EVENT'){
        const sub = this._subs.get(m[1]); if (!sub || !sub.onEvent) return;
        const ev = m[2]; if (!ev || sub.seen.has(ev.id)) return;   // dedup across relays
        if (conn.trusted){ sub.seen.add(ev.id); sub.onEvent(ev); }
        else { this._vq.push({ ev, sub }); if (!this._vt) this._vt = setTimeout(()=>this._flush(), 40); }
      } else if (typ === 'EOSE' || typ === 'CLOSED'){
        const sub = this._subs.get(m[1]); if (!sub) return;
        sub.eosed.add(conn.url);
        if (sub.onEose && sub.eosed.size >= this._conns.size){
          // for untrusted relays, drain pending verifications so the last events aren't lost
          const fire = () => { if (sub.onEose){ const cb = sub.onEose; sub.onEose = null; cb(); } };
          this._vq.length ? this._flush().then(fire) : fire();
        }
      } else if (typ === 'OK'){
        const w = this._okWaiters.get(m[1]);
        if (w && m[2]){ this._okWaiters.delete(m[1]); w.settle({ ok: true, msg: m[3]||'' }); }   // first accept wins
      }
    },
    async _flush(){
      if (this._vt){ clearTimeout(this._vt); this._vt = null; }
      const batch = this._vq.splice(0, this._vq.length);
      if (!batch.length) return;
      try {
        const results = await worker.call('verifyBatch', { events: batch.map(b=>b.ev) });
        const valid = new Set(results.filter(r=>r.valid).map(r=>r.id));
        for (const b of batch){ if (valid.has(b.ev.id) && b.sub.onEvent && !b.sub.seen.has(b.ev.id)){ b.sub.seen.add(b.ev.id); b.sub.onEvent(b.ev); } }
      } catch(e){ console.warn('verify batch failed', e); }
    },

    // filters: array of filter objects. Returns subId. live=true keeps it open for new events.
    subscribe(filters, { onEvent, onEose, live=true } = {}){
      const id = 'sub' + Math.random().toString(36).slice(2,9);
      this._subs.set(id, { filters, onEvent, onEose, live, seen: new Set(), eosed: new Set() });
      this._send(['REQ', id, ...filters]);
      return id;
    },
    close(subId){
      if (!this._subs.has(subId)) return;
      this._send(['CLOSE', subId]); this._subs.delete(subId);
    },
    // one-shot query across all relays -> resolves with a deduped array after every relay EOSEs
    query(filters, timeout=6000){
      return new Promise((res)=>{
        const got = []; let done = false;
        const finish = () => { if (done) return; done = true; this.close(id); res(got); };
        const id = this.subscribe(filters, {
          live: false,
          onEvent: ev => got.push(ev),    // pool already deduped by id before delivery
          onEose: finish
        });
        setTimeout(finish, timeout);
      });
    },
    // publish to every connected relay; resolves on the first relay that accepts (OK true)
    publish(event, timeout=8000){
      return new Promise((res)=>{
        let settled = false;
        const t = setTimeout(()=>{ if(!settled){ settled = true; this._okWaiters.delete(event.id); res({ ok:false, msg:'timeout' }); } }, timeout);
        this._okWaiters.set(event.id, { settle: (r)=>{ if(!settled){ settled = true; clearTimeout(t); this._okWaiters.delete(event.id); res(r); } } });
        this._send(['EVENT', event]);
      });
    }
  };

  window.Relay = Relay;
})();

/* WebSocket client for the built-in relay ONLY. Incoming events are verified in the crypto
 * worker (off the main thread) before they reach the app, so a busy global firehose can't peg
 * the UI thread. Also exposes the worker RPC (sign / keygen / nip04) used by the signer. */
(function(){
  // ---- worker RPC ----
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
  const worker = new WorkerRPC('/static/js/client/signer-worker.js');

  const Relay = {
    url: null, ws: null, status: 'init',
    _subs: new Map(),         // subId -> {filters, onEvent, onEose, live}
    _okWaiters: new Map(),    // eventId -> {res, t}
    _vq: [],                  // verify queue [{ev, sub}]
    _vt: null,
    onStatus: null,
    worker,

    connect(url){
      this.url = url;
      this._open();
    },
    _setStatus(s){ this.status = s; if (this.onStatus) this.onStatus(s); },
    _open(){
      try { this.ws = new WebSocket(this.url); } catch(e){ this._setStatus('off'); this._retry(); return; }
      this._setStatus('connecting');
      this.ws.onopen = () => {
        this._setStatus('ok');
        // re-arm only LIVE subscriptions; one-shot query() subs (live:false) must not be
        // re-REQ'd — they'd duplicate events into an already-resolving query.
        for (const [id, s] of this._subs) if (s.live) this._send(['REQ', id, ...s.filters]);
      };
      this.ws.onclose = () => { this._setStatus('off'); this._retry(); };
      this.ws.onerror = () => { try{ this.ws.close(); }catch(_){} };
      this.ws.onmessage = (e) => this._onMessage(e.data);
    },
    _retry(){ clearTimeout(this._rt); this._rt = setTimeout(()=>this._open(), 2200 + Math.random()*1500); },
    _send(arr){ if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(arr)); },

    _onMessage(raw){
      let m; try { m = JSON.parse(raw); } catch(_){ return; }
      const typ = m[0];
      if (typ === 'EVENT'){
        const sub = this._subs.get(m[1]); if (!sub) return;
        this._vq.push({ ev: m[2], sub });
        if (!this._vt) this._vt = setTimeout(()=>this._flush(), 25);
      } else if (typ === 'EOSE'){
        const sub = this._subs.get(m[1]); if (sub && sub.onEose) sub.onEose();
      } else if (typ === 'OK'){
        const w = this._okWaiters.get(m[1]);
        if (w){ clearTimeout(w.t); this._okWaiters.delete(m[1]); w.res({ ok: !!m[2], msg: m[3]||'' }); }
      } else if (typ === 'CLOSED'){
        const sub = this._subs.get(m[1]); if (sub && sub.onEose) sub.onEose();
      }
    },
    async _flush(){
      this._vt = null;
      const batch = this._vq.splice(0, this._vq.length);
      if (!batch.length) return;
      try {
        const results = await worker.call('verifyBatch', { events: batch.map(b=>b.ev) });
        const valid = new Set(results.filter(r=>r.valid).map(r=>r.id));
        for (const b of batch){ if (valid.has(b.ev.id) && b.sub.onEvent) b.sub.onEvent(b.ev); }
      } catch(e){ console.warn('verify batch failed', e); }
    },

    // filters: array of filter objects. Returns subId. live=true keeps it open for new events.
    subscribe(filters, { onEvent, onEose, live=true } = {}){
      const id = 'sub' + Math.random().toString(36).slice(2,9);
      this._subs.set(id, { filters, onEvent, onEose, live });
      this._send(['REQ', id, ...filters]);
      return id;
    },
    close(subId){
      if (!this._subs.has(subId)) return;
      this._send(['CLOSE', subId]); this._subs.delete(subId);
    },
    // one-shot query -> resolves with array of events after EOSE
    query(filters, timeout=6000){
      return new Promise((res)=>{
        const got = []; let done = false;
        const id = this.subscribe(filters, {
          live:false,
          onEvent: ev => got.push(ev),
          onEose: () => { if(!done){ done=true; this.close(id); res(got); } }
        });
        setTimeout(()=>{ if(!done){ done=true; this.close(id); res(got); } }, timeout);
      });
    },
    publish(event, timeout=8000){
      return new Promise((res)=>{
        this._okWaiters.set(event.id, { res, t: setTimeout(()=>{ this._okWaiters.delete(event.id); res({ ok:false, msg:'timeout' }); }, timeout) });
        this._send(['EVENT', event]);
      });
    }
  };

  window.Relay = Relay;
})();

/* vmrpc.js — talk to a PosterChan VM host over Nostr (kinds 5310 → 6310/7310).
 *
 * DOM-free and dependency-injected, so tests/client/vms_rpc_runtime.mjs runs THIS file under node
 * against a stub relay with real NostrTools signing and NIP-44. The UI (vms.js) is the only caller.
 *
 * One call = one signed request, NIP-44-encrypted to the host and p-tagged to it, published to the
 * host's relay on a socket of our own (a host relay need not be in the user's pool, and a client with
 * only a key and relays must still reach it). The ORDER is the whole correctness story:
 *
 *   1. sign the request (its id is now known);
 *   2. SUBSCRIBE to {6310,7310} authored by the host with #e = that id;
 *   3. only then PUBLISH — a result that lands between publish and subscribe is otherwise missed.
 *
 * THREE OUTCOMES, never two. `{ok:true,result}`, `{ok:false,error:{code,message}}` (the host answered
 * and said no), and `{ok:false,noAnswer:true}` (nobody answered). The last is NOT "empty": a host that is
 * offline and a host that silently drops strangers look identical from here, and a screen that renders
 * that as "no VMs" tells somebody their machines are gone.
 *
 * A RETRY KEEPS THE ID. A silent attempt is re-sent as a NEW event (new created_at, new signature — the
 * host drops a replayed event id) carrying the SAME idempotency id, so the host's journal answers with
 * the stored result instead of powering a VM twice.
 */
(function(root){
  const KINDS = { REQ: 5310, RES: 6310, PROGRESS: 7310, ANNOUNCE: 31310 };

  function createVmRpc(deps){
    const d = Object.assign({
      timeout: 12000, retries: 1, connectTimeout: 8000, idleClose: 60000,
      now: () => Date.now(),
      newId: () => {
        const b = new Uint8Array(16);
        (root.crypto || globalThis.crypto).getRandomValues(b);
        return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
      },
      setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: t => clearTimeout(t),
    }, deps || {});
    const socks = new Map();        // url -> conn

    function connect(url){
      let c = socks.get(url);
      if(c && c.ws && (c.ws.readyState === 0 || c.ws.readyState === 1)) return c.ready;
      c = { url, ws: null, subs: new Map(), oks: new Map(), idle: null };
      socks.set(url, c);
      c.ready = new Promise((resolve) => {
        let settled = false;
        const done = v => { if(!settled){ settled = true; resolve(v); } };
        let ws;
        try{ ws = new d.WebSocket(url); }catch(_){ socks.delete(url); return done(null); }
        c.ws = ws;
        const t = d.setTimeout(() => { done(null); try{ ws.close(); }catch(_){} }, d.connectTimeout);
        ws.onopen = () => { d.clearTimeout(t); done(c); };
        ws.onerror = () => { d.clearTimeout(t); done(null); };
        ws.onclose = () => {
          d.clearTimeout(t); done(null);
          if(socks.get(url) === c) socks.delete(url);
          for(const f of c.oks.values()) try{ f([false, 'connection closed']); }catch(_){}
          c.oks.clear();
        };
        ws.onmessage = (e) => {
          let m; try{ m = JSON.parse(typeof e.data === 'string' ? e.data : ''); }catch(_){ return; }
          if(!Array.isArray(m)) return;
          if(m[0] === 'EVENT' && c.subs.has(m[1])) { try{ c.subs.get(m[1])(m[2]); }catch(_){} }
          else if(m[0] === 'OK' && c.oks.has(m[1])){ const f = c.oks.get(m[1]); c.oks.delete(m[1]); f([m[2] === true, String(m[3] || '')]); }
        };
      });
      return c.ready;
    }

    function touch(c){
      if(c.idle) d.clearTimeout(c.idle);
      c.idle = d.setTimeout(() => {
        if(c.subs.size || c.oks.size) return touch(c);
        try{ c.ws.close(); }catch(_){}
        if(socks.get(c.url) === c) socks.delete(c.url);
      }, d.idleClose);
    }

    function send(c, msg){
      try{ c.ws.send(JSON.stringify(msg)); return true; }catch(_){ return false; }
    }

    const tagv = (ev, n) => ((ev.tags || []).find(t => t && t[0] === n) || [])[1];

    async function attempt(c, host, me, body, onProgress, timeout){
      const ts = Math.floor(d.now() / 1000);
      const content = await d.enc(host.pubkey, JSON.stringify(Object.assign({}, body, { ts })));
      const ev = await d.sign({ kind: KINDS.REQ, created_at: ts, content,
                                tags: [['p', host.pubkey], ['expiration', String(ts + 120)], ['nofederate']] });
      if(!ev || !ev.id) return { ok: false, error: { code: 'signer', message: 'the request could not be signed' } };
      return await new Promise((resolve) => {
        const subId = 'vm' + ev.id.slice(0, 12);
        let finished = false, timer = null;
        const finish = (r) => {
          if(finished) return; finished = true;
          if(timer) d.clearTimeout(timer);
          c.subs.delete(subId);
          send(c, ['CLOSE', subId]);
          touch(c);
          resolve(r);
        };
        c.subs.set(subId, async (rev) => {
          if(finished || !rev || rev.pubkey !== host.pubkey || tagv(rev, 'e') !== ev.id) return;
          if(rev.kind !== KINDS.RES && rev.kind !== KINDS.PROGRESS) return;
          try{ if(d.verify && !(await d.verify(rev))) return; }catch(_){ return; }
          let msg;
          try{ msg = JSON.parse(await d.dec(host.pubkey, rev.content)); }catch(_){ return; }
          if(!msg || msg.id !== body.id) return;
          if(rev.kind === KINDS.PROGRESS){ if(onProgress && msg.progress) try{ onProgress(msg.progress); }catch(_){} return; }
          if(msg.ok === true) finish({ ok: true, result: msg.result || {}, id: body.id });
          else finish({ ok: false, error: (msg.error && msg.error.code) ? msg.error : { code: 'internal', message: 'bad reply' }, id: body.id });
        });
        // Subscribe FIRST, publish second (see the header).
        send(c, ['REQ', subId, { kinds: [KINDS.RES, KINDS.PROGRESS], authors: [host.pubkey], '#e': [ev.id] }]);
        c.oks.set(ev.id, ([ok, why]) => {
          if(!ok) finish({ ok: false, error: { code: 'relay_refused', message: why || 'the relay refused the request' }, id: body.id });
        });
        if(!send(c, ['EVENT', ev])) return finish({ ok: false, noAnswer: true, reason: 'unreachable', id: body.id });
        timer = d.setTimeout(() => finish({ ok: false, noAnswer: true, reason: 'timeout', id: body.id }), timeout);
      });
    }

    async function call(host, op, args, opts){
      const o = opts || {};
      const me = d.me && d.me();
      if(!me) return { ok: false, error: { code: 'signed_out', message: 'sign in first' } };
      if(!host || !/^[0-9a-f]{64}$/.test(String(host.pubkey || '')) || !/^wss?:\/\//.test(String(host.relay || '')))
        return { ok: false, error: { code: 'bad_host', message: 'this host has no usable key or relay' } };
      const body = { v: 1, id: o.id || d.newId(), op, args: args || {} };
      const tries = 1 + Math.max(0, o.retries == null ? d.retries : o.retries);
      let last = { ok: false, noAnswer: true, reason: 'unreachable', id: body.id };
      for(let i = 0; i < tries; i++){
        const c = await connect(host.relay);
        if(!c){ last = { ok: false, noAnswer: true, reason: 'unreachable', id: body.id }; continue; }
        last = await attempt(c, host, me, body, o.onProgress, o.timeout || d.timeout);
        if(!last.noAnswer) return last;
      }
      return last;
    }

    function closeAll(){
      for(const c of socks.values()){ try{ c.ws.close(); }catch(_){} }
      socks.clear();
    }

    return { call, closeAll, KINDS, _socks: socks };
  }

  root.PCVmRpc = { createVmRpc, KINDS };
})(typeof window !== 'undefined' ? window : globalThis);

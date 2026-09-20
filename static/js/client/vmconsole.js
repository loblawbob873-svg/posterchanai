/* vmconsole.js — a VM's screen in the client: noVNC over the host's /ws/vmconsole.
 *
 * The ticket was obtained over Nostr (console.ticket); this file only spends it. The socket protocol
 * (app/routers/vmhost.py) is:  → {t:open,ticket}  ← {t:ok}  → {t:go}  ⇄ binary RFB  ← {t:err,m}.
 *
 * TWO ORDERING RULES, each a silent hang if broken:
 *   * noVNC is loaded BEFORE the socket opens. RFB speaks first (the server sends its version the
 *     moment we say `go`), and a module import that is still resolving when bytes arrive delivers
 *     them to a handler that is about to be replaced — the session sits at "connecting" for ever.
 *   * `go` is sent AFTER `new RFB(...)` has attached to the socket, for the same reason.
 * The ticket is sent in the first FRAME, never in the URL (proxy access logs keep URLs).
 */
(function(root){
  const NOVNC = '/static/vendor/novnc/core/rfb.js';

  function wsUrlFor(ticketWs, host){
    // The host may hand back an absolute wss:// URL (vmhost_public_url set) or just the path, in which
    // case the console lives on the same origin as the relay we reached the host through.
    const w = String(ticketWs || '');
    if(/^wss?:\/\//.test(w)) return w;
    try{
      const r = new URL(String(host && host.relay || ''));
      if(host && /^https?:\/\//.test(host.https || '')){
        const h = new URL(host.https);
        return (h.protocol === 'https:' ? 'wss://' : 'ws://') + h.host + (w || '/ws/vmconsole');
      }
      return (r.protocol === 'wss:' ? 'wss://' : 'ws://') + r.host + (w || '/ws/vmconsole');
    }catch(_){ return ''; }
  }

  function loadRFB(){
    if(root.__pcNoVNC) return root.__pcNoVNC;
    root.__pcNoVNC = import(NOVNC).then(m => m.default || m.RFB).catch(e => { root.__pcNoVNC = null; throw e; });
    return root.__pcNoVNC;
  }

  /* open({target, url, ticket, password, onStatus}) → handle. onStatus(state, message) with state in
   * connecting | connected | error | closed. */
  async function open(opts){
    const o = opts || {};
    const status = (s, m) => { try{ o.onStatus && o.onStatus(s, m || ''); }catch(_){} };
    status('connecting', 'Loading the console…');
    let RFB;
    try{ RFB = await (o.loadRFB || loadRFB)(); }
    catch(e){ status('error', 'The console viewer could not be loaded'); return null; }
    const WS = o.WebSocket || root.WebSocket;
    let ws, rfb = null, closed = false;
    try{ ws = new WS(o.url); }catch(_){ status('error', 'Could not reach the host'); return null; }
    ws.binaryType = 'arraybuffer';
    const handle = {
      get rfb(){ return rfb; },
      close(){
        if(closed) return; closed = true;
        try{ rfb ? rfb.disconnect() : ws.close(); }catch(_){}
        status('closed', '');
      },
      ctrlAltDel(){ try{ rfb && rfb.sendCtrlAltDel(); }catch(_){} },
      setFit(fit){ if(rfb){ rfb.scaleViewport = !!fit; rfb.clipViewport = !fit; } },
      focus(){ try{ rfb && rfb.focus(); }catch(_){} },
    };
    ws.onopen = () => { try{ ws.send(JSON.stringify({ t: 'open', ticket: o.ticket })); }catch(_){} };
    ws.onerror = () => { if(!rfb && !closed) status('error', 'The console connection failed'); };
    ws.onclose = () => { if(!rfb && !closed){ closed = true; status('closed', 'The console closed'); } };
    ws.onmessage = (e) => {
      if(typeof e.data !== 'string') return;          // nothing binary may arrive before `go`
      let m; try{ m = JSON.parse(e.data); }catch(_){ return; }
      if(m.t === 'err'){ status('error', m.m || 'The host refused the console'); try{ ws.close(); }catch(_){} return; }
      if(m.t !== 'ok' || rfb) return;
      try{
        rfb = new RFB(o.target, ws, { credentials: { password: o.password || '' }, shared: true });
      }catch(err){ status('error', 'The console viewer failed to start'); try{ ws.close(); }catch(_){} return; }
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.addEventListener('connect', () => status('connected', ''));
      rfb.addEventListener('disconnect', (ev) => {
        if(closed) return; closed = true;
        status((ev && ev.detail && ev.detail.clean) ? 'closed' : 'error',
               (ev && ev.detail && ev.detail.clean) ? 'The console closed' : 'The console connection dropped');
      });
      rfb.addEventListener('securityfailure', () => status('error', 'The VM refused the console password'));
      // noVNC initialises its RFB state machine from the WebSocket's `open` EVENT. This socket is
      // ALREADY open — we ran the {t:open}/{t:ok} handshake on it before creating the RFB — so that
      // event fired before noVNC attached and never reaches it, leaving the handshake stuck in an
      // empty init state ("Unknown init state (state: )") the moment the first RFB byte arrives.
      // noVNC's own "socket already open" fast-path does not run here, so kick the open handler it
      // installed (which drives _socketOpen → ProtocolVersion) BEFORE the byte stream starts.
      try{ if(typeof ws.onopen === 'function') ws.onopen(); }catch(_){}
      // After the attach: RFB now owns onmessage, so the host may start the byte stream.
      try{ ws.send(JSON.stringify({ t: 'go' })); }catch(_){}
    };
    return handle;
  }

  root.PCVmConsole = { open, wsUrlFor, loadRFB };
})(typeof window !== 'undefined' ? window : globalThis);

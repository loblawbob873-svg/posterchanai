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
    let ws, rfb = null, closed = false, pointerLock = null;
    try{ ws = new WS(o.url); }catch(_){ status('error', 'Could not reach the host'); return null; }
    ws.binaryType = 'arraybuffer';
    const handle = {
      get rfb(){ return rfb; },
      close(){
        if(closed) return; closed = true;
        try{ pointerLock && pointerLock.release(); }catch(_){}
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
      rfb.addEventListener('connect', () => status('connected', 'Click the screen to grab the mouse'));
      rfb.addEventListener('disconnect', (ev) => {
        if(closed) return; closed = true;
        status((ev && ev.detail && ev.detail.clean) ? 'closed' : 'error',
               (ev && ev.detail && ev.detail.clean) ? 'The console closed' : 'The console connection dropped');
      });
      rfb.addEventListener('securityfailure', () => status('error', 'The VM refused the console password'));
      // OPT-IN POINTER LOCK — "grab" the mouse so it cannot wander off the VM's screen. noVNC has no
      // pointer lock of its own and reads absolute ev.clientX/clientY, so while locked we keep a virtual
      // cursor (seeded at the canvas centre), advance it by the raw movementX/Y CLAMPED to the canvas,
      // and re-dispatch synthetic mouse events at that point to noVNC's canvas — the VM cursor tracks 1:1
      // and is confined, which works for the absolute (tablet) device the VMs use. It is opt-in (click to
      // grab, Esc to release), so a phone/desktop that never grabs keeps the ordinary mouse untouched.
      try{ pointerLock = attachPointerLock(o.target, status); }catch(_){}
      // After the attach: RFB now owns onmessage, so the host may start the byte stream.
      try{ ws.send(JSON.stringify({ t: 'go' })); }catch(_){}
    };
    return handle;
  }

  /* Confine the mouse to the VM. noVNC reads absolute ev.clientX/clientY, so while the pointer is
   * locked we keep a virtual cursor, move it by the raw movementX/Y clamped to the canvas, and
   * re-dispatch synthetic mouse events at that point — absolute tracking that cannot leave the VM.
   * Returns { release() }. Opt-in: nothing changes until the user clicks the screen to grab. */
  function attachPointerLock(target, status){
    const doc = target.ownerDocument, win = doc.defaultView || root;
    let vx = 0, vy = 0, dispatching = false, on = false;
    const canvas = () => target.querySelector('canvas');
    function clientRect(){ const c = canvas(); return c ? c.getBoundingClientRect() : target.getBoundingClientRect(); }
    function seed(){ const r = clientRect(); vx = r.left + r.width / 2; vy = r.top + r.height / 2; }
    function relay(type, e){
      const c = canvas(); if(!c) return;
      dispatching = true;
      try{
        c.dispatchEvent(new win.MouseEvent(type, { clientX: vx, clientY: vy, screenX: vx, screenY: vy,
          button: e.button, buttons: e.buttons, bubbles: true, cancelable: true, view: win }));
      }catch(_){}
      dispatching = false;
    }
    function onMove(e){
      if(!on || dispatching) return;
      e.preventDefault(); e.stopImmediatePropagation();
      const r = clientRect();
      vx = Math.max(r.left, Math.min(r.right - 1, vx + (e.movementX || 0)));
      vy = Math.max(r.top, Math.min(r.bottom - 1, vy + (e.movementY || 0)));
      relay('mousemove', e);
    }
    function onBtn(e){ if(!on || dispatching) return; e.preventDefault(); e.stopImmediatePropagation(); relay(e.type, e); }
    function onWheel(e){
      if(!on || dispatching) return;
      e.preventDefault(); e.stopImmediatePropagation();
      const c = canvas(); if(!c) return;
      dispatching = true;
      try{ c.dispatchEvent(new win.WheelEvent('wheel', { clientX: vx, clientY: vy, deltaX: e.deltaX, deltaY: e.deltaY,
        deltaMode: e.deltaMode, bubbles: true, cancelable: true, view: win })); }catch(_){}
      dispatching = false;
    }
    function onChange(){
      const locked = doc.pointerLockElement === target || doc.pointerLockElement === canvas();
      on = !!locked; target.classList.toggle('vmc-grabbed', on);
      if(on){ seed(); status && status('connected', 'Mouse grabbed — press Esc to release'); }
      else { status && status('connected', 'Click the screen to grab the mouse'); }
    }
    function grab(){ if(on) return; try{ (target.requestPointerLock ? target : canvas() || target).requestPointerLock(); }catch(_){} }
    target.addEventListener('click', grab);
    doc.addEventListener('pointerlockchange', onChange);
    win.addEventListener('mousemove', onMove, true);
    win.addEventListener('mousedown', onBtn, true);
    win.addEventListener('mouseup', onBtn, true);
    win.addEventListener('wheel', onWheel, { capture: true, passive: false });
    return { release(){
      try{ if(doc.pointerLockElement) doc.exitPointerLock(); }catch(_){}
      target.removeEventListener('click', grab);
      doc.removeEventListener('pointerlockchange', onChange);
      win.removeEventListener('mousemove', onMove, true);
      win.removeEventListener('mousedown', onBtn, true);
      win.removeEventListener('mouseup', onBtn, true);
      win.removeEventListener('wheel', onWheel, true);
    } };
  }

  root.PCVmConsole = { open, wsUrlFor, loadRFB };
})(typeof window !== 'undefined' ? window : globalThis);

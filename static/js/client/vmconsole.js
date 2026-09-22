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
      grab(){ try{ pointerLock && pointerLock.grab(); }catch(_){} },
      grabbed(){ try{ return !!(pointerLock && pointerLock.grabbed()); }catch(_){ return false; } },
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
      /* THE DESKTOP'S UI ZOOM. noVNC measures its box and maps the mouse with getBoundingClientRect,
       * which is ON-SCREEN (zoomed) size, and treats it as unzoomed CSS pixels. Under the desktop's
       * zoom (1.25 on a 4K panel) the VM screen was drawn 1.25x too big for its window — its right
       * part clipped off, "the cursor is not going to the right quarter of the screen" — and pointer
       * offsets were divided by an unzoomed scale. Both are divided by the EFFECTIVE zoom, measured
       * on the element itself (visual width / layout width), so this is right at any zoom and a
       * no-op at 1. tests/client/test_vm_console_grab_real_click.py renders it at 1, 1.25 and 1.5. */
      const zoomOf = (el) => { try{ const w = el.offsetWidth; return w ? (el.getBoundingClientRect().width / w) || 1 : 1; }catch(_){ return 1; } };
      if(typeof rfb._screenSize === 'function'){
        const size = rfb._screenSize.bind(rfb);
        rfb._screenSize = () => { const s = size(), z = zoomOf(o.target); return { w: s.w / z, h: s.h / z }; };
      }
      const disp = rfb._display;
      if(disp && typeof disp.absX === 'function' && typeof disp.absY === 'function'){
        const ax = disp.absX.bind(disp), ay = disp.absY.bind(disp);
        const cz = () => zoomOf(rfb._canvas || o.target.querySelector('canvas') || o.target);
        disp.absX = (x) => ax(x / cz());
        disp.absY = (y) => ay(y / cz());
      }
      rfb.scaleViewport = true;
      rfb.resizeSession = false;
      rfb.addEventListener('connect', () => status('connected', 'Click the screen to grab the mouse'));
      rfb.addEventListener('disconnect', (ev) => {
        if(closed) return; closed = true;
        status((ev && ev.detail && ev.detail.clean) ? 'closed' : 'error',
               (ev && ev.detail && ev.detail.clean) ? 'The console closed' : 'The console connection dropped');
      });
      rfb.addEventListener('securityfailure', () => status('error', 'The VM refused the console password'));
      // POINTER LOCK — "grab" the mouse so it cannot wander off the VM's screen. noVNC has no
      // pointer lock of its own and reads absolute ev.clientX/clientY, so while locked we keep a virtual
      // cursor (seeded at the canvas centre), advance it by the raw movementX/Y CLAMPED to the canvas,
      // and re-dispatch synthetic mouse events at that point to noVNC's canvas — the VM cursor tracks 1:1
      // and is confined, which works for the absolute (tablet) device the VMs use. A click on the screen
      // grabs (unless opts.grabOnClick === false), Esc releases; touch never grabs, so a phone keeps its
      // ordinary input, and nothing is intercepted until the lock is actually held.
      try{ pointerLock = attachPointerLock(o.target, status, o.grabOnClick !== false); }catch(_){}
      // After the attach: RFB now owns onmessage, so the host may start the byte stream.
      try{ ws.send(JSON.stringify({ t: 'go' })); }catch(_){}
    };
    return handle;
  }

  /* Confine the mouse to the VM. noVNC reads absolute ev.clientX/clientY, so while the pointer is
   * locked we keep a virtual cursor, move it by the raw movementX/Y clamped to the canvas, and
   * re-dispatch synthetic mouse events at that point — absolute tracking that cannot leave the VM.
   * Returns { grab(), grabbed(), release() }.
   *
   * THE GRAB IS TAKEN ON MOUSEDOWN, IN THE CAPTURE PHASE — never on `click`. The first version listened
   * for `click` on the screen and it could not fire: noVNC's canvas handles `click` itself and calls
   * stopPropagation(), and on `mousedown` it raises a full-page capture element (setCapture →
   * #noVNC_mouse_capture_elem) so the `mouseup` lands on THAT and the click's target is <body>. Either
   * one alone starves a click listener on the screen, so "click the screen to grab the mouse" did
   * nothing at all — reported as "Cursor Lock ... Still an issue". A capturing mousedown on the screen
   * runs before any of noVNC's handlers, is a user activation (requestPointerLock needs one), and still
   * lets the press through to the VM. tests/client/test_vm_console_grab_real_click.py drives a REAL
   * click at the real noVNC canvas. */
  function attachPointerLock(target, status, grabOnClick){
    const doc = target.ownerDocument, win = doc.defaultView || root;
    let vx = 0, vy = 0, dispatching = false, on = false, pressAt = null;
    const canvas = () => target.querySelector('canvas');
    function clientRect(){ const c = canvas(); return c ? c.getBoundingClientRect() : target.getBoundingClientRect(); }
    // Seed the virtual cursor where the grabbing click WAS (so the VM's cursor does not jump), else the
    // centre; clamped either way.
    function seed(){
      const r = clientRect(), p = pressAt; pressAt = null;
      vx = p ? p.x : r.left + r.width / 2; vy = p ? p.y : r.top + r.height / 2;
      vx = Math.max(r.left, Math.min(r.right - 1, vx)); vy = Math.max(r.top, Math.min(r.bottom - 1, vy));
    }
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
    function isLocked(){ const el = doc.pointerLockElement; return !!el && (el === target || target.contains(el)); }
    function onChange(){
      const was = on;
      on = isLocked(); target.classList.toggle('vmc-grabbed', on);
      if(on && !was){ seed(); status && status('connected', 'Mouse grabbed — press Esc to release'); }
      else if(!on && was){ status && status('connected', 'Click the screen to grab the mouse'); }
    }
    function onError(){
      on = false; target.classList.remove('vmc-grabbed');
      status && status('connected', 'The mouse could not be grabbed — click the screen again');
    }
    function grab(){
      if(on) return;
      const el = target.requestPointerLock ? target : (canvas() || target);
      try{
        const p = el.requestPointerLock();
        if(p && typeof p.catch === 'function') p.catch(onError);         // Chrome ≥ 88 returns a promise
      }catch(_){ onError(); }
    }
    function onDown(e){
      if(on || dispatching || !grabOnClick || e.button !== 0) return;
      pressAt = { x: e.clientX, y: e.clientY };
      grab();                                                             // the press itself still reaches the VM
    }
    target.addEventListener('mousedown', onDown, true);
    doc.addEventListener('pointerlockchange', onChange);
    doc.addEventListener('pointerlockerror', onError);
    win.addEventListener('mousemove', onMove, true);
    win.addEventListener('mousedown', onBtn, true);
    win.addEventListener('mouseup', onBtn, true);
    win.addEventListener('wheel', onWheel, { capture: true, passive: false });
    return {
      grab,
      grabbed: () => on,
      release(){
        try{ if(isLocked()) doc.exitPointerLock(); }catch(_){}
        target.classList.remove('vmc-grabbed');
        target.removeEventListener('mousedown', onDown, true);
        doc.removeEventListener('pointerlockchange', onChange);
        doc.removeEventListener('pointerlockerror', onError);
        win.removeEventListener('mousemove', onMove, true);
        win.removeEventListener('mousedown', onBtn, true);
        win.removeEventListener('mouseup', onBtn, true);
        win.removeEventListener('wheel', onWheel, true);
      },
    };
  }

  root.PCVmConsole = { open, wsUrlFor, loadRFB };
})(typeof window !== 'undefined' ? window : globalThis);

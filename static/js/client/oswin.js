/* REAL WINDOWS ON PosterChanOS — stage 1: the container.
 *
 * WHY. The desktop is ONE tiled sway window and every native app floats above it, so sway always
 * paints Telegram and Firefox in front of anything we draw. "Click a window to bring it forward"
 * was therefore faked: the native surface is taken off the screen entirely and its frame keeps a
 * frozen screenshot. That fake has been reported three times in one day — "Settings glitching my
 * screen and telegram, sticking", the Music/Firefox "glitch", and "telegram is swallowing windows
 * and its not separating" — and no threshold fixes it, because a 9px conflict costs the whole app
 * when a real window manager would leave the other 99% visible and live.
 *
 * The fix is for a PosterChan window to BE a compositor window. Then sway stacks it with Telegram
 * natively, clicking raises exactly that window, and the entire parking subsystem (stashPlan, the
 * previews, `_natSent`, the black-window family of bugs) has nothing left to do.
 *
 * WHAT MAKES IT AFFORDABLE. A same-origin `window.open()` child shares the opener's PROCESS and,
 * because it is same-origin, its JavaScript objects. So a window can use the desktop's Store, relay
 * pool and signer directly through `window.opener` — there is no engine/view IPC split to write and
 * no second copy of the relay pool per window. (Two full copies already exist, one per monitor.)
 *
 * WEB AND ANDROID DO NOT CHANGE. A browser tab cannot make OS windows, so they keep the DOM windows
 * in os.js. This is a second BACKEND behind the same openApp/openDoc/focusWin API, and the two are
 * deliberately fed from the same list of views — two render paths for one surface is a trap this
 * codebase has paid for before.
 *
 * OFF BY DEFAULT until stage 4. `localStorage.pc_os_toplevels = '1'` turns it on for one machine.
 */
(function (root) {
  'use strict';

  const PARAM = 'pcwin';                 // the view this window was opened for
  const TITLE = 'PosterChan Window';     // sway keys its floating rule on this — see sway.config
  const ROUTE_CHANNEL = 'pc-os-window-route-v1';

  /* Is this document a window rather than the desktop? Asked of the URL, not of a flag: a window is
   * opened WITH the parameter, so a child must behave as one even if the flag was turned off in the
   * desktop after it opened. */
  /* REMEMBERED, NOT RE-READ. The client rewrites its own URL during boot — routing replaces the
   * path and the query goes with it — so `?pcwin=` is gone within a second of the page loading.
   * Asked of the URL, this window then reported that it was not a window at all: it kept the title
   * (set before the rewrite) and landed on the timeline like any other page, so every window was
   * the same window. Measured on the real desktop: `location.search` empty, `isWindow()` false,
   * `VIEW` 'global'.
   *
   * The URL is the only way IN — a child is opened with it — but the answer is latched the first
   * time it is asked, before anything can navigate. */
  function isWindow(){
    try{ if(root.__PC_WIN_STATE__) return true; }catch(_){ }
    try{
      if(new URLSearchParams(root.location.search).has(PARAM)){ _latch(); return true; }
    }catch(_){ }
    return false;
  }

  function viewOf(){
    try{ if(root.__PC_WIN_STATE__) return String(root.__PC_WIN_STATE__.view || ''); }catch(_){ }
    try{ return String(new URLSearchParams(root.location.search).get(PARAM) || ''); }
    catch(_){ return ''; }
  }

  /* Write the latch as early as anything asks, so a later navigation cannot erase what this window
   * is for. `adopt()` fills in the rest. */
  function _latch(){
    try{
      if(root.__PC_WIN_STATE__) return;
      const v = String(new URLSearchParams(root.location.search).get(PARAM) || '');
      root.__PC_WIN_STATE__ = { view: v, shared: false, label: '' };
    }catch(_){ }
  }

  /* THE DESKTOP THIS WINDOW BELONGS TO, or null. `window.opener` is same-origin here, so this is a
   * live reference to the shell's own client — not a copy and not a message channel.
   *
   * Every access is guarded: the opener can be CLOSED while this window is still up (the desktop
   * crashed, or the renderer was rebuilt under memory pressure), and reading a dead one throws. A
   * window whose desktop has gone is not broken, it is just alone — stage 2 decides what it can
   * still do; stage 1 only has to notice. */
  function desktop(){
    try{
      const o = root.opener;
      if(!o || o.closed) return null;
      return o.__PC ? o : null;
    }catch(_){ return null; }
  }

  /* Only where a compositor window is possible AND asked for. The desktop shell is the one place:
   * a browser tab has no such thing, and the APK's WebView is a single surface. */
  function enabled(){
    try{
      /* `pcWM` is the compositor bridge and exists ONLY in the PosterChanOS shell — not in a
       * browser, not in the APK, and not in the plain desktop app running on somebody's Mac. It is
       * the honest capability test: a real toplevel is only meaningful where a window manager is
       * placing our windows. */
      if(!root.pcWM) return false;
      if(isWindow()) return false;        // a window does not open windows; the desktop does
      /* ON BY DEFAULT ON POSTERCHANOS, WITH ONE KEY TO TURN IT OFF.
       *
       * This was opt-in while it was unproven. What it replaces is not a rough edge: sway paints
       * floating windows above tiled ones unconditionally, the shell is the one tiled window, and
       * every native app floats — so a PosterChan window can NEVER be drawn in front of Firefox,
       * Telegram or Steam. Everything the shell does to fake it (park the surface, leave a
       * screenshot card, put it back) produced "swallowing windows", "sticking", "terminal gets
       * fucked by telegram and firefox, can never get focus".
       *
       * A real toplevel has none of that problem: sway stacks it with everything else, clicking
       * raises exactly it, and alt-tab reaches it.
       *
       * `pc_os_toplevels = '0'` turns it off for one machine, and every caller falls back to the
       * in-page frame when `open()` answers null — so a refusal costs the old behaviour, not a
       * lost window. */
      return String(root.localStorage.getItem('pc_os_toplevels') || '') !== '0';
    }catch(_){ return false; }
  }

  /* Open one. Returns the child window handle, or null when this build/platform cannot — the caller
   * falls back to the in-page window, which is what web and Android always use. */
  /* A WINDOW IS ONLY WORTH OPENING FOR SOMETHING THE CLIENT CAN RENDER.
   *
   * `switchView` does not validate its argument: an unknown view sets VIEW and falls through to the
   * default timeline. So a window opened on a name nothing routes does not fail — it succeeds, with
   * the wrong contents and the right title, which is how "System settings just loaded a social
   * feed" happened. The caller that got it wrong has been fixed; this is the rule stated where no
   * future caller can get round it. `.nav-item[data-view]` is the same list the desktop reads to
   * draw its icons, so a view added to the nav is poppable for free and one that is not, is not. */
  function routable(view){
    const v = String(view || '');
    if(!v || !/^[a-z0-9_-]+$/i.test(v)) return false;
    try{ return !!root.document.querySelector('.nav-item[data-view="' + v + '"]'); }
    catch(_){ return false; }
  }

  function open(view, label, opts){
    if(!enabled()) return null;
    if(!routable(view)) return null;
    /* Harmless when no window exists, essential when main rejects this request because another
     * monitor (or a click one frame earlier) already created the singleton. It also returns a
     * navigated Social window before the compositor snapshot catches up. */
    routeExisting(view);
    const o = opts || {};
    const url = root.location.pathname + '?' + PARAM + '=' + encodeURIComponent(String(view || ''));
    /* The size is a HINT to the compositor, passed as window features because a frameless Electron
     * child takes its geometry from them. sway may place it elsewhere and that is fine: it is the
     * window manager now, which is the entire point of this change. */
    const features = 'width=' + Math.max(360, Math.round(o.width || 1100)) +
                     ',height=' + Math.max(240, Math.round(o.height || 760));
    let win = null;
    try{ win = root.open(url, '_blank', features); }catch(_){ win = null; }
    if(!win) return null;
    try{ win.__PC_WINDOW_LABEL__ = String(label || view || ''); }catch(_){ }
    return win;
  }

  /* A launcher click may come from either monitor's shell renderer, while the already-open app is
   * a different renderer. Focusing its compositor surface is not navigation: if Social currently
   * shows a profile, focus alone strands the person on that profile forever. Broadcast the
   * canonical app route across the shared app:// origin; only the window whose latched identity
   * matches consumes it. This also works when the window was opened by the other monitor. */
  function routeExisting(view){
    const v=String(view||'');
    if(!v||!routable(v)||typeof root.BroadcastChannel!=='function')return false;
    try{const ch=new root.BroadcastChannel(ROUTE_CHANNEL);ch.postMessage({view:v});ch.close();return true;}
    catch(_){return false;}
  }

  try{
    if(typeof root.BroadcastChannel==='function'){
      const ch=new root.BroadcastChannel(ROUTE_CHANNEL);
      ch.onmessage=(event)=>{
        const v=String(event&&event.data&&event.data.view||''),state=root.__PC_WIN_STATE__;
        if(!state||String(state.view||'')!==v)return;
        try{if(root.__PC&&typeof root.__PC.switchView==='function')root.__PC.switchView(v);}catch(_){}
        try{root.focus();}catch(_){}
      };
    }
  }catch(_){}

  /* Called by the child as early as it can. Stage 1 does two things and no more: name the window so
   * the compositor can tell it from the desktop, and record whether the shared client is reachable
   * — which is the one assumption the whole design rests on and the one worth failing loudly. */
  function adopt(){
    if(!isWindow()) return null;
    const view = viewOf();
    try{ root.document.title = TITLE + (view ? ' — ' + view : ''); }catch(_){ }
    try{ root.document.documentElement.classList.add('pc-oswin'); }catch(_){ }
    const host = desktop();
    const state = root.__PC_WIN_STATE__ || { view, shared: false, label: '' };
    state.view = view || state.view; state.shared = !!host;
    try{ state.label = String(root.__PC_WINDOW_LABEL__ || ''); }catch(_){ }
    root.__PC_WIN_STATE__ = state;
    installChrome(state);
    return state;
  }

  function installChrome(state){
    try{
      if(root.document.getElementById('pc-oswin-chrome'))return;
      const bar=root.document.createElement('header');bar.id='pc-oswin-chrome';
      bar.className=root.localStorage.getItem('osDesktopStyle')==='mac'?'mac':'';
      bar.innerHTML='<span class="pc-oswin-title"></span><span class="pc-oswin-buttons">'
        +'<button data-action="min" title="Minimise" aria-label="Minimise">−</button>'
        +'<button data-action="max" title="Maximise" aria-label="Maximise">□</button>'
        +'<button data-action="close" title="Close" aria-label="Close">×</button></span>';
      bar.querySelector('.pc-oswin-title').textContent=String(state.label||state.view||'PosterChan');
      (root.document.body||root.document.documentElement).prepend(bar);
      bar.querySelector('[data-action="close"]').onclick=()=>root.close();
      bar.querySelector('[data-action="min"]').onclick=async()=>{const row=await root.pcWM.self();if(row)await root.pcWM.hide(row.id);};
      /* An application maximise is the output WORKAREA, not compositor fullscreen. Fullscreen is
       * reserved for games/video and deliberately obscures every sibling on that output — using it
       * here made Wayfire look like it only supported one application at a time. */
      bar.querySelector('[data-action="max"]').onclick=async()=>{const row=await root.pcWM.self();if(row)await root.pcWM.snap(row.id,'max');};
    }catch(_){ }
  }

  const API = { isWindow, viewOf, desktop, enabled, open, routeExisting, routable, adopt, PARAM, TITLE };
  root.PCOSWin = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);

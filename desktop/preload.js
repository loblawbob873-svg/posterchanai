/* Bridge for OUR OWN pages: the bundled client (app://posterchan) and the local file:// pages
 * (boot.html, shell.html, picker.html).
 *
 * The trust story changed when the client moved into the installer. It used to be REMOTE — served by
 * whichever instance the user pointed at — so the bridge was withheld from it: a compromised instance
 * could otherwise have repointed the app or enumerated the user's windows. The client now ships inside
 * the app and runs on its own app:// origin, so it is as much "our page" as boot.html is, and it needs
 * the bridge to do the two things a page cannot: name an instance, and drive tor.
 *
 * What must NOT get the bridge is anything remote the client embeds — the <iframe> to <instance>/admin
 * above all. The local exception is an exact allowlist: trusting the whole file:// scheme would give
 * a downloaded HTML file the same native powers as shell.html. Every handler in main.js re-checks
 * the sender rather than trusting this.
 */
const { contextBridge, ipcRenderer, webFrame } = require('electron');

/* Surface metadata only proves that Wayfire accepted a buffer; it does not prove that a physical
 * output is displaying the renderer.  The session watchdog captures every output and looks for
 * this tiny, deterministic 8x8 marker before declaring the desktop ready.  It is intentionally
 * installed by the preload (not page JavaScript), so a stalled client bootstrap still fails the
 * visual gate.  Four solid quadrants survive fractional output scaling and colour rounding while
 * remaining smaller than a title-bar icon. */
/* ...AND ONLY ON A SHELL SURFACE. `process.argv` belongs to the PROCESS, and a popped-out window is
 * a same-origin child in the SHELL'S renderer process — so every window and every popup inherited
 * the flag and painted this 8x8 marker in its own top-left corner, permanently. Reported exactly
 * that way: "why do all the posterchan windows have a colored square on the left? that is ugly."
 * It is a watchdog contract for the two shell surfaces the health probe screenshots, and on those
 * it sits 1px from the corner of the screen; in a window it is 1px from the corner of the TITLE
 * BAR, which is where somebody is looking. `window.opener` is the honest test — a popped-out window
 * is opened through window.open and keeps a live opener, a shell surface is created by main.js and
 * has none — and `?pcpopup=` covers the menus, which main.js loads without an opener. */
const _pcShellSurface = () => {
  try{ if(window.opener) return false; }catch(_){ return false; }
  try{ if(new URLSearchParams(location.search).get('pcpopup')) return false; }catch(_){ }
  return true;
};
if(process.argv.includes('--pc-shell-health-marker') && _pcShellSurface()){
  const installHealthMarker=()=>{
    if(document.getElementById('pc-shell-health-marker'))return;
    const marker=document.createElement('div');
    marker.id='pc-shell-health-marker';
    marker.setAttribute('aria-hidden','true');
    marker.innerHTML='<i></i><i></i><i></i><i></i>';
    Object.assign(marker.style,{position:'fixed',left:'1px',top:'1px',width:'8px',height:'8px',
      display:'grid',gridTemplateColumns:'4px 4px',gridTemplateRows:'4px 4px',zIndex:'2147483647',
      pointerEvents:'none',contain:'strict'});
    const colours=process.argv.includes('--pc-secondary-surface')
      ? ['#4b5cff','#ff5c35','#9bdb4d','#f6d55c']
      : ['#d12e91','#23cde8','#79d447','#f0b429'];
    colours.forEach((colour,index)=>{
      marker.children[index].style.cssText=`display:block;background:${colour}!important`;
    });
    (document.documentElement||document.body).appendChild(marker);
  };
  let _markerWatch=null,_markerDone=false;
  const dropHealthMarker=()=>{
    _markerDone=true;
    try{ if(_markerWatch) _markerWatch.disconnect(); }catch(_){ }
    _markerWatch=null;
    try{ const m=document.getElementById('pc-shell-health-marker'); if(m) m.remove(); }catch(_){ }
  };
  const keepHealthMarker=()=>{
    installHealthMarker();
    /* The client can replace documentElement/body while adopting its hydrated shell. The marker
     * is a startup contract, not a one-frame splash, so keep re-installing it through hydration
     * and in-app navigation — until the gate it exists for has passed. */
    _markerWatch=new MutationObserver(()=>{
      if(_markerDone) return;
      if(!document.getElementById('pc-shell-health-marker'))installHealthMarker();});
    _markerWatch.observe(document.documentElement,{childList:true,subtree:true});
  };
  /* AND THEN IT GOES AWAY. It used to stay for the life of the session — a small four-colour
   * square in the top-left corner of the desktop, for ever, on a machine whose whole job is to
   * look like a desktop ("there is a color box on the top left of the desktop too"). It is only
   * ever READ once: `pc-wayfire-health wait` screenshots the outputs before the launcher declares
   * the shell ready. main.js watches for that verdict and says so here. */
  ipcRenderer.on('pc:host:health-marker-off', dropHealthMarker);
  /* The message may have been sent BEFORE this listener existed — the ready file can appear while
   * the renderer is still loading, and an ipc send with no listener is dropped. main.js repeats it
   * on every `did-finish-load` for that reason; this covers the same race from the other side by
   * telling it we are here. */
  try{ ipcRenderer.send('pc:host:health-marker-listening'); }catch(_){ }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',keepHealthMarker,{once:true});
  else keepHealthMarker();
}

/* A WAYLAND MENU MUST NOT PAINT BEFORE THE COMPOSITOR HAS POSITIONED IT. Sway's title rule is still
 * the outer safety net, but a newly mapped surface can contribute one frame before that rule wins.
 * Hide the renderer independently, then let main.js remove this sheet only after placeAndReveal.
 * The BrowserWindow itself is transparent too, so neither the client background nor a dark native
 * backing rectangle can flash in the centre of the monitor. */
if(process.argv.includes('--pc-popup-surface')){
  let placed=false,shield='';
  const reveal=(why)=>{
    if(placed) return;
    placed=true;
    if(shield)Promise.resolve(webFrame.removeInsertedCSS(shield)).catch(()=>{});
    if(why!=='placed'){ try{ console.log('popup revealed without a placement ('+why+')'); }catch(_){ } }
  };
  Promise.resolve(webFrame.insertCSS('html,body{opacity:0!important;background:transparent!important}'))
    .then(key=>{shield=key||'';if(placed&&shield)webFrame.removeInsertedCSS(shield);}).catch(()=>{});
  ipcRenderer.once('pc:host:popup-placed',()=>reveal('placed'));
  /* A FLOOR, BECAUSE THIS LATCH IS SET BEFORE THE ATTEMPT IT WAITS ON.
   *
   * The sheet is lifted only by `pc:host:popup-placed`, and `placePopupWindow` has three ways of
   * never sending it: the window was destroyed, `_popupWin !== win` (a second popup replaced this
   * one mid-flight), or the send itself throws. Any of those leaves a LIVE, fully painted menu at
   * `opacity: 0` for as long as it exists — observed on the real desktop as a mapped
   * `PosterChan Popup` view of the right size, in the right place, with `.os-pop` drawn inside it
   * and nothing whatsoever on screen. A menu nobody can see is indistinguishable from a dead
   * button, which is how it was reported.
   *
   * Placement's own worst case is bounded (12 attempts, 60ms apart, then it sends regardless), so
   * anything past a second means the message is not coming. Revealing an unplaced menu shows it
   * briefly at the compositor's chosen position, which is the flash this shield exists to prevent
   * -- but a flash is a menu you can use and this is the case where the alternative is none. */
  setTimeout(()=>reveal('no placement within 1500ms'),1500);
}

/* Sandboxed Electron preloads may require Electron and a small built-in allowlist only. A relative
 * require works in ordinary Node tests and even leaves page-trust.js inside app.asar, but fails in
 * the real packaged renderer before ANY bridge is exposed. Keep this tiny predicate self-contained;
 * main.js still uses the shared Node module to enforce the same boundary again at IPC. */
function isTrustedPreloadPage(raw, localDir) {
  let u;
  try { u = new URL(String(raw || '')); } catch (_) { return false; }
  if (u.protocol === 'app:') return u.hostname === 'posterchan' && !u.port && !u.username && !u.password;
  if (u.protocol !== 'file:' || u.hostname) return false;
  let candidate;
  try { candidate = decodeURIComponent(u.pathname).replace(/\\/g, '/'); } catch (_) { return false; }
  let dir = String(localDir || '').replace(/\\/g, '/').replace(/\/$/, '');
  if (process.platform === 'win32') {
    candidate = candidate.replace(/^\/([A-Za-z]:\/)/, '$1').toLowerCase();
    dir = dir.toLowerCase();
  }
  return ['boot.html', 'shell.html', 'picker.html'].some(name => candidate === dir + '/' + name);
}

const preloadDirArg = process.argv.find(v => String(v).startsWith('--pc-preload-dir='));
const preloadDir = preloadDirArg ? String(preloadDirArg).slice('--pc-preload-dir='.length) : '';
const isOurPage = isTrustedPreloadPage(location.href, preloadDir);
/* A POSTERCHAN WINDOW IS NEVER THE BACKGROUND OWNER, whatever process it landed in.
 *
 * `--pc-secondary-surface` is a PROCESS argument, and a same-origin `window.open` child runs in the
 * OPENER'S process — so a window popped out of the primary surface inherits `backgroundOwner: true`
 * and becomes a second folder-sync writer over the same tree with the same device identity. That is
 * the failure the marker exists to prevent, arriving through the one door it does not cover. The
 * document says what it is: `?pcwin=` is on the URL when this preload runs, before the client
 * rewrites its own address. */
const _isWindowDoc = (() => {
  try { return new URLSearchParams(location.search).has('pcwin'); } catch (_) { return false; }
})();
const backgroundOwner = !_isWindowDoc && !process.argv.includes('--pc-secondary-surface');

// Clipboard WRITE, exposed more widely than the controls below — it cannot repoint the app or enumerate
// anything, and the main process still checks the caller. It exists because there is no working web
// clipboard against a cleartext instance: navigator.clipboard is absent outside a secure context and
// execCommand('copy') is refused, which left the Go Live stream key impossible to copy. Write-only — the
// page can never read the user's clipboard.
contextBridge.exposeInMainWorld('pcClip', {
  write: (s) => ipcRenderer.invoke('pc:clip:write', String(s == null ? '' : s)),
});

if (isOurPage) {
  // instanceSync is read SYNCHRONOUSLY, at preload time, because the bundle's shim needs
  // __PC_API_BASE__ defined before any app script evaluates — an async round trip would leave the first
  // fetch and the first WebSocket to resolve against the wrong base. sendSync is the only thing that
  // can answer that early, and it is one tiny string once per page load.
  let instanceSync = '';
  try { instanceSync = ipcRenderer.sendSync('pc:instance:sync') || ''; } catch (_) {}

  /* READ is OUR PAGE'S ONLY -- see pcClip above, which is write-only on purpose and exposed to any
   * page the app loads. Reading somebody's clipboard is a different power and it stays behind the
   * same gate as the compositor and the network. */
  contextBridge.exposeInMainWorld('pcClipRead', {
    read: () => ipcRenderer.invoke('pc:clip:read'),
  });

  contextBridge.exposeInMainWorld('pcShell', {
    instanceSync,
    /* Multi-monitor PosterChanOS has one renderer per output. Only the primary may run unattended
     * services such as folder sync; otherwise every extra monitor becomes another filesystem
     * writer with the same device identity. */
    backgroundOwner,
    getInstance: () => ipcRenderer.invoke('pc:instance:get'),
    setInstance: (url) => ipcRenderer.invoke('pc:instance:set', url == null ? '' : url),
    retry: () => ipcRenderer.send('pc:retry'),
    /* Tray → "Sync folders now". The tray lives in the main process and folder sync lives in the
     * renderer (every step is signed by the user's key, which only the page can reach), so the menu
     * item can only ask. Wrapped so the page never sees the IpcRendererEvent. */
    onSyncNow: (fn) => {
      if (typeof fn !== 'function') return;
      ipcRenderer.on('pc:sync:now', () => { try { fn(); } catch (_) {} });
    },
    /* The machine came back from sleep. See pushWake in main.js: a desktop window that was never
     * hidden gets no visibilitychange, so this is the only reliable signal the page has that its
     * sockets are older than they look. */
    onWake: (fn) => {
      if (typeof fn !== 'function') return;
      ipcRenderer.on('pc:wake', () => { try { fn(); } catch (_) {} });
    },
    // `tor` doubles as the capability test the client uses (_hasNativeTor), so it must be absent rather
    // than present-and-broken on a build without it.
    tor: {
      status: () => ipcRenderer.invoke('pc:tor:status'),
      set: (opts) => ipcRenderer.invoke('pc:tor:set', opts || {}),
      newCircuit: () => ipcRenderer.invoke('pc:tor:new-circuit'),
      restart: () => ipcRenderer.invoke('pc:tor:restart'),
      // Push, not poll: bootstrap goes 0→100 over seconds and the progress card would either lag or
      // spin a timer. The listener is wrapped so the page never receives the IpcRendererEvent itself.
      onStatus: (fn) => {
        if (typeof fn !== 'function') return;
        ipcRenderer.on('pc:tor:status', (_e, s) => { try { fn(s); } catch (_) {} });
      },
    },
  });
  /* Folder sync. Present only in the desktop app, which is also how the client feature-detects it:
   * the web PWA has no filesystem at all (and Firefox has no File System Access API), so the Sync
   * screen keys off `window.pcFs` rather than off a user agent.
   *
   * Nothing here takes an absolute path. Every call names a ROOT ID the user created in a native
   * folder picker plus a path RELATIVE to it, so the page cannot express a location outside a
   * directory a human chose — the check is enforced again in the main process (fsbridge.resolveIn),
   * because a preload is not a security boundary against the page it serves.
   *
   * read() hands back a Uint8Array rather than the Buffer that crosses IPC: Buffer is a Node type,
   * and leaking one into the page gives it a prototype full of things the page should not have. */
  /* The compositor, the network, and the one privileged call.
   *
   * PosterChanOS runs this app as the SHELL of a Wayland compositor: sway owns the screen, a
   * browser and a Steam game are ordinary clients, and the page decides where they go. Absent
   * rather than broken off a compositor — `available()` answers no when SWAYSOCK is unset, so a
   * desktop install that is not PosterChanOS has no window manager rather than calls that throw. */
  contextBridge.exposeInMainWorld('pcWM', {
    available: () => ipcRenderer.invoke('pc:wm:available'),
    windows: () => ipcRenderer.invoke('pc:wm:windows'),
    self: () => ipcRenderer.invoke('pc:wm:self'),
    /* `windows` is this shell surface's ownership view; `allIds` distinguishes an app that moved
     * to another output from one that actually exited. */
    snapshot: () => ipcRenderer.invoke('pc:wm:snapshot'),
    preview: (id) => ipcRenderer.invoke('pc:wm:preview', Number(id)),
    focus: (id) => ipcRenderer.invoke('pc:wm:focus', Number(id)),
    cycleOutput: (direction) => ipcRenderer.invoke('pc:wm:cycle-output', String(direction||'')),
    close: (id) => ipcRenderer.invoke('pc:wm:close', Number(id)),
    place: (id, x, y, w, h) => ipcRenderer.invoke('pc:wm:place', Number(id), Number(x), Number(y),
                                                  Number(w), Number(h)),
    /* The taskbar band, measured by the only process that can measure it. Sent as plain numbers:
     * the compositor side must not have to trust a shape from the page. */
    workArea: (area) => ipcRenderer.invoke('pc:wm:workarea', {
      x: Number((area||{}).x)||0, y: Number((area||{}).y)||0,
      w: Number((area||{}).w)||0, h: Number((area||{}).h)||0,
      reserve: Number((area||{}).reserve)||0 }),
    move: (id, x, y) => ipcRenderer.invoke('pc:wm:move', Number(id), Number(x), Number(y)),
    handoff: (id, direction, drop) => ipcRenderer.invoke('pc:wm:handoff', Number(id),
                                                          String(direction||''), drop||{}),
    nativeHandoffAck: (token, rect) => ipcRenderer.invoke('pc:wm:native-handoff-ack',
                                                           String(token||''), rect||{}),
    handoffReady: (ready) => ipcRenderer.invoke('pc:wm:handoff-ready', ready !== false),
    updateIdle: (token) => ipcRenderer.invoke('pc:shell:update-idle', String(token||'')),
    onNativeHandoff: (fn) => {
      const h = (_e, payload) => { try { fn(payload || {}); } catch (_) {} };
      ipcRenderer.on('pc:wm:native-handoff', h);
      return () => ipcRenderer.removeListener('pc:wm:native-handoff', h);
    },
    onNativeHandoffPrepare: (fn) => {
      const h = (_e, payload) => { try { fn(payload || {}); } catch (_) {} };
      ipcRenderer.on('pc:wm:native-handoff-prepare', h);
      return () => ipcRenderer.removeListener('pc:wm:native-handoff-prepare', h);
    },
    onNativeHandoffAbort: (fn) => {
      const h = (_e, payload) => { try { fn(payload || {}); } catch (_) {} };
      ipcRenderer.on('pc:wm:native-handoff-abort', h);
      return () => ipcRenderer.removeListener('pc:wm:native-handoff-abort', h);
    },
    handoffFrame: (payload, direction) => ipcRenderer.invoke('pc:wm:handoff-frame', payload || {},
                                                              String(direction||'')),
    onHandoffFrame: (fn) => {
      const h = (_e, payload) => { try { fn(payload || {}); } catch (_) {} };
      ipcRenderer.on('pc:wm:handoff-frame', h);
      return () => ipcRenderer.removeListener('pc:wm:handoff-frame', h);
    },
    previewFrame: (payload, direction) => ipcRenderer.invoke('pc:wm:preview-frame', payload || null,
                                                              String(direction||'')),
    onPreviewFrame: (fn) => {
      const h = (_e, payload) => { try { fn(payload || null); } catch (_) {} };
      ipcRenderer.on('pc:wm:preview-frame', h);
      return () => ipcRenderer.removeListener('pc:wm:preview-frame', h);
    },
    /* Minimise, as the compositor can express it: the window is moved to the scratchpad, keeps
     * running, and comes back where it was. */
    hide: (id) => ipcRenderer.invoke('pc:wm:hide', Number(id)),
    show: (id) => ipcRenderer.invoke('pc:wm:show', Number(id)),
    restore: (id,x,y,w,h) => ipcRenderer.invoke('pc:wm:restore',Number(id),Number(x),Number(y),
                                                 Number(w),Number(h)),
    fullscreen: (id, on) => ipcRenderer.invoke('pc:wm:fullscreen', Number(id), !!on),
    snap: (id, zone) => ipcRenderer.invoke('pc:wm:snap', Number(id), String(zone||'')),
    decorate: (id, hosted) => ipcRenderer.invoke('pc:wm:decorate', Number(id), !!hosted),
    /* An ARGV ARRAY, never a command string — a string would have to reach a shell to be useful,
     * and then a file name with a space in it is an injection. */
    launch: (argv, opts) => ipcRenderer.invoke('pc:wm:launch', (argv || []).map(String), opts || {}),
    subscribe: () => ipcRenderer.invoke('pc:wm:subscribe'),
    /* Returns an unsubscribe function: a listener the page cannot remove leaks a closure per view
     * change, and the desktop redraws its taskbar on every window event. */
    onEvent: (fn) => {
      const h = (_e, ev) => { try { fn(ev); } catch (_) {} };
      ipcRenderer.on('pc:wm:event', h);
      return () => ipcRenderer.removeListener('pc:wm:event', h);
    },
  });

  /* THE POPUP SURFACE. A menu that must sit above applications cannot be drawn inside the desktop
   * shell: sway paints floating windows above tiled ones and the shell is the tiled one. This opens
   * a real floating window for it instead — see pc:popup:open in main.js. `pick` is how that
   * window, which is its own renderer, tells the shell what was chosen. */
  contextBridge.exposeInMainWorld('pcPopup', {
    open: (kind, rect, arg) =>
      ipcRenderer.invoke('pc:popup:open', String(kind || ''), rect || {}, String(arg == null ? '' : arg)),
    /* Open it, or close it if this kind is already up — decided by the process that owns the
     * window, never by what a renderer remembers about it. Resolves true when a window is now
     * showing, false when there is none. */
    toggle: (kind, rect, arg) =>
      ipcRenderer.invoke('pc:popup:toggle', String(kind || ''), rect || {}, String(arg == null ? '' : arg)),
    close: () => ipcRenderer.invoke('pc:popup:close'),
    pick: (view) => ipcRenderer.invoke('pc:popup:pick', String(view || '')),
    /* Anything that is not a view name — open this post, reply to this event, run this tray action.
     * `keepOpen` leaves the popup up, which is what a settings flyout needs and a menu does not. */
    act: (action, keepOpen) =>
      ipcRenderer.invoke('pc:popup:act', String(action || ''), !!keepOpen),
  });

  contextBridge.exposeInMainWorld('pcDisplays', {
    status: () => ipcRenderer.invoke('pc:display:status'),
    preview: rows => ipcRenderer.invoke('pc:display:preview', Array.isArray(rows) ? rows : []),
    confirm: token => ipcRenderer.invoke('pc:display:confirm', String(token||'')),
    revert: token => ipcRenderer.invoke('pc:display:revert', String(token||'')),
  });

  contextBridge.exposeInMainWorld('pcRemoteControl', {
    configure: info => ipcRenderer.invoke('pc:remote:configure', info && typeof info==='object' ? info : {}),
    input: event => ipcRenderer.invoke('pc:remote:input', event && typeof event==='object' ? event : {}),
    release: () => ipcRenderer.invoke('pc:remote:release'),
  });

  contextBridge.exposeInMainWorld('pcLiveUSB', {
    devices: () => ipcRenderer.invoke('pc:liveusb:devices'),
    status: () => ipcRenderer.invoke('pc:liveusb:status'),
    build: (dir, home) => ipcRenderer.invoke('pc:liveusb:build', String(dir||''), !!home),
    burn: (iso, disk) => ipcRenderer.invoke('pc:liveusb:burn', String(iso||''), String(disk||'')),
    pickISO: () => ipcRenderer.invoke('pc:liveusb:pick-iso'),
    pickDir: () => ipcRenderer.invoke('pc:liveusb:pick-dir'),
  });

  contextBridge.exposeInMainWorld('pcNet', {
    available: () => ipcRenderer.invoke('pc:net:available'),
    status: () => ipcRenderer.invoke('pc:net:status'),
    wifi: (rescan) => ipcRenderer.invoke('pc:net:wifi', !!rescan),
    connect: (ssid, password) => ipcRenderer.invoke('pc:net:connect', String(ssid || ''),
                                                    password == null ? '' : String(password)),
    forget: (ssid) => ipcRenderer.invoke('pc:net:forget', String(ssid || '')),
    radio: (on) => ipcRenderer.invoke('pc:net:radio', !!on),
  });

  /* Printers. CUPS's own admin pages authenticate a system account through PAM, and a PosterChanOS
   * identity account deliberately has no Unix password — so that UI can never be logged into on
   * this OS. The shell drives the CUPS command-line tools through the NOPASSWD sudo grant the first
   * owner already holds, the same way Displays and Power drive their hardware. */
  contextBridge.exposeInMainWorld('pcPrinters', {
    status: () => ipcRenderer.invoke('pc:printers:status'),
    discover: () => ipcRenderer.invoke('pc:printers:discover'),
    add: (spec) => ipcRenderer.invoke('pc:printers:add', spec && {
      name: String(spec.name || ''), uri: String(spec.uri || ''),
      description: String(spec.description || '') }),
    setDefault: (name) => ipcRenderer.invoke('pc:printers:default', String(name || '')),
    remove: (name) => ipcRenderer.invoke('pc:printers:remove', String(name || '')),
    testPage: (name) => ipcRenderer.invoke('pc:printers:test', String(name || '')),
  });

  contextBridge.exposeInMainWorld('pcPower', {
    status: () => ipcRenderer.invoke('pc:power:status'),
    /* A PERCENTAGE, and the main process clamps it — it can never reach 0, because on most panels
     * 0 is off rather than dim and somebody who cannot see the screen cannot undo it. */
    setBrightness: (pct) => ipcRenderer.invoke('pc:power:brightness', Number(pct)),
    setProfile: (name) => ipcRenderer.invoke('pc:power:profile', String(name || '')),
    setKeepAwake: (on) => ipcRenderer.invoke('pc:power:keep-awake', !!on),
    setIdleTimeout: (seconds) => ipcRenderer.invoke('pc:power:idle', Number(seconds)),
    suspend: () => ipcRenderer.invoke('pc:power:suspend'),
    hibernate: () => ipcRenderer.invoke('pc:power:hibernate'),
    enableHibernation: () => ipcRenderer.invoke('pc:power:enable-hibernate'),
    poweroff: () => ipcRenderer.invoke('pc:power:poweroff'),
    reboot: () => ipcRenderer.invoke('pc:power:reboot'),
  });

  contextBridge.exposeInMainWorld('pcAudio', {
    status: () => ipcRenderer.invoke('pc:audio:status'),
    /* Also a percentage. wpctl wants a fraction, and 50 means five thousand percent to it. */
    setVolume: (pct, which) => ipcRenderer.invoke('pc:audio:volume', Number(pct), which || 'sink'),
    setMuted: (on, which) => ipcRenderer.invoke('pc:audio:mute', !!on, which || 'sink'),
    setDefault: (id) => ipcRenderer.invoke('pc:audio:default', Number(id)),
    /* One row per playing application, each with its own level. Read only while the mixer is open:
     * `wpctl status` does not print a stream's volume, so every row costs a subprocess. */
    mixer: () => ipcRenderer.invoke('pc:audio:mixer'),
    setStreamVolume: (id, pct) => ipcRenderer.invoke('pc:audio:streamvol', Number(id), Number(pct)),
    setStreamMuted: (id, on) => ipcRenderer.invoke('pc:audio:streammute', Number(id), !!on),
  });

  contextBridge.exposeInMainWorld('pcBluetooth', {
    status: (scan) => ipcRenderer.invoke('pc:bt:status', !!scan),
    power: (on) => ipcRenderer.invoke('pc:bt:power', !!on),
    device: (address, action) => ipcRenderer.invoke('pc:bt:device', String(address||''), String(action||'')),
  });

  contextBridge.exposeInMainWorld('pcSystem', {
    snapshot: (withProcesses) => ipcRenderer.invoke('pc:system:snapshot', !!withProcesses),
    end: (pid) => ipcRenderer.invoke('pc:system:end', Number(pid)),
  });

  contextBridge.exposeInMainWorld('pcVM', {
    list: () => ipcRenderer.invoke('pc:vm:list'),
    create: (opts) => ipcRenderer.invoke('pc:vm:create', opts || {}),
    action: (name, action) => ipcRenderer.invoke('pc:vm:action', String(name||''), String(action||'')),
    remove: (name, disks) => ipcRenderer.invoke('pc:vm:remove', String(name||''), !!disks),
    view: (name) => ipcRenderer.invoke('pc:vm:view', String(name||'')),
    details: (name) => ipcRenderer.invoke('pc:vm:details', String(name||'')),
    update: (name, opts) => ipcRenderer.invoke('pc:vm:update', String(name||''), opts||{}),
    addDisk: (name, gib) => ipcRenderer.invoke('pc:vm:add-disk', String(name||''), Number(gib)),
    changeIso: (name, iso) => ipcRenderer.invoke('pc:vm:change-iso', String(name||''), String(iso||'')),
    ejectIso: (name) => ipcRenderer.invoke('pc:vm:eject-iso', String(name||'')),
    bootDisk: (name) => ipcRenderer.invoke('pc:vm:boot-disk', String(name||'')),
    addNetwork: (name) => ipcRenderer.invoke('pc:vm:add-network', String(name||'')),
    gamingMouse: (name, on) => ipcRenderer.invoke('pc:vm:gaming-mouse', String(name||''), !!on),
    pickIso: () => ipcRenderer.invoke('pc:vm:pick-iso'),
  });

  /* SCREENSHOTS. `available()` answers before anything is drawn, so the tray never offers a button
   * whose only possible outcome is an error about a missing package. */
  contextBridge.exposeInMainWorld('pcShot', {
    available: () => ipcRenderer.invoke('pc:shot:available'),
    take: (opts) => ipcRenderer.invoke('pc:shot:take', opts || {}),
  });

  contextBridge.exposeInMainWorld('pcTerm', {
    start: (opts) => ipcRenderer.invoke('pc:term:start', opts || {}),
    write: (id, d) => ipcRenderer.invoke('pc:term:write', String(id), String(d == null ? '' : d)),
    resize: (id, c, r) => ipcRenderer.invoke('pc:term:resize', String(id), Number(c), Number(r)),
    /* What a reloaded page redraws from. The WebView is recreated under memory pressure and on a
     * crash, and a terminal that comes back blank is one nobody trusts with a long command. */
    backlog: (id, since) => ipcRenderer.invoke('pc:term:backlog', String(id), Number(since) || 0),
    close: (id) => ipcRenderer.invoke('pc:term:close', String(id)),
    list: () => ipcRenderer.invoke('pc:term:list'),
    attach: (id) => ipcRenderer.invoke('pc:term:attach', String(id)),
    detach: (id) => ipcRenderer.invoke('pc:term:detach', String(id)),
    onData: (fn) => {
      const h = (_e, ev) => { try { fn(ev); } catch (_) {} };
      ipcRenderer.on('pc:term:data', h);
      return () => ipcRenderer.removeListener('pc:term:data', h);
    },
  });

  /* THE MACHINE'S OWN APPLICATIONS, for the start menu. Read-only: this lists what is installed and
   * nothing else — starting one goes through `pcWM.launch`, which is the same guarded path the
   * built-in entries already use, so there is one place a process can be started from. */
  /* THE COMPUTER'S OWN FILES, so the Files screen can browse the disk it is running on as well as
   * the encrypted drive and a synced folder. Absent everywhere else, which is how the client
   * feature-detects it — a browser tab has no filesystem at all. */
  contextBridge.exposeInMainWorld('pcHost', {
    notify: (opts) => ipcRenderer.invoke('pc:host:notify', opts || {}),
    onNotificationClick: (fn) => {
      if (typeof fn !== 'function') return () => {};
      const h = (_e, route) => { try { fn(String(route || 'notifications')); } catch (_) {} };
      ipcRenderer.on('pc:host:notification-click', h);
      return () => ipcRenderer.removeListener('pc:host:notification-click', h);
    },
    pickDirectory: () => ipcRenderer.invoke('pc:host:pickDirectory'),
    pickFile: (opts) => ipcRenderer.invoke('pc:host:pickFile', opts || {}).then((r) => r && ({
      name:String(r.name||'file'), type:String(r.type||'application/octet-stream'), size:Number(r.size)||0,
      path:String(r.path||''), mtime:Number(r.mtime)||0, data:new Uint8Array(r.data)
    })),
    saveFile: (name, bytes) => ipcRenderer.invoke('pc:host:saveFile', String(name || 'document'),
      bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || [])),
    list: (dir) => ipcRenderer.invoke('pc:host:list', String(dir || '')),
    roots: () => ipcRenderer.invoke('pc:host:roots'),
    // PosterChan Code, editing a file on this computer.
    readText: (p) => ipcRenderer.invoke('pc:host:readText', String(p || '')),
    writeText: (p, text, mtime) => ipcRenderer.invoke('pc:host:writeText', String(p || ''),
                                                       String(text == null ? '' : text), Number(mtime) || 0),
    // Office, editing a document on this computer: bytes, never a string (an .odt is a ZIP).
    writeBytes: (p, bytes, mtime) => ipcRenderer.invoke('pc:host:writeBytes', String(p || ''),
      bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []), Number(mtime) || 0),
    gitStatus: (root) => ipcRenderer.invoke('pc:host:gitStatus', String(root||'')),
    gitDiff: (root, p) => ipcRenderer.invoke('pc:host:gitDiff', String(root||''), String(p||'')),
    gitAction: (root, action, paths, message) => ipcRenderer.invoke('pc:host:gitAction', String(root||''),
      String(action||''), Array.isArray(paths)?paths:[], String(message||'')),
    search: (q, opts) => ipcRenderer.invoke('pc:host:search', String(q || ''), opts || {}),
    mkdir: (dir, name) => ipcRenderer.invoke('pc:host:mkdir', String(dir || ''), String(name || '')),
    rename: (from, to) => ipcRenderer.invoke('pc:host:rename', String(from || ''), String(to || '')),
    trash: (target) => ipcRenderer.invoke('pc:host:trash', String(target || '')),
    transfer: (items, destination, move) => ipcRenderer.invoke('pc:host:transfer',
      Array.isArray(items) ? items.map(String) : [], String(destination || ''), !!move),
    read: (target, max) => ipcRenderer.invoke('pc:host:read', String(target || ''), Number(max) || 0)
      .then((b) => new Uint8Array(b)),
    open: (target) => ipcRenderer.invoke('pc:host:open', String(target || '')),
  });

  contextBridge.exposeInMainWorld('pcApps', {
    list: () => ipcRenderer.invoke('pc:apps:list'),
  });

  contextBridge.exposeInMainWorld('pcOS', {
    /* A Unix account and a private home for whoever just signed in. The main process re-checks the
     * npub before it runs anything as root, and so does the script — the page is not trusted to
     * have validated it. */
    provision: (npub) => ipcRenderer.invoke('pc:os:provision', String(npub || '')),
    provisioned: () => ipcRenderer.invoke('pc:os:provisioned'),
    identity: () => ipcRenderer.invoke('pc:os:identity'),
    switch: (npub, handoff) => ipcRenderer.invoke('pc:os:switch', String(npub || ''), handoff || {}),
    logout: () => ipcRenderer.invoke('pc:os:logout'),
    bootstrap: () => ipcRenderer.sendSync('pc:os:bootstrap'),
  });

  contextBridge.exposeInMainWorld('pcFs', {
    list: () => ipcRenderer.invoke('pc:fs:list'),
    pick: () => ipcRenderer.invoke('pc:fs:pick'),
    forget: (id) => ipcRenderer.invoke('pc:fs:forget', String(id || '')),
    scan: (id, opts) => ipcRenderer.invoke('pc:fs:scan', String(id || ''), opts || {}),
    read: (id, rel) => ipcRenderer.invoke('pc:fs:read', String(id || ''), String(rel || ''))
      .then((b) => new Uint8Array(b)),
    write: (id, rel, bytes, mtime) =>
      ipcRenderer.invoke('pc:fs:write', String(id || ''), String(rel || ''),
                         bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []), mtime || 0),
    // Slice I/O. Present only where the platform implements it; sync checks for it and falls back to
    // whole-file reads (and refuses oversized files) where it is absent.
    readPart: (id, rel, offset, len) =>
      ipcRenderer.invoke('pc:fs:read-part', String(id || ''), String(rel || ''), offset || 0, len || 0)
        .then((b) => new Uint8Array(b)),
    writePart: (id, rel, offset, bytes) =>
      ipcRenderer.invoke('pc:fs:write-part', String(id || ''), String(rel || ''), offset || 0,
                         bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || [])),
    writeCommit: (id, rel, mtime) =>
      ipcRenderer.invoke('pc:fs:write-commit', String(id || ''), String(rel || ''), mtime || 0),
    move: (id, from, to) => ipcRenderer.invoke('pc:fs:move', String(id || ''), String(from || ''), String(to || '')),
    /* The trash is one place now — on the server — so a deletion here really is a deletion. Called
     * only after the executor has confirmed the store holds the bytes. */
    remove: (id, rel) => ipcRenderer.invoke('pc:fs:remove', String(id || ''), String(rel || '')),
    trash: (id, rel, when) => ipcRenderer.invoke('pc:fs:trash', String(id || ''), String(rel || ''), when || 0),
    emptyTrash: (id, days) => ipcRenderer.invoke('pc:fs:empty-trash', String(id || ''), days === 0 ? 0 : (days || 30)),
    trashStat: (id) => ipcRenderer.invoke('pc:fs:trash-stat', String(id || '')),
    hashPart: (id, rel) => ipcRenderer.invoke('pc:fs:hash-part', String(id || ''), String(rel || '')),
    /* What the far side verifies a download against. Without it a chunked upload from this device
     * carries no content identity at all, and a corrupt transfer is written and played. */
    hashFile: (id, rel) => ipcRenderer.invoke('pc:fs:hash-file', String(id || ''), String(rel || '')),
    // Positive proof for a deletion claim — see fsbridge.confirmGone. Absent on old builds,
    // and syncexec treats absence as "cannot confirm", which deletes nothing.
    confirmGone: (id, rel) => ipcRenderer.invoke('pc:fs:confirm-gone', String(id || ''), String(rel || '')),
    listTrash: (id) => ipcRenderer.invoke('pc:fs:list-trash', String(id || '')),
    purgeTrash: (id, rels) => ipcRenderer.invoke('pc:fs:purge-trash', String(id || ''), (rels || []).map(String)),
    discardPart: (id, rel) => ipcRenderer.invoke('pc:fs:discard-part', String(id || ''), String(rel || '')),
    partSize: (id, rel) => ipcRenderer.invoke('pc:fs:part-size', String(id || ''), String(rel || '')),
    sweepParts: (id, olderThanMs) => ipcRenderer.invoke('pc:fs:sweep-parts', String(id || ''), olderThanMs),
    power: () => ipcRenderer.invoke('pc:fs:power'),
    watch: (id, debounceMs) => ipcRenderer.invoke('pc:fs:watch', String(id || ''), debounceMs || 0),
    unwatch: (id) => ipcRenderer.invoke('pc:fs:unwatch', String(id || '')),
    // Push, not poll — a watcher that the page has to ask about is a timer, which is the thing the
    // battery policy exists to avoid. Wrapped so the page never receives the IpcRendererEvent.
    onChanged: (fn) => {
      if (typeof fn !== 'function') return;
      ipcRenderer.on('pc:fs:changed', (_e, id) => { try { fn(id); } catch (_) {} });
    },
  });
  // Screen-share source picker (picker.html). Kept separate so the capability stays scoped to the page
  // that draws the picker; the instance must never be able to enumerate the user's windows.
  contextBridge.exposeInMainWorld('pcScreen', {
    list: () => ipcRenderer.invoke('pc:screen:list'),
    pick: (id) => ipcRenderer.send('pc:screen:pick', String(id)),
  });
}

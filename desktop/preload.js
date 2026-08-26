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
 * above all. That is why the test is the exact app:// origin and file://, never "not remote", and why
 * every handler in main.js re-checks the sender rather than trusting this.
 */
const { contextBridge, ipcRenderer } = require('electron');

const isOurPage = location.protocol === 'file:' || location.origin === 'app://posterchan';
const backgroundOwner = !process.argv.includes('--pc-secondary-surface');

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
    /* `windows` is this shell surface's ownership view; `allIds` distinguishes an app that moved
     * to another output from one that actually exited. */
    snapshot: () => ipcRenderer.invoke('pc:wm:snapshot'),
    focus: (id) => ipcRenderer.invoke('pc:wm:focus', Number(id)),
    close: (id) => ipcRenderer.invoke('pc:wm:close', Number(id)),
    place: (id, x, y, w, h) => ipcRenderer.invoke('pc:wm:place', Number(id), Number(x), Number(y),
                                                  Number(w), Number(h)),
    move: (id, x, y) => ipcRenderer.invoke('pc:wm:move', Number(id), Number(x), Number(y)),
    handoff: (id, direction) => ipcRenderer.invoke('pc:wm:handoff', Number(id), String(direction||'')),
    onNativeHandoff: (fn) => {
      const h = (_e, payload) => { try { fn(payload || {}); } catch (_) {} };
      ipcRenderer.on('pc:wm:native-handoff', h);
      return () => ipcRenderer.removeListener('pc:wm:native-handoff', h);
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
    decorate: (id) => ipcRenderer.invoke('pc:wm:decorate', Number(id)),
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

  contextBridge.exposeInMainWorld('pcDisplays', {
    status: () => ipcRenderer.invoke('pc:display:status'),
    preview: rows => ipcRenderer.invoke('pc:display:preview', Array.isArray(rows) ? rows : []),
    confirm: token => ipcRenderer.invoke('pc:display:confirm', String(token||'')),
    revert: token => ipcRenderer.invoke('pc:display:revert', String(token||'')),
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
    pickDirectory: () => ipcRenderer.invoke('pc:host:pickDirectory'),
    list: (dir) => ipcRenderer.invoke('pc:host:list', String(dir || '')),
    roots: () => ipcRenderer.invoke('pc:host:roots'),
    // PosterChan Code, editing a file on this computer.
    readText: (p) => ipcRenderer.invoke('pc:host:readText', String(p || '')),
    writeText: (p, text, mtime) => ipcRenderer.invoke('pc:host:writeText', String(p || ''),
                                                       String(text == null ? '' : text), Number(mtime) || 0),
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

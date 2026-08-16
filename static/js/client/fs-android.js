/* Android's end of the folder-sync filesystem interface.
 *
 * Defines `window.pcFs` from the FolderSync Capacitor plugin, with exactly the shape desktop's
 * preload exposes — so sync.js, syncrun.js and foldersync.js are identical on both platforms and
 * neither knows which one it is running on. Everything platform-specific about Android lives on the
 * far side of this file: SAF tree URIs instead of paths, base64 instead of Buffers, no watcher.
 *
 * A no-op everywhere else. In a browser (and in the desktop app, which sets window.pcFs from its
 * preload) `window.Capacitor.Plugins.FolderSync` does not exist and this file defines nothing, which
 * is also how the Sync screen decides whether this device can reach a folder at all.
 */
(function(){
  'use strict';

  /* FINDING THE PLUGIN IS THE HARD PART, and getting it wrong is silent.
   *
   * This used to read `Capacitor.Plugins.FolderSync` ONCE, at script-evaluation time, and give up if
   * it was not there. Two ways that fails, and both end identically — `window.pcFs` is never set, so
   * `FS()` is null for the whole session, `startAll()` returns before doing anything, the Sync screen
   * hides "Add a folder…" and every folder sits at its placeholder status. Reported as "why is
   * Documents in Folder Sync 'not syncing yet', it was working before", which is exactly what a
   * STARTUP RACE looks like from the outside: fine most times, dead some times, and an app update is
   * enough to change which.
   *
   *   1. `Capacitor.Plugins.<name>` is EMPTY for a plugin registered in Java with no JS package of
   *      its own — `registerPlugin(name)` is what resolves those. (The same trap that made a widget
   *      push do nothing earlier the same day.)
   *   2. The native bridge may not have injected its plugin list by the time this script runs.
   *
   * So: try both, and if the bridge is not up yet, keep trying for a few seconds rather than
   * deciding for the rest of the session that this device has no filesystem.
   *
   * `registerPlugin` is only used on a NATIVE platform. In a browser it would hand back a proxy that
   * accepts every call and rejects it, which would replace "this device cannot sync" with "every
   * sync operation fails" — a worse answer, and a wrong one. */
  function _plugin(){
    const cap = window.Capacitor;
    if(!cap) return null;
    const p = cap.Plugins && cap.Plugins.FolderSync;
    if(p) return p;
    const native = cap.isNativePlatform ? cap.isNativePlatform() : !!cap.isNative;
    if(!native || !cap.registerPlugin) return null;
    try{ return cap.registerPlugin('FolderSync'); }catch(_){ return null; }
  }

  function install(){
    if(window.pcFs) return true;          // the desktop shell sets its own; never replace it
    const P = _plugin();
    if(!P) return false;
    _define(P);
    return true;
  }

  function _define(P){

  // Bytes cross the Capacitor bridge as base64: the bridge is JSON, so a Uint8Array would arrive as
  // {"0":12,"1":99,…} — roughly 6x the size and a parse of one object per byte.
  const toBytes = (b64) => {
    const bin = atob(b64 || '');
    const out = new Uint8Array(bin.length);
    for(let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };
  const toB64 = (bytes) => {
    let s = '';
    const u = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
    for(let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  };

  /* HOW MUCH THIS PLATFORM SHOULD MOVE AT ONCE.
   *
   * Every chunk crosses the Capacitor bridge as base64: 4 bytes of string per 3 of data, held as
   * UTF-16 in the WebView, so a 16 MB chunk is ~21 MB of string ≈ 42 MB of memory before the plain
   * bytes, the ciphertext and the Java-side copy are counted. Electron pays none of that — it moves
   * Buffers over IPC — which is why the same size that is comfortable on a desktop reloads a tablet
   * mid-sync. 4 MB keeps the whole round trip inside a WebView's budget.
   *
   * The size is recorded in each manifest entry (`cs`), so a device choosing a different one cannot
   * make an identical file look different to a device that chose another. */
  const CHUNK_BYTES = 4 * 1024 * 1024;

  window.pcFs = {
    chunkBytes: CHUNK_BYTES,
    list: () => P.list().then(r => r.roots || []),
    // {} when the user backed out of the system picker — a cancel is not an error.
    pick: () => P.pick().then(r => (r && r.id) ? r : null),
    forget: (id) => P.forget({ id }),
    scan: (id, opts) => P.scan(Object.assign({ id }, opts || {})),
    read: (id, rel) => P.read({ id, rel }).then(r => toBytes(r.b64)),
    /* Slice I/O — what lets a file bigger than this process can hold move at all. Whole-file read()
     * puts the file in the plugin, again across the bridge as base64, and again in the WebView,
     * which is then asked to encrypt it: a WebView has far less headroom than a desktop and simply
     * died. Their presence is also the signal syncrun checks before choosing the chunked path, so a
     * build without them keeps the old behaviour instead of calling something undefined. */
    readPart: (id, rel, offset, len) =>
      P.readPart({ id, rel, offset: offset || 0, len: len || 0 }).then(r => toBytes(r.b64)),
    writePart: (id, rel, offset, bytes) =>
      P.writePart({ id, rel, offset: offset || 0, b64: toB64(bytes) }),
    writeCommit: (id, rel, mtime) =>
      P.writeCommit({ id, rel, when: mtime || 0 }),
    /* `mtime` is accepted and ignored: SAF has no writable last-modified column, so the provider
     * decides. What comes back is what the file ACTUALLY became, and syncrun.js records that as the
     * agreed state — which is what stops the next sweep reading our own download as a local edit. */
    write: (id, rel, bytes, mtime) => P.write({ id, rel, b64: toB64(bytes), when: mtime || 0 }),
    move: (id, from, to) => P.move({ id, from, to }),
    trash: (id, rel, when) => P.trash({ id, rel, when: when || 0 }).then(r => r.to),
    /* Download verification, and therefore also resume — syncrun skips BOTH when hashPart is
     * absent, so without these three the phone wrote every download unchecked and re-fetched from
     * byte zero after any drop, while the desktop did neither. */
    hashPart: (id, rel) => P.hashPart({ id, rel }).then(r => r && r.sha),
    discardPart: (id, rel) => P.discardPart({ id, rel }).then(() => true),
    partSize: (id, rel) => P.partSize({ id, rel }).then(r => (r && r.size) || 0),
    emptyTrash: (id, days) => P.emptyTrash({ id, days: days === 0 ? 0 : (days || 30) }),
    power: () => P.power(),
    /* Background change DETECTION, not sync — see SyncCheckWorker. It cannot upload because every
     * network step of a sweep is signed by the user's nostr key (a kind-24242 per blob, a 27235 for
     * the manifest, NIP-44 for the manifest body), and with Amber or a remote signer that key is not
     * on the device at all. So the job notices and notifies; opening the app does the sync. */
    backgroundCheck: (enabled, minutes) => P.backgroundCheck({ enabled: !!enabled, minutes: minutes || 180 }),
    markSynced: () => P.markSynced(),
    // No tree watcher exists in SAF worth having, and polling one is the battery bug the sync policy
    // exists to avoid. The app sweeps when it is opened and when the OS says the constraints are met.
    watch: () => Promise.resolve(false),
    unwatch: () => Promise.resolve(false),
    onChanged: () => {},
    /* THE CLOCK, WHICH ON THIS PLATFORM CANNOT LIVE IN JAVASCRIPT.
     *
     * There is no watcher above and there cannot be one, so with the screen off the only automatic
     * trigger left was `setInterval` — and Android throttles timers in a hidden WebView, so a phone
     * with "Stay connected" on kept the process alive and was never asked to sync. StayAwakeService
     * owns the timer now and this is where it arrives; sync.js turns it into the same nudge every
     * other trigger produces, so the battery/Wi-Fi policy still decides whether anything runs.
     *
     * Absent on an APK older than that plugin, which is why sync.js checks for it — there the
     * behaviour is exactly what it is today, not an error.
     *
     * `addListener` is ASYNC — it answers a promise — so a `try/catch` around it sees a failed
     * subscription only when the call throws synchronously, which is not how it fails. The catch is
     * on the promise too, or a rejection lands as an unhandled rejection in the WebView and this
     * still reports success. What is returned is "this bridge has the method", which is the question
     * sync.js actually asks; a listener that fails to attach is silence either way. */
    /* What the PHONE measured about the background clock — alarms armed, alarms that came back,
     * ticks delivered to a live page, ticks dropped into a dead one.
     *
     * ANSWERS null ON A BUILD THAT CANNOT TELL YOU, rather than throwing. The shim is a literal
     * object, so this method exists on every build — a caller probing `if(fs.tickStats)` gets true
     * even against an APK whose plugin has no such method, and would then take a Capacitor
     * rejection for an unimplemented call. null is the honest answer and is NOT zeros: zeros would
     * read as "the clock has never ticked", which is the actual failure this exists to detect. */
    tickStats: () => P.tickStats().catch(() => null),
    /* THE BATTERY/DATA POLICY, PUSHED TO THE NATIVE CLOCK. Without this line the whole pre-filter is
     * dead: `pNeedCharging`/`pNeedUnmetered` keep their false defaults, `suppressed()` returns false
     * at its first statement, and the alarm wakes the WebView every sixteen minutes on cellular
     * exactly as it did before — a feature that ships inert and tests green, because the Java and
     * the caller were both correct and only the wire between them was missing. */
    setTickPolicy: (o) => P.setTickPolicy(o || {}).catch(() => {}),
    /* Keep the CPU up for the duration of a sweep. A foreground service keeps the PROCESS resident
     * and lets the processor sleep, so with the screen off a sweep stops mid-file — measured, 23
     * downloads the minute before and 0 the minute after. Never fatal if absent or refused: the
     * sweep runs exactly as it does today, which is the bug, not a new one. */
    wakeBegin: () => P.sweepBegin().catch(() => {}),
    wakeEnd: () => P.sweepEnd().catch(() => {}),
    /* ---- THE SWEEP THAT RUNS WITHOUT THIS PAGE -------------------------------------------------
     *
     * Chromium throttles a hidden page's JavaScript however awake the processor is, so the tick
     * above is a request the WebView may be in no position to honour. The transfer therefore also
     * exists in Java (NativeSweep) — and everything it needs is something only this page knows, so
     * it is pushed from here on every sweep: the instance, the media server, the pair keys, the
     * exclusions, the switches, and the NIP-44-WRAPPED drive key (never the key itself — the phone
     * unwraps it with the account secret the native signer already holds).
     *
     * All four tolerate an older APK the same way `tickStats` does: the shim is a literal object, so
     * the method always exists and a build whose plugin lacks it rejects rather than throwing. That
     * is caught, and the behaviour falls back to exactly what it is today. */
    configureNative: (o) => P.configure(o || {}).then(() => true).catch(() => false),
    forgetNative: () => P.forgetNative().catch(() => {}),
    nativeReport: () => P.nativeReport().catch(() => null),
    /* ONE SWEEP PER FOLDER ACROSS BOTH ENGINES. Two sweeps writing the same manifest is
     * last-writer-wins on the document that decides whether files exist, and the moment it is most
     * likely is somebody opening the app while the alarm is mid-sweep. A build without the lock
     * answers true — the behaviour it has today — rather than refusing to sync at all. */
    claimSweep: (key) => P.claimSweep({ key }).then(r => !r || r.ok !== false).catch(() => true),
    releaseSweep: (key) => P.releaseSweep({ key }).catch(() => {}),
    onTick: (fn) => {
      try{
        const p = P.addListener('folderSyncTick', () => { try{ fn(); }catch(_){} });
        if(p && typeof p.catch === 'function') p.catch(() => {});
        return true;
      }
      catch(_){ return false; }
    },
  };
  }

  if(!install()){
    // The bridge can arrive after this script. Bounded — ten seconds is far longer than it has ever
    // taken, and after that this really is a platform with no filesystem adapter.
    let n = 0;
    const t = setInterval(() => { if(install() || ++n > 40) clearInterval(t); }, 250);
  }
})();

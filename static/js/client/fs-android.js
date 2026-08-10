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
  const P = (window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.FolderSync) || null;
  if(!P || window.pcFs) return;

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
    emptyTrash: (id, days) => P.emptyTrash({ id, days: days || 30 }),
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
  };
})();

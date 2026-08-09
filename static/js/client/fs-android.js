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

  window.pcFs = {
    list: () => P.list().then(r => r.roots || []),
    // {} when the user backed out of the system picker — a cancel is not an error.
    pick: () => P.pick().then(r => (r && r.id) ? r : null),
    forget: (id) => P.forget({ id }),
    scan: (id, opts) => P.scan(Object.assign({ id }, opts || {})),
    read: (id, rel) => P.read({ id, rel }).then(r => toBytes(r.b64)),
    /* `mtime` is accepted and ignored: SAF has no writable last-modified column, so the provider
     * decides. What comes back is what the file ACTUALLY became, and syncrun.js records that as the
     * agreed state — which is what stops the next sweep reading our own download as a local edit. */
    write: (id, rel, bytes, mtime) => P.write({ id, rel, b64: toB64(bytes), when: mtime || 0 }),
    move: (id, from, to) => P.move({ id, from, to }),
    trash: (id, rel, when) => P.trash({ id, rel, when: when || 0 }).then(r => r.to),
    emptyTrash: (id, days) => P.emptyTrash({ id, days: days || 30 }),
    power: () => P.power(),
    // No tree watcher exists in SAF worth having, and polling one is the battery bug the sync policy
    // exists to avoid. The app sweeps when it is opened and when the OS says the constraints are met.
    watch: () => Promise.resolve(false),
    unwatch: () => Promise.resolve(false),
    onChanged: () => {},
  };
})();

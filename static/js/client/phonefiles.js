/* THE PHONE'S OWN STORAGE, for Files' "This phone" (asked 2026-10-28: "android: no way to browse user's android
 * storage on files").
 *
 * Defines `window.pcPhoneHost` from the PhoneFiles Capacitor plugin (files/PhoneFilesPlugin.java), with the SAME
 * shapes the desktop's `window.pcHost` answers -- so hostfiles.js draws the phone exactly as it draws a computer.
 * It is deliberately NOT `window.pcHost`: across the client that name means "the desktop app" (its notifications,
 * Code's git, the Texts file picker), and claiming it on a phone would switch all of those on.
 *
 * A no-op everywhere but the APK: no plugin, nothing defined, and Files draws no "This phone".
 */
(function(){
  'use strict';

  function _plugin(){
    const cap = window.Capacitor;
    if(!cap) return null;
    const p = cap.Plugins && cap.Plugins.PhoneFiles;
    if(p) return p;
    const native = cap.isNativePlatform ? cap.isNativePlatform() : !!cap.isNative;
    if(!native || !cap.registerPlugin) return null;
    try{ return cap.registerPlugin('PhoneFiles'); }catch(_){ return null; }
  }

  const toBytes = (b64) => {
    const bin = atob(b64 || ''), out = new Uint8Array(bin.length);
    for(let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };
  const toB64 = (bytes) => {
    const u = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
    let s = '';
    for(let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  };
  // The plugin rejects with the reason as its message; keep exactly that, never "[object Object]".
  const why = (e) => { throw new Error(String((e && (e.message || e.errorMessage)) || e || 'could not do that')); };

  function define(P){
    /* WITHOUT ACCESS, A LISTING IS REFUSED -- AND SAYS WHY, with a way to grant it. An unreadable folder must not
     * look like an empty phone. `needsAccess` is what hostfiles.js turns into the "Allow access" button. */
    const noAccess = () => { const e = new Error('PosterChan needs permission to see this phone’s files');
                             e.needsAccess = true; return e; };
    const H = {
      phone: true,
      label: 'This phone',
      access: () => Promise.resolve(P.access()).then(r => !!(r && r.granted), () => false),
      requestAccess: () => Promise.resolve(P.requestAccess()).catch(why),
      /* "Allow access", drawn by Files in place of the listing: one button to the system screen that grants it, and
       * coming back to the app draws the phone. */
      askAccess: (pane, bar, err, redraw, toast) => {
        const esc = (t) => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;');
        pane.innerHTML = bar + `<div class="empty hf-access">${esc(err)}.<div class="muted small">Android asks you to turn on
            “All files access” for PosterChan. Only Files uses it, to show and manage what is on this phone.</div>
            <div><button class="btn btn-cyan small hf-grant">Allow access</button></div></div>`;
        const g = pane.querySelector('.hf-grant');
        if(g) g.onclick = async () => {
          try{ await H.requestAccess(); }catch(e){ if(toast) toast(String((e && e.message) || e)); return; }
          const back = () => { if(document.visibilityState !== 'visible') return;
            document.removeEventListener('visibilitychange', back); if(pane.isConnected) redraw(); };
          document.addEventListener('visibilitychange', back);
        };
      },
      roots: () => Promise.resolve(P.roots()).then(r => (r && r.roots) || [], () => []),
      list: async (dir) => {
        if(!(await H.access())) throw noAccess();
        return Promise.resolve(P.list({ dir: String(dir || '') })).catch(why);
      },
      read: (p, max) => Promise.resolve(P.read({ path: String(p || ''), max: Number(max) || 0 }))
        .then(r => toBytes(r && r.b64), why),
      /* For PosterChan Code: the desktop's {path, text, size, mtime}. Refused for a binary file rather than shown as
       * mojibake that a save would then write back over the original. */
      readText: (p) => Promise.resolve(P.read({ path: String(p || ''), max: 8 * 1024 * 1024 })).then(r => {
        let text;
        try{ text = new TextDecoder('utf-8', { fatal: true }).decode(toBytes(r && r.b64)); }
        catch(_){ throw new Error('that is not a text file'); }
        return { path: r.path, text, size: r.size, mtime: r.mtime };
      }, why),
      writeText: (p, text, mtime) => H.writeBytes(p, new TextEncoder().encode(String(text == null ? '' : text)), mtime),
      // Streamed by the WebView from the file itself (Capacitor's local file route), never read into the heap.
      fileUrl: (p) => {
        const cap = window.Capacitor;
        return cap && cap.convertFileSrc ? cap.convertFileSrc('file://' + String(p || '')) : '';
      },
      mkdir: (dir, name) => Promise.resolve(P.mkdir({ dir: String(dir || ''), name: String(name || '') })).catch(why),
      rename: (from, to) => Promise.resolve(P.rename({ from: String(from || ''), to: String(to || '') })).catch(why),
      trash: (target) => Promise.resolve(P.trash({ target: String(target || '') })).catch(why),
      transfer: (items, destination, move) => Promise.resolve(P.transfer({
        items: (items || []).map(String), destination: String(destination || ''), move: !!move })).catch(why),
      writeBytes: (p, bytes, mtime) => Promise.resolve(P.writeBytes({
        path: String(p || ''), b64: toB64(bytes), mtime: Number(mtime) || 0 })).catch(why),
      open: (p) => Promise.resolve(P.open({ path: String(p || '') }))
        .then(r => r || { ok: false, why: 'could not open it' }, e => ({ ok: false, why: String((e && e.message) || e) })),
    };
    return H;
  }

  /* RESOLVED WHEN FIRST ASKED, not at load: the native bridge may not have injected its plugins when this script
   * runs (fs-android.js's startup race), and Files asks only when it draws. Nothing plugin-less ever gets an object. */
  let H = null;
  Object.defineProperty(window, 'pcPhoneHost', { configurable: true, get(){
    if(H) return H;
    const P = _plugin();
    if(P) H = define(P);
    return H || undefined;
  } });
})();

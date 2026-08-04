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

  contextBridge.exposeInMainWorld('pcShell', {
    instanceSync,
    getInstance: () => ipcRenderer.invoke('pc:instance:get'),
    setInstance: (url) => ipcRenderer.invoke('pc:instance:set', url == null ? '' : url),
    retry: () => ipcRenderer.send('pc:retry'),
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
  // Screen-share source picker (picker.html). Kept separate so the capability stays scoped to the page
  // that draws the picker; the instance must never be able to enumerate the user's windows.
  contextBridge.exposeInMainWorld('pcScreen', {
    list: () => ipcRenderer.invoke('pc:screen:list'),
    pick: (id) => ipcRenderer.send('pc:screen:pick', String(id)),
  });
}

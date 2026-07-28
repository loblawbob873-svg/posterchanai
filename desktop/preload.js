/* Bridge for the bundled shell page ONLY (offline notice + instance picker).
 * The main window keeps this preload for every navigation, so it must hand the API out to the local
 * file:// page and to nothing else — otherwise the loaded instance (or anything it embeds) could
 * repoint the app at another server. */
const { contextBridge, ipcRenderer } = require('electron');

// Clipboard WRITE, exposed to the loaded instance as well as the shell page — unlike the controls below
// it cannot repoint the app or enumerate anything, and the main process still checks the caller is the
// instance origin. It exists because this shell has no working web clipboard: navigator.clipboard is
// absent over plain http (not a secure context) and execCommand('copy') is refused, which left the
// Go Live stream key impossible to copy. Write-only — the page can never read the user's clipboard.
contextBridge.exposeInMainWorld('pcClip', {
  write: (s) => ipcRenderer.invoke('pc:clip:write', String(s == null ? '' : s)),
});

if (location.protocol === 'file:') {
  contextBridge.exposeInMainWorld('pcShell', {
    getInstance: () => ipcRenderer.invoke('pc:instance:get'),
    setInstance: (url) => ipcRenderer.invoke('pc:instance:set', url),
    retry: () => ipcRenderer.send('pc:retry'),
  });
  // Screen-share source picker (picker.html). Same file:-only rule: the instance must never be able to
  // enumerate the user's windows or start a capture by itself.
  contextBridge.exposeInMainWorld('pcScreen', {
    list: () => ipcRenderer.invoke('pc:screen:list'),
    pick: (id) => ipcRenderer.send('pc:screen:pick', String(id)),
  });
}

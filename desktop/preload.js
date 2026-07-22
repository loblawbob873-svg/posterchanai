/* Bridge for the bundled shell page ONLY (offline notice + instance picker).
 * The main window keeps this preload for every navigation, so it must hand the API out to the local
 * file:// page and to nothing else — otherwise the loaded instance (or anything it embeds) could
 * repoint the app at another server. */
const { contextBridge, ipcRenderer } = require('electron');

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

'use strict';
/* The only thing the desktop PosterChan's own window (buddy.html) can do: move herself while being
 * dragged, report where she was dropped, and ask to be hidden. No other bridge reaches this page. */
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('pcBuddyWin', {
  drag: (dx, dy) => ipcRenderer.send('pc:buddy:drag', Number(dx) || 0, Number(dy) || 0),
  drop: () => ipcRenderer.send('pc:buddy:drop'),
  menu: (action) => ipcRenderer.send('pc:buddy:menu', String(action || '')),
});

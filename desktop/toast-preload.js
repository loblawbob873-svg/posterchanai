'use strict';
/* The only things the notification-card window (toast.html) can do: receive its cards, and say which
 * one was clicked or dismissed. No other bridge reaches this page. */
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('pcToastWin', {
  onCards: (fn) => { ipcRenderer.on('pc:toast:cards', (_e, cards) => { try{ fn(Array.isArray(cards) ? cards : []); }catch(_){ } }); },
  click: (id) => ipcRenderer.send('pc:toast:click', String(id || ''), false),
  dismiss: (id) => ipcRenderer.send('pc:toast:click', String(id || ''), true),
});

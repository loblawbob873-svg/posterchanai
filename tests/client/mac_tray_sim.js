#!/usr/bin/env node
'use strict';

/* Execute the shipped helper itself. This guards the structural invariant CSS cannot prove: macOS
 * status controls are siblings of the Dock, while PosterChan mode puts the same node back inside
 * the taskbar. */
const fs = require('fs');
const src = fs.readFileSync('static/js/client/os.js', 'utf8');
const start = src.indexOf('function placeDesktopTray()');
if (start < 0) throw new Error('placeDesktopTray is missing');
let depth = 0, end = -1, opened = false;
for (let i = src.indexOf('{', start); i < src.length; i++) {
  if (src[i] === '{') { depth++; opened = true; }
  if (src[i] === '}' && opened && --depth === 0) { end = i + 1; break; }
}
if (end < 0) throw new Error('placeDesktopTray is incomplete');

const tray = { parentElement: null };
const bar = { appendChild(x) { x.parentElement = this; } };
const root = {
  mac: true,
  classList: { contains(x) { return x === 'os-style-mac' && root.mac; } },
  appendChild(x) { x.parentElement = this; }
};
const $ = (_selector, host) => host === tray.parentElement ? tray : null;
tray.parentElement = bar;
const placeDesktopTray = eval('(' + src.slice(start, end) + ')');

placeDesktopTray();
if (tray.parentElement !== root) throw new Error('macOS tray remained inside the Dock');
root.mac = false;
placeDesktopTray();
if (tray.parentElement !== bar) throw new Error('PosterChan tray did not return to the taskbar');
console.log('OK  macOS tray is structurally separate from the Dock');

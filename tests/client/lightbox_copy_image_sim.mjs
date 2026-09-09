/* Execute the SHIPPED _lbCopyImg against stubbed shells. Lifted from app.js rather than retyped, so
   the path under test cannot drift from the one that runs.

   The bug: on the PosterChanOS desktop this had never worked (app:// refuses
   navigator.clipboard.write, and the native bridge was text-only), and every attempt ended in one
   toast telling a mouse user to long-press the image. */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const a = src.indexOf('  /* WHAT TO TELL SOMEBODY WHOSE IMAGE COPY DID NOT HAPPEN.');
if (a < 0) throw new Error('_lbCopyImgFail moved');
const b = src.indexOf('\n  // Save the media in the lightbox.', a);
if (b < 0) throw new Error('_lbCopyImg end marker moved');
const shipped = src.slice(a, b);

/* The ClipboardItem is handed a PENDING promise on purpose (see _lbCopyImg), so when a scenario
   makes the web path fail before clipboard.write() consumes it, that promise rejects with nobody
   listening. A browser logs it; Node kills the process, which would turn a readable wrong answer
   into a crash. */
process.on('unhandledRejection', () => {});

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);
const pngBlob = { type: 'image/png', arrayBuffer: async () => PNG.buffer.slice(0) };

let toasts = [];
globalThis.toast = (m) => { toasts.push(String(m)); };
globalThis._aiToken = '';
globalThis._isNativeApp = () => false;
globalThis._nativeShareMedia = async () => false;
globalThis._blobToPng = async () => pngBlob;
globalThis.fetchMediaBlob = async () => ({ blob: pngBlob, disp: '' });
// Node has a REAL fetch. Left unstubbed, a desktop scenario that wrongly falls through to the web
// path would go out to the network and die as an ENOTFOUND crash instead of a readable failure.
globalThis.fetch = async () => { throw new Error('unexpected web fetch'); };

const setNavigator = (v) => Object.defineProperty(globalThis, 'navigator',
  { value: v, configurable: true, writable: true });

const api = new Function(`${shipped}; return {_lbCopyImg,_lbCopyImgFail};`)();
const out = {};

// A) The desktop shell: the native bridge is the only path that can work there.
let sent = null;
toasts = [];
globalThis.window = { pcClip: { writeImage: (buf) => { sent = new Uint8Array(buf); return Promise.resolve(true); } } };
setNavigator({ maxTouchPoints: 0 });
await api._lbCopyImg('https://blossom.example/abc');
out.desktopToast = toasts.join('|');
out.desktopBytes = sent ? Array.from(sent) : null;

// B) The bridge answered false: wl-copy's own exit status said the selection was not taken.
toasts = [];
globalThis.window = { pcClip: { writeImage: () => Promise.resolve(false) } };
await api._lbCopyImg('https://blossom.example/abc');
out.refusedToast = toasts.join('|');

// C) The image itself could not be fetched — the status belongs in the message.
toasts = [];
globalThis.fetchMediaBlob = async () => { throw new Error('HTTP 403'); };
globalThis.window = { pcClip: { writeImage: () => Promise.resolve(true) } };
await api._lbCopyImg('https://blossom.example/abc');
out.desktopFetchToast = toasts.join('|');
globalThis.fetchMediaBlob = async () => ({ blob: pngBlob, disp: '' });

// D) The bridge WINS over a present web clipboard: on the desktop, Chromium's cache is not the
//    compositor selection, so falling back to it would copy into PosterChan and nowhere else.
toasts = [];
let webUsed = false;
function WatchedClipboardItem(map) { webUsed = true; this.map = map; }
globalThis.window = {
  pcClip: { writeImage: () => Promise.resolve(true) },
  ClipboardItem: WatchedClipboardItem,
};
globalThis.ClipboardItem = WatchedClipboardItem;   // the code constructs the BARE global, not window.*
setNavigator({ maxTouchPoints: 0, clipboard: { write: async () => { webUsed = true; } } });
await api._lbCopyImg('https://blossom.example/abc');
out.bridgePreferred = !webUsed;
out.bridgePreferredToast = toasts.join('|');

// E) No bridge and no web clipboard: a missing capability, on a machine with a mouse.
toasts = [];
globalThis.window = {};
setNavigator({ maxTouchPoints: 0 });
await api._lbCopyImg('https://blossom.example/abc');
out.mouseUnavailable = toasts.join('|');

// F) The same, on a surface that really does have touch.
toasts = [];
globalThis.window = { ontouchstart: null };
setNavigator({ maxTouchPoints: 5 });
await api._lbCopyImg('https://blossom.example/abc');
out.touchUnavailable = toasts.join('|');

// G) The browser path still works, and still hands ClipboardItem a PROMISE synchronously.
toasts = [];
let item = null, sync = true;
function ClipboardItem(map) { this.map = map; item = map; }
globalThis.window = { ClipboardItem };
globalThis.ClipboardItem = ClipboardItem;
setNavigator({
  maxTouchPoints: 0,
  clipboard: { write: async (items) => { if (!item) sync = false; await items[0].map['image/png']; } },
});
globalThis.fetch = async () => ({ ok: true, blob: async () => pngBlob });
await api._lbCopyImg('https://example.test/x.png');
out.webToast = toasts.join('|');
out.webHandedAPromise = !!(item && item['image/png'] && typeof item['image/png'].then === 'function');
out.webSynchronous = sync;

// H) The browser path with a 404 must not be reported as a refused clipboard.
toasts = [];
globalThis.fetch = async () => ({ ok: false, status: 404 });
await api._lbCopyImg('https://example.test/gone.png');
out.webFetchToast = toasts.join('|');

// I) A refused clipboard PERMISSION, with the fetch fine, is the other half of the same catch.
toasts = [];
globalThis.fetch = async () => ({ ok: true, blob: async () => pngBlob });
setNavigator({
  maxTouchPoints: 0,
  clipboard: { write: async () => { throw new Error('NotAllowedError'); } },
});
await api._lbCopyImg('https://example.test/x.png');
out.webRefusedToast = toasts.join('|');

process.stdout.write(JSON.stringify(out));

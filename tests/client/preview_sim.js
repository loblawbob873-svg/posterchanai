/* Preview (static/js/client/preview.js) run under node against a stub DOM.
 *
 * "basiucally i want the preview to app to handle videos, images when people click on them from
 * blossom" - and before that, PDFs. Blossom's only answer for any of those was "open in a new tab",
 * which on the encrypted drive means decrypting to a blob URL and handing it to the browser: you
 * leave the app and lose the folder you were in, and on the APK it does nothing useful at all.
 *
 * The module is the SHIPPED file, required directly. What is stubbed is the browser.
 */
'use strict';
const path = require('path');
const PREVIEW = process.env.PC_INSTALLED_PREVIEW_JS ||
  path.resolve(__dirname, '../../static/js/client/preview.js');

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  ok   ' + name); return; }
  failures++; console.log('  FAIL ' + name + (detail ? '  - ' + detail : ''));
}

/* --- the smallest DOM this module touches ------------------------------------------------- */
function El(tag) {
  const e = {
    tagName: (tag || 'div').toUpperCase(), children: [], style: {}, dataset: {},
    _cls: new Set(), _html: '', src: '', textContent: '', attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    remove() { const i = this.parent ? this.parent.children.indexOf(this) : -1;
               if (i >= 0) this.parent.children.splice(i, 1); },
    load() {}, pause() { this.paused = true; }, play() { this.paused = false; return Promise.resolve(); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this._q = null; },
    querySelector(sel) { return matchIn(this, sel); },
    querySelectorAll() { return []; },
  };
  e.classList = {
    add(...cs) { cs.forEach(c => e._cls.add(c)); },
    remove(...cs) { cs.forEach(c => e._cls.delete(c)); },
    toggle(c, on) { on ? e._cls.add(c) : e._cls.delete(c); },
    contains(c) { return e._cls.has(c); },
  };
  return e;
}
// The module only ever queries the markup IT just wrote, so matching on the class token in the
// stored HTML is enough and keeps this stub honest about what it can answer.
function matchIn(el, sel) {
  const cls = String(sel).replace(/^[.#]/, '').split(',')[0].trim().replace(/^\./, '');
  if (!el._html || el._html.indexOf(cls) < 0) return null;
  if (!el._q) el._q = {};
  if (!el._q[cls]) el._q[cls] = El(cls.indexOf('vid') >= 0 ? 'video' : 'div');
  return el._q[cls];
}
function install() {
  const body = El('body');
  global.document = {
    body,
    createElement: (t) => { const e = El(t); e.parent = body; return e; },
    querySelector: () => null,
  };
  const listeners = {};
  global.window = {
    __PC: { toast: () => {}, saveBlobAs: async () => {} },
    addEventListener: (k, f) => { (listeners[k] = listeners[k] || []).push(f); },
    removeEventListener: (k, f) => { listeners[k] = (listeners[k] || []).filter(x => x !== f); },
    _listeners: listeners,
  };
  // node 21+ defines a real `navigator` with only a getter, so it has to be REPLACED, not assigned.
  Object.defineProperty(global, 'navigator', { value: {}, configurable: true, writable: true });
  global.URL = { _live: 0, _lastType: '', createObjectURL(blob) { this._live++; this._lastType = blob.type; return 'blob:x' + this._live; },
                 revokeObjectURL() { this._live--; } };
  global.Blob = class { constructor(parts, opts) { this.type = (opts && opts.type) || '';
                                                   this.size = 10; } };
  global.fetch = async () => ({ blob: async () => new global.Blob([]) });
  delete require.cache[require.resolve(PREVIEW)];
  require(PREVIEW);
  return global.window.PCPreview;
}

const P = install();

console.log('what it offers to open');
check('a photograph', P.handles('holiday.jpg', ''));
check('a photograph by mime alone', P.handles('IMG_0042', 'image/jpeg'));
check('a video', P.handles('clip.mp4', ''));
check('a video by mime alone', P.handles('recording', 'video/webm'));
check('a phone video (.mov)', P.handles('IMG_1.MOV', ''));
check('a pdf', P.handles('statement.pdf', ''));
check('audio', P.handles('song.mp3', ''));
check('NOT a spreadsheet', !P.handles('books.xlsx', ''));
check('NOT a text file', !P.handles('notes.md', ''));
check('NOT an unknown binary', !P.handles('firmware.bin', 'application/octet-stream'));

console.log('the kind decides what is drawn');
check('image', P.kindOf('a.png', '') === 'image');
check('video', P.kindOf('a.mkv', '') === 'video');
check('pdf', P.kindOf('a.pdf', '') === 'pdf');
check('audio', P.kindOf('a.flac', '') === 'audio');

console.log('opening one');
{
  const ok = P.open({ name: 'holiday.jpg', mime: 'image/jpeg', blob: new global.Blob([]) });
  check('it opens', ok === true);
  check('and reports it is open', P.isOpen() === true);
  check('a blob url was taken', global.URL._live === 1);
  P.close();
  check('closing releases the blob url', global.URL._live === 0,
        'a viewer that leaks one per file holds every picture you looked at');
  check('and it is no longer open', P.isOpen() === false);
}

console.log('a file it cannot show is refused, not half-drawn');
check('returns false', P.open({ name: 'books.xlsx', mime: '', blob: new global.Blob([]) }) === false);
check('and nothing was opened', P.isOpen() === false);

console.log('only one at a time');
{
  P.open({ name: 'a.jpg', mime: 'image/jpeg', blob: new global.Blob([]) });
  P.open({ name: 'b.jpg', mime: 'image/jpeg', blob: new global.Blob([]) });
  check('the first was closed by the second', global.URL._live === 1,
        global.URL._live + ' blob urls are live');
  P.close();
}

console.log('the desktop uses its full document workspace');
{
  const calls = { open: 0, document: 0, close: 0, noFeed: false };
  const slot = El('div');
  window.PCOS = {
    isOn: () => true,
    openDoc(key, name, icon, render, noFeed) {
      calls.open++; calls.noFeed = noFeed === true;
      return { slot };
    },
    documentWindow(w) { calls.document++; w.documentWorkspace = true; },
    closeDoc() { calls.close++; },
  };
  check('desktop preview opens',
        P.open({ name: 'desktop.pdf', mime: 'application/pdf', blob: new global.Blob([]) }) === true);
  check('it owns a no-feed window', calls.open === 1 && calls.noFeed);
  check('it requests the maximised neutral document workspace', calls.document === 1);
  check('the media is mounted in the window slot', slot.classList.contains('pv-win'));
  P.close();
  check('closing uses the desktop window lifecycle', calls.close === 1);
  delete window.PCOS;
}

console.log('a platform with no PDF viewer uses the bundled renderer');
{
  navigator.pdfViewerEnabled = false;
  P.open({ name: 'a.pdf', mime: 'application/pdf', blob: new global.Blob([]) });
  const host = global.document.body.children[0];
  check('it opens', P.isOpen() === true);
  check('and mounts pdf.js pages instead of a browser PDF iframe',
        /pv-pdf-pages/.test(host._html) && !/<iframe/.test(host._html));
  P.close();
  delete navigator.pdfViewerEnabled;
}

console.log('a video is playsinline and preloads only metadata');
{
  P.open({ name: 'clip.mp4', mime: 'video/mp4', blob: new global.Blob([]) });
  const host = global.document.body.children[0];
  check('playsinline', /playsinline/.test(host._html),
        'without it iOS goes full screen on play and throws away the window');
  check('preload=metadata', /preload="metadata"/.test(host._html));
  check('controls', /controls/.test(host._html));
  P.close();
}

console.log('an old Blossom MP4 returned as generic binary is playable');
{
  P.open({ name: 'camera.mp4', mime: 'application/octet-stream',
           blob: new global.Blob([], { type: 'application/octet-stream' }) });
  check('generic MP4 is rebuilt with video/mp4', global.URL._lastType === 'video/mp4',
        'object URL type was ' + global.URL._lastType);
  const host = global.document.body.children[0];
  check('loading is visible instead of a silent black rectangle', /Loading video/.test(host._html));
  P.close();
}

console.log('a desktop monitor handoff transfers the live blob and playback before source cleanup');
(async()=>{
  let sourceWindow=null, destinationWindow=null, opens=0;
  const slot=()=>El('div');
  window.PCOS={isOn:()=>true,openDoc(){const w={slot:slot()};if(!opens++)sourceWindow=w;else destinationWindow=w;return w;},
    documentWindow(){},closeDoc(){}};
  P.open({name:'handoff.mp4',mime:'video/mp4',blob:new global.Blob([],{type:'video/mp4'})});
  const sourceVideo=sourceWindow.slot.querySelector('.pv-vid');
  sourceVideo.currentTime=37.5;sourceVideo.paused=false;sourceVideo.volume=.4;
  sourceVideo.muted=true;sourceVideo.playbackRate=1.5;
  const state=sourceWindow.handoffState();
  P.close();
  check('source close preserves the transferring blob URL',global.URL._live===1);
  check('handoff payload names Preview and its blob URL',state.preview===true&&/^blob:/.test(state.url));
  check('destination reconstructs Preview from transferred bytes',await P.acceptHandoff(state)===true);
  check('destination owns a real Preview document',!!destinationWindow&&destinationWindow.slot.classList.contains('pv-win'));
  const destinationVideo=destinationWindow.slot.querySelector('.pv-vid');
  check('destination preserves video time and playing state',destinationVideo.currentTime===37.5&&!destinationVideo.paused);
  check('destination preserves video audio and rate',destinationVideo.volume===.4&&destinationVideo.muted&&destinationVideo.playbackRate===1.5);
  check('old transfer URL was replaced, not leaked',global.URL._live===1);
  P.close();delete window.PCOS;
  console.log(failures ? '\nFAILED ' + failures : '\nOK  preview holds');
  process.exitCode=failures?1:0;
})().catch(e=>{console.error(e&&e.stack||e);process.exitCode=1;});

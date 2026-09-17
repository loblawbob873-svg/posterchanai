/* PDF preview on the phone: a page that fails mid-document keeps the pages already rendered, a PDF
 * too large for the native hand-off says so and is saved, and a small one goes to the native viewer.
 * Runs the SHIPPED static/js/client/preview.js under node with a stub DOM and a stub pdf.js. */
'use strict';
const path = require('path');
const PREVIEW = process.env.PC_INSTALLED_PREVIEW_JS || path.resolve(__dirname, '../../static/js/client/preview.js');

let failures = 0;
const check = (name, ok, detail) => { if (ok) console.log('  ok   ' + name); else { failures++; console.log('  FAIL ' + name + (detail ? ' - ' + detail : '')); } };

function El(tag) {
  const e = { tagName: tag, children: [], style: {}, attrs: {}, _html: '', clientWidth: 800,
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); return c; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; this._btn = null; },
    querySelector(sel) {
      if (sel === '.pv-pdf-native' && /pv-pdf-native/.test(this._html)) return this._btn || (this._btn = El('button'));
      if (sel === '.pv-pdf-pages') return this._pages || null;
      return null;
    },
    getContext() { return {}; } };
  return e;
}

const saved = [], opened = [], toasts = [];
let nativeAvailable = true;
global.document = { createElement: El, head: El('head'), documentElement: El('html') };
global.window = {
  devicePixelRatio: 1,
  __PC: {
    toast: m => toasts.push(m),
    saveBlobAs: async (b, n) => { saved.push(n); },
    capPlugin: () => nativeAvailable ? { open: async o => { opened.push(o.name); return { ok: true }; } } : null,
  },
  addEventListener() {}, removeEventListener() {},
};
Object.defineProperty(global, 'navigator', { value: {}, configurable: true, writable: true });

function blob(size) { return { size, type: 'application/pdf', arrayBuffer: async () => new Uint8Array(Math.min(size, 16)).buffer }; }

// pdf.js stub: three pages, page 3 throws while rendering.
window.pdfjsLib = {
  GlobalWorkerOptions: {},
  getDocument: () => ({ promise: Promise.resolve({
    numPages: 3, destroy() {},
    getPage: async n => ({
      getViewport: ({ scale }) => ({ width: 600 * scale, height: 800 * scale }),
      render: () => ({ promise: n === 3 ? Promise.reject(new Error('bad xref on page 3')) : Promise.resolve() }),
    }),
  }) }),
};
global.root = window;
delete require.cache[require.resolve(PREVIEW)];
require(PREVIEW);
const P = window.PCPreview;

(async () => {
  // 1. mid-document failure keeps rendered pages
  const host = El('div'), pages = El('div');
  host._pages = pages;
  P._renderPdf(host, blob(1000), 'report.pdf');
  await new Promise(r => setTimeout(r, 50));
  const canvases = pages.children.filter(c => c.tagName === 'canvas');
  check('pages 1-2 stay on screen after page 3 fails', canvases.length === 3 && canvases.slice(0, 2).every(c => c.width > 0),
        'children=' + pages.children.map(c => c.tagName).join(','));
  const note = pages.children.find(c => c.tagName === 'div');
  check('the failure is appended, naming the page', !!note && /rest of this PDF \(page 3\)/.test(note._html), note && note._html);
  check('and still offers the native way out', !!note && /pv-pdf-native/.test(note._html));

  // 2. first-page failure replaces the spinner as before
  window.pdfjsLib.getDocument = () => ({ promise: Promise.reject(new Error('not a PDF')) });
  const host2 = El('div'), pages2 = El('div'); host2._pages = pages2; pages2._html = '<div class="spinner"></div>';
  P._renderPdf(host2, blob(10), 'broken.pdf');
  await new Promise(r => setTimeout(r, 30));
  check('an unrenderable PDF shows the fallback', /Could not render this PDF\./.test(pages2._html) && /pv-pdf-native/.test(pages2._html));

  // 3. native hand-off: small goes to the viewer, big is saved and explained
  await P._openElsewhere(blob(1024), 'small.pdf');
  check('a small PDF opens in the native viewer', opened.includes('small.pdf') && !saved.includes('small.pdf'));
  await P._openElsewhere(blob(40 * 1024 * 1024), 'huge.pdf');
  check('an oversized PDF is not pushed through the bridge', !opened.includes('huge.pdf'));
  check('it is saved instead', saved.includes('huge.pdf'));
  check('and the user is told why', toasts.some(t => /over 32 MB/.test(t)), JSON.stringify(toasts));

  // 4. the "Open in app" action exists only where a native viewer can be asked
  const src = require('fs').readFileSync(PREVIEW, 'utf8');
  check('the toolbar offers Open in app for PDFs when the plugin exists',
        /kind === 'pdf' && nativeOpen\(\) \? '<button class="btn btn-ghost small pv-open">Open in app<\/button>'/.test(src));

  if (failures) { console.log(failures + ' FAILED'); process.exit(1); }
  console.log('pdf native preview holds');
})().catch(e => { console.log('FAIL ' + (e && e.stack || e)); process.exit(1); });

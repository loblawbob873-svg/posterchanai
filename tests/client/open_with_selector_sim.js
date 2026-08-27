/* Executes the shipped Open With routing, not a copy of its regexes.
 * Run: node tests/client/open_with_selector_sim.js */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '../..');
// The ordinary suite exercises the worktree. Installed-package gates point this at app.js extracted
// from /opt/posterchan/resources/app.asar, so a green source test cannot hide a stale Gentoo payload.
const app = fs.readFileSync(process.env.PC_INSTALLED_APP_JS ||
  path.join(root, 'static/js/client/app.js'), 'utf8');

function statement(head) {
  const i = app.indexOf(head);
  if (i < 0) throw new Error('missing ' + head);
  const j = app.indexOf(';', i);
  if (j < 0) throw new Error('unterminated ' + head);
  return app.slice(i, j + 1);
}
function fn(head) {
  const i = app.indexOf(head), begin = app.indexOf('{', i);
  if (i < 0 || begin < 0) throw new Error('missing ' + head);
  let depth = 0, quote = '', escaped = false;
  for (let p = begin; p < app.length; p++) {
    const c = app[p];
    if (quote) {
      if (escaped) escaped = false;
      else if (c === '\\') escaped = true;
      else if (c === quote) quote = '';
      continue;
    }
    if (c === "'" || c === '"' || c === '`') { quote = c; continue; }
    if (c === '{') depth++;
    if (c === '}' && --depth === 0) return app.slice(i, p + 1);
  }
  throw new Error('unterminated ' + head);
}

let sheet = null, closed = 0, ran = [];
const context = {
  CFG: { office_enabled: true }, location: { href: 'https://poster.place/' }, URL,
  enc: s => String(s), toast() {},
  openPreviewFile() {}, openOfficeFile() {}, openSyncOfficeFile() {},
  openCodeFile() {}, openSyncCodeFile() {},
  modal(html, mount) { sheet = {html, mount}; },
  closeModal() { closed++; },
};
vm.createContext(context);
vm.runInContext([
  statement('const _OFFICE_EXT ='), statement('const _officeable ='),
  statement('const _CODE_EXT ='), statement('const _CODE_BARE ='), statement('const _codeable ='),
  statement('const _PREVIEW_EXT ='), statement('const _previewable ='),
  fn('function _openFileName('), fn('function _handlersFor('),
  fn('function _openWithSheet('),
  'globalThis.route = d => _handlersFor(d, {}).map(x => x.id);',
  'globalThis.openSheet = (name, hs) => _openWithSheet(name, hs);',
].join('\n'), context);

function same(got, want, label) {
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error(label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want));
}
// No PCPreview global exists: this exercises the fresh-session regression.
same(context.route({ name:'manual.pdf', mime:'application/pdf', url:'https://b/x' }),
     ['preview', 'office', 'code'], 'cold-start PDF');
same(context.route({ name:'server.conf', mime:'application/octet-stream', url:'https://b/x' }),
     ['code'], '.conf by indexed name');
same(context.route({ name:'sheet.csv', mime:'text/csv', url:'https://b/x' }),
     ['office', 'code'], '.csv offers spreadsheet and raw-text editors');
for (const [name, mime] of [
  ['photo.jpg', 'image/jpeg'], ['diagram.svg', 'image/svg+xml'],
  ['movie.mp4', 'video/mp4'], ['recording.ogg', 'audio/ogg'],
]) same(context.route({name, mime, url:'https://b/x'}), ['preview', 'code'], name);
for (const [name, mime] of [
  ['letter.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
  ['book.ods', 'application/vnd.oasis.opendocument.spreadsheet'],
  ['slides.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'],
]) same(context.route({name, mime, url:'https://b/x'}), ['office', 'code'], name);
same(context.route({ name:'', mime:'application/octet-stream', url:'https://b/f/nginx%2Econf' }),
     ['code'], '.conf by encoded Blossom URL');
same(context.route({ name:'firmware.bin', mime:'application/octet-stream', url:'https://b/x' }),
     ['code'], 'unknown binary can still be inspected');
same(context.route({ name:'README', mime:'application/octet-stream', url:'https://b/x' }),
     ['code'], 'extensionless project file can still be inspected');

// Exercise the shipped chooser lifecycle. Cancel must only remove the in-app overlay: no handler,
// route, native dialog, or desktop mode transition is involved. Choosing closes first, so a
// Preview/Office sheet cannot be opened underneath the chooser in the shared modal root.
const fake = (id, run) => ({id, icon:'x', label:id, hint:'hint', run});
context.openSheet('manual.pdf', [fake('preview',()=>ran.push(['preview',closed])),
  fake('office',()=>ran.push(['office',closed]))]);
if (!sheet || !sheet.html.includes('id="ow-x"')) throw new Error('chooser did not render a close control');
let xClick = null, optClick = null;
const closeButton = {set onclick(v){xClick=v;}};
const option = {dataset:{ow:'office'}, set onclick(v){optClick=v;}};
context.$ = (sel) => sel==='#ow-x' ? closeButton : null;
context.$$ = (sel) => sel==='.ow-opt' ? [option] : [];
sheet.mount({});
xClick();
if (closed!==1 || ran.length) throw new Error('cancel launched a handler or did not close cleanly');
context.openSheet('manual.pdf', [fake('preview',()=>ran.push(['preview',closed])),
  fake('office',()=>ran.push(['office',closed]))]);
sheet.mount({}); optClick();
if (closed!==2 || JSON.stringify(ran)!==JSON.stringify([['office',2]]))
  throw new Error('chooser did not close before launching the selected handler');
console.log('open-with selector holds');

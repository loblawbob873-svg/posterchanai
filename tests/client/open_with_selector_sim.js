/* Executes the shipped Open With routing, not a copy of its regexes.
 * Run: node tests/client/open_with_selector_sim.js */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '../..');
const app = fs.readFileSync(path.join(root, 'static/js/client/app.js'), 'utf8');

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

const context = {
  CFG: { office_enabled: true }, location: { href: 'https://poster.place/' }, URL,
  openPreviewFile() {}, openOfficeFile() {}, openSyncOfficeFile() {},
  openCodeFile() {}, openSyncCodeFile() {},
};
vm.createContext(context);
vm.runInContext([
  statement('const _OFFICE_EXT ='), statement('const _officeable ='),
  statement('const _CODE_EXT ='), statement('const _CODE_BARE ='), statement('const _codeable ='),
  statement('const _PREVIEW_EXT ='), statement('const _previewable ='),
  fn('function _openFileName('), fn('function _handlersFor('),
  'globalThis.route = d => _handlersFor(d, {}).map(x => x.id);',
].join('\n'), context);

function same(got, want, label) {
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error(label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want));
}
// No PCPreview global exists: this exercises the fresh-session regression.
same(context.route({ name:'manual.pdf', mime:'application/pdf', url:'https://b/x' }),
     ['preview', 'office'], 'cold-start PDF');
same(context.route({ name:'server.conf', mime:'application/octet-stream', url:'https://b/x' }),
     ['code'], '.conf by indexed name');
same(context.route({ name:'', mime:'application/octet-stream', url:'https://b/f/nginx%2Econf' }),
     ['code'], '.conf by encoded Blossom URL');
console.log('open-with selector holds');

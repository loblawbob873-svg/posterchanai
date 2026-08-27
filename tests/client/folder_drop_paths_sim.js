/* Execute the shipped Files directory walker and folder router together.
 * Installed-package gates point PC_INSTALLED_APP_JS at app.js extracted from app.asar, so a source
 * fix cannot make a stale Gentoo package appear green. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '../..');
const app = fs.readFileSync(process.env.PC_INSTALLED_APP_JS ||
  path.join(root, 'static/js/client/app.js'), 'utf8');

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

const context = {};
vm.createContext(context);
vm.runInContext(fn('async function _walkEntries(') + '\n' +
  fn('function _uploadTargetFolder(') + '\n' +
  'globalThis.walk=_walkEntries;globalThis.target=_uploadTargetFolder;', context);
const file = p => ({isFile:true, fullPath:p, file(ok) {
  ok({name:p.split('/').pop(), webkitRelativePath:''});
}});
const dir = (p, children) => ({isDirectory:true, fullPath:p, createReader() {
  let sent = false;
  return {readEntries(ok) { if (sent) ok([]); else { sent = true; ok(children); } }};
}});

(async () => {
  const files = await context.walk([dir('/Pictures', [
    dir('/Pictures/Trips', [file('/Pictures/Trips/a.jpg')]), file('/Pictures/b.jpg')])]);
  const got = files.map(f => [f._pcRelativePath, context.target(null, f._pcRelativePath)]);
  const want = [['Pictures/Trips/a.jpg', 'Pictures/Trips'], ['Pictures/b.jpg', 'Pictures']];
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error('dropped folder paths flattened: got ' + JSON.stringify(got));
  console.log('dropped directory paths hold');
})().catch(e => { console.error(e && e.stack || e); process.exitCode = 1; });

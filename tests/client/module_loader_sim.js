/* Execute app.js's shipped lazy-module loader through a cold and warm load.
 * A Promise return is part of the contract: Files must wait for PosterChan Code before handing it
 * a drive, synced-folder, or local file. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const app = fs.readFileSync(path.join(__dirname, '../../static/js/client/app.js'), 'utf8');

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

const window = {};
const context = {
  window,
  Promise,
  document: {
    head: { appendChild(el) {
      setTimeout(() => {
        window.PCCode = { openBlob() {}, openHostFile() {} };
        el.onload();
      }, 0);
    } },
    documentElement: { appendChild() {} },
    createElement() { return {}; },
  },
};
vm.createContext(context);
vm.runInContext(`const _lateLoad = {}; ${fn('function _withModule(')};
  globalThis.load = () => _withModule('code.js', 'PCCode');
  globalThis.warm = cb => _withModule('code.js', 'PCCode', cb);`, context);

(async () => {
  const cold = await context.load();
  if (!cold || typeof cold.openBlob !== 'function') throw new Error('cold load did not return PCCode');
  let called = false;
  const warm = await context.warm(() => { called = true; });
  if (warm !== cold || !called) throw new Error('warm load lost module or callback');
  console.log('lazy module promise holds');
})().catch(e => { console.error(e); process.exitCode = 1; });

'use strict';
/* The web client's source as a node harness should read it — the JS twin of tests/client_source.py.
 *
 * app.js was one 2.9 MB IIFE; screens that are not needed for the first paint now live in their own
 * files (mail.js, ai.js, …) as factories app.js builds on first use (`_lzGet` in app.js). A harness
 * that slices a function out of the client by its text keeps slicing exactly the same text — the
 * moved code is byte-identical — it only has to look in every file. `clientSource()` is every split
 * module followed by app.js; the modules come first so that a search for `function renderAI(` meets
 * the real definition before app.js's one-line entry point of the same name.
 *
 * The one rewrite a move makes: a read of one of app.js's live `let`s (ME, VIEW, CFG, …) is spelled
 * `S.ME` inside a module, where `S` is the getter object app.js hands it. A harness that stubs those
 * as globals (`globalThis.ME = …`) calls `installStateGlobals()` once, and `S.ME` then reads — and
 * writes — the same global the unmoved code would have. */
const fs = require('fs');
const path = require('path');

const CLIENT = path.join(__dirname, '..', '..', 'static', 'js', 'client');

function splitModules(){
  const app = fs.readFileSync(path.join(CLIENT, 'app.js'), 'utf8');
  const seen = [];
  for(const m of app.matchAll(/_lzGet\('([\w.-]+\.js)'/g)) if(!seen.includes(m[1])) seen.push(m[1]);
  return seen;
}

function clientSource(){
  return clientSourceAt(path.join(CLIENT, 'app.js'));
}

/* The same, for a harness handed an app.js path (an installed copy, a checkout under test): the
 * split modules are read from beside THAT app.js, and the list of them from that app.js too. */
function clientSourceAt(appPath){
  const dir = path.dirname(appPath), app = fs.readFileSync(appPath, 'utf8');
  const names = [];
  for(const m of app.matchAll(/_lzGet\('([\w.-]+\.js)'/g)) if(!names.includes(m[1])) names.push(m[1]);
  return names.map(n => fs.readFileSync(path.join(dir, n), 'utf8')).concat([app]).join('\n');
}

function installStateGlobals(target = globalThis){
  target.S = new Proxy({}, {
    get: (_, k) => target[k],
    set: (_, k, v) => { target[k] = v; return true; },
    has: (_, k) => k in target,
  });
  return target.S;
}

module.exports = { CLIENT, splitModules, clientSource, clientSourceAt, installStateGlobals };

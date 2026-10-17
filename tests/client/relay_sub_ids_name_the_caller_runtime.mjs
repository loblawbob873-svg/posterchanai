/* A subscription id names the client file that opened it, so the relay's cost line can say which code is asking.
 * 2026-10-17: one desktop client pulled 1.1 GB through the relay and every id was "sub" + random. */
import fs from 'node:fs'; import vm from 'node:vm'; import assert from 'node:assert/strict';
const src = fs.readFileSync(new URL('../../static/js/client/relay.js', import.meta.url), 'utf8');
const i = src.indexOf('function _callerTag()'); assert(i >= 0, '_callerTag is gone');
let d = 0, j = src.indexOf('{', i), end = -1;
for (let k = j; k < src.length; k++){ if (src[k] === '{') d++; else if (src[k] === '}' && --d === 0){ end = k; break; } }
const ctx = {}; vm.createContext(ctx);
vm.runInContext(src.slice(i, end + 1) + '\nglobalThis.tag = _callerTag;', ctx);
// The stack as a browser prints it: relay.js frames first, then the file that asked.
ctx.Error = class extends Error { constructor(){ super(); this.stack = 'Error\n    at _callerTag (app://posterchan/static/js/client/relay.js?v=2191:44:20)\n    at Object.subscribe (app://posterchan/static/js/client/relay.js?v=2191:762:30)\n    at refresh (app://posterchan/static/js/client/notes.js?v=2191:338:30)'; } };
vm.runInContext('globalThis.tag2 = function(){ return tag(); }', ctx);
assert.equal(ctx.tag(), '-notes-');
ctx.Error = class extends Error { constructor(){ super(); this.stack = undefined; } };
assert.equal(ctx.tag(), '', 'an unreadable stack must give no tag, never throw');
console.log('ok');

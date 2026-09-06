/* Execute the SHIPPED copyValue against a stubbed clipboard. Lifted from app.js rather than
   retyped, so the guard under test cannot drift from the one that runs. */
import fs from 'node:fs';
const src = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const a = src.indexOf('  function copyValue(text, okMsg, failLabel){');
if (a < 0) throw new Error('copyValue moved');
const b = src.indexOf('\n  function _stopLiveHb()', a);
if (b < 0) throw new Error('copyValue end marker moved');
const shipped = src.slice(a, b);

let written = null, toasts = [];
globalThis.toast = (m) => { toasts.push(String(m)); };
globalThis.document = { createElement: () => ({ style:{}, setAttribute(){}, focus(){}, select(){},
                                                setSelectionRange(){}, remove(){} }),
                        body: { appendChild(){} }, execCommand: () => false };
globalThis._copyFallback = () => {};
globalThis.window = {};
// The desktop/APK route, which is the one that actually runs on the machines this bug was reported from.
globalThis.window.pcClip = { write: (s) => { written = s; return Promise.resolve(true); } };

const copyValue = new Function(`${shipped}; return copyValue;`)();
const out = {};

await copyValue(Promise.resolve('note1realvalue'), 'ok', 'Copy:');
out.written = written; out.resolved = written;

written = null; toasts = [];
await copyValue({ id: 'x' }, 'ok', 'Copy:');
out.objectWritten = written; out.objectToast = toasts.join('|');

written = null; toasts = [];
await copyValue(Promise.reject(new Error('nope')), 'ok', 'Copy:');
out.rejectedWritten = written; out.rejectedToast = toasts.join('|');

written = null; await copyValue('note1plain', 'ok', 'Copy:'); out.plain = written;
written = null; await copyValue(42, 'ok', 'Copy:'); out.number = written;
written = null; await copyValue('', 'ok', 'Copy:'); out.empty = written;

process.stdout.write(JSON.stringify(out));

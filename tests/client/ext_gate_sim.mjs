/* The SHIPPED _extGate against a signer that is asleep, refusing, orphaned, or fine. */
import fs from 'node:fs';
const src = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const a = src.indexOf('  const _extGate = {');
const b = src.indexOf('\n  async function _nip17wrapVia', a);
if (a < 0 || b < 0) throw new Error('_extGate moved');
const _extGate = new Function(`${src.slice(a, b)}; return _extGate;`)();

const out = {};
const asleep = () => { const e = new Error('Could not establish connection. Receiving end does not exist.'); return e; };

// 1. Asleep on the first attempt, awake on the second: the caller must not see a failure.
let calls = 0;
out.wokeUp = await _extGate.call(null, async () => { calls++; if (calls < 2) throw asleep(); return 'signed'; });
out.wokeUpAttempts = calls;

// 2. Asleep every time: it gives up rather than looping for ever.
calls = 0;
try { await _extGate.call(null, async () => { calls++; throw asleep(); }); out.deadResult = 'RESOLVED'; }
catch (e) { out.deadResult = 'threw'; }
out.deadAttempts = calls;

// 3. A REFUSAL is an answer, not a nap — one attempt only, and the message survives.
calls = 0;
try { await _extGate.call(null, async () => { calls++; throw new Error('User rejected'); }); }
catch (e) { out.refusedMsg = e.message; }
out.refusedAttempts = calls;

// 4. An orphaned content script cannot be woken; it must say to reload.
calls = 0;
try { await _extGate.call(null, async () => { calls++; throw new Error('Extension context invalidated'); }); }
catch (e) { out.orphanMsg = e.message; }
out.orphanAttempts = calls;

// 5. The happy path is untouched: exactly one call.
calls = 0;
out.plain = await _extGate.call(null, async () => { calls++; return 'ok'; });
out.plainAttempts = calls;

// 6. Keyed de-duplication still collapses concurrent identical asks to ONE run.
calls = 0;
const k = 'same-key';
const [x, y] = await Promise.all([
  _extGate.call(k, async () => { calls++; return 'once'; }),
  _extGate.call(k, async () => { calls++; return 'once'; }),
]);
out.dedupe = (x === 'once' && y === 'once');
out.dedupeRuns = calls;

process.stdout.write(JSON.stringify(out));

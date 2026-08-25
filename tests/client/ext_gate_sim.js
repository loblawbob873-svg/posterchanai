/* The NIP-07 gate (app.js `_extGate`), run against an extension that behaves like the real one.
 *
 * "firefox throwing more signer errors... I login and nothing happening."
 *
 * Signing in restores everything sealed to your own key at once - dozens of nip44.decrypt calls in
 * one tick. An extension caps how many approval windows it will open (ours: `open >= 3` -> deny) and
 * past the cap it DENIES WITHOUT PROMPTING. Three questions were asked about and everything else was
 * refused invisibly, so the login came up with no notes and the default theme.
 *
 * The FakeExtension below is that behaviour, not a mock of the fix: it counts concurrent unanswered
 * asks and silently denies past three, exactly like extension/background.js did before the
 * coalescing fix - which is what a Firefox user still runs while AMO reviews the update, and what
 * nos2x and Alby may do regardless.
 *
 * The gate is LIFTED FROM THE SHIPPED FILE by brace matching rather than copied, so this cannot pass
 * against a fix that only exists here.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const APP = path.resolve(__dirname, '../../static/js/client/app.js');

function lift(name) {
  const src = fs.readFileSync(APP, 'utf8');
  const head = src.indexOf('const ' + name + ' = {');
  if (head < 0) throw new Error(name + ' is not in app.js');
  let i = src.indexOf('{', head), depth = 0;
  for (let k = i; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (!depth) return src.slice(head, k + 1); }
  }
  throw new Error(name + ' never closes');
}

const make = () => eval('(' + lift('_extGate').replace(/^const _extGate = /, '') + ')');

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* The extension as it actually behaves. `remembered` models the stored allow: once the user has
 * answered for this permission, later calls short-circuit and open no window at all. */
class FakeExtension {
  constructor(cap = 3, answerMs = 20) {
    this.cap = cap; this.answerMs = answerMs;
    this.open = 0; this.maxOpen = 0; this.windows = 0;
    this.denied = 0; this.calls = 0; this.remembered = false;
  }
  async decrypt(peer, ct) {
    this.calls++;
    if (this.remembered) { await sleep(1); return 'plain:' + ct; }
    if (this.open >= this.cap) { this.denied++; throw new Error('denied'); }  // SILENT: no window
    this.open++; this.windows++;
    this.maxOpen = Math.max(this.maxOpen, this.open);
    await sleep(this.answerMs);
    this.open--; this.remembered = true;                                     // the user clicked allow
    return 'plain:' + ct;
  }
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  ok   ' + name); return; }
  failures++; console.log('  FAIL ' + name + (detail ? '  - ' + detail : ''));
}

async function loginRestore(n, ext, gate) {
  // What a login does: n DISTINCT documents, all decrypted in the same tick.
  const jobs = [];
  for (let i = 0; i < n; i++) {
    jobs.push(gate.call('44d me ct' + i, () => ext.decrypt('me', 'ct' + i)));
  }
  return Promise.allSettled(jobs);
}

(async () => {
  console.log('the login fan-out, against an extension that denies past three windows');
  {
    const ext = new FakeExtension(), gate = make();
    const out = await loginRestore(19, ext, gate);
    const ok = out.filter(r => r.status === 'fulfilled').length;
    check('every document is decrypted', ok === 19, ok + '/19 came back');
    check('nothing was silently denied', ext.denied === 0, ext.denied + ' refused with no prompt');
    check('at most one window is ever open', ext.maxOpen <= 1, 'peak ' + ext.maxOpen);
    check('the user is asked exactly once', ext.windows === 1, ext.windows + ' prompts');
  }

  console.log('the same fan-out with NO gate - the bug, reproduced');
  {
    const ext = new FakeExtension();
    const out = await Promise.allSettled(
      Array.from({ length: 19 }, (_, i) => ext.decrypt('me', 'ct' + i)));
    const ok = out.filter(r => r.status === 'fulfilled').length;
    check('reproduces: most documents never arrive', ok < 19, ok + '/19 came back');
    check('reproduces: refusals with no prompt', ext.denied > 0, 'denied ' + ext.denied);
  }

  console.log('identical reads are asked once');
  {
    const ext = new FakeExtension(), gate = make();
    ext.remembered = true;                       // no prompt in the way; count raw calls
    const key = '44d me same';
    const all = await Promise.all(Array.from({ length: 40 }, () =>
      gate.call(key, () => ext.decrypt('me', 'same'))));
    check('one call served forty askers', ext.calls === 1, ext.calls + ' reached the extension');
    check('and they all got the answer', all.every(v => v === 'plain:same'));
  }

  console.log('signing is NOT coalesced - two events must both be signed');
  {
    const gate = make();
    let signed = 0;
    await Promise.all([1, 2, 3].map(() => gate.call(null, async () => { signed++; return 'sig'; })));
    check('every signature was produced', signed === 3, signed + ' of 3');
  }

  console.log('the queue widens once the extension has answered');
  {
    const ext = new FakeExtension(), gate = make();
    await gate.call('44d me first', () => ext.decrypt('me', 'first'));
    ext.maxOpen = 0;
    await loginRestore(12, ext, gate);
    check('later work is not serialised one at a time', gate._cap > 1, 'cap stayed ' + gate._cap);
  }

  console.log('a wedged extension rejects instead of hanging the login');
  {
    const gate = make();
    gate._MS = 60;                               // the shipped bound is 130s, above the 115s prompt
    let settled = false;
    const p = gate.call(null, () => new Promise(() => {}));   // never answers
    p.then(() => { settled = true; }, () => { settled = true; });
    await sleep(200);
    check('it settles', settled, 'a promise that never settles is not a rejection');
    await p.catch(e => check('and it says what happened',
                             /did not answer/.test(String(e && e.message)), String(e)));
  }

  console.log(failures ? '\nFAILED ' + failures : '\nOK  the gate holds');
  process.exit(failures ? 1 : 0);
})();

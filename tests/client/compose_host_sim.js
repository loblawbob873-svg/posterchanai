/* RUN the composer's window-host decision. It is a gate, and both of its answers matter:
 *   say yes to something unserialisable -> the popup opens EMPTY and the attachment is gone;
 *   say no to everything                -> the composer is behind a window again, which is the bug.
 *
 * Usage: node compose_host_sim.js ../../static/js/client/os.js
 */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.resolve(__dirname, process.argv[2]), 'utf8');

function lift(name) {
  const decl = '  function ' + name + '(opts){';
  const at = src.indexOf(decl);
  if (at < 0) throw new Error(name + ' is gone — the composer no longer opens in a window');
  let depth = 0, end = -1;
  for (let i = src.indexOf('{', at); i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  return src.slice(at, end);
}

const body = lift('_composeInWindow');
function run(opts, env) {
  const ctx = Object.assign({ on: true, popupKind: '', bridge: true, opened: [] }, env || {});
  const decl = lift('_composeInWindow');
  const inner = decl.slice(decl.indexOf('{') + 1, decl.lastIndexOf('}'));
  const fn = new Function('opts', 'ctx', `
    const on = ctx.on;
    const popupKind = () => ctx.popupKind;
    const vwL = () => 2560, vhL = () => 1706;
    const window = { pcPopup: ctx.bridge ? { open: (k, r, a) => ctx.opened.push([k, r, a]) } : undefined };
    const pcPopup = window.pcPopup;
    ${inner}
  `);
  const took = fn(opts, ctx);
  return { took, opened: ctx.opened };
}

let bad = 0, ran = 0;
function check(name, f) { ran++; try { f(); } catch (e) { bad++; console.log('  FAIL ' + name + ': ' + e.message); } }
const ID = 'a'.repeat(64), PK = 'b'.repeat(64);

check('a plain new post opens in a window', () => {
  const r = run({});
  if (!r.took) throw new Error('refused an ordinary composer');
  if (r.opened[0][0] !== 'compose') throw new Error('wrong popup kind');
});

check('a reply carries its event and author across', () => {
  const r = run({ reply: ID, replyPk: PK });
  const arg = JSON.parse(r.opened[0][2]);
  if (arg.reply !== ID || arg.replyPk !== PK) throw new Error('reply target lost: ' + r.opened[0][2]);
});

check('a quote carries its id', () => {
  const r = run({ quote: ID });
  if (JSON.parse(r.opened[0][2]).quote !== ID) throw new Error('quote lost');
});

check('a content warning survives', () => {
  const r = run({ cw: true, cwReason: 'spoiler' });
  const a = JSON.parse(r.opened[0][2]);
  if (!a.cw || a.cwReason !== 'spoiler') throw new Error('cw lost: ' + r.opened[0][2]);
});

/* THE REFUSALS. Each of these would open an empty composer and silently drop what the person
 * had already chosen, which is worse than a modal that is behind a window. */
for (const [what, opts] of [
  ['picked files', { files: [{ name: 'a.png' }] }],
  ['an article comment', { articleComment: {} }],
  ['an article parent', { articleParent: {} }],
  ['an explicit open target', { open: {} }],
]) {
  check('it refuses ' + what + ' and lets the in-page modal handle it', () => {
    const r = run(opts);
    if (r.took) throw new Error(what + ' was sent through a query string — it cannot survive one');
    if (r.opened.length) throw new Error('a window was opened anyway');
  });
}

check('a draft too long for a URL stays in the page', () => {
  const r = run({ text: 'x'.repeat(2000) });
  if (r.took) throw new Error('a 2000-character draft was pushed through the argument cap');
});

check('no compositor means no window', () => {
  const r = run({}, { bridge: false });
  if (r.took) throw new Error('claimed to open a window with no popup bridge');
});

check('it does nothing when the desktop is not up', () => {
  if (run({}, { on: false }).took) throw new Error('opened a composer window outside the desktop');
});

check('a popup never opens another popup', () => {
  if (run({}, { popupKind: 'compose' }).took) throw new Error('the compose window would recurse');
});

console.log((bad ? 'FAILED ' : 'ok ') + (ran - bad) + '/' + ran);
process.exit(bad ? 1 : 0);

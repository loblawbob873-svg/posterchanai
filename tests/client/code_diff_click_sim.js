/* Runtime regression: a diff row knows which line of the file it is, and clicking it opens that
 * file there.
 *
 * The walk is the whole thing worth testing. Every individual row of a unified diff renders
 * correctly no matter what line number you attach to it, so a wrong walk is invisible in the
 * picture and shows up only as a click that lands in the wrong place — further off the further
 * down the patch you click, which reads as "the editor scrolls randomly" rather than as an
 * arithmetic bug.
 */
'use strict';
const assert = require('assert');

global.window = global;
global.localStorage = { getItem(){ return null; }, setItem(){} };
global.document = { querySelectorAll(){ return []; }, addEventListener(){}, hidden: false };
global.addEventListener = () => {};
global.getComputedStyle = () => ({ lineHeight: '19.5px' });
/* A stand-in for the one element the scroll half actually measures. `restoreCaret` and
 * `syncScroll` reach for it too, so it answers everything they touch and nothing else — a fuller
 * fake would start agreeing with whatever the code does, which is how a fixture ends up unable to
 * see the bug it was written for. */
const TA = { value: '', scrollTop: 0, scrollLeft: 0, scrollHeight: 0,
             setSelectionRange(){}, selectionStart: 0 };
let taMounted = false;
global.__PC = {
  VIEW: 'code', ME: null,
  $(sel){ return (taMounted && sel === '#pcc-ta') ? TA : null; },
  enc(v){ return String(v); }, toast(){},
  authFetch(){ throw new Error('network must not be used'); },
  ensureAiSession(){}, uiPrompt(){}, uiConfirm(){ return true; },
};

const reads = [];
global.pcHost = {
  pickDirectory(){},
  async list(path){ return { path, entries: [] }; },
};
global.PCHostFiles = {
  async readText(path){
    reads.push(path);
    if(path === '/repo/src/app.js')
      return { path, text: 'one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n', mtime: 1 };
    const e = new Error('no such file or directory'); e.code = 'ENOENT'; throw e;
  },
};

require('../../static/js/client/code.js');

const P = () => PCCode._parseDiff;

(async () => {
  assert(PCCode, 'Code module did not initialise');

  // ---- the walk ------------------------------------------------------------------------------
  const patch = [
    'diff --git a/src/app.js b/src/app.js',
    'index 111..222 100644',
    '--- a/src/app.js',
    '+++ b/src/app.js',
    '@@ -1,4 +1,5 @@',
    ' one',
    '-two',
    '+TWO',
    '+two and a half',
    ' three',
    ' four',
    '@@ -20,3 +21,3 @@ def thing():',
    ' twenty',
    '-twentyone',
    '+TWENTYONE',
    ' twentytwo',
    '\\ No newline at end of file',
  ].join('\n');

  const rows = P()(patch);
  const at = (t) => rows.filter(r => r.type === t);

  assert.deepEqual(rows.slice(0, 4).map(r => r.type), ['meta', 'meta', 'meta', 'meta'],
    'the file header was walked as content');
  assert.deepEqual(at('hunk').map(r => r.line), [1, 21]);

  // ` one` is line 1; `-two` describes a line that is GONE, so it takes the position it was
  // removed from (2); `+TWO` is the new line 2 and `+two and a half` the new line 3.
  const first = rows.slice(5, 11).map(r => [r.type, r.line]);
  assert.deepEqual(first, [['ctx', 1], ['del', 2], ['add', 2], ['add', 3], ['ctx', 4], ['ctx', 5]],
    'the new-file line walk is wrong: ' + JSON.stringify(first));

  // Second hunk restarts from its own header, it does not continue from the first.
  const second = rows.slice(12, 16).map(r => [r.type, r.line]);
  assert.deepEqual(second, [['ctx', 21], ['del', 22], ['add', 22], ['ctx', 23]],
    'the second hunk did not restart at its header: ' + JSON.stringify(second));

  const note = rows[rows.length - 1];
  assert.equal(note.type, 'note');
  assert.equal(note.line, 0, 'the "no newline" marker was given a line of its own');

  // Counts come from the same walk, so the `+++`/`---` headers can never be counted as content.
  assert.deepEqual(PCCode._diffCounts(patch), { added: 3, removed: 2 });

  // An untracked file's whole-file diff, and an empty patch.
  assert.deepEqual(PCCode._diffCounts('--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+a\n+b\n'),
    { added: 2, removed: 0 });
  assert.deepEqual(PCCode._diffCounts(''), { added: 0, removed: 0 });
  assert.deepEqual(P()(''), []);

  // ---- rendering -----------------------------------------------------------------------------
  const html = PCCode._diffRowsHtml(patch);
  assert(/data-diff-line="3"/.test(html), 'an added line is not clickable');
  assert(!/<div class="pcc-dl pcc-dl-meta"[^>]*data-diff-line/.test(html),
    'a file header was made clickable');
  assert.equal((html.match(/data-diff-line=/g) || []).length, 12,
    'clickable rows: ' + (html.match(/data-diff-line=/g) || []).length);

  // A patch far larger than anybody reads is BOUNDED, and the cut is stated — a diff that quietly
  // stops halfway reads as a smaller change than it is.
  const huge = ['--- a/big.txt', '+++ b/big.txt', '@@ -1,9000 +1,9000 @@']
    .concat(Array.from({ length: 9000 }, (_, i) => '+line ' + i)).join('\n');
  const bigHtml = PCCode._diffRowsHtml(huge);
  const drawn = (bigHtml.match(/data-diff-line=/g) || []).length;
  assert(drawn > 0 && drawn <= 4000, 'row cap: ' + drawn);
  assert(/more lines in this patch/.test(bigHtml), 'the patch was truncated silently');

  // ---- the path a click opens ------------------------------------------------------------------
  PCCode._state.hostRoot = '/repo/src';
  PCCode._state.git = { root: '/repo', branch: 'master', files: [{ xy: ' M', path: 'src/app.js' }] };
  assert.equal(PCCode._gitFilePath('src/app.js'), '/repo/src/app.js',
    'a repository-relative path was opened against the picked folder instead of the repository');

  PCCode._state.hostRoot = '';
  PCCode._state.git = { repo: 'checkout', files: [] };
  assert.equal(PCCode._gitFilePath('src/app.js'), 'checkout/src/app.js');
  PCCode._state.git = { repo: '', files: [] };
  assert.equal(PCCode._gitFilePath('src/app.js'), 'src/app.js');

  // ---- clicking a line opens the file there -----------------------------------------------------
  PCCode._state.hostRoot = '/repo/src';
  PCCode._state.git = { root: '/repo', files: [{ xy: ' M', path: 'src/app.js' }] };
  PCCode._state.gitOpen = true;
  PCCode._state.gitDiff = { path: 'src/app.js', text: patch, error: '', busy: false };
  PCCode._state.open = []; PCCode._state.active = -1;

  assert.equal(await PCCode._openDiffAt('src/app.js', 4), true);
  assert.deepEqual(reads, ['/repo/src/app.js']);
  const d = PCCode._state.open[PCCode._state.active];
  // "one\ntwo\nthree\nfour" — line 4 begins after 4+4+6 = 14 characters.
  assert.equal(d.sel.s, 14, 'the caret did not land on line 4: ' + JSON.stringify(d.sel));
  assert.equal(d.sel.e, 18);
  assert.equal(PCCode._state.gitOpen, false, 'Source Control stayed mounted over the file it opened');
  assert.equal(PCCode._state.gitDiff, null, 'the patch stayed on top of the editor');

  // ---- and it SCROLLS there ---------------------------------------------------------------------
  // A caret on a line the pane is not showing is a selection nobody can see, which reads exactly
  // like a click that did nothing.
  taMounted = true;
  TA.value = d.text; TA.scrollTop = 0; TA.scrollHeight = 10 * 19.5;
  PCCode._state.gitDiff = { path: 'src/app.js', text: patch, error: '', busy: false };
  assert.equal(await PCCode._openDiffAt('src/app.js', 9), true);
  // Line 9 is row 8; three lines of lead-in at 19.5px each.
  assert.equal(PCCode._state.open[PCCode._state.active].scroll, 5 * 19.5);
  assert.equal(TA.scrollTop, 5 * 19.5, 'the editor did not scroll to the line it selected');
  taMounted = false;

  // A file that cannot be opened must not move the caret of whatever tab was active.
  PCCode._state.gitDiff = { path: 'src/gone.js', text: patch, error: '', busy: false };
  const before = JSON.stringify(PCCode._state.open[PCCode._state.active].sel);
  assert.equal(await PCCode._openDiffAt('src/gone.js', 9), false);
  assert.equal(JSON.stringify(PCCode._state.open[PCCode._state.active].sel), before,
    'a failed open moved the caret of a different file');

  console.log('code diff click runtime: ok');
})().catch(e => { console.error(e); process.exitCode = 1; });

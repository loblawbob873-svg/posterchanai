/* Runtime regression: Source Control's discard says what it is about to lose, and whether anything
 * anywhere could bring it back, BEFORE it asks.
 *
 * The rule is Folder Sync's, not a count: "can this be undone" is answered per file from evidence,
 * and "could not ask" is a third answer that is never dressed up as either of the other two. The
 * shape this replaces was one sentence — "Discard every change to X? This cannot be undone." —
 * used for three different acts, of which it described none:
 *
 *   - an untracked file is DELETED, not "changed", and nothing has ever held a copy of it;
 *   - a staged edit is thrown away along with the working copy, which staging does not read like;
 *   - and a diff that failed to load is not an empty diff.
 */
'use strict';
const assert = require('assert');

global.window = global;
global.localStorage = { getItem(){ return null; }, setItem(){} };
global.document = { querySelectorAll(){ return []; }, addEventListener(){}, hidden: false };
global.addEventListener = () => {};

const asked = [];
let answer = true;
global.__PC = {
  VIEW: 'code', ME: null,
  $(){ return null; }, enc(v){ return String(v); }, toast(){},
  authFetch(){ throw new Error('network must not be used'); },
  ensureAiSession(){}, uiPrompt(){},
  uiConfirm(message, opts){ asked.push({ message, opts: opts || {} }); return Promise.resolve(answer); },
};

const order = [];
let diffAnswer = null;
global.pcHost = {
  pickDirectory(){},
  async list(path){ return { path, entries: [] }; },
  async gitDiff(_root, path){
    order.push('diff:' + path);
    if(diffAnswer instanceof Error) throw diffAnswer;
    return { diff: diffAnswer };
  },
  async gitStatus(){ order.push('status'); return { root: '/repo', branch: 'master', files: [] }; },
  async gitAction(_root, action, paths){ order.push(action + ':' + paths.join(',')); return { ok: true }; },
};

require('../../static/js/client/code.js');

const plan = (o) => PCCode._discardPlan(o);
const MOD = '--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n';
const NEWFILE = '--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d\n';

(async () => {
  assert(PCCode, 'Code module did not initialise');

  // ---- an untracked file is DELETED, and nothing has a copy -----------------------------------
  const u = plan({ path: 'new.txt', xy: '??', diff: NEWFILE, measured: true });
  assert.equal(u.untracked, true);
  assert.equal(u.ok, 'Delete file', 'the confirm button still called a deletion a discard');
  assert(/DELETES/.test(u.message), 'the dialog did not say the file is deleted: ' + u.message);
  assert(/ever stored a copy/.test(u.message),
    'the dialog did not answer whether it could be brought back: ' + u.message);
  assert(/\b4 lines\b/.test(u.message), 'the dialog did not say how much is lost: ' + u.message);
  assert(!/last commit/.test(u.message),
    'a file no commit has ever held was described as restorable from one');

  // An untracked file whose diff the node cannot produce (the server route diffs tracked paths
  // only) must still say it is a deletion — with no invented count.
  const u0 = plan({ path: 'new.txt', xy: '??', diff: '', measured: true });
  assert(/DELETES/.test(u0.message) && /nothing can bring it back/.test(u0.message));
  assert(!/\d+ line/.test(u0.message), 'a count was printed for an unmeasurable file: ' + u0.message);

  // ---- a working-tree edit --------------------------------------------------------------------
  const m = plan({ path: 'a.txt', xy: ' M', diff: MOD, measured: true });
  assert.equal(m.untracked, false);
  assert.equal(m.staged, false);
  assert.equal(m.ok, 'Discard changes');
  assert(/1 added and 1 removed lines/.test(m.message), m.message);
  assert(/committed nowhere/.test(m.message), m.message);
  assert(!/staged/.test(m.message), 'an unstaged file was told its staged copy would go');

  // ---- a STAGED edit loses the staged copy too --------------------------------------------------
  const st = plan({ path: 'a.txt', xy: 'M ', diff: MOD, measured: true });
  assert.equal(st.staged, true);
  assert(/staged copy goes too/.test(st.message),
    'a staged file was not told its index copy goes as well: ' + st.message);

  // ---- "could not ask" is not "nothing to lose" ---------------------------------------------
  const un = plan({ path: 'a.txt', xy: ' M', diff: '', measured: false, error: 'timed out' });
  assert.equal(un.measured, false);
  assert.equal(un.ok, 'Discard anyway', 'an unmeasured discard kept a confident button');
  assert(/could not read what this would lose/.test(un.message), un.message);
  assert(/timed out/.test(un.message), 'the reason it could not measure was swallowed');
  assert(!/\d+ added/.test(un.message),
    'a line count was claimed about a diff nothing read: ' + un.message);

  // A clean file measured as clean says so, and is NOT confused with an unread one.
  const clean = plan({ path: 'a.txt', xy: ' M', diff: '', measured: true });
  assert(/Nothing in this file differs/.test(clean.message), clean.message);
  assert(!/could not read/.test(clean.message));

  // ---- the measurement happens BEFORE the question -----------------------------------------
  const S = PCCode._state;
  S.hostRoot = '/repo';
  S.git = { root: '/repo', branch: 'master', files: [{ xy: '??', path: 'new.txt' }] };
  S.gitDiff = null; S.gitOpen = true;
  diffAnswer = NEWFILE;
  order.length = 0; asked.length = 0; answer = true;

  assert.equal(await PCCode._discardFile('new.txt'), true);
  assert.equal(order[0], 'diff:new.txt',
    'the dialog opened before anything measured what it would cost: ' + JSON.stringify(order));
  assert(order.indexOf('restore:new.txt') > 0, JSON.stringify(order));
  assert.equal(asked.length, 1);
  assert.equal(asked[0].opts.ok, 'Delete file');
  assert.equal(asked[0].opts.danger, true, 'the destructive action was offered as an ordinary OK');
  assert.equal(asked[0].opts.cancel, 'Keep it');
  assert(/DELETES/.test(asked[0].message), asked[0].message);

  // Declining touches nothing.
  order.length = 0; asked.length = 0; answer = false;
  assert.equal(await PCCode._discardFile('new.txt'), false);
  assert.equal(order.filter(o => o.startsWith('restore')).length, 0,
    'a declined discard ran anyway: ' + JSON.stringify(order));

  // A diff that will not load still offers the action, saying it could not measure.
  order.length = 0; asked.length = 0; answer = false;
  diffAnswer = new Error('git is unavailable');
  S.git = { root: '/repo', files: [{ xy: ' M', path: 'a.txt' }] };
  await PCCode._discardFile('a.txt');
  assert.equal(asked.length, 1);
  assert.equal(asked[0].opts.ok, 'Discard anyway');
  assert(/git is unavailable/.test(asked[0].message), asked[0].message);
  assert(!/\d+ added/.test(asked[0].message), asked[0].message);

  console.log('code discard safety runtime: ok');
})().catch(e => { console.error(e); process.exitCode = 1; });

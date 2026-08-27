/* Drive the native Git bridge extracted from an installed app.asar against a disposable repo. */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');
const hostfs = process.env.PC_INSTALLED_HOSTFS_JS;
if (!hostfs) throw new Error('PC_INSTALLED_HOSTFS_JS must name hostfs.js extracted from app.asar');
const H = require(path.resolve(hostfs));
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'pc-installed-git-'));
const run = (...args) => cp.execFileSync('git', ['-C', root, ...args], {stdio:'pipe'});

(async () => {
  try {
    run('init', '-q');
    run('config', 'user.name', 'PosterChan package gate');
    run('config', 'user.email', 'package-gate@example.invalid');
    const changed = path.join(root, 'changed.js');
    fs.writeFileSync(changed, 'const staged = false;\n');
    run('add', 'changed.js'); run('commit', '-qm', 'initial');
    fs.writeFileSync(changed, 'const staged = true;\n'); run('add', 'changed.js');
    const before = await H.gitStatus(root);
    const diff = await H.gitDiff(root, 'changed.js');
    await H.gitAction(root, 'restore', ['changed.js'], '');
    const after = await H.gitStatus(root);
    if (JSON.stringify(before.files) !== JSON.stringify([{xy:'M ', path:'changed.js'}]))
      throw new Error('packaged bridge did not report the staged edit: ' + JSON.stringify(before));
    if (!String(diff.diff || '').includes('+const staged = true;'))
      throw new Error('packaged bridge did not return the staged diff');
    if ((after.files || []).length || fs.readFileSync(changed, 'utf8') !== 'const staged = false;\n')
      throw new Error('packaged Restore left the index or worktree dirty: ' + JSON.stringify(after));
    console.log('installed native Git diff/staged restore holds');
  } finally {
    fs.rmSync(root, {recursive:true, force:true});
  }
})().catch(e => { console.error(e && e.stack || e); process.exitCode = 1; });

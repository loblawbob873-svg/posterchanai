/* Exercise waitForWindow from wm.js extracted from an installed ASAR against a live /proc fork. */
'use strict';
const cp = require('child_process');
const path = require('path');
const wmPath = process.env.PC_INSTALLED_WM_JS;
if (!wmPath) throw new Error('PC_INSTALLED_WM_JS must name wm.js extracted from app.asar');
const {WM} = require(path.resolve(wmPath));
const helper = "const {spawn}=require('child_process');" +
  "const c=spawn('sleep',['8'],{stdio:'ignore'});process.stdout.write(String(c.pid)+'\\n');" +
  "setTimeout(()=>{},8000)";
const root = cp.spawn(process.execPath, ['-e', helper],
  {stdio:['ignore', 'pipe', 'ignore'], detached:true});

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  try {
    let raw = '';
    root.stdout.setEncoding('utf8'); root.stdout.on('data', chunk => { raw += chunk; });
    for (let i = 0; i < 20 && !raw.includes('\n'); i++) await delay(25);
    const child = Number(raw.trim()) || 0;
    if (!child) throw new Error('disposable child process did not appear');
    const wm = Object.create(WM.prototype);
    wm.windows = async () => [{id:77, pid:child, app:'firefox-disposable'}];
    const hit = await wm.waitForWindow(root.pid, 700, []);
    if (!hit || hit.pid !== child)
      throw new Error('packaged waitForWindow lost late-forked child ' + child);
    console.log('installed native process ancestry holds');
  } finally {
    try { process.kill(-root.pid, 'SIGTERM'); } catch (_) { try { root.kill('SIGTERM'); } catch (__) {} }
  }
})().catch(e => { console.error(e && e.stack || e); process.exitCode = 1; });

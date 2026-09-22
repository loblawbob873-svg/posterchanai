/* THE GRAPHICAL INSTALLER'S HALF IN THE MAIN PROCESS — and all it does is ask gentoo.sh.
 *
 * PosterChanOS already has an installer: `gentoo.sh install-live`, the one the release gate drives
 * (scripts/check_livecd_install_vm.py) and the one the LiveCD's terminal entry runs. It prepares
 * the disk, copies the image, builds the boot chain and refuses to report success until it has
 * read the result back. None of that is repeated here. This module only:
 *
 *   - says whether this session is a LIVE boot (the kernel command line carries `rd.live.image`
 *     on every live boot entry liveCD() writes, and on nothing an installed machine boots with);
 *   - lists the disks, marking the one we booted from, which is never offered;
 *   - starts gentoo.sh with the answers the wizard collected, as environment variables it reads
 *     instead of prompting (PC_INSTALL_DISK, PC_INSTALL_ROOT_NAME, PC_INSTALL_MODE,
 *     PC_ASSUME_YES, PC_INSTALL_PASSWORD_FILE, PC_INSTALL_PROGRESS);
 *   - reports its progress from the `::pc-install::` lines it prints, and its log.
 *
 * THE INSTALL OUTLIVES THE WINDOW. It runs under liveusb-runner.js, the same detached supervisor
 * the ISO builder uses, so closing the installer, or the desktop restarting, does not kill a
 * half-written disk — and a reopened installer reads the same state file and picks the job up.
 *
 * THE PASSWORD NEVER GOES ON A COMMAND LINE OR INTO AN ENVIRONMENT. Both are readable in /proc for
 * the length of the install (the runner's own spec travels in its argv). It is written to a 0600
 * file in this account's 0700 state directory; gentoo.sh reads it once and deletes it, and this
 * side deletes it again when the job ends, whatever happened.
 */
'use strict';
const { execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const LSBLK = process.env.PC_LSBLK || 'lsblk';
const SUDO = process.env.PC_SUDO || 'sudo';
const CMDLINE = process.env.PC_PROC_CMDLINE || '/proc/cmdline';
const INSTALLERS = process.env.PC_INSTALLER_SCRIPT
  ? [process.env.PC_INSTALLER_SCRIPT]
  : ['/usr/bin/gentoo.sh', '/usr/local/share/posterchanos/gentoo.sh'];
const STATE_DIR = process.env.PC_INSTALLER_STATE_DIR
  || path.join(process.env.XDG_STATE_HOME || path.join(process.env.HOME || '', '.local/state'), 'posterchan');
const STATE_FILE = path.join(STATE_DIR, 'installer-job.json');
const LOG_FILE = path.join(STATE_DIR, 'installer-job.log');
const LOCK_FILE = path.join(STATE_DIR, 'installer-job.lock');
const SECRET_FILE = path.join(STATE_DIR, 'installer-secret');
const RUNNER = path.join(__dirname, 'liveusb-runner.js');

/* gentoo.sh refuses anything smaller for its own default; a smaller disk may still be chosen (an
 * "8 GB" eMMC is 7.45 GiB) — it is WARNED about, the way the terminal installer names it. */
const MIN_BYTES = 8 * 1024 * 1024 * 1024;
const ROOT_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
const NOT_A_TARGET = /^(fd|sr|zram|loop|ram)/;

/* ---------------------------------------------------------------- is this a live boot? */

function installerPath(){
  for(const p of INSTALLERS){ try{ fs.accessSync(p, fs.constants.X_OK); return p; }catch(_){} }
  return '';
}
function info(){
  let cmdline = '';
  try{ cmdline = fs.readFileSync(CMDLINE, 'utf8'); }catch(_){}
  const live = /(^|\s)rd\.live\.image(\s|$)/.test(cmdline);
  const installer = live ? installerPath() : '';
  let efi = false;
  try{ efi = fs.existsSync(process.env.PC_EFI_DIR || '/sys/firmware/efi'); }catch(_){}
  /* `available` is the one answer the desktop acts on: a live boot WITH an installer to run. A live
   * boot without one (a rescue disc built from some other tree) gets no icon that could only fail. */
  return { live, installer, available: !!(live && installer), efi };
}

/* ---------------------------------------------------------------- the disks */

function run(bin, args, ms){
  return new Promise((resolve, reject) => execFile(bin, args, { timeout: ms || 8000 }, (e, out, err) => {
    if(e) return reject(new Error(String(err || e.message || e).trim().split('\n').pop()));
    resolve(String(out || ''));
  }));
}
function flatten(rows, out = []){ for(const r of rows || []){ out.push(r); flatten(r.children, out); } return out; }
const mounts = (x) => (x.mountpoints || []).filter(Boolean);

/* WHICH DISK IS THE LIVE MEDIUM — the same three answers gentoo.sh's setDevices uses, because a
 * wizard that disagreed with the script about this would offer a disk the script then refuses (or,
 * worse, the reverse): the disk holding dracut's live mount, the one the installer's own medium scan
 * mounted, or a whole disk carrying an ISO9660 signature, which no installed disk does. */
function liveDiskNames(roots){
  const names = new Set();
  for(const d of roots){
    const all = flatten([d]);
    if(d.fstype === 'iso9660') names.add(d.name);
    if(all.some(x => mounts(x).some(m => m === '/run/initramfs/live' || m === '/run/posterchan-live-media'
                                          || m === '/run/rootfsbase')))
      names.add(d.name);
    if(all.some(x => mounts(x).includes('/'))) names.add(d.name);
  }
  return names;
}

async function disks(){
  const raw = await run(LSBLK, ['-J', '-b', '-o', 'NAME,PATH,TYPE,SIZE,RM,RO,TRAN,MODEL,FSTYPE,LABEL,MOUNTPOINTS']);
  const roots = (JSON.parse(raw).blockdevices || []).filter(d => d && d.type === 'disk');
  const live = liveDiskNames(roots);
  return roots.filter(d => !NOT_A_TARGET.test(String(d.name || ''))).map(d => {
    const parts = (d.children || []).filter(c => c.type === 'part');
    const size = Number(d.size) || 0;
    let why = '';
    if(live.has(d.name)) why = 'This is the drive PosterChanOS is running from.';
    else if(String(d.ro) === '1' || d.ro === true) why = 'This drive is read-only.';
    const layout = parts.length >= 2 && parts[0].fstype === 'vfat' && parts[1].fstype === 'crypto_LUKS';
    return {
      name: String(d.name), path: String(d.path || '/dev/' + d.name), size,
      model: String(d.model || '').trim(), tran: String(d.tran || ''),
      removable: String(d.rm) === '1' || d.rm === true,
      small: size < MIN_BYTES,
      mounted: flatten([d]).some(x => mounts(x).length),
      selectable: !why, why,
      /* An earlier PosterChanOS install (FAT32 ESP + LUKS) — gentoo.sh offers to RESUME onto it. */
      posterchanLayout: layout,
      parts: parts.map(p => ({ name: String(p.name), size: Number(p.size) || 0,
                               fstype: String(p.fstype || ''), label: String(p.label || '') })),
    };
  });
}

/* ---------------------------------------------------------------- progress, read from the log */

/* Where each phase starts on the bar. The copy is the long one (gigabytes through rsync), so it
 * owns most of the bar and moves with rsync's own percentage; everything else is a step. */
const STAGES = [
  ['medium', 1], ['disk', 2], ['format', 4], ['mount', 7], ['copy', 9], ['kernel', 78],
  ['initramfs', 80], ['accounts', 85], ['bootloader', 87], ['shell', 93], ['verify', 97], ['done', 100],
];
const COPY_END = 78;
const ANSI = /\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>]/g;

function cleanLog(text){
  /* rsync --info=progress2 redraws ONE line with carriage returns. Kept raw, a copy is thousands of
   * overwritten fragments on one "line"; keep what a terminal would finally show. */
  return String(text || '').replace(ANSI, '').split('\n')
    .map(l => { const seg = l.split('\r').filter(s => s.trim()); return seg.length ? seg[seg.length - 1] : ''; })
    .join('\n');
}

function progress(text){
  const s = String(text || '');
  const re = /::pc-install:: (\S+)(?: ([^\r\n]*))?/g;
  let m, last = null;
  while((m = re.exec(s))) last = { stage: m[1], label: (m[2] || '').trim(), at: re.lastIndex };
  if(!last) return { stage: '', label: '', percent: 0 };
  const i = STAGES.findIndex(x => x[0] === last.stage);
  let percent = i < 0 ? 0 : STAGES[i][1];
  let copied = null;
  if(last.stage === 'copy'){
    const tail = s.slice(last.at);
    const pct = tail.match(/(\d{1,3})%/g);
    if(pct){
      copied = Math.min(100, Number(pct[pct.length - 1].replace('%', '')) || 0);
      percent = STAGES[i][1] + Math.round((COPY_END - STAGES[i][1]) * copied / 100);
    }
  }
  return { stage: last.stage, label: last.label, percent, copied };
}

/* ---------------------------------------------------------------- the job */

let job = { kind: 'install', running: false, launching: false, ok: false, message: '', started: 0,
            finished: 0, token: '', pid: 0, procStart: '', exitCode: null, disk: '' };

function save(){
  try{
    fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
    const tmp = STATE_FILE + '.new';
    fs.writeFileSync(tmp, JSON.stringify(job), { mode: 0o600 }); fs.renameSync(tmp, STATE_FILE);
  }catch(_){}
}
function procStart(pid){
  try{ const s = fs.readFileSync('/proc/' + pid + '/stat', 'utf8'), end = s.lastIndexOf(')');
       return end >= 0 ? s.slice(end + 2).split(' ')[19] || '' : ''; }catch(_){ return ''; }
}
function alive(old){
  const pid = Number(old && old.pid);
  if(!Number.isInteger(pid) || pid < 2) return false;
  try{ process.kill(pid, 0); }catch(_){ return false; }
  const start = procStart(pid);
  if(old.procStart && start !== String(old.procStart)) return false;
  try{ if(old.token && !fs.readFileSync('/proc/' + pid + '/cmdline', 'utf8').includes(old.token)) return false; }
  catch(_){ if(process.platform === 'linux') return false; }
  return true;
}
function forgetSecret(){ try{ fs.unlinkSync(SECRET_FILE); }catch(_){} }
function recover(){
  try{
    const old = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    if(!old || old.kind !== 'install') return;
    if(old.launching && Date.now() - Number(old.started || 0) < 10000){ job = Object.assign(job, old); return; }
    old.running = alive(old);
    /* The supervisor records its exit an instant after its child returns; inside that handoff the
     * process is gone and the result not yet written. Read that as still running, never as failed. */
    if(!old.running && !old.finished && Date.now() - Number(old.started || 0) < 2000){
      job = Object.assign(job, old); job.running = true; return;
    }
    if(!old.running && !old.finished){
      old.finished = Date.now(); old.ok = false; old.launching = false;
      old.message = 'The installer stopped before it finished.';
      job = Object.assign(job, old); save();
    }else job = Object.assign(job, old);
    if(!job.running){
      forgetSecret();
      try{ if(fs.readFileSync(LOCK_FILE, 'utf8') === job.token) fs.unlinkSync(LOCK_FILE); }catch(_){}
    }
  }catch(_){}
}
function acquireLock(token){
  fs.mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  for(let n = 0; n < 2; n++){
    try{ const fd = fs.openSync(LOCK_FILE, 'wx', 0o600); fs.writeFileSync(fd, token); fs.closeSync(fd); return; }
    catch(e){
      if(n) throw new Error('the installer is already running');
      let recent = true; try{ recent = Date.now() - fs.statSync(LOCK_FILE).mtimeMs < 10000; }catch(_){}
      let owner = null; try{ owner = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); }catch(_){}
      if(recent || (owner && fs.readFileSync(LOCK_FILE, 'utf8') === owner.token && (owner.launching || alive(owner))))
        throw new Error('the installer is already running');
      try{ fs.unlinkSync(LOCK_FILE); }catch(_){}
    }
  }
}

function status(){
  recover();
  let log = '';
  try{ log = fs.readFileSync(LOG_FILE, 'utf8'); }catch(_){}
  const p = progress(log);
  /* THE BAR NEVER MOVES BACKWARDS. rsync --info=progress2 recomputes its total as incremental
   * recursion discovers files, so its overall percentage can fall (measured in the VM run:
   * 77 -> 74 -> 77); a bar that jumps back reads as the install undoing itself. Kept per job — a new
   * job object starts from zero again. */
  if(p.percent < (job._maxPercent || 0)) p.percent = job._maxPercent;
  else job._maxPercent = p.percent;
  const out = Object.assign({}, job, { progress: p, log: cleanLog(log.slice(-400000)).slice(-60000) });
  delete out._maxPercent;
  delete out.token;
  /* A job that ended well IS at the end of the bar, even if the last marker scrolled out of the
   * slice; one that failed keeps the bar where it stopped, which is where the log says to look. */
  if(out.finished && out.ok) out.progress = Object.assign({}, p, { stage: 'done', percent: 100 });
  return out;
}

async function start(opts){
  const o = opts && typeof opts === 'object' ? opts : {};
  const about = info();
  if(!about.live) throw new Error('This is not a live session — there is nothing to install from.');
  if(!about.installer) throw new Error('The PosterChanOS installer (gentoo.sh) is not on this medium.');
  recover();
  if(job.running || job.launching) throw new Error('the installer is already running');

  const mode = o.mode === 'resume' ? 'resume' : 'fresh';
  const rootName = String(o.rootName || 'gentoo');
  if(!ROOT_NAME.test(rootName)) throw new Error('The system volume name may use letters, digits, - and _ (up to 32).');
  const password = String(o.password == null ? '' : o.password);
  if(!password) throw new Error('Choose a disk encryption password.');
  if(/[\r\n\0]/.test(password)) throw new Error('The password cannot contain a line break.');

  /* RE-READ THE DISKS NOW. The list the person chose from may be minutes old; a USB stick pulled
   * and another plugged in can reuse a name. The disk must still exist, still be allowed, and
   * still be the size they were shown. */
  const want = String(o.disk || '').replace(/^\/dev\//, '');
  const d = (await disks()).find(x => x.name === want);
  if(!d) throw new Error('That disk is no longer attached. Go back and choose again.');
  if(!d.selectable) throw new Error(d.why || 'That disk cannot be installed to.');
  if(o.size != null && Number(o.size) !== d.size) throw new Error('That disk changed since it was chosen. Go back and choose again.');
  if(mode === 'resume' && !d.posterchanLayout) throw new Error('There is no earlier PosterChanOS install on that disk to resume.');

  const token = crypto.randomBytes(18).toString('hex');
  acquireLock(token);
  job = { kind: 'install', running: false, launching: true, ok: false,
          message: 'Starting the installer…', started: Date.now(), finished: 0, token, pid: 0,
          procStart: '', exitCode: null, disk: d.path, mode, model: d.model, size: d.size };
  save();
  try{ if(fs.readFileSync(LOCK_FILE, 'utf8') !== token) throw new Error('installer launch ownership changed'); }
  catch(e){ job.launching = false; job.finished = Date.now(); job.message = String(e.message || e); save(); throw e; }
  fs.writeFileSync(LOG_FILE, '', { mode: 0o600 });
  try{ forgetSecret(); fs.writeFileSync(SECRET_FILE, password, { mode: 0o600, flag: 'wx' }); }
  catch(e){ job.launching = false; job.finished = Date.now(); job.message = 'could not hand the password to the installer';
            save(); try{ fs.unlinkSync(LOCK_FILE); }catch(_){} throw new Error(job.message); }

  const args = ['-n', 'env', 'PC_INSTALL_PROGRESS=1', 'PC_INSTALL_DISK=' + d.name,
                'PC_INSTALL_ROOT_NAME=' + rootName, 'PC_INSTALL_MODE=' + mode,
                'PC_INSTALL_PASSWORD_FILE=' + SECRET_FILE];
  /* The erase is only ever pre-answered for a FRESH install, which the wizard asked the person to
   * confirm by typing the disk's name. A resume never erases, so it never carries the yes. */
  if(mode === 'fresh') args.push('PC_ASSUME_YES=1');
  args.push(about.installer, 'install-live');

  const spec = { state: STATE_FILE, log: LOG_FILE, lock: LOCK_FILE, token, kind: 'install', bin: SUDO, args, env: {},
                 cleanup: [SECRET_FILE] };
  let p;
  try{
    /* The token in the clear too: alive() identifies the supervisor by finding it in
     * /proc/<pid>/cmdline, where the base64 spec does not contain it (see liveusb.js). */
    p = spawn(process.execPath, [RUNNER, Buffer.from(JSON.stringify(spec)).toString('base64'), token], {
      env: Object.assign({}, process.env, { ELECTRON_RUN_AS_NODE: '1' }), stdio: 'ignore', detached: true });
  }catch(e){
    forgetSecret(); try{ fs.unlinkSync(LOCK_FILE); }catch(_){}
    job.launching = false; job.finished = Date.now(); job.message = String(e.message || e); save(); throw e;
  }
  job.pid = p.pid; job.procStart = procStart(p.pid); job.running = true; job.launching = false;
  job.message = 'Installing PosterChanOS…'; save(); p.unref();
  return status();
}

module.exports = { info, disks, start, status, progress, cleanLog, liveDiskNames, STAGES };

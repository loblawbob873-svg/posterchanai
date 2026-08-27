/* THE COMPUTER'S OWN FILES, for the Files screen.
 *
 * Files already browses two things: the encrypted drive on Blossom, and a synced folder's manifest.
 * On PosterChanOS there is a third and it is the obvious one — the disk you are sitting in front
 * of. "Blossom file manager to also support the host os so it can be a file manager too."
 *
 * WHAT LIMITS THIS IS THE UNIX ACCOUNT, AND SAYING SO IS THE HONEST THING. There is no path
 * allowlist here and adding one would be theatre: the Terminal on this same desktop is a real PTY
 * running as the session user, so anything this bridge could reach is already reachable by typing
 * `ls`. A file manager is strictly less capability than a shell. What keeps one person's files from
 * another's is what has always kept them apart — every signed-in identity gets its own account with
 * a 0700 home, and this runs as that account.
 *
 * That is also why every handler is behind the same `fsGuard` as the rest: the boundary being
 * enforced is "our own page, not some other page", not "this directory, not that one".
 *
 * DELETION GOES TO THE FREEDESKTOP TRASH, not to unlink. A file manager whose delete is permanent
 * on the first click is one people lose things to, and ~/.local/share/Trash is the same bin every
 * other application on the machine uses — so a mistake here is undone with the tools they already
 * have, rather than with a private trash only this app knows about.
 *
 * The pure half — path handling, entry shaping, the trashinfo record — is exported for
 * tests/test_host_fs.py, which runs it under node against a real temporary directory.
 */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn, execFile } = require('child_process');

/* AN ABSOLUTE, NORMALISED PATH OR NOTHING. `..` is resolved rather than rejected — a file manager
 * navigates upwards constantly and refusing the segment would break the parent button — but the
 * result must still be absolute, so a relative path cannot quietly become one relative to wherever
 * the Electron process happens to have been started. */
function clean(p) {
  const s = String(p == null ? '' : p);
  if (!s) return '';
  const abs = path.resolve(s.startsWith('~') ? path.join(os.homedir(), s.slice(1)) : s);
  return path.isAbsolute(abs) ? abs : '';
}

/** The parent of `p`, or null at the root — which is where the "up" button must stop. */
function parentOf(p) {
  const c = clean(p);
  if (!c) return null;
  const up = path.dirname(c);
  return up === c ? null : up;
}

/* One directory entry, in the shape the explorer's comparator already understands. Keep birthtime
 * separately: list mode calls this “Date created”, and showing mtime there makes an old file look
 * newly created merely because it was edited. Filesystems without birth time legitimately fall
 * back to mtime in the client. */
function shape(dir, name, st, lst) {
  const isLink = !!(lst && lst.isSymbolicLink());
  const s = st || lst;
  return {
    name,
    path: path.join(dir, name),
    dir: !!(s && s.isDirectory()),
    /* A DIRECTORY'S `size` IS NOT ITS CONTENTS — it is the size of the directory record, which is
     * 4096 on most filesystems and means nothing to anybody. Reported as 0 so a sort by size does
     * not interleave every folder in the middle of the files. */
    size: (s && s.isDirectory()) ? 0 : Number((s && s.size) || 0),
    mtime: Number((s && s.mtimeMs) || 0),
    created: Number((s && s.birthtimeMs) || 0),
    link: isLink,
    /* Dotfiles are marked, not dropped: the explorer decides whether to show them, the same way
     * every file manager has a switch for it. */
    hidden: name.startsWith('.'),
    /* A symlink whose target is gone still has a name and still has to be shown — deleting it is
     * usually why somebody is looking at it. `broken` is what stops it being reported as a file
     * of size 0 with no explanation. */
    broken: isLink && !st,
  };
}

/** What is in `dir`. Throws for a path that cannot be read — "I could not ask" is not "it is empty". */
function list(dir) {
  const d = clean(dir);
  if (!d) throw new Error('not a path');
  const names = fs.readdirSync(d);
  const entries = [];
  for (const name of names) {
    const full = path.join(d, name);
    let lst = null, st = null;
    try { lst = fs.lstatSync(full); } catch (_) { continue; }   // vanished between readdir and stat
    /* `stat` FOLLOWS the link and `lstat` does not, and both are needed: the first says whether it
     * behaves as a folder, the second says it is a link at all. A dangling one throws on stat,
     * which is the `broken` flag rather than a reason to drop the row. */
    try { st = fs.statSync(full); } catch (_) { st = null; }
    entries.push(shape(d, name, st, lst));
  }
  return { path: d, parent: parentOf(d), entries };
}

/* THE PLACES WORTH STARTING FROM. Home first because it is where somebody's own files are; then the
 * real mount points, because "where is my USB stick" is most of what a file manager is asked. `/`
 * is last and is deliberately offered — this is the machine's own file manager and hiding its root
 * would be pretending. */
function roots(env) {
  const e = env || process.env;
  const home = e.HOME || os.homedir();
  const out = [{ name: 'Home', path: home, kind: 'home' }];
  const seen = new Set([home]);
  const user = e.USER || e.LOGNAME || path.basename(home);
  for (const base of ['/run/media/' + user, '/media/' + user, '/media', '/mnt']) {
    let names = [];
    try { names = fs.readdirSync(base); } catch (_) { continue; }
    for (const n of names) {
      const p = path.join(base, n);
      if (seen.has(p)) continue;
      try { if (!fs.statSync(p).isDirectory()) continue; } catch (_) { continue; }
      seen.add(p);
      out.push({ name: n, path: p, kind: 'mount' });
    }
  }
  out.push({ name: 'This computer', path: '/', kind: 'root' });
  return out;
}

/* THE FREEDESKTOP TRASH RECORD. A file in the bin without one of these is a file every other trash
 * tool on the machine refuses to restore — it no longer knows where it came from — so the record is
 * written FIRST and the move only happens if it landed. The date is local time with no zone, which
 * is what the spec asks for and is not what toISOString() produces. */
function trashInfo(original, when) {
  const d = when || new Date();
  const p = (n) => String(n).padStart(2, '0');
  const stamp = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
              + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  /* The path is URL-encoded per the spec, with `/` left alone. */
  const enc = String(original).split('/').map(encodeURIComponent).join('/');
  return '[Trash Info]\nPath=' + enc + '\nDeletionDate=' + stamp + '\n';
}

/** A name that is free in `dir` — `notes.txt`, then `notes.2.txt`, and so on. */
function freeName(dir, name, exists) {
  const has = exists || ((p) => { try { fs.lstatSync(p); return true; } catch (_) { return false; } });
  if (!has(path.join(dir, name))) return name;
  const ext = path.extname(name), stem = name.slice(0, name.length - ext.length);
  for (let i = 2; i < 10000; i++) {
    const n = stem + '.' + i + ext;
    if (!has(path.join(dir, n))) return n;
  }
  throw new Error('nowhere to put it');
}

function trashDir(env) {
  const e = env || process.env;
  const base = e.XDG_DATA_HOME || path.join(e.HOME || os.homedir(), '.local', 'share');
  return path.join(base, 'Trash');
}

/** Move `target` to the desktop trash. Returns where it went, so an undo is possible. */
function trash(target, env) {
  const t = clean(target);
  if (!t) throw new Error('not a path');
  if (!parentOf(t)) throw new Error('refusing to delete the root of the filesystem');
  fs.lstatSync(t);                                   // throws if it is not there — say so
  const base = trashDir(env);
  const files = path.join(base, 'files'), info = path.join(base, 'info');
  fs.mkdirSync(files, { recursive: true });
  fs.mkdirSync(info, { recursive: true });
  const name = freeName(files, path.basename(t));
  /* The record BEFORE the move: a file in the bin with no record is one nothing can restore, and
   * writing it afterwards leaves exactly that whenever the process is interrupted between the two. */
  fs.writeFileSync(path.join(info, name + '.trashinfo'), trashInfo(t), { flag: 'wx' });
  try {
    fs.renameSync(t, path.join(files, name));
  } catch (err) {
    /* ACROSS A FILESYSTEM, rename fails with EXDEV and the trash is on the home volume — so a file
     * on a USB stick cannot be moved into it. Copy-then-remove is the only way, and the record is
     * withdrawn if even that fails, rather than left pointing at nothing. */
    if (err && err.code === 'EXDEV') {
      try {
        fs.cpSync(t, path.join(files, name), { recursive: true });
        fs.rmSync(t, { recursive: true, force: true });
      } catch (e2) {
        try { fs.rmSync(path.join(info, name + '.trashinfo'), { force: true }); } catch (_) {}
        throw e2;
      }
    } else {
      try { fs.rmSync(path.join(info, name + '.trashinfo'), { force: true }); } catch (_) {}
      throw err;
    }
  }
  return { trashed: path.join(files, name), from: t };
}

function mkdir(dir, name) {
  const d = clean(dir);
  if (!d) throw new Error('not a path');
  const n = String(name || '').trim();
  /* A NAME, NOT A PATH. `../../etc` in a "new folder" box is a directory created somewhere nobody
   * asked for; the separator is refused rather than resolved. */
  if (!n || n === '.' || n === '..' || n.includes('/')) throw new Error('that is not a folder name');
  const p = path.join(d, n);
  fs.mkdirSync(p);
  return { path: p };
}

function rename(from, to) {
  const f = clean(from);
  if (!f) throw new Error('not a path');
  const leaf = String(to || '').trim();
  if (!leaf || leaf === '.' || leaf === '..' || leaf.includes('/'))
    throw new Error('that is not a name');
  const p = path.join(path.dirname(f), leaf);
  /* `rename` OVERWRITES silently on POSIX, and in a file manager that is somebody's other file
   * gone with no dialog and no undo. Checked first, and the race that remains is narrower than the
   * one where it is not checked at all. */
  try { fs.lstatSync(p); throw new Error('there is already something called that'); }
  catch (e) { if (e && e.message === 'there is already something called that') throw e; }
  fs.renameSync(f, p);
  return { path: p };
}

/* COPY/MOVE INTO A DIRECTORY. Refuse collisions instead of silently replacing data. `fs.cpSync`
 * handles directory trees; a cross-device move falls back to copy + remove, just like trash(). */
function transfer(items, destination, move) {
  const dest = clean(destination);
  if (!dest || !fs.statSync(dest).isDirectory()) throw new Error('destination is not a folder');
  const sources = Array.from(new Set((items || []).map(clean).filter(Boolean)));
  if (!sources.length) throw new Error('nothing selected');
  const planned = sources.map(from => {
    fs.lstatSync(from);
    const to = path.join(dest, path.basename(from));
    if (to === from) throw new Error('source and destination are the same');
    if (dest === from || dest.startsWith(from + path.sep))
      throw new Error('a folder cannot be copied inside itself');
    try { fs.lstatSync(to); throw new Error('there is already something called ' + path.basename(to)); }
    catch (e) { if (e && /^there is already/.test(e.message || '')) throw e; }
    return { from, to };
  });
  const done = [];
  for (const x of planned) {
    if (!move) fs.cpSync(x.from, x.to, { recursive: true, errorOnExist: true, force: false });
    else {
      try { fs.renameSync(x.from, x.to); }
      catch (e) {
        if (!e || e.code !== 'EXDEV') throw e;
        fs.cpSync(x.from, x.to, { recursive: true, errorOnExist: true, force: false });
        fs.rmSync(x.from, { recursive: true });
      }
    }
    done.push(x);
  }
  return { moved: !!move, items: done };
}

/* OPENING A FILE IS THE MACHINE'S JOB, not ours. xdg-open consults the same associations every
 * other application does, so a PDF opens in whatever this person set as their PDF reader — and the
 * window it makes is adopted into a PosterChan frame like any other, because it is an ordinary
 * compositor client. Detached, or the viewer dies with the desktop. */
function open(target) {
  const t = clean(target);
  if (!t) throw new Error('not a path');
  fs.lstatSync(t);
  const child = spawn('xdg-open', [t], { detached: true, stdio: 'ignore' });
  let onFail = null;
  const failed = new Promise((res) => { onFail = res; });
  child.on('error', (e) => onFail(e && e.code === 'ENOENT'
    ? 'this machine has no xdg-open, so nothing knows what opens that'
    : String((e && e.message) || e)));
  child.unref();
  return { pid: child.pid, failed };
}

/* THE MACHINE'S OWN FILES, FOR A SEARCH BOX — and every bound here exists because the thing being
 * searched is somebody's whole disk.
 *
 * BREADTH-FIRST, not depth-first. A depth-first walk disappears into the first deep directory it
 * meets (node_modules, a git object store, a photo library by year) and spends the entire budget
 * there, so the answer depends on which folder happens to sort first. Breadth-first spends it near
 * the top, which is where the file somebody is searching for by name almost always is.
 *
 * FOUR SEPARATE BOUNDS, because each one alone has a case that defeats it: a DEADLINE (a spinning
 * disk or a stale network mount can make one readdir take seconds), a RESULT cap (a menu shows a
 * handful), a SCAN cap (a flat directory of 200,000 files is one readdir and no recursion at all),
 * and a DEPTH cap. Hitting any of them returns what was found rather than failing — a partial
 * answer to "find me a file" is the normal answer.
 *
 * Dotfiles are skipped, which also takes .git, .cache and .config out of the walk. Symlinks are not
 * followed: a link back up the tree is an infinite walk, and the deadline would be the only thing
 * that noticed.
 */
function search(query, opts) {
  const q = String(query || '').trim().toLowerCase();
  if (q.length < 2) return [];                 // one character matches a whole disk
  const o = opts || {};
  const limit = Math.max(1, Math.min(Number(o.limit) || 8, 50));
  const until = Date.now() + Math.max(50, Math.min(Number(o.ms) || 350, 3000));
  const maxDepth = Math.max(1, Math.min(Number(o.depth) || 4, 8));
  const maxScan = 40000;
  const out = [];
  let scanned = 0;
  const queue = (Array.isArray(o.roots) && o.roots.length ? o.roots : roots(o.env).map(r => r.path))
                  .map(p => ({ p: p, d: 0 }));
  while (queue.length && out.length < limit && scanned < maxScan && Date.now() < until) {
    const cur = queue.shift();
    let ents = [];
    try { ents = fs.readdirSync(cur.p, { withFileTypes: true }); } catch (_) { continue; }
    for (const e of ents) {
      if (out.length >= limit) break;
      if (++scanned > maxScan) break;
      const nm = e.name;
      if (!nm || nm.charAt(0) === '.') continue;
      if (e.isSymbolicLink()) continue;
      const full = path.join(cur.p, nm);
      if (nm.toLowerCase().indexOf(q) >= 0) out.push({ name: nm, path: full, dir: e.isDirectory() });
      if (e.isDirectory() && cur.d < maxDepth) queue.push({ p: full, d: cur.d + 1 });
    }
  }
  return out;
}

/* READING AND WRITING ONE FILE'S CONTENTS — what PosterChan Code needs to edit a file on THIS
 * computer, and the one thing this bridge could not do. Browsing, moving and trashing were all
 * here; opening a file meant handing it to the OS.
 *
 * NO PATH ALLOWLIST, for the reason at the top of this file: this is the person's own machine and
 * their own session. What IS enforced is that a text editor only ever sees text — a size ceiling
 * and a NUL-byte check, so a 4 GB disk image or a JPEG cannot be loaded into a buffer and saved
 * back mangled. The same two rules the drive and the synced-folder openers apply, made here as well
 * because a bridge must not trust its caller.
 */
const TEXT_MAX = 2 * 1024 * 1024;

function readText(p){
  const abs = path.resolve(String(p || ''));
  const st = fs.statSync(abs);
  if(st.isDirectory()) throw new Error('that is a folder');
  if(st.size > TEXT_MAX) throw new Error('that file is too big to edit here');
  const buf = fs.readFileSync(abs);
  if(buf.includes(0)) throw new Error('that looks like a binary file');
  return { path: abs, text: buf.toString('utf8'), size: st.size, mtime: Math.round(st.mtimeMs) };
}

/* WRITTEN THROUGH A TEMPORARY FILE IN THE SAME DIRECTORY, then renamed over the original — a rename
 * within one filesystem is atomic, so a crash or a full disk leaves the OLD file intact rather than
 * a truncated one. Writing in place is how an editor destroys the thing it was editing.
 *
 * `mtime` is a compare-and-swap: if the file changed since it was opened the write is refused, and
 * the editor says so. A terminal sitting beside the editor is the likeliest thing to have changed it.
 */
function writeText(p, text, mtime){
  const abs = path.resolve(String(p || ''));
  if(mtime){
    let cur = 0;
    try{ cur = Math.round(fs.statSync(abs).mtimeMs); }catch(_){ cur = 0; }
    if(cur && Math.abs(cur - Number(mtime)) > 1000) throw new Error('changed-on-disk');
  }
  const data = Buffer.from(String(text == null ? '' : text), 'utf8');
  if(data.length > TEXT_MAX) throw new Error('that file is too big to save here');
  const tmp = path.join(path.dirname(abs), '.pc-' + path.basename(abs) + '.tmp');
  fs.writeFileSync(tmp, data);
  try{ fs.renameSync(tmp, abs); }
  catch(e){ try{ fs.unlinkSync(tmp); }catch(_){} throw e; }
  const st = fs.statSync(abs);
  return { path: abs, size: st.size, mtime: Math.round(st.mtimeMs) };
}

function _git(root, args, timeout){
  const cwd=clean(root);if(!cwd||!fs.statSync(cwd).isDirectory())return Promise.reject(new Error('not a project folder'));
  return new Promise((resolve,reject)=>execFile('git',['-C',cwd].concat(args),{
    encoding:'utf8',maxBuffer:8*1024*1024,timeout:timeout||30000,env:Object.assign({},process.env,{GIT_TERMINAL_PROMPT:'0'})
  },(e,stdout,stderr)=>e?reject(new Error(String(stdout||stderr||e.message||e).trim())):resolve(stdout)));
}
function _gitPath(p){
  p=String(p||'');
  if(!p||p.startsWith('-')||path.isAbsolute(p)||p.split(/[\\/]/).includes('..'))throw new Error('invalid Git path');
  return p;
}
async function gitStatus(root){
  const top=(await _git(root,['rev-parse','--show-toplevel'])).trim();
  const branch=(await _git(top,['branch','--show-current'])).trim()||'detached HEAD';
  let origin='';try{origin=(await _git(top,['remote','get-url','origin'])).trim();}catch(_){}
  const raw=await _git(top,['status','--porcelain=v1','-z','--untracked-files=all']);
  const records=raw.split('\0').filter(Boolean),files=[];
  for(let i=0;i<records.length;i++){
    const rec=records[i],xy=rec.slice(0,2);let p=rec.slice(3);
    if((xy[0]==='R'||xy[0]==='C')&&records[i+1])p=records[++i];
    files.push({xy,path:p});
  }
  return {root:top,branch,origin,nostr:/^nostr:/i.test(origin),files};
}
async function gitDiff(root,p){
  p=_gitPath(p);let out=await _git(root,['diff','--',p]);
  if(!out)out=await _git(root,['diff','--cached','--',p]);
  if(!out){try{out=await _git(root,['diff','--no-index','--','/dev/null',p]);}catch(e){out=String(e.message||'');}}
  return {diff:out};
}
async function gitAction(root,action,paths,message){
  const ps=(paths||[]).map(_gitPath);
  if(action==='stage')await _git(root,['add','--'].concat(ps));
  else if(action==='unstage')await _git(root,['restore','--staged','--'].concat(ps));
  else if(action==='restore')for(const p of ps){
    let tracked=true;try{await _git(root,['ls-files','--error-unmatch','--',p]);}catch(_){tracked=false;}
    /* “Discard changes” means the same thing in the native desktop and the server API: restore
     * both the index and working tree from HEAD. Restoring only --worktree leaves a staged edit in
     * Source Control, so the destructive action reports success while the file remains modified. */
    if(tracked)await _git(root,['restore','--staged','--worktree','--',p]);
    else {const top=(await _git(root,['rev-parse','--show-toplevel'])).trim(),abs=path.resolve(top,p);
      if(abs!==top&&!abs.startsWith(top+path.sep))throw new Error('invalid Git path');
      fs.rmSync(abs,{recursive:true,force:true});}
  }
  else if(action==='commit'){if(!String(message||'').trim())throw new Error('write a commit message');await _git(root,['commit','-m',String(message).slice(0,5000)]);}
  else if(action==='pull'||action==='push')await _git(root,[action],120000);
  else throw new Error('unknown Git action');
  return {ok:true};
}

module.exports = { list, roots, search, trash, mkdir, rename, transfer, open, clean, parentOf, shape,
  readText, writeText, gitStatus, gitDiff, gitAction,
                   trashInfo, freeName, trashDir };

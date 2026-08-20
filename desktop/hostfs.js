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
const { spawn } = require('child_process');

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

/* One directory entry, in the shape the explorer's comparator already understands: a name, a size,
 * a modified time and whether it is a folder. */
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

module.exports = { list, roots, trash, mkdir, rename, open, clean, parentOf, shape,
                   trashInfo, freeName, trashDir };

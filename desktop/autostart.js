'use strict';
/* STARTUP APPS -- "Users need a way to be able to define startup programs for posterchanOS".
 *
 * THE FORMAT IS THE FREEDESKTOP ONE: a .desktop file in ~/.config/autostart. Not a list in our own
 * settings, because that is where every Linux program that offers "start at login" already writes
 * (Steam, Discord, Syncthing…) -- and on PosterChanOS nothing ever read it, since Wayfire does not
 * implement the autostart spec. So a box ticked inside one of those programs did nothing either.
 * Reading the standard directory makes both work, and an entry made here moves with the home
 * directory to any other desktop.
 *
 * ONLY THE USER'S OWN DIRECTORY RUNS. /etc/xdg/autostart is the distribution's, and on a Gentoo box it
 * holds daemons for desktops this is not (a GNOME keyring, an at-spi bus, an applet for a tray that
 * does not exist). Starting those because they are present would change the machine without anybody
 * having asked; the user's directory is exactly the set somebody chose.
 *
 * NEVER THE DESKTOP ITSELF. background.js writes `posterchanai.desktop` here for the desktop APP on
 * other distros; on PosterChanOS that program IS the shell, and launching it again from the shell's
 * own autostart would open a second desktop on top of the first.
 *
 * ONCE PER LOGIN. The shell restarts (an update, a recovery); a marker in XDG_RUNTIME_DIR -- a tmpfs
 * that is emptied at logout -- keeps a restart from launching every startup app a second time.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const apps = require('./apps.js');

const SELF = new Set(['posterchanai.desktop', 'posterchan.desktop', 'posterchan-desktop.desktop']);
const MARK = 'posterchan-autostart.done';

function dir(env) {
  const e = env || process.env;
  return path.join(e.XDG_CONFIG_HOME || path.join(e.HOME || os.homedir(), '.config'), 'autostart');
}

const truthy = (v) => String(v || '').toLowerCase() === 'true';
const falsy = (v) => String(v || '').toLowerCase() === 'false';

/** Is this entry switched on? Hidden=true is the spec's "deleted"; the GNOME key is what every
 *  "start at login" checkbox in the wild actually toggles. */
function enabledOf(e) {
  return !truthy(e.Hidden) && !falsy(e['X-GNOME-Autostart-enabled']);
}

/** Every startup app in the user's directory, sorted by name. */
function list(env) {
  const d = dir(env);
  let files = [];
  try { files = fs.readdirSync(d).filter((f) => /\.desktop$/i.test(f)); } catch (_) { return []; }
  const out = [];
  for (const file of files) {
    if (SELF.has(file)) continue;
    let e;
    try { e = apps.parseEntry(fs.readFileSync(path.join(d, file), 'utf8')); } catch (_) { continue; }
    if ((e.Type || 'Application') !== 'Application' || !e.Exec) continue;
    out.push({ id: file, name: e.Name || file.replace(/\.desktop$/i, ''), exec: e.Exec, icon: e.Icon || '',
               terminal: truthy(e.Terminal), enabled: enabledOf(e) });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

/* A file name from a label: what a person can recognise in the directory, never a path. */
function fileFor(name, d) {
  const base = String(name || 'startup').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'startup';
  let file = base + '.desktop', n = 2;
  while (SELF.has(file) || fs.existsSync(path.join(d, file))) file = base + '-' + (n++) + '.desktop';
  return file;
}

/* Values are written on one line each; a newline in a name would start a new key. */
const one = (v) => String(v == null ? '' : v).replace(/[\r\n]+/g, ' ').trim();

function write(file, fields, env) {
  const d = dir(env);
  fs.mkdirSync(d, { recursive: true });
  const lines = ['[Desktop Entry]', 'Type=Application'];
  for (const [k, v] of Object.entries(fields)) if (v !== '' && v != null) lines.push(k + '=' + one(v));
  fs.writeFileSync(path.join(d, file), lines.join('\n') + '\n', { mode: 0o644 });
  return file;
}

/** A startup app from a command somebody typed. */
function add(spec, env) {
  const s = spec || {};
  const exec = one(s.exec);
  if (!exec || !apps.execArgv(exec).length) throw new Error('type the command to run');
  const name = one(s.name) || apps.execArgv(exec)[0].split('/').pop();
  const d = dir(env);
  return write(fileFor(name, d), { Name: name, Exec: exec, Icon: one(s.icon), Terminal: s.terminal ? 'true' : '',
                                   'X-GNOME-Autostart-enabled': 'true', 'X-PosterChan-Added': 'true' }, env);
}

/** A startup app from an installed program, by its desktop-file id (what the start menu lists). */
function addApp(appId, env) {
  const id = String(appId || '');
  const app = id && apps.scan({ env: env || process.env }).apps.find((x) => x.id === id);
  if (!app) throw new Error('that app is not installed');
  let e = {};
  try { e = apps.parseEntry(fs.readFileSync(app.path, 'utf8')); } catch (_) {}
  const exec = e.Exec || app.argv.join(' ');
  const already = list(env).find((x) => x.exec === exec);
  if (already) return already.id;
  return write(fileFor(app.name || id, dir(env)), { Name: app.name || id, Exec: exec, Icon: app.icon || '',
               Terminal: app.terminal ? 'true' : '', 'X-GNOME-Autostart-enabled': 'true',
               'X-PosterChan-Added': 'true' }, env);
}

function safeId(id) {
  const f = String(id || '');
  if (!/^[\w.+-]+\.desktop$/.test(f) || SELF.has(f)) throw new Error('not a startup app');
  return f;
}

/** Switch one on or off. Rewrites only the two keys that decide it; everything else is kept. */
function setEnabled(id, on, env) {
  const file = path.join(dir(env), safeId(id));
  const text = fs.readFileSync(file, 'utf8');
  const keep = text.split(/\r?\n/).filter((l) => !/^\s*(Hidden|X-GNOME-Autostart-enabled)\s*=/.test(l));
  const at = keep.findIndex((l) => l.trim() === '[Desktop Entry]');
  keep.splice(at < 0 ? keep.length : at + 1, 0, 'X-GNOME-Autostart-enabled=' + (on ? 'true' : 'false'));
  fs.writeFileSync(file, keep.join('\n').replace(/\n*$/, '\n'));
  return { id, enabled: !!on };
}

function remove(id, env) {
  fs.unlinkSync(path.join(dir(env), safeId(id)));
  return { id };
}

/** What to start: argv per enabled entry, honouring OnlyShowIn/NotShowIn/TryExec. Daemons are FINE
 *  here (unlike the start menu) -- a startup entry is precisely where a background program belongs. */
function toRun(env) {
  const e = env || process.env;
  const out = [];
  for (const item of list(e)) {
    if (!item.enabled) continue;
    let entry;
    try { entry = apps.parseEntry(fs.readFileSync(path.join(dir(e), item.id), 'utf8')); } catch (_) { continue; }
    const m = apps.menuable(Object.assign({}, entry, { NoDisplay: '', Exec: entry.Exec.replace(/\s--?(server|daemon|no-fork)\b/gi, '') }),
                            { tryExecMissing: entry.TryExec && !apps.onPath(entry.TryExec, e) });
    if (!m.ok) continue;
    const argv = apps.execArgv(entry.Exec);
    // A program that is not on this machine is skipped, as the start menu skips it: launching it only
    // writes an error nobody is there to read.
    if (argv.length && apps.onPath(argv[0], e)) out.push({ id: item.id, name: item.name, argv, terminal: item.terminal });
  }
  return out;
}

/** Start every enabled entry, once per login. `launch(argv, item)` is the shell's own launcher. */
async function runOnce(launch, env, log) {
  const e = env || process.env;
  const say = log || ((m) => { try { console.log('[autostart] ' + m); } catch (_) {} });
  const runtime = e.XDG_RUNTIME_DIR;
  if (runtime) {
    const mark = path.join(runtime, MARK);
    if (fs.existsSync(mark)) { say('already ran this login'); return []; }
    try { fs.writeFileSync(mark, String(Date.now())); } catch (_) {}
  }
  const started = [];
  for (const item of toRun(e)) {
    try {
      const r = await launch(item.argv, item);
      const why = r && r.why;
      say(item.name + (why ? ' did not start: ' + why : ' started'));
      started.push({ id: item.id, ok: !why, why: why || '' });
    } catch (err) {
      say(item.name + ' did not start: ' + ((err && err.message) || err));
      started.push({ id: item.id, ok: false, why: String((err && err.message) || err) });
    }
  }
  return started;
}

module.exports = { dir, list, add, addApp, setEnabled, remove, toRun, runOnce, SELF, MARK };

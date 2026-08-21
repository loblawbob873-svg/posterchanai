/* EVERY APP INSTALLED ON THIS MACHINE, for the start menu.
 *
 * The launcher used to offer a fixed list of three, with a comment arguing that a menu scraped from
 * /usr/share/applications is the thing PosterChanOS exists not to be. That argument was about the
 * MENU being unusable, and it is answered by filtering rather than by refusing to look: a desktop
 * you cannot start your own programs from is not a desktop, and "should be able to manage/open any
 * game/app under PosterChan Desktop" is the requirement.
 *
 * WHAT MAKES THIS UNUSABLE IF YOU GET IT WRONG, measured on the test laptop's 19 entries — of which
 * only FIVE belong in a menu. The rest are `foot-server`, `footclient`, `gcr-prompter`,
 * `gcr-viewer`, three `*-geo-handler`s, `org.freedesktop.Xwayland`, `org.gnupg.pinentry-qt`,
 * `xdg-desktop-portal-gtk` and `cups`: URL handlers, D-Bus activation stubs, password prompters and
 * a print-queue page. Every one of them is marked in its own file — `NoDisplay`, `Hidden`, a
 * `Type` that is not Application — and a scan that ignores those markings produces exactly the
 * ninety-entry menu the original comment was afraid of. The spec is the filter.
 *
 * The parsing half is DOM-free and filesystem-free so tests/test_desktop_apps.py can run it under
 * node against real .desktop files; `scan()` is the only part that touches a disk.
 */
'use strict';
const fs = require('fs');
const path = require('path');

/* This desktop's names, for OnlyShowIn/NotShowIn. `wlroots` and `sway` because that is what the
 * compositor is; `PosterChan` because that is what this is, and an entry may one day say so. */
const DESKTOP_NAMES = ['PosterChan', 'sway', 'wlroots'];

/* FIELD CODES ARE NOT ARGUMENTS. `Exec=firefox %u` means "firefox, and put a URL here if you have
 * one" — passed through verbatim, firefox opens a tab for a file literally named `%u`. They are
 * removed rather than substituted because a launcher click carries no file and no URL.
 * `%%` is an escaped percent and must survive. */
const FIELD_CODES = /(^|\s)%[fFuUdDnNickvm](?=\s|$)/g;

/** One .desktop file's [Desktop Entry] group, as a flat map. Other groups (actions) are ignored. */
function parseEntry(text) {
  const out = {};
  let inGroup = false;
  for (const raw of String(text == null ? '' : text).split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line[0] === '#') continue;
    if (line[0] === '[') {
      /* GROUPS, and only the first one counts. A file's "Desktop Action" groups carry their own
       * Name and Exec — read as part of the entry they overwrite the app's, so "Firefox" becomes
       * "Open a New Private Window" and the menu launches the wrong thing. */
      inGroup = line === '[Desktop Entry]';
      continue;
    }
    if (!inGroup) continue;
    const eq = line.indexOf('=');
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    const val = line.slice(eq + 1).trim();
    // A localized key is `Name[de]`. The plain one is kept; a locale-aware menu is a separate job
    // and picking the wrong locale's string is worse than showing the C one.
    if (key.indexOf('[') >= 0) continue;
    if (!(key in out)) out[key] = val;   // first wins, as the spec says of duplicate keys
  }
  return out;
}

const truthy = (v) => String(v || '').toLowerCase() === 'true';
const listOf = (v) => String(v || '').split(';').map((s) => s.trim()).filter(Boolean);

/* SHOULD THIS BE IN A MENU AT ALL?
 *
 * Answered from the file's own markings, never from a guess about its name. `why` is returned
 * rather than a bare false because a missing app is the hardest kind of bug to look at: "it is not
 * in the menu" and "it is not installed" look identical from the outside. */
function menuable(entry, opts) {
  const e = entry || {};
  const o = opts || {};
  if ((e.Type || 'Application') !== 'Application') return { ok: false, why: 'not an application' };
  if (truthy(e.NoDisplay)) return { ok: false, why: 'NoDisplay' };
  if (truthy(e.Hidden)) return { ok: false, why: 'Hidden' };
  if (!e.Name) return { ok: false, why: 'no Name' };
  if (!e.Exec) return { ok: false, why: 'nothing to run' };
  const names = o.desktops || DESKTOP_NAMES;
  const only = listOf(e.OnlyShowIn);
  if (only.length && !only.some((n) => names.includes(n)))
    return { ok: false, why: 'OnlyShowIn ' + only.join(',') };
  const not = listOf(e.NotShowIn);
  if (not.length && not.some((n) => names.includes(n)))
    return { ok: false, why: 'NotShowIn ' + not.join(',') };
  /* TryExec is the spec's own "is this actually installed" field, and it is the difference between
   * a menu of programs and a menu of packages somebody once had. Checked by the caller, which is
   * the half that can look at a disk. */
  if (o.tryExecMissing) return { ok: false, why: 'TryExec ' + e.TryExec + ' is not installed' };
  /* A DAEMON HAS NO WINDOW, EVER, and the spec has no field that says so.
   *
   * `foot-server.desktop` runs `foot --server`: a background process that draws nothing and waits
   * for clients. It passes every check above -- it is an Application, it is not NoDisplay, it is
   * installed -- so it reached the menu, and clicking it opened a frame that waited twenty seconds
   * for a window that was never coming and then reported "Foot Server did not open -- is it
   * installed?". It IS installed. That message sends somebody to look for a broken package.
   *
   * Matched on the ARGUMENT rather than the name, because the name is a label somebody translated
   * and the flag is the thing that makes it a daemon. Bounded to whole arguments so an app whose
   * path merely contains the word is untouched. */
  const argv = String(e.Exec || '').split(/\s+/);
  if (argv.some((a) => /^--?(server|daemon|no-fork)$/i.test(a)))
    return { ok: false, why: 'a daemon, not a window' };
  return { ok: true, why: '' };
}

/* Exec → argv, honouring the spec's quoting: double quotes group, and inside them a backslash
 * escapes `"`, `` ` ``, `$` and `\`. Done properly because the alternative is `split(' ')`, which
 * turns `/opt/My App/run` into two arguments and starts nothing. */
function execArgv(exec) {
  const s = String(exec == null ? '' : exec).replace(FIELD_CODES, '$1').trim();
  const argv = [];
  let cur = '', q = false, esc = false, has = false;
  for (const ch of s) {
    if (esc) { cur += ch; esc = false; continue; }
    if (q && ch === '\\') { esc = true; continue; }
    if (ch === '"') { q = !q; has = true; continue; }
    if (!q && /\s/.test(ch)) { if (has || cur) { argv.push(cur); cur = ''; has = false; } continue; }
    cur += ch;
  }
  if (has || cur) argv.push(cur);
  return argv.map((a) => a.replace(/%%/g, '%')).filter((a, i) => i === 0 || a !== '');
}

/* A STABLE ID. The desktop-file ID is its path under the applications directory with `/` → `-`,
 * which is what makes a user's own copy in ~/.local/share/applications REPLACE the system one
 * rather than appear beside it. Keyed on that, first directory wins, and the directories are in
 * XDG order (user before system) for exactly that reason. */
function entryId(dir, file) {
  const rel = path.relative(dir, file).split(path.sep).join('-');
  return rel.replace(/\.desktop$/i, '');
}

/* THE DIRECTORIES, in XDG order. XDG_DATA_HOME first (a user's own entries win), then
 * XDG_DATA_DIRS, then the two defaults the spec names — because a machine with neither variable set
 * still has /usr/share/applications, and reading the environment alone finds nothing on it. */
function appDirs(env) {
  const e = env || process.env;
  const home = e.XDG_DATA_HOME || (e.HOME ? path.join(e.HOME, '.local', 'share') : '');
  const dirs = e.XDG_DATA_DIRS ? e.XDG_DATA_DIRS.split(':').filter(Boolean)
                               : ['/usr/local/share', '/usr/share'];
  const all = (home ? [home] : []).concat(dirs, ['/usr/local/share', '/usr/share']);
  const seen = new Set(), out = [];
  for (const d of all) {
    const p = path.join(d, 'applications');
    if (seen.has(p)) continue;
    seen.add(p);
    out.push(p);
  }
  return out;
}

/** Is `cmd` runnable — an absolute path that exists, or a bare name on PATH? */
function onPath(cmd, env) {
  const c = String(cmd || '');
  if (!c) return false;
  const isX = (p) => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch (_) { return false; } };
  if (c.includes('/')) return isX(c);
  const paths = String((env || process.env).PATH || '/usr/bin:/bin').split(':').filter(Boolean);
  return paths.some((d) => isX(path.join(d, c)));
}

/* Categories, folded to the few a person navigates by. The spec's list is long and mostly
 * uninteresting to somebody looking for a program; GAMES is the one that was asked for by name. */
const CATEGORY = [
  ['Game', 'Games'], ['AudioVideo', 'Media'], ['Audio', 'Media'], ['Video', 'Media'],
  ['Graphics', 'Graphics'], ['Development', 'Development'], ['Office', 'Office'],
  ['Network', 'Internet'], ['System', 'System'], ['Settings', 'System'], ['Utility', 'Utilities'],
];
function groupOf(entry) {
  const cats = listOf((entry || {}).Categories);
  for (const [needle, name] of CATEGORY) if (cats.includes(needle)) return name;
  return 'Other';
}

/** Everything installed here that belongs in a menu. Sorted by name; ids are unique. */
/* ── THE APP'S OWN PICTURE ──────────────────────────────────────────────────────────────────────
 *
 * Every program found by this scan was drawn in the start menu with ONE generic glyph, because the
 * `Icon=` key is a THEME NAME (`firefox`, `btop`) and a web page cannot resolve one — that lookup
 * is a walk over icon directories on a disk, which only this side can do. So the menu listed
 * Firefox, OBS, mupdf, qemu and btop as five identical grey squares: "start menu ... missing icons".
 *
 * The result travels as a data: URI rather than a path. The renderer is on the `app://` origin and
 * cannot read `/usr/share/icons` — a `<img src="file:///…">` there is blocked, silently, which
 * would have produced the same blank square by a longer route.
 *
 * SIZE IS THE WHOLE RISK. These are inlined into a menu that repaints on every keystroke, so a
 * 1 MB PNG per app is a stutter you can feel. Preferred sizes are asked for smallest-usable-first
 * and anything past the cap is skipped rather than shrunk — there is no image decoder here, and a
 * missing icon costs one grey square while a slow menu costs the whole feature.
 */
const ICON_DIRS = (env) => {
  const home = (env || process.env).HOME || '';
  const dirs = [];
  if (home) dirs.push(path.join(home, '.local', 'share', 'icons'), path.join(home, '.icons'));
  const data = String((env || process.env).XDG_DATA_DIRS || '/usr/local/share:/usr/share');
  for (const d of data.split(':')) if (d) dirs.push(path.join(d, 'icons'));
  return dirs;
};
/* Biggest-of-the-sensible-sizes first: a start menu row draws at ~20px on a scaled desktop, and a
 * 16px source upscaled to that is mush. `scalable` (SVG) wins outright where it exists — it is one
 * small file that is sharp at every size. */
const ICON_SIZES = ['scalable', '128x128', '96x96', '64x64', '48x48', '256x256', '32x32'];
const ICON_EXT = { '.svg': 'image/svg+xml', '.png': 'image/png' };
const ICON_MAX = 96 * 1024;

/** Where an `Icon=` value actually lives on this disk, or '' — an absolute path is used as given. */
function iconFile(name, opts) {
  const o = opts || {};
  const env = o.env || process.env;
  const n = String(name || '').trim();
  if (!n) return '';
  const ok = (f) => { try { return fs.statSync(f).isFile() ? f : ''; } catch (_) { return ''; } };
  if (n.startsWith('/')) return ICON_EXT[path.extname(n).toLowerCase()] ? ok(n) : '';
  /* An `Icon=` that already carries an extension is still a NAME, not a path — the spec says to
   * strip it — but plenty of entries in the wild mean the pixmaps file, so both are tried. */
  const bare = n.replace(/\.(png|svg|xpm)$/i, '');
  const roots = o.dirs || ICON_DIRS(env);
  for (const root of roots) {
    let themes = [];
    try { themes = fs.readdirSync(root); } catch (_) { continue; }
    /* hicolor is the spec's fallback theme and is where almost everything installs. The rest are
     * tried after it rather than instead of it, so a half-installed theme cannot hide an icon. */
    themes = ['hicolor'].concat(themes.filter((t) => t !== 'hicolor'));
    for (const theme of themes) {
      for (const size of ICON_SIZES) {
        for (const ext of Object.keys(ICON_EXT)) {
          const f = ok(path.join(root, theme, size, 'apps', bare + ext));
          if (f) return f;
        }
      }
    }
  }
  for (const d of ['/usr/share/pixmaps', '/usr/local/share/pixmaps']) {
    for (const ext of Object.keys(ICON_EXT)) {
      const f = ok(path.join(d, bare + ext));
      if (f) return f;
    }
  }
  return '';
}

/** That file as a `data:` URI a renderer can actually paint, or '' when it is missing or too big. */
function iconDataUri(name, opts) {
  const f = iconFile(name, opts);
  if (!f) return '';
  const mime = ICON_EXT[path.extname(f).toLowerCase()];
  if (!mime) return '';
  let buf;
  try {
    if (fs.statSync(f).size > ICON_MAX) return '';
    buf = fs.readFileSync(f);
  } catch (_) { return ''; }
  return 'data:' + mime + ';base64,' + buf.toString('base64');
}

function scan(opts) {
  const o = opts || {};
  const env = o.env || process.env;
  const dirs = o.dirs || appDirs(env);
  const byId = new Map();
  const skipped = [];
  for (const dir of dirs) {
    let files = [];
    try { files = walk(dir, dir, 0); } catch (_) { continue; }
    for (const file of files) {
      const id = entryId(dir, file);
      if (byId.has(id)) continue;                 // an earlier directory already answered for it
      let text = '';
      try { text = fs.readFileSync(file, 'utf8'); } catch (_) { continue; }
      const e = parseEntry(text);
      const missing = !!(e.TryExec && !onPath(e.TryExec, env));
      const v = menuable(e, { desktops: o.desktops, tryExecMissing: missing });
      if (!v.ok) { skipped.push({ id, why: v.why }); byId.set(id, null); continue; }
      const argv = execArgv(e.Exec);
      /* An Exec whose PROGRAM is not on this machine is a menu entry that can only disappoint.
       * Checked here as well as via TryExec, because most files do not carry a TryExec at all. */
      if (!argv.length || !onPath(argv[0], env)) {
        skipped.push({ id, why: (argv[0] || 'nothing') + ' is not installed' });
        byId.set(id, null);
        continue;
      }
      byId.set(id, {
        id, name: e.Name, comment: e.Comment || '', argv,
        icon: e.Icon || '', group: groupOf(e),
        terminal: truthy(e.Terminal),
        /* What the app's WINDOW will be called, so the launcher can focus one that is already open
         * instead of starting a second. StartupWMClass is the entry's own answer; the program's
         * basename is the usual fallback and is what matches for most toolkits. */
        match: e.StartupWMClass || path.basename(argv[0]),
        path: file,
      });
    }
  }
  const apps = [...byId.values()].filter(Boolean)
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
  return { apps, skipped, dirs };
}

/** *.desktop under `dir`, following the spec's subdirectories. Bounded: this is a menu, not a find. */
function walk(dir, root, depth) {
  if (depth > 4) return [];
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) { try { out.push(...walk(p, root, depth + 1)); } catch (_) {} }
    else if (/\.desktop$/i.test(ent.name)) out.push(p);
  }
  return out;
}

module.exports = { scan, parseEntry, menuable, execArgv, entryId, appDirs, groupOf, onPath,
                   walk, iconFile, iconDataUri, DESKTOP_NAMES, FIELD_CODES };

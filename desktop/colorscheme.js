/* THE MACHINE'S LIGHT/DARK PREFERENCE FOLLOWS THE DESKTOP'S THEME.
 *
 * "Firefox Dark theme/mode not showing as Dark on PosterChanOS. Light theme is being used instead."
 * MEASURED on the laptop (192.168.0.154), in a private D-Bus session + headless Wayfire with the
 * session's own environment (GTK_THEME=Adwaita:dark, GTK_APPLICATION_PREFER_DARK_THEME=1):
 *
 *   gsettings color-scheme   portal org.freedesktop.appearance color-scheme   Firefox 156
 *   'default'   (as shipped)  0                                              LIGHT (chrome and pages)
 *   'prefer-dark'             1                                              dark
 *   'prefer-light'            2                                              light
 *
 * The session exported GTK_THEME=Adwaita:dark and considered the job done. Firefox does not ask
 * GTK first: widget/gtk/nsLookAndFeel.cpp reads the XDG portal's `color-scheme`, maps 0 ("no
 * preference") to LIGHT, lets that override the GTK theme's own darkness, and then swaps GTK over
 * to the light variant to match. Nothing on the machine ever wrote that key, so every Firefox —
 * and every libadwaita/GTK4 app, which read the same key — was light on a dark desktop.
 *
 * The key is `org.gnome.desktop.interface color-scheme`: xdg-desktop-portal-gtk serves it as the
 * portal setting, and it emits SettingChanged, so a running Firefox follows a theme switch live.
 * The session seeds `prefer-dark` at login (pc-shell-start-wayfire); this keeps it in step with the
 * theme picked in the desktop afterwards. Only the OS shell calls it — the same binary on somebody
 * else's GNOME must not rewrite their desktop preference because they changed an app's theme.
 */
'use strict';
const { execFile } = require('child_process');
const path = require('path');

const GSETTINGS = process.env.PC_GSETTINGS || 'gsettings';
const SCHEMA = 'org.gnome.desktop.interface';
const KEY = 'color-scheme';

/** 'dark' | 'light' → the gsettings enum value, or '' for anything else. */
function valueFor(scheme) {
  if (scheme === 'dark') return 'prefer-dark';
  if (scheme === 'light') return 'prefer-light';
  return '';
}

/** `'prefer-dark'\n` → prefer-dark. gsettings prints the GVariant text form. */
function parse(out) {
  const m = /'([a-z-]+)'/.exec(String(out || ''));
  return m ? m[1] : '';
}

/* dconf speaks over the session bus. The shell inherits DBUS_SESSION_BUS_ADDRESS from the session,
 * but a recovery launch may not, and gsettings without a bus silently writes to a memory backend
 * that nothing else can read — a "success" that changes nothing. Same fallback pc-compositor-session
 * uses for text-scaling-factor. */
function envFor(base) {
  const env = Object.assign({}, base || process.env);
  if (!env.DBUS_SESSION_BUS_ADDRESS) {
    const run = env.XDG_RUNTIME_DIR || (typeof process.getuid === 'function' ? '/run/user/' + process.getuid() : '');
    if (run) env.DBUS_SESSION_BUS_ADDRESS = 'unix:path=' + path.join(run, 'bus');
  }
  return env;
}

let chain = Promise.resolve();

/* READ, THEN WRITE ONLY ON A DIFFERENCE. Every window of the desktop applies the theme when it
 * loads, and a write is a SettingChanged broadcast to every GTK app on the bus; asking first makes
 * the common case (nothing changed) free. Serialised, so a quick preview-then-save cannot land its
 * two writes in the wrong order. */
function apply(scheme, runner, env) {
  const want = valueFor(scheme);
  if (!want) return Promise.resolve({ ok: false, why: 'unknown colour scheme: ' + String(scheme) });
  const run = runner || execFile;
  const opts = { timeout: 4000, env: envFor(env) };
  const step = () => new Promise((resolve) => {
    run(GSETTINGS, ['get', SCHEMA, KEY], opts, (err, out) => {
      if (err) return resolve({ ok: false, why: 'gsettings could not read ' + KEY + ': ' + ((err && err.message) || err) });
      const cur = parse(out);
      if (cur === want) return resolve({ ok: true, changed: false, value: cur });
      run(GSETTINGS, ['set', SCHEMA, KEY, want], opts, (err2) => {
        if (err2) return resolve({ ok: false, why: 'gsettings could not set ' + KEY + ': ' + ((err2 && err2.message) || err2) });
        resolve({ ok: true, changed: true, value: want, was: cur });
      });
    });
  });
  const p = chain.then(step, step);
  chain = p.catch(() => {});
  return p;
}

module.exports = { apply, valueFor, parse, envFor, SCHEMA, KEY };

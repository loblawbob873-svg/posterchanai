/* Origin comparison for the desktop shell.
 *
 * Its own module, and unit-tested (tests/test_desktop_origin.py), because getting it wrong is not a
 * cosmetic bug: main.js asks "is this ours?" before deciding whether to navigate or hand a URL to the
 * OS, whether to grant camera/mic/screen-share, and whether to answer an IPC call at all. One wrong
 * answer breaks all three at once, in ways that look unrelated to each other.
 *
 * THE TRAP: `new URL('app://posterchan/x').origin` is the STRING "null".
 *
 * WHATWG only defines a tuple origin for the "special" schemes — http, https, ws, wss, ftp and file —
 * and returns an opaque origin for everything else. Chromium's renderer knows better for a scheme
 * registered via registerSchemesAsPrivileged({standard:true}), so `location.origin` in the PAGE is
 * "app://posterchan"; Node's URL in the MAIN process has no access to that registry and says "null".
 * Comparing the two therefore fails for every single app:// URL, which is how:
 *
 *   - will-navigate treated an ordinary in-app navigation as off-site and called
 *     shell.openExternal('app://posterchan/...') — Windows answers "We can't open this app link",
 *     which is what the Logout button did;
 *   - setWindowOpenHandler sent target=_blank links to the OS for the same reason;
 *   - setPermissionRequestHandler / setPermissionCheckHandler DENIED camera, mic, notifications,
 *     screen share and the file-save picker to our own page, so calls could not work;
 *   - every IPC handler refused the bundled client, so the instance picker and the Tor controls
 *     silently did nothing.
 *
 * So build the origin by hand whenever URL declines to.
 */

function originOf(u) {
  let p;
  try { p = new URL(u); } catch (_) { return ''; }
  // Special schemes: trust the parser.
  if (p.origin && p.origin !== 'null') return p.origin;
  // Everything else: scheme + authority, which is what a "standard" scheme's tuple origin is. Guard on
  // host so `file:///x` and `data:...` (no authority, genuinely opaque) never collapse to a value that
  // could compare EQUAL to something — two opaque origins must not be treated as the same origin.
  if (!p.host) return '';
  return p.protocol + '//' + p.host;
}

// True when `url` belongs to the app bundle or to the configured instance. `instance` may be '' (the
// relays-only mode), in which case only the bundle qualifies — which is exactly right.
function isOurs(url, appOrigin, instance) {
  const o = originOf(url);
  if (!o) return false;
  if (o === appOrigin) return true;
  const inst = originOf(instance || '');
  return !!inst && o === inst;
}

/* Webxdc is deliberately NOT "ours": untrusted mini apps run on another origin so they cannot read
 * the client's keys or storage.  Electron still has to recognise that origin for the tiny set of
 * game permissions delegated by the embedding iframe (pointer lock/fullscreen/gamepad).  Keep this
 * separate from isOurs() so it can never accidentally inherit camera, display capture, clipboard,
 * filesystem or IPC access.
 *
 * The ordinary deployment is xdc.<instance>.  An operator with a wildcard certificate may enable
 * the per-app form, whose label is the 50-character lower-case base36 HMAC emitted by webxdc.js.
 * Accept exactly those two shapes and the configured instance's scheme/port; lookalike suffixes and
 * arbitrary sibling subdomains do not qualify. */
function isWebxdcSandbox(url, instance) {
  let u, base;
  try { u = new URL(url); base = new URL(instance || ''); } catch (_) { return false; }
  if (!u.origin || u.origin === 'null' || !base.hostname) return false;
  if (u.protocol !== base.protocol || u.port !== base.port) return false;
  const host = u.hostname.toLowerCase();
  const root = base.hostname.toLowerCase();
  if (host === 'xdc.' + root) return true;
  const suffix = '.' + root;
  if (!host.endsWith(suffix)) return false;
  const label = host.slice(0, -suffix.length);
  return /^[0-9a-z]{50}$/.test(label);
}

module.exports = { originOf, isOurs, isWebxdcSandbox };

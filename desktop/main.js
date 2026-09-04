/* PosterChan desktop (Windows .exe / macOS .dmg / Linux AppImage).
 *
 * The app BUNDLES the web client (desktop/www, assembled by build-www.sh from the same
 * static/js/client files the site serves) and loads it from disk over a privileged app:// scheme. An
 * instance — if the user names one — is a DATA endpoint only: AI, media rendering, streams, admin.
 * With no instance the app is a Nostr client and nothing is missing except the things a server does.
 *
 * That is the whole point: relays and a key are enough. Everything below follows from it.
 *
 * Why app:// and not file:// — a file:// page is not a secure context, and Chromium then removes
 * crypto.subtle and navigator.mediaDevices. The client SIGNS with crypto.subtle (NIP-44, the vault,
 * every event), so on file:// it could not log in, let alone make a call. A scheme registered as
 * `secure` + `standard` gets a real origin, a working WebCrypto, service workers and IndexedDB.
 *
 * The shell still owns only what a page can't: window state, the tor process and the session proxy,
 * routing off-site links to the real browser, the permission grants the client needs, and auto-update.
 */
const electron = require('electron');
// Only app/protocol/ipcMain are valid during Electron's pre-ready configuration phase. Most of the
// other exports are native lazy getters; destructuring `screen`, powerMonitor, session or Tray while
// this module loads can initialize platform backends before app.ready and SIGTRAP on a fast boot.
const { app, ipcMain, protocol } = electron;
let BrowserWindow, shell, session, Menu, clipboard, dialog, systemPreferences, screen, desktopCapturer;
const path = require('path');
const fsbridge = require('./fsbridge');
const fs = require('fs');
/* Electron's patched fs treats an asar as a virtual filesystem. statSync(app.asar) can therefore
 * report synthetic inode values that change on every call, making the installed-bundle watcher
 * announce a phantom update forever. original-fs observes the archive file on disk. */
let archiveFs=fs;try{archiveFs=require('original-fs');}catch(_){}
const tor = require('./tor');
let background = null; // background.js imports Tray, so require it only after app.ready too.
const vm = require('./vm');
const bluetooth = require('./bluetooth');
const liveusb = require('./liveusb');
const remotecontrol = require('./remotecontrol');
const diagnostic = require('./diagnostic').resolve(process.argv, process.env);
/* ProcessSingleton is acquired much later, but Electron chooses its lock directory from userData.
 * Set the diagnostic domain before any config/session access and, crucially, before
 * requestSingleInstanceLock(): Chromium's --user-data-dir flag alone does not change Electron's
 * application singleton and previously forwarded a verifier launch into the live desktop. */
if (diagnostic) app.setPath('userData', diagnostic.profile);

/* --shell: this process IS the desktop, not an app running on one.
 *
 * PosterChanOS execs the same binary the Windows and macOS builds ship, and the difference is
 * entirely in what it must NOT show: an application menu reading File / Edit / View / Help across
 * the top of an operating system is the single most convincing way to tell somebody they are
 * looking at an app in a window. Same for a title bar, a resize border and remembered geometry —
 * the compositor decides the size, and it is the whole screen. */
const SHELL_MODE = process.argv.includes('--shell');

const DEFAULT_INSTANCE = 'https://poster.place';
const APP_ORIGIN = 'app://posterchan';                  // the bundle's own origin
const APP_URL = APP_ORIGIN + '/index.html';
const UPDATE_EVERY_MS = 6 * 60 * 60 * 1000;             // re-check every 6h for long-running windows
const WWW = path.join(__dirname, 'www');

/* ONE NATIVE TOPLEVEL PER POSTERCHAN APP, PROCESS-WIDE.
 * Renderer task snapshots are necessarily a little late, and there can be a shell renderer on
 * each monitor. Two quick launcher clicks can therefore both ask Electron to create the same app.
 * Reserve the identity while the child is being created; main is the one authority shared by every
 * renderer. The timeout only releases a reservation if Electron never creates the child. */
const pcAppWindows = new Map();
const DENY_WINDOW_OPEN = Object.freeze({ action: 'deny' });
function pcWindowView(raw) {
  try {
    const u = new URL(String(raw || ''));
    if (u.protocol !== 'app:' || u.hostname !== 'posterchan') return '';
    return String(u.searchParams.get('pcwin') || '');
  } catch (_) { return ''; }
}
function claimPcAppWindow(raw) {
  const view = pcWindowView(raw);
  if (!view) return false;
  const prior = pcAppWindows.get(view);
  if (prior) {
    if (prior.pending) return true;
    if (!prior.isDestroyed()) {
      try { if (prior.isMinimized()) prior.restore(); prior.show(); prior.focus(); } catch (_) {}
      return true;
    }
    pcAppWindows.delete(view);
  }
  const reservation = { pending: true };
  pcAppWindows.set(view, reservation);
  setTimeout(() => {
    if (pcAppWindows.get(view) === reservation) pcAppWindows.delete(view);
  }, 5000);
  return false;
}

/* Tray / background state.
 *   quitting    — a REAL quit is under way, so window.close must not be turned into a hide.
 *   startHidden — this process was started by the login item; the first window loads out of sight.
 *   closeToTray — the user preference, default ON, and only meaningful when a tray actually exists. */
let quitting = false;
let startHidden = false;
const closeToTray = () => cfg.closeToTray !== false;

let win = null;
let cfg = {};
// Electron's Linux powerMonitor accessor initializes its native login1 backend. Keep even the
// accessor lazy: on a fast boot, destructuring it above can run before app.ready and Chromium
// deliberately traps before creating a window.
let powerMonitor = null;
function wireReadyElectronModules() {
  ({ BrowserWindow, shell, session, Menu, clipboard, dialog, systemPreferences, screen, desktopCapturer } = electron);
  background = require('./background');
}

// ---- tiny JSON config in userData (instance + window geometry + tor) ---------------------------
function cfgPath() { return path.join(app.getPath('userData'), 'config.json'); }

/* THIS FILE HOLDS THE FOLDER GRANTS, and losing it is not a cosmetic reset.
 *
 * `syncRoots` is the only record that the user ever pointed a native picker at ~/Pictures. Nothing
 * else can recreate it — not the renderer, which keeps only the mapping, and not the relay. When it
 * goes, every synced folder reports "access to this folder was withdrawn" and has to be picked again.
 *
 * It went. Two faults, and they compound:
 *
 *   1. the write was not atomic. writeFileSync truncates and then fills, so a process killed in
 *      between leaves a short, invalid JSON file — and this app has been killed repeatedly by the
 *      renderer running out of memory on a large sync.
 *   2. an unreadable file became `{}` in memory, and the next save wrote that `{}` over the top.
 *      The corruption was survivable; overwriting it was not.
 *
 * So: write to a temp file, fsync, rename (atomic on every filesystem this ships to), and never
 * silently discard a file that exists but does not parse — keep it as .bad and refuse to overwrite
 * until a real save happens. Same rule as the sync manifest's collapse guard: an empty read must
 * never be written back over a full one.
 */
let cfgLoadFailed = false;
let cfgSaveFailed = null;   // the last save error, or null — see saveCfg
function loadCfg() {
  let raw = null;
  try { raw = fs.readFileSync(cfgPath(), 'utf8'); }
  catch (_) { cfg = {}; cfgLoadFailed = false; return; }   // no file yet: a genuine fresh install
  try {
    cfg = JSON.parse(raw) || {};
    cfgLoadFailed = false;
  } catch (e) {
    cfg = {};
    cfgLoadFailed = true;
    try {
      const bad = cfgPath() + '.bad';
      fs.writeFileSync(bad, raw);
      console.warn('[cfg] unreadable config kept at', bad, '-', e.message);
    } catch (_) {}
  }
}
function saveCfg() {
  // A save AFTER a failed load would replace a recoverable file with {}. The first deliberate write
  // (a picked folder, a chosen instance) clears the flag, because by then there is something real to
  // store; until then the broken original is worth more than an empty one.
  if (cfgLoadFailed && !Object.keys(cfg).length) return;
  cfgLoadFailed = false;
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    const tmp = cfgPath() + '.tmp';
    const fd = fs.openSync(tmp, 'w');
    try {
      fs.writeSync(fd, JSON.stringify(cfg, null, 2));
      fs.fsyncSync(fd);                       // the rename is only atomic if the BYTES landed first
    } finally { fs.closeSync(fd); }
    fs.renameSync(tmp, cfgPath());
    cfgSaveFailed = null;
  } catch (e) {
    /* NOT SWALLOWED. This file holds the desktop's SYNC ROOTS — the mapping from a folder pair to a
     * directory on this disk — so a write that fails without a word means the pick works, the sweep
     * works for that session, and on the next launch the bridge has no roots at all: the folder is
     * still listed by the page (that lives in localStorage and survives), its handle resolves to
     * nothing, and the app asks the user to point at the folder again. Every launch, for ever, with
     * nothing in any log. Reported as "why do I have to keep pointing Pictures to the Pictures
     * folder on Desktop!".
     *
     * Recorded rather than thrown: the caller is usually a UI action that has already happened, and
     * the useful thing is that the NEXT thing to persist a root can say it did not stick. */
    cfgSaveFailed = (e && e.message) || String(e);
    console.error('[cfg] could not save', cfgPath(), '-', cfgSaveFailed,
                  '\n[cfg] sync folders picked in this session will be forgotten on restart');
  }
}
/* The configured instance, or '' for "relays only".
 *
 * '' and undefined are DIFFERENT and the difference is the whole feature: undefined means a fresh
 * install that has never chosen (→ poster.place, which is what every existing install has been doing),
 * while '' means the user deliberately turned the server off and must not be quietly reconnected to
 * one on the next launch. `cfg.instance == null` is the only test that keeps them apart.
 */
function instance() {
  if (cfg.instance == null) return DEFAULT_INSTANCE;
  return String(cfg.instance).replace(/\/+$/, '');
}
// origin.js, NOT `new URL(u).origin` — that is the string "null" for app:// and every other
// non-special scheme, which made isOurs() false for our OWN pages. Read the header there before
// touching this; it is the difference between a working app and one that hands its own URLs to
// Windows, denies itself the camera, and ignores its own IPC.
const { originOf, isOurs: _isOurs, isWebxdcSandbox: _isWebxdcSandbox } = require('./origin');
const { isTrustedPage } = require('./page-trust');
// "Ours" = the bundle, plus the instance's own pages (the client frames <instance>/admin). With no
// instance only the bundle qualifies, which is exactly right.
function isOurs(url) { return _isOurs(url, APP_ORIGIN, instance()); }
function isWebxdcSandbox(url) { return _isWebxdcSandbox(url, instance()); }

// ---- the sign-in round trip is not an off-site link -------------------------------------------
// "Sign in with Google / a fediverse account" leaves our origin BY DESIGN and comes back carrying a
// one-time code that the client swaps for the account's key. Handing that trip to the system browser —
// which the off-site rule below otherwise does — spends the single-use code THERE: the person ends up
// signed in in Firefox while the app they clicked in stays logged out.
//
// Recognised without a hardcoded provider list: an off-site URL whose `redirect_uri` points back at the
// instance IS the round trip, which is equally true of Google and of any fediverse instance someone
// types. While one is open, navigation WITHIN that provider stays in the app; it closes as soon as we
// are back on our own origin, or after OAUTH_MAX_MS, so this can never become a general allowance.
let oauth = null;
const OAUTH_MAX_MS = 5 * 60 * 1000;

function comesBackToUs(url) {
  try {
    const back = new URL(url).searchParams.get('redirect_uri') || '';
    const inst = instance();
    return !!back && !!inst && originOf(back) === originOf(inst);
  } catch (_) { return false; }
}
function isSignInNav(url) {
  const o = originOf(url);
  if (!o) return false;
  if (isOurs(url)) { oauth = null; return false; }        // home again: the trip is over
  if (comesBackToUs(url)) { oauth = { origin: o, until: Date.now() + OAUTH_MAX_MS }; return true; }
  return !!(oauth && oauth.origin === o && Date.now() < oauth.until);
}

// ---- the app:// scheme -------------------------------------------------------------------------
// registerSchemesAsPrivileged MUST run before app ready — Chromium reads the scheme registry once, at
// startup. `secure` is what gives the page WebCrypto (see the header note); `standard` is what gives it
// a real tuple origin, which is what the instance's CORS allowlist matches and what makes the service
// worker registerable.
protocol.registerSchemesAsPrivileged([{
  scheme: 'app',
  privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true,
                stream: true, allowServiceWorkers: true },
}]);

const _MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.svg': 'image/svg+xml', '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
  '.woff': 'font/woff', '.ttf': 'font/ttf', '.map': 'application/json', '.mp4': 'video/mp4',
  '.webm': 'video/webm', '.wasm': 'application/wasm',
};

function serveBundle() {
  protocol.handle('app', async (request) => {
    let rel;
    try { rel = decodeURIComponent(new URL(request.url).pathname); } catch (_) { rel = '/'; }
    if (rel === '/' || rel === '') rel = '/index.html';
    // Contain every request inside www/. path.normalize collapses ../ BEFORE the prefix test, so a
    // crafted app://posterchan/../../etc/passwd resolves and is then rejected — testing the raw
    // pathname for '..' would miss encoded and mixed-separator forms.
    const full = path.normalize(path.join(WWW, rel));
    if (!full.startsWith(WWW + path.sep) && full !== WWW) {
      return new Response('forbidden', { status: 403 });
    }
    try {
      const body = await fs.promises.readFile(full);
      const type = _MIME[path.extname(full).toLowerCase()] || 'application/octet-stream';
      return new Response(body, {
        status: 200,
        headers: {
          'Content-Type': type,
          // The shell is what carries the ?v= tokens for the JS/CSS, and it ships INSIDE the installer,
          // so a cached copy could only ever be staler than the file on disk. Reading it is a local
          // read; there is nothing to save.
          'Cache-Control': 'no-store',
        },
      });
    } catch (_) {
      return new Response('not found', { status: 404 });
    }
  });
}

// A cleartext instance (an .onion — plain HTTP by design — or a LAN box) is mixed content to our
// secure app:// page, and Chromium blocks it. The app is allowed to speak cleartext on purpose, the
// same way the APK sets usesCleartextTraffic. Must be set before ready.
function wireInsecureContent() {
  app.commandLine.appendSwitch('allow-running-insecure-content');
}

// Google refuses OAuth from a user agent it can identify as an embedded browser
// (disallowed_useragent), and Electron's default UA advertises exactly that:
// `posterchan/1.0.3 ... Electron/x.y.z`. Underneath it is plain Chromium of the stated Chrome/NNN
// version, so drop the two tokens and present that. Nothing else keys off the UA — the client picks
// its layout from viewport/pointer, never this string.
function wirePlainUserAgent() {
  try {
    app.userAgentFallback = app.userAgentFallback
      .replace(/\s?(?:Electron|posterchan)\/[\d.]+/gi, '')
      .replace(/\s{2,}/g, ' ').trim();
  } catch (_) {}
}

/* NATIVE WAYLAND WHEN THERE IS A WAYLAND SESSION — decided HERE, in code we ship, rather than by an
 * environment variable a wrapper is trusted to export.
 *
 * Electron defaults to the X11 backend, and a minimal Wayland compositor need not have XWayland at
 * all. PosterChanOS's does not. What that looks like is not a degraded window or a warning: the app
 * prints `Missing X server or $DISPLAY` and EXITS, the compositor execs the shell, the shell quits,
 * and the machine sits on a black screen with nothing on it and nothing in any log the person can
 * reach. On the box that IS the desktop that is a brick, not a bug.
 *
 * `auto` is the whole point of the hint: Wayland when WAYLAND_DISPLAY names a session, X11
 * otherwise — so this is also correct on an ordinary X11 desktop, where it changes nothing. It is
 * ALSO what makes the compositor able to place the window at all: an X11 client sets WM_CLASS after
 * it maps, so sway matches its rules against a window with no class yet and the shell floats in the
 * middle of the screen instead of being the desktop. A Wayland client has its app_id from the start.
 *
 * The wrapper still exports ELECTRON_OZONE_PLATFORM_HINT; this is the half that cannot be lost
 * between a .desktop file, an AppRun and a shell script. */
function wireOzonePlatform() {
  if (process.platform !== 'linux') return;
  app.commandLine.appendSwitch('ozone-platform-hint', 'auto');
}

// Wayland has no X11-style screen grab: capture goes through the xdg-desktop-portal/PipeWire path,
// which Chromium only takes when this feature is on. Harmless no-op if the feature name ever changes.
function wireWaylandCapture() {
  if (process.platform !== 'linux') return;
  // Keep this unconditional on Linux. PosterChanOS launches Electron with an explicit
  // `--ozone-platform=wayland`, but its locked-down shell wrapper deliberately clears most of the
  // environment. Testing WAYLAND_DISPLAY here therefore selected Chromium's X11 capturer on a
  // machine with no X server, yielding zero sources before the portal could open. The feature is a
  // harmless no-op on X11 and is required for the PipeWire portal path on Wayland.
  app.commandLine.appendSwitch('enable-features', 'WebRTCPipeWireCapturer');
}

// ---- Tor ---------------------------------------------------------------------------------------
/* One place decides what the session's proxy is, and it is derived from tor's state rather than set
 * from the two places that change it — so "on" and "off" cannot disagree.
 *
 * FAIL CLOSED. While Tor is enabled the proxy stays pointed at its SOCKS port even when the process
 * has died: every request then fails, which is the promise the switch makes. The tempting bug is to
 * "recover" by clearing the proxy, which silently drops someone onto the clear net at the exact moment
 * they were relying on it not to.
 */
async function applyProxy() {
  const s = tor.status();
  try {
    if (s.enabled) {
      // socks5:// (not socks4/http) is what makes Chromium resolve hostnames AT the proxy. With local
      // DNS the browsing is anonymous but every lookup still names the site to the local resolver —
      // and .onion cannot resolve locally at all, so it is also what makes onion addresses work.
      await session.defaultSession.setProxy({ proxyRules: tor.proxyRules() });
    } else {
      await session.defaultSession.setProxy({ mode: 'direct' });
    }
  } catch (e) { console.warn('[tor] setProxy', (e && e.message) || e); }
}

/* THE PAGE CANNOT TELL THAT THIS MACHINE SLEPT, AND ON A DESKTOP NOTHING ELSE TELLS IT EITHER.
 *
 * The client's reconnect paths are hung off `visibilitychange`, `online` and `pageshow`. A desktop
 * window that was never hidden fires none of them on resume, and `online` only fires if Chromium
 * decided the interface went down — which a suspend often does not do. So every socket comes back
 * from suspend either closed or, worse, a zombie (readyState 1, delivering nothing), and the app
 * looks connected while nothing arrives. It was reported as a signer that stopped working overnight
 * and could only be fixed by reloading the page.
 *
 * powerMonitor is the one source that KNOWS, so it says so. The renderer decides what to redial. */
function pushWake() {
  for (const w of BrowserWindow.getAllWindows()) {
    if (!w.isDestroyed()) { try { w.webContents.send('pc:wake'); } catch (_) {} }
  }
}
function wirePowerMonitor() {
  // Electron documents powerMonitor as unavailable until app.ready. Registering this while the
  // module was loading happened to survive on slower machines, but a fast fresh VM reached the
  // native login1 backend early and Chromium aborted with SIGTRAP before creating its first window.
  try {
    powerMonitor = require('electron').powerMonitor;
    powerMonitor.on('resume', pushWake);
  } catch (e) {
    console.warn('[power] could not subscribe to resume:', (e && e.message) || e);
  }
}

function pushTorStatus() {
  const s = tor.status();
  for (const w of BrowserWindow.getAllWindows()) {
    if (!w.isDestroyed()) { try { w.webContents.send('pc:tor:status', s); } catch (_) {} }
  }
}

// ---- auto-update -------------------------------------------------------------------------------
// Feed is our own domain (https://poster.place/desktop/), which 302s to the GitHub release assets —
// see app/main.py. Going through the server rather than electron-updater's GitHub provider keeps the
// feed stable: the repo carries TWO rolling releases (apk-latest, desktop-latest) and the GitHub
// provider picks whichever was published last, which would break update checks after an APK build.
//
// Now that the UI ships INSIDE the installer, this is how a client change reaches desktop users at
// all — it is no longer only about the shell.
function initUpdater() {
  if (!app.isPackaged) return;
  // macOS: Squirrel.Mac refuses to swap in an app that isn't code-signed, and these builds are
  // unsigned (no Apple Developer ID). Don't even check — download the new .dmg from poster.place.
  if (process.platform === 'darwin') return;
  // Over Tor the update check would go through the SOCKS proxy from a Node http stack that does not
  // use it, so it would either leak or hang. Skip it; the user can update by choice.
  if (tor.status().enabled) return;
  let autoUpdater;
  try { ({ autoUpdater } = require('electron-updater')); } catch (_) { return; }
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  /* A DOWNLOADED UPDATE THAT IS NOT NEWER MUST NOT BE OFFERED.
   *
   * electron-updater caches the installer it downloaded and keeps offering it until it is applied.
   * Install a build by hand in the meantime — which is what anybody does when they are waiting on a
   * fix — and the cache is now BEHIND the running app, so "PosterChan 1.0.467 is ready to install"
   * appears for ever on a machine already running 1.0.468. Reported exactly that way. Accepting it
   * would be a downgrade, and the version scheme is 1.0.<build number>, so the comparison is a
   * number.
   *
   * The stale download is deleted as well, so the check that follows re-downloads whatever the feed
   * actually offers instead of finding the old one and stopping. Both halves are best-effort: being
   * unable to tidy up is not a reason to nag somebody about a downgrade. */
  const buildOf = (v) => {
    const m = /(\d+)\s*$/.exec(String(v || ''));
    return m ? parseInt(m[1], 10) : NaN;
  };
  const isNewer = (v) => {
    const a = buildOf(v), b = buildOf(app.getVersion());
    return !(Number.isFinite(a) && Number.isFinite(b)) || a > b;
  };
  const dropStaleDownload = () => {
    try {
      const dir = path.join(app.getPath('cache'), app.getName() + '-updater');
      fs.rmSync(dir, { recursive: true, force: true });
    } catch (_) { /* nothing to tidy, or not ours to remove */ }
  };
  autoUpdater.on('update-downloaded', async (info) => {
    if (!isNewer(info && info.version)) {
      console.warn('[update] ignoring a cached', info && info.version,
                   '— this app is', app.getVersion());
      dropStaleDownload();
      return;
    }
    const r = await dialog.showMessageBox(win, {
      type: 'info',
      buttons: ['Restart now', 'Later'],
      defaultId: 0,
      cancelId: 1,
      title: 'Update ready',
      message: `PosterChan ${info && info.version ? info.version : ''} is ready to install.`,
      detail: 'The app will restart to finish updating. Otherwise it installs next time you quit.',
    });
    if (r.response === 0) { setImmediate(() => autoUpdater.quitAndInstall()); }
  });
  // A failed check must never surface as an error dialog — an offline laptop is not a problem.
  autoUpdater.on('error', (e) => console.warn('[update]', (e && e.message) || e));
  const check = () => autoUpdater.checkForUpdates().catch(() => {});
  setTimeout(check, 8000);                 // let the client finish loading first
  setInterval(check, UPDATE_EVERY_MS);
}

/* Bring the window back from every state it can be in: hidden by close-to-tray, minimised, behind
 * other windows, or gone entirely (macOS, where closing can destroy it while the app lives on). */
function showWindow() {
  if (!win || win.isDestroyed()) { createWindow(); return; }
  try {
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  } catch (_) {}
}

/* The one real quit path. Everything else (window close, tray Quit, the menu's role:quit) has to
 * come through here or `quitting` is never set and the close handler hides the window instead. */
function quitApp() {
  quitting = true;
  // The tray is destroyed on will-quit, not here: app.quit() can still be cancelled (an unload
  // handler, a download in progress), and tearing the icon down first would leave a running app with
  // no way back to it.
  app.quit();
}

/* THE HEALTH MARKER IS A STARTUP CONTRACT, AND IT USED TO OUTLIVE IT FOR EVER.
 *
 * The preload paints a deterministic 8x8 four-colour square in the corner of each shell surface so
 * `pc-wayfire-health wait` can screenshot the outputs and prove the renderer is actually painting
 * before the launcher declares the desktop ready. It is read EXACTLY ONCE, and then it sat there
 * for the life of the session — on a machine whose entire job is to look like a desktop. Reported
 * as "there is a color box on the top left of the desktop too".
 *
 * The verdict has a name in the filesystem: the launcher writes `$PC_WAYFIRE_READY_FILE` right
 * after the probe passes. Watch for it and retire the marker. The fallback timeout is not a
 * shortcut — a session started by hand, or on another compositor, has no ready file and would
 * otherwise keep the square for ever; it is comfortably longer than the launcher's own worst case
 * (10s for the Xwayland socket plus a 30s health gate), so it can never retire a marker the probe
 * still needs.
 */
function armHealthMarkerRetirement(target){
  const ready = process.env.PC_WAYFIRE_READY_FILE || '';
  let done = false;
  const retire = (why) => {
    if(done) return;
    done = true;
    clearInterval(poll); clearTimeout(cap);
    try{ if(!target.isDestroyed()) target.webContents.send('pc:host:health-marker-off'); }catch(_){ }
    try{ console.log('health marker retired (' + why + ')'); }catch(_){ }
  };
  const poll = setInterval(() => {
    if(target.isDestroyed()) { clearInterval(poll); clearTimeout(cap); return; }
    if(!ready) return;
    try{ if(fs.existsSync(ready)) retire('the shell was declared ready'); }catch(_){ }
  }, 500);
  const cap = setTimeout(() => retire(ready ? 'no ready signal within the gate\'s worst case'
                                            : 'no launcher ready file on this session'), 90000);
  if(poll.unref) poll.unref();
  if(cap.unref) cap.unref();
}

// ---- window ------------------------------------------------------------------------------------
function createWindow(assignment) {
  const primary = !assignment || assignment.primary !== false;
  const b = primary ? (cfg.bounds || {}) : (assignment.rect || {});
  const created = new BrowserWindow({
    width: b.width || 1280,
    height: b.height || 860,
    x: b.x, y: b.y,
    minWidth: 480,
    minHeight: 520,
    backgroundColor: '#0a0a10',            // matches the client's dark shell — no white flash on open
    // Menu bar stays VISIBLE: it carries "Switch instance…" and "Tor…", and behind an Alt-press nobody
    // would ever find them. NOT in shell mode — see SHELL_MODE: those settings live in the client's
    // own UI there, and a menu bar across the top of an operating system is what makes it look like
    // an app in a window.
    autoHideMenuBar: SHELL_MODE,
    frame: !SHELL_MODE,
    /* NOT `fullscreen: true`, AND THAT IS THE WHOLE POINT OF THIS LINE.
     *
     * A compositor fullscreen window covers its entire workspace INCLUDING every floating window on
     * it — so with the shell fullscreen, a native app launches, exists, reports its geometry, and is
     * `visible: false`. Which is to say: the desktop's one job, hosting other programs, cannot work
     * while it asks for this. Measured — opening a screenshot gave mupdf a window at 35,20 1849x1040
     * with `visible:false` behind a shell at `fullscreen_mode: 1`, drawn as an empty frame:
     * "clicking on a screenshot file loads black screen with spinning circle", and very likely the
     * same cause as "firefox is now a black screen window".
     *
     * `pc-shell-start` already knew this — it runs `fullscreen disable` on the shell right after the
     * window appears, and says why in a comment. That made it a RACE that this flag can win, and one
     * nothing reports when it does. The shell fills the screen by being TILED, which is what
     * `floating disable` there arranges, and a tiled window has floating windows above it, which is
     * exactly the stacking a desktop needs.
     *
     * `maximize` rather than nothing, so a compositor that does not pin the window for us still gets
     * a shell filling the display instead of a 1280x860 box in the middle. */
    /* A tiled Wayland client MUST accept the compositor's configure size. With `resizable:false`,
     * sway correctly allocated 1920x1080 but Electron kept submitting its requested 1280x860
     * buffer, leaving black strips on the right and bottom. The shell is still immovable and Sway
     * controls its geometry; resizable here means "honour the compositor", not user window chrome. */
    ...(SHELL_MODE ? { kiosk: false, resizable: true, movable: false } : {}),
    icon: path.join(__dirname, 'icon.png'),
    // Started by the login item: come up HIDDEN rather than showing and then hiding, which is a
    // window flashing on screen at every boot — the thing that makes people turn autostart off.
    show: primary ? !startHidden : false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: true,
      /* A LIVE STREAM CANNOT BE THROTTLED. Chromium's default is to throttle timers and rendering
       * whenever the window is not in the foreground, which is right for a document and wrong for a
       * broadcast: put a game in front of this window and the player stops being serviced, falls
       * behind the live edge, and then has to catch up when you come back — seen as a black frame on
       * switching and stuttering while it is behind.
       *
       * Reported as "windows app stuttery, firefox smooth and perfect" — same machine, same stream,
       * same moment, and the same client code once the app was rebuilt. Firefox does not throttle a
       * visible-but-unfocused window the way Chromium does, which is the whole of the difference. */
      backgroundThrottling: false,
      preload: path.join(__dirname, 'preload.js'),
      /* A companion display is a view, not a second background agent. The preload uses this marker
       * to withhold folder-sync ownership so adding a monitor cannot run two writers over one tree. */
      additionalArguments: ['--pc-preload-dir=' + __dirname]
        .concat(SHELL_MODE ? ['--pc-shell-health-marker'] : [])
        .concat(primary ? [] : ['--pc-secondary-surface']),
    },
  });
  if(primary) win = created;
  if (primary && cfg.maximized) created.maximize();
  if (SHELL_MODE) armHealthMarkerRetirement(created);
  /* The window still exists and the renderer still runs while hidden, which is the whole mechanism:
   * folder sync is renderer code, so "running in the background" is a hidden window, not a headless
   * process. Consumed here so a LATER createWindow (macOS activate) opens normally. */
  startHidden = false;

  const remember = () => {
    if (!primary || created.isDestroyed()) return;
    cfg.maximized = created.isMaximized();
    if (!cfg.maximized && !created.isMinimized()) cfg.bounds = created.getNormalBounds();
    saveCfg();
  };
  created.on('close', remember);
  /* Close means HIDE while the tray is holding the app open — otherwise closing the window ends the
   * process and, with it, the folder sync the tray exists to keep running. Guarded on the tray
   * actually being there: on a desktop with no tray this would make the app impossible to close.
   * `quitting` is the escape hatch every real quit path sets. */
  created.on('close', (e) => {
    if (!primary || quitting || !closeToTray() || !background.available()) return;
    e.preventDefault();
    if (!created.isDestroyed()) created.hide();
    /* Say so, ONCE. Closing a window and having the app keep running is not what closing a window
     * normally means, and an app that appears not to have quit — with no window and no message — is
     * indistinguishable from one that hung. Every app that does this shows this notice; skipping it
     * is how you get "I closed it and it's still in my task manager". */
    if (!cfg.trayNoticeShown) {
      cfg.trayNoticeShown = true;
      saveCfg();
      background.notify('PosterChan is still running',
        'Folder sync keeps working in the background. Use the tray icon to open it again, or to quit.');
    }
  });

  // Give the PAGE keyboard focus on launch. The window itself is focused, but its webContents is not
  // necessarily — with a visible menu bar the first keystroke can go to the chrome instead, which is why
  // no shortcut or scroll key worked until you clicked inside the page. Also on every window focus, so
  // alt-tabbing back does not need a click either.
  const focusPage = () => { if (!created.isDestroyed()) created.webContents.focus(); };
  created.webContents.on('did-finish-load', focusPage);
  /* Reload keeps the same webContents id. Clear readiness while its listener-owning JavaScript is
   * gone; os.js rearms it only after all monitor-handoff destinations have been installed. */
  created.webContents.on('did-start-navigation', (_e, _url, _inPlace, isMainFrame) => {
    if(isMainFrame) _handoffReady.delete(Number(created.webContents.id));
  });
  created.on('focus', focusPage);
  /* FILLS THE SCREEN WITHOUT BEING FULLSCREEN. See the SHELL_MODE block in the options above: the
   * compositor's fullscreen state hides every floating window on the workspace, which is every app
   * this desktop exists to host. Maximising is the state that means "as big as the screen" without
   * claiming exclusive use of it, and on PosterChanOS `pc-shell-start` tiles the window anyway — so
   * this is the fallback for a compositor that does not, not the normal path. */
  if (SHELL_MODE) { try { created.maximize(); } catch (_) {} }

  /* AND AGAIN WHENEVER THE SCREEN CHANGES SHAPE, which on a live boot is always.
   *
   * `maximize()` above runs once, against whatever the display was at that instant. A machine
   * booting from a USB stick brings the panel up at a firmware mode and sets the real one moments
   * later; a virtual machine resizes its guest display after the guest has started. Either way the
   * window keeps the size it was given and the compositor's background shows around it -- reported
   * as "the login and welcome screen does not fit, black on right and bottom", which is the first
   * screen anybody sees on the disc.
   *
   * sway tiles this window, and a tiled window follows its output on its own -- but `for_window`
   * cannot be relied on for THIS window (see pc-shell-start), so on the boot where the rule has not
   * matched yet it is floating, and a floating window keeps its geometry for ever. This does not
   * depend on which of those happened.
   *
   * Re-maximising an already-maximised window at the right size is a no-op, so the cost of being
   * wrong here is nothing. */
  if (SHELL_MODE) {
    const refit = () => {
      try {
        if (created.isDestroyed() || created.isMinimized()) return;
        // unmaximize FIRST: a window still flagged maximized at a stale size ignores maximize().
        if (created.isMaximized()) created.unmaximize();
        created.maximize();
      } catch (_) {}
    };
    const displayEvents = ['display-metrics-changed', 'display-added', 'display-removed'];
    for (const ev of displayEvents) { try { screen.on(ev, refit); } catch (_) {} }
    created.once('closed', () => {
      for (const ev of displayEvents) { try { screen.removeListener(ev, refit); } catch (_) {} }
    });
    // The mode can also settle a moment after the window is first shown, with no event we can see.
    created.once('ready-to-show', () => { setTimeout(refit, 1200); setTimeout(refit, 4000); });
  }
  created.once('ready-to-show', focusPage);

  // Right-click menu. Electron ships NO default context menu, so `spellcheck: true` above only ever drew
  // the red underline — there was no way to act on it, and no cut/copy/paste either. Chromium hands us the
  // suggestions in params.dictionarySuggestions; replaceMisspelling() applies one. Built per-event because
  // the suggestions differ for every word.
  created.webContents.on('context-menu', (_e, params) => {
    const items = [];
    if (params.misspelledWord) {
      for (const s of params.dictionarySuggestions) {
        items.push({ label: s, click: () => created.webContents.replaceMisspelling(s) });
      }
      if (!params.dictionarySuggestions.length) items.push({ label: 'No suggestions', enabled: false });
      items.push({ type: 'separator' });
      items.push({
        label: 'Add to dictionary',
        click: () => created.webContents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
      });
      items.push({ type: 'separator' });
    }
    if (params.linkURL) {
      items.push({ label: 'Open link in browser', click: () => shell.openExternal(params.linkURL) });
      items.push({ label: 'Copy link address', click: () => clipboard.writeText(params.linkURL) });
      items.push({ type: 'separator' });
    }
    const canEdit = params.isEditable;
    items.push({ role: 'cut', enabled: canEdit && params.editFlags.canCut });
    items.push({ role: 'copy', enabled: params.editFlags.canCopy });
    items.push({ role: 'paste', enabled: canEdit && params.editFlags.canPaste });
    if (canEdit) items.push({ role: 'selectAll' });
    Menu.buildFromTemplate(items).popup({ window: created });
  });

  // Ordinary links belong in the user's real browser. The only frameless children this shell owns
  // are explicit `pcwin` application surfaces; silently turning a same-origin target=_blank into a
  // raw Electron child leaves it outside PCOS bookkeeping, with no PosterChan title bar and no
  // reliable way to close it on a compositor. blob:/data: cannot be handed to another process, so
  // those retain a small, conventional native frame of their own.
  created.webContents.on('did-create-window', (child, details) => {
    const view = pcWindowView(details && details.url);
    if (!view) return;
    pcAppWindows.set(view, child);
    /* A managed PosterChan window is still a browser surface. Without its own open/navigation
     * policy, a link clicked from Social inside that window bypasses the desktop's handler and
     * Electron creates a raw 800x600 child: no frame, no controls, and no reliable close affordance
     * under Sway. Install the same external boundary before the user can interact with it. */
    child.webContents['setWindow' + 'OpenHandler'](({ url }) => {
      if (/^https?:/i.test(url)) shell.openExternal(url);
      return { action: 'deny' };
    });
    child.webContents.on('will-navigate', (e, url) => {
      if (isOurs(url) || String(url || '').startsWith('file://')) return;
      e.preventDefault();
      if (/^https?:/i.test(url)) shell.openExternal(url);
    });
    child.once('closed', () => {
      if (pcAppWindows.get(view) === child) pcAppWindows.delete(view);
    });
  });
  created.webContents.setWindowOpenHandler(({ url, features }) => {
    /* A PosterChan WINDOW — see static/js/client/oswin.js. On PosterChanOS a window is its own
     * compositor toplevel so sway stacks it with Telegram and Firefox natively, instead of the
     * desktop faking "bring to front" by taking the native surface off the screen.
     *
     * A real toplevel must also have real controls. The child page is one app view and does not
     * render the desktop's `.osw` titlebar, so `frame:false` left it with no close/minimise/maximise
     * affordance at all under Wayfire. Electron's client frame supplies those controls while the
     * shell surfaces and transient popups remain explicitly frameless. */
    if (isOurs(url) && /[?&]pcwin=/.test(url)) {
      /* `typeof` keeps the small shipped-handler simulation self-contained while production always
       * takes this process-wide path. Return the shared frozen result so the title-at-map regression
       * test can continue to distinguish this early singleton exit from the ordinary-link deny. */
      if (typeof claimPcAppWindow === 'function' && claimPcAppWindow(url)) return DENY_WINDOW_OPEN;
      const num = (name, fallback) => {
        const m = new RegExp(name + '=(\\d+)').exec(String(features || ''));
        const v = m ? Number(m[1]) : NaN;
        return Number.isFinite(v) && v > 0 ? v : fallback;
      };
      return { action: 'allow', overrideBrowserWindowOptions: {
        frame: false,
        /* THE TITLE HAS TO BE RIGHT THE INSTANT THE WINDOW MAPS.
         *
         * sway evaluates `for_window` when a surface maps, and the rule that floats these keys on
         * `title="^PosterChan Window"` (the app_id is shared with the desktop, which must stay
         * TILED). The page sets that title in `PCOSWin.adopt()` — which runs after the document
         * loads, i.e. after the map. Without it here the window maps under Electron's default
         * title, matches nothing, and sway TILES it into the shell's layout: the desktop splits in
         * half the first time an app is opened.
         *
         * The page still sets it (it appends the view name); this is only about the first frame. */
        title: 'PosterChan Window',
        width: num('width', 1100),
        height: num('height', 760),
        minWidth: 360,
        minHeight: 240,
        backgroundColor: '#0a0a10',
        autoHideMenuBar: true,
        // The window is a VIEW onto the desktop's client and reaches it through window.opener, so
        // it needs the same preload bridges — and, critically, the same process. Electron keeps a
        // same-origin child in the opener's process, which is what makes window.opener usable.
        /* NO `sandbox: false` HERE, AND THAT ONE WORD WAS THE WHOLE OF "THE TERMINAL DOESN'T WORK".
         *
         * A same-origin child shares the OPENER'S renderer process — that is the design, it is what
         * makes `window.opener.__PC` a live reference instead of a message channel. Every shell
         * surface is created by createWindow(), which does not set `sandbox` and therefore gets
         * Electron's default: sandboxed. Asking for an UNsandboxed preload inside that process is a
         * contradiction Electron cannot honour, and it does not refuse the window — it fails to
         * bootstrap the preload and carries on:
         *     TypeError: Cannot destructure property 'preloadScripts' of 'binding.startupData'
         * one line in shell.log per window opened, and nothing at all on screen.
         *
         * MEASURED on the machine: in `app://posterchan/index.html?pcwin=terminal`, `window.pcClip`
         * — exposed unconditionally at the top of preload.js — was `undefined`, along with pcTerm,
         * pcWM, pcFs and PCOSShell. With no `pcTerm`, `_viewNeedsInstance('terminal')` answers true
         * on this instance-less machine and the nav row is `hidden gated-off`, so the window routes
         * to the timeline; and with no `PCOSShell.available()`, `PCOS.restore()` falls through to
         * the remembered desktop preference and builds a SECOND DESKTOP inside the window, whose
         * `#os-root` `html.pc-oswin` then hides — taking `#feed` with it. The result is a
         * 1100x760 window containing the client's background gradient and nothing else, which is
         * exactly what "PosterChan Window — terminal" was photographed as. */
        webPreferences: {
          preload: path.join(__dirname, 'preload.js'), contextIsolation: true,
          /* The same two arguments the desktop's own surfaces get. `--pc-preload-dir` is how the
           * preload finds its siblings; `--pc-secondary-surface` withholds folder-sync ownership,
           * which a WINDOW must never claim — it is a view onto the desktop's client, and a second
           * writer over one tree is the failure that marker exists to prevent. */
          additionalArguments: ['--pc-preload-dir=' + __dirname, '--pc-secondary-surface'],
        },
      } };
    }
    if (/^blob:|^data:/.test(url) || (isOurs(url) && !/^https?:/i.test(url))) {
      return { action: 'allow', overrideBrowserWindowOptions: {
      frame: true, title: 'PosterChan Preview', width: 960, height: 720,
      minWidth: 360, minHeight: 240, autoHideMenuBar: true,
      } };
    }
    /* `isOurs` is intentionally irrelevant here: target=_blank means "open a browser page", not
     * "make another desktop application surface". Internal app navigation does not use _blank;
     * the explicit pcwin branch above is the sole exception. */
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // A 302 out to the provider fires will-redirect, not will-navigate, so watch it too — but only to
  // NOTICE the trip starting.
  created.webContents.on('will-redirect', (e, url) => { isSignInNav(url); });
  created.webContents.on('will-navigate', (e, url) => {
    if (isOurs(url) || url.startsWith('file://')) { oauth = null; return; }
    if (isSignInNav(url)) return;                     // the sign-in round trip comes back to us
    e.preventDefault(); shell.openExternal(url);      // everything else belongs in the real browser
  });

  // The bundle is on disk, so "can't load the app" is no longer a network condition — it means the
  // packaged www/ is missing or unreadable, which is a broken install and nothing a retry fixes. Say
  // that rather than showing Chromium's error page.
  created.webContents.on('did-fail-load', (e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;   // -3 = aborted (a normal in-app navigation)
    console.warn('[load]', code, desc, url);
    dialog.showErrorBox('PosterChan could not start',
      'The app files could not be read (' + (desc || code) + ').\n\nReinstalling should fix it.');
  });

  /* A DEAD RENDERER MUST NOT BE A BLACK WINDOW.
   *
   * When Chromium kills the render process — and the way to earn that here is memory, a folder sync
   * holding a multi-gigabyte file's plaintext, ciphertext and Blob at once — Electron leaves the
   * BrowserWindow open and empty. Reported as "after sync, i get black screen in windows app": no
   * error, no dialog, nothing in the window, and the app apparently still running.
   *
   * `reason` is 'oom'/'crashed'/'killed'; 'clean-exit' is an ordinary teardown and is left alone.
   * Reload rather than quit — everything this app holds is either on the relay or on disk, so a
   * reload is cheap and returns a usable window instead of a black one. Say what happened first, or
   * the reload just looks like the app blinked. */
  created.webContents.on('render-process-gone', (e, details) => {
    const reason = (details && details.reason) || 'unknown';
    console.warn('[renderer] gone:', reason, details && details.exitCode);
    if (reason === 'clean-exit') return;
    dialog.showErrorBox('PosterChan ran out of memory',
      reason === 'oom'
        ? 'The window was closed by the system because it ran out of memory. This usually means a '
          + 'very large file was being synced.\n\nThe app will reload. Files already synced are '
          + 'safe, and the next check resumes where it stopped.'
        : 'The window stopped unexpectedly (' + reason + ').\n\nThe app will reload.');
    try { created.webContents.reloadIgnoringCache(); }
    catch (_) { try { loadApp(created); } catch (_e) {} }
  });

  const contentsId = created.webContents.id;
  if(assignment) _shellScopes.set(contentsId, assignment);
  created.on('closed', () => { _shellScopes.delete(contentsId); _handoffReady.delete(contentsId); });
  loadApp(created);
  return created;
}

/* What the window shows, and in what order.
 *
 * With Tor on, the boot card comes FIRST and the bundle is not loaded until the circuit is up. That
 * ordering is the feature: the client opens relay sockets and fetches media the moment it evaluates,
 * so loading it first would put real traffic on the clear net during the seconds before the proxy took
 * effect — the exact leak the switch is meant to prevent.
 */
// Re-entrancy is real here, not theoretical: this awaits a bootstrap that can take a minute, and
// "Continue without Tor" (or Reload, or a menu action) calls it again from inside that window. Two
// concurrent runs would both eventually loadURL, and the LOSER could apply a stale proxy decision
// after the winner's. A generation counter lets the older run notice it has been superseded and stop.
const loadGens = new WeakMap();
async function loadApp(target) {
  target = target || win;
  if (!target || target.isDestroyed()) return;
  const gen = (loadGens.get(target) || 0) + 1;
  loadGens.set(target, gen);
  const current = () => gen === loadGens.get(target) && !target.isDestroyed();
  if (tor.status().enabled) {
    await target.loadFile(path.join(__dirname, 'boot.html'));
    if (!current()) return;
    pushTorStatus();
    const ok = await tor.waitBootstrapped(120000);
    if (!current()) return;
    // Not bootstrapped → stay on the card. It is showing the reason and offering both ways out
    // ("Continue without Tor" flips the switch, which comes back through here).
    if (!ok && tor.status().enabled) { pushTorStatus(); return; }
  }
  await applyProxy();
  if (!current()) return;
  /* BrowserWindow.loadURL is a promise. Await it: shell recovery must not mark a mapped secondary
   * surface healthy while its renderer is still about:blank (the visible result is a black
   * monitor after Ctrl+Alt+Backspace). Normal callers already await loadApp, so this makes the
   * existing contract true instead of changing it. */
  await target.loadURL(APP_URL);
}
function loadAllApps(){
  /* Native pickers are BrowserWindows too, but they are not app surfaces. Reloading one as the
   * PosterChan client halfway through choosing a folder turns a settings change into a destroyed
   * dialog. The primary and the registered output surfaces are the complete reload set. */
  const windows = Array.from(new Set([win].concat(
    Array.from(_shellSurfaces.values()).map(surface => surface.browser))))
    .filter(w => w && !w.isDestroyed());
  if(!windows.length) return loadApp();
  return Promise.all(windows.map(w => loadApp(w)));
}

// ---- downloads ---------------------------------------------------------------------------------
// Electron's DEFAULT save dialog is built with no `filters`, and a Windows save dialog without a
// matching filter drops the extension from the suggested name: a blob served as
// `Content-Disposition: attachment; filename="f335a5036df3.mp4"` landed on disk as `f335a5036df3`,
// which nothing can open. So set the dialog options ourselves.
//
// The name is rebuilt from every source available, in order of trust, because which one survives
// depends on where the extension was lost:
//   1. `?filename=` — the client's own name for the file (the drive knows it; the server does not);
//   2. what Electron parsed out of Content-Disposition;
//   3. the URL's basename (our Blossom URLs carry the extension now);
//   4. the MIME type of the response.
const _DL_MIME_EXT = {
  'video/mp4': 'mp4', 'video/webm': 'webm', 'video/quicktime': 'mov', 'image/jpeg': 'jpg',
  'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp', 'audio/mpeg': 'mp3',
  'audio/ogg': 'ogg', 'audio/wav': 'wav', 'audio/flac': 'flac', 'application/pdf': 'pdf',
  'application/zip': 'zip',
};
const _extOf = (s) => ((String(s || '').match(/\.([A-Za-z0-9]{1,8})$/) || [])[1] || '').toLowerCase();

function downloadName(item) {
  let url = null;
  try { url = new URL(item.getURL()); } catch (_) { /* data:/blob: — no query to read */ }
  const fromQuery = (url && url.searchParams.get('filename')) || '';
  const fromItem = item.getFilename() || '';
  const fromPath = url ? decodeURIComponent(url.pathname.split('/').pop() || '') : '';
  let name = (fromQuery || fromItem || fromPath || 'download').replace(/[\\/:*?"<>|]/g, '_').slice(0, 120);
  const ext = _extOf(name) || _extOf(fromItem) || _extOf(fromPath) || _DL_MIME_EXT[item.getMimeType()] || '';
  if (ext && !_extOf(name)) name += '.' + ext;
  return { name, ext };
}

function wireDownloads() {
  session.defaultSession.on('will-download', (_e, item) => {
    try {
      const { name, ext } = downloadName(item);
      item.setSaveDialogOptions({
        defaultPath: path.join(app.getPath('downloads'), name),
        filters: ext
          ? [{ name: `${ext.toUpperCase()} file`, extensions: [ext] }, { name: 'All files', extensions: ['*'] }]
          : [{ name: 'All files', extensions: ['*'] }],
      });
    } catch (e) { console.warn('[download]', (e && e.message) || e); }   // the default dialog still opens
  });
}

// The client is a real app: calls need camera/mic, notifications need permission, screen share needs
// display-capture. Grant those to our own pages only; deny everything else by default.
function wirePermissions() {
  // 'fileSystem' is the File System Access API — window.showSaveFilePicker/showOpenFilePicker.
  // Without it, Electron denies the picker and every "save a file" path in the client silently
  // degrades or fails: the Notes BACKUP is the one people hit, because a library with attachments is
  // gigabytes and the only way to write it is to stream it to a file handle. The user still gets the
  // OS save dialog, so nothing is written anywhere they did not choose.
  const ALLOW = new Set(['media', 'notifications', 'fullscreen', 'clipboard-read',
    'clipboard-sanitized-write', 'display-capture', 'pointerLock', 'background-sync', 'fileSystem']);
  // Untrusted Webxdc frames are not "ours" and must stay that way. These are the only capabilities
  // a game origin may request; notably absent are media, display capture, clipboard and filesystem.
  const WEBXDC_ALLOW = new Set(['pointerLock', 'fullscreen']);
  const ses = session.defaultSession;

  const permissionAllowed = (permission, from) =>
    ALLOW.has(permission) && (isOurs(from)
      || (WEBXDC_ALLOW.has(permission) && isWebxdcSandbox(from)));

  ses.setPermissionRequestHandler(async (wc, permission, cb, details) => {
    const from = (details && (details.requestingUrl || details.securityOrigin)) || (wc && wc.getURL()) || '';
    if (!permissionAllowed(permission, from)) { console.warn('[perm] denied', permission, from); return cb(false); }
    // macOS gates camera/mic behind TCC on top of the page grant: without asking, the OS hands the
    // renderer a silent black/silent stream. askForMediaAccess is what raises the system prompt.
    if (process.platform === 'darwin' && permission === 'media') {
      for (const t of (details && details.mediaTypes) || []) {
        const kind = t === 'video' ? 'camera' : t === 'audio' ? 'microphone' : null;
        if (kind && !(await systemPreferences.askForMediaAccess(kind).catch(() => false))) return cb(false);
      }
    }
    cb(true);
  });

  // The other half, and easy to miss: most web APIs run a permission CHECK first and only fall back to
  // a request if the check says no. Electron answers checks from a separate handler, so a grant above
  // means nothing on its own.
  ses.setPermissionCheckHandler((wc, permission, requestingOrigin, details) => {
    const from = requestingOrigin || (details && details.securityOrigin) || (wc && wc.getURL()) || '';
    return permissionAllowed(permission, from);
  });

  // Screen share. getDisplayMedia does NOT go through the handlers above: Electron rejects it outright
  // unless a display-media handler is set (a browser has a picker built in; an Electron app has to
  // supply one). On macOS 15+ the native picker takes over and this handler is never called.
  ses.setDisplayMediaRequestHandler(async (req, cb) => {
    if (!isOurs((req && req.frame && req.frame.url) || (req && req.securityOrigin) || '')) return cb({});
    /* Wayland's portal source often has no display_id (its id is a synthetic, ever-increasing
     * `screen:N:0:s`). Remember the display where the share gesture began BEFORE the portal moves
     * the cursor. Recomputing "nearest cursor" for every remote packet made the mapping switch
     * monitors as the remote pointer itself moved. */
    try{
      const d=screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
      remoteControlDisplayId=d ? String(d.id) : '';
    }catch(_){ remoteControlDisplayId=''; }
    remoteControlDisplayExplicit=false;
    let source = null;
    try { source = await pickScreenSource(); } catch (e) { screenLog('request failed: ' + ((e && (e.stack || e.message)) || e)); }
    if (!source) return cb({});   // cancelled → the page sees a plain NotAllowedError, as in a browser
    if(source.display_id){ remoteControlDisplayId=String(source.display_id);remoteControlDisplayExplicit=true; }
    // 'loopback' = share the system audio too, which only Windows supports. The client asks for
    // video-only today; this costs nothing and is right the day it asks for audio.
    cb(req && req.audioRequested && process.platform === 'win32'
      ? { video: source, audio: 'loopback' }
      : { video: source });
  // The native portal picker is reliable on current macOS. Electron's native-picker path cancels
  // immediately on wlroots/Sway; Linux must enter our handler below instead.
  }, { useSystemPicker: process.platform === 'darwin' });
}

// ---- screen-source picker (our stand-in for the browser's built-in one) -------------------------
// Modal child window listing every screen + window with a live thumbnail. Resolves to a source, or to
// null if the user cancels or closes it — never leaves getDisplayMedia hanging.
let pendingSources = [];
let pickerOpen = false;
let remoteControlDisplayId = '';
let remoteControlDisplayExplicit = false;
function screenLog(message) {
  const line = new Date().toISOString() + ' ' + String(message) + '\n';
  try { fs.appendFileSync(path.join(app.getPath('userData'), 'screen-share.log'), line); } catch (_) {}
  console.warn('[screen]', String(message));
}
function pickScreenSource() {
  if (pickerOpen) return Promise.resolve(null);   // one picker at a time — a second request just cancels
  pickerOpen = true;
  // Capture backend selection happens inside WebRTC and otherwise leaves only a misleading X11
  // error. Keep the three inputs Chromium uses in the persistent diagnostic so a recovery launch
  // that lost its compositor environment is distinguishable from a PipeWire/portal failure.
  if (process.platform === 'linux') {
    screenLog('capture environment: XDG_SESSION_TYPE=' + String(process.env.XDG_SESSION_TYPE || '')
      + ' XDG_CURRENT_DESKTOP=' + String(process.env.XDG_CURRENT_DESKTOP || '')
      + ' WAYLAND_DISPLAY=' + String(process.env.WAYLAND_DISPLAY || ''));
  }
  // xdg-desktop-portal-wlr advertises monitor capture only. Requesting a window source alongside it
  // can make Chromium reject the entire source request on that backend.
  const sourceTypes = process.platform === 'linux' ? ['screen'] : ['screen', 'window'];
  return desktopCapturer
    .getSources({ types: sourceTypes, thumbnailSize: { width: 320, height: 200 }, fetchWindowIcons: false })
    .then((sources) => {
      screenLog('desktopCapturer returned ' + sources.length + ' source(s): ' + sources.map((s) => s.id + ':' + s.name).join(', '));
      if (!sources.length) { pickerOpen = false; return null; }
      // Wayland's portal has ALREADY made the user choose a monitor before Electron returns this
      // single synthetic source. Opening our picker now asks them to choose twice, backwards. The
      // in-app instruction shown before getDisplayMedia explains the portal; accept its result here.
      if (process.platform === 'linux' && sources.length === 1) {
        pickerOpen = false;
        return sources[0];
      }
      pendingSources = sources;
      return new Promise((resolve) => {
        let done = false;
        const finish = (id) => {
          if (done) return; done = true; pickerOpen = false;
          resolve(sources.find((s) => s.id === id) || null);
          if (pick && !pick.isDestroyed()) pick.close();
        };
        const pick = new BrowserWindow({
          parent: win && !win.isDestroyed() ? win : undefined,
          modal: !!(win && !win.isDestroyed()), show: false, width: 880, height: 620, minWidth: 520, minHeight: 400,
          title: 'Choose what to share', backgroundColor: '#0a0a10', autoHideMenuBar: true,
          webPreferences: { contextIsolation: true, nodeIntegration: false,
            preload: path.join(__dirname, 'preload.js'),
            additionalArguments: ['--pc-preload-dir=' + __dirname] },
        });
        ipcMain.once('pc:screen:pick', (_e, id) => finish(id));
        pick.once('ready-to-show', () => { if(!pick.isDestroyed()) pick.show(); });
        pick.on('closed', () => { ipcMain.removeAllListeners('pc:screen:pick'); finish(null); });
        /* A picker renderer is disposable, but its PROMISE is not. A missing packaged picker.html
         * or a renderer killed while thumbnails decode used to leave this BrowserWindow black and
         * `pickerOpen` true forever: Cancel could not settle, and every later Share attempt was
         * rejected as a second picker. Route both lifecycle failures through the same one-shot
         * finish used by Close/Escape so focus returns to the owner and a retry is possible. */
        pick.webContents.once('render-process-gone', (_event, details) => {
          screenLog('picker renderer stopped: ' + String(details && details.reason || 'unknown'));
          finish(null);
        });
        try{
          Promise.resolve(pick.loadFile(path.join(__dirname, 'picker.html'))).catch(error => {
            screenLog('picker page failed to load: ' + String(error && error.message || error));
            finish(null);
          });
        }catch(error){
          screenLog('picker page failed to load: ' + String(error && error.message || error));
          finish(null);
        }
      });
    })
    .catch((error) => {
      // A rejected portal request must not poison every later attempt as "one picker at a time".
      pickerOpen = false;
      screenLog('desktopCapturer failed: ' + ((error && (error.stack || error.message)) || error));
      throw error;
    });
}

function buildMenu() {
  const inst = instance();
  /* NO APPLICATION MENU WHEN THIS IS THE DESKTOP. `null` rather than an empty template: an empty
   * one still reserves the bar's height, which is a strip of nothing across the top of the screen
   * that people will ask about. */
  if (SHELL_MODE) { Menu.setApplicationMenu(null); return; }
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: 'File',
      submenu: [
        { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => loadApp() },
        { type: 'separator' },
        { label: 'Switch instance…', click: () => win && win.loadFile(path.join(__dirname, 'shell.html'), { query: { pick: '1', url: inst } }) },
        {
          label: inst ? 'Use relays only (no server)' : 'Using relays only ✓',
          enabled: !!inst,
          click: () => setInstance(''),
        },
        { type: 'separator' },
        {
          label: tor.status().enabled ? 'Turn Tor off' : 'Turn Tor on…',
          enabled: tor.available(),
          click: () => setTor({ enabled: !tor.status().enabled }),
        },
        { type: 'separator' },
        /* The same two switches as the tray menu, because the tray is not discoverable — someone who
         * has never closed the window has no reason to have found it. Both read their state live, so
         * whichever surface you change it from, the other is right the next time it opens. */
        {
          label: 'Start at login', type: 'checkbox', checked: background.getAutostart(),
          click: (item) => { item.checked = background.setAutostart(item.checked); background.refresh(); },
        },
        {
          label: 'Keep running when the window is closed', type: 'checkbox',
          checked: closeToTray(), enabled: background.available(),
          click: (item) => { cfg.closeToTray = !!item.checked; saveCfg(); background.refresh(); },
        },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { label: 'Edit', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    {
      label: 'View',
      submenu: [
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' }, { role: 'togglefullscreen' }, { role: 'toggleDevTools' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        { label: inst ? 'Open this instance in browser' : 'No instance configured', enabled: !!inst,
          click: () => inst && shell.openExternal(inst + '/client') },
        { label: `Version ${app.getVersion()}`, enabled: false },
      ],
    },
  ]));
}

// ---- instance + tor changes ---------------------------------------------------------------------
/* Setting the instance reloads the app, because __PC_API_BASE__ is read once at page evaluation and
 * every relay socket, media URL and auth path hangs off it. '' is a legitimate value — see instance(). */
function setInstance(url) {
  const clean = String(url == null ? '' : url).trim().replace(/\/+$/, '');
  if (clean && !/^https?:\/\/[^\s/]+$/i.test(clean)) return false;
  cfg.instance = clean;
  saveCfg();
  buildMenu();                      // the File menu names the current instance
  loadAllApps();
  return true;
}

async function setTor(opts) {
  const before = tor.status().enabled;
  const s = await tor.set(opts || {});
  cfg.tor = { enabled: s.enabled, country: s.country };
  saveCfg();
  buildMenu();
  await applyProxy();
  // Turning Tor on or off changes which network every open socket uses, and the page holds plenty of
  // them (relay WebSockets above all). Only a reload re-opens them through the new route; leaving them
  // up would keep the old path alive under a UI claiming otherwise.
  if (s.enabled !== before) loadAllApps();
  pushTorStatus();
  return s;
}

// ---- IPC ----------------------------------------------------------------------------------------
// The bundle IS our own page, so the bridge is legitimately available to it — unlike the old shell,
// where the client was remote and a compromised instance could otherwise have repointed the app. Every
// handler still checks the shared exact-page predicate, so a framed third party or arbitrary local
// document gets nothing.
function fromOurPage(e) {
  const from = (e && e.senderFrame && e.senderFrame.url) || (e && e.sender && e.sender.getURL()) || '';
  return isTrustedPage(from, __dirname);
}

ipcMain.on('pc:instance:sync', (e) => { e.returnValue = instance(); });
ipcMain.handle('pc:instance:get', () => instance());
ipcMain.handle('pc:instance:set', (e, url) => fromOurPage(e) ? setInstance(url) : false);
ipcMain.on('pc:retry', (e) => {
  if(!fromOurPage(e)) return;
  const target = BrowserWindow.fromWebContents(e.sender);
  loadApp(target || win);
});

ipcMain.handle('pc:tor:status', () => tor.status());
ipcMain.handle('pc:tor:set', (e, opts) => fromOurPage(e) ? setTor(opts) : tor.status());
ipcMain.handle('pc:tor:new-circuit', (e) => fromOurPage(e) ? tor.newCircuit() : false);
ipcMain.handle('pc:tor:restart', async (e) => {
  if (!fromOurPage(e)) return tor.status();
  await tor.start();
  await applyProxy();
  loadAllApps();
  return tor.status();
});

// Clipboard for the page. Both web paths are dead in an Electron window over a cleartext instance
// (navigator.clipboard is removed outside a secure context and execCommand('copy') is refused), so the
// Go Live stream key simply could not be copied. Write-only by design: nothing here can READ what the
// user has copied.
/* ---- folder sync: the filesystem bridge ------------------------------------------------------
 *
 * Every handler is gated on fromOurPage AND on a root the user picked in a native dialog. The page
 * cannot create a root — only `pc:fs:pick` can, and that opens a real folder chooser, so the set of
 * reachable directories is exactly the set a human has agreed to. desktop/fsbridge.js re-checks the
 * resolved path against those roots on every call; this layer's only job is to refuse anything that
 * did not come from our own page.
 */
const fsGuard = (e) => { if (!fromOurPage(e)) throw new Error('denied'); };

/* ── THE COMPOSITOR AND THE NETWORK ────────────────────────────────────────────────────────────
 *
 * PosterChanOS runs PosterChan as the SHELL of a Wayland compositor: sway owns the screen, a
 * browser and a Steam game are ordinary clients, and this app decides where they go. Both halves
 * are tested modules (desktop/wm.js, desktop/net.js); this is the only place they reach the page.
 *
 * Same guard as the filesystem, for the same reason and more of it: `launch` starts a PROCESS and
 * `connect` hands a wifi password to NetworkManager. Neither may be reachable from any page but our
 * own. And both are ABSENT rather than broken off a compositor — `available()` answers no when
 * SWAYSOCK is unset, so a desktop install that is not PosterChanOS simply has no window manager
 * rather than a set of calls that throw. */
let _wm = null;
function wm() {
  if (!_wm) { const { WM } = require('./wm.js'); _wm = new WM(); }
  return _wm;
}
/* One renderer per output needs one compositor view per renderer. Kept empty on a single-output
 * session, where the existing window sees everything. Scratchpad rows have no real workspace, so
 * remember the last non-scratch owner; otherwise every monitor would adopt the same minimised app. */
const _shellScopes = new Map();       // webContents.id -> { output, workspace }
const _nativeOwners = new Map();      // con_id -> last ordinary workspace
const _shellSurfaces = new Map();     // output name -> { browser, conId, assignment }
const _handoffReady = new Set();      // webContents ids with destination listeners installed
const _nativeHandoffAcks = new Map(); // token -> { contentsId, resolve, timer }
const { recoverSurfaces } = require('./shell-recovery.js');
const { runAtomicHandoff } = require('./native-handoff.js');
const { createDesktopBottomGuard } = require('./desktop-bottom.js');
let _displayReconcile = null;
let _displayReconcileTimer = null;
/* Recover the owner of a parked window after the shell process itself restarts.
 *
 * `_nativeOwners` is process-local, but Sway's scratchpad survives an Electron restart. A stashed
 * Firefox/Telegram therefore comes back with no ordinary workspace and no remembered owner; every
 * scoped renderer filters it out and the app appears to vanish. Sway preserves its last absolute
 * rectangle, so assign it to the display containing its centre (or the nearest display when an old
 * rectangle is partly off-screen). The renderer's normal placement pass then clamps it. */
function ownerFromRect(row){
  const r = row && row.rect || {};
  const x = (Number(r.x)||0) + Math.max(0, Number(r.width)||0) / 2;
  const y = (Number(r.y)||0) + Math.max(0, Number(r.height)||0) / 2;
  let best = null;
  for(const scope of _shellScopes.values()){
    const b = scope && scope.rect || {};
    const left=Number(b.x)||0, top=Number(b.y)||0;
    const right=left+Math.max(0,Number(b.width)||0), bottom=top+Math.max(0,Number(b.height)||0);
    const dx=x<left?left-x:x>right?x-right:0, dy=y<top?top-y:y>bottom?y-bottom:0;
    const d=dx*dx+dy*dy;
    if(!best || d<best.d) best={d,workspace:String(scope.workspace||'')};
  }
  return best && best.workspace || '';
}
function scopedWindows(e, rows){
  const all = Array.isArray(rows) ? rows : [];
  for(const row of all){
    const id = Number(row && row.id);
    if(Number.isFinite(id) && row && !row.stashed && row.workspace)
      _nativeOwners.set(id, String(row.workspace));
  }
  const scope = e && e.sender && _shellScopes.get(e.sender.id);
  if(!scope) return all;
  return all.filter(row => {
    const id = Number(row && row.id);
    let owner = row && row.stashed ? _nativeOwners.get(id) : String(row && row.workspace || '');
    if(row && row.stashed && !owner){
      owner = ownerFromRect(row);
      if(Number.isFinite(id) && owner) _nativeOwners.set(id, owner);
    }
    return owner === String(scope.workspace);
  });
}
let _shellRecoveryWired = false;
let _updateRestart = null;
const bundleRestartMarker=()=>path.join(app.getPath('userData'),'installed-bundle-restart.json');
function rememberBundleRestart(identity,source){
  if(!identity) return;
  try{
    const marker=bundleRestartMarker(),tmp=marker+'.tmp';
    fs.writeFileSync(tmp,JSON.stringify({identity,source,at:Date.now()}),{mode:0o600});
    fs.renameSync(tmp,marker);
  }catch(e){console.warn('[update restart] marker',e&&e.message||e);}
}
function rememberedBundleRestart(){
  try{return String(JSON.parse(fs.readFileSync(bundleRestartMarker(),'utf8')).identity||'');}
  catch(_){return '';}
}
function requestSafeShellRestart(source='unknown',bundleIdentity=''){
  /* A verifier owns a private singleton/compositor domain and is never the installed desktop.
   * It may observe an updated ASAR copied underneath it, but it must neither acknowledge nor
   * initiate the canonical lifecycle. */
  if(!SHELL_MODE || diagnostic || _updateRestart) return false;
  const targets=Array.from(_shellSurfaces.values()).map(r=>r&&r.browser)
    .filter(w=>w&&!w.isDestroyed()&&_handoffReady.has(Number(w.webContents.id)));
  if(!targets.length) return false;
  const token=require('crypto').randomBytes(12).toString('hex');
  _updateRestart={token,pending:new Set(targets.map(w=>w.webContents.id)),source,bundleIdentity};
  console.warn(`[update restart] requested source=${source} pid=${process.pid}`);
  for(const target of targets) try{ target.webContents.send('pc:wm:event',
    {name:'tick',change:'update',payload:'pc:update-installed:'+token,window:null}); }catch(_){}
  return true;
}
ipcMain.handle('pc:shell:update-idle',(e,token)=>{
  const pending=_updateRestart;
  if(!pending || token!==pending.token || !pending.pending.has(e.sender.id)) return false;
  pending.pending.delete(e.sender.id);
  if(pending.pending.size) return true;
  _updateRestart=null;
  rememberBundleRestart(pending.bundleIdentity,pending.source);
  /* The shipped helper owns TERM, singleton-lock cleanup, canonical environment recovery and the
   * proof that both replacement surfaces mapped. Never duplicate that fragile sequence here. */
  console.warn(`[update restart] spawning source=${pending.source} pid=${process.pid}`);
  setTimeout(()=>{ try{ const child=require('child_process').spawn(
    '/usr/local/bin/pc-shell-restart',[String(process.pid)],
    {detached:true,stdio:'ignore',env:process.env});
    /* spawn() reports a missing helper asynchronously. Without a listener EventEmitter throws the
     * ENOENT into Electron, producing a visible error window on an otherwise healthy desktop. */
    child.on('error',e=>console.warn('[update restart] helper',e&&e.message||e));child.unref();
  }catch(e){console.warn('[update restart]',e&&e.message||e);} },250);
  return true;
});
async function forwardShellTick(ev){
  /* Actions belong to desktop renderers, never whichever popout/popup happens to own Electron
   * focus. Starting with getAllWindows made the fallback deliver Notifications/Super to a Social
   * popout while Firefox or that popout was active; it accepted the IPC and had no desktop handler,
   * so the action vanished. Exact shell ownership is already recorded here. */
  let targets=Array.from(_shellSurfaces.values()).map(record=>record&&record.browser)
    .filter(target=>target&&!target.isDestroyed());
  /* One desktop renderer exists per output, but a gesture has exactly ONE owner. Filtering only by
   * workspace was insufficient: two outputs may legitimately show the same named workspace, so a
   * single Start/popup choice opened Drafts (and every other app route) twice. Resolve the focused
   * OUTPUT first. State-clearing messages remain broadcasts so stale open flags cannot strand a
   * popup on the other surface. */
  const payload=String(ev&&ev.payload||'');
  const broadcast=payload==='pc:start:close'||payload.indexOf('pc:popup-closed:')===0;
  if(ev && !broadcast){
    try{
      const focusedOutput=(await wm().outputs()).find(x=>x&&x.focused);
      const active=(await wm().workspaces()).find(x=>x && x.focused);
      let owned=focusedOutput&&targets.filter(target=>{
        const scope=_shellScopes.get(target.webContents.id);
        return scope&&String(scope.output)===String(focusedOutput.name);
      });
      if(!owned||!owned.length) owned=active&&targets.filter(target=>{
        const scope=_shellScopes.get(target.webContents.id);
        return scope&&String(scope.workspace)===String(active.name);
      });
      /* Never broadcast an action merely because compositor discovery raced startup. A single
       * existing shell is a safer owner than duplicating every app/data action across monitors. */
      if(owned&&owned.length) targets=[owned[0]];
      else if(focusedOutput||active) targets=[];
      else if(targets.length>1) targets=[targets.find(t=>t.isFocused&&t.isFocused())||targets[0]];
    }catch(_){}
    /* A transient IPC failure must degrade to one owner, never back to a broadcast. */
    if(targets.length>1) targets=[targets.find(t=>t.isFocused&&t.isFocused())||targets[0]];
  }
  for(const target of targets){
    try{ target.webContents.send('pc:wm:event',
      {name:'tick',change:ev&&ev.change,payload:ev&&ev.payload,window:null}); }catch(_){}
  }
}
/* Proton commonly maps an anonymous XWayland surface and publishes `steam_app_<id>` only on a
 * later title/class event. Sway's `for_window` rule is map-time, while a renderer can be scoped to
 * the other output or busy loading; neither is an authoritative place to catch that transition.
 * The native event subscriber sees every final container on every output. Promote each game
 * surface once, including games which create two handoff surfaces, then leave subsequent user
 * fullscreen/windowed choices alone. */
const _nativeGameFullscreenAsked = new Set();
const _nativeBrowserSized = new Set();
const _nativeGameReconcileTimers = new Set();
async function reconcileNativeGameFullscreen(){
  let rows=[];
  try{ rows=await wm().windows(); }catch(_){ return; }
  const alive=new Set(rows.map(row=>Number(row&&row.id)).filter(Number.isFinite));
  for(const id of [..._nativeGameFullscreenAsked]) if(!alive.has(id)) _nativeGameFullscreenAsked.delete(id);
  for(const id of [..._nativeBrowserSized]) if(!alive.has(id)) _nativeBrowserSized.delete(id);
  for(const row of rows){
    const id=Number(row&&row.id), identity=String(row&&row.app||'');
    if(!Number.isFinite(id))continue;
    if(/^(?:steam_app_\d+|gamescope)/i.test(identity)){
      if(row.fullscreen){ _nativeGameFullscreenAsked.add(id); continue; }
      if(_nativeGameFullscreenAsked.has(id))continue;
      _nativeGameFullscreenAsked.add(id);
      wm().fullscreen(id,true).catch(()=>_nativeGameFullscreenAsked.delete(id));
      continue;
    }
    /* A browser opened from Web Search is a normal Firefox window, not an Electron preview. The
     * old 1400x900 map rule made it look like the same uncloseable square child we removed. Give a
     * newly observed browser the usable output area once; later user resizing is never overridden. */
    if(/firefox/i.test(identity)&&!_nativeBrowserSized.has(id)){
      _nativeBrowserSized.add(id);
      wm().snap(id,'max').catch(()=>_nativeBrowserSized.delete(id));
    }
  }
}
function scheduleNativeGameReconcile(){
  /* One XWayland map may cause several title/focus events. Share a single bounded sweep rather
   * than multiplying full-tree requests, while still covering Proton's delayed WM_CLASS. */
  if(_nativeGameReconcileTimers.size)return;
  for(const ms of [180,900,2500]){
    const timer=setTimeout(()=>{
      _nativeGameReconcileTimers.delete(timer);
      reconcileNativeGameFullscreen().catch(()=>{});
    },ms);
    _nativeGameReconcileTimers.add(timer);
  }
}
function enforceNativeGameFullscreen(ev){
  if(ev&&ev.wayfireView){
    const row=ev.wayfireView,id=Number(row.id),identity=String(row.app||'');
    if(!Number.isFinite(id))return;
    if(/unmapped/.test(String(ev.change||''))){_nativeGameFullscreenAsked.delete(id);return;}
    scheduleNativeGameReconcile();
    if(!/^(?:steam_app_\d+|gamescope)/i.test(identity)||row.fullscreen||_nativeGameFullscreenAsked.has(id))return;
    _nativeGameFullscreenAsked.add(id);wm().fullscreen(id,true).catch(()=>_nativeGameFullscreenAsked.delete(id));return;
  }
  const c=ev&&ev.container;
  if(!c)return;
  const id=Number(c.id);
  if(!Number.isFinite(id))return;
  if(ev.change==='close'){
    _nativeGameFullscreenAsked.delete(id);
    return;
  }
  scheduleNativeGameReconcile();
  const p=c.window_properties||{};
  const identity=String(c.app_id||p.class||p.instance||'');
  if(!/^(?:steam_app_\d+|gamescope)/i.test(identity))return;
  if(c.fullscreen_mode){
    _nativeGameFullscreenAsked.add(id);
    return;
  }
  if(_nativeGameFullscreenAsked.has(id))return;
  _nativeGameFullscreenAsked.add(id);
  wm().fullscreen(id,true).catch(()=>_nativeGameFullscreenAsked.delete(id));
}
async function wireShellRecovery(){
  if(!SHELL_MODE || _shellRecoveryWired || !wm().available()) return;
  _shellRecoveryWired = true;
  try{
    await wm().subscribe(['window','workspace','output','tick']);
    wm().on('window', enforceNativeGameFullscreen);
    wm().on('window', createDesktopBottomGuard({
      backend:wm().backend,
      shellIds:()=>Array.from(_shellSurfaces.values()).map(record=>Number(record&&record.conId)).filter(Number.isFinite),
      windows:()=>wm().windows(),
      focus:id=>wm().focus(id)
    }));
    wm().on('window', (ev) => {
      const row=ev&&ev.wayfireView;
      if(!row||ev.change!=='view-geometry-changed')return;
      /* Wayfire's move plugin cannot exclude a view by matcher. If Super+drag begins over bare
       * desktop, it can therefore grab the full-output Electron surface. Repair only a known shell
       * con_id; popouts share its app-id and must remain movable. */
      if(Array.from(_shellSurfaces.values()).some(record=>record&&Number(record.conId)===Number(row.id)))
        scheduleDisplayReconcile();
    });
    wm().on('tick', (ev) => {
      if(!ev || ev.first) return;
      if(ev.payload !== 'pc:restart'){
        /* Keyboard events cannot depend on a renderer first asking to receive them. On the laptop,
         * Sway ran the physical Super binding but a slow renderer startup had never armed the
         * forwarding handler, so the tick died here in the main process. This subscription is
         * always installed for shell recovery; it is therefore the authoritative keyboard path. */
        if(ev.payload==='pc:update-installed') requestSafeShellRestart('sway-tick');
        else forwardShellTick(ev).catch(()=>{});
        return;
      }
      /* Keep the Wayland surface mapped. Killing Electron and racing its replacement against the
       * singleton socket is what turned Ctrl+Alt+Backspace into a permanent black screen. */
      try{
        if(win && !win.isDestroyed()){
          /* `reloadIgnoringCache()` only reloads the URL already present. A companion surface that
           * lost its first navigation is still about:blank, so Ctrl+Alt+Backspace kept that monitor
           * blank for ever. loadApp is the canonical proxy/Tor-aware navigation and repairs both a
           * loaded client and a never-loaded renderer. */
          recoverSurfaces(_shellSurfaces.values(), loadApp).catch(e =>
            console.warn('[shell restart]', e && e.message || e));
        }
      }catch(e){ console.warn('[shell restart]', e && e.message || e); }
    });
  }catch(e){
    _shellRecoveryWired = false;
    console.warn('[shell recovery]', e && e.message || e);
  }
}

function watchInstalledBundle(){
  if(!SHELL_MODE || !app.isPackaged) return;
  const bundle=path.join(process.resourcesPath,'app.asar');
  let identity='';
  try{const s=archiveFs.statSync(bundle);identity=`${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}`;}catch(_){return;}
  let candidate='',stable=0;
  const check=()=>{try{const s=archiveFs.statSync(bundle),next=`${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}`;
    if(next===identity){candidate='';stable=0;return;}
    /* Remember the immutable bundle accepted before spawning the replacement. A new process must
     * never interpret that same file as another update and form a restart loop. */
    if(next===rememberedBundleRestart()){identity=next;candidate='';stable=0;return;}
    if(next!==candidate){candidate=next;stable=1;
      console.warn(`[update restart] bundle mismatch observed old=${identity} next=${next}`);return;}
    if(++stable>=2 && requestSafeShellRestart('bundle-watch',next)) identity=next;
  }catch(_){}};
  setInterval(check,30000).unref();
}

/* ONE DESKTOP SURFACE PER OUTPUT. Sway cannot stretch one Wayland surface across unrelated
 * outputs, and trying to fake it in the page is what left the second monitor black. Each surface
 * owns one numbered workspace. Native windows belong to the surface for their current workspace,
 * so dragging Firefox across an output is a hand-off, not a second copy and not a close. */
const shellDisplays = require('./shell-displays.js');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function newShellContainer(before){
  const old = new Set((before || []).map(row => Number(row.id)));
  for(let n=0;n<40;n++){
    const rows = await wm().windows();
    const found = rows.find(row => Number(row.pid) === process.pid && !old.has(Number(row.id)));
    if(found) return found;
    await delay(50);
  }
  throw new Error('the compositor did not map the PosterChan desktop surface');
}
async function placeShellSurface(record, assignment){
  record.assignment = assignment;
  _shellScopes.set(record.browser.webContents.id, assignment);
  await wm().assignShell(record.conId,assignment);
}
async function reconcileShellDisplays(){
  if(!SHELL_MODE || !wm().available()) return;
  if(_displayReconcile) return _displayReconcile;
  _displayReconcile = (async () => {
    const assignments = shellDisplays.plan(await wm().outputs(), await wm().workspaces());
    if(!assignments.length) return;
    const wanted = new Set(assignments.map(a => a.output));
    for(const [output, record] of Array.from(_shellSurfaces)){
      if(wanted.has(output)) continue;
      _shellSurfaces.delete(output);
      if(record.browser !== win && !record.browser.isDestroyed()) record.browser.destroy();
    }
    let rows = await wm().windows();
    let used = new Set(Array.from(_shellSurfaces.values()).map(r => Number(r.conId)));
    for(const assignment of assignments){
      let record = _shellSurfaces.get(assignment.output);
      let fresh = false;
      if(record && (!record.browser || record.browser.isDestroyed())){
        _shellSurfaces.delete(assignment.output);
        record = null;
      }
      if(!record && assignment.primary){
        /* BrowserWindow creation returns before Wayland maps its surface. An immediate tree lookup
         * wins that race on a cold boot, aborts this entire reconciliation, and pc-shell-start later
         * repairs only the primary display: every other monitor stays black until another output
         * event happens. Wait for the primary exactly as we already wait for companions. */
        let own = rows.find(row => Number(row.pid) === process.pid && !used.has(Number(row.id)));
        if(!own) own = await newShellContainer(rows);
        record = { browser: win, conId: Number(own.id), assignment };
        fresh = true;
      }else if(!record){
        const before = await wm().windows();
        const browser = createWindow(assignment);
        browser.show();
        const own = await newShellContainer(before);
        record = { browser, conId: Number(own.id), assignment };
        fresh = true;
      }
      used.add(Number(record.conId));
      _shellSurfaces.set(assignment.output, record);
      const current=rows.find(row=>Number(row.id)===Number(record.conId));
      if(fresh||shellDisplays.needsPlacement(current,assignment)) await placeShellSurface(record, assignment);
      else{
        record.assignment=assignment;
        _shellScopes.set(record.browser.webContents.id,assignment);
      }
      if(!record.browser.isVisible()) record.browser.show();
    }
  })().catch(e => {
      console.warn('[shell displays]', (e && e.message) || e);
      clearTimeout(_displayReconcileTimer);
      _displayReconcileTimer = setTimeout(() => reconcileShellDisplays(), 1200);
    })
    .finally(() => { _displayReconcile = null; });
  return _displayReconcile;
}
function scheduleDisplayReconcile(){
  if(!SHELL_MODE) return;
  clearTimeout(_displayReconcileTimer);
  _displayReconcileTimer = setTimeout(() => reconcileShellDisplays(), 250);
}
const net = require('./net.js');
let _displays = null;
function displays(){
  if(!_displays){
    const { Displays } = require('./displays.js');
    /* An installed-package diagnostic owns a nested compositor, but it still inherits the real
     * account's HOME. Letting Displays use its default path therefore writes HEADLESS-1 into the
     * person's live ~/.config/sway/outputs.conf when the diagnostic repairs its synthetic layout.
     * Keep every persistent display byte inside the already-validated diagnostic token domain. */
    const opts = diagnostic ? { file: path.join(diagnostic.profile, 'sway-outputs.conf') } : undefined;
    _displays = new Displays(wm(), opts);
  }
  return _displays;
}

ipcMain.handle('pc:wm:available', (e) => { fsGuard(e); return wm().available(); });
ipcMain.handle('pc:wm:windows', async (e) => { fsGuard(e); return scopedWindows(e, await wm().windows()); });
ipcMain.handle('pc:wm:self', async (e) => {
  fsGuard(e);
  const owner=BrowserWindow.fromWebContents(e.sender), title=owner&&!owner.isDestroyed()?owner.getTitle():'';
  const rows=await wm().windows();
  const matches=rows.filter(row=>String(row.title||'')===String(title||''));
  return matches.length===1?matches[0]:null;
});
ipcMain.handle('pc:wm:cycle-output', async (e, direction) => {
  fsGuard(e);
  const dir=String(direction||''); if(dir!=='next'&&dir!=='previous')return false;
  const scope=_shellScopes.get(e.sender.id); if(!scope||_shellSurfaces.size<2)return false;
  const rows=[..._shellSurfaces.values()].filter(x=>x&&x.browser&&!x.browser.isDestroyed()&&x.assignment)
    .sort((a,b)=>{const ar=a.assignment.rect||{},br=b.assignment.rect||{};return (Number(ar.y)||0)-(Number(br.y)||0)||(Number(ar.x)||0)-(Number(br.x)||0)||String(a.assignment.output||'').localeCompare(String(b.assignment.output||''));});
  const at=rows.findIndex(x=>String(x.assignment.output||'')===String(scope.output||''));
  if(at<0||rows.length<2)return false;
  const step=dir==='previous'?-1:1,target=rows[(at+step+rows.length)%rows.length];
  if(!target||target===rows[at])return false;
  /* ASK THE OTHER MONITOR BEFORE GIVING IT THE GESTURE — and before moving the keyboard there.
   *
   * The origin renderer tears its chooser down the instant this resolves true, so answering "yes"
   * on behalf of an output that has nothing to cycle ends the gesture with no chooser anywhere and
   * focus on an empty desktop. Measured on the two-monitor machine: one PosterChan window on DP-1
   * and nothing of ours on DP-2 made the chooser appear and disappear again within 38ms on every
   * single press, which is the whole of "alt tab disappears each time you switch". A refusal is
   * honest and cheap — the origin then wraps locally, which is what a one-window desk should do. */
  if(!await surfaceCanCycle(target))return false;
  if(Number.isFinite(Number(target.conId)))await wm().focus(Number(target.conId));
  try{target.browser.webContents.send('pc:wm:event',{name:'tick',change:'run',payload:'pc:cycle-enter:'+dir,window:null});}catch(_){return false;}
  return true;
});

/* The destination answers for itself. A renderer that cannot answer in time keeps the gesture where
 * it is rather than swallowing it: a busy monitor is a reason to wrap locally, never a reason for
 * Alt+Tab to do nothing at all. */
function surfaceCanCycle(rec){
  if(!rec||!rec.browser||rec.browser.isDestroyed())return Promise.resolve(false);
  let wc; try{ wc=rec.browser.webContents; }catch(_){ return Promise.resolve(false); }
  if(!wc||wc.isDestroyed())return Promise.resolve(false);
  let ask;
  try{
    ask=Promise.resolve(wc.executeJavaScript(
      '(()=>{try{return !!(window.PCOS&&PCOS.__canCycle&&PCOS.__canCycle());}catch(_){return false;}})()',
      true)).then(v=>!!v).catch(()=>false);
  }catch(_){ return Promise.resolve(false); }
  return Promise.race([ask,new Promise(res=>setTimeout(()=>res(false),400))]);
}
ipcMain.handle('pc:wm:snapshot', async (e) => {
  fsGuard(e);
  /* Today the primary surface owns the full list. Per-output surfaces replace `windows` with their
   * workspace slice, while allIds remains global; keeping that distinction in the API prevents a
   * renderer from killing an app merely because it crossed a monitor boundary. */
  const rows = await wm().windows();
  const scope = _shellScopes.get(e.sender.id);
  const own = scope && _shellSurfaces.get(scope.output);
  return { windows: scopedWindows(e, rows),
           allIds: rows.map(x => Number(x.id)).filter(Number.isFinite),
           /* The exact compositor surface behind THIS renderer.  A multi-monitor diagnostic can
            * temporarily put two PosterChan processes on one output; guessing "the first shell"
            * then moves or measures the wrong desktop.  Identity is cheap and makes callers safe. */
           shellId: own && Number.isFinite(Number(own.conId)) ? Number(own.conId) : null };
});
ipcMain.handle('pc:wm:handoff-ready', (e, ready) => {
  fsGuard(e);
  const id=Number(e.sender.id);
  if(ready === false) _handoffReady.delete(id); else _handoffReady.add(id);
  return true;
});
ipcMain.handle('pc:wm:native-handoff-ack', (e, token, rect) => {
  fsGuard(e);
  const pending=_nativeHandoffAcks.get(String(token||''));
  if(!pending || pending.contentsId!==Number(e.sender.id)) return false;
  const r=rect&&typeof rect==='object'?rect:{};
  const out={x:Number(r.x),y:Number(r.y),w:Number(r.w),h:Number(r.h)};
  if(!Object.values(out).every(Number.isFinite) || out.w<64 || out.h<64) return false;
  clearTimeout(pending.timer); _nativeHandoffAcks.delete(String(token)); pending.resolve(out);
  return true;
});
ipcMain.handle('pc:wm:focus', (e, id) => { fsGuard(e); return wm().focus(Number(id)); });
ipcMain.handle('pc:wm:preview', async (e, id) => {
  fsGuard(e); id=Number(id); if(!Number.isFinite(id))return '';
  const rows=scopedWindows(e,await wm().windows()),target=rows.find(row=>Number(row.id)===id);
  if(!target||target.stashed||target.visible===false||!target.rect)return '';
  /* grim captures screen pixels, not a con_id. Refuse if another native client intersects the
   * requested surface: an exact rectangle is not enough if it could contain somebody else's app.
   * The PosterChan shell is tiled behind the target and is the only safe overlap. */
  const shell=/^(?:posterchan(?:-desktop)?|place\.poster\.desktop)$/i;
  const r=target.rect,overlap=rows.some(row=>Number(row.id)!==id&&!row.stashed&&row.visible!==false&&
    !shell.test(String(row.app||''))&&row.rect&&r.x<row.rect.x+row.rect.width&&
    r.x+r.width>row.rect.x&&r.y<row.rect.y+row.rect.height&&r.y+r.height>row.rect.y);
  if(overlap)return '';
  try{return await require('./native-preview.js').capture(r);}catch(_){return '';}
});
/* A POPUP THAT MUST APPEAR ABOVE APPLICATIONS HAS TO BE ITS OWN WINDOW.
 *
 * sway paints floating windows above tiled ones unconditionally, and the desktop shell is the TILED
 * window — so the start menu, the tray flyouts and the notification panel, all drawn inside that
 * surface, are underneath Firefox and Telegram no matter what z-index they carry. Reported over and
 * over: "start menu is not going over windows", "notifications do not go over open windows",
 * "volume mixer widget and nostr widget still hide behind the damn windows".
 *
 * Two workarounds were tried and both were wrong. Fullscreening the shell puts it on top and HIDES
 * every other window on the workspace — pressing Start emptied the desktop. Hosting the apps inside
 * the shell fixes the stacking but is the code path that breaks fullscreen games.
 *
 * A separate floating window has neither problem, and the assumption was measured on the real
 * machine before this was written: a brand-new floating window mapped ABOVE Telegram and both
 * PosterChan windows, last in sway's floating stack. So the popup is a real surface the compositor
 * stacks for us, and nothing has to be hidden or reparented.
 *
 * One at a time, closed on blur — a menu you have clicked away from is closed, which is also what
 * stops a stranded popup surviving on another output. */
let _popupWin = null;
let _popupKind = '';
const POPUP_TITLE = 'PosterChan Popup';
/* THE SHELL HAS TO BE TOLD, and not telling it was the whole of "the glitchiest thing ever".
 *
 * The desktop sets `startOpen = true` and asks for a window. That window then closes on its own —
 * on blur, on Escape, or the moment you choose something — and the renderer that opened it never
 * hears about it. So the shell still believes the menu is up, and the NEXT Super press toggles it
 * shut and opens nothing. Measured, pressing Super four times: no menu, menu, no menu, no menu.
 *
 * The tick goes out on every path that closes a popup, so the flag can never be left standing. */
function closePopupWindow(){
  const p = _popupWin, kind = _popupKind;
  _popupWin = null; _popupKind = '';
  if(p && !p.isDestroyed()) { try{
    /* Menu/flyout pages load the full client, whose unload protection may veto BrowserWindow.close.
     * That left an inactive Start/notification surface mapped indefinitely underneath the next
     * app. Those surfaces hold no user data and must disappear synchronously; sticky composers keep
     * the graceful close path because they can contain a draft. */
    if(STICKY_POPUPS.has(kind)) p.close(); else p.destroy();
  }catch(_){ } }
  if(kind) { try{ forwardShellTick({ change: 'run', payload: 'pc:popup-closed:' + kind }); }catch(_){ } }
}
/* A COMPOSER IS NOT A MENU. Every other popup is a menu and closes when you click away, which is
 * what makes it feel like a menu rather than a window somebody has to dismiss. A composer that did
 * that would throw away what you typed the first time you clicked the desktop or reached for a file
 * dialog — the exact loss `.modal-sticky` exists to prevent on the web. So these kinds keep focus
 * discipline of their own: they close when the user closes them. */
const STICKY_POPUPS = new Set(['compose']);
/* PRESSING SUPER MUST NOT DEPEND ON WHAT THE SHELL REMEMBERS.
 *
 * The desktop used to keep `startOpen` and decide open-or-close from it. That flag is a guess about
 * a window this process owns: the popup also closes on blur, on Escape and on every choice, so the
 * two drift apart constantly and the next press "closes" a menu that is not there. Measured before
 * this: Super six times gave menu, nothing, menu, menu, nothing, menu.
 *
 * So the question "is it open?" is answered HERE, by the process holding the window, and the shell
 * only paints what it is told. */
ipcMain.handle('pc:popup:toggle', async (e, kind, rect, arg) => {
  fsGuard(e);
  const k = String(kind || '').replace(/[^a-z-]/g, '').slice(0, 24) || 'start';
  if(_popupWin && !_popupWin.isDestroyed() && _popupKind === k){
    closePopupWindow();
    return false;                       // it was open; it is closed now
  }
  return openPopupWindow(e, kind, rect, arg);
});
ipcMain.handle('pc:popup:open', async (e, kind, rect, arg) => {
  fsGuard(e);
  return openPopupWindow(e, kind, rect, arg);
});
async function openPopupWindow(e, kind, rect, arg){
  const k = String(kind || '').replace(/[^a-z-]/g, '').slice(0, 24) || 'start';
  const sticky = STICKY_POPUPS.has(k);
  const r = (rect && typeof rect === 'object') ? rect : {};
  const num = (v, min, max, dflt) => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.max(min, Math.min(max, Math.round(n))) : dflt;
  };
  /* WHICH SURFACE OWNS THIS, DECIDED BEFORE ANYTHING IS CLOSED OR CREATED.
   *
   * A tick reaches EVERY shell renderer, so on two monitors both call this. Deciding afterwards was
   * a race that ate the menu: the focused surface opened its window, the other one called in, the
   * `closePopupWindow()` below destroyed the window that had just opened, and only then was the
   * second caller declined — leaving no menu at all, on a keypress that had done everything right.
   * Reported as "start menu glitchey as fuck".
   *
   * The output also supplies the ORIGIN. Each surface measures in its own viewport, so a menu
   * anchored to the Start button at local x=10 was placed at global x=10 — always the leftmost
   * screen, whichever one the person was looking at. */
  const scope = _shellScopes.get(e.sender.id);
  let originX = 0, originY = 0;
  try{
    const outs = await wm().outputs();
    const mine = scope && outs.find(o => o && o.name === scope.output);
    const focused = outs.find(o => o && o.focused);
    if(mine && focused && mine.name !== focused.name) return false;   // the focused surface opens it
    const box = (mine && mine.rect) || (focused && focused.rect);
    if(box){ originX = Math.round(box.x) || 0; originY = Math.round(box.y) || 0; }
  }catch(_){ /* one output, or no compositor — local coordinates are global */ }

  closePopupWindow();
  const p = new BrowserWindow({
    show: false, frame: false, resizable: sticky, skipTaskbar: true,
    title: POPUP_TITLE,
    width: num(r.width, 220, 1400, 420), height: num(r.height, 160, 2200, 560),
    x: Number.isFinite(Number(r.x)) ? Math.round(Number(r.x)) : undefined,
    y: Number.isFinite(Number(r.y)) ? Math.round(Number(r.y)) : undefined,
    transparent: true, backgroundColor: '#00000000', autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true, nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
      additionalArguments: ['--pc-preload-dir=' + __dirname, '--pc-secondary-surface', '--pc-popup-surface'],
    },
  });
  _popupWin = p;
  _popupKind = k;
  /* THE TITLE IS THE HANDLE, so the page must not take it. Electron hands a window its page's
   * <title> — every popup would be called "PosterChan · Nostr", indistinguishable from the two
   * desktop surfaces. That matters twice: sway can only be told to move a window it can name, and
   * pc-window-snap decides what a window IS from its title. */
  p.on('page-title-updated', (e) => e.preventDefault());
  p.once('ready-to-show', () => {
    if(p.isDestroyed()) return;
    p.show();
    placePopupWindow(p, { x: originX + num(r.x, -20000, 20000, 0),
                          y: originY + num(r.y, -20000, 20000, 0),
                          w: p.getBounds().width, h: p.getBounds().height });
  });
  if(!sticky) p.on('blur', () => { if(_popupWin === p) closePopupWindow(); });
  p.on('closed', () => {
    if(_popupWin !== p) return;              // already reported by closePopupWindow
    const kind = _popupKind;
    _popupWin = null; _popupKind = '';
    if(kind) { try{ forwardShellTick({ change: 'run', payload: 'pc:popup-closed:' + kind }); }catch(_){ } }
  });
  /* Connectivity snapshots include one bounded record per configured relay. 400 bytes truncated
   * perfectly valid JSON on ordinary 8-relay accounts, silently putting the popup back on its cold
   * local Relay state. This is still a small URL and is renderer-generated, not shell-evaluated. */
  const extra = String(arg == null ? '' : arg).slice(0, 8192);
  try{ await p.loadURL(APP_URL + '?pcpopup=' + encodeURIComponent(k)
                       + (extra ? '&pcarg=' + encodeURIComponent(extra) : '')); }
  catch(err){ closePopupWindow(); return false; }
  return true;
}
/* WAYLAND GIVES A CLIENT NO SAY IN WHERE ITS WINDOW GOES, and this is the whole reason this
 * function exists. `new BrowserWindow({x, y})` is honoured on X11 and Windows and IGNORED here:
 * xdg-shell has no toplevel positioning, so sway placed the first start menu dead centre —
 * measured at 1070,573 for a 420x560 window on a 2560x1706 output, which is (2560-420)/2 and
 * (1706-560)/2 exactly. A menu anchored to the Start button appeared in the middle of the screen.
 *
 * So the compositor is ASKED, by con_id, once the surface exists. It cannot be asked before that:
 * the window is not in the tree until it maps, which is why this polls rather than firing once.
 * Failure is silent and harmless — a centred popup is worse than an anchored one and better than
 * no popup, so nothing here is allowed to throw into the open path. */
async function placePopupWindow(win, want){
  for(let attempt = 0; attempt < 12; attempt++){
    if(win.isDestroyed() || _popupWin !== win) return;
    try{
      const rows = await wm().windows();
      const row = rows.find(x => String(x.title || '') === POPUP_TITLE);
      if(row){
        /* sway.config maps this title transparent. Reveal only in the SAME transaction as its final
         * geometry, otherwise Wayland's unavoidable initial centre placement visibly flashes. */
        await wm().placeAndReveal(Number(row.id), Math.round(want.x), Math.round(want.y),
                                  Math.round(want.w), Math.round(want.h));
        try{ win.webContents.send('pc:host:popup-placed'); }catch(_){ }
        return;
      }
    }catch(_){ /* no compositor, or it refused — the popup stays where it was put */ }
    await new Promise(res => setTimeout(res, 60));
  }
  /* Do not leave the renderer permanently shielded on a non-Sway compositor. It may honour the
   * BrowserWindow x/y directly; even when it does not, a delayed centred menu is still usable. */
  try{ if(!win.isDestroyed())win.webContents.send('pc:host:popup-placed'); }catch(_){ }
}
ipcMain.handle('pc:popup:close', (e) => { fsGuard(e); closePopupWindow(); return true; });
/* What the popup chose, handed to the SHELL. The popup is its own renderer and cannot call the
 * desktop's openApp directly; the shell already routes `pc:` ticks (that is how Super opens Start
 * and Ctrl+Alt+Del opens the task manager), so this reuses that path rather than inventing one. */
ipcMain.handle('pc:popup:pick', (e, view) => {
  fsGuard(e);
  const v = String(view || '').replace(/[^a-z0-9_:-]/gi, '').slice(0, 48);
  closePopupWindow();
  if(v) forwardShellTick({ change: 'run', payload: 'pc:open:' + v });
  return true;
});
/* THE SAME PATH FOR ANYTHING THAT IS NOT A VIEW NAME. A notification row opens one post, and a
 * reply button opens the composer for one event — neither is an app the launcher can name, and both
 * have to happen in the SHELL rather than inside a 380px popup. `pick` stays the view-name case it
 * has always been; this carries the rest as an opaque string the shell's tick router parses.
 *
 * `keepOpen` is for the surfaces that are not menus: the quick-settings flyout changes the volume
 * without dismissing itself, exactly like the taskbar flyout it replaced. */
ipcMain.handle('pc:popup:act', (e, action, keepOpen) => {
  fsGuard(e);
  /* `%` is allowed because _menuAct percent-encodes its argument — a search query or a file path
   * carries spaces and slashes, and this is one string crossing a process boundary. The shell
   * decodes it; everything outside this set is still dropped. */
  const a = String(action || '').replace(/[^a-z0-9_:.@%-]/gi, '').slice(0, 400);
  if(!keepOpen) closePopupWindow();
  if(a) forwardShellTick({ change: 'run', payload: 'pc:act:' + a });
  return true;
});

ipcMain.handle('pc:wm:close', (e, id) => { fsGuard(e); return wm().close(Number(id)); });
ipcMain.handle('pc:wm:place', (e, id, x, y, w, h) => {
  fsGuard(e); return wm().place(Number(id), Number(x), Number(y), Number(w), Number(h));
});
ipcMain.handle('pc:wm:move', (e, id, x, y) => {
  fsGuard(e); return wm().move(Number(id), Number(x), Number(y));
});
/* Drag a hosted app through a monitor edge. Each output is a different Electron surface, so DOM
 * pointer coordinates cannot continue into the next renderer. The compositor performs the
 * ownership hand-off; the destination renderer then adopts the still-running native window. */
function adjacentShellSurface(e, direction){
  const scope = _shellScopes.get(e.sender.id);
  if(!scope) throw new Error('this desktop surface has no display');
  const from = scope.rect || {};
  const cx = (Number(from.x)||0) + (Number(from.width)||0)/2;
  const cy = (Number(from.y)||0) + (Number(from.height)||0)/2;
  const rows = Array.from(_shellSurfaces.values()).filter(r=>r && r.assignment && r.browser
    && !r.browser.isDestroyed() && r.browser.webContents.id !== e.sender.id);
  const candidates = rows.map(record=>{
    const a=record.assignment, r=a.rect||{}, x=(Number(r.x)||0)+(Number(r.width)||0)/2,
          y=(Number(r.y)||0)+(Number(r.height)||0)/2;
    const forward = direction==='left' ? cx-x : direction==='right' ? x-cx
                  : direction==='up' ? cy-y : y-cy;
    const cross = /left|right/.test(direction) ? Math.abs(y-cy) : Math.abs(x-cx);
    return {record,forward,cross};
  }).filter(x=>x.forward>0).sort((a,b)=>a.forward-b.forward||a.cross-b.cross);
  return candidates.length ? candidates[0].record : null;
}

ipcMain.handle('pc:wm:handoff', async (e, id, direction, drop) => {
  fsGuard(e);
  direction=String(direction||'');
  drop=drop&&typeof drop==='object'?Object.assign({},drop,{direction}):{direction};
  const record=adjacentShellSurface(e, direction);
  if(!record) return false;
  /* Sending to a loading renderer succeeds but reaches no listener. Refuse before moving the real
   * native surface, so a cross-monitor drag during reload cannot lose Firefox/Telegram. */
  if(!_handoffReady.has(Number(record.browser.webContents.id)) ||
     record.browser.webContents.isLoadingMainFrame()) return false;
  const target=record.assignment;
  const nativeId=Number(id), rows=await wm().windows();
  const before=rows.find(row=>Number(row.id)===nativeId);
  if(!before || !before.rect) return false;
  const sourceWorkspace=String(before.workspace||_nativeOwners.get(nativeId)||'');
  const token=nativeId+'-'+Date.now()+'-'+Math.random().toString(36).slice(2);
  const abort=async()=>{
    const pending=_nativeHandoffAcks.get(token);
    if(pending){clearTimeout(pending.timer);_nativeHandoffAcks.delete(token);}
    try{record.browser.webContents.send('pc:wm:native-handoff-abort',{token,id:nativeId});}catch(_send){}
  };
  return runAtomicHandoff({
    prepare:()=>{
      const ack=new Promise(resolve=>{
        const timer=setTimeout(()=>{_nativeHandoffAcks.delete(token);resolve(null);},1800);
        _nativeHandoffAcks.set(token,{contentsId:Number(record.browser.webContents.id),resolve,timer});
      });
      record.browser.webContents.send('pc:wm:native-handoff-prepare',
                                      {token,row:before,direction,drop});
      return ack;
    },
    commit:async prepared=>{
      const b=target.rect||{}, right=Number(b.x)+Number(b.width), bottom=Number(b.y)+Number(b.height);
      if(prepared.x<Number(b.x)||prepared.y<Number(b.y)||prepared.x+prepared.w>right||
         prepared.y+prepared.h>bottom) throw new Error('destination frame geometry is outside output');
      await wm().finishMove(nativeId);
      await wm().moveToAssignment(nativeId,target);
      await wm().place(nativeId,prepared.x,prepared.y,prepared.w,prepared.h);
      _nativeOwners.set(nativeId,String(target.workspace));
      const moved=(await wm().windows()).find(row=>Number(row.id)===nativeId);
      if(!moved) throw new Error('native surface disappeared during handoff');
      record.browser.webContents.send('pc:wm:native-handoff',{token,row:moved,rect:prepared});
      await decorateNative(nativeId);
      await wm().focus(nativeId);
      return {output:target.output,workspace:target.workspace};
    },
    rollback:async()=>{
      try{
        if(sourceWorkspace){
          const source=Array.from(_shellSurfaces.values()).find(r=>r&&r.assignment&&String(r.assignment.workspace)===sourceWorkspace);
          if(source)await wm().moveToAssignment(nativeId,source.assignment);
          await wm().place(nativeId,before.rect.x,before.rect.y,before.rect.width,before.rect.height);
          _nativeOwners.set(nativeId,sourceWorkspace);
          await wm().focus(nativeId);
        }
      }catch(_rollback){}
    },
    abort,
  },2000);
});

/* DOM application windows live inside one output renderer. Transfer their durable identity to the
 * adjacent renderer, which recreates the frame there; this is the counterpart to the compositor
 * move above and is what makes Notes, Messages and every other PosterChan app cross a monitor. */
ipcMain.handle('pc:wm:handoff-frame', async (e, payload, direction) => {
  fsGuard(e);
  const record=adjacentShellSurface(e, String(direction||''));
  if(!record) return false;
  if(!_handoffReady.has(Number(record.browser.webContents.id)) ||
     record.browser.webContents.isLoadingMainFrame()) return false;
  const p=payload && typeof payload==='object' ? payload : {};
  record.browser.webContents.send('pc:wm:handoff-frame', {
    view:String(p.view||''), title:String(p.title||''), icon:String(p.icon||''),
    width:Number(p.width)||0, height:Number(p.height)||0, direction:String(direction||''),
    overflow:Number(p.overflow)||0,
    scrollTop:Math.max(0,Number(p.scrollTop)||0),
    terminalSid:String(p.terminalSid||''),
    messagesTab:String(p.view||'')==='messages'&&
      (p.messagesTab==='concord'||p.messagesTab==='messages')?p.messagesTab:'',
    /* THIS LIST IS AN ALLOWLIST, so a field the renderer sends and this does not name is dropped
     * in silence — the payload arrives, looks complete, and one thing is simply missing.
     *
     * `path` is how the destination reopens a repo/article/stream rather than a bare view name.
     * It is a SAME-ORIGIN PATH and is checked as one here as well as at both ends: it is handed to
     * history.replaceState, and a protocol-relative `//host` would be an origin the user never
     * navigated to. Must start with exactly one slash; bounded, because a URL is not a payload. */
    path:(()=>{ const v=String(p.path||'');
      return /^\/(?!\/)/.test(v) && v.length<=2048 ? v : ''; })(),
    ui:(()=>{ try{
      const s=p.ui==null?null:JSON.parse(JSON.stringify(p.ui));
      return s!=null && JSON.stringify(s).length<=512*1024 ? s : null;
    }catch(_){ return null; } })(),
    state:(()=>{ try{
      const s=p.state==null?null:JSON.parse(JSON.stringify(p.state));
      return s!=null && JSON.stringify(s).length<=512*1024 ? s : null;
    }catch(_){ return null; } })()
  });
  await wm().focus(Number(record.conId));
  return {output:record.assignment.output,workspace:record.assignment.workspace};
});
ipcMain.handle('pc:wm:preview-frame', (e, payload, direction) => {
  fsGuard(e);
  const record=adjacentShellSurface(e, String(direction||''));
  if(!record) return false;
  const p=payload && typeof payload==='object' ? payload : null;
  record.browser.webContents.send('pc:wm:preview-frame', p);
  return true;
});
ipcMain.handle('pc:wm:hide', (e, id) => { fsGuard(e); return wm().hide(Number(id)); });
ipcMain.handle('pc:wm:show', (e, id) => { fsGuard(e); return wm().show(Number(id)); });
ipcMain.handle('pc:wm:restore', (e, id, x, y, w, h) => {
  fsGuard(e); return wm().restore(Number(id),Number(x),Number(y),Number(w),Number(h));
});
/* Renderer timers are throttled/frozen when Chromium loses visibility. Alt+Tab briefly raises the
 * tiled desktop with compositor fullscreen so its chooser is visible above floating apps; if its
 * keyup/timer is frozen, that fullscreen shell hides every other window indefinitely. Keep the
 * mandatory failsafe in the main process, whose timers are not renderer-lifecycle dependent. */
const _shellFullscreenFailsafes = new Map();
ipcMain.handle('pc:wm:fullscreen', async (e, id, on) => {
  fsGuard(e);
  const n=Number(id), enable=!!on;
  const old=_shellFullscreenFailsafes.get(n);
  if(old){ clearTimeout(old); _shellFullscreenFailsafes.delete(n); }
  const result=await wm().fullscreen(n, enable);
  let shellWindow=false;
  if(enable && SHELL_MODE){
    try{
      const rows=await wm().windows();
      const row=(rows||[]).find(x=>Number(x&&x.id)===n);
      shellWindow=!!(row && /^(?:posterchan(?:-desktop)?|place\.poster\.desktop)$/i.test(String(row.app||''))
        && /^PosterChan(?: · Nostr)?$/i.test(String(row.title||'')));
    }catch(_){ shellWindow=false; }
  }
  if(shellWindow){
    const timer=setTimeout(()=>{
      _shellFullscreenFailsafes.delete(n);
      wm().fullscreen(n,false).catch(()=>{});
    }, 3000);
    _shellFullscreenFailsafes.set(n,timer);
  }
  return result;
});
ipcMain.handle('pc:wm:snap', (e, id, zone) => { fsGuard(e); return wm().snap(Number(id), String(zone||'')); });
function remoteAbsolutePoint(displays, displayId, cursor, nx, ny){
  nx=Number(nx);ny=Number(ny);
  if(!Number.isFinite(nx)||!Number.isFinite(ny)||nx<0||nx>1||ny<0||ny>1)return null;
  displays=Array.isArray(displays)?displays:[];
  const display=displays.find(d=>String(d&&d.id)===String(displayId))
    || screen.getDisplayNearestPoint(cursor);
  if(!display||!display.bounds)return null;
  const b=display.bounds;
  return {x:b.x+Math.round(nx*Math.max(0,b.width-1)),
          y:b.y+Math.round(ny*Math.max(0,b.height-1))};
}
ipcMain.handle('pc:remote:input', (e, input) => {
  fsGuard(e);
  if(!SHELL_MODE) return false;
  if(input && (input.type === 'absolute' || input.type === 'button') &&
      (input.type === 'absolute' || input.x != null || input.y != null)) {
    const nx = Number(input.x), ny = Number(input.y);
    const point = screen.getCursorScreenPoint();
    const mapped=remoteAbsolutePoint(screen.getAllDisplays(),remoteControlDisplayId,point,nx,ny);
    if(!mapped)return false;
    input = Object.assign({}, input, mapped);
  }
  return remotecontrol.input(input);
});
ipcMain.handle('pc:remote:configure', (e, raw) => {
  fsGuard(e);
  if(!SHELL_MODE)return false;
  const width=Math.round(Number(raw&&raw.width)),height=Math.round(Number(raw&&raw.height));
  if(!Number.isFinite(width)||!Number.isFinite(height)||width<64||height<64||width>32768||height>32768)
    return false;
  /* A portal-selected source without display_id can still be identified by its captured pixel
   * dimensions. Accept a UNIQUE best match only; equal-resolution monitors deliberately retain
   * the display frozen when sharing began instead of jumping nondeterministically. */
  if(!remoteControlDisplayExplicit){
    const ranked=screen.getAllDisplays().filter(d=>d&&d.bounds).map(d=>{
      const b=d.bounds,s=Math.max(.1,Number(d.scaleFactor)||1);
      const score=Math.min(Math.abs(b.width-width)+Math.abs(b.height-height),
                           Math.abs(Math.round(b.width*s)-width)+Math.abs(Math.round(b.height*s)-height));
      return {d,score};
    }).sort((a,b)=>a.score-b.score);
    if(ranked.length&&(!ranked[1]||ranked[0].score<ranked[1].score))remoteControlDisplayId=String(ranked[0].d.id);
  }
  return {ok:true,displayId:remoteControlDisplayId,width,height};
});
ipcMain.handle('pc:remote:release', (e) => { fsGuard(e); return SHELL_MODE ? remotecontrol.release() : false; });
/* Decorating the FIRST native window is also when the palette is (re)applied: it is the earliest
 * moment we know a compositor is there and something is about to be drawn with it, and it costs
 * seven idempotent commands once per session. Without this the colours come only from a config file
 * the machine may have been installed with months ago. */
let _chromeDone = false;
async function decorateNative(id, hosted){
  if(!_chromeDone){ _chromeDone = true; try{ await wm().applyChrome(); }catch(_){ _chromeDone = false; } }
  /* WHOEVER DRAWS THE CHROME, SOMETHING MUST.
   *
   * `border none` was right while the shell HOSTED native windows: the PosterChan frame was the
   * only chrome, and a second sway titlebar around it was the mismatched decoration people saw.
   * Once hosting stopped being the default the same line left Firefox with no frame AND no border
   * — reported as "firefox does not even have a window decoration? cant maximize and minimize?".
   *
   * So it follows the hosting decision instead of assuming it. Unhosted, sway draws its own
   * titlebar: a title, a drag handle, right-click for its menu, and double-click to fullscreen. */
  return wm().decorate(Number(id),!!hosted);
}
ipcMain.handle('pc:wm:decorate', async (e, id, hosted) => {
  fsGuard(e);
  /* The PosterChan HTML frame is the only chrome. A second Sway titlebar is the mismatched
   * Firefox/Telegram decoration users were seeing around it.
   *
   * `sticky` is persistent compositor state, not useful application state. Firefox private
   * windows and Telegram can inherit it from a prior session; then the surface follows every
   * workspace, appears above unrelated PosterChan frames and can be claimed by the wrong output.
   * Clear it by exact con_id whenever a surface is adopted. Do not blanket-disable fullscreen:
   * games and videos deliberately own that state and the renderer tracks it separately. */
  return decorateNative(id, !!hosted);
});
ipcMain.handle('pc:display:status', (e) => { fsGuard(e); return displays().status(); });
ipcMain.handle('pc:display:preview', (e, rows) => { fsGuard(e); return displays().preview(rows); });
ipcMain.handle('pc:display:confirm', (e, token) => { fsGuard(e); return displays().confirm(token); });
ipcMain.handle('pc:display:revert', (e, token) => { fsGuard(e); return displays().revert(token); });
ipcMain.handle('pc:liveusb:devices', (e) => { fsGuard(e); return liveusb.devices(); });
ipcMain.handle('pc:liveusb:status', (e) => { fsGuard(e); return liveusb.status(); });
ipcMain.handle('pc:liveusb:build', (e, dir, home) => { fsGuard(e); return liveusb.build(String(dir||''), !!home); });
ipcMain.handle('pc:liveusb:burn', (e, iso, disk) => { fsGuard(e); return liveusb.burn(String(iso||''), String(disk||'')); });
ipcMain.handle('pc:liveusb:pick-iso', async (e) => {
  fsGuard(e); const r=await dialog.showOpenDialog(win,{properties:['openFile'],filters:[{name:'ISO images',extensions:['iso']}]});
  return r.canceled?'':(r.filePaths[0]||'');
});
ipcMain.handle('pc:liveusb:pick-dir', async (e) => {
  fsGuard(e); const r=await dialog.showOpenDialog(win,{properties:['openDirectory','createDirectory']});
  return r.canceled?'':(r.filePaths[0]||'');
});
/* Launch takes an ARGV ARRAY, never a command string. A string would have to be handed to a shell
 * to be useful, and then a file name with a space in it is an injection. */
ipcMain.handle('pc:wm:launch', async (e, argv, opts) => {
  fsGuard(e);
  /* CANDIDATES ARE RESOLVED HERE, because only this side can look at the filesystem. A launcher in
   * the page cannot know that Gentoo installs firefox as /usr/bin/firefox-bin and not
   * /usr/bin/firefox — and a hardcoded path that does not exist starts nothing, silently, which is
   * indistinguishable from a broken launcher. `candidates` is a list of whole command lines; the
   * first whose program exists wins. */
  let list;
  if (opts && opts.candidates && Array.isArray(argv)) {
    const tried = [];
    for (const cand of argv) {
      const av = (Array.isArray(cand) ? cand : [cand]).map(String).filter(Boolean);
      if (!av.length) continue;
      tried.push(av[0]);
      try { fs.accessSync(av[0], fs.constants.X_OK); list = av; break; } catch (_) {}
    }
    if (!list) return { pid: null, window: null, why: 'not installed (looked for ' + tried.join(', ') + ')' };
  } else {
    list = (Array.isArray(argv) ? argv : []).map(String).filter(Boolean);
  }
  if (!list.length) throw new Error('nothing to launch');
  let launchOpts = opts || {};
  /* Wayfire is floating-first; games get their own nested compositor so resolution changes,
   * pointer constraints and Proton helper surfaces cannot destabilize the desktop compositor.
   * The active Wayfire output owns the new fullscreen surface, so this naturally follows the
   * monitor whose Start menu launched it. Sway remains an untouched rollback backend. */
  if(opts&&opts.game&&process.env.WAYFIRE_SOCKET){
    const gamescope='/usr/bin/gamescope';
    try{
      fs.accessSync(gamescope,fs.constants.X_OK);
      const steam=/^steam(?:-native)?$/i.test(path.basename(list[0]));
      list=[gamescope,'-f','-b','--force-windows-fullscreen',...(steam?['-e']:[]),'--',...list];
    }catch(_){ return {pid:null,window:null,why:'gamescope is required to launch games on PosterChanOS'}; }
  }
  /* TELEGRAM 7 + QT 6.9 ON SWAY: its native Wayland QRhi window probes AMD OpenGL correctly,
   * then Qt fails `EGL_WL_bind_wayland_display`. The window paints its spinner and turns black as
   * soon as rendering moves to that surface. XWayland on the same Mesa driver initializes GLX and
   * stays painted. Scope the fallback to Telegram — forcing every Qt app through X11 would throw
   * away native Wayland and would be particularly wrong for Steam/games.
   *
   * Sway starts XWayland lazily, and this shell was exec'd before it, so DISPLAY can be absent from
   * our inherited environment even though its socket is :0. That is why the fallback supplies it. */
  if (/^(telegram-desktop|telegram-desktop-bin)$/i.test(path.basename(list[0]))) {
    launchOpts = Object.assign({}, launchOpts, { env: Object.assign({}, launchOpts.env || {}, {
      QT_QPA_PLATFORM: 'xcb', DISPLAY: process.env.DISPLAY || ':0',
    }) });
  }
  /* Firefox must be a native Wayland client. An inherited/stale GDK_BACKEND=x11 put it through
   * XWayland with a 3822px client geometry inside a 1278px compositor box; pointer confinement then
   * hit an invisible wall partway across the monitor—the same symptom games exposed. */
  let firefoxRunning=false;
  if (/^(firefox|firefox-bin)$/i.test(path.basename(list[0]))) {
    /* A running Firefox owns its profile and remote endpoint. Starting a command for that X11
     * instance with a contradictory forced-Wayland environment produces a headless process and
     * "failed to open display" instead of a new window. Reuse it as-is; native Wayland becomes the
     * default the next time Firefox is fully exited and starts as the first process. */
    try{
      firefoxRunning=fs.readdirSync('/proc').some(pid=>/^\d+$/.test(pid) && (()=>{
        try{return /(?:^|\/)(?:firefox|firefox-bin)(?:\0|$)/.test(fs.readFileSync('/proc/'+pid+'/cmdline','utf8'));}
        catch(_){return false;}
      })());
    }catch(_){}
  }
  if (/^(firefox|firefox-bin)$/i.test(path.basename(list[0])) && !firefoxRunning) {
    launchOpts = Object.assign({}, launchOpts, { env: Object.assign({}, launchOpts.env || {}, {
      GDK_BACKEND: 'wayland', MOZ_ENABLE_WAYLAND: '1',
      WAYLAND_DISPLAY: process.env.WAYLAND_DISPLAY || 'wayland-1',
    }) });
  }
  /* Firefox's private-window command delegates to an already-running profile. Its new surface
   * belongs to the OLD browser PID, not to the short-lived command we spawn, so ancestry matching
   * can never see it. Snapshot exact con_ids before launch and accept only a new Firefox surface. */
  let firefoxBefore=[];
  if(firefoxRunning){
    try{firefoxBefore=(await wm().windows()).filter(w=>/firefox/i.test(String(w.app||''))).map(w=>w.id);}
    catch(_){firefoxBefore=[];}
  }
  const started = wm().launch(list, launchOpts);
  /* The window is matched by PID and reported back, so the desktop can place what it just opened
   * rather than guessing which of several windows appeared. Null when it never shows — an app that
   * failed to start must not be reported as launched.
   *
   * RACED AGAINST THE FAILURE, because spawn reports a missing program asynchronously: waiting only
   * for a window turns "not installed" into fifteen seconds of a launcher that appears to do
   * nothing, and then a null that says no more than a timeout would. */
  const waitMs=(opts && opts.waitMs) || 15000;
  const appeared=firefoxRunning
    ? wm().waitForNewWindow(firefoxBefore,waitMs,w=>/firefox/i.test(String(w.app||'')))
    : wm().waitForWindow(started.pid,waitMs);
  const settled = await Promise.race([
    appeared.then((w) => ({ window: w })),
    (started.failed || new Promise(() => {})).then((why) => ({ why })),
  ]);
  if (settled.why) return { pid: null, window: null, why: settled.why };
  return { pid: started.pid, window: settled.window };
});
/* EVERY APP INSTALLED ON THIS MACHINE, for the start menu — "should be able to manage/open any
 * game/app under PosterChan Desktop".
 *
 * The scan and the spec's filtering live in apps.js and are tested there. This is the wiring, plus
 * the one decision only this side can make: a `Terminal=true` entry (`btop`, `nvim`, an installer
 * script) has no window of its own and must be run INSIDE a terminal, or clicking it starts a
 * process with no stdout attached to anything and nothing at all appears. Which terminal exists is
 * a question about this disk, so it is answered here rather than by a page guessing at a path.
 *
 * Deliberately NOT PosterChan's own terminal: that is a PTY the page draws, and handing it an argv
 * to run instead of a login shell is a second contract on a screen that already resumes sessions.
 * `foot` is on this profile and is what $mod+Return already opens. */
const TERMINALS = [['/usr/bin/foot', '-e'], ['/usr/local/bin/foot', '-e'],
                   ['/usr/bin/xterm', '-e'], ['/usr/bin/alacritty', '-e']];
function terminalPrefix() {
  for (const t of TERMINALS) {
    try { fs.accessSync(t[0], fs.constants.X_OK); return t; } catch (_) {}
  }
  return null;
}

ipcMain.handle('pc:apps:list', (e) => {
  fsGuard(e);
  let scanned = { apps: [], skipped: [], dirs: [] };
  try { scanned = require('./apps.js').scan(); }
  catch (err) { return { apps: [], why: String((err && err.message) || err) }; }
  const term = terminalPrefix();
  const apps = [];
  const A = require('./apps.js');
  /* THE ICON IS RESOLVED HERE, ONCE PER SCAN. `Icon=` is a theme name and the renderer lives on the
   * app:// origin, which can read neither an icon theme nor a file:// path — so every scanned
   * program drew as the same generic square ("start menu ... missing icons"). The lookup is a walk
   * over icon directories and the answer is a data: URI, both of which only this side can produce.
   *
   * Cached by NAME across the loop: a desktop full of one toolkit's apps shares icons, and this is
   * called every time the start menu is opened. */
  const iconCache = new Map();
  const uriFor = (a) => {
    const argv0 = a && a.argv && a.argv[0] ? path.basename(a.argv[0]) : '';
    const candidates = [a && a.icon, argv0, String(argv0).replace(/-bin$/i, ''),
      String(a && a.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-')].filter(Boolean);
    const key = candidates.join('\n');
    if (!iconCache.has(key)) {
      let u = '';
      for (const n of candidates) {
        try { u = A.iconDataUri(n); } catch (_) { u = ''; }
        if (u) break;
      }
      iconCache.set(key, u);
    }
    return iconCache.get(key);
  };
  for (const a of scanned.apps) {
    if (a.terminal && !term) continue;   // nothing could run it, so offering it is a dead button
    apps.push(Object.assign({}, a, {
      argv: a.terminal ? term.concat(a.argv) : a.argv,
      iconUri: uriFor(a),
    }));
  }
  /* `skipped` is carried, counted rather than listed: "why is Foo not in my menu" is a real
   * question and the honest answer is in the entry's own file (NoDisplay, NotShowIn, TryExec). */
  return { apps, skipped: scanned.skipped.length, dirs: scanned.dirs, terminal: !!term };
});

/* THE COMPUTER'S OWN FILES. See hostfs.js for what limits this and why it is not a path allowlist.
 *
 * The same `fsGuard` as everything else, and for the boundary it actually enforces: our own page
 * rather than some other page. Every one of these THROWS rather than returning an empty answer,
 * because "I could not read that directory" and "that directory is empty" are different facts and
 * a file manager that confuses them shows somebody an empty folder full of their files. */
let _hostfs = null;
const hostfs = () => (_hostfs || (_hostfs = require('./hostfs.js')));

ipcMain.handle('pc:host:list', (e, dir) => { fsGuard(e); return hostfs().list(String(dir || '')); });
ipcMain.handle('pc:host:roots', (e) => { fsGuard(e); return hostfs().roots(); });
ipcMain.handle('pc:host:notify', (e, options) => {
  fsGuard(e);
  const o=options&&typeof options==='object'?options:{},Notification=electron.Notification;
  if(!Notification||!Notification.isSupported())return false;
  const owner=BrowserWindow.fromWebContents(e.sender)||win,route=String(o.route||'notifications').slice(0,220);
  const note=new Notification({title:String(o.title||'PosterChan').slice(0,120),
    body:String(o.body||'').slice(0,1000),icon:path.join(__dirname,'icon.png'),silent:false});
  note.on('click',()=>{try{if(owner){owner.show();owner.focus();}if(!e.sender.isDestroyed())e.sender.send('pc:host:notification-click',route);}catch(_){}});
  note.show();return true;
});
ipcMain.handle('pc:host:pickDirectory', async (e) => {
  fsGuard(e);
  const owner = BrowserWindow.fromWebContents(e.sender) || win;
  const r = await dialog.showOpenDialog(owner, { title: 'Open project folder', properties: ['openDirectory'] });
  return r.canceled || !r.filePaths || !r.filePaths[0] ? null : hostfs().clean(r.filePaths[0]);
});
ipcMain.handle('pc:host:pickFile', async (e, options) => {
  fsGuard(e);
  const o=options && typeof options==='object' ? options : {};
  const filters=o.images ? [{name:'Images',extensions:['jpg','jpeg','png','gif','webp','heic','heif','avif']}]
                         : [{name:'All files',extensions:['*']}];
  const r=await dialog.showOpenDialog(win,{title:String(o.title||'Choose a file').slice(0,80),
    properties:['openFile'],filters});
  if(r.canceled || !r.filePaths[0])return null;
  const file=r.filePaths[0],st=fs.statSync(file),max=Math.min(Math.max(Number(o.max)||32*1024*1024,1),64*1024*1024);
  if(!st.isFile())throw new Error('that is not a file');
  if(st.size>max)throw new Error('that file is too large');
  const ext=path.extname(file).toLowerCase(),types={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png',
    '.gif':'image/gif','.webp':'image/webp','.heic':'image/heic','.heif':'image/heif','.avif':'image/avif'};
  return {name:path.basename(file),path:file,type:types[ext]||'application/octet-stream',size:st.size,
    mtime:Math.round(st.mtimeMs),data:fs.readFileSync(file)};
});
ipcMain.handle('pc:host:saveFile', async (e, name, bytes) => {
  fsGuard(e);
  const owner=BrowserWindow.fromWebContents(e.sender)||win;
  const r=await dialog.showSaveDialog(owner,{title:'Save document',defaultPath:path.basename(String(name||'document'))});
  if(r.canceled||!r.filePath)return null;
  const data=Buffer.from(bytes||[]);
  if(data.length>256*1024*1024)throw new Error('that file is too large');
  // A failed write must not truncate an existing document selected in Save As.
  const tmp=path.join(path.dirname(r.filePath),'.pc-'+path.basename(r.filePath)+'.tmp');
  fs.writeFileSync(tmp,data);
  try{fs.renameSync(tmp,r.filePath);}catch(err){try{fs.unlinkSync(tmp);}catch(_){}throw err;}
  return hostfs().clean(r.filePath);
});
/* PosterChan Code editing a file on THIS computer. The guards (size, NUL bytes, atomic rename,
 * mtime compare-and-swap) are in hostfs.js, where a bridge cannot be talked out of them. */
ipcMain.handle('pc:host:readText', (e, p) => { fsGuard(e); return hostfs().readText(String(p || '')); });
ipcMain.handle('pc:host:writeText', (e, p, text, mtime) => { fsGuard(e);
  return hostfs().writeText(String(p || ''), String(text == null ? '' : text), Number(mtime) || 0); });
ipcMain.handle('pc:host:writeBytes', (e, p, bytes, mtime) => { fsGuard(e);
  return hostfs().writeBytes(String(p || ''), bytes, Number(mtime) || 0); });
/* Searched from the start menu, so it is called while somebody is typing. Every bound lives in
 * hostfs.search; the only thing decided here is that the renderer does not get to raise them —
 * `limit` and `ms` are clamped there, and a page asking for a 10-minute walk of the whole disk on
 * each keystroke is exactly the sort of thing this bridge exists to refuse. */
ipcMain.handle('pc:host:search', (e, q, opts) => {
  fsGuard(e);
  const o = opts || {};
  return hostfs().search(String(q || ''), { limit: Number(o.limit) || 8, ms: Number(o.ms) || 350 });
});
ipcMain.handle('pc:host:gitStatus', (e, root) => { fsGuard(e); return hostfs().gitStatus(String(root||'')); });
ipcMain.handle('pc:host:gitDiff', (e, root, p) => { fsGuard(e); return hostfs().gitDiff(String(root||''),String(p||'')); });
ipcMain.handle('pc:host:gitAction', (e, root, action, paths, message) => { fsGuard(e);
  return hostfs().gitAction(String(root||''),String(action||''),Array.isArray(paths)?paths:[],String(message||'')); });
ipcMain.handle('pc:host:mkdir', (e, dir, name) => {
  fsGuard(e); return hostfs().mkdir(String(dir || ''), String(name || ''));
});
ipcMain.handle('pc:host:rename', (e, from, to) => {
  fsGuard(e); return hostfs().rename(String(from || ''), String(to || ''));
});
ipcMain.handle('pc:host:trash', (e, target) => { fsGuard(e); return hostfs().trash(String(target || '')); });
ipcMain.handle('pc:host:transfer', (e, items, destination, move) => {
  fsGuard(e);
  return hostfs().transfer(Array.isArray(items) ? items.map(String) : [], String(destination || ''), !!move);
});
ipcMain.handle('pc:host:read', (e, target, max) => {
  fsGuard(e);
  const p = hostfs().clean(String(target || ''));
  if (!p) throw new Error('not a path');
  const cap = Math.min(Number(max) || 64 * 1024 * 1024, 256 * 1024 * 1024);
  /* BOUNDED, and the bound is checked before the read rather than after. This crosses an IPC
   * boundary into a renderer's heap; a disk image read whole is the renderer dying, which on this
   * machine is the desktop disappearing. */
  const st = fs.statSync(p);
  if (st.size > cap) throw new Error('that file is too big to open here (' + st.size + ' bytes)');
  return fs.readFileSync(p);
});
ipcMain.handle('pc:host:open', async (e, target) => {
  fsGuard(e);
  const r = hostfs().open(String(target || ''));
  /* RACED against the failure, the same way `pc:wm:launch` is: spawn reports a missing program
   * asynchronously, so returning immediately turns "nothing on this machine opens that" into a
   * click that silently does nothing. */
  const why = await Promise.race([r.failed, new Promise((res) => setTimeout(() => res(''), 700))]);
  return why ? { ok: false, why } : { ok: true, pid: r.pid };
});

/* Events, forwarded to the page. A shell that polls for its own window list is a shell that is
 * always slightly wrong about what is on screen. */
ipcMain.handle('pc:wm:subscribe', async (e) => {
  fsGuard(e);
  const w = wm();
  if (w.__forwarding) return true;
  w.__forwarding = true;
  /* `tick` is how the COMPOSITOR talks to this shell, and it is the only channel there is.
   *
   * A key binding in sway can only run a command — it cannot call into us — so a binding that has
   * to reach the desktop runs `swaymsg -t send_tick <payload>`, which sway broadcasts to every
   * subscriber. That is what makes the Super key open the start menu even while FIREFOX has the
   * keyboard, which is the case that matters: a key handler in this page only ever fires when this
   * page is focused, and the moment you want a start menu is usually the moment something else is. */
  /* Tick forwarding is owned by wireShellRecovery(), whose lifetime is the shell process rather
   * than a renderer's startup. Keeping it here too would toggle Start twice. */
  /* Include tick on the FIRST subscription. WM.subscribe intentionally owns one socket and returns
   * when it already exists; if the renderer reached this call before wireShellRecovery(), the old
   * list permanently omitted tick and every Super shortcut ran in Sway but vanished before the
   * page. wireShellRecovery still owns the one tick listener, so this does not double-deliver. */
  const NAMES = ['window', 'workspace', 'output', 'tick'];
  await w.subscribe(NAMES);
  for (const name of NAMES) {
    /* ONE PRESS, ONE DELIVERY — and this loop was the second one.
     *
     * The comment above says tick forwarding belongs to wireShellRecovery() and that keeping it
     * here too would toggle Start twice. It describes the fix; the code never made it. Both sites
     * registered a `tick` listener on the same socket, so EVERY compositor binding arrived at the
     * renderer twice. Measured on the two-monitor machine by adding a second `pcWM.onEvent`
     * listener through the debugger and sending two ticks by hand:
     *     {"pc:probe-one":2,"pc:probe-two":2}
     * That is Alt+Tab stepping two windows per press (and therefore running off the end of the list
     * and throwing the gesture at the other monitor on the FIRST press), Super opening the start
     * menu and closing it again, Print Screen saving two files, Alt+Return opening two terminals.
     * Every one of those reads as "the key does nothing" or "it jumps", never as a double event.
     *
     * `subscribe` still names tick — the socket's name list is fixed on first subscription, and a
     * renderer that got here before wireShellRecovery would otherwise strand the whole keyboard. */
    if (name === 'tick') continue;
    w.on(name, (ev) => {
      if(name === 'output') scheduleDisplayReconcile();
      /* A shell surface can be moved by the same compositor command path used for native/window
       * handoffs.  Output topology did not change, so listening only for `output` left the moved
       * renderer alive on a hidden workspace while its monitor showed an empty black workspace.
       * Reconcile only shell-window events: ordinary application moves must not churn the desktop,
       * and the debounce absorbs the placement commands' own follow-up event. */
      if(name === 'window' && ev && ev.container){
        const c=ev.container, appId=String(c.app_id||''), pid=Number(c.pid);
        if(appId==='place.poster.desktop' || pid===process.pid) scheduleDisplayReconcile();
      }
      /* window::new already contains the container we need. Throwing it away forced the renderer
       * to ask for the entire sway tree (and PCOSShell to ask once more) before it could draw a
       * frame around a freshly launched app. On a busy terminal launch that is visibly late: btop
       * appears, then its PosterChan frame catches up. Send the one normalized leaf with the event;
       * the ordinary tree refresh remains the recovery path for rename/close/workspace changes. */
      let window = null;
      if(name === 'window' && ev && ev.wayfireView) window=ev.wayfireView;
      else if (name === 'window' && ev && ev.container) {
        try {
          const { flatten } = require('./wm.js');
          window = flatten(ev.container, [], '')[0] || null;
        } catch (_) {}
      }
      const deliver = async () => {
        let targets = BrowserWindow.getAllWindows();
        /* No tick reaches this loop any more; `forwardShellTick` owns that channel and does the
         * focused-output filtering there. Leaving a second copy of the rule here is how the two
         * paths drifted in the first place. */
        for (const target of targets) {
        try {
          const scope = _shellScopes.get(target.webContents.id);
          const owner = window && (window.stashed ? _nativeOwners.get(Number(window.id))
                                                   : String(window.workspace || ''));
          const localWindow = !scope || !window || owner === String(scope.workspace) ? window : null;
          target.webContents.send('pc:wm:event',
            { name, change: ev && ev.change, payload: ev && ev.payload, window: localWindow });
        } catch (_) {}
        }
      };
      deliver().catch(()=>{});
    });
  }
  return true;
});

/* Power, brightness, battery, sleep — and the mixer. Same guard as everything else: `setBrightness`
 * writes to sysfs and `poweroff` ends the session, so neither may be reachable from any page but
 * ours. Each answers "absent" for hardware that is not there rather than throwing, because a tower
 * has no battery and no backlight and neither is a fault to report. */
const power = require('./power.js');
const audio = require('./audio.js');

const printers = require('./printers');
ipcMain.handle('pc:printers:status', (e) => { fsGuard(e); return printers.status(); });
ipcMain.handle('pc:printers:discover', (e) => { fsGuard(e); return printers.discover(); });
ipcMain.handle('pc:printers:add', (e, spec) => { fsGuard(e); return printers.add(spec || {}); });
ipcMain.handle('pc:printers:default', (e, n) => { fsGuard(e); return printers.setDefault(n); });
ipcMain.handle('pc:printers:remove', (e, n) => { fsGuard(e); return printers.remove(n); });
ipcMain.handle('pc:printers:test', (e, n) => { fsGuard(e); return printers.testPage(n); });
ipcMain.handle('pc:power:status', (e) => { fsGuard(e); return power.status(); });
ipcMain.handle('pc:power:brightness', (e, pct) => { fsGuard(e); return power.setBrightness(pct); });
ipcMain.handle('pc:power:profile', (e, name) => { fsGuard(e); return power.setProfile(name); });
ipcMain.handle('pc:power:keep-awake', (e, on) => { fsGuard(e); return power.setKeepAwake(!!on); });
ipcMain.handle('pc:power:idle', (e, seconds) => { fsGuard(e); return power.setIdleTimeout(seconds); });
/* The four that END things are separate handlers rather than one with a verb argument: a single
 * `pc:power:do(action)` is one typo away from a page asking to power off when it meant to sleep. */
ipcMain.handle('pc:power:suspend', (e) => { fsGuard(e); return power.suspend(); });
ipcMain.handle('pc:power:hibernate', (e) => { fsGuard(e); return power.hibernate(); });
ipcMain.handle('pc:power:enable-hibernate', (e) => { fsGuard(e); return power.enableHibernation(); });
ipcMain.handle('pc:power:poweroff', (e) => { fsGuard(e); return power.poweroff(); });
ipcMain.handle('pc:power:reboot', (e) => { fsGuard(e); return power.reboot(); });

/* THE LOCAL TERMINAL. The client's terminal view already speaks a resumable protocol over SSH; on
 * PosterChanOS the machine IS the node, and going out over the network to reach one's own computer
 * is absurd — worse, PosterChanOS can run with no PosterChan server at all, and then there is
 * nothing to SSH to. desktop/localterm.js gives it a PTY through `script`, needing no native
 * module in an app that ships as one AppImage. */
const localterm = require('./localterm.js');

ipcMain.handle('pc:term:start', (e, opts) => { fsGuard(e); return localterm.start(opts || {}); });
ipcMain.handle('pc:term:write', (e, id, d) => { fsGuard(e); return localterm.write(String(id), d); });
ipcMain.handle('pc:term:resize', (e, id, c, r) => { fsGuard(e); return localterm.resize(String(id), c, r); });
ipcMain.handle('pc:term:backlog', (e, id, since) => { fsGuard(e); return localterm.backlog(String(id), since); });
ipcMain.handle('pc:term:close', (e, id) => { fsGuard(e); return localterm.close(String(id)); });
ipcMain.handle('pc:term:list', (e) => { fsGuard(e); return localterm.list(); });
/* Output is PUSHED to every window rather than polled: a terminal that updates on a timer is one
 * you can watch your own keystrokes arrive late in. */
const localTermRenderers = new WeakMap(); // webContents -> Map(session id, unsubscribe)
ipcMain.handle('pc:term:attach', (e, id) => {
  fsGuard(e);
  const sid = String(id);
  const wc = e.sender;
  let links = localTermRenderers.get(wc);
  if (!links) {
    links = new Map();
    localTermRenderers.set(wc, links);
    /* A renderer owns its subscriptions. Without this cleanup, reopening the PosterChan window
     * leaves callbacks aimed at a dead page attached to every PTY it ever viewed. */
    wc.once('destroyed', () => {
      for (const off of links.values()) { try { off(); } catch (_) {} }
      links.clear();
    });
  }
  /* IDEMPOTENT PER RENDERER AND SESSION. The session strip calls attach each time a tab is chosen;
   * adding another callback on every visit duplicated every byte (including the shell's input
   * echo), and broadcasting each callback to every BrowserWindow made separate terminals collide.
   * One requesting renderer gets one stream for this PTY. */
  if (!links.has(sid)) {
    const off = localterm.subscribe(sid, (ev) => {
      if (wc.isDestroyed()) return;
      try { wc.send('pc:term:data', Object.assign({ id: sid }, ev)); } catch (_) {}
    });
    links.set(sid, off);
  }
  return true;
});
ipcMain.handle('pc:term:detach', (e, id) => {
  fsGuard(e);
  const links = localTermRenderers.get(e.sender);
  const sid = String(id);
  if (!links || !links.has(sid)) return true;
  try { links.get(sid)(); } catch (_) {}
  links.delete(sid);
  return true;
});
/* Every shell dies with the app. A session outliving the desktop is a process nobody can reach and
 * nothing will ever reap. */
app.on('will-quit', () => { try { localterm.closeAll(); } catch (_) {} });

ipcMain.handle('pc:audio:status', (e) => { fsGuard(e); return audio.status(); });
ipcMain.handle('pc:audio:volume', (e, pct, which) => { fsGuard(e); return audio.setVolume(pct, which); });
ipcMain.handle('pc:audio:mute', (e, on, which) => { fsGuard(e); return audio.setMuted(!!on, which); });
ipcMain.handle('pc:audio:default', (e, id) => { fsGuard(e); return audio.setDefault(id); });
/* THE PER-APPLICATION MIXER. Its own calls rather than a wider `pc:audio:set(id, …)`, for the same
 * reason the power actions are four verbs and not `pc:power:do(action)`: a single id-taking setter
 * makes "turn this app down" and "turn the whole machine down" one typo apart. */
ipcMain.handle('pc:audio:mixer', (e) => { fsGuard(e); return audio.mixer(); });
ipcMain.handle('pc:audio:streamvol', (e, id, pct) => { fsGuard(e); return audio.setStreamVolume(id, pct); });
ipcMain.handle('pc:audio:streammute', (e, id, on) => { fsGuard(e); return audio.setStreamMuted(id, !!on); });
ipcMain.handle('pc:bt:status', (e, scan) => { fsGuard(e); return bluetooth.status(!!scan); });
ipcMain.handle('pc:bt:power', (e, on) => { fsGuard(e); return bluetooth.power(!!on); });
ipcMain.handle('pc:bt:device', (e, address, action) => {
  fsGuard(e); return bluetooth.device(String(address||''),String(action||''));
});
const systemInfo = require('./system.js');
ipcMain.handle('pc:system:snapshot', (e, full) => { fsGuard(e); return systemInfo.snapshot(!!full); });
ipcMain.handle('pc:system:end', (e, pid) => { fsGuard(e); return systemInfo.end(Number(pid)); });
ipcMain.handle('pc:vm:list', (e) => { fsGuard(e); return vm.list(); });
ipcMain.handle('pc:vm:create', (e, opts) => { fsGuard(e); return vm.create(opts || {}); });
ipcMain.handle('pc:vm:action', (e, name, action) => { fsGuard(e); return vm.action(name, action); });
ipcMain.handle('pc:vm:remove', (e, name, disks) => { fsGuard(e); return vm.remove(name, !!disks); });
ipcMain.handle('pc:vm:view', (e, name) => { fsGuard(e); return vm.view(name); });
ipcMain.handle('pc:vm:details', (e, name) => { fsGuard(e); return vm.details(name); });
ipcMain.handle('pc:vm:update', (e, name, opts) => { fsGuard(e); return vm.update(name, opts||{}); });
ipcMain.handle('pc:vm:add-disk', (e, name, gib) => { fsGuard(e); return vm.addDisk(name, gib); });
ipcMain.handle('pc:vm:change-iso', (e, name, iso) => { fsGuard(e); return vm.changeIso(name, iso); });
ipcMain.handle('pc:vm:eject-iso', (e, name) => { fsGuard(e); return vm.ejectIso(name); });
ipcMain.handle('pc:vm:boot-disk', (e, name) => { fsGuard(e); return vm.bootDisk(name); });
ipcMain.handle('pc:vm:add-network', (e, name) => { fsGuard(e); return vm.addNetwork(name); });
ipcMain.handle('pc:vm:gaming-mouse', (e, name, on) => { fsGuard(e); return vm.gamingMouse(name, !!on); });
ipcMain.handle('pc:vm:pick-iso', async (e) => {
  fsGuard(e); const r=await dialog.showOpenDialog(win,{title:'Choose installation ISO',properties:['openFile'],
    filters:[{name:'Disc images',extensions:['iso','img']},{name:'All files',extensions:['*']}]});
  return r.canceled?'':(r.filePaths[0]||'');
});

/* SCREENSHOTS. See screenshot.js for why this is grim and not capturePage() or desktopCapturer.
 * `available` is asked separately so a tray can hide a button that could only ever fail. */
ipcMain.handle('pc:shot:available', (e) => { fsGuard(e); return require('./screenshot.js').available(); });
ipcMain.handle('pc:shot:take', async (e, opts) => {
  fsGuard(e);
  const o = opts || {};
  const r = await require('./screenshot.js').capture({ mode: String(o.mode || 'screen'),
                                                       geometry: o.geometry });
  if (!r.ok || o.copy === false) return r;
  /* THE CLIPBOARD, WITH ELECTRON'S OWN API RATHER THAN `wl-copy`.
   *
   * `wl-copy` does not exit — it forks a daemon that serves the clipboard offer until something
   * else takes the selection, and that daemon inherits this process's OPEN FILE DESCRIPTORS.
   * Measured on the test machine, one screenshot left a `wl-copy -t image/png` holding 95 of them,
   * 13 sockets, INCLUDING a listening socket of the shell. Restarting the desktop then could not
   * rebind its own port: the listener was still held by a clipboard process from a screenshot taken
   * twenty minutes earlier, and everything connecting to it queued for ever against a socket
   * nothing was accepting on. From the outside that is indistinguishable from a hung app.
   *
   * This writes the same clipboard through Chromium, in-process, with nothing to inherit.
   *
   * COPYING IS A BONUS, NEVER THE VERDICT: the picture is on disk either way, and failing the whole
   * call would throw away a screenshot that was taken perfectly. */
  let copied = false;
  try {
    const { nativeImage } = require('electron');
    const img = nativeImage.createFromPath(r.path);
    if (!img.isEmpty()) {
      clipboard.writeImage(img);
      /* CHECKED BY SOMEBODY ELSE, because the write can be accepted and do nothing.
       *
       * MEASURED on the test machine: `clipboard.writeImage()` returned without error and
       * `wl-paste --list-types` in another client answered "Nothing is copied". `readImage()` is no
       * use as a check either — Chromium hands back its own cached write, so the readback agrees
       * with itself whether or not the compositor ever gave us the selection.
       *
       * Reporting a copy that did not happen is the exact failure this screen exists to avoid:
       * somebody pastes a screenshot into a chat and posts whatever was on their clipboard before.
       * So the claim is made by a real Wayland client or not at all, and when it is not the toast
       * says "saved" and stops there — true, and still the useful half.
       *
       * On Windows and macOS there is no wl-paste and none of this applies; `writeImage` is simply
       * how the clipboard works there, so the claim is taken at face value. */
      if (process.platform === 'linux') {
        copied = await require('./screenshot.js').clipboardHasImage();
      } else {
        copied = !clipboard.readImage().isEmpty();
      }
    }
  } catch (err) { console.warn('[shot] clipboard:', (err && err.message) || err); }
  return Object.assign({}, r, { copied });
});

ipcMain.handle('pc:net:available', (e) => { fsGuard(e); return net.available(); });
ipcMain.handle('pc:net:status', (e) => { fsGuard(e); return net.status(); });
ipcMain.handle('pc:net:wifi', (e, rescan) => { fsGuard(e); return net.wifi(!!rescan); });
ipcMain.handle('pc:net:connect', (e, ssid, password) => {
  fsGuard(e); return net.connect(String(ssid || ''), password ? String(password) : '');
});
ipcMain.handle('pc:net:forget', (e, ssid) => { fsGuard(e); return net.forget(String(ssid || '')); });
ipcMain.handle('pc:net:radio', (e, on) => { fsGuard(e); return net.radio(!!on); });

/* Provisioning a Unix account for whoever signed in — the one privileged thing the shell asks for,
 * and it is a fixed command with a validated argument (the script refuses anything that is not a
 * well-formed npub, because it runs as root and its input comes from a login screen). */
ipcMain.handle('pc:os:provision', (e, npub) => {
  fsGuard(e);
  /* Installed-package diagnostics run in an isolated profile and nested compositor, but they are
   * still the real packaged binary and therefore used to retain this privileged bridge. A UI test
   * that signed in a throwaway identity consequently provisioned a real Unix account, rewrote
   * tty1's autologin and restarted getty, terminating the operator's desktop. Diagnostic mode is
   * evidence-gathering only: it must never mutate the host login/session. */
  if (diagnostic) return { ok: false, why: 'OS account changes are disabled in diagnostics' };
  const id = String(npub || '');
  if (!/^npub1[023456789acdefghjklmnpqrstuvwxyz]{58}$/.test(id)) throw new Error('not an npub');
  return new Promise((resolve) => {
    const { execFile } = require('child_process');
    execFile('sudo', ['-n', '/usr/local/bin/pc-provision-user', id], { timeout: 30000 },
      (err, stdout, stderr) => {
        if (err) return resolve({ ok: false, why: String(stderr || err.message || err).trim() });
        const out = {};
        for (const line of String(stdout).split('\n')) {
          const i = line.indexOf('=');
          if (i > 0) out[line.slice(0, i)] = line.slice(i + 1);
        }
        resolve(Object.assign({ ok: true }, out));
      });
  });
});
/* Browser storage is not the authority for whether this MACHINE was provisioned. Chromium may
 * restore the renderer before its session store, a profile may be repaired, and either used to
 * resurrect the first-boot welcome after an administrator had already been created. The root
 * helper's claim is durable OS state and is the one answer that survives every renderer restart. */
ipcMain.handle('pc:os:provisioned', (e) => {
  fsGuard(e);
  try { return fs.statSync('/var/lib/posterchanos/admin-npub').size > 0; }
  catch (_) { return false; }
});
ipcMain.handle('pc:os:identity', (e) => {
  fsGuard(e);
  try { return fs.readFileSync(path.join(app.getPath('home'), '.posterchan-npub'), 'utf8').trim(); }
  catch (_) { return ''; }
});
ipcMain.handle('pc:os:switch', (e, npub, handoff) => {
  fsGuard(e);
  if (diagnostic) return { ok: false, why: 'OS session changes are disabled in diagnostics' };
  const id = String(npub || '');
  if (!/^npub1[023456789acdefghjklmnpqrstuvwxyz]{58}$/.test(id))
    return { ok: false, why: 'not an npub' };
  let body = '';
  try { body = JSON.stringify(handoff || {}); } catch (_) { return { ok: false, why: 'bad session' }; }
  if (Buffer.byteLength(body) > 65535) return { ok: false, why: 'session is too large' };
  return new Promise((resolve) => {
    const { spawn } = require('child_process');
    const p = spawn('sudo', ['-n', '/usr/local/bin/pc-session-switch', id],
                    { stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '', err = '';
    p.stdout.on('data', b => { if (out.length < 65536) out += b; });
    p.stderr.on('data', b => { if (err.length < 65536) err += b; });
    p.on('error', x => resolve({ ok: false, why: String(x.message || x) }));
    p.on('close', code => resolve(code === 0 ? { ok: true, output: out.trim() }
                                               : { ok: false, why: err.trim() || `exit ${code}` }));
    p.stdin.end(body);
  });
});
ipcMain.handle('pc:os:logout', (e) => {
  fsGuard(e);
  if (diagnostic) return { ok: false, why: 'OS session changes are disabled in diagnostics' };
  return new Promise((resolve) => {
    const { execFile } = require('child_process');
    execFile('sudo', ['-n', '/usr/local/bin/pc-session-switch', '--greeter'], { timeout: 10000 },
      (err, stdout, stderr) => resolve(err ? { ok: false, why: String(stderr || err.message).trim() }
                                           : { ok: true, output: String(stdout).trim() }));
  });
});
/* Synchronous because store.js must import the one-shot handoff before app.js resumes a session.
 * The file is inside this Unix user's 0700 home and is removed on the first read. */
ipcMain.on('pc:os:bootstrap', (e) => {
  try {
    const file = path.join(app.getPath('home'), '.posterchan-session-bootstrap');
    const raw = fs.readFileSync(file, 'utf8');
    fs.unlinkSync(file);
    e.returnValue = raw.length <= 65536 ? JSON.parse(raw) : null;
  } catch (_) { e.returnValue = null; }
});
ipcMain.handle('pc:fs:list', (e) => { fsGuard(e); return fsbridge.list(); });
ipcMain.handle('pc:fs:pick', async (e) => {
  fsGuard(e);
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose a folder to sync',
    properties: ['openDirectory', 'createDirectory'],
  });
  if (r.canceled || !r.filePaths || !r.filePaths[0]) return null;
  const root = fsbridge.addRoot(r.filePaths[0]);
  /* SAY SO IF IT WILL NOT SURVIVE A RESTART. The root has just been written to the app's config, and
   * if that write failed the folder works for this session and is gone on the next launch — the user
   * is then asked to point at it again, with nothing anywhere explaining why. Reported as having to
   * do it over and over. The pick still succeeds: the folder DOES work now, and refusing it would
   * trade a recurring annoyance for a feature that cannot be used at all. */
  return cfgSaveFailed ? Object.assign({}, root, { persisted: false, why: cfgSaveFailed })
                       : Object.assign({}, root, { persisted: true });
});
ipcMain.handle('pc:fs:forget', (e, id) => { fsGuard(e); return fsbridge.removeRoot(String(id || '')); });
ipcMain.handle('pc:fs:scan', (e, id, opts) => { fsGuard(e); return fsbridge.scan(String(id || ''), opts || {}); });
ipcMain.handle('pc:fs:read', (e, id, rel) => { fsGuard(e); return fsbridge.read(String(id || ''), String(rel || '')); });
// Slice I/O, so a file bigger than the renderer's heap can still be synced — see fsbridge.readPart.
ipcMain.handle('pc:fs:read-part', (e, id, rel, offset, len) => {
  fsGuard(e);
  return fsbridge.readPart(String(id || ''), String(rel || ''), Number(offset) || 0, Number(len) || 0);
});
ipcMain.handle('pc:fs:write-part', (e, id, rel, offset, bytes) => {
  fsGuard(e);
  return fsbridge.writePart(String(id || ''), String(rel || ''), Number(offset) || 0,
                            bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []));
});
ipcMain.handle('pc:fs:write-commit', (e, id, rel, mtime) => {
  fsGuard(e);
  return fsbridge.writeCommit(String(id || ''), String(rel || ''), Number(mtime) || 0);
});
ipcMain.handle('pc:fs:write', (e, id, rel, bytes, mtime) => {
  fsGuard(e); return fsbridge.write(String(id || ''), String(rel || ''), bytes, mtime);
});
ipcMain.handle('pc:fs:move', (e, id, from, to) => { fsGuard(e); return fsbridge.move(String(id || ''), String(from || ''), String(to || '')); });
ipcMain.handle('pc:fs:remove', (e, id, rel) => { fsGuard(e); return fsbridge.remove(String(id || ''), String(rel || '')); });
ipcMain.handle('pc:fs:trash', (e, id, rel, when) => { fsGuard(e); return fsbridge.trash(String(id || ''), String(rel || ''), when); });
ipcMain.handle('pc:fs:empty-trash', (e, id, days) => { fsGuard(e); return fsbridge.emptyTrash(String(id || ''), days); });
ipcMain.handle('pc:fs:trash-stat', (e, id) => { fsGuard(e); return fsbridge.trashStat(String(id || '')); });
ipcMain.handle('pc:fs:hash-part', (e, id, rel) => { fsGuard(e); return fsbridge.hashPart(String(id || ''), String(rel || '')); });
ipcMain.handle('pc:fs:hash-file', (e, id, rel) => { fsGuard(e); return fsbridge.hashFile(String(id || ''), String(rel || '')); });
ipcMain.handle('pc:fs:confirm-gone', (e, id, rel) => { fsGuard(e); return fsbridge.confirmGone(String(id || ''), String(rel || '')); });
ipcMain.handle('pc:fs:list-trash', (e, id) => { fsGuard(e); return fsbridge.listTrash(String(id || '')); });
ipcMain.handle('pc:fs:purge-trash', (e, id, rels) => { fsGuard(e); return fsbridge.purgeTrash(String(id || ''), (rels || []).map(String)); });
ipcMain.handle('pc:fs:discard-part', (e, id, rel) => { fsGuard(e); return fsbridge.discardPart(String(id || ''), String(rel || '')); });
ipcMain.handle('pc:fs:part-size', (e, id, rel) => { fsGuard(e); return fsbridge.partSize(String(id || ''), String(rel || '')); });
ipcMain.handle('pc:fs:sweep-parts', (e, id, ms) => { fsGuard(e); return fsbridge.sweepParts(String(id || ''), ms); });
ipcMain.handle('pc:fs:watch', (e, id, debounceMs) => {
  fsGuard(e);
  return fsbridge.watch(String(id || ''), (which) => {
    try { if (win && !win.isDestroyed()) win.webContents.send('pc:fs:changed', which); } catch (_) {}
  }, debounceMs);
});
ipcMain.handle('pc:fs:unwatch', (e, id) => { fsGuard(e); return fsbridge.unwatch(String(id || '')); });
/* Device state for the battery policy (foldersync.js shouldSync). `charging` is what the "only sync
 * when plugged in" switch reads; powerMonitor is the only source that is right on a desktop with no
 * battery at all, where onBattery is false and the answer is "always plugged in". */
ipcMain.handle('pc:fs:power', (e) => {
  fsGuard(e);
  let onBattery = false;
  try { onBattery = !!powerMonitor && powerMonitor.isOnBatteryPower(); } catch (_) {}
  return { charging: !onBattery, metered: false, online: true };
});

ipcMain.handle('pc:clip:write', async (e, text) => {
  if (!fromOurPage(e)) { console.warn('[clip] denied'); return false; }
  const s = String(text == null ? '' : text);
  if (!s || s.length > 8192) return false;    // a stream key/url is short; refuse to be a bulk channel
  clipboard.writeText(s);
  /* Chromium's internal cache is not proof that another Wayland client can paste it.  Publish the
   * compositor selection too; the helper has bounded input/time and cannot inherit shell sockets. */
  if (require('./clipboard.js').isWayland()) {
    return require('./clipboard.js').writeWaylandText(s);
  }
  return true;
});
/* CLIPBOARD READ, and why this exists at all when write was deliberately alone.
 *
 * `pcClip.write` is exposed to any page the app loads, and its comment says the page can never READ
 * the clipboard -- which was right for a bridge handed to an arbitrary instance. This one is not
 * that: it is gated by `fromOurPage`, the same check every privileged control uses, and it exists
 * for one thing -- right-click paste in the terminal. A terminal you cannot paste into is a
 * terminal people copy commands out of a browser and retype by hand.
 *
 * Bounded, because a paste goes to a shell: 64KB is far more than any command line and far less
 * than a channel worth abusing. */
ipcMain.handle('pc:clip:read', async (e) => {
  if (!fromOurPage(e)) { console.warn('[clip] read denied'); return ''; }
  const native = await require('./clipboard.js').readWaylandText();
  if (native !== null) return native;
  try { return String(clipboard.readText() || '').slice(0, 65536); } catch (_) { return ''; }
});

// Screen picker: thumbnails as data URLs so the page stays a plain, network-free document.
ipcMain.handle('pc:screen:list', (e) => {
  fsGuard(e);
  return pendingSources.map((s) => ({
    id: s.id,
    name: s.name || 'Screen',
    screen: String(s.id).startsWith('screen:'),
    thumb: (() => { try { return s.thumbnail.toDataURL(); } catch (_) { return ''; } })(),
  }));
});

// Second launch → focus the running window instead of opening a duplicate.
if (!app.requestSingleInstanceLock()) { app.quit(); } else {
  // Config first: the switches below depend on it, and Chromium only reads them before ready.
  loadCfg();
  /* AFTER loadCfg, not at module load. This ran at import time, when `cfg` was still {} — so the
   * roots persisted from the previous session were read as an empty list and then loadCfg replaced
   * the whole cfg object underneath it. The picker worked, the sweep in the same session worked, and
   * every folder was silently forgotten on restart: "unknown sync folder" from pc:fs:scan, against a
   * folder still listed in the UI, because the renderer's own list lives in localStorage and survived
   * what the main process had dropped. */
  fsbridge.init({
    roots: Array.isArray(cfg.syncRoots) ? cfg.syncRoots : [],
    /* VERIFIED BY READING IT BACK. A root that is not on disk is a folder the user will be asked to
     * point at again on the next launch, and that question is indistinguishable from the feature
     * simply not working. */
    save: (roots) => {
      cfg.syncRoots = roots;
      saveCfg();
      if (!cfgSaveFailed) {
        try {
          const back = JSON.parse(fs.readFileSync(cfgPath(), 'utf8'));
          const kept = Array.isArray(back.syncRoots) ? back.syncRoots.length : 0;
          if (kept !== roots.length) {
            cfgSaveFailed = 'wrote ' + roots.length + ' folder(s), read back ' + kept;
            console.error('[cfg] sync roots did not persist -', cfgSaveFailed);
          }
        } catch (e) {
          cfgSaveFailed = 'could not read the config back: ' + ((e && e.message) || e);
          console.error('[cfg]', cfgSaveFailed);
        }
      }
    },
  });
  wireInsecureContent();
  wireOzonePlatform();
  wireWaylandCapture();
  wirePlainUserAgent();
  // A second launch now happens routinely — autostart puts one copy up at login and the user then
  // clicks the icon — and that copy may be HIDDEN in the tray, so this has to un-hide, not just focus.
  app.on('second-instance', () => showWindow());
  app.whenReady().then(async () => {
    wireReadyElectronModules();
    wirePowerMonitor();
    serveBundle();
    tor.setOnChange(pushTorStatus);
    // Before the window: with Tor on, applyProxy() must have run before anything can request a byte,
    // and loadApp() (called by createWindow) is what waits for the circuit.
    /* TOR IS OFF UNTIL SOMEBODY TURNS IT ON — but when they do, it exits through the US.
     *
     * Forcing every byte through Tor is not a privacy feature, it is a decision taken on somebody's
     * behalf: it is slower, a lot of sites refuse it outright, and a machine that is suddenly
     * unable to reach half the web with no explanation is a broken machine. So `enabled` is left
     * alone and the first-run wizard asks.
     *
     * The COUNTRY is a different question and it has a right answer here: an exit country of "any"
     * is what tor does with no ExitNodes line at all, so the moment the switch IS flipped — from
     * the wizard, from Settings, from the tray — the circuit is built somewhere nobody chose. This
     * pre-answers it, and is overridden the instant anybody picks a country of their own, because
     * `cfg.tor` is written by every path that touches either setting.
     *
     * It needs the geoip database the bundle already ships (tor.js note 1) or `ExitNodes {us}` is
     * decoration; `StrictNodes 1` goes with it, which tor.js already pairs. PosterChanOS only:
     * `--shell` means this process is the operating system, and pre-setting a country on somebody
     * else's Windows install is a preference they did not ask for. */
    const torDefault = SHELL_MODE ? { enabled: false, country: 'us' } : {};
    await tor.init(cfg.tor || torDefault);
    await applyProxy();
    wireDownloads();
    wirePermissions();
    buildMenu();
    // The OS shell is the desktop itself and must never honor Electron's login-item "hidden"
    // state. Electron 44 can report wasOpenedAsHidden on a recovery launch even without --hidden;
    // hiding the only desktop surface leaves a healthy Sway session as a permanent black screen.
    startHidden = !SHELL_MODE && background.launchedHidden();
    createWindow();
    /* Upgrade old saved layouts before creating the per-output shell relationship. A small gap in
     * outputs.conf is a real pointer wall; leaving it until somebody happens to open Displays keeps
     * the broken drag/gaming geometry after an otherwise successful OS update. */
    if(SHELL_MODE){
      /* Display repair is useful, not permission to abort the desktop.  A recovery/diagnostic
       * launch once missed SWAYSOCK here; this rejected out of app.whenReady before
       * reconcileShellDisplays(), leaving the primary surface on one output and a completely black
       * second output.  WM now recovers the live socket from the user's runtime directory, and this
       * final guard keeps a transient compositor failure from cancelling renderer creation. */
      try{ await displays().repairPointerGaps(); }
      catch(e){ console.warn('[shell displays] pointer-gap repair deferred:', e&&e.message||e); }
    }
    await reconcileShellDisplays();
    wireShellRecovery();
    watchInstalledBundle();
    background.init({
      show: showWindow,
      syncNow: () => { try { win && !win.isDestroyed() && win.webContents.send('pc:sync:now'); } catch (_) {} },
      isCloseToTray: closeToTray,
      // Rebuild the app menu too: the same two switches live in both places, and a checkbox that
      // disagrees with the behaviour is worse than not offering it.
      setCloseToTray: (on) => { cfg.closeToTray = !!on; saveCfg(); buildMenu(); },
      onAutostartChanged: buildMenu,
      quit: quitApp,
    });
    /* Started hidden but there is no tray to get the window back from — a desktop session with no
     * status area, or a tray that failed to create. Show it rather than run invisibly with no way in. */
    if (!background.available() && !win.isVisible()) showWindow();
    initUpdater();
    app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); else showWindow(); });
  });
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
  /* `quitting` is set HERE, not only in quitApp: the menu's role:quit, Cmd+Q and any app.quit()
   * elsewhere all reach before-quit, and none of them would otherwise set it — the close handler
   * would turn the resulting window close into a hide and the app would refuse to exit. */
  app.on('before-quit', () => { quitting = true; try { tor.stop(); } catch (_) {} });
  app.on('will-quit', () => { try { background.destroy(); } catch (_) {} });
}

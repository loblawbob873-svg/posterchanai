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
const { app, BrowserWindow, shell, session, Menu, clipboard, dialog, ipcMain, desktopCapturer,
        systemPreferences, protocol, powerMonitor } = require('electron');
const path = require('path');
const fsbridge = require('./fsbridge');
const fs = require('fs');
const tor = require('./tor');
const background = require('./background');

const DEFAULT_INSTANCE = 'https://poster.place';
const APP_ORIGIN = 'app://posterchan';                  // the bundle's own origin
const APP_URL = APP_ORIGIN + '/index.html';
const UPDATE_EVERY_MS = 6 * 60 * 60 * 1000;             // re-check every 6h for long-running windows
const WWW = path.join(__dirname, 'www');

/* Tray / background state.
 *   quitting    — a REAL quit is under way, so window.close must not be turned into a hide.
 *   startHidden — this process was started by the login item; the first window loads out of sight.
 *   closeToTray — the user preference, default ON, and only meaningful when a tray actually exists. */
let quitting = false;
let startHidden = false;
const closeToTray = () => cfg.closeToTray !== false;

let win = null;
let cfg = {};

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
  } catch (_) {}
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
const { originOf, isOurs: _isOurs } = require('./origin');
// "Ours" = the bundle, plus the instance's own pages (the client frames <instance>/admin). With no
// instance only the bundle qualifies, which is exactly right.
function isOurs(url) { return _isOurs(url, APP_ORIGIN, instance()); }

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

// Wayland has no X11-style screen grab: capture goes through the xdg-desktop-portal/PipeWire path,
// which Chromium only takes when this feature is on. Harmless no-op if the feature name ever changes.
function wireWaylandCapture() {
  if (process.platform !== 'linux') return;
  if (!process.env.WAYLAND_DISPLAY && process.env.XDG_SESSION_TYPE !== 'wayland') return;
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
try { powerMonitor.on('resume', pushWake); } catch (_) {}

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
  autoUpdater.on('update-downloaded', async (info) => {
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

// ---- window ------------------------------------------------------------------------------------
function createWindow() {
  const b = cfg.bounds || {};
  win = new BrowserWindow({
    width: b.width || 1280,
    height: b.height || 860,
    x: b.x, y: b.y,
    minWidth: 480,
    minHeight: 520,
    backgroundColor: '#0a0a10',            // matches the client's dark shell — no white flash on open
    // Menu bar stays VISIBLE: it carries "Switch instance…" and "Tor…", and behind an Alt-press nobody
    // would ever find them.
    autoHideMenuBar: false,
    icon: path.join(__dirname, 'icon.png'),
    // Started by the login item: come up HIDDEN rather than showing and then hiding, which is a
    // window flashing on screen at every boot — the thing that makes people turn autostart off.
    show: !startHidden,
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
    },
  });
  if (cfg.maximized) win.maximize();
  /* The window still exists and the renderer still runs while hidden, which is the whole mechanism:
   * folder sync is renderer code, so "running in the background" is a hidden window, not a headless
   * process. Consumed here so a LATER createWindow (macOS activate) opens normally. */
  startHidden = false;

  const remember = () => {
    if (!win || win.isDestroyed()) return;
    cfg.maximized = win.isMaximized();
    if (!cfg.maximized && !win.isMinimized()) cfg.bounds = win.getNormalBounds();
    saveCfg();
  };
  win.on('close', remember);
  /* Close means HIDE while the tray is holding the app open — otherwise closing the window ends the
   * process and, with it, the folder sync the tray exists to keep running. Guarded on the tray
   * actually being there: on a desktop with no tray this would make the app impossible to close.
   * `quitting` is the escape hatch every real quit path sets. */
  win.on('close', (e) => {
    if (quitting || !closeToTray() || !background.available()) return;
    e.preventDefault();
    if (win && !win.isDestroyed()) win.hide();
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
  const focusPage = () => { if (win && !win.isDestroyed()) win.webContents.focus(); };
  win.webContents.on('did-finish-load', focusPage);
  win.on('focus', focusPage);
  win.once('ready-to-show', focusPage);

  // Right-click menu. Electron ships NO default context menu, so `spellcheck: true` above only ever drew
  // the red underline — there was no way to act on it, and no cut/copy/paste either. Chromium hands us the
  // suggestions in params.dictionarySuggestions; replaceMisspelling() applies one. Built per-event because
  // the suggestions differ for every word.
  win.webContents.on('context-menu', (_e, params) => {
    const items = [];
    if (params.misspelledWord) {
      for (const s of params.dictionarySuggestions) {
        items.push({ label: s, click: () => win.webContents.replaceMisspelling(s) });
      }
      if (!params.dictionarySuggestions.length) items.push({ label: 'No suggestions', enabled: false });
      items.push({ type: 'separator' });
      items.push({
        label: 'Add to dictionary',
        click: () => win.webContents.session.addWordToSpellCheckerDictionary(params.misspelledWord),
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
    Menu.buildFromTemplate(items).popup({ window: win });
  });

  // Off-site links (and target=_blank to another host) belong in the user's real browser; our own pages —
  // plus blob:/data: (media the client builds locally) — open as a normal app window.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isOurs(url) || /^blob:|^data:/.test(url)) return { action: 'allow' };
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // A 302 out to the provider fires will-redirect, not will-navigate, so watch it too — but only to
  // NOTICE the trip starting.
  win.webContents.on('will-redirect', (e, url) => { isSignInNav(url); });
  win.webContents.on('will-navigate', (e, url) => {
    if (isOurs(url) || url.startsWith('file://')) { oauth = null; return; }
    if (isSignInNav(url)) return;                     // the sign-in round trip comes back to us
    e.preventDefault(); shell.openExternal(url);      // everything else belongs in the real browser
  });

  // The bundle is on disk, so "can't load the app" is no longer a network condition — it means the
  // packaged www/ is missing or unreadable, which is a broken install and nothing a retry fixes. Say
  // that rather than showing Chromium's error page.
  win.webContents.on('did-fail-load', (e, code, desc, url, isMainFrame) => {
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
  win.webContents.on('render-process-gone', (e, details) => {
    const reason = (details && details.reason) || 'unknown';
    console.warn('[renderer] gone:', reason, details && details.exitCode);
    if (reason === 'clean-exit') return;
    dialog.showErrorBox('PosterChan ran out of memory',
      reason === 'oom'
        ? 'The window was closed by the system because it ran out of memory. This usually means a '
          + 'very large file was being synced.\n\nThe app will reload. Files already synced are '
          + 'safe, and the next check resumes where it stopped.'
        : 'The window stopped unexpectedly (' + reason + ').\n\nThe app will reload.');
    try { win.webContents.reloadIgnoringCache(); }
    catch (_) { try { loadApp(); } catch (_e) {} }
  });

  loadApp();
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
let loadGen = 0;
async function loadApp() {
  if (!win || win.isDestroyed()) return;
  const gen = ++loadGen;
  const current = () => gen === loadGen && win && !win.isDestroyed();
  if (tor.status().enabled) {
    await win.loadFile(path.join(__dirname, 'boot.html'));
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
  win.loadURL(APP_URL);
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
  const ses = session.defaultSession;

  ses.setPermissionRequestHandler(async (wc, permission, cb, details) => {
    const from = (details && (details.requestingUrl || details.securityOrigin)) || (wc && wc.getURL()) || '';
    if (!ALLOW.has(permission) || !isOurs(from)) { console.warn('[perm] denied', permission, from); return cb(false); }
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
    return ALLOW.has(permission) && isOurs(from);
  });

  // Screen share. getDisplayMedia does NOT go through the handlers above: Electron rejects it outright
  // unless a display-media handler is set (a browser has a picker built in; an Electron app has to
  // supply one). On macOS 15+ the native picker takes over and this handler is never called.
  ses.setDisplayMediaRequestHandler(async (req, cb) => {
    if (!isOurs((req && req.frame && req.frame.url) || (req && req.securityOrigin) || '')) return cb({});
    let source = null;
    try { source = await pickScreenSource(); } catch (e) { console.warn('[screen]', (e && e.message) || e); }
    if (!source) return cb({});   // cancelled → the page sees a plain NotAllowedError, as in a browser
    // 'loopback' = share the system audio too, which only Windows supports. The client asks for
    // video-only today; this costs nothing and is right the day it asks for audio.
    cb(req && req.audioRequested && process.platform === 'win32'
      ? { video: source, audio: 'loopback' }
      : { video: source });
  }, { useSystemPicker: true });
}

// ---- screen-source picker (our stand-in for the browser's built-in one) -------------------------
// Modal child window listing every screen + window with a live thumbnail. Resolves to a source, or to
// null if the user cancels or closes it — never leaves getDisplayMedia hanging.
let pendingSources = [];
let pickerOpen = false;
function pickScreenSource() {
  if (pickerOpen) return Promise.resolve(null);   // one picker at a time — a second request just cancels
  pickerOpen = true;
  return desktopCapturer
    .getSources({ types: ['screen', 'window'], thumbnailSize: { width: 320, height: 200 }, fetchWindowIcons: false })
    .then((sources) => {
      if (!sources.length) { pickerOpen = false; return null; }
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
          webPreferences: { contextIsolation: true, nodeIntegration: false, preload: path.join(__dirname, 'preload.js') },
        });
        ipcMain.once('pc:screen:pick', (_e, id) => finish(id));
        pick.once('ready-to-show', () => pick.show());
        pick.on('closed', () => { ipcMain.removeAllListeners('pc:screen:pick'); finish(null); });
        pick.loadFile(path.join(__dirname, 'picker.html'));
      });
    });
}

function buildMenu() {
  const inst = instance();
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
  loadApp();
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
  if (s.enabled !== before) loadApp();
  pushTorStatus();
  return s;
}

// ---- IPC ----------------------------------------------------------------------------------------
// The bundle IS our own page, so the bridge is legitimately available to it — unlike the old shell,
// where the client was remote and a compromised instance could otherwise have repointed the app. Every
// handler still checks isOurs(), so a framed third party gets nothing.
function fromOurPage(e) {
  const from = (e && e.senderFrame && e.senderFrame.url) || (e && e.sender && e.sender.getURL()) || '';
  return from.startsWith('file://') || isOurs(from);   // file:// = boot.html / shell.html / picker.html
}

ipcMain.on('pc:instance:sync', (e) => { e.returnValue = instance(); });
ipcMain.handle('pc:instance:get', () => instance());
ipcMain.handle('pc:instance:set', (e, url) => fromOurPage(e) ? setInstance(url) : false);
ipcMain.on('pc:retry', (e) => { if (fromOurPage(e)) loadApp(); });

ipcMain.handle('pc:tor:status', () => tor.status());
ipcMain.handle('pc:tor:set', (e, opts) => fromOurPage(e) ? setTor(opts) : tor.status());
ipcMain.handle('pc:tor:new-circuit', (e) => fromOurPage(e) ? tor.newCircuit() : false);
ipcMain.handle('pc:tor:restart', async (e) => {
  if (!fromOurPage(e)) return tor.status();
  await tor.start();
  await applyProxy();
  loadApp();
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
ipcMain.handle('pc:fs:list', (e) => { fsGuard(e); return fsbridge.list(); });
ipcMain.handle('pc:fs:pick', async (e) => {
  fsGuard(e);
  const r = await dialog.showOpenDialog(win, {
    title: 'Choose a folder to sync',
    properties: ['openDirectory', 'createDirectory'],
  });
  if (r.canceled || !r.filePaths || !r.filePaths[0]) return null;
  return fsbridge.addRoot(r.filePaths[0]);
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
ipcMain.handle('pc:fs:trash', (e, id, rel, when) => { fsGuard(e); return fsbridge.trash(String(id || ''), String(rel || ''), when); });
ipcMain.handle('pc:fs:empty-trash', (e, id, days) => { fsGuard(e); return fsbridge.emptyTrash(String(id || ''), days); });
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
  try { onBattery = powerMonitor.isOnBatteryPower(); } catch (_) {}
  return { charging: !onBattery, metered: false, online: true };
});

ipcMain.handle('pc:clip:write', (e, text) => {
  if (!fromOurPage(e)) { console.warn('[clip] denied'); return false; }
  const s = String(text == null ? '' : text);
  if (!s || s.length > 8192) return false;    // a stream key/url is short; refuse to be a bulk channel
  clipboard.writeText(s);
  return true;
});
// Screen picker: thumbnails as data URLs so the page stays a plain, network-free document.
ipcMain.handle('pc:screen:list', () => pendingSources.map((s) => ({
  id: s.id,
  name: s.name || 'Screen',
  screen: String(s.id).startsWith('screen:'),
  thumb: (() => { try { return s.thumbnail.toDataURL(); } catch (_) { return ''; } })(),
})));

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
    save: (roots) => { cfg.syncRoots = roots; saveCfg(); },
  });
  wireInsecureContent();
  wireWaylandCapture();
  wirePlainUserAgent();
  // A second launch now happens routinely — autostart puts one copy up at login and the user then
  // clicks the icon — and that copy may be HIDDEN in the tray, so this has to un-hide, not just focus.
  app.on('second-instance', () => showWindow());
  app.whenReady().then(async () => {
    serveBundle();
    tor.setOnChange(pushTorStatus);
    // Before the window: with Tor on, applyProxy() must have run before anything can request a byte,
    // and loadApp() (called by createWindow) is what waits for the circuit.
    await tor.init(cfg.tor || {});
    await applyProxy();
    wireDownloads();
    wirePermissions();
    buildMenu();
    startHidden = background.launchedHidden();
    createWindow();
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

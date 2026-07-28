/* PosterChan desktop (Windows .exe / Linux AppImage) — a deliberately THIN Electron shell around the
 * SAME web client the site serves: the window just loads <instance>/client.
 *
 * Thin on purpose. Because the window is same-origin with the server, cookies, CORS, WebRTC and the
 * service worker all behave exactly as they do in a browser — none of the bundled-mode plumbing the
 * Android APK needs (mobile/build-www.sh) exists here. It also means a UI change ships with the site,
 * so users get it on the next reload and the .exe/AppImage only ever needs rebuilding when THIS shell
 * changes. The shell owns only what a browser tab can't:
 *   - window state, an instance picker (any self-hosted PosterChan, not just poster.place)
 *   - routing off-site links to the real browser
 *   - the permission grants the client needs (camera/mic for calls, notifications, screen share)
 *   - auto-update (electron-updater, generic feed at https://poster.place/desktop/)
 */
const { app, BrowserWindow, shell, session, Menu, clipboard, dialog, ipcMain, desktopCapturer, systemPreferences } = require('electron');
const path = require('path');
const fs = require('fs');

const DEFAULT_INSTANCE = 'https://poster.place';
const UPDATE_EVERY_MS = 6 * 60 * 60 * 1000;   // re-check every 6h for long-running windows

let win = null;
let cfg = {};
let insecureInstance = false;   // true when we started with the http-instance switch below applied

// ---- tiny JSON config in userData (instance + window geometry) --------------------------------
function cfgPath() { return path.join(app.getPath('userData'), 'config.json'); }
function loadCfg() { try { cfg = JSON.parse(fs.readFileSync(cfgPath(), 'utf8')) || {}; } catch (_) { cfg = {}; } }
function saveCfg() {
  try {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(cfgPath(), JSON.stringify(cfg, null, 2));
  } catch (_) {}
}
function instance() { return String(cfg.instance || DEFAULT_INSTANCE).replace(/\/+$/, ''); }
function clientUrl() { return instance() + '/client'; }
function originOf(u) { try { return new URL(u).origin; } catch (_) { return ''; } }
function isOurs(url) { const o = originOf(url); return !!o && o === originOf(instance()); }

// ---- the sign-in round trip is not an off-site link -------------------------------------------
// "Sign in with Google / a fediverse account" leaves our origin BY DESIGN and comes back carrying a
// one-time code that /client swaps for the account's key. Handing that trip to the system browser —
// which the off-site rule below otherwise does — spends the single-use code THERE: the person ends up
// signed in in Firefox while the app they clicked in stays logged out. That is what shipped.
//
// Recognised without a hardcoded provider list: an off-site URL whose `redirect_uri` points back at
// this instance IS the round trip, which is equally true of Google and of any fediverse instance
// someone types. While one is open, navigation WITHIN that provider stays in the app; it closes as
// soon as we are back on our own origin, or after OAUTH_MAX_MS, so this can never become a general
// off-site allowance.
let oauth = null;   // { origin, until } while a sign-in is in flight
const OAUTH_MAX_MS = 5 * 60 * 1000;

function comesBackToUs(url) {
  try {
    const back = new URL(url).searchParams.get('redirect_uri') || '';
    return !!back && originOf(back) === originOf(instance());
  } catch (_) { return false; }
}
function isSignInNav(url) {
  const o = originOf(url);
  if (!o) return false;
  if (o === originOf(instance())) { oauth = null; return false; }   // home again: the trip is over
  if (comesBackToUs(url)) { oauth = { origin: o, until: Date.now() + OAUTH_MAX_MS }; return true; }
  return !!(oauth && oauth.origin === o && Date.now() < oauth.until);
}

// ---- media prerequisites (must run BEFORE app ready — Chromium reads these once, at startup) ----
// A self-hosted instance reached over plain http is not a SECURE CONTEXT, and Chromium then removes
// navigator.mediaDevices entirely: mic, camera and screen share all report "not supported" even though
// the very same instance works over https. Trust the one origin the user configured — nothing else.
function wireInsecureInstance() {
  const o = originOf(instance());
  if (!/^http:\/\//i.test(o)) return;
  app.commandLine.appendSwitch('unsafely-treat-insecure-origin-as-secure', o);
  // Chromium ignores that switch unless a user-data-dir is on the command line. Passing the path
  // Electron already uses keeps the profile exactly where it was.
  app.commandLine.appendSwitch('user-data-dir', app.getPath('userData'));
  insecureInstance = true;
}

// Google refuses OAuth from a user agent it can identify as an embedded browser (disallowed_useragent),
// and Electron's default UA advertises exactly that: `posterchan/1.0.3 ... Electron/x.y.z`. Underneath it
// is plain Chromium of the stated Chrome/NNN version, so drop the two tokens and present that. Nothing
// else keys off the UA — the client picks its layout from viewport/pointer, never this string.
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

// ---- auto-update -------------------------------------------------------------------------------
// Feed is our own domain (https://poster.place/desktop/), which 302s to the GitHub release assets —
// see app/main.py. Going through the server rather than electron-updater's GitHub provider keeps the
// feed stable: the repo carries TWO rolling releases (apk-latest, desktop-latest) and the GitHub
// provider picks whichever was published last, which would break update checks after an APK build.
// Skipped in dev.
function initUpdater() {
  if (!app.isPackaged) return;
  // macOS: Squirrel.Mac refuses to swap in an app that isn't code-signed, and these builds are
  // unsigned (no Apple Developer ID). Don't even check — download the new .dmg from poster.place.
  if (process.platform === 'darwin') return;
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
    // Menu bar stays VISIBLE: "File → Switch instance…" is how a user points the app at their own
    // self-hosted PosterChan, and behind an Alt-press nobody would ever find it.
    autoHideMenuBar: false,
    icon: path.join(__dirname, 'icon.png'),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: true,
      // preload.js exposes its bridge ONLY to the bundled file:// page (offline / instance picker) —
      // the remote client gets a plain window, so a compromised instance can't repoint the app.
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  if (cfg.maximized) win.maximize();

  const remember = () => {
    if (!win || win.isDestroyed()) return;
    cfg.maximized = win.isMaximized();
    if (!cfg.maximized && !win.isMinimized()) cfg.bounds = win.getNormalBounds();
    saveCfg();
  };
  win.on('close', remember);

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
    // A link under the cursor: the client opens off-site links externally, so offer the same for copying.
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

  // Off-site links (and target=_blank to another host) belong in the user's real browser; the instance's
  // own pages — plus blob:/data: (media the client builds locally) — open as a normal app window.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (originOf(url) === originOf(instance()) || /^blob:|^data:/.test(url)) return { action: 'allow' };
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  // A 302 out to the provider fires will-redirect, not will-navigate, so watch it too — but only to
  // NOTICE the trip starting. Rerouting a redirect is deliberately not done here: redirects already
  // stayed in the window before this change and that is not what broke.
  win.webContents.on('will-redirect', (e, url) => { isSignInNav(url); });
  win.webContents.on('will-navigate', (e, url) => {
    const o = originOf(url);
    if (!o || url.startsWith('file://') || o === originOf(instance())) { if (isOurs(url)) oauth = null; return; }
    if (isSignInNav(url)) return;                     // the sign-in round trip comes back to us
    e.preventDefault(); shell.openExternal(url);      // everything else belongs in the real browser
  });

  // Can't reach the instance (offline, wrong domain, server down) → our own page, not Chromium's.
  win.webContents.on('did-fail-load', (e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;   // -3 = aborted (a normal in-app navigation)
    win.loadFile(path.join(__dirname, 'shell.html'), { query: { err: desc || String(code), url: instance() } });
  });

  win.loadURL(clientUrl());
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
// Whatever wins is then guaranteed an extension, and the filter for that extension is what makes
// Windows keep it.
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
// display-capture. Grant those to the instance origin only; deny everything else by default.
function wirePermissions() {
  const ALLOW = new Set(['media', 'notifications', 'fullscreen', 'clipboard-read',
    'clipboard-sanitized-write', 'display-capture', 'pointerLock', 'background-sync']);
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
  // supply one). That's why "share screen" failed in the app while the same client works in Chrome.
  // On macOS 15+ the native picker takes over and this handler is never called (useSystemPicker).
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
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: 'File',
      submenu: [
        { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => win && win.loadURL(clientUrl()) },
        { label: 'Switch instance…', click: () => win && win.loadFile(path.join(__dirname, 'shell.html'), { query: { pick: '1', url: instance() } }) },
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
        { label: 'Open in browser', click: () => shell.openExternal(clientUrl()) },
        { label: `Version ${app.getVersion()}`, enabled: false },
      ],
    },
  ]));
}

// ---- IPC from shell.html (offline page / instance picker) ---------------------------------------
ipcMain.handle('pc:instance:get', () => instance());
ipcMain.handle('pc:instance:set', (_e, url) => {
  const clean = String(url || '').trim().replace(/\/+$/, '');
  if (!/^https?:\/\/[^\s/]+$/i.test(clean)) return false;
  cfg.instance = clean; saveCfg();
  // The insecure-origin switch is read once at startup, so moving to (or off) an http instance only
  // takes effect after a relaunch — without it that instance would have no mic/camera/screen share.
  if (/^http:\/\//i.test(clean) !== insecureInstance) { app.relaunch(); app.exit(0); return true; }
  if (win) win.loadURL(clientUrl());
  return true;
});
ipcMain.on('pc:retry', () => { if (win) win.loadURL(clientUrl()); });
// Clipboard for the loaded instance. BOTH web paths are dead in this shell: navigator.clipboard is
// removed outright when the instance is reached over plain http (not a secure context — see the note at
// the top of this file), and execCommand('copy') is refused as well, so the Go Live stream key simply
// could not be copied. Writing text is a far narrower capability than the file:-only instance controls,
// but it is still gated on the instance origin so an embedded third party can't scribble on the
// clipboard. Write-only by design: nothing here can READ what the user has copied.
ipcMain.handle('pc:clip:write', (e, text) => {
  const from = (e && e.senderFrame && e.senderFrame.url) || (e && e.sender && e.sender.getURL()) || '';
  if (!isOurs(from)) { console.warn('[clip] denied', from); return false; }
  const s = String(text == null ? '' : text);
  if (!s || s.length > 8192) return false;    // a stream key/url is short; refuse to be a bulk channel
  clipboard.writeText(s);
  return true;
});
// Screen picker: thumbnails as data URLs so the page stays a plain, network-free file:// document.
ipcMain.handle('pc:screen:list', () => pendingSources.map((s) => ({
  id: s.id,
  name: s.name || 'Screen',
  screen: String(s.id).startsWith('screen:'),
  thumb: (() => { try { return s.thumbnail.toDataURL(); } catch (_) { return ''; } })(),
})));

// Second launch → focus the running window instead of opening a duplicate.
if (!app.requestSingleInstanceLock()) { app.quit(); } else {
  // Config first: both switches below depend on which instance we're pointed at, and Chromium only
  // reads them before ready.
  loadCfg();
  wireInsecureInstance();
  wireWaylandCapture();
  wirePlainUserAgent();
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(() => {
    wireDownloads();
    wirePermissions();
    buildMenu();
    createWindow();
    initUpdater();
    app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
  });
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
}

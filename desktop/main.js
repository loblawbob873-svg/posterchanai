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
const { app, BrowserWindow, shell, session, Menu, dialog, ipcMain } = require('electron');
const path = require('path');
const fs = require('fs');

const DEFAULT_INSTANCE = 'https://poster.place';
const UPDATE_EVERY_MS = 6 * 60 * 60 * 1000;   // re-check every 6h for long-running windows

let win = null;
let cfg = {};

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

  // Off-site links (and target=_blank to another host) belong in the user's real browser; the instance's
  // own pages — plus blob:/data: (media the client builds locally) — open as a normal app window.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (originOf(url) === originOf(instance()) || /^blob:|^data:/.test(url)) return { action: 'allow' };
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (e, url) => {
    const o = originOf(url);
    if (o && o !== originOf(instance()) && !url.startsWith('file://')) { e.preventDefault(); shell.openExternal(url); }
  });

  // Can't reach the instance (offline, wrong domain, server down) → our own page, not Chromium's.
  win.webContents.on('did-fail-load', (e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;   // -3 = aborted (a normal in-app navigation)
    win.loadFile(path.join(__dirname, 'shell.html'), { query: { err: desc || String(code), url: instance() } });
  });

  win.loadURL(clientUrl());
}

// The client is a real app: calls need camera/mic, notifications need permission, screen share needs
// display-capture. Grant those to the instance origin only; deny everything else by default.
function wirePermissions() {
  const ALLOW = new Set(['media', 'notifications', 'fullscreen', 'clipboard-read',
    'clipboard-sanitized-write', 'display-capture', 'pointerLock', 'background-sync']);
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb, details) => {
    const from = originOf((details && details.requestingUrl) || (wc && wc.getURL()) || '');
    cb(ALLOW.has(permission) && from === originOf(instance()));
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
  if (win) win.loadURL(clientUrl());
  return true;
});
ipcMain.on('pc:retry', () => { if (win) win.loadURL(clientUrl()); });

// Second launch → focus the running window instead of opening a duplicate.
if (!app.requestSingleInstanceLock()) { app.quit(); } else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(() => {
    loadCfg();
    wirePermissions();
    buildMenu();
    createWindow();
    initUpdater();
    app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
  });
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
}

/* Living in the background: a tray icon, close-to-tray, and starting at login.
 *
 * This exists because of folder sync. A sync that only runs while somebody has the app open on
 * screen is not a sync — it is a manual copy with extra steps. The three pieces here are what let
 * the app be running when the files change: start when you log in, keep running when you close the
 * window, and stay reachable from the tray while it does.
 *
 * Two things are deliberately NOT here:
 *
 *   * uploading in the background with no window. Every network step in folder sync is signed by the
 *     user's Nostr key, and with a remote signer that key is not on this machine — the renderer has
 *     to be alive to ask for a signature. So "background" means a running, hidden window, not a
 *     headless daemon.
 *   * a second copy of the app. requestSingleInstanceLock in main.js already handles a second
 *     launch; with autostart on, that second launch is a routine event (log in, then click the icon)
 *     rather than a rarity, which is why `show()` below has to handle a window that exists but is
 *     minimised, hidden, or on another workspace.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { app, Menu, Tray, nativeImage } = require('electron');

let tray = null;
let opts = {};                       // { getWin, show, syncNow, isCloseToTray, setCloseToTray, quit }

// ---- start at login ----------------------------------------------------------------------------
/* Electron's setLoginItemSettings covers macOS and Windows only. Linux has no equivalent API
 * because there is no single mechanism — what there IS, and what every desktop environment on this
 * user's machines honours, is the freedesktop autostart spec: a .desktop file in
 * ~/.config/autostart. So Linux gets a file and the other two get the API, behind one function.
 */
const LINUX_AUTOSTART_NAME = 'posterchanai.desktop';

function linuxAutostartPath() {
  const home = os.homedir();
  const base = process.env.XDG_CONFIG_HOME || path.join(home, '.config');
  return path.join(base, 'autostart', LINUX_AUTOSTART_NAME);
}

/* What to actually run at login. An AppImage is the case that breaks the obvious answer: inside one,
 * process.execPath is the UNPACKED binary in a temporary mount that will not exist next boot, so the
 * launcher has to point at the AppImage itself. Electron sets APPIMAGE for exactly this. */
function launchCommand() {
  const exe = process.env.APPIMAGE || process.execPath;
  return { exe, args: ['--hidden'] };
}

function setLinuxAutostart(enabled) {
  const file = linuxAutostartPath();
  if (!enabled) {
    try { fs.unlinkSync(file); } catch (_) {}       // already absent is the desired state, not an error
    return;
  }
  const { exe, args } = launchCommand();
  // Quote the path: an AppImage in ~/Downloads or a Program-Files-style path with spaces is normal,
  // and an unquoted Exec= silently starts nothing at all.
  const body = [
    '[Desktop Entry]',
    'Type=Application',
    'Name=PosterChan',
    'Comment=Keeps folder sync running',
    `Exec="${exe}" ${args.join(' ')}`,
    'Terminal=false',
    'X-GNOME-Autostart-enabled=true',
    '',
  ].join('\n');
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, body);
}

function setAutostart(enabled) {
  const on = !!enabled;
  try {
    if (process.platform === 'linux') { setLinuxAutostart(on); return on; }
    const { exe, args } = launchCommand();
    app.setLoginItemSettings({
      openAtLogin: on,
      // macOS honours openAsHidden; Windows does not, which is why --hidden is also passed as an
      // argument and main.js reads it. Both paths have to agree or the app starts at login with a
      // window in your face, which is the fastest way to get autostart turned back off.
      openAsHidden: on,
      path: exe,
      args,
    });
  } catch (e) {
    console.warn('[autostart]', (e && e.message) || e);
    return getAutostart();
  }
  return on;
}

function getAutostart() {
  try {
    if (process.platform === 'linux') return fs.existsSync(linuxAutostartPath());
    // The args matter: Windows keys a login item on the FULL command, so querying with the path
    // alone reports `openAtLogin: false` for an entry that is really there — the switch would read
    // off on every launch no matter how many times it was turned on.
    const { exe, args } = launchCommand();
    return !!app.getLoginItemSettings({ path: exe, args }).openAtLogin;
  } catch (_) { return false; }
}

/* Was this process started BY the login item? Both spellings, because the two paths above set it two
 * different ways, and Windows adds its own flag. */
function launchedHidden(argv) {
  const args = argv || process.argv;
  if (args.includes('--hidden')) return true;
  try { return !!app.getLoginItemSettings().wasOpenedAsHidden; } catch (_) { return false; }
}

// ---- tray --------------------------------------------------------------------------------------
function trayIcon() {
  const img = nativeImage.createFromPath(path.join(__dirname, 'icon.png'));
  if (img.isEmpty()) return img;
  // A 512px source in a 16px slot is not merely wasteful — on Windows it is drawn unscaled and you
  // get a corner of the artwork. Resize explicitly for the platforms that want a small icon.
  return process.platform === 'darwin' ? img.resize({ width: 22, height: 22 })
                                       : img.resize({ width: 16, height: 16 });
}

function menu() {
  return Menu.buildFromTemplate([
    { label: 'Open PosterChan', click: () => opts.show && opts.show() },
    { label: 'Sync folders now', click: () => opts.syncNow && opts.syncNow() },
    { type: 'separator' },
    {
      label: 'Start at login', type: 'checkbox', checked: getAutostart(),
      click: (item) => {
        const now = setAutostart(item.checked); item.checked = now;
        refresh(); if (opts.onAutostartChanged) opts.onAutostartChanged();
      },
    },
    {
      label: 'Keep running when the window is closed', type: 'checkbox',
      checked: !!(opts.isCloseToTray && opts.isCloseToTray()),
      click: (item) => { opts.setCloseToTray && opts.setCloseToTray(item.checked); refresh(); },
    },
    { type: 'separator' },
    { label: 'Quit PosterChan', click: () => opts.quit && opts.quit() },
  ]);
}

function refresh() {
  if (!tray || tray.isDestroyed()) return;
  try { tray.setContextMenu(menu()); } catch (_) {}
}

function init(o) {
  opts = o || {};
  if (tray) return tray;
  try {
    tray = new Tray(trayIcon());
    tray.setToolTip('PosterChan');
    tray.setContextMenu(menu());
    // Left-click opens the window on Windows/Linux; on macOS a left click is the menu, which is the
    // platform convention and also all a menu-bar extra can do.
    if (process.platform !== 'darwin') tray.on('click', () => opts.show && opts.show());
    tray.on('double-click', () => opts.show && opts.show());
  } catch (e) {
    // No tray on this desktop (a bare X session, some kiosk setups). Everything else must still work:
    // without a tray, close-to-tray is a way to lose the app, so main.js checks `available()`.
    console.warn('[tray] unavailable:', (e && e.message) || e);
    tray = null;
  }
  return tray;
}

function available() { return !!(tray && !tray.isDestroyed()); }

/* A one-off message from the tray. Notification is used rather than tray.displayBalloon because
 * balloons are Windows-only, and this is needed most on whichever platform the user is on. Silent
 * failure is correct: a notice nobody can show is not worth an error path. */
function notify(title, body) {
  try {
    const { Notification } = require('electron');
    if (!Notification || !Notification.isSupported()) return;
    new Notification({ title, body, icon: path.join(__dirname, 'icon.png'), silent: true }).show();
  } catch (_) {}
}

function destroy() {
  try { if (tray && !tray.isDestroyed()) tray.destroy(); } catch (_) {}
  tray = null;
}

module.exports = {
  init, refresh, available, destroy, notify,
  setAutostart, getAutostart, launchedHidden,
  _linuxAutostartPath: linuxAutostartPath, _launchCommand: launchCommand,
};

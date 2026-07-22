# PosterChan desktop (Electron)

A thin native window around the **same** web client the site serves: it loads `<instance>/client`
directly. Nothing about the client is bundled here.

That is the whole design decision, and it buys three things:

* **No plumbing.** The window is same-origin with the server, so cookies, CORS, WebRTC calls and the
  service worker behave exactly as in a browser — none of the bundled-mode shims the Android APK needs
  (`mobile/build-www.sh`) exist here.
* **Caching for free.** The client's service worker registers and caches the app shell + media in the
  app exactly as it does in the browser/PWA, with the same user-set media budget.
* **UI updates without a release.** A web-client change reaches desktop users on their next reload, so
  the installer only needs rebuilding when *this shell* changes.

The shell itself owns only what a browser tab can't: window state, the instance picker, sending
off-site links to the real browser, the permission grants the client needs (camera/mic, notifications,
screen share) and auto-update.

## Build

```
npm install
npm run build:win      # PosterChan-Setup.exe   (NSIS, Start-menu + desktop shortcut)
npm run build:linux    # PosterChan.AppImage
npm run build:mac      # PosterChan-arm64.dmg + PosterChan-x64.dmg   (needs a Mac / macOS runner)
npm start              # run from source
```

CI (`.github/workflows/desktop.yml`) builds all three on every push to `desktop/**` and publishes them
to the rolling `desktop-latest` GitHub Release. `poster.place/desktop` is the download page and the
redirect layer in front of those assets (`app/main.py`).

## Auto-update

`electron-updater` against a **generic** feed at `https://poster.place/desktop/`, which 302s to the
release assets. Not the GitHub provider: the repo carries two rolling releases (`apk-latest`,
`desktop-latest`) and that provider picks whichever was published last, which would break the check
after every APK build.

Windows and Linux update themselves (check at launch, then every 6h, prompt to restart when ready).
macOS does not — Squirrel.Mac refuses unsigned apps, and these builds have no Apple Developer ID.

## Instance

Defaults to `https://poster.place`; **File → Switch instance…** points it at any self-hosted
PosterChan, stored in `config.json` under the user data dir (same idea as the APK's `pc_instance`).
The picker also appears automatically when the instance can't be reached.

An instance served over plain **http** is not a secure context, and Chromium then deletes
`navigator.mediaDevices` outright — mic, camera and screen share all report *"not supported"*. The app
marks the configured origin (and only that one) as trusted at startup, so a LAN instance behaves like
an https one. That switch is read once, before any page loads, so switching to or from an http
instance relaunches the app.

## Camera, mic and screen share

* **Camera/mic** — granted to the instance origin only, via `setPermissionRequestHandler` *and*
  `setPermissionCheckHandler`. Both are needed: web APIs check first and only request if the check
  says no, and Electron answers those from separate handlers. On macOS the grant is also asked of the
  OS (`askForMediaAccess`), and the `NS*UsageDescription` strings live in `build.mac.extendInfo`.
* **Screen share** — `getDisplayMedia` ignores those handlers: Electron rejects it unless a
  `setDisplayMediaRequestHandler` is installed, because the source picker a browser draws for you is
  the app's job here. `picker.html` is that picker (screens + windows with thumbnails, via
  `desktopCapturer`); on macOS 15+ the native picker replaces it. On Wayland, capture goes through the
  PipeWire portal, which needs `WebRTCPipeWireCapturer` — enabled when the session is Wayland.

## Signing

Everything ships unsigned: Windows shows SmartScreen's "More info → Run anyway" on first run, and on
macOS the .dmg needs right-click → Open. Fixing that means a code-signing cert (Windows) and an Apple
Developer ID (macOS).

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

## Signing

Everything ships unsigned: Windows shows SmartScreen's "More info → Run anyway" on first run, and on
macOS the .dmg needs right-click → Open. Fixing that means a code-signing cert (Windows) and an Apple
Developer ID (macOS).

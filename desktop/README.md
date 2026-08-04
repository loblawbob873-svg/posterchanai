# PosterChan desktop (Electron)

A native window that **bundles** the web client — `desktop/build-www.sh` assembles `www/` from the same
`static/js/client` files the site serves — and loads it from disk over a privileged `app://` scheme. A
PosterChan instance is a **data endpoint only**: AI, media rendering, streams, the file manager, admin.

**It runs with no instance at all.** Relays and a key are enough, and that is the point: *File → Use
relays only*, or the button in *Settings → Profile → Instance*. In that mode the server-backed views are
hidden rather than left to fail, and you keep Social, Notifications, Messages, Bookmarks, Calls, Notes,
Passwords, Drafts, Budget, Articles, Communities, Chat, Streams and the games. The relay list comes
**pre-filled with the relays PosterChan uses**, so it works out of the box and you can replace them.

It used to be a thin shell that loaded `<instance>/client` live. That made a UI change ship without a
release, but it also meant no reachable server → no app at all.

## Why `app://` and not `file://`

A `file://` page is not a secure context, and Chromium then removes `crypto.subtle` and
`navigator.mediaDevices`. The client **signs with `crypto.subtle`** (NIP-44, the vault, every event), so
on `file://` it could not log in, let alone make a call. The scheme is registered `standard` + `secure`,
which also gives it a real tuple origin — that origin (`app://posterchan`) is on the server's CORS
allowlist in `app/main.py`, and it is what lets the service worker register.

An instance reached over plain **http** (an `.onion`, which is HTTP by design, or a LAN box) is mixed
content to that secure page, so the app sets `allow-running-insecure-content` — the same allowance the
APK makes with `usesCleartextTraffic`.

## Native Tor

The installer **ships tor** (the Tor Project's expert bundle, fetched per platform by
`.github/workflows/desktop.yml` and packed as an `extraResources` tree). `desktop/tor.js` starts it with a
generated torrc and points the whole Electron session at its SOCKS port, so relays, media, the instance
and `.onion` addresses all go through it. Turn it on in *Settings → Profile → Tor* (or *File → Turn Tor
on*) and pick an exit country.

* **The window is held on `boot.html` until the circuit is up.** The client opens relay sockets the moment
  it evaluates, so loading it first would put real traffic on the clear net during the seconds before the
  proxy took effect — the exact leak the switch exists to prevent.
* **Fail closed.** If tor dies while enabled, the proxy stays pointed at its dead SOCKS port and every
  request fails. "Recovering" by clearing the proxy would silently drop the user onto the clear net at the
  worst possible moment.
* **`socks5://`, not `socks4`/`http`** — that is what makes Chromium resolve hostnames *at* the proxy, and
  therefore what makes `.onion` work at all.
* **`GeoIPFile`/`GeoIPv6File` are load-bearing.** Without them `ExitNodes {us}` cannot be mapped to
  relays: tor starts, bootstraps, reports 100% and exits wherever it likes while the UI says otherwise.
  That is why `data/` is packaged, and why the CI step fails the build if `geoip` is missing.
* **`StrictNodes 1` only alongside `ExitNodes`.** With a country it makes the country a guarantee (tor
  refuses rather than quietly exiting elsewhere); on its own it is meaningless.
* Ports are **ephemeral**, never 9050/9051 — a Tor user already runs a system tor, and the collision looks
  like "tor exited immediately".
* A **LAN instance stops working while Tor is on**, by design: "everything through Tor" includes it.

Covered by `tests/test_desktop_tor.py`, which drives `tor.js` under node with `electron` and the tor
binary stubbed. Each assertion was verified to fail with its guard removed.

## Build

```
npm install
npm run www            # assemble www/ from ../static (also run by every build:* target)
npm start              # run from source
npm run build:win      # PosterChan-Setup.exe   (NSIS, Start-menu + desktop shortcut)
npm run build:linux    # PosterChan.AppImage
npm run build:mac      # PosterChan-arm64.dmg + PosterChan-x64.dmg   (needs a Mac / macOS runner)
```

`build-www.sh` renders `templates/client.html` **locally** rather than curl'ing it from poster.place the
way `mobile/build-www.sh` does. A build for an app whose selling point is not needing a server must not
die when production is down, nor bake production's settings into every installer. `nostr_only` is
deliberately **not** baked: one bundle serves every instance, so the client decides it at runtime from
`/client/config`, or unconditionally when there is no instance.

CI builds all three on every push touching `desktop/**` **or `static/js/client/**`** — the client ships
inside the installer now, so a web-client change needs a release to reach desktop users.

## Auto-update

`electron-updater` against a **generic** feed at `https://poster.place/desktop/`, which 302s to the
release assets. Not the GitHub provider: the repo carries two rolling releases (`apk-latest`,
`desktop-latest`) and that provider picks whichever was published last, which would break the check after
every APK build.

Windows and Linux update themselves (check at launch, then every 6h, prompt to restart when ready).
macOS does not — Squirrel.Mac refuses unsigned apps, and these builds have no Apple Developer ID. The
check is **skipped entirely while Tor is on**: electron-updater's Node HTTP stack does not use the
session proxy, so it would either leak or hang.

## Instance

Defaults to `https://poster.place` on a fresh install; `''` means "relays only" and is a **different**
value from unset — `cfg.instance == null` is the only test that keeps them apart, and conflating them
would quietly reconnect a deliberately server-less install on the next launch. Stored in `config.json`
under the user data dir, alongside window geometry and the tor choice.

Change it from *Settings → Profile → Instance* (a quick-pick, a text field, and "relays only") or
*File → Switch instance…*.

## Camera, mic and screen share

* **Camera/mic** — granted to our own origins only, via `setPermissionRequestHandler` *and*
  `setPermissionCheckHandler`. Both are needed: web APIs check first and only request if the check says
  no, and Electron answers those from separate handlers. On macOS the grant is also asked of the OS
  (`askForMediaAccess`), and the `NS*UsageDescription` strings live in `build.mac.extendInfo`.
* **Screen share** — `getDisplayMedia` ignores those handlers: Electron rejects it unless a
  `setDisplayMediaRequestHandler` is installed, because the source picker a browser draws for you is the
  app's job here. `picker.html` is that picker (screens + windows with thumbnails, via
  `desktopCapturer`); on macOS 15+ the native picker replaces it. On Wayland, capture goes through the
  PipeWire portal, which needs `WebRTCPipeWireCapturer` — enabled when the session is Wayland.

## The preload bridge

`preload.js` hands `pcShell` (instance + tor) to `app://posterchan` and to the local `file://` pages, and
to nothing else. That is a change of posture: the client used to be **remote**, so the bridge was withheld
from it — a compromised instance could otherwise have repointed the app. It now ships inside the
installer. What must still never get the bridge is anything remote the client embeds, above all the
`<iframe>` to `<instance>/admin`, which is why the test is the exact origin and every handler in
`main.js` re-checks the sender.

## Signing

Everything ships unsigned: Windows shows SmartScreen's "More info → Run anyway" on first run, and on
macOS the .dmg needs right-click → Open. Fixing that means a code-signing cert (Windows) and an Apple
Developer ID (macOS).

# Passwords

A password manager inside the Nostr client — `/client` → **Passwords** — with autofill on Android
and in Firefox, one-time codes, a generator, and import from Bitwarden.

Nobody but you can read it, including this server. That is the constraint everything below follows
from, and it is also why there is no `password` command, nothing on Telegram, and nothing the AI can
see: the operator cannot serve what the operator cannot decrypt.

## Where it lives

One **kind-30078 event per credential**, `d = pcai:pw:<id>`, tagged `l = pcai-pw` so the whole vault
is a single indexed subscription. Folders are `pcai:pwfolder:<id>`. Same shape as [Notes](NOTES.md),
for the same reasons: one document would be a read-modify-write of every password on every save (two
devices editing two logins lose one), and there is no index event anywhere, because an index is a
second source of truth that one empty read can wipe.

Public metadata is the `d` tag and the timestamps: someone watching the relay learns how many
credentials you hold and when you change them, never what or whose they are. Item ids are random,
never derived from the site.

## The vault key, and why it isn't NIP-44

Notes are NIP-44-encrypted **to your own pubkey**, which only your secret key can open. That is
right for notes and wrong here: the Firefox extension and the Android autofill service both have to
read passwords, and under that scheme each would need your nsec — your whole identity, on every
device, to fill a login form.

So items are sealed with **AES-256-GCM under a random 32-byte vault key**, and the vault key itself
is NIP-44-wrapped to your own pubkey and published as `d = pcai:pwkey`. The key that unlocks
everything is still readable only by you; but it can be handed to one device on its own, so a stolen
browser profile costs the vault and not the identity.

**The vault key is the whole vault.** Lose it and every item is ciphertext forever — there is no
recovery, by construction. Hence, in `vault.js`:

* it is cached on the device (wrapped, and unwrapped — see *Unlocking*) as well as published, so a
  device that has it never depends on a relay read;
* a wrapped key that will not unwrap **fails loudly and never mints a replacement**. Minting one
  would produce a working-looking *empty* vault whose new key overwrites the only way back. This is
  the rule `FilesIdx._ensureMK` learned the hard way;
* a key is minted **only when the relay read succeeded and found nothing** — never on a failed read,
  which at the wire level is indistinguishable from an empty vault;
* the key event is published before the first item is sealed under it.

`scripts/check_vault_mobile.py` fails if any of that regresses, and was verified to fail against a
version that minted on an unreachable relay.

## Unlocking — you are never asked for anything

Being signed into PosterChan is what opens the vault, exactly as it is for Notes. There is no vault
PIN and no second password.

The unwrapped key is cached per device (**Passwords → This device → Keep this device unlocked**, on
by default). Without that cache, opening the vault is a NIP-44 decrypt through your signer — silent
for a local key, but a prompt from Amber on **every** cold start, every time Android kills the
WebView, every time the autofill service wakes. A manager that interrupts you before it will show you
a password is one you stop using.

The honest trade, which the toggle states in those words: with it on, anything that can read this
app's storage **on this device** can read your vault. On a local-key login your nsec is already
sitting beside it; on an external-signer login this is a real, opt-out widening.

## One-time codes

RFC 6238, computed on the device. Paste either a bare base32 secret or the whole
`otpauth://totp/...?secret=…&digits=8&period=60&algorithm=SHA256` URI — both are read by
`totpConfig()`. The code ticks in place with the seconds remaining, in the app and in the extension.

`otpauth-migration://` (Google Authenticator's bulk export) is **not** handled: it is a protobuf
payload, and half-parsing it would produce silently wrong secrets.

## Import from Bitwarden

**Tools → Export vault → .json, password protection OFF**, then Passwords → Import. `.csv` works
too, including quoted commas and embedded newlines.

An **encrypted** export is refused loudly rather than imported as a wall of blank entries — the same
call `joplin.js` makes about an E2EE Joplin export, and for the same reason: silence looks like
success. Logins, secure notes, cards and identities all come across; each keeps its Bitwarden id in
`src`, so re-importing **updates in place** instead of duplicating.

**You do not have to re-import to pick up a fix.** An older build stored Bitwarden's comma-joined
URI cell as one unparseable URL — a host like `blackhillsenergy.com,https`, matching nothing, on 14
of 117 entries in a real vault including most of the banks. A parsed host containing a comma cannot
arise any other way, so the repair is unambiguous and runs by itself the next time Passwords is
opened, touching only the damaged entries and republishing each as itself.

Delete the export file afterwards. Until you do, it is a plaintext copy of every password you own.

## Sharing it with other devices

**Passwords → Pair a device** produces a code carrying the vault key. Two modes, chosen per install,
and the difference is stated on the pairing screen rather than buried here:

| | reads | saves a new login | if that device is stolen |
|---|---|---|---|
| **read-only** (default) | yes | queued until the app publishes it | your passwords |
| **full** | yes | immediately, standalone | your passwords **and** your identity |

Full mode hands over the signing key, so it is offered only when this device actually holds one — a
NIP-07/NIP-46/Amber login has nothing to give, and the radio is disabled rather than producing a
pairing that silently cannot save.

## Firefox and Chrome

`extension/` — MV3, with `browser_specific_settings` for Firefox desktop **and** `gecko_android`.

**One source tree, two bundles.** The only thing the browsers disagree about is the background entry:
Firefox MV3 takes `"background": {"scripts": [...]}` and runs them as an event page, Chrome MV3 takes
exactly one `service_worker` and REFUSES to load an extension that lists `scripts` — which is the only
reason this would not install in Chrome. Everything else was already portable: every file aliases
`const B = browser ?? chrome`, and no background script touches the DOM, `localStorage` or
`XMLHttpRequest`, none of which exist in a service worker.

So `build.sh` stages `dist/chrome/` with a **generated** manifest (service worker in, the Firefox-only
`browser_specific_settings` out) plus `background-chrome.js`, a one-line worker that `importScripts`
the same three files the Firefox manifest lists. There is deliberately no second checked-in manifest:
two manifests drift, and the one that drifts is the one nobody loads day to day.
`tests/test_extension_chrome_build.py` fails if that import list and `background.scripts` stop
agreeing, or if a background script starts using something a worker does not have.

### Getting it

> Just installing it? Both stores now carry it, so nobody has to sideload:
> **Firefox: <https://addons.mozilla.org/firefox/addon/posterchan-passwords/>**
> (or `poster.place/extension`, which redirects there) ·
> **Chrome/Edge/Brave:
> <https://chromewebstore.google.com/detail/posterchan-passwords/iigdaolbcfinlkmkhkfignoknfpmnfeg>**.
> **[extension/README.md](../extension/README.md)** is the step-by-step, including sideloading your
> own build, pairing, and turning on bookmark sync. The rest of this section is why it's built the
> way it is.

    https://poster.place/extension            the signed listing on addons.mozilla.org
    https://poster.place/extension/unpacked   the unpacked bundle (.tar.gz) — for about:debugging
    https://poster.place/extension/zip        the packed .zip — what gets submitted to AMO
    https://poster.place/extension/chrome     the Chrome/Edge/Brave bundle

The last three redirect to the rolling `extension-latest` GitHub Release, built by
`.github/workflows/extension.yml` on every change to `extension/` or to the shared core. Those builds
are **unsigned** — the same sources AMO signs, not the signed article.

**The built artifact is not in the repo, and neither is `extension/vendor/`.** Both are assembled
from files that already live here — `static/js/client/vaultcore.js` and the vendored nostr bundle —
and a committed copy is a copy that goes stale. So a fresh checkout cannot be loaded as-is: run

    extension/build.sh

first, which populates `extension/vendor/` and refreshes `extension/vaultcore.js` in place, and then
`about:debugging` → *This Firefox* → *Load Temporary Add-on* → `extension/manifest.json` works.

For Chrome (and Edge/Brave): `chrome://extensions` → **Developer mode** → **Load unpacked** →
`extension/dist/chrome`. Chrome will not install a zip directly, and unlike a Firefox temporary
add-on an unpacked extension stays loaded across restarts — no signing, no store account. The release
also ships `posterchan-passwords-chrome.zip`, which is the same folder zipped for transport.

**That is now the DEVELOPER path, not the install path.** Ordinary users get it from the Chrome Web
Store listing above, which auto-updates the same way AMO does.

### Listed on the Chrome Web Store

**<https://chromewebstore.google.com/detail/posterchan-passwords/iigdaolbcfinlkmkhkfignoknfpmnfeg>**
— item id `iigdaolbcfinlkmkhkfignoknfpmnfeg`, covering Chrome, Edge and Brave in one listing.

A store install differs from a loaded-unpacked one in a way worth knowing when a bug report arrives:
the MV3 **service worker is only started on demand** and is torn down after ~30s idle. If it fails to
boot at all — an exception anywhere in the `importScripts` chain leaves `onMessage` unregistered —
then every popup action's `sendMessage` REJECTS, and what the user sees is whatever generic sentence
that action prints. "Pairing failed" is the usual one, and it says nothing about the real fault: the
pairing code was never even parsed. `popup.js` therefore reports an unanswered message as itself,
with Chrome's own explanation, rather than collapsing it to `null`; and
`tests/test_extension_worker_boot.py` boots the worker's exact file list in a stubbed MV3 global
(no `window`, no DOM) and drives a real `pair` through it, because headless Chrome on a build box
will not load an unpacked extension and so cannot be asked directly.

### Signed and listed on AMO

**<https://addons.mozilla.org/firefox/addon/posterchan-passwords/>** — approved, so a permanent
install on **release Firefox and Firefox for Android** is one click, and it **auto-updates**.

Android is now a route real users take rather than a theoretical one, so two known behaviours there
stop being footnotes: host permissions are **not granted at install** on Firefox MV3, so the in-page
badge does nothing until they're allowed (see *Getting a code on Firefox for Android* below — the
popup works regardless, which is why it searches the whole vault), and **bookmark sync cannot work**
because Firefox for Android has no bookmarks API. Passwords, one-time codes and the NIP-07 signer all
work.

That closes the hole this section used to describe: a temporary add-on unloads on restart, release
Firefox refuses a permanent unsigned install, and Android had no sideload path at all. The CI builds
are still unsigned and still useful (running a local change, or a version that isn't through review
yet) — see [extension/README.md](../extension/README.md).

**Shipping a new version is a manual AMO upload, and it needs a version bump first.** Nothing in CI
holds Mozilla credentials, so the release flow is: bump `version` in `extension/manifest.json`, push
(CI rebuilds the rolling release), then upload `posterchan-passwords.zip` to AMO as a **new version
of the existing add-on** — never "Submit a New Add-on", the ID `passwords@poster.place` already has a
listing. **AMO refuses a version string it has already seen** (*"Version 1.1.1 already exists"*), and
it refuses it *after* the upload rather than before, so the bump is the first step and not the last.
Review is human and takes as long as it takes; the GitHub release is what users have in the meantime.

**AMO requires a data-collection declaration.** Without
`browser_specific_settings.gecko.data_collection_permissions` the submission is rejected with *"The
data_collection_permissions property is missing"*. It is declared as `authenticationInfo`: this
add-on handles credentials and syncs them — encrypted, to the relay you paired it with — and
under-declaring is what gets an add-on pulled after the fact. `optional` is empty because there is no
telemetry of any kind. `tests/test_vault_extension.py` fails if the key goes missing again.

The extension connects to your relay itself, decrypts with the paired vault key, and caches the
decrypted set so the popup opens instantly and works offline. It offers a badge in login fields, an
"is this new?" save bar on submit, one-time codes with a countdown, and the same generator as the app.

**One-time codes autofill too, including on the second step.** 2FA is almost always a separate page
with no password field on it, which is exactly where a badge keyed to password fields never
appeared. A field is treated as a code box by `autocomplete="one-time-code"` first, then the usual
words in its name/label, then the shape nothing else has (a short numeric input) — and both layouts
are filled: one box, or six single-character boxes getting a digit each rather than the whole code in
the first. The code is fetched at the moment of the click, never earlier, because a TOTP is only
valid for its window. The entry whose password was just filled is offered first on the code step,
for five minutes, which is how the right account is picked on a site where you have several.

**It ships the app's `vaultcore.js` verbatim** — copied by `build.sh`, and
`tests/test_vault_extension.py` fails if the copy drifts. That is what stops the generator, the TOTP
and the URL matcher from disagreeing between the app and the browser, which is the class of bug
nobody notices until it matters.

**It reads from every relay you use, not one.** `Relay._send` already broadcasts a publish to the
whole pool, so the vault is on each relay the app talks to — but the extension only knew about one,
which made that one a single point of failure for reading a password. The pairing code now carries
the list (up to six), the extension keeps a socket to each, and `absorb()` merges them newest-wins
per item. A relay that is down contributes nothing; the popup says "ready · 2/3 relays" rather than
claiming to be offline while working perfectly. Pairing codes from an older build still carry a
single `relay` and keep working.

What a hostile relay can do: withhold an update or replay an older event, i.e. show you a password
you have since changed. It cannot forge one — the item bodies are authenticated (GCM) and it does
not have the key. Newest-`created_at`-wins bounds it to *stale*, never *attacker-chosen*.

## Android

A native `AutofillService` (`mobile/android/.../vault/`), so logins are offered in other apps and in
any browser on the phone.

It never touches the network and holds no key of its own. The app, which has already decrypted the
vault, writes what autofill needs into a **Keystore-encrypted snapshot** (`VaultStore`), and the
service reads only that — so no NIP-44 in Java, and it works on a plane.

`setUserAuthenticationRequired(false)` is deliberate: requiring device auth on the Keystore key is
how you end up being asked for a fingerprint before the phone will even list which logins it has.
The phone's own lock screen is the boundary. Anyone past it, with the app installed, can autofill.

Turn it on in **Passwords → This device → Turn on autofill** (Android only lets a service be chosen
from its own settings screen, so that button opens it). API 26+; below that the system never binds
the service.

Matching keys — exact hosts and registrable domains — are computed by the shared `vaultcore.js` and
shipped **with** the snapshot. Java only compares strings. A fourth implementation of
`hostOf`/`baseDomain`, including the multi-label public-suffix table, would be the copy with no tests
deciding whether a password is offered on the right site.

Saving from an autofill prompt hands you back to the app: the service cannot sign vault events,
because the signing key deliberately isn't there.

## Getting a code on Firefox for Android

The in-page badge needs host permissions, and **Firefox MV3 does not grant those at install** — until
you allow them, the content script never runs and nothing appears in the page. The popup works
regardless: open it, type any part of the entry's name, and the row shows a live code with its
countdown and a button to copy it. That path needs no page access at all, which is why the popup
searches the WHOLE vault rather than only what matches the current tab.

## Matching, and what it refuses

`exact` (same host) fills. `domain` (same registrable domain, e.g. `gist.github.com` for
`github.com`) is **offered and named**, never the silent answer. Anything else is not a match:
`paypal.com.evil.com` and `paypa1.com` get nothing, and `hsbc.co.uk` does not match
`barclays.co.uk` — the multi-label suffix table is what stops every British bank collapsing into one
site. All of it is in `tests/test_vault_core.py`.

## Checks

    venv-unified/bin/python -m unittest tests.test_vault_core tests.test_vault_extension
    venv-unified/bin/python scripts/check_vault_mobile.py
    venv-unified/bin/python scripts/check_extension_popup.py
    venv-unified/bin/python scripts/check_extension_autofill.py

`test_vault_core.py` runs the shared core under node against the **RFC 6238 test vectors** (SHA1,
SHA256, SHA512, including a timestamp past 2³¹ where a 32-bit counter folds), checks the generator is
uniform over its alphabet and always includes every enabled class, and checks the lookalike-domain
cases above.

`check_extension_popup.py` covers the two ways a browser-action popup renders as a thin vertical
line, which it did once: **sized in viewport units** (a popup has no viewport to measure — Firefox
lays the document out to discover how big the popup should be, so `100vw` resolves to 0; Chrome
resolves it against the screen, so a render test cannot see this and the check reads the stylesheet
instead), and **the script throwing** before it reveals a pane, since every pane starts hidden.

`check_extension_autofill.py` drives the real content script against the app's OWN login markup —
a password input inside a `<details>`, with no `<form>` — because that is where the panel was
reported disappearing. It clicks the badge immediately after a blur, waits out every timer involved,
re-renders the field underneath it, and then fills. Verified to fail against both of the causes it
was written for.

`check_vault_mobile.py` drives the real `vault.js` at phone and desktop widths: the drawer, tap
targets, the 16px input floor, that the password field is not rendered in clear when an entry opens,
that a stored secret produces a live code, and the vault-key rule above.

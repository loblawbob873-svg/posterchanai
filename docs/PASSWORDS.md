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

## Firefox

`extension/` — MV3, with `browser_specific_settings` for Firefox desktop **and** `gecko_android`.

    extension/build.sh      → extension/dist/posterchan-passwords.zip

Load with `about:debugging` → *Load Temporary Add-on* → `manifest.json`.

The extension connects to your relay itself, decrypts with the paired vault key, and caches the
decrypted set so the popup opens instantly and works offline. It offers a badge in login fields, an
"is this new?" save bar on submit, one-time codes with a countdown, and the same generator as the app.

**It ships the app's `vaultcore.js` verbatim** — copied by `build.sh`, and
`tests/test_vault_extension.py` fails if the copy drifts. That is what stops the generator, the TOTP
and the URL matcher from disagreeing between the app and the browser, which is the class of bug
nobody notices until it matters.

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

## Matching, and what it refuses

`exact` (same host) fills. `domain` (same registrable domain, e.g. `gist.github.com` for
`github.com`) is **offered and named**, never the silent answer. Anything else is not a match:
`paypal.com.evil.com` and `paypa1.com` get nothing, and `hsbc.co.uk` does not match
`barclays.co.uk` — the multi-label suffix table is what stops every British bank collapsing into one
site. All of it is in `tests/test_vault_core.py`.

## Checks

    venv-unified/bin/python -m unittest tests.test_vault_core tests.test_vault_extension
    venv-unified/bin/python scripts/check_vault_mobile.py

`test_vault_core.py` runs the shared core under node against the **RFC 6238 test vectors** (SHA1,
SHA256, SHA512, including a timestamp past 2³¹ where a 32-bit counter folds), checks the generator is
uniform over its alphabet and always includes every enabled class, and checks the lookalike-domain
cases above.

`check_vault_mobile.py` drives the real `vault.js` at phone and desktop widths: the drawer, tap
targets, the 16px input floor, that the password field is not rendered in clear when an entry opens,
that a stored secret produces a live code, and the vault-key rule above.

# PosterChan Passwords — install without an app store

A browser add-on that turns **your own Nostr relay** into a password manager and a bookmark sync
service. Every login and every bookmark is an AES-GCM-encrypted `kind-30078` event decrypted only on
your device — the relay stores ciphertext and nothing else. It's also a **NIP-07 signer**, so you can
log into Nostr sites without pasting your `nsec` into a page.

This page is about **getting it installed from a file** — no Chrome Web Store, no addons.mozilla.org,
no store account. For how the vault itself works (the key, unlocking, Bitwarden import, matching
rules), see [docs/PASSWORDS.md](../docs/PASSWORDS.md).

**What you need first:** a PosterChan instance you're signed into. The add-on holds no account of its
own — it is paired to a vault that already exists.

---

## 1. Get the bundle

### Download a build

    https://poster.place/extension        unpacked .tar.gz  — Firefox
    https://poster.place/extension/zip    packed .zip       — Firefox (signing / AMO)

Both redirect to the rolling `extension-latest` GitHub Release, which also carries
**`posterchan-passwords-chrome.zip`** (Chrome/Edge/Brave) and **`posterchan-passwords.xpi`**
(permanent Firefox install, see below). Grab the one for your browser from that release page.

### Or build it yourself

```bash
extension/build.sh
```

Needs `python3` and `zip`; Pillow is optional (it generates the icons — without it the committed ones
are used). It writes everything into `extension/dist/`.

**A fresh checkout cannot be loaded as-is** — you have to run this first. `vaultcore.js` and
`vendor/nostr.bundle.js` are *copied from the app's own tree* at build time rather than committed,
so the add-on's password generator, TOTP and URL matching are byte-identical to the app's. A
committed copy is a copy that goes stale, and a generator that quietly drops a character class is
exactly the bug this arrangement prevents.

---

## 2. Install it

### Chrome, Edge, Brave — permanent, no store, no signing

1. Extract `posterchan-passwords-chrome.zip` (or use `extension/dist/chrome/` if you built it).
2. Go to **`chrome://extensions`**.
3. Turn on **Developer mode** (top right).
4. **Load unpacked** → pick the extracted folder.

That's the whole thing. An unpacked extension **stays installed across restarts** until you remove
it — unlike a Firefox temporary add-on. Chrome will not install a `.zip` or `.crx` directly, so the
folder is the install.

Needs **Chrome 111+**. Chrome may show a "disable developer mode extensions" bubble on startup;
dismissing it is harmless and the add-on keeps working.

### Firefox — pick your trade-off

Release Firefox refuses to permanently install an unsigned add-on, and nothing in this repo signs
anything (that needs a Mozilla account and an AMO submission). So there are two honest options:

| | How | Survives restart? | Works on |
|---|---|---|---|
| **Temporary** | `about:debugging` → *This Firefox* → **Load Temporary Add-on** → pick `manifest.json` from the extracted `-unpacked.tar.gz` | ❌ unloads every restart | any Firefox, including release |
| **Permanent, unsigned** | `about:config` → set `xpinstall.signatures.required` to **`false`**, then `about:addons` → ⚙ → **Install Add-on From File** → pick `posterchan-passwords.xpi` | ✅ | **Developer Edition / Nightly / ESR only** |

Release Firefox **ignores** that pref — it is not a setting you can talk it into. If you want a
permanent install on release Firefox, that is the one case where you need a signed build.

**Firefox for Android:** signing is not optional there either; it installs add-ons only from AMO or a
custom collection. There's no sideload path.

---

## 3. Pair it to your vault

In the app: **Passwords → Pair a device**, choose an access level, and paste the code into the
add-on's popup.

| | reads your logins | saves a new login | if the device is stolen |
|---|---|---|---|
| **read-only** (default) | yes | queued until the app publishes it | your passwords |
| **full** | yes | immediately, on its own | your passwords **and** your identity |

The code carries the key that decrypts your passwords — **don't send it over chat.** Full access
hands over a signing key, so it's only offered when the device you're pairing from actually holds
one; a NIP-07/NIP-46/Amber login has nothing to give and the option is disabled rather than creating
a pairing that silently can't save.

If sync does nothing after pairing, check **Relays** in the popup — a pairing code that carried an
unreachable relay looks exactly like a broken add-on. Setting relays there overrides the code.

---

## 4. Turn on bookmark sync

It's **off until you ask for it**, deliberately: switching it on writes into your browser's real
bookmark tree, and that shouldn't happen as a side effect of installing a password manager.

Open the popup → **Bookmarks** → tick **Sync bookmarks**.

From then on, one encrypted event per bookmark, sealed with the same vault key, kept in sync across
every browser you've paired: adds, moves and deletes propagate (and deletions *stay* deleted),
toolbar-vs-menu placement and folders are preserved, and duplicates are de-duped by URL. An idle
browser keeps syncing on an alarm, so it doesn't have to be focused.

---

## 5. Updating

Nothing auto-updates when you install from a file.

- **Chrome:** download the new bundle, replace the folder's contents, then hit **Reload** (↻) on the
  card at `chrome://extensions`.
- **Firefox, permanent:** install the newer `.xpi` over the old one.
- **Firefox, temporary:** it's gone on restart anyway — load the new one.

Your pairing and settings live in extension storage and survive an update. They don't survive a
**Remove**, which is what "Unpair" in the popup is for.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Firefox: *"this add-on could not be installed because it appears to be corrupt"* | Release Firefox rejecting an unsigned `.xpi`. Use the temporary route, or a Nightly/Dev/ESR build with the pref set |
| Chrome: *"Manifest is not valid JSON"* or the folder won't load | You picked the packed `.zip`, or a folder one level off — pick the folder that has `manifest.json` directly inside it |
| Loaded, but no autofill anywhere | Not paired yet — open the popup and paste a pairing code |
| Paired, but nothing syncs | Unreachable relay. Set one explicitly under **Relays** in the popup |
| Saving a login does nothing visible | A read-only pairing queues saves until the app publishes them. That's by design |
| Autofill doesn't offer a credential you know you have | Matching is **exact-origin**, on purpose — it will never offer a login on a sibling subdomain |
| A fresh clone won't load at all | Run `extension/build.sh` first; `vendor/` and the built bundle aren't committed |

---

## What lives where

- **On the relay:** ciphertext. One `kind-30078` event per login and per bookmark.
- **On your device:** the vault key from the pairing code, in extension storage, and decrypted
  entries in memory while the popup is open.
- **On any PosterChan server:** nothing readable. It cannot decrypt your vault, and neither can the
  AI features on it.

NIP-07 signing is asked for **per site, per event kind**, in a real browser window rather than a page
overlay — review or revoke under **Sites** in the popup.

# PosterChan Passwords — installing it

A browser add-on that turns **your own Nostr relay** into a password manager and a bookmark sync
service. Every login and every bookmark is an AES-GCM-encrypted `kind-30078` event decrypted only on
your device — the relay stores ciphertext and nothing else. It's also a **NIP-07 signer**, so you can
log into Nostr sites without pasting your `nsec` into a page.

For how the vault itself works (the key, unlocking, Bitwarden import, matching rules), see
[docs/PASSWORDS.md](../docs/PASSWORDS.md).

**What you need first:** a PosterChan instance you're signed into. The add-on holds no account of its
own — it is paired to a vault that already exists.

---

## Install it from your browser's store

**Firefox:** <https://addons.mozilla.org/firefox/addon/posterchan-passwords/> — or
`poster.place/extension`, which redirects there.

**Chrome, Edge, Brave:**
<https://chromewebstore.google.com/detail/posterchan-passwords/iigdaolbcfinlkmkhkfignoknfpmnfeg>

That's the whole thing: one click, and it **auto-updates** from then on. Everything below this
section is for running a build you made yourself; you don't need any of it to install the add-on.

Firefox for Android in particular has **no other route** — it installs add-ons only from AMO or a
custom collection, so the sideload options further down don't exist there at all.

---

## 1. Get the bundle (only if you're not using AMO)

### Download a build

    https://poster.place/extension/unpacked   unpacked .tar.gz  — Firefox, about:debugging
    https://poster.place/extension/zip        packed .zip       — what gets submitted to AMO
    https://poster.place/extension/chrome     Chrome/Edge/Brave

All three redirect to the rolling `extension-latest` GitHub Release, which also carries
**`posterchan-passwords.xpi`** (permanent Firefox install, see below). Grab the one for your browser
from that release page.

These builds are **unsigned** — they are the same sources AMO signs, not the signed article. Release
Firefox will only load one temporarily.

### Or build it yourself

```bash
extension/build.sh
```

Needs **`bash` and `python3`, and nothing else** — the archives are built with Python's own
`zipfile`/`tarfile`, so the same command works on Linux, macOS, WSL and **Git Bash on Windows**.
Pillow is optional (it generates the icons — without it the committed ones are used). It writes
everything into `extension/dist/`.

**On Windows**, run it from **Git Bash** (bundled with Git for Windows) or WSL:

```bash
cd /c/path/to/posterchanai
extension/build.sh
```

If `python3` isn't found there, the python.org installer names it `python` — either add a
`python3` alias or use the Microsoft Store build, which provides `python3`. The build used to shell
out to `zip`, which Git for Windows does not ship at all; it died at the first archive line and left
no `dist/chrome/` to load either, so that is gone.

**A fresh checkout cannot be loaded as-is** — you have to run this first. `vaultcore.js` and
`vendor/nostr.bundle.js` are *copied from the app's own tree* at build time rather than committed,
so the add-on's password generator, TOTP and URL matching are byte-identical to the app's. A
committed copy is a copy that goes stale, and a generator that quietly drops a character class is
exactly the bug this arrangement prevents.

---

## 2. Install it

### Chrome, Edge, Brave — from a build you made

> Installing normally? Use the [Web Store
> listing](https://chromewebstore.google.com/detail/posterchan-passwords/iigdaolbcfinlkmkhkfignoknfpmnfeg)
> — one click and it auto-updates. This is the developer path.

1. Extract `posterchan-passwords-chrome.zip` (or use `extension/dist/chrome/` if you built it).
2. Go to **`chrome://extensions`**.
3. Turn on **Developer mode** (top right).
4. **Load unpacked** → pick the extracted folder.

That's the whole thing. An unpacked extension **stays installed across restarts** until you remove
it — unlike a Firefox temporary add-on. Chrome will not install a `.zip` or `.crx` directly, so the
folder is the install.

Needs **Chrome 111+**. Chrome may show a "disable developer mode extensions" bubble on startup;
dismissing it is harmless and the add-on keeps working.

### Firefox — sideloading your own build

**[AMO](https://addons.mozilla.org/firefox/addon/posterchan-passwords/) is the install.** This is for
running a build you made yourself — a local change, or a version that isn't through review yet.
Release Firefox refuses to *permanently* install an unsigned add-on, so there are two options:

| | How | Survives restart? | Works on |
|---|---|---|---|
| **Temporary** | `about:debugging` → *This Firefox* → **Load Temporary Add-on** → pick `manifest.json` from the extracted `-unpacked.tar.gz` | ❌ unloads every restart | any Firefox, including release |
| **Permanent, unsigned** | `about:config` → set `xpinstall.signatures.required` to **`false`**, then `about:addons` → ⚙ → **Install Add-on From File** → pick `posterchan-passwords.xpi` | ✅ | **Developer Edition / Nightly / ESR only** |

Release Firefox **ignores** that pref — it is not a setting you can talk it into. A permanent install
on release Firefox is the signed build from AMO, and that's why it's there.

Your own build shares the add-on ID (`passwords@poster.place`) with the signed one, so installing it
**replaces** the AMO copy in that profile rather than sitting beside it. Use a separate profile if
you want to keep the signed one working while you test a build.

**Firefox for Android:** AMO only — no sideload path at all.

---

## 3. Pair it to your vault

In the app: **Passwords → Pair a device**, choose an access level, and paste the code into the
add-on's popup.

| | reads your logins | saves a new login | if the device is stolen |
|---|---|---|---|
| **read-only** (default) | yes | queued until the app publishes it | your passwords |
| **full** | yes | immediately, through your local key or PosterChan Signer | your passwords and signing access |

The code carries the key that decrypts your passwords — **don't send it over chat.** With a local
login, full access also carries the signing key. With PosterChan Signer (NIP-46), it carries a
delegated client session instead: Firefox asks Signer directly and the account `nsec` never leaves
Signer. Other external signers that cannot delegate remain read-only rather than creating
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

**Not on Firefox for Android** — it has no bookmarks API at all, so the toggle says so rather than
pretending. Passwords, one-time codes and the NIP-07 signer all work there; bookmark sync is the one
thing that can't.

---

## 5. Post the page you're on

Open the popup → **Post**. The draft is the page's title, anything you had **selected** on it, and its
address — all editable before it goes anywhere. Press **Post to Nostr** and it is published as an
ordinary note (kind 1), signed with the key this browser already holds.

It is a **public, unencrypted** note — the one thing this add-on sends that isn't ciphertext, and the
only thing it sends that you typed rather than saved. Nothing goes out until you press the button.

- **Needs a full pairing.** Read-only holds no signing key, so the pane says so instead of failing at
  the button.
- **Goes to every relay the pairing knows**, not just the one the vault syncs on — a note that reached
  one relay reached nobody but you. It reports *"posted to 2 of 3 relays"*, and if a relay refuses it
  you get the relay's own words back (`blocked: not in the web of trust`, `rate-limited`).
- `#hashtags` you type become real `t` tags, so the note turns up in hashtag feeds; the page address
  becomes an `r` tag, so clients can show a preview.
- Posting the **same text twice is refused** for a day — a note cannot be recalled. Edit it to post
  again. That guard lives in the extension's background page, not the popup, because the popup is
  destroyed on focus loss (including mid-publish), so a popup-side guard is no guard at all.
- **Your draft is not saved.** A browser-action popup is destroyed the moment it loses focus, so if
  you click into the page mid-sentence the box starts fresh. A draft store was tried and removed: it
  produced more ways to lose text than it saved (on upgrade, and on installs with no host
  permission). Write it, send it.

## Save it to Notes instead

The same screen, the other button: **Save to Notes — private**. It writes one encrypted note into
your PosterChan **Notes** library — the page title (first line of the draft) becomes the note's
title, the rest becomes the body — and it opens in **PosterChan → Notes** like any note you wrote
there.

It is the app's own format: one `kind-30078` event, `d = pcai:note:<id>`, **NIP-44 encrypted to your
own key**. Nobody else can read it — not the relay, not any PosterChan server, not the AI. That is
the opposite of the Post button beside it, which is why the two say *public* and *private* on their
faces.

Needs a full pairing, same as posting. Unlike a post, saving twice is not refused — a note is yours
to edit and delete, so a duplicate costs one delete rather than being permanent.

---

## Releasing (maintainers)

**Bump `version` in `extension/manifest.json` and push.** That is the whole release: CI builds, then
submits to **both** stores — but only when the version differs from the commit before the push.

The gate is the point. Both stores reject a version they have already seen, and both do it *after*
the upload completes, so an ungated workflow would attempt a release on every commit and fail on
every one. A bump releases to both; an ordinary commit releases to neither.

| | Secrets | Set up with |
|---|---|---|
| **Firefox (AMO)** | `AMO_JWT_ISSUER`, `AMO_JWT_SECRET` | [AMO API keys](https://addons.mozilla.org/en-US/developers/addon/api/key/) |
| **Chrome Web Store** | `CWS_CLIENT_ID`, `CWS_CLIENT_SECRET`, `CWS_REFRESH_TOKEN`, `CWS_ITEM_ID` | `python3 scripts/cws_refresh_token.py` |

A step whose secrets are absent is **skipped, not failed** — forks and PRs cannot see them.

Both stores still put a submission through **human review**; this automates the upload, which was the
manual and error-prone part. The first upload to each store had to be done by hand — AMO needs the
listing to exist, and the Web Store's **item ID** does not exist until it does — and **both are now
done**, so a version bump is the entire release on both:

| | Listing | `CWS_ITEM_ID` |
|---|---|---|
| **AMO** | <https://addons.mozilla.org/firefox/addon/posterchan-passwords/> | — |
| **Chrome Web Store** | <https://chromewebstore.google.com/detail/posterchan-passwords/iigdaolbcfinlkmkhkfignoknfpmnfeg> | `iigdaolbcfinlkmkhkfignoknfpmnfeg` |

**The one trap that bites later, not now:** while the Google OAuth consent screen is in *Testing*,
Google expires the refresh token after **7 days** — so Chrome publishing starts failing about a week
after it is set up, for a reason that is nowhere in this repo. Set the consent screen to *In
production* (no verification review is needed for a client only you use).
`tests/test_extension_store_workflows.py` runs both steps with a stubbed `curl`, so a duplicate
version, an expired token and a refused publish are all covered without touching the real APIs.

---

## 6. Updating

- **Firefox, from AMO:** nothing to do. Firefox updates it in the background; **Check for Updates**
  under ⚙ at `about:addons` forces it.
- **Chrome:** nothing auto-updates from a file — download the new bundle, replace the folder's
  contents, then hit **Reload** (↻) on the card at `chrome://extensions`.
- **Firefox, permanent sideload:** install the newer `.xpi` over the old one.
- **Firefox, temporary:** it's gone on restart anyway — load the new one.

Your pairing and settings live in extension storage and survive an update. They don't survive a
**Remove**, which is what "Unpair" in the popup is for.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Firefox: *"this add-on could not be installed because it appears to be corrupt"* | Release Firefox rejecting an unsigned `.xpi` you built. Install the signed one from [AMO](https://addons.mozilla.org/firefox/addon/posterchan-passwords/), or use the temporary route / a Nightly/Dev/ESR build with the pref set |
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

# Privacy Policy — PosterChan Passwords

**Last updated: 7 August 2026**

This policy covers the **PosterChan Passwords** browser extension for Firefox, Chrome, Edge and
Brave (add-on ID `passwords@poster.place`).

## The short version

**The developer collects nothing.** There is no account, no analytics, no telemetry, no
advertising, and no server operated by the developer anywhere in the extension's path. The
extension makes exactly one kind of network connection: a WebSocket to the Nostr relay **you**
choose. Everything it sends there is encrypted on your device first.

## What the extension stores on your device

In the browser's extension storage:

- your vault entries, held as **AES-GCM ciphertext** plus a decrypted copy in memory while the
  popup is open;
- the **vault key** you supplied when you paired the device, and — only if you chose a "full
  access" pairing — a Nostr signing key;
- the list of relays to talk to;
- a queue of entries saved while offline, until they can be published.

## What leaves your device, and where it goes

Only your vault: password entries (site, username, password, TOTP secret, notes) and, if you
switch bookmark sync on, your bookmarks. Each is **encrypted on your device before it is sent**
and travels only to the Nostr relay(s) you configured.

The relay stores ciphertext. It cannot read your entries, and neither can the developer, who
operates no server in this path — the relay is one you name. Nobody but you holds the key.

## What the extension never does

- It does **not** send your data to the developer, or to any third party.
- It makes **no** third-party requests of any kind — no analytics, no fonts, no favicon lookups,
  no crash reporting.
- It does **not** transmit the content of pages you visit. The content script reads form fields on
  your device in order to fill them, and that data stays there.
- It does **not** read your clipboard. It only writes to it, when you ask it to copy a password.
- It does **not** execute remote code. Everything it runs ships inside the package.
- It does **not** sell or transfer your data, use it for anything unrelated to being a password
  manager, or use it to assess creditworthiness or lending.

## Permissions

Each permission exists for one feature and is used for nothing else:

| Permission | Used for |
|---|---|
| `storage` | Keeping your encrypted vault, relay list and offline queue on this device |
| `activeTab` | Reading the current tab's URL, when you click the toolbar icon, to offer logins that match that site and to fill them |
| `clipboardWrite` | Copying a password when you ask (write-only) |
| `bookmarks` | Bookmark sync — **off until you turn it on** |
| `alarms` | Waking the extension on a timer so it keeps syncing while the browser is idle |
| Access to websites | Detecting login and one-time-code fields on the pages where you have logins. A saved login is matched by **exact origin**, so it is never offered on a different site or a sibling subdomain |

## Keeping your data, and deleting it

Your vault lives on the relay you chose, so you control its lifetime. To remove everything from
this device, use **Unpair** in the popup, or remove the extension — both clear its local storage,
including the keys. Removing the extension does not delete what is stored on your relay; that is
managed through the relay or through the PosterChan app you paired with.

## Children

The extension is not directed at children and collects nothing from anyone.

## Changes

Any change to this policy will be committed to this file in the public repository, so its history
is auditable.

## Contact

Questions or reports: <https://github.com/loblawbob873-svg/posterchanai/issues>

Source code for the extension is in the `extension/` directory of the same repository, and the
build is reproducible with `extension/build.sh`.

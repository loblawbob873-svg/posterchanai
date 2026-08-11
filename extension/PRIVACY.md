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
- a queue of entries saved while offline, until they can be published;
- if you use **Post**: a record of your **most recent 50 posts** — the page address, the time, and a
  fingerprint of the text — kept in plain text so the extension can refuse to publish the same note
  twice. It is a list of pages you posted about, which is browsing-history-shaped, so: it is capped
  at 50, it never leaves your device, and **Unpair** or removing the add-on deletes it along with
  everything else. Drafts are **not** stored at all.

## What leaves your device, and where it goes

Your vault: password entries (site, username, password, TOTP secret, notes) and, if you switch
bookmark sync on, your bookmarks. Each is **encrypted on your device before it is sent** and
travels only to the Nostr relay(s) you configured.

The relay stores ciphertext. It cannot read your entries, and neither can the developer, who
operates no server in this path — the relay is one you name. Nobody but you holds the key.

**Saving to Notes stays private.** The popup's **Save to Notes** button writes the same draft into
your PosterChan Notes library as an event encrypted to **your own key**, so the relay stores
ciphertext and nobody else — including the developer and any PosterChan server — can read it. It is
the private alternative to the public button beside it.

**One thing is deliberately not encrypted: a note you choose to post.** The popup's **Post** screen
publishes an ordinary public Nostr note containing exactly the text shown in the box before you press
the button — by default the current page's title, its address, and anything you had selected on it,
all editable first. It is public by design: that is what posting means. Nothing is sent until you
press **Post to Nostr**, nothing is drafted unless you open that screen, and no page you merely visit
is ever posted or recorded.

## What the extension never does

- It does **not** send your data to the developer, or to any third party.
- It makes **no** third-party requests of any kind — no analytics, no fonts, no favicon lookups,
  no crash reporting.
- It does **not** transmit the content of pages you visit. The content script reads form fields on
  your device in order to fill them, and that data stays there. The one exception is what you put in
  a note yourself and publish with the **Post** button, described above — the current page's title,
  address and your own selection, shown to you and editable before it is sent.
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
| `scripting` | **Save page as a picture** only — running the capture in the page you asked to save, to measure it, hide sticky headers so they do not repeat, and scroll it. Nothing runs in a page you did not press the button on |
| Access to websites | Detecting login and one-time-code fields on the pages where you have logins. A saved login is matched by **exact origin**, so it is never offered on a different site or a sibling subdomain |

## Saving a page as a picture

When you press **Save page as a picture**, the extension photographs the page you are looking at,
scrolling it to capture the whole thing, and saves it as a note.

The picture is **encrypted on this device** before it leaves it, with your account's own file key —
which the server holds only in a form that needs your key to open, and which the extension unwraps
locally. It is then stored with your other files, and the note that references it is encrypted to you
like every other note. Nobody operating the server can look at either.

It photographs **only the tab you pressed the button on**, and only while that tab is the one on
screen: if you switch tabs mid-capture it stops there rather than photographing what you switched to.
The page's scroll position and any headers hidden for the capture are put back afterwards, including
when the capture fails.

This is the one feature that talks to your PosterChan server over the web rather than through a
relay, because a picture is far too large to fit in a note. It talks to no other host.

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

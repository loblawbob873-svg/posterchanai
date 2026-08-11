# Email (Nostr-native mailbox)

An email client inside PosterChan. It speaks IMAP/SMTP to your real mail accounts, and mirrors the
mail into **encrypted Nostr events** on this node's relay — so the mailbox is offline-capable,
searchable without an IMAP round trip, and synced across your devices.

**Where it is.** Its own **Email** entry in the sidebar, directly under Messages; on a phone, ☰ More
→ **Email**; in desktop mode, its own window from the launcher. (It used to be the second tab of
Messages. The two share nothing but a metaphor — DMs are NIP-17 events on relays, mail is IMAP
through this node — and as a tab it was unreachable from the phone's More sheet and could not be a
desktop window of its own.)

## This is LOCAL to your node

The mailbox is **not federated**, on purpose, and this is the most important thing on the page.

`_broadcastable` in `nostr_relay/server.py` returns False for every kind-30078 carrying a `pcai:`
d-tag, so mail is never copied to the instance's ~22 public upstream relays. That is not a
precaution against a hypothetical — in June 2026 making `pcai:mail:` broadcastable pinned the outbox
queue at 500 (the public relays reject encrypted mail), pegged the CPU, and the whole feature was
removed for two months.

**If portability is ever wanted, it targets the USER's own relays over NIP-65 — never the instance's
upstream set.** Do not add `pcai:mail:` to the broadcast list or to `_BACKUP_NS`.

## How it is stored

| | |
|---|---|
| One event per message | `pcai:mail:<account>:<folder>:<uid>`, kind 30078 |
| Encryption | NIP-44 to the user's **server-held storage key** |
| Credentials | server-side (`UserSetting`, Settings → Mail) — a browser cannot open IMAP sockets |
| Attachments | AES-GCM, uploaded to **Blossom**; the message doc holds `{name,type,size,url,key,iv}` |
| Large bodies | offloaded to Blossom too, because NIP-44 has a 64 KB ceiling |

The node can read your mail. It has to — it is the thing that speaks SMTP for you. This is the same
trade the calendar makes and the opposite of Notes and Budget.

### Two things that will silently destroy mail if changed

1. **Attachment blobs are written with `keep=True`.** An attachment is the only copy of that mail's
   content; the message doc holds a reference, not the bytes. Blossom's age sweep is driven live by
   `blossom_blob_ttl_days`, so without `keep` an admin turning that setting on later retroactively
   deletes every attachment in every mailbox and leaves the docs pointing at a dead sha, with nothing
   to say it happened. `keep` only ever goes False→True (dedup means one blob can be both a
   throwaway and something irreplaceable).
2. **`_SCAN_LIMIT` is the dedup's correctness, not a performance knob.** A `d` prefix is not
   something a Nostr filter can match, so reads pull the author's kind-30078 documents and filter in
   Python — and that keyspace is shared with calendars, contacts, chats and uploads. Truncate the
   window and `have_uids` comes back short, which the sync reads as "never seen this message": it
   re-downloads and re-writes the whole mailbox. That is the write-storm, by a different road.

## Contacts in the composer

**✏️ Compose → 👤 Contacts** reads the same encrypted CardDAV addressbooks the Contacts screen and
your phone sync (see `docs/CONTACTS.md`). Only cards with an email address are offered, and picking
one appends `Name <address>` to **To**, or to **Cc** once To is filled.

Both pickers (Contacts and 🌸 Blossom) render on their **own overlay**, not through `modal()`.
`closeModal()` empties `#modal-root` and `modal()` appends to it, so a picker opened from the
composer took the half-written email with it when it closed — the address had nowhere to land.
`uiConfirm` solved this the same way, for the same reason.

## Notifications

Two different problems, two different answers:

* **The app is open** — the client syncs on login and on opening the screen, toasts what arrived and
  badges the sidebar's **Email** entry (and the ☰ More row on a phone). It also raises an OS
  notification, since an in-app toast cannot reach a window that is behind another one. Nothing to
  configure.
* **The phone is locked** — needs the background poller: Admin → Tools → **Email notifications**.
  It polls each user's IMAP INBOX and pushes new mail through the same Web Push/UnifiedPush path
  that reminders and DMs use.

The poller is **off by default**, runs in the **worker** process (an IMAP round trip per account is
exactly the long await that must not share the request loop), and enforces a floor of 2 minutes
whatever the setting says. A background poll across every account on a node is real recurring load,
and it is the shape of the thing that took this feature down once.

It announces up to 3 messages individually and collapses anything larger into a count — a mailbox
that has been offline for a week must not deliver two hundred separate buzzes. Only INBOX is
watched; nobody wants a notification because a copy of their own reply landed in Sent.

## What came back, and what did not

Restored from `f80ac3ef` (the commit before the removal), which is the **server-side** build with
its CPU fixes already in it. The **federated rewrite** — a different design where the bridge key
authored events encrypted to each user — is NOT restored and should not be: its three root causes
(federation flood, a `#p` filter that returns 0 rows, and a d-tag scheme that could not see the old
one and so re-synced everything) are documented in the git history of `c28c1204`/`df71343b`.

## Tests

```
venv-unified/bin/python scripts/check_mail_mobile.py    # layout + behaviour, phone and desktop
```

`check_client_mobile.py` never opens this screen, so that one is not optional. It lifts the shipped
`Mail` object out of `app.js` and drives it, and it is what caught the three-pane grid never
collapsing on a phone (`.mail-wrap` kept its `210px 330px 1fr` tracks, so 360px got a 210px sidebar
and a reading pane squeezed to nothing), the 14–15px fields that make iOS zoom and never zoom back,
and the modal bug above.

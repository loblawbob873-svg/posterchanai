# Contacts (bundled CardDAV)

Your addressbook, in the same server as the calendar. Your phone syncs it over **CardDAV**, and the
cards themselves are stored as **encrypted Nostr events** — replicated, portable, and unreadable to
the relay.

Nothing extra to install and nothing extra to run. CardDAV is served by the same Radicale mounted
inside this app at `/caldav`, an addressbook is just a collection whose kind is `VADDRESSBOOK`, and
it rides the **same switch as the calendar** (Admin → Tools → *Calendar server enabled*). One port,
one process, one certificate, one password.

## What "encrypted" means here — read this first

Exactly what it means for calendars, and it is worth repeating because contacts feel more private
than appointments.

A CardDAV client authenticates with a password and sends plain vCards; the server has to read them to
answer it. So an addressbook is encrypted **at rest and on the relay**, with your server-held storage
key: the relay operator sees ciphertext and no other user can read it. **This node can read your
contacts** — that is the price of your phone syncing with them. Notes and Budget make the opposite
trade (nobody but you can read them, and nothing syncs by CardDAV).

If you want an addressbook nobody but you can read, don't turn this on.

## The screen

Sidebar → **Contacts** (☰ More on a phone). An alphabetical list with photos, a search box, an A–Z
jump rail, and a card editor — plus **⋯ → Addressbooks** for creating books, import/export and the
device panel.

Search matches names, companies, emails and phone numbers, and a **punctuated number matches a stored
one**: typing `719-275-8666` finds a card holding `7192758666`, because nobody remembers which of the
two forms their phone wrote.

## Your cards are stored as your phone wrote them

This is the design, not an implementation detail.

A real addressbook carries base64 `PHOTO`s, Apple-style **grouped** properties (`item1.EMAIL`
labelled by `item1.X-ABLABEL`), a `PRODID` naming the app that wrote it, and `X-*` fields nobody else
understands. This app has form fields for about eight properties. If saving a phone number rebuilt
the card from those fields, everything else would be dropped — the contact would still look right
here and lose its photo everywhere else, with nothing to say it had happened.

So `static/js/client/vcard.js` rewrites only the properties the editor manages and **carries every
other line through untouched**, keeping each one's group prefix so the labels still point at the
right property. `tests/test_vcard.py` runs that file under node and asserts it: editing a phone
number keeps the photo, keeps `X-*` fields, keeps the other app's `PRODID`, and never changes the
UID.

## Sync to a phone

Same account, same password, same URL as the calendar — there is **one** CalDAV/CardDAV identity per
user, not two. Generate the app password in Calendar → **⋯ → Sync to a device** and use:

```
URL       https://<your-node>/caldav/<username>/
Username  your account username        (an email is accepted too, but never required)
Password  the generated app password   (shown once)
```

The username is your account's username — for a Nostr sign-in that is the `npub_…` handle, and **no
email is needed**. DAVx5 discovers both the calendars and the addressbooks from that one URL.

The password is separate from your login password on purpose: a phone stores it forever in plain
form, most accounts here signed in with a Nostr key and have no password at all, and revoking a
device must not log you out of everything else. Generating a new one immediately invalidates every
device using the old one — that is also how you revoke.

## On Android: put them in the phone's OWN Contacts app

**⋯ → Addressbooks → "Show these contacts in this phone's Contacts app"**, in the packaged Android
app only. Your address book is copied into the phone itself, so these people appear in the **dialer**,
in messaging apps, in the share sheet and anywhere else the phone offers a contact — with **no other
software installed**. CardDAV (above) already gives you this through DAVx⁵; this is the same result
without it.

**Off by default**, per device, and it asks for Android's contacts permission at the moment you turn
it on. Refusing changes nothing.

### What it is, precisely

**One way: app → phone.** Edits made in the phone's own Contacts app are not read back and are
replaced by the next push. The account type declares no edit schema, so an AOSP-derived Contacts app
shows these cards as read-only — which is what they are. **CardDAV remains the two-way path**, and is
the one to use on a desktop, on iOS, or when you want to add a contact from the phone.

It is a **reconcile**, not an append: cards are matched on their UID, updated in place (so the
favourite star, the contact id and any home-screen shortcut survive), and anything you delete here is
deleted from the phone. That last half is the one that is easy to leave out — it is the same trap
this file records against the CardDAV path below, and both are pinned by tests.

**Photos are written.** They cost more than anything else in this feature (a base64 `PHOTO` crosses
the JS→Java bridge and is the bulk of every push), so they are downscaled to 512px on the way in and
only sent for cards that actually changed — but a phone book with no faces in the dialer is visibly
the wrong product.

### Why it works the way it does

The cards are encrypted Nostr events, and the session that can read them lives in the **WebView**.
Native Java has no session and no key, and Android's own sync scheduler runs when the app is *closed*
— which is exactly when nothing on the device can read a contact. So this is **driven from
JavaScript**: `static/js/client/contacts.js` hands already-decrypted cards to a Capacitor plugin and
the plugin writes them into `ContactsContract`. It pushes when the Contacts screen loads, a few
seconds after the app starts, and when you change something; a push where nothing changed sends
nothing at all.

For the same reason there is **no `SyncAdapter`**. One would give you a "Sync now" button that can
never do anything, so the account is created with `setIsSyncable(…, 0)` — no sync toggle, no periodic
job, nothing the OS can start.

There **is** an account (`PosterChan`, account type `place.poster.app.contacts`), because a
`RawContact` must belong to one and because the account is what makes this reversible: the Contacts
app groups the cards under it, you can hide them there, and removing the account deletes every one.
The authenticator behind it is a stub — nothing here authenticates anything.

### Signing out

Signing out, switching account, or turning the switch off **removes the account and every contact it
put on the phone**. That is not tidiness: without it a handed-down phone keeps the previous user's
people in its dialer and every share sheet. There is a second guard for the app that is killed before
it can say goodbye — each push records which account it wrote for, and a push under a different one
wipes first.

### What is not covered

Nothing is written when the app is closed, and nothing can be. Groups, and any vCard property the
editor has no field for, are preserved in the *store* (see above) but are not written to the phone —
the phone gets name, phones, emails, company/title, postal address, birthday, note and photo.

| | |
|---|---|
| Plugin | `mobile/android/.../contacts/ContactSyncPlugin.java` (`begin` / `put` / `commit`) |
| Provider writer | `mobile/android/.../contacts/ContactWriter.java` |
| Account type | `mobile/android/.../contacts/PosterChanAuthenticator.java` + `AuthenticatorService.java` |
| Client | `static/js/client/contacts.js` — `pushPhonebook`, `PCContacts.syncTick/forgetDevice` |
| Wiring test | `tests/test_android_contact_sync.py` |

Android code reaches a phone only through the **CI APK build** (`.github/workflows/android.yml`) — a
`sync.sh` deploy ships the JavaScript half and nothing else.

## Import and export

**⋯ → Addressbooks → Import .vcf** takes an export from Radicale, a phone, Google Contacts, anything.
Existing UIDs are **updated rather than duplicated**, so re-importing the same file (or a newer
export of it) converges instead of doubling every contact — which matters because an import of a few
thousand cards gets interrupted.

**Export** gives back a plain `.vcf`. Unlike iCalendar there is no envelope: a `.vcf` *is* a
concatenation of cards, so wrapping it in anything would produce a file no client reads.

Limits: 20 MB and 5000 cards per import. Over either is an **error**, never a silent truncation — a
file cut mid-card imports a broken last contact.

## Under the hood

| | |
|---|---|
| Client | `static/js/client/contacts.js` + `vcard.js` (DOM-free, tested under node) |
| API | `app/routers/contacts.py` → `/api/contacts/*` |
| Store | `app/services/caldav_store.py` — one event per card, `pcai:cal:<book>:<uid>` |
| Metadata | `pcai:calmeta:<book>`, with `kind: VADDRESSBOOK` |
| CardDAV | `app/services/caldav/storage.py` — the same Radicale plugin the calendar uses |

**One event per card**, for the reason Notes gives: a per-book document is a read-modify-write of
everything on every save, and two devices editing different people lose one.

**Calendars and addressbooks share one namespace**, distinguished by the `kind` field on the metadata
document. That is what makes hydration a single relay pass rather than two, and it is also where the
two silent failures live:

1. **A collection written before addressbooks existed has no `kind`.** It must default to a calendar
   — anything else would make every existing calendar vanish from the calendar UI at once, with the
   data intact on the relay and nothing in any log.
2. **The reconcile picks the file extension and the Radicale tag from the kind.** A vCard written
   into `<uid>.ics` inside a collection announcing itself as a `VCALENDAR` gives a phone an
   addressbook with no contacts and a calendar it cannot parse. The delete half matters just as much:
   matching `.ics` unconditionally meant an addressbook's `.vcf` files were never reconciled, so a
   contact deleted in the web UI stayed on the phone and could be edited back into existence.

Both are pinned by `tests/test_contacts.py`.

## Auto-clean never touches this

Cards are kind 30078 and are written by the app, so they land with `origin = 'direct'`. Every prune
rule in the relay is an allowlist over `_PRUNABLE_KINDS`, which does not contain 30078, and 30078 is
in `_NEVER_EXPIRE_KINDS` so a stray NIP-40 `expiration` tag cannot delete a card either.

The rule worth knowing about is **pay-to-stay**: its tiered rules are the only ones in the codebase
that can delete a direct-published event. Their `kind IN (…)` qualifier is the single clause standing
between a relay with the paid tier on and somebody's entire phone book.
`tests/test_relay_prune.py::test_calendars_and_contacts_survive_every_cleaner` asserts it by name —
with the qualifier removed, **all** of a stranger's calendar and contact documents are deleted.

## Tests

```
venv-unified/bin/python -m unittest tests.test_contacts tests.test_vcard
venv-unified/bin/python -m pytest tests/test_android_contact_sync.py   # the phone-book wiring
venv-unified/bin/python scripts/check_contacts_mobile.py     # layout + behaviour, phone and desktop
```

`check_client_mobile.py` never opens this screen, so the second one is not optional. It asserts the
things a contact list breaks on specifically: a long name or the A–Z rail scrolling the page
sideways, a base64 photo failing to render, search losing the caret after one character, and — the
one the preservation design exists for — opening a contact, saving it, and checking that the vCard
the client sent still carries the photo, the unknown fields and the original UID.

## Not supported

Groups (`KIND:group` / `X-ADDRESSBOOKSERVER-MEMBER`) are stored and synced verbatim like any other
property, but the web UI has no editor for them. Contact photos can be viewed and are preserved, but
not changed from this screen.

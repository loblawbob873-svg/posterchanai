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

## Sync to another device (CardDAV)

**On the Android app, skip this section.** ⋯ → Addressbooks → *Sync to this phone's Contacts app* is
one switch and needs no URL, no password and no other software — see below. CardDAV is for your
*other* devices: a desktop, an iPhone, a second Android phone, and anything that has to keep syncing
while this app is closed. The Addressbooks panel leads with whichever of the two applies to the
device you are holding.

Same account, same password, same URL as the calendar — there is **one** CalDAV/CardDAV identity per
user, not two. **⋯ → Addressbooks → Sync to a device** on this screen shows the details, including
each address book's own collection URL, and generates the app password:

```
URL       https://<your-node>/caldav/<username>/
Username  your account username        (an email is accepted too, but never required)
Password  the generated app password   (shown once)
```

The username is your account's username — for a Nostr sign-in that is the `npub_…` handle, and **no
email is needed**. DAVx5 discovers both the calendars and the addressbooks from that one URL.

That panel is the Contacts screen's own, and it hands out **addressbook** URLs. It used to borrow the
calendar's, which is not exported from `calendar.js` — so the button fell through to its fallback and
navigated to the calendar. Reached, it would have been worse than that: a calendar URL under a
Contacts heading is accepted by every client and syncs an empty address book with no error.

The password is separate from your login password on purpose: a phone stores it forever in plain
form, most accounts here signed in with a Nostr key and have no password at all, and revoking a
device must not log you out of everything else. Generating a new one immediately invalidates every
device using the old one — that is also how you revoke.

## On Android: put them in the phone's OWN Contacts app

**⋯ → Addressbooks → "Sync to this phone's Contacts app"**, in the packaged Android app only, where
it is the first thing in that panel and the only one you need. Your address book is kept in step with the phone itself, so these people appear in the
**dialer**, in messaging apps, in the share sheet and anywhere else the phone offers a contact — with
**no other software installed** — and a contact you add or edit in the phone's own Contacts app comes
back here. CardDAV (above) already gives you this through DAVx⁵; this is the same result without it,
with one difference that matters and is stated below: it only runs while this app is open.

**Off by default**, per device, and it asks for Android's contacts permission at the moment you turn
it on. Refusing changes nothing.

An APK older than this feature says so — "update the app to turn this on" — rather than quietly
offering the CardDAV page instead. A CardDAV URL is the wrong answer to "put my contacts on this
phone": it sends somebody to install DAVx⁵ for something the app in front of them already does.

### What it is, precisely

**Both ways.** Add or edit a contact in the phone's own Contacts app and it appears here; add or edit
one here and it appears on the phone. The account declares an edit schema, so the Contacts app offers
"Edit" on these cards and lists PosterChan when you choose where to save a new one.

**It only runs while this app is open.** Not "usually", not "within an hour" — never, otherwise.
A card is an encrypted Nostr event and the key that reads it lives in this app's WebView, so nothing
else on the phone *can* sync it: not Android's sync scheduler, not a background job, not the Contacts
app itself. A sweep happens when you open the app, when you open this screen, and when you change
something. Edit a contact on the phone with PosterChan closed and it sits there, marked as changed,
until the next time you open the app — which is fine, and is not the same thing as being lost.
**If you want contacts that sync while the app is closed, use CardDAV** (above); it is also the route
for a desktop or an iPhone, and it is unaffected by any of this.

**Order: pull, then merge, then push.** Every sweep reads what the phone changed before it writes
anything, because a push *is* an overwrite of the phone's copy — done first, it would destroy an edit
made there before anything had read it, with nothing anywhere to say so.

It is a **reconcile**, not an append: cards are matched on their UID, updated in place (so the
favourite star, the contact id and any home-screen shortcut survive), and anything you delete here is
deleted from the phone — and anything you delete on the phone is deleted here. Deleting on the phone
leaves a tombstone rather than removing the row, which is what makes the deletion survive the app
being killed before it could be told.

### What wins when both sides changed

Nothing is deleted to resolve a conflict, ever.

* Only the **phone** changed since the last sync → the phone's version is stored. That is the normal
  case and it is not a conflict.
* **Both** sides changed → **last writer wins**, decided by the phone's own update time against the
  card's `REV`, and **the loser is kept as a second card** named "… (conflict copy)". Both clocks are
  approximate — `REV` is written by whichever app last touched the card, and Android's timestamp
  belongs to the merged contact rather than to our row, so it moves when a linked Google contact does
  — and that is precisely why the losing version is kept instead of trusted away. Merge the two and
  delete the copy; nothing else is needed.

### What a phone-side edit does NOT touch

The rule this file states at the top applies in this direction too, and matters more: a phone edits
about eight properties, and a card carries a photo, Apple-style grouped labels, a foreign `PRODID` and
`X-*` fields. So an edit made on the phone **rewrites only the managed properties and carries every
other line through untouched** — saving a phone number on the phone keeps the photo, keeps the
labels, keeps the UID. `tests/test_vcard.py` asserts it.

The edit schema deliberately offers **only** the fields that make the round trip: name (with prefix,
middle and suffix), phones, emails, company and title, postal addresses, birthday and note. There is
no nickname, website, IM or group field, and **no photo**: pictures are written *to* the phone and are
changed here, not there. A field the phone offers and we then discard is worse than one it never
showed — you would watch yourself type it and find it gone hours later.

**Photos are written.** They cost more than anything else in this feature (a base64 `PHOTO` crosses
the JS→Java bridge and is the bulk of every push), so they are downscaled to 512px on the way in and
only sent for cards that actually changed — but a phone book with no faces in the dialer is visibly
the wrong product.

### Why it works the way it does

The cards are encrypted Nostr events, and the session that can read them lives in the **WebView**.
Native Java has no session and no key, and Android's own sync scheduler runs when the app is *closed*
— which is exactly when nothing on the device can read a contact. So this is **driven from
JavaScript**: `static/js/client/contacts.js` hands already-decrypted cards to a Capacitor plugin and
the plugin writes them into `ContactsContract`, and reads back what the phone changed for the client
to merge. It sweeps when the Contacts screen loads, a few seconds after the app starts, and when you
change something; a sweep where nothing changed sends nothing at all.

The phone side of the sweep costs nothing either, because the provider does the bookkeeping:
`RawContacts.DIRTY` is set by the system on a *user* edit and **not** on a write carrying
`CALLER_IS_SYNCADAPTER`, which is what every write of ours carries. So "dirty" means exactly
"somebody changed this on the phone since we last wrote it", with no state of our own to keep in
step. Nothing is marked clean until the app has confirmed it stored it, and only at the row version
it was read at: edit the same contact again while a sweep is in flight and it stays dirty for the
next one, rather than being marked uploaded and lost.

The decisions — what a dirty row means, what to merge, what wins, what to keep — are in `vcard.js`,
DOM-free and tested under node, for the reason folder sync gives: everything that can get the *answer*
wrong is pure and tested, and everything that can destroy a *contact* is a thin adapter over it.

For the same reason there is **no `SyncAdapter`**. One would give you a "Sync now" button that can
never do anything, so the account is created with `setIsSyncable(…, 0)` — no sync toggle, no periodic
job, nothing the OS can start. This is also the whole content of "only while the app is open" above:
it is a property of the encryption, not an unfinished feature.

There **is** an account (`PosterChan`, account type `place.poster.app.contacts`), because a
`RawContact` must belong to one and because the account is what makes this reversible: the Contacts
app groups the cards under it, you can hide them there, and removing the account deletes every one.
The authenticator behind it is a stub — nothing here authenticates anything.

### Signing out

Signing out, switching account, or turning the switch off **removes the account and every contact it
put on the phone**, and **turns the switch itself back off**. That is not tidiness: without it a
handed-down phone keeps the previous user's people in its dialer and every share sheet — and a switch
left on is consent the *next* account inherits, so signing in would push a second person's address
book into that handset with nothing asked. The switch is therefore recorded against the pubkey that
flipped it and only counts for that account. There is a second guard for the app that is killed
before it can say goodbye — each push records which account it wrote for, and a push under a
different one wipes first.

### The reconcile refuses to empty a phone book

The reconcile is a keep-set: everything under the account that is not in it is deleted. That makes a
**short** list the most destructive thing this bridge can be handed, and every way to produce one is
silent — the app opening before wifi associates, a 5xx, one addressbook out of several whose cards
never arrived, a relay that answered a 200 with fewer contacts than you have.

This emptied a real phone book, twice, and it took four guards to bring back:

1. **A sweep needs a load that COMPLETED, and a WHOLE one.** A per-book fetch that failed used to be
   swallowed into "that book has no contacts in it" — and the flag that says a load succeeded is
   about history, so it could not see it: one *had* succeeded, earlier. A book that did not load now
   keeps its last good cards and the sweep is skipped until a whole load lands.
2. **`/api/contacts/cards` reads the relay strictly.** An unreachable relay answers `503`, never a
   `200` carrying part of your address book. This is the same rule the drive index, the folder-sync
   manifest and the uptime document each learned the hard way: `[]` must not mean both "nothing" and
   "I could not ask".
3. **The client refuses a reconcile that would delete more than it keeps**, out loud, before the
   phone is asked to do anything.
4. **And so does the plugin** — against the ROWS, not a count, because every guard on the client is
   advisory and the client is the thing that got it wrong. It answers `refused:true` with the numbers
   rather than pruning, and `force:true` is the only way past it. Nothing in the app passes `force`.

The cost is that a genuine mass delete no longer reaches the handset by itself. Turning the switch
off does — it removes the account and every row with it — and turning it back on writes what you
have now. A stale contact against an emptied address book is not a close call.

### What is not covered

Nothing is synced when the app is closed, and nothing can be. Groups, and any vCard property the
editor has no field for, are preserved in the *store* (see above) but are not written to the phone —
the phone gets name, phones, emails, company/title, postal addresses, birthday, note and photo, and
gives back everything on that list except the photo.

| | |
|---|---|
| Plugin | `mobile/android/.../contacts/ContactSyncPlugin.java` (`pull` / `taken` / `begin` / `put` / `commit`) |
| Provider writer | `mobile/android/.../contacts/ContactWriter.java` |
| Provider reader | `mobile/android/.../contacts/ContactReader.java` (dirty rows, tombstones, uid minting) |
| Edit schema | `mobile/android/app/src/main/res/xml/contacts_structure.xml` |
| Account type | `mobile/android/.../contacts/PosterChanAuthenticator.java` + `AuthenticatorService.java` |
| Merge + conflict rule | `static/js/client/vcard.js` — `toPhone`, `applyPhone`, `phonePlan` (pure, node-tested) |
| Client | `static/js/client/contacts.js` — `syncPhonebook` (pull → merge → push), `PCContacts.syncTick/forgetDevice` |
| Wiring test | `tests/test_android_contact_sync.py` (+ `tests/androidstubs/` for the javac pass) |

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
venv-unified/bin/python -m pytest tests/test_android_contact_sync.py   # the phone-book wiring + javac
venv-unified/bin/python scripts/check_contacts_mobile.py     # layout + behaviour, phone and desktop
```

`tests.test_vcard` is where the two-way sync's *decisions* are tested: a phone-side edit keeps the
photo and the unknown fields, a deletion on the phone is a deletion rather than a card to re-add, a
contact created on the phone becomes exactly one card however many times it is swept, a dirty row
that says the same thing writes nothing, and the loser of a conflict is kept. They run the shipped
`vcard.js` under node, so they test the code the phone runs.

`check_client_mobile.py` never opens this screen, so the second one is not optional. It asserts the
things a contact list breaks on specifically: a long name or the A–Z rail scrolling the page
sideways, a base64 photo failing to render, search losing the caret after one character, and — the
one the preservation design exists for — opening a contact, saving it, and checking that the vCard
the client sent still carries the photo, the unknown fields and the original UID.

## Not supported

Groups (`KIND:group` / `X-ADDRESSBOOKSERVER-MEMBER`) are stored and synced verbatim like any other
property, but the web UI has no editor for them. Contact photos can be viewed and are preserved, but
not changed from this screen.

# Calendar (bundled CalDAV)

A calendar server inside PosterChan. Your phone and desktop calendar app sync against it the way they
sync with any CalDAV server, and the calendars themselves are stored as **encrypted Nostr events** —
so they are replicated, portable, and unreadable to the relay.

Nothing to install and nothing extra to run: `radicale` is a dependency, and its WSGI application is
mounted **inside this app** at `/caldav`. One port, one process, one TLS certificate, one systemd
unit. **Off by default** (Admin → Tools → *Calendar server enabled*), because turning it on opens a
password login.

## What "encrypted" means here — read this first

It is **not** what Notes and Budget mean, and the difference is the whole design.

A CalDAV client authenticates with a password and sends plain iCalendar; the server has to read your
data to answer it. So a calendar is encrypted **at rest and on the relay**, with your server-held
storage key: the relay operator sees ciphertext, no other user can read it, and it replicates like
every other app document. **This node can read your calendar** — that is the price of your phone
being able to sync with it. Notes and Budget make the opposite trade: nobody but you can read them,
and nothing syncs by CalDAV.

If you want a calendar nobody but you can read, don't turn this on.

## The screen

Sidebar → **Calendar** (☰ More on a phone, or `y`). A month grid, a day panel under it, and an event
editor — plus **⋯ → Calendars** for creating calendars, import/export and the device panel.

Two things about it match the rest of the client. The month lives in MODULE state, not the DOM:
`#feed` is one element every view shares and app.js blanks it on entry, so leaving and coming back
returns you to the month you were on rather than to today. And **the ICS is generated in the client
and stored verbatim** — the server never rewrites an event, so what a phone syncs is exactly what
this screen wrote, and an export is what any other program reads.

That last point is why the date handling is fussy about two things:

* a **timed** event is written in **UTC** (`DTSTART:…Z`) rather than with a hand-rolled `VTIMEZONE`,
  because an absolute instant needs no timezone table and every client renders it locally;
* an **all-day** event must be `VALUE=DATE`, because it is a date and not an instant — read as UTC it
  lands on the wrong day for anyone west of London.

`scripts/check_calendar_mobile.py` drives the real screen at 360/390/1280px against a stubbed server
and asserts both: an all-day event shows as "All day", and a 14:00Z event shows in local time, on the
right day. It also checks the grid is 42 cells and Monday-first, that the month you navigated to
survives leaving the view, and that no field is under 16px (iOS zooms on focus and never comes back).

## Adding it to a device

1. Admin turns the server on (Admin → Tools).
2. In the client: **Settings → Calendar → Generate app password**. It is shown **once**.
3. On the device, add a CalDAV account:

   | | |
   |---|---|
   | Server | `https://<your-node>/caldav/<username>/` |
   | Username | your PosterChan username |
   | Password | the app password from step 2 |

The password is **CalDAV-only** and separate from your login, deliberately: a phone keeps it forever
in plain form and syncs it to a vendor cloud, most accounts here have no login password at all (they
sign in with a Nostr key), and revoking a device must not log you out of everything else. Generating
a new one immediately invalidates every device using the old one — that is also how you revoke.
It is stored as a PBKDF2 hash, so there is no "show it to me again".

## How it is put together

```
phone ──CalDAV──▶ /caldav (Radicale, mounted in the app)
                      │  auth  plugin → this node's accounts + app password
                      │  storage plugin
                      ├─▶ working directory (a cache)
                      └─▶ encrypted Nostr events  ← the record
web UI ──/api/calendar/*──────────────────────────┘  (same events, one calendar)
```

* **One event per item**, `pcai:cal:<calendar>:<uid>`, NIP-44-encrypted to your storage key; calendar
  properties live in `pcai:calmeta:<calendar>`. Per item and not one document per calendar, for the
  reason Notes gives: a document is a read-modify-write of everything on every save, so two devices
  editing different events lose one — and phones sync constantly. It also makes one appointment
  individually deletable instead of rewriting the calendar.
* **The storage plugin subclasses Radicale's `multifilesystem`** rather than reimplementing
  `BaseStorage`. CalDAV's hard parts are sync tokens, the history a `sync-collection` REPORT walks,
  collection locking and etag semantics; getting those subtly wrong means a phone that silently
  stops syncing or duplicates every event. Upstream already has them right. The directory is a
  **cache**: delete it and the calendars come back from the relay on the next request (verified —
  `rm -rf caldav-data/collection-root`, restart, the event is still there).
* **A write in the web UI is visible to a phone immediately.** Hydration is once per process, so an
  app-side write calls `storage.forget_user()` to force a re-read; without it a calendar created in
  the client would not exist for CalDAV until the app restarted.

## Import and export

* **Import** — `POST /api/calendar/import?cal=<name>` with an `.ics` file. Works with a Radicale
  export, Google, Thunderbird, anything. Existing UIDs are **updated, not duplicated**, so
  re-importing the same file (or a newer export of it) converges instead of doubling every
  appointment — the same rule the Joplin import follows, and for the same reason: big imports get
  interrupted and re-run.
* **Export** — `GET /api/calendar/export?cal=<name>` returns one standard `.ics`. Moving off this
  node is a file copy, not a migration.

Items are stored exactly as the client PUT them, which is a whole `VCALENDAR` each, so the export
**unwraps** them — otherwise the file has a calendar inside a calendar, which some programs import as
one broken entry and others refuse outright.

### What an import stores, and why it is a RESOURCE and not a component

An `.ics` file is a flat list of components; CalDAV addresses **resources**. The two differ in ways
that were measured on a real 707-event Radicale export, where getting them wrong was silent:

* **A `VTIMEZONE` has no UID.** Keyed on UID alone, every one of them was dropped: 577 of those 707
  events referenced a `TZID` and **not one** definition survived the import.
  `DTSTART;TZID=America/Denver:20220109T100000` with no matching `VTIMEZONE` is an invalid resource —
  a strict client refuses it, and a lenient one reads the time as floating and shifts the appointment
  by the UTC offset. `wrap_ics(..., timezones=…)` now attaches the definitions a resource actually
  uses, hoisted to the front and deduped, so an export carries each one once rather than once per
  event.
* **Components sharing a UID are ONE resource.** A recurring event with an edited occurrence is a
  master `VEVENT` plus a `VEVENT` carrying `RECURRENCE-ID`, both under the same UID. Stored as
  separate documents the second write silently overwrote the first: the master disappeared and the
  calendar showed a lone stray occurrence.
* **`VTODO`s are resources too.** 10 of those 707 items were todos; keying on `VEVENT` drops them.

`group_resources()` and `timezones_of()` in `caldav_store.py` do this, and `tests/test_calendar.py`
pins each case — including that a `DESCRIPTION` containing the literal text `TZID=` is prose and not
a reference, and that a folded `DTSTART;TZ\r\n ID=…` still is one.

Three of that calendar's events reference `TZID:GMT-0600`, which their source file never defines and
which is not an IANA zone. Nothing can be recovered there, and nothing is invented: they are stored
the way Radicale stores them, and the client falls back to floating local time rather than throwing.

### Recurring events

`static/js/client/ical.js` expands `RRULE` for the month being drawn — `FREQ` daily/weekly/monthly/
yearly with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (including ordinals like `-1FR`), `BYMONTHDAY` and
`BYMONTH`, plus `EXDATE` exclusions and `RECURRENCE-ID` overrides.

It is a **separate, DOM-free file** so `tests/test_ical_recurrence.py` can run the shipped parser
under node against real rules. That matters because the grid previously placed only `DTSTART`: a
weekly delivery running since 2025 drew on one Friday in 2025 and never again, and an imported
calendar of 707 events looked almost empty with nothing in any log.

Expansion **jumps to the period containing the window** instead of stepping from `DTSTART` — a daily
series from 2011 is ~5500 iterations away from today, done for every rule on every repaint. `COUNT`
is the exception, since it can only be known by counting from the beginning; that is bounded by the
count itself.

Editing a recurring event from the web UI keeps the series: the repeat rule, its exceptions and any
individually-edited occurrences are re-emitted **verbatim**, because rebuilding an event from the
form alone would flatten a weekly series into one appointment the first time somebody fixed a typo in
its title.

### An imported calendar is usually a history

Of that real 707-event export, exactly **one** event fell in the month it was imported in. Landing on
an empty grid after a successful import is indistinguishable from the import having failed, so after
an import the view moves to a month that has something in it, and an otherwise-empty month says how
many items the calendar holds and offers the jump. The import itself shows progress — a few hundred
signed, encrypted writes take tens of seconds, and a modal that just sits there reads as "nothing
happened".

## Gotchas, each of which cost a debugging session

1. **The auth plugin implements `_login`, not `login`.** Radicale marks `login()` `@final` (it owns
   rate limiting, the login cache and username normalisation) and dispatches to `_login`. Overriding
   the public one imports cleanly and then raises *"takes 3 positional arguments but 4 were given"*
   on the first request — every CalDAV call 500s while the server looks perfectly healthy.
2. **Nothing may configure `[server]`.** Those options belong to Radicale's own listener, which never
   runs here. Setting `hosts: ""` makes Radicale refuse to build at all, and since the app builds it
   at import, **that took the entire app down**.
3. **The mount must be non-fatal in fact, not just in intent.** The first version's `except` called
   an undefined `logger`, so the guard meant to keep a calendar problem from stopping startup raised
   `NameError` instead — every request 502'd.
4. **Radicale logs ~60 INFO lines about its own configuration on every construction.** Its `logging`
   section is read before ours applies, so the level is set on the logger directly.
5. The storage API is **synchronous** and runs in a threadpool (a2wsgi), so each call opens its own
   event loop and its own DB session — the request's session is closed by then.

## Tests

```
venv-unified/bin/python -m unittest tests.test_calendar
```

Covers the plugin contract, the password hashing, and the `.ics` helpers. The end-to-end path was
verified by hand against the mounted server: `PROPFIND` (207 with the password, 401 without),
`MKCALENDAR`, `PUT`, `GET`, `DELETE`, a wiped cache re-hydrating from the relay, and the stored event
being ciphertext on the relay rather than plaintext.

## Subscribing to somebody else's calendar

Calendars → **Subscribe to a URL**. Paste a published `.ics` (or the `webcal://` link a site gives
you) — a school term, a team's fixtures, a holiday feed — and it is copied into a calendar of your
own and re-checked a few times a day. Because it becomes an ordinary calendar here, it syncs to your
phone over CalDAV like everything else, with no separate setup on the phone.

**A subscription is a MIRROR, not an import.** The feed is the source of truth, so a refresh also
DELETES what the feed has dropped — a cancelled fixture that lingers forever is worse than not
subscribing — and the calendar is read-only in the web UI, because anything you changed would be
replaced on the next update. **Unsubscribe** stops the updates and keeps the events you already have;
deleting the calendar is the separate button that removes them.

Refreshing happens in the **worker**, not in the browser, because the people reading these are usually
on a phone in an app that never opens PosterChan — a refresh that only ran when somebody looked at
the web UI would leave the phone confidently showing last term.

**Two things you may hit, both the publisher's end:**

* *"that URL did not return a calendar — it looks like a web page"*. The commonest mistake is pasting
  the page the calendar is displayed on rather than the feed it links to; look for a "subscribe" or
  `.ics` link on it.
* *"that site's security certificate could not be verified"*. Usually the site's own misconfiguration,
  or a certificate authority this server does not know about yet — it is not something you can fix.
  You are offered **Subscribe anyway**, which skips certificate checking **for that one feed** and
  nothing else. (Measured on a real school feed: its chain ends at Let's Encrypt's "ISRG Root YR",
  which no trust store here carries yet. A perfectly valid certificate that every correct client
  refuses.) The SSRF guard is unaffected by that choice — a private or link-local address is never
  fetched either way, on any redirect hop.

Tests: `tests/test_calendar_subscribe.py`.

# The phone shell — launcher, messages, phone

PosterChan can be an Android phone's **home screen**, its **messages app** and its **phone app**.
Each is separate, each is opt-in, and each is given back the same way. Nothing changes until you ask:
the switches are in **Settings → Use PosterChan as your phone** in the app.

This is not only a feature. Android's restrictions attach to **roles**, not to good behaviour, and
two long-standing bugs in this app are downstream of not having one:

* **HOME** means the process is foreground whenever nothing else is, so the WebView's render process
  stops being a low-memory-killer candidate. That killer is the documented cause of "the APK closes
  with no error" and of the background folder sweep being throttled.
* **SMS** and **DIALER** are the *documented* grounds for a battery-optimisation exemption rather
  than the heuristic one every other app is guessing at.

---

## The rule everything here is built on

**A launcher that fails takes the phone's home screen with it.** There is no other app to fall back
to, and somebody who cannot reach Settings cannot change it back without knowing a hardware key
sequence. This app's WebView renderer is *measured* to die under memory pressure — that is what
`MainActivity.surviveRenderProcessDeath` exists for.

So every screen in the phone shell is **native Android**. Not one of them inflates a WebView, starts
the Capacitor bridge, reads a Nostr key or touches the network:

| Screen | Class | Draws with |
|---|---|---|
| Home screen | `place.poster.app.home.HomeActivity` | plain `Activity` + `GridView` |
| Messages | `place.poster.app.sms.ThreadListActivity`, `ThreadActivity` | plain `Activity` + `ListView` |
| Phone | `place.poster.app.phone.DialerActivity`, `InCallActivity` | plain `Activity` |

PosterChan's own screens sit on the home screen as icons that **start** the browser engine. They are
never what the home screen is made of. `tests/test_android_launcher.py` asserts that HomeActivity
mentions no WebView, no `BridgeActivity` and no Capacitor, and the emulator asserts its live view
tree contains no `WebView` at all.

### It is shaped like a home screen

The first version was one scrolling grid of every app — an app drawer used as a home screen — and was
reported as *"missing traditional home desktop view"*. There are three layers now:

* **the desktop** (`DeskView` + `Desk`) — a cell grid holding icons **and other apps' widgets**,
  dragged into place and resized by hand;
* **the dock** — the toolbar of main icons along the bottom, on screen always;
* **the drawer** — every app on the phone, alphabetical and searchable, over the top. Opened from the
  dock's last button, closed by BACK.

**Widgets are real `AppWidgetHost` widgets**, picked from **our own list** and resized by dragging
the frame's edges.

**The list is ours, because handing step 1 to `ACTION_APPWIDGET_PICK` is not a route a third-party
launcher can rely on.** The first version fired that intent, and the catch that was meant to explain
an exotic failure was on the *only* path: it freed the widget id and showed "this phone has no widget
picker", so every route into the flow ended there — which is why "no widgets can be added to
posterchan launcher home screen" survived a round of fixes that included making the flow findable
from three menus. All three led to the same dead end. **Nothing about that is visible from a source
file**: an intent that cannot be started and a person cancelling a dialog land two lines apart.

**A theory this device refuted, recorded rather than repeated.** The tidy explanation was that
nothing answers that intent any more. The emulator says otherwise — `systemPicker=true` on API 34 —
so that explanation is wrong, at least on that image, and the device test now *prints* what the
activity is (package, exported, permission) instead of asserting a story about it. Resolving and
being startable by us are different questions. What does not depend on the answer is our own list:
every launcher builds one, it works whether or not an image has a picker, and it is the only version
of this flow that can show a preview or say "no app on this phone offers a widget".

`providers()` builds the list from `getInstalledProviders()`, sorted by owning app then by the
widget's own label, each row showing its size in cells. **An empty list says "no app on this phone
offers a widget"** — a different sentence from "the picker is missing", and telling those apart is
the difference between this round of the bug and the last three.

**"I want the calendar widget and weather widget!" was a report about this list, not about missing
features.** Calendar and Music were already installed and declared, both sitting in the picker,
scattered between Photos and System UI — and both called *"PosterChan"*, because neither receiver
declared a label. Nobody could pick them out, so nobody believed they existed. The list is grouped by
app with a heading per group and **ours come first**: this is PosterChan's own launcher and
PosterChan's widgets were the ones nobody could find. Everything after them stays alphabetical, so
nothing is hidden by the choice, and a heading is not selectable (`areAllItemsEnabled`/`isEnabled`)
or tapping the word "Clock" silently adds whatever row happened to be under it.

**A name is not enough to choose by** — *"widgets UI is terrible now. You have no idea which widget
you are adding."* Each row draws the real thing: `previewImage`, then **`previewLayout`** (API 31+,
which is what modern providers ship *instead* and which `loadPreviewImage` does not render — it is a
layout id in the *provider's* resources, so it is inflated against that package's own
`CONTEXT_RESTRICTED` context and drawn to a bitmap), then the provider's icon, then the app's. The
owning app is on the row too, because "Clock" from three apps is three identical rows otherwise.

**Our own widgets were the worst offenders and it was our bug, not the picker's.** A widget's label
is its *receiver's* label; neither of ours declared one, so `loadLabel` fell back to the
*application* label and the list read `PosterChan / PosterChan 2x2` and `PosterChan / PosterChan
3x1` while every other app read `Clock / Analog`. That was found in the emulator's provider dump, not
guessed, and a device test now asserts Calendar, Music and Weather are three distinct names.

**A placed widget could not be long-pressed, so it could not be removed** — reported as *"no way to
remove widgets"*, and it was the same reason one could not be moved or resized. An icon cell is an
inert `View`, so its touches fall through to `DeskView`. An `AppWidgetHostView` is not: its
RemoteViews children carry PendingIntents, so they are **clickable and consume the DOWN**, and
`DeskView` was never told a finger had gone down on a widget. The long press was never armed and
every menu hanging off it — Remove from home, Resize, Add a widget — was unreachable on the one kind
of item that most needs them. That is `247a1be8`'s shape exactly: a child that eats the gesture is
invisible in the parent's code. The DOWN is watched in `onInterceptTouchEvent` now, and the gesture
is only **stolen** once the long press has actually fired, so a short tap still reaches the widget's
own buttons.

**`setItems` had called `v.setClickable(false)` on the child since `66c7f2ec`, and that could never
have worked** — it clears the flag on the `AppWidgetHostView` *itself*, while the views that consume
the touch are its RemoteViews *descendants*. The device probe prints `clickableContent=true` for a
widget whose host view is not clickable, which is the whole bug in one line. A fix aimed one level
too high looks correct in the diff and changes nothing on the phone. The menu is titled with the widget's own name, too — a menu about an existing widget
headed "Add a widget" reads as the wrong menu.

Three things after that are easy to get half right and each fails silently: `BIND_APPWIDGET` is
signature-level so a third-party launcher must ask via `ACTION_APPWIDGET_BIND` (skip it and the
widget draws nothing, for ever — and the ask must carry `EXTRA_APPWIDGET_PROVIDER_PROFILE`, or a
work-profile widget binds against the wrong user and the dialog refuses in a way that looks like the
person said no); a provider's configuration activity that is never started leaves the classic grey
box; and a widget that is not told its new size with `updateAppWidgetOptions` keeps drawing its old
layout inside the new hole. Every failure path gives the allocated id back — a leaked one is a row in
the system's own table nothing will reclaim.

`WidgetDeviceTest` drives all of it on a real API-34 device. `appwidget grantbind` stands in for the
one step that needs a person, so allocate → bind → configure-or-ready → draw → place all run for
real; it asserts the refusal **before** the grant too, so the bind dialog cannot quietly become dead
code, and it checks the id count so an abandoned add cannot leak.

### It fits the screen it is on

`HomeMetrics` is pure and run by tests. Four columns and a five-slot dock are right on a phone; on a
ten-inch screen they are four icons the size of coasters and five more floating in the middle of a
bar. Above `smallestScreenWidthDp` 600 the grid widens to 5-7 columns, the dock to 6-9 slots at 64dp,
and the drawer's `auto_fit` column width goes from 80dp to 104.

**Columns come from the SHORT side, never from the current width.** If they came from the width, every
rotation would re-flow the arrangement through `Desk.fit` — nothing deleted, but rotate to landscape
and back and your icons are not where you left them, for ever, and a tablet rotates all the time. A
phone in landscape is still a phone for the same reason: its width in dp is tablet-sized and its
ergonomics are not.

**Rows come from the height that was actually available, and that is only knowable after layout.**
`deskRows` divides the desktop's measured height, which during `onCreate` and `onStart` is zero — so
every first draw used the fallback and the grid only ever adapted if something later happened to
redraw it. `resizeSoon()` is a `post` after the first layout pass; it redraws only if the shape
changed.

**The desktop is stored per grid shape** (`desk.6x4`), so landscape and portrait are two
arrangements rather than one being fitted into the other. A shape that has never been seen
*inherits* the previous one rather than starting empty — a blank desktop after a rotation reads as
the launcher having thrown everything away.

Where things sit is `Desk`, which is pure and run by tests. Its three load-bearing rules:

* **nothing is dropped for not fitting.** The grid changes size on a rotation, on a tablet, on a
  restored backup from a bigger phone — anything that no longer fits is re-placed, never deleted.
  `if (!fits) continue` is the silent version of throwing away what somebody arranged by hand.
* **nothing overlaps**, and a move onto something else is refused and puts the icon back.
* **a resize that would collide is refused, not clamped.** A widget that comes back a different size
  from the one you dragged to is a widget that feels broken, and you cannot tell whether it was you.

### The way back

The grid always carries a **Phone settings** tile. It is *essential*: it cannot be hidden, it
survives every filter, and it opens the system Settings app **by intent action** rather than by
package name (that name differs on every OEM). It works when the package query returned nothing,
when the saved arrangement is corrupt, when the WebView is dead and when this app's own UI will not
start.

Two more rules in `AppShelf`, both run by tests:

* an arrangement that would empty the grid is **ignored**, not obeyed — a `hidden` set covering every
  phone app is a broken arrangement, and a grid of only PosterChan tiles is the kiosk this must never
  become;
* a **search always sees hidden apps**, so hiding is never a one-way door.

### Giving the home screen back

`HomeRoles.releaseHome` refuses — having changed nothing — when PosterChan is the only home app on
the phone. Disabling the component then leaves the device with no home screen at all and no way to
install one. Android will happily let you do it.

### Opt-in means the chooser never appears unasked

`HomeActivity` ships `android:enabled="false"`. A `CATEGORY_HOME` activity makes Android offer this
app in "Select a Home app" from the moment it is installed — including to people who installed a
Nostr client and have no idea it can be a launcher. The component is switched on at the moment
somebody asks, and off again when they stop.

---

## Battery

With the HOME role the process is resident for the life of the battery, so **anything it polls, it
polls for ever**. There is therefore no timer, no wake lock, no periodic refresh and no
`WorkManager` job anywhere in `home/`, `sms/` or `phone/`. What replaces them:

| Instead of | It uses |
|---|---|
| polling the app list | `PACKAGE_ADDED/REMOVED/CHANGED` — registered in `onStart`, gone in `onStop` |
| polling now-playing | a push from `MusicService` (a second, additive hook; never audio focus) |
| polling for new texts | a `ContentObserver` on `content://sms`, up only while a screen is |
| polling call state | `InCallService`'s own callbacks |
| a wake lock during a call | `FLAG_KEEP_SCREEN_ON`, which is scoped to the activity and released with it |

Icons load lazily on two low-priority threads into a bounded cache and stop the moment the grid is
off screen. The only repeating thing in the entire feature is the call timer, and it repeats because
it displays seconds.

**Measured**, not asserted: `scripts/android_device_checks.sh` takes the HOME role on the emulator,
presses HOME, turns the screen off for a minute and prints the wake locks held, the alarms and jobs
scheduled, and the CPU sample. `tests/test_android_launcher.py` fails on any of `newWakeLock`,
`setRepeating`, `PeriodicWorkRequest` or a `postDelayed` that posts itself.

---

## Messages

### What is authoritative

**On the phone, the system message store is the truth.** Only the default messages app may write
`content://sms`, and it must — every other app on the phone and every backup reads it. A messaging
app that keeps texts somewhere private silently takes them away from all of that.

`SmsDeliverReceiver` is the one code path in this app where a mistake is unrecoverable: a text that
is not written there does not exist anywhere, there is no retry, and nothing in any log says it
happened. So the steps are ordered by what matters — **store, then notify, then tell the app** — and
each is guarded separately, so a failing notification cannot cost the message.

### The archive

On top of that, each message is published as **one encrypted Nostr document**: kind `30078`,
`d = pcai:sms:<24 hex>`, tagged `l=pcai-sms`, NIP-44-sealed to the user's own key and direct-published
to their own relay. That is what lets a laptop read and answer the same conversation
(`static/js/client/sms.js`, sidebar → **Texts**).

It **mirrors; it never replaces**. When the two disagree, the phone wins.

The address is derived from the **message** — the number's last seven digits, the second it was sent,
the direction and the body — never from the provider's row id. A row id is local to one handset, so a
restored backup would renumber everything and republish the whole history.

### Deleting

Deleting publishes a **tombstone at the same address** (an addressable event's newest version is what
every client sees, so the old ciphertext stops being served) plus a NIP-09 kind 5, **and** removes the
row from the phone's provider. On a public relay the kind 5 would be a request; here it is a real
delete, because these events are direct-published to the user's own relay and replicate nowhere — the
same property the folder-sync records rely on.

The UI says which copies went and does not promise the ones it cannot reach.

### Sending from another device

A laptop cannot reach a radio, so it publishes an encrypted request at `pcai:smsout:<id>` and the
handset performs it, replacing that same document with a completion marker. One addressable event has
exactly one newest version, so the marker cannot race the request it answers.

**It needs the phone to be reachable**, and says so rather than pretending: a request published to a
handset that is switched off waits until the app is next opened, and the UI reports it as *waiting for
your phone*, never as sent. A request older than a day is discarded rather than sent — a phone that
was off for a week must not wake up and deliver a week of messages whose moment has passed, and there
is no way to un-send a text.

The id is computed in **Java on the phone and in JavaScript on the laptop**, and
`tests/test_android_sms.py` runs the two against each other. Compute it differently and the marker
lands where nothing is watching, so the phone sends the message again on every drain, for ever.

### The three auto-cleaners

Kind 30078 was chosen because it already carries two of the three exemptions a private library needs
here, each of which cost Notes a total silent loss before it was learned:

1. the relay's NIP-40 expiration sweep skips 30078 (and drops the tag at ingest, since a stored
   expiration hides an event from every read);
2. the paid-retention tier's `kind IN (_PRUNABLE_KINDS)` qualifier spares it;
3. **the client cache's newest-N eviction had to be told.** `pcai:sms` is in `_isPinned` (store.js)
   and in `_CARRY_D` (app.js). On every device that is *not* the phone the archive is the only copy,
   so eviction there is not a cache miss — it is the messages being gone.

### Picture messages are READ; FETCHING a new one is still not supported

Two different pieces of work wearing one word, and collapsing them is what lets a screen promise the
second while delivering the first. `SmsPlugin.status` reports them separately — `mms` and `mmsFetch`.

**Reading** (`MmsStore` + `Messages`): everything the phone already has — every picture message ever
sent from it, and every one received while another app was the default messages app. This needs no
role, no relay and no network, and it is the half somebody notices first.

`content://mms` is a **different table** from `content://sms` and almost nothing carries over. It has
its own columns, its own message-box constants, the sender in a second table (`/addr`), the content in
a third (`/part`), and its `date` is in **seconds** where the SMS table's is in milliseconds. Read as
milliseconds every picture is dated 1970 and sorts to the bottom of every thread; used the other way
round in a `WHERE` clause, `since` matches nothing until the year 55000 and the archive silently never
publishes a picture at all. The one thing the two providers share is `thread_id` — both write into the
same `threads` table — which is why `Messages` is a sort and not a match. Three screens go through it
(the WebView's Texts view, `ThreadListActivity`, `ThreadActivity`), because a conversation that
interleaves on one and not on another is the same bug reported three times.

**Identity gains the attachments.** A picture message frequently has no text, so `docId`'s
who/when/direction/body is the identical string for two photos sent inside one second; filed at one
address the second replaces the first and one of them is gone from every device that is not the
handset. `SmsKeys.docId(..., partsKey)` appends the attachments' content type, the name the *sender's*
phone chose and the length — all three ride in the PDU, so they mean the same on every device, unlike
a provider row id. It stays in the **`pcai:sms:`** namespace on purpose: all three auto-cleaner
exemptions above are keyed on that prefix, and `pcai:mms:` would have matched none of them. An empty
parts key gives a byte-identical id to the four-argument form, so a text-only message read back
through the MMS path is not a second document. `tests/test_android_mms.py` runs the Java and the
JavaScript against each other.

**The archive carries attachments through encrypted Blossom storage.** The phone reads each provider
part, encrypts it with the user's drive key and uploads the ciphertext into the logical `MMS` folder.
The Nostr document carries the encrypted-store hash plus type/name/length, never the plaintext bytes.
Images also receive a small encrypted thumbnail: other clients show that first and fetch the original
only when it is opened. Message bodies use the same design in the logical `Messages` folder, so the
relay document is a small encrypted pointer rather than a second place holding the payload.

**A delete is now two URIs as well as two copies.** A picture message is `content://mms/<id>`; handed
to `SmsStore.delete` it removes nothing AND reports nothing, which the client correctly reads as a
provider refusal — so the archive is left alone and the delete quietly did not happen.

**The two tables refuse independently.** Several OEM builds guard the MMS tables differently from the
SMS ones, so a phone whose texts read perfectly can hand over no pictures at all. `refused` and
`mmsRefused` are separate answers and the screen says which — a thread that silently lost its photos
looks exactly like a thread somebody sent fewer photos in.

### Fetching an incoming MMS is still not supported, and it says so

Android will not grant the SMS role without a `WAP_PUSH_DELIVER` receiver, so `MmsDeliverReceiver`
exists. What it does not do is pretend. Retrieving an MMS means decoding a WSP-encoded
`M-Notification.ind`, fetching from the carrier's MMSC over the MMS APN and decomposing the
`M-Retrieve.conf` into the `pdu`/`addr`/`part` tables — several hundred lines of binary parsing that
cannot be exercised without a SIM and a carrier, on the one code path where a mistake means somebody's
message is gone. Writing a placeholder row would be worse than nothing: it would put a message that
does not exist into every app and every backup on the phone.

So a picture message raises a notification saying plainly that PosterChan cannot fetch it and that
switching the messages app back will. **The opt-in screen says the same thing before the role is
taken**, which is the only honest place to say it. Nothing touches the provider; an MMS not fetched is
an MMS still waiting at the carrier.

---

## Phone

`PcInCallService` is the call UI when PosterChan is the default dialer. It is **not**
`place.poster.app.call.CallService`, which is a confusingly similar name for a different feature: a
Nostr WebRTC call, over the internet, to another Nostr user. Both live in the same process, so
everything cellular is prefixed `TEL_` and `pcai_cell_`:

| | Internet call (Nostr) | Mobile network call |
|---|---|---|
| service | `call.CallService` | `phone.PcInCallService` |
| actions | `place.poster.app.CALL_*` | `place.poster.app.TEL_*` |
| channels | `pcai_calls`, `pcai_ongoing_calls` | `pcai_cell_incoming`, `pcai_cell_ongoing` |

Separate channels matter to the person, not just to the code: silencing calls over the mobile network
must not silence calls over the internet, and there is no way back from a mis-shared channel except
uninstalling the app.

### Where a dialer goes wrong

Audio routing and call state, and the failures are never exceptions — they are a button that does
nothing. The platform answers an impossible request (answering a call that is already connected,
holding one that is still ringing, sending DTMF down a call that has not connected) by doing nothing
at all: no throw, no callback, no log.

So the legality of every control lives in **`CallRules`**, which has no Android in it and is run
state-by-state by `tests/test_android_dialer.py`. Illegal controls are **hidden, not greyed**: a
disabled-looking button and a live one that silently fails are the same thing to whoever presses it.

Two distinctions the code keeps and most hand-rolled dialers lose:

* **reject ≠ hang up.** Rejecting a ringing call can send the caller to voicemail; disconnecting one
  just drops it. Same red button, two meanings, chosen by the state.
* **DTMF only reaches a connected call.** A dialing call swallows every tone — the phone-tree digits
  somebody typed while it rang are simply lost, with the keypad drawing them the whole time.

### The ringer

`onCallAdded` starts the call screen *and* posts a full-screen-intent notification. The direct start
is refused **silently** on a locked or dozing device (Android has blocked background activity starts
since 10), and a phone that rings with nothing on screen is a missed call.

### The dialpad

`Dial` is pure and tested. Three rules whose failure is invisible:

* `+` is only a `+` in the first position (long-press zero) — anywhere else it is part of a number
  nobody can call;
* `,` and `;` are **pauses**, not punctuation: `+15550100,,1234` dials the extension after the call
  connects, and stripping them "to clean up the number" quietly breaks every stored phone-tree
  shortcut somebody has;
* a GSM **service code** (`*#06#`, `*21*…#`) goes through `ACTION_DIAL` so the platform can intercept
  it and show the result. Placed as a call it either fails or silently changes a network setting.

A tap on a recent call **fills the pad**; it does not dial. The green button is the commitment.

### Contacts, voicemail and search

A keypad and a call log is the half of a dialer nobody opens it for — which is how the first version
was reported. **Recents, Contacts and Voicemail** are three tabs over one list and one search box,
because they are the same question asked three ways.

Contacts come from `ContactsContract` across every account (`ContactList`), which is where
PosterChan's synced cards already are; searching is handed to `Contacts.CONTENT_FILTER_URI` rather
than reimplemented, because the provider already does the T9-style matching the rest of the phone
does. One row per person, not per number — the Phone table has a row per number, so somebody with a
mobile and a work line otherwise appears twice.

Voicemail is two different things and a dialer that offers one of them feels broken in a way people
find hard to describe: **holding "1"** calls the SIM's own voicemail number (`getVoiceMailNumber`,
never the literal "1", which just dials a stranger — a phone with none says so), and the **messages**
are the `VOICEMAIL_TYPE` rows the carrier logs. Opening one is handed to whoever owns the voicemail
source; the audio is the carrier's and fetching it is their protocol, not ours.

### The call log

`CallLog.Calls`, the phone's own, for the same reason as the message store. It is **deliberately not
mirrored to Nostr**: a text can be read and answered from a laptop, a call log entry can only be
looked at, and the copy would be a second source of truth for something with no second use.

---

## Contacts, calendar and music

Nothing here builds a second store of anything.

* **Contacts.** `PhoneBook` resolves a number through `ContactsContract.PhoneLookup` across **all**
  accounts — deliberately *not* scoped the way `place.poster.app.contacts` is, because that package is
  a reconcile where a short keep-set is a delete order. Caller ID and a message thread must name
  whoever the person has in their phone, including the cards PosterChan already syncs there.
* **Calendar.** `CalendarPeek` reads the same SharedPreferences blob the home-screen calendar widget
  draws from: the client pushes days it has already decrypted, and nothing native parses iCalendar or
  expands a recurrence rule. It matches an event's attendees (usually email addresses) to a caller's
  number through the phone's own address book, and shows one line — *"Sprint review · 15:00"* — beside
  a caller or a thread. A wrong line is worse than none, so anything it cannot resolve shows nothing.
* **Music.** The launcher's now-playing strip is **pushed** by `MusicService`, never polled, and its
  play/pause goes through the widget's receipt-checked broadcast. It does **not** request audio focus:
  the audio lives in the WebView, and a second request from this same app takes it from the first, at
  which point Chromium pauses the very element the music controls exist to keep playing.

---

## The settings live in one place

**User Settings → Phone**, and only on the packaged app: the three switches ask *Android* for a
system role, so on the web and in the desktop shell there is nothing for them to ask.

**A granted role is not read before it settles.** Granting is asynchronous on the system side: the
dialog returns and for a moment `getDefaultSmsPackage` still names the old app, so reading once in the
activity callback answers *no* for a role that was in fact granted — the switch springs back while
Android's own settings screen already says PosterChan. `HomePlugin.settle()` re-reads for about a
second and a half, watching **the specific role that was asked for**; settling on "any role is held"
returns instantly for somebody who already has the home screen, which is the same bug wearing a
different hat. The client re-reads once more after a moment, and again whenever the page becomes
visible, because the role dialog takes the person out of the app entirely.

**A refused role is named.** Android refuses a role the app cannot hold by starting the request
activity and finishing it immediately with `RESULT_CANCELED` — no dialog, no error, no log. The
switch flips, nothing appears, and it flips back, which is exactly what a switch that was never wired
up does; it was reported as *"sms does nothing when checked"*. So `status()` now reports whether the
build declares the components each role needs at all, the switch says so instead of offering a request
that cannot succeed, and a request that comes back without the role offers Android's own **Default
apps** screen — which on an OEM build that suppresses the role dialog is the only route there is.

## Themes and icons

The native screens have no stylesheet, so all nine of the client's themes are transcribed into
`place.poster.app.ui.PcTheme` and the client mirrors the chosen slug into SharedPreferences whenever
it changes (`PcThemePlugin` → `PcThemeStore`). `localStorage` stays authoritative; this is a copy,
written only from it. `tests/test_android_theme_palettes.py` parses `client.css`, runs the Java and
compares them value for value.

**The icons were broken by packed SVG arc flags, and only a device could say so.** SVG lets an arc
pack its two flags against the following number — `a9.8 9.8 0 01-2.6-.35` is large-arc 0, sweep 1,
x=-2.6 — and every SVG renderer reads it correctly. Android's `PathParser` reads numbers greedily:
`01` becomes the number 1, the arc runs out of parameters, and the whole `VectorDrawable` fails to
inflate with `Resources$NotFoundException`. **26 of the 63 transcribed glyphs were written that way**,
which is exactly *"the icons are mostly letters"*. `normalize_path()` in the generator re-emits every
command space-separated; the geometry is untouched (all 119 sprite glyphs rasterise byte-identically
before and after).

Every static check had passed: the files existed, the names were right, the geometry was there, and
rasterising all 63 locally showed visible pixels — because `rsvg` is a real SVG parser and only
Android is this strict. The instrumented test that draws each icon and counts lit pixels is what
found it, the first time it was ever able to run.

**A tile is never blank.** An icon that does not resolve falls back to the app's initial rather than
to an empty circle — a coloured circle with nothing in it is indistinguishable from a broken
launcher, and that is how it was reported. The cause was a vector carrying a baked `android:tint`
*and* a runtime colour filter: it inflates fine, reports a size, and paints no pixels. The tint is
gone, tinting goes through `DrawableCompat`, and an instrumented test now **draws every tile icon and
counts the lit pixels**, which is the only question that separates "the resource exists" from "the
icon is visible".

Icons are **transcribed, not redrawn**: `scripts/gen_android_icons.py` reads
`static/js/client/sprite.js` and emits a `VectorDrawable` per glyph plus the name→`R.drawable` switch.
Colour is applied at runtime from the palette, so nine themes cost nine tints rather than nine icon
sets. No emoji anywhere — `tests/test_android_icon_sprite.py` fails on one in a UI string.

---

## Tests

| What | Where |
|---|---|
| the home screen's decisions, run | `tests/test_android_launcher.py` |
| message identity, the four role components, the delivery order | `tests/test_android_sms.py` |
| call-state legality, the dialpad, the role components | `tests/test_android_dialer.py` |
| nine palettes against `client.css` | `tests/test_android_theme_palettes.py` |
| icons against the sprite | `tests/test_android_icon_sprite.py` |
| the tablet grid, the dock and the per-shape desktop | `tests/test_android_launcher.py` |
| the whole shell compiles against the real `android.jar` | `tests/test_android_shell_compiles.py` |
| the instrumented tests themselves compile | `tests/test_android_device_tests_compile.py` |
| adding a widget, end to end, on a real device | `androidTest/.../home/WidgetDeviceTest.java` |
| Messages, Phone and Email are in the drawer | `androidTest/.../shortcut/DrawerAppsDeviceTest.java` |
| on a real Android | `mobile/android/app/src/androidTest/` |
| the lifecycle, Doze, and what the launcher costs | `scripts/android_device_checks.sh` |

`tests/androidcompile.py` is what makes the sixth row possible on a machine with no Gradle: it finds
the `android.jar` on the box, synthesises `R` from the real `res/` tree, and compiles against the
genuine SDK instead of hand-written stubs. It found three real bugs on its first run.

## What is deliberately not built

* **MMS retrieval** — see above. Existing picture messages are read and shown; *fetching* a newly
  arrived one off the carrier's MMSC is not built. The receiver exists because the role requires it;
  it says what it cannot do rather than pretending.
* **Call-log mirroring to Nostr** — a second source of truth for something with no second use.
* **Notification mirroring** — forwarding *every* app's notifications to other devices needs
  `BIND_NOTIFICATION_LISTENER_SERVICE`, which reads every notification on the phone. What is built is
  narrower and needs no such permission: an incoming text raises a notification on your other devices
  because its archive document arrives there.
* **Group MMS threads** — a group picture message is READ (it lands under its first recipient, which
  is where the client's last-seven-digits grouping puts it), but there is no group *conversation*:
  current MMS sending addresses one conversation/recipient at a time.

---

## Messages, Phone and Email are apps, not just handlers

Only `MainActivity` carried a `MAIN`/`LAUNCHER` filter, so Messages and Phone could be *routed* to as
the phone's default handlers and appeared in no drawer at all — PosterChan's own or the stock one.
From the person's side they did not exist: *"my point is that there is no phone app/icon for it!"*

Both now have an **`activity-alias`** with `MAIN`/`LAUNCHER`, its own label and its own icon. An alias
rather than a second filter on the activity, deliberately: `ROLE_SMS` is granted only to an app
declaring exactly four components, one of which is `SendToActivity`'s `SENDTO` filter, and every
future edit to that block would then be one slip from Android silently refusing to offer the app as
default. The alias leaves the routing filters untouched, and it is the shape this manifest already
uses for `ShareToAi`.

They appear in **every** launcher, which matters because the HOME role is opt-in and most people will
keep their existing home screen. The icons are generated from the same sprite
(`scripts/gen_android_app_icons.py`) — a handset, a speech bubble and an envelope, so a drawer shows
four different things rather than the PosterChan mark four times. Adaptive for Android 8+, with
legacy rasters per density because minSdk is 23 and an adaptive icon alone does not resolve on 23-25.

**Email is the third one and it could not be an alias onto a native activity** — the mail client is a
view inside the WebView, so there is nothing to target. `.shortcut.ViewActivity` is a trampoline: no
layout, no window, no WebView of its own, it starts `MainActivity` carrying the same
`HomeActivity.EXTRA_VIEW` the launcher's own tiles use (consumed by `HomePlugin.consumeLaunchView` →
`PC.switchView`) and finishes before it has drawn anything. **Which view is the alias's `meta-data`,
not a Java fact**, read back off the launching component — an alias reports *itself* from
`getComponentName()`, the same property `.ShareToAi` relies on — so the next PosterChan screen in the
phone's drawer is an alias and a string and no code at all.

`DrawerAppsDeviceTest` asks the only authority there is: a `MAIN`/`LAUNCHER` query against this
package on a real device, plus that no two entries share a name or an icon. A routing filter
(`SENDTO`, `APP_MESSAGING`, `DIAL`) makes an app the phone's default handler and puts it in no drawer
at all, which is exactly how Messages and Phone existed while their owner correctly reported that
they did not.

## A tile that cannot launch is never drawn

Reported from the dock: *"there is some P icon on the dock that says this app would not open, useless
does nothing"* — two failures stacked on the one row that is always on screen. Three rules now:

* **`canLaunch` is asked of the package manager** (`resolveActivity`) before a tile is offered, so a
  screen that is in the catalogue but not in *this* build simply is not there. Absent, not greyed,
  not erroring on tap.
* **The dock is seeded from the filtered list**, never from raw ids, so the first thing a new person
  sees cannot be a dead button.
* **Our own tiles never fall back to a letter.** Every PosterChan screen has a real sprite glyph, so a
  letter is not a fallback but a bug in disguise — it hides which tile failed. A glyph that will not
  resolve falls back to the app's own launcher icon and says so in the log. The initial-letter
  fallback stays where it belongs: a third-party app whose icon the package manager would not give us.

## Swipe up for all apps

The drawer opens by swiping up from the home surface and the button is off the dock — what every
Android launcher has done since Pixel dropped it, and a dock slot back for an app somebody uses.

The gesture is measured against `ViewConfiguration`'s slop and fling velocity rather than a hand-picked
pixel count, which would feel wrong at a different density. **But slop is a density answer to a size
question**: six times it is about 48dp, a deliberate drag in a hand and a twitch on a ten-inch screen
propped on a desk, so the travel needed is the larger of that and a sixteenth of the desktop's height
(`HomeMetrics.swipeUpMinPx` — unchanged on every phone, proportional above). The **fling** half is
untouched, which is why this cannot make the drawer harder to open: a flick still opens it at any
distance. It is only ever considered while nothing
is lifted and nothing is being dragged, so a long-press-drag always wins; and it closes three ways —
**Back**, a **swipe down** (only while the grid is already at the top, or flicking back up through a
long list would close it under your finger) and **Home**.

## The keypad is a whole tab

*"The Phone app should be an entire tab that looks like a nice dialer, glow keys when pressing."*
Keypad is the first tab and the one you land on; on it the list is gone and the pad gets the screen,
sized from the display rather than a constant — a fixed dp that suits a tall phone clips the bottom
row on a short one, and a bottom row you cannot reach is a dialpad with nine keys.

**Every key lights up under your finger** (`KeyGlow`): a bloom outside the rim, a bright ring and a lit
interior, all in the palette's accent, drawn with concentric strokes rather than a `BlurMaskFilter`
(which needs a software layer under hardware acceleration and silently draws nothing without one).
The digit lights with it, on the way *down* — a glow that arrives when you let go is a glow nobody
sees. On the light palettes it degrades to a firm flat colour change, because a bloom behind dark text
destroys it.

Two things that make a stateful Drawable actually work, both silent when wrong: it must declare
`isStateful()` **and** return `true` from `onStateChange` to ask for the redraw, and the view must be
`clickable` or the background never hears about the press at all.

---

## The device was answering, and the job was throwing the answer away

Twice, and both cost rounds of guessing at things a device had already measured.

**`connectedDebugAndroidTest` fans out to every subproject**, and the subprojects here are the
Capacitor plugins under `node_modules`. One of them, `send-intent`, declares `minSdkVersion 22`,
which the manifest merger refuses against `capacitor-android`'s 23 — for the **androidTest variant
only**. So `:send-intent:processDebugAndroidTestManifest` failed, gradle exited non-zero and the step
went red **after `:app:connectedDebugAndroidTest` had already run all 34 tests on the device and
passed every one**. A red job whose real answer was green is worse than a red job: it read as "the
device tests are still broken", so the icon fix underneath it was reported as unverified when the
device had in fact verified it. It is `:app:connectedDebugAndroidTest` now, and none of those plugin
modules has a single androidTest source (every one logs `NO-SOURCE`), so nothing is being skipped.

**The HOME check pressed HOME on a locked device.** The section above it deliberately turns the
screen off, so the phone comes back on the keyguard, and a HOME press there resolves to
`com.android.settings/.FallbackHome` — the placeholder shown to a user who has not unlocked. That is
neither our launcher nor the stock one, so the check stood the stock launcher down (which changed
nothing), pressed HOME again, saw `FallbackHome` again, and failed with "HOME did not bring up our
launcher". Every emulator run reported the launcher as broken for a reason entirely about the lock
screen. `wm dismiss-keyguard` comes first now.

**And the logcat the tests themselves write is published.** A device test can measure something there
is no assertion for — whether an image ships the system widget picker, how many widget providers it
has — and the XML report carries only failures. `android_instrumented.sh` dumps logcat into the
report artifact, so getting a fact off the device no longer means failing a test on purpose.

**The HOME-role leg cannot run on the emulator, and says so rather than failing.** `pm enable
<component>` prints *nothing* on the API-34 google_apis image — not a success line, not an error —
the component stays out of the `MAIN`/`HOME` query, and `cmd package set-home-activity` then answers
*"Error: Failed to set default home"*. The HOME key falls through to
`com.android.settings/.FallbackHome`, which is the system saying "I have no home app I can use", not
a launcher that beat ours; standing the stock launcher down changes nothing. Two rounds went into
that before the check printed enough to answer it. It now asks the question directly — is our
HomeActivity in the HOME query at all — and reports a **SKIP with its reason** when it is not, which
is this repo's own rule for a check that could not run. Nothing is lost: the instrumented tests
exercise the launcher hard on the same boot. Everything below it still runs, started with `am start`
instead of the key, because the screenshots, the drawer swipe, the tablet resize and the wake-lock
measurement need the launcher *on screen*, not the role.

**Tablet mode is measured on the same emulator**, with no second AVD and no second boot:
`wm size 2560x1600` + `wm density 240` gives a 1066dp short side, which Android reports as a large
screen, so the launcher takes the tablet path through the same configuration change a real rotation
delivers. It lives in `LauncherDeviceTest.onATabletTheGridIsWiderAndTheDockIsLonger` rather than in
`android_device_checks.sh`, because that script's whole launcher section skips on this image (it
cannot enable the home component) — an instrumented test enables it from *inside* the app, which
does work. The phone grid is read first as a control on the same boot, the live configuration is
checked against `HomeMetrics` rather than assumed, and the size is reset in a `finally`: a device
left resized poisons every test after it. **Verified green on API 34.** The script still does the
resize for its screenshots when it does hold the launcher.

---

## The weather widget, and what leaves the phone

*"i want the calendar widget and weather widget!"* The calendar widget already existed
(`calendar.CalendarWidget`); it was unreachable because the picker was dead. Weather is new
(`place.poster.app.weather/`), and the only interesting question about it is where the numbers come
from.

**It asks this user's own PosterChan instance — `<base>/api/weather` — and nothing else.** No third
party is contacted from the phone. The node was already proxying Open-Meteo for the desktop's weather
widget (`app/services/weather_service.py`) precisely so that a reader's IP and coordinates never
reach an upstream, and it caches on the coordinate **rounded to about a kilometre**, so what the
forecast service sees is one server and a grid square. This widget adds no new destination to the app.

**There is no location permission anywhere in the feature.** The place is *typed* and looked up
through the same node's `/api/weather/geocode`, so the phone's own location is never read and the
widget never has to ask for it. A permission prompt for a home-screen widget is a bad bargain, and a
place somebody chose beats a fix from a cold GPS. The picker is the widget's own **configuration
activity**, which the system starts the moment the widget is placed — a widget that lands blank with
no way to fix itself is the classic grey box — and a tap on an unconfigured one opens it again.

**It never goes blank and it never lies.** A failed fetch writes nothing, so what is on screen is the
last real reading with its age beside it once that age is worth mentioning; a fresh reading carries
no timestamp, because a timestamp on every reading trains people to ignore the one that matters. A
reading that arrived without a temperature draws an em dash — `0°` is a real temperature and would be
a confident lie in exactly the weather somebody is checking before choosing a coat. And the three
empty states are three different sentences: **"Tap to set your location"**, "weather needs your
PosterChan server", "no forecast yet — tap to try again". One "unavailable" sends people looking in
the wrong place.

**It is the one widget here that polls**, and that is worth stating because nothing else in this
feature does. The calendar is pushed a month ahead and the music widget is pushed on every change, so
both are `updatePeriodMillis="0"`. A forecast is the one thing that genuinely goes stale on its own,
so this asks for an hourly tick — clamped by the platform to a 30-minute minimum and batched with
other wake-ups — and a tap refreshes immediately, which is what makes an hour rather than a minute
acceptable. The fetch runs on a plain background thread, never on the broadcast's main thread: a
receiver that blocks is an ANR drawn on somebody else's home screen.

The instance URL is the one thing the widget cannot work out for itself — the launcher's process has
no session and one bundle serves every instance — so `WeatherPlugin.sync` mirrors it across from the
client exactly as `PcThemePlugin` mirrors the theme. **An empty base is a real answer**, not a failure
to send one: it is the "no server" state above.

The display rules are pure (`weather.Weather`) and `tests/test_android_weather.py` runs them under
javac, including that the words match the client's own `_wxDesc` grouping — the desktop widget and the
phone widget describing the same sky differently is the kind of difference nobody reports and everybody
notices. The condition glyphs are hand-written (`ic_wx_*`), and a test asserts **not one of them
contains an arc**: packed arc flags are what made 26 of 63 generated icons fail to inflate, and these
are the only vectors here that no generator checks.

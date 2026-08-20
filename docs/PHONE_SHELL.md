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

**Widgets are real `AppWidgetHost` widgets**, picked through Android's own picker and resized by
dragging the frame's edges. Three things there are easy to get half right and each fails silently:
`BIND_APPWIDGET` is signature-level so a third-party launcher must ask via `ACTION_APPWIDGET_BIND`
(skip it and the widget draws nothing, for ever); a provider's configuration activity that is never
started leaves the classic grey box; and a widget that is not told its new size with
`updateAppWidgetOptions` keeps drawing its old layout inside the new hole. Every failure path gives
the allocated id back — a leaked one is a row in the system's own table nothing will reclaim.

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

### MMS is not supported, and it says so

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
| the whole shell compiles against the real `android.jar` | `tests/test_android_shell_compiles.py` |
| on a real Android | `mobile/android/app/src/androidTest/` |
| the lifecycle, Doze, and what the launcher costs | `scripts/android_device_checks.sh` |

`tests/androidcompile.py` is what makes the sixth row possible on a machine with no Gradle: it finds
the `android.jar` on the box, synthesises `R` from the real `res/` tree, and compiles against the
genuine SDK instead of hand-written stubs. It found three real bugs on its first run.

## What is deliberately not built

* **MMS retrieval** — see above. The receiver exists because the role requires it; it says what it
  cannot do rather than pretending.
* **Call-log mirroring to Nostr** — a second source of truth for something with no second use.
* **Notification mirroring** — forwarding *every* app's notifications to other devices needs
  `BIND_NOTIFICATION_LISTENER_SERVICE`, which reads every notification on the phone. What is built is
  narrower and needs no such permission: an incoming text raises a notification on your other devices
  because its archive document arrives there.
* **Group MMS threads** — a group conversation needs MMS, which is the first item on this list.

---

## Messages and Phone are apps, not just handlers

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
(`scripts/gen_android_app_icons.py`) — a handset and a speech bubble, so a drawer shows three
different things rather than the PosterChan mark three times. Adaptive for Android 8+, with legacy
rasters per density because minSdk is 23 and an adaptive icon alone does not resolve on 23-25.

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
pixel count, which would feel wrong at a different density; it is only ever considered while nothing
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

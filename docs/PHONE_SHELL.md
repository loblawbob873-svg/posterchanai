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

## Themes and icons

The native screens have no stylesheet, so all nine of the client's themes are transcribed into
`place.poster.app.ui.PcTheme` and the client mirrors the chosen slug into SharedPreferences whenever
it changes (`PcThemePlugin` → `PcThemeStore`). `localStorage` stays authoritative; this is a copy,
written only from it. `tests/test_android_theme_palettes.py` parses `client.css`, runs the Java and
compares them value for value.

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

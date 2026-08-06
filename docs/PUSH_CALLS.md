# Ringing a closed app — push, calls, and mobile battery

How a call or a message reaches a phone whose screen is off, what works today, and what's missing.
Written as a plan, so each phase says what it changes and how you'd know it worked.

## How it works today

**Calls are the only thing that rings immediately. The rest polls, and DMs don't notify at all.**

| What | Mechanism | Latency |
|---|---|---|
| **Calls** (25050) | live relay subscription, `_call_sub_loop` | immediate |
| Mentions, replies, reactions, zaps, NIP-22, NIP-28 chat | APScheduler poll, `_POLL_SECS = 20` | up to 20s |
| Joined-channel chatter | poll, `_CHAN_POLL_SECS = 60` (+300s per-room cooldown) | up to 60s |
| **DMs** | **none** | — |

That last row is not an oversight in this document. `_KINDS = [1, 6, 7, 9735, 1111, 42]`
(`nostr_push_service.py:25`) contains neither kind 4 nor kind 1059, so a gift-wrapped NIP-17 DM
produces **no push by any path**. A closed phone is never told about a message. See Phase 5.

Calls have to be a live subscription rather than a poll, because 25050 is **ephemeral** — the relay
transmits it to current subscribers and never persists it (`nostr_relay/server.py:107`), so there is
nothing for a later query to find.

**App open.** `startCallSignaling()` opens `{'#p':[me], kinds:[25050]}` and the relay sends the invite
down that socket the moment it accepts it. `_ringtone()` beeps every 1.4s. Latency is a round-trip.

**App closed or backgrounded.** The client's socket dies — `relay.js:66` skips the heartbeat while
hidden (the OS freezes the timer anyway) and the proxy idle-closes it; the client never closes it
itself. Instead the **server** holds the subscription: `_call_handler` watches its own relay and sends
a **Web Push** to the `PushSubscription` rows matching the `p`-tagged callee. The service worker shows
it with `requireInteraction: true` and a distinct vibrate pattern, and **suppresses it when a window
is focused** — an open app rings itself, and peers exchange 25050 for the whole call, which would
otherwise spam the caller. That suppression is call-only (`sw.js:504`).

A 30-second cooldown is keyed on the **callee**, not the caller/callee pair: the relay accepts a 25050
for any WoT recipient, so an attacker re-signing with throwaway keys would defeat a per-pair limit.

**The 30s relay ping is not part of this.** `ping_interval=30` is liveness only — detecting a socket
that has silently died and keeping NAT state warm. Events arrive unsolicited regardless of it.

### What that means per platform

| | App open | Backgrounded / closed |
|---|---|---|
| **Desktop browser** | ✅ | ✅ Web Push |
| **Android Chrome PWA** | ✅ | ✅ Web Push |
| **iOS PWA** | ✅ | ✅ Web Push — **only when installed to the Home Screen** (iOS 16.4+). A Safari tab gets nothing |
| **Android APK** | ✅ | ❌ **nothing** — see Phase 2 |

**iOS ceiling, permanently:** a notification, not a ringing screen. PushKit and CallKit are native-app
APIs; a PWA cannot get a full-screen incoming call, cannot ring through silent mode, and cannot
auto-answer. Tap-to-answer is as good as it gets, and saying so up front prevents "the call didn't
ring" being filed as a bug.

**Genuinely powered-off phone:** nothing arrives. APNs/FCM queue and deliver on reconnect, long after a
45-second ring gave up. Signal and WhatsApp have the same limit; the answer is a missed-call record
(Phase 4), not a delivery trick.

---

## Phase 0 — pin call signaling to the instance relay ✅ done

**The bug:** `_callSend` published with `Relay.publish(ev)`, which fans out to the relays *the user
happens to be connected to*. The push watcher subscribes to the **instance's own** relay. Drop our
relay from your list and every frame becomes invisible to the watcher — closed-app ringing silently
stops, with nothing in any log to say so, and because 25050 is ephemeral nothing syncs it afterwards.

**The fix** — `_callPublish()` in `app.js`:

```js
Relay.publish(ev);                       // the pool, as before
Relay.publishTo([CFG.relay_url], ev);    // + this instance, always
```

`publishTo` **skips relays already in the pool**, so in the normal case this costs nothing — no second
socket, no duplicate EVENT. It only opens a short-lived socket when our relay isn't in the user's list,
which is precisely the case that was broken. `CFG.relay_url` is empty on a standalone install (no
instance), where the whole push path is absent anyway, so it no-ops there.

Calls are an **instance feature**: they deliberately do not depend on which relays either party
configured, and there is no NIP-65 relay-list resolution involved.

**Also widened here:** the live subscription used `since: now-5`, now `now-45` (the caller's ring
window). The reason is **clock skew, not catch-up** — an ephemeral kind is never stored, so a socket
that was down when the invite was fanned out has missed it permanently and no `since` value recovers
it; that gap is what push exists for. But `since` *is* applied to live frames, so if the caller's
clock runs more than 5 seconds ahead of the callee's, a perfectly good invite was being filtered out
at the callee. 45s makes that robust without letting anything stale through — `_callSeen` dedups, and
a call that old is over anyway.

**Known limit, by design:** two users on *different* instances still connect while both apps are open,
but closed-app push only fires on the instance whose relay carried the invite. Cross-instance ringing
of a closed app is out of scope.

**Verify:** call a second account with the callee's app closed → the callee's server logs a push send.
Then remove the instance relay from the caller's relay list and repeat; it must still ring.

---

## Phase 1 — iOS PWA: fix the funnel, not the pipeline

Nothing to build in the delivery path. What's missing is that a user must do three things and is told
about none: install to the Home Screen, grant notification permission, enable push in settings. Miss
any one and it fails silently.

**Build:** one guided *"Turn on call notifications"* flow that detects which of the three is missing and
asks for exactly that. Extend `_maybeIosInstallHint` (`app.js:81`) rather than adding a second path.

Note which check that is: it reads `navigator.standalone`/display-mode — *"is this a Home Screen
install?"*. It is **not** `_standalone()` (`app.js:178` = `BUNDLED && !_instanceBase()`), which asks
*"is this a bundled app with no instance?"*. CLAUDE.md calls out conflating those two as a trap that
has already cost a release.

**Verify:** on a fresh iPhone, from a Safari tab, the flow walks to a working ring without the user
needing this document.

---

## Phase 2 — Android: a transport, and a battery-settings diagnostic

### 2a. Push transport

Today: none. Capacitor's WebView has no push service, `mobile/package.json` has no push plugin, and
`build.gradle` logs *"google-services.json not found… Push Notifications won't work"*.

**Recommended: UnifiedPush.** A UnifiedPush endpoint is a plain HTTPS POST URL, so server-side this is a
`transport` column on `PushSubscription` and a `send_unifiedpush()` sibling to `push_service.send()` —
the watcher, the cooldown and the payload are untouched. No Google, self-hostable via ntfy, and it
rides one OS-level socket shared by every app. Cost: the user installs a distributor app.

**FCM** is the alternative: better Doze wake behaviour and zero user setup, at the price of a Firebase
dependency in a project whose pitch is not having one. Ship UnifiedPush first; add FCM as an optional
build flavour if adoption demands it.

Then a **CallStyle full-screen-intent** notification for real ringing — declare
`USE_FULL_SCREEN_INTENT`, which Android 14+ grants to calling apps.

### 2b. Detect battery restrictions

Samsung's *Deep sleeping apps* force-stops an app: no background execution, **no notifications**, and a
force-stopped process cannot have its `AutofillService` bound until you open the app again. So one
toggle silently disables both closed-app ringing *and* password autofill — the two features users are
least likely to connect to a battery setting. It is the single most common bug report Bitwarden and
KeePassDX receive.

**Build:** check `ActivityManager.isBackgroundRestricted()` and
`PowerManager.isIgnoringBatteryOptimizations()`, plus whether we're still the selected autofill
provider. Surface a warning with a button that deep-links to the battery settings screen, in **both**
Passwords → This device and the notification settings.

Note the autofill service reads a Keystore-encrypted snapshot and never touches the network, so it
works on a plane — but nothing survives a force-stop.

---

## Phase 3 — battery and data discipline

Only relevant if a socket is held in the background. If push does the waking, the phone does nothing at
all until a call arrives, which is why push is the *cheap* design rather than the compromise: APNs, FCM
and a UnifiedPush distributor each maintain one connection shared by every app on the device.

If a foreground socket is ever added anyway:

1. **Per-connection ping cadence.** `ping_interval=30` is global today — phones, desktops and bots all
   get 30s. That's ~120 radio wakes per hour per relay, and the cost isn't the ping bytes, it's the
   modem never settling into idle. Let the client advertise a backgrounded cadence.
   **Floor:** carrier NAT drops idle TCP mappings in roughly 5–30 minutes, and exceeding it kills the
   socket *silently* — everything looks connected and calls vanish. Target ~5 minutes, measured, not 15.
2. **A backgrounded subscription profile:** kind-25050 `#p:me` and, once Phase 5 lands, the DM kinds.
   No home feed, no firehose, no negentropy sync — that's where the megabytes are.
3. **One relay while backgrounded** — `CFG.relay_url`, the one the watcher reads. N relays multiplies
   everything above by N.
4. **Suspend service-worker media prefetch** while hidden.
5. **Budget:** idle background data in KB/hour, checked with `dumpsys batterystats` and per-uid
   `TrafficStats`.

---

## Phase 5 — DMs push at all

Found while fact-checking this document, and arguably the biggest gap on the page: **a direct message
never notifies a closed phone.** `_KINDS` covers mentions, reposts, reactions, zaps, NIP-22 comments
and NIP-28 channel messages — but not kind 4, and not kind 1059. Someone messages you, your phone
says nothing, and you find out when you next open the app.

NIP-17 makes this harder than adding a number to a list, which is presumably why it wasn't:

- A **gift-wrapped 1059 is addressed to a throwaway pubkey**, not to yours, so the `#p`-tag filter the
  poller uses does not match it. You can subscribe by `#p` on the recipient's *own* key only for the
  outer wrap that targets them — check what this relay actually accepts and fans out for 1059.
- The server **cannot read the content** (that's the point), so the push body can only be *"New
  message"* with no sender name — unless the client re-writes the notification after decrypting, which
  the SW can do on `notificationclick` but not before display.
- Rumor timestamps are randomised, so cursor/dedup logic keyed on `created_at` needs care.

Minimum viable: push *"New message"* with no sender or content, let the client fill in detail once
opened. Better than silence, and it leaks nothing.

## Phase 6 — stop the phantom second ring on every missed call

Found in review. **Every unanswered call to a backgrounded phone rings twice**, and the second ring
arrives 45 seconds after the caller already gave up.

The watcher pushes on **any** kind-25050 it sees, because the content is encrypted and it cannot tell
an invite from a hangup. Walk the standard missed call:

| t | frame | what the callee's phone does |
|---|---|---|
| 0s | `invite` | 📞 Incoming call ✅ |
| 45s | `bye` — the caller's no-answer timer fires `_hangup(false)`, which sends one | 45s > the 30s cooldown, so: 📞 **Incoming call again** ❌ |

The second notification carries `requireInteraction: true`, so it sits on the lock screen until
dismissed, and tapping it opens an app with no call in it.

**Fix:** put an unencrypted marker on the invite only — `['t','invite']` — and have `_call_handler`
push solely for that. It leaks nothing the relay can't already infer from kind 25050 plus the `p` tag,
and it cuts push volume hard, since today every ICE candidate is a push candidate too.

**Deploy order matters:** old clients send no marker. Push on *(marker present AND `t=invite`)* OR
*(no marker at all)* during the transition, then tighten once clients have rolled over — otherwise an
old caller silently stops ringing new callees.

## Phase 4 — missed calls

kind-25050 is ephemeral, so a phone that was off leaves no trace of the call at all. Record a missed
call so the user sees it on next open. Cheap, and it's the difference between "the app is broken" and
"I was offline".

---

## Sequencing

**0 → 6 → 1 → 5 → 3 → 2 → 4.** Phase 0 unblocks testing anything else. Phase 6 jumps the queue because
it is a bug users hit on every missed call, and it's small. Phase 1 is nearly free and gets iOS working
end to end. Phase 5 next, because "my messages don't notify" affects every user every day while
closed-app ringing affects them occasionally. Phase 3 before 2, so the APK is built against a
background profile that is already cheap instead of being retrofitted onto an expensive one.

Code: `app/services/nostr_push_service.py` (the watcher + cooldown), `app/services/push_service.py`
(VAPID), `static/js/client/sw.js` (the `push` handler), the call block in `static/js/client/app.js`.
[PASSWORDS.md](PASSWORDS.md) covers the Android autofill service that Phase 2b also protects.

"""A call has to survive leaving the app, and the app has to come back connected.

Run: venv-unified/bin/python -m pytest tests/test_android_call_service.py

Reported as: "can you make sure the apks are working in the background? when I open the window back
up, says reconnecting. I want to make sure calls background services are working".

THE PLATFORM RULE THIS IS ABOUT. Since Android 11 an app in the BACKGROUND may not capture the
microphone or the camera at all, unless a foreground service of the matching type is running. There
was no such service, so pressing Home mid-call silenced the microphone instantly: the other party
heard nothing, the in-app UI stayed perfect, and nothing was logged anywhere. A foreground service is
also what keeps the process off the cached-process freezer, which is what lets the WebRTC connection
survive a locked screen — the same thing MusicService does for playback.

None of it can be driven here (no device; Gradle runs on CI), so what is guarded is the WIRING, and
every assertion is a way for the feature to vanish silently:

  * the plugin is not registered, or the two sides disagree about its name — `Capacitor.Plugins
    .CallControls` is then simply absent and the client's guarded lookup falls through to "browser",
    which is the pre-fix behaviour exactly;
  * the foregroundServiceType or its Android-14 permission goes missing — the start then throws and
    the call keeps running with no background microphone;
  * a PendingIntent without FLAG_IMMUTABLE, which on Android 12+ throws when the notification builds;
  * the service starts but is never stopped, leaving an ongoing "call in progress" notification for a
    call that ended;
  * it is started on a hand-picked transition rather than on the repaint path, so one of the six
    places a call's state moves misses it — a dead microphone with nothing to see.

…and the BATTERY half, which is the other way this goes wrong: the service must hold no wake lock,
and the resume path must not tear down and reopen every relay socket because somebody glanced at the
notification shade.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


MANIFEST = _read(ANDROID, "src", "main", "AndroidManifest.xml")
MAIN = _read(JAVA, "MainActivity.java")
SERVICE = _read(JAVA, "call", "CallService.java")
PLUGIN = _read(JAVA, "call", "CallPlugin.java")
APPJS = _read(ROOT, "static", "js", "client", "app.js")


def test_plugin_is_registered_and_named_the_same_on_both_sides():
    """A plugin that lives in this app is NOT auto-discovered. Unregistered, the JS lookup returns
    nothing and the client behaves exactly as it did before the fix — no error, no service."""
    assert "registerPlugin(place.poster.app.call.CallPlugin.class);" in MAIN
    assert 'name = "CallControls"' in PLUGIN
    assert "_capPlugin('CallControls')" in APPJS, "the JS asks for a different plugin name"


def test_the_service_declares_both_capture_types_and_their_permissions():
    """Android 14+ refuses to START a service whose declared type it has no permission for, and the
    Android 11 capture rule needs the type to match what is actually being captured."""
    assert re.search(r'android:name="\.call\.CallService"', MANIFEST), "the service is not declared"
    assert re.search(r'android:foregroundServiceType="microphone\|camera"', MANIFEST)
    assert "android.permission.FOREGROUND_SERVICE_MICROPHONE" in MANIFEST
    assert "android.permission.FOREGROUND_SERVICE_CAMERA" in MANIFEST, (
        "a video call declares the camera type; without the permission the start throws")
    assert "FOREGROUND_SERVICE_TYPE_MICROPHONE" in SERVICE
    assert "FOREGROUND_SERVICE_TYPE_CAMERA" in SERVICE


def test_the_camera_type_is_only_asked_for_when_there_is_video():
    """Declaring capture you are not doing is what gets an app flagged, and on some builds refused."""
    assert re.search(r"if \(video\) types \|= ServiceInfo\.FOREGROUND_SERVICE_TYPE_CAMERA", SERVICE)


def test_every_pending_intent_is_immutable():
    """On Android 12+ a mutable PendingIntent is an IllegalArgumentException the moment the
    notification is built — so the service starts and immediately dies."""
    # The flags are computed once into `f` and passed to both, so a window-scan for the constant
    # would report a false failure. Check what is actually true: `f` carries FLAG_IMMUTABLE, and
    # every PendingIntent is built with `f`.
    assert re.search(r"int f = PendingIntent\.FLAG_UPDATE_CURRENT[\s\S]{0,160}FLAG_IMMUTABLE",
                     SERVICE), "the shared PendingIntent flags no longer include FLAG_IMMUTABLE"
    calls = re.findall(r"PendingIntent\.get(?:Activity|Service|Broadcast)\(([\s\S]{0,200}?)\);",
                       SERVICE)
    assert calls, "no PendingIntent found — has the notification lost its actions?"
    for args in calls:
        assert re.search(r",\s*f\s*$", args.strip()), (
            f"a PendingIntent does not use the shared immutable flags: {' '.join(args.split())}")


def test_hanging_up_from_the_notification_goes_back_to_javascript():
    """JS owns the call — the `bye` to the peer, the tracks, the PeerConnection. Tearing anything
    down natively would drop the notification while the call carried on inside the WebView."""
    i = SERVICE.index("ACTION_HANGUP.equals(action)")
    body = SERVICE[i:i + 500]
    assert 'listener.onCallAction("hangup")' in body
    assert "stopSelf()" not in body, "the service ends itself instead of letting JS end the call"
    assert "notifyListeners(\"callAction\"" in PLUGIN
    assert "a.action!=='hangup'" in APPJS, "the client does not act on the notification's Hang up"


def test_a_group_call_hangs_up_as_a_group_call():
    """`_hangup` on a room would send `bye` to a peer that is not there and leave the mesh up."""
    i = APPJS.index("a.action!=='hangup'")
    assert "_roomLeave()" in APPJS[i:i + 200]


def test_the_service_is_started_from_the_repaint_path_not_a_hand_picked_moment():
    """There are six places a call's state can move. A missed start is a dead microphone with nothing
    on screen to say so, so it is driven from the one function that always runs."""
    for fn in ("function _callUI(){", "function _roomUI(){"):
        i = APPJS.index(fn)
        assert "_callService(" in APPJS[i:i + 700], f"{fn} does not drive the call service"


def test_the_service_is_always_stopped():
    """An ongoing 'call in progress' notification for a call that ended is the thing users go hunting
    through settings for."""
    for fn in ("function _callTeardown(){", "function _roomLeave(){"):
        i = APPJS.index(fn)
        assert "_callService(false)" in APPJS[i:i + 600], f"{fn} leaves the service running"
    assert "STOP_FOREGROUND_REMOVE" in SERVICE
    assert "START_NOT_STICKY" in SERVICE, (
        "a restarted service would show a call notification whose Hang up reaches nothing")


# ---------------------------------------------------------------------------------------------
# battery and CPU


def test_the_service_holds_no_wake_lock():
    """The foreground status is what stops the process being FROZEN. A PARTIAL_WAKE_LOCK on top would
    additionally stop the CPU idling between audio packets — battery spent to change nothing."""
    assert "WakeLock" not in SERVICE and "PowerManager" not in SERVICE


def test_the_service_does_no_polling_of_its_own():
    """It holds no media and no state beyond what to draw; everything it shows is pushed."""
    for bad in ("Timer(", "scheduleAtFixedRate", "postDelayed", "Handler("):
        assert bad not in SERVICE, f"the call service runs its own {bad} loop"


def test_repeated_updates_cost_a_string_compare_not_an_intent():
    """It is called from the repaint path (deliberately — see above), so without this a call would
    fire an Intent per ICE state change, per mute, per frame of UI."""
    i = APPJS.index("function _callService(")
    body = APPJS[i:i + 1400]
    assert "if(sig === _callSvcSig) return;" in body, "every repaint sends an Intent to the service"
    assert "if(!_callSvcSig) return;" in body, (
        "stop() is sent even when the service was never started — an Intent per call teardown in a "
        "browser that has no service at all")


def test_an_already_running_service_is_refreshed_not_restarted(app=None):
    """startForegroundService from the background throws on Android 12+, and an update lands there
    routinely (the peer answers after you pressed Home). Only the FIRST start is an Intent."""
    i = PLUGIN.index("public void start(")
    body = PLUGIN[i:i + 900]
    assert "CallService.INSTANCE" in body and "live.refresh(" in body
    assert body.index("live.refresh(") < body.index("startForegroundService"), (
        "the plugin starts a service that is already running instead of refreshing it")


def test_a_glance_at_the_shade_does_not_reopen_every_socket():
    """`wake()` tears down and reopens every relay connection — on a five-relay pool that is five TLS
    handshakes and five re-subscriptions, which is real radio time. An app-switch loop must not pay
    that repeatedly."""
    i = APPJS.index("function _nativeResume(){")
    assert "_hiddenAt > 6000" in APPJS[i:i + 200], "the native resume reconnects unconditionally"
    assert re.search(r"else _hiddenAt = Date\.now\(\);", APPJS), (
        "nothing records when the app was BACKGROUNDED natively, so the gate above reads a number "
        "only the unreliable signal ever wrote")


def test_the_resume_helper_is_reachable_from_both_places_that_use_it():
    """It was a `const` inside bindGlobalsOnce and called from startApp's native listeners — a
    ReferenceError at the exact moment the app comes back, i.e. never on a desktop and always on a
    phone."""
    i = APPJS.index("function _resumeRelay(){")
    assert i < APPJS.index("  function startApp(){")
    assert i < APPJS.index("  function bindGlobalsOnce(){")
    assert "const _resumeRelay" not in APPJS, "the helper is function-scoped again"


def test_every_resume_signal_is_wired_and_they_share_one_debounce():
    """visibilitychange is the least reliable of the four on a frozen Android process, and it was the
    only one the relay reconnect listened to."""
    for sig in ("addListener('resume'", "addListener('appStateChange'",
                "addEventListener('online', _resumeRelay)", "pageshow"):
        assert sig in APPJS, f"the {sig} resume signal is not wired"
    i = APPJS.index("function _resumeRelay(){")
    assert "_lastWake < 4000" in APPJS[i:i + 300], "the four signals would fire four reconnects"


# ---------------------------------------------------------------------------------------------
# the rest of "working in the background" — it was never only about calls


NOTES = _read(ROOT, "static", "js", "client", "notes.js")
VAULT = _read(ROOT, "static", "js", "client", "vault.js")


def test_notes_and_the_vault_drain_when_the_relay_comes_back():
    """A note or a password written offline is SIGNED, ENCRYPTED and queued — and then has to be
    published by something.

    Both modules exposed a `flush` for exactly this moment and nothing ever called it; they were left
    holding two weaker triggers. Neither survives what a phone does. `online` fires when the RADIO
    changes, and returning to a frozen app is not a radio change — the network never went away, the
    process did — so it simply never fires. The 45-second interval is worse: a frozen process runs no
    timers and a killed one runs none until 45s after it is next opened. So the write stayed on the
    device, correctly saved and correctly queued, until something unrelated jogged it.
    """
    assert "flush: flushPending" in NOTES and "flush: flushPending" in VAULT, (
        "a module stopped exposing its queue drain")
    i = APPJS.index("if(s === 'ok'){")
    assert "_flushPrivateQueues()" in APPJS[i:i + 200], (
        "the relay reaching 'ok' — the one signal that fires on a cold start, a reconnect AND a "
        "resume — does not drain the notes/vault queues")
    j = APPJS.index("function _flushPrivateQueues(){")
    body = APPJS[j:j + 400]
    for mod in ("PCNotes", "PCVault"):
        assert mod in body, f"{mod}'s queue is never drained on reconnect"


def test_draining_twice_at_once_cannot_lose_a_queued_write():
    """Each flush reads the queue, awaits a publish per item, then writes the survivors back — so two
    overlapping runs let the second's write clobber the first's. Harmless while the only triggers
    were a radio event and a 45s timer; not once every relay reconnect drains it, and a flaky link
    reconnects several times a minute."""
    for name, src in (("notes.js", NOTES), ("vault.js", VAULT)):
        i = src.index("async function flushPending(){")
        read = src.index("const list = pending();", i)
        # The CHECK has to come before the first read of the queue, or two runs both see the same
        # list and the second's write-back clobbers the first's.
        assert "if(_flushing) return 0;" in src[i:read], f"{name}'s flush is re-entrant"
        # The SET may come after the empty-queue early return — a no-op flush should not take a lock.
        body = src[i:src.index("\n  }", read)]
        assert "_flushing = true;" in body, f"{name} checks a flag it never sets"
        assert "finally{ _flushing = false; }" in body, f"{name} can wedge its flush after a throw"

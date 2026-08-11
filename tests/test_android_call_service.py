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
    # The native PAUSE branch has to record the timestamp — the gate above reads a number that was
    # otherwise only ever written by the least reliable signal. (It also drops the timeline there; see
    # tests/test_timeline_background_pause.py.)
    # Sliced to the END of the listener rather than a guessed number of characters: a fixed window
    # goes red when the code around it grows, which is what it did twice.
    i = APPJS.index("addListener('appStateChange'")
    pause = APPJS[i:APPJS.index("}); }catch(_){} }", i)]
    assert re.search(r"else \{[\s\S]*_hiddenAt = Date\.now\(\);", pause), (
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


# ---------------------------------------------------------------------------------------------
# reaching a CLOSED app: the notification half


PUSHSVC = _read(JAVA, "push", "PushEventService.java")
PUSHPLUG = _read(JAVA, "push", "PushPlugin.java")
STAY = _read(JAVA, "push", "StayAwakeService.java")


def test_the_apk_can_raise_a_notification_at_all():
    """Android's WebView does not implement the Notifications API. `new Notification(...)` in there is
    not an error and not a refusal — it is SILENCE, so the client's one notification helper drew
    nothing on the packaged app: a DM that had already arrived on the open relay socket produced an
    in-app toast if you happened to be looking and nothing whatsoever if you were not. Same shape as
    the media-controls gap (an API the WebView accepts and does nothing with)."""
    i = APPJS.index("function osNotify(")
    body = APPJS[i:i + 1600]
    assert "_capPlugin('PosterChanPush', 'notify')" in body, (
        "osNotify still relies on window.Notification, which does nothing in a WebView")
    assert body.index("_capPlugin('PosterChanPush'") < body.index("window.Notification"), (
        "the browser path is tried first, so the APK never reaches the native one")
    assert "public void notify(PluginCall call)" in PUSHPLUG


def test_a_push_and_a_locally_raised_notification_use_one_builder():
    """Otherwise the same event looks different depending on whether the app happened to be running —
    and the call treatment (its own channel, a full-screen intent) is the part that would differ."""
    assert "public static void show(Context ctx" in PUSHSVC
    assert "show(ctx, title, body, type, null);" in PUSHSVC, (
        "onMessage builds its own notification again instead of going through show()")
    assert "PushEventService.show(getContext()" in PUSHPLUG


def test_stay_connected_is_off_by_default_and_says_what_it_costs():
    """It is a FALLBACK for phones with no push distributor, not the plan — push reaches a closed app
    for free. A permanent connection is real battery, so it is a trade the user makes knowingly."""
    assert "getBoolean(PREF_ON, false)" in STAY, "stay-connected defaults to ON"
    i = APPJS.index('id="set-stay-row"')
    row = APPJS[i:i + 900]
    assert "hidden" in row, "the switch is shown on builds that cannot do it"
    assert "more battery" in row, "the switch does not say what it costs"


def test_stay_connected_declares_specialUse_not_dataSync():
    """Android 15 caps dataSync at six hours in any twenty-four — for a stay-connected service that
    means it silently stops working for most of the day, which is worse than not having it."""
    assert re.search(r'android:name="\.push\.StayAwakeService"', MANIFEST)
    assert 'android:foregroundServiceType="specialUse"' in MANIFEST
    assert "PROPERTY_SPECIAL_USE_FGS_SUBTYPE" in MANIFEST, (
        "Android 14+ requires the subtype property beside a specialUse service")
    assert "android.permission.FOREGROUND_SERVICE_SPECIAL_USE" in MANIFEST
    assert 'foregroundServiceType="dataSync"' not in MANIFEST, (
        "a service was moved to dataSync, which Android 15 caps at 6h/day")


def test_the_switch_shows_the_remembered_choice_not_the_running_state():
    """Android may kill the service. A switch that flips itself off because of that tells the user
    they turned something off, which they did not."""
    i = PUSHPLUG.index("public void stayConnected(")
    body = PUSHPLUG[i:i + 500]
    assert 'out.put("on", StayAwakeService.wanted(' in body, (
        "the switch reads the live service instead of the stored preference")
    assert "START_STICKY" in STAY, (
        "the service does not come back after being killed, which is the whole point of asking for it")


def test_a_refused_start_puts_the_switch_back():
    """It must never show a state the phone is not in."""
    i = APPJS.index("async function _wireStayConnected(")
    body = APPJS[i:i + 1400]
    assert "box.checked = !want;" in body, "a rejected change leaves the switch lying"
    assert "P.setStayConnected(" in body


# ---------------------------------------------------------------------------------------------
# what "working in the background" means for everything else


SYNCJS = _read(ROOT, "static", "js", "client", "sync.js")


def test_folder_sync_may_run_on_a_phone_that_was_asked_to_stay_alive():
    """"make sure Folder Syncing works in background mode too, if conditions are met, like plugged
    in, on, wifi".

    The CONDITIONS were already there — RUN.due wants charging, an unmetered link and a battery that
    is not low, and the WorkManager job carries the same three. What refused every background sweep
    on Android was `document.hidden`: the phone is hidden the moment you leave the app, so sync could
    only ever run with it on screen.

    "Stay connected" is an explicit opt-in with a permanent notification saying it costs battery, so
    an app running under it is not one nobody is looking at — it is one somebody asked to keep
    working. This changes WHO MAY ASK, not what is allowed."""
    assert "const _idle = () => document.hidden && !window.pcShell && !_keptAlive;" in SYNCJS, (
        "a kept-alive phone is still treated as idle, so it never syncs in the background")
    assert "stayConnected" in SYNCJS
    i = SYNCJS.index("appStateChange")
    assert "_readKeptAlive()" in SYNCJS[i:i + 400], (
        "the switch can move while the app is away and would never be re-read")


def test_a_covered_desktop_window_still_updates_torrent_progress():
    """Chromium reports `hidden` for a window merely COVERED by another one (native occlusion), so
    putting any other window in front froze the progress bars — "torrents on desktop, not updating
    progress if not focused". The same trap already had to be worked around for the timeline."""
    i = APPJS.index("function _torStartPoll()")
    body = APPJS[i:i + 1400]
    assert "document.visibilityState==='hidden' && !_isDesktopApp()" in body, (
        "a covered desktop window stops polling")
    assert "document.visibilityState==='hidden') return;" not in body, (
        "the unguarded check is back")


def test_the_mail_poll_has_the_same_exemption():
    """It is the same trap in the same shape: a mail check that stops when another window is in front
    is a mail check that stops whenever you are working."""
    i = APPJS.index("startPolling(){")
    body = APPJS[i:i + 900]
    assert "document.visibilityState === 'hidden' && !_isDesktopApp()" in body


BOOT = _read(JAVA, "push", "BootReceiver.java")


def test_start_at_boot_restarts_the_service_and_not_the_app():
    """"PosterChan should have a start at boot option for android like apps do too" — and what apps
    actually do is restart the part that was RUNNING, not put themselves on screen. Android has
    refused to allow the latter from the background for years, and it is what people uninstall an app
    for."""
    assert "android.permission.RECEIVE_BOOT_COMPLETED" in MANIFEST
    assert re.search(r'android:name="\.push\.BootReceiver"', MANIFEST)
    assert "android.intent.action.BOOT_COMPLETED" in MANIFEST
    assert "MainActivity" not in BOOT, "the receiver launches the app at boot"
    assert "StayAwakeService.wanted(ctx)" in BOOT, (
        "it starts the service whether or not the user asked for it")
    i = BOOT.index("StayAwakeService.wanted(ctx)")
    assert "return;" in BOOT[i:i + 60], "an opted-out install still starts a service at boot"


def test_it_also_survives_an_app_update():
    """MY_PACKAGE_REPLACED: an update stops every service the app was running, and without this the
    switch stays on while nothing is running behind it until the app is next opened."""
    assert "MY_PACKAGE_REPLACED" in MANIFEST and "MY_PACKAGE_REPLACED" in BOOT


def test_the_home_screen_calendar_is_redrawn_after_a_reboot():
    """The data survives (it is on disk) but the LAUNCHER rebuilds its widgets from scratch — so the
    first thing seen after a restart would be a widget that has not decided what day it is."""
    assert "CalendarWidget.refresh(ctx)" in BOOT


def test_stay_connected_comes_back_after_a_force_stop():
    """"persistent notification not working if you force close app and reopen, no notification".

    Force-stopping is its own case: it kills the service AND puts the app into Android's "stopped"
    state, where it receives no broadcasts at all until launched by hand — so BootReceiver never
    fires, and START_STICKY does not apply either (the system takes a force-stop to mean the user
    wanted it stopped). The only moment left is the next time the app is opened, which is the
    plugin's load(). Without it the switch reads "on" while nothing runs behind it: no notifications,
    and a setting insisting there should be."""
    assert "public void load()" in PUSHPLUG, "nothing restores the service when the app is opened"
    i = PUSHPLUG.index("public void load()")
    body = PUSHPLUG[i:i + 900]
    assert "StayAwakeService.wanted(getContext())" in body, "it starts whether or not it was asked for"
    assert "StayAwakeService.running" in body, (
        "it would restart the service on every ordinary resume as well")
    assert "startForegroundService" in body

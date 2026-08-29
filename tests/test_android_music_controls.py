"""The APK's media controls: lock screen, shade, headset/car, home-screen widget.

None of this can be driven here — there is no device, and the Gradle build runs on CI — so what is
guarded is the WIRING, which is where this feature fails SILENTLY. Every assertion below is a way for
the controls to disappear with nothing in any log to say so:

  * the plugin is not registered in MainActivity — a plugin that lives in this app is not
    auto-discovered, so `Capacitor.Plugins.MusicControls` is simply absent and the client's guarded
    lookup falls through to "no native controls", exactly as it does in a browser.
  * the JS and the Java disagree on the plugin's NAME, which fails the same way.
  * FOREGROUND_SERVICE_MEDIA_PLAYBACK or the service's foregroundServiceType goes missing — Android
    14+ then refuses to start the service at all, so there is no session and no notification.
  * a PendingIntent without FLAG_IMMUTABLE, which on Android 12+ is an IllegalArgumentException the
    moment the notification is built.
  * the native push gets moved back under the `'mediaSession' in navigator` guard. That guard is
    about the BROWSER API — and the whole reason this feature exists is that the WebView has that API
    and does nothing with it.
"""
from pathlib import Path
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


MANIFEST = _read(ANDROID, "src", "main", "AndroidManifest.xml")
GRADLE = _read(ANDROID, "build.gradle")
MAIN = _read(JAVA, "MainActivity.java")
SERVICE = _read(JAVA, "music", "MusicService.java")
PLUGIN = _read(JAVA, "music", "MusicPlugin.java")
WIDGET = _read(JAVA, "music", "MusicWidget.java")
APPJS = _read(ROOT, "static", "js", "client", "app.js")


def test_plugin_is_registered_and_named_the_same_on_both_sides():
    assert "registerPlugin(place.poster.app.music.MusicPlugin.class)" in MAIN
    name = re.search(r'@CapacitorPlugin\(\s*name\s*=\s*"([^"]+)"', PLUGIN)
    assert name, "MusicPlugin lost its @CapacitorPlugin name"
    assert name.group(1) == "MusicControls"
    # …and that is the name the client asks for. A rename on one side only is invisible: the lookup
    # is guarded (it must be — the same code runs in the PWA), so it degrades to no controls.
    assert "_capPlugin('MusicControls','update')" in APPJS
    assert "_capPlugin('MusicControls','addListener')" in APPJS
    assert "_capPlugin('MusicControls','stop')" in APPJS


def test_initial_audio_waits_for_foreground_media_session_before_playing():
    """A fast HOME must not beat the first foreground-service update after audio starts."""
    play = APPJS[APPJS.index("async play(sha, opts)"):]
    play = play[:play.index("toggle(){")]
    prime = play.index("await Promise.race([this._nativePush()")
    audible = play.index("await _audioEl.play()", prime)
    assert prime < audible
    push = APPJS[APPJS.index("_nativePush(){"):APPJS.index("consumeLaunch(){")]
    assert "return r.then(" in push
    assert "return Promise.resolve(false)" in push
    assert "setTimeout(()=>resolve(false),600)" in play


def test_foreground_service_is_declared_the_way_android_14_requires():
    assert "android.permission.FOREGROUND_SERVICE_MEDIA_PLAYBACK" in MANIFEST
    svc = re.search(r"<service\b[^>]*\.music\.MusicService[^>]*>", MANIFEST, re.S)
    assert svc, "MusicService is not declared in the manifest"
    assert 'android:foregroundServiceType="mediaPlayback"' in svc.group(0)
    assert 'android:exported="false"' in svc.group(0)
    # The type must also be claimed at runtime, or the service runs typeless and Android 14 kills it.
    assert "FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK" in SERVICE
    # …and going foreground must happen on the FIRST update: a service started with
    # startForegroundService has ~5s to call startForeground or the whole app is killed.
    assert "startForeground" in SERVICE
    assert "startForegroundService" in PLUGIN


def test_widget_is_declared_with_its_provider_metadata():
    recv = re.search(r"<receiver\b[^>]*\.music\.MusicWidget[^>]*>.*?</receiver>", MANIFEST, re.S)
    assert recv, "MusicWidget is not declared — the widget never appears in the picker"
    body = recv.group(0)
    assert "android.appwidget.action.APPWIDGET_UPDATE" in body
    assert 'android:name="android.appwidget.provider"' in body
    assert "@xml/music_widget_info" in body
    info = _read(ANDROID, "src", "main", "res", "xml", "music_widget_info.xml")
    assert "@layout/widget_music" in info
    # Pushed by the service, never polled — a period here would be clamped to 30 minutes and redraw
    # what is already on screen.
    assert 'android:updatePeriodMillis="0"' in info


def test_media_button_receiver_and_the_compat_dependency_are_present():
    # MediaSessionCompat resolves this receiver BY NAME at construction; without it the session is
    # built but hardware/headset buttons route nowhere.
    assert "androidx.media.session.MediaButtonReceiver" in MANIFEST
    assert "android.intent.action.MEDIA_BUTTON" in MANIFEST
    assert re.search(r'implementation\s+"androidx\.media:media:', GRADLE), \
        "androidx.media is what provides MediaSessionCompat + MediaStyle"


def test_every_pending_intent_is_immutable():
    """Android 12+ throws when a PendingIntent declares neither mutability — at BUILD time of the
    notification, i.e. the controls die on the first track rather than misbehaving later."""
    for src, where in ((SERVICE, "MusicService"), (WIDGET, "MusicWidget")):
        for call in re.findall(r"PendingIntent\.get\w+\((?:[^()]|\([^()]*\))*\)", src, re.S):
            assert "FLAG_IMMUTABLE" in call, f"{where}: PendingIntent without FLAG_IMMUTABLE: {call}"


def test_the_native_push_is_not_gated_on_the_browser_api():
    """`if(!('mediaSession' in navigator)) return;` is about the BROWSER's media session. The WebView
    HAS that API and publishes nothing with it — which is the entire reason for the native half — so
    a native push placed after that guard would work in Chrome and do nothing in the app."""
    body = re.search(r"_media\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert body, "MusicPlayer._media() moved — re-point this test"
    body = body.group(1)
    push = body.index("this._nativePush()")
    guard = body.index("'mediaSession' in navigator")
    assert push < guard, "the native push must run before the browser-API guard"


def test_the_native_push_waits_for_something_to_actually_play():
    """update() raises a foreground service. Called with no current track it would put a notification
    on screen for a player that has never played — and a background foreground-service start is the
    one Android is most likely to refuse outright."""
    push = re.search(r"_nativePush\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert push, "MusicPlayer._nativePush() moved — re-point this test"
    assert re.search(r"if\(!this\.cur\b[^)]*\)\s*return(?:\s+Promise\.resolve\(false\))?;", push.group(1))


def test_closing_the_player_takes_the_notification_with_it():
    close = re.search(r"\n    close\(\)\{(.*?)\},\n", APPJS, re.S)
    assert close and "MusicControls" in close.group(1), \
        "close() must stop the native controls, or the shade keeps a transport for nothing"


def test_transport_events_cover_every_button_we_publish():
    """A button the OS is told about but that JS does not handle is a DEAD button — the same class of
    bug as an unregistered mediaSession handler, and just as invisible from the app."""
    for action in ("play", "pause", "next", "prev", "stop", "seekTo", "seekBy"):
        assert f"emit(\"{action}\"" in SERVICE or f'"{action}"' in SERVICE, \
            f"MusicService never emits {action}"
        assert f"a==='{action}'" in APPJS, f"the client ignores the {action} transport event"


def test_a_dismissal_travels_back_but_a_plugin_stop_does_not():
    """The two ends of a stop must stay separate, or they chase each other: JS closes the player →
    the service echoes `stop` → JS closes again → …  A dismissal starts at the notification and has
    to reach the player; a plugin stop arrives BECAUSE the player already stopped."""
    dismiss = re.search(r"ACTION_DISMISS\.equals\(action\)\)\s*\{([^}]*)\}", SERVICE)
    stop = re.search(r"[^_]ACTION_STOP\.equals\(action\)\)\s*\{([^}]*)\}", SERVICE)
    assert dismiss and 'emit("stop"' in dismiss.group(1), "a swiped-away notification must tell the player"
    assert stop and 'emit(' not in stop.group(1), "a plugin stop must NOT be echoed back to the player"
    assert "setDeleteIntent(command(ACTION_DISMISS))" in SERVICE


def test_closing_the_player_cannot_be_undone_by_its_own_pause_event():
    """close() pauses the audio, and the 'pause' event lands a moment LATER — its state push would
    raise the notification the close just took down. The flag is what makes the close stick."""
    assert "_nativeOff=true" in APPJS and "this._nativeOff" in APPJS
    push = re.search(r"_nativePush\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert push and "this._nativeOff" in push.group(1)
    play = APPJS[APPJS.index("async play(sha, opts){"):][:400]
    assert "_nativeOff=false" in play, "play() is what brings the controls back"


def test_a_swiped_away_app_does_not_leave_a_dead_notification():
    """The audio lives in the WebView, so removing the task destroys it — the notification must go
    too, or the shade keeps a media card whose buttons control nothing."""
    assert "onTaskRemoved" in SERVICE


def test_device_home_probe_requires_background_audio_and_visible_launcher_together():
    device = (Path(ROOT) / "mobile/android/app/src/androidTest/java/place/poster/app/music/"
                     "MusicBackgroundDeviceTest.java").read_text()
    assert "after > before + 0.7" in device
    assert "scenario.getState() == Lifecycle.State.CREATED" in device
    assert 'assertTrue("the emulator did not assign PosterChan the HOME role"' in device
    assert "if (HomeRoles.isDefaultHome(ctx))" not in device
    assert "LauncherState.atHome()" in device


def test_no_two_methods_in_a_file_share_a_signature():
    """THE ONE THAT ALREADY SHIPPED PAST EVERY OTHER TEST HERE.

    A receipt-checked press was added as `private void command(String)` — beside the pre-existing
    `private PendingIntent command(String)` that builds the notification's button intents. Java does
    not count the return type as part of a signature, so that is a duplicate method and the whole
    module stops compiling: the CI APK never builds, while `sync.sh` ships the client half regardless
    and leaves a JS side calling a native side that is not on any phone. Every other test in this
    file is a regex over the source text and all 24 passed against it in 0.06s.

    So this one parses the declarations. Not a compiler — there is no Android SDK here — but the
    duplicate-signature class of error does not need one, and it is exactly the class that a
    text-matching suite is blind to."""
    for src, name in ((SERVICE, "MusicService"), (PLUGIN, "MusicPlugin"), (WIDGET, "MusicWidget")):
        body = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S))
        seen = {}
        decl = re.compile(
            r"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|native)\s+)+"
            r"[\w.<>\[\], ?]+\s+(\w+)\s*\(([^)]*)\)\s*\{", re.M)
        for m in decl.finditer(body):
            meth, params = m.group(1), m.group(2)
            if meth in ("if", "for", "while", "switch", "catch", "synchronized", "new"):
                continue
            # Erase to the parameter TYPES — names and `final` are not part of a signature.
            types = tuple(
                re.sub(r"\bfinal\b", "", p).strip().rsplit(" ", 1)[0].strip()
                for p in params.split(",") if p.strip())
            key = (meth, types)
            assert key not in seen, (
                f"{name}: two methods share the signature {meth}{types} — Java rejects this and the "
                f"APK will not build (return type is not part of a signature)")
            seen[key] = True


# ── The press that lands in nobody: "after a while in the car the controls stop working" ──────────
#
# The service outlives the page. Android kills the WebView's render process under memory pressure
# (MainActivity.recreate → a fresh page with an empty player) and destroys backgrounded Activities
# outright, while this foreground service keeps the session and the notification exactly as they
# were. emit() succeeds either way — it is a call into a listener that is still registered — so from
# every side the press REPORTS SUCCESS and no sound comes out. There is no device on this side of the
# work, so what is guarded is the measurement.


def test_a_press_that_should_make_sound_is_checked_for_a_receipt():
    """The client answers every transport event with a state push, synchronously. A press with no
    push behind it was not performed, and that is the only evidence available from the service."""
    cb = re.search(r"session\.setCallback\((.*?)\n    \}\);", SERVICE, re.S)
    assert cb, "the media session callback moved — re-point this test"
    cb = cb.group(1)
    for method in ("onPlay", "onSkipToNext", "onSkipToPrevious"):
        line = re.search(r"public void %s\(\) \{([^}]*)\}" % method, cb)
        assert line and "press(" in line.group(1), \
            f"{method} must go through press() — a car button is where nobody can see it fail"
    # PAUSE gets the check too, and it is not optional: `playing` is only ever written by the client,
    # so a WebView that vanished mid-track leaves the service believing a track plays FOREVER — and
    # the notification's one transport button is drawn from that belief, so it stays a ⏸ whose every
    # press takes the same dead branch. What pause must NOT do is wake the app: hush(), not press().
    pause = re.search(r"public void onPause\(\) \{([^}]*)\}", cb)
    assert pause and "hush(" in pause.group(1), "onPause must be receipt-checked"
    hush = re.search(r"private void hush\(final String action\) \{(.*?)\n  \}", SERVICE, re.S)
    assert hush and "revive(" not in hush.group(1), \
        "a pause must never open the app — that is a car stereo launching an app to stop silence"
    assert "markGone()" in hush.group(1), "…but it must clear `playing`, or the toggle stays stuck"
    # …and the pause that follows headphones being pulled must NOT be checked at all: an unanswered
    # one means the audio is already gone with the WebView, so there is nothing to heal.
    noisy = re.search(r"BroadcastReceiver noisy = new BroadcastReceiver\(\) \{(.*?)\n  \};", SERVICE, re.S)
    assert noisy and "press(" not in noisy.group(1) and "hush(" not in noisy.group(1)
    # The receipt itself: apply() is the push, and it is what stamps the clock.
    apply_body = re.search(r"public void apply\((.*?)\n  \}", SERVICE, re.S)
    assert apply_body and "lastWebAt = SystemClock.elapsedRealtime()" in apply_body.group(1)


def test_the_transport_listener_is_armed_at_startup_not_at_first_play():
    """THE window this whole family of bugs lives in. _nativeInit only ever ran from
    MusicPlayer.ensure() — the first time THIS page played something — so a page that came back after
    a renderer death had nothing subscribed to `musicTransport` while the notification it belongs to
    was still on screen. Every press did nothing until the app was opened and something played."""
    assert "MusicPlayer._nativeInit();" in APPJS, \
        "the transport listener must be armed on startup, not only when this page first plays"
    startup = APPJS[APPJS.index("if(window.Capacitor){"):]
    assert startup.index("MusicPlayer._nativeInit();") < startup.index("MusicPlayer.consumeLaunch();")


def test_a_destroyed_bridge_cannot_keep_receiving_transport_presses():
    """MusicService outlives an Activity. A static listener aimed at that Activity's destroyed
    bridge makes emit() appear successful, preventing the cold-press revival path from running."""
    destroy = re.search(r"protected void handleOnDestroy\(\) \{(.*?)\n  \}", PLUGIN, re.S)
    assert destroy and "MusicService.clearListener(transportListener)" in destroy.group(1)
    assert "if (listener == l) listener = null" in SERVICE, (
        "late teardown of an old bridge must not clear the replacement bridge's listener")


def test_play_from_outside_works_on_a_page_that_has_never_played():
    """A car button, the lock screen and a Bluetooth connection cannot see this page, and after a
    reload it holds no track: `_audioEl.play()` against an empty <audio> resolves against nothing and
    the press is silently lost. The fallback to the last track this device played is what makes the
    press work with the app still in the background."""
    init = re.search(r"_nativeInit\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert init and "if(a==='play'){ this._resumeOrPlay(); }" in init.group(1)
    body = re.search(r"_resumeOrPlay\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert body, "MusicPlayer._resumeOrPlay() moved — re-point this test"
    body = body.group(1)
    assert "_lastTrack()" in body and "{force:true, at:last.pos||0}" in body
    # …and the position is only settable once the track is seekable, so the seek happens after play().
    play = APPJS[APPJS.index("async play(sha, opts){"):]
    play = play[: play.index("\n    toggle()")]
    assert play.index("await _audioEl.play()") < play.index("opts.at>0")


def test_the_heartbeat_pushes_while_paused():
    """Paused is the state this breaks in, and it was the one state nothing was ever pushed in — so
    a player paused in a pocket and a player whose WebView Android threw away looked identical for
    the length of the pause. The heartbeat is what makes those distinguishable."""
    beat = re.search(r"_nativeBeat\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert beat, "MusicPlayer._nativeBeat() moved — re-point this test"
    beat = beat.group(1)
    assert "setInterval" in beat
    # Only while there is a service to talk to: every push with none is a background
    # foreground-service start, which Android refuses, and retrying that every 15s for ever is worse
    # than not knowing.
    assert "this._nativeUp" in beat
    assert "this._nativeUp=true" in APPJS and "this._nativeUp=false" in APPJS


def test_every_action_the_service_can_hand_the_app_is_one_the_client_performs():
    """An unanswered press travels as the widget's launch extra. A verb the service queues and the
    client drops is a press that opened the app and then did nothing — the same dead button, one
    step further along."""
    queued = set(re.findall(r'revive\("(\w+)"\)', SERVICE))
    queued |= set(re.findall(r'return "(\w+)";', SERVICE))
    assert queued, "nothing is queued for the app any more — re-point this test"
    consume = re.search(r"consumeLaunch\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert consume, "MusicPlayer.consumeLaunch() moved — re-point this test"
    for verb in queued:
        assert f"a==='{verb}'" in consume.group(1), f"consumeLaunch ignores the queued {verb}"


def test_a_media_button_with_no_player_cannot_crash_the_app():
    """MediaButtonReceiver starts this service with startForegroundService, which gives it ~5s to
    call startForeground or the whole app is killed. Started cold there is no state to publish, so
    the only safe answers are 'go foreground' (a notification for a player that does not exist) or
    'get out of the way'."""
    cold = re.search(r"if \(!foreground\) \{(.*?)\n    \}", SERVICE, re.S)
    assert cold, "the cold-start branch moved — re-point this test"
    assert "stopSelf(startId)" in cold.group(1)


def test_one_press_is_one_wake_up():
    """A media button broadcast delivers the DOWN and the UP of a single press as two intents. Taken
    at face value that is two attempts to open the app per press."""
    body = re.search(r"private static String coldPress\((.*?)\n  \}", SERVICE, re.S)
    assert body, "coldPress() moved — re-point this test"
    body = body.group(1)
    assert "KeyEvent.ACTION_DOWN" in body
    # …and ⏭ on a steering wheel is a media button too. Answering it with "play" would start the
    # library at track one when the driver asked for the next song.
    assert "KEYCODE_MEDIA_NEXT" in body and "KEYCODE_MEDIA_PREVIOUS" in body


# ── Bluetooth autoplay ────────────────────────────────────────────────────────────────────────────


def test_bluetooth_autoplay_asks_for_no_bluetooth_permission():
    """ACTION_ACL_CONNECTED and the A2DP connection-state broadcast both need BLUETOOTH_CONNECT at
    runtime on Android 12+ — a permission prompt about Bluetooth, in a music player, for something
    the user asked for in a music player. The device TYPE is all this needs and comes free."""
    assert "registerAudioDeviceCallback" in SERVICE
    assert "unregisterAudioDeviceCallback" in SERVICE, "an unregistered callback leaks past the service"
    assert "ACTION_ACL_CONNECTED" not in SERVICE
    assert "BLUETOOTH_CONNECT" not in MANIFEST


def test_bluetooth_autoplay_is_opt_in_on_both_sides():
    """A phone that starts playing music by itself in someone's car is what people uninstall an app
    over. Both halves default to off, and the native half is the one that decides."""
    assert re.search(r"getBoolean\(PREF_AUTOPLAY_BT,\s*false\)", SERVICE)
    assert "return false;   // opt-in" in SERVICE
    assert "localStorage.getItem('pc_music_autoplay_bt')==='1'" in APPJS
    autoplay = re.search(r"if \(!autoplayBt\)", SERVICE)
    assert autoplay, "the service must be what gates the autoplay, not only the UI"


def test_the_first_device_sweep_is_not_a_car_door_opening():
    """registerAudioDeviceCallback fires immediately with everything ALREADY connected. Treated as
    an arrival, that autoplays on every service start made while a speaker happens to be paired."""
    assert "firstDeviceSweep" in SERVICE
    cb = re.search(r"onAudioDevicesAdded\(AudioDeviceInfo\[\] added\) \{(.*?)\n    \}", SERVICE, re.S)
    assert cb, "the audio-device callback moved — re-point this test"
    assert cb.group(1).lstrip().startswith("if (firstDeviceSweep)")


def test_the_options_are_pushed_again_on_every_page_load():
    """The service outlives the page, so a reloaded WebView (or one whose renderer died) has to tell
    it what the user chose all over again — and it is stored natively so a service that has been up
    for an hour still honours a switch flipped before any of this page existed."""
    init = re.search(r"_nativeInit\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert init and "setOptions({autoplayBluetooth:this.autoplayBT()})" in init.group(1)
    assert "SharedPreferences" in SERVICE or "getSharedPreferences" in SERVICE
    assert "setOptions" in PLUGIN and "status" in PLUGIN


def test_the_receipt_does_not_depend_on_there_being_a_track():
    """`_nativePush` refuses to send state for a player holding nothing — rightly, since that would
    raise a notification about nothing. If that push were also the receipt, a page that had just
    reloaded (the exact case this feature exists for) would be indistinguishable from a dead one:
    the service wakes an app that is already awake and then writes "the player stopped responding"
    over a notification the user is looking at."""
    init = re.search(r"_nativeInit\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert init, "MusicPlayer._nativeInit() moved — re-point this test"
    handler = init.group(1)
    # First thing in the handler, before any branch that can decline to act on the press.
    assert handler.index("this._nativeAck();") < handler.index("if(a==='play')")
    ack = re.search(r"_nativeAck\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert ack and "this.cur" not in ack.group(1), "the ack must not be gated on holding a track"
    assert "public void ack(PluginCall call)" in PLUGIN
    assert "void ack() {" in SERVICE


def test_a_skip_on_an_empty_player_is_a_resume_not_a_skip():
    """`next()` on a reloaded page walks an empty queue, or — worse — `indexOf(null)` is -1 and the
    modulo lands on track one of the library, which is not what ⏭ means to anybody in a car."""
    init = re.search(r"_nativeInit\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert init
    for verb in ("next", "prev"):
        assert f"if(a==='{verb}'){{ if(this.cur) this.{verb}(); else this._resumeOrPlay(); }}" \
            in init.group(1), f"a {verb} press with no track loaded must resume, not walk an empty queue"


def test_a_press_that_could_not_be_performed_still_lands_somewhere():
    """The launch extra is consumed natively — there is no second chance at it. A verb that returns
    without doing anything leaves the app dragged to the foreground on an unrelated screen with
    nothing to say why."""
    consume = re.search(r"consumeLaunch\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert consume, "MusicPlayer.consumeLaunch() moved — re-point this test"
    body = consume.group(1)
    assert "renderMusicApp();" in body
    assert "if(done){ this._render(); renderMusicApp(); return; }" in body, \
        "a successful widget press left the confusing floating player over an unrelated screen"


def test_the_widget_gets_the_same_receipt_check_as_every_other_surface():
    """A widget is pressed with the app closed more often than anything else here, which makes it the
    surface most likely to be pressed at a WebView Android has taken away."""
    live = re.search(r"MusicService svc = MusicService\.INSTANCE;\s*if \(svc != null\) \{(.*?)\n    \}",
                     WIDGET, re.S)
    assert live, "MusicWidget's live-player branch moved — re-point this test"
    assert "fromWidget(" in live.group(1) and "MusicService.emit(" not in live.group(1), \
        "a bare emit from the widget reports success and does nothing"
    fw = re.search(r"void fromWidget\(String action\) \{(.*?)\n  \}", SERVICE, re.S)
    assert fw and "press(" in fw.group(1) and "hush(" in fw.group(1)


def test_the_position_is_not_filed_under_the_wrong_track():
    """play() sets `cur` to the new sha synchronously and assigns `_audioEl.src` only after awaiting
    the decrypted URL — and ontimeupdate keeps firing on the OLD source across that gap."""
    body = re.search(r"_rememberLast\(\)\{(.*?)\n    \},", APPJS, re.S)
    assert body, "MusicPlayer._rememberLast() moved — re-point this test"
    assert "if(this._loading) return;" in body.group(1)


def test_the_counters_outlive_the_service_that_counted_them():
    """The case the panel exists to explain is the COLD one — a press made in the car with the app
    closed — and that path deliberately ends in stopSelf(). Read off the instance, the counters were
    gone by the time anyone could look, and the panel answered "nothing has played this session"
    about the very press being investigated."""
    assert re.search(r"static volatile int btConnects", SERVICE), \
        "the counters must survive the instance"
    status = re.search(r"public void status\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S)
    assert status, "MusicPlugin.status() moved — re-point this test"
    body = status.group(1)
    for key in ("btConnects", "btAutoplays", "unanswered", "revived", "note"):
        assert f"MusicService.{key}" in body, f"{key} is still read off the instance"


def test_the_state_shared_across_threads_is_volatile():
    """apply()/ack() run on Capacitor's plugin thread; the receipt checks and the device callback run
    on the main looper. Without a barrier the check can read a stale timestamp and declare a living
    player dead."""
    for field in ("long lastWebAt", "boolean webGone", "boolean autoplayBt"):
        assert re.search(r"volatile %s" % field, SERVICE), f"{field} is read from another thread"


def test_a_cold_press_prefers_a_live_client_to_launching_the_app():
    """`listener` is non-null only while a page is loaded with the transport handler armed. Handing
    it the press is cheaper and less rude than throwing an activity at somebody who is driving."""
    cold = re.search(r"if \(!foreground\) \{(.*?)\n    \}", SERVICE, re.S)
    assert cold, "the cold-start branch moved — re-point this test"
    body = cold.group(1)
    assert "if (listener != null) emit(want, 0);" in body and "else revive(want);" in body


def test_a_parked_press_goes_stale_rather_than_replaying():
    """An intent delivered to an app already on screen fires onNewIntent and then sits there —
    nothing re-reads it until the next resume. Performed minutes later, it starts music over
    somebody who had since paused it."""
    assert "EXTRA_LAUNCH_AT" in PLUGIN and "EXTRA_LAUNCH_AT" in SERVICE and "EXTRA_LAUNCH_AT" in WIDGET
    consume = re.search(r"public void consumeLaunchAction\(PluginCall call\) \{(.*?)\n  \}", PLUGIN, re.S)
    assert consume and "LAUNCH_MAX_AGE_MS" in consume.group(1)


def test_the_phone_reports_what_it_measured():
    """Support diagnostics remain measurable without frightening users in the Music player."""
    for key in ("webSilenceMs", "webGone", "btConnects", "btAutoplays", "unanswered", "revived", "note"):
        assert f'"{key}"' in PLUGIN, f"status() does not report {key}"
    assert 'id="ma-cardiag"' not in APPJS
    assert 'id="ma-carnote"' not in APPJS


# ---- Bluetooth autoplay with the app CLOSED ----------------------------------------------------
#
# The feature existed and could never fire for the case it was written for. `MusicService.deviceCb`
# is registered inside that service's startup, and the service is created by something PLAYING — so
# arming autoplay required the exact action autoplay replaces. A car finds the app closed, Details
# read `media controls: not running`, and the report was "I still have to manually play the song".
#
# The listener now lives in StayAwakeService — the foreground service that IS alive with the app
# closed, restarted by BootReceiver, already owning a permanent notification — while every decision
# stays in MusicService. These assert that split, because each half is silent on its own: a listener
# with no policy autoplays on every reboot, and policy with no listener is what shipped.

STAY = _read(JAVA, "push", "StayAwakeService.java")

def _code(java):
    """Java with comments removed.

    Two of the guards below assert that a token is ABSENT, or that one appears before another — and
    both tripped on the file's own prose, which names the very APIs it explains why it is not using.
    A guard that reads comments is asserting on documentation.
    """
    java = re.sub(r"/\*.*?\*/", "", java, flags=re.S)
    return re.sub(r"//[^\n]*", "", java)




def test_the_closed_app_listener_exists_where_something_is_actually_running():
    assert "registerAudioDeviceCallback" in STAY, (
        "StayAwakeService does not listen for a Bluetooth sink — autoplay is then back to needing "
        "MusicService, which only exists once something has already played")
    assert "MusicService.onBluetoothSinkConnected" in STAY, (
        "the listener must hand the decision to MusicService, not re-implement the policy")


def test_the_listener_is_unregistered_when_the_service_stops():
    assert "unregisterAudioDeviceCallback" in STAY, \
        "an AudioDeviceCallback outliving its service leaks it for the life of the process"


def test_the_listener_is_registered_once_and_only_after_going_foreground():
    """onStartCommand runs again on every restart and on the STICKY relaunch. A second registration
    delivers each connection twice — which the debounce would absorb, so it would go unnoticed
    until it did not."""
    code = _code(STAY)
    # The GATE, not just the token: `audioCbOn` also appears at its assignment and in onDestroy, so
    # asserting the name is present passed with the guard deleted (verified).
    i = code.index("registerAudioDeviceCallback")
    assert "if (!audioCbOn)" in code[:i], (
        "the registration is not gated on audioCbOn — onStartCommand runs again on every restart "
        "and on the STICKY relaunch, so the callback would be registered again each time")
    assert code.index("startForeground") < i, \
        "register only after the service is foreground, or the start can be refused mid-setup"


def test_the_first_sweep_is_not_a_car_door():
    """registerAudioDeviceCallback fires immediately with everything already connected. At boot,
    with a paired speaker in range, that is not somebody getting into a car."""
    assert "firstDeviceSweep" in STAY, \
        "without the first-sweep guard the phone autoplays every time it reboots near a speaker"


def test_the_background_entry_point_does_not_double_fire():
    """If MusicService IS up, its own deviceCb already has this connection; firing again would be a
    second press for one car door."""
    fn = SERVICE[SERVICE.index("public static void onBluetoothSinkConnected"):]
    fn = fn[:fn.index("\n  }")]
    assert "INSTANCE" in fn, "the background path must stand down when the player is already running"
    assert "autoplayBluetooth(ctx)" in fn, "the opt-in must be checked on the background path too"
    assert "lastBgAutoplayAt" in fn, \
        "one connection reports several devices — without a debounce that is several presses"


def test_the_background_start_is_not_a_foreground_service_start():
    """The cold branch of onStartCommand deliberately never calls startForeground (it has nothing to
    publish), and a startForegroundService that does not is killed with a crash five seconds later."""
    fn = _code(SERVICE)
    fn = fn[fn.index("public static void onBluetoothSinkConnected"):]
    fn = fn[:fn.index("\n  }")]
    assert "ctx.startService(" in fn, "background autoplay must use a plain start"
    assert "startForegroundService" not in fn, \
        "startForegroundService here crashes the app 5s later — the cold path never goes foreground"


def test_a_refused_start_is_counted_rather_than_swallowed():
    """There is no device here, and 'the car sent nothing', 'the switch is off' and 'Android refused
    the start' are indistinguishable from the driver's seat unless the phone says which."""
    assert "btRefused" in SERVICE and "btRefused" in PLUGIN, \
        "a refused background start must reach MusicPlugin.status(), or it fails the silent way"
    assert "stayConnected" in PLUGIN, \
        "status() must report whether the closed-app listener is even running"
    assert "stayConnected" in APPJS, \
        "the Details panel must say when 'stay connected' is off, or that reads as a broken car"


def test_the_switch_names_what_it_depends_on():
    """Autoplay now rides 'stay connected', which is off by default. An unstated dependency is the
    same silent no-op this feature already was."""
    phone = _read(ROOT, "static", "js", "client", "phoneshell.js")
    i = phone.index("Bluetooth autoplay on")
    assert "stay connected" in phone[i:i + 300], \
        "the autoplay toast must name its dependency on 'stay connected'"


MAIN_ACT = _read(JAVA, "MainActivity.java")


def test_the_webview_may_start_audio_without_a_tap():
    """AUTOPLAY'S ACTUAL BLOCKER, measured in a car: the track loaded and never played.

    `setMediaPlaybackRequiresUserGesture` defaults to TRUE and nothing had ever set it. Every other
    way this app starts audio follows a touch, so it never mattered — until a feature whose entire
    job is to start a song when nobody is touching the phone. play() returned a rejected promise and
    the player sat there looking frozen.
    """
    code = _code(MAIN_ACT)
    assert "setMediaPlaybackRequiresUserGesture(false)" in code, (
        "the WebView will refuse audio.play() that no tap asked for — Bluetooth autoplay loads the "
        "track and never plays it, with nothing on screen and nothing in any log")
    assert code.index("super.onCreate") < code.index("setMediaPlaybackRequiresUserGesture"), \
        "the bridge and its WebView do not exist before super.onCreate()"


def test_a_refused_play_is_reported_not_swallowed():
    """`r.catch(()=>{})` is why this looked frozen rather than broken. The report has to reach the
    SERVICE: the failure happens with the app backgrounded, where a toast reaches nobody."""
    assert "_nativeBlocked" in APPJS, "a rejected play() is still being swallowed"
    assert "playBlocked" in PLUGIN and "playBlocked" in SERVICE, \
        "the refusal never reaches the half that outlives the page"
    assert "blocked" in APPJS and "blocked" in PLUGIN, \
        "Details cannot show a refused playback, so 'frozen' stays indistinguishable from 'never tried'"


def test_a_media_session_exists_before_anything_plays():
    """WHY THE CAR SAW NOTHING AND ITS PLAY BUTTON DID NOTHING.

    Android routes a car's PLAY to an ACTIVE MediaSession and a head unit reads its "now playing"
    line from that session's metadata. This app built one only when a track started, so before the
    first song there was no session at all — no metadata to show, and a button with nowhere to route.
    Every other media player keeps a session alive whether or not sound is coming out.
    """
    code = _code(STAY)
    assert "MediaSessionCompat" in code and "setActive(true)" in code, (
        "no standby session — the car has no media app to talk to until something has already "
        "played, which is the state a car is never in")
    assert "ACTION_PLAY" in code and "ACTION_SKIP_TO_NEXT" in code, (
        "a session that declares no actions gives a head unit a media app it cannot operate")


def test_the_standby_session_stands_down_for_a_real_one():
    """Two active sessions in one app lets the car route to the wrong one — and the wrong one here
    is the one with no track in it."""
    assert "ACTION_DROP_STANDBY" in _code(STAY), "nothing tells the standby session to release"
    assert "releaseStandby" in _code(SERVICE), \
        "MusicService takes over without dropping the standby session"


def test_the_standby_session_publishes_no_false_state():
    """A head unit shows whatever the metadata says. Naming a song that is not loaded puts a lie on
    somebody's dashboard."""
    code = _code(STAY)
    assert "STATE_PAUSED" in code, "standby must not claim to be playing"


# ---- the home-screen widget's markup ------------------------------------------------------------

import xml.etree.ElementTree as _ET

RES = os.path.join(ANDROID, "src", "main", "res")


def _res_xml():
    for root, _dirs, files in os.walk(RES):
        for f in files:
            if f.endswith(".xml"):
                yield os.path.join(root, f)


def test_every_android_resource_xml_parses():
    """A malformed resource is a BUILD failure, and the way it happens here is not obvious: XML
    forbids `--` inside a comment, and this app's palette is written `--neon` / `--neon2` in the CSS
    these drawables are copied from. Pasting those names into an explanatory comment is enough to
    break aapt, which is exactly what happened while restyling the widget."""
    for path in _res_xml():
        try:
            _ET.parse(path)
        except _ET.ParseError as e:
            raise AssertionError(f"{os.path.relpath(path, ROOT)} does not parse: {e}")


def test_the_widget_keeps_the_ids_the_code_drives():
    """RemoteViews binds by id. A restyle that renames or drops one leaves a widget that inflates
    fine and does nothing — no crash, no log, just dead buttons."""
    layout = _read(RES, "layout", "widget_music.xml")
    for wid in ("mw_body", "mw_title", "mw_artist", "mw_prev", "mw_play", "mw_next"):
        assert f'@+id/{wid}"' in layout, f"widget_music.xml lost {wid}, which MusicWidget binds"
        assert f"R.id.{wid}" in WIDGET, f"MusicWidget no longer drives {wid}"


def test_the_widget_uses_only_view_types_a_launcher_can_inflate():
    """RemoteViews supports a fixed handful. Anything else throws in the LAUNCHER's process, so the
    widget shows 'Problem loading widget' and nothing in this app's log says why."""
    allowed = {"LinearLayout", "FrameLayout", "RelativeLayout", "ImageView", "TextView",
               "ProgressBar", "Chronometer", "ViewFlipper", "GridLayout", "Space", "View"}
    used = {el.tag for el in _ET.parse(os.path.join(RES, "layout", "widget_music.xml")).iter()}
    bad = used - allowed
    assert not bad, f"widget uses view types RemoteViews cannot inflate: {sorted(bad)}"


def test_the_widget_touch_targets_stay_big_enough():
    """A widget sits among home-screen icons; anything under ~44dp is hit by accident on the wrong
    control, which on a transport row means skipping a track when you meant to pause."""
    layout = _read(RES, "layout", "widget_music.xml")
    for wid in ("mw_prev", "mw_play", "mw_next"):
        block = layout[layout.index(f'@+id/{wid}"'):]
        block = block[:block.index("/>")]
        m = re.search(r'android:layout_width="(\d+)dp"', block)
        assert m and int(m.group(1)) >= 44, f"{wid} is smaller than 44dp"

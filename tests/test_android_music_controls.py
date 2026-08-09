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
    assert re.search(r"if\(!this\.cur\b[^)]*\)\s*return;", push.group(1))


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

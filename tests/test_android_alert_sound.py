"""The PosterChan Alert reaches the phone: "make a alert or notification sound for posterchan launcher that user
can easily enable" -- "cute anime cyber punk sound".

Two switches, both one tap in User Settings → Sounds: (1) PosterChan's own notifications play it -- a SECOND
messages channel carrying the bundled res/raw sound, because Android fixes a channel's sound when it is created
and no app can change it afterwards; (2) it becomes the phone's notification sound -- RingtonePlugin stores it
under Notifications/ (IS_NOTIFICATION) and sets TYPE_NOTIFICATION. The decisions run under javac; the wiring that
has failed silently in this repo before (a raw resource never bundled, a channel never created, the switch never
consulted) is asserted, and the sound is decoded.
"""
import os
import shutil
import subprocess



from tests.test_android_ringtone import ANDROID, PKG, ROOT, _run_rules

PUSH = open(os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "push", "PushEventService.java")).read()
PLUGIN = open(os.path.join(PKG, "RingtonePlugin.java")).read()


def test_the_switch_picks_the_alert_channel_and_a_notification_goes_to_notifications():
    got = _run_rules("\n".join([
        'System.out.println(RingtoneRules.messagesChannel(true));',
        'System.out.println(RingtoneRules.messagesChannel(false));',
        'System.out.println(RingtoneRules.folder("notification"));',
        'System.out.println(RingtoneRules.folder("ringtone"));',
        'System.out.println(RingtoneRules.folder(null));']))
    assert got == ["pcai_messages_alert", "pcai_messages", "Notifications/", "Ringtones/", "Ringtones/"], got


def test_notifications_are_posted_to_the_channel_the_switch_chose():
    # Both the builder and the readiness check ask the switch; the old constant is gone, so nothing can post to
    # the plain channel while the person has the alert turned on.
    assert "new Notification.Builder(ctx, isCall ? CH_CALLS : msgsChannel(ctx))" in PUSH
    assert "getNotificationChannel(call ? CH_CALLS : msgsChannel(ctx))" in PUSH
    assert "CH_MSGS)" not in PUSH and "CH_MSGS =" not in PUSH
    # The alert channel exists and carries the bundled sound as a NOTIFICATION sound.
    assert "messagesChannel(true)" in PUSH and "R.raw.posterchan_alert" in PUSH and "USAGE_NOTIFICATION" in PUSH
    assert "nm.createNotificationChannel(alert)" in PUSH


def test_the_plugin_sets_the_phone_notification_sound_and_flips_the_switch():
    assert "RingtoneManager.TYPE_NOTIFICATION" in PLUGIN and "IS_NOTIFICATION, notify ? 1 : 0" in PLUGIN
    assert "@PluginMethod public void appAlert(PluginCall call)" in PLUGIN
    assert "PushEventService.setAlertSound(getContext(), on)" in PLUGIN
    assert 'r.put("appAlert", place.poster.app.push.PushEventService.alertSoundOn(getContext()))' in PLUGIN


def test_the_sound_is_bundled_everywhere_and_is_short_real_audio():
    raw = os.path.join(ANDROID, "src", "main", "res", "raw", "posterchan_alert.ogg")
    for path in (raw, os.path.join(ROOT, "static", "sounds", "posterchan-alert.ogg"),
                 os.path.join(ROOT, "static", "sounds", "posterchan-alert.mp3"),
                 os.path.join(ROOT, "static", "sounds", "posterchan-chime.ogg"),
                 os.path.join(ROOT, "static", "sounds", "posterchan-chime.mp3")):
        assert 10_000 < os.path.getsize(path) < 200_000, path
        if shutil.which("ffprobe"):
            d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, text=True).stdout.strip()
            assert (0.5 if "chime" in path else 1.0) < float(d) < 2.5, (path, d)   # a notification, not a song
    for script in ("mobile/build-www.sh", "desktop/build-www.sh"):
        assert 'static/sounds/* www/static/sounds/' in open(os.path.join(ROOT, script)).read(), script

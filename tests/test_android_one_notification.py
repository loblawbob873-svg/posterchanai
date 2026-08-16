"""ONE permanent notification, however many background services this app runs.

"2 notifications is bullshit!" — and it was. The signer and "stay connected" are two services because
they do two jobs, but nobody outside this codebase asked for two services; they asked for an app that
does not put two permanent items in their shade. Android requires every foreground service to post a
notification, so the fix is not fewer notifications, it is fewer notification IDS: two services
posting the same id are one item on screen.

None of this can be driven here — there is no device and the Gradle build runs on CI — so what is
guarded is the WIRING, and specifically the three ways it silently comes apart:

  * a service keeps (or regrows) its own id/channel, and there are two items again. This is the one
    that regresses by accident: a private `NOTIF_ID` next to a `startForeground` reads like normal
    Android and is exactly how the second notification existed in the first place.
  * a service stopping calls STOP_FOREGROUND_REMOVE while the other is still up. That deletes the
    shared notification out from under a service that is still foreground, leaving a running
    background service with nothing in the shade — the thing the platform requires and the user is
    owed. It has to DETACH while anything else needs it.
  * a service sets its `running` flag AFTER going foreground. The text is composed from those flags,
    so the first notification of every start would describe an app in which nothing is running.

The notification cannot be built here (it needs the framework), so these are read as text. That is
weaker than running it and is used because it is the best available — the actual proof is a device.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


SIGNER = _read(JAVA, "signer", "SignerRelayService.java")
STAY = _read(JAVA, "push", "StayAwakeService.java")
SYNC = _read(JAVA, "sync", "SyncService.java")
NOTE = _read(JAVA, "RunningNote.java")
# The background folder sweep is the THIRD service to post this notification, and the third is where
# a truth table over "the other one" stops meaning anything — see othersRunning.
SERVICES = (("SignerRelayService", SIGNER), ("StayAwakeService", STAY), ("SyncService", SYNC))


def test_there_is_exactly_one_notification_id_and_one_channel():
    """The id and the channel live in RunningNote, and nowhere else."""
    assert re.search(r"public static final int ID\s*=\s*\d+", NOTE), "RunningNote lost its shared id"
    assert re.search(r'CHANNEL\s*=\s*"pcai_running"', NOTE), "RunningNote lost its shared channel"
    for name, src in SERVICES:
        assert not re.search(r"private static final int NOTIF_ID", src), (
            f"{name} has its own notification id again — that is a second permanent notification"
        )
        assert not re.search(r'CHANNEL\s*=\s*"pcai_(signer|stay_connected)"', src), (
            f"{name} has its own notification channel again"
        )


def test_both_services_go_foreground_with_the_shared_notification():
    for name, src in SERVICES:
        assert re.search(r"startForeground\(\s*this,\s*RunningNote\.ID,\s*RunningNote\.build\(this\)", src), (
            f"{name} does not post the shared notification when it goes foreground"
        )


def test_a_service_standing_down_never_removes_a_notification_another_one_is_using():
    """REMOVE while the other service is still foreground leaves it with nothing in the shade."""
    for name, src in SERVICES:
        assert "dropNotification()" in src, f"{name} no longer stands down through dropNotification()"
        body = src[src.index("private void dropNotification()"):]
        body = body[:body.index("\n    }") + 6] if "\n    }" in body else body
        assert "othersRunning" in body, (
            f"{name}.dropNotification() does not ask whether anything else still needs the notification"
        )
        assert "STOP_FOREGROUND_DETACH" in body, (
            f"{name}.dropNotification() has no DETACH branch, so stopping it removes the shared "
            f"notification from under the other service"
        )
        # An unconditional REMOVE anywhere else in the file is the same bug by another route.
        for m in re.finditer(r"STOP_FOREGROUND_REMOVE", src):
            window = src[max(0, m.start() - 400):m.start()]
            assert "othersRunning" in window, (
                f"{name} calls STOP_FOREGROUND_REMOVE outside the guarded stand-down path"
            )


def test_running_is_set_before_going_foreground_not_after():
    """The shared text is composed from these flags, so the order is what makes it true."""
    for name, src in SERVICES:
        start = src.index("public int onStartCommand(")
        fg = src.index("startForeground(", start)
        before = src[start:fg]
        assert re.search(r"\brunning\s*=\s*true\s*;", before), (
            f"{name} sets running=true after startForeground, so the first notification of a start "
            f"describes an app in which it is not running"
        )
        # …and puts it back if going foreground failed, or the text lies in the other direction.
        # A generous window: StayAwakeService does real work (the audio callback, the standby
        # session) between going foreground and its catch.
        after = src[fg:fg + 4000]
        assert re.search(r"catch[\s\S]{0,200}running\s*=\s*false", after), (
            f"{name} does not clear running when startForeground throws"
        )


def test_the_upgrade_clears_the_notification_nothing_will_post_again():
    """An install coming from the two-notification build already has both items on screen.

    The signer's old id is not posted by anything after this change, so nothing would ever remove it
    and the user would keep staring at the exact thing this was meant to fix.
    """
    assert re.search(r"cancel\(LEGACY_SIGNER_ID\)", NOTE), (
        "the old signer notification is never cancelled, so upgrading leaves it on screen for ever"
    )
    assert "deleteNotificationChannel" in NOTE, (
        "the retired channels stay in the app's notification settings offering switches that "
        "control nothing"
    )


def test_the_text_names_every_job_that_is_running():
    """One item still has to say what it is doing, or it is just a mystery notification.

    COMPOSED, NOT ENUMERATED. This was a truth table over two services, which is readable at two and
    is four branches at three — and the branch that gets forgotten is always the new service's,
    which then runs under a notification describing somebody else's job."""
    body = NOTE[NOTE.index("public static String text()"):]
    body = body[:body.index("public static Notification build")]
    for svc in ("SignerRelayService.running", "StayAwakeService.running", "SyncService.running"):
        assert svc in body, f"the composed text never asks about {svc}"
    assert "Working in the background" in body, "there is no text for the case nothing is running"


def test_a_third_service_cannot_be_told_apart_from_the_other_two():
    """`othersRunning(boolean)` answered "is the OTHER one up" while there were exactly two. A third
    makes that meaningless, and the failure is the one this file exists for: a service stands down,
    reads a stale "nothing else is running", and REMOVEs the shared notification out from under a
    service that is still foreground."""
    body = NOTE[NOTE.index("public static boolean othersRunning("):]
    body = body[:body.index("\n    }")]
    for svc in ("SignerRelayService.running", "StayAwakeService.running", "SyncService.running"):
        assert svc in body, f"othersRunning does not consider {svc}"
    assert "me !=" in body, "othersRunning does not exclude the caller, so it always answers yes"


def test_two_stop_actions_are_told_apart():
    """Two buttons both saying "Turn off" beside each other is a coin toss, and they differ."""
    body = NOTE[NOTE.index("public static Notification build"):]
    assert re.search(r'both\s*\?\s*"Stop signing"\s*:\s*"Turn off"', body), (
        "the signer's action is not renamed when both services are running"
    )
    assert re.search(r'both\s*\?\s*"Stop staying connected"\s*:\s*"Turn off"', body), (
        "the stay-connected action is not renamed when both services are running"
    )

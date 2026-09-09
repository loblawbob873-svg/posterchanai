import re

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUSH = (ROOT / "mobile/android/app/src/main/java/place/poster/app/push/PushEventService.java").read_text()
APP = (ROOT / "static/js/client/app.js").read_text(errors="replace")


def test_direct_push_is_not_dropped_just_because_the_app_is_visible():
    deliver = PUSH[PUSH.index("public static boolean deliver"):PUSH.index("public static void show")]
    assert "AppVisible.is()" not in deliver
    assert "show(ctx, title, body, type, eventTag, route)" in deliver


def test_live_and_direct_delivery_share_one_android_replacement_tag():
    """The RULE, not the spelling: an event id becomes the `nostr-<id>` tag on both transports.

    This used to assert one exact line, which broke the moment the same expression grew a branch —
    a test measuring formatting where it meant a rule. What has to hold is that a push carrying an
    `eid` and the client's own live notification for that event agree on the tag, so Android
    replaces rather than stacks.
    """
    assert '"nostr-" + eid' in PUSH
    ping = APP[APP.index("function notifPing(ev)"):APP.index("function notifToast")]
    assert "tag:'nostr-'+ev.id" in ping


def test_an_explicit_tag_beats_the_event_id():
    """A gift wrap has no `eid`, so a DM can only share the client's tag by being told one.

    Without this every DM push posted under the default "msg" while the client's decrypted
    "Alice sent you a DM" posted under "pc-dm" — two cards for one message.
    """
    deliver = PUSH[PUSH.index("public static boolean deliver"):PUSH.index("public static boolean canNotify")]
    assert 'j.optString("tag"' in deliver
    # An explicit tag must be the FIRST branch of the assignment, or the eid still wins whenever
    # there is one and the two transports part company again.
    assert re.search(r"eventTag\s*=\s*!\w+\.isEmpty\(\)\s*\?\s*\w+\s*:", deliver), deliver[:400]


def test_calls_and_messages_both_reach_the_native_builder():
    deliver = PUSH[PUSH.index("public static boolean deliver"):PUSH.index("public static void show")]
    assert "show(ctx, title, body, type, eventTag, route)" in deliver
    assert "return true;" in deliver

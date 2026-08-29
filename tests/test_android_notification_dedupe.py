from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUSH = (ROOT / "mobile/android/app/src/main/java/place/poster/app/push/PushEventService.java").read_text()
APP = (ROOT / "static/js/client/app.js").read_text(errors="replace")


def test_gcompat_push_is_not_dropped_just_because_the_app_is_visible():
    on_message = PUSH[PUSH.index("public void onMessage"):PUSH.index("public static void show")]
    assert "AppVisible.is()" not in on_message
    assert "show(ctx, title, body, type, eventTag, route)" in on_message


def test_live_and_gcompat_delivery_share_one_android_replacement_tag():
    assert 'eventTag = !eid.isEmpty() ? "nostr-" + eid : null;' in PUSH
    ping = APP[APP.index("function notifPing(ev)"):APP.index("function notifToast")]
    assert "tag:'nostr-'+ev.id" in ping


def test_calls_and_messages_both_reach_the_native_builder():
    on_message = PUSH[PUSH.index("public void onMessage"):PUSH.index("public static void show")]
    assert "show(ctx, title, body, type, eventTag, route)" in on_message
    assert "return;" not in on_message

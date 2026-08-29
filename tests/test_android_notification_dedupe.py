from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUSH = (ROOT / "mobile/android/app/src/main/java/place/poster/app/push/PushEventService.java").read_text()
APP = (ROOT / "static/js/client/app.js").read_text(errors="replace")


def test_gcompat_push_defers_to_visible_webview_notification_owner():
    assert 'if (!"call".equals(type) && place.poster.app.sms.AppVisible.is()) return;' in PUSH
    assert PUSH.index("AppVisible.is()) return") < PUSH.index(
        "show(ctx, title, body, type, eventTag, route)"
    )


def test_live_and_gcompat_delivery_share_one_android_replacement_tag():
    assert 'eventTag = !eid.isEmpty() ? "nostr-" + eid : null;' in PUSH
    ping = APP[APP.index("function notifPing(ev)"):APP.index("function notifToast")]
    assert "tag:'nostr-'+ev.id" in ping


def test_calls_remain_native_even_while_main_activity_is_visible():
    assert 'if (!"call".equals(type)' in PUSH

"""System notifications must preserve the screen/item they describe."""
from pathlib import Path

ROOT=Path(__file__).parents[1]
APP=(ROOT/'static/js/client/app.js').read_text(errors='replace')
SW=(ROOT/'static/js/client/sw.js').read_text()
PHONE=(ROOT/'static/js/client/phoneshell.js').read_text()
PUSH=(ROOT/'mobile/android/app/src/main/java/place/poster/app/push/PushEventService.java').read_text()
PLUGIN=(ROOT/'mobile/android/app/src/main/java/place/poster/app/push/PushPlugin.java').read_text()


def test_android_notification_carries_fresh_exact_route():
    assert 'route = !eid.isEmpty() ? "post:" + eid' in PUSH
    assert 'HomeActivity.EXTRA_VIEW' in PUSH
    assert 'HomeActivity.EXTRA_VIEW_AT' in PUSH
    assert 'String route = call.getString("route", "notifications")' in PLUGIN


def test_native_client_consumes_post_route():
    assert "v.indexOf('post:') === 0" in PHONE
    assert 'PC.openThread(id)' in PHONE
    assert "route:(opts&&opts.route)||'notifications'" in APP
    assert "route:'messages'" in APP
    assert "route:'mail'" in APP


def test_web_push_click_routes_event_or_view():
    assert "'?event='" in SW
    assert "'?view='" in SW
    assert 'c.navigate(home)' in SW
    assert "sp.get('event')" in APP
    assert 'openThread(event)' in APP


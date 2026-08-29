from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_visible_client_is_the_single_notification_producer_for_every_push_kind():
    sw = (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")
    push = sw[sw.index("self.addEventListener('push'"):
              sw.index("self.addEventListener('notificationclick'")]
    assert "clients.matchAll({ type: 'window', includeUncontrolled: true })" in push
    assert "cs.some(c => c.focused || c.visibilityState === 'visible')" in push
    assert push.index("cs.some(c => c.focused || c.visibilityState === 'visible')") < push.index(
        "self.registration.showNotification(title, opts)"
    )
    assert "suppressIfFocused" not in push


def test_live_relay_event_is_deduped_by_event_id_before_it_announces():
    app = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    watch = app[app.index("function watchNotifications"):
                app.index("function notifPing")]
    assert "if(Store.saveEvent(ev))" in watch
    assert watch.index("if(Store.saveEvent(ev))") < watch.index("notifPing(ev)")

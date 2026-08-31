"""One send() call, two first-party transports, and the discriminator between them.

The packaged Android app cannot use Web Push, so it registers a PosterChan Direct socket. Explicit
transport metadata keeps a malformed Web Push row from ever becoming an arbitrary server-side POST.

Getting that wrong is silent in both directions. Route a Web Push subscription down the HTTP path and
we queue it for the wrong device; route Direct through pywebpush and it raises on missing keys.
"""
from unittest.mock import patch

from app.services import push_service as ps

WEBPUSH = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "k", "auth": "a"}}
DIRECT = {"id": 42, "transport": "posterchan-direct", "endpoint": "direct:x:y", "keys": {}}


def test_a_web_push_subscription_never_takes_the_http_path():
    with patch.object(ps.direct_push_service, "enqueue") as direct, \
         patch.object(ps.settings_store, "get", return_value=None):
        ps.send(WEBPUSH, {"type": "dm"})
    assert not direct.called


def test_a_direct_subscription_is_queued_by_database_id():
    with patch.object(ps.direct_push_service, "enqueue", return_value=True) as enqueue:
        assert ps.send(DIRECT, {"title": "x", "type": "dm"}) is True
    enqueue.assert_called_once_with(42, {"title": "x", "type": "dm"})


def test_a_direct_failure_does_not_fall_through_to_webpush():
    with patch.object(ps.direct_push_service, "enqueue", return_value=False), \
         patch.object(ps.settings_store, "get") as vapid:
        assert ps.send(DIRECT, {"type": "call"}) is False
    vapid.assert_not_called()


def test_a_legacy_keyless_endpoint_is_rejected_not_posted():
    """Old UnifiedPush URLs are capability URLs. They must never regain an HTTP POST path."""
    with patch.object(ps.direct_push_service, "enqueue") as direct:
        assert ps.send({"endpoint": "https://ntfy.sh/old-secret", "keys": {}}, {}) is False
    direct.assert_not_called()

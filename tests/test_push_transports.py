"""One send() call, two transports, and the discriminator between them.

The packaged Android app cannot use Web Push at all — a WebView has no push service — so it registers
a UnifiedPush endpoint instead, which is a plain URL we POST to. Both live in the same table and are
told apart by ONE thing: a Web Push row has VAPID keys and a UnifiedPush row does not.

Getting that wrong is silent in both directions. Route a Web Push subscription down the HTTP path and
we POST an unencrypted payload to Google's FCM endpoint, which drops it; route a UnifiedPush endpoint
through pywebpush and it raises on missing keys. Either way the phone just never buzzes.
"""
from unittest.mock import MagicMock, patch

from app.services import push_service as ps

WEBPUSH = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "k", "auth": "a"}}
UNIFIED = {"endpoint": "https://ntfy.sh/pcai-abc", "keys": {}}


def test_a_web_push_subscription_never_takes_the_http_path():
    with patch.object(ps, "_send_unifiedpush") as up, patch.object(ps.settings_store, "get", return_value=None):
        ps.send(WEBPUSH, {"type": "dm"})
    assert not up.called


def test_a_keyless_subscription_is_posted():
    with patch.object(ps.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        assert ps.send(UNIFIED, {"title": "x", "type": "dm"}) is True
    assert post.call_args.args[0] == UNIFIED["endpoint"]
    # The SAME payload shape the service worker already parses — one notification contract for every
    # transport, so the Android receiver cannot drift from the web client's.
    assert post.call_args.kwargs["json"] == {"title": "x", "type": "dm"}


def test_a_call_is_marked_urgent():
    """Distributors deprioritise normal traffic when the phone is dozing. A ring cannot wait."""
    got = {}
    for typ in ("call", "dm"):
        with patch.object(ps.requests, "post") as post:
            post.return_value = MagicMock(status_code=200)
            ps.send(UNIFIED, {"type": typ})
            got[typ] = post.call_args.kwargs["headers"]["Urgency"]
    assert got == {"call": "high", "dm": "normal"}


def test_a_gone_endpoint_is_reported_for_pruning():
    """False is the caller's signal to delete the row. Returning True for a dead endpoint means we
    retry it forever; returning False for a transient blip loses a real device."""
    for code, expect in ((410, False), (404, False), (500, True), (200, True)):
        with patch.object(ps.requests, "post") as post:
            post.return_value = MagicMock(status_code=code)
            assert ps.send(UNIFIED, {}) is expect, f"status {code}"


def test_a_network_error_keeps_the_subscription():
    with patch.object(ps.requests, "post", side_effect=OSError("no route")):
        assert ps.send(UNIFIED, {}) is True, "a transient failure must not delete someone's device"


def test_a_nonsense_endpoint_is_not_posted():
    with patch.object(ps.requests, "post") as post:
        assert ps.send({"endpoint": "javascript:alert(1)", "keys": {}}, {}) is True
    assert not post.called

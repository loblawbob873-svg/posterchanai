"""Discover → Server Stats → Fediverse: the PUBLIC read of the delivery worker's `pcai:ap:stats`.

Owner: "serverstats should probably show the fediverse queue too". Server Stats is open to anyone,
logged out included, while the stats document is an operator document that carries inbox hosts in
`last_error` ("HTTP 500 from mastodon.social") and exception text that can quote a URL. So the endpoint
is an ALLOWLIST: the numbers, timestamps and an error CLASS, nothing else -- even when the document
holds more. And a document that could not be read is "unknown" (503), never a page of zeros, which
on a delivery panel reads as "caught up, nothing queued".
"""
import json
import time

import pytest

from app.services import nostr_store
from app.services.activitypub import health
from tests.test_activitypub import run
from tests.test_activitypub import world  # noqa: F401  (fixture)

HOUR = 3600
# Things that must never reach a public page, planted in every field the worker could write them to.
SECRETS = ("mastodon.social", "evil.example", "x.social", "https://", "http://", "/users/", "inbox",
           "a1a1a1a1", "secret post text")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(health, "_public_cache", {"at": 0.0, "data": None, "ok": False}, raising=False)


def _get():
    from app.routers.client import client_federation
    resp = run(client_federation())
    return resp.status_code, json.loads(resp.body)


def _leaky_doc(now):
    return {"delivered": 4210, "failed": 37, "queued": 12, "deleting": 3, "at": now - 40,
            "last_error": "HTTP 500 from mastodon.social",
            "inbox": "https://mastodon.social/inbox", "hosts": {"x.social": 4},
            "actor": "https://poster.test/ap/users/" + "a1" * 32, "content": "secret post text",
            "alerted": True, "stall_since": now - 2 * HOUR,
            "health": {"state": "behind", "lag": 2 * HOUR, "fails": 2, "per_min": 6.5, "checked": now - 40,
                       "oldest_unsent": now - 2 * HOUR,
                       "error": "send failed: ConnectError to https://evil.example/inbox for https://x.social/users/bob"}}


def test_the_endpoint_serves_the_documents_numbers_and_nothing_that_names_anyone(world):
    now = int(time.time())
    world["docs"][health.DOC] = _leaky_doc(now)
    status, body = _get()
    assert status == 200, body
    assert body["state"] == "behind"
    assert (body["lag"], body["per_min"], body["queued"], body["failed"], body["deleting"], body["fails"]) == \
        (2 * HOUR, 6.5, 12, 37, 3, 2)
    assert body["last_pass"] == now - 40
    assert body["lag_alert"] == health.LAG_ALERT and body["fails_alert"] == health.FAILS_ALERT
    assert body["error_class"] == "ConnectError"
    text = json.dumps(body)
    leaked = [s for s in SECRETS if s in text]
    assert not leaked, f"the public payload carries {leaked}: {text}"


def test_an_http_error_is_reduced_to_its_status(world):
    now = int(time.time())
    doc = _leaky_doc(now)
    doc["health"]["error"] = ""
    world["docs"][health.DOC] = doc
    status, body = _get()
    assert status == 200 and body["error_class"] == "HTTP 500"
    assert "mastodon" not in json.dumps(body)


def test_an_unreadable_document_is_unknown_never_zero(world, monkeypatch):
    async def down(*a, **k):
        raise ConnectionError("relay did not answer")
    monkeypatch.setattr(nostr_store, "get_doc", down)
    status, body = _get()
    assert status == 503, body
    assert body.get("state") == "unknown"
    for k in ("lag", "queued", "failed", "deleting", "fails", "per_min", "last_pass"):
        assert k not in body, f"an unreadable report still answered {k}={body.get(k)!r}"
    # and asking again inside the failure window does not turn into zeros either
    status, body = _get()
    assert status == 503 and "queued" not in body


def test_no_report_yet_is_unknown_not_caught_up(world):
    world["docs"].pop(health.DOC, None)
    status, body = _get()
    assert status == 200 and body["state"] == "unknown"
    assert "lag" not in body and "queued" not in body


def test_the_read_is_cached_for_every_viewer(world, monkeypatch):
    now = int(time.time())
    world["docs"][health.DOC] = _leaky_doc(now)
    calls = []
    real = nostr_store.get_doc

    async def counted(*a, **k):
        calls.append(a)
        return await real(*a, **k)
    monkeypatch.setattr(nostr_store, "get_doc", counted)
    for _ in range(5):
        assert _get()[0] == 200
    assert len(calls) == 1, f"{len(calls)} relay reads for 5 viewers"


def test_federation_off_says_off(world, monkeypatch):
    from app.services.activitypub import config
    monkeypatch.setattr(config, "enabled", lambda: False)
    status, body = _get()
    assert status == 200 and body["state"] == "off" and "queued" not in body

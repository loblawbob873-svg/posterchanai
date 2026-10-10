"""A refused event says WHICH part is wrong (2026-10-10).

Vyram's app showed "invalid: bad id or signature" for kinds 1, 5, 7 and 31234, while every one of his 698
events on public relays verifies here. "id or signature" cannot tell an event CHANGED AFTER SIGNING (its id no
longer matches its content -- something re-serialized it on the way) from one SIGNED WRONG, and those point at
different software. The reason now names it, for the person's app and for our log alike."""
from app.services.nostr import bip340
from app.services.nostr.event import build_event, invalid_reason, verify_event

SK = bytes([7]) * 32


def test_a_valid_event_has_no_reason():
    ev = build_event(SK, 1, "hello")
    assert verify_event(ev) and invalid_reason(ev) is None


def test_content_changed_after_signing_is_named():
    ev = build_event(SK, 1, "hello")
    ev["content"] = "hello!"
    assert "does not match" in invalid_reason(ev)


def test_a_bad_signature_is_named():
    ev = build_event(SK, 1, "hello")
    other = build_event(bytes([8]) * 32, 1, "x")
    ev["sig"] = other["sig"]
    r = invalid_reason(ev)
    assert r.startswith("invalid:") and "signature" in r and "does not match" not in r


def test_a_malformed_event_is_named():
    assert "malformed" in invalid_reason({"id": "x", "kind": 1})


def test_the_relay_sends_the_specific_reason():
    import inspect
    from app.services.nostr_relay import server
    src = inspect.getsource(server.RelayServer._on_event)
    assert "invalid_reason(ev)" in src

"""THE ONE FUNCTION EVERY SERVER→USER NOTIFICATION GOES THROUGH.

`system_dm.py` had ZERO test references. It is 48 lines and a single `send()`, and it carries the
notifications that exist precisely because something else has gone wrong: the paid-retention lapse
warning sent 7 days before a subscriber's posts are deleted, the uptime up→down alert, the bridge
access grant. CLAUDE.md is explicit about the cost of losing one of those:

    marking first lets one transient publish failure swallow the only warning a subscriber gets
    before their posts are deleted.

Its contract is unusually strict for its size, and all four parts are the kind that rot silently:

  * **It must never raise.** "a failed notification must not break its caller" — the callers are
    schedulers and request handlers doing something else. An exception escaping here takes out the
    payment that was being credited, not just the DM about it.
  * **Every refusal must be reported as `False`.** Callers key their retry/marker logic on the
    return value; a `None` from a forgotten branch is falsy by luck rather than by contract.
  * **The LOCAL relay only.** `publish_event(port, ...)` with the node's own relay port — it
    federates outward from there. That is the rule every publisher in this codebase follows.
  * **The DM body must never reach a log.** These carry notification content addressed to one
    person; the module logs a truncated pubkey and nothing else.

`nip17.wrap` is NOT stubbed out of the happy-path test: a notification that publishes something
other than a sealed NIP-17 wrap would be readable by the relay operator.
"""
import asyncio

import pytest

from app.services import system_dm


HEX = "a" * 64
NSEC = "nsec1" + "q" * 58


@pytest.fixture
def wired(monkeypatch):
    """A working node. Individual tests break one piece at a time."""
    from app.services import keystore, settings_store
    from app.services.nostr import nostr_service
    from app.services import nostr_store

    published = []

    async def _publish(port, event):
        published.append((port, event))
        return True, ""

    monkeypatch.setattr(nostr_service, "to_pubkey_hex", lambda r: HEX)
    monkeypatch.setattr(nostr_service, "decode_seckey", lambda n: b"\x01" * 32)
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: NSEC)
    monkeypatch.setattr(settings_store, "get_int", lambda key, default=0: 3052)
    monkeypatch.setattr(nostr_store, "publish_event", _publish)
    return published


def _send(recipient="npub1someone", text="your subscription lapses in 7 days"):
    return asyncio.run(system_dm.send(recipient, text))


# --------------------------------------------------------------------------- the happy path


def test_a_notification_is_published_to_the_local_relay(wired):
    assert _send() is True
    assert len(wired) == 1
    port, event = wired[0]
    assert port == 3052, "the DM must go to this node's own relay, not a remote one"


def test_what_is_published_is_a_sealed_nip17_wrap(wired):
    """Kind 1059 is the gift wrap. Publishing the plaintext kind-14 — or a kind-4 — would put the
    notification's contents in front of anybody reading the relay."""
    _send(text="your subscription lapses in 7 days")
    _port, event = wired[0]
    assert event.get("kind") == 1059, f"expected a NIP-17 gift wrap, got kind {event.get('kind')}"
    assert "lapses in 7 days" not in str(event), "the DM body was published in the clear"


def test_the_relay_port_is_read_from_settings_not_hardcoded(monkeypatch, wired):
    """A node running its relay on another port would otherwise publish into nothing and report
    success — the failure mode this whole file exists to prevent."""
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "get_int", lambda key, default=0: 4444)
    _send()
    assert wired[0][0] == 4444


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("recipient,text", [
    ("", "hello"), (None, "hello"), ("npub1someone", ""), ("npub1someone", None),
    ("", ""),
])
def test_an_empty_recipient_or_body_is_refused_without_touching_the_relay(wired, recipient, text):
    assert asyncio.run(system_dm.send(recipient, text)) is False
    assert wired == [], "a relay publish was attempted for an empty notification"


def test_an_unusable_recipient_is_refused(monkeypatch, wired):
    from app.services.nostr import nostr_service
    monkeypatch.setattr(nostr_service, "to_pubkey_hex", lambda r: None)
    assert _send("not-an-npub") is False
    assert wired == []


def test_no_operator_key_yet_is_refused_rather_than_crashing(monkeypatch, wired):
    """A node that has not minted its operator key must not raise on every notification during
    startup — the key appears later and the next one goes out."""
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: None)
    assert _send() is False
    assert wired == []


def test_an_undecodable_operator_key_is_refused(monkeypatch, wired):
    from app.services.nostr import nostr_service
    monkeypatch.setattr(nostr_service, "decode_seckey", lambda n: None)
    assert _send() is False
    assert wired == []


def test_a_relay_that_rejects_the_event_returns_false(monkeypatch, wired):
    from app.services import nostr_store

    async def _refuse(port, event):
        return False, "blocked: not on the whitelist"

    monkeypatch.setattr(nostr_store, "publish_event", _refuse)
    assert _send() is False


# --------------------------------------------------------------------------- never raises


@pytest.mark.parametrize("broken", ["to_pubkey_hex", "decode_seckey", "get_operator_nsec",
                                    "get_int", "publish_event"])
def test_an_exception_anywhere_is_swallowed_and_reported_as_false(monkeypatch, wired, broken):
    """"Never raises: a failed notification must not break its caller." The callers are schedulers
    and request handlers in the middle of doing something else — crediting a payment, recording an
    outage — and an escaping exception costs THAT, not just the DM."""
    from app.services import keystore, settings_store, nostr_store
    from app.services.nostr import nostr_service

    def _boom(*a, **kw):
        raise RuntimeError("something broke")

    async def _aboom(*a, **kw):
        raise RuntimeError("something broke")

    target = {
        "to_pubkey_hex": (nostr_service, _boom),
        "decode_seckey": (nostr_service, _boom),
        "get_operator_nsec": (keystore, _boom),
        "get_int": (settings_store, _boom),
        "publish_event": (nostr_store, _aboom),
    }[broken]
    monkeypatch.setattr(target[0], broken, target[1])

    assert _send() is False, f"a failure in {broken} did not come back as False"


def test_the_return_value_is_always_a_real_bool(wired):
    """Callers branch on it and some persist a marker on the strength of it, so a bare falsy value
    from a forgotten branch is a contract nobody checked."""
    assert isinstance(_send(), bool)
    assert isinstance(asyncio.run(system_dm.send("", "")), bool)


# --------------------------------------------------------------------------- privacy


def test_the_message_body_never_reaches_a_log(monkeypatch, caplog):
    """These DMs carry notification content addressed to one person, and this repo's rule is that
    what a user typed — or is told — stays out of the logs. Every branch is exercised, because the
    one that logs the body is by definition the failure branch nobody reads until it fires."""
    from app.services import keystore, settings_store, nostr_store
    from app.services.nostr import nostr_service

    secret = "SENSITIVE-NOTIFICATION-BODY"
    caplog.set_level("DEBUG")

    async def _refuse(port, event):
        return False, "rejected"

    monkeypatch.setattr(nostr_service, "to_pubkey_hex", lambda r: HEX)
    monkeypatch.setattr(nostr_service, "decode_seckey", lambda n: b"\x01" * 32)
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: NSEC)
    monkeypatch.setattr(settings_store, "get_int", lambda key, default=0: 3052)
    monkeypatch.setattr(nostr_store, "publish_event", _refuse)
    asyncio.run(system_dm.send("npub1someone", secret))

    # ...and the exception branch, which formats the most and is the least often read.
    async def _boom(port, event):
        raise RuntimeError("relay exploded")

    monkeypatch.setattr(nostr_store, "publish_event", _boom)
    asyncio.run(system_dm.send("npub1someone", secret))

    assert secret not in caplog.text, "the notification body was written to the log"


def test_only_a_truncated_pubkey_is_logged(monkeypatch, caplog):
    """The full recipient key is an identity; the module deliberately logs `recipient[:16]`."""
    from app.services.nostr import nostr_service
    caplog.set_level("DEBUG")
    monkeypatch.setattr(nostr_service, "to_pubkey_hex", lambda r: None)
    npub = "npub1" + "z" * 58
    asyncio.run(system_dm.send(npub, "anything"))
    assert npub not in caplog.text

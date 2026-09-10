"""A TOGGLE IS ONLY REAL IF EVERY SENDER READS IT.

"i am still getting push notifications for email despite having that turned off", said repeatedly,
across app builds. The setting was mirrored to the subscription row correctly the whole time —
MEASURED on the live node, row id=15 held
`{"email": false, "likes": false, ..., "concord": true}` — and the email sender never looked at it:

    rows = db.query(PushSubscription).filter(PushSubscription.pubkey == pk).all()
    payload = {"title": title, "body": body, "type": "mail"}
    for row in rows:
        push_service.send(sub, payload)          # no pref check, ever

`push_prefs` was imported ZERO times by `mail_notify_service` and ZERO times by `reminder_service`.
Only `nostr_push_service` filtered. So no app build could ever have fixed this: the client did its
half and the server threw the answer away.

THE SECOND HALF, which would have made a filter that looked right do nothing: the toggle is named
`email` while the PAYLOAD is typed `mail` (that string is the client's deep-link routing, not a
preference name), and `allows()` returns True for any name no toggle governs — so filtering on the
payload type would have silently allowed everything.

This test is written against the SENDERS, not against one of them, because the failure is a sender
that forgets — which is what a new sender does by default.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "app" / "services"

# Every module that pushes to PushSubscription rows, and the toggle each one is governed by.
SENDERS = {
    "mail_notify_service.py": "email",
    "reminder_service.py": "reminders",
    "nostr_push_service.py": None,      # classifies per event via push_type(); checked separately
    "direct_push_service.py": None,     # the transport, not a decision
}


def _senders_on_disk():
    """Any module that queries PushSubscription is a sender and must appear above."""
    found = set()
    for f in SERVICES.glob("*.py"):
        if "PushSubscription" in f.read_text(encoding="utf-8"):
            found.add(f.name)
    return found - {"push_prefs.py"}


def test_no_sender_exists_that_this_test_does_not_know_about():
    """The whole failure mode is a sender nobody remembered to gate."""
    unknown = _senders_on_disk() - set(SENDERS)
    assert not unknown, (
        f"new push sender(s) {sorted(unknown)} — add them here and make them call "
        "push_prefs.allows_row, or the toggles are decorative for whatever they send")


def test_the_email_sender_reads_the_email_toggle():
    src = (SERVICES / "mail_notify_service.py").read_text(encoding="utf-8")
    assert 'push_prefs.allows_row(row, "email")' in src, src[-600:]


def test_the_reminder_sender_reads_the_reminders_toggle():
    src = (SERVICES / "reminder_service.py").read_text(encoding="utf-8")
    assert 'push_prefs.allows_row(row, "reminders")' in src


def test_a_gated_sender_checks_before_it_sends():
    """A check after the send is not a check."""
    for name, toggle in SENDERS.items():
        if toggle is None:
            continue
        src = (SERVICES / name).read_text(encoding="utf-8")
        guard = src.index(f'allows_row(row, "{toggle}")')
        send = src.index("push_service.send", guard - 4000 if guard > 4000 else 0)
        assert guard < src.index("push_service.send", guard), f"{name}: sends before it checks"


def test_the_toggle_name_is_not_the_payload_type():
    """`allows()` returns True for a name no toggle governs, so filtering on the payload type
    ('mail') would look like a filter and allow everything."""
    from app.services import push_prefs
    assert "email" in push_prefs.PUSH_TYPES
    assert "mail" not in push_prefs.PUSH_TYPES
    assert push_prefs.allows({"email": False}, "mail") is True     # the trap
    assert push_prefs.allows({"email": False}, "email") is False   # the rule


def test_an_unset_row_still_fails_open():
    """Deliberate: a device that never configured anything keeps the old behaviour. This is why a
    missing filter was invisible — NULL prefs and 'no filter at all' look identical from outside."""
    from app.services import push_prefs
    assert push_prefs.allows(None, "email") is True
    assert push_prefs.allows("", "email") is True

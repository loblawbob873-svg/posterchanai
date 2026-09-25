"""A NEW incoming email is its own conversation -- not every older mail that shares its subject.

Reported: "the latest email from Dustin Darcy -- when I open it, I see a bunch of old emails above
it". Measured on that mailbox: the new message is a ROOT (subject "Payroll", no In-Reply-To, no
References), the header graph links nothing to it, and `_build_thread` then fell back to the subject
and returned 86 messages back to 2022 -- every "Re: Payroll" and every sent "Payroll". The shapes
below are that mailbox's, in miniature.
"""
from app.routers.mail import _build_thread


def msg(uid, folder="INBOX", subject="Payroll", mid="", irt="", refs="", ts=0):
    return {"uid": str(uid), "folder": folder, "subject": subject, "message_id": mid,
            "in_reply_to": irt, "references": refs, "ts": ts}


DAY = 86400
T0 = 1_790_000_000                      # "today"
OLD = [
    msg("a1", subject="Payroll", mid="<2023-root@them>", ts=T0 - 800 * DAY),
    msg("a2", subject="Re: Payroll", mid="<2023-re@them>", irt="<2023-root@them>", ts=T0 - 799 * DAY),
    msg("a3", folder="INBOX.Sent", subject="Re: Payroll", ts=T0 - 798 * DAY),        # ours, pre-ID era
    msg("a4", subject="Re: Payroll", mid="<2024-re@them>", refs="<gone@mta>", ts=T0 - 400 * DAY),
    msg("a5", folder="Sent", subject="Payroll", mid="<2025-ours@us>", ts=T0 - 60 * DAY),  # ours, isolated
]


def test_a_new_incoming_email_opens_on_its_own():
    new = msg("n1", subject="Payroll", mid="<2026-new@them>", ts=T0)
    got = [m["uid"] for m in _build_thread(new, OLD + [new])]
    assert got == ["n1"], f"a new email inherited an old conversation: {got}"


def test_our_reply_to_it_still_joins_even_without_a_message_id():
    new = msg("n1", subject="Payroll", mid="<2026-new@them>", ts=T0)
    ours = msg("n2", folder="INBOX.Sent", subject="Re: Payroll", ts=T0 + 3600)           # idless, AFTER it
    got = [m["uid"] for m in _build_thread(new, OLD + [new, ours])]
    assert got == ["n1", "n2"], got


def test_a_real_reply_to_it_joins_through_its_headers():
    new = msg("n1", subject="Payroll", mid="<2026-new@them>", ts=T0)
    rep = msg("n2", subject="Re: Payroll", mid="<2026-re@them>", irt="<2026-new@them>", ts=T0 + 7200)
    assert [m["uid"] for m in _build_thread(new, OLD + [new, rep])] == ["n1", "n2"]
    assert [m["uid"] for m in _build_thread(rep, OLD + [new, rep])] == ["n1", "n2"]


def test_a_reply_whose_parent_is_missing_still_repairs_by_subject():
    """The fallback's real job survives: a reply claims a parent, so a subject match is a repair."""
    got = {m["uid"] for m in _build_thread(OLD[3], OLD)}
    assert {"a4", "a2"} <= got, got

"""A ROUTINE SYNC HAS TO MIRROR SENT MAIL, OR HALF EVERY CONVERSATION IS MISSING.

Asked about roughly ten times, ending with "i asked you probably 10 times to fix email right about
including the sent conversation in the thread view" and then, decisively, a named thread:
"look for RE: Information about your DoorDash Inquiry and you can see it's missing the messages I
sent too".

Every previous round fixed something real in the THREADING — sent-folder recognition, the 5000-doc
scan cap, a seed that could not be found, a cache that never hit, a headerless singleton, the wrong
isolation test. All of them were necessary. None of them could have fixed this, because the data
they thread over did not contain the replies.

Both routine callers asked for one folder:

    app/routers/mail.py                    sync_all(db, current_user, folders=["INBOX"])
    app/services/mail_notify_service.py    sync_all(db, user,         folders=["INBOX"])

So Sent was only ever mirrored by an explicit full sync. Measured on the reporting mailbox:

    INBOX          39 messages   newest 2026-09-02   (that day)
    INBOX.Sent    807 messages   newest 2026-08-30   (three days stale)

and in the named thread, the three "Information about your DoorDash Inquiry" messages had arrived
that morning while the user's replies were not in the mailbox at all. The thread builder was working
perfectly on a mailbox that was missing one side of the conversation.

The folder name is READ, never guessed: this one account alone carries `INBOX.Sent`, `Sent`,
`Sent Messages` and `sent-mail`.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.services import mail_sync

ROOT = Path(__file__).resolve().parents[1]


def test_the_routine_callers_no_longer_ask_for_inbox_only():
    """THE BUG, named at both call sites."""
    for rel in ("app/routers/mail.py", "app/services/mail_notify_service.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert 'sync_all(db, current_user, folders=["INBOX"])' not in src, rel
        assert 'sync_all(db, user, folders=["INBOX"])' not in src, rel
        assert "mail_sync.ESSENTIAL" in src, f"{rel} no longer requests the essential folders"


def test_essential_is_inbox_plus_this_accounts_own_sent_folder():
    assert mail_sync._essential_folders({"sent": "INBOX.Sent"}) == ["INBOX", "INBOX.Sent"]
    assert mail_sync._essential_folders({"sent": "Sent Messages"}) == ["INBOX", "Sent Messages"]


def test_every_sent_folder_is_mirrored_not_only_the_flagged_one():
    """The flagged folder alone was still not enough. One real account carries all three, written by
    different clients over the years — INBOX.Sent (381), Sent (7), Sent Messages (52) — so mirroring
    only the flagged one leaves 59 of that person's own replies out of every conversation."""
    got = mail_sync._essential_folders(
        {"sent": "INBOX.Sent"},
        ["INBOX", "INBOX.Sent", "Sent", "Sent Messages", "Trash", "INBOX.Archive"])
    assert got == ["INBOX", "INBOX.Sent", "Sent", "Sent Messages"]


def test_the_flagged_folder_comes_first():
    """Order is not cosmetic: it is the folder a sent copy is SAVED to, so it should be mirrored
    first when a sync is interrupted."""
    got = mail_sync._essential_folders({"sent": "Sent Messages"}, ["INBOX", "Sent", "Sent Messages"])
    assert got[1] == "Sent Messages"


@pytest.mark.parametrize("name", ["Sent", "INBOX.Sent", "sent-mail", "Sent Messages",
                                  "Sent Items", "SENTBOX", "INBOX/Sent"])
def test_the_names_clients_actually_use_are_recognised(name):
    assert mail_sync._is_sent_leaf(name) is True


@pytest.mark.parametrize("name", ["Consent", "Unsent drafts", "Sent by Alice", "Archive",
                                  "INBOX", "Trash", "Presentations", ""])
def test_a_folder_that_merely_contains_the_word_is_not_a_sent_folder(name):
    """Substring matching would sweep in Consent, Unsent and somebody's 'Sent by Alice' — and each
    of those is a whole extra folder on a five-minute timer."""
    assert mail_sync._is_sent_leaf(name) is False


def test_a_pathological_mailbox_cannot_turn_this_into_a_full_sync():
    many = ["INBOX"] + [f"Sent{i}" for i in range(50)] + ["Sent", "Sent Messages", "Sent Items",
                                                          "sent-mail", "Sentbox"]
    got = mail_sync._essential_folders({"sent": "Sent"}, many)
    assert len(got) <= mail_sync._MAX_SENT_FOLDERS + 1


def test_the_sent_folder_name_is_read_not_guessed():
    """The reporting mailbox carries INBOX.Sent, Sent, Sent Messages AND sent-mail. A hardcoded
    "Sent" would mirror the wrong (or an empty) folder on most of those accounts."""
    src = inspect.getsource(mail_sync._essential_folders)
    assert '"sent"' in src or "'sent'" in src
    assert not re.search(r'\[\s*["\']INBOX["\']\s*,\s*["\']Sent["\']\s*\]', src), \
        "the Sent folder name is hardcoded again"


def test_an_account_with_no_known_sent_folder_still_syncs_inbox():
    """Never return an empty list: that would sync nothing at all, which is worse than the bug."""
    assert mail_sync._essential_folders({}) == ["INBOX"]
    assert mail_sync._essential_folders({"sent": ""}) == ["INBOX"]
    assert mail_sync._essential_folders(None) == ["INBOX"]


def test_a_sent_folder_that_is_the_inbox_is_not_synced_twice():
    assert mail_sync._essential_folders({"sent": "INBOX"}) == ["INBOX"]
    assert mail_sync._essential_folders({"sent": "inbox"}) == ["INBOX"]


def test_it_does_not_quietly_become_a_full_sync():
    """Trash (11,565) and Archive (2,717) are a different order of cost and nothing needs them on a
    five-minute timer. Two folders is the point."""
    assert len(mail_sync._essential_folders({"sent": "INBOX.Sent"})) == 2


def test_sync_all_understands_the_sentinel():
    """A sentinel that no branch reads would silently sync every folder — the opposite mistake."""
    src = inspect.getsource(mail_sync.sync_all)
    assert "folders == ESSENTIAL" in src
    assert "_essential_folders(meta, _all)" in src


def test_the_sentinel_cannot_collide_with_a_real_folder_name():
    """It travels in the same argument as a folder list, so it must not be a name a server could
    plausibly use."""
    assert mail_sync.ESSENTIAL.startswith("__") and mail_sync.ESSENTIAL.endswith("__")


def test_an_explicit_folder_list_still_works():
    """sync_one and any caller naming folders must be unaffected."""
    src = inspect.getsource(mail_sync.sync_all)
    assert "elif folders is not None:" in src, "an explicit folder list no longer takes its own path"
    assert "flist, meta = folders, {}" in src

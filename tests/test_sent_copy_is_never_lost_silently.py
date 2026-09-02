"""A SENT MESSAGE MUST BE FILED, AND A FAILURE TO FILE IT MUST BE LOUD.

The real answer to "you didn't fix emails conversations mode showing sent items in thread", asked
about ten times. Every previous round fixed the THREADING, and the threading was fine. The user's
replies were not in the mailbox because they were never saved anywhere.

Measured on the reporting instance's journal:

    9x  "Sent email from yummy@... to ...: RE: Information about your DoorDash Inquiry"   (3 days)
    0x  "Saved sent email to ..."                                       (last one: Aug 31 08:59)

SMTP delivered every message. The IMAP append that files the copy failed every time, and the loop
was written so that filing NOTHING logged nothing at all — no success line, no error — while
send_email still returned True. Three separate defects fed it:

  1. `_sent_folder_candidates` took "the last quoted field" of a LIST reply as the mailbox name. On
     a server that quotes the DELIMITER and not the name, that is `'.'` — and `SELECT "."` answers
     NO. Measured: candidates came back `['.', 'Sent Messages', ...]`.
  2. A server may flag more than one mailbox `\\Sent`. This account has both `Sent` (7 messages,
     months stale) and `INBOX.Sent` (381, the one the app displays), and the save picked whichever
     LIST mentioned first while the list view resolved the other — "delivered, filed, and
     invisible", which this module's own comment already warned about.
  3. Nothing anywhere reported the failure.

The last line of defence is that the thread view reads OUR mailbox, not the IMAP server: when no
folder accepts the copy it is kept locally, so a user's own half of a conversation cannot be lost to
a server that refused it.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.services import mail_service

ROOT = Path(__file__).resolve().parents[1]


def _parse(line: str):
    """The shipped LIST-name parser, lifted from _sent_folder_candidates."""
    src = inspect.getsource(mail_service._sent_folder_candidates)
    pat = re.search(r"m = re\.match\(r'(.+?)', line\)", src)
    assert pat, "the LIST parse has moved — re-read this test"
    m = re.match(pat.group(1).replace("\\\\", "\\"), line)
    if not m:
        return None
    name = m.group(2).strip()
    return name[1:-1] if len(name) >= 2 and name[0] == '"' and name[-1] == '"' else name


@pytest.mark.parametrize("line,expect", [
    ('(\\HasNoChildren \\Sent) "." "INBOX.Sent"', "INBOX.Sent"),
    ('(\\HasNoChildren \\Sent) "." Sent Messages', "Sent Messages"),   # THE BUG: gave '.'
    ('(\\HasNoChildren \\Sent) NIL "Sent"', "Sent"),
    ('(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"', "[Gmail]/Sent Mail"),
])
def test_the_mailbox_name_is_parsed_not_the_delimiter(line, expect):
    assert _parse(line) == expect


def test_the_delimiter_is_never_returned_as_a_folder():
    """`SELECT "."` answers NO, and the loop then moved on with nothing logged."""
    assert _parse('(\\HasNoChildren \\Sent) "." Sent Messages') != "."


def test_the_save_prefers_the_folder_the_list_displays():
    """Two flagged mailboxes, and the halves must not disagree — that is a message the user can only
    find by searching."""
    class FakeImap:
        def list(self):
            return "OK", [b'(\\HasNoChildren \\Sent) "." "Sent"',
                          b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']
    got = mail_service._sent_folder_candidates(FakeImap(), "INBOX.Sent")
    assert got[0] == "INBOX.Sent", f"the save would file into {got[0]!r} while the list reads INBOX.Sent"


def test_without_a_preference_it_still_offers_the_flagged_ones():
    class FakeImap:
        def list(self):
            return "OK", [b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']
    got = mail_service._sent_folder_candidates(FakeImap(), "")
    assert "INBOX.Sent" in got


def test_send_email_accepts_the_resolved_folder():
    assert "sent_folder" in inspect.signature(mail_service.send_email).parameters


def test_a_reply_passes_it_through():
    """A reply is the commonest way a sent copy goes missing — it is what the DoorDash thread was."""
    src = inspect.getsource(mail_service.reply_to_message)
    assert "sent_folder=" in src, "reply_to_message no longer tells send_email where Sent is"
    assert "list_special_folders(" in src


def test_a_copy_that_could_not_be_filed_is_reported():
    """THE SILENCE, which is what made this survive ten rounds of investigation."""
    src = inspect.getsource(mail_service.send_email)
    assert "SENT COPY NOT FILED" in src
    assert "logger.error(" in src.split("if not saved_to:")[1][:400]


def test_every_attempt_is_named_in_that_report():
    """"It failed" is not enough to act on; which folders were tried, and what each said, is."""
    src = inspect.getsource(mail_service.send_email)
    assert "tried.append" in src
    assert "SELECT" in src and "APPEND" in src


def test_the_per_folder_failure_is_no_longer_swallowed_bare():
    src = inspect.getsource(mail_service.send_email)
    block = src[src.index("for folder in sent_folders:"):src.index("if not saved_to:")]
    assert "except Exception:\n                        continue" not in block, (
        "a per-folder failure is being swallowed with no record again")


def test_a_refused_copy_is_kept_in_our_own_mailbox():
    """The thread view reads OUR mailbox. If the server will not file it, keeping it here is the
    difference between a complete conversation and half of one."""
    src = inspect.getsource(mail_service.send_email)
    assert "_mirror_sent_locally(" in src
    helper = inspect.getsource(mail_service._mirror_sent_locally)
    assert "store_message" in helper


def test_the_local_copy_is_only_made_on_failure():
    """When the append works, the ordinary sync mirrors that copy under its real IMAP uid; storing
    it twice would show the same message twice in one conversation."""
    src = inspect.getsource(mail_service.send_email)
    assert src.index("if not saved_to:") < src.index("_mirror_sent_locally(")


def test_the_local_copy_cannot_break_sending():
    """It runs after delivery. Nothing it can do is worth raising into the caller."""
    helper = inspect.getsource(mail_service._mirror_sent_locally)
    assert "except Exception" in helper


def test_the_local_uid_is_stable_for_the_same_message():
    """Derived from the Message-ID, so a retry cannot duplicate it."""
    helper = inspect.getsource(mail_service._mirror_sent_locally)
    assert "sha256" in helper and "message" in helper.lower()

"""MAIL: ARCHIVING A BATCH IS ONE IMAP SESSION, AND A MOVE IS `UID MOVE`.

"Mail performance for moving to archive is slow." Measured 2026-10-14 against the real mail server
(Dovecot at mxrouting): every archived message paid its own TLS connect + login (~0.4 s), SELECT,
SEARCH, a LIST of every folder, COPY, STORE, EXPUNGE and LOGOUT -- about 1 s per message, in series,
so archiving 30 messages took half a minute. The server offers MOVE and UIDPLUS once logged in
(imaplib only keeps the PRE-login greeting's capabilities, which do not list them).

The shipped `mail_service` is driven against a fake IMAP connection that records every command.
"""
from types import SimpleNamespace

import pytest

from app.services import mail_service as M

POST_LOGIN = b"IMAP4rev1 UIDPLUS MOVE SPECIAL-USE"


class FakeIMAP:
    def __init__(self, held, caps=POST_LOGIN, log=None):
        self.held, self.caps, self.log = set(held), caps, log if log is not None else []

    def socket(self):
        return None

    def select(self, folder):
        self.log.append(("SELECT", folder))
        return "OK", [str(len(self.held)).encode()]

    def capability(self):
        self.log.append(("CAPABILITY",))
        return "OK", [self.caps]

    def list(self):
        self.log.append(("LIST",))
        return "OK", [b'(\\HasNoChildren) "." INBOX.Archive', b'(\\HasChildren) "." INBOX']

    def create(self, name):
        self.log.append(("CREATE", name))
        return "OK", [b""]

    def uid(self, cmd, *args):
        self.log.append(("UID", cmd.upper()) + tuple(a for a in args if a is not None))
        if cmd.upper() == "SEARCH":
            want = args[-1].split(" ", 1)[1].split(",")
            return "OK", [" ".join(u for u in want if u in self.held).encode()]
        return "OK", [b""]

    def expunge(self):
        self.log.append(("EXPUNGE",))
        return "OK", [b""]

    def logout(self):
        self.log.append(("LOGOUT",))


@pytest.fixture
def wire(monkeypatch):
    state = {"connects": 0, "log": [], "held": {"3", "4", "5"}, "caps": POST_LOGIN}
    acc = SimpleNamespace(email="me@x.test")
    monkeypatch.setattr(M, "get_user_mail_accounts", lambda uid, db: [acc])
    monkeypatch.setattr(M, "_account_by_email", lambda uid, db, email: acc, raising=False)

    def connect(a):
        state["connects"] += 1
        return FakeIMAP(state["held"], state["caps"], state["log"])
    monkeypatch.setattr(M, "connect_imap", connect)
    return state


def _cmds(log, *names):
    return [x for x in log if x[0] in names or (x[0] == "UID" and x[1] in names)]


def test_archiving_a_batch_logs_in_once_and_moves_the_whole_set(wire):
    done, failed = M.archive_messages(1, None, "me@x.test", ["3", "4", "5"], "INBOX")
    assert sorted(done) == ["3", "4", "5"] and failed == []
    assert wire["connects"] == 1, ("a login per message", wire["connects"])
    moves = _cmds(wire["log"], "MOVE")
    assert moves == [("UID", "MOVE", "3,4,5", "INBOX.Archive")], wire["log"]
    assert not _cmds(wire["log"], "EXPUNGE", "COPY"), "MOVE is offered: nothing else is needed"
    assert len(_cmds(wire["log"], "LIST")) == 1


def test_a_message_the_folder_no_longer_holds_is_failed_not_archived(wire):
    done, failed = M.archive_messages(1, None, "me@x.test", ["3", "9", "x 1"], "INBOX")
    assert done == ["3"] and sorted(failed) == ["9", "x 1"]
    assert _cmds(wire["log"], "MOVE") == [("UID", "MOVE", "3", "INBOX.Archive")]


def test_without_move_the_expunge_is_scoped_to_the_archived_set(wire):
    """A plain EXPUNGE would also purge whatever ELSE the user had flagged \\Deleted in that folder."""
    wire["caps"] = b"IMAP4rev1 UIDPLUS"
    done, _ = M.archive_messages(1, None, "me@x.test", ["3", "4"], "INBOX")
    assert sorted(done) == ["3", "4"]
    assert ("UID", "COPY", "3,4", "INBOX.Archive") in wire["log"]
    assert ("UID", "EXPUNGE", "3,4") in wire["log"]
    assert ("EXPUNGE",) not in wire["log"]


def test_a_move_to_a_chosen_folder_is_uid_move_too(wire):
    assert M.move_message(1, None, "me@x.test", "3,4", "INBOX", "Trash") is True
    assert _cmds(wire["log"], "MOVE") == [("UID", "MOVE", "3,4", '"Trash"')]
    assert not _cmds(wire["log"], "EXPUNGE", "COPY")


def test_the_chat_command_form_still_archives_one(wire):
    assert M.archive_message(1, None, "me@x.test", "4", "INBOX") is True
    assert _cmds(wire["log"], "MOVE") == [("UID", "MOVE", "4", "INBOX.Archive")]

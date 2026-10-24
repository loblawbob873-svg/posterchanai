"""Mail → mark read / unread, Move to a folder, Save draft -- over the wire, against a real IMAP server on loopback.

A route-coverage scan (2026-10-24) found these three with no test at all. They run here through the node's REAL mail
router, mail_service (imaplib), mail_sync and mail_store, against tests/mail_wire_servers.py:

  * a message marked read STAYS read after the next sync (the flag lives in the encrypted mailbox, the server's
    copy is still unseen -- a sync that re-imported the server's flags would undo every click), and unread works;
  * Move really moves it ON THE SERVER, both on a server with RFC 6851 MOVE and on one without it (COPY + \\Deleted +
    EXPUNGE), the mailbox lists it in the new folder only, and moving it to a folder that does not exist says so and
    leaves it where it was;
  * a saved draft opens in Drafts with what was typed, and saving it again UPDATES that draft instead of adding one.
"""
import pytest

from tests.test_mail_wire_flows import ME, incoming, me, node  # noqa: F401  (fixtures)


def _deliver(node, subject):
    raw, _ = incoming(subject, 'hello\n')
    return str(node.wire.imap.deliver(raw))


def _row(node, me, folder, uid):
    code, page = node.call(me, 'GET', '/messages?account=%s&folder=%s' % (ME, folder))
    assert code == 200, page
    return next((m for m in page['messages'] if m['uid'] == uid), None)


def _subjects(node, me, folder):
    code, page = node.call(me, 'GET', '/messages?account=%s&folder=%s' % (ME, folder))
    return [m['subject'] for m in page.get('messages', [])] if code == 200 else []


def test_marking_a_message_read_sticks_through_the_next_sync_and_unread_works(node, me):
    uid = _deliver(node, 'Mark me read')
    assert node.call(me, 'POST', '/sync')[0] == 200
    assert _row(node, me, 'INBOX', uid)['read'] is False
    code, got = node.call(me, 'POST', '/mark-read', {'account': ME, 'folder': 'INBOX', 'uid': uid})
    assert code == 200 and got['ok'], got
    assert _row(node, me, 'INBOX', uid)['read'] is True
    assert node.call(me, 'POST', '/sync')[0] == 200
    assert _row(node, me, 'INBOX', uid)['read'] is True, 'the next sync put the message back to unread'
    code, got = node.call(me, 'POST', '/mark-read', {'account': ME, 'folder': 'INBOX', 'uid': uid, 'read': False})
    assert code == 200 and got['ok'] and _row(node, me, 'INBOX', uid)['read'] is False


@pytest.mark.parametrize('server_has_move', [True, False], ids=['MOVE', 'COPY+EXPUNGE'])
def test_move_moves_the_message_on_the_server_and_in_the_mailbox(node, me, server_has_move):
    imap = node.wire.imap
    caps = imap.caps
    imap.caps = 'IMAP4rev1 UIDPLUS SPECIAL-USE' + (' MOVE' if server_has_move else '')
    try:
        subject = 'Move me (%s)' % ('MOVE' if server_has_move else 'COPY')
        uid = _deliver(node, subject)
        keep = _deliver(node, subject + ' -- stays')
        assert node.call(me, 'POST', '/sync')[0] == 200
        before = len(imap.commands)
        code, got = node.call(me, 'POST', '/move', {'account': ME, 'folder': 'INBOX', 'uid': uid, 'dest': 'Trash'})
        assert code == 200 and got == {'ok': True, 'moved': True}, got
        sent = imap.commands[before:]
        assert ('UID MOVE' if server_has_move else 'UID COPY') in ' '.join(sent), sent
        inbox = [m[0] for m in imap.boxes['INBOX'].msgs]
        trash = [m[2] for m in imap.boxes['Trash'].msgs]
        assert int(uid) not in inbox, 'the message is still in the server INBOX'
        assert int(keep) in inbox, 'moving one message took another with it'
        assert any(subject.encode() in raw for raw in trash), 'the message did not arrive in the server Trash'
        assert _row(node, me, 'INBOX', uid) is None, 'the mailbox still lists the moved message in INBOX'
        # Opening Trash refreshes it, as the client does (other folders sync when opened, not on every /sync).
        code, got = node.call(me, 'POST', '/sync-folder', {'account': ME, 'folder': 'Trash'})
        assert code == 200, got
        assert subject in _subjects(node, me, 'Trash'), ('the moved message is not in Trash', _subjects(node, me, 'Trash'))
    finally:
        imap.caps = caps


def test_a_move_the_server_refuses_says_so_and_leaves_the_message(node, me):
    uid = _deliver(node, 'Nowhere to go')
    assert node.call(me, 'POST', '/sync')[0] == 200
    code, got = node.call(me, 'POST', '/move', {'account': ME, 'folder': 'INBOX', 'uid': uid, 'dest': 'No Such Folder'})
    assert code == 502, (code, got)
    assert int(uid) in [m[0] for m in node.wire.imap.boxes['INBOX'].msgs]
    assert _row(node, me, 'INBOX', uid) is not None, 'a refused move removed the message from the mailbox'
    code, got = node.call(me, 'POST', '/move', {'account': ME, 'folder': 'INBOX', 'uid': uid})
    assert code == 400, 'a move with no destination must be refused'


def test_a_saved_draft_opens_with_what_was_typed_and_saving_again_updates_it(node, me):
    draft = {'to': 'zoe@far.test', 'subject': 'Half-written', 'body': 'first thoughts'}
    code, got = node.call(me, 'POST', '/draft', {'account': ME, 'draft': draft})
    assert code == 200 and got['ok'], got
    uid = got['uid']
    code, msg = node.call(me, 'GET', '/message?account=%s&uid=%s&folder=Drafts' % (ME, uid))
    m = msg.get('message', msg)
    assert code == 200 and m['subject'] == 'Half-written' and 'first thoughts' in m['body_text'], m
    code, got = node.call(me, 'POST', '/draft', {'account': ME, 'draft': {**draft, 'uid': uid, 'body': 'second thoughts'}})
    assert code == 200 and got['uid'] == uid
    code, page = node.call(me, 'GET', '/messages?account=%s&folder=Drafts' % ME)
    mine = [x for x in page['messages'] if x['subject'] == 'Half-written']
    assert len(mine) == 1, ('saving a draft again added a second copy', mine)
    code, msg = node.call(me, 'GET', '/message?account=%s&uid=%s&folder=Drafts' % (ME, uid))
    assert 'second thoughts' in msg.get('message', msg)['body_text']

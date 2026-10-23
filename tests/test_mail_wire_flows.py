"""MAIL OVER THE WIRE: what the composer sends is what an SMTP server receives, and what an IMAP server holds is what
the mailbox shows -- through the node's REAL mail router, mail_service (smtplib/imaplib), mail_sync and mail_store on
the shipped relay, against a real SMTP server and a real IMAP server on loopback (tests/mail_wire_servers.py). The
request bodies are the ones static/js/client/mail.js builds (`gather()` + the reply/forward fields). No internet.

Held, each with the exact bytes that crossed the socket:
  * a new message: envelope = every address in To/Cc (two people in To used to be ONE `RCPT TO:<a@x, b@y>`,
    refused by every MTA), headers, a non-ASCII subject/body/attachment NAME decoding to what was typed
    (the name went out as an RFC 2047 word inside RFC 2231 and was shown as `=?utf-8?b?...?=`), attachment
    bytes, STARTTLS before AUTH, and the copy filed in Sent;
  * Bcc reaches the envelope and never the message (it used to be a header every recipient could read);
  * received mail (IMAP -> /sync -> the encrypted mailbox -> /messages, /message, attachment download);
  * reply/forward carry In-Reply-To/References; a reply sends the To/Cc/Subject the composer showed (they were
    ignored -- a Cc added to a reply got nothing); a forward does not quote the original twice;
  * every refusal is a failed send WITH the server's reason, nothing delivered: 5xx recipient, 5xx data, 5xx auth,
    connection refused, a TLS handshake that fails; one recipient refused of two is "sent" with that one named;
  * a slow server is waited for, not given up on.

Runtime ~10s. Fail-first: copy an older app/services/mail_service.py / app/routers/mail.py over the working tree.
"""

import asyncio
import base64
import shutil
import tempfile
import time
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from tests import mail_wire_harness as W
from tests.client import realnode_harness as R
from tests.mail_wire_servers import closed_port

ME = W.ACCOUNT


@pytest.fixture(scope='module')
def node():
    import httpx
    from fastapi import FastAPI, HTTPException, Request
    from app import auth as app_auth
    from app.database import get_db
    from app.routers import mail as mail_router
    from tests.member_fixtures import allow_keys

    mp = pytest.MonkeyPatch()
    tmp = tempfile.mkdtemp(prefix='pc-mailwire-')
    loop = R._Loop()
    home = R.Home(loop, tmp)
    users = [W.user(i) for i in range(1, 40)]
    by_pk = {u.nostr_npub: u for u in users}
    allow_keys(mp, *by_pk)
    wire = W.MailWire(mp, home.port, users)
    app = FastAPI()
    app.include_router(mail_router.router)

    def who(request: Request):
        pk = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
        if pk not in by_pk:
            raise HTTPException(401, 'not signed in')
        return by_pk[pk]
    app.dependency_overrides[app_auth.get_current_user] = who
    app.dependency_overrides[get_db] = lambda: None

    def call(u, method, path, body=None):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node',
                                         timeout=120) as c:
                r = await c.request(method, '/api/mail' + path, json=body,
                                    headers={'Authorization': 'Bearer ' + u.nostr_npub})
                return r.status_code, (r.json() if r.headers.get('content-type', '').startswith('application/json')
                                       else r.content)
        return asyncio.run(go())
    ns = type('Node', (), {})()
    ns.call, ns.wire, ns.users, ns.home = call, wire, iter(users), home
    yield ns
    wire.stop()
    home.close()
    mp.undo()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def me(node):
    """A fresh account with the mailbox configured, and a sink with nothing in it."""
    s = node.wire.smtp
    s.delivered.clear()
    s.sessions.clear()
    s.rcpt_reply = s.data_reply = s.auth_reply = None
    s.data_delay, s.tls = 0, 'ok'
    node.wire.imap.appended.clear()
    return next(node.users)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def sent_one(node):
    got = node.wire.smtp.wait(1)
    assert len(got) == 1, [d.rcpts for d in got]
    return got[0]


def body_of(msg):
    part = msg.get_body(preferencelist=('plain',))
    return part.get_content()


def incoming(subject, body, sender='Zoë Example <zoe@far.test>', to=ME, msgid=None, refs=None, attach=None):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'] = sender, to, subject
    m['Date'] = 'Thu, 22 Oct 2026 09:00:00 +0000'
    m['Message-ID'] = msgid or make_msgid(domain='far.test')
    if refs:
        m['References'] = refs
        m['In-Reply-To'] = refs.split()[-1]
    m.set_content(body)
    if attach:
        name, data, typ = attach
        main, sub = typ.split('/')
        m.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return m.as_bytes(), m['Message-ID']


# ------------------------------------------------------------------------------------------------ send
def test_a_new_message_leaves_exactly_as_it_was_composed(node, me):
    pdf = b'%PDF-1.4\n' + bytes(range(256)) * 40
    body = 'Grüße aus Köln — ½ price, 🎉 tonight.\nSecond line, ünïcödé.\n'
    payload = {'account': ME, 'to': '"Doe, Jane" <jane@x.test>, bob@y.test', 'cc': 'carol@z.test',
               'subject': 'Résumé für Zoë 🎉', 'body': body,
               'attachments': [{'name': 'résumé 2026.pdf', 'type': 'application/pdf', 'b64': b64(pdf)}]}
    code, got = node.call(me, 'POST', '/send', payload)
    assert code == 200 and got == {'ok': True}, got
    d = sent_one(node)
    assert d.tls and d.auth == ME, 'the password must only cross after STARTTLS'
    assert sorted(d.rcpts) == ['bob@y.test', 'carol@z.test', 'jane@x.test'], d.rcpts
    m = d.msg
    assert m['From'] == ME
    assert str(m['Subject']) == 'Résumé für Zoë 🎉'
    assert [a.addr_spec for a in m['To'].addresses] == ['jane@x.test', 'bob@y.test']
    assert [a.display_name for a in m['To'].addresses][0] == 'Doe, Jane'
    assert str(m['Cc']) == 'carol@z.test' and m['Bcc'] is None
    assert m['Message-ID'] and m['Message-ID'].endswith('@home.test>') and m['Date']
    assert body_of(m) == body
    atts = list(m.iter_attachments())
    assert [a.get_filename() for a in atts] == ['résumé 2026.pdf'], [a['Content-Disposition'] for a in atts]
    assert atts[0].get_content_type() == 'application/pdf' and atts[0].get_content() == pdf
    assert max(len(line) for line in d.data.split(b'\r\n')) <= 998, 'an SMTP line limit broken'
    # ...and the very same message is filed in Sent, so the conversation view has the user's own half.
    filed = [raw for box, raw in node.wire.imap.appended if box == 'Sent']
    assert len(filed) == 1 and message_from_bytes(filed[0], policy=policy.default)['Message-ID'] == m['Message-ID']


def test_bcc_is_in_the_envelope_and_never_in_the_message(node, me):
    code, got = node.call(me, 'POST', '/send', {'account': ME, 'to': 'ann@x.test', 'cc': '',
                                                'bcc': 'boss@hidden.test', 'subject': 'hi', 'body': 'b'})
    assert got == {'ok': True}, got
    d = sent_one(node)
    assert sorted(d.rcpts) == ['ann@x.test', 'boss@hidden.test']
    assert b'boss@hidden.test' not in d.data and d.msg['Bcc'] is None, 'every recipient can read the Bcc list'


# ------------------------------------------------------------------------------------------------ receive
def test_received_mail_reaches_the_mailbox_with_its_attachment(node, me):
    xls = bytes(range(200)) * 7
    raw, mid = incoming('Rechnung Nr. 7 — fällig', 'Bitte zahlen: 12,50 €\nDanke!\n',
                        attach=('Übersicht.xlsx', xls, 'application/vnd.ms-excel'))
    uid = node.wire.imap.deliver(raw)
    code, got = node.call(me, 'POST', '/sync')
    assert code == 200 and got['ok'] and got['new'][ME] >= 1, got   # INBOX + what Sent holds
    code, page = node.call(me, 'GET', '/messages?account=%s&folder=INBOX' % ME)
    row = next(m for m in page['messages'] if m['uid'] == str(uid))
    assert row['subject'] == 'Rechnung Nr. 7 — fällig' and row['from_email'] == 'zoe@far.test'
    assert row['attachments'] == 1 and not row['read']
    code, msg = node.call(me, 'GET', '/message?account=%s&uid=%s&folder=INBOX' % (ME, uid))
    m = msg.get('message', msg)
    assert 'Bitte zahlen: 12,50 €' in m['body_text'] and m['message_id'] == mid
    assert m['attachments'][0]['name'] == 'Übersicht.xlsx'
    code, data = node.call(me, 'GET', '/dl/%s/INBOX/%s/0' % (ME, uid))
    assert code == 200 and data == xls
    # A second sync stores nothing twice.
    assert node.call(me, 'POST', '/sync')[1]['new'] == {ME: 0}, 'a second sync stored messages again'


# ------------------------------------------------------------------------------------------------ reply / forward
def _seed(node, me, subject, body, refs=None, attach=None):
    raw, mid = incoming(subject, body, refs=refs, attach=attach)
    uid = node.wire.imap.deliver(raw)
    return str(uid), mid


def test_a_reply_threads_and_sends_what_the_composer_showed(node, me):
    uid, mid = _seed(node, me, 'Plans für Samstag', 'Kommst du?\n',
                     refs='<root@far.test> <second@far.test>')
    # mail.js: reply → to = from_email, subject "Re: …"; the person added a Cc and edited the subject.
    payload = {'account': ME, 'to': 'zoe@far.test', 'cc': 'dan@x.test', 'subject': 'Re: Plans für Samstag (ja!)',
               'body': 'Ja, gerne.\n', 'attachments': [], 'uid': uid, 'folder': 'INBOX', 'reply_all': False}
    code, got = node.call(me, 'POST', '/reply', payload)
    assert got == {'ok': True}, got
    d = sent_one(node)
    m = d.msg
    assert m['In-Reply-To'] == mid
    assert m['References'].split() == ['<root@far.test>', '<second@far.test>', mid]
    assert sorted(d.rcpts) == ['dan@x.test', 'zoe@far.test'], 'the Cc added in the reply box got nothing'
    assert str(m['Subject']) == 'Re: Plans für Samstag (ja!)' and body_of(m) == 'Ja, gerne.\n'


def test_an_older_client_reply_still_works_out_the_recipients(node, me):
    """No to/cc/subject in the body (a cached PWA): answered from the original, as before."""
    uid, mid = _seed(node, me, 'Ping', 'p\n')
    code, got = node.call(me, 'POST', '/reply', {'account': ME, 'uid': uid, 'folder': 'INBOX', 'body': 'pong'})
    assert got == {'ok': True}
    m = sent_one(node).msg
    assert str(m['Subject']) == 'Re: Ping' and str(m['To']) == 'zoe@far.test' and m['In-Reply-To'] == mid


def test_a_forward_threads_carries_the_attachment_and_quotes_once(node, me):
    doc = b'PK\x03\x04' + b'\x00' * 300
    original = 'Das Protokoll von gestern, bitte prüfen.\nGruß, Zoë\n'
    uid, mid = _seed(node, me, 'Protokoll', original, attach=('Protokoll.docx', doc,
                                                               'application/octet-stream'))
    # mail.js prefills the forward body with the original (the person can trim it):
    body = 'FYI\n\n---------- Forwarded ----------\nFrom: Zoë Example\nSubject: Protokoll\n\n' + original
    code, got = node.call(me, 'POST', '/forward', {'account': ME, 'to': 'boss@x.test', 'cc': '',
                                                   'subject': 'Fwd: Protokoll', 'body': body, 'attachments': [],
                                                   'uid': uid, 'folder': 'INBOX'})
    assert got == {'ok': True}, got
    m = sent_one(node).msg
    assert m['In-Reply-To'] == mid and m['References'].split()[-1] == mid
    text = body_of(m)
    assert text.count('Das Protokoll von gestern') == 1, ('the original is quoted twice', text)
    atts = list(m.iter_attachments())
    assert [a.get_filename() for a in atts] == ['Protokoll.docx'] and atts[0].get_content() == doc


# ------------------------------------------------------------------------------------------------ refusals
@pytest.mark.parametrize('how,expect', [
    ('rcpt', '550 5.1.1'),
    ('data', '554 5.7.1'),
    ('auth', '535 5.7.8'),
    ('refused', 'Could not reach the mail server'),
    ('tls', 'secure connection'),
])
def test_a_send_the_server_refuses_fails_and_says_why(node, me, how, expect):
    s = node.wire.smtp
    if how == 'rcpt':
        s.rcpt_reply = '550 5.1.1 <nobody@x.test>: Recipient address rejected: User unknown'
    elif how == 'data':
        s.data_reply = '554 5.7.1 Message rejected as spam'
    elif how == 'auth':
        s.auth_reply = '535 5.7.8 Error: authentication failed'
    elif how == 'refused':
        node.wire.configure(me, smtp_port=closed_port())
    elif how == 'tls':
        s.tls = 'broken'
    try:
        code, got = node.call(me, 'POST', '/send', {'account': ME, 'to': 'nobody@x.test', 'cc': '',
                                                    'subject': 'x', 'body': 'y', 'attachments': []})
    finally:
        node.wire.configure(me)
    assert code == 200 and got['ok'] is False, got
    assert expect.lower() in got['error'].lower(), got
    assert s.delivered == [], 'a refused send delivered something'


def test_one_refused_recipient_of_two_is_named(node, me):
    node.wire.smtp.rcpt_reply = {'gone@x.test': '550 5.1.1 <gone@x.test>: mailbox unavailable'}
    code, got = node.call(me, 'POST', '/send', {'account': ME, 'to': 'ok@x.test, gone@x.test', 'cc': '',
                                                'subject': 'x', 'body': 'y'})
    assert got['ok'] is True and [r['address'] for r in got.get('refused', [])] == ['gone@x.test'], got
    assert sent_one(node).rcpts == ['ok@x.test']


def test_a_slow_server_is_waited_for(node, me):
    node.wire.smtp.data_delay = 3
    t = time.monotonic()
    code, got = node.call(me, 'POST', '/send', {'account': ME, 'to': 'ann@x.test', 'cc': '', 'subject': 's',
                                                'body': 'b'})
    assert got == {'ok': True} and time.monotonic() - t >= 3
    assert sent_one(node).rcpts == ['ann@x.test']

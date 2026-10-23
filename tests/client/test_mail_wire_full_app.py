"""Mail as a person uses it, from the SHIPPED client to a real SMTP server and from a real IMAP server back.

  The node   app/routers/mail.py mounted in-process (discover_flows_harness.Routers) over the REAL mail_service
             (smtplib/imaplib), mail_sync and mail_store -> this node's relay (the shipped RelayServer). The account's
             settings row and Blossom's byte store are in memory; tests/mail_wire_harness.py says exactly what.
  The world  a real ESMTP server (STARTTLS + AUTH) and a real IMAP4rev1 server on loopback
             (tests/mail_wire_servers.py), each recording the exact bytes. No internet.

Flows (each through the screen's own controls, by id):
  * compose -> Attach (a real file through the file input) -> Send: the message the SMTP server received is exactly
    what was typed -- non-ASCII subject and body, the attachment's name and bytes;
  * a message delivered to the IMAP inbox is seen on a FRESH device (empty profile) after Refresh, and opens;
  * Reply from the reader: In-Reply-To/References to the message answered, sent to the sender;
  * the server REFUSES the send (550): the composer stays open with the text in it, says the server's reason, and
    the message is saved in Drafts (read back from the node) -- never silently lost;
  * a SLOW server (6 s to accept the message): Send says "Sending…" and is disabled while the page stays
    responsive (the composer's fields still take input), then "Sent".

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/mail.js=/tmp/old/mail.js" serves an older client; copy an older
app/services/mail_service.py / app/routers/mail.py over the working tree for the server half.
"""
import asyncio
import json
import os
import tempfile
import time
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from tests import mail_wire_harness as W
from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('mail',)
FIRST_FILL = 120
SCENARIOS = {}
COST = {}
TOASTS = "(window.__toastLog||[])"


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Ctx:
    def __init__(self, n):
        from app.routers import mail as mail_router
        self.n = n
        self.mp = mp = pytest.MonkeyPatch()
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(mail_router.router, '')], self.users, paths=['/api/mail/'])
        self.wire = W.MailWire(mp, n.home.port, list(self.users.values()))
        self.tmp = tempfile.mkdtemp(prefix='pc-mailwire-files-')

    def call(self, pk, method, path):
        async def go():
            return await self.api.client.request(method, '/api/mail' + path, headers={'Authorization': 'Bearer tok-' + pk})
        return asyncio.run_coroutine_threadsafe(go(), self.api.loop).result(60).json()

    def stop(self):
        self.wire.stop()
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    # ONE lane: the scenarios share one SMTP server, and two of them make it refuse or stall.
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx, lanes=1)


async def press(d, sel, what, seconds=20):
    await F.wait(d, "(e=>!!e&&(e.click(),true))(document.querySelector(%s))" % json.dumps(sel), seconds, what)


async def open_mail(d):
    await d.b.js("__PC.switchView('mail');true")
    await F.wait(d, "!!document.querySelector('#mail-compose') && !!document.querySelector('#mail-items')", 40,
                 'the Mail screen')


async def type_into(d, sel, text):
    await d.b.js("(()=>{const e=document.querySelector(%s);e.focus();e.value=%s;"
                 "e.dispatchEvent(new Event('input',{bubbles:true}));return true})()" % (json.dumps(sel), json.dumps(text)))


async def attach(d, path):
    doc = await d.b.call('DOM.getDocument', {'depth': -1})
    node = await d.b.call('DOM.querySelector', {'nodeId': doc['root']['nodeId'], 'selector': '#cm-file'})
    await d.b.call('DOM.setFileInputFiles', {'nodeId': node['nodeId'], 'files': [path]})


async def open_row(d, subject):
    """Click the row's content (that is what opens it), found and clicked in ONE evaluation -- the list repaints."""
    await F.wait(d, "(r=>!!r&&(r.click(),true))([...document.querySelectorAll('.mail-item')].find(e=>e.innerText"
                    ".includes(%s))?.querySelector('.mi-content'))" % json.dumps(subject), 20, 'the row')
    await F.wait(d, "!!document.querySelector('#mail-read .mail-actions')", 30, 'the reader')


def incoming(subject, body, msgid=None):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'] = 'Zoë Example <zoe@far.test>', W.ACCOUNT, subject
    m['Date'] = 'Thu, 22 Oct 2026 09:00:00 +0000'
    m['Message-ID'] = msgid or make_msgid(domain='far.test')
    m.set_content(body)
    return m.as_bytes(), m['Message-ID']


async def compose(d, to, subject, body):
    await press(d, '#mail-compose', 'the compose button')
    await F.wait(d, "!!document.querySelector('#cm-send') && !!document.querySelector('#cm-to')", 15, 'the composer')
    await type_into(d, '#cm-to', to)
    await type_into(d, '#cm-subj', subject)
    await type_into(d, '#cm-body', body)


@scenario
async def compose_attach_and_send_reaches_the_server_exactly(S):
    ctx = S.ctx
    data = bytes(range(256)) * 50
    path = os.path.join(ctx.tmp, 'Bericht für Zoë.pdf')
    with open(path, 'wb') as f:
        f.write(data)
    d = await S.device(0)
    await open_mail(d)
    body = 'Hallo Zoë,\nanbei der Bericht — 12,50 € bezahlt. 🎉\n'
    await compose(d, '"Example, Zoë" <zoe@far.test>, ann@x.test', 'Bericht März ✓', body)
    await press(d, '#cm-attach', 'the Attach button')
    await attach(d, path)
    await F.wait(d, "/Bericht f\\u00fcr Zo\\u00eb\\.pdf/.test(document.querySelector('#cm-atts').textContent)", 15,
                 'the attachment listed in the composer')
    n0 = len(ctx.wire.smtp.delivered)
    await press(d, '#cm-send', 'Send')
    await F.wait(d, "%s.includes('Sent')" % TOASTS, 40, 'Sent')
    got = ctx.wire.smtp.wait(n0 + 1)[-1]
    m = got.msg
    assert sorted(got.rcpts) == ['ann@x.test', 'zoe@far.test'], got.rcpts
    assert str(m['Subject']) == 'Bericht März ✓' and m['From'] == W.ACCOUNT
    assert m.get_body(preferencelist=('plain',)).get_content() == body
    atts = list(m.iter_attachments())
    assert [a.get_filename() for a in atts] == ['Bericht für Zoë.pdf'] and atts[0].get_content() == data


@scenario
async def received_mail_shows_on_a_fresh_device_and_opens(S):
    ctx = S.ctx
    subject = 'Einladung: Grillfest am Sonntag %d' % time.time_ns()
    raw, _ = incoming(subject, 'Bring Salat mit! — Zoë\n')
    ctx.wire.imap.deliver(raw)
    d = await S.device(0)                 # a fresh Chrome profile: nothing cached, the mailbox is the node's
    await open_mail(d)
    await press(d, '#mail-refresh', 'Refresh')
    await F.wait(d, "document.querySelector('#mail-items').innerText.includes(%s)" % json.dumps(subject), 60,
                 'the received message in the list')
    await open_row(d, subject)
    await F.wait(d, "!!document.querySelector('#mail-read') && document.querySelector('#mail-read').innerText"
                    ".includes('Bring Salat mit!')", 30, 'the message opened')


@scenario
async def reply_from_the_reader_threads(S):
    ctx = S.ctx
    subject = 'Frage zum Termin %d' % time.time_ns()
    raw, mid = incoming(subject, 'Passt dir 10 Uhr?\n')
    ctx.wire.imap.deliver(raw)
    d = await S.device(0)
    await open_mail(d)
    await press(d, '#mail-refresh', 'Refresh')
    await F.wait(d, "document.querySelector('#mail-items').innerText.includes(%s)" % json.dumps(subject), 60,
                 'the message in the list')
    await open_row(d, subject)
    await press(d, '#mail-read .mail-actions [data-act="reply"]', 'Reply', 30)
    await F.wait(d, "!!document.querySelector('#cm-send')", 15, 'the reply composer')
    await type_into(d, '#cm-body', 'Ja, 10 Uhr passt.\n')
    n0 = len(ctx.wire.smtp.delivered)
    await press(d, '#cm-send', 'Send')
    await F.wait(d, "%s.includes('Sent')" % TOASTS, 40, 'Sent')
    m = ctx.wire.smtp.wait(n0 + 1)[-1].msg
    assert m['In-Reply-To'] == mid and m['References'].split()[-1] == mid, (m['In-Reply-To'], m['References'])
    assert str(m['To']) == 'zoe@far.test' and str(m['Subject']) == 'Re: ' + subject


@scenario
async def a_refused_send_is_said_and_the_message_is_kept(S):
    ctx, pk = S.ctx, S.pk(0)
    d = await S.device(0)
    await open_mail(d)
    subject = 'Wichtig %d' % time.time_ns()
    await compose(d, 'nobody@x.test', subject, 'Dieser Text darf nicht verloren gehen.\n')
    ctx.wire.smtp.rcpt_reply = '550 5.1.1 <nobody@x.test>: Recipient address rejected: User unknown'
    try:
        n0 = len(ctx.wire.smtp.delivered)
        await press(d, '#cm-send', 'Send')
        await F.wait(d, "!document.querySelector('#cm-fail').classList.contains('hidden')", 40, 'the failure said')
    finally:
        ctx.wire.smtp.rcpt_reply = None
    st = await d.b.js("({fail:document.querySelector('#cm-fail').textContent, body:document.querySelector('#cm-body').value,"
                      "subj:document.querySelector('#cm-subj').value, sendOn:!document.querySelector('#cm-send').disabled,"
                      "toasts:%s})" % TOASTS)
    assert 'User unknown' in st['fail'] and 'kept in Drafts' in st['fail'], st
    assert st['body'] == 'Dieser Text darf nicht verloren gehen.\n' and st['subj'] == subject and st['sendOn'], st
    assert 'Sent' not in st['toasts'], st
    assert len(ctx.wire.smtp.delivered) == n0
    drafts = ctx.call(pk, 'GET', '/messages?account=%s&folder=Drafts' % W.ACCOUNT)['messages']
    assert [m['subject'] for m in drafts] == [subject], drafts
    # Once the server accepts, the same Send finishes the job and the draft goes.
    await press(d, '#cm-send', 'Send again')
    await F.wait(d, "%s.includes('Sent')" % TOASTS, 40, 'Sent on the second try')
    await F.until(lambda: ctx.call(pk, 'GET', '/messages?account=%s&folder=Drafts' % W.ACCOUNT)['messages'] == [],
                  20, 'the draft removed after the send', d)


@scenario
async def a_slow_server_shows_progress_and_the_page_stays_live(S):
    ctx = S.ctx
    d = await S.device(0)
    await open_mail(d)
    await compose(d, 'ann@x.test', 'Langsam', 'b\n')
    ctx.wire.smtp.data_delay = 6
    try:
        n0 = len(ctx.wire.smtp.delivered)
        await press(d, '#cm-send', 'Send')
        await F.wait(d, "document.querySelector('#cm-send').disabled && "
                        "/Sending/.test(document.querySelector('#cm-send').textContent)", 5, 'Sending… on the button')
        # The page answers while the server thinks: the composer still takes input, and quickly.
        t = time.monotonic()
        await type_into(d, '#cm-cc', 'cc@x.test')
        assert await d.b.js("document.querySelector('#cm-cc').value") == 'cc@x.test'
        assert time.monotonic() - t < 2, 'the page froze while sending'
        assert len(ctx.wire.smtp.delivered) == n0 and not await d.b.js("%s.includes('Sent')" % TOASTS)
        await F.wait(d, "%s.includes('Sent')" % TOASTS, 40, 'Sent once the server accepts')
    finally:
        ctx.wire.smtp.data_delay = 0
    assert len(ctx.wire.smtp.delivered) == n0 + 1


@CHROME
def test_compose_attach_and_send_reaches_the_server_exactly(ran):
    ran('compose_attach_and_send_reaches_the_server_exactly')


@CHROME
def test_received_mail_shows_on_a_fresh_device_and_opens(ran):
    ran('received_mail_shows_on_a_fresh_device_and_opens')


@CHROME
def test_reply_from_the_reader_threads(ran):
    ran('reply_from_the_reader_threads')


@CHROME
def test_a_refused_send_is_said_and_the_message_is_kept(ran):
    ran('a_refused_send_is_said_and_the_message_is_kept')


@CHROME
def test_a_slow_server_shows_progress_and_the_page_stays_live(ran):
    ran('a_slow_server_shows_progress_and_the_page_stays_live')

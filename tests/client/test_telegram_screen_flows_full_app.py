"""The Telegram screen, end to end: the shipped client, the node's REAL /api/tgc router and the REAL manager
(app/services/telegram_client/manager.py), with Telegram itself faked at the one boundary the manager talks to --
the Telethon client object its `client_factory` makes (same method names, same error class names). Nothing
reaches real Telegram.

  signs in      phone -> code -> two-step password -> the chats; a chat opens, a message typed and sent reaches
                Telegram, a message arriving on Telegram reaches the open chat over the LIVE socket (/api/tgc/ws,
                served by uvicorn on the routers' own loop and spliced through the instance like nginx does);
                a FRESH device of the same account opens straight to the chats -- one sign-in, every device.
  unreachable   the node cannot reach Telegram (Telethon's connect() raises ConnectionError): the screen says
                exactly that, in words -- never "HTTP 500", never a black CONNECTING... -- and comes back by
                itself once Telegram answers again.
  hangs         Telegram's connect never finishing in time: CONNECTING... gives way to a sentence and a Retry
                within the client's bound, and the screen recovers without leaving.
  node down     a device that had read its chats, then a node answering 503 / dropping the connection: the chats
                kept on THIS device, with a banner; a send fails out loud and keeps the text.
  fresh+down    a FRESH device and a 503 node: a sentence and "Trying again", no sign-in form; then the chats.
  no session    the relay holding the saved Telegram session could not be asked (the store raises Unavailable):
                never the sign-in form (a second login would replace the real session), nothing written; the
                chats once the relay answers.
  slow          a node answering status, chats and a chat 3 s late: CONNECTING... / Loading..., then the content.

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/telegram.js=...".
"""
import asyncio
import json
import socket
import time

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('tg',)
FIRST_FILL = 100          # 100 .. 127 (account keys are one byte)
SCENARIOS = {}
COST = {'signs_in_chats_sends_and_hears_live_messages': 30, 'telegram_that_hangs_ends_connecting': 25}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------- fake Telegram
class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class _Date:
    def __init__(self, ts):
        self._ts = ts

    def timestamp(self):
        return self._ts


class _User:
    def __init__(self, id, first, bot=False):
        self.id, self.first_name, self.last_name, self.username, self.bot = id, first, '', '', bot


class _Msg:
    def __init__(self, id, text, out=False, sender=None):
        self.id, self.message, self.out, self.media, self.file = id, text, out, None, None
        self.date, self.sender, self.sender_id = _Date(time.time() - 3600 + id), sender, getattr(sender, 'id', 1)
        self.reply_to = self.edit_date = self.reactions = None


class _Dialog:
    def __init__(self, id, ent, unread, last):
        self.id, self.entity, self.unread_count, self.message, self.pinned, self.dialog = id, ent, unread, last, False, None


class Tg:
    """One person's Telegram account on the other end. `connect`: None (fine) | 'refuse' (the DCs cannot be
    reached: Telethon's ConnectionError) | seconds (connect() takes that long)."""

    def __init__(self, phone, password=''):
        self.phone, self.password = phone, password
        self.sessions, self.sent, self.read, self.connect = set(), [], [], None
        alice = _User(42, 'Alice')
        self.chats = {42: [_Msg(1, 'hey, are you coming tonight?', sender=alice),
                           _Msg(2, 'yes, 8 pm', out=True),
                           _Msg(3, 'bring the projector', sender=alice)],
                      77: [_Msg(1, 'meeting moved to friday', sender=_User(9, 'Bob'))]}
        self.names = {42: _User(42, 'Alice'), 77: type('G', (), {'id': 77, 'title': 'Night City Crew'})()}
        self.clients = []


class World:
    def __init__(self):
        self.by_phone, self.by_session = {}, {}

    def add(self, tg):
        self.by_phone[tg.phone] = tg
        return tg


class FakeClient:
    """Telethon's TelegramClient, as far as the manager uses it."""

    def __init__(self, world, session):
        self.world, self._session, self.session, self._connected = world, session, self, False
        self.tg = world.by_session.get(session)
        self.handlers = []
        if self.tg:
            self.tg.clients.append(self)

    def save(self):
        return self._session

    def is_connected(self):
        return self._connected

    async def connect(self):
        how = self.tg.connect if self.tg else None
        if isinstance(how, (int, float)) and how:
            await asyncio.sleep(how)
            how = self.tg.connect
        if how == 'refuse' or isinstance(how, (int, float)):
            # telethon/client/telegrambaseclient.py: what connect() raises when no data centre answers.
            raise ConnectionError('Connection to Telegram failed 5 time(s)')
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def is_user_authorized(self):
        return bool(self.tg) and self._session in self.tg.sessions

    async def send_code_request(self, phone):
        self.tg = self.world.by_phone[phone]
        self.tg.clients.append(self)
        return type('Sent', (), {'phone_code_hash': 'h'})()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != self.tg.password:
                raise type('PasswordHashInvalidError', (Exception,), {})()
        elif code != '12345':
            raise PhoneCodeInvalidError()
        elif self.tg.password:
            raise SessionPasswordNeededError()
        self._session = 'sess-%s-%d' % (self.tg.phone, len(self.tg.sessions) + 1)
        self.tg.sessions.add(self._session)
        self.world.by_session[self._session] = self.tg

    async def get_me(self):
        return _User(99, 'Me')

    async def log_out(self):
        self.tg.sessions.discard(self._session)

    def add_event_handler(self, fn, ev):
        self.handlers.append((fn, ev))

    async def iter_dialogs(self, limit=100):
        for cid, msgs in self.tg.chats.items():
            yield _Dialog(cid, self.tg.names[cid], 1 if cid == 42 else 0, msgs[-1])

    async def iter_messages(self, chat, limit=50, offset_id=0):
        for m in reversed(self.tg.chats.get(chat, [])):
            if not offset_id or m.id < offset_id:
                yield m

    async def send_message(self, chat, text, reply_to=None):
        m = _Msg(len(self.tg.chats.setdefault(chat, [])) + 1, text, out=True)
        self.tg.chats[chat].append(m)
        self.tg.sent.append((chat, text))
        return m

    async def send_read_acknowledge(self, chat, max_id=None):
        self.tg.read.append((chat, max_id))

    async def get_input_entity(self, peer):
        raise ValueError('no peer cache here')        # read_out() then answers 0, as it does when it cannot ask

    async def download_profile_photo(self, peer, file=bytes, download_big=False):
        return None


async def arrive(tg, chat, text):
    """A message arriving on Telegram: Telethon calls the NewMessage handler the manager installed."""
    from telethon import events
    msg = _Msg(len(tg.chats[chat]) + 1, text, sender=tg.names[chat])
    tg.chats[chat].append(msg)

    class Ev:
        message, chat_id = msg, chat

        async def get_chat(self):
            return tg.names[chat]
    for c in tg.clients:
        for fn, kind in c.handlers:
            if isinstance(kind, events.NewMessage):
                await fn(Ev())


class Store:
    """Where the manager keeps the session string (session_store: a kind-30078 doc on the relay). `fail`: user
    ids whose read raises Unavailable -- the relay could not be asked."""

    def __init__(self):
        self.docs, self.saves, self.fail = {}, [], set()

    async def load(self, db, user):
        if user.id in self.fail:
            from app.services.relay_reader import Unavailable
            raise Unavailable('the relay did not answer')
        return self.docs.get(user.id, '')

    async def save(self, db, user, s):
        self.saves.append(user.id)
        self.docs[user.id] = s
        return True

    async def clear(self, db, user):
        self.docs.pop(user.id, None)
        self.saves.append(user.id)
        return True


# ------------------------------------------------------------------------------------------------ the node
def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Ctx:
    def __init__(self, n):
        from fastapi.responses import JSONResponse

        from app import auth as app_auth
        from app.routers import telegram_client as TR
        from app.services.relay_reader import Unavailable
        from app.services.telegram_client import manager as M
        self.mp = mp = pytest.MonkeyPatch()
        self.world, self.store = World(), Store()
        self.mgr = M.Manager(client_factory=lambda session, api: FakeClient(self.world, session), store=self.store,
                             api=lambda: (1, '0123456789abcdef0123456789abcdef'))
        mp.setattr(TR, 'manager', lambda: self.mgr)
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(TR.router, '')], self.users, paths=['/api/tgc/'])
        app = self.api.client._transport.app

        async def unavailable(request, exc):          # app/main.py's handler, the same shape
            return JSONResponse(status_code=503, content={'detail': 'Temporarily unavailable; try again shortly'},
                                headers={'Retry-After': '10'})
        app.add_exception_handler(Unavailable, unavailable)
        # The live socket: its first frame is the session token (`tok-<pubkey>` here), as on a node.
        by_id = {u.id: u for u in self.users.values()}
        orig_decode = app_auth.decode_token
        mp.setattr(app_auth, 'decode_token',
                   lambda t: {'sub': str(self.users[t[4:]].id)} if str(t).startswith('tok-') and t[4:] in self.users
                   else orig_decode(t))

        async def aget(db, uid):
            return by_id.get(int(uid))
        mp.setattr(TR.user_table, 'aget', aget)
        mp.setattr(TR, 'SessionLocal', lambda: None)
        # uvicorn on the routers' OWN loop (the manager's queues and locks live there), reached through the instance
        # by a byte splice of the upgrade -- the way nginx hands /api/tgc/ws to the app.
        import uvicorn
        self.ws_port = _free_port()
        self.uv = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.ws_port, log_level='error',
                                                lifespan='off', ws='websockets'))
        asyncio.run_coroutine_threadsafe(self.uv.serve(), self.api.loop)
        for _ in range(100):
            if self.uv.started:
                break
            time.sleep(.05)
        self.ws_down = set()            # device tags whose live socket is refused

        def ws_route(h, method, path, body):
            if method != 'GET' or not path.startswith('/api/tgc/ws'):
                return False
            tag = ''
            for part in (h.headers.get('Cookie') or '').split(';'):
                k, _, v = part.strip().partition('=')
                if k == 'pcdev':
                    tag = v
            if tag in self.ws_down:
                h.close_connection = True
                return True
            h._splice_to(self.ws_port)
            return True
        self.ws_route = ws_route
        H.ROUTES.insert(0, ws_route)

    def stop(self):
        try:
            H.ROUTES.remove(self.ws_route)
        except ValueError:
            pass
        self.uv.should_exit = True
        self.api.stop()
        self.mp.undo()

    def user(self, S, i=0):
        return self.users[S.pk(i)]

    def signed_in(self, S, password=''):
        """Account `i` of the scenario already signed in to Telegram on the node (a saved session)."""
        tg = self.world.add(Tg('+1555%07d' % S.base, password))
        sess = 'sess-%s-saved' % tg.phone
        tg.sessions.add(sess)
        self.world.by_session[sess] = tg
        self.store.docs[self.user(S).id] = sess
        return tg

    def on_loop(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.api.loop).result(20)


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------- what a person sees
SEEN = ("(()=>{const f=document.querySelector('#feed .tg-app');return {text:f?f.innerText:'',"
        "login:!!document.querySelector('.tg-login form'),dialogs:[...document.querySelectorAll('.tg-dialog[data-chat]')]"
        ".map(b=>b.querySelector('.tg-dtitle').textContent),msgs:[...document.querySelectorAll('.tg-msg .tg-body')]"
        ".map(b=>b.textContent),banner:(document.querySelector('.tg-offline')||{}).innerText||''}})()")
DIALOGS = "document.querySelectorAll('.tg-dialog[data-chat]').length"
CARD = "document.querySelector('#feed .tg-app .tg-card')"


async def seen(d):
    return await d.b.js(SEEN)


async def open_tg(d):
    await d.b.js("__PC.switchView('tg');true")
    await F.wait(d, "!!document.querySelector('#feed .tg-app')", 20, 'the Telegram screen')


async def open_chat(d, cid):
    await d.b.js("document.querySelector('.tg-dialog[data-chat=\"%d\"]').click();true" % cid)
    await F.wait(d, "document.querySelectorAll('.tg-msg').length>0", 20, 'the chat opened')


async def type_and_send(d, text):
    await d.b.js("(t=>{const a=document.querySelector('.tg-text');a.value=t;a.dispatchEvent(new Event('input',{bubbles:true}));"
                 "document.querySelector('.tg-send').click();return true})(%s)" % json.dumps(text))


def plain(text):
    """A screen that reads as a broken page: an HTTP code, a JS error name, or nothing but CONNECTING."""
    bad = ['HTTP 5', 'Failed to fetch', 'TypeError', 'undefined', '[object', 'Internal Server Error']
    return [b for b in bad if b in text]


# ------------------------------------------------------------------------------------------------ the scenarios
@scenario
async def signs_in_chats_sends_and_hears_live_messages(S):
    ctx = S.ctx
    tg = ctx.world.add(Tg('+1555%07d' % S.base, password='hunter2'))
    d = await S.device(0)
    await open_tg(d)
    for value in (tg.phone, '12345', 'hunter2'):
        await F.wait(d, "!!document.querySelector('.tg-login form') && !document.querySelector('.tg-login button[type=submit]').disabled"
                        " && document.querySelector('.tg-login form').dataset.step!==%s" % json.dumps(value), 20, 'the sign-in step')
        await d.b.js("(v=>{const f=document.querySelector('.tg-login form');f.dataset.step=v;f.v.value=v;f.requestSubmit();return true})(%s)"
                     % json.dumps(value))
    await F.wait(d, "%s===2" % DIALOGS, 20, 'the chats after signing in')
    got = await seen(d)
    assert got['dialogs'] == ['Alice', '👥 Night City Crew'], got
    assert ctx.store.saves == [ctx.user(S).id], 'the session was saved %r' % ctx.store.saves
    await open_chat(d, 42)
    got = await seen(d)
    assert got['msgs'] == ['hey, are you coming tonight?', 'yes, 8 pm', 'bring the projector'], got
    await F.until(lambda: (42, 3) in tg.read, 15, 'the chat marked read on Telegram', d)
    await type_and_send(d, 'on my way %d' % S.base)
    await F.until(lambda: (42, 'on my way %d' % S.base) in tg.sent, 20, 'the message reached Telegram', d)
    await F.wait(d, "[...document.querySelectorAll('.tg-msg.out .tg-body')].some(b=>b.textContent==='on my way %d')" % S.base,
                 10, 'the sent message in the chat')
    assert await d.b.js("document.querySelector('.tg-text').value") == ''
    # Something arrives on Telegram: the live socket carries it into the open chat.
    await F.until(lambda: any(c.handlers for c in tg.clients), 10, 'the manager listening on Telegram', d)
    await F.wait(d, "!!window.PCTelegram && !!PCTelegram._state.ws && PCTelegram._state.ws.readyState===1", 20,
                 'the live socket open')
    ctx.on_loop(arrive(tg, 42, 'see you at the door %d' % S.base))
    await F.wait(d, "[...document.querySelectorAll('.tg-msg .tg-body')].some(b=>b.textContent==='see you at the door %d')" % S.base,
                 15, 'the live message in the open chat')
    # One sign-in, every device: a FRESH device of this account opens to the chats.
    d2 = await S.device(0)
    await open_tg(d2)
    await F.wait(d2, "%s===2" % DIALOGS, 20, 'the chats on a fresh device')
    assert not (await seen(d2))['login']
    await open_chat(d2, 42)
    assert 'see you at the door %d' % S.base in (await seen(d2))['msgs']
    assert ctx.store.saves == [ctx.user(S).id], 'a second device wrote the session again'


@CHROME
def test_signs_in_chats_sends_and_hears_live_messages(ran):
    ran('signs_in_chats_sends_and_hears_live_messages')


@scenario
async def a_node_that_cannot_reach_telegram_says_so(S):
    ctx = S.ctx
    tg = ctx.signed_in(S)
    tg.connect = 'refuse'
    d = await S.device(0)
    await open_tg(d)
    await F.wait(d, "!!%s && !%s.classList.contains('tg-boot')" % (CARD, CARD), 25, 'what happened, said')
    got = await seen(d)
    assert 'reach Telegram' in got['text'] and not plain(got['text']), got
    assert not got['login'], 'the sign-in form for an account that IS signed in: %r' % got
    assert 'Trying again automatically' in got['text'], got
    assert ctx.store.docs.get(ctx.user(S).id), 'the saved session was dropped'
    tg.connect = None
    await F.wait(d, "%s===2" % DIALOGS, 30, 'the chats once Telegram answers again')


@CHROME
def test_a_node_that_cannot_reach_telegram_says_so(ran):
    ran('a_node_that_cannot_reach_telegram_says_so')


@scenario
async def telegram_that_hangs_ends_connecting(S):
    ctx = S.ctx
    tg = ctx.signed_in(S)
    tg.connect = 120                      # connect() is still trying, long after anyone would wait
    d = await S.device(0)
    await open_tg(d)
    assert 'CONNECTING' in (await seen(d))['text']
    t0 = time.monotonic()
    await F.wait(d, "!!%s" % CARD, 40, 'CONNECTING given up on, in words')
    took = time.monotonic() - t0
    got = await seen(d)
    assert 'CONNECTING' not in got['text'] and not plain(got['text']) and not got['login'], got
    assert 'Retry' in got['text'], got
    assert took < 35, 'CONNECTING for %.0f s' % took
    tg.connect = None
    await d.b.js("document.querySelector('#feed [data-act=retry]').click();true")
    await F.wait(d, "%s===2" % DIALOGS, 30, 'Retry brings the chats once Telegram connects')


@CHROME
def test_telegram_that_hangs_ends_connecting(ran):
    ran('telegram_that_hangs_ends_connecting')


@scenario
async def a_node_gone_down_shows_the_chats_kept_here(S):
    ctx, api, tag = S.ctx, S.ctx.api, 'tgk%d' % S.base
    tg = ctx.signed_in(S)
    d = await S.device(0, tag=tag)
    await open_tg(d)
    await F.wait(d, "%s===2" % DIALOGS, 20, 'the chats')
    await open_chat(d, 42)
    await d.b.js("__PC.switchView('home');true")
    for how in (503, 'drop'):
        api.fail('/api/tgc/', how, tag)
        await open_tg(d)
        await F.wait(d, "!!document.querySelector('.tg-offline') && %s===2" % DIALOGS, 30,
                     'the kept chats with a banner (%s)' % how)
        got = await seen(d)
        assert 'showing the messages kept on this device' in got['banner'], got
        await open_chat(d, 42)
        assert (await seen(d))['msgs'][-1] == 'bring the projector'
        # Sending needs the node: it fails out loud and the text stays to send again.
        toasts = len(await d.toasts())
        await type_and_send(d, 'are you there %s' % how)
        await F.wait(d, "(window.__toastLog||[]).length>%d" % toasts, 20, 'the failed send said')
        toast = (await d.toasts())[-1]
        assert toast.startswith('Telegram') and not plain(toast), toast
        assert await d.b.js("document.querySelector('.tg-text').value") == 'are you there %s' % how
        assert not [s for s in tg.sent if s[1].startswith('are you there')]
        await d.b.js("__PC.switchView('home');true")
    api.heal('/api/tgc/', tag)
    await open_tg(d)
    await F.wait(d, "%s===2 && !document.querySelector('.tg-offline')" % DIALOGS, 30, 'the live chats, no banner')


@CHROME
def test_a_node_gone_down_shows_the_chats_kept_here(ran):
    ran('a_node_gone_down_shows_the_chats_kept_here')


@scenario
async def a_fresh_device_on_a_node_that_is_down(S):
    ctx, api, tag = S.ctx, S.ctx.api, 'tgf%d' % S.base
    ctx.signed_in(S)
    api.fail('/api/tgc/', 503, tag)
    d = await S.device(0, tag=tag)
    await open_tg(d)
    await F.wait(d, "!!%s && !%s.classList.contains('tg-boot')" % (CARD, CARD), 25, 'the 503 said')
    got = await seen(d)
    assert 'not available' in got['text'] and 'Trying again automatically' in got['text'], got
    assert not plain(got['text']) and not got['login'], got
    api.heal('/api/tgc/', tag)
    await F.wait(d, "%s===2" % DIALOGS, 20, 'the chats when the node is back (no button pressed)')


@CHROME
def test_a_fresh_device_on_a_node_that_is_down(ran):
    ran('a_fresh_device_on_a_node_that_is_down')


@scenario
async def a_session_the_relay_could_not_read_is_not_a_signed_out_account(S):
    ctx = S.ctx
    ctx.signed_in(S)
    uid = ctx.user(S).id
    ctx.store.fail.add(uid)
    d = await S.device(0)
    await open_tg(d)
    await F.wait(d, "!!%s && !%s.classList.contains('tg-boot')" % (CARD, CARD), 25, 'the unread session said')
    got = await seen(d)
    assert not got['login'], 'the sign-in form while the saved session could not be read: %r' % got
    assert not plain(got['text']), got
    assert uid not in ctx.store.saves, 'written after a failed read'
    ctx.store.fail.discard(uid)
    await F.wait(d, "%s===2" % DIALOGS, 20, 'the chats once the relay answers')
    assert uid not in ctx.store.saves


@CHROME
def test_a_session_the_relay_could_not_read_is_not_a_signed_out_account(ran):
    ran('a_session_the_relay_could_not_read_is_not_a_signed_out_account')


@scenario
async def a_slow_node_is_waited_for(S):
    ctx, api, tag = S.ctx, S.ctx.api, 'tgs%d' % S.base
    ctx.signed_in(S)
    api.fail('/api/tgc/', 3, tag)
    d = await S.device(0, tag=tag)
    await open_tg(d)
    assert 'CONNECTING' in (await seen(d))['text']
    await F.wait(d, "!!document.querySelector('.tg-dialogs') && /Loading chats/.test(document.querySelector('.tg-dialogs').innerText)",
                 15, 'Loading chats while it waits')
    await F.wait(d, "%s===2" % DIALOGS, 20, 'the slow chats drawn')
    await d.b.js("document.querySelector('.tg-dialog[data-chat=\"77\"]').click();true")
    await F.wait(d, "/Loading/.test((document.querySelector('.tg-msgs')||{}).innerText||'')", 5, 'Loading the chat')
    await F.wait(d, "document.querySelectorAll('.tg-msg').length===1", 20, 'the slow chat drawn')
    assert (await seen(d))['msgs'] == ['meeting moved to friday']
    api.heal('/api/tgc/', tag)


@CHROME
def test_a_slow_node_is_waited_for(ran):
    ran('a_slow_node_is_waited_for')

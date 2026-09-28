"""The Telegram client's server half — app/services/telegram_client + app/routers/telegram_client.py.

Run: venv-unified/bin/python -m pytest tests/test_telegram_client.py

Driven against a FAKE Telegram (a stand-in for Telethon's client with the same method names), so the
real manager, the real router and the real ticket rules run end to end:

  sign in       phone → code → (2FA password) → ready; the session is saved ONLY after success
  once          a new manager (a restart) resumes from the saved session with no second login
  revoked       a session Telegram revoked elsewhere is forgotten, not retried for ever
  yours         one account's Telegram is invisible to another
  attachments   the uploaded bytes, name, caption and mode reach Telegram unchanged
  media         an attachment comes back with a safe content type, behind a TICKET that opens
                only Telegram media for that one account
  live          a new message reaches every window the account has open
"""
import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.telegram_client import manager as M


def run(c):
    return asyncio.run(c)


_CUR = {"tg": None}          # whose Telegram answers the next client the manager makes


class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class _Date:
    def __init__(self, ts):
        self._ts = ts

    def timestamp(self):
        return self._ts


class _Msg:
    def __init__(self, id, text="", out=False, media=None, file=None, sender=None, **flags):
        self.id, self.message, self.out, self.media, self.file = id, text, out, media, file
        self.date, self.sender, self.sender_id, self.reply_to, self.edit_date = _Date(1_700_000_000 + id), sender, 7, None, None
        for k in ("photo", "video", "voice", "video_note", "gif", "audio", "sticker"):
            setattr(self, k, flags.get(k))


class _File:
    def __init__(self, name, size, mime):
        self.name, self.size, self.mime_type = name, size, mime


class _User:
    def __init__(self, id, first, bot=False):
        self.id, self.first_name, self.last_name, self.username, self.bot = id, first, "", "", bot


class _Dialog:
    def __init__(self, id, ent, unread, last):
        self.id, self.entity, self.unread_count, self.message, self.pinned, self.dialog = id, ent, unread, last, False, None


class Tg:
    """A Telegram account on the other end."""
    def __init__(self, password=""):
        self.password, self.authorized_sessions, self.sent, self.files, self.read = password, set(), [], [], []
        self.chats = {42: [_Msg(1, "hello"), _Msg(2, "", media=object(), file=_File("cat.jpg", 1234, "image/jpeg"), photo=True),
                           _Msg(3, "", media=object(), file=_File("notes.pdf", 5000, "application/pdf"))]}


class FakeClient:
    def __init__(self, tg: Tg, session: str):
        self.tg, self.handlers = tg, []
        self._session = session
        self.session = self
        self._connected = False

    def save(self):
        return self._session

    def is_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def disconnect(self):
        self._connected = False

    async def is_user_authorized(self):
        return self._session in self.tg.authorized_sessions

    async def send_code_request(self, phone):
        class S:
            phone_code_hash = "h"
        return S()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is not None:
            if password != self.tg.password:
                raise type("PasswordHashInvalidError", (Exception,), {})()
        elif code != "12345":
            raise PhoneCodeInvalidError()
        elif self.tg.password:
            raise SessionPasswordNeededError()
        self._session = "sess-" + str(len(self.tg.authorized_sessions) + 1)
        self.tg.authorized_sessions.add(self._session)

    async def get_me(self):
        return _User(99, "Me")

    async def log_out(self):
        self.tg.authorized_sessions.discard(self._session)

    def add_event_handler(self, fn, ev):
        self.handlers.append(fn)

    async def iter_dialogs(self, limit=100):
        yield _Dialog(42, _User(42, "Alice"), 3, self.tg.chats[42][-1])

    async def iter_messages(self, chat, limit=50, offset_id=0):
        for m in reversed(self.tg.chats.get(chat, [])):
            if not offset_id or m.id < offset_id:
                yield m

    async def send_message(self, chat, text, reply_to=None):
        m = _Msg(100 + len(self.tg.sent), text, out=True)
        self.tg.sent.append((chat, text, reply_to))
        return m

    async def send_file(self, chat, f, caption=None, force_document=False, voice_note=False, video_note=False,
                        supports_streaming=True, reply_to=None):
        self.tg.files.append({"chat": chat, "name": f.name, "data": f.read(), "caption": caption,
                              "document": force_document, "voice": voice_note, "video_note": video_note})
        return _Msg(200 + len(self.tg.files), caption or "", out=True, media=object(), file=_File(f.name, 3, "image/png"), photo=True)

    async def send_read_acknowledge(self, chat, max_id=None):
        self.tg.read.append((chat, max_id))

    async def get_messages(self, chat, ids):
        return next((m for m in self.tg.chats.get(chat, []) if m.id == ids), None)

    async def download_media(self, m, file=bytes, thumb=None):
        return (b"THUMB" if thumb is not None else b"FULL:") + m.file.name.encode()

    async def download_profile_photo(self, peer, file=bytes, download_big=False):
        return b"\xff\xd8avatar"


class Store:
    def __init__(self):
        self.docs, self.saves, self.fail = {}, [], False

    async def load(self, db, user):
        return self.docs.get(user.id, "")

    async def save(self, db, user, s):
        if self.fail:
            return False
        self.saves.append((user.id, s))
        self.docs[user.id] = s
        return True

    async def clear(self, db, user):
        self.docs.pop(user.id, None)
        return True


class U:
    def __init__(self, id):
        self.id, self.nostr_npub = id, "npub1u%d" % id


@pytest.fixture
def world(monkeypatch):
    import telethon.events as tev                       # the real event classes the manager registers
    tgs = {1: Tg(), 2: Tg(password="hunter2")}
    store = Store()
    made = []

    def factory(session, api):
        # Which account's Telegram answers is decided by who signed in last -- mapped by the test.
        c = FakeClient(_CUR['tg'], session)
        made.append(c)
        return c
    mgr = M.Manager(client_factory=factory, store=store, api=lambda: (1, "hash"))
    _CUR['tg'] = tgs[1]
    monkeypatch.setattr(M, "_why", lambda e: type(e).__name__)
    return mgr, tgs, store, made


def login(mgr, user, tg, code="12345", password=None):
    _CUR['tg'] = tg
    assert run(mgr.send_code(None, user, "+1 555 010 4477")) == {"state": "code"}
    r = run(mgr.sign_in_code(None, user, code))
    if r["state"] == "password":
        r = run(mgr.sign_in_password(None, user, password))
    return r


def test_sign_in_saves_the_session_only_after_it_works(world):
    mgr, tgs, store, _ = world
    u = U(1)
    _CUR['tg'] = tgs[1]
    run(mgr.send_code(None, u, "+15550104477"))
    with pytest.raises(M.TGError):
        run(mgr.sign_in_code(None, u, "00000"))
    assert store.saves == [], "a failed code must not save anything"
    r = run(mgr.sign_in_code(None, u, "12345"))
    assert r["state"] == "ready" and r["me"]["name"] == "Me"
    assert store.saves == [(1, "sess-1")]


def test_two_step_verification(world):
    mgr, tgs, store, _ = world
    u = U(2)
    _CUR['tg'] = tgs[2]
    run(mgr.send_code(None, u, "+15550104477"))
    assert run(mgr.sign_in_code(None, u, "12345")) == {"state": "password"}
    with pytest.raises(M.TGError):
        run(mgr.sign_in_password(None, u, "wrong"))
    assert run(mgr.sign_in_password(None, u, "hunter2"))["state"] == "ready"
    assert store.docs[2]


def test_a_session_that_could_not_be_saved_is_said_out_loud(world):
    mgr, tgs, store, _ = world
    store.fail = True
    _CUR['tg'] = tgs[1]
    run(mgr.send_code(None, U(1), "+15550104477"))
    with pytest.raises(M.TGError, match="could not be saved"):
        run(mgr.sign_in_code(None, U(1), "12345"))


def test_sign_in_once_and_a_restart_resumes_it(world):
    mgr, tgs, store, _ = world
    login(mgr, U(1), tgs[1])
    fresh = M.Manager(client_factory=lambda s, api: FakeClient(tgs[1], s), store=store, api=lambda: (1, "h"))
    assert run(fresh.status(None, U(1)))["state"] == "ready", "a restart asked the user to sign in again"


def test_a_session_revoked_elsewhere_is_forgotten(world):
    mgr, tgs, store, _ = world
    login(mgr, U(1), tgs[1])
    tgs[1].authorized_sessions.clear()                    # Telegram → Settings → Devices → terminate
    fresh = M.Manager(client_factory=lambda s, api: FakeClient(tgs[1], s), store=store, api=lambda: (1, "h"))
    assert run(fresh.status(None, U(1)))["state"] == "none"
    assert 1 not in store.docs


def test_not_configured_is_a_sentence_not_a_crash():
    mgr = M.Manager(client_factory=lambda s, a: None, store=Store(), api=lambda: None)
    assert run(mgr.status(None, U(1))) == {"configured": False, "state": "none", "admin": False}
    with pytest.raises(M.TGError, match="Admin"):
        run(mgr.send_code(None, U(1), "+15550104477"))


def test_nothing_before_sign_in(world):
    mgr, _, _, _ = world
    for call in (lambda: mgr.dialogs(None, U(1)), lambda: mgr.send_text(None, U(1), 42, "x"),
                 lambda: mgr.media(None, U(1), 42, 2)):
        with pytest.raises(M.TGError, match="Sign in"):
            run(call())


def test_chats_messages_and_text(world):
    mgr, tgs, _, _ = world
    login(mgr, U(1), tgs[1])
    (d,) = run(mgr.dialogs(None, U(1)))
    assert d["id"] == 42 and d["title"] == "Alice" and d["unread"] == 3 and d["last"]["media"] == "file"
    ms = run(mgr.messages(None, U(1), 42))
    assert [m["id"] for m in ms] == [1, 2, 3], "oldest first"
    assert ms[1]["media"]["kind"] == "photo" and ms[2]["media"]["name"] == "notes.pdf"
    assert run(mgr.messages(None, U(1), 42, before_id=3)) and all(m["id"] < 3 for m in run(mgr.messages(None, U(1), 42, before_id=3)))
    sent = run(mgr.send_text(None, U(1), 42, "hi there", reply_to=1))
    assert sent["out"] and tgs[1].sent == [(42, "hi there", 1)]
    with pytest.raises(M.TGError):
        run(mgr.send_text(None, U(1), 42, "   "))


def test_attachments_arrive_unchanged(world):
    mgr, tgs, _, _ = world
    login(mgr, U(1), tgs[1])
    m = run(mgr.send_file(None, U(1), 42, b"\x89PNGdata", "shot.png", caption="look", mode="auto"))
    f = tgs[1].files[0]
    assert f == {"chat": 42, "name": "shot.png", "data": b"\x89PNGdata", "caption": "look",
                 "document": False, "voice": False, "video_note": False}
    assert m["media"]["kind"] == "photo"
    run(mgr.send_file(None, U(1), 42, b"x", "a/b.pdf", mode="document"))
    assert tgs[1].files[1]["document"] and tgs[1].files[1]["name"] == "a_b.pdf", "a path in a name is not a path"
    with pytest.raises(M.TGError):
        run(mgr.send_file(None, U(1), 42, b"", "empty.bin"))


def test_media_download(world):
    mgr, tgs, _, _ = world
    login(mgr, U(1), tgs[1])
    data, mime, name = run(mgr.media(None, U(1), 42, 3))
    assert data == b"FULL:notes.pdf" and mime == "application/pdf" and name == "notes.pdf"
    assert run(mgr.media(None, U(1), 42, 2, thumb=True))[:2] == (b"THUMBcat.jpg", "image/jpeg")
    with pytest.raises(M.TGError):
        run(mgr.media(None, U(1), 42, 1))


def test_one_account_never_sees_another(world):
    mgr, tgs, _, _ = world
    login(mgr, U(1), tgs[1])
    with pytest.raises(M.TGError, match="Sign in"):
        run(mgr.dialogs(None, U(2)))


def test_live_events_reach_every_open_window(world):
    mgr, tgs, _, made = world
    login(mgr, U(1), tgs[1])
    q1, q2 = mgr.subscribe(1), mgr.subscribe(1)
    other = mgr.subscribe(2)
    client = made[-1]
    new = client.handlers[0]

    class Ev:
        chat_id = 42
        message = _Msg(9, "ping")

        async def get_chat(self):
            return _User(42, "Alice")
    run(new(Ev()))
    for q in (q1, q2):
        ev = q.get_nowait()
        assert ev["type"] == "message" and ev["message"]["text"] == "ping" and ev["chat"]["title"] == "Alice"
    assert other.empty(), "another account's window received this account's message"


# ---- the router ------------------------------------------------------------------------------------

@pytest.fixture
def api(world, monkeypatch):
    mgr, tgs, store, _ = world
    from app.routers import telegram_client as R
    monkeypatch.setattr(R, "manager", lambda: mgr)
    who = {"user": U(1)}

    async def member():
        return who["user"]
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.member] = member
    app.dependency_overrides[R.get_db] = lambda: None
    from app.auth import get_current_user_optional
    app.dependency_overrides[get_current_user_optional] = lambda: None

    class _Q:
        def __init__(self, users):
            self.users = users

        def filter(self, *a):
            return self

        def first(self):
            return self.users.get(R._TICKETS and list(R._TICKETS.values())[-1][1])

    class _DB:
        def query(self, model):
            return _Q({1: U(1), 2: U(2)})
    app.dependency_overrides[R.get_db] = lambda: _DB()
    login(mgr, U(1), tgs[1])
    return TestClient(app), R, who, tgs


def test_the_router_sends_an_attachment_and_serves_it_back_behind_a_ticket(api):
    c, R, who, tgs = api
    r = c.post("/api/tgc/send-file", data={"chat_id": "42", "caption": "hi", "mode": "auto"},
               files={"file": ("shot.png", b"\x89PNGbytes", "image/png")})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert tgs[1].files[-1]["data"] == b"\x89PNGbytes" and tgs[1].files[-1]["name"] == "shot.png"
    assert c.get("/api/tgc/media/42/3").status_code == 401, "media without a session or ticket"
    t = c.get("/api/tgc/ticket").json()["t"]
    m = c.get(f"/api/tgc/media/42/3?t={t}")
    assert m.status_code == 200 and m.content == b"FULL:notes.pdf"
    assert m.headers["x-content-type-options"] == "nosniff"
    assert c.get("/api/tgc/media/42/3?t=forged").status_code == 401
    R._TICKETS[t] = (time.time() - 1, 1)
    assert c.get(f"/api/tgc/media/42/3?t={t}").status_code == 401, "an expired ticket still opened media"


def test_the_router_turns_refusals_into_sentences(api):
    c, R, who, tgs = api
    r = c.post("/api/tgc/send", json={"chat_id": 42, "text": "   "})
    assert r.status_code == 400 and r.json()["error"]

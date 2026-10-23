"""THE SERVER HALF OF THE PUSH CHAIN, PRODUCED BY THE SHIPPED CODE — never typed into a test.

Every push test before 2026-10-22 fed one end of the Android push chain a HAND-MADE payload: the
server tests asserted on a dict they built a stand-in transport for, the Java tests rendered a JSON
string somebody typed. Two imitations can agree with each other while the real producer and the real
consumer disagree — which is exactly how an SMS phone→desktop notification bug survived for months.

This module drives the REAL producers end to end and hands back what actually goes on the wire:

  * a REAL relay (tests/push_relay_harness.py — the shipped RelayServer + PosterChanDB store) that
    the triggering events are PUBLISHED to, signed with real keys (NIP-17 gift wraps via
    app/services/nostr/nip17.py, the server's own implementation);
  * the REAL watchers reading it: `_poll` (mentions, replies, reactions, follows), the live
    `_dm_sub_loop` and `_call_sub_loop` subscriptions, and `mail_notify_service._push`;
  * the REAL device registration (`/api/push/direct/register`, `/api/push/subscribe`) and the REAL
    preference mirror (`/api/push/prefs`) and own-sent ledger (`/api/push/sent`), each called with a
    genuinely signed proof;
  * the REAL transports: `push_service.send` → `direct_push_service.enqueue_result` → the durable
    queue → the shipped `/api/push/direct/ws` handler and `serve` loop, whose frames are serialised by
    STARLETTE'S OWN `WebSocket.send_json` — so the text captured is byte-for-byte what a phone reads.
    Web Push is captured at the last line of our code: the `data=` handed to pywebpush.

What is NOT real, and why: pywebpush itself (it would POST to a vendor push service), and the relay
port lookup in `_local_relay` (a settings read; pointed at the harness relay).
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import types

from app.services.nostr import bip340, nip17
from app.services.nostr import event as nostr_event

ALICE_SK = bytes.fromhex("a1" * 32)
BOB_SK = bytes.fromhex("b2" * 32)
ALICE = bip340.pubkey_from_seckey(ALICE_SK).hex()
BOB = bip340.pubkey_from_seckey(BOB_SK).hex()
DEVICE = "12345678-aaaa-bbbb-cccc-123456789abc"
WEB_ENDPOINT = "https://push.example.invalid/bob-browser"


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _self_auth(sk, purpose):
    pk = bip340.pubkey_from_seckey(sk).hex()
    ev = nostr_event.build_event(sk, 27235, purpose, tags=[["p", pk]])
    return base64.b64encode(json.dumps(ev).encode()).decode()


def _direct_auth(sk, action, device):
    ev = nostr_event.build_event(sk, 27235, f"posterchan-direct:{action}:{device}")
    return base64.urlsafe_b64encode(json.dumps(ev).encode()).decode()


class Phone:
    """A stand-in for the Android socket: authenticates, ACKs every notification, records the TEXT.

    `send_json` is starlette's own implementation bound to this object, so the serialisation (compact
    separators, ensure_ascii=False) is the production one, not a json.dumps written here."""

    def __init__(self, token):
        from starlette.websockets import WebSocket
        self._send_json = WebSocket.send_json
        self.token = token
        self.texts: list[str] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._incoming.put_nowait({"type": "auth", "token": token})
        self.closed = None

    async def accept(self):
        pass

    async def send(self, message):
        assert message["type"] == "websocket.send"
        self.texts.append(message["text"])
        frame = json.loads(message["text"])
        if frame.get("type") == "notification":
            # The phone ACKs after rendering; the chain test proves the Java side does render it.
            await self._incoming.put({"type": "ack", "id": frame["id"]})

    async def send_json(self, data, mode="text"):
        await self._send_json(self, data, mode)

    async def receive_json(self):
        return await self._incoming.get()

    async def close(self, code=1000):
        self.closed = code


class Server:
    """One relay, one recipient (Bob: a phone on PosterChan Direct + a browser on Web Push)."""

    def __init__(self, tmp_path, monkeypatch):
        from tests import push_relay_harness as H
        from app.services import nostr_push_service as nps, push_service, settings_store
        self.H, self.nps = H, nps
        self.relay = H.start(tmp_path, monkeypatch)
        self.url = f"ws://127.0.0.1:{self.relay.port}"
        monkeypatch.setattr(nps, "_local_relay", lambda: [self.url])
        # A clean watcher: these are process-lifetime caches, and a previous test's would leak in.
        for name, val in (("_cursor", 0), ("_sub_pks_at", 0.0)):
            monkeypatch.setattr(nps, name, val)
        for name in ("_seen", "_names", "_dm_recent", "_call_recent", "_sub_pks"):
            monkeypatch.setattr(nps, name, type(getattr(nps, name))())
        # Web Push: the payload handed to pywebpush is the last byte our code decides.
        self.webpush: list[str] = []
        fake = types.ModuleType("pywebpush")

        class WebPushException(Exception):
            pass

        def webpush(subscription_info=None, data=None, **_kw):
            self.webpush.append(data)
        fake.webpush, fake.WebPushException = webpush, WebPushException
        monkeypatch.setitem(sys.modules, "pywebpush", fake)
        real_get = settings_store.get
        monkeypatch.setattr(settings_store, "get",
                            lambda k, d=None: "test-vapid-key" if k == "push_vapid_private" else real_get(k, d))
        monkeypatch.setattr(push_service.direct_push_service, "wake", lambda *_a: None)
        self.loop = asyncio.new_event_loop()
        self.token = None
        self.sid = None
        self._tasks = []

    # ---- lifecycle -----------------------------------------------------------------------------
    def run(self, coro, timeout=60):
        return self.loop.run_until_complete(asyncio.wait_for(coro, timeout))

    def close(self):
        try:
            for stop in (self.nps._dm_stop, self.nps._call_stop):
                if stop is not None:
                    stop.set()
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                self.loop.run_until_complete(asyncio.gather(*self._tasks, return_exceptions=True))
        finally:
            self.nps._dm_stop = self.nps._call_stop = None
            self.loop.close()
            self.H.reset()
            self.relay.close()

    # ---- setup through the shipped routes -------------------------------------------------------
    async def register(self):
        from app.routers import push as R
        r = await R.register_direct(_Req({"pubkey": BOB, "device_id": DEVICE,
                                          "auth": _direct_auth(BOB_SK, "register", DEVICE)}))
        assert r.get("ok"), r
        self.token = r["token"]
        rows = await R.push_store.subs_where(lambda x: x.get("device_id") == DEVICE)
        self.sid = rows[0]["id"]
        w = await R.subscribe(_Req({"pubkey": BOB, "auth": _self_auth(BOB_SK, "push-subscribe"),
                                    "subscription": {"endpoint": WEB_ENDPOINT,
                                                     "keys": {"p256dh": "p" * 20, "auth": "a" * 8}}}))
        assert w.get("ok"), w
        await self.publish(nostr_event.build_event(ALICE_SK, 0, json.dumps({"name": "Alice"})))
        # The live watchers, exactly as start_nostr_push_scheduler starts them.
        self.nps._dm_stop, self.nps._call_stop = asyncio.Event(), asyncio.Event()
        self._tasks = [asyncio.ensure_future(self.nps._dm_sub_loop()),
                       asyncio.ensure_future(self.nps._call_sub_loop())]
        await self.nps._poll()            # the first poll only sets the cursor, by design
        await asyncio.sleep(1.5)          # let both live subscriptions reach EOSE

    async def set_prefs(self, prefs, *, device=True):
        from app.routers import push as R
        body = {"pubkey": BOB, "auth": _self_auth(BOB_SK, "push-prefs"), "prefs": prefs}
        body.update({"device_id": DEVICE} if device else {"endpoint": WEB_ENDPOINT})
        r = await R.set_prefs(_Req(body))
        assert r.get("ok") and r.get("devices") == 1, r

    async def note_sent(self, wrap_ids):
        from app.routers import push as R
        r = await R.note_sent_wraps(_Req({"pubkey": BOB, "auth": _self_auth(BOB_SK, "push-sent"),
                                          "ids": list(wrap_ids)}))
        assert r.get("ok"), r

    async def publish(self, ev):
        from app.services.nostr import relay
        n = await relay.publish([self.url], ev)
        assert n == 1, f"the relay refused kind {ev.get('kind')}"
        return ev

    # ---- the triggering events --------------------------------------------------------------------
    def note(self, kind, content, tags, sk=ALICE_SK):
        return nostr_event.build_event(sk, kind, content, tags=tags)

    def dm_to_bob(self, text="hi bob"):
        return nip17.wrap(ALICE_SK, BOB, text)

    def bobs_self_copy(self, text="hi alice"):
        """The SELF-COPY NIP-17 makes of a message Bob sends (from his desktop): p-tagged to Bob."""
        return nip17.wrap(BOB_SK, BOB, text)

    async def mail(self, title="📧 New email", body="From: carol@example.com — Lunch?"):
        from app.services import mail_notify_service
        from app.services.nostr import bech32
        user = types.SimpleNamespace(id=1, nostr_npub=bech32.encode("npub", bytes.fromhex(BOB)))
        await mail_notify_service._push(None, user, title, body)

    # ---- what reached the wire --------------------------------------------------------------------
    async def queued(self):
        from app.services import push_store
        return await asyncio.to_thread(push_store.queue_for, self.sid)

    async def wait_queued(self, n=1, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if len(await self.queued()) >= n:
                return True
            await asyncio.sleep(0.1)
        return False

    async def drain(self, settle=0.6):
        """Connect the phone to the SHIPPED socket handler, take every frame, ACK them, hang up.

        Returns the notification frames as the exact TEXT the phone receives."""
        from app.routers import push as R
        phone = Phone(self.token)
        task = asyncio.ensure_future(R.direct_socket(phone))
        try:
            await asyncio.sleep(settle)
            for _ in range(50):
                if not await self.queued():
                    break
                await asyncio.sleep(0.1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert phone.texts and json.loads(phone.texts[0]).get("type") == "ready", phone.texts[:1]
        return [t for t in phone.texts if json.loads(t).get("type") == "notification"]

    def take_webpush(self):
        out, self.webpush[:] = list(self.webpush), []
        return out

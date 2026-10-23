"""One Telegram connection per account, shared by every window that account opens.

Everything network-shaped goes through `client_factory` so tests drive the real state machine with a
fake Telethon client. The session string is persisted through `store` (session_store by default):
only AFTER a successful sign-in, and again whenever Telethon rotates it.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_UPLOAD = 1536 * 1024 * 1024      # Telegram's own limit for a non-premium account is 2 GB
PAGE = 50


def _muted(ns) -> bool:
    """A notify-settings object is muted only while its mute_until is in the FUTURE."""
    mu = getattr(ns, "mute_until", None) if ns is not None else None
    if not mu:
        return False
    t = mu.timestamp() if hasattr(mu, "timestamp") else float(mu)
    return t > time.time()


class TGError(Exception):
    """A refusal a person can act on — its text is shown as-is."""


class TGUnreachable(TGError):
    """Telegram itself could not be reached from this server. Not a refusal and not "signed out": the saved session is
    kept and the router answers 503, so the screen says this and keeps retrying (and shows what the device holds)."""


# HOW LONG ONE CONNECT MAY TAKE. Telethon's own connect() retries for up to ~a minute (5 x 10 s), and it runs while the
# account's lock is held -- so a connect to a Telegram that is not answering held EVERY request of that account for
# that long, Retry included: the screen gave up at 15 s and Retry waited on the same lock. Bounded, the next request
# makes a fresh attempt. Under the client's own 15 s, so the person reads why rather than "did not answer".
CONNECT_LIMIT = 12
UNREACHABLE = ("This server cannot reach Telegram right now. Your sign-in is kept — it will connect again "
               "by itself.")


def parse_api_credentials(raw_id, raw_hash):
    """The api_id and api_hash as Telegram wants them, from what an admin PASTED.

    my.telegram.org shows them as labelled rows ("App api_id: 1234567", "App api_hash: 0123…cdef"), and
    copying the row copies the label: the stored hash measured 46 characters, the 32-hex hash plus its
    label, so every sign-in was refused with "The server's Telegram API id/hash are not valid". The hash
    is the 32-hex run in the value and the id is its run of digits; anything else is not configured."""
    import re
    id_m = re.search(r"(?<!\d)(\d{3,12})(?!\d)", str(raw_id or ""))
    hash_m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", str(raw_hash or ""))
    if not id_m or not hash_m:
        return None
    return int(id_m.group(1)), hash_m.group(1).lower()


def _api_config():
    from app.services import settings_store
    return parse_api_credentials(settings_store.get("telegram_api_id", ""), settings_store.get("telegram_api_hash", ""))


def telethon_available() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("telethon") is not None
    except (ImportError, ValueError):
        return False


def default_client_factory(session: str, api):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(session or ""), api[0], api[1],
                          device_model="PosterChan", system_version="web",
                          app_version="PosterChan 1", lang_code="en", system_lang_code="en")


@dataclass
class Account:
    user_id: int
    client: object = None
    state: str = "none"             # none | code | password | ready
    phone: str = ""
    code_hash: str = ""
    listeners: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    me: dict = field(default_factory=dict)
    handlers_on: bool = False
    last_session: str = ""
    calls: object = None             # calls.CallHub — voice/video calls, once the account is ready
    reaction_set: tuple = ()         # (when read, [emoji]) — the reactions Telegram has at all
    chat_reactions: dict = field(default_factory=dict)   # chat → (when read, [emoji]) it allows


def _name(ent) -> str:
    if ent is None:
        return ""
    t = getattr(ent, "title", None)
    if t:
        return str(t)
    parts = [getattr(ent, "first_name", "") or "", getattr(ent, "last_name", "") or ""]
    n = " ".join(p for p in parts if p).strip()
    return n or (getattr(ent, "username", "") or "") or str(getattr(ent, "id", ""))


def _kind(ent) -> str:
    if ent is None:
        return "user"
    if getattr(ent, "broadcast", False):
        return "channel"
    if hasattr(ent, "title"):
        return "group"
    return "bot" if getattr(ent, "bot", False) else "user"


def media_of(msg) -> dict | None:
    """What a message carries, described without downloading it."""
    m = getattr(msg, "media", None)
    if not m:
        return None
    f = getattr(msg, "file", None)
    out = {"kind": "file", "name": (getattr(f, "name", None) or ""), "size": int(getattr(f, "size", 0) or 0),
           "mime": (getattr(f, "mime_type", None) or "")}
    if getattr(msg, "photo", None):
        out["kind"] = "photo"
        out["mime"] = out["mime"] or "image/jpeg"
    elif getattr(msg, "sticker", None):
        out["kind"] = "sticker"
    elif getattr(msg, "video_note", None):
        out["kind"] = "video_note"
    elif getattr(msg, "voice", None):
        out["kind"] = "voice"
    elif getattr(msg, "gif", None):
        out["kind"] = "gif"
    elif getattr(msg, "video", None):
        out["kind"] = "video"
    elif getattr(msg, "audio", None):
        out["kind"] = "audio"
    elif getattr(msg, "web_preview", None) or type(m).__name__ == "MessageMediaWebPage":
        return None                                  # a link preview is not an attachment
    dur = getattr(f, "duration", None)
    if dur:
        out["duration"] = int(dur)
    return out


def reactions_of(msg) -> list:
    """A message's emoji reactions: [{emoji, count, mine}]. Custom-emoji and paid reactions carry no
    emoji a browser can draw, so they are left out rather than shown as an empty chip."""
    out = []
    rs = getattr(msg, "reactions", None)
    # YOURS, two ways. `chosen_order` is the usual mark, but on a message somebody ELSE sent Telegram can
    # report your reaction only in `recent_reactions` (`my`). Read as "not yours", a tap asked Telegram to
    # add what it already held and was refused: "Content of the message was not modified".
    my_recent = {str(getattr(getattr(p, "reaction", None), "emoticon", "") or "")
                 for p in (getattr(rs, "recent_reactions", None) or []) if getattr(p, "my", False)}
    for rc in (getattr(rs, "results", None) or []):
        emoji = getattr(getattr(rc, "reaction", None), "emoticon", None)
        if not emoji:
            continue
        out.append({"emoji": str(emoji), "count": int(getattr(rc, "count", 0) or 0),
                    "mine": getattr(rc, "chosen_order", None) is not None or str(emoji) in my_recent})
    return out


REACTIONS_TTL = 600                  # s — a chat's allowed reactions change when an admin edits them


def _same_emoji(a: str, b: str) -> bool:
    """One emoji whichever way it was typed: Telegram's heart is ❤ and every keyboard sends ❤️ (U+FE0F)."""
    return str(a).replace("\ufe0f", "") == str(b).replace("\ufe0f", "")


def message_dict(msg, chat_id) -> dict:
    sender = getattr(msg, "sender", None)
    reply = getattr(msg, "reply_to", None)
    date = getattr(msg, "date", None)
    return {"id": int(msg.id), "chat_id": int(chat_id), "out": bool(getattr(msg, "out", False)),
            "date": int(date.timestamp()) if date else 0,
            "text": getattr(msg, "message", None) or getattr(msg, "text", None) or "",
            "sender_id": int(getattr(msg, "sender_id", 0) or 0), "sender": _name(sender),
            "reply_to": int(getattr(reply, "reply_to_msg_id", 0) or 0) if reply else 0,
            "media": media_of(msg), "edited": bool(getattr(msg, "edit_date", None)),
            "reactions": reactions_of(msg)}


class Manager:
    def __init__(self, client_factory=default_client_factory, store=None, api=_api_config, tgcalls_factory=None):
        self._factory = client_factory
        self._tgcalls_factory = tgcalls_factory      # tests hand in a fake call engine
        self._api = api
        if store is None:
            from app.services.telegram_client import session_store as store
        self._store = store
        self._accounts: dict[int, Account] = {}

    # ---- lifecycle ------------------------------------------------------------------------------
    def configured(self) -> bool:
        # No library = not set up, said as such on screen, rather than a sign-in that 500s on import
        # (a node installed from an older requirements list, or a hand-built venv).
        return self._api() is not None and (self._factory is not default_client_factory or telethon_available())

    def account(self, user_id: int) -> Account:
        a = self._accounts.get(user_id)
        if a is None:
            a = self._accounts[user_id] = Account(user_id=user_id)
        return a

    async def _client(self, a: Account, session: str = ""):
        api = self._api()
        if api is None:
            raise TGError("Telegram is not set up on this server yet — an admin enters the API id and "
                          "hash from my.telegram.org in Admin → Telegram.")
        if not self.configured():
            raise TGError("Telegram is not set up on this server yet — its Telegram library is missing "
                          "(an admin re-runs the installer, or Admin → Telegram once it is installed).")
        if a.client is None:
            a.client = self._factory(session, api)
        if not a.client.is_connected():
            try:
                await asyncio.wait_for(a.client.connect(), CONNECT_LIMIT)
            except (OSError, asyncio.TimeoutError) as e:      # ConnectionError is an OSError
                logger.info("[tgc] Telegram unreachable: %s", type(e).__name__)
                await _safe(a.client.disconnect())
                raise TGUnreachable(UNREACHABLE) from e
        return a.client

    async def resume(self, db, user) -> Account:
        """Bring back a saved session (first open after a restart, or the startup sweep)."""
        a = self.account(user.id)
        async with a.lock:
            if a.state == "ready" and a.client is not None:
                return a
            saved = await self._store.load(db, user)
            if not saved:
                return a
            c = await self._client(a, saved)
            if await c.is_user_authorized():
                a.state = "ready"
                a.last_session = saved
                await self._on_ready(db, user, a)
            else:
                # The session was revoked elsewhere (Settings → Devices in Telegram). Forget it.
                await self._store.clear(db, user)
                a.state = "none"
        return a

    async def status(self, db, user) -> dict:
        if not self.configured():
            # `admin` decides which instructions the screen shows: the steps to set it up, or who to ask.
            return {"configured": False, "state": "none", "admin": bool(getattr(user, "is_admin", False))}
        a = await self.resume(db, user)
        return {"configured": True, "state": a.state, "me": a.me, "phone": a.phone if a.state != "ready" else "",
                "calls": self.calls_available()}

    # ---- sign in ----------------------------------------------------------------------------------
    async def send_code(self, db, user, phone: str) -> dict:
        phone = "".join(ch for ch in str(phone or "") if ch.isdigit() or ch == "+")
        if len(phone.lstrip("+")) < 6:
            raise TGError("Enter your phone number with its country code, e.g. +1 555 010 4477.")
        a = self.account(user.id)
        async with a.lock:
            c = await self._client(a)
            try:
                sent = await c.send_code_request(phone)
            except Exception as e:
                raise TGError(_why(e)) from e
            a.phone, a.code_hash, a.state = phone, getattr(sent, "phone_code_hash", ""), "code"
        return {"state": "code"}

    async def sign_in_code(self, db, user, code: str) -> dict:
        a = self.account(user.id)
        async with a.lock:
            if a.state != "code":
                raise TGError("Ask for a login code first.")
            c = await self._client(a)
            try:
                await c.sign_in(phone=a.phone, code=str(code or "").strip(), phone_code_hash=a.code_hash)
            except Exception as e:
                if type(e).__name__ == "SessionPasswordNeededError":
                    a.state = "password"
                    return {"state": "password"}
                raise TGError(_why(e)) from e
            await self._signed_in(db, user, a)
        return {"state": "ready", "me": a.me}

    async def sign_in_password(self, db, user, password: str) -> dict:
        a = self.account(user.id)
        async with a.lock:
            if a.state != "password":
                raise TGError("This account does not need a password right now.")
            c = await self._client(a)
            try:
                await c.sign_in(password=str(password or ""))
            except Exception as e:
                raise TGError(_why(e)) from e
            await self._signed_in(db, user, a)
        return {"state": "ready", "me": a.me}

    async def _signed_in(self, db, user, a: Account) -> None:
        a.state, a.code_hash = "ready", ""
        s = a.client.session.save()
        if not await self._store.save(db, user, s):
            # The login worked; only persistence failed. Say so rather than pretend it will survive.
            raise TGError("Signed in, but the session could not be saved — it will not survive a "
                          "restart. Try signing in again in a moment.")
        a.last_session = s
        await self._on_ready(db, user, a)

    async def logout(self, db, user) -> dict:
        a = self.account(user.id)
        async with a.lock:
            if a.client is not None:
                try:
                    if a.state == "ready":
                        await a.client.log_out()
                    else:
                        await a.client.disconnect()
                except Exception:
                    pass
            await self._store.clear(db, user)
            self._accounts.pop(user.id, None)
        await self._emit(a, {"type": "state", "state": "none"})
        return {"state": "none"}

    async def _on_ready(self, db, user, a: Account) -> None:
        try:
            me = await a.client.get_me()
            a.me = {"id": int(getattr(me, "id", 0) or 0), "name": _name(me), "username": getattr(me, "username", "") or "",
                    "premium": bool(getattr(me, "premium", False))}
        except Exception:
            a.me = {}
        if not a.handlers_on:
            self._install_handlers(a)
            a.handlers_on = True
        await self._emit(a, {"type": "state", "state": "ready", "me": a.me})
        # Calls must be listening BEFORE anyone presses Call, or an incoming call rings nowhere.
        # Best effort: a node without the call engine still does everything else.
        if self.calls_available() and (self._tgcalls_factory is not None or self._factory is default_client_factory):
            asyncio.get_running_loop().create_task(self._start_calls(a))

    # ---- voice and video calls ----------------------------------------------------------------------
    def calls_available(self) -> bool:
        from app.services.telegram_client import calls
        return self._tgcalls_factory is not None or calls.available()

    def _hub(self, a: Account):
        if a.calls is None:
            from app.services.telegram_client.calls import CallHub

            async def emit(ev):
                await self._emit(a, ev)
            a.calls = CallHub(a.client, emit, tgcalls_factory=self._tgcalls_factory)
            a.calls.watch_requests(a.client)
        return a.calls

    async def _start_calls(self, a: Account):
        try:
            await self._hub(a)._engine()
        except Exception as e:
            logger.warning("Telegram calls could not start for account %s: %s", a.user_id, type(e).__name__)

    async def call_hub(self, db, user):
        await self._ready(db, user)
        if not self.calls_available():
            raise TGError("Calls are not set up on this server yet — its call engine (py-tgcalls) is "
                          "missing; an admin re-runs the installer.")
        return self._hub(self.account(user.id))

    async def call(self, db, user, action: str, peer: int = 0, video: bool = False) -> dict:
        from app.services.telegram_client.calls import CallError
        hub = await self.call_hub(db, user)
        try:
            if action == "start":
                return await hub.start(int(peer), bool(video))
            if action == "accept":
                return await hub.accept(int(peer), bool(video))
            if action == "hangup":
                return await hub.hangup(int(peer) or None)
            if action == "camera":
                return await hub.camera(bool(video))
            if action == "state":
                return {"peer": hub.peer, "state": hub.state, "title": hub.title, "video": hub.video,
                        "remote_video": hub.remote_video}
        except CallError as e:
            raise TGError(str(e))
        except TGError:
            raise
        except Exception as e:
            raise TGError(_why(e))
        raise TGError("Unknown call action.")

    # ---- live events ------------------------------------------------------------------------------
    def _install_handlers(self, a: Account) -> None:
        from telethon import events

        async def on_new(ev):
            await self._emit(a, {"type": "message", "message": message_dict(ev.message, ev.chat_id),
                                 "chat": {"id": int(ev.chat_id), "title": _name(await _safe(ev.get_chat()))}})

        async def on_edit(ev):
            await self._emit(a, {"type": "edited", "message": message_dict(ev.message, ev.chat_id)})

        async def on_delete(ev):
            await self._emit(a, {"type": "deleted", "chat_id": int(getattr(ev, "chat_id", 0) or 0),
                                 "ids": [int(i) for i in (ev.deleted_ids or [])]})

        async def on_reactions(update):
            # Somebody reacted. In groups Telegram says so with this update alone, never an edit.
            if type(update).__name__ != "UpdateMessageReactions":
                return
            try:
                from telethon import utils
                chat_id = int(utils.get_peer_id(update.peer))
            except Exception:
                return
            await self._emit(a, {"type": "reactions", "chat_id": chat_id, "id": int(update.msg_id),
                                 "reactions": reactions_of(update)})

        async def on_read(update):
            """READ SOMEWHERE ELSE, OR READ BY THEM. Telegram sends one of four updates: your messages
            were read by the other side (outbox → ✓✓), or you read theirs on another device, e.g. the
            phone (inbox → the unread count here must drop to what Telegram says is still unread,
            not stay at whatever this window last counted). Groups/channels use the channel variants."""
            name = type(update).__name__
            if name not in ("UpdateReadHistoryInbox", "UpdateReadHistoryOutbox",
                            "UpdateReadChannelInbox", "UpdateReadChannelOutbox"):
                return
            try:
                from telethon import utils
                from telethon.tl.types import PeerChannel
                peer = update.peer if "History" in name else PeerChannel(update.channel_id)
                chat_id = int(utils.get_peer_id(peer))
            except Exception:
                return
            ev = {"type": "read", "chat_id": chat_id, "max_id": int(getattr(update, "max_id", 0) or 0),
                  "outbox": name.endswith("Outbox")}
            if not ev["outbox"]:
                # A forum TOPIC read carries that topic's count, not the chat's -- leave the chat's alone.
                if getattr(update, "top_msg_id", None):
                    return
                ev["unread"] = int(getattr(update, "still_unread_count", 0) or 0)
            await self._emit(a, ev)

        a.client.add_event_handler(on_new, events.NewMessage())
        a.client.add_event_handler(on_edit, events.MessageEdited())
        a.client.add_event_handler(on_delete, events.MessageDeleted())
        a.client.add_event_handler(on_reactions, events.Raw())
        a.client.add_event_handler(on_read, events.Raw())

    def subscribe(self, user_id: int) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=500)
        self.account(user_id).listeners.add(q)
        return q

    def unsubscribe(self, user_id: int, q) -> None:
        a = self._accounts.get(user_id)
        if a:
            a.listeners.discard(q)

    async def _emit(self, a: Account, event: dict) -> None:
        for q in list(a.listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                a.listeners.discard(q)          # a window that stopped reading is dropped, not waited on

    # ---- reading and sending ------------------------------------------------------------------------
    async def _ready(self, db, user):
        a = await self.resume(db, user)
        if a.state != "ready":
            raise TGError("Sign in to Telegram first.")
        return a.client

    async def dialogs(self, db, user, limit: int = 100) -> list:
        c = await self._ready(db, user)
        out = []
        async for d in c.iter_dialogs(limit=max(1, min(int(limit), 500))):
            last = getattr(d, "message", None)
            raw = getattr(d, "dialog", None)
            out.append({"id": int(d.id), "title": _name(d.entity) or getattr(d, "name", ""),
                        "kind": _kind(d.entity), "unread": int(getattr(d, "unread_count", 0) or 0),
                        # How far the OTHER side has read what you sent (✓✓ up to here), and how far
                        # you have read theirs. 0 = unknown, which the client draws as a single ✓.
                        "read_out": int(getattr(raw, "read_outbox_max_id", 0) or 0),
                        "read_in": int(getattr(raw, "read_inbox_max_id", 0) or 0),
                        "pinned": bool(getattr(d, "pinned", False)),
                        # A mute that has ENDED is not a mute: Telegram keeps the past date there.
                        "muted": _muted(getattr(getattr(d, "dialog", None), "notify_settings", None)),
                        "silent": bool(getattr(getattr(getattr(d, "dialog", None), "notify_settings", None), "silent", False)),
                        "last": {"id": int(getattr(last, "id", 0) or 0) if last else 0,
                                 "text": (getattr(last, "message", "") or "")[:200] if last else "",
                                 "date": int(last.date.timestamp()) if last and getattr(last, "date", None) else 0,
                                 "out": bool(getattr(last, "out", False)) if last else False,
                                 "media": (media_of(last) or {}).get("kind") if last else None}})
        return out

    async def messages(self, db, user, chat_id: int, before_id: int = 0, limit: int = PAGE) -> list:
        c = await self._ready(db, user)
        out = []
        async for m in c.iter_messages(int(chat_id), limit=max(1, min(int(limit), 200)),
                                       offset_id=int(before_id or 0)):
            out.append(message_dict(m, chat_id))
        return list(reversed(out))                 # oldest first, the order a chat is read in

    async def send_text(self, db, user, chat_id: int, text: str, reply_to: int = 0) -> dict:
        text = str(text or "")
        if not text.strip():
            raise TGError("Nothing to send.")
        if len(text) > 4096:
            raise TGError("A Telegram message is at most 4096 characters.")
        c = await self._ready(db, user)
        m = await c.send_message(int(chat_id), text, reply_to=int(reply_to) or None)
        return message_dict(m, chat_id)

    async def send_file(self, db, user, chat_id: int, data: bytes, filename: str, caption: str = "",
                        mode: str = "auto", reply_to: int = 0) -> dict:
        if not data:
            raise TGError("That file is empty.")
        if len(data) > MAX_UPLOAD:
            raise TGError("That file is larger than Telegram accepts.")
        c = await self._ready(db, user)
        f = io.BytesIO(data)
        f.name = (filename or "file").replace("/", "_")[:200]
        m = await c.send_file(int(chat_id), f, caption=str(caption or "")[:1024] or None,
                              force_document=(mode == "document"), voice_note=(mode == "voice"),
                              video_note=(mode == "video_note"), supports_streaming=True,
                              reply_to=int(reply_to) or None)
        return message_dict(m, chat_id)

    async def _all_reactions(self, a: Account) -> list:
        """Every emoji Telegram accepts as a reaction from THIS account, in Telegram's order. It is a
        fixed list, not "any emoji" — 😂 and 😮 are not on it, and its heart is ❤ with no variation
        selector — and some entries are Premium-only."""
        when, got = a.reaction_set or (0, [])
        if got and time.time() - when < REACTIONS_TTL:
            return got
        from telethon.tl.functions.messages import GetAvailableReactionsRequest
        res = await a.client(GetAvailableReactionsRequest(hash=0))
        premium = bool((a.me or {}).get("premium"))
        got = [str(r.reaction) for r in (getattr(res, "reactions", None) or [])
               if getattr(r, "reaction", None) and not getattr(r, "inactive", False)
               and (premium or not getattr(r, "premium", False))]
        if got:
            a.reaction_set = (time.time(), got)
        return got

    async def allowed_reactions(self, db, user, chat_id: int) -> list:
        """The reactions THIS chat takes: a private chat takes all of them; a group or channel admin
        may narrow that to a few, or turn reactions off (an empty list). Raises TGError when Telegram
        cannot be asked — the caller decides what an unknown answer means."""
        c = await self._ready(db, user)
        a = self.account(user.id)
        chat_id = int(chat_id)
        when, got = a.chat_reactions.get(chat_id, (0, None))
        if got is not None and time.time() - when < REACTIONS_TTL:
            return got
        try:
            every = await self._all_reactions(a)
            rule = None
            if chat_id < 0:
                from telethon.tl.functions.channels import GetFullChannelRequest
                from telethon.tl.functions.messages import GetFullChatRequest
                ent = await c.get_input_entity(chat_id)
                if type(ent).__name__ == "InputPeerChat":
                    full = await c(GetFullChatRequest(chat_id=ent.chat_id))
                else:
                    full = await c(GetFullChannelRequest(channel=ent))
                rule = getattr(getattr(full, "full_chat", None), "available_reactions", None)
        except TGError:
            raise
        except Exception as e:
            raise TGError(_why(e))
        kind = type(rule).__name__
        if kind == "ChatReactionsNone":
            got = []
        elif kind == "ChatReactionsSome":
            some = [str(getattr(r, "emoticon", "") or "") for r in (getattr(rule, "reactions", None) or [])]
            got = [e for e in some if e and (e in every or not every)]
        elif every:                             # a person, ChatReactionsAll, or no rule given
            got = list(every)
        else:                                   # never "no reactions" on the strength of an empty read
            raise TGError("Telegram did not say which reactions it takes.")
        a.chat_reactions[chat_id] = (time.time(), got)
        return got

    async def react(self, db, user, chat_id: int, msg_id: int, emoji: str) -> list:
        """Toggle YOUR reaction on one message and answer with its reactions as Telegram now has them.
        Choosing the reaction you already gave takes it back; choosing another replaces it (an
        ordinary account holds one reaction per message)."""
        emoji = str(emoji or "").strip()
        if not emoji or len(emoji) > 16 or any(ch.isascii() and ch.isalnum() for ch in emoji):
            raise TGError("That is not an emoji.")
        c = await self._ready(db, user)
        try:
            allowed = await self.allowed_reactions(db, user, chat_id)
        except TGError:
            allowed = None                      # could not ask: let Telegram be the judge
        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji
            m = await c.get_messages(int(chat_id), ids=int(msg_id))
            if m is None:
                raise TGError("That message is gone.")
            mine = {r["emoji"] for r in reactions_of(m) if r["mine"]}
            took_back = next((e for e in mine if _same_emoji(e, emoji)), None)
            if took_back is None and allowed is not None:
                # Telegram names the refusal REACTION_INVALID and nothing more; say which, before asking.
                hit = next((e for e in allowed if _same_emoji(e, emoji)), None)
                if hit is None:
                    raise TGError("This chat does not allow reactions." if not allowed else
                                  "Telegram does not take {} as a reaction here.".format(emoji))
                emoji = hit                     # Telegram's own spelling (❤, not ❤️)
            want = [] if took_back is not None else [ReactionEmoji(emoticon=emoji)]
            try:
                await c(SendReactionRequest(peer=int(chat_id), msg_id=int(msg_id), reaction=want))
            except Exception as e:
                if type(e).__name__ != "MessageNotModifiedError":
                    raise
                # Telegram already held exactly this. Asked to ADD, the reaction was already yours and
                # simply not reported as yours (it can say nothing on another person's message) — so the
                # tap means take it back. Asked to REMOVE, there was nothing to remove: done either way.
                if want:
                    await c(SendReactionRequest(peer=int(chat_id), msg_id=int(msg_id), reaction=[]))
            m = await c.get_messages(int(chat_id), ids=int(msg_id))
        except TGError:
            raise
        except Exception as e:
            raise TGError(_why(e))
        return reactions_of(m) if m is not None else []

    async def delete(self, db, user, chat_id: int, ids, revoke: bool = True) -> int:
        """Delete messages, for EVERYONE (`revoke`, Telegram's "also delete for <name>") or only here.

        Telegram answers with how many messages it actually removed (`pts_count`). Zero is a refusal --
        another person's message in a group you do not administer, a message already gone -- and is said
        as one, because a deletion reported done that did not happen leaves the message on the other
        person's screen while it vanishes from yours."""
        try:
            ids = sorted({int(i) for i in (ids or []) if int(i) > 0})
        except (TypeError, ValueError):
            raise TGError("Those are not message ids.")
        if not ids:
            raise TGError("Nothing to delete.")
        if len(ids) > 100:
            raise TGError("Telegram deletes at most 100 messages at a time.")
        c = await self._ready(db, user)
        try:
            done = await c.delete_messages(int(chat_id), ids, revoke=bool(revoke))
        except TGError:
            raise
        except Exception as e:
            raise TGError(_why(e))
        n = sum(int(getattr(r, "pts_count", 0) or 0) for r in (done if isinstance(done, list) else [done]))
        if n <= 0:
            raise TGError("Telegram would not delete that message." if len(ids) == 1
                          else "Telegram would not delete those messages.")
        return n

    # ---- per-chat controls: block, notifications, auto-delete (2026-10-21: "no way to block, mute notification
    # sounds from user, set chat expiration"). Each is Telegram's own setting, so it follows the account to the
    # official apps; a refusal (a group you do not administer) is said as one, never reported done.
    TTL_PERIODS = (0, 86400, 604800, 2592000)          # off, 1 day, 1 week, 1 month -- what Telegram's own apps offer

    async def peer_settings(self, db, user, chat_id: int) -> dict:
        """What the chat menu shows: blocked (people only), muted until, silent, auto-delete period."""
        c = await self._ready(db, user)
        from telethon.tl.functions.account import GetNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer
        try:
            ent = await c.get_input_entity(int(chat_id))
            ns = await c(GetNotifySettingsRequest(peer=InputNotifyPeer(peer=ent)))
            kind, blocked, ttl = "user", False, 0
            full = None
            try:
                from telethon.tl.functions.users import GetFullUserRequest
                full = await c(GetFullUserRequest(ent))
                fu = getattr(full, "full_user", None)
                blocked = bool(getattr(fu, "blocked", False)); ttl = int(getattr(fu, "ttl_period", 0) or 0)
            except Exception:
                kind = "chat"
                try:
                    from telethon.tl.functions.channels import GetFullChannelRequest
                    full = await c(GetFullChannelRequest(ent))
                except Exception:
                    from telethon.tl.functions.messages import GetFullChatRequest
                    full = await c(GetFullChatRequest(int(chat_id)))
                ttl = int(getattr(getattr(full, "full_chat", None), "ttl_period", 0) or 0)
        except TGError:
            raise
        except Exception as e:
            raise TGError(_why(e))
        mu = getattr(ns, "mute_until", None)
        until = int(mu.timestamp()) if hasattr(mu, "timestamp") else int(mu or 0)
        return {"kind": kind, "blocked": blocked, "muted": _muted(ns), "muted_until": until if _muted(ns) else 0,
                "silent": bool(getattr(ns, "silent", False)), "ttl": ttl}

    async def set_blocked(self, db, user, chat_id: int, blocked: bool) -> dict:
        c = await self._ready(db, user)
        from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
        try:
            ent = await c.get_input_entity(int(chat_id))
            await c((BlockRequest if blocked else UnblockRequest)(id=ent))
        except Exception as e:
            raise TGError(_why(e))
        return {"blocked": bool(blocked)}

    async def set_notify(self, db, user, chat_id: int, mute: int | None = None, silent: bool | None = None) -> dict:
        """`mute`: seconds from now, -1 = until turned back on, 0 = unmute, None = leave. `silent`: notify without
        sound (True/False), None = leave."""
        import datetime
        c = await self._ready(db, user)
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
        kw = {}
        if mute is not None:
            m = int(mute)
            if m < 0:
                kw["mute_until"] = datetime.datetime(2038, 1, 1, tzinfo=datetime.timezone.utc)   # Telegram's "forever"
            else:
                kw["mute_until"] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=min(m, 365 * 86400))
        if silent is not None:
            kw["silent"] = bool(silent)
        if not kw:
            raise TGError("Nothing to change.")
        try:
            ent = await c.get_input_entity(int(chat_id))
            await c(UpdateNotifySettingsRequest(peer=InputNotifyPeer(peer=ent), settings=InputPeerNotifySettings(**kw)))
        except Exception as e:
            raise TGError(_why(e))
        return await self.peer_settings(db, user, chat_id)

    async def set_ttl(self, db, user, chat_id: int, period: int) -> dict:
        period = int(period)
        if period not in self.TTL_PERIODS:
            raise TGError("Auto-delete can be off, 1 day, 1 week or 1 month.")
        c = await self._ready(db, user)
        from telethon.tl.functions.messages import SetHistoryTTLRequest
        try:
            ent = await c.get_input_entity(int(chat_id))
            await c(SetHistoryTTLRequest(peer=ent, period=period))
        except Exception as e:
            msg = _why(e)
            if "ADMIN" in str(e).upper() or "RIGHT" in str(e).upper():
                msg = "Only an admin of this group can change its auto-delete timer."
            raise TGError(msg)
        return {"ttl": period}

    async def search(self, db, user, q: str, limit: int = 20) -> list:
        """People, groups and channels on Telegram — the ones you know first, then public ones."""
        q = str(q or "").strip().lstrip("@")
        if len(q) < 2:
            return []
        c = await self._ready(db, user)
        try:
            from telethon import utils
            from telethon.tl.functions.contacts import SearchRequest
            res = await c(SearchRequest(q=q[:64], limit=max(1, min(int(limit), 50))))
        except Exception as e:
            raise TGError(_why(e))
        ents = {}
        for e in list(getattr(res, "users", []) or []) + list(getattr(res, "chats", []) or []):
            try:
                ents[int(utils.get_peer_id(e))] = e
            except Exception:
                continue
        out, seen = [], set()
        for group, known in ((getattr(res, "my_results", []) or [], True), (getattr(res, "results", []) or [], False)):
            for peer in group:
                try:
                    pid = int(utils.get_peer_id(peer))
                except Exception:
                    continue
                e = ents.get(pid)
                if e is None or pid in seen or getattr(e, "deleted", False):
                    continue
                seen.add(pid)
                out.append({"id": pid, "title": _name(e), "kind": _kind(e), "known": known,
                            "username": getattr(e, "username", "") or "",
                            "members": int(getattr(e, "participants_count", 0) or 0)})
        return out

    async def read_out(self, db, user, chat_id: int) -> int:
        """How far the other side has read this chat's outgoing messages. 0 when Telegram could not be
        asked -- which draws a single ✓, never a ✓✓ nobody confirmed."""
        c = await self._ready(db, user)
        try:
            from telethon.tl.functions.messages import GetPeerDialogsRequest
            from telethon.tl.types import InputDialogPeer
            peer = await c.get_input_entity(int(chat_id))
            got = await c(GetPeerDialogsRequest(peers=[InputDialogPeer(peer=peer)]))
            d = (getattr(got, "dialogs", None) or [None])[0]
            return int(getattr(d, "read_outbox_max_id", 0) or 0)
        except Exception as e:
            logger.debug("[tgc] read_out %s: %s", chat_id, _why(e))
            return 0

    async def mark_read(self, db, user, chat_id: int, max_id: int = 0) -> None:
        c = await self._ready(db, user)
        await c.send_read_acknowledge(int(chat_id), max_id=int(max_id) or None)

    async def media(self, db, user, chat_id: int, msg_id: int, thumb: bool = False):
        """(bytes, mime, filename) of one message's attachment — or its thumbnail."""
        c = await self._ready(db, user)
        m = await c.get_messages(int(chat_id), ids=int(msg_id))
        if m is None or not getattr(m, "media", None):
            raise TGError("That message has no attachment.")
        info = media_of(m) or {}
        if info.get("size", 0) > MAX_UPLOAD:
            raise TGError("That attachment is too large to fetch here.")
        data = await c.download_media(m, file=bytes, thumb=(-1 if thumb else None))
        if data is None:
            raise TGError("Telegram did not return that attachment.")
        mime = "image/jpeg" if thumb else (info.get("mime") or "application/octet-stream")
        return data, mime, info.get("name") or f"telegram-{msg_id}"

    _avatars: dict = {}

    async def avatar(self, db, user, peer_id: int):
        key = (user.id, int(peer_id))
        hit = self._avatars.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        c = await self._ready(db, user)
        try:
            data = await c.download_profile_photo(int(peer_id), file=bytes, download_big=False)
        except Exception:
            data = None
        self._avatars[key] = (time.time() + 3600, data or b"")
        if len(self._avatars) > 5000:
            for k in sorted(self._avatars, key=lambda k: self._avatars[k][0])[:1000]:
                self._avatars.pop(k, None)
        return data or b""


async def _safe(coro):
    try:
        return await coro
    except Exception:
        return None


def _why(e: Exception) -> str:
    """Telethon's errors, in words somebody signing in can act on."""
    n = type(e).__name__
    if n not in _KNOWN_ERRORS:
        # The NAME only (never the request, the chat or the text): "Telegram refused that: … (caused by
        # SendReactionRequest)" reached a person with nothing in the log to say which refusal it was.
        logger.warning("[tgc] untranslated Telegram error %s", n)
    return {
        "PhoneNumberInvalidError": "Telegram does not recognise that phone number.",
        "PhoneCodeInvalidError": "That code is not right — check the latest code Telegram sent.",
        "PhoneCodeExpiredError": "That code has expired. Ask for a new one.",
        "PasswordHashInvalidError": "That is not your two-step verification password.",
        "FloodWaitError": "Telegram asks you to wait before trying again ({} s).".format(getattr(e, "seconds", "?")),
        "PhoneNumberBannedError": "Telegram has banned that phone number.",
        "ApiIdInvalidError": "The server's Telegram API id/hash are not valid — ask the admin.",
        "ReactionInvalidError": "This chat does not allow that reaction.",
        "ReactionsTooManyError": "This message already has as many different reactions as it may.",
        "ChatWriteForbiddenError": "You cannot react or write in this chat.",
        "SearchQueryEmptyError": "Type something to search for.",
    }.get(n, "Telegram refused that: " + (str(e) or n))


_KNOWN_ERRORS = {"PhoneNumberInvalidError", "PhoneCodeInvalidError", "PhoneCodeExpiredError",
                 "PasswordHashInvalidError", "FloodWaitError", "PhoneNumberBannedError", "ApiIdInvalidError",
                 "ReactionInvalidError", "ReactionsTooManyError", "ChatWriteForbiddenError",
                 "SearchQueryEmptyError"}


_manager: Manager | None = None


def manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager()
    return _manager

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


class TGError(Exception):
    """A refusal a person can act on — its text is shown as-is."""


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


def message_dict(msg, chat_id) -> dict:
    sender = getattr(msg, "sender", None)
    reply = getattr(msg, "reply_to", None)
    date = getattr(msg, "date", None)
    return {"id": int(msg.id), "chat_id": int(chat_id), "out": bool(getattr(msg, "out", False)),
            "date": int(date.timestamp()) if date else 0,
            "text": getattr(msg, "message", None) or getattr(msg, "text", None) or "",
            "sender_id": int(getattr(msg, "sender_id", 0) or 0), "sender": _name(sender),
            "reply_to": int(getattr(reply, "reply_to_msg_id", 0) or 0) if reply else 0,
            "media": media_of(msg), "edited": bool(getattr(msg, "edit_date", None))}


class Manager:
    def __init__(self, client_factory=default_client_factory, store=None, api=_api_config):
        self._factory = client_factory
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
            await a.client.connect()
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
        return {"configured": True, "state": a.state, "me": a.me, "phone": a.phone if a.state != "ready" else ""}

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
            a.me = {"id": int(getattr(me, "id", 0) or 0), "name": _name(me), "username": getattr(me, "username", "") or ""}
        except Exception:
            a.me = {}
        if not a.handlers_on:
            self._install_handlers(a)
            a.handlers_on = True
        await self._emit(a, {"type": "state", "state": "ready", "me": a.me})

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

        a.client.add_event_handler(on_new, events.NewMessage())
        a.client.add_event_handler(on_edit, events.MessageEdited())
        a.client.add_event_handler(on_delete, events.MessageDeleted())

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
            out.append({"id": int(d.id), "title": _name(d.entity) or getattr(d, "name", ""),
                        "kind": _kind(d.entity), "unread": int(getattr(d, "unread_count", 0) or 0),
                        "pinned": bool(getattr(d, "pinned", False)),
                        "muted": bool(getattr(getattr(d, "dialog", None), "notify_settings", None)
                                      and getattr(d.dialog.notify_settings, "mute_until", None)),
                        "last": {"text": (getattr(last, "message", "") or "")[:200] if last else "",
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
    return {
        "PhoneNumberInvalidError": "Telegram does not recognise that phone number.",
        "PhoneCodeInvalidError": "That code is not right — check the latest code Telegram sent.",
        "PhoneCodeExpiredError": "That code has expired. Ask for a new one.",
        "PasswordHashInvalidError": "That is not your two-step verification password.",
        "FloodWaitError": "Telegram asks you to wait before trying again ({} s).".format(getattr(e, "seconds", "?")),
        "PhoneNumberBannedError": "Telegram has banned that phone number.",
        "ApiIdInvalidError": "The server's Telegram API id/hash are not valid — ask the admin.",
    }.get(n, "Telegram refused that: " + (str(e) or n))


_manager: Manager | None = None


def manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager()
    return _manager

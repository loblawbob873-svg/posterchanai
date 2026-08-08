"""Radicale storage plugin: a working directory, mirrored into encrypted Nostr events.

WHY A SUBCLASS AND NOT A FRESH BaseStorage. CalDAV's hard parts are not "where do the bytes live" —
they are sync tokens, the item history a `sync-collection` REPORT walks, collection locking, and etag
semantics. Radicale's `multifilesystem` already gets all of that right and is exercised by every
CalDAV client in the world. Reimplementing it against a document store would be re-deriving those
rules from scratch, and the failure mode of getting them subtly wrong is a phone that silently stops
syncing or duplicates every event.

So the filesystem is a CACHE and Nostr is the RECORD: every write is mirrored out as an encrypted
event (see caldav_store), and a collection is hydrated back from those events the first time it is
touched in a process. Delete the working directory and the calendars come back; the node's disk is
not where your calendar lives.

WHAT THIS IS NOT. Not end-to-end encrypted — see the note at the top of caldav_store. A CalDAV
client sends plaintext and this server must answer it.
"""
import asyncio
import logging
import os
import threading

from radicale.storage import multifilesystem

logger = logging.getLogger(__name__)

# Users whose calendars this process has already pulled back from the relay. Per PROCESS, not per
# request: hydration is a relay round trip per collection and the working directory is durable, so
# doing it once on first touch is the whole cost.
_hydrated: set = set()
_hydrate_lock = threading.Lock()


def _run(coro):
    """Radicale's storage API is synchronous and is called from a threadpool (the app mounts it as
    WSGI). The document store is async, so each call gets its own loop — there is no running loop in
    this thread to schedule onto."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _user_of(path: str) -> str:
    """The account a collection path belongs to: /<user>/<calendar>."""
    parts = [p for p in (path or "").split("/") if p]
    return parts[0] if parts else ""


def _calendar_of(path: str) -> str:
    parts = [p for p in (path or "").split("/") if p]
    return parts[1] if len(parts) > 1 else ""


def _with_user(username: str, fn):
    """Run `fn(db, user)` against a fresh session. Callers here are threadpool workers, never the
    request's own session — that one belongs to the HTTP request and is closed by the time a storage
    call runs."""
    from app.database import SessionLocal
    from app.models import User
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return None
        return fn(db, user)
    finally:
        db.close()


class Collection(multifilesystem.Collection):
    """A calendar. Every mutation lands on disk (Radicale's rules) and in an event (the record)."""

    def upload(self, href, item):
        out = super().upload(href, item)
        try:
            self._mirror_put(href)
        except Exception as e:            # a mirror failure must never lose the client's write
            logger.warning("[caldav] mirror of %s failed: %s", href, e)
        return out

    def delete(self, href=None):
        # Read the uid BEFORE the delete, or there is nothing left to address the event by.
        uids = []
        try:
            if href is None:
                uids = [i.uid for i in self.get_all() if getattr(i, "uid", None)]
            else:
                it = self._get(href) if hasattr(self, "_get") else None
                uid = getattr(it, "uid", None) or os.path.splitext(href)[0]
                uids = [uid] if uid else []
        except Exception:
            pass
        super().delete(href)
        try:
            user, cal = _user_of(self.path), _calendar_of(self.path)
            if user and cal:
                from app.services import caldav_store
                if href is None:
                    _with_user(user, lambda db, u: _run(caldav_store.delete_calendar(db, u, cal)))
                else:
                    for uid in uids:
                        _with_user(user, lambda db, u, _uid=uid: _run(
                            caldav_store.delete_item(db, u, cal, _uid)))
        except Exception as e:
            logger.warning("[caldav] mirror of delete %s failed: %s", href, e)

    def set_meta(self, props):
        out = super().set_meta(props)
        try:
            user, cal = _user_of(self.path), _calendar_of(self.path)
            if user and cal:
                from app.services import caldav_store
                meta = dict(self.get_meta() or {})
                _with_user(user, lambda db, u: _run(caldav_store.put_calendar(db, u, cal, meta)))
        except Exception as e:
            logger.warning("[caldav] mirror of props for %s failed: %s", self.path, e)
        return out

    # -- helpers -------------------------------------------------------------------------------
    def _mirror_put(self, href):
        user, cal = _user_of(self.path), _calendar_of(self.path)
        if not (user and cal):
            return
        item = None
        for it in self.get_multi([href]):
            item = it[1] if isinstance(it, tuple) else it
            break
        if item is None:
            return
        from app.services import caldav_store
        ics = item.serialize()
        uid = getattr(item, "uid", None) or os.path.splitext(href)[0]
        comp = getattr(getattr(item, "component", None), "name", None) or "VEVENT"
        _with_user(user, lambda db, u: _run(caldav_store.put_item(db, u, cal, uid, ics, comp)))


class Storage(multifilesystem.Storage):
    _collection_class = Collection

    def discover(self, path, depth="0", child_context_manager=None, user_groups=set()):
        # First touch for this account in this process: pull the calendars back from the relay so a
        # fresh node (or a wiped working directory) serves what the user already had.
        try:
            self._hydrate(_user_of(path))
        except Exception as e:
            logger.warning("[caldav] hydrate failed for %s: %s", path, e)
        return super().discover(path, depth, child_context_manager, user_groups)

    def _hydrate(self, username: str):
        if not username:
            return
        with _hydrate_lock:
            if username in _hydrated:
                return
            _hydrated.add(username)

        from app.services import caldav_store

        def _load(db, user):
            cals = _run(caldav_store.list_calendars(db, user))
            for cal in cals:
                cid = cal.get("id")
                if not cid:
                    continue
                items = _run(caldav_store.get_items(db, user, cid))
                self._write_cache(username, cid, cal, items)
            return len(cals)

        n = _with_user(username, _load)
        if n:
            logger.info("[caldav] hydrated %s calendar(s) for %s from the relay", n, username)

    def _write_cache(self, username: str, cal_id: str, meta: dict, items: list):
        """Materialise one calendar into the working directory, without disturbing anything already
        there — the disk copy may be NEWER than what we just read (a client wrote while we were
        fetching), and this runs before every discover."""
        root = os.path.join(self._get_collection_root_folder(), username, cal_id)
        if os.path.isdir(root) and os.listdir(root):
            return
        os.makedirs(root, exist_ok=True)
        props = {"tag": "VCALENDAR"}
        for k in ("D:displayname", "displayname", "ICAL:calendar-color", "C:supported-calendar-component-set"):
            if meta.get(k):
                props[k] = meta[k]
        if meta.get("displayname") and "D:displayname" not in props:
            props["D:displayname"] = meta["displayname"]
        try:
            import json as _json
            with open(os.path.join(root, ".Radicale.props"), "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(props))
        except Exception:
            pass
        from app.services import caldav_store
        for rec in items:
            uid = rec.get("uid")
            if not uid:
                continue
            body = rec.get("ics") or ""
            if "BEGIN:VCALENDAR" not in body.upper():
                body = caldav_store.wrap_ics([body], meta.get("displayname") or cal_id)
            safe = "".join(c for c in uid if c.isalnum() or c in "-_.@") or "item"
            with open(os.path.join(root, f"{safe}.ics"), "w", encoding="utf-8") as fh:
                fh.write(body)

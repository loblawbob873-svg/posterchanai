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
import shutil
import threading
import time

from radicale.storage import multifilesystem

logger = logging.getLogger(__name__)

# Users whose calendars this process has already pulled back from the relay. Per PROCESS, not per
# request: hydration is a relay round trip per collection and the working directory is durable, so
# doing it once on first touch is the whole cost.
_hydrated: set = set()
_hydrate_lock = threading.Lock()
# How recently a file must have been touched to be spared by the reconcile. A CalDAV client's write
# lands on disk BEFORE its mirror reaches the relay, so a reconcile racing that gap would delete the
# write the client just made.
_DELETE_GRACE = 120
# Paths this process wrote FROM the relay. The grace period above exists to protect a CalDAV
# client's write that has not been mirrored yet — but a file the reconcile itself wrote is also
# "recent", so without this distinction deleting an event in the web UI left it on the phone for two
# minutes, which is exactly the ghost the reconcile was added to prevent.
_ours: dict = {}


def _safe_name(uid: str) -> str:
    return "".join(c for c in uid if c.isalnum() or c in "-_.@") or "item"


def collection_props(meta: dict, cal_id: str):
    """(Radicale props, item file extension) for one collection.

    Split out of the reconcile so it can be tested without a Radicale instance: getting either half
    wrong is silent. A vCard written into `<uid>.ics` inside a collection that announces itself as a
    VCALENDAR gives a phone an addressbook with no contacts and a calendar it cannot parse, and
    neither side logs anything.
    """
    from app.services import caldav_store
    kind = caldav_store.kind_of(meta)
    name = meta.get("displayname") or meta.get("D:displayname") or cal_id
    props = {"tag": kind, "D:displayname": name}
    color = meta.get("color") or meta.get("ICAL:calendar-color")
    if color:
        props["ICAL:calendar-color"] = color
    ext = ".vcf" if kind == caldav_store.KIND_ADDRESSBOOK else ".ics"
    return props, ext


def forget_user(username: str) -> None:
    """Drop the hydrate-once marker so the next CalDAV request re-reads this user from the relay.

    Hydration is per PROCESS (it is a relay round trip per collection), which is right for a phone
    syncing all day and wrong the moment the WEB UI writes: a calendar imported or created in the app
    would not exist on disk, so the phone would not see it until the app restarted. Every app-side
    write calls this.
    """
    with _hydrate_lock:
        _hydrated.discard(username)


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
        """Reconcile this user's working directory with the relay.

        The marker is set only AFTER the work succeeds, and the lock is held FOR the work. Marking
        first meant a relay hiccup during startup (or a phone opening several connections at once)
        left the user permanently "hydrated" with an empty directory: the phone authenticated,
        discovered ZERO calendars, and stayed that way until the service restarted — one WARNING line
        in the journal the only sign that a calendar had not been lost.
        """
        if not username:
            return
        with _hydrate_lock:
            if username in _hydrated:
                return
            from app.services import caldav_store

            def _load(db, user):
                # STRICT, both of them: this reconcile DELETES files the relay no longer has, and an
                # unreachable relay answers exactly like an empty one. Without it, a relay blip
                # during a phone sync would wipe the working copy of every calendar the user owns.
                #
                # list_COLLECTIONS, not list_calendars: addressbooks live in the same id space and
                # are served by this same plugin. Listing only calendars meant no addressbook was
                # ever written to disk — CardDAV discovery returned nothing, so the whole Contacts
                # feature was invisible over the protocol it exists for — and, worse, an addressbook
                # a phone had created was absent from `seen` below, so _drop_missing deleted its
                # directory and every card the phone had written but not yet mirrored.
                cals = _run(caldav_store.list_collections(db, user, strict=True))
                seen = set()
                for cal in cals:
                    cid = cal.get("id")
                    if not cid:
                        continue
                    seen.add(cid)
                    items = _run(caldav_store.get_items(db, user, cid, strict=True))
                    self._reconcile(username, cid, cal, items)
                self._drop_missing(username, seen)
                return len(cals)

            n = _with_user(username, _load)      # raises → marker not set → retried next request
            _hydrated.add(username)
            if n:
                logger.info("[caldav] reconciled %s calendar(s) for %s from the relay", n, username)

    def _reconcile(self, username: str, cal_id: str, meta: dict, items: list):
        """Make one calendar's directory match the relay: write what is missing, remove what is gone.

        The first version returned early whenever the directory was non-empty, so hydration could
        only ever materialise a BRAND-NEW calendar — an event added in the web UI to a calendar the
        phone already had was never written to disk and the phone never saw it, restart or no
        restart. And nothing ever deleted, so an event deleted in the web UI stayed on the phone and
        could be edited there, which mirrored it straight back into the relay: deleted data
        resurrecting itself.
        """
        import json as _json
        from app.services import caldav_store

        root = os.path.join(self._get_collection_root_folder(), username, cal_id)
        os.makedirs(root, exist_ok=True)

        # A collection is a calendar or an ADDRESSBOOK, and everything below differs: the tag
        # Radicale reports to a client, the file extension it discovers items by, and whether an
        # item is wrapped. Hardcoding the calendar answers wrote vCards into `<uid>.ics` files inside
        # a collection announcing itself as a VCALENDAR — a phone then syncs an addressbook that
        # contains no contacts and a calendar that cannot be parsed.
        props, ext = collection_props(meta, cal_id)
        book = props["tag"] == caldav_store.KIND_ADDRESSBOOK
        name = props["D:displayname"]
        try:
            with open(os.path.join(root, ".Radicale.props"), "w", encoding="utf-8") as fh:
                fh.write(_json.dumps(props))
        except Exception:
            pass

        wanted = {}
        for rec in items:
            uid = rec.get("uid")
            if not uid:
                continue
            body = rec.get("ics") or ""
            if book:
                pass                                   # a vCard is stored exactly as it is served
            elif "BEGIN:VCALENDAR" not in body.upper():
                body = caldav_store.wrap_ics([body], name)
            wanted[_safe_name(uid)] = body

        for fname, body in wanted.items():
            path = os.path.join(root, fname + ext)
            try:
                _ours[path] = time.time()     # ours, so a later pass may delete it without waiting
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as fh:
                        if fh.read() == body:
                            continue          # unchanged — don't churn the mtime a client syncs on
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
            except Exception as e:
                logger.warning("[caldav] could not write %s: %s", path, e)

        # …and remove what the relay no longer has. GRACE PERIOD: a file a CalDAV client wrote
        # SECONDS ago may not have been mirrored to the relay yet (the mirror runs after the upload
        # returns), and deleting it here would throw away the client's write.
        now = time.time()
        for fname in os.listdir(root):
            # Only this collection's own item files. Matching ".ics" unconditionally meant an
            # addressbook's .vcf files were never reconciled, so a contact deleted in the web UI
            # stayed on the phone and could be edited back into existence.
            if not fname.endswith(ext):
                continue
            stem = fname[:-len(ext)]
            if stem in wanted:
                continue
            path = os.path.join(root, fname)
            try:
                # A file WE wrote from the relay goes as soon as the relay stops listing it. Only a
                # file this process did not write gets the grace period, because that is a client's
                # upload whose mirror may still be in flight.
                if path not in _ours and now - os.path.getmtime(path) < _DELETE_GRACE:
                    continue
                os.remove(path)
                _ours.pop(path, None)
            except Exception:
                pass

    def _drop_missing(self, username: str, keep: set):
        """Remove calendar directories the relay no longer lists — a calendar deleted in the web UI
        must stop being served to a phone."""
        base = os.path.join(self._get_collection_root_folder(), username)
        if not os.path.isdir(base):
            return
        now = time.time()
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if not os.path.isdir(path) or name in keep:
                continue
            try:
                ours = any(k.startswith(path + os.sep) for k in _ours)
                if not ours and now - os.path.getmtime(path) < _DELETE_GRACE:
                    continue          # just created by a client; its mirror may still be in flight
                shutil.rmtree(path, ignore_errors=True)
                for k in [k for k in _ours if k.startswith(path + os.sep)]:
                    _ours.pop(k, None)
                logger.info("[caldav] dropped %s/%s — no longer on the relay", username, name)
            except Exception:
                pass

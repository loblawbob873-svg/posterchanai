"""Nostr → Web Push watcher.

Polls the local relay for events that p-tag a pubkey with a registered push subscription (mentions,
replies, reposts, reactions, zaps, NIP-22 comments) and delivers them as OS notifications — so the PWA
notifies you even when it's closed. Runs in the background worker process (like the other pollers).
First poll just sets the cursor (no backfill burst); dedup by event id; dead endpoints are pruned.
"""
import asyncio
import json
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services import push_service, settings_store
from app.services.nostr import relay

logger = logging.getLogger(__name__)

_sched = None
_cursor = 0                 # `since` cursor (unix secs); set to now on first poll → no backfill
_seen: set[str] = set()     # event ids already pushed (bounded)
_names: dict[str, str] = {}  # pubkey -> display name (cached)

_KINDS = [1, 6, 7, 9735, 1111]   # mention/reply, repost, reaction, zap receipt, NIP-22 comment
_POLL_SECS = 20
_SEEN_MAX = 5000


def _local_relay() -> list[str]:
    port = settings_store.get("nostr_relay_port", 3052) or 3052
    return [f"ws://127.0.0.1:{port}"]


async def _name_for(pk: str) -> str:
    if pk in _names:
        return _names[pk]
    name = ""
    try:
        evs = await relay.query(_local_relay(), [{"kinds": [0], "authors": [pk], "limit": 1}], timeout=4)
        if evs:
            meta = json.loads(evs[0].get("content") or "{}")
            name = (meta.get("display_name") or meta.get("name") or "").strip()
    except Exception:
        pass
    _names[pk] = name
    if len(_names) > 4000:
        _names.clear()
    return name


def _title(ev: dict, name: str) -> str:
    k = ev.get("kind")
    who = name or "Someone"
    if k == 9735:
        return "⚡ You were zapped"          # author of a 9735 is the zap service, not the zapper
    if k == 7:
        emoji = (ev.get("content") or "").strip()
        return f"{who} reacted {emoji or '❤️'}"
    if k == 6:
        return f"{who} reposted your note"
    if k == 1111:
        return f"{who} commented on your post"
    return f"{who} mentioned you"            # kind 1 (reply / mention)


async def _poll():
    global _cursor
    from app.database import SessionLocal
    from app.models import PushSubscription
    db = SessionLocal()
    try:
        subs = db.query(PushSubscription).all()
        if not subs:
            return
        by_pk: dict[str, list] = {}
        for s in subs:
            by_pk.setdefault(s.pubkey, []).append(s)

        now = int(time.time())
        if not _cursor:                      # first poll → set cursor, don't backfill old mentions
            _cursor = now
            return
        since = _cursor - 5                   # small overlap for clock skew
        _cursor = now

        evs = await relay.query(_local_relay(), [{"kinds": _KINDS, "#p": list(by_pk.keys()), "since": since}], timeout=8)
        for ev in evs:
            eid = ev.get("id")
            if not eid or eid in _seen:
                continue
            _seen.add(eid)
            author = ev.get("pubkey", "")
            ptags = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "p"]
            recips = [pk for pk in ptags if pk in by_pk and pk != author]   # not your own event
            if not recips:
                continue
            name = await _name_for(author)
            payload = {"title": "PosterChan", "body": _title(ev, name), "eid": eid, "author": author}
            dead = []
            for pk in recips:
                for s in by_pk[pk]:
                    ok = await asyncio.to_thread(
                        push_service.send,
                        {"endpoint": s.endpoint, "keys": {"p256dh": s.p256dh, "auth": s.auth}}, payload)
                    if not ok:
                        dead.append(s)
            for s in dead:
                try:
                    db.delete(s)
                except Exception:
                    pass
            if dead:
                db.commit()

        if len(_seen) > _SEEN_MAX:
            _seen.clear()
    except Exception as e:
        logger.warning(f"[nostr-push] poll error: {e}")
    finally:
        db.close()


def start_nostr_push_scheduler():
    global _sched
    if _sched:
        return
    _sched = AsyncIOScheduler()
    _sched.add_job(_poll, "interval", seconds=_POLL_SECS, max_instances=1, coalesce=True)
    _sched.start()
    logger.info("[nostr-push] scheduler started (every %ss)", _POLL_SECS)


def stop_nostr_push_scheduler():
    global _sched
    if _sched:
        try:
            _sched.shutdown(wait=False)
        except Exception:
            pass
        _sched = None

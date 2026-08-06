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

_KINDS = [1, 6, 7, 9735, 1111, 42]   # mention/reply, repost, reaction, zap receipt, NIP-22 comment, NIP-28 chat
_POLL_SECS = 20
_SEEN_MAX = 5000

# ---- Joined-channel push (NIP-28 chatter that does NOT tag you) -------------------------------------
# The poll above only fires when an event p-tags you, so an ordinary message in a channel you're in
# reaches nobody. Which channels a user wants is their kind-10005 ("public chats") list, so this reads
# that per subscriber and watches those channels directly. Slower cadence + a per-(user, channel)
# cooldown, because unlike a mention this fires on traffic the user didn't ask for individually — one
# chatty room must not become a push every 20 seconds.
_CHAN_POLL_SECS = 60
_CHAN_COOLDOWN = 300.0
_chan_cursor = 0
_chan_seen: set[str] = set()
_joined: dict[str, set[str]] = {}          # subscriber pubkey -> channel ids from their kind-10005
_joined_at = 0.0                           # monotonic stamp of the last _joined refresh
_JOINED_TTL = 300.0
_chan_names: dict[str, str] = {}           # channel id -> display name
_chan_last: dict[tuple, float] = {}        # (pubkey, channel id) -> last push (monotonic)


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
    if k == 42:
        return f"{who} mentioned you in a chat"
    return f"{who} mentioned you"            # kind 1 (reply / mention)


def _root_channel(ev: dict) -> str:
    """The kind-40 a chat message belongs to. Prefer the "root"-marked `e` tag — a REPLY inside a channel
    carries a second `e` (the message replied to), so taking the first blindly misfiles it."""
    es = [t for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e" and t[1]]
    for t in es:
        if len(t) >= 4 and t[3] == "root":
            return t[1]
    return es[0][1] if es else ""


async def _channel_name(cid: str) -> str:
    if cid in _chan_names:
        return _chan_names[cid]
    name = ""
    try:
        # The kind-40 holds the original metadata; a kind-41 BY ITS AUTHOR supersedes it (renames), so
        # read both and prefer the newest edit from the creator — the same rule the client applies.
        evs = await relay.query(_local_relay(), [{"ids": [cid], "kinds": [40], "limit": 1}], timeout=4)
        if evs:
            owner = evs[0].get("pubkey", "")
            name = (json.loads(evs[0].get("content") or "{}").get("name") or "").strip()
            ups = await relay.query(_local_relay(),
                                    [{"kinds": [41], "#e": [cid], "authors": [owner], "limit": 5}], timeout=4)
            if ups:
                newest = max(ups, key=lambda x: x.get("created_at", 0))
                name = (json.loads(newest.get("content") or "{}").get("name") or "").strip() or name
    except Exception:
        pass
    # Only cache a real answer. A channel whose kind-40 hasn't synced to this node yet would otherwise be
    # pinned as nameless forever, and every push for it would read "💬 Chat".
    if name:
        _chan_names[cid] = name
        if len(_chan_names) > 2000:
            _chan_names.clear()
    return name


async def _refresh_joined(pubkeys: list[str]) -> None:
    """Rebuild `pubkey -> joined channel ids` from everyone's kind-10005. Replaces the map wholesale ONLY
    on a successful query; a relay hiccup leaves the previous map in place rather than silently
    unsubscribing every user from every channel until the next refresh."""
    global _joined, _joined_at
    try:
        evs = await relay.query(_local_relay(), [{"kinds": [10005], "authors": pubkeys}], timeout=8)
    except Exception as e:
        logger.debug("[nostr-push] joined-channel refresh failed: %s", e)
        return
    newest: dict[str, dict] = {}
    for ev in evs:
        pk = ev.get("pubkey", "")
        if not pk:
            continue
        if pk not in newest or ev.get("created_at", 0) > newest[pk].get("created_at", 0):
            newest[pk] = ev
    _joined = {pk: {t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e" and t[1]}
               for pk, ev in newest.items()}
    _joined_at = time.monotonic()


async def _poll_channels():
    global _chan_cursor
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

        if not _joined or (time.monotonic() - _joined_at) > _JOINED_TTL:
            await _refresh_joined(list(by_pk.keys()))
        watched: set[str] = set()
        for pk, chans in _joined.items():
            if pk in by_pk:
                watched |= chans
        if not watched:
            return

        now = int(time.time())
        if not _chan_cursor:                 # first poll → set the cursor, never backfill a room's history
            _chan_cursor = now
            return
        since = _chan_cursor - 5
        _chan_cursor = now

        evs = await relay.query(_local_relay(),
                                [{"kinds": [42], "#e": sorted(watched), "since": since}], timeout=8)
        mono = time.monotonic()
        dead = []
        for ev in evs:
            eid = ev.get("id")
            if not eid or eid in _chan_seen:
                continue
            _chan_seen.add(eid)
            author = ev.get("pubkey", "")
            cid = _root_channel(ev)
            if not cid:
                continue
            ptags = {t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "p"}
            recips = []
            for pk in by_pk:
                if pk == author or cid not in _joined.get(pk, ()):
                    continue
                if pk in ptags:
                    continue                 # p-tagged → _poll() already pushed it; don't push twice
                if mono - _chan_last.get((pk, cid), 0.0) < _CHAN_COOLDOWN:
                    continue                 # this room already notified them recently
                recips.append(pk)
            if not recips:
                continue
            name = await _name_for(author)
            room = await _channel_name(cid)
            body = (ev.get("content") or "").strip().replace("\n", " ")[:80] or "New message"
            payload = {"title": f"💬 {room or 'Chat'}", "body": f"{name or 'Someone'}: {body}",
                       "eid": eid, "author": author, "chan": cid}
            for pk in recips:
                _chan_last[(pk, cid)] = mono
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

        if len(_chan_seen) > _SEEN_MAX:
            _chan_seen.clear()
        if len(_chan_last) > _SEEN_MAX:
            _chan_last.clear()
    except Exception as e:
        logger.warning(f"[nostr-push] channel poll error: {e}")
    finally:
        db.close()


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


# ---- Ring-a-closed-app: push incoming voice/video call (kind-25050) invites to a closed PWA. -------------
# Call signaling is EPHEMERAL (not stored), so we can't poll for it — keep a LIVE subscription to the local
# relay and push the p-tagged callee. A per-CALLEE cooldown (see _call_recent below — per-pair was the
# first cut and is forgeable) collapses the offer/ice/bye burst into
# one ring, and the SW suppresses the notification when the app is focused (it rings itself) — so only a
# genuinely backgrounded/closed client gets the OS notification. (Web Push → PWA/web; the native APK's
# WebView can't Web Push, so closed-app ringing there still needs a native piece.)
_CALL_KIND = 25050
_CALL_COOLDOWN = 30.0
_call_stop = None
_call_task = None
# Rate limit is keyed on the CALLEE (not the caller/callee pair): the relay accepts a kind-25050 whenever
# the p-tagged RECIPIENT is a WoT member, so an attacker could otherwise flood a victim by re-signing each
# event with a throwaway key (defeating a per-pair cooldown). Per-callee → at most one ring per _CALL_COOLDOWN
# regardless of who "sent" it. Legit offer/ice/bye of a real call also collapse to one ring.
_call_recent: dict = {}   # callee pubkey -> last-considered monotonic time


def _rings(ev: dict) -> bool:
    """Should this signaling frame wake a phone?

    Only the invite does. The body is NIP-44 to the callee, so we cannot read it — the client marks the
    ring-worthy frame with a cleartext `t=invite` instead (see _callTags in app.js). Everything else in
    a call — the ICE burst, answers, and the `bye` — must NOT push.

    That `bye` is the bug this exists for: a caller gives up at 45s and sends one, which lands OUTSIDE
    the 30s cooldown, so every unanswered call rang the callee a second time long after the caller had
    gone. Filtering here also drops the dozens of per-call frames before the rate-limit dict, the stats
    bump and the subscription query — the DB work now happens once per call instead of once per frame.

    A frame with NO `t` tag rings, because that is what a client older than this change sends; drop the
    fallback once they have rolled over, or an old caller silently stops ringing anyone.

    This works only because new clients tag EVERY frame (`invite` or `sig`). Tagging just the invites
    would leave a new client's ICE frame indistinguishable from an old client's invite — both untagged —
    so the fallback would have to ring for both and the second ring would survive. Absence has to mean
    exactly one thing.
    """
    ts = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "t"]
    return (not ts) or ("invite" in ts)


async def _call_handler(ev: dict):
    if not _rings(ev):
        return                       # ice/answer/bye — cheapest possible exit, before any state or I/O
    author = ev.get("pubkey", "")
    ptags = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "p"]
    recips = [pk for pk in ptags if pk and pk != author]
    if not recips:
        return
    now = time.monotonic()
    if len(_call_recent) > 2000:   # prune (belt-and-suspenders; the per-callee key bounds cardinality anyway)
        for k in [k for k, v in _call_recent.items() if now - v > 300]:
            _call_recent.pop(k, None)
    # Per-callee rate limit FIRST — in-memory, BEFORE any DB work — so a key-rotation flood is dropped here
    # (no DB query, no event-loop blocking) and every callee (subscribed or not) is stamped immediately.
    fresh = [pk for pk in recips if now - _call_recent.get(pk, 0.0) >= _CALL_COOLDOWN]
    if not fresh:
        return
    for pk in fresh:
        _call_recent[pk] = now
    # Count the call for the public stats page. The per-callee cooldown above has already collapsed
    # this call's offer/ice/bye burst into one event, so this counts calls, not signaling frames.
    # In-memory and exception-proof: nothing in the stats path may delay or break a ring.
    try:
        from app.services import stats_service
        stats_service.bump_call(len(fresh))
    except Exception:
        pass

    def _lookup_subs():
        from app.database import SessionLocal
        from app.models import PushSubscription
        db = SessionLocal()
        try:
            out = {}
            for pk in fresh:
                rows = db.query(PushSubscription).filter(PushSubscription.pubkey == pk).all()
                if rows:
                    out[pk] = [{"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}} for r in rows]
            return out
        finally:
            db.close()

    try:
        targets = await asyncio.to_thread(_lookup_subs)   # off the event loop
        if not targets:
            return
        name = await _name_for(author)
        payload = {"title": "📞 Incoming call", "body": f"{name} is calling…", "type": "call", "author": author}
        for subs in targets.values():
            for sub in subs:
                await asyncio.to_thread(push_service.send, sub, payload)
    except Exception as e:
        logger.warning(f"[nostr-push] call handler error: {e}")


async def _flush_call_stats():
    try:
        from app.services import stats_service
        await stats_service.flush_calls()
    except Exception as e:
        logger.debug("[nostr-push] call stat flush skipped: %s", e)


async def _call_sub_loop():
    try:
        await relay.subscribe(_local_relay()[0], [{"kinds": [_CALL_KIND]}], _call_handler,
                              _call_stop, since_now=True)
    except Exception as e:
        logger.warning(f"[nostr-push] call subscription ended: {e}")


def start_nostr_push_scheduler():
    global _sched, _call_stop, _call_task
    if _sched:
        return
    _sched = AsyncIOScheduler()
    _sched.add_job(_poll, "interval", seconds=_POLL_SECS, max_instances=1, coalesce=True)
    _sched.add_job(_poll_channels, "interval", seconds=_CHAN_POLL_SECS, max_instances=1, coalesce=True)
    # Persist the day's call tally to the relay every few minutes, so a restart doesn't lose it.
    _sched.add_job(_flush_call_stats, "interval", seconds=300, max_instances=1, coalesce=True)
    _sched.start()
    try:
        if _local_relay():
            _call_stop = asyncio.Event()
            _call_task = asyncio.create_task(_call_sub_loop())
    except Exception as e:
        logger.warning(f"[nostr-push] could not start call subscription: {e}")
    logger.info("[nostr-push] scheduler started (mentions every %ss, joined channels every %ss) + call-invite push subscription",
                _POLL_SECS, _CHAN_POLL_SECS)


def stop_nostr_push_scheduler():
    global _sched, _call_stop, _call_task
    if _call_stop:
        try:
            _call_stop.set()
        except Exception:
            pass
    if _call_task:
        try:
            _call_task.cancel()
        except Exception:
            pass
    _call_task = None
    if _sched:
        try:
            _sched.shutdown(wait=False)
        except Exception:
            pass
        _sched = None

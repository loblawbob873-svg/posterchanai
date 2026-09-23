"""Members' own Nostr events → the fediverse. Runs in the WORKER process (app/worker.py).

Nothing is copied: every tick reads what members published to this node's relay since the saved
cursor and translates it on the way out (convert.py). A post's ActivityPub object is served from
the relay by the same id (`/ap/objects/<event id>`), so there is exactly one copy of everything.

    kind 1 / 1111  → Create(Note)   top-level posts, and replies whose parent is on the fediverse
                                    or is a member's post (a reply to a Nostr-only thread would
                                    arrive on the fediverse with no context, so it stays on Nostr)
    kind 6         → Announce       of a fediverse note or a member's post
    kind 7         → Like           ditto; sent to the author, who is the one it concerns
    kind 5         → Delete / Undo  for what was sent
    kind 0         → Update(Person)
    kind 3         → Follow / Undo(Follow) for fediverse accounts (puppets) added to or removed from
                                    the contact list -- which is what brings their posts in

COMPATIBILITY WITH THE PLEROMA BRIDGE: a member whose activity already reaches the fediverse through
their own linked Pleroma account is skipped entirely here (actors.uses_linked_account), and anything
carrying a `proxy` tag is a mirror, never a member's own words, so it is never sent back out.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from app.services import nostr_store, settings_store
from app.services.activitypub import actors, config, convert, remote, state

logger = logging.getLogger(__name__)

KINDS = [0, 1, 3, 5, 6, 7, 1111]
_TICK_SECONDS = 30
_PAGE = 500
_RETRY_DELAYS = (60, 300, 1800)
_seen: dict = {}                 # event id → time processed (so the inclusive cursor second is not replayed)
_retries: list = []              # [(due, inbox, activity, member, attempt)]
_stats = {"delivered": 0, "failed": 0, "last_error": "", "last_tick": 0, "queued": 0}
_scheduler = None
_SENT = "pcai:ap:sent:"          # per-Like record of who it was sent to, so an Undo reaches them


def stats() -> dict:
    return dict(_stats, queued=len(_retries))


# ------------------------------------------------------------------------------------ resolution

def _puppet_row(pubkey: str):
    from app.database import SessionLocal
    from app.models import FediPuppet
    db = SessionLocal()
    try:
        return db.query(FediPuppet).filter(FediPuppet.pubkey_hex == pubkey).first() if pubkey else None
    finally:
        db.close()


def _mirror_row(event_id: str):
    from app.database import SessionLocal
    from app.models import FediBridgeDelivered
    db = SessionLocal()
    try:
        return db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == event_id).first()
    finally:
        db.close()


async def _event(event_id: str) -> dict | None:
    from app.services.fedi_bridge_identity import query_one
    ok, ev = await query_one(settings_store._port(), {"ids": [event_id], "limit": 1})
    return ev if ok else None


async def resolve_pubkey(pubkey: str) -> dict:
    """{"href", "name", "inbox"?} for a pubkey that has a fediverse address, else {}."""
    base = config.base_url()
    name = actors.name_of(pubkey)
    if name:
        return {"href": convert.actor_url(base, name), "name": f"@{name}@{config.domain()}"}
    row = _puppet_row(pubkey)
    if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)):
        return {"href": row.actor_uri, "name": f"@{row.acct}" if row.acct else row.actor_uri, "remote": True}
    return {}


async def resolve_event(event_id: str) -> dict:
    """{"uri", "actor", "remote"} for an event that exists on the fediverse, else {}: a mirrored note
    (its original URI and author) or a member's own post (our object URL)."""
    base = config.base_url()
    row = _mirror_row(event_id)
    if row and row.note_uri:
        prow = _puppet_row(row.nostr_pubkey or "")
        return {"uri": row.note_uri, "actor": prow.actor_uri if prow else "", "remote": True}
    ev = await _event(event_id)
    if ev and actors.name_of(ev.get("pubkey", "")) and not _is_mirror(ev):
        return {"uri": convert.object_url(base, event_id),
                "actor": convert.actor_url(base, actors.name_of(ev["pubkey"])), "remote": False}
    return {}


def _is_mirror(ev: dict) -> bool:
    return any(t and t[0] in ("proxy", "fedibridge") for t in ev.get("tags") or [])


def parent_of(ev: dict) -> str:
    """The event this one replies to: NIP-22's lowercase `e` for a comment, NIP-10's `reply` marker
    (else the last `e`) for a kind-1."""
    es = [t for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e" and t[1]]
    if not es:
        return ""
    if ev.get("kind") == 1111:
        return es[-1][1]
    for marker in ("reply", "root"):
        hit = next((t for t in es if len(t) > 3 and t[3] == marker), None)
        if hit:
            return hit[1]
    return es[-1][1]


def _referenced_pubkeys(ev: dict) -> list:
    from app.services.nostr import nostr_service
    out = [t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] in ("p", "P") and t[1]]
    for m in convert._NOSTR_REF_RE.finditer(ev.get("content") or ""):
        try:
            pk = nostr_service.to_pubkey_hex(m.group(1))
        except Exception:
            pk = None
        if pk:
            out.append(pk)
    seen, uniq = set(), []
    for pk in out:
        if pk not in seen:
            seen.add(pk)
            uniq.append(pk)
    return uniq[:30]


async def _inbox_for(actor_uri: str) -> str:
    try:
        return remote.inbox_of(await remote.actor(actor_uri))
    except remote.FetchError:
        return ""


# ------------------------------------------------------------------------------------ translation

async def plan(ev: dict, member: str) -> list:
    """[(inbox, activity)] to send for one event. Empty when there is nothing to federate."""
    base, name = config.base_url(), actors.name_of(member)
    if not base or not name or _is_mirror(ev):
        return []
    me = convert.actor_url(base, name)
    followers_url = f"{me}/followers"
    kind = ev.get("kind")
    fol = [f["inbox"] for f in await state.followers(member)]
    out: list = []

    if kind in (1, 1111):
        parent = parent_of(ev)
        reply_uri = reply_actor = ""
        if parent:
            target = await resolve_event(parent)
            if not target:
                return []                     # a reply in a Nostr-only thread stays on Nostr
            reply_uri, reply_actor = target["uri"], target.get("actor", "")
        mentions = {}
        for pk in _referenced_pubkeys(ev):
            who = await resolve_pubkey(pk)
            if who:
                mentions[pk] = who
        note = convert.note_from_event(ev, base=base, actor=me, followers=followers_url, mentions=mentions,
                                       in_reply_to=reply_uri, reply_to_actor=reply_actor)
        act = convert.create(note, me)
        targets = set(fol)
        for who in list(mentions.values()) + ([{"href": reply_actor, "remote": target["remote"]}] if reply_actor else []):
            if who.get("remote") and who.get("href"):
                inbox = await _inbox_for(who["href"])
                if inbox:
                    targets.add(inbox)
        out = [(i, act) for i in sorted(targets)]

    elif kind in (6, 7):
        target_id = next((t[1] for t in reversed(ev.get("tags") or []) if len(t) > 1 and t[0] == "e"), "")
        target = await resolve_event(target_id) if target_id else {}
        if not target:
            return []
        author_inbox = await _inbox_for(target["actor"]) if target.get("remote") and target.get("actor") else ""
        if kind == 6:
            act = convert.announce(ev, base=base, actor=me, followers=followers_url, target=target["uri"],
                                   target_actor=target.get("actor", ""))
            out = [(i, act) for i in sorted(set(fol) | ({author_inbox} if author_inbox else set()))]
        else:
            act = convert.like(ev, base=base, actor=me, target=target["uri"])
            if author_inbox:
                out = [(author_inbox, act)]
                await _remember_sent(ev["id"], author_inbox)

    elif kind == 5:
        kinds = {t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "k"}
        for t in ev.get("tags") or []:
            if len(t) < 2 or t[0] != "e":
                continue
            gone = t[1]
            if "7" in kinds and "1" not in kinds:
                inbox = await _sent_to(gone)
                act = convert.undo(convert.activity_url(base, gone), "Like", base=base, actor=me, target="",
                                   deletion_id=ev["id"])
                out += [(inbox, act)] if inbox else []
            elif "6" in kinds and "1" not in kinds:
                act = convert.undo(convert.activity_url(base, gone), "Announce", base=base, actor=me, target="",
                                   deletion_id=ev["id"])
                out += [(i, act) for i in sorted(set(fol))]
            else:
                act = convert.delete_note(gone, base=base, actor=me, followers=followers_url, deletion_id=ev["id"])
                out += [(i, act) for i in sorted(set(fol))]

    elif kind == 0:
        doc = await actors.person(name, member)
        act = {"@context": convert.AS_CONTEXT, "id": convert.activity_url(base, ev["id"], "update"),
               "type": "Update", "actor": me, "to": [config.PUBLIC], "cc": [followers_url], "object": doc}
        out = [(i, act) for i in sorted(set(fol))]

    elif kind == 3:
        out = await _follows(ev, member, me)
    return out


async def _follows(ev: dict, member: str, me: str) -> list:
    """Follow the fediverse accounts newly in the contact list; unfollow the ones taken out."""
    wanted = {}
    for t in ev.get("tags") or []:
        if len(t) > 1 and t[0] == "p":
            row = _puppet_row(t[1])
            if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)):
                wanted[row.actor_uri] = True
    current = await state.following(member)
    out = []
    for actor_uri in sorted(set(wanted) - set(current)):
        inbox = await _inbox_for(actor_uri)
        if not inbox:
            continue
        follow = {"@context": convert.AS_CONTEXT, "id": f"{me}#follows/{_h(actor_uri)}", "type": "Follow",
                  "actor": me, "object": actor_uri}
        await state.set_following(member, actor_uri, inbox, "pending")
        out.append((inbox, follow))
    for actor_uri in sorted(set(current) - set(wanted)):
        inbox = current[actor_uri].get("inbox") or await _inbox_for(actor_uri)
        await state.drop_following(member, actor_uri)
        if inbox:
            out.append((inbox, {"@context": convert.AS_CONTEXT, "id": f"{me}#follows/{_h(actor_uri)}/undo",
                                "type": "Undo", "actor": me,
                                "object": {"id": f"{me}#follows/{_h(actor_uri)}", "type": "Follow",
                                           "actor": me, "object": actor_uri}}))
    if out:
        state.forget_followed_cache()
    return out


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


async def _remember_sent(event_id: str, inbox: str) -> None:
    try:
        await nostr_store.put_doc(settings_store._port(), settings_store._operator_seckey(None),
                                  _SENT + event_id, {"inbox": inbox})
    except Exception:
        pass


async def _sent_to(event_id: str) -> str:
    try:
        doc = await nostr_store.get_doc(settings_store._port(), _SENT + event_id,
                                        seckey=settings_store._operator_seckey(None))
        return (doc or {}).get("inbox", "")
    except Exception:
        return ""


# ------------------------------------------------------------------------------------ delivery

MAX_RETRIES_QUEUED = 5000


async def _send(inbox: str, activity: dict, member: str, attempt: int = 0) -> None:
    keys = await state.keypair(member)
    key_id, priv = actors.signing(member, keys)
    status = await remote.deliver(inbox, activity, key_id=key_id, private_pem=priv)
    if 200 <= status < 300:
        _stats["delivered"] += 1
        return
    _stats["failed"] += 1
    _stats["last_error"] = f"HTTP {status} from {remote.host_of(inbox)}"
    # 4xx other than 408/429 is an answer, not an outage: retrying it only makes us the storm.
    if (status == 0 or status >= 500 or status in (408, 429)) and attempt < len(_RETRY_DELAYS):
        if len(_retries) >= MAX_RETRIES_QUEUED:
            _retries.pop(0)                      # the oldest is the least likely to still matter
        _retries.append((time.time() + _RETRY_DELAYS[attempt], inbox, activity, member, attempt + 1))
        return
    # Given up. A Follow that never arrived must not stay recorded as asked-for, or the member's
    # next contact-list change would think it already sent and never try again.
    if activity.get("type") == "Follow":
        try:
            await state.drop_following(member, convert.id_of(activity.get("object")))
        except Exception:
            pass


async def _flush_retries(limit: int = 200) -> None:
    """Send what is due -- up to `limit` of them, concurrently; the rest stay queued for the next
    tick rather than being dropped."""
    now = time.time()
    due = [r for r in _retries if r[0] <= now][:limit]
    for r in due:
        _retries.remove(r)
    sem = asyncio.Semaphore(8)

    async def one(r):
        async with sem:
            await _send(r[1], r[2], r[3], r[4])
    await asyncio.gather(*(one(r) for r in due))


async def _members() -> list:
    """Every local user on the fediverse, minus those whose activity already goes out through
    their own linked Pleroma account (see actors.uses_linked_account)."""
    return [pk for pk in actors.all_actors() if not actors.uses_linked_account(pk)]


async def tick() -> int:
    """One pass: read members' events since the cursor, translate, deliver. Returns events handled."""
    if not config.enabled() or not config.base_url():
        return 0
    _stats["last_tick"] = int(time.time())
    await _flush_retries()
    try:
        since = await state.cursor()
    except Exception as e:
        _stats["last_error"] = f"cursor unreadable: {type(e).__name__}"
        return 0                                   # never deliver from a guessed position
    now = int(time.time())
    if not since:
        await state.set_cursor(now)                # first run: no backfill of everything ever posted
        return 0
    members = await _members()
    if not members:
        await state.set_cursor(now)
        return 0
    evs = await _since(members, since)
    evs = sorted((e for e in evs if e.get("id") not in _seen), key=lambda e: (e.get("created_at", 0), e["id"]))
    handled, newest = 0, since
    sem = asyncio.Semaphore(8)

    async def one(inbox, act, member):
        async with sem:
            await _send(inbox, act, member)

    for ev in evs:
        try:
            jobs = await plan(ev, ev["pubkey"])
        except Exception as e:
            # STOP HERE rather than step over it: the usual cause is a relay read that failed (the
            # follower list is read strictly), and moving the cursor past the event would lose it
            # for good. One event that fails every time is skipped after three tries, and said so.
            n = _failures[ev["id"]] = _failures.get(ev["id"], 0) + 1
            logger.info("[activitypub] could not translate %s (try %d): %s: %s",
                        ev.get("id", "")[:12], n, type(e).__name__, e)
            if n < 3:
                break
            _stats["last_error"] = f"skipped {ev['id'][:12]} after 3 failures: {type(e).__name__}"
            jobs = []
        if jobs:
            await asyncio.gather(*(one(i, a, ev["pubkey"]) for i, a in jobs))
        _failures.pop(ev["id"], None)
        _seen[ev["id"]] = time.time()
        newest = max(newest, int(ev.get("created_at", 0)))
        handled += 1
    if len(_seen) > 20000:
        for k in sorted(_seen, key=_seen.get)[:10000]:
            _seen.pop(k, None)
    # Inclusive of the newest second handled (more may share it; `_seen` stops a replay).
    if newest != since:
        await state.set_cursor(newest)
    return handled


_failures: dict = {}
_MAX_PAGES = 20


async def _since(members: list, since: int) -> list:
    """Everything members published since `since`. The relay answers NEWEST FIRST with a limit, so
    one query after a long quiet spell (worker down, a busy bot) would return only the newest page,
    and a cursor moved to it would skip everything older. So it pages backwards with `until` until a
    page comes back short. Bounded: past _MAX_PAGES the backlog is too old to be worth sending, and
    the oldest part is dropped with a note rather than holding delivery up for ever."""
    out, seen_ids, until = [], set(), None
    for _ in range(_MAX_PAGES):
        flt = {"authors": members, "kinds": KINDS, "since": since, "limit": _PAGE}
        if until is not None:
            flt["until"] = until
        page = await nostr_store._ws_query(settings_store._port(), [flt], strict=True)
        fresh = [e for e in page if e.get("id") not in seen_ids]
        for e in fresh:
            seen_ids.add(e["id"])
        out.extend(fresh)
        if len(page) < _PAGE or not fresh:
            return out
        until = min(int(e.get("created_at", 0)) for e in page)
    _stats["last_error"] = f"backlog over {_MAX_PAGES * _PAGE} events; the oldest were not sent"
    return out


def start_activitypub_delivery() -> None:
    """Start the delivery loop (idempotent). The TICK reads `activitypub_enabled`, so turning the
    feature on or off in Admin needs no restart."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    async def _job():
        try:
            await asyncio.wait_for(tick(), timeout=_TICK_SECONDS * 8)
        except Exception as e:
            _stats["last_error"] = f"{type(e).__name__}: {e}"[:200]
            logger.info("[activitypub] delivery tick failed: %s: %s", type(e).__name__, e)
        # The admin panel reads this (the app process cannot see the worker's memory). Only while
        # the feature is on, and only when something moved -- not a document every 30 seconds.
        snap = stats()
        snap.pop("last_tick", None)
        if config.enabled() and snap != _job.last:
            try:
                await nostr_store.put_doc(settings_store._port(), settings_store._operator_seckey(None),
                                          "pcai:ap:stats", dict(snap, at=int(time.time())))
                _job.last = snap
            except Exception:
                pass
    _job.last = None

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=_TICK_SECONDS, id="activitypub_delivery",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[activitypub] delivery loop started (every %ss; off until activitypub_enabled)", _TICK_SECONDS)


def stop_activitypub_delivery() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


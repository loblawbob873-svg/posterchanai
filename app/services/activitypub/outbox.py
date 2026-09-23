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
    name = actors.handle(pubkey)
    if name and (actors.is_actor(pubkey) or await actors.exposed(pubkey)):
        return {"href": convert.actor_url(base, name), "name": f"@{name}@{config.domain()}"}
    row = _puppet_row(pubkey)
    if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)):
        canonical, _inbox = await _canonical(row.actor_uri)
        href = canonical or row.actor_uri
        return {"href": href, "name": f"@{row.acct}" if row.acct else href, "remote": True}
    return {}


async def resolve_event(event_id: str) -> dict:
    """{"uri", "actor", "remote"} for an event that exists on the fediverse, else {}: a mirrored note
    (its original URI and author) or a member's own post (our object URL)."""
    base = config.base_url()
    row = _mirror_row(event_id)
    if row and row.note_uri:
        prow = _puppet_row(row.nostr_pubkey or "")
        actor_id = (await _canonical(prow.actor_uri))[0] or prow.actor_uri if prow else ""
        return {"uri": row.note_uri, "actor": actor_id, "remote": True}
    ev = await _event(event_id)
    if ev and not _is_mirror(ev) and await actors.exposed(ev.get("pubkey", "")):
        return {"uri": convert.object_url(base, event_id),
                "actor": convert.actor_url(base, actors.handle(ev["pubkey"])), "remote": False}
    return {}


def _blocked_cached(actor_id: str) -> bool:
    """A follower blocked AFTER it followed (instance or single account) gets nothing more. Uses the
    actor cache only -- delivery must not fetch every follower on every post."""
    if config.host_blocked(remote.host_of(actor_id)):
        return True
    hit = remote._actors.get((actor_id or "").split("#")[0])
    if not hit:
        return False
    from app.services.activitypub.inbox import acct_of_actor
    return config.account_blocked(acct_of_actor(hit[1]))


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


async def _canonical(actor_uri: str) -> tuple[str, str]:
    """(canonical actor id, inbox) for an address from our own records -- see remote.actor(alias)."""
    try:
        doc = await remote.actor(actor_uri, alias=True)
    except remote.FetchError:
        return "", ""
    return str(doc.get("id") or "").split("#")[0], remote.inbox_of(doc)


async def _inbox_for(actor_uri: str) -> str:
    return (await _canonical(actor_uri))[1]


# ------------------------------------------------------------------------------------ translation

async def plan(ev: dict, member: str) -> list:
    """[(inbox, activity)] to send for one event. Empty when there is nothing to federate."""
    base, name = config.base_url(), actors.handle(member)
    if not base or not name or _is_mirror(ev) or _protected(ev):
        return []
    if not actors.is_actor(member) and ev.get("kind") == 3:
        return []          # a non-local user's follows would bring posts only to THIS relay, which they do not read
    # A member on a linked Pleroma account already posts, replies, likes and boosts THERE, so none of
    # that is sent from here (one of each on the fediverse, never two). FOLLOWS are the exception:
    # nobody sees a follow twice, and it is what brings the followed accounts' posts in over ActivityPub.
    if ev.get("kind") != 3 and actors.uses_linked_account(member):
        return []
    me = convert.actor_url(base, name)
    followers_url = f"{me}/followers"
    kind = ev.get("kind")
    fol = [f["inbox"] for f in await state.followers(member) if not _blocked_cached(f.get("actor", ""))]
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
    # By CANONICAL actor id: a puppet made by the Pleroma bridge is keyed on a profile URL, and the
    # Follow, the Accept that answers it and the posts that follow are all by the canonical id.
    wanted = {}
    for t in ev.get("tags") or []:
        if len(t) > 1 and t[0] == "p":
            row = _puppet_row(t[1])
            if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)):
                wanted[row.actor_uri] = True
    current = await state.following(member)
    resolved = {}
    for alias in sorted(wanted):
        canonical, inbox = await _canonical(alias)
        if canonical and inbox:
            resolved[canonical] = inbox
    unresolved_known = {a for a in wanted if a in current}      # keep what we cannot re-check right now
    out = []
    for actor_uri in sorted(set(resolved) - set(current)):
        inbox = resolved[actor_uri]
        follow = {"@context": convert.AS_CONTEXT, "id": f"{me}#follows/{_h(actor_uri)}", "type": "Follow",
                  "actor": me, "object": actor_uri}
        await state.set_following(member, actor_uri, inbox, "pending")
        out.append((inbox, follow))
    for actor_uri in sorted(set(current) - set(resolved) - unresolved_known):
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
    keys = await state.keypair(member)               # driven by an event on this relay: never limited
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
    """Every local user on the fediverse. (Those on a linked Pleroma account send only follows from
    here -- see `plan`.)"""
    return actors.all_actors()


async def tick() -> int:
    """One round: local users' events, then (in `everyone` mode) what other Nostr users on this relay
    addressed to the fediverse. Each pass has its own cursor. Returns events handled."""
    if not config.enabled() or not config.base_url():
        return 0
    if not settings_store.is_hydrated():
        return 0          # "is the Pleroma bridge on?" and friends cannot be answered yet -- decide nothing
    _stats["last_tick"] = int(time.time())
    await _flush_retries()
    handled = await _pass(state.CURSOR, await _members(), KINDS, None)
    if config.everyone():
        handled += await _pass(state.CURSOR_EVERYONE, None, _EVERYONE_KINDS, await _everyone_filter())
    return handled


_EVERYONE_KINDS = [0, 1, 1111, 5, 6, 7]


async def _everyone_filter():
    """What a NON-local Nostr user publishes that concerns the fediverse: anything by somebody who
    has followers there, and otherwise only what is addressed to it -- a mention of a fediverse
    account, a reply/like/boost of one of its notes, or the deletion of something that went out.
    Never a local user's (the first pass has those), never a puppet's (that is the fediverse
    talking), never a mirror, never a protected (`-`) event.

    Returns an async predicate over a whole PAGE, so "is this a mirrored note?" is one query per
    page rather than one per event (almost every reply and reaction carries an `e` tag)."""
    followed = await state.nostr_users_with_followers()     # raises on a failed read: stop the pass
    puppets = _puppet_set()

    def _direct(ev: dict, mirrored: set) -> bool:
        tags = ev.get("tags") or []
        if any(len(t) > 1 and t[0] in ("p", "P") and t[1] in puppets for t in tags):
            return True
        return any(len(t) > 1 and t[0] in ("e", "E") and t[1] in mirrored for t in tags)

    async def page(evs: list) -> set:
        refs = {t[1] for ev in evs for t in ev.get("tags") or [] if len(t) > 1 and t[0] in ("e", "E")}
        mirrored = _mirrored_among(refs)
        deletions = [ev for ev in evs if ev.get("kind") == 5]
        gone_ids = {t[1] for ev in deletions for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e"}
        gone = await _events(list(gone_ids)) if gone_ids else {}
        gone_refs = {t[1] for g in gone.values() for t in g.get("tags") or [] if len(t) > 1 and t[0] in ("e", "E")}
        mirrored |= _mirrored_among(gone_refs)
        ok = set()
        for ev in evs:
            pk = ev.get("pubkey", "")
            if actors.is_actor(pk) or pk in puppets or _is_mirror(ev) or _protected(ev):
                continue
            if pk in followed:
                keep = True
            elif ev.get("kind") == 0:
                keep = False
            elif ev.get("kind") == 5:
                # A deletion goes out when what it deletes did: a reply we sent must not outlive
                # its author's delete on the fediverse just because they have no followers there.
                keep = any(_direct(gone[t[1]], mirrored) for t in ev.get("tags") or []
                           if len(t) > 1 and t[0] == "e" and t[1] in gone)
            else:
                keep = _direct(ev, mirrored)
            if keep and await actors.exposed(pk):
                ok.add(ev["id"])
        return ok
    return page


def _protected(ev: dict) -> bool:
    """NIP-70: the author asked that this event not be republished by anybody else."""
    return any(t == ["-"] for t in ev.get("tags") or [])


async def _events(ids: list) -> dict:
    from app.services import nostr_store
    evs = await nostr_store._ws_query(settings_store._port(), [{"ids": ids[:500]}], strict=True)
    return {e["id"]: e for e in evs if isinstance(e, dict) and e.get("id")}


def _puppet_set() -> frozenset:
    from app.services.activitypub import dm
    return dm._puppet_pubkeys()


def _mirrored_among(event_ids) -> set:
    """Which of these event ids are notes mirrored from the fediverse -- ONE query."""
    ids = list(event_ids)[:2000]
    if not ids:
        return set()
    from app.database import SessionLocal
    from app.models import FediBridgeDelivered
    db = SessionLocal()
    try:
        return {eid for (eid,) in db.query(FediBridgeDelivered.nostr_event_id).filter(
            FediBridgeDelivered.nostr_event_id.in_(ids), FediBridgeDelivered.note_uri.isnot(None)).all()}
    finally:
        db.close()


async def _pass(cursor_key: str, authors, kinds: list, qualifies) -> int:
    """Read since `cursor_key`, translate, deliver, advance. `authors` None = every author (the
    everyone pass), narrowed by `qualifies`."""
    try:
        since = await state.cursor(cursor_key)
    except Exception as e:
        _stats["last_error"] = f"cursor unreadable: {type(e).__name__}"
        return 0                                   # never deliver from a guessed position
    now = int(time.time())
    if not since:
        await state.set_cursor(now, cursor_key)    # first run: no backfill of everything ever posted
        return 0
    if authors is not None and not authors:
        await state.set_cursor(now, cursor_key)
        return 0
    evs = await _since(authors, since, kinds)
    evs = sorted((e for e in evs if e.get("id") not in _seen), key=lambda e: (e.get("created_at", 0), e["id"]))
    wanted = await qualifies(evs) if qualifies is not None else None
    handled, newest = 0, since
    sem = asyncio.Semaphore(8)

    async def one(inbox, act, member):
        async with sem:
            await _send(inbox, act, member)

    for ev in evs:
        if wanted is not None and ev["id"] not in wanted:
            _seen[ev["id"]] = time.time()
            newest = max(newest, int(ev.get("created_at", 0)))
            continue
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
    if len(_seen) > 40000:
        for k in sorted(_seen, key=_seen.get)[:20000]:
            _seen.pop(k, None)
    # Inclusive of the newest second handled (more may share it; `_seen` stops a replay).
    if newest != since:
        await state.set_cursor(newest, cursor_key)
    return handled


_failures: dict = {}
_MAX_PAGES = 20


async def _since(members, since: int, kinds: list = None) -> list:
    """Everything members published since `since`. The relay answers NEWEST FIRST with a limit, so
    one query after a long quiet spell (worker down, a busy bot) would return only the newest page,
    and a cursor moved to it would skip everything older. So it pages backwards with `until` until a
    page comes back short. Bounded: past _MAX_PAGES the backlog is too old to be worth sending, and
    the oldest part is dropped with a note rather than holding delivery up for ever."""
    out, seen_ids, until = [], set(), None
    for _ in range(_MAX_PAGES):
        flt = {"kinds": kinds or KINDS, "since": since, "limit": _PAGE}
        if members is not None:
            flt["authors"] = members
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


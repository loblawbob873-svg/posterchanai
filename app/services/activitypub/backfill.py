"""FETCHING from the fediverse -- the half of a real server that only ever waiting for deliveries lacks.

The inbox stores what other servers SEND, and they send a post only to servers where somebody
follows its author. Nothing here ever read anything back: following an account showed none of its
history until it posted again, a fediverse person's profile showed only what happened to be
delivered, and a thread showed only the replies from accounts somebody here follows. The old
Pleroma bridge hid all of it by mirroring a whole federated timeline. Measured when it was reported
(2026-09-27): JohnSmith87298753@nicecrew.digital -- followed by nobody here -- had 1 of his last 20
posts on poster.place, and fediverse traffic otherwise depended on one outside relay.

Three reads, all from the account's OWN server (remote.fetch_json: signed GET for authorized-fetch
servers, SSRF-checked, pinned to the address it judged), and every note goes through the inbox's own
`store_note` -- public only, not blocked, on its author's server, stored once -- never around it:

  * an account's recent posts (its outbox), when a member's follow is accepted, once for every
    account followed before this existed, and when somebody opens that account's profile here;
  * a post's replies (its `replies` collection), when somebody opens the thread here.

Every account is read at most once per REFRESH (a relay-stored marker, so a restart does not repeat
it), one at a time, and a failed read marks nothing -- "could not ask" is never "has nothing".
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time

from app.services.activitypub import convert, remote, state

logger = logging.getLogger(__name__)

RECENT = 20                 # an account's posts brought in per backfill
PAGES = 3                   # outbox pages read at most (a page of boosts can hold no posts)
REPLIES = 40                # a thread's replies brought in per read
ANCESTORS = 30              # posts climbed above an opened post, to reach its thread's first post
REFRESH = 6 * 3600          # an account or thread is re-read at most this often
_POSTS = ("Note", "Article", "Question", "Page")
_MARK = "pcai:ap:backfilled:"
# PACING -- this reads other people's servers, and writes into our own relay.
PACE = 30.0                 # seconds between BACKGROUND reads (discovery, catch-up, a follow)
HOST_GAP = 300.0            # one server is read in the background at most this often
BUSY_REST = 3600.0          # a server that answered 429 or 5xx is left alone this long
NOTE_GAP = 0.25             # between posts written into our relay
DISCOVERY_PER_HOUR = 120    # newly met accounts queued per hour, at most
_last_bg = 0.0
_host_last: dict = {}
_host_rest: dict = {}
_discovery_times: list = []


def _resting(host: str) -> bool:
    return _host_rest.get(host, 0.0) > time.monotonic()


def _note_failure(host: str, err: Exception) -> None:
    """A server that says it is busy (429) or failing (5xx) gets an hour of peace."""
    msg = str(err)
    if "429" in msg or any(f"HTTP {c}" in msg for c in range(500, 600)):
        _host_rest[host] = time.monotonic() + BUSY_REST


async def _pace(actor_or_object: str, background: bool) -> str:
    """Wait for this read's turn, or say why it should not happen now ("" = go ahead). Called with
    the gate held, so reads stay one at a time and the wait is the whole node's pace, not a queue of
    parallel sleepers."""
    global _last_bg
    host = remote.host_of(actor_or_object)
    if _resting(host):
        return "server is resting"
    if background:
        if time.monotonic() - _host_last.get(host, -1e12) < HOST_GAP:
            return "server read recently"
        wait = _last_bg + PACE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_bg = time.monotonic()
    _host_last[host] = time.monotonic()
    if len(_host_last) > 20000:
        _host_last.clear()
    return ""
_THREAD_MARK = "pcai:ap:threadread:"

_gate = asyncio.Semaphore(1)        # one remote read at a time: this must never be a crawler
# Set while a backfill stores notes. Accounts met INSIDE one (a fetched reply's parent's author) are not
# "discovered" -- that is how fetching one account's history would turn into a crawl of the network.
_fetching = contextvars.ContextVar("pc_ap_backfilling", default=False)
_inflight: set = set()
_tasks: set = set()


def _same_host(a: str, b: str) -> bool:
    return bool(a) and bool(b) and remote.host_of(a) == remote.host_of(b)


async def recent_posts(actor_uri: str, limit: int = RECENT) -> list:
    """The account's most recent posts, newest first, read from its own outbox. Only its OWN posts
    (Create), each on its own server; boosts, replies' parents and anything else are not its history."""
    who = await remote.actor(actor_uri)
    outbox = str(who.get("outbox") or "")
    if not _same_host(outbox, actor_uri):
        raise remote.FetchError("the account's outbox is not on its own server")
    col = await remote.fetch_json(outbox)
    page = col.get("first") or (col if (col.get("orderedItems") or col.get("items")) else None)
    notes, seen, pages = [], set(), 0
    while page and pages < PAGES and len(notes) < limit:
        if isinstance(page, str):
            if not _same_host(page, actor_uri):
                break
            page = await remote.fetch_json(page)
        if not isinstance(page, dict):
            break
        pages += 1
        for item in convert._as_list(page.get("orderedItems") or page.get("items")):
            if not isinstance(item, dict) or item.get("type") != "Create":
                continue
            obj = item.get("object")
            if isinstance(obj, str):
                if not _same_host(obj, actor_uri):
                    continue
                try:
                    obj = await remote.fetch_object(obj)
                except Exception:
                    continue
            if not isinstance(obj, dict) or obj.get("type") not in _POSTS:
                continue
            oid = convert.id_of(obj)
            if convert.id_of(obj.get("attributedTo")) != actor_uri or not _same_host(oid, actor_uri) or oid in seen:
                continue
            seen.add(oid)
            notes.append(obj)
            if len(notes) >= limit:
                break
        page = page.get("next")
    return notes


async def _marked(key: str, min_age: float) -> bool:
    try:
        prev = await state._get(key)
    except Exception:
        return False                    # could not ask: read again rather than trust a guess
    return isinstance(prev, dict) and time.time() - int(prev.get("at") or 0) < min_age


async def backfill_actor(actor_uri: str, *, limit: int = RECENT, min_age: float = REFRESH,
                         background: bool = False) -> dict:
    """Bring an account's recent posts in. {what happened: count}; {"skipped": why} when it did nothing."""
    from app.services.activitypub import inbox
    key = _MARK + state._h(actor_uri)
    if min_age and await _marked(key, min_age):
        return {"skipped": "read recently"}
    if await inbox.blocked_actor(actor_uri):
        return {"skipped": "blocked"}
    async with _gate:
        why = await _pace(actor_uri, background)
        if why:
            return {"skipped": why}                        # not marked: it is read another time
        try:
            notes = await recent_posts(actor_uri, limit)    # raises -> nothing is marked
        except Exception as e:
            _note_failure(remote.host_of(actor_uri), e)
            raise
        counts: dict = {}
        _fetching.set(True)
        for note in reversed(notes):                        # oldest first, like arrival order
            await asyncio.sleep(NOTE_GAP)                  # our relay is not a firehose target
            try:
                what = await inbox.store_note(note, actor_uri, need_gate=False)
            except Exception as e:
                what = f"error: {type(e).__name__}"
            counts[what.split(":")[0]] = counts.get(what.split(":")[0], 0) + 1
        await state._put(key, {"actor": actor_uri, "at": int(time.time()), "n": len(notes)})
    logger.info("[activitypub] backfilled %s: %s", remote.host_of(actor_uri), counts)
    return counts


async def thread_replies(object_uri: str, *, limit: int = REPLIES, min_age: float = REFRESH) -> dict:
    """Bring in the CONVERSATION a post belongs to, from the servers it lives on.

    UP FIRST: the post's ancestors, climbed through `inReplyTo` to the thread's first post (each from
    ITS own server, at most ANCESTORS). Reading only the opened post's replies left a deep fediverse
    thread headless -- measured 2026-10-06: a 167-reply conversation across five servers whose first
    post was never stored, so opening any reply showed one post and "0 replies" ("how come i cant see
    entire thread", "dont we have intelligent backfilling?"). THEN DOWN: the replies each end lists --
    the opened post's and the first post's -- each fetched from its own server and stored through
    store_note, which also brings a missing parent in."""
    from app.services.activitypub import inbox
    key = _THREAD_MARK + state._h(object_uri)
    if min_age and await _marked(key, min_age):
        return {"skipped": "read recently"}
    async with _gate:
        why = await _pace(object_uri, False)
        if why:
            return {"skipped": why}
        _fetching.set(True)
        try:
            post = await remote.fetch_object(object_uri)
        except Exception as e:
            _note_failure(remote.host_of(object_uri), e)
            raise
        counts: dict = {}

        def tally(what: str) -> None:
            k = what.split(":")[0]
            counts[k] = counts.get(k, 0) + 1

        # ---- up: the ancestors, to the first post
        top, cur, seen = post, post, {object_uri}
        for _ in range(ANCESTORS):
            parent = convert.id_of(cur.get("inReplyTo"))
            if not parent or not parent.startswith("https://") or parent in seen:
                break
            seen.add(parent)
            try:
                cur = await remote.fetch_object(parent)          # from its own server, always
            except Exception:
                counts["ancestor unreachable"] = counts.get("ancestor unreachable", 0) + 1
                break
            author = convert.id_of(cur.get("attributedTo"))
            if cur.get("type") not in _POSTS or not author:
                break
            top = cur
            await asyncio.sleep(NOTE_GAP)
            try:
                tally("ancestor " + await inbox.store_note(cur, author, need_gate=False))
            except Exception as e:
                tally(f"ancestor error: {type(e).__name__}")

        # ---- down: the replies each end of the thread lists
        got = 0
        for host_post in ([post, top] if top is not post else [post]):
            host_uri = convert.id_of(host_post) or object_uri
            col = host_post.get("replies")
            if isinstance(col, str):
                try:
                    col = await remote.fetch_json(col) if _same_host(col, host_uri) else {}
                except Exception:
                    col = {}
            page = (col or {}).get("first") or col if isinstance(col, dict) else None
            pages = 0
            while page and pages < PAGES and got < limit:
                if isinstance(page, str):
                    if not _same_host(page, host_uri):
                        break
                    try:
                        page = await remote.fetch_json(page)
                    except Exception:
                        break
                if not isinstance(page, dict):
                    break
                pages += 1
                for item in convert._as_list(page.get("orderedItems") or page.get("items")):
                    if got >= limit:
                        break
                    try:
                        note = await remote.fetch_object(convert.id_of(item))   # from its own server, always
                    except Exception:
                        counts["unreachable"] = counts.get("unreachable", 0) + 1
                        continue
                    author = convert.id_of(note.get("attributedTo"))
                    if note.get("type") not in _POSTS or not author:
                        continue
                    got += 1
                    await asyncio.sleep(NOTE_GAP)
                    try:
                        tally(await inbox.store_note(note, author, need_gate=False))
                    except Exception as e:
                        tally(f"error: {type(e).__name__}")
                page = page.get("next")
        await state._put(key, {"object": object_uri, "at": int(time.time()), "n": got})
    logger.info("[activitypub] thread %s: %s", remote.host_of(object_uri), counts)
    return counts


DISCOVERED_REFRESH = 7 * 86400     # an account met in passing is read once a week at most
_QUEUE_MAX = 100                    # background reads waiting at once; past this, the next sighting retries
_sighted: set = set()


def discovered(actor_uri: str) -> None:
    """Pleroma's "fetch initial posts": the first time this server meets a fediverse account -- in a
    boost, a reply, a mention, a follow -- its recent posts come in too. Called for every note the
    inbox stores, so the per-process `_sighted` set is what keeps this free after the first time; the
    relay marker (DISCOVERED_REFRESH) is what keeps a restart from reading everyone again."""
    if not actor_uri or actor_uri in _sighted or _fetching.get():
        return
    if len(_inflight) >= _QUEUE_MAX:
        return                          # busy: not marked sighted, so a later sighting asks again
    now = time.monotonic()
    while _discovery_times and now - _discovery_times[0] > 3600:
        _discovery_times.pop(0)
    if len(_discovery_times) >= DISCOVERY_PER_HOUR:
        return                          # the hour's allowance is spent; a later sighting asks again
    _discovery_times.append(now)
    _sighted.add(actor_uri)
    if len(_sighted) > 200000:
        _sighted.clear()
    schedule(actor_uri, min_age=DISCOVERED_REFRESH)


def schedule(actor_uri: str, *, min_age: float = REFRESH) -> bool:
    """Backfill in the background (the caller -- an inbox answer, a page -- must not wait for it).
    False when that account is already being read."""
    if not actor_uri or actor_uri in _inflight:
        return False
    _inflight.add(actor_uri)

    async def run():
        try:
            await backfill_actor(actor_uri, min_age=min_age, background=True)
        except Exception as e:
            logger.info("[activitypub] backfill of %s failed: %s: %s", remote.host_of(actor_uri),
                        type(e).__name__, e)
        finally:
            _inflight.discard(actor_uri)
    task = asyncio.ensure_future(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def catch_up(per_tick: int = 2) -> int:
    """The accounts followed BEFORE backfill existed: a few per tick, each once (its marker), so 300+
    accounts arrive over a few hours rather than as a burst. Returns how many it read this tick."""
    try:
        followed = await state.followed_actors(max_age=300)
        from app.services import nostr_store
        done = await nostr_store.list_docs(state._port(), _MARK, seckey=state._seckey(), strict=True, limit=100000)
    except Exception:
        return 0                        # could not ask what is done: do nothing rather than repeat
    have = {d.get("actor") for d in done.values() if isinstance(d, dict)}
    todo = [a for a in sorted(followed) if a not in have]
    n = 0
    for actor in todo:
        if n >= per_tick:
            break
        host = remote.host_of(actor)
        if _resting(host) or time.monotonic() - _host_last.get(host, -1e12) < HOST_GAP:
            continue                    # that server was read a moment ago: somebody else's turn
        try:
            got = await backfill_actor(actor, min_age=0, background=True)
            if "skipped" in got:
                continue
            n += 1
        except Exception as e:
            logger.info("[activitypub] catch-up of %s failed: %s", remote.host_of(actor), type(e).__name__)
            # Marked anyway, so one dead server is not retried every tick forever; a follow, a
            # profile view or the next REFRESH window reads it again.
            try:
                await state._put(_MARK + state._h(actor), {"actor": actor, "at": int(time.time()), "n": 0,
                                                           "failed": type(e).__name__})
            except Exception:
                pass
    return n

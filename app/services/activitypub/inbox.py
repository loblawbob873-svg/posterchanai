"""Incoming activities → Nostr events. The inverse of outbox.py.

Everything that is CONTENT becomes an ordinary Nostr event signed by the author's PUPPET key -- the
same deterministic key the Pleroma timeline bridge gives them (fedi_bridge_identity.ensure_puppet)
-- with a NIP-48 `proxy` tag back to the object, and a row in the SAME `FediBridgeDelivered` table
the bridge dedups on. That shared table is the whole compatibility story in this direction:

  * a note that arrives here AND through the Pleroma timeline mirror is stored once, whichever path
    is first (both check the canonical note URI before publishing);
  * a Nostr reply/like/boost ON one of these puppet notes is exactly what the bridge's write-back
    already understands, because to it this is just another mirrored note.

WHAT GETS IN. A post is stored only if a member follows its author, or it mentions a member, or it
replies to a member's post -- otherwise anybody on the fediverse could fill this relay by sending
to its inbox. Followers-only and direct posts are never stored (a kind-1 is public; see
convert.is_public). Instance blocking is the relay's: every event here carries a `proxy` tag and
the relay rejects blocked instances on ingest.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse

from app.services import settings_store
from app.services.activitypub import actors, config, convert, remote, state

logger = logging.getLogger(__name__)

_tasks: set = set()
_ACTOR_PATH = "/ap/users/"


def _port() -> int:
    return settings_store._port()


def _origin(uri: str) -> str:
    p = urlparse(uri or "")
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _our_member_name(uri: str) -> str:
    base = config.base_url()
    if base and isinstance(uri, str) and uri.startswith(base + _ACTOR_PATH):
        return uri[len(base + _ACTOR_PATH):].split("/")[0].split("#")[0]
    return ""


MAX_PENDING = 200
MAX_PENDING_PER_HOST = 40
_pending_by_host: dict = {}


def schedule(activity: dict, signer: str) -> bool:
    """Process an activity after the 202 has gone back -- remote servers time out slow inboxes and
    then retry, which is how a slow inbox gets its own traffic twice. BOUNDED: each one can make
    outbound fetches, so past MAX_PENDING the caller answers 503 and the sender retries later,
    instead of this process holding an unlimited pile of work for whoever sends fastest."""
    host = remote.host_of(signer)
    # Per SENDER too: one server whose activities point at objects it serves slowly could otherwise
    # hold every slot and make this inbox refuse the whole fediverse.
    if len(_tasks) >= MAX_PENDING or _pending_by_host.get(host, 0) >= MAX_PENDING_PER_HOST:
        return False
    _pending_by_host[host] = _pending_by_host.get(host, 0) + 1
    t = asyncio.create_task(_run(activity, signer))
    _tasks.add(t)

    def done(task, host=host):
        _tasks.discard(task)
        left = _pending_by_host.get(host, 1) - 1
        if left > 0:
            _pending_by_host[host] = left
        else:
            _pending_by_host.pop(host, None)
    t.add_done_callback(done)
    return True


def _own_id(activity_id: str, signer: str) -> bool:
    """An activity's id must be the SENDER'S -- non-empty and on its host. Otherwise it could name
    somebody else's note (poisoning the dedup shared with the Pleroma bridge) or be blank (never
    deduplicated, so one account could like a post without limit)."""
    return bool(activity_id) and remote.host_of(activity_id) == remote.host_of(signer)


async def authentic(obj, signer: str) -> dict:
    """The object an activity refers to, trusted only as far as it can be: embedded by its OWN
    author it is taken as sent (the signature covers it); anything else -- a boost of somebody
    else's note, a bare id -- is fetched from its own server and must be the object asked for.
    Without that a boost could carry a forged note 'by' anyone."""
    oid = convert.id_of(obj)
    if isinstance(obj, dict) and convert.id_of(obj.get("attributedTo")) == signer \
            and remote.host_of(oid) == remote.host_of(signer):
        return obj
    if not oid:
        raise remote.FetchError("no object id")
    return await remote.fetch_object(oid)


async def _run(activity: dict, signer: str) -> None:
    try:
        await asyncio.wait_for(process(activity, signer), timeout=90)
    except Exception as e:
        logger.info("[activitypub] %s from %s not processed: %s: %s", activity.get("type"),
                    remote.host_of(signer), type(e).__name__, e)


# ------------------------------------------------------------------------------------ dispatch

def acct_of_actor(doc: dict) -> str:
    user = convert.username_of(doc)
    host = remote.host_of(convert.id_of(doc))
    return f"{user}@{host}" if user and host else ""


async def blocked_actor(actor_id: str) -> bool:
    """On a blocked instance, a single account blocked by a `user@host` line in the lists, or an
    account whose key is blocked on the relay (a block made from the client -- see
    actors.puppet_blocked)."""
    if config.host_blocked(remote.host_of(actor_id)):
        return True
    if actors.puppet_blocked(actor_id):
        return True
    try:
        doc = await remote.actor(actor_id)
    except remote.FetchError:
        return False
    acct = acct_of_actor(doc)
    return config.account_blocked(acct) or actors.puppet_blocked(actor_id, acct)


FORWARDED = "_ForwardedCreate"
FORWARDED_DELETE = "_ForwardedDelete"


async def _forwarded_delete(object_id: str) -> str:
    """A Delete a third server FORWARDED (Mastodon forwards the deletion of a reply, signed by itself).
    Its word proves nothing, so the object's OWN server is asked: gone there (404/410, or a
    Tombstone) is the proof, and the stored copy goes. Answered 401, the forwarder retried for days."""
    if not object_id:
        return "ignored: forwarded nothing"
    row = await asyncio.to_thread(_delivered, object_id)
    if not row:
        return "ignored: nothing stored for that"
    author = await asyncio.to_thread(_actor_of_puppet, row.nostr_pubkey or "")
    if not author or remote.host_of(author) != remote.host_of(object_id):
        return "ignored: not a note by someone on its own server"
    try:
        doc = await remote.fetch_json(object_id)
        gone = doc.get("type") == "Tombstone"
    except remote.FetchError as e:
        gone = str(e).startswith(("HTTP 410", "HTTP 404"))
    if not gone:
        return "ignored: still there on its own server"
    from app.services.fedi_bridge_identity import delete_note
    ok = await delete_note(_port(), author, row.nostr_event_id, broadcast=config.broadcast())
    return "deleted (confirmed with its server)" if ok else "relay refused the deletion"


async def _forwarded(object_id: str) -> str:
    """A post a third server FORWARDED (Mastodon forwards replies to its users' followers, signed by
    the forwarder, not the author). The forwarder's word proves nothing about the post, so it is
    fetched from its OWN server and handled exactly as if its author had sent it."""
    if not object_id:
        return "ignored: forwarded nothing"
    doc = await remote.fetch_object(object_id)
    author = convert.id_of(doc.get("attributedTo"))
    if doc.get("type") not in ("Note", "Article", "Question", "Page") or not author \
            or remote.host_of(author) != remote.host_of(object_id):
        return "ignored: forwarded object is not a post by someone on its own server"
    return await process({"type": "Create", "id": object_id, "actor": author, "object": doc}, author)


async def process(activity: dict, signer: str) -> str:
    """Handle one verified activity; returns what was done (for logs and tests)."""
    if await blocked_actor(signer):
        return "ignored: blocked account"
    kind = activity.get("type")
    obj = activity.get("object")
    if kind == FORWARDED:
        return await _forwarded(convert.id_of(obj))
    if kind == FORWARDED_DELETE:
        return await _forwarded_delete(convert.id_of(obj))
    if kind == "Follow":
        if convert.id_of(obj) == f"{config.base_url()}/ap/actor":
            from app.services.activitypub import relays
            return await relays.follow_back(activity, signer)
        async with _lock(f"follow:{signer}"):
            return await _follow(activity, signer)
    if kind == "Undo":
        async with _lock(f"follow:{signer}"):
            return await _undo(obj if isinstance(obj, dict) else {"id": convert.id_of(obj)}, signer)
    if kind == "Move":
        return await _move(activity, signer)
    if kind == "Flag":
        return await _flag(activity, signer)
    if kind in ("Accept", "Reject"):
        from app.services.activitypub import relays
        if relays.is_relay_follow(convert.id_of(obj)):
            return await relays.answer(kind, obj, signer)
        return await _accept(kind, obj, signer)
    if kind == "Block":
        return await _block(activity, signer, undo=False)
    if kind == "Create":
        note = await authentic(obj, signer)
        if note.get("type") not in ("Note", "Article", "Question", "Page"):
            return "ignored: not a note"
        # The CREATOR is the author -- not merely somebody on the same server, or any account on an
        # instance could post as its neighbours.
        if convert.id_of(note.get("attributedTo")) != signer:
            return "ignored: posted for somebody else"
        if convert.is_vote(note):
            # A poll answer: addressed to the poll's author only, so it would otherwise be read as a DM.
            return await _vote(note, signer)
        if not convert.is_public(note):
            # Followers-only or direct: never a public kind-1. Addressed to one of ours, it is a
            # direct message (dm.py) -- delivered privately, and only to someone who agreed to hear.
            from app.services.activitypub import dm
            return await dm.receive_direct(note, signer)
        return await store_note(note, signer, need_gate=True)
    if kind == "Announce":
        return await _announce(activity, signer)
    if kind in ("Like", "EmojiReact"):
        return await _like(activity, signer)
    if kind == "Delete":
        if convert.id_of(obj) == signer:
            await state.mark_gone(signer)
            return "account deleted"
        return await _delete(convert.id_of(obj), signer)
    if kind == "Update" and isinstance(obj, dict) and obj.get("type") in ("Note", "Article", "Question", "Page"):
        async with _lock(f"edit:{convert.id_of(obj)}"):
            return await _edit(obj, signer)
    if kind == "Update" and isinstance(obj, dict) and obj.get("type") in ("Person", "Service", "Group"):
        if convert.id_of(obj) == signer:
            await _puppet(await remote.actor(signer, refresh=True))
            return "profile refreshed"
    return f"ignored: {kind}"


_locks: dict = {}


class _lock:
    """One activity at a time per key (per signer for follows, per note for edits). They run as
    concurrent tasks, so an Undo arriving right behind its Follow could finish first and the Follow
    then re-add a follower who had just left; two Updates of one note each stored a new copy."""
    def __init__(self, key: str):
        self.key = key

    async def __aenter__(self):
        entry = _locks.setdefault(self.key, [asyncio.Lock(), 0])
        entry[1] += 1
        await entry[0].acquire()

    async def __aexit__(self, *exc):
        entry = _locks.get(self.key)
        if entry:
            entry[0].release()
            entry[1] -= 1
            if entry[1] <= 0:
                _locks.pop(self.key, None)


# ------------------------------------------------------------------------------------ follows

NEW_FOLLOWERS_PER_HOST_PER_HOUR = 300
_new_followers: dict = {}


def _new_follower_ok(host: str) -> bool:
    """Every follower costs a signed POST on every post a member makes, and a follower's inbox only
    has to be on its own server -- which a wildcard DNS name makes free. So one server may add only
    so many NEW followers an hour; the ones it already has are never touched."""
    now = time.monotonic()
    start, n = _new_followers.get(host, (now, 0))
    if now - start > 3600:
        start, n = now, 0
    if n >= NEW_FOLLOWERS_PER_HOST_PER_HOUR:
        return False
    _new_followers[host] = (start, n + 1)
    if len(_new_followers) > 20000:
        for k in sorted(_new_followers, key=lambda k: _new_followers[k][0])[:4000]:
            _new_followers.pop(k, None)
    return True


async def _follow(activity: dict, signer: str) -> str:
    name = _our_member_name(convert.id_of(activity.get("object")))
    member = await actors.member_of_path(name) if name else ""
    if not member:
        return "ignored: not one of our actors"
    if convert.id_of(activity.get("actor")) != signer:
        return "ignored: follow for somebody else"
    who = await remote.actor(signer)
    inbox = remote.inbox_of(who)
    # On the FOLLOWER'S own server: otherwise a thousand made-up followers pointing at one victim's
    # address would turn every post here into a flood at that victim.
    if not inbox or remote.host_of(inbox) != remote.host_of(signer):
        return "ignored: follower has no inbox on its own server"
    known = await state.is_follower(member, signer)
    if not known and not _new_follower_ok(remote.host_of(signer)):
        return "ignored: too many new followers from that server this hour"
    await state.add_follower(member, signer, inbox)
    if convert.id_of(activity):
        await state.remember_follow_id(convert.id_of(activity), member, signer)
    # An account marked GONE that follows again is plainly back (Mastodon deletes a suspended account
    # and restores it on unsuspend): left marked, it would never be delivered to again.
    from app.services.activitypub import outbox as _ob
    if signer in await _ob._gone():
        await state.unmark_gone(signer)
        _ob._gone_cache["at"] = 0.0
    await _announce_follows(signer, who, member, following=True)
    keys = await state.keypair(member)              # a verified Follow is behind it
    key_id, priv = await actors.signing(member, keys)
    me = await actors.actor_id(member)
    accept = {"@context": convert.AS_CONTEXT, "id": f"{me}#accepts/{int(time.time() * 1000)}",
              "type": "Accept", "actor": me, "object": {k: activity[k] for k in ("id", "type", "actor", "object")
                                                        if k in activity}}
    personal = who.get("inbox") if remote.host_of(who.get("inbox") or "") == remote.host_of(signer) else ""
    status = await remote.deliver(personal or inbox, accept, key_id=key_id, private_pem=priv)
    return f"follower added (accept HTTP {status})"


async def _announce_follows(actor: str, actor_doc, member: str, *, following: bool) -> None:
    """Say it the NOSTR way: the fediverse account's puppet publishes a contact list naming the accounts
    here it follows. A Nostr client raises "X followed you" for exactly that event -- without it, a
    fediverse follow was accepted and recorded and the person followed was never told.

    Best effort, and never allowed to cost the follow itself: the Accept has to go out regardless.
    One at a time per account (a Follow of one member racing an Undo of another would lose one), and
    NEVER broadcast beyond this relay: who follows whom is something a fediverse user may have chosen
    to hide, so it goes only to the person followed."""
    async with _lock(f"follows:{actor}"):
        await _announce_follows_locked(actor, actor_doc, member, following=following)


async def _announce_follows_locked(actor: str, actor_doc, member: str, *, following: bool) -> None:
    try:
        members = set(await state.follows_of(actor))
        if (member in members) == following:
            return                                    # nothing changed: republishing would re-notify
        members = members | {member} if following else members - {member}
        await state.set_follows_of(actor, sorted(members))
        puppet = None
        try:
            doc = actor_doc if isinstance(actor_doc, dict) else await remote.actor(actor)
            puppet = await _puppet(doc)
        except remote.FetchError:
            pass
        if not puppet:
            # An account that can no longer be fetched (deleted, its server down) still has its key:
            # the unfollow is published rather than left saying "follows you" for ever.
            from app.services.fedi_bridge_identity import puppet_from_actor
            row_actor = await asyncio.to_thread(_puppet_actor_uri, actor)
            puppet = puppet_from_actor(row_actor or actor)
        from app.services.fedi_bridge_identity import build_event, publish, query_one
        # STRICTLY NEWER than the list it replaces. A kind-3 is replaceable, and two in one second (a
        # follow and its undo, back to back) are a tie a relay breaks by the LOWER id -- which can keep
        # the follow and throw away the unfollow.
        ok, prev = await query_one(_port(), {"kinds": [3], "authors": [puppet["pubkey_hex"]], "limit": 1})
        at = max(int(time.time()), int((prev or {}).get("created_at") or 0) + 1)
        ev = build_event(puppet, 3, "", tags=[["p", m] for m in sorted(members)], broadcast=False, created_at=at)
        ok, msg = await publish(_port(), ev)
        if not ok:
            logger.info("[activitypub] follow of %s not announced on Nostr: %s", member[:12], msg)
            return
        # A Nostr user here in `everyone` mode reads their OWN relays, not this one -- the unfollow too.
        if not actors.is_actor(member) and (following or await actors.exposed(member)):
            from app.services.activitypub import nostrside
            await nostrside.deliver(member, [ev], dm=False)
    except Exception as e:
        logger.info("[activitypub] follow of %s not announced on Nostr: %s: %s", member[:12], type(e).__name__, e)


async def _accept(kind: str, obj, signer: str) -> str:
    """The answer to a Follow WE sent -- and only to one we sent. An unsolicited Accept would
    otherwise make any account 'followed', which is the gate that lets its posts into the relay.

    The Follow may come back embedded or as just its id (`<our actor>#follows/<hash of target>`);
    both are mapped back to the member and the account they asked to follow."""
    follow_id = convert.id_of(obj)
    follow = obj if isinstance(obj, dict) else {}
    me = convert.id_of(follow.get("actor")) or follow_id.split("#follows/")[0]
    name = _our_member_name(me)
    member = await actors.member_of_path(name) if name else ""
    if not member:
        return "ignored: not our follow"
    target = convert.id_of(follow.get("object"))
    if not target:
        from app.services.activitypub.outbox import _h
        h = follow_id.split("#follows/")[-1].split("/")[0]
        target = next((a for a in await state.following(member) if _h(a) == h), "")
    if target != signer:
        return "ignored: answered by somebody other than who was followed"
    current = (await state.following(member)).get(signer)
    if not current:
        return "ignored: we never asked to follow them"
    if kind == "Accept":
        if current.get("state") != "accepted":
            await state.set_following(member, signer, current.get("inbox") or "", "accepted",
                                      follow_id=current.get("id") or "")
    else:
        await state.drop_following(member, signer)
    state.forget_followed_cache()
    return f"follow {kind.lower()}ed"


async def _undo(inner: dict, signer: str) -> str:
    itype = inner.get("type")
    # FIRST: an undone Block names one of our actors as its object too, and read by the rule below it
    # would be taken for an unfollow.
    if itype == "Block":
        return await _block(inner, signer, undo=True)
    if itype == "Follow" or _our_member_name(convert.id_of(inner.get("object"))):
        member = await actors.member_of_path(_our_member_name(convert.id_of(inner.get("object"))))
        if member:
            await state.remove_follower(member, signer)
            await _announce_follows(signer, None, member, following=False)
            return "follower removed"
    if not itype and inner.get("id"):
        # An Undo naming the Follow by ID ONLY (allowed, and sent that way by some servers): mapped
        # back through the id recorded when the Follow arrived -- and only by the account that sent it.
        rec = await state.follow_by_id(inner["id"])
        if rec.get("member") and rec.get("actor") == signer:
            await state.remove_follower(rec["member"], signer)
            await _announce_follows(signer, None, rec["member"], following=False)
            return "follower removed"
    return await _delete(convert.id_of(inner), signer, undo=True)


async def _block(activity: dict, signer: str, *, undo: bool) -> str:
    """A fediverse account blocking one of ours. Recorded for the block bot (who blocked whom, and
    the leaderboard of most-blocked accounts), and -- as Mastodon does -- a block ends the follows
    between the two in both directions, so nothing keeps being delivered across it."""
    name = _our_member_name(convert.id_of(activity.get("object")))
    member = await actors.member_of_path(name) if name else ""
    if not member:
        return "ignored: not one of our actors"
    if undo:
        await state.undo_block(member, signer)
        return "unblocked"
    try:
        acct = acct_of_actor(await remote.actor(signer))
    except remote.FetchError:
        acct = ""
    await state.record_block(member, signer, acct)
    await state.remove_follower(member, signer)
    await _announce_follows(signer, None, member, following=False)
    if signer in await state.following(member, strict=False):
        await state.drop_following(member, signer)
    return "block recorded"


# ------------------------------------------------------------------------------------ content

async def _puppet(actor_doc: dict) -> dict | None:
    from app.database import SessionLocal
    from app.services.activitypub import dm
    from app.services.fedi_bridge_identity import ensure_puppet
    db = SessionLocal()
    try:
        p = await ensure_puppet(db, _port(), convert.account_from_actor(actor_doc),
                                remote.host_of(convert.id_of(actor_doc)))
    finally:
        db.close()
    if p:
        dm.remember_puppet(p["pubkey_hex"])      # a puppet made a moment ago is still a puppet
    return p


def _delivered(uri: str):
    from app.database import SessionLocal
    from app.models import FediBridgeDelivered
    db = SessionLocal()
    try:
        return db.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).first() if uri else None
    finally:
        db.close()


def _record(uri: str, event_id: str, pubkey: str, acct: str = "") -> None:
    from app.database import SessionLocal
    from app.models import FediBridgeDelivered
    db = SessionLocal()
    try:
        if not db.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).first():
            db.add(FediBridgeDelivered(platform="activitypub", instance_url=_origin(uri), note_id=uri[:255],
                                       note_uri=uri[:512], author_acct=(acct or None),
                                       nostr_event_id=event_id, nostr_pubkey=pubkey))
            db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[activitypub] dedup row for %s not saved: %s", remote.host_of(uri), e)
    finally:
        db.close()


async def _event(event_id: str) -> dict | None:
    from app.services.fedi_bridge_identity import query_one
    ok, ev = await query_one(_port(), {"ids": [event_id], "limit": 1})
    return ev if ok else None


async def _target(uri: str) -> tuple[str, str]:
    """(event id, author pubkey) for an object URI: one of our members' posts, or a mirrored one."""
    eid = convert.event_id_from_object_url(config.base_url(), uri)
    if eid:
        ev = await _event(eid)
        return (eid, ev.get("pubkey", "")) if ev else ("", "")
    row = await asyncio.to_thread(_delivered, uri)
    return (row.nostr_event_id, row.nostr_pubkey or "") if row else ("", "")


async def store_note(note: dict, author: str, *, need_gate: bool, depth: int = 0) -> str:
    """Store a public note as the author's puppet kind-1. Returns what happened."""
    from app.services.fedi_bridge_identity import build_event, publish
    from app.services.fedi_normalize import _emoji_url_map, emoji_tags_for
    uri = convert.id_of(note)
    if not uri or remote.host_of(uri) != remote.host_of(author):
        return "ignored: note is not on its author's server"
    if await blocked_actor(author):                 # e.g. a blocked account's post, boosted by somebody else
        return "ignored: blocked account"
    if not convert.is_public(note):
        return "ignored: not public"
    if await asyncio.to_thread(_delivered, uri):
        return "already stored"
    local = actors.local_actor_map()
    # A POLL becomes a NIP-88 poll (kind 1068) that Nostr users can vote in -- their votes go back to
    # its server (outbox._vote). Its options are tags, so they are not also listed in the text.
    poll_opts, poll_multi, poll_end = convert.question_poll(note) if note.get("type") == "Question" else ([], False, 0)
    text, tags = convert.note_content({**note, "type": "Note"} if len(poll_opts) >= 2 else note, local_actors=local)
    # A mention counts only of an account that IS reachable here -- not of any npub-shaped URL.
    kept = []
    for t in tags:
        if t[0] != "p" or actors.is_actor(t[1]) or await actors.exposed(t[1]):
            kept.append(t)
    tags = kept
    parent_id, parent_pk = ("", "")
    reply_to = convert.id_of(note.get("inReplyTo"))
    if reply_to:
        parent_id, parent_pk = await _target(reply_to)
    parent_known = bool(parent_id)
    if need_gate:
        mentions_ours = any(t[0] == "p" for t in tags)
        replies_to_ours = bool(parent_pk) and (actors.is_actor(parent_pk) or await actors.exposed(parent_pk))
        followed = author in await state.followed_actors()
        if not (followed or mentions_ours or replies_to_ours):
            return "ignored: nobody here follows or was addressed"
    if reply_to and not parent_known and depth == 0:
        # A reply to something not stored here would become a top-level post with no thread. Bring
        # the parent in -- ONE level, from its own server, and only for a note already admitted.
        try:
            pdoc = await asyncio.wait_for(remote.fetch_object(reply_to), timeout=20)
            pauthor = convert.id_of(pdoc.get("attributedTo"))
            if pdoc.get("type") in ("Note", "Article", "Question", "Page") and pauthor \
                    and remote.host_of(pauthor) == remote.host_of(reply_to):
                await store_note(pdoc, pauthor, need_gate=False, depth=1)
                parent_id, parent_pk = await _target(reply_to)
        except Exception as e:
            logger.info("[activitypub] parent of %s not fetched: %s", remote.host_of(uri), type(e).__name__)
    if not text.strip():
        return "ignored: empty"
    if len(text) > 60000:
        return "ignored: too long"
    text, more = await _link_mentions(note, text, remote.host_of(author), is_reply=bool(reply_to))
    tags += [t for t in more if t not in tags]
    who = await remote.actor(author)
    puppet = await _puppet(who)
    if not puppet:
        return "ignored: no puppet"
    if parent_id:
        # The thread's ROOT is the parent's root, not the parent: marking the parent as root put every
        # reply deeper than one level in the wrong thread in Nostr clients.
        root = parent_id
        pev = await _event(parent_id)
        for t in (pev or {}).get("tags") or []:
            if len(t) > 3 and t[0] == "e" and t[3] == "root" and len(t[1]) == 64:
                root = t[1]
                break
        tags = [["e", root, "", "root"], ["e", parent_id, "", "reply"]] + \
               ([["p", parent_pk]] if parent_pk else []) + [t for t in tags if t != ["p", parent_pk]]
    elif reply_to:
        tags.append(["r", reply_to])        # the thread it belongs to, where nothing here holds it
    quote = next((q for q in (convert.id_of(note.get(k)) for k in ("quoteUrl", "_misskey_quote", "quoteUri", "quote"))
                  if isinstance(q, str) and q.startswith("https://")), "")
    if quote:
        qid, _qpk = await _target(quote)
        if qid:
            tags.append(["q", qid])         # NIP-18: a client draws the quoted post under this one
    emap = _emoji_url_map([{"shortcode": str(t.get("name") or "").strip(":"), "url": convert.icon_url(t.get("icon"))}
                           for t in convert._as_list(note.get("tag")) if isinstance(t, dict) and t.get("type") == "Emoji"])
    tags += emoji_tags_for(text, emap)
    kind = 1
    if len(poll_opts) >= 2:
        kind = 1068
        tags += [["option", oid, label] for oid, label in poll_opts]
        tags.append(["polltype", "multiplechoice" if poll_multi else "singlechoice"])
        if poll_end:
            tags.append(["endsAt", str(poll_end)])
    ts = convert.parse_time(note.get("published")) or int(time.time())
    ev = build_event(puppet, kind, text, tags=tags, object_uri=uri, broadcast=config.broadcast(),
                     created_at=min(ts, int(time.time())))
    ok, msg = await publish(_port(), ev)
    if not ok:
        return f"relay refused: {msg}"
    await asyncio.to_thread(_record, uri, ev["id"], ev["pubkey"], puppet.get("acct", ""))
    await _reach_nostr_users(ev, puppet)
    return "stored"


MAX_MENTIONS = 20


async def _resolve_mentions(cands: list) -> list:
    """Mastodon-shaped mention dicts for fediverse accounts, each taken from the account's OWN actor
    document -- never from the Mention tag. A tag's `href` and `name` are whatever the sender wrote,
    so trusting them let one note register `victim@mastodon.social` against an address the sender
    controls, and every later sighting of the real person reused that identity (their key, their
    DMs, their deletions). An account already known here is used as it is recorded."""
    from app.database import SessionLocal
    from app.models import FediPuppet
    def known():
        out, fetch = [], []
        db = SessionLocal()
        try:
            for href, user, host in cands:
                if config.host_blocked(host) or config.host_blocked(remote.host_of(href)):
                    continue
                row = db.query(FediPuppet).filter(FediPuppet.actor_uri == href).first()
                if row is not None and row.acct:
                    out.append({"url": href, "acct": row.acct, "username": row.acct.split("@")[0],
                                "shown": f"{user}@{host}"})
                else:
                    fetch.append((href, user, host))
        finally:
            db.close()
        return out, fetch
    out, fetch = await asyncio.to_thread(known)

    async def one(href, user, host):
        try:
            doc = await asyncio.wait_for(remote.actor(href), timeout=15)
        except Exception as e:
            logger.info("[activitypub] mention %s@%s left as text: %s", user, host, type(e).__name__)
            return None
        acct = convert.account_from_actor(doc).get("acct") or ""
        if not acct:
            return None
        return {"url": convert.id_of(doc), "acct": acct, "username": acct.split("@")[0],
                "shown": f"{user}@{host}"}
    out += [m for m in await asyncio.gather(*(one(*c) for c in fetch)) if m]
    return out


async def _link_mentions(note: dict, text: str, author_host: str, *, is_reply: bool) -> tuple[str, list]:
    """Make the note's @mentions CLICKABLE: each `@name` / `@name@host` in the text becomes a
    `nostr:npub…` reference (which every client renders as a profile link) and the person is p-tagged.

    A fediverse note carries its mentions as plain text plus a `Mention` tag list. Stored as-is, the
    names were dead text and only OUR members were tagged -- "usernames not clickable in fediverse
    posts". A mention of one of our users (or an npub account here) links THEIR key; anybody else gets
    their puppet (`mentions.rewrite`: the blocklist, the bare-vs-qualified handle rules). A reply's
    leading run of @-recipients (which fediverse clients hide) is dropped."""
    import re as _re
    from app.database import SessionLocal
    from app.services.activitypub import mentions
    from app.services.nostr import bech32
    ours, cands = [], []
    for t in note.get("tag") or []:
        if not isinstance(t, dict) or t.get("type") != "Mention":
            continue
        href = convert.id_of(t.get("href"))
        name = str(t.get("name") or "").strip().lstrip("@")
        if not href or not name:
            continue
        user, _, host = name.partition("@")
        host = (host or remote.host_of(href)).lower()
        pk = actors.pubkey_of_actor_url(href) if config.is_own_host(remote.host_of(href)) else ""
        if pk:
            if actors.is_actor(pk) or await actors.exposed(pk):
                ours.append((user, host, pk))
        elif not config.is_own_host(remote.host_of(href)) and len(cands) < MAX_MENTIONS:
            cands.append((href, user, host))
    theirs = await _resolve_mentions(cands)
    for m in theirs:
        # The handle the TEXT shows may be a WebFinger domain (`@bob@example.com`) while the actor
        # lives elsewhere; point it at the verified handle so rewrite() links it.
        shown, real = m.pop("shown"), m["acct"]
        if shown.lower() != real.lower():
            text = _re.sub(r"@" + _re.escape(shown) + r"(?![A-Za-z0-9_.\-@])", lambda _m, r=real: "@" + r, text, flags=_re.I)
    tags = []
    for user, host, pk in sorted(ours, key=lambda x: -len(x[0])):
        ref = "nostr:" + bech32.encode("npub", bytes.fromhex(pk))
        text = _re.sub(r"@" + _re.escape(f"{user}@{host}") + r"(?![A-Za-z0-9_.\-@])", ref, text, flags=_re.I)
        text = _re.sub(r"(?<![\w@/])@" + _re.escape(user) + r"(?![A-Za-z0-9_.\-@])", ref, text)
        tags.append(["p", pk])
    if theirs:
        db = SessionLocal()
        try:
            text, ptags = await mentions.rewrite(db, _port(), author_host, text, theirs)
        except Exception as e:
            logger.info("[activitypub] mentions left as text: %s: %s", type(e).__name__, e)
            ptags = []
        finally:
            db.close()
        tags += [t for t in ptags if t not in tags]
    if is_reply:
        stripped = mentions.LEADING_MENTIONS_RE.sub("", text).strip()
        if stripped:
            text = stripped
    return text, tags


async def _reach_nostr_users(ev: dict, puppet: dict) -> None:
    """A reply to, or mention of, a Nostr user who does not read this relay reaches THEIR relays too
    (with the author's profile), or it would sit here unseen by the one person it was written to."""
    from app.services.activitypub import nostrside
    if not config.everyone():
        return
    others = [t[1] for t in ev.get("tags", []) if len(t) > 1 and t[0] == "p" and not actors.is_actor(t[1])]
    if not others:
        return
    extra = await nostrside.puppet_identity_events(puppet["pubkey_hex"])
    for pk in others[:10]:
        if await actors.exposed(pk):
            await nostrside.deliver(pk, extra + [ev], dm=False)


_ACTIVITY_TYPES = ("Like", "Dislike", "EmojiReact", "Delete", "Undo", "Follow", "Accept", "Reject", "Add",
                   "Remove", "Block", "Flag", "Announce", "Move")


async def _announce(activity: dict, signer: str) -> str:
    from app.services.fedi_bridge_identity import build_event, publish
    act_id = convert.id_of(activity)
    if not _own_id(act_id, signer):
        return "ignored: activity id is not the sender's"
    if not convert.is_public(activity):
        return "ignored: not public"
    if await asyncio.to_thread(_delivered, act_id):
        return "already stored"
    obj = activity.get("object")
    # FEP-1b12: a GROUP (a Lemmy community, a Guppe group) relays its members' posts as an Announce of
    # the whole Create. Read as the note itself it had no author, so following a community brought in
    # nothing. The inner object is fetched from its own server like any boosted note; relayed likes,
    # votes and deletions are the community's business, not a post.
    if isinstance(obj, dict) and obj.get("type") in ("Create", "Update"):
        obj = obj.get("object")
    elif isinstance(obj, dict) and obj.get("type") in _ACTIVITY_TYPES:
        return f"ignored: relayed {obj.get('type')}"
    target_uri = convert.id_of(obj)
    eid, pk = await _target(target_uri)
    if not eid:
        if signer not in await state.followed_actors():
            return "ignored: boost of something nobody here asked for"
        note = await authentic(obj, signer)                     # from its own server, as it really is
        if note.get("type") in ("Create", "Update"):             # a bare id that named the activity
            note = await remote.fetch_object(convert.id_of(note.get("object")))
            target_uri = convert.id_of(note)
        author = convert.id_of(note.get("attributedTo"))
        await store_note(note, author, need_gate=False)
        eid, pk = await _target(target_uri)
        if not eid:
            return "ignored: boosted note could not be stored"
    puppet = await _puppet(await remote.actor(signer))
    if not puppet:
        return "ignored: no puppet"
    ev = build_event(puppet, 6, "", tags=[["e", eid], ["p", pk], ["k", "1"]] if pk else [["e", eid], ["k", "1"]],
                     object_uri=act_id, broadcast=config.broadcast())
    ok, msg = await publish(_port(), ev)
    if not ok:
        return f"relay refused: {msg}"
    await asyncio.to_thread(_record, act_id, ev["id"], ev["pubkey"], puppet.get("acct", ""))
    await _reach_nostr_users(ev, puppet)
    return "boost stored"


async def _like(activity: dict, signer: str) -> str:
    from app.services.fedi_bridge_identity import build_event, publish
    act_id = convert.id_of(activity)
    if not _own_id(act_id, signer):
        return "ignored: activity id is not the sender's"
    eid, pk = await _target(convert.id_of(activity.get("object")))
    if not eid or not (actors.is_actor(pk) or await actors.exposed(pk)):
        return "ignored: like of something that is not ours"
    if await asyncio.to_thread(_delivered, act_id):
        return "already stored"
    puppet = await _puppet(await remote.actor(signer))
    if not puppet:
        return "ignored: no puppet"
    # The emoji: Pleroma/Akkoma send EmojiReact with `content`; Misskey a Like with
    # `_misskey_reaction`; Mastodon a bare Like ("+"). A CUSTOM emoji comes as `:shortcode:` plus an
    # Emoji tag with its image -- carried over as a NIP-30 emoji tag, or Nostr clients would show the
    # shortcode as text.
    content = str(activity.get("content") or activity.get("_misskey_reaction") or "").strip() or "+"
    if len(content) > 64:
        content = "+"
    tags = [["e", eid], ["p", pk], ["k", "1"]]
    if content.startswith(":") and content.endswith(":") and len(content) > 2:
        sc = content.strip(":")
        for t in convert._as_list(activity.get("tag")):
            if isinstance(t, dict) and t.get("type") == "Emoji" and str(t.get("name") or "").strip(":") == sc:
                url = convert.icon_url(t.get("icon"))
                if url:
                    tags.append(["emoji", sc, url])
                break
    ev = build_event(puppet, 7, content, tags=tags, object_uri=act_id, broadcast=config.broadcast())
    ok, msg = await publish(_port(), ev)
    if not ok:
        return f"relay refused: {msg}"
    await asyncio.to_thread(_record, act_id, ev["id"], ev["pubkey"], puppet.get("acct", ""))
    await _reach_nostr_users(ev, puppet)
    return "like stored"


async def _edit(obj: dict, signer: str) -> str:
    """An edited post (Update of a Note). A Nostr kind-1 cannot be changed, so the stored copy is
    REPLACED: the new version is published, and only once it is the old one is deleted. Only the
    author may do it (the same rule as a deletion). Runs one at a time per note (see `process`).

    A POLL is sent an Update every time somebody votes, and replacing it would throw away every vote
    Nostr users cast on the stored copy -- so an Update that changes only the counts changes nothing."""
    from app.services.fedi_bridge_identity import delete_note
    note = await authentic(obj, signer)
    if convert.id_of(note.get("attributedTo")) != signer:
        return "ignored: edit for somebody else"
    uri = convert.id_of(note)
    row = await asyncio.to_thread(_delivered, uri)
    if not row:
        return "ignored: edit of a note not stored here"
    author = await _author_of(row, signer)
    if not author:
        return "ignored: edit by somebody other than the author"
    if not convert.is_public(note):
        return "ignored: not public"
    old = row.nostr_event_id
    if note.get("type") == "Question":
        stored = await _event(old)
        opts, _multi, end = convert.question_poll(note)
        if stored and stored.get("kind") == 1068 and [l for _, l in convert.poll_options(stored)] == [l for _, l in opts]:
            text, _t = convert.note_content({**note, "type": "Note"}, local_actors=actors.local_actor_map())
            if (text or "").strip() == (stored.get("content") or "").strip() or not text.strip():
                return "ignored: poll counts only"
    old_acct = row.author_acct or ""
    await asyncio.to_thread(_forget, uri)
    result = "not stored"
    try:
        result = await store_note(note, signer, need_gate=False)
    finally:
        if result != "stored":
            # Put the old copy's mapping back -- on a refusal AND on a failure or a timeout, or the
            # old post could never be deleted and a re-delivered Create would store a duplicate.
            await asyncio.to_thread(_record, uri, old, row.nostr_pubkey or "", old_acct)
    if result != "stored":
        return f"edit not stored: {result}"
    await delete_note(_port(), author, old, broadcast=config.broadcast())
    return "edited"


async def _author_of(row, signer: str) -> str:
    """The actor a stored post's puppet was derived from, IF that is the signer -- by identity, not by
    spelling: the old bridge may have given this person their puppet under the other form of their
    address (/@alice vs /users/alice). "" when the signer is not the author."""
    author = await asyncio.to_thread(_actor_of_puppet, row.nostr_pubkey or "")
    if not author:
        return ""
    if author == signer:
        return author
    try:
        signer_puppet = await _puppet(await remote.actor(signer))
    except remote.FetchError:
        signer_puppet = None
    return author if signer_puppet and signer_puppet.get("pubkey_hex") == row.nostr_pubkey else ""


def _forget(uri: str) -> None:
    from app.database import SessionLocal
    from app.models import FediBridgeDelivered
    db = SessionLocal()
    try:
        db.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).delete()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


_confirming: set = set()


async def confirm_gone(actor: str) -> None:
    """A deleted account's Delete of ITSELF can never verify (its key is gone with it), so it is
    confirmed with the account's own server: 410 Gone (or 404) there is the proof, and nothing a
    third party can fake. Bounded: each unverified Delete costs one fetch at most."""
    if not actor.startswith("https://") or actor in _confirming or len(_confirming) >= 20:
        return
    _confirming.add(actor)
    try:
        await remote.fetch_json(actor)
    except remote.FetchError as e:
        if str(e).startswith(("HTTP 410", "HTTP 404")):
            await state.mark_gone(actor)
    except Exception:
        pass
    finally:
        _confirming.discard(actor)


def _puppet_actor_uri(actor: str) -> str:
    """The actor address a fediverse account's puppet key was DERIVED from (the old bridge may have
    keyed it on the profile URL) -- so a re-derived key is the same key."""
    from app.database import SessionLocal
    from app.models import FediPuppet
    db = SessionLocal()
    try:
        row = db.query(FediPuppet).filter(FediPuppet.actor_uri == actor).first()
        return row.actor_uri if row else ""
    finally:
        db.close()


def _actor_of_puppet(pubkey: str) -> str:
    from app.database import SessionLocal
    from app.models import FediPuppet
    db = SessionLocal()
    try:
        row = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == pubkey).first() if pubkey else None
        return row.actor_uri if row else ""
    finally:
        db.close()


async def _delete(uri: str, signer: str, *, undo: bool = False) -> str:
    """Delete (or undo) what an account stored here -- ONLY its own. Same-server is not enough:
    any account on an instance would otherwise delete its neighbours' posts. The stored event's
    puppet is mapped back to the actor it was derived from, and that must be the signer."""
    from app.services.fedi_bridge_identity import delete_note
    row = await asyncio.to_thread(_delivered, uri)
    if not row:
        return "ignored: nothing stored for that"
    author = await _author_of(row, signer)
    if not author:
        return "ignored: deletion by somebody other than the author"
    ok = await delete_note(_port(), author, row.nostr_event_id, broadcast=config.broadcast())
    return ("undone" if undo else "deleted") if ok else "relay refused the deletion"


# ------------------------------------------------------------------------------------ polls

def _voter_anchor(poll_uri: str, voter: str) -> str:
    """The key a fediverse account VOTES with, one per voter per poll. A vote is public on Nostr
    (NIP-88 counts kind-1018 events by pubkey), while Mastodon shows only the numbers -- so it is NOT
    the voter's puppet: a keyed hash no reader can reverse, stable so a second vote replaces the first."""
    import hashlib
    import hmac
    from app.services.fedi_bridge_identity import _secret
    tag = hmac.new(_secret(), f"vote\n{poll_uri}\n{voter}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{poll_uri}#voter-{tag}"


async def _vote(note: dict, signer: str) -> str:
    """A fediverse account voting in one of OUR polls (a kind-1068 of a member): stored as a NIP-88
    vote (kind 1018), which is what our tally and every Nostr client count. Multiple choice arrives
    as one Note per option; they are merged into the voter's one vote, because NIP-88 counts only a
    pubkey's latest response."""
    from app.services.fedi_bridge_identity import build_event, publish, puppet_from_actor, query_one
    vote_uri = convert.id_of(note)
    if not _own_id(vote_uri, signer):
        return "ignored: vote id is not the sender's"
    eid, pk = await _target(convert.id_of(note.get("inReplyTo")))
    if not eid or not (actors.is_actor(pk) or await actors.exposed(pk)):
        return "ignored: vote on a poll that is not ours"
    poll = await _event(eid)
    if not poll or poll.get("kind") != 1068:
        return "ignored: not a poll"
    end = convert.poll_end(poll)
    if end and end < int(time.time()):
        return "ignored: poll closed"
    label = str(note.get("name") or "").strip()
    oid = next((o for o, l in convert.poll_options(poll) if l == label), "")
    if not oid:
        return "ignored: no such option"
    if await asyncio.to_thread(_delivered, vote_uri):
        return "already stored"
    voter = puppet_from_actor(_voter_anchor(convert.object_url(config.base_url(), eid), signer))
    chosen = [oid]
    ok, prev = await query_one(_port(), {"kinds": [1018], "authors": [voter["pubkey_hex"]], "#e": [eid], "limit": 1})
    if ok and prev:
        before = [t[1] for t in prev.get("tags") or [] if len(t) > 1 and t[0] == "response"]
        if not convert.poll_multi(poll):
            return "ignored: already voted"
        if oid in before:
            return "already stored"
        chosen = before + [oid]
    # LATER than the vote it replaces, strictly: NIP-88 counts a pubkey's latest response, and two in
    # the same second (one Note per option arrives back to back) is a tie any reader may break wrongly.
    at = max(int(time.time()), int((prev or {}).get("created_at") or 0) + 1)
    ev = build_event(voter, 1018, "", tags=[["e", eid], ["p", pk]] + [["response", o] for o in chosen],
                     broadcast=config.broadcast(), created_at=at)
    ok, msg = await publish(_port(), ev)
    if not ok:
        return f"relay refused: {msg}"
    await asyncio.to_thread(_record, vote_uri, ev["id"], ev["pubkey"], "")
    try:
        who = await remote.actor(signer)
        inbox_url = remote.inbox_of(who)
        if inbox_url and remote.host_of(inbox_url) == remote.host_of(signer):
            await state.add_poll_voter_inbox(eid, inbox_url)      # it hears the new counts
    except Exception:
        pass
    return "vote stored"


# ------------------------------------------------------------------------------------ moves, reports

async def _move(activity: dict, signer: str) -> str:
    """An account MOVED (Mastodon's account migration). Believed only when the NEW account names the
    old one in its own `alsoKnownAs` -- fetched from its own server -- or anybody could redirect
    somebody else's followers. Every member following the old account then follows the new one."""
    old = convert.id_of(activity.get("object"))
    new = convert.id_of(activity.get("target"))
    if old != signer or not new or new == old:
        return "ignored: a move of somebody else"
    if await blocked_actor(new):
        return "ignored: moved to a blocked account"
    try:
        doc = await remote.actor(new, refresh=True)
    except remote.FetchError:
        return "ignored: the new account could not be read"
    if signer not in [convert.id_of(x) for x in convert._as_list(doc.get("alsoKnownAs"))]:
        return "ignored: the new account does not confirm the move"
    await state.record_move(old, new)
    inbox_url = remote.inbox_of(doc)
    members = (await state.followed_actors(max_age=0)).get(old, set())
    moved = 0
    for member in sorted(members):
        me = await actors.actor_id(member)
        if not me or not inbox_url:
            continue
        from app.services.activitypub.outbox import _h
        fid = f"{me}#follows/{_h(new)}/{int(time.time() * 1000)}"
        follow = {"@context": convert.AS_CONTEXT, "id": fid, "type": "Follow", "actor": me, "object": new}
        await state.set_following(member, new, inbox_url, "pending", follow_id=fid)
        await state.drop_following(member, old)
        keys = await state.keypair(member)
        key_id, priv = await actors.signing(member, keys)
        await remote.deliver(inbox_url, follow, key_id=key_id, private_pem=priv)
        moved += 1
    state.forget_followed_cache()
    await _puppet(doc)
    return f"moved ({moved} follow(s) moved)"


async def _flag(activity: dict, signer: str) -> str:
    """A report (Flag) from another server about one of ours: stored as a NIP-56 report (kind 1984)
    signed by the reporter's puppet, which is how this node's reports are read. Only reports that
    name OUR accounts or posts are kept -- anything else is not this server's to hear."""
    from app.services.fedi_bridge_identity import build_event, publish
    base = config.base_url()
    people, posts = [], []
    for o in convert._as_list(activity.get("object"))[:20]:
        uri = convert.id_of(o)
        eid = convert.event_id_from_object_url(base, uri)
        if eid:
            ev = await _event(eid)
            if ev and (actors.is_actor(ev.get("pubkey", "")) or await actors.exposed(ev.get("pubkey", ""))):
                posts.append(eid)
                people.append(ev["pubkey"])
            continue
        name = _our_member_name(uri)
        pk = await actors.member_of_path(name) if name else ""
        if pk:
            people.append(pk)
    people = list(dict.fromkeys(people))
    if not people:
        return "ignored: a report about nobody here"
    try:
        puppet = await _puppet(await remote.actor(signer))
    except remote.FetchError:
        puppet = None
    if not puppet:
        return "ignored: no puppet"
    reason = " ".join(str(activity.get("content") or "").split())[:1000]
    tags = [["p", pk, "other"] for pk in people[:10]] + [["e", eid, "other"] for eid in posts[:10]]
    ev = build_event(puppet, 1984, reason, tags=tags, broadcast=False)
    ok, msg = await publish(_port(), ev)
    logger.info("[activitypub] report from %s about %d account(s)%s", remote.host_of(signer), len(people),
                "" if ok else f" not stored: {msg}")
    return "report stored" if ok else f"relay refused: {msg}"

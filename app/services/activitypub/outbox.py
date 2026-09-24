"""Members' own Nostr events → the fediverse. Runs in the WORKER process (app/worker.py).

Nothing is copied: every tick reads what members published to this node's relay since the saved
cursor and translates it on the way out (convert.py). A post's ActivityPub object is served from
the relay by the same id (`/ap/objects/<event id>`), so there is exactly one copy of everything.

    kind 1 / 1111  → Create(Note)   top-level posts, and replies whose parent is on the fediverse
                                    or is a member's post (a reply to a Nostr-only thread would
                                    arrive on the fediverse with no context, so it stays on Nostr)
    kind 6         → Announce       of a fediverse note or a member's post
    kind 7         → Like           ditto; sent to the author, who is the one it concerns
    kind 1068      → Create(Question)  a NIP-88 poll; its tallies go out as Update(Question)
    kind 1018      → a vote (Note with `name`) on a fediverse poll, to the poll's author
    kind 5         → Delete / Undo  for what was sent -- only the deleter's own
    kind 0         → Update(Person)
    kind 3         → Follow / Undo(Follow) for fediverse accounts (puppets) added to or removed from
                                    the contact list -- which is what brings their posts in

Anything carrying a `proxy` tag is a mirror of somebody else's words (a note that arrived from the
fediverse, or another bridge's copy), never a member's own, so it is never sent back out.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from app.services import nostr_store, settings_store
from app.services.activitypub import actors, config, convert, remote, state

logger = logging.getLogger(__name__)

KINDS = [0, 1, 3, 5, 6, 7, 1018, 1068, 1111]
POST_KINDS = (1, 1111, 1068)     # what becomes an ActivityPub object (Note / Question)
_TICK_SECONDS = 30
_PAGE = 500
_RETRY_DELAYS = (60, 300, 1800)
_seen: dict = {}                 # event id → time processed (so the inclusive cursor second is not replayed)
_retries: list = []              # [(due, inbox, activity, member, attempt, key)] -- also on the relay
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
    name = actors.handle(pubkey)
    if name and (actors.is_actor(pubkey) or await actors.exposed(pubkey)):
        href = await actors.actor_id(pubkey)
        shown = await actors.readable_handle(pubkey) or name
        if href:
            return {"href": href, "name": f"@{shown}@{config.domain()}"}
    row = _puppet_row(pubkey)
    # A blocked ACCOUNT (a `user@host` line, or its puppet npub on the relay blocklist) is as out of
    # reach as a blocked instance: a member's mention or reply must not deliver to it either.
    if row and row.actor_uri and not config.host_blocked(remote.host_of(row.actor_uri)) \
            and not (row.acct and config.account_blocked(row.acct)) \
            and not actors.puppet_blocked(row.actor_uri, row.acct or ""):
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
    # Only what IS served as an object: a member's reaction, a protected post or an article has no
    # /ap/objects/ address (it answers 404), and a reply naming one arrived pointing at nothing.
    if ev and ev.get("kind") in POST_KINDS and not _is_mirror(ev) and not _protected(ev) \
            and await actors.exposed(ev.get("pubkey", "")):
        actor = await actors.actor_id(ev["pubkey"])
        if actor:
            return {"uri": convert.object_url(base, event_id), "actor": actor, "remote": False}
    return {}


def _blocked_cached(actor_id: str) -> bool:
    """A follower blocked AFTER it followed (instance or single account) gets nothing more. Uses the
    actor cache only -- delivery must not fetch every follower on every post."""
    if config.host_blocked(remote.host_of(actor_id)):
        return True
    hit = remote._actors.get((actor_id or "").split("#")[0])
    if not hit:
        return actors.puppet_blocked(actor_id)
    from app.services.activitypub.inbox import acct_of_actor
    acct = acct_of_actor(hit[1])
    return config.account_blocked(acct) or actors.puppet_blocked(actor_id, acct)


_gone_cache = {"at": 0.0, "set": frozenset()}


async def _gone() -> frozenset:
    """Deleted fediverse accounts (state.mark_gone), read at most every five minutes."""
    now = time.monotonic()
    if now - _gone_cache["at"] > 300:
        try:
            _gone_cache.update(at=now, set=frozenset(await state.gone_actors()))
        except Exception:
            _gone_cache["at"] = now - 240           # try again in a minute; keep the last good set
    return _gone_cache["set"]


def _is_mirror(ev: dict) -> bool:
    return any(t and t[0] in ("proxy", "fedibridge") for t in ev.get("tags") or [])


def parent_of(ev: dict) -> str:
    """The event this one replies to: NIP-22's lowercase `e` for a comment, NIP-10's `reply` marker
    (else the last UNMARKED `e`) for a kind-1. A tag marked `mention` is a quote, not a parent: read
    as one, a top-level post quoting a Nostr-only note looked like a reply in a Nostr-only thread
    and was never federated at all."""
    es = [t for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e" and t[1]]
    if not es:
        return ""
    if ev.get("kind") == 1111:
        return es[-1][1]
    for marker in ("reply", "root"):
        hit = next((t for t in es if len(t) > 3 and t[3] == marker), None)
        if hit:
            return hit[1]
    # Only a real NIP-10 MARKER in the 4th place says what a tag is. Clients also put the PARENT'S
    # PUBKEY there (["e", id, relay, <pubkey>]) -- read as "marked" that made every such reply look
    # top-level, and it was federated as a new post with no thread (2026-09-24).
    plain = [t for t in es if len(t) < 4 or t[3] not in ("mention", "reply", "root")]
    return plain[-1][1] if plain else ""


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


async def _post_refs(ev: dict) -> tuple[dict, str]:
    """({bech32: fediverse address}, quoted address) for the posts an event references -- NIP-18's
    `q` tag and `nostr:note1…/nevent1…` in the text. A post with no fediverse address is left to
    the converter (it links this node's page for it)."""
    from app.services.nostr import bech32
    refs = {}
    for m in convert._NOSTR_REF_RE.finditer(ev.get("content") or ""):
        value = m.group(1)
        if value.lower().startswith(("note1", "nevent1")):
            raw = bech32.decode_any(value)
            if raw:
                refs[value] = raw.hex()
    q = next((t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "q"
              and len(t[1]) == 64), "")
    links, by_id = {}, {}
    for eid in list(dict.fromkeys(list(refs.values()) + ([q] if q else [])))[:5]:
        target = await resolve_event(eid)
        if target:
            by_id[eid] = target["uri"]
    for value, eid in refs.items():
        if eid in by_id:
            links[value] = by_id[eid]
    quote = by_id.get(q) if q else next((by_id[e] for e in refs.values() if e in by_id), "")
    return links, quote or ""


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

async def build_object(ev: dict, me: str) -> tuple[dict | None, dict]:
    """(the ActivityPub object for a member's post, {"target", "mentions"}) -- a Note, or a Question
    for a poll -- or (None, {}) when the post does not go to the fediverse. ONE builder for delivery,
    the object URL, the outbox and `featured`, so the four can never disagree about a post."""
    base = config.base_url()
    kind = ev.get("kind")
    if kind not in POST_KINDS or _is_mirror(ev) or _protected(ev):
        return None, {}
    parent = parent_of(ev)
    if kind == 1111 and not parent:
        # A comment on an article (`a`) or a web page (`i`): nothing on the fediverse it could
        # reply to, and sent anyway it arrived as a standalone post with no context.
        return None, {}
    target = {}
    if parent:
        target = await resolve_event(parent)
        if not target:
            return None, {}                   # a reply in a Nostr-only thread stays on Nostr
    mentions = {}
    for pk in _referenced_pubkeys(ev):
        who = await resolve_pubkey(pk)
        if who:
            mentions[pk] = who
    links, quote = await _post_refs(ev)
    kw = dict(base=base, actor=me, followers=f"{me}/followers", mentions=mentions,
              in_reply_to=target.get("uri", ""), reply_to_actor=target.get("actor", ""), links=links, quote=quote)
    if kind == 1068:
        tally = await _tally_of(ev["id"])
        obj = convert.question_from_event(ev, counts=tally.get("counts") or {}, voters=int(tally.get("voters") or 0), **kw)
    else:
        obj = convert.note_from_event(ev, **kw)
    return obj, {"target": target, "mentions": mentions}


async def _tally_of(poll_id: str) -> dict:
    try:
        return await state.poll_tally(poll_id)
    except Exception:
        return {}


async def _follower_inboxes(member: str) -> list:
    gone = await _gone()
    return [f["inbox"] for f in await state.followers(member)
            if not _blocked_cached(f.get("actor", "")) and f.get("actor") not in gone]


async def plan(ev: dict, member: str) -> list:
    """[(inbox, activity)] to send for one event. Empty when there is nothing to federate."""
    base = config.base_url()
    if not base or not actors.handle(member) or _is_mirror(ev) or _protected(ev):
        return []
    if not actors.is_actor(member) and ev.get("kind") == 3:
        return []          # a non-local user's follows would bring posts only to THIS relay, which they do not read
    me = await actors.actor_id(member)
    if not me:
        return []
    followers_url = f"{me}/followers"
    kind = ev.get("kind")
    fol = await _follower_inboxes(member)
    out: list = []

    if kind in POST_KINDS:
        note, ctx = await build_object(ev, me)
        if note is None:
            return []
        target, mentions = ctx["target"], ctx["mentions"]
        reply_actor = target.get("actor", "")
        act = convert.create(note, me)
        targets = set(fol)
        extra = set()
        for who in list(mentions.values()) + ([{"href": reply_actor, "remote": target["remote"]}] if reply_actor else []):
            if who.get("remote") and who.get("href"):
                inbox = await _inbox_for(who["href"])
                if inbox:
                    extra.add(inbox)
        # ACCEPTED relays take public top-level posts in scope (relays.carries). Recorded with the
        # other extra inboxes, so the post's Delete reaches the relays too.
        from app.services.activitypub import relays
        if relays.carries(ev, member):
            extra |= set(await relays.accepted_inboxes())
        targets |= extra
        await _remember_sent(ev["id"], extra - set(fol), kind=kind, by=member)
        out = [(i, act) for i in sorted(targets)]

    elif kind == 1018:
        out = await _vote(ev, member, me)

    elif kind in (6, 7):
        target_id = next((t[1] for t in reversed(ev.get("tags") or []) if len(t) > 1 and t[0] == "e"), "")
        target = await resolve_event(target_id) if target_id else {}
        if not target:
            return []
        author_inbox = await _inbox_for(target["actor"]) if target.get("remote") and target.get("actor") else ""
        # Recorded EVEN WITH NO AUTHOR INBOX (a boost of a member's own post): what it was and what it
        # was about is how its deletion later goes out as the right verb, naming the right object.
        if kind == 6:
            act = convert.announce(ev, base=base, actor=me, followers=followers_url, target=target["uri"],
                                   target_actor=target.get("actor", ""))
            out = [(i, act) for i in sorted(set(fol) | ({author_inbox} if author_inbox else set()))]
            await _remember_sent(ev["id"], {author_inbox} - {""}, kind=6, by=member, target=target["uri"])
        elif (ev.get("content") or "").strip() == "-":
            return []                          # a NIP-25 dislike: the fediverse has no such thing
        else:
            act = convert.like(ev, base=base, actor=me, target=target["uri"])
            if author_inbox:
                out = [(author_inbox, act)]
            await _remember_sent(ev["id"], {author_inbox} - {""}, kind=7, by=member, target=target["uri"])

    elif kind == 5:
        kinds = {t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "k"}
        for t in ev.get("tags") or []:
            if len(t) < 2 or t[0] != "e":
                continue
            gone = t[1]
            # What it was is taken from our own record of sending it first: many clients send a
            # kind-5 with no `k` tag, and read from the tags alone deleting a reaction sent a Delete
            # of a Note (and never the Undo the liked author needed).
            rec = await _sent_rec(gone)
            # ONLY THE DELETER'S OWN. The relay refuses a kind-5 for somebody else's event, but this
            # would still have sent a Delete of that event SIGNED BY THE DELETER -- and Pleroma and
            # Akkoma accept a Delete from the same domain as the object, i.e. from any member here.
            if rec.get("by") and rec["by"] != member:
                continue
            if not rec.get("by"):
                still = await _event(gone)
                if still and still.get("pubkey") != member:
                    continue
            was = str(rec.get("kind") or "")
            if not was:
                was = "7" if ("7" in kinds and "1" not in kinds) else "6" if ("6" in kinds and "1" not in kinds) else "1"
            extra = set(rec.get("inboxes") or [])
            if was == "7":
                act = convert.undo(convert.activity_url(base, gone), "Like", base=base, actor=me,
                                   target=rec.get("target") or "", deletion_id=ev["id"])
                out += [(i, act) for i in sorted(extra)]
            elif was == "6":
                act = convert.undo(convert.activity_url(base, gone), "Announce", base=base, actor=me,
                                   target=rec.get("target") or "", deletion_id=ev["id"])
                out += [(i, act) for i in sorted(set(fol) | extra)]
            elif was == "1018":
                continue                       # a poll vote cannot be withdrawn on the fediverse
            else:
                act = convert.delete_note(gone, base=base, actor=me, followers=followers_url, deletion_id=ev["id"])
                out += [(i, act) for i in sorted(set(fol) | extra)]

    elif kind == 0:
        doc = await actors.person(member)
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
    moved = await state.moves()
    resolved = {}
    # Concurrently: a contact list of a few hundred fediverse accounts is a few hundred actor fetches,
    # and one after another that outlived every time limit it ran under.
    sem = asyncio.Semaphore(12)

    async def one(alias):
        async with sem:
            canonical, inbox = await _canonical(alias)
            # An account that MOVED is followed at its new address: the contact list still names the
            # old one's puppet, and cannot be rewritten here (the member signs it).
            new = moved.get(canonical) if canonical else ""
            if new and new != canonical:
                canonical, inbox = await _canonical(new)
            return alias, canonical, inbox
    unresolved_hosts = set()
    for alias, canonical, inbox in await asyncio.gather(*(one(a) for a in sorted(wanted))):
        if canonical and inbox:
            resolved[canonical] = inbox
        else:
            unresolved_hosts.add(remote.host_of(alias))
    # KEEP WHAT CANNOT BE RE-CHECKED RIGHT NOW. By host, not by address: a puppet from the old bridge
    # is keyed on a profile URL (/@bob) while the follow is recorded under the canonical id
    # (/users/bob), so comparing addresses never matched and a brief outage on bob's server made an
    # unrelated contact-list edit unfollow him.
    unresolved_known = {a for a in current if remote.host_of(a) in unresolved_hosts}
    out = []
    for actor_uri in sorted(set(resolved) - set(current)):
        inbox = resolved[actor_uri]
        # A NEW id every time: Pleroma and Akkoma drop an activity whose id they already hold, so a
        # follow → unfollow → follow again reused the first Follow's id and the refollow was ignored
        # (for ever "pending"). The hash of the target stays first -- _accept maps an Accept back by it.
        fid = f"{me}#follows/{_h(actor_uri)}/{int(time.time() * 1000)}"
        follow = {"@context": convert.AS_CONTEXT, "id": fid, "type": "Follow", "actor": me, "object": actor_uri}
        await state.set_following(member, actor_uri, inbox, "pending", follow_id=fid)
        out.append((inbox, follow))
    for actor_uri in sorted(set(current) - set(resolved) - unresolved_known):
        inbox = current[actor_uri].get("inbox") or await _inbox_for(actor_uri)
        await state.drop_following(member, actor_uri)
        if inbox:
            fid = current[actor_uri].get("id") or f"{me}#follows/{_h(actor_uri)}"   # older records: the old id
            out.append((inbox, {"@context": convert.AS_CONTEXT, "id": f"{fid}/undo/{int(time.time() * 1000)}",
                                "type": "Undo", "actor": me,
                                "object": {"id": fid, "type": "Follow", "actor": me, "object": actor_uri}}))
    if out:
        state.forget_followed_cache()
    return out


async def _vote(ev: dict, member: str, me: str) -> list:
    """A member's NIP-88 vote (kind 1018) on a FEDIVERSE poll → one answer Note per chosen option, to
    the poll's author (the only server that counts it). A vote on a poll that lives on Nostr is
    tallied here and reaches the fediverse as the poll's Update(Question) instead."""
    poll_id = next((t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e" and len(t[1]) == 64), "")
    row = _mirror_row(poll_id) if poll_id else None
    if not row or not row.note_uri:
        return []
    poll = await _event(poll_id)
    if not poll or poll.get("kind") != 1068:
        return []
    labels = dict(convert.poll_options(poll))
    chosen = [t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "response" and t[1] in labels]
    chosen = list(dict.fromkeys(chosen))
    if not chosen:
        return []
    if not convert.poll_multi(poll):
        chosen = chosen[:1]
    prow = _puppet_row(row.nostr_pubkey or "")
    author, inbox = await _canonical(prow.actor_uri) if prow and prow.actor_uri else ("", "")
    if not author or not inbox or config.host_blocked(remote.host_of(author)):
        return []
    base = config.base_url()
    out = []
    for i, oid in enumerate(chosen):
        note = convert.vote_note(vote_id=f"{convert.activity_url(base, ev['id'])}/vote/{i}", actor=me,
                                 question=row.note_uri, question_actor=author, label=labels[oid])
        act = convert.create(note, me)
        act["to"], act["cc"] = [author], []
        out.append((inbox, act))
    await _remember_sent(ev["id"], inbox, kind=1018, by=member, target=row.note_uri)
    return out


async def public_posts(member: str, *, until: int = 0, after: str = "", limit: int = 20) -> tuple[list, int, str]:
    """(Create activities, oldest created_at) for a member's recent public posts, newest first -- what
    their OUTBOX collection serves. A remote server fills a profile from it: Akkoma and Mastodon read
    it when somebody opens the profile, and with it empty they showed only whatever had happened to
    be delivered to them (one post, or none). The same conversion and the same rules as delivery:
    no mirrors, no protected events, and a reply only when it answers something on the fediverse."""
    base = config.base_url()
    me = await actors.actor_id(member) if base else ""
    if not me:
        return [], 0, ""
    flt = {"kinds": [1, 1068], "authors": [member], "limit": max(limit * 3, 30)}
    if until:
        # INCLUSIVE, with the id of the last one served as the tie-break: `until - 1` skipped every
        # post published in the same second as the last one on the previous page.
        flt["until"] = int(until)
    evs = await nostr_store._ws_query(settings_store._port(), [flt], strict=True)
    evs.sort(key=lambda e: (-int(e.get("created_at") or 0), e.get("id", "")))
    items, oldest, last_id = [], 0, ""
    for ev in evs:
        ts = int(ev.get("created_at") or 0)
        if until and ts == int(until) and after and ev.get("id", "") <= after:
            continue
        oldest, last_id = ts, ev.get("id", "")
        note, _ctx = await build_object(ev, me)
        if note is None:
            continue
        items.append(convert.create(note, me))
        if len(items) >= limit:
            break
    return items, oldest, last_id


def counts_as_public(ev: dict) -> bool:
    """Whether an event is one of the posts an outbox serves (a label's count, so replies are
    included without resolving their parents)."""
    return not _is_mirror(ev) and not _protected(ev)


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


async def _remember_sent(event_id: str, inbox, kind: int = 7, *, by: str = "", target: str = "") -> None:
    """Where an event went BEYOND the followers (the replied-to author, the mentioned, the boosted or
    liked author), what it was, WHO sent it and what it was about -- so its deletion reaches the same
    servers, as the right verb, naming the right object, even when the kind-5 carries no `k` tag and
    the relay has already dropped the original; and so only its author can delete it."""
    inboxes = [inbox] if isinstance(inbox, str) else sorted(set(inbox))
    inboxes = [i for i in inboxes if i]
    if not inboxes and kind not in (6, 7, 1018):
        return                        # a post to followers only: nothing beyond them to remember
    doc = {"inbox": inboxes[0] if inboxes else "", "inboxes": inboxes[:200], "kind": kind}
    if by:
        doc["by"] = by
    if target:
        doc["target"] = target
    try:
        await nostr_store.put_doc(settings_store._port(), settings_store._operator_seckey(None), _SENT + event_id, doc)
    except Exception:
        pass


async def _sent_rec(event_id: str) -> dict:
    try:
        doc = await nostr_store.get_doc(settings_store._port(), _SENT + event_id,
                                        seckey=settings_store._operator_seckey(None))
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    inboxes = doc.get("inboxes") or ([doc["inbox"]] if doc.get("inbox") else [])
    return {"kind": doc.get("kind"), "inboxes": [i for i in inboxes if isinstance(i, str) and i],
            "by": str(doc.get("by") or ""), "target": str(doc.get("target") or "")}


# ------------------------------------------------------------------------------------ delivery

MAX_RETRIES_QUEUED = 5000


_down: dict = {}                 # host -> (consecutive failures, resting until)
_DOWN_AFTER = 5


def _host_resting(host: str) -> float:
    """Seconds a server that keeps failing is left alone for (0 when it is not)."""
    n, until = _down.get(host, (0, 0.0))
    return max(0.0, until - time.time())


def _note_result(host: str, ok: bool) -> None:
    """A per-server circuit breaker. A dead instance used to cost a 12-second timeout on EVERY post
    to EVERY one of its followers; after a run of failures it rests (15 minutes, doubling to 4 hours)
    and what was meant for it waits in the retry queue instead."""
    if ok:
        _down.pop(host, None)
        return
    n = _down.get(host, (0, 0.0))[0] + 1
    until = time.time() + min(900 * 2 ** max(0, n - _DOWN_AFTER), 4 * 3600) if n >= _DOWN_AFTER else 0.0
    _down[host] = (n, until)
    if len(_down) > 20000:
        for k in sorted(_down, key=lambda k: _down[k][1])[:4000]:
            _down.pop(k, None)


def _retry_key(inbox: str, activity: dict) -> str:
    return hashlib.sha256(f"{inbox}\n{activity.get('id', '')}".encode()).hexdigest()[:32]


async def _queue_retry(due: float, inbox: str, activity: dict, member: str, attempt: int) -> None:
    """Queue a delivery for later -- in memory AND on the relay, so a worker restart (every deploy)
    does not silently drop what a resting server is owed."""
    key = _retry_key(inbox, activity)
    if any(r[5] == key for r in _retries):
        return
    if len(_retries) >= MAX_RETRIES_QUEUED:
        old = _retries.pop(0)                    # the oldest is the least likely to still matter
        await _forget_retry(old[5])
    _retries.append((due, inbox, activity, member, attempt, key))
    try:
        await state.save_retry(key, {"due": due, "inbox": inbox, "activity": activity, "member": member,
                                     "attempt": attempt})
    except Exception:
        pass                                     # memory still has it; only a restart would lose it


async def _forget_retry(key: str) -> None:
    if key:
        try:
            await state.drop_retry(key)
        except Exception:
            pass


_retries_loaded = False


async def _load_retries() -> None:
    """Once per process: pick up the deliveries a previous worker left queued."""
    global _retries_loaded
    if _retries_loaded:
        return
    try:
        saved = await state.load_retries()
    except Exception:
        return                                   # try again next tick; never assume "none"
    _retries_loaded = True
    have = {r[5] for r in _retries}
    for key, e in sorted(saved.items(), key=lambda kv: float(kv[1].get("due") or 0))[:MAX_RETRIES_QUEUED]:
        if key not in have and isinstance(e.get("activity"), dict):
            _retries.append((float(e.get("due") or 0), str(e["inbox"]), e["activity"], str(e.get("member") or ""),
                             int(e.get("attempt") or 1), key))


async def _send(inbox: str, activity: dict, member: str, attempt: int = 0, key: str = "") -> None:
    host = remote.host_of(inbox)
    resting = _host_resting(host)
    if resting:
        if attempt < len(_RETRY_DELAYS):
            await _queue_retry(time.time() + max(resting, _RETRY_DELAYS[attempt]), inbox, activity, member, attempt + 1)
        _stats["failed"] += 1
        return
    keys = await state.keypair(member)               # driven by an event on this relay: never limited
    key_id, priv = await actors.signing(member, keys)
    status = await remote.deliver(inbox, activity, key_id=key_id, private_pem=priv)
    _note_result(host, 200 <= status < 500 and status not in (408, 429))
    if 200 <= status < 300:
        _stats["delivered"] += 1
        return
    _stats["failed"] += 1
    _stats["last_error"] = f"HTTP {status} from {remote.host_of(inbox)}"
    # 4xx other than 408/429 is an answer, not an outage: retrying it only makes us the storm.
    if (status == 0 or status >= 500 or status in (408, 429)) and attempt < len(_RETRY_DELAYS):
        await _queue_retry(time.time() + _RETRY_DELAYS[attempt], inbox, activity, member, attempt + 1)
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
    await _load_retries()
    now = time.time()
    due = [r for r in _retries if r[0] <= now][:limit]
    for r in due:
        _retries.remove(r)
    sem = asyncio.Semaphore(8)

    async def one(r):
        async with sem:
            await _send(r[1], r[2], r[3], r[4])
            if not any(x[5] == r[5] for x in _retries):
                await _forget_retry(r[5])        # sent or given up (a re-queue keeps its document)
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
    try:
        from app.services.activitypub import relays
        await relays.reconcile()
    except Exception as e:
        logger.info("[activitypub] relay subscriptions not reconciled: %s: %s", type(e).__name__, e)
    handled = await _pass(state.CURSOR, await _members(), KINDS, None)
    if config.everyone():
        handled += await _pass(state.CURSOR_EVERYONE, None, _EVERYONE_KINDS, await _everyone_filter())
    return handled


# ------------------------------------------------------------------------------ poll tallies

_POLL_WINDOW = 30 * 86400        # a poll older than this is left as it was last counted
_POLL_GRACE = 86400              # counted once more for a day after it closes (late votes, "closed")


def count_votes(poll: dict, votes: list) -> tuple[dict, int]:
    """({option id: votes}, voters) -- NIP-88's rule: each pubkey's LATEST response counts, only for
    options the poll has, and only its first option on a single-choice poll."""
    opts = {oid for oid, _ in convert.poll_options(poll)}
    end = convert.poll_end(poll)
    latest: dict = {}
    for v in sorted(votes, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        if end and int(v.get("created_at") or 0) > end:
            continue
        latest[v.get("pubkey")] = v
    counts: dict = {}
    voters = 0
    multi = convert.poll_multi(poll)
    for v in latest.values():
        chosen = list(dict.fromkeys(t[1] for t in v.get("tags") or [] if len(t) > 1 and t[0] == "response" and t[1] in opts))
        if not multi:
            chosen = chosen[:1]
        if chosen:
            voters += 1
        for oid in chosen:
            counts[oid] = counts.get(oid, 0) + 1
    return counts, voters


async def poll_updates(members: list = None) -> int:
    """Send Update(Question) for each member poll whose count changed (or that just closed) -- to its
    followers and to every fediverse server that voted in it. Votes come from both sides: a Nostr
    user's kind-1018 and a fediverse answer (stored as a 1018 by inbox.py) are counted the same way."""
    if not config.enabled() or not config.base_url() or not settings_store.is_hydrated():
        return 0
    members = await _members() if members is None else members
    if not members:
        return 0
    now = int(time.time())
    polls = await nostr_store._ws_query(settings_store._port(), [{"kinds": [1068], "authors": members,
                                                                  "since": now - _POLL_WINDOW, "limit": 200}], strict=True)
    polls = [p for p in polls if not _is_mirror(p) and not _protected(p)
             and not (convert.poll_end(p) and convert.poll_end(p) < now - _POLL_GRACE)]
    if not polls:
        return 0
    votes = await nostr_store._ws_query(settings_store._port(), [{"kinds": [1018], "#e": [p["id"] for p in polls],
                                                                  "limit": 10000}], strict=True)
    by_poll: dict = {}
    for v in votes:
        pid = next((t[1] for t in v.get("tags") or [] if len(t) > 1 and t[0] == "e"), "")
        by_poll.setdefault(pid, []).append(v)
    sent = 0
    for poll in polls:
        counts, voters = count_votes(poll, by_poll.get(poll["id"], []))
        end = convert.poll_end(poll)
        closed = bool(end and end <= now)
        try:
            last = await state.poll_tally(poll["id"])
        except Exception:
            continue                                   # unreadable: decide nothing
        if last.get("counts") == counts and int(last.get("voters") or 0) == voters \
                and bool(last.get("closed")) == closed:
            continue
        if not last and not counts and not closed:
            continue                                   # nothing to say yet
        member = poll["pubkey"]
        me = await actors.actor_id(member)
        if not me:
            continue
        await state.set_poll_tally(poll["id"], {"counts": counts, "voters": voters, "closed": closed})
        note, _ctx = await build_object(poll, me)
        if note is None:
            continue
        act = {"@context": convert.AS_CONTEXT,
               "id": f"{convert.activity_url(config.base_url(), poll['id'], 'update')}/{now}",
               "type": "Update", "actor": me, "to": note.get("to"), "cc": note.get("cc"), "object": note}
        inboxes = set(await _follower_inboxes(member)) | set(await state.poll_voter_inboxes(poll["id"]))
        sem = asyncio.Semaphore(8)

        async def one(inbox):
            async with sem:
                await _send(inbox, act, member)
        await asyncio.gather(*(one(i) for i in sorted(inboxes)))
        sent += 1
    return sent


# ------------------------------------------------------------------------------ follow catch-up

_K3 = "pcai:ap:k3:"            # per member: created_at of the contact list last turned into Follows
_CATCHUP_PER_RUN = 3
_caught_up: set = set()        # members settled in this process -- newer lists are `_pass`'s job


async def catch_up_follows(limit: int = _CATCHUP_PER_RUN) -> int:
    """Follow the fediverse accounts already in each member's contact list.

    The delivery pass only ever sees contact lists published AFTER its cursor, and its first run
    sets the cursor to "now" -- so a list written before the fediverse server was switched on is
    never an event it handles, and a member who already followed three hundred fediverse accounts
    (through the Pleroma bridge, say) had a fediverse account that followed nobody. With the bridge
    then switched off, nothing of theirs arrived from anywhere, and nothing said so.

    `_follows` works from the WHOLE list against what is recorded as followed, so running it once on
    the current list is exactly the missing step, and running it again is harmless. The marker is
    the list's created_at: a member is revisited only when a newer list exists that nothing has
    handled -- the delivery pass hands every new contact list here. Its own job, outside the tick's timeout, because one
    member can be hundreds of actor fetches and a cancelled run would leave Follows recorded as
    asked-for that were never sent."""
    if not config.enabled() or not config.base_url() or not settings_store.is_hydrated():
        return 0
    done = 0
    for member in await _members():
        if done >= limit:
            break
        if member in _caught_up:
            continue
        try:
            mark = await state.cursor(_K3 + member)
            evs = await nostr_store._ws_query(settings_store._port(),
                                              [{"kinds": [3], "authors": [member], "limit": 1}], strict=True)
        except Exception:
            continue                                   # unreadable: ask again next run, decide nothing
        latest = max(evs, key=lambda e: e.get("created_at", 0)) if evs else None
        at = int(latest.get("created_at", 0)) if latest else 1
        if mark >= at:
            _caught_up.add(member)
            continue
        if latest is not None:
            done += 1
            try:
                jobs = await plan(latest, member)
            except Exception as e:
                logger.info("[activitypub] follow catch-up for %s failed: %s: %s", member[:12], type(e).__name__, e)
                continue
            sem = asyncio.Semaphore(8)

            async def one(inbox, act):
                async with sem:
                    await _send(inbox, act, member)
            await asyncio.gather(*(one(i, a) for i, a in jobs))
            if jobs:
                logger.info("[activitypub] follow catch-up: %s sent %d follow change(s)", member[:12], len(jobs))
        await state.set_cursor(at, _K3 + member)
        _caught_up.add(member)
    return done


_EVERYONE_KINDS = [0, 1, 1111, 1068, 1018, 5, 6, 7]


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
    from app.services.activitypub import relays
    to_relays = relays.scope() == "everyone" and bool(await relays.accepted_inboxes())

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
            elif to_relays and ev.get("kind") in (1, 1068) and not parent_of(ev):
                keep = True                    # relay scope "everyone": every public top-level post
            elif ev.get("kind") == 0:
                keep = False
            elif ev.get("kind") == 5:
                # A deletion goes out when what it deletes did: a reply we sent must not outlive
                # its author's delete on the fediverse just because they have no followers there.
                # By OUR RECORD of sending it first -- the relay removes the author's own events as it
                # stores the kind-5, so by now the deleted event itself is almost never there to judge.
                keep = any(_direct(gone[t[1]], mirrored) for t in ev.get("tags") or []
                           if len(t) > 1 and t[0] == "e" and t[1] in gone)
                if not keep:
                    for t in [t for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e"][:10]:
                        rec = await _sent_rec(t[1])
                        if rec.get("kind") and rec.get("by") in ("", pk):
                            keep = True
                            break
            else:
                keep = _direct(ev, mirrored)
            if keep and await actors.exposed(pk):
                ok.add(ev["id"])
        return ok
    return page


def _protected(ev: dict) -> bool:
    """NIP-70: the author asked that this event not be republished by anybody else -- or the app marked
    it `nofederate` (its own private traffic)."""
    return any(t == ["-"] or (t and t[0] == "nofederate") for t in ev.get("tags") or [])


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


# THE CURSOR IS A `created_at`, AND `created_at` IS WHAT THE AUTHOR SAYS, NOT WHEN IT ARRIVED. An
# event signed offline and replayed later, one that reaches this relay late through the firehose, or
# anything behind an event from a fast clock (the relay accepts up to 15 minutes ahead) was older than
# the cursor by the time it was here -- and never federated. So every pass re-reads a trailing window
# behind the cursor, the cursor never runs ahead of the wall clock, and what was already SENT inside
# the window is remembered on the relay (`done`), so a worker restart does not send it twice.
_OVERLAP = {state.CURSOR: 6 * 3600, state.CURSOR_EVERYONE: 1800}
_DONE_MAX = 1500
_done: dict = {}                 # cursor key -> {event id[:16]: created_at} sent inside the window


async def _cursor_doc(cursor_key: str) -> tuple[int, dict]:
    doc = await state._get(cursor_key)
    doc = doc if isinstance(doc, dict) else {}
    done = doc.get("done") if isinstance(doc.get("done"), dict) else {}
    return int(doc.get("since") or 0), {str(k): int(v) for k, v in done.items() if isinstance(v, (int, float))}


async def _save_cursor(cursor_key: str, since: int, done: dict) -> None:
    keep = sorted(done.items(), key=lambda kv: -kv[1])[:_DONE_MAX]
    await state._put(cursor_key, {"since": int(since), "done": dict(keep)})


async def _pass(cursor_key: str, authors, kinds: list, qualifies) -> int:
    """Read since `cursor_key` (minus the overlap window), translate, deliver, advance. `authors` None
    = every author (the everyone pass), narrowed by `qualifies`."""
    try:
        since, saved_done = await _cursor_doc(cursor_key)
    except Exception as e:
        _stats["last_error"] = f"cursor unreadable: {type(e).__name__}"
        return 0                                   # never deliver from a guessed position
    now = int(time.time())
    if not since:
        await _save_cursor(cursor_key, now, {})    # first run: no backfill of everything ever posted
        return 0
    if authors is not None and not authors:
        await _save_cursor(cursor_key, min(max(since, now - 60), now), saved_done)
        return 0
    overlap = _OVERLAP.get(cursor_key, 900)
    done = _done.setdefault(cursor_key, {})
    for k, v in saved_done.items():
        done.setdefault(k, v)
    evs = await _since(authors, max(1, min(since, now) - overlap), kinds)
    evs = sorted((e for e in evs if e.get("id") not in _seen and e.get("id", "")[:16] not in done),
                 key=lambda e: (e.get("created_at", 0), e["id"]))
    wanted = await qualifies(evs) if qualifies is not None else None
    handled, newest, sent_any = 0, min(since, now), False
    sem = asyncio.Semaphore(8)

    async def one(inbox, act, member):
        async with sem:
            await _send(inbox, act, member)

    for ev in evs:
        if wanted is not None and ev["id"] not in wanted:
            _seen[ev["id"]] = time.time()
            newest = max(newest, int(ev.get("created_at", 0)))
            continue
        if ev.get("kind") == 3 and authors is not None:
            # A member's contact list is the catch-up's job, never the tick's. Turning a list of a few
            # hundred fediverse accounts into Follows is a few hundred actor fetches; inside the tick's
            # time limit it timed out, the cursor never moved, and every post, reply and like queued
            # behind it waited too ("I didn't see anything on DRC"). The catch-up has no limit and runs
            # every minute; it sees this list as newer than its marker and handles it there.
            _caught_up.discard(ev["pubkey"])
            _seen[ev["id"]] = time.time()
            newest = max(newest, int(ev.get("created_at", 0)))
            handled += 1
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
            done[ev["id"][:16]] = int(ev.get("created_at", 0))
            sent_any = True
        _failures.pop(ev["id"], None)
        _seen[ev["id"]] = time.time()
        newest = max(newest, int(ev.get("created_at", 0)))
        handled += 1
    if len(_seen) > 40000:
        for k in sorted(_seen, key=_seen.get)[:20000]:
            _seen.pop(k, None)
    # Never ahead of the clock: one future-dated event must not move the window past what is still
    # to arrive. Inclusive of the newest second handled (more may share it; `_seen` stops a replay).
    newest = min(newest, now)
    floor = min(newest, now) - overlap
    for k in [k for k, v in done.items() if v < floor]:
        done.pop(k, None)
    if newest != since or sent_any:
        await _save_cursor(cursor_key, newest, done)
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

    async def _catchup():
        try:
            await catch_up_follows()
        except Exception as e:
            logger.info("[activitypub] follow catch-up failed: %s: %s", type(e).__name__, e)

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=_TICK_SECONDS, id="activitypub_delivery",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_catchup, "interval", seconds=60, id="activitypub_follow_catchup",
                       max_instances=1, coalesce=True)

    async def _polls():
        try:
            await asyncio.wait_for(poll_updates(), timeout=240)
        except Exception as e:
            logger.info("[activitypub] poll tallies failed: %s: %s", type(e).__name__, e)
    _scheduler.add_job(_polls, "interval", seconds=300, id="activitypub_poll_tallies",
                       max_instances=1, coalesce=True)

    def _prune():
        from app.services.activitypub import ledger
        n = ledger.prune()
        if n:
            logger.info("[activitypub] pruned %d delivered-note record(s) past retention", n)
    _scheduler.add_job(_prune, "interval", hours=24, id="activitypub_ledger_prune",
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


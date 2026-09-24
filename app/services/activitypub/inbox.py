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


def schedule(activity: dict, signer: str) -> bool:
    """Process an activity after the 202 has gone back -- remote servers time out slow inboxes and
    then retry, which is how a slow inbox gets its own traffic twice. BOUNDED: each one can make
    outbound fetches, so past MAX_PENDING the caller answers 503 and the sender retries later,
    instead of this process holding an unlimited pile of work for whoever sends fastest."""
    if len(_tasks) >= MAX_PENDING:
        return False
    t = asyncio.create_task(_run(activity, signer))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
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
    user = str(doc.get("preferredUsername") or "").strip()
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


async def process(activity: dict, signer: str) -> str:
    """Handle one verified activity; returns what was done (for logs and tests)."""
    if await blocked_actor(signer):
        return "ignored: blocked account"
    kind = activity.get("type")
    obj = activity.get("object")
    if kind == "Follow":
        return await _follow(activity, signer)
    if kind == "Undo":
        return await _undo(obj if isinstance(obj, dict) else {"id": convert.id_of(obj)}, signer)
    if kind in ("Accept", "Reject"):
        return await _accept(kind, obj, signer)
    if kind == "Create":
        note = await authentic(obj, signer)
        if note.get("type") not in ("Note", "Article", "Question", "Page"):
            return "ignored: not a note"
        # The CREATOR is the author -- not merely somebody on the same server, or any account on an
        # instance could post as its neighbours.
        if convert.id_of(note.get("attributedTo")) != signer:
            return "ignored: posted for somebody else"
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
        return await _delete(convert.id_of(obj), signer)
    if kind == "Update" and isinstance(obj, dict) and obj.get("type") in ("Person", "Service", "Group"):
        if convert.id_of(obj) == signer:
            await _puppet(await remote.actor(signer, refresh=True))
            return "profile refreshed"
    return f"ignored: {kind}"


# ------------------------------------------------------------------------------------ follows

async def _follow(activity: dict, signer: str) -> str:
    name = _our_member_name(convert.id_of(activity.get("object")))
    member = await actors.member_by_name(name) if name else ""
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
    await state.add_follower(member, signer, inbox)
    keys = await state.keypair(member)              # a verified Follow is behind it
    key_id, priv = actors.signing(member, keys)
    me = convert.actor_url(config.base_url(), actors.handle(member))
    accept = {"@context": convert.AS_CONTEXT, "id": f"{me}#accepts/{int(time.time() * 1000)}",
              "type": "Accept", "actor": me, "object": {k: activity[k] for k in ("id", "type", "actor", "object")
                                                        if k in activity}}
    personal = who.get("inbox") if remote.host_of(who.get("inbox") or "") == remote.host_of(signer) else ""
    status = await remote.deliver(personal or inbox, accept, key_id=key_id, private_pem=priv)
    return f"follower added (accept HTTP {status})"


async def _accept(kind: str, obj, signer: str) -> str:
    """The answer to a Follow WE sent -- and only to one we sent. An unsolicited Accept would
    otherwise make any account 'followed', which is the gate that lets its posts into the relay.

    The Follow may come back embedded or as just its id (`<our actor>#follows/<hash of target>`);
    both are mapped back to the member and the account they asked to follow."""
    follow_id = convert.id_of(obj)
    follow = obj if isinstance(obj, dict) else {}
    me = convert.id_of(follow.get("actor")) or follow_id.split("#follows/")[0]
    name = _our_member_name(me)
    member = await actors.member_by_name(name) if name else ""
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
            await state.set_following(member, signer, current.get("inbox") or "", "accepted")
    else:
        await state.drop_following(member, signer)
    state.forget_followed_cache()
    return f"follow {kind.lower()}ed"


async def _undo(inner: dict, signer: str) -> str:
    itype = inner.get("type")
    if itype == "Follow" or _our_member_name(convert.id_of(inner.get("object"))):
        member = await actors.member_by_name(_our_member_name(convert.id_of(inner.get("object"))))
        if member:
            await state.remove_follower(member, signer)
            return "follower removed"
    return await _delete(convert.id_of(inner), signer, undo=True)


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
    row = _delivered(uri)
    return (row.nostr_event_id, row.nostr_pubkey or "") if row else ("", "")


async def store_note(note: dict, author: str, *, need_gate: bool) -> str:
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
    if _delivered(uri):
        return "already stored"
    local = actors.local_actor_map()
    text, tags = convert.note_content(note, local_actors=local)
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
    if need_gate:
        mentions_ours = any(t[0] == "p" for t in tags)
        replies_to_ours = bool(parent_pk) and (actors.is_actor(parent_pk) or await actors.exposed(parent_pk))
        followed = author in await state.followed_actors()
        if not (followed or mentions_ours or replies_to_ours):
            return "ignored: nobody here follows or was addressed"
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
        tags = [["e", parent_id, "", "root"], ["e", parent_id, "", "reply"]] + \
               ([["p", parent_pk]] if parent_pk else []) + [t for t in tags if t != ["p", parent_pk]]
    emap = _emoji_url_map([{"shortcode": str(t.get("name") or "").strip(":"),
                            "url": convert.id_of((t.get("icon") or {}).get("url")) or (t.get("icon") or {}).get("url")}
                           for t in note.get("tag") or [] if isinstance(t, dict) and t.get("type") == "Emoji"])
    tags += emoji_tags_for(text, emap)
    ts = convert.parse_time(note.get("published")) or int(time.time())
    ev = build_event(puppet, 1, text, tags=tags, object_uri=uri, broadcast=config.broadcast(),
                     created_at=min(ts, int(time.time())))
    ok, msg = await publish(_port(), ev)
    if not ok:
        return f"relay refused: {msg}"
    _record(uri, ev["id"], ev["pubkey"], puppet.get("acct", ""))
    await _reach_nostr_users(ev, puppet)
    return "stored"


async def _link_mentions(note: dict, text: str, author_host: str, *, is_reply: bool) -> tuple[str, list]:
    """Make the note's @mentions CLICKABLE: each `@name` / `@name@host` in the text becomes a
    `nostr:npub…` reference (which every client renders as a profile link) and the person is p-tagged.

    A fediverse note carries its mentions as plain text plus a `Mention` tag list. Stored as-is, the
    names were dead text and only OUR members were tagged -- "usernames not clickable in fediverse
    posts". A mention of one of our users (or an npub account here) links THEIR key; anybody else gets
    the same treatment the Pleroma bridge has always given a mirrored post (`_rewrite_mentions`: the
    account's puppet, the blocklist, the bare-vs-qualified handle rules), so the two paths render a
    mention identically. A reply's leading run of @-recipients (which fediverse clients hide) is
    dropped, as the bridge does."""
    import re as _re
    from app.database import SessionLocal
    from app.services import fedi_nostr_bridge_service as bridge
    from app.services.nostr import bech32
    ours, theirs = [], []
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
        elif not config.is_own_host(remote.host_of(href)):
            theirs.append({"url": href, "acct": f"{user}@{host}", "username": user})
    tags = []
    for user, host, pk in sorted(ours, key=lambda x: -len(x[0])):
        ref = "nostr:" + bech32.encode("npub", bytes.fromhex(pk))
        text = _re.sub(r"@" + _re.escape(f"{user}@{host}") + r"(?![A-Za-z0-9_.\-@])", ref, text, flags=_re.I)
        text = _re.sub(r"(?<![\w@/])@" + _re.escape(user) + r"(?![A-Za-z0-9_.\-@])", ref, text)
        tags.append(["p", pk])
    if theirs:
        db = SessionLocal()
        try:
            text, ptags = await bridge._rewrite_mentions(db, _port(), author_host, text, theirs,
                                                         bridge._blocked_domains())
        except Exception as e:
            logger.info("[activitypub] mentions left as text: %s: %s", type(e).__name__, e)
            ptags = []
        finally:
            db.close()
        tags += [t for t in ptags if t not in tags]
    if is_reply:
        stripped = bridge._LEADING_MENTIONS_RE.sub("", text).strip()
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


async def _announce(activity: dict, signer: str) -> str:
    from app.services.fedi_bridge_identity import build_event, publish
    act_id = convert.id_of(activity)
    if not _own_id(act_id, signer):
        return "ignored: activity id is not the sender's"
    if not convert.is_public(activity):
        return "ignored: not public"
    if _delivered(act_id):
        return "already stored"
    target_uri = convert.id_of(activity.get("object"))
    eid, pk = await _target(target_uri)
    if not eid:
        if signer not in await state.followed_actors():
            return "ignored: boost of something nobody here asked for"
        note = await authentic(activity.get("object"), signer)   # from its own server, as it really is
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
    _record(act_id, ev["id"], ev["pubkey"], puppet.get("acct", ""))
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
    if _delivered(act_id):
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
                url = convert.id_of((t.get("icon") or {}).get("url")) or (t.get("icon") or {}).get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    tags.append(["emoji", sc, url])
                break
    ev = build_event(puppet, 7, content, tags=tags, object_uri=act_id, broadcast=config.broadcast())
    ok, msg = await publish(_port(), ev)
    if not ok:
        return f"relay refused: {msg}"
    _record(act_id, ev["id"], ev["pubkey"], puppet.get("acct", ""))
    await _reach_nostr_users(ev, puppet)
    return "like stored"


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
    row = _delivered(uri)
    if not row:
        return "ignored: nothing stored for that"
    # By IDENTITY, not by URI spelling: the bridge may have given this person their puppet under the
    # other form of their address (/@alice vs /users/alice), and then the stored actor differs from
    # the signer while the person is the same. The puppet's key is what cannot be spelled two ways.
    author = _actor_of_puppet(row.nostr_pubkey or "")
    if not author:
        return "ignored: deletion by somebody other than the author"
    if author != signer:
        try:
            signer_puppet = await _puppet(await remote.actor(signer))
        except remote.FetchError:
            signer_puppet = None
        if not signer_puppet or signer_puppet.get("pubkey_hex") != row.nostr_pubkey:
            return "ignored: deletion by somebody other than the author"
    ok = await delete_note(_port(), author, row.nostr_event_id, broadcast=config.broadcast())
    return ("undone" if undo else "deleted") if ok else "relay refused the deletion"

"""ActivityPub state that is not content: signing keys, followers, follows, the delivery cursor.

All of it is operator-signed, NIP-44-encrypted kind-30078 documents on this node's own relay (the
same store as settings and Media Center), ONE DOCUMENT PER ITEM:

    pcai:ap:key:<member pubkey | "instance">          {"priv", "pub"}         RSA, PEM
    pcai:ap:follower:<member pubkey>:<sha(actor)>     {"actor", "inbox", "gone"?}
    pcai:ap:following:<member pubkey>:<sha(actor)>    {"actor", "inbox", "state", "gone"?}
    pcai:ap:cursor                                    {"since"}

Per item, not per member: NIP-44 caps a document at 64 KB, which one popular account's follower
list would pass, and a list is a read-modify-write that two concurrent Follows would race.

A KEY IS NEVER MINTED ON THE STRENGTH OF A FAILED READ. Reads are strict (an unreachable relay
raises instead of answering "no such document"), because a member whose key was silently replaced
has every existing follow on the fediverse stop verifying -- quietly, on every instance at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

from app.services import nostr_store, settings_store
from app.services.activitypub import httpsig

_KEY_PREFIX = "pcai:ap:key:"
_FOLLOWER_PREFIX = "pcai:ap:follower:"
_FOLLOWING_PREFIX = "pcai:ap:following:"
_CURSOR = "pcai:ap:cursor"

_key_cache: dict = {}
_key_lock = asyncio.Lock()


def _port() -> int:
    return settings_store._port()


def _seckey():
    return settings_store._operator_seckey(None)


def _h(actor: str) -> str:
    return hashlib.sha256((actor or "").encode()).hexdigest()[:24]


async def _get(d_tag: str):
    return await nostr_store.get_doc(_port(), d_tag, seckey=_seckey(), strict=True)


async def _put(d_tag: str, value) -> None:
    if not await nostr_store.put_doc(_port(), _seckey(), d_tag, value):
        raise RuntimeError(f"could not save {d_tag}")


# ------------------------------------------------------------------------------------ keys

_MINTS_PER_MINUTE = 30
_mints: list = []


class MintLimited(Exception):
    """Too many new keys for accounts that are not local users in the last minute."""


async def keypair(owner: str, *, local: bool = True) -> dict:
    """{"priv", "pub"} for a pubkey (or "instance"), created once and then only ever read.

    `local=False` is ONLY the unauthenticated actor fetch of a non-local account: minting an RSA key
    costs real CPU and that request costs a stranger nothing, so it is rate-limited. Everything else
    that mints -- a verified Follow, a delivery driven by an event on this relay -- is paid for by
    the caller already and is never refused, so a flood of free GETs cannot starve real traffic."""
    hit = _key_cache.get(owner)
    if hit:
        return hit
    async with _key_lock:
        hit = _key_cache.get(owner)
        if hit:
            return hit
        doc = await _get(_KEY_PREFIX + owner)          # raises if the relay cannot be asked
        if not (isinstance(doc, dict) and doc.get("priv") and doc.get("pub")):
            if not local:
                now = time.monotonic()
                _mints[:] = [t for t in _mints if now - t < 60]
                if len(_mints) >= _MINTS_PER_MINUTE:
                    raise MintLimited("too many new accounts at once; try again shortly")
                _mints.append(now)
            priv, pub = await asyncio.to_thread(httpsig.new_keypair)
            await _put(_KEY_PREFIX + owner, {"priv": priv, "pub": pub, "created": int(time.time())})
            # READ IT BACK and use what the relay holds. The app process and the worker can both
            # mint a first key at once; the relay keeps one of them, and a process that went on
            # using its own would sign with a key no remote server can ever see.
            await asyncio.sleep(0.5)
            doc = await _get(_KEY_PREFIX + owner)
            if not (isinstance(doc, dict) and doc.get("priv") and doc.get("pub")):
                raise RuntimeError("a new signing key was written but cannot be read back")
        _key_cache[owner] = doc
        if len(_key_cache) > 5000:                     # bounded: any npub can end up with a key
            for k in list(_key_cache)[:1000]:
                if k != "instance":
                    _key_cache.pop(k, None)
        return doc


# ------------------------------------------------------------------------------------ followers

async def add_follower(member: str, actor: str, inbox: str) -> None:
    await _put(f"{_FOLLOWER_PREFIX}{member}:{_h(actor)}", {"actor": actor, "inbox": inbox, "at": int(time.time())})


async def is_follower(member: str, actor: str) -> bool:
    doc = await _get(f"{_FOLLOWER_PREFIX}{member}:{_h(actor)}")
    return isinstance(doc, dict) and bool(doc.get("actor")) and not doc.get("gone")


async def remove_follower(member: str, actor: str) -> None:
    # A replaceable document cannot be un-written by us; a tombstone replaces it.
    await _put(f"{_FOLLOWER_PREFIX}{member}:{_h(actor)}", {"actor": actor, "gone": True, "at": int(time.time())})


async def followers(member: str, *, strict: bool = True) -> list:
    """[{"actor", "inbox"}] for a member's live followers."""
    docs = await nostr_store.list_docs(_port(), f"{_FOLLOWER_PREFIX}{member}:", seckey=_seckey(), strict=strict,
                                       limit=100000)
    return [d for d in docs.values() if isinstance(d, dict) and d.get("actor") and not d.get("gone")]


async def follower_count(member: str) -> int:
    try:
        return len(await followers(member, strict=False))
    except Exception:
        return 0


# ------------------------------------------------------------------------------------ following

async def set_following(member: str, actor: str, inbox: str, state: str, follow_id: str = "") -> None:
    doc = {"actor": actor, "inbox": inbox, "state": state, "at": int(time.time())}
    if follow_id:
        doc["id"] = follow_id                 # the Follow's own id, which an Undo must name
    await _put(f"{_FOLLOWING_PREFIX}{member}:{_h(actor)}", doc)


async def drop_following(member: str, actor: str) -> None:
    await _put(f"{_FOLLOWING_PREFIX}{member}:{_h(actor)}", {"actor": actor, "gone": True, "at": int(time.time())})


# Readable handles for Nostr users with no name here (see actors.readable_handle): assigned once and
# kept, both ways, so the handle survives a profile rename and resolves back to its owner.
_NICK_PREFIX = "pcai:ap:nick:"            # pubkey -> {"nick"}
_NICK_OWNER_PREFIX = "pcai:ap:nickof:"    # nick   -> {"pk"}


async def nick_of(pubkey: str) -> str:
    doc = await _get(_NICK_PREFIX + pubkey)          # raises when the relay cannot be asked
    return str((doc or {}).get("nick") or "")


async def owner_of_nick(nick: str) -> str:
    doc = await _get(_NICK_OWNER_PREFIX + nick.lower())
    return str((doc or {}).get("pk") or "")


async def claim_nick(pubkey: str, nick: str) -> None:
    """Owner first: a crash between the two writes leaves a name pointing at its key (harmless, it
    still resolves) rather than a key naming a handle nobody can look up."""
    await _put(_NICK_OWNER_PREFIX + nick.lower(), {"pk": pubkey, "at": int(time.time())})
    await _put(_NICK_PREFIX + pubkey, {"nick": nick, "at": int(time.time())})


_GONE_PREFIX = "pcai:ap:gone:"


async def mark_gone(actor: str) -> None:
    """A fediverse account that no longer exists (its own Delete, confirmed by its server). Kept so
    delivery stops posting to it -- it cannot Undo its follows any more, so nothing else would."""
    await _put(_GONE_PREFIX + _h(actor), {"actor": actor, "at": int(time.time())})


async def gone_actors() -> set:
    docs = await nostr_store.list_docs(_port(), _GONE_PREFIX, seckey=_seckey(), strict=False, limit=100000)
    return {d["actor"] for d in docs.values() if isinstance(d, dict) and d.get("actor") and not d.get("back")}


_BLOCKED_PREFIX = "pcai:ap:blocked:"


async def record_block(member: str, actor: str, acct: str) -> None:
    """A fediverse account blocked one of ours (an incoming Block). Kept so the block bot can say so
    and count it -- the equivalent of the Block rows Pleroma kept in its own database."""
    await _put(f"{_BLOCKED_PREFIX}{member}:{_h(actor)}", {"actor": actor, "acct": acct, "at": int(time.time())})


async def undo_block(member: str, actor: str) -> None:
    await _put(f"{_BLOCKED_PREFIX}{member}:{_h(actor)}", {"actor": actor, "gone": True, "at": int(time.time())})


async def blocks(*, strict: bool = True) -> list:
    """[{"member", "actor", "acct", "at"}] for every live block of one of ours."""
    docs = await nostr_store.list_docs(_port(), _BLOCKED_PREFIX, seckey=_seckey(), strict=strict, limit=100000)
    out = []
    for d_tag, d in docs.items():
        if isinstance(d, dict) and d.get("actor") and not d.get("gone"):
            out.append({"member": d_tag[len(_BLOCKED_PREFIX):].split(":")[0], "actor": d["actor"],
                        "acct": d.get("acct", ""), "at": int(d.get("at") or 0)})
    return out


async def following(member: str, *, strict: bool = True) -> dict:
    """{actor: {"inbox", "state"}} for the remote accounts a member follows (live ones only)."""
    docs = await nostr_store.list_docs(_port(), f"{_FOLLOWING_PREFIX}{member}:", seckey=_seckey(), strict=strict,
                                       limit=100000)
    return {d["actor"]: d for d in docs.values() if isinstance(d, dict) and d.get("actor") and not d.get("gone")}


_followed_cache = {"at": 0.0, "map": {}}


async def followed_actors(max_age: float = 60.0) -> dict:
    """{remote actor: {member pubkey, ...}} across ALL members, from ONE relay read, cached a minute.

    This is the gate for accepting a remote account's posts at all (see inbox.py), so it is asked
    for every incoming Create; a read per member per post would be the inbox's whole cost. On a
    failed refresh the last good map keeps answering -- "could not ask" is not "nobody follows"."""
    now = time.monotonic()
    if now - _followed_cache["at"] < max_age and _followed_cache["at"]:
        return _followed_cache["map"]
    try:
        docs = await nostr_store.list_docs(_port(), _FOLLOWING_PREFIX, seckey=_seckey(), strict=True,
                                           limit=100000)
    except Exception:
        return _followed_cache["map"]
    out: dict = {}
    for d_tag, d in docs.items():
        if isinstance(d, dict) and d.get("actor") and not d.get("gone"):
            member = d_tag[len(_FOLLOWING_PREFIX):].split(":")[0]
            out.setdefault(d["actor"], set()).add(member)
    _followed_cache.update(at=now, map=out)
    return out


def forget_followed_cache() -> None:
    _followed_cache["at"] = 0.0


# ------------------------------------------------------------------------------------ DM conversations

_CONVO_PREFIX = "pcai:ap:convo:"


async def open_conversation(pubkey: str, actor: str) -> None:
    """`pubkey` messaged `actor`: from now on `actor` may message `pubkey` back."""
    await _put(f"{_CONVO_PREFIX}{pubkey}:{_h(actor)}", {"actor": actor, "at": int(time.time())})


async def in_conversation(pubkey: str, actor: str) -> bool:
    doc = await nostr_store.get_doc(_port(), f"{_CONVO_PREFIX}{pubkey}:{_h(actor)}", seckey=_seckey())
    return isinstance(doc, dict) and doc.get("actor") == actor


# ------------------------------------------------------------------------------------ cursor

CURSOR = _CURSOR
CURSOR_EVERYONE = "pcai:ap:cursor:everyone"


async def cursor(key: str = _CURSOR) -> int:
    doc = await _get(key)
    return int((doc or {}).get("since") or 0)


async def set_cursor(since: int, key: str = _CURSOR) -> None:
    await _put(key, {"since": int(since)})


_nonlocal_cache = {"at": 0.0, "set": frozenset()}


async def nostr_users_with_followers(max_age: float = 60.0) -> frozenset:
    """Every pubkey that has at least one live follower on the fediverse, from ONE read, cached a
    minute. (The everyone pass sends such a user's public posts to them.)"""
    now = time.monotonic()
    if _nonlocal_cache["at"] and now - _nonlocal_cache["at"] < max_age:
        return _nonlocal_cache["set"]
    # A failed read RAISES: this set decides which posts are sent, and answering "nobody has
    # followers" would make the everyone pass skip every post it reads -- and move past them.
    docs = await nostr_store.list_docs(_port(), _FOLLOWER_PREFIX, seckey=_seckey(), strict=True, limit=100000)
    out = frozenset(d_tag[len(_FOLLOWER_PREFIX):].split(":")[0] for d_tag, d in docs.items()
                    if isinstance(d, dict) and d.get("actor") and not d.get("gone"))
    _nonlocal_cache.update(at=now, set=out)
    return out


# ------------------------------------------------------------------------------------ pinned actor ids

# AN ACTOR'S ID NEVER MOVES. It used to be spelled from whatever the account was called at the time
# (`/ap/users/<registry name>`, else `/ap/users/<npub>`), so a Nostr user followed as their npub who
# later claimed a name here became a NEW account on every server -- deliveries signed by an actor
# nobody followed, and a refetch of the old address answering with a different id. The first spelling
# an account federates under is recorded here, both ways, and is what it is called from then on.
_ID_PREFIX = "pcai:ap:id:"            # pubkey -> {"h"}
_ID_OWNER_PREFIX = "pcai:ap:idof:"    # h      -> {"pk"}


async def pinned_handle(pubkey: str) -> str:
    doc = await _get(_ID_PREFIX + pubkey)            # raises when the relay cannot be asked
    return str((doc or {}).get("h") or "")


async def owner_of_handle(h: str) -> str:
    doc = await _get(_ID_OWNER_PREFIX + h.lower())
    return str((doc or {}).get("pk") or "")


async def pin_handle(pubkey: str, h: str) -> None:
    """Owner first, like claim_nick: a crash between the two writes leaves a name that resolves."""
    await _put(_ID_OWNER_PREFIX + h.lower(), {"pk": pubkey, "at": int(time.time())})
    await _put(_ID_PREFIX + pubkey, {"h": h, "at": int(time.time())})


async def unmark_gone(actor: str) -> None:
    """An account marked gone that is plainly back (it just followed one of ours, verified): a
    suspended-then-restored Mastodon account sends Delete(actor) and later returns."""
    await _put(_GONE_PREFIX + _h(actor), {"actor": actor, "back": True, "at": int(time.time())})


# ------------------------------------------------------------------------------------ followers, as Nostr sees them

# The accounts HERE a fediverse account follows -- what its puppet's kind-3 contact list names, which is
# how a Nostr client learns "carol followed you" (the notification every client already raises for a
# contact list that p-tags you). One document per fediverse account.
_FOLLOWS_OF_PREFIX = "pcai:ap:followsof:"


async def follows_of(actor: str) -> list:
    doc = await _get(_FOLLOWS_OF_PREFIX + _h(actor))
    return [m for m in ((doc or {}).get("members") or []) if isinstance(m, str)]


async def set_follows_of(actor: str, members: list) -> None:
    await _put(_FOLLOWS_OF_PREFIX + _h(actor), {"actor": actor, "members": sorted(set(members)),
                                                 "at": int(time.time())})


# ------------------------------------------------------------------------------------ follow ids

# The id of each incoming Follow, so an Undo that names ONLY that id (allowed, and sent by some
# servers) can be mapped back to the member it was for.
_FOLLOW_ID_PREFIX = "pcai:ap:followid:"


async def remember_follow_id(follow_id: str, member: str, actor: str) -> None:
    await _put(_FOLLOW_ID_PREFIX + _h(follow_id), {"member": member, "actor": actor, "at": int(time.time())})


async def follow_by_id(follow_id: str) -> dict:
    doc = await _get(_FOLLOW_ID_PREFIX + _h(follow_id))
    return doc if isinstance(doc, dict) else {}


# ------------------------------------------------------------------------------------ moves

# A fediverse account that MOVED (Move activity, confirmed by the new account's alsoKnownAs). The
# members' contact lists still name the old account's puppet and cannot be rewritten here (they are
# signed by the members), so delivery reads the old actor as the new one through this map.
_MOVED_PREFIX = "pcai:ap:moved:"
_moved_cache = {"at": 0.0, "map": {}}


async def record_move(old: str, new: str) -> None:
    await _put(_MOVED_PREFIX + _h(old), {"old": old, "new": new, "at": int(time.time())})
    _moved_cache["at"] = 0.0


async def moves(max_age: float = 120.0) -> dict:
    """{old actor: new actor}, cached; a failed read keeps the last good map."""
    now = time.monotonic()
    if _moved_cache["at"] and now - _moved_cache["at"] < max_age:
        return _moved_cache["map"]
    try:
        docs = await nostr_store.list_docs(_port(), _MOVED_PREFIX, seckey=_seckey(), strict=True, limit=100000)
    except Exception:
        return _moved_cache["map"]
    out = {d["old"]: d["new"] for d in docs.values() if isinstance(d, dict) and d.get("old") and d.get("new")}
    _moved_cache.update(at=now, map=out)
    return out


# ------------------------------------------------------------------------------------ polls

_POLL_VOTERS_PREFIX = "pcai:ap:pollvoters:"     # poll event id -> {"inboxes": [...]} (app process)
_POLL_TALLY_PREFIX = "pcai:ap:polltally:"       # poll event id -> {"counts", "voters"} (worker)


async def add_poll_voter_inbox(poll_id: str, inbox: str) -> None:
    doc = await _get(_POLL_VOTERS_PREFIX + poll_id) or {}
    inboxes = [i for i in (doc.get("inboxes") or []) if isinstance(i, str)]
    if inbox and inbox not in inboxes and len(inboxes) < 500:
        await _put(_POLL_VOTERS_PREFIX + poll_id, {"inboxes": inboxes + [inbox]})


async def poll_voter_inboxes(poll_id: str) -> list:
    doc = await nostr_store.get_doc(_port(), _POLL_VOTERS_PREFIX + poll_id, seckey=_seckey())
    return [i for i in ((doc or {}).get("inboxes") or []) if isinstance(i, str)]


async def poll_tally(poll_id: str) -> dict:
    doc = await _get(_POLL_TALLY_PREFIX + poll_id)
    return doc if isinstance(doc, dict) else {}


async def set_poll_tally(poll_id: str, tally: dict) -> None:
    await _put(_POLL_TALLY_PREFIX + poll_id, tally)


# ------------------------------------------------------------------------------------ retry queue

# Deliveries waiting to be retried, ONE DOCUMENT EACH, so a worker restart (every deploy) does not
# silently drop what a resting server was owed. Removed by writing a tombstone.
_RETRY_PREFIX = "pcai:ap:retry:"


async def save_retry(key: str, entry: dict) -> None:
    await _put(_RETRY_PREFIX + key, entry)


async def drop_retry(key: str) -> None:
    await _put(_RETRY_PREFIX + key, {"done": True})


async def load_retries() -> dict:
    docs = await nostr_store.list_docs(_port(), _RETRY_PREFIX, seckey=_seckey(), strict=True, limit=20000)
    return {k[len(_RETRY_PREFIX):]: v for k, v in docs.items()
            if isinstance(v, dict) and not v.get("done") and v.get("inbox") and v.get("activity")}

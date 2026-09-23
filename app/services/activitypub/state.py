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

async def keypair(owner: str) -> dict:
    """{"priv", "pub"} for a member pubkey (or "instance"), created once and then only ever read."""
    hit = _key_cache.get(owner)
    if hit:
        return hit
    async with _key_lock:
        hit = _key_cache.get(owner)
        if hit:
            return hit
        doc = await _get(_KEY_PREFIX + owner)          # raises if the relay cannot be asked
        if not (isinstance(doc, dict) and doc.get("priv") and doc.get("pub")):
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
        return doc


# ------------------------------------------------------------------------------------ followers

async def add_follower(member: str, actor: str, inbox: str) -> None:
    await _put(f"{_FOLLOWER_PREFIX}{member}:{_h(actor)}", {"actor": actor, "inbox": inbox, "at": int(time.time())})


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

async def set_following(member: str, actor: str, inbox: str, state: str) -> None:
    await _put(f"{_FOLLOWING_PREFIX}{member}:{_h(actor)}",
               {"actor": actor, "inbox": inbox, "state": state, "at": int(time.time())})


async def drop_following(member: str, actor: str) -> None:
    await _put(f"{_FOLLOWING_PREFIX}{member}:{_h(actor)}", {"actor": actor, "gone": True, "at": int(time.time())})


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


# ------------------------------------------------------------------------------------ cursor

async def cursor() -> int:
    doc = await _get(_CURSOR)
    return int((doc or {}).get("since") or 0)


async def set_cursor(since: int) -> None:
    await _put(_CURSOR, {"since": int(since)})

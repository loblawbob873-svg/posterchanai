"""OUR actors: which pubkeys are on the fediverse as `@name@<domain>` (local users) or
`@npub1…@<domain>` (any Nostr user, in `everyone` mode), and what they look like.

EVERY LOCAL USER, AUTOMATICALLY. When ActivityPub is on, every name in this node's NIP-05 registry
(`nostr_relay_nip05_names`, the list this node writes when it grants a name) is an actor -- nobody
signs up, and their signing key is made the first time anything asks for them. The registry is the
authority: it is server-side, written by this node, so it needs no second confirmation from the
account's own profile. The one exclusion is an account blocked on the relay.
"""
from __future__ import annotations

import asyncio
import re

import json
import logging
import time

from app.services import settings_store
from app.services.activitypub import config, convert, state

logger = logging.getLogger(__name__)

_names_cache = {"raw": None, "by_name": {}, "by_pk": {}}


def _registry() -> tuple[dict, dict]:
    """({name: pubkey}, {pubkey: first name}) from the node's NIP-05 registry, parsed once per edit."""
    raw = settings_store.get("nostr_relay_nip05_names", "") or ""
    if raw != _names_cache["raw"]:
        from app.services.nostr_relay.thread import _parse_nip05
        names, _ = _parse_nip05(raw, "")
        by_name = {n.lower(): pk.lower() for n, pk in names.items() if n and pk}
        by_pk: dict = {}
        for n, pk in sorted(by_name.items()):
            by_pk.setdefault(pk, n)
        _names_cache.update(raw=raw, by_name=by_name, by_pk=by_pk)
    return _names_cache["by_name"], _names_cache["by_pk"]


def name_of(pubkey: str) -> str:
    return _registry()[1].get((pubkey or "").lower(), "")


def pubkey_of_name(name: str) -> str:
    return _registry()[0].get((name or "").lower(), "")


_blocked_cache = {"raw": None, "set": frozenset()}


def _relay_blocked() -> frozenset:
    """Pubkeys blocked on the relay -- parsed once per edit of the setting, not per call."""
    raw = settings_store.get("nostr_relay_blocked_pubkeys", "") or ""
    if raw != _blocked_cache["raw"]:
        from app.services.nostr import nostr_service
        out = set()
        for tok in raw.replace(",", "\n").split():
            h = nostr_service.to_pubkey_hex(tok.strip())
            if h:
                out.add(h.lower())
        _blocked_cache.update(raw=raw, set=frozenset(out))
    return _blocked_cache["set"]


def puppet_blocked(actor_id: str, acct: str = "") -> bool:
    """A fediverse account whose stand-in Nostr key is on the RELAY's blocklist.

    Blocking somebody from the client (or Admin → Relay → blocked pubkeys) blocks their KEY, and a
    fediverse account's key is its puppet's. The relay refuses that key -- but posts arriving over
    ActivityPub are written here by the server itself, so the inbox has to ask too, or a blocked
    fediverse account kept appearing ("I blocked them but I still see new posts"). Checks every key
    this account has had: the stored puppet rows (by actor id and by handle -- the retired Pleroma
    bridge keyed some on a profile URL) and the key derived from the actor id."""
    blocked = _relay_blocked()
    if not blocked or not actor_id:
        return False
    ck = (actor_id, (acct or "").lower())
    if _puppet_block_cache.get("set") is not blocked:        # the list changed: every answer is stale
        _puppet_block_cache.clear()
        _puppet_block_cache["set"] = blocked
    if ck in _puppet_block_cache:
        return _puppet_block_cache[ck]
    keys = set()
    try:
        from sqlalchemy import func, or_
        from app.database import SessionLocal
        from app.models import FediPuppet
        db = SessionLocal()
        try:
            conds = [FediPuppet.actor_uri == actor_id.split("#")[0]]
            if acct:
                conds.append(func.lower(FediPuppet.acct) == acct.lower().lstrip("@"))
            keys |= {pk for (pk,) in db.query(FediPuppet.pubkey_hex).filter(or_(*conds)).all() if pk}
        finally:
            db.close()
    except Exception:
        pass
    try:
        from app.services import fedi_bridge_identity as ident
        keys.add(ident.puppet_for({"url": actor_id.split("#")[0], "acct": acct or ""})["pubkey_hex"])
    except Exception:
        pass
    hit = any(k.lower() in blocked for k in keys)
    if len(_puppet_block_cache) > 5000:
        _puppet_block_cache.clear()
        _puppet_block_cache["set"] = blocked
    _puppet_block_cache[ck] = hit
    return hit


_puppet_block_cache: dict = {}


def is_actor(pubkey: str) -> bool:
    """A local user this node gave a name, and not blocked on the relay."""
    pk = (pubkey or "").lower()
    return bool(pk) and pk in _registry()[1] and pk not in _relay_blocked()


def all_actors() -> list:
    """Every local user who is on the fediverse right now."""
    blocked = _relay_blocked()
    return [pk for pk in _registry()[1] if pk not in blocked]


async def member_by_name(name: str) -> str:
    """The pubkey behind a handle -- a local user's name, or (in `everyone` mode) any Nostr user's
    readable handle or npub -- if that account is on the fediverse, else ""."""
    # A readable handle already given out WINS over a registry name spelled the same: the registry is
    # written by public signup, so checked first it let anyone register `alice_4b56` and take over
    # the fediverse address of the Nostr user already known everywhere by it.
    if config.everyone() and _NICK_RE.fullmatch((name or "").lower()):
        try:
            owner = await _nick_owner(name.lower())
        except Exception:
            owner = ""
        if owner:
            return owner if await exposed(owner) else ""
    pk = pubkey_of_name(name)
    if pk:
        return pk if is_actor(pk) else ""
    pk = _npub_pubkey(name)
    return pk if pk and await exposed(pk) else ""


async def name_is_someone_elses_handle(name: str, pubkey: str = "") -> bool:
    """Whether a registry name would collide with a fediverse address another account already has:
    an npub, or a readable handle claimed by a different key. An unreadable relay says yes -- a
    signup can pick another name, a taken address cannot be given back."""
    n = (name or "").strip().lower()
    if n.startswith("npub1"):
        return True
    if not _NICK_RE.fullmatch(n):
        return False
    try:
        owner = await state.owner_of_nick(n)
    except Exception:
        return True
    return bool(owner) and owner != (pubkey or "").lower()


# A readable handle: letters/digits/underscore, ending in `_` + a hex prefix of the key -- the shape
# readable_handle mints, so no local name or npub can be mistaken for one.
_NICK_RE = re.compile(r"[a-z0-9_]{1,24}_[0-9a-f]{4,16}")


def _nick_base(profile: dict) -> str:
    for raw in (profile.get("display_name"), profile.get("name"), (profile.get("nip05") or "").split("@")[0]):
        s = re.sub(r"[^a-z0-9_]+", "_", str(raw or "").lower()).strip("_")
        s = re.sub(r"_+", "_", s)[:20].strip("_")
        if s and s != "_":
            return s
    return "nostr"




async def readable_handle(pubkey: str) -> str:
    """The handle an account SHOWS on the fediverse. A local user's name; for any other Nostr user
    (`everyone` mode) a readable one made once from their profile name and a short key suffix --
    `alice_4b56` -- instead of a 63-character npub. It is stored the first time and never recomputed,
    so a profile rename cannot break the follows and mentions that name it; the actor's ADDRESS is
    pinned separately (ap_handle), and WebFinger answers for both. A failed relay read falls back to
    the npub for this call and mints nothing -- a handle is only ever created on the strength of a
    real answer."""
    pk = (pubkey or "").lower()
    local = name_of(pk)
    if local:
        # A registry name spelled like a readable handle somebody ELSE already has (an admin-typed
        # `dan_2024`): shown as this account's handle, WebFinger would answer it with the other
        # account, and no server could follow this one. It gets a handle of its own instead.
        try:
            other = await _nick_owner(local)
        except Exception:
            other = ""
        if not other or other == pk:
            return local
    fallback = local or handle(pk)
    if not fallback:
        return ""
    hit = _nick_cache.get(pk)
    if hit:
        return hit
    try:
        async with _nick_lock:
            nick = await state.nick_of(pk)
            if not nick:
                base = _nick_base(await profile(pk))
                for n in (4, 6, 8, 12, 16):
                    cand = f"{base}_{pk[:n]}"
                    if pubkey_of_name(cand) not in ("", pk):
                        continue                        # a local user's name is never minted as a handle
                    if await state.owner_of_handle(cand) not in ("", pk):
                        continue                        # nor another account's pinned address
                    if await state.owner_of_nick(cand) not in ("", pk):
                        continue
                    await state.claim_nick(pk, cand)
                    # READ IT BACK: the app process and the worker can claim at once, and the loser
                    # would advertise a handle whose WebFinger answers with somebody else.
                    await asyncio.sleep(0.3)
                    if await state.owner_of_nick(cand) == pk:
                        nick = cand
                        break
    except Exception:
        return fallback
    if not nick:
        return fallback
    _nick_cache[pk] = nick
    _nick_owner_cache[nick] = (float("inf"), pk)
    while len(_nick_cache) > 20000:
        _nick_cache.popitem(last=False)
    return nick


async def _nick_owner(nick: str) -> str:
    """state.owner_of_nick, cached: a claimed handle never changes hands, so a hit is kept for good and
    a miss for a minute -- anybody can make us ask about any handle-shaped name."""
    n = (nick or "").lower()
    hit = _nick_owner_cache.get(n)
    now = time.monotonic()
    if hit and now < hit[0]:
        return hit[1]
    owner = await state.owner_of_nick(n)
    _nick_owner_cache[n] = (float("inf") if owner else now + 60, owner)
    while len(_nick_owner_cache) > 20000:
        _nick_owner_cache.popitem(last=False)
    return owner


async def ap_handle(pubkey: str) -> str:
    """The path segment of an account's ACTOR ID -- `/ap/users/<this>` -- pinned the first time the
    account federates and never changed after (see state.pin_handle). "" when the account has no
    fediverse address at all. Raises when the relay cannot be asked: an id must never be guessed."""
    pk = (pubkey or "").lower()
    if not pk:
        return ""
    hit = _pin_cache.get(pk)
    if hit:
        return hit
    h = await state.pinned_handle(pk)
    if not h:
        cand = handle(pk)
        if not cand:
            return ""
        owner = await state.owner_of_handle(cand)
        if owner and owner != pk:
            # The name was given to somebody else before (a registry name reassigned): that address
            # is theirs for good, so this account federates under its npub.
            from app.services.nostr import nostr_service
            cand = nostr_service.npub_of(pk)
        await state.pin_handle(pk, cand)
        h = cand
    elif not (is_actor(pk) or config.everyone()) or pk in _relay_blocked():
        return ""
    _pin_cache[pk] = h
    _pin_owner_cache[h.lower()] = pk
    while len(_pin_cache) > 20000:
        _pin_cache.popitem(last=False)
    while len(_pin_owner_cache) > 20000:
        _pin_owner_cache.popitem(last=False)
    return h


async def actor_id(pubkey: str) -> str:
    """This account's actor URL ("" when it has none)."""
    h = await ap_handle(pubkey)
    return convert.actor_url(config.base_url(), h) if h else ""


async def member_of_path(name: str) -> str:
    """The pubkey behind `/ap/users/<name>` -- the account whose PINNED id this is, else whoever the
    name means today (member_by_name) -- if that account is on the fediverse, else ""."""
    n = (name or "").strip().lower()
    if not n:
        return ""
    owner = _pin_owner_cache.get(n)
    if owner is None:
        try:
            owner = await state.owner_of_handle(n)
        except Exception:
            owner = ""
        if owner:
            _pin_owner_cache[n] = owner
    if owner:
        return owner if await exposed(owner) else ""
    # Not anybody's pinned id: whoever the name means today. Their document still carries their own
    # pinned id, so an old or alternative spelling of an address is an alias, never a second account.
    return await member_by_name(name)


def _npub_pubkey(handle: str) -> str:
    h = (handle or "").strip().lower()
    if not h.startswith("npub1"):
        return ""
    try:
        from app.services.nostr import nostr_service
        return (nostr_service.to_pubkey_hex(h) or "").lower()
    except Exception:
        return ""


def handle(pubkey: str) -> str:
    """The name an account has on the fediverse: its local name, or its npub in `everyone` mode --
    "" when it has none. (Whether that account may be SERVED is `exposed`; this only spells it.)"""
    pk = (pubkey or "").lower()
    local = name_of(pk)
    if local:
        return local
    if config.everyone() and pk and pk not in _relay_blocked():
        from app.services.nostr import nostr_service
        try:
            return nostr_service.npub_of(pk)
        except Exception:
            return ""
    return ""


_known_cache: dict = {}


async def exposed(pubkey: str) -> bool:
    """May this account be served on the fediverse? A local user always (is_actor); anybody else in
    `everyone` mode when this relay holds a profile for them -- an address nobody has ever published
    anything under is not an account, and minting a key for it would let anyone make this server
    generate keys for made-up npubs."""
    pk = (pubkey or "").lower()
    if not pk:
        return False
    if is_actor(pk):
        return True
    if not config.everyone() or pk in _relay_blocked():
        return False
    hit = _known_cache.get(pk)
    if hit is not None and time.monotonic() - hit[0] < 600:
        return hit[1]
    known = await _is_native_nostr_account(pk)
    _known_cache[pk] = (time.monotonic(), known)
    if len(_known_cache) > 20000:
        _known_cache.clear()
    return known


async def _is_native_nostr_account(pk: str) -> bool:
    """A real Nostr account: it has a profile here, and it is NOT somebody else seen through a
    bridge. A fediverse puppet (ours) or a mirror account (Mostr and friends, whose profiles carry
    a `proxy` or `fedibridge` tag) served as `npub…@<our domain>` would put a copy of a fediverse
    person under this domain -- followable, messageable, signing as them."""
    if is_puppet(pk):
        return False
    ev = await profile_event(pk)
    if not ev:
        return False
    return not any(t and t[0] in ("proxy", "fedibridge") for t in ev.get("tags") or [])


def is_puppet(pk: str) -> bool:
    from app.database import SessionLocal
    from app.models import FediPuppet
    db = SessionLocal()
    try:
        return db.query(FediPuppet.pubkey_hex).filter(FediPuppet.pubkey_hex == pk).first() is not None
    finally:
        db.close()


def pubkey_of_actor_url(url: str) -> str:
    """The pubkey behind one of OUR actor URLs (local name or npub), without deciding exposure."""
    base = config.base_url()
    prefix = f"{base}/ap/users/"
    if not base or not isinstance(url, str) or not url.startswith(prefix):
        return ""
    h = url[len(prefix):].split("/")[0].split("#")[0].split("?")[0]
    return _pin_owner_cache.get(h.lower()) or pubkey_of_name(h) or _npub_pubkey(h)


class _LocalActors(dict):
    """{our actor URL: pubkey} -- how an incoming Mention of one of ours is recognised. A plain dict
    of the local names, that ALSO answers for any `/ap/users/npub1…` URL in `everyone` mode, so a
    mention of a Nostr user who is not local still becomes a `p` tag they are notified by.
    Recognising a mention grants nothing, so it is not gated on exposure."""
    def get(self, key, default=None):
        hit = dict.get(self, key)
        if hit:
            return hit
        if config.everyone():
            pk = pubkey_of_actor_url(key)
            if pk and pk not in _relay_blocked():
                return pk
        return default


def local_actor_map() -> dict:
    base = config.base_url()
    return _LocalActors({convert.actor_url(base, n): pk for n, pk in _registry()[0].items()} if base else {})


from collections import OrderedDict

_nick_cache: OrderedDict = OrderedDict()   # pubkey -> readable handle (readable_handle)
_nick_owner_cache: OrderedDict = OrderedDict()   # handle -> (valid until, owner pubkey)
_nick_lock = asyncio.Lock()
_pin_cache: OrderedDict = OrderedDict()    # pubkey -> pinned id handle (ap_handle)
_pin_owner_cache: OrderedDict = OrderedDict()    # pinned id handle -> pubkey

_profile_cache: "OrderedDict[str, tuple]" = OrderedDict()
_PROFILE_CACHE_MAX = 5000


async def profile_event(pubkey: str) -> dict | None:
    """The account's kind-0 EVENT from this node's relay (None when it has none), cached five
    minutes in a bounded cache -- anybody can make us look up any npub."""
    hit = _profile_cache.get(pubkey)
    if hit and time.monotonic() - hit[0] < 300:
        _profile_cache.move_to_end(pubkey)
        return hit[1]
    from app.services.fedi_bridge_identity import query_one
    ok, ev = await query_one(settings_store._port(), {"kinds": [0], "authors": [pubkey], "limit": 1})
    if ok:
        _profile_cache[pubkey] = (time.monotonic(), ev)
        _profile_cache.move_to_end(pubkey)
        while len(_profile_cache) > _PROFILE_CACHE_MAX:
            _profile_cache.popitem(last=False)
    return ev if ok else None


async def profile(pubkey: str) -> dict:
    """The account's kind-0 content, parsed ({} when there is none)."""
    ev = await profile_event(pubkey)
    try:
        prof = json.loads((ev or {}).get("content") or "{}")
    except ValueError:
        return {}
    return prof if isinstance(prof, dict) else {}


async def person(pubkey: str, *, anonymous: bool = False) -> dict:
    """An account's actor document. `anonymous` is an unauthenticated fetch of it: the only path a
    stranger can drive for free, so the only one whose first key is rate-limited (state.keypair)."""
    keys = await state.keypair(pubkey, local=is_actor(pubkey) or not anonymous)
    return convert.person(base=config.base_url(), name=await ap_handle(pubkey), profile=await profile(pubkey),
                          public_key_pem=keys["pub"], username=await readable_handle(pubkey),
                          consented=is_actor(pubkey))


async def instance_actor() -> dict:
    """The node's own Application actor: what signs our fetches (servers with authorized fetch
    require a signed GET, and the signer has to be an actor they can look up)."""
    base = config.base_url()
    keys = await state.keypair("instance")
    actor = f"{base}/ap/actor"
    return {"@context": convert.AS_CONTEXT, "id": actor, "type": "Application",
            "preferredUsername": config.domain(), "name": "PosterChan",
            "inbox": f"{base}/ap/inbox", "outbox": f"{actor}/outbox",
            "endpoints": {"sharedInbox": f"{base}/ap/inbox"}, "manuallyApprovesFollowers": True,
            "publicKey": {"id": f"{actor}#main-key", "owner": actor, "publicKeyPem": keys["pub"]}}


async def signing(pubkey: str, keys: dict) -> tuple[str, str]:
    """(keyId, private PEM) for delivering as an account -- under its PINNED id."""
    return f"{await actor_id(pubkey)}#main-key", keys["priv"]


async def featured_ids(pubkey: str) -> list:
    """The event ids an account PINNED (its NIP-51 kind-10001 list), newest pin first -- what its
    `featured` collection serves. [] when it has none or the relay cannot be read."""
    from app.services.fedi_bridge_identity import query_one
    try:
        ok, ev = await query_one(settings_store._port(), {"kinds": [10001], "authors": [pubkey], "limit": 1})
    except Exception:
        return []
    if not ok or not ev:
        return []
    ids = [t[1] for t in ev.get("tags") or [] if len(t) > 1 and t[0] == "e" and len(t[1]) == 64]
    return list(reversed(ids))[:20]

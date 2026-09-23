"""OUR actors: which pubkeys are on the fediverse as `@name@<domain>`, and what they look like.

EVERY LOCAL USER, AUTOMATICALLY. When ActivityPub is on, every name in this node's NIP-05 registry
(`nostr_relay_nip05_names`, the list this node writes when it grants a name) is an actor -- nobody
signs up, and their signing key is made the first time anything asks for them. The registry is the
authority: it is server-side, written by this node, so it needs no second confirmation from the
account's own profile. The one exclusion is an account blocked on the relay.
"""
from __future__ import annotations

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


def _relay_blocked() -> set:
    from app.services.nostr import nostr_service
    out = set()
    for tok in (settings_store.get("nostr_relay_blocked_pubkeys", "") or "").replace(",", "\n").split():
        h = nostr_service.to_pubkey_hex(tok.strip())
        if h:
            out.add(h.lower())
    return out


def is_actor(pubkey: str) -> bool:
    """A local user this node gave a name, and not blocked on the relay."""
    pk = (pubkey or "").lower()
    return bool(pk) and pk in _registry()[1] and pk not in _relay_blocked()


def all_actors() -> list:
    """Every local user who is on the fediverse right now."""
    blocked = _relay_blocked()
    return [pk for pk in _registry()[1] if pk not in blocked]


async def member_by_name(name: str) -> str:
    """The pubkey behind `name` if that local user is on the fediverse, else ""."""
    pk = pubkey_of_name(name)
    return pk if is_actor(pk) else ""


def local_actor_map() -> dict:
    """{our actor URL: pubkey} for every granted name -- how an incoming Mention of one of ours is
    recognised. Deliberately NOT gated on membership: recognising a mention grants nothing."""
    base = config.base_url()
    return {convert.actor_url(base, n): pk for n, pk in _registry()[0].items()} if base else {}


def uses_linked_account(pubkey: str) -> bool:
    """THE COMPATIBILITY RULE WITH THE PLEROMA BRIDGE: a member whose Nostr activity already reaches
    the fediverse through their OWN linked Pleroma account (the write-back whitelist) is not ALSO
    delivered from `@name@<domain>` -- one like, one boost, one reply on the fediverse, never two
    from two different identities. Their actor still exists, and can still be followed and replied
    to. Read from the write-back service itself, so the two can never disagree about who that is."""
    try:
        from app.services.fedi_nostr_writeback_service import _bridge_allowed_pubkeys
        return (pubkey or "").lower() in _bridge_allowed_pubkeys()
    except Exception:
        return False


_profile_cache: dict = {}


async def profile(pubkey: str) -> dict:
    """The member's kind-0 content (parsed), from this node's relay, cached five minutes."""
    hit = _profile_cache.get(pubkey)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    from app.services.fedi_bridge_identity import query_one
    ok, ev = await query_one(settings_store._port(), {"kinds": [0], "authors": [pubkey], "limit": 1})
    prof = {}
    if ok and ev:
        try:
            prof = json.loads(ev.get("content") or "{}")
            if not isinstance(prof, dict):
                prof = {}
        except ValueError:
            prof = {}
    if ok:
        _profile_cache[pubkey] = (time.monotonic(), prof)
    return prof


async def person(name: str, pubkey: str) -> dict:
    keys = await state.keypair(pubkey)
    return convert.person(base=config.base_url(), name=name, profile=await profile(pubkey),
                          public_key_pem=keys["pub"])


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


def signing(pubkey: str, keys: dict) -> tuple[str, str]:
    """(keyId, private PEM) for delivering as a member."""
    return f"{convert.actor_url(config.base_url(), name_of(pubkey))}#main-key", keys["priv"]

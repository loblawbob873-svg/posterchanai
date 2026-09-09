"""ONE answer to: *does this pubkey hold a NIP-05 identity THIS NODE granted, and has it published
it?* — consulted by every node-backed feature gate (AI chat, image generation, music generation,
Blossom uploads) so the entitlement follows the NIP-05 grant instead of being re-granted per user
per feature.

Why one module. Those four gates were four unrelated mechanisms — a `can_ai` column, a `can_image`
column, a `can_music` column and the shared `blossom_whitelist` setting — reconciled only by a
15-minute batch job (`relay_access_policy`). A member who signed in between two runs was refused
their own instance's AI, and a member who had never signed in had no `User` row for the batch to
grant anything to at all. This repo has been bitten repeatedly by hand-copied allowlists drifting
apart, so there is exactly one predicate here and the gates call it.

**THE PROFILE CLAIM IS NOT PROOF, AND READING IT AS PROOF IS A PRIVILEGE ESCALATION.** Anyone can
write `nip05: alice@poster.place` into their own kind-0 — it is unsigned-by-us, unverified text in a
document its subject controls. The entitlement therefore comes from `nostr_relay_nip05_names`, the
server-side registry THIS NODE writes when it grants a name; the profile is only ever the second
half of the test (`instance_membership`: the member must also have *published* the exact address the
node granted them), never the source of it. `is_granted()` below deliberately never opens a kind-0.

**THIS MODULE CAN ONLY EVER GRANT.** Every gate calls it as `<existing check> or <this>`, so a user
who passes today passes regardless of what happens here, and every failure path — settings not
hydrated, relay unreachable, membership check busy — resolves to False, i.e. to exactly the
behaviour that shipped before it existed. It must never be turned into a denial: an unreadable
registry would then lock every member out of their own instance.
"""
from __future__ import annotations

import logging
import threading
import time

from app.services import settings_store
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

# The server-side registry of names this node has granted: "<name> <npub-or-hex>" per line, the same
# setting the relay serves /.well-known/nostr.json from. Read raw, WITHOUT the relay's baked-in
# `_DEFAULT_NIP05_NAMES` fallback — a node that has never configured a registry grants nobody.
NAMES_SETTING = "nostr_relay_nip05_names"

# Master switch (Admin → Nostr Relay). Default ON.
ENABLED_SETTING = "nip05_grants_access"

# Positive-only cache of confirmed members, for the few gates that cannot await (see
# `is_member_cached`). Short, because it stands in for an authorization answer.
_MEMBER_TTL = 60.0
_members: dict = {}

_granted_cache = {"raw": None, "set": frozenset()}
_lock = threading.Lock()


def enabled() -> bool:
    """Whether a granted NIP-05 identity entitles its holder to this node's features.

    A BLANK stored value reads as ON, not off: `settings_store.get_bool` maps "" to False, and a
    blank row (an admin form saved before this key existed) would silently switch every member's
    access off node-wide with nothing anywhere to say so.
    """
    v = settings_store.get(ENABLED_SETTING, None)
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def granted_pubkeys() -> frozenset:
    """Hex pubkeys this node has granted a NIP-05 name to. Empty when the registry is unset or the
    settings cache has not hydrated — "I could not read the registry" degrades to "grant nothing
    extra", never to "deny", because every caller uses this additively."""
    raw = settings_store.get(NAMES_SETTING, "") or ""
    with _lock:
        if raw == _granted_cache["raw"]:
            return _granted_cache["set"]
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(raw, "")
    out = frozenset(pk.lower() for pk in names.values() if pk)
    with _lock:
        _granted_cache.update(raw=raw, set=out)
    return out


def is_granted(pubkey_hex: str) -> bool:
    """True iff THIS NODE granted this pubkey a NIP-05 name. Server-side registry only — this
    function never looks at a kind-0 profile, because the profile is written by the very account
    being authorized (see the module docstring)."""
    if not pubkey_hex:
        return False
    return pubkey_hex.lower() in granted_pubkeys()


def _remember(pubkey_hex: str) -> None:
    now = time.time()
    with _lock:
        _members[pubkey_hex] = now
        if len(_members) > 4096:
            for k in [k for k, t in _members.items() if now - t > _MEMBER_TTL]:
                _members.pop(k, None)


async def is_member(pubkey: str) -> bool:
    """True iff this node granted the pubkey a NIP-05 name AND its signed kind-0 publishes that
    exact address — the same predicate `relay_access_policy` reconciles on and the same one the
    other member-only surfaces (Mail, News, Git, Office, Files, Web Search) already require, so a
    member is a member everywhere.

    NEVER RAISES. `instance_membership.status` answers 503 for an unreachable relay, an unhydrated
    settings cache or a busy check, and every one of those must degrade to the pre-existing
    per-account flags rather than to a refusal.
    """
    if not pubkey or not enabled():
        return False
    pk = nostr_service.to_pubkey_hex(pubkey) or ""
    if not is_granted(pk):
        # Cheap sync pre-filter: the overwhelming majority of pubkeys hold no name here, and this
        # keeps them off the relay round trip entirely.
        return False
    try:
        from app.services import instance_membership
        result = await instance_membership.status(pk)
    except Exception as e:                       # 503s, cancelled relay reads, config churn
        logger.debug("[nip05-access] membership check unavailable for %s: %s", pk[:12], e)
        return False
    if result.get("qualified"):
        _remember(pk)
        return True
    return False


def is_member_cached(pubkey_hex: str) -> bool:
    """Sync peek for the gates that cannot await. TRUE only from a fresh confirmed answer; FALSE
    means "not known here", never "denied" — so a caller must keep its own check as the primary."""
    if not pubkey_hex or not enabled() or not is_granted(pubkey_hex):
        return False
    with _lock:
        ts = _members.get(pubkey_hex.lower())
    return bool(ts and time.time() - ts < _MEMBER_TTL)


def user_pubkey(user) -> str:
    npub = getattr(user, "nostr_npub", "") or ""
    if not npub:
        return ""
    try:
        return (nostr_service.to_pubkey_hex(npub) or "").lower()
    except Exception:
        return ""


async def user_is_member(user) -> bool:
    """`is_member` for a web account, via the Nostr key it linked."""
    pk = user_pubkey(user) if user is not None else ""
    return await is_member(pk) if pk else False


async def ai_allowed(user) -> bool:
    """THE AI gate. Admins pass; so does the admin-granted `can_ai` flag; so does a confirmed
    instance member. Five copies of the first two clauses used to live in five files
    (auth.get_ai_user, chat.py twice, websearch._require_ai, node_service.has_ai_access)."""
    if user is None:
        return False
    if getattr(user, "is_admin", False) or getattr(user, "can_ai", False):
        return True
    return await user_is_member(user)

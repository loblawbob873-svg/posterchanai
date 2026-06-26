"""Deterministic key derivation for the Nostr ↔ Fediverse bridge.

Every fediverse account the bridge mirrors gets a "puppet" Nostr keypair derived from a single
local secret (kept in the keystore) and the account's canonical ActivityPub actor URI:

    puppet_seckey = HMAC-SHA256(bridge_secret, actor_uri)        (re-tried on the ~never overflow)
    puppet_pubkey = BIP340-x-only(puppet_seckey)

Properties this buys us:
  - **Stable**: the same fedi account always maps to the same npub on this deployment, across
    restarts and even a full DB loss (only the secret must survive).
  - **No stored secrets**: the app re-derives a puppet's secret on demand to sign; nothing per-user
    is persisted but the pubkey (for bookkeeping).
  - **Self-validating at the relay**: a puppet event carries the actor URI in a `fedibridge` tag, so
    the relay re-derives the expected pubkey and accepts the event ONLY when it matches the signer.
    Since producing a valid signature for that pubkey requires the secret, no allowlist / no
    registration handshake is needed — forging a puppet post is infeasible without the secret.

Both the app (signer) and the relay subprocess (validator) import this module so they can never
disagree on the algorithm. Pure functions, no I/O.
"""

import hmac
import hashlib

from . import bip340

# secp256k1 group order — a valid secret key is in [1, n-1]. HMAC output landing outside is
# astronomically unlikely, but we re-derive deterministically if it ever does so the mapping stays total.
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# The event tag that carries the canonical actor URI a puppet event derives from.
ACTOR_TAG = "fedibridge"


def derive_seckey(secret: bytes, actor_uri: str) -> bytes:
    """HMAC(secret, actor_uri) → a valid secp256k1 secret scalar (32 bytes)."""
    msg = (actor_uri or "").encode("utf-8")
    sk = hmac.new(secret, msg, hashlib.sha256).digest()
    # Keep it in range deterministically; the loop effectively never runs.
    guard = 0
    while not (0 < int.from_bytes(sk, "big") < _N):
        guard += 1
        sk = hmac.new(secret, msg + bytes([guard]), hashlib.sha256).digest()
    return sk


def derive_pubkey(secret: bytes, actor_uri: str) -> str:
    """The x-only hex pubkey for a fedi actor's puppet."""
    return bip340.pubkey_from_seckey(derive_seckey(secret, actor_uri)).hex()


def actor_uri_of(ev: dict) -> str | None:
    """The canonical actor URI a puppet event claims to derive from (its `fedibridge` tag)."""
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == ACTOR_TAG and isinstance(t[1], str) and t[1]:
            return t[1]
    return None


def is_puppet_event(secret: bytes | None, ev: dict) -> bool:
    """True iff `ev` carries a `fedibridge` actor tag whose derived puppet pubkey equals the event's
    signer. (Caller has already verified the signature, so a match proves the secret-holder signed
    it.)"""
    if not secret:
        return False
    uri = actor_uri_of(ev)
    if not uri:
        return False
    try:
        return derive_pubkey(secret, uri) == ev.get("pubkey", "")
    except Exception:
        return False

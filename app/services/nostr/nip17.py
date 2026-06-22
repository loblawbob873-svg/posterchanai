"""NIP-17 private direct messages via NIP-59 gift wrap (server-side Python).

A private DM is a 3-layer onion: an unsigned **rumor** (kind 14, the actual message) → sealed
inside a **seal** (kind 13, NIP-44-encrypted by the sender, signed by the sender) → wrapped inside a
**gift wrap** (kind 1059, NIP-44-encrypted by a fresh EPHEMERAL key, signed by it, p-tagging the
recipient). Only the recipient can peel it; the wrap leaks no sender metadata. Mirrors the client
signer-worker's nip17wrap/unwrap so bot↔app DMs interoperate. Built on the existing nip44 + bip340 +
event modules.

Reference: NIP-17 + NIP-59.
"""
import json
import time
import hashlib
import secrets

from . import nip44, bip340, event as _event


def _rumor(sender_pk_hex: str, recipient_hex: str, text: str, created_at: int, extra_tags=None) -> dict:
    """An unsigned kind-14 chat 'rumor' (has an id, NO signature)."""
    tags = [["p", recipient_hex]] + (extra_tags or [])
    canonical = _event._canonical(sender_pk_hex, created_at, 14, tags, text)
    rid = hashlib.sha256(canonical).hexdigest()
    return {"id": rid, "pubkey": sender_pk_hex, "created_at": created_at,
            "kind": 14, "tags": tags, "content": text}


def _rand_past_ts(now: int) -> int:
    """A timestamp randomized up to 2 days in the past (NIP-59 — defeats timing correlation)."""
    return now - secrets.randbelow(2 * 86400)


def wrap(sender_sk: bytes, recipient_hex: str, text: str, extra_tags=None) -> dict:
    """Build a kind-1059 gift wrap addressed to `recipient_hex` carrying `text` from `sender_sk`."""
    recipient = bytes.fromhex(recipient_hex)
    now = int(time.time())
    sender_pk = bip340.pubkey_from_seckey(sender_sk).hex()
    rumor = _rumor(sender_pk, recipient_hex, text, now, extra_tags)
    # seal (kind 13): sender → recipient, signed by the sender
    seal_content = nip44.encrypt_to(sender_sk, recipient, json.dumps(rumor, separators=(",", ":")))
    seal = _event.build_event(sender_sk, 13, seal_content, tags=[], created_at=_rand_past_ts(now))
    # gift wrap (kind 1059): EPHEMERAL key → recipient, p-tagging the recipient
    eph_sk = secrets.token_bytes(32)
    wrap_content = nip44.encrypt_to(eph_sk, recipient, json.dumps(seal, separators=(",", ":")))
    return _event.build_event(eph_sk, 1059, wrap_content,
                              tags=[["p", recipient_hex]], created_at=_rand_past_ts(now))


def unwrap(recipient_sk: bytes, wrap_event: dict):
    """Peel a kind-1059 gift wrap addressed to us → (sender_pubkey_hex, text, rumor).
    Raises on a decryption failure or a seal/rumor author mismatch (forgery guard)."""
    wrap_pk = bytes.fromhex(wrap_event["pubkey"])
    seal = json.loads(nip44.decrypt_from(recipient_sk, wrap_pk, wrap_event["content"]))
    seal_pk = bytes.fromhex(seal["pubkey"])
    rumor = json.loads(nip44.decrypt_from(recipient_sk, seal_pk, seal["content"]))
    # the seal must be signed by the rumor's claimed author, else the sender is forged
    if rumor.get("pubkey") != seal.get("pubkey"):
        raise ValueError("nip17: seal/rumor author mismatch")
    return rumor["pubkey"], rumor.get("content", ""), rumor

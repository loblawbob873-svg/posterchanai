"""Nostr event construction, signing and verification (NIP-01).

An event id is sha256 over the canonical JSON array
[0, pubkey, created_at, kind, tags, content]; the signature is BIP340 Schnorr
over that id. NIP-10 e/p tag helpers and a NIP-92 imeta builder live here too.
"""

import json
import time
import hashlib

from . import bip340


def _canonical(pubkey_hex: str, created_at: int, kind: int, tags: list, content: str) -> bytes:
    # NIP-01 requires no whitespace and UTF-8; ensure_ascii=False keeps emoji/handles intact.
    arr = [0, pubkey_hex, created_at, kind, tags, content]
    return json.dumps(arr, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_event(seckey: bytes, kind: int, content: str,
                tags: list | None = None, created_at: int | None = None) -> dict:
    """Build, id and sign a Nostr event. Returns the full event dict ready to publish."""
    tags = tags or []
    created_at = int(created_at if created_at is not None else time.time())
    pubkey_hex = bip340.pubkey_from_seckey(seckey).hex()
    serialized = _canonical(pubkey_hex, created_at, kind, tags, content)
    event_id = hashlib.sha256(serialized).hexdigest()
    sig = bip340.sign(bytes.fromhex(event_id), seckey)
    return {
        "id": event_id,
        "pubkey": pubkey_hex,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig.hex(),
    }


def verify_event(event: dict) -> bool:
    """Recompute the id and verify the signature of an event dict."""
    try:
        serialized = _canonical(
            event["pubkey"], int(event["created_at"]), int(event["kind"]),
            event["tags"], event["content"],
        )
        if hashlib.sha256(serialized).hexdigest() != event["id"]:
            return False
        return bip340.verify(
            bytes.fromhex(event["id"]),
            bytes.fromhex(event["pubkey"]),
            bytes.fromhex(event["sig"]),
        )
    except (KeyError, ValueError, TypeError):
        return False


def verify_self_auth(auth_b64: str, pubkey_hex: str) -> bool:
    """Does `auth_b64` prove the caller holds the key for `pubkey_hex`, right now?

    A base64 Nostr event, signed by that pubkey, stamped within a 5-minute window — the standard
    "prove you own this npub" handshake for an endpoint that takes a pubkey as input and would
    otherwise trust it. Without a check like this, anyone can name anyone else's key.

    The window is what makes a captured proof useless a few minutes later; the signature is what
    makes one impossible to forge in the first place.
    """
    import base64
    try:
        ev = json.loads(base64.b64decode(auth_b64))
    except Exception:
        return False
    return (verify_event(ev) and ev.get("pubkey") == pubkey_hex
            and abs(int(ev.get("created_at", 0)) - int(time.time())) <= 300)


def reply_tags(parent: dict, root_id: str | None = None) -> list:
    """Build NIP-10 e/p tags for a reply to `parent`.

    Marks the root and the immediate reply, and p-tags the parent author plus any
    authors carried forward from the parent's p tags (so the whole thread is notified).
    """
    parent_id = parent.get("id")
    parent_author = parent.get("pubkey")
    # Carry the root forward: prefer an explicit root tag on the parent, else the parent is root.
    existing_root = None
    for t in parent.get("tags", []):
        if len(t) >= 4 and t[0] == "e" and t[3] == "root":
            existing_root = t[1]
            break
    root = root_id or existing_root or parent_id
    tags: list = []
    if root and root != parent_id:
        tags.append(["e", root, "", "root"])
        tags.append(["e", parent_id, "", "reply"])
    else:
        tags.append(["e", parent_id, "", "root"])
    # p-tag the parent author + carried-forward participants (deduped).
    seen = set()
    authors = [parent_author] + [t[1] for t in parent.get("tags", []) if len(t) >= 2 and t[0] == "p"]
    for a in authors:
        if a and a not in seen:
            seen.add(a)
            tags.append(["p", a])
    return tags


def imeta_tag(url: str, mime: str = "", sha256: str = "", dim: str = "") -> list:
    """NIP-92 imeta tag describing an inline media URL."""
    parts = [f"url {url}"]
    if mime:
        parts.append(f"m {mime}")
    if dim:
        parts.append(f"dim {dim}")
    if sha256:
        parts.append(f"x {sha256}")
    return ["imeta"] + parts

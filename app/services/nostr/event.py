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


def invalid_reason(event: dict) -> str | None:
    """None for a valid event, else a NIP-01 `invalid:` message naming WHICH part is wrong: an id that does
    not match the content (the event was changed after signing -- re-serialized on its way here) is a
    different fault, in different software, from a signature that does not verify. No content is included."""
    try:
        serialized = _canonical(
            event["pubkey"], int(event["created_at"]), int(event["kind"]),
            event["tags"], event["content"],
        )
        if hashlib.sha256(serialized).hexdigest() != event["id"]:
            return "invalid: event id does not match its content (changed after it was signed)"
        if not bip340.verify(bytes.fromhex(event["id"]), bytes.fromhex(event["pubkey"]), bytes.fromhex(event["sig"])):
            return "invalid: bad signature"
        return None
    except (KeyError, ValueError, TypeError):
        return "invalid: malformed event"


def _ours(ev: dict, pubkey_hex: str) -> bool:
    """Is this kind-27235 shaped like a proof made FOR THIS APP, rather than a NIP-98 token made for
    somebody else's server?

    Kind 27235 is NIP-98's HTTP-auth kind, so the same key signs them all day for other services: a
    NIP-96 upload to a media host, a git push through ngit, any site asking a NIP-46 signer. Those carry
    the target URL and method (`u`/`method` tags) and, per NIP-98, an EMPTY content. With freshness,
    signature and pubkey the only checks, any one of them -- handed to that other server, which may be
    hostile -- was a working proof here for five minutes: delete the account, mint the AI-files cookie,
    take the push subscription, link a Google account. Every proof this app's clients make (web,
    desktop, the APK's Java) has the opposite shape, and has since July: a non-empty content naming the
    action and a `p` tag naming the signer, never a `u`. So, where the caller names no purpose, a
    `u`/`method` tag, an empty content or a missing self-`p` is refused. A caller that names a purpose
    already demands that exact content, which no foreign token carries."""
    tags = ev.get("tags")
    if not isinstance(tags, list) or not ev.get("content"):
        return False
    own = False
    for t in tags:
        if not isinstance(t, list) or not t:
            continue
        if t[0] in ("u", "method"):
            return False
        if t[0] == "p" and len(t) >= 2 and t[1] == pubkey_hex:
            own = True
    return own


def verify_self_auth(auth_b64: str, pubkey_hex: str, purpose: str | None = None) -> bool:
    """Does `auth_b64` prove the caller holds the key for `pubkey_hex`, right now?

    Only an explicit kind-27235 proof is accepted. Public notes, metadata and
    relay events are replayable public data, not evidence of key possession.
    Cached self-auth proofs remain short-lived bearer capabilities; sensitive
    exchanges additionally bind the content to their requested purpose.
    """
    import base64
    if not isinstance(auth_b64, str) or len(auth_b64) > 8192:
        return False
    try:
        ev = json.loads(base64.b64decode(auth_b64))
    except Exception:
        return False
    ok = (isinstance(ev, dict) and ev.get("kind") == 27235 and ev.get("content")
          and (purpose is None or ev.get("content") == purpose)
          and (purpose is not None or _ours(ev, pubkey_hex))
          and verify_event(ev) and ev.get("pubkey") == pubkey_hex
          and abs(int(ev.get("created_at", 0)) - int(time.time())) <= 300)
    if not ok:
        return False
    # A key on the relay's block list proves nothing here: every signed request this app serves (files,
    # sync, drafts, media tools, push, NIP-05 claims, login) asks this function.
    try:
        from app.services import relay_blocklist
        return not relay_blocklist.is_blocked(pubkey_hex)
    except Exception:
        return True


def comment_tags(parent: dict) -> list:
    """NIP-22 tags for a comment (kind 1111) answering the kind-1111 comment `parent`: the root scope
    (the uppercase E/A/I/K/P tags) carried forward unchanged, and the parent itself in lowercase.

    Clients -- this one included -- send every reply to a note as a kind-1111 comment, so a bot that
    answered one with a NIP-10 kind-1 reply put its answer outside the thread the question lives in."""
    tags = [list(t) for t in parent.get("tags", [])
            if len(t) >= 2 and t[0] in ("E", "A", "I", "K", "P")]
    tags += [["e", parent.get("id"), "", parent.get("pubkey", "")], ["k", "1111"]]
    if parent.get("pubkey"):
        tags.append(["p", parent["pubkey"]])
    return tags


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

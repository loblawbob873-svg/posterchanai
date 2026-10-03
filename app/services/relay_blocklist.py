"""The relay's blocked-accounts list (`nostr_relay_blocked_pubkeys`): one place that reads and edits it.

It was a bare textarea of npubs -- 461 of them on poster.place, no names, four lines tall, and not
searchable with the browser's Ctrl+F (find does not look inside a text box). An account blocked by a
stray click on "🚫 Block author" could not be found again to unblock: the admin was told "it's on line
64" of a list nobody can read. Admin → Relay now lists the entries WITH their names and pictures and
an Unblock per row, and the client's ⋯ menu offers Unblock for a blocked author -- both through here,
the same read-modify-write `/client/block` always did.
"""
from __future__ import annotations

import json
import logging

from app.services import settings_store
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

KEY = "nostr_relay_blocked_pubkeys"


def blocked_hex() -> list:
    """The list as canonical hex, in stored order, duplicates and unreadable tokens dropped."""
    out, seen = [], set()
    for tok in (settings_store.get(KEY, "") or "").replace(",", "\n").split():
        h = nostr_service.to_pubkey_hex(tok.strip())
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h.lower())
    return out


_cache: tuple = ("\0", frozenset())


def blocked_set() -> frozenset:
    """The list as a set of lowercase hex, re-parsed only when the stored text changes -- this is asked
    on every authenticated request, and decoding ~500 npubs each time would cost more than the request."""
    global _cache
    raw = settings_store.get(KEY, "") or ""
    if raw != _cache[0]:
        _cache = (raw, frozenset(blocked_hex()))
    return _cache[1]


def is_blocked(pubkey) -> bool:
    """Is this account on the relay's block list? Takes hex or an npub. The ONE question every gate on
    the platform asks ("we need to make sure that blocked users can't do anything on the platform
    despite having a nip05 in their profile"). A list that cannot be read answers False -- what the gate
    did before this check existed -- rather than locking every account out."""
    if not pubkey:
        return False
    try:
        pk = nostr_service.to_pubkey_hex(str(pubkey).strip()) or ""
        return bool(pk) and pk.lower() in blocked_set()
    except Exception:
        return False


def is_user_blocked(user) -> bool:
    """A web account is blocked when the Nostr key it is linked to is."""
    return bool(user) and is_blocked(getattr(user, "nostr_npub", None) or "")


async def set_blocked(db, target_hex: str, blocked: bool) -> dict:
    """Add or remove one key, store the list as npubs and re-apply it on the running relay.
    {"ok", "blocked", "count"} or {"ok": False, "error", "status"}.

    THE LIST IS WRITTEN TO THE RELAY BEFORE THE RELAY IS TOLD TO RELOAD IT, AND A WRITE THAT DID NOT
    LAND IS AN ERROR. This used to be `settings_store.put` (a background writer that retries) followed at
    once by `trigger_block_reload()`. The relay process reads the list from its own event store, so the
    reload raced the write: on 2026-10-02 the reload ran at 18:00:45, re-read the OLD 489-key list, and
    the write (which had timed out twice) landed at 18:01:04 with nothing to reload it. The admin was
    told "blocked", the npub was in Admin -> Relay, and the relay kept accepting that author's posts,
    DMs, games and a git issue for as long as it ran.
    """
    target = (target_hex or "").lower()
    if blocked:
        # Never the node's own operator/bot keys: that rejects the operator's signup-follow events and
        # breaks new-account admission, with no in-app way back.
        from app.services.blossom_service import _operator_pubkeys
        if target in _operator_pubkeys(db):
            return {"ok": False, "error": "refusing to block an operator key"}
    cur = set(blocked_hex())
    if blocked:
        cur.add(target)
    else:
        cur.discard(target)
    out = []
    for h in sorted(cur):
        try:
            out.append(nostr_service.npub_of(h))
        except Exception:
            out.append(h)
    value = "\n".join(out)
    previous = settings_store.get(KEY, None)
    settings_store.put(KEY, value, write_relay=False)
    try:
        wrote = await settings_store.write_through(db, {KEY: value})
    except Exception as e:
        logger.warning("[relay-blocklist] durable write failed: %s", e)
        wrote = 0
    if not wrote:
        # The cache must not claim a list the relay never stored, or the UI shows the block and the
        # next restart (which hydrates from the relay) quietly drops it.
        settings_store.restore_cached(KEY, previous)
        return {"ok": False, "status": 503,
                "error": "the relay did not save the block list -- nothing changed, try again"}
    try:
        from app.services.nostr_relay.thread import trigger_block_reload
        trigger_block_reload()
    except Exception as e:
        logger.warning("[relay-blocklist] reload failed: %s", e)
    return {"ok": True, "blocked": blocked, "count": len(cur)}


async def profiles(pubkeys: list) -> tuple:
    """({hex: {"name", "picture", "nip05"}}, read_ok) from the profiles on this node's relay. A key
    with no profile here is simply absent -- the caller still lists it (by npub), never hides it."""
    from app.services import nostr_store
    found: dict = {}
    ok = True
    for i in range(0, len(pubkeys), 400):
        chunk = pubkeys[i:i + 400]
        try:
            evs = await nostr_store._ws_query(settings_store._port(),
                                              [{"kinds": [0], "authors": chunk, "limit": len(chunk) * 2}],
                                              strict=True)
        except Exception:
            ok = False
            continue
        for ev in sorted(evs or [], key=lambda e: e.get("created_at", 0)):
            try:
                c = json.loads(ev.get("content") or "{}")
            except (ValueError, TypeError):
                continue
            if not isinstance(c, dict):
                continue
            found[ev.get("pubkey")] = {
                "name": str(c.get("display_name") or c.get("name") or "")[:80],
                "picture": str(c.get("picture") or "") if str(c.get("picture") or "").startswith("https://") else "",
                "nip05": str(c.get("nip05") or "")[:120],
            }
    return found, ok

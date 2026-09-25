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


def set_blocked(db, target_hex: str, blocked: bool) -> dict:
    """Add or remove one key, store the list as npubs and re-apply it on the running relay.
    {"ok", "blocked", "count"} or {"ok": False, "error"}."""
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
    settings_store.put(KEY, "\n".join(out))
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

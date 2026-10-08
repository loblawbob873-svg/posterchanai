"""The NIP-05 identities this node grants, as rows a person can read -- Admin → Relay → Identities.

The registry is one text setting (`nostr_relay_nip05_names`, "<name> <npub-or-hex>" per line). As a
textarea it answers nothing an admin actually asks -- WHO is `jonnyfever`, does their profile really
publish the address, which line is the bot -- so, like the blocked-accounts list, it is drawn with the
profile from this relay beside every name. `verified` is the same test the NIP-05 entitlement uses
(nip05_access): the account's OWN kind-0 publishes exactly `name@domain`. A name whose owner has no
profile here is still listed; nothing is hidden because a read came back short.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

KEY = "nostr_relay_nip05_names"


def _names() -> dict:
    from app.services import settings_store
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(settings_store.get(KEY, "") or "", "")
    return names


def address(name: str, domain: str) -> str:
    return f"{name}@{domain}" if domain else name


# "Signed up and never did anything" (2026-10-08). What signing up writes BY ITSELF -- the profile, the contact
# list the operator-follow creates, relay/DM-relay/media-server lists -- is not activity; everything else a key
# authors here is. POSTS are the social kinds a person would call posting.
SIGNUP_KINDS = (0, 3, 10002, 10050, 10063)
POST_KINDS = (1, 6, 7, 16, 20, 21, 22, 40, 41, 42, 1063, 1068, 1018, 1111, 1984, 9802, 30023, 30311, 34550)


def activity(pubkeys: list) -> dict | None:
    """BLOCKING: {pubkey: {"posts", "events", "last_post", "last_event", "first_seen"}} from the relay's own
    Postgres, one grouped query for the whole list. None when the relay could not be asked -- which is never
    the same answer as "no activity", or every member would read as inactive the moment Postgres hiccups."""
    if not pubkeys:
        return {}
    try:
        import psycopg2
        from app.services.stats_bot_service import _relay_dsn
        conn = psycopg2.connect(_relay_dsn(), connect_timeout=10)
    except Exception as e:
        logger.warning("[nip05] activity: relay database unreachable: %s", type(e).__name__)
        return None
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = 20000")
        cur.execute(
            "SELECT pubkey, "
            " count(*) FILTER (WHERE kind = ANY(%s)), "
            " count(*) FILTER (WHERE NOT (kind = ANY(%s))), "
            " max(created_at) FILTER (WHERE kind = ANY(%s)), "
            " max(created_at) FILTER (WHERE NOT (kind = ANY(%s))), "
            " min(created_at) "
            "FROM events WHERE pubkey = ANY(%s) GROUP BY pubkey",
            (list(POST_KINDS), list(SIGNUP_KINDS), list(POST_KINDS), list(SIGNUP_KINDS), list(pubkeys)))
        out = {pk: {"posts": 0, "events": 0, "last_post": None, "last_event": None, "first_seen": None}
               for pk in pubkeys}
        for pk, posts, events, last_post, last_event, first in cur.fetchall():
            out[pk] = {"posts": int(posts or 0), "events": int(events or 0), "last_post": last_post,
                       "last_event": last_event, "first_seen": first}
        return out
    except Exception as e:
        logger.warning("[nip05] activity: query failed: %s", type(e).__name__)
        return None
    finally:
        conn.close()


async def rows(domain: str) -> dict:
    from app.services import relay_blocklist
    from app.services.nostr import nostr_service
    names = _names()
    found, names_ok = await relay_blocklist.profiles(sorted(set(names.values())))
    import asyncio
    acts = await asyncio.to_thread(activity, sorted(set(names.values())))
    # SEVERAL NAMES, ONE PROFILE. A key may hold more than one name here, and a kind-0 carries ONE
    # `nip05` -- so judged per name, every extra name read "not in profile", and "Remove all not in
    # profile" would revoke them all. A name verifies when its owner's profile publishes ANY address
    # this node granted that key; `via` says which, when it is another name.
    accepted_by_pk: dict = {}
    for name, pk in names.items():
        acc = accepted_by_pk.setdefault(pk, set())
        acc.add(address(name, domain).lower())
        if name == "_" and domain:
            acc.add(domain.lower())             # the root identity may be published as just "domain"
    out = []
    for name, pk in sorted(names.items(), key=lambda kv: kv[0].lower()):
        p = found.get(pk) or {}
        try:
            npub = nostr_service.npub_of(pk)
        except Exception:
            npub = pk
        addr = address(name, domain)
        claimed = (p.get("nip05") or "").strip().lower()
        own = {addr.lower()} | ({domain.lower()} if name == "_" and domain else set())
        verified = claimed in accepted_by_pk.get(pk, set())
        out.append({"name": name, "address": addr, "pubkey": pk, "npub": npub,
                    "display": p.get("name", ""), "picture": p.get("picture", ""),
                    "profile_nip05": p.get("nip05", ""),
                    "verified": verified,
                    "via": (p.get("nip05", "") if verified and claimed not in own else ""),
                    "others": sorted(n for n, k in names.items() if k == pk and n != name),
                    "activity": (acts or {}).get(pk) if acts is not None else None})
    return {"identities": out, "names_complete": names_ok, "domain": domain, "activity_complete": acts is not None}


def remove(name: str) -> dict:
    """Take ONE name out of the registry and apply it live. Refuses while settings are not loaded:
    an unloaded read is "", and writing back "" minus one line would wipe every other identity."""
    from app.services import settings_store
    if not settings_store.is_hydrated():
        return {"ok": False, "error": "settings are still loading — try again in a moment"}
    raw = settings_store.get(KEY, "") or ""
    want = (name or "").strip().lower()
    kept, removed, owners = [], 0, set()
    for ln in raw.split("\n"):
        s = ln.strip()
        if s and not s.startswith("#"):
            toks = s.replace("=", " ").replace(",", " ").split()
            if len(toks) >= 2 and toks[0].lower() == want:
                removed += 1
                owners.add(toks[1])
                continue
        kept.append(ln)
    if not removed:
        return {"ok": False, "error": f"no identity named {name!r}"}
    settings_store.put(KEY, "\n".join(l for l in kept if l.strip()))
    try:
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception:
        pass
    return {"ok": True, "removed": removed, "value": settings_store.get(KEY, "") or "",
            "orphaned": orphaned(owners)}


def remove_keys(pubkeys) -> dict:
    """Take EVERY name held by any of `pubkeys` out of the registry in ONE write and apply it live -- the
    "Remove, unfollow & block" action retires the key, so it keeps none of its names. Same refusal as
    remove(): an unloaded read is "", and "" minus some lines written back would wipe every identity."""
    from app.services import settings_store
    from app.services.nostr import nostr_service
    if not settings_store.is_hydrated():
        return {"ok": False, "error": "settings are still loading — try again in a moment"}
    want = {str(p).lower() for p in pubkeys or ()}
    raw = settings_store.get(KEY, "") or ""
    kept, names, owners = [], [], set()
    for ln in raw.split("\n"):
        s = ln.strip()
        if s and not s.startswith("#"):
            toks = s.replace("=", " ").replace(",", " ").split()
            if len(toks) >= 2:
                try:
                    h = (nostr_service.to_pubkey_hex(toks[1]) or "").lower()
                except Exception:
                    h = ""
                if h in want:
                    names.append(toks[0])
                    owners.add(toks[1])
                    continue
        kept.append(ln)
    if not names:
        return {"ok": True, "removed": 0, "names": [], "value": raw, "orphaned": []}
    settings_store.put(KEY, "\n".join(l for l in kept if l.strip()))
    try:
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception:
        pass
    return {"ok": True, "removed": len(names), "names": names, "value": settings_store.get(KEY, "") or "",
            "orphaned": orphaned(owners)}


def orphaned(owner_tokens) -> list:
    """The keys among `owner_tokens` that hold NO name in the registry any more -- the ones whose
    access should go. A key that still has another name here is still a member."""
    from app.services.nostr import nostr_service
    still = set(_names().values())
    out = set()
    for t in owner_tokens or ():
        try:
            h = nostr_service.to_pubkey_hex(str(t).strip())
        except Exception:
            h = None
        if h and h not in still:
            out.add(h)
    return sorted(out)



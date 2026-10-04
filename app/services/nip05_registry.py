"""The NIP-05 identities this node grants, as rows a person can read -- Admin → Relay → Identities.

The registry is one text setting (`nostr_relay_nip05_names`, "<name> <npub-or-hex>" per line). As a
textarea it answers nothing an admin actually asks -- WHO is `jonnyfever`, does their profile really
publish the address, which line is the bot -- so, like the blocked-accounts list, it is drawn with the
profile from this relay beside every name. `verified` is the same test the NIP-05 entitlement uses
(nip05_access): the account's OWN kind-0 publishes exactly `name@domain`. A name whose owner has no
profile here is still listed; nothing is hidden because a read came back short.
"""
from __future__ import annotations

KEY = "nostr_relay_nip05_names"


def _names() -> dict:
    from app.services import settings_store
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(settings_store.get(KEY, "") or "", "")
    return names


def address(name: str, domain: str) -> str:
    return f"{name}@{domain}" if domain else name


async def rows(domain: str) -> dict:
    from app.services import relay_blocklist
    from app.services.nostr import nostr_service
    names = _names()
    found, names_ok = await relay_blocklist.profiles(sorted(set(names.values())))
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
                    "others": sorted(n for n, k in names.items() if k == pk and n != name)})
    return {"identities": out, "names_complete": names_ok, "domain": domain}


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


async def remove_unverified(domain: str, shown: list) -> dict:
    """Revoke, in ONE write, every identity the admin was shown as "not in profile" that STILL does
    not verify. Two things make this safe to be a single button:

    * it refuses outright when any profile could not be read. `rows()` still lists such a name, but
      an unread profile looks exactly like one that does not publish the address -- read as proof,
      one relay timeout would revoke every member of the node at once;
    * it removes only the intersection of what was SHOWN and what fails now, so a member who fixed
      their profile after the page loaded keeps their name, and a name granted since is untouched."""
    from app.services import settings_store
    if not settings_store.is_hydrated():
        return {"ok": False, "retry": True, "error": "settings are still loading — try again in a moment"}
    cur = await rows(domain)
    if not cur.get("names_complete"):
        return {"ok": False, "retry": True,
                "error": "some profiles could not be read from the relay just now — nothing was removed; try again"}
    shown_l = {str(n or "").strip().lower() for n in (shown or []) if str(n or "").strip()}
    doomed = {r["name"].lower() for r in cur["identities"] if not r["verified"] and r["name"].lower() in shown_l}
    if not doomed:
        return {"ok": True, "removed": 0, "names": [], "value": settings_store.get(KEY, "") or "", "orphaned": []}
    raw = settings_store.get(KEY, "") or ""
    kept, gone, owners = [], [], set()
    for ln in raw.split("\n"):
        s = ln.strip()
        if s and not s.startswith("#"):
            toks = s.replace("=", " ").replace(",", " ").split()
            if len(toks) >= 2 and toks[0].lower() in doomed:
                gone.append(toks[0])
                owners.add(toks[1])
                continue
        kept.append(ln)
    settings_store.put(KEY, "\n".join(l for l in kept if l.strip()))
    try:
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception:
        pass
    return {"ok": True, "removed": len(gone), "names": gone, "value": settings_store.get(KEY, "") or "",
            "orphaned": orphaned(owners)}

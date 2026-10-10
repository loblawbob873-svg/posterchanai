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


_T_MAX = 2 ** 31 - 1         # the relay's created_at column is a 32-bit integer; never ask past it
_HEAD = len(SIGNUP_KINDS) + 1   # the signup kinds are replaceable: one each at most, so one more is a non-signup


def _chunks(seq, n):
    seq = list(seq)
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _operator_key():
    """THIS node's operator key, or None -- read, never minted (settings_store._operator_seckey mints one)."""
    try:
        from app.services import keystore
        from app.services.nostr import nostr_service
        nsec = keystore.get_operator_nsec()
        return nostr_service.decode_seckey(nsec) if nsec else None
    except Exception:
        return None


def _req_per_key(filters_by_pk: dict) -> dict:
    """{pk: events} for {pk: filter}, ten filters per REQ (the relay's per-REQ cap -- more are dropped, silently)."""
    from app.services import relay_reader
    out = {pk: [] for pk in filters_by_pk}
    for part in _chunks(filters_by_pk.items(), 10):
        for ev in relay_reader.query([f for _pk, f in part], timeout=20):
            pk = ev.get("pubkey")
            if pk in out:
                out[pk].append(ev)
    return out


def _bisect(preds: dict, count) -> dict:
    """Binary searches over created_at, every key's step answered by ONE batch of relay COUNTs per round.

    `preds` is {key: (lo, hi, make)}, where make(T) -> (filters, test(counts) -> bool) and the predicate is TRUE at
    lo and FALSE at hi (monotone between). Returns {key: the largest T with the predicate true}."""
    state = {k: [lo, hi, make] for k, (lo, hi, make) in preds.items()}
    while True:
        live = [st for st in state.values() if st[1] - st[0] > 1]
        if not live:
            return {k: st[0] for k, st in state.items()}
        batch, plan = [], []
        for st in live:
            mid = (st[0] + st[1]) // 2
            flts, test = st[2](mid)
            plan.append((st, mid, test, len(batch), len(flts)))
            batch.extend(flts)
        got = count(batch)
        for st, mid, test, at, n in plan:
            if test(got[at:at + n]):
                st[0] = mid
            else:
                st[1] = mid


def activity(pubkeys: list) -> dict | None:
    """BLOCKING: {pubkey: {"posts", "events", "last_post", "last_event", "first_seen"}} as THIS node's relay
    answers its OPERATOR (#161: no SQL against the relay's tables). None when the relay could not be asked --
    which is never the same answer as "no activity", or every member would read as inactive the moment it
    hiccups, and Admin offers inactive accounts for removal.

    Every number counts PRIVATE documents too (kinds 78/30078: Notes, Budget, the files index...): a member who
    only ever writes Notes is active. The relay serves those to nobody but their author, so they are COUNTED --
    NIP-45, signed in (NIP-42) as this node's operator key, the one socket the relay counts them for -- and never
    read. The two dates a count cannot give directly (the newest event that is not a signup kind; the oldest
    event) are found by binary search over those same counts with since/until. A relay that does not give the
    operator count (no operator key here, an older relay, a refused sign-in) is "could not ask"."""
    if not pubkeys:
        return {}
    import time
    from app.services import relay_reader
    pks = list(dict.fromkeys(pubkeys))
    out = {pk: {"posts": 0, "events": 0, "last_post": None, "last_event": None, "first_seen": None}
           for pk in pks}
    sk = _operator_key()
    if not sk:
        logger.warning("[nip05] activity: no operator key — cannot count private documents")
        return None

    def count(flts):
        return relay_reader.counts(flts, timeout=20, auth_seckey=sk, private=True)
    try:
        now = int(time.time()) + 1
        flts = []
        for pk in pks:
            flts += [{"authors": [pk], "kinds": list(POST_KINDS)}, {"authors": [pk]},
                     {"authors": [pk], "kinds": list(SIGNUP_KINDS)},
                     {"authors": [pk], "since": now}, {"authors": [pk], "kinds": list(SIGNUP_KINDS), "since": now}]
        got = count(flts)
        total, future = {}, {}
        for i, pk in enumerate(pks):
            posts, everything, signup, later, later_signup = got[5 * i:5 * i + 5]
            total[pk] = everything
            future[pk] = later - later_signup > 0
            out[pk]["posts"] = posts
            out[pk]["events"] = max(0, everything - signup)
        have = [pk for pk in pks if total[pk] > 0]
        newest = _req_per_key({pk: {"authors": [pk], "kinds": list(POST_KINDS), "limit": 1}
                               for pk in pks if out[pk]["posts"] > 0})
        for pk, evs in newest.items():
            if evs:
                out[pk]["last_post"] = max(int(e.get("created_at", 0)) for e in evs)
        # The newest events a client may SEE bound both searches; private documents are withheld from any REQ,
        # so this page never decides a date on its own unless it provably holds every counted event.
        heads = _req_per_key({pk: {"authors": [pk], "limit": _HEAD} for pk in have})
        preds = {}
        for pk in have:
            seen = [int(e.get("created_at", 0)) for e in heads.get(pk, []) if e.get("pubkey") == pk]
            other = [int(e.get("created_at", 0)) for e in heads.get(pk, [])
                     if e.get("pubkey") == pk and int(e.get("kind", -1)) not in SIGNUP_KINDS]
            if out[pk]["events"] > 0:
                def make_last(t, pk=pk):
                    return ([{"authors": [pk], "since": t}, {"authors": [pk], "kinds": list(SIGNUP_KINDS), "since": t}],
                            lambda c: c[0] - c[1] > 0)
                lo = max(other) if other else 0
                hi = _T_MAX + 1 if future[pk] else max(now, lo + 1)
                preds[("last", pk)] = (lo, hi, make_last)
            if seen and len(seen) >= total[pk]:
                out[pk]["first_seen"] = min(seen)      # the page held every event the relay counts
            else:
                # Largest T with NOTHING at or before it; the oldest event is T + 1.
                def make_first(t, pk=pk):
                    return [{"authors": [pk], "until": t}], (lambda c: c[0] == 0)
                preds[("first", pk)] = (-1, min(seen) if seen else _T_MAX, make_first)
        for (what, pk), t in _bisect(preds, count).items():
            if what == "last":
                out[pk]["last_event"] = t
            else:
                out[pk]["first_seen"] = t + 1
        return out
    except relay_reader.Unavailable as e:
        logger.warning("[nip05] activity: the relay could not be asked: %s", type(e).__name__)
        return None


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



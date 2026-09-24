"""What this node's community is doing, for the bots that post about it (the block bot, the daily
top posts and active-user counts).

These used to be read straight out of the retired Pleroma instance's own database. The same facts
now live in two places, and both are read here:

  * on the fediverse, a Block of one of our users arrives at the ActivityPub inbox and is recorded
    (activitypub.state.record_block);
  * on Nostr, a block is a PUBLIC mute list (kind 10000). A list BY one of ours, or one that names one
    of ours, is a block involving this community. Only its public `p` tags are read -- the encrypted
    half is private by design.

Activity is measured on the relay: who of ours published, and which of their posts drew reactions,
reposts and replies. Everything is read-only; nothing here writes.
"""
from __future__ import annotations

import time
from urllib.parse import urlparse

from app.services import nostr_store, settings_store
from app.services.nostr import bech32


def _port() -> int:
    return settings_store._port()


def members() -> dict:
    """{pubkey: "@name@domain"} for every name this node granted (the NIP-05 registry)."""
    from app.services.activitypub import actors, config
    domain = config.domain()
    by_name, _by_pk = actors._registry()
    return {pk: (f"@{name}@{domain}" if domain else f"@{name}") for name, pk in by_name.items() if pk}


def _npub(pk: str) -> str:
    try:
        return bech32.encode("npub", bytes.fromhex(pk))
    except Exception:
        return pk


def handle(pk: str, known: dict | None = None) -> str:
    """How to NAME an account in a post: a member's address, a fediverse account's `@user@host`
    (its puppet), else a `nostr:npub…` reference every Nostr client renders as a profile link."""
    known = members() if known is None else known
    if pk in known:
        return known[pk]
    try:
        from app.database import SessionLocal
        from app.models import FediPuppet
        db = SessionLocal()
        try:
            row = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == pk).first()
            if row and row.acct:
                return "@" + row.acct.lstrip("@")
        finally:
            db.close()
    except Exception:
        pass
    return "nostr:" + _npub(pk)


async def _query(filters: list) -> list:
    return await nostr_store._ws_query(_port(), filters, timeout=10.0, strict=True)


async def mute_relations(known: dict | None = None) -> list:
    """[{blocker, blocked, at, via:"nostr"}] -- public mutes by one of ours, or of one of ours."""
    known = members() if known is None else known
    pks = sorted(known)
    if not pks:
        return []
    lists = await _query([{"kinds": [10000], "authors": pks, "limit": len(pks) + 10}])
    lists += await _query([{"kinds": [10000], "#p": pks, "limit": 5000}])
    newest: dict = {}
    for ev in lists:                               # a replaceable list: only its newest version counts
        cur = newest.get(ev.get("pubkey"))
        if cur is None or ev.get("created_at", 0) > cur.get("created_at", 0):
            newest[ev.get("pubkey")] = ev
    out, seen = [], set()
    for author, ev in newest.items():
        for t in ev.get("tags") or []:
            if len(t) < 2 or t[0] != "p" or not isinstance(t[1], str) or len(t[1]) != 64 or t[1] == author:
                continue
            if author not in known and t[1] not in known:
                continue
            if (author, t[1]) in seen:
                continue
            seen.add((author, t[1]))
            out.append({"blocker": author, "blocked": t[1], "at": int(ev.get("created_at") or 0), "via": "nostr"})
    return out


async def blocks() -> list:
    """Every block involving this community, from both sides, each with printable names."""
    from app.services.activitypub import actors, state
    known = members()
    out = []
    for b in await state.blocks():
        # The member is keyed by pubkey; the blocker is a fediverse actor, named by its @user@host.
        member = b["member"]
        out.append({"blocker": b["actor"], "blocked": member, "at": b["at"], "via": "fediverse",
                    "blocker_handle": ("@" + b["acct"]) if b.get("acct") else b["actor"],
                    "blocked_handle": known.get(member) or ("@" + actors.handle(member))})
    for r in await mute_relations(known):
        r["blocker_handle"] = handle(r["blocker"], known)
        r["blocked_handle"] = handle(r["blocked"], known)
        out.append(r)
    out.sort(key=lambda r: r["at"])
    return out


PER_SERVER = 3


async def leaderboard(min_count: int = 1) -> list:
    """[{handle, pubkey, count}] -- our most-blocked accounts, most first. A blocker counts once per
    member whichever side it came from."""
    known = members()
    by_member: dict = {}
    per_host: dict = {}
    for b in await blocks():
        if b["blocked"] not in known:
            continue
        # A fediverse Block costs nothing to fake in bulk -- actors on a server somebody controls --
        # so any one server can put at most PER_SERVER blockers on one member's count.
        if b.get("via") == "fediverse":
            key = (b["blocked"], urlparse(str(b["blocker"])).hostname or "")
            seen = per_host.setdefault(key, set())
            if b["blocker"] not in seen and len(seen) >= PER_SERVER:
                continue
            seen.add(b["blocker"])
        by_member.setdefault(b["blocked"], set()).add(b["blocker"])
    rows = [{"pubkey": pk, "handle": known[pk], "count": len(who)} for pk, who in by_member.items()
            if len(who) >= max(1, int(min_count))]
    rows.sort(key=lambda r: (-r["count"], r["handle"]))
    return rows


async def activity(top: int = 5) -> dict:
    """Active members over a day and a month, and the day's most-engaged posts by members."""
    known = members()
    pks = sorted(known)
    now = int(time.time())
    if not pks:
        return {"dau": 0, "mau": 0, "members": 0, "top_posts": []}

    async def authors_since(seconds: int) -> set:
        evs = await _query([{"kinds": [1, 6, 7, 1111], "authors": pks, "since": now - seconds, "limit": 20000}])
        return {e.get("pubkey") for e in evs}
    dau, mau = await authors_since(86400), await authors_since(30 * 86400)
    posts = await _query([{"kinds": [1], "authors": pks, "since": now - 86400, "limit": 2000}])
    posts = [p for p in posts if not any(len(t) > 1 and t[0] == "proxy" for t in p.get("tags") or [])]
    scores: dict = {}
    if posts:
        ids = [p["id"] for p in posts]
        for i in range(0, len(ids), 200):
            for e in await _query([{"kinds": [1, 6, 7, 1111], "#e": ids[i:i + 200], "limit": 20000}]):
                if e.get("pubkey") in (None, ""):
                    continue
                ref = next((t[1] for t in reversed(e.get("tags") or []) if len(t) > 1 and t[0] == "e"), "")
                s = scores.setdefault(ref, {"reactions": 0, "reposts": 0, "replies": 0})
                key = {7: "reactions", 6: "reposts"}.get(e.get("kind"), "replies")
                s[key] += 1
    from app.services.activitypub import config
    base = config.base_url()
    ranked = []
    for p in posts:
        s = scores.get(p["id"], {"reactions": 0, "reposts": 0, "replies": 0})
        total = s["reactions"] + 2 * s["reposts"] + 2 * s["replies"]
        if total <= 0:
            continue
        note = bech32.encode("note", bytes.fromhex(p["id"]))
        ranked.append({"id": p["id"], "handle": known.get(p["pubkey"], ""), "score": total, **s,
                       "text": (p.get("content") or "")[:280],
                       "url": f"{base}/{note}" if base else f"nostr:{note}"})
    ranked.sort(key=lambda r: -r["score"])
    return {"dau": len(dau), "mau": len(mau), "members": len(pks), "top_posts": ranked[:max(1, int(top))]}

"""TRACE AN ACCOUNT ON THIS RELAY — why it is here, what it put here, and who vouched for it.

"i suspect this user is spam ... how did it make it to my relay!" (2026-10-08) took a hand-run crawl to
answer: the nightly web-of-trust rebuild stored only a bare SET of members, so nothing could say which
rule let a key in. The rebuild now records each member's tier and how many accounts of the tier below
vouched for it (`wot.WOT_TIERS_KEY`), and this module turns that plus a live follower lookup into the
admin's profile ⋯ → "Trace on this relay" panel.

`gather()` collects FACTS (relay Postgres, the tier record, the upstream follower lists); `explain()` is
a pure function from facts to the sentences on screen, so the wording is testable without a relay.
Nothing here writes anything.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter

from app.services.nostr_relay.langfilter import is_hidden_payload
from app.services.nostr_relay.wot import WOT_TIERS_KEY

logger = logging.getLogger(__name__)

TIER_NAMES = {0: "seed", 1: "followed by a seed", 2: "friend of a friend", 3: "three hops out"}
_FOLLOWER_LIMIT = 500
_SHOWN_FOLLOWERS = 30
_RECENT = 300


def _relay_rows(pk: str) -> dict:
    """BLOCKING: everything the relay's own Postgres knows about `pk`."""
    import psycopg2
    from app.services.stats_bot_service import _relay_dsn
    conn = psycopg2.connect(_relay_dsn(), connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT kind, origin, count(*), min(created_at), max(created_at) FROM events "
                    "WHERE pubkey=%s GROUP BY kind, origin", (pk,))
        groups = cur.fetchall()
        cur.execute("SELECT depth, added_at FROM wot WHERE pubkey=%s", (pk,))
        wot = cur.fetchone()
        cur.execute("SELECT value::jsonb->'tiers'->%s, value::jsonb->'built_at', value::jsonb->'depth', "
                    "value::jsonb->'min_followers' FROM relay_kv WHERE key=%s", (pk, WOT_TIERS_KEY))
        tier_row = cur.fetchone()
        cur.execute("SELECT content, tags, created_at FROM events WHERE pubkey=%s AND kind=1 "
                    "ORDER BY created_at DESC LIMIT %s", (pk, _RECENT))
        recent = cur.fetchall()
        cur.execute("SELECT content FROM events WHERE pubkey=%s AND kind=0 LIMIT 1", (pk,))
        prof = cur.fetchone()
        return {"groups": groups, "wot": wot, "tier_row": tier_row, "recent": recent,
                "profile": prof[0] if prof else None}
    finally:
        conn.close()


def _tiers_for(pks: list) -> dict:
    """BLOCKING: {pubkey: [tier, vouchers]} for the members among `pks` (from the last clean rebuild),
    plus {pubkey: None} for members the record does not cover (operators, admitted between rebuilds)."""
    if not pks:
        return {}
    import psycopg2
    from app.services.stats_bot_service import _relay_dsn
    conn = psycopg2.connect(_relay_dsn(), connect_timeout=10)
    try:
        cur = conn.cursor()
        cur.execute("SELECT pubkey FROM wot WHERE pubkey = ANY(%s)", (list(pks),))
        members = [r[0] for r in cur.fetchall()]
        out = {m: None for m in members}
        if members:
            cur.execute("SELECT k, v FROM relay_kv, jsonb_each(value::jsonb->'tiers') AS t(k, v) "
                        "WHERE key=%s AND k = ANY(%s)", (WOT_TIERS_KEY, members))
            for k, v in cur.fetchall():
                out[k] = v if isinstance(v, list) else json.loads(v)
        return out
    finally:
        conn.close()


async def _followers(pk: str, relays: list) -> list:
    """Authors whose LATEST contact list (kind 3) on the upstream relays follows `pk`."""
    from app.services.nostr import relay as _relay
    evs = await _relay.query(relays, [{"kinds": [3], "#p": [pk], "limit": _FOLLOWER_LIMIT}])
    latest = {}
    for e in evs or []:
        a = e.get("pubkey")
        if a and e.get("created_at", 0) >= latest.get(a, {}).get("created_at", -1):
            latest[a] = e
    return [a for a, e in latest.items()
            if any(len(t) >= 2 and t[0] == "p" and t[1] == pk for t in e.get("tags", []))]


def _rank(tiers: dict, pks: list) -> list:
    """Members first, lowest tier first, most vouched-for first."""
    def key(p):
        t = tiers.get(p, "absent")
        if t == "absent":
            return (2, 9, 0)
        if t is None:
            return (1, 9, 0)
        return (0, t[0], -t[1])
    return sorted(pks, key=key)


async def gather(pk: str, relays: list, *, hops: int = 2) -> dict:
    """Every fact the panel shows. Upstream failures leave `followers` as None ("could not ask"),
    never [] ("nobody follows it")."""
    rows = await asyncio.to_thread(_relay_rows, pk)
    followers = chain = None
    tiers = {}
    try:
        followers = await asyncio.wait_for(_followers(pk, relays), 20)
        tiers = await asyncio.to_thread(_tiers_for, followers)
        # Walk up the strongest voucher until it reaches the seeds or their follows.
        chain, cur = [], _rank(tiers, followers)
        for _ in range(hops):
            best = next((p for p in cur if p in tiers), None)
            if not best:
                break
            chain.append({"pubkey": best, "tier": tiers[best]})
            if tiers[best] is None or tiers[best][0] <= 1:
                break
            up = await asyncio.wait_for(_followers(best, relays), 20)
            up_t = await asyncio.to_thread(_tiers_for, up)
            tiers.update(up_t)
            cur = _rank(up_t, up)
    except Exception as e:
        logger.info("[relay-trace] follower lookup failed: %s", e)
    return {"pubkey": pk, "rows": rows, "followers": followers, "tiers": tiers, "chain": chain}


def explain(facts: dict, *, blocked: bool = False, member: dict | None = None, local_user: bool = False) -> dict:
    """Facts -> the panel: a headline, a tone (ok / info / warn / bad), the stored-content summary,
    signals, the voucher chain and the followers. Pure."""
    rows = facts.get("rows") or {}
    groups = rows.get("groups") or []
    total = sum(g[2] for g in groups)
    by_origin, by_kind = Counter(), Counter()
    first = last = None
    for kind, origin, n, lo, hi in groups:
        by_origin[origin] += n
        by_kind[kind] += n
        first = lo if first is None else min(first, lo)
        last = hi if last is None else max(last, hi)
    recent = rows.get("recent") or []
    hidden = sum(1 for c, _t, _ts in recent if is_hidden_payload(c or ""))
    tags = Counter()
    texts = Counter()
    for c, t, _ts in recent:
        try:
            for tg in json.loads(t or "[]"):
                if len(tg) >= 2 and tg[0] == "t":
                    tags[str(tg[1]).lower()] += 1
        except Exception:
            pass
        texts[(c or "").strip()[:200]] += 1
    repeated = sum(n for n in texts.values() if n > 1)
    newest = max((ts for _c, _t, ts in recent), default=None)
    per_day = 0
    if recent and len(recent) > 1:
        span = max(1, newest - min(ts for _c, _t, ts in recent))
        per_day = round(len(recent) * 86400 / span)

    wot = rows.get("wot")
    tier_row = rows.get("tier_row") or (None, None, None, None)
    tier = tier_row[0]
    if isinstance(tier, str):
        tier = json.loads(tier)
    built_at, depth, min_f = tier_row[1], tier_row[2], tier_row[3]

    # WHY IT IS HERE — the first rule that applies, in the order the relay applies them.
    if blocked:
        tone, head = "bad", "Blocked on this relay — nothing new from this account is stored."
    elif member and member.get("qualified"):
        tone, head = "ok", f"A member of this server ({', '.join(member.get('addresses') or [])})."
    elif local_user:
        tone, head = "ok", "Has an account on this server, so the relay keeps what it publishes."
    elif wot and wot[0] == 0:
        tone, head = "ok", "An operator key of this server (always trusted)."
    elif wot and tier:
        t, n = int(tier[0]), int(tier[1])
        if t == 0:
            tone, head = "ok", "One of the seed accounts the web of trust starts from."
        elif t == 1:
            tone, head = "info", f"In the web of trust: followed directly by {n} of your seed account{'s' if n != 1 else ''}."
        elif t == 2:
            tone, head = "warn", (f"In the web of trust as a friend of a friend: {n} accounts your seeds follow "
                                  f"follow it (needs {min_f}).")
        else:
            tone, head = "warn", (f"In the web of trust from three hops out: {n} friends-of-friends follow it "
                                  f"(needs {min_f}). This is the loosest rule the relay has.")
    elif wot:
        vouch = sum(1 for p in (facts.get("followers") or []) if p in (facts.get("tiers") or {}))
        tone, head = "info", ((f"In the web of trust: {vouch} account{'s' if vouch != 1 else ''} already in it "
                               f"follow{'s' if vouch == 1 else ''} it. " if vouch else "In the web of trust. ")
                              + "The next nightly rebuild records exactly which rule let it in.")
    elif total:
        tone = "warn"
        if by_origin.get("direct"):
            head = "Not in the web of trust; a client published these notes directly to this relay."
        elif by_origin.get("ancestor"):
            head = "Not in the web of trust; its notes were pulled in as the parents of replies in threads."
        else:
            head = "Not in the web of trust any more; these notes were stored while it was."
    else:
        tone, head = "info", "Not on this relay: nothing stored and not in the web of trust."

    signals = []
    if hidden:
        tone = "bad" if tone != "ok" else tone
        signals.append({"level": "bad", "text": f"{hidden} of its last {len(recent)} notes are hidden-character "
                                                "payloads (data a reader never sees) — refused from now on."})
    if recent and repeated >= max(5, len(recent) // 3):
        signals.append({"level": "warn", "text": f"{repeated} of its last {len(recent)} notes repeat the same text."})
    if per_day >= 100:
        signals.append({"level": "warn", "text": f"Posting about {per_day} notes a day."})
    if not rows.get("profile"):
        signals.append({"level": "warn", "text": "No profile stored here (no name, no picture)."})
    followers = facts.get("followers")
    tiers = facts.get("tiers") or {}
    if followers is None:
        signals.append({"level": "info", "text": "Could not ask the public relays who follows it."})
    elif not followers:
        signals.append({"level": "warn", "text": "No follower found on the public relays."})

    shown = []
    if followers:
        for p in _rank(tiers, followers)[:_SHOWN_FOLLOWERS]:
            t = tiers.get(p, "absent")
            shown.append({"pubkey": p, "in_wot": t != "absent",
                          "tier": None if t in ("absent", None) else t[0],
                          "vouchers": None if t in ("absent", None) else t[1]})
    return {
        "ok": True, "pubkey": facts.get("pubkey"), "tone": tone, "headline": head,
        "stored": {"total": total, "by_kind": dict(by_kind.most_common(8)), "by_origin": dict(by_origin),
                   "first": first, "last": last, "per_day": per_day,
                   "top_tags": [t for t, _ in tags.most_common(5)]},
        "signals": signals,
        "chain": [{"pubkey": c["pubkey"], "tier": None if c["tier"] is None else c["tier"][0],
                   "vouchers": None if c["tier"] is None else c["tier"][1]} for c in (facts.get("chain") or [])],
        "followers": {"found": None if followers is None else len(followers),
                      "in_wot": sum(1 for p in (followers or []) if p in tiers), "shown": shown},
        "wot": {"member": bool(wot), "tier": None if not tier else tier[0], "built_at": built_at, "depth": depth,
                "min_followers": min_f},
    }

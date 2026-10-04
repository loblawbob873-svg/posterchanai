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


def bot_token() -> str:
    """The credential the bot manager hands every bot it spawns, good for /api/community/* ONLY.

    The block bot read this API with the operator-configured bots API key, and with none configured
    it sent nothing: every poll was a 401 that the bot logs as "could not read" and retries for ever,
    so a freshly created block bot announced nothing, silently. The manager runs in the process that
    serves this API, so it can hand over a credential nobody has to type: an HMAC of the app's own
    persisted SECRET_KEY, stable across restarts (a new value on every restart would change every
    bot's environment and restart them all), and scoped to these read-only endpoints."""
    import hashlib
    import hmac
    from app.auth import SECRET_KEY
    return hmac.new(str(SECRET_KEY).encode(), b"pcai-community-bot-v1", hashlib.sha256).hexdigest()


def members() -> dict:
    """{pubkey: "@name@domain"} for every member this node granted a name (the NIP-05 registry).

    A member may hold SEVERAL names; they are named here by the same one the fediverse knows them by
    (actors._registry's by_pk), not by whichever name a dict happened to iterate last."""
    from app.services.activitypub import actors, config
    domain = config.domain()
    _by_name, by_pk = actors._registry()
    return {pk: (f"@{name}@{domain}" if domain else f"@{name}") for pk, name in by_pk.items() if pk}


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


# ---- mute lists that never reached this relay ---------------------------------------------------
#
# A mute list lives on its AUTHOR's relays. Somebody on Damus or Primal who mutes one of our members
# publishes that list there, and this relay only holds it if its firehose happened to pull it in.
# Measured 2026-09-26 against the 159 poster.place members: 1,564 (muter, member) pairs across four
# public relays, 583 of them on this relay -- the block bot could see about a third of the mutes.
#
# So the relays this node syncs with are asked too, and each author's NEWEST list wins wherever it
# was found. Three rules, each because the alternative announces something false:
#   * every event's signature is verified -- a relay can serve anything, and an unverified list
#     would let anyone announce that a stranger muted one of ours;
#   * the result is CACHED and only ever replaced by a newer list: a relay that fails to answer must
#     not read as "they unmuted", or the bot forgets the mute and announces it again later;
#   * asked at most every _EXT_TTL seconds -- the bot polls every minute, the upstream relays should
#     not be asked that often.
_EXT_TTL = 600
_EXT_RELAYS_MAX = 6
_ext_lists: dict = {}          # author pubkey -> newest verified kind-10000 naming a member
_ext_state = {"at": 0.0}
_ext_lock = None


def _upstream_relays() -> list:
    from app.services.nostr import nostr_service
    ups = nostr_service.relay.normalize_relays(settings_store.get("nostr_relay_upstream_relays", "") or "")
    return (ups or list(nostr_service.DEFAULT_RELAYS))[:_EXT_RELAYS_MAX]


async def _ask_relay(uri: str, pks: list) -> list:
    """Every kind-10000 this relay holds that names one of `pks`. Raises when it cannot be asked."""
    from app.services.nostr import nostr_service
    import json as _json
    import asyncio
    out = []
    async with nostr_service.relay._connect(uri, False, max_size=1 << 24, close_timeout=2) as ws:
        for i in range(0, len(pks), 50):
            sid = "mute%d" % i
            await ws.send(_json.dumps(["REQ", sid, {"kinds": [10000], "#p": pks[i:i + 50], "limit": 2000}]))
            while True:
                msg = _json.loads(await asyncio.wait_for(ws.recv(), 15))
                if msg[0] == "EVENT" and len(msg) > 2 and isinstance(msg[2], dict):
                    out.append(msg[2])
                elif msg[0] in ("EOSE", "CLOSED"):
                    break
            await ws.send(_json.dumps(["CLOSE", sid]))
    return out


def _newer(a: dict, b: dict | None) -> bool:
    if b is None:
        return True
    return (a.get("created_at", 0), a.get("id", "")) > (b.get("created_at", 0), b.get("id", ""))


def _keep_verified(events: list, pks: set) -> list:
    from app.services.nostr.event import verify_event
    now = time.time()
    keep = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("kind") != 10000:
            continue
        if not isinstance(ev.get("created_at"), int) or ev["created_at"] > now + 300:
            continue
        if not any(len(t) > 1 and t[0] == "p" and t[1] in pks for t in ev.get("tags") or []):
            continue
        try:
            if verify_event(ev):
                keep.append(ev)
        except Exception:
            continue
    return keep


async def external_mute_lists(pks: list) -> list:
    """The newest verified mute list per author, from the upstream relays, naming one of `pks`."""
    import asyncio
    global _ext_lock
    if not pks:
        return []
    if _ext_lock is None:
        _ext_lock = asyncio.Lock()
    async with _ext_lock:
        if time.time() - _ext_state["at"] < _EXT_TTL:
            return list(_ext_lists.values())
        relays = _upstream_relays()
        results = await asyncio.gather(*(asyncio.wait_for(_ask_relay(u, pks), 60) for u in relays),
                                       return_exceptions=True)
        got = [ev for r in results if isinstance(r, list) for ev in r]
        answered = sum(1 for r in results if isinstance(r, list))
        verified = await asyncio.to_thread(_keep_verified, got, set(pks))
        for ev in verified:
            if _newer(ev, _ext_lists.get(ev["pubkey"])):
                _ext_lists[ev["pubkey"]] = ev
        # A pass where NO relay answered is retried soon rather than trusted for _EXT_TTL.
        _ext_state["at"] = time.time() if answered else time.time() - _EXT_TTL + 60
        return list(_ext_lists.values())


async def mute_relations(known: dict | None = None) -> list:
    """[{blocker, blocked, at, via:"nostr"}] -- public mutes by one of ours, or of one of ours."""
    known = members() if known is None else known
    pks = sorted(known)
    if not pks:
        return []
    lists = await _query([{"kinds": [10000], "authors": pks, "limit": len(pks) + 10}])
    lists += await _query([{"kinds": [10000], "#p": pks, "limit": 5000}])
    try:
        lists += await external_mute_lists(pks)
    except Exception:
        pass                                       # the local relay's answer still stands
    newest: dict = {}
    for ev in lists:                               # a replaceable list: only its newest version counts
        if _newer(ev, newest.get(ev.get("pubkey"))):
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


def _ref(pk: str) -> str:
    """How a post TAGS an account: a `nostr:npub…` reference. Nostr clients render it as the person's
    name and notify them (the post carries the matching `p` tag), and the fediverse side turns it into
    a real @mention for anyone with a fediverse address. A printed `@name@domain` does neither."""
    return "nostr:" + _npub(pk)


def _puppet_of(actor: str) -> str:
    """The Nostr key a fediverse account is mirrored under, or "" when it has none yet."""
    try:
        from app.database import SessionLocal
        from app.models import FediPuppet
        db = SessionLocal()
        try:
            row = db.query(FediPuppet).filter(FediPuppet.actor_uri == actor).first()
            return (row.pubkey_hex or "") if row else ""
        finally:
            db.close()
    except Exception:
        return ""


async def blocks() -> list:
    """Every block involving this community, from both sides: printable names (`*_handle`) and the
    references a post tags them with (`*_ref`)."""
    from app.services.activitypub import actors, state
    known = members()
    out = []
    for b in await state.blocks():
        # The member is keyed by pubkey; the blocker is a fediverse actor, named by its @user@host.
        member = b["member"]
        local = actors.handle(member)
        puppet = _puppet_of(b["actor"])
        out.append({"blocker": b["actor"], "blocked": member, "at": b["at"], "via": "fediverse",
                    "blocker_handle": ("@" + b["acct"]) if b.get("acct") else b["actor"],
                    # A member with no name here used to print as a bare "@" -- which a model then
                    # "completed" with an invented name. Never an empty handle.
                    "blocked_handle": known.get(member) or (("@" + local) if local else _ref(member)),
                    "blocker_ref": _ref(puppet) if puppet else (("@" + b["acct"]) if b.get("acct") else b["actor"]),
                    "blocked_ref": _ref(member)})
    for r in await mute_relations(known):
        r["blocker_handle"] = handle(r["blocker"], known)
        r["blocked_handle"] = handle(r["blocked"], known)
        r["blocker_ref"] = _ref(r["blocker"])
        r["blocked_ref"] = _ref(r["blocked"])
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
        ranked.append({"id": p["id"], "handle": known.get(p["pubkey"], ""), "ref": _ref(p["pubkey"]),
                       "score": total, **s,
                       "text": (p.get("content") or "")[:280],
                       "url": f"{base}/{note}" if base else f"nostr:{note}"})
    ranked.sort(key=lambda r: -r["score"])
    return {"dau": len(dau), "mau": len(mau), "members": len(pks), "top_posts": ranked[:max(1, int(top))]}


# ---- reports and newcomers (the Nostr report bot and welcome bot) --------------------------------

def bot_pubkeys() -> set:
    """Every managed bot's own key. A Nostr bot mints itself a NIP-05 name here, so without this the
    welcome bot would greet each new bot as a new member."""
    out = set()
    try:
        import json as _json
        from app.database import SessionLocal
        from app.models import Bot
        from app.services.nostr import nostr_service
        db = SessionLocal()
        try:
            rows = db.query(Bot.config).all()
        finally:
            db.close()
        for (cfg,) in rows:
            try:
                nsec = str(_json.loads(cfg or "{}").get("nostr_nsec") or "").strip()
                if nsec:
                    out.add(nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec)))
            except Exception:
                continue
    except Exception:
        pass
    return out


def member_list() -> list:
    """[{pubkey, handle, ref, bot}] -- the members this node granted a name to (the welcome bot's
    roll). Raises when the registry cannot be read: an empty roll is never "nobody is here"."""
    known = members()
    bots = bot_pubkeys()
    return [{"pubkey": pk, "handle": h, "ref": _ref(pk), "bot": pk in bots} for pk, h in sorted(known.items())]


_REPORT_TYPES = {"nudity", "malware", "profanity", "illegal", "spam", "impersonation", "other"}


def _report_fields(ev: dict) -> dict | None:
    """NIP-56: the reported account is the `p` tag; a post, when there is one, the `e` tag; the type is
    the 3rd element of either. Anything else is not a report this bot can describe truthfully."""
    tags = [t for t in ev.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and isinstance(t[1], str)]
    p = next((t for t in tags if t[0] == "p" and len(t[1]) == 64), None)
    if not p:
        return None
    e = next((t for t in tags if t[0] == "e" and len(t[1]) == 64), None)
    kind = next((str(t[2]).lower() for t in ([e] if e else []) + [p] if len(t) > 2 and t[2]), "")
    return {"reported": p[1], "note": e[1] if e else "",
            "type": kind if kind in _REPORT_TYPES else "other"}


async def reports(since: int = 0) -> list:
    """[{id, at, reporter, reported, note, note_ref, type, reason, *_handle, *_ref}] -- NIP-56 reports
    (kind 1984) involving THIS node's members only: filed by one of ours, or about one of ours.
    Somebody on another server reporting somebody else is none of this instance's business."""
    known = members()
    pks = sorted(known)
    if not pks:
        return []
    window = {"since": int(since)} if since else {}
    evs = await _query([{"kinds": [1984], "authors": pks, "limit": 500, **window}])
    evs += await _query([{"kinds": [1984], "#p": pks, "limit": 500, **window}])
    out, seen = [], set()
    for ev in sorted(evs, key=lambda e: (e.get("created_at", 0), e.get("id", ""))):
        if ev.get("id") in seen or ev.get("kind") != 1984:
            continue
        seen.add(ev.get("id"))
        f = _report_fields(ev)
        reporter = ev.get("pubkey") or ""
        if not f or f["reported"] == reporter:
            continue
        if reporter not in known and f["reported"] not in known:
            continue
        reason = " ".join((ev.get("content") or "").split())
        note = f["note"]
        out.append({"id": ev["id"], "at": int(ev.get("created_at") or 0),
                    "reporter": reporter, "reported": f["reported"], "type": f["type"],
                    "reason": reason[:280] + ("…" if len(reason) > 280 else ""),
                    "note": note, "note_ref": ("nostr:" + bech32.encode("note", bytes.fromhex(note))) if note else "",
                    "reporter_handle": handle(reporter, known), "reported_handle": handle(f["reported"], known),
                    "reporter_ref": _ref(reporter), "reported_ref": _ref(f["reported"])})
    return out

"""Pay-to-stay — a paid retention tier for the built-in relay, bought with Nostr zaps.

OPTIONAL, and OFF on every node until an admin turns it on in Admin → Nostr Relay. With it off this
module does nothing at all and the relay prunes exactly as it always did (see the guard in
`store._tiered_rules`), so deploying it changes no node's behaviour.

What it does when it IS on:

  * FREE tier — a direct write (`origin='direct'`, i.e. an event a client chose to publish HERE)
    by an author with no account on this instance is deleted once it is older than
    `nostr_relay_free_retention_days`. That is a real change to what this relay keeps, which is why
    the window defaults to 0 (= forever) even after the feature is enabled: it deletes nothing
    until an admin types a number.
  * PAID tier — an author with a live subscription in the ledger keeps their notes for
    `nostr_relay_paid_retention_days` instead (0 = forever).
  * Nobody with an account here is affected either way. Local users, this server's NIP-05 holders,
    operators and bridged users' puppets are in the relay's preserve set and no age rule can touch
    them; pay-to-stay is about the WoT strangers whose data this relay stores for free today.

How a subscription is bought: the payer ZAPS THE RELAY'S PROFILE (a NIP-57 zap with no `e` tag —
a zap of a *post* stays an ordinary tip, so the operator can still be tipped without it silently
becoming storage credit). The zap receipt (kind 9735) lands here, this watcher verifies it and
credits days at `nostr_relay_paid_sats_per_month`.

What makes a receipt trustworthy is NOT that it exists on our relay — anyone in the WoT can publish
a kind 9735 claiming anything. It is that the receipt is SIGNED BY THE ZAPPER SERVICE of the
configured lightning address: NIP-57 has the LNURL-pay endpoint advertise `nostrPubkey`, and only
that key's signature means an invoice was actually paid. So every receipt is checked against the
`nostrPubkey` resolved live from `nostr_relay_paid_lud16` (cached), and a receipt signed by anyone
else is ignored. The payer is the pubkey of the kind-9734 zap REQUEST embedded in the receipt's
`description` tag, whose signature is verified too.

Where the ledger lives: ONE operator-signed kind-30078 doc (`pcai:kv:paid_retention`) in the relay
— not a new SQL table, same rule the rest of this codebase follows. That also solves the process
split for free: this watcher runs in the WORKER (sole writer), while the RELAY reads the same doc
out of its own Postgres before each prune, and the app process reads it to answer
`/client/retention`.

Two safety properties, both learned the expensive way elsewhere in this repo:

  * Reads are `strict=True` and a failed read is NEVER written back. The doc is replaceable, so
    persisting a fresh empty ledger over a failed read would wipe every subscription (the same
    replaceable-doc wipe that took out a drive's files index).
  * On the RELAY side, "I could not read the ledger" and "nobody is subscribed" must not look the
    same, because the second one deletes paying users' notes. `store.set_subscribers(..., ledger_ok)`
    carries that distinction and the tiered rules are skipped outright when it is False — including
    when the ledger document does not exist at all.
"""

import json
import re
import time
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

paid_retention_scheduler = None

DOC = "pcai:kv:paid_retention"   # the one relay doc: subscriptions + scan cursor + credited ids
_TICK = 300                      # seconds between zap scans (a subscription is not time-critical)
_OVERLAP = 3600                  # re-scan this far behind the cursor (receipts arrive out of order)
_MAX_HORIZON_DAYS = 3650         # ceiling on how far ahead one account can buy (a fat-fingered zap)
_LNURL_TTL = 3600                # seconds an LNURL-pay lookup (its nostrPubkey) is cached
_READ_TTL = 15.0                 # seconds the app process serves one snapshot of the ledger
_QUERY_LIMIT = 500               # receipts pulled per scan

_lnurl_cache: dict = {}          # lud16 -> (expiry_ts, params|None)
_read_cache: dict = {"at": 0.0, "data": None}
_write_lock = asyncio.Lock()     # ledger writes are read-modify-write — they must serialize


# ---- config -------------------------------------------------------------

def enabled() -> bool:
    from app.services import settings_store
    return settings_store.get_bool("nostr_relay_paid_retention_enabled", False)


def _relay_port() -> int:
    from app.services import settings_store
    return settings_store.get_int("nostr_relay_port", 3052)


def receiving_pubkey() -> str:
    """The pubkey a zap must be addressed to (`p` on the zap request) to count as a payment here.

    `nostr_relay_paid_pubkey` if the admin set one, else the relay's advertised NIP-11 pubkey, else
    the datastore operator key — which is what a client zapping "this relay" will have used."""
    from app.services import settings_store, keystore
    from app.services.nostr import nostr_service
    for key in ("nostr_relay_paid_pubkey", "nostr_relay_pubkey"):
        raw = (settings_store.get(key, "") or "").strip()
        if raw:
            hexpk = nostr_service.to_pubkey_hex(raw)
            if hexpk:
                return hexpk
    try:
        nsec = keystore.get_operator_nsec()
        if nsec:
            return nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec))
    except Exception:
        pass
    return ""


def price_msats_per_month() -> int:
    from app.services import settings_store
    return max(0, settings_store.get_int("nostr_relay_paid_sats_per_month", 0)) * 1000


def policy() -> dict:
    """The public description of the tiers — what the relay's NIP-11 doc, its landing page and
    `/client/retention` all answer with, so the three can't describe different policies."""
    from app.services import settings_store
    on = enabled()
    return {
        "enabled": on,
        "free_days": max(0, settings_store.get_int("nostr_relay_free_retention_days", 0)) if on else 0,
        "paid_days": max(0, settings_store.get_int("nostr_relay_paid_retention_days", 0)) if on else 0,
        "sats_per_month": max(0, settings_store.get_int("nostr_relay_paid_sats_per_month", 0)) if on else 0,
        "lud16": (settings_store.get("nostr_relay_paid_lud16", "") or "").strip() if on else "",
        "pubkey": receiving_pubkey() if on else "",
    }


# ---- bolt11 -------------------------------------------------------------

# `lnbc2500u1pv…`: the human-readable part is everything before the LAST '1' (bech32's separator —
# the data charset has no '1'), and carries the amount as digits + an optional multiplier.
_HRP_RE = re.compile(r"^ln[a-z]{2,5}?(\d+)([munp])?$")
_MULT = {"m": 10 ** 8, "u": 10 ** 5, "n": 10 ** 2, "p": 0.1}   # → msats per unit


def decode_bolt11_msats(invoice: str) -> int:
    """Amount of a BOLT11 invoice in millisats, 0 if absent/unparseable. This is the AUTHORITATIVE
    amount: the zap request's own `amount` tag is only what the payer's client asked for, while this
    is what the zapper service actually invoiced and the receipt is signed over."""
    inv = (invoice or "").strip().lower()
    cut = inv.rfind("1")
    if cut <= 0:
        return 0
    m = _HRP_RE.match(inv[:cut])
    if not m:
        return 0
    try:
        amount = int(m.group(1))
    except ValueError:
        return 0
    return int(amount * _MULT.get(m.group(2) or "", 10 ** 11))


# ---- LNURL --------------------------------------------------------------

async def _lnurl_params(addr: str) -> dict | None:
    """Resolve a lightning address to its LNURL-pay params, cached. The field we're after is
    `nostrPubkey` — the key whose signature on a kind 9735 means "this invoice was paid". Without it
    (a service with no NIP-57 support) there is no way to trust a receipt, so no credit is possible."""
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return None
    hit = _lnurl_cache.get(addr)
    if hit and hit[0] > time.time():
        return hit[1]
    name, _, domain = addr.partition("@")
    params = None
    try:
        import httpx
        url = f"https://{domain}/.well-known/lnurlp/{name}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("allowsNostr") and data.get("nostrPubkey"):
                    params = data
                else:
                    logger.warning("[paid-retention] %s does not advertise nostrPubkey/allowsNostr "
                                   "— zap receipts from it cannot be verified", addr)
    except Exception as e:
        logger.warning("[paid-retention] LNURL lookup for %s failed: %s", addr, e)
        # Cache the failure only briefly, so a transient outage doesn't wedge crediting for an hour.
        _lnurl_cache[addr] = (time.time() + 60, None)
        return None
    _lnurl_cache[addr] = (time.time() + _LNURL_TTL, params)
    return params


# ---- receipt verification ----------------------------------------------

def _tag(ev: dict, name: str) -> str:
    for t in ev.get("tags") or []:
        if len(t) >= 2 and t[0] == name:
            return t[1]
    return ""


def verify_receipt(ev: dict, zapper_pubkey: str, recv_pubkey: str) -> tuple[str, int] | None:
    """Validate a kind-9735 zap receipt as a payment TO US. Returns (payer_pubkey, msats) or None.

    Pure (no I/O) so the rules are testable: `zapper_pubkey` is the `nostrPubkey` the caller resolved
    from the configured lightning address, `recv_pubkey` the key a zap must be addressed to."""
    from app.services.nostr.event import verify_event
    if not zapper_pubkey or not recv_pubkey:
        return None
    if ev.get("kind") != 9735:
        return None
    # THE payment proof: only the zapper service of our own lightning address can attest that an
    # invoice was paid. Anything else is a stranger's unverifiable claim.
    if (ev.get("pubkey") or "").lower() != zapper_pubkey.lower():
        return None
    if not verify_event(ev):
        return None
    try:
        req = json.loads(_tag(ev, "description") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(req, dict) or req.get("kind") != 9734 or not verify_event(req):
        return None
    if (_tag(req, "p") or "").lower() != recv_pubkey.lower():
        return None
    # Self-contained: receipts are collected from several relays, not only from a query we filtered,
    # so don't rely on the caller's filter to have established who this was paid to.
    if (_tag(ev, "p") or "").lower() != recv_pubkey.lower():
        return None
    # A zap of a POST is a tip for that post. Only a profile zap (no `e` tag) buys storage — so the
    # operator can still be tipped normally, and nobody accidentally pays for a service they didn't
    # want. The landing page and NIP-11 say so.
    if _tag(req, "e"):
        return None
    payer = (req.get("pubkey") or "").lower()
    if len(payer) != 64:
        return None
    # The INVOICE is what was paid; the request's `amount` tag is only what the payer's client asked
    # for. So when a bolt11 tag is present it is the answer, full stop — an unparseable one is
    # refused rather than quietly falling back to the payer's own claim, which is the one number in
    # here a payer controls. The fallback exists only for a receipt carrying no invoice at all.
    inv = _tag(ev, "bolt11")
    if inv:
        msats = decode_bolt11_msats(inv)
    else:
        try:
            msats = int(_tag(req, "amount") or 0)
        except (ValueError, TypeError):
            msats = 0
    if msats <= 0:
        return None
    return payer, msats


# ---- the ledger ---------------------------------------------------------

def _empty() -> dict:
    return {"updated": 0, "cursor": 0, "subs": {}, "credited": {}}


def _normalize(doc) -> dict:
    """Coerce whatever came back from the relay into the ledger shape (a corrupt/foreign document
    must not crash the watcher, but must also not silently read as 'no subscriptions')."""
    if not isinstance(doc, dict):
        raise ValueError("ledger document is not an object")
    out = _empty()
    out["updated"] = int(doc.get("updated") or 0)
    out["cursor"] = int(doc.get("cursor") or 0)
    subs = doc.get("subs")
    if isinstance(subs, dict):
        for pk, rec in subs.items():
            if not isinstance(pk, str) or len(pk) != 64 or not isinstance(rec, dict):
                continue
            out["subs"][pk.lower()] = {"until": int(rec.get("until") or 0),
                                       "msats": int(rec.get("msats") or 0),
                                       "bal": int(rec.get("bal") or 0),
                                       "since": int(rec.get("since") or 0)}
    credited = doc.get("credited")
    if isinstance(credited, dict):
        out["credited"] = {k: int(v or 0) for k, v in credited.items() if isinstance(k, str)}
    return out


async def load_ledger() -> dict:
    """Read the ledger. Raises if the relay could not be reached (strict) — callers that WRITE must
    let that propagate rather than treat it as an empty ledger. Returns None when the document does
    not exist yet."""
    from app.database import SessionLocal
    from app.services import settings_store, nostr_store
    db = SessionLocal()
    try:
        sk = settings_store._operator_seckey(db)
    finally:
        db.close()
    if not sk:
        raise RuntimeError("no operator key")
    doc = await nostr_store.get_doc(_relay_port(), DOC, seckey=sk, strict=True)
    return None if doc is None else _normalize(doc)


async def save_ledger(ledger: dict) -> bool:
    from app.database import SessionLocal
    from app.services import settings_store, nostr_store
    db = SessionLocal()
    try:
        sk = settings_store._operator_seckey(db)
    finally:
        db.close()
    if not sk:
        return False
    ledger["updated"] = int(time.time())
    return await nostr_store.put_doc(_relay_port(), sk, DOC, ledger)


async def get_status(pubkey: str = "") -> dict:
    """Policy + (optionally) one author's standing. Cached briefly so a page of viewers costs one
    relay query per TTL. A failed read reports `known: false` rather than "not subscribed" — the
    caller must not tell a paying user their subscription is gone because the relay hiccuped."""
    out = policy()
    now = int(time.time())
    if not out["enabled"]:
        return out
    if _read_cache["data"] is not None and (time.monotonic() - _read_cache["at"]) < _READ_TTL:
        ledger = _read_cache["data"]
    else:
        try:
            ledger = await load_ledger()
        except Exception as e:
            logger.debug("[paid-retention] status read failed: %s", e)
            out["known"] = False
            return out
        if ledger is None:      # no ledger document — same "unknown" as an unreadable one, so this
            out["known"] = False    # never tells a payer their subscription is gone
            return out
        _read_cache.update(at=time.monotonic(), data=ledger)
    out["known"] = True
    out["subscribers"] = sum(1 for r in ledger["subs"].values() if r["until"] > now)
    pk = (pubkey or "").lower()
    if len(pk) == 64:
        rec = ledger["subs"].get(pk) or {}
        until = int(rec.get("until") or 0)
        out["until"] = until
        out["paid"] = until > now
        out["days_left"] = max(0, (until - now) // 86400) if until > now else 0
    return out


async def live_subscribers() -> tuple[set, bool]:
    """(pubkeys with an unexpired subscription, ledger_was_readable). The second value is what stops
    an unreadable ledger from being mistaken for an empty one — see set_subscribers in the store."""
    try:
        ledger = await load_ledger()
    except Exception as e:
        logger.warning("[paid-retention] ledger unreadable (%s) — tiered prune skipped this pass", e)
        return set(), False
    if ledger is None:
        # No document at all. That is "unknown", not "nobody has ever paid": on a node where the
        # feature is on, the watcher writes the doc on its first tick, so a missing one means the
        # ledger has not been established (or was lost) — never a licence to start deleting.
        logger.warning("[paid-retention] no ledger document yet — tiered prune skipped this pass")
        return set(), False
    now = int(time.time())
    return {pk for pk, rec in ledger["subs"].items() if rec["until"] > now}, True


async def grant(pubkey: str, days: int) -> dict:
    """Admin override: add (or with a negative value, remove) days for one author. Same ledger and
    same read-modify-write discipline as a zap credit — an operator needs a way to fix a bad credit,
    comp an account, or honour a payment that arrived some other way."""
    from app.services.nostr import nostr_service
    pk = (nostr_service.to_pubkey_hex(pubkey) or "").lower()
    if len(pk) != 64:
        raise ValueError("not a usable pubkey")
    async with _write_lock:
        ledger = await load_ledger()
        if ledger is None:
            ledger = _empty()
        rec = _credit(ledger, pk, int(days) * 86400, msats=0)
        if not await save_ledger(ledger):
            raise RuntimeError("ledger write rejected by the relay")
        _read_cache.update(at=0.0, data=None)
    return rec


def _credit(ledger: dict, pubkey: str, seconds: int, *, msats: int) -> dict:
    """Extend `pubkey`'s subscription by `seconds`, from now or from their current expiry, whichever
    is later — so renewing early doesn't burn the time already paid for. Capped at _MAX_HORIZON_DAYS
    ahead. Returns the updated record."""
    now = int(time.time())
    rec = ledger["subs"].setdefault(pubkey, {"until": 0, "msats": 0, "bal": 0, "since": now})
    base = max(now, int(rec.get("until") or 0))
    rec["until"] = max(0, min(base + seconds, now + _MAX_HORIZON_DAYS * 86400))
    rec["msats"] = int(rec.get("msats") or 0) + max(0, msats)
    rec.setdefault("since", now)
    return rec


# ---- the watcher --------------------------------------------------------

def _relays() -> list:
    """Where to look for receipts: this node's own relay first (our client tags it in the zap
    request's `relays`, so that is where a zap from here lands), plus the configured upstream relays
    — a payer zapping from Amethyst publishes the receipt to THEIR relays, not ours."""
    from app.services import settings_store
    from app.services.nostr import nostr_service
    out = [f"ws://127.0.0.1:{_relay_port()}/relay"]
    raw = (settings_store.get("nostr_relay_upstream_relays", "") or "").replace(",", "\n")
    ups = [u.strip() for u in raw.split("\n") if u.strip().startswith("ws")]
    # Blank upstreams means the relay itself falls back to the bots' defaults; mirror that, but keep
    # the fan-out small — this reconnects every tick, and strangers' relays are not ours to hammer.
    return out + (ups or list(getattr(nostr_service, "DEFAULT_RELAYS", []))[:4])


async def scan_once() -> int:
    """One pass: pull zap receipts addressed to us, credit the verified ones. Returns days credited."""
    if not enabled():
        return 0
    from app.services import settings_store
    from app.services.nostr import relay as relay_client

    recv = receiving_pubkey()
    price = price_msats_per_month()
    addr = (settings_store.get("nostr_relay_paid_lud16", "") or "").strip()
    if not recv or not price or not addr:
        return 0                      # not priced / no address yet — nothing can be credited
    params = await _lnurl_params(addr)
    if not params:
        return 0
    # LUD-06 says hex, but normalize anyway: a service answering with an npub would match no receipt
    # ever, and the symptom is a feature that takes payments and credits nobody, in silence.
    from app.services.nostr import nostr_service
    zapper = (nostr_service.to_pubkey_hex(params.get("nostrPubkey") or "") or "").lower()
    if not zapper:
        logger.warning("[paid-retention] %s advertises an unusable nostrPubkey (%r) — no zap can be "
                       "verified", addr, params.get("nostrPubkey"))
        return 0

    async with _write_lock:
        try:
            ledger = await load_ledger()
        except Exception as e:
            logger.warning("[paid-retention] ledger read failed (%s) — skipping this scan rather "
                           "than writing over it", e)
            return 0
        first_run = ledger is None
        if first_run:
            # Start the cursor at NOW. Without this, enabling the feature would retroactively credit
            # every historical tip the operator ever received, which nobody paid for storage with.
            ledger = _empty()
            ledger["cursor"] = int(time.time())
            await save_ledger(ledger)
            logger.info("[paid-retention] ledger created — crediting zaps from now on")
            return 0

        since = max(0, int(ledger["cursor"]) - _OVERLAP)
        try:
            evs = await relay_client.query(
                _relays(), [{"kinds": [9735], "#p": [recv], "since": since, "limit": _QUERY_LIMIT}])
        except Exception as e:
            logger.warning("[paid-retention] receipt query failed: %s", e)
            return 0

        credited_days, newest = 0, int(ledger["cursor"])
        notify: list = []
        for ev in evs:
            eid = ev.get("id") or ""
            created = int(ev.get("created_at") or 0)
            newest = max(newest, created)
            if not eid or eid in ledger["credited"]:
                continue
            got = verify_receipt(ev, zapper, recv)
            if not got:
                continue
            payer, msats = got
            ledger["credited"][eid] = created
            # Carry the remainder: a run of small zaps adds up to a day instead of each rounding to
            # nothing, and an overpayment isn't quietly pocketed.
            rec = ledger["subs"].get(payer) or {}
            bal = int(rec.get("bal") or 0) + msats
            days = int(bal * 30 // price)
            bal -= int(days * price // 30)
            rec = _credit(ledger, payer, days * 86400, msats=msats)
            rec["bal"] = max(0, bal)
            credited_days += days
            if days:
                notify.append((payer, days, rec["until"]))
            logger.info("[paid-retention] %s sats from %s → +%d day(s) (until %s)",
                        msats // 1000, payer[:12], days,
                        time.strftime("%Y-%m-%d", time.gmtime(rec["until"])))
        ledger["cursor"] = newest
        # Drop credited ids that can no longer come back in a scan window, so the doc stays bounded
        # while still covering the overlap the dedup depends on.
        floor = newest - 4 * _OVERLAP
        ledger["credited"] = {k: v for k, v in ledger["credited"].items() if v >= floor}
        if not await save_ledger(ledger):
            logger.warning("[paid-retention] ledger write REJECTED — %d day(s) of credit not saved",
                           credited_days)
            return 0
        _read_cache.update(at=0.0, data=None)

    for payer, days, until in notify:
        try:
            from app.services import system_dm
            await system_dm.send(
                payer,
                f"Thanks — your storage subscription on this relay is extended by {days} day(s). "
                f"Your notes here are kept until "
                f"{time.strftime('%Y-%m-%d', time.gmtime(until))}.")
        except Exception as e:
            logger.debug("[paid-retention] confirmation DM to %s failed: %s", payer[:12], e)
    return credited_days


async def _tick() -> None:
    if not enabled():
        return
    try:
        await scan_once()
    except Exception as e:
        logger.warning("[paid-retention] scan failed: %s", e)


# ---- scheduler ----------------------------------------------------------

def start_paid_retention_scheduler():
    """Idempotent start (worker process). The tick itself is a no-op while the feature is off, so a
    node that never enables pay-to-stay pays one settings read every _TICK seconds and nothing else."""
    global paid_retention_scheduler
    if paid_retention_scheduler:
        return paid_retention_scheduler
    paid_retention_scheduler = AsyncIOScheduler()
    paid_retention_scheduler.add_job(_tick, IntervalTrigger(seconds=_TICK),
                                     id="paid_retention", replace_existing=True,
                                     max_instances=1, coalesce=True)
    paid_retention_scheduler.start()
    logger.info("[paid-retention] watcher started (every %ds; feature %s)",
                _TICK, "ON" if enabled() else "off")
    return paid_retention_scheduler


def stop_paid_retention_scheduler():
    global paid_retention_scheduler
    if paid_retention_scheduler:
        try:
            paid_retention_scheduler.shutdown(wait=False)
        except Exception:
            pass
        paid_retention_scheduler = None

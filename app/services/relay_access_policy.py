"""Operator-controlled cleanup of AI/Blossom/live-streaming grants for non-local Nostr identities."""
import asyncio
import json
import logging
from datetime import datetime, timezone

from app.models import User
from app.services import settings_store as settings, users_store, blossom_service
from app.services.nostr import nostr_service as ns

log = logging.getLogger(__name__)
_lock = asyncio.Lock()
_scheduler = None


def configuration():
    try:
        value = json.loads(settings.get("relay_access_policy", "") or "{}")
        return {"enabled": value.get("enabled") is True,
                "exempt_fediverse": value.get("exempt_fediverse", True) is not False}
    except (ValueError, AttributeError):
        return {"enabled": False, "exempt_fediverse": True}



async def _infrastructure_keys(db, users=None) -> set:
    """Admins, this node's bots and its GPU-sharing peers: infrastructure, not consumer grants.
    Nothing in this module ever revokes them."""
    keep = set()
    for u in (users if users is not None else db.query(User).all()):
        if u.is_admin and u.nostr_npub:
            keep.add(ns.to_pubkey_hex(u.nostr_npub))
    from app.services import nostr_dvm
    keep.update(nostr_dvm.peer_pubkeys())
    from app.services import bot_table
    for bot in await bot_table.aall_bots(db):   # Unavailable propagates: never plan from a list missing the bots
        try:
            key = json.loads(bot.config or "{}").get("nostr_nsec")
            if key:
                keep.add(ns.derive_pubkey(ns.decode_seckey(key)))
        except (ValueError, TypeError):
            continue
    return keep


async def _plan(db, exempt_fediverse=True):
    from app.services.nostr_relay.thread import _parse_nip05
    settings.hydrate_from_db(db)
    if not settings.is_hydrated():
        raise ValueError("Relay settings are still loading; no permissions were changed")
    from app.services.instance_membership import _configuration
    profile_config = _configuration()
    domain = (settings.get("nostr_relay_nip05_domain", "") or "").strip().lower()
    registry = settings.get("nostr_relay_nip05_names", "") or ""
    names, _ = _parse_nip05(registry, "")
    if not domain or not names:
        raise ValueError("Configure a NIP-05 domain and registered names before running this policy")
    registered = {pk.lower() for pk in names.values()}
    qualified_keys = set()
    users = db.query(User).all()
    keep = await _infrastructure_keys(db, users)
    if exempt_fediverse:
        # Raises relay_reader.Unavailable on an unreadable registry, which aborts the plan like any
        # other error here -- "no puppets" would strip every fediverse account of its exemption.
        from app.services import fedi_tables
        keep.update(await fedi_tables.apuppet_pubkeys())
        # Accounts that linked a fediverse account under the retired bridge (legacy columns).
        keep.update(ns.to_pubkey_hex(u.nostr_npub) for u in users if u.nostr_npub and
                    (u.pleroma_acct or u.pleroma_enabled or u.pleroma_instance_url))
    # Every registered key is asked the SAME question the app gates ask (instance_membership.status:
    # a granted name and not blocked), before any mutation, so this reconcile and the gates cannot
    # disagree. Any error aborts the entire plan.
    from app.services.instance_membership import status
    from fastapi import HTTPException
    slots = asyncio.Semaphore(8)
    async def check(pk):
        async with slots:
            return pk, (await status(pk, force=True))['qualified']
    try:
        # Bound the whole preview as well as concurrency. No SQL writes occur until every
        # answer is known; a slow relay cannot leave a request/transaction open indefinitely.
        async with asyncio.timeout(45):
            results = await asyncio.gather(*(check(pk) for pk in sorted(registered)),
                                           return_exceptions=True)
    except TimeoutError:
        raise HTTPException(503, 'Membership check timed out; no permissions were changed')
    for result in results:
        if isinstance(result, BaseException):
            raise result
        pk, qualified = result
        if qualified:
            keep.add(pk)
            qualified_keys.add(pk)
    if profile_config != _configuration():
        raise HTTPException(503, 'Instance identity settings changed; retry without changing permissions')
    whitelist = set(blossom_service._whitelist_pubkeys(db))
    removed = whitelist - keep
    targets = [u for u in users if u.nostr_npub and ns.to_pubkey_hex(u.nostr_npub) not in keep
               and (u.can_ai or u.can_blossom or u.can_stream or ns.to_pubkey_hex(u.nostr_npub) in removed
                    or (u.nostr_nsec and not u.access_revoked))]
    grants = [u for u in users if u.nostr_npub and ns.to_pubkey_hex(u.nostr_npub) in qualified_keys
              and (u.access_revoked or not all(getattr(u, field) for field in GRANT_FIELDS))]
    added = qualified_keys - whitelist
    summary = {"domain": domain, "accounts": len(targets),
               "ai": sum(bool(u.can_ai) for u in targets),
               "blossom": sum(bool(u.can_blossom) for u in targets),
               "streaming": sum(bool(u.can_stream) for u in targets),
               "whitelist": len(removed), "granted_accounts": len(grants),
               "whitelist_added": len(added),
               **{"granted_" + field.removeprefix("can_"): sum(not bool(getattr(u, field)) for u in grants)
                  for field in GRANT_FIELDS}}
    return targets, (whitelist - removed) | qualified_keys, summary, grants, profile_config


GRANT_FIELDS = ("can_ai", "can_blossom", "can_image", "can_music", "can_stream")


async def plan(db, exempt_fediverse=True):
    targets, whitelist, summary, _, _ = await _plan(db, exempt_fediverse)
    return targets, whitelist, summary


async def preview(db, exempt_fediverse=True):
    """Return both actions from one verified profile snapshot, without any writes."""
    targets, _, summary, grants, _ = await _plan(db, exempt_fediverse)
    return targets, grants, summary


async def run(db, exempt_fediverse=True):
    async with _lock:
        targets, keep, summary, grants, profile_config = await _plan(db, exempt_fediverse)
        from app.services.instance_membership import _configuration
        from fastapi import HTTPException
        def verify_config():
            if profile_config != _configuration():
                raise HTTPException(503, 'Instance identity settings changed; retry reconciliation')
        # Persist each revocation to the authoritative relay before changing its read-cache.
        # A failed write leaves a visible error and is retried on the next run.
        for u in targets:
            verify_config()
            previous = u.can_ai, u.can_blossom, u.can_stream, u.access_revoked
            u.can_ai = u.can_blossom = u.can_stream = False
            u.access_revoked = True
            try:
                with db.no_autoflush:
                    ok = await users_store.sync_user(db, u, force=True)
            except Exception:
                ok = False
            if not ok:
                u.can_ai, u.can_blossom, u.can_stream, u.access_revoked = previous
                db.rollback()
                raise RuntimeError("Account synchronization failed; some revocations may have completed")
            db.commit()
        for u in grants:
            verify_config()
            previous = tuple(getattr(u, field) for field in GRANT_FIELDS) + (u.access_revoked,)
            for field in GRANT_FIELDS:
                setattr(u, field, True)
            u.access_revoked = False
            try:
                with db.no_autoflush:
                    ok = await users_store.sync_user(db, u, force=True)
            except Exception:
                ok = False
            if not ok:
                for field, value in zip(GRANT_FIELDS + ("access_revoked",), previous):
                    setattr(u, field, value)
                db.rollback()
                raise RuntimeError("Account synchronization failed; some access changes may have completed")
            db.commit()
        verify_config()
        if summary['whitelist'] or summary['whitelist_added']:
            value = '\n'.join(ns.npub_of(pk) for pk in sorted(keep))
            if await settings.write_through(db, {"blossom_whitelist": value}) != 1:
                raise RuntimeError("Whitelist synchronization failed; account revocations completed")
            settings.put("blossom_whitelist", value, write_relay=False)
        blossom_service.invalidate_operator_cache()
        summary['completed_at'] = datetime.now(timezone.utc).isoformat()
        value = json.dumps(summary)
        if await settings.write_through(db, {"relay_access_policy_last_run": value}) != 1:
            raise RuntimeError("Cleanup completed, but its result could not be saved")
        settings.put("relay_access_policy_last_run", value, write_relay=False)
        return summary


def start():
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.database import SessionLocal
    async def tick():
        with SessionLocal() as db:
            try:
                settings.hydrate_from_db(db)
                cfg = configuration()
                if cfg['enabled']:
                    result = await run(db, cfg['exempt_fediverse'])
                    log.info('Relay access policy cleanup completed: %s', result)
            except Exception:
                log.exception('Relay access policy cleanup failed')
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(tick, 'interval', minutes=15, max_instances=1, coalesce=True,
                       id='relay-access-policy', name='Relay access policy cleanup')
    _scheduler.start()


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


# Everything the Additional-permissions panel can grant one account.
REVOKE_FIELDS = ("can_ai", "can_blossom", "can_image", "can_music", "can_video", "can_torrent",
                 "can_media", "can_stream")


async def revoke_identities(db, pubkeys) -> dict:
    """Take away the access a NIP-05 identity carried, for keys whose identity was just REMOVED.

    Remove (Admin -> Identities) takes the name off the registry, which ends the
    NIP-05 entitlement (nip05_access) at once -- but a grant written onto the account (the can_*
    columns, the shared `blossom_whitelist`) outlived it until the access policy's next run, and that
    policy is OFF unless an operator turns it on. So removing somebody left them their AI, Blossom
    and streaming. This revokes exactly the removed keys, the same way `run` does (grant columns off,
    access_revoked on, the account written through to the relay BEFORE the local commit), and edits
    the whitelist line by line so nobody else's hand-written entry is rewritten.

    Never touches infrastructure (admins, bots, GPU peers). Raises if a write-through fails, leaving
    that account as it was, so the caller can say the revoke did not finish."""
    keys = {str(pk or "").lower() for pk in (pubkeys or []) if pk}
    if not keys:
        return {"accounts": 0, "whitelist": 0, "protected": []}
    async with _lock:
        protected = keys & await _infrastructure_keys(db)
        targets = keys - protected
        out = {"accounts": 0, "whitelist": 0, "protected": sorted(protected)}
        npubs = {}
        for pk in targets:
            try:
                npubs[ns.npub_of(pk)] = pk
            except Exception:
                continue
        users = db.query(User).filter(User.nostr_npub.in_(list(npubs))).all() if npubs else []
        for u in users:
            previous = {f: getattr(u, f) for f in REVOKE_FIELDS + ("access_revoked",)}
            for f in REVOKE_FIELDS:
                setattr(u, f, False)
            u.access_revoked = True
            try:
                with db.no_autoflush:
                    ok = await users_store.sync_user(db, u, force=True)
            except Exception:
                ok = False
            if not ok:
                for f, v in previous.items():
                    setattr(u, f, v)
                db.rollback()
                raise RuntimeError("account synchronization failed; some permissions were not revoked")
            db.commit()
            out["accounts"] += 1
        # The shared Blossom whitelist: drop only these keys' lines; every other line stays verbatim.
        raw = settings.get("blossom_whitelist", "") or ""
        kept, dropped = [], 0
        for line in raw.replace(",", "\n").split("\n"):
            tok = line.strip()
            if tok and ns.to_pubkey_hex(tok) in targets:
                dropped += 1
                continue
            kept.append(line)
        if dropped:
            value = "\n".join(l for l in kept if l.strip())
            if await settings.write_through(db, {"blossom_whitelist": value}) != 1:
                raise RuntimeError("Blossom whitelist synchronization failed; account permissions were revoked")
            settings.put("blossom_whitelist", value, write_relay=False)
            out["whitelist"] = dropped
        blossom_service.invalidate_operator_cache()
        return out

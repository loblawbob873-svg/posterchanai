"""Operator-controlled cleanup of AI/Blossom grants for non-local Nostr identities."""
import asyncio
import json
import logging
from datetime import datetime, timezone

from app.models import User, FediPuppet, Bot
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



def plan(db, exempt_fediverse=True):
    from app.services.nostr_relay.thread import _parse_nip05
    settings.hydrate_from_db(db)
    if not settings.is_hydrated():
        raise ValueError("Relay settings are still loading; no permissions were changed")
    domain = (settings.get("nostr_relay_nip05_domain", "") or "").strip().lower()
    names, _ = _parse_nip05(settings.get("nostr_relay_nip05_names", "") or "", "")
    if not domain or not names:
        raise ValueError("Configure a NIP-05 domain and registered names before running this policy")
    keep = set(names.values())
    users = db.query(User).all()
    # Infrastructure identities are not consumer access grants.
    for u in users:
        if u.is_admin and u.nostr_npub:
            keep.add(ns.to_pubkey_hex(u.nostr_npub))
    from app.services import nostr_dvm
    keep.update(nostr_dvm.peer_pubkeys())
    for bot in db.query(Bot).all():
        try:
            key = json.loads(bot.config or "{}").get("nostr_nsec")
            if key:
                keep.add(ns.derive_pubkey(ns.decode_seckey(key)))
        except (ValueError, TypeError):
            continue
    if exempt_fediverse:
        keep.update(pk for pk, in db.query(FediPuppet.pubkey_hex).all())
        keep.update(ns.to_pubkey_hex(u.nostr_npub) for u in users if u.nostr_npub and
                    (u.pleroma_acct or u.pleroma_enabled or u.pleroma_instance_url))
    whitelist = set(blossom_service._whitelist_pubkeys(db))
    removed = whitelist - keep
    targets = [u for u in users if u.nostr_npub and ns.to_pubkey_hex(u.nostr_npub) not in keep
               and (u.can_ai or u.can_blossom or ns.to_pubkey_hex(u.nostr_npub) in removed
                    or (u.nostr_nsec and not u.access_revoked))]
    summary = {"domain": domain, "accounts": len(targets),
               "ai": sum(bool(u.can_ai) for u in targets),
               "blossom": sum(bool(u.can_blossom) for u in targets),
               "whitelist": len(removed)}
    return targets, whitelist - removed, summary


async def run(db, exempt_fediverse=True):
    async with _lock:
        targets, keep, summary = plan(db, exempt_fediverse)
        # Persist each revocation to the authoritative relay before changing its read-cache.
        # A failed write leaves a visible error and is retried on the next run.
        for u in targets:
            previous = u.can_ai, u.can_blossom, u.access_revoked
            u.can_ai = u.can_blossom = False
            u.access_revoked = True
            with db.no_autoflush:
                ok = await users_store.sync_user(db, u, force=True)
            if not ok:
                u.can_ai, u.can_blossom, u.access_revoked = previous
                db.rollback()
                raise RuntimeError("Account synchronization failed; some revocations may have completed")
            db.commit()
        if summary['whitelist']:
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
                    await run(db, cfg['exempt_fediverse'])
            except Exception:
                log.exception('Relay access policy cleanup failed')
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(tick, 'interval', minutes=15, max_instances=1, coalesce=True)
    _scheduler.start()


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None

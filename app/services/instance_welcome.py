"""Durable instance-name applications and retryable approval notifications."""
import time
import json
import logging


from app.services import settings_store, system_dm
from app.services.nostr.nostr_service import npub_of

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- the store (#161 wave 3)
# One application per pubkey: a DocTable row `pcai:t:instance_applications:<pubkey>` on this node's relay (relay-only;
# the old SQL rows are copied once by scripts/migrate_tables_to_nostr.py). "Could not ask" raises Unavailable
# everywhere -- never "no application" (which would offer a second one) and never "nobody to notify".
#
# The retry CLAIMS (admin_retry_at / approval_retry_at, so two passes do not DM twice) were one conditional UPDATE;
# they are now a read-check-write under this process's row lock. Across processes (the app's route and the
# worker's retry job) the window is the live stream's lag -- at worst one duplicate notification, never a lost one.
TABLE = 'instance_applications'
FIELDS = ('created_at', 'admin_notified', 'notified_admins', 'admin_retry_at', 'approval_retry_at',
          'approved_address', 'notified_address')
_DEFAULTS = {'admin_notified': 0, 'notified_admins': '[]', 'admin_retry_at': 0, 'approval_retry_at': 0,
             'approved_address': '', 'notified_address': ''}


class Application:
    """One application, with the old row's attributes."""

    def __init__(self, pubkey, row):
        self.pubkey = pubkey
        for f in FIELDS:
            v = row.get(f)
            setattr(self, f, _DEFAULTS.get(f) if v is None else v)

    def row(self):
        return {f: getattr(self, f) for f in FIELDS}


async def application(db, pubkey):
    """The application for this key, or None (the store answered: there is none)."""
    from app.services import table_gate
    from app.services.doc_table import DocTable
    r = await table_gate.aget_row(DocTable(TABLE), pubkey)
    return Application(pubkey, r) if r is not None else None


async def save(db, app_row) -> None:
    from app.services.doc_table import DocTable
    await DocTable(TABLE).aput(app_row.pubkey, app_row.row())


async def _all(db) -> list:
    from app.services import table_gate
    from app.services.doc_table import DocTable
    return [Application(k, r) for k, r in await table_gate.arows(DocTable(TABLE))]


async def apply(db, pubkey):
    from app.services import app_tables
    async with app_tables.row_lock(TABLE):
        row = await application(db, pubkey)
        if row:
            return row, False
        row = Application(pubkey, {'created_at': int(time.time())})
        await save(db, row)
        return row, True


async def _claim(db, pubkey, field, now) -> bool:
    """Take the next notification attempt for this application (the old conditional UPDATE)."""
    from app.services import app_tables
    async with app_tables.row_lock(TABLE):
        row = await application(db, pubkey)
        if row is None or int(getattr(row, field) or 0) > now:
            return False
        setattr(row, field, now + 60)
        await save(db, row)
        return True


async def notify_admins(db, row):
    if row.admin_notified:
        return True
    now = int(time.time())
    if not await _claim(db, row.pubkey, 'admin_retry_at', now):
        return False
    row = await application(db, row.pubkey)
    from app.services import user_table
    admins = await user_table.afilter(db, lambda u: bool(u.is_admin and u.nostr_npub))
    site = settings_store.get('site_name', '') or 'this instance'
    text = (f'A user applied for an instance NIP-05 name on {site}.\n\n'
            f'nostr:{npub_of(row.pubkey)}\n\nOpen their profile → Permissions → NIP-05 to approve a name. '
            'Review File Storage, Live Streaming, and AI Access permissions there too.')
    delivered = set(json.loads(row.notified_admins))
    for admin in admins:
        if admin.nostr_npub not in delivered and await system_dm.send(admin.nostr_npub, text):
            delivered.add(admin.nostr_npub)
            row.notified_admins = json.dumps(sorted(delivered))
            await save(db, row)
    if admins and all(admin.nostr_npub in delivered for admin in admins):
        row.admin_notified = 1
        await save(db, row)
        return True
    return False


async def notify_approval(db, pubkey, address):
    row = await application(db, pubkey)
    if not row:
        return False
    row.approved_address = address
    await save(db, row)
    if row.notified_address == address:
        return True
    now = int(time.time())
    if not await _claim(db, pubkey, 'approval_retry_at', now):
        return False
    text = (f'Your NIP-05 name has been approved: {address}\n\n'
            'Your access to this instance’s apps is active now. You do not need to change your profile: '
            'keep the NIP-05 it shows if you have one of your own, and both addresses are shown on your profile here.')
    if await system_dm.send(pubkey, text):
        row = await application(db, pubkey)
        row.notified_address = address
        await save(db, row)
        return True
    return False


async def clear_approval(db, pubkey) -> None:
    """Their last granted name was removed: the application is pending again."""
    row = await application(db, pubkey)
    if row is not None:
        row.approved_address = ''
        await save(db, row)


async def retry_notifications(db):
    """Bounded retry batch; successful recipients are never intentionally resent."""
    now = int(time.time())
    rows = [r for r in await _all(db)
            if (not r.admin_notified and r.approved_address == '' and int(r.admin_retry_at or 0) <= now)
            or (r.approved_address != '' and r.approved_address != r.notified_address
                and int(r.approval_retry_at or 0) <= now)]
    rows.sort(key=lambda r: r.created_at or 0)
    for row in rows[:100]:
        try:
            if row.approved_address:
                await notify_approval(db, row.pubkey, row.approved_address)
            else:
                await notify_admins(db, row)
        except Exception:
            db.rollback()
            logger.exception('Instance application notification retry failed')


_scheduler = None


def start_instance_welcome_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.database import SessionLocal

    async def job():
        with SessionLocal() as db:
            await retry_notifications(db)

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(job, 'interval', minutes=1, id='instance_welcome', max_instances=1, coalesce=True)
    _scheduler.start()

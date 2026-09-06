"""Durable instance-name applications and retryable approval notifications."""
import time
import json
import logging

from sqlalchemy import Column, String, Integer, Text
from sqlalchemy.exc import IntegrityError

from app.database import Base
from app.models import User
from app.services import settings_store, system_dm
from app.services.nostr.nostr_service import npub_of

logger = logging.getLogger(__name__)


class InstanceApplication(Base):
    __tablename__ = 'instance_applications'
    pubkey = Column(String(64), primary_key=True)
    created_at = Column(Integer, nullable=False)
    admin_notified = Column(Integer, nullable=False, default=0)
    notified_admins = Column(Text, nullable=False, default='[]')
    admin_retry_at = Column(Integer, nullable=False, default=0)
    approval_retry_at = Column(Integer, nullable=False, default=0)
    approved_address = Column(String(320), nullable=False, default='')
    notified_address = Column(String(320), nullable=False, default='')


def application(db, pubkey):
    return db.get(InstanceApplication, pubkey)


def apply(db, pubkey):
    row = application(db, pubkey)
    if row:
        return row, False
    row = InstanceApplication(pubkey=pubkey, created_at=int(time.time()))
    db.add(row)
    try:
        db.commit()
        return row, True
    except IntegrityError:
        db.rollback()
        return application(db, pubkey), False


async def notify_admins(db, row):
    if row.admin_notified:
        return True
    now = int(time.time())
    claimed = db.query(InstanceApplication).filter(
        InstanceApplication.pubkey == row.pubkey,
        InstanceApplication.admin_retry_at <= now).update({'admin_retry_at': now + 60})
    db.commit()
    if not claimed:
        return False
    admins = db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).all()  # noqa: E712
    site = settings_store.get('site_name', '') or 'this instance'
    text = (f'A user applied for an instance NIP-05 name on {site}.\n\n'
            f'nostr:{npub_of(row.pubkey)}\n\nOpen their profile → Permissions → NIP-05 to approve a name. '
            'Review File Storage, Live Streaming, and AI Access permissions there too.')
    delivered = set(json.loads(row.notified_admins))
    for admin in admins:
        if admin.nostr_npub not in delivered and await system_dm.send(admin.nostr_npub, text):
            delivered.add(admin.nostr_npub)
            row.notified_admins = json.dumps(sorted(delivered))
            db.commit()
    if admins and all(admin.nostr_npub in delivered for admin in admins):
        row.admin_notified = 1
        db.commit()
        return True
    return False


async def notify_approval(db, pubkey, address):
    row = application(db, pubkey)
    if not row:
        return False
    row.approved_address = address
    db.commit()
    if row.notified_address == address:
        return True
    now = int(time.time())
    claimed = db.query(InstanceApplication).filter(
        InstanceApplication.pubkey == pubkey,
        InstanceApplication.approval_retry_at <= now).update({'approval_retry_at': now + 60})
    db.commit()
    if not claimed:
        return False
    text = (f'Your NIP-05 name has been approved: {address}\n\n'
            'You can now set it in your Nostr profile. Open Edit profile, paste this address '
            'into the NIP-05 / verified address field, and save your profile.')
    if await system_dm.send(pubkey, text):
        row.notified_address = address
        db.commit()
        return True
    return False


async def retry_notifications(db):
    """Bounded retry batch; successful recipients are never intentionally resent."""
    from sqlalchemy import or_
    now = int(time.time())
    rows = db.query(InstanceApplication).filter(or_(
        (InstanceApplication.admin_notified == 0) & (InstanceApplication.approved_address == '') &
        (InstanceApplication.admin_retry_at <= now),
        (InstanceApplication.approved_address != '') &
        (InstanceApplication.approved_address != InstanceApplication.notified_address) &
        (InstanceApplication.approval_retry_at <= now))).order_by(InstanceApplication.created_at).limit(100).all()
    for row in rows:
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

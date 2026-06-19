"""Users / account-authority read-path → Nostr relay (Phase 2 of the Nostr-as-datastore migration).

Mirrors `settings_store`: the relay becomes the authoritative store for each account's **identity +
capabilities** (the "who exists and what they can do" record), while the SQLite `users` table is kept
as a fast local **read-through cache** so the many `db.query(User)` callers + the FK relationships
(conversations/messages) keep working unchanged.

  * `hydrate(db)`     — at startup (relay → users): for each operator-signed `pcai:user:<npub>` doc,
    UPSERT a `User` row keyed by `nostr_npub`.
  * `sync_user(db,u)` — on any account mutation (signup, claim-admin, ai-access, caps): write that
    user's account record through to the relay.

Scope is deliberately the **account-authority record** only (identity, admin, feature caps). Per-user
config + secrets are NOT synced here:
  * `nostr_nsec` (the operator's root secret bootstraps the encryption that protects these very docs
    — storing it inside one would be circular) and `password_hash` (unused, Nostr-only) stay local.
  * social tokens / notif cursors / news+telegram prefs belong to the separate "user settings →
    user-signed events" task, not the operator-authority record.

Flag-gated by `users_backend` (`relay` on; default `sqlite` = off). A fresh node can rebuild accounts
from the relay, but the operator's `nsec` must still be supplied out-of-band (it's the root key).
"""

import logging
import secrets

from app.models import User
from app.services import nostr_store as store
from app.services import settings_store as _ss  # reuse operator-key / port / enabled helpers
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

# Account-authority fields mirrored to the relay (keyed by npub, which is stored alongside).
ACCOUNT_FIELDS = (
    "username", "email", "email_verified", "is_admin",
    "can_ai", "can_image", "can_music", "can_video", "can_torrent", "can_blossom",
    "storage_quota",
)

# Per-user CONFIG (the old "User Settings" UI) — also mirrored so settings live in the relay and a
# fresh node restores them. Excludes secrets that bootstrap encryption (nostr_nsec), transient link
# tokens (telegram_key*), and per-node runtime cursors (*_notif_since) which must stay local.
CONFIG_FIELDS = (
    "notification_email", "avatar",
    "news_schedule_enabled", "news_schedule_time", "news_sources",
    "telegram_enabled", "telegram_chat_id", "telegram_notifications",
    "misskey_enabled", "misskey_instance_url", "misskey_api_token",
    "pleroma_enabled", "pleroma_instance_url", "pleroma_access_token",
    "nostr_enabled", "nostr_relays", "nostr_media_service", "nostr_media_endpoint",
    "matrix_enabled", "matrix_homeserver", "matrix_user_id", "matrix_access_token",
    "matrix_dm_bot_user_id",
    "finance_api_key", "social_notif_enabled", "matrix_notif_enabled",
)

_SYNCED = ACCOUNT_FIELDS + CONFIG_FIELDS


def enabled(db) -> bool:
    """True when accounts should be sourced from the relay (setting users_backend == 'relay')."""
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == "users_backend").first()
    return bool(row and (row.value or "").strip().lower() == "relay")


def _record(u: User) -> dict:
    rec = {f: getattr(u, f, None) for f in _SYNCED}
    rec["nostr_npub"] = u.nostr_npub
    return rec


async def sync_user(db, user: User, *, force: bool = False) -> bool:
    """Write one user's account record through to the relay. No-op when disabled (unless `force`,
    used by the one-time migrate to populate the relay before the cutover), no operator key, or the
    user has no npub (npub is the doc key). Returns True on success."""
    if user is None or not user.nostr_npub or (not force and not enabled(db)):
        return False
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return False
    try:
        ok = await store.put_doc(_ss._port(db), op_sk, store.NS_USER + user.nostr_npub, _record(user))
        if ok:
            logger.info("[users-store] synced account %s (%s) to relay", user.username, user.nostr_npub[:16])
        return ok
    except Exception as e:
        logger.warning("[users-store] sync_user failed for %s: %s", user.nostr_npub[:16], e)
        return False


def _apply(db, rec: dict) -> bool:
    """UPSERT a User row from a relay account record (keyed by npub). Returns True if changed."""
    npub = rec.get("nostr_npub")
    if not npub:
        return False
    u = db.query(User).filter(User.nostr_npub == npub).first()
    created = False
    if u is None:
        from app.auth import get_password_hash
        base = (rec.get("username") or ("npub_" + npub[4:16]))[:50]
        username = base
        for i in range(2, 1000):
            if not db.query(User).filter(User.username == username).first():
                break
            username = f"{base[:46]}{i}"
        u = User(username=username, email=rec.get("email"),
                 password_hash=get_password_hash(secrets.token_urlsafe(32)),
                 nostr_npub=npub)
        db.add(u)
        created = True
    # Authority fields (admin/caps) are mutated only via synced endpoints, so always reconcile them.
    # Config fields can be changed by flows that don't write-through (e.g. telegram_chat_id on link),
    # so only restore them when reconstructing a MISSING account — never revert a live node's config.
    fields = _SYNCED if created else ACCOUNT_FIELDS
    changed = created
    for f in fields:
        if f == "username" and not created:
            continue  # don't rename an existing local account out from under its FK rows
        if f in rec and getattr(u, f, None) != rec[f]:
            setattr(u, f, rec[f])
            changed = True
    return changed


async def hydrate(db) -> int:
    """relay → users cache. UPSERT a User row for every operator-signed account doc. No-op when
    disabled / no operator key. Returns the number of accounts created-or-updated."""
    if not enabled(db):
        return 0
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        logger.info("[users-store] hydrate skipped — no operator key")
        return 0
    try:
        docs = await store.list_docs(_ss._port(db), store.NS_USER, seckey=op_sk)
    except Exception as e:
        logger.warning("[users-store] hydrate failed to read relay: %s", e)
        return 0
    changed = 0
    for d_tag, value in (docs or {}).items():
        rec = value.get("value") if isinstance(value, dict) and "value" in value else value
        if isinstance(rec, dict) and _apply(db, rec):
            changed += 1
    if changed:
        db.commit()
    logger.info("[users-store] hydrated %d account(s) from relay", changed)
    return changed


async def migrate(db) -> dict:
    """Push every existing npub-keyed account into the relay (idempotent). Returns a small report."""
    users = db.query(User).filter(User.nostr_npub.isnot(None)).all()
    wrote = 0
    for u in users:
        if await sync_user(db, u, force=True):
            wrote += 1
    return {"accounts": len(users), "written": wrote}

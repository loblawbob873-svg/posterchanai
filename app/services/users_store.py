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

The relay is the only datastore; a fresh node rebuilds accounts from it, but the operator's `nsec`
must still be supplied out-of-band (it's the root key).
"""

import asyncio
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
    "can_ai", "can_image", "can_music", "can_video", "can_torrent", "can_blossom", "can_stream",
    "storage_quota", "access_revoked",
)

# Per-user CONFIG (the old "User Settings" UI) — also mirrored so settings live in the relay and a
# fresh node restores them. Excludes secrets that bootstrap encryption (nostr_nsec), transient link
# tokens (telegram_key*), and per-node runtime cursors (*_notif_since) which must stay local.
CONFIG_FIELDS = (
    "notification_email", "avatar", "theme",
    "news_sources",
    "telegram_enabled", "telegram_chat_id", "telegram_notifications",
    "pleroma_enabled", "pleroma_instance_url", "pleroma_access_token", "pleroma_acct",
    "nostr_enabled", "nostr_relays", "nostr_media_service", "nostr_media_endpoint",
    "social_notif_enabled",
    "fedi_bridge_enabled", "fedi_crosspost_enabled", "fedi_only",   # Nostr↔Fediverse opt-ins (cursors *_since stay local)
    "stream_record",   # per-user opt-in: save ended live streams to the user's Blossom drive
)

_SYNCED = ACCOUNT_FIELDS + CONFIG_FIELDS


def enabled(db) -> bool:
    """The relay is the ONLY datastore — always on (legacy sqlite mode removed). The users table is
    a hydrated read-cache."""
    return True


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


def sync_user_blocking(db, user: User) -> None:
    """asyncio.run wrapper so the SYNCHRONOUS admin/auth routes can write an account through to the
    relay (no-op for accounts without an npub — the relay store is npub-keyed)."""
    try:
        asyncio.run(sync_user(db, user))
    except Exception as e:
        logger.warning("[users-store] sync_user_blocking failed: %s", e)


async def delete_user(db, npub: str) -> bool:
    """Remove a deleted account's relay docs (account-authority + per-user config) so a fresh-node
    rebuild doesn't RESURRECT it from the lingering docs. No-op without an npub / operator key."""
    if not npub:
        return False
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return False
    ok = False
    for ns in (store.NS_USER, store.NS_USERCFG):
        try:
            if await store.delete_doc(_ss._port(db), op_sk, ns + npub):
                ok = True
        except Exception as e:
            logger.warning("[users-store] delete_user %s%s failed: %s", ns, npub[:16], e)
    return ok


def delete_user_blocking(db, npub: str) -> None:
    """asyncio.run wrapper for the synchronous admin delete-user route."""
    try:
        asyncio.run(delete_user(db, npub))
    except Exception as e:
        logger.warning("[users-store] delete_user_blocking failed: %s", e)


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
    there's no operator key. Returns the number of accounts created-or-updated."""
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


# ---- UserSetting key/value (mail accounts, caldav/webdav/music configs, etc.) ----
# Mirrored to a per-user encrypted doc so these are Nostr events too, not just a SQLite/PG cache. The
# server runs these features (mail/caldav), so it must read them → operator-key encrypted at
# rest (same model as the account doc + chats). EXEMPT: storage_nsec (it bootstraps the encryption —
# lives in the keyfile), per-node sync cursors (*_since/_seen), and transient scratch.
def _kv_exempt(key: str) -> bool:
    return (key in ("storage_nsec", "ai_requested")
            or key.endswith(("_since", "_seen")))


async def sync_user_kv(db, user, *, force: bool = False) -> bool:
    """Write a user's (non-exempt) UserSetting kv to one encrypted doc. No-op when disabled."""
    if user is None or not getattr(user, "nostr_npub", None) or (not force and not enabled(db)):
        return False
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return False
    from app.models import UserSetting
    kv = {r.key: r.value for r in db.query(UserSetting).filter(UserSetting.user_id == user.id).all()
          if not _kv_exempt(r.key)}
    try:
        return await store.put_doc(_ss._port(db), op_sk, store.NS_USERCFG + user.nostr_npub, kv)
    except Exception as e:
        logger.warning("[users-store] sync_user_kv failed for %s: %s", user.nostr_npub[:16], e)
        return False


async def hydrate_user_kv(db) -> int:
    """relay → UserSetting cache. Restore each user's non-exempt kv from their usercfg doc (fills only
    MISSING keys — never clobbers a live local value like a freshly-linked token). Returns rows made."""
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return 0
    from app.models import UserSetting
    made = 0
    for user in db.query(User).filter(User.nostr_npub.isnot(None)).all():
        try:
            doc = await store.get_doc(_ss._port(db), store.NS_USERCFG + user.nostr_npub, seckey=op_sk)
        except Exception:
            doc = None
        if not isinstance(doc, dict):
            continue
        for k, v in doc.items():
            if _kv_exempt(k):
                continue
            if db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == k).first() is None:
                db.add(UserSetting(user_id=user.id, key=k, value=v))
                made += 1
    if made:
        db.commit()
    logger.info("[users-store] hydrated %d user-setting kv row(s) from relay", made)
    return made


# ---- reconcile sweep: SQL → relay catch-all -------------------------------------------------
# The per-mutation write-throughs (sync_user/sync_user_kv on the main settings save, caps, signup,
# avatar) cover the primary paths, but auxiliary feature endpoints write UserSetting/User columns
# WITHOUT calling them (the timezone save, and the caldav/carddav/webdav/music config saves whose
# keys aren't even traceable in app/). Rather than chase every endpoint (fragile, and new ones will
# forget), a periodic sweep mirrors EVERY npub account's authority record + non-exempt kv to the
# relay — so a setting changed via any path lands in Nostr within one interval. Change-detected by a
# content hash so an unchanged account isn't rewritten (no replaceable-event churn).
#
# That cache is IN MEMORY, so a restart empties it and the first sweep considered every account
# changed — on this deployment, 176 accounts x (record + kv) = ~352 replaceable docs rewritten with
# byte-identical content on every single deploy. Each one is then re-broadcast to every upstream
# relay (nostr_relay_backup_datastore, on by default), and the outbox is paced at ~1 event / 3s, so
# a deploy buried the outbound queue for ~20 minutes and drowned out every real post in it. It was
# invisible until Server Stats grew a queue-depth reading. _seed_hashes fixes it by asking the relay
# what it ALREADY holds, in two queries, before the first sweep runs.
_last_synced_hash: dict = {}
_reconcile_task = None
_RECONCILE_INTERVAL = 600   # seconds (10 min)


def _hash(record: dict, kv: dict) -> str:
    """The change-detection hash. ONE definition, used both to decide whether to write and to seed
    from what is already stored — if the two ever computed it differently, seeding would silently
    stop matching and every restart would resume rewriting everything."""
    import hashlib
    import json as _json
    return hashlib.sha256(
        _json.dumps([record, kv], sort_keys=True, default=str).encode()).hexdigest()


async def _seed_hashes(db) -> int:
    """Prime the cache from the relay's CURRENT docs, so a restart doesn't rewrite unchanged accounts.

    A bulk read of exactly the docs we care about, not two reads per user — and `get_docs`, not
    `list_docs`: the latter pulls every doc this key owns under a 5000 cap and filters client-side,
    which on the operator key (4028 docs, 2972 of them bookmarks and still growing) would one day
    start returning a partial answer with no signal, and the accounts it quietly omitted would go
    back to being rewritten on every restart.

    For each account we compare the stored pair against the local pair and, only when they are
    identical, record the hash the sweep would compute — so an account whose doc is missing, stale or
    unreadable is left unseeded and gets rewritten exactly as it does today. The comparison
    round-trips the local side through JSON first: a value that isn't JSON-native (no column in
    _SYNCED is one today, but it is a hand-edited list) is not equal to the string it was stored as,
    and comparing them directly would make every account look changed and seed nothing.

    Failure is a no-op, deliberately. If the relay can't be read we seed nothing and the sweep runs
    as it always has — this only ever REMOVES redundant writes, it can never skip a needed one.
    """
    op_sk = _ss._operator_seckey(db)
    if not op_sk:
        return 0
    import json as _json
    from app.models import UserSetting
    users = db.query(User).filter(User.nostr_npub.isnot(None)).all()
    if not users:
        return 0
    try:
        port = _ss._port(db)
        stored_rec = await store.get_docs(
            port, [store.NS_USER + u.nostr_npub for u in users], seckey=op_sk, strict=True)
        stored_kv = await store.get_docs(
            port, [store.NS_USERCFG + u.nostr_npub for u in users], seckey=op_sk, strict=True)
    except Exception as e:
        logger.warning("[users-store] could not read stored docs to seed change-detection (%s) — "
                       "this pass will re-sync every account", e)
        return 0
    seeded = 0
    for user in users:
        npub = user.nostr_npub
        rec = stored_rec.get(store.NS_USER + npub)
        kvd = stored_kv.get(store.NS_USERCFG + npub)
        if not isinstance(rec, dict) or not isinstance(kvd, dict):
            continue                       # never stored, or unreadable → let the sweep write it
        kv = {r.key: r.value
              for r in db.query(UserSetting).filter(UserSetting.user_id == user.id).all()
              if not _kv_exempt(r.key)}
        local = _json.loads(_json.dumps([_record(user), kv], sort_keys=True, default=str))
        if local == [rec, kvd]:
            _last_synced_hash[npub] = _hash(_record(user), kv)
            seeded += 1
    if seeded:
        logger.info("[users-store] %d account(s) already match the relay — not re-syncing them", seeded)
    return seeded


async def reconcile_all(db, *, force: bool = False) -> int:
    """SQL → relay sweep over all npub accounts; (re)sync only those whose record+kv changed since
    the last pass (or all when `force`). Returns the number (re)synced."""
    if not _ss._operator_seckey(db):
        return 0
    from app.models import UserSetting
    # First sweep after a restart: ask the relay what it already has, so identical content isn't
    # rewritten (and re-broadcast to every upstream relay) just because this process is new.
    if not force and not _last_synced_hash:
        await _seed_hashes(db)
    synced = 0
    for user in db.query(User).filter(User.nostr_npub.isnot(None)).all():
        kv = {r.key: r.value
              for r in db.query(UserSetting).filter(UserSetting.user_id == user.id).all()
              if not _kv_exempt(r.key)}
        h = _hash(_record(user), kv)
        if not force and _last_synced_hash.get(user.nostr_npub) == h:
            continue
        ok_rec = await sync_user(db, user, force=True)
        ok_kv = await sync_user_kv(db, user, force=True)
        if ok_rec or ok_kv:
            _last_synced_hash[user.nostr_npub] = h
            synced += 1
    if synced:
        logger.info("[users-store] reconciled %d account(s)+kv → relay (SQL→relay sweep)", synced)
    return synced


def start_users_reconcile() -> None:
    """Spawn the periodic SQL→relay reconcile task on the running loop (idempotent). Each pass opens
    its own DB session. No-op if already started or there's no running loop."""
    global _reconcile_task
    if _reconcile_task is not None:
        return

    async def _loop():
        from app.database import SessionLocal
        while True:
            await asyncio.sleep(_RECONCILE_INTERVAL)
            db = SessionLocal()
            try:
                await reconcile_all(db)
            except Exception as e:
                logger.warning("[users-store] periodic reconcile failed: %s", e)
            finally:
                db.close()

    try:
        _reconcile_task = asyncio.get_event_loop().create_task(_loop())
        logger.info("[users-store] periodic SQL→relay reconcile started (every %ds)", _RECONCILE_INTERVAL)
    except RuntimeError:
        pass   # no running loop (non-async caller) — startup wires it from the async hydrate task

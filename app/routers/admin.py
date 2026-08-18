from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi import Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, OperationalError
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
import logging
from app.database import get_db
from app.utils import lb_auth
from app.models import User, ExternalStorage
from app.schemas import UserCreate, UserResponse, SettingsUpdate, SettingsResponse
from app.auth import get_admin_user, get_password_hash
from app.services.email_service import get_email_service
from app.services.storage_service import StorageService
from app.services.video_transcode_service import transcode_video
from app.services.thumbnail_service import is_video_file
from pathlib import Path
import asyncio
import os

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# External Storage Management

class ExternalStorageCreate(BaseModel):
    name: str
    mount_path: str
    mount_point: str
    description: Optional[str] = None
    is_active: bool = True
    allowed_user_ids: Optional[List[int]] = None  # List of user IDs allowed to access


class ExternalStorageUpdate(BaseModel):
    name: Optional[str] = None
    mount_path: Optional[str] = None
    mount_point: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    allowed_user_ids: Optional[List[int]] = None  # List of user IDs allowed to access


class ExternalStorageResponse(BaseModel):
    id: int
    name: str
    mount_path: str
    mount_point: str
    description: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    allowed_user_ids: List[int] = []
    allowed_users: List[dict] = []  # User details for admin UI
    
    class Config:
        from_attributes = True


def _validate_external_storage_path(mount_path: str) -> bool:
    """Validate that external storage path is safe and exists."""
    try:
        path = Path(mount_path)
        # Must be absolute path
        if not path.is_absolute():
            return False
        
        # Must exist and be a directory
        if not path.exists() or not path.is_dir():
            return False
        
        # Must be readable
        if not os.access(path, os.R_OK):
            return False
        
        # Prevent mounting sensitive system directories
        forbidden_paths = ['/etc', '/sys', '/proc', '/dev', '/boot', '/root']
        for forbidden in forbidden_paths:
            if str(path).startswith(forbidden):
                return False
        
        return True
    except Exception:
        return False


def _validate_mount_point(mount_point: str) -> bool:
    """Validate mount point name (virtual path in file manager)."""
    if not mount_point:
        return False
    
    # Must be alphanumeric with dashes/underscores, no path separators
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', mount_point):
        return False
    
    # Reserved names
    reserved = ['home', 'root', 'api', 'static', 'admin', 'login', 'register']
    if mount_point.lower() in reserved:
        return False
    
    return True


@router.get("/external-storage", response_model=List[ExternalStorageResponse])
def get_external_storage(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get all external storage mounts with user access info."""
    mounts = db.query(ExternalStorage).order_by(ExternalStorage.name).all()
    result = []
    for mount in mounts:
        mount_dict = {
            "id": mount.id,
            "name": mount.name,
            "mount_path": mount.mount_path,
            "mount_point": mount.mount_point,
            "description": mount.description,
            "is_active": mount.is_active,
            "created_at": mount.created_at,
            "updated_at": mount.updated_at,
            "allowed_user_ids": [user.id for user in mount.allowed_users],
            "allowed_users": [
                {"id": user.id, "username": user.username, "email": user.email}
                for user in mount.allowed_users
            ]
        }
        result.append(mount_dict)
    return result


@router.post("/external-storage", response_model=ExternalStorageResponse, status_code=status.HTTP_201_CREATED)
def create_external_storage(
    data: ExternalStorageCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Create a new external storage mount."""
    # Validate mount path
    if not _validate_external_storage_path(data.mount_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount path. Path must be absolute, exist, be a directory, and be readable."
        )
    
    # Validate mount point
    if not _validate_mount_point(data.mount_point):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount point. Must be alphanumeric with dashes/underscores only."
        )
    
    # Check if mount point already exists
    existing = db.query(ExternalStorage).filter(
        ExternalStorage.mount_point == data.mount_point
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Mount point '{data.mount_point}' already exists"
        )
    
    # Create mount
    mount = ExternalStorage(
        name=data.name,
        mount_path=data.mount_path,
        mount_point=data.mount_point,
        description=data.description,
        is_active=data.is_active
    )
    
    # Set allowed users if provided
    if data.allowed_user_ids:
        users = db.query(User).filter(User.id.in_(data.allowed_user_ids)).all()
        mount.allowed_users = users
    
    db.add(mount)
    db.commit()
    db.refresh(mount)
    
    logger.info(f"Created external storage mount: {mount.name} -> {mount.mount_path} (mount_point: {mount.mount_point})")
    
    # Return with user info
    return {
        "id": mount.id,
        "name": mount.name,
        "mount_path": mount.mount_path,
        "mount_point": mount.mount_point,
        "description": mount.description,
        "is_active": mount.is_active,
        "created_at": mount.created_at,
        "updated_at": mount.updated_at,
        "allowed_user_ids": [user.id for user in mount.allowed_users],
        "allowed_users": [
            {"id": user.id, "username": user.username, "email": user.email}
            for user in mount.allowed_users
        ]
    }


@router.put("/external-storage/{mount_id}", response_model=ExternalStorageResponse)
def update_external_storage(
    mount_id: int,
    data: ExternalStorageUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Update an external storage mount."""
    mount = db.query(ExternalStorage).filter(ExternalStorage.id == mount_id).first()
    if not mount:
        raise HTTPException(status_code=404, detail="External storage mount not found")
    
    # Validate mount path if provided
    if data.mount_path and not _validate_external_storage_path(data.mount_path):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount path. Path must be absolute, exist, be a directory, and be readable."
        )
    
    # Validate mount point if provided
    if data.mount_point and not _validate_mount_point(data.mount_point):
        raise HTTPException(
            status_code=400,
            detail="Invalid mount point. Must be alphanumeric with dashes/underscores only."
        )
    
    # Check if mount point conflicts with another mount
    if data.mount_point and data.mount_point != mount.mount_point:
        existing = db.query(ExternalStorage).filter(
            ExternalStorage.mount_point == data.mount_point,
            ExternalStorage.id != mount_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Mount point '{data.mount_point}' already exists"
            )
    
    # Update fields
    if data.name is not None:
        mount.name = data.name
    if data.mount_path is not None:
        mount.mount_path = data.mount_path
    if data.mount_point is not None:
        mount.mount_point = data.mount_point
    if data.description is not None:
        mount.description = data.description
    if data.is_active is not None:
        mount.is_active = data.is_active
    
    # Update allowed users if provided
    if data.allowed_user_ids is not None:
        users = db.query(User).filter(User.id.in_(data.allowed_user_ids)).all()
        mount.allowed_users = users
    
    db.commit()
    db.refresh(mount)
    
    logger.info(f"Updated external storage mount: {mount.name}")
    
    # Return with user info
    return {
        "id": mount.id,
        "name": mount.name,
        "mount_path": mount.mount_path,
        "mount_point": mount.mount_point,
        "description": mount.description,
        "is_active": mount.is_active,
        "created_at": mount.created_at,
        "updated_at": mount.updated_at,
        "allowed_user_ids": [user.id for user in mount.allowed_users],
        "allowed_users": [
            {"id": user.id, "username": user.username, "email": user.email}
            for user in mount.allowed_users
        ]
    }


@router.delete("/external-storage/{mount_id}")
def delete_external_storage(
    mount_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Delete an external storage mount."""
    mount = db.query(ExternalStorage).filter(ExternalStorage.id == mount_id).first()
    if not mount:
        raise HTTPException(status_code=404, detail="External storage mount not found")
    
    db.delete(mount)
    db.commit()
    
    logger.info(f"Deleted external storage mount: {mount.name}")
    return {"message": "External storage mount deleted"}


_TEXT_KEYS_CACHE = None


def _settings_text_keys() -> set:
    """Names of SettingsResponse fields whose type is string (so an empty value is a legitimate
    CLEAR). Numeric/bool fields are excluded — saving '' for them would break the typed GET parse.
    Computed once from the schema so new string settings are clearable automatically."""
    global _TEXT_KEYS_CACHE
    if _TEXT_KEYS_CACHE is not None:
        return _TEXT_KEYS_CACHE
    import typing
    out = set()
    for name, field in SettingsResponse.model_fields.items():
        ann = field.annotation
        args = typing.get_args(ann)
        base = next((a for a in args if a is not type(None)), ann) if args else ann
        if base is str:
            out.add(name)
    # Secret tokens set OUT-OF-BAND (OAuth), never typed into the form: a blank field on Save must
    # mean "leave as-is", NOT clear — otherwise a normal Save wipes the token the admin never sees.
    out -= {"fedi_bridge_access_token", "fedi_bridge_admin_token", "turn_shared_secret", "stream_auth_secret"}
    _TEXT_KEYS_CACHE = out
    return out


@router.get("/settings", response_model=SettingsResponse)
def get_settings(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    from app.database import safe_query_settings
    settings = safe_query_settings(db)
    resp = SettingsResponse(**settings)
    # Mask secret tokens that are set OUT-OF-BAND (OAuth) so the admin form doesn't pre-fill them and
    # then re-submit a STALE value on Save, clobbering a freshly-connected token. These keys are
    # excluded from the clearable text-keys, so a blank field on Save means "leave as-is".
    for _k in ("fedi_bridge_access_token", "fedi_bridge_admin_token", "turn_shared_secret", "stream_auth_secret"):
        if getattr(resp, _k, None):
            setattr(resp, _k, "")
    return resp


@router.post("/models/{kind}/download")
def models_download(kind: str, admin: User = Depends(get_admin_user)):
    """Start an on-demand model download (kind = chat | image | music) in the background. Models are
    NOT auto-downloaded; this is the button behind each settings tab. Poll /models/{kind}/status."""
    from app.services import model_download_service as mds
    from app.database import SessionLocal
    mds.start(kind, SessionLocal)
    return mds.status(kind)


@router.get("/models/{kind}/status")
def models_status(kind: str, admin: User = Depends(get_admin_user)):
    """Status of a model download: {state: idle|running|done|error, message, pct}."""
    from app.services import model_download_service as mds
    return mds.status(kind)


@router.post("/nostr-relay/refresh-wot")
def refresh_nostr_relay_wot(admin: User = Depends(get_admin_user)):
    """Rebuild the Nostr relay's Web of Trust now (Admin → Relay button)."""
    from app.services.nostr_relay.thread import trigger_wot_refresh
    return trigger_wot_refresh()


@router.post("/nostr-relay/restore-datastore")
def nostr_relay_restore_datastore(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    """DR: pull the operator's encrypted pcai: CONFIG docs (settings/accounts/per-user config/bots)
    from the UPSTREAM relays into this (possibly fresh) node, then re-hydrate so they go live without
    a restart. Needs the operator nsec + 'Back up datastore' to have been ON when the data was saved."""
    import asyncio as _asyncio
    from app.services import settings_store, users_store

    async def _go():
        n = await settings_store.restore_from_upstream(db)
        if n:
            settings_store.hydrate_from_db(db)
            await settings_store.hydrate(db)
            await users_store.hydrate(db)
            await users_store.hydrate_user_kv(db)
        return n
    try:
        return {"ok": True, "restored": _asyncio.run(_go())}
    except Exception as e:
        logger.warning(f"[Admin] restore-datastore failed: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/nostr-relay/backup-datastore")
def nostr_relay_backup_datastore(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    """DR: re-publish ALL of the operator's encrypted pcai: CONFIG docs (settings/accounts/per-user
    config/bots) so the relay outbox federates the FULL current config to upstream. Federation is
    incremental (only docs written since 'Back up datastore' turned on reach upstream), so this seeds
    the rest in one shot. Paced so it can't overflow the outbox. Needs 'Back up datastore' ON."""
    import asyncio as _asyncio
    from app.services import settings_store
    if not settings_store.get_bool("nostr_relay_backup_datastore", True):
        return {"ok": False, "error": "Enable 'Back up datastore' first"}
    try:
        counts = _asyncio.run(settings_store.republish_datastore(db))
        return {"ok": True, "republished": counts}
    except Exception as e:
        logger.warning(f"[Admin] backup-datastore failed: {e}")
        return {"ok": False, "error": str(e)}


@router.post("/nostr-relay/backfill")
def nostr_relay_backfill(npub: str = Query(...), admin: User = Depends(get_admin_user)):
    """Backfill one or MORE users' Nostr history into the relay by npub/hex (Admin → Relay).
    `npub` accepts a single value or a comma/space/newline-separated list (batch a big sync)."""
    import re as _re
    from app.services.nostr import nostr_service
    from app.services.nostr_relay.thread import trigger_backfill
    started, bad = [], []
    for tok in _re.split(r"[\s,]+", (npub or "").strip()):
        if not tok:
            continue
        pk = nostr_service.to_pubkey_hex(tok)
        if pk:
            trigger_backfill(pk)
            started.append(pk)
        else:
            bad.append(tok)
    logger.info("[admin] relay sync requested by %s: %d user(s) queued%s",
                getattr(admin, "username", "?"), len(started),
                (" (%d invalid)" % len(bad)) if bad else "")
    if not started:
        return {"ok": False, "error": "no valid npub or hex pubkey", "bad": bad}
    return {"ok": True, "count": len(started), "pubkeys": [p[:12] for p in started], "bad": bad}


@router.post("/nostr-relay/purge-blocks")
def nostr_relay_purge_blocks(admin: User = Depends(get_admin_user)):
    """Apply the relay's blocked pubkeys/words/langs/bridge filters to already-stored notes now.
    Filtering is live at ingest and a purge runs nightly; this is the on-demand cleanup (heavy)."""
    from app.services.nostr_relay.thread import trigger_block_purge
    return trigger_block_purge()


@router.post("/nostr-relay/prune")
def nostr_relay_prune(dry_run: bool = False, admin: User = Depends(get_admin_user)):
    """Run the relay's auto-clean (age/retention prune) now instead of waiting for the daily loop.
    `?dry_run=1` counts what would be deleted without deleting it — worth doing first, this can be
    hundreds of thousands of rows on a relay that has never completed a prune cycle."""
    from app.services.nostr_relay.thread import trigger_prune
    logger.info("[Admin] relay auto-clean %s requested by %s",
                "DRY RUN" if dry_run else "run", getattr(admin, "username", "?"))
    return trigger_prune(dry_run=dry_run)


@router.get("/nostr-relay/paid-retention")
async def nostr_relay_paid_retention(admin: User = Depends(get_admin_user)):
    """Pay-to-stay: the configured policy plus who currently has a paid subscription.

    `known: false` means the ledger could not be READ (relay down, no ledger yet) — reported as
    such rather than as an empty subscriber list, because the same distinction is what stops the
    prune from deleting paying users' notes."""
    from app.services import paid_retention_service as prs
    out = prs.policy()
    out["subs"] = []
    try:
        ledger = await prs.load_ledger()
    except Exception as e:
        out["known"], out["error"] = False, str(e)
        return out
    if ledger is None:
        out["known"] = False
        out["error"] = "no ledger document yet"
        return out
    import time
    from app.services.nostr import nostr_service
    now = int(time.time())
    for pk, rec in sorted(ledger["subs"].items(), key=lambda kv: -kv[1]["until"]):
        out["subs"].append({"pubkey": pk, "npub": nostr_service.npub_of(pk),
                            "until": rec["until"], "live": rec["until"] > now,
                            "sats": rec.get("msats", 0) // 1000})
    out["known"] = True
    return out


@router.post("/nostr-relay/paid-retention/grant")
async def nostr_relay_paid_grant(body: dict, admin: User = Depends(get_admin_user)):
    """Add (or with negative days, take back) paid retention for one author. The ops surface for a
    payment that arrived some other way, a comped account, or a credit that needs undoing."""
    from app.services import paid_retention_service as prs
    try:
        rec = await prs.grant(str(body.get("pubkey") or ""), int(body.get("days") or 0))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    logger.info("[Admin] pay-to-stay grant %s day(s) to %s by %s", body.get("days"),
                str(body.get("pubkey"))[:16], getattr(admin, "username", "?"))
    return {"ok": True, "until": rec["until"]}


@router.get("/nostr-relay/status")
def nostr_relay_status(admin: User = Depends(get_admin_user)):
    from app.services.nostr_relay.thread import relay_status
    return relay_status()


@router.get("/nostr-relay/identity")
def nostr_relay_identity(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    """This node's relay operator identity — the key it signs its datastore docs with. Add the
    npub to ANOTHER node's Web-of-Trust seeds so that node accepts this one's events when it's an
    upstream. The nsec lets an admin reuse the same operator key across nodes. Mints a key if none
    exists yet."""
    from app.services import settings_store
    from app.services.nostr import nostr_service, bech32
    sk = settings_store._operator_seckey(db)
    if not sk:
        return {"ok": False, "error": "no operator key"}
    pub = nostr_service.derive_pubkey(sk)
    return {"ok": True, "npub": nostr_service.npub_of(pub), "nsec": bech32.encode("nsec", sk)}


@router.post("/nostr-relay/backfill-me")
def nostr_relay_backfill_me(admin: User = Depends(get_admin_user)):
    """Backfill the admin's own Nostr post history into the relay (Admin → Relay button)."""
    from app.services.nostr import nostr_service
    from app.services.nostr_relay.thread import trigger_backfill
    nsec = getattr(admin, "nostr_nsec", None)
    if not nsec:
        return {"ok": False, "error": "link a Nostr key in your user settings first"}
    try:
        pk = nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec))
    except Exception:
        return {"ok": False, "error": "could not derive your Nostr pubkey"}
    return trigger_backfill(pk)


class OnionToggle(BaseModel):
    enabled: bool


@router.get("/onion")
def get_onion(admin: User = Depends(get_admin_user)):
    """Current .onion state for this deployment (enabled flag + the persistent address, if up).
    The address is only reported when enabled — the hostname file lingers on disk for a stable
    re-enable, so a disabled onion must not look reachable."""
    from app.services import tor_service, settings_store
    enabled = settings_store.get_bool("onion_enabled")
    return {"enabled": enabled,
            "address": tor_service.get_onion_address() if enabled else None}


@router.post("/onion")
def set_onion(body: OnionToggle, admin: User = Depends(get_admin_user)):
    """One-click enable/disable this deployment's .onion hidden service. Applies it LIVE on the
    primary Tor daemon via SIGHUP (no full restart), THEN persists onion_enabled to the Nostr
    settings store (so the live state is established before we record it). The address persists across
    restarts (Tor keys on disk). Returns the address once Tor has written it (may be null on the first
    enable — the UI polls GET /onion). Requires Tor running."""
    from app.services import tor_service, settings_store
    if tor_service.primary_service() is None:
        raise HTTPException(status_code=409, detail="Enable the Managed Tor Service first.")
    target = f"127.0.0.1:{os.getenv('POSTERCHANAI_PORT', '3051')}"
    # The relay rides the same onion on its own port (see TorService.onion_relay_port) — without it
    # the onion serves the client shell but has no relay to talk to.
    addr = tor_service.set_onion(body.enabled, target,
                                 relay_port=settings_store.get_int("nostr_relay_port", 3052))
    settings_store.put("onion_enabled", "true" if body.enabled else "false")
    return {"enabled": body.enabled, "address": addr}


@router.put("/settings")
def update_settings(
    data: SettingsUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    # Track if cache settings changed
    cache_settings_changed = False
    cache_keys = {"file_cache_enabled", "file_cache_ttl", "file_cache_max_size"}
    # The admin UI sends ALL fields on every save; only the keys whose value actually CHANGED need to
    # be mirrored to the relay (each is its own replaceable event — don't rewrite ~250 on every save).
    changed_keys = set()

    try:
        from app.services import settings_store
        text_keys = _settings_text_keys()
        for key, value in data.settings.items():
            # Persist only keys whose value actually CHANGED. Empty is allowed ONLY for text settings
            # (so a TEXT field like the relay upstream list / blocklists can be CLEARED) — for typed
            # (number/bool) settings an empty string would break SettingsResponse parsing on the next
            # GET, and "" there just means "leave as-is" from a partial UI update, so skip it.
            if settings_store.get(key, "") != value and (value != "" or key in text_keys):
                settings_store.put(key, value)   # updates the cache + writes through to the relay
                changed_keys.add(key)
            if key in cache_keys:
                cache_settings_changed = True
        logger.info(f"[Admin] Saved {len(changed_keys)} changed setting(s)")

        # Relay TASK-TOPOLOGY keys force a full subprocess restart (see the restart block below).
        # Computed up front so the LIVE-reload blocks (upstream / store-config) can SKIP themselves
        # when a restart is already going to happen in the same save — otherwise a combined save
        # (e.g. upstream + send_only) would do the live reconnect AND then throw it away with a
        # restart, reintroducing the ~90s /client outage the live path exists to avoid.
        _relay_topology_keys = (
            "nostr_relay_send_only", "nostr_relay_wot_enabled", "nostr_relay_firehose_enabled",
            "nostr_relay_mirror_feeds", "nostr_relay_disable_proxy",
            "nostr_relay_bind", "nostr_relay_port",
        )
        _relay_will_restart = any(k in changed_keys for k in _relay_topology_keys)

        # If the relay blocklist/filters were edited in the UI, push them to the running relay
        # immediately (otherwise the change wouldn't apply until restart / daily refresh).
        if any(k in data.settings for k in ("nostr_relay_blocked_pubkeys", "nostr_relay_blocked_words", "nostr_relay_blocked_langs", "nostr_relay_blocked_relays", "nostr_relay_block_bridged")):
            try:
                from app.services.nostr_relay.thread import trigger_block_reload
                trigger_block_reload()
            except Exception as e:
                logger.warning(f"[Admin] relay block reload after settings save failed: {e}")
        # NIP-05 identities edited → push to the running relay (serves /.well-known/nostr.json)
        if any(k in data.settings for k in ("nostr_relay_nip05_enabled", "nostr_relay_nip05_names", "nostr_relay_nip05_relays")):
            try:
                from app.services.nostr_relay.thread import trigger_nip05_reload
                trigger_nip05_reload()
            except Exception as e:
                logger.warning(f"[Admin] relay NIP-05 reload after settings save failed: {e}")
        # Upstream relay set / firehose breadth / ingest kinds changed → reconnect the relay's
        # firehose + outbox LIVE instead of restarting the subprocess. The relay is the app's
        # datastore, so a restart drops every /client connection and blocks settings writes for ~90s;
        # the firehose can just reconnect to the new config in place (like the block/NIP-05 reloads
        # above). Flush the new values to the datastore SYNCHRONOUSLY first — the relay re-reads its
        # config from Postgres on reload, and the normal write-through is async, so without this it
        # could read STALE values and ignore the change.
        # nostr_relay_private_relays is here so clearing it actually STOPS the mirror: without a
        # reload the relay keeps copying every private write to a relay the operator has removed.
        _relay_reload_keys = ("nostr_relay_private_relays",
                              "nostr_relay_upstream_relays", "nostr_relay_firehose_max_relays",
                              "nostr_relay_ingest_kinds")
        if not _relay_will_restart and any(k in changed_keys for k in _relay_reload_keys):
            flushed = False
            try:
                import asyncio as _asyncio
                flush = {k: settings_store.get(k, "") for k in changed_keys if k in _relay_reload_keys}
                # Gate on the actual count written, not just "no exception": write_through swallows
                # per-key failures and returns 0 (e.g. no operator key), so a flush that persisted
                # NOTHING must not fire the reload — else the relay re-reads STALE config and
                # reconnects to the OLD set (the exact bug the synchronous flush prevents).
                flushed = bool(flush) and _asyncio.run(settings_store.write_through(None, flush)) > 0
            except Exception as e:
                logger.warning(f"[Admin] pre-reload relay flush failed: {e}")
            # Only fire the reload if the durable flush actually persisted. A failed flush leaves the
            # running relay on its current config; the next successful save reapplies.
            if flushed:
                try:
                    from app.services.nostr_relay.thread import trigger_upstream_reload
                    trigger_upstream_reload()
                    logger.info("[Admin] relay firehose reconnect requested (upstream/ingest changed, no restart)")
                except Exception as e:
                    logger.warning(f"[Admin] relay upstream reload after settings save failed: {e}")
        # Prune retention / count cap changed → apply to the running relay's store LIVE. store
        # .retention_days / .max_events are read by the nightly prune but were only set at relay
        # startup, so editing them in the UI did nothing until a restart (symptom: "I set prune to 0
        # but old notes still get deleted"). Flush durably first (relay re-reads from Postgres), then
        # push the live update — same pattern as the upstream reload above, no restart.
        # The pay-to-stay windows ride the same path — they are prune inputs like the two above, and
        # a tier change an admin can't see take effect until a restart is a tier change they will
        # assume is broken.
        _relay_store_keys = ("nostr_relay_retention_days", "nostr_relay_max_events",
                             "nostr_relay_paid_retention_enabled", "nostr_relay_free_retention_days",
                             "nostr_relay_paid_retention_days")
        if not _relay_will_restart and any(k in changed_keys for k in _relay_store_keys):
            flushed = False
            try:
                import asyncio as _asyncio
                flush = {k: settings_store.get(k, "") for k in changed_keys if k in _relay_store_keys}
                flushed = bool(flush) and _asyncio.run(settings_store.write_through(None, flush)) > 0
            except Exception as e:
                logger.warning(f"[Admin] pre-reload relay store-config flush failed: {e}")
            if flushed:
                try:
                    from app.services.nostr_relay.thread import trigger_store_config_reload
                    trigger_store_config_reload()
                    logger.info("[Admin] relay store config reload requested (retention/max_events/mirror-retention, no restart)")
                except Exception as e:
                    logger.warning(f"[Admin] relay store-config reload after settings save failed: {e}")
        # Git-host topology changed (enable / bind / port / proxy_url — all per-node plumbing) →
        # reconcile the running git-host subprocess so an Admin toggle takes effect WITHOUT a full app
        # restart (symptom otherwise: "I enabled the git host but :3053 never comes up"). put() already
        # wrote the new plumbing values to local_settings.json + the cache, and start_git_http re-reads
        # them; if the node is now a proxy or disabled, start_git_http is a no-op.
        _git_topology_keys = ("git_server_enabled", "git_server_bind", "git_server_port",
                              "git_server_proxy_url")
        if any(k in changed_keys for k in _git_topology_keys):
            try:
                from app.services.git_http_service import stop_git_http, start_git_http
                stop_git_http()
                start_git_http()
                logger.info("[Admin] git host reconciled after settings change (%s)",
                            ",".join(k for k in changed_keys if k in _git_topology_keys))
            except Exception as e:
                logger.warning(f"[Admin] git host reconcile after settings save failed: {e}")
        # Blossom mirror list / public URL / enable changed → re-advertise the operator's kind-10063
        # server list so clients pick up the new failover targets (off-thread; needs the relay + loop).
        if any(k in changed_keys for k in ("blossom_mirror_servers", "blossom_public_url", "blossom_enabled")):
            try:
                import threading as _threading, asyncio as _asyncio
                from app.services import blossom_service
                from app.database import SessionLocal as _SessionLocal
                def _advertise():
                    _db = _SessionLocal()
                    try:
                        _asyncio.run(blossom_service.publish_operator_server_list(_db))
                    except Exception as e:
                        logger.warning(f"[Admin] blossom kind-10063 publish failed: {e}")
                    finally:
                        _db.close()
                _threading.Thread(target=_advertise, daemon=True).start()
            except Exception as e:
                logger.warning(f"[Admin] could not schedule blossom server-list publish: {e}")
        # Relay TASK-TOPOLOGY settings only take effect when the relay rebuilds its config at startup
        # — they decide which background tasks run (send-only vs firehose/sync, mirror-feeds sweep)
        # or where/how it connects. Toggling them in the UI must RESTART the relay, else the already-
        # running firehose ignores the change (symptom: "I clicked send-only but it's still pulling
        # posts"). Restart in the background so the admin save returns promptly. (_relay_topology_keys
        # / _relay_will_restart are computed above so the live-reload blocks can defer to the restart.)
        # NOTE: nostr_relay_upstream_relays / firehose_max_relays / ingest_kinds / retention_days /
        # max_events are NOT here — they apply live (see the reload blocks above), no restart needed.
        if _relay_will_restart:
            # Flush the changed settings to the relay datastore SYNCHRONOUSLY first. The restarted
            # relay re-reads its config from the datastore (postgres), and the normal write is async
            # — without this, the relay could read STALE config on restart and ignore the change
            # (e.g. "I set the upstream but it still uses the defaults") until the next restart.
            try:
                import asyncio as _asyncio
                flush = {k: settings_store.get(k, "") for k in changed_keys}
                if flush:
                    _asyncio.run(settings_store.write_through(None, flush))
            except Exception as e:
                logger.warning(f"[Admin] pre-restart settings flush failed: {e}")
            try:
                import threading as _threading
                def _restart_relay():
                    try:
                        from app.services.nostr_relay.thread import restart_nostr_relay
                        restart_nostr_relay()
                        logger.info("[Admin] relay restarted to apply a task-topology setting change")
                    except Exception as e:
                        logger.warning(f"[Admin] relay restart after settings save failed: {e}")
                _threading.Thread(target=_restart_relay, daemon=True).start()
            except Exception as e:
                logger.warning(f"[Admin] could not schedule relay restart: {e}")
        # (settings_store.put above already mirrors each changed key to the relay datastore.)

    except IntegrityError as e:
        # Handle constraint violations (e.g., unique constraint, foreign key)
        db.rollback()
        logger.error(f"[Admin] Integrity error saving settings: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except OperationalError as e:
        # Handle database connection/operational errors
        db.rollback()
        logger.error(f"[Admin] Database operational error saving settings: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable. Please try again.")
    except SQLAlchemyError as e:
        # Handle other database errors
        db.rollback()
        logger.error(f"[Admin] Database error saving settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except Exception as e:
        # Handle unexpected errors
        db.rollback()
        logger.error(f"[Admin] Unexpected error saving settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
    # Reload file cache if cache settings changed (after successful commit)
    if cache_settings_changed:
        try:
            from app.routers.files import get_file_cache
            get_file_cache(db, force_reload=True)
            logger.info("[Admin] File cache settings updated, cache reloaded")
        except Exception as e:
            logger.warning(f"[Admin] Failed to reload file cache: {e}")

    return {"message": "Settings updated"}


@router.get("/users", response_model=List[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    return db.query(User).all()


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    # Check if username already exists
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists"
        )

    # New users default to chat + image + music only; video and torrents stay off until
    # the admin grants them via the per-user toggles (matches the self-signup default).
    user = User(
        username=user_data.username,
        password_hash=get_password_hash(user_data.password),
        is_admin=user_data.is_admin,
        can_image=True,
        can_music=True,
        can_video=False,
        can_torrent=False,
        can_blossom=False,
        can_stream=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete yourself"
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    npub = user.nostr_npub   # capture before delete — needed to remove the relay account docs

    try:
        # Manually delete related records in the correct order to avoid foreign key violations
        # This is necessary because existing databases might not have CASCADE constraints

        from app.models import (
            Conversation, Message, UserSetting, APIKey, VerificationToken
        )


        # 1. Delete messages (referenced by conversations)
        # Get conversation IDs first to avoid subquery issues
        conversation_ids = [c.id for c in db.query(Conversation.id).filter(Conversation.user_id == user_id).all()]
        if conversation_ids:
            db.query(Message).filter(Message.conversation_id.in_(conversation_ids)).delete(synchronize_session=False)

        # 2. Delete conversations
        db.query(Conversation).filter(Conversation.user_id == user_id).delete(synchronize_session=False)

        # 7. Delete user settings
        db.query(UserSetting).filter(UserSetting.user_id == user_id).delete(synchronize_session=False)
        
        # 8. Delete API keys
        db.query(APIKey).filter(APIKey.user_id == user_id).delete(synchronize_session=False)
        
        # 9. Delete verification tokens
        db.query(VerificationToken).filter(VerificationToken.user_id == user_id).delete(synchronize_session=False)
        
        
        # 11. Finally, delete the user
        db.delete(user)
        db.commit()

        # Remove the account's relay docs too, or a fresh-node rebuild would resurrect it.
        if npub:
            from app.services import users_store
            users_store.delete_user_blocking(db, npub)

        logger.info(f"User {user_id} ({user.username}) deleted by admin {admin.id}")
        return {"message": "User deleted"}
        
    except IntegrityError as e:
        db.rollback()
        logger.error(f"Failed to delete user {user_id}: IntegrityError - {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user due to database constraints: {str(e)}"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete user {user_id}: {type(e).__name__} - {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user: {str(e)}"
        )


class PasswordUpdate(BaseModel):
    password: str

@router.put("/users/{user_id}/password")
def update_user_password(
    user_id: int,
    data: PasswordUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.password_hash = get_password_hash(data.password)
    db.commit()
    return {"message": "Password updated"}


@router.put("/users/{user_id}/storage-quota")
def update_user_storage_quota(
    user_id: int,
    quota_mb: float = Query(..., description="Storage quota in MB (0 = unlimited)"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Update user storage quota."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Convert MB to bytes (0 = unlimited)
    quota_bytes = int(quota_mb * 1024 * 1024) if quota_mb > 0 else 0
    user.storage_quota = quota_bytes
    db.commit()
    from app.services import users_store
    users_store.sync_user_blocking(db, user)   # storage_quota → relay

    return {"message": "Storage quota updated", "quota_mb": quota_mb, "quota_bytes": quota_bytes}


@router.put("/users/{user_id}/capabilities", response_model=UserResponse)
def update_user_capabilities(
    user_id: int,
    can_image: bool = Query(...),
    can_music: bool = Query(...),
    can_video: bool = Query(...),
    can_torrent: bool = Query(...),
    can_blossom: bool = Query(False),
    can_stream: bool = Query(False),
    can_ai: bool = Query(True),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Set a user's per-feature access (image/music/video/torrent/blossom/ai). Admins are always
    allowed regardless of these flags."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.can_image = can_image
    user.can_music = can_music
    user.can_video = can_video
    user.can_torrent = can_torrent
    _was_blossom, _was_ai, _was_stream = bool(user.can_blossom), bool(user.can_ai), bool(user.can_stream)
    user.can_blossom = can_blossom
    user.can_ai = can_ai
    user.can_stream = can_stream
    _newly = [k for k, was, now in (("ai", _was_ai, can_ai), ("blossom", _was_blossom, can_blossom),
                                    ("stream", _was_stream, can_stream)) if now and not was]
    # Remember a REVOCATION, so the automatic fediverse-sign-in grant can't hand it straight back the
    # next time they log in. Granting either capability clears the mark.
    if (_was_ai and not can_ai) or (_was_blossom and not can_blossom):
        user.access_revoked = True
    elif (can_ai and not _was_ai) or (can_blossom and not _was_blossom):
        user.access_revoked = False
    db.commit()
    db.refresh(user)
    from app.services import users_store
    users_store.sync_user_blocking(db, user)   # caps → relay (authoritative)
    # DM the user about anything newly GRANTED (not revoked, not re-saved unchanged).
    if _newly:
        from app.services.access_notify_service import notify_access_granted_blocking
        notify_access_granted_blocking(db, user, _newly)   # one message, however many caps changed
    logger.info(f"[ADMIN] Updated capabilities for user {user_id} ({user.username}): "
                f"image={can_image} music={can_music} video={can_video} torrent={can_torrent} "
                f"blossom={can_blossom} ai={can_ai} stream={can_stream}")
    return user


@router.post("/storage/rescan")
async def rescan_storage(
    request: Request,
    user_id: Optional[int] = Query(None, description="User ID to rescan (None = all users)"),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """
    Scan file storage for a user or all users. Performs comprehensive file scanning:
    - Restores EXIF timestamps from photo/video metadata
    - Generates thumbnails for images and videos
    - Updates file index and database consistency
    - Invalidates file caches
    """
    from app.services.storage_service import get_storage_service
    from app.routers.files import get_file_cache
    from app.services.thumbnail_service import generate_thumbnails_for_user
    from app.services import settings_store

    # Check if storage server is configured - proxy the scan request if so
    storage_server_url = settings_store.get("storage_server_url", "")
    if storage_server_url:
        url = storage_server_url.strip()
        if url.startswith(('http://', 'https://')):
            logger.info(f"[ADMIN] Proxying storage rescan to storage server: {url}")
            try:
                import httpx
                headers = lb_auth.headers()
                access_token = request.cookies.get("access_token")
                auth_header = request.headers.get("Authorization")
                if auth_header:
                    headers["Authorization"] = auth_header
                elif access_token:
                    headers["Cookie"] = f"access_token={access_token}"
                
                # Proxy to storage server with long timeout for large scans
                async with httpx.AsyncClient(timeout=600.0) as client:  # 10 minutes
                    params = {}
                    if user_id:
                        params["user_id"] = user_id
                    
                    response = await client.post(
                        f"{url}/api/admin/storage/rescan",
                        params=params,
                        headers=headers
                    )
                    
                    if response.status_code == 200:
                        return response.json()
                    else:
                        logger.error(f"[ADMIN] Storage server rescan failed: {response.status_code} - {response.text[:500]}")
                        raise HTTPException(status_code=response.status_code, detail=f"Storage server error: {response.text[:200]}")
            except httpx.TimeoutException:
                logger.error(f"[ADMIN] Timeout proxying rescan to storage server")
                raise HTTPException(status_code=504, detail="Storage server scan timeout (this is normal for large collections)")
            except httpx.ConnectError as e:
                logger.error(f"[ADMIN] Cannot connect to storage server: {e}")
                raise HTTPException(status_code=503, detail=f"Cannot reach storage server: {e}")
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"[ADMIN] Failed to proxy rescan: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to proxy scan: {str(e)}")
    
    def _scan_user_files(user_id: int, username: str):
        """Scan files for a single user - includes EXIF, thumbnails, and indexing. Uses local filesystem."""
        # Create a NEW database session for this thread (SQLite sessions are not thread-safe)
        import sys
        import os
        
        # Ensure Python path includes the project root (important for thread execution)
        # Try multiple methods to get project root
        project_root = None
        try:
            # Method 1: Use __file__ if available
            current_file = os.path.abspath(__file__)
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(current_file)))
        except NameError:
            # Method 2: Use inspect to get file location
            try:
                import inspect
                current_file = inspect.getfile(_scan_user_files)
                project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(current_file))))
            except Exception:
                # Method 3: Try common project root locations
                possible_roots = [
                    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    os.path.expanduser('~/posterchanai'),
                    os.getcwd()
                ]
                for root in possible_roots:
                    if os.path.exists(os.path.join(root, 'app', 'services', 'storage_service.py')):
                        project_root = root
                        break
        
        if project_root and os.path.exists(project_root):
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            # Change to project root directory
            try:
                os.chdir(project_root)
            except Exception:
                pass  # If chdir fails, continue anyway
        
        from app.database import SessionLocal
        thread_db = SessionLocal()
        try:
            # Import after path is configured
            from app.utils.exif_utils import batch_restore_timestamps
            
            # Invalidate file cache for this user
            cache = get_file_cache(thread_db)
            cache.invalidate(f"{username}:")
            
            # Initialize stats
            file_count = 0
            dir_count = 0
            exif_stats = {'restored': 0, 'processed': 0}
            thumbnail_stats = {'successful': 0, 'failed': 0}
            
            # Scan local filesystem
            storage = get_storage_service(thread_db)
            user_path = storage.get_user_path(username)
            
            if user_path.exists():
                # Step 1: Restore EXIF timestamps for all media files
                logger.info(f"[File Scan] Step 1/3: Restoring EXIF timestamps for user {username}")
                logger.info(f"[File Scan] This will update file modification times from EXIF metadata")
                logger.info(f"[File Scan] Files copied via rsync will get their original photo/video dates restored")
                exif_stats = batch_restore_timestamps(user_path)
                logger.info(f"[File Scan] EXIF stats: {exif_stats['restored']} restored, {exif_stats['processed']} processed, {exif_stats.get('skipped', 0)} skipped, {exif_stats.get('errors', 0)} errors")
                
                # Step 2: Generate thumbnails
                logger.info(f"[File Scan] Step 2/3: Generating thumbnails for user {username}")
                successful, failed = generate_thumbnails_for_user(user_path)
                thumbnail_stats = {'successful': successful, 'failed': failed}
                logger.info(f"[File Scan] Thumbnail stats: {successful} generated, {failed} failed")
                
                # Step 3: Count files for indexing
                logger.info(f"[File Scan] Step 3/3: Indexing files for user {username}")
                for item in user_path.rglob('*'):
                    try:
                        if item.is_file():
                            file_count += 1
                        elif item.is_dir():
                            dir_count += 1
                    except Exception as e:
                        logger.warning(f"Error processing {item} for user {username}: {e}")
                        continue
            
            logger.info(f"[File Scan] Complete for {username}: {file_count} files, {dir_count} directories")
            return {
                "user_id": user_id,
                "username": username,
                "files": file_count,
                "directories": dir_count,
                "exif_restored": exif_stats.get('restored', 0),
                "exif_processed": exif_stats.get('processed', 0),
                "thumbnails_generated": thumbnail_stats.get('successful', 0),
                "thumbnails_failed": thumbnail_stats.get('failed', 0),
                "storage_type": "local",
                "status": "success"
            }
        except Exception as e:
            logger.error(f"[File Scan] Error scanning user {username}: {e}", exc_info=True)
            return {
                "user_id": user_id,
                "username": username,
                "status": "error",
                "error": str(e)
            }
        finally:
            # Always close the thread-local database session
            thread_db.close()
    
    # Run scan in thread pool to avoid blocking
    if user_id:
        # Scan specific user
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Pass primitive values, not ORM objects (to avoid session issues in threads)
        result = await asyncio.to_thread(_scan_user_files, user.id, user.username)
        return {
            "message": f"File scan completed for user {user.username}",
            "results": [result]
        }
    else:
        # Scan all users - extract primitive values before passing to threads
        users = [(u.id, u.username) for u in db.query(User).all()]
        results = []
        
        # Scan all users in parallel (but in thread pool)
        # Each user is a tuple of (user_id, username)
        tasks = [asyncio.to_thread(_scan_user_files, uid, uname) for uid, uname in users]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle any exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                uid, uname = users[i]
                logger.error(f"[File Scan] Exception for user {uname}: {result}")
                processed_results.append({
                    "user_id": uid,
                    "username": uname,
                    "status": "error",
                    "error": str(result)
                })
            else:
                processed_results.append(result)
        
        # Calculate summary stats
        total_files = sum(r.get("files", 0) for r in processed_results if r.get("status") == "success")
        total_dirs = sum(r.get("directories", 0) for r in processed_results if r.get("status") == "success")
        total_exif_restored = sum(r.get("exif_restored", 0) for r in processed_results if r.get("status") == "success")
        total_thumbnails = sum(r.get("thumbnails_generated", 0) for r in processed_results if r.get("status") == "success")
        success_count = sum(1 for r in processed_results if r.get("status") == "success")
        
        return {
            "message": f"File scan completed for {len(users)} user(s)",
            "summary": {
                "total_users": len(users),
                "successful": success_count,
                "failed": len(users) - success_count,
                "total_files": total_files,
                "total_directories": total_dirs,
                "total_exif_restored": total_exif_restored,
                "total_thumbnails_generated": total_thumbnails
            },
            "results": processed_results
        }


class TestEmailRequest(BaseModel):
    to_email: str


@router.post("/test-email")
def send_test_email(
    data: TestEmailRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Send a test email to verify SMTP configuration"""
    email_service = get_email_service(db)

    if not email_service.smtp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP is not enabled. Enable it in settings first."
        )

    success, message = email_service.send_test_email(data.to_email)

    if success:
        return {"success": True, "message": message}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message
        )


@router.post("/reload-model")
def reload_model(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Reload the native LLM model."""
    from app.services.inference_factory import reload_inference_model

    try:
        reload_inference_model(db)
        return {"success": True, "message": "Model reloaded successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reload model: {str(e)}"
        )


@router.get("/model-status")
def get_model_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get the current LLM model status"""
    from app.services.inference_factory import get_inference_status

    return get_inference_status(db)


@router.post("/reload-image-model")
def reload_image_model(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Reload the image generation model (for native backend)"""
    from app.services.image_factory import reload_image_model, get_image_backend_info

    info = get_image_backend_info(db)

    if info.get("backend") != "native":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model reload is only available for native image backend. Current backend: " + info.get("backend", "unknown")
        )

    try:
        reload_image_model(db)
        return {"success": True, "message": "Image model reloaded successfully"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reload image model: {str(e)}"
        )


@router.get("/image-status")
def get_image_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get the current image generation backend status"""
    from app.services.image_factory import get_image_backend_info

    return get_image_backend_info(db)


@router.get("/vram-status")
def get_vram_status(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Get the current VRAM status (which models are loaded)"""
    from app.services.vram_manager import get_vram_status

    return get_vram_status(db)


@router.post("/transcode-video")
async def transcode_video_admin(
    username: str = Query(..., description="Username to transcode videos for"),
    file_path: Optional[str] = Query(None, description="Specific file path to transcode (optional, if not provided, transcodes all videos)"),
    force: bool = Query(False, description="Force re-transcoding even if transcoded version exists"),
    db: Session = Depends(get_admin_user)
):
    """
    Transcode video(s) for a user. Can transcode a specific video or all videos.
    Admin only.
    """
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    if not user_path.exists():
        raise HTTPException(status_code=404, detail=f"User storage not found: {username}")
    
    if file_path:
        # Transcode specific file
        try:
            safe_path = Path(*[p for p in file_path.split('/') if p])
            video_path = user_path / safe_path
            
            if not video_path.exists():
                raise HTTPException(status_code=404, detail="Video file not found")
            
            if not is_video_file(video_path):
                raise HTTPException(status_code=400, detail="File is not a video")
            
            # Check if transcoded version already exists (unless force)
            from app.services.video_transcode_service import get_transcoded_video_if_exists
            if not force:
                existing_transcoded = get_transcoded_video_if_exists(user_path, video_path)
                if existing_transcoded:
                    return {
                        "message": "Transcoded version already exists",
                        "file": str(video_path.relative_to(user_path)),
                        "transcoded": str(existing_transcoded.relative_to(user_path)),
                        "skipped": True
                    }
            
            # Transcode (always transcode to ensure web-optimized format)
            transcoded_path = await asyncio.to_thread(transcode_video, user_path, video_path)
            
            if transcoded_path:
                return {
                    "message": "Video transcoded successfully",
                    "file": str(video_path.relative_to(user_path)),
                    "transcoded": str(transcoded_path.relative_to(user_path))
                }
            else:
                raise HTTPException(status_code=500, detail="Video transcoding failed")
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error transcoding video {file_path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Error transcoding video: {str(e)}")
    else:
        # Transcode all videos for user
        def _find_all_videos():
            videos = []
            for item in user_path.rglob('*'):
                if item.is_file() and is_video_file(item):
                    # Skip already transcoded videos
                    if '.transcoded' not in str(item):
                        videos.append(item)
            return videos
        
        videos = await asyncio.to_thread(_find_all_videos)
        
        if not videos:
            return {
                "message": "No videos found to transcode",
                "count": 0
            }
        
        # Transcode videos in background (don't block response)
        async def _transcode_all():
            transcoded = 0
            failed = 0
            skipped = 0
            
            for video_path in videos:
                try:
                    # Check if transcoded version already exists (unless force)
                    if not force:
                        from app.services.video_transcode_service import get_transcoded_video_if_exists
                        existing_transcoded = get_transcoded_video_if_exists(user_path, video_path)
                        if existing_transcoded:
                            skipped += 1
                            continue
                    
                    # Always transcode to ensure web-optimized format
                    transcoded_path = await asyncio.to_thread(transcode_video, user_path, video_path)
                    if transcoded_path:
                        transcoded += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(f"Error transcoding {video_path.name}: {e}")
                    failed += 1
            
            logger.info(f"Video transcoding complete for {username}: {transcoded} transcoded, {failed} failed, {skipped} skipped")
        
        # Start transcoding in background
        asyncio.create_task(_transcode_all())
        
        return {
            "message": f"Started transcoding {len(videos)} video(s) in background",
            "count": len(videos),
            "status": "processing"
        }


@router.get("/transcode-status")
async def get_transcode_status(
    username: str = Query(..., description="Username to check transcoding status for"),
    db: Session = Depends(get_admin_user)
):
    """
    Get transcoding status for a user's videos.
    Admin only.
    """
    storage = StorageService(db)
    user_path = storage.get_user_path(username)
    
    if not user_path.exists():
        raise HTTPException(status_code=404, detail=f"User storage not found: {username}")
    
    def _get_status():
        from app.services.video_transcode_service import get_transcoded_video_if_exists
        
        all_videos = []
        transcoded_videos = []
        optimized_videos = []
        
        for item in user_path.rglob('*'):
            if item.is_file() and is_video_file(item):
                # Skip transcoded files themselves
                if '.transcoded' in str(item):
                    continue
                
                all_videos.append(item)
                
                # Check if transcoded version exists
                transcoded_path = get_transcoded_video_if_exists(user_path, item)
                if transcoded_path:
                    transcoded_videos.append(item)
        
        return {
            "total_videos": len(all_videos),
            "transcoded": len(transcoded_videos),
            "needs_transcoding": len(all_videos) - len(transcoded_videos)
        }
    status = await asyncio.to_thread(_get_status)

    return status


@router.post("/blossom/scan")
async def blossom_scan(
    payload: dict | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Does this node hold what its database says it holds?

    A blob row and the bytes behind it live in two different places, and nothing ever compared them.
    When they disagree the symptom lands on somebody's phone — a download that 404s on every sweep,
    for ever, with no way for the client to tell the difference between "gone" and "not yet" — and
    finding out meant reading the access log and hand-querying the database.

    Read-only. `deep` re-hashes every file against the sha it is stored under, which is the only way
    to catch bytes that rotted rather than vanished, and is much slower.
    """
    from app.services import blossom_service
    body = payload or {}
    try:
        # Clamped: a negative LIMIT is a driver error, which comes back as a 500 carrying the raw
        # message and leaves the request's transaction aborted.
        want = int(body.get("limit") or 0)
        out = await blossom_service.scan_store(db, limit=max(0, min(want, 500000)),
                                               deep=bool(body.get("deep")))
    except Exception as e:
        logger.error("blossom scan failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    logger.info("[admin] blossom scan: %d checked, %d missing, %d unknown, %d orphans",
                out.get("checked", 0), len(out.get("missing", [])), out.get("unknown", 0),
                out.get("orphans", 0))
    return {"ok": True, "scan": out}


@router.post("/blossom/forget-missing")
async def blossom_forget_missing(
    payload: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Drop rows whose bytes this node does not have.

    Nothing is deleted from storage — that is what "missing" means. It stops the node claiming to
    hold something it does not, which is what makes a client's own repair possible: while the row is
    there, every device is told the file exists and cannot do anything about it.

    The list is passed in explicitly from a scan the admin just ran, never re-derived here: a fresh
    probe could answer differently in the seconds in between, and this deletes rows.
    """
    from app.services import blossom_service
    shas = [s for s in (payload.get("shas") or []) if isinstance(s, str) and len(s) == 64]
    if not shas:
        return {"ok": True, "removed": 0, "kept": 0, "unknown": 0}
    out = await blossom_service.forget_missing(db, shas)
    logger.info("[admin] blossom forget-missing: dropped %d, kept %d (still there), %d unanswered%s",
                out.get("removed", 0), out.get("kept", 0), out.get("unknown", 0),
                (" — REFUSED: " + out["refused"]) if out.get("refused") else "")
    return dict({"ok": True}, **out)


@router.post("/run-logs")
async def run_logs_scheduler(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Manually trigger the logs scheduler."""
    from app.services.logs_scheduler import run_logs_for_admin
    import asyncio
    
    try:
        await run_logs_for_admin()
        return {"ok": True, "message": "Logs scheduler executed"}
    except Exception as e:
        logger.error(f"Error running logs scheduler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stats-preview")
async def stats_preview(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Build the Nostr Stats chart and return it (base64 PNG + summary) for display — NO posting,
    NO Telegram. Used by the 'Preview' button on the Nostr Stats bot feature."""
    from app.services.stats_bot_service import build_stats
    import base64
    try:
        summary, png = await build_stats()
        return {"ok": True, "summary": summary,
                "image": "data:image/png;base64," + base64.b64encode(png).decode()}
    except Exception as e:
        logger.error(f"Stats preview failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class StatsRunBody(BaseModel):
    nsec: str = ""


@router.post("/stats-run")
async def stats_run(
    body: StatsRunBody,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Post the Nostr Stats chart to Nostr NOW, from the given bot account (its nsec) — to the local
    relay (which federates it). Nostr-only."""
    from app.services.stats_bot_service import post_stats
    if not (body.nsec or "").strip():
        raise HTTPException(status_code=400, detail="This bot has no Nostr secret key (nsec) to post with.")
    try:
        summary = await post_stats(body.nsec.strip())
        return {"ok": True, "summary": summary, "message": "Posted to Nostr"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid nsec: {e}")
    except Exception as e:
        logger.error(f"Stats run failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/proxy-test")
async def test_proxy_chain(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Test each step of the proxy chain and report exactly where it fails."""
    import asyncio
    import socket
    import httpx
    from app.services import settings_store

    def get_setting(key, default=""):
        return settings_store.get(key, None) or default

    results = {}

    # Step 1: Read configured values
    bt_proxy_host = get_setting("bt_proxy_host")
    bt_proxy_port = get_setting("bt_proxy_port", "8118")
    proxy_socks_host = get_setting("proxy_socks_host")
    proxy_socks_port = get_setting("proxy_socks_port", "9052")
    torrent_site_url = get_setting("torrent_site_url", "https://torrentgalaxy.one")

    results["config"] = {
        "bt_proxy_host": bt_proxy_host or "(not set)",
        "bt_proxy_port": bt_proxy_port,
        "proxy_socks_host": proxy_socks_host or "(not set)",
        "proxy_socks_port": proxy_socks_port,
        "torrent_site_url": torrent_site_url,
    }

    # Step 2: TCP connection to HTTP proxy
    if bt_proxy_host:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: socket.create_connection((bt_proxy_host, int(bt_proxy_port)), timeout=5)
            )
            results["http_proxy_tcp"] = {"ok": True, "msg": f"TCP connect to {bt_proxy_host}:{bt_proxy_port} succeeded"}
        except Exception as e:
            results["http_proxy_tcp"] = {"ok": False, "msg": f"TCP connect to {bt_proxy_host}:{bt_proxy_port} FAILED: {e}"}
    else:
        results["http_proxy_tcp"] = {"ok": False, "msg": "bt_proxy_host not configured"}

    # Step 3: TCP connection to SOCKS5 (Tor)
    if proxy_socks_host:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: socket.create_connection((proxy_socks_host, int(proxy_socks_port)), timeout=5)
            )
            results["socks5_tcp"] = {"ok": True, "msg": f"TCP connect to SOCKS5 {proxy_socks_host}:{proxy_socks_port} succeeded"}
        except Exception as e:
            results["socks5_tcp"] = {"ok": False, "msg": f"TCP connect to SOCKS5 {proxy_socks_host}:{proxy_socks_port} FAILED: {e}"}
    else:
        results["socks5_tcp"] = {"ok": False, "msg": "proxy_socks_host not configured — HTTP proxy has no SOCKS5 target"}

    # Step 4: HTTP request through full proxy chain
    if bt_proxy_host:
        proxy_url = f"http://{bt_proxy_host}:{bt_proxy_port}"
        try:
            async with httpx.AsyncClient(proxy=proxy_url, timeout=15) as client:
                resp = await client.get("https://check.torproject.org/api/ip")
                if resp.status_code == 200:
                    data = resp.json()
                    results["proxy_chain"] = {
                        "ok": True,
                        "msg": f"Request succeeded via proxy",
                        "tor": data.get("IsTor", False),
                        "ip": data.get("IP", "unknown"),
                    }
                else:
                    results["proxy_chain"] = {"ok": False, "msg": f"HTTP {resp.status_code} from test URL"}
        except Exception as e:
            results["proxy_chain"] = {"ok": False, "msg": f"Request through proxy FAILED: {e}"}
    else:
        results["proxy_chain"] = {"ok": False, "msg": "bt_proxy_host not configured"}

    return results


# Datastore migration endpoints (/nostr-migrate, /nostr-purge) removed — the relay IS the
# datastore now (no SQLite app.db to migrate from); the one-time seed is done. The read helper
# nostr_migrate.settings_all() lives on (used by settings_store hydrate).


# WebDAV sync config endpoints removed

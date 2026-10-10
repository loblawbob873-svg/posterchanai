"""SQL row readers for the wave-1 tables (#161): each turns a node's OLD Postgres rows into EXACTLY the row dicts
the relay-only app stores (the same keys, the same shapes), for scripts/migrate_tables_to_nostr.py to copy once.

    SOURCES = {doc_table_name: fn(db) -> {key: row}}
    DERIVED = {doc_table_name: fn(sources_rows: {doc_table_name: {key: row}}) -> {key: row}}

Read-only: every function only SELECTs. Not copied, on purpose (transient):
  * proxy_image_cache -- an in-process TTL dict now;
  * push_sent_wraps   -- a 10-minute "already pushed" ledger; an empty one costs at most a duplicate push.
"""
from __future__ import annotations

from app.services.table_migrate import iso


# ------------------------------------------------------------------------------------ api_keys (+ api_key_index)
def _api_key(k) -> dict:
    return {"id": int(k.id), "user_id": int(k.user_id), "key": k.key, "name": k.name,
            "created_at": iso(k.created_at), "last_used_at": iso(k.last_used_at),
            # NULL was NOT active (`is_active == True` never matched it): stays refused
            "is_active": k.is_active is True}


def api_keys(db) -> dict:
    """key = str(id)"""
    from app.models import APIKey
    return {str(k.id): _api_key(k) for k in db.query(APIKey).all()}


def api_key_index(src: dict) -> dict:
    """key = sha256(api key) -> {"id"} (d-tags are plaintext: never the key itself)"""
    from app.services.api_key_store import key_hash
    return {key_hash(r["key"]): {"id": int(r["id"])} for r in src["api_keys"].values() if r.get("key")}


# ------------------------------------------------------------------------------------ blossom_blobs (+ owners)
def blossom_blobs(db) -> dict:
    """key = sha256; the blob row with its owners embedded (blossom_blob_owners merged in)."""
    from app.models import BlossomBlob, BlossomBlobOwner
    from app.services import blob_index
    out: dict = {}
    for b in db.query(BlossomBlob).order_by(BlossomBlob.sha256).yield_per(5000):
        out[b.sha256] = blob_index.new_row(
            pubkey=b.pubkey, size=b.size, mime=b.mime, created_at=b.created_at, expires_at=b.expires_at,
            storage=b.storage, path=b.path, private=bool(b.private), keep=bool(b.keep))
    for o in db.query(BlossomBlobOwner).order_by(BlossomBlobOwner.sha256).yield_per(5000):
        row = out.get(o.sha256)
        if row is not None:         # an owner of a blob with no row (the FK cascade makes this 0) is dropped
            row["owners"][o.pubkey] = [int(o.created_at or 0), o.name or None]
    return out


# ------------------------------------------------------------------------------------ external_storage (+ users)
def external_storage(db) -> dict:
    """key = str(id); the junction's user ids as `allowed_user_ids`."""
    from app.models import ExternalStorage, external_storage_users as J
    allowed: dict = {}
    for sid, uid in db.execute(J.select().with_only_columns(J.c.external_storage_id, J.c.user_id)).fetchall():
        allowed.setdefault(int(sid), set()).add(int(uid))
    out = {}
    for m in db.query(ExternalStorage).all():
        out[str(m.id)] = {"id": int(m.id), "name": m.name, "mount_path": m.mount_path,
                          "mount_point": m.mount_point, "description": m.description,
                          "is_active": m.is_active is True, "created_at": iso(m.created_at),
                          "updated_at": iso(m.updated_at), "allowed_user_ids": sorted(allowed.get(int(m.id), set()))}
    return out


# ------------------------------------------------------------------------------------ fediverse bridge
def fedi_puppets(db) -> dict:
    """key = actor_uri (the SQL primary key)"""
    from app.models import FediPuppet
    return {p.actor_uri: {"actor_uri": p.actor_uri, "acct": p.acct, "instance_host": p.instance_host,
                          "pubkey_hex": p.pubkey_hex, "nip05_name": p.nip05_name,
                          "display_name": p.display_name, "avatar_url": p.avatar_url,
                          "profile_sig": p.profile_sig, "last_seen": iso(p.last_seen),
                          "created_at": iso(p.created_at)}
            for p in db.query(FediPuppet).all()}


def _ledger_keep_days() -> int:
    from app.services import settings_store
    try:
        return int(settings_store.get("nostr_relay_retention_days", "30") or "30")
    except ValueError:
        return 30


def fedi_bridge_delivered(db) -> dict:
    """key = fedi_tables.ledger_key(platform, instance_url, note_id). Only the rows inside the relay's retention
    window (older ones are about events the relay no longer holds; the daily prune would drop them), and rows
    sharing a (platform, instance, note) collapse into the FIRST one recorded -- the one dedup ever consulted."""
    from datetime import datetime, timedelta
    from app.models import FediBridgeDelivered as F
    from app.services.fedi_tables import ledger_key
    keep_days = _ledger_keep_days()
    q = db.query(F)
    if keep_days > 0:
        q = q.filter(F.created_at >= datetime.utcnow() - timedelta(days=keep_days))
    out = {}
    for r in q.order_by(F.id.asc()).all():
        k = ledger_key(r.platform, r.instance_url, r.note_id)
        if k not in out:
            out[k] = {"platform": r.platform, "instance_url": r.instance_url or "",
                      "note_id": (r.note_id or "")[:255], "note_uri": r.note_uri, "author_acct": r.author_acct,
                      "nostr_event_id": r.nostr_event_id or "", "nostr_pubkey": r.nostr_pubkey,
                      "created_at": iso(r.created_at), "deleted_at": iso(r.deleted_at)}
    return out


# ------------------------------------------------------------------------------------ files, verification, VODs
def shared_files(db) -> dict:
    """key = str(id)"""
    from app.models import SharedFile
    return {str(s.id): {"id": int(s.id), "user_id": int(s.user_id), "token": s.token, "file_path": s.file_path,
                        "filename": s.filename, "created_at": iso(s.created_at),
                        "expires_at": iso(s.expires_at), "access_count": int(s.access_count or 0),
                        "max_accesses": s.max_accesses,
                        # NULL never matched `is_active == True`: it stays inactive
                        "is_active": s.is_active is True}
            for s in db.query(SharedFile).all()}


def verification_tokens(db) -> dict:
    """key = str(id)"""
    from app.models import VerificationToken
    return {str(v.id): {"id": int(v.id), "user_id": int(v.user_id), "token": v.token,
                        "created_at": iso(v.created_at), "expires_at": iso(v.expires_at)}
            for v in db.query(VerificationToken).all()}


def stream_vods(db) -> dict:
    """key = "<token>:<started_at>" (the old unique index)"""
    from app.models import StreamVOD
    from app.services.stream_vod_store import vod_key
    return {vod_key(v.token, v.started_at): {
                "id": v.id, "user_id": v.user_id, "pubkey": v.pubkey, "token": v.token, "sha256": v.sha256,
                "mime": v.mime, "size": int(v.size), "duration_s": v.duration_s, "title": v.title,
                "started_at": int(v.started_at), "created_at": int(v.created_at)}
            for v in db.query(StreamVOD).order_by(StreamVOD.id).yield_per(2000)}


# ------------------------------------------------------------------------------------ push
def _ts(value):
    from app.services.push_store import _ts as ts
    return ts(value)


def push_subs(db) -> dict:
    """key = str(id) (the id a live Direct socket and the delivery queue are keyed on)"""
    from app.models import PushSubscription
    return {str(r.id): {"pubkey": r.pubkey, "endpoint": r.endpoint, "transport": r.transport or "webpush",
                        "device_id": r.device_id, "token_hash": r.token_hash, "last_seen": _ts(r.last_seen),
                        "p256dh": r.p256dh, "auth": r.auth,
                        "prefs": r.prefs,      # NULL stays None: "never configured" means send everything
                        "created_at": _ts(r.created_at)}
            for r in db.query(PushSubscription).all()}


def direct_push_msgs(db) -> dict:
    """key = str(id) (the id the phone ACKs). Already-expired queue rows are not copied: every reader deletes
    them on sight anyway."""
    import time
    from app.models import DirectPushMessage
    now = int(time.time())
    out = {}
    for r in db.query(DirectPushMessage).all():
        exp = _ts(r.expires_at)
        if exp is None or exp <= now:
            continue
        out[str(r.id)] = {"sub": int(r.subscription_id), "payload": r.payload,
                          "at": _ts(r.created_at) or now, "exp": exp}
    return out


def push_follow_seen(db) -> dict:
    """key = "<recipient>" -> {"seeded": true, "at": 0} (the old follower="" row), or "<recipient>:<xx>" ->
    {"r", "f": [follower ...]} sharded on the follower's first two hex digits."""
    from app.models import PushFollowSeen
    from app.services.push_store import _shard
    shards: dict = {}
    seeded: set = set()
    for r in db.query(PushFollowSeen).all():
        if not r.recipient:
            continue
        if not r.follower:
            seeded.add(r.recipient)
            continue
        shards.setdefault("%s:%s" % (r.recipient, _shard(r.follower)), set()).add(r.follower)
    out = {k: {"r": k.rsplit(":", 1)[0], "f": sorted(v)} for k, v in shards.items()}
    for rcp in seeded:
        out[rcp] = {"seeded": True, "at": 0}
    return out


# ------------------------------------------------------------------------------------ the small app tables
def reminders(db) -> dict:
    """key = str(id)"""
    from app.models import Reminder
    from app.services.reminder_service import reminder_row
    return {str(r.id): reminder_row(r.user_id, r.text, r.due_at, r.status, r.created_at, r.delivered_at)
            for r in db.query(Reminder).all()}


def scheduled_posts(db) -> dict:
    """key = str(id)"""
    from app.models import ScheduledPost
    from app.services.scheduled_posts_service import post_row
    return {str(r.id): post_row(r.user_id, r.event_id, r.event_json, r.scheduled_at, r.status, r.content_preview,
                                r.attempts, r.created_at, r.sent_at)
            for r in db.query(ScheduledPost).all()}


def saved_searches(db) -> dict:
    """key = str(id)"""
    from app.models import SavedSearch
    from app.services.saved_search_service import search_row
    return {str(r.id): search_row(r.user_id, r.query, r.created_at) for r in db.query(SavedSearch).all()}


def social_reply_map(db) -> dict:
    """key = "<telegram chat>:<telegram message>"; two SQL rows for one message collapse to the NEWEST (highest
    id) -- the one the old `ORDER BY id DESC` lookup answered with."""
    from app.models import SocialReplyMap
    from app.services.app_tables import to_iso
    from app.services.social_notifications_service import reply_key
    out = {}
    for r in db.query(SocialReplyMap).order_by(SocialReplyMap.id.asc()).all():
        out[reply_key(r.telegram_chat_id, r.telegram_message_id)] = {
            "user_id": r.user_id, "telegram_chat_id": str(r.telegram_chat_id),
            "telegram_message_id": int(r.telegram_message_id), "platform": r.platform,
            "target_id": r.target_id, "visibility": r.visibility, "created_at": to_iso(r.created_at)}
    return out


# ------------------------------------------------------------------------------------ the registry
SOURCES = {
    "api_keys": api_keys,
    "blossom_blobs": blossom_blobs,
    "external_storage": external_storage,
    "fedi_puppets": fedi_puppets,
    "fedi_bridge_delivered": fedi_bridge_delivered,
    "shared_files": shared_files,
    "verification_tokens": verification_tokens,
    "stream_vods": stream_vods,
    "push_subs": push_subs,
    "direct_push_msgs": direct_push_msgs,
    "push_follow_seen": push_follow_seen,
    # push_sent_wraps: NOT copied (a transient 10-minute dedup ledger)
    "reminders": reminders,
    "scheduled_posts": scheduled_posts,
    "saved_searches": saved_searches,
    "social_reply_map": social_reply_map,
}

DERIVED = {
    "api_key_index": api_key_index,     # needs SOURCES["api_keys"]
}

# old SQL table names that are not their DocTable's name (the script accepts either)
ALIASES = {"push_subscriptions": "push_subs", "direct_push_messages": "direct_push_msgs",
           "blossom_blob_owners": "blossom_blobs", "external_storage_users": "external_storage"}

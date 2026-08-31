from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, ForeignKey, Index, Table, BigInteger
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    notification_email = Column(String(255), nullable=True)
    avatar = Column(String(255), nullable=True)  # Path to avatar image
    theme = Column(String(40), default="cyberpunk")  # client UI theme slug (mirrored to Nostr per-user); matches the pre-login site default

    # News sources for the on-demand `news` command (the scheduled daily-digest feature was removed).
    news_sources = Column(Text, default="")  # Custom news sources, one per line: url|name


    # Storage quota (in bytes, 0 = unlimited)
    storage_quota = Column(Integer, default=0)  # 0 means unlimited

    # Per-user feature access (admins are always allowed). Managed in Admin → Users.
    # Default True = unchanged behavior for existing users; admin unchecks to restrict.
    can_image = Column(Boolean, default=True)    # image generation (geni)
    can_music = Column(Boolean, default=True)    # music generation (musicgeni)
    can_video = Column(Boolean, default=True)    # video generation (videogeni)
    can_torrent = Column(Boolean, default=True)  # torrent search / download
    # Blossom upload privilege. Default False (opt-in): granting it lets this user's linked
    # Nostr key (nostr_npub) upload blobs to the built-in Blossom server. See blossom_service.
    can_blossom = Column(Boolean, default=False)
    # Going live is gated like Blossom and AI: it consumes real upstream bandwidth and puts this
    # instance's name on whatever is broadcast, so it's opt-in per account rather than open to
    # anyone who signs up. Watching stays open to everyone.
    can_stream = Column(Boolean, default=False)
    # AI access privilege (opt-in). With the Nostr client as the face of the app, anyone can sign
    # up with a Nostr key, but the AI features stay gated until an admin approves (see can_ai flow).
    can_ai = Column(Boolean, default=False)
    # An admin TOOK AI/Blossom away, as opposed to never having granted it. Only the automatic grants
    # need to tell those apart: a fediverse sign-in hands out AI+Blossom (holding an account on an
    # instance we trust IS the identity check can_ai stands in for), and without this marker that
    # grant would undo every revocation the next time the user signed in — so the one lever for
    # dealing with abuse would quietly last only until their next login. Cleared when an admin grants
    # the capability back, so a revoke is never permanent by accident.
    access_revoked = Column(Boolean, default=False)

    # Telegram integration settings
    telegram_enabled = Column(Boolean, default=False)
    telegram_chat_id = Column(String(50), nullable=True)  # Uniqueness enforced by partial index (non-NULL only)
    telegram_notifications = Column(Text, default="")  # Comma-separated: "news,downloads,mentions"
    telegram_key = Column(String(64), nullable=True, index=True)  # One-time link key generated from User Settings
    telegram_key_expires_at = Column(DateTime, nullable=True)  # Expiry for the pending link key


    # Pleroma integration settings
    pleroma_enabled = Column(Boolean, default=False)
    pleroma_instance_url = Column(String(500), nullable=True)
    pleroma_access_token = Column(String(500), nullable=True)
    # Which account that token belongs to (`user@host`), recorded so "sign in with Pleroma" can find
    # an EXISTING linked user instead of minting a second identity for the same person. Backfilled
    # from the instance on first login for accounts linked before this column existed.
    pleroma_acct = Column(String(255), nullable=True, index=True)

    # Nostr integration settings. Identity is a secret key (nsec/hex); posts publish to the
    # user's relays; media uploads to an external Blossom/NIP-96 host (not an "instance").
    nostr_enabled = Column(Boolean, default=False)
    nostr_nsec = Column(String(200), nullable=True)            # secret key (nsec1… or hex)
    nostr_npub = Column(String(100), nullable=True, index=True)  # derived pubkey; indexed — Blossom
    #                                              auth (is_pubkey_allowed) looks users up by npub per upload/delete
    nostr_relays = Column(Text, nullable=True)                 # comma/newline list; blank = defaults
    nostr_media_service = Column(String(20), nullable=True)    # "blossom" | "nip96"
    nostr_media_endpoint = Column(String(500), nullable=True)  # blank = service default
    # Google sign-in (social_login.py). `google_sub` is Google's stable per-account id and is what a
    # login matches on — never the email, which is re-assignable and would let a recycled address take
    # over an identity. The email is kept for display only. Set either by signing in with Google (the
    # node mints the key) or by linking Google to a key you already have, from User Settings.
    google_sub = Column(String(64), nullable=True, index=True)
    google_email = Column(String(320), nullable=True)
    # Opt-in: save this user's ended live streams to their Blossom drive (stream_vod_service).
    # Mirrored to Nostr via users_store.CONFIG_FIELDS; gated by the global stream_record_enabled.
    stream_record = Column(Boolean, default=False)


    # Social notification relay → Telegram (master per-user toggle + per-platform cursors)
    social_notif_enabled = Column(Boolean, default=False)
    pleroma_notif_since = Column(Text, nullable=True)   # last-seen Pleroma notification id
    nostr_notif_since = Column(Text, nullable=True)     # last-seen Nostr event created_at (unix)

    # Nostr ↔ Fediverse bridge: per-user opt-in for the PERSONAL plane (your fedi DMs arrive as
    # NIP-17 Nostr DMs, your fedi notifications as the matching Nostr events). The public global-
    # timeline mirror is server-wide (admin setting) and independent of this. Needs a linked Pleroma
    # account + a linked Nostr identity. Cursors are kept separate from the Telegram relay's.
    fedi_bridge_enabled = Column(Boolean, default=False)
    fedi_bridge_dm_since = Column(Text, nullable=True)      # last-seen fedi direct-conversation id
    fedi_bridge_notif_since = Column(Text, nullable=True)   # last-seen fedi notification id
    # Cross-post: when on, this user's top-level Nostr notes are federated to their linked Pleroma
    # account as new public posts (replies/likes/reposts already federate via the write-back path).
    fedi_crosspost_enabled = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), default="New Chat")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at, Message.id")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)  # 'user' or 'assistant'
    content = Column(Text, nullable=False)
    image_path = Column(String(500), nullable=True)  # Path to saved image file
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


# NOTE: the `Setting` model / `settings` table is GONE. Global settings now live in the Nostr relay
# datastore as operator-signed pcai:setting: events (read via app.services.settings_store). See
# docs/NOSTR_DATASTORE.md.


class ProxyImageCache(Base):
    """Short-lived cache for image proxy thumb IDs (shared across workers)."""
    __tablename__ = "proxy_image_cache"

    id = Column(String(32), primary_key=True)
    url = Column(Text, nullable=False)
    expires_at = Column(Integer, nullable=False)  # Unix timestamp when this entry expires


class UserSetting(Base):
    """Per-user settings (calendar configs, etc.)"""
    __tablename__ = "user_settings"
    __table_args__ = (Index('ix_user_settings_user_key', 'user_id', 'key'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(Text)


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key = Column(String(100), unique=True, nullable=False, index=True)  # "sk-" + 64 hex = 67 chars
    name = Column(String(100), default="Default")
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)


class SharedFile(Base):
    """Public sharing for files via token-based URLs."""
    __tablename__ = "shared_files"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    file_path = Column(String(1000), nullable=False)  # Relative to user root
    filename = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)  # None = never expires
    access_count = Column(Integer, default=0)
    max_accesses = Column(Integer, nullable=True)  # None = unlimited
    is_active = Column(Boolean, default=True)

    user = relationship("User", backref="shared_files")


class VerificationToken(Base):
    __tablename__ = "verification_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", backref="verification_tokens")


# Junction table for ExternalStorage and User many-to-many relationship
external_storage_users = Table(
    'external_storage_users',
    Base.metadata,
    Column('external_storage_id', Integer, ForeignKey('external_storage.id', ondelete='CASCADE'), primary_key=True),
    Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Index('ix_external_storage_users', 'external_storage_id', 'user_id')
)


class ExternalStorage(Base):
    """External storage mounts for File Manager"""
    __tablename__ = "external_storage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)  # Display name (e.g., "Video Storage")
    mount_path = Column(String(500), nullable=False)  # Actual path (e.g., "/raid/video")
    mount_point = Column(String(255), nullable=False)  # Virtual path in file manager (e.g., "video")
    description = Column(Text, nullable=True)  # Optional description
    is_active = Column(Boolean, default=True)  # Enable/disable mount
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Many-to-many relationship with User (users allowed to access this storage)
    allowed_users = relationship("User", secondary=external_storage_users, backref="external_storage_mounts")


class SocialReplyMap(Base):
    """Maps a notification forwarded to Telegram → where a reply should be posted.

    When the social-notification poller DMs a notification to a user's Telegram chat, it
    records the resulting Telegram message id here. If the user replies to that message in
    Telegram, we look it up to post the reply back to the right platform/target."""
    __tablename__ = "social_reply_map"
    __table_args__ = (Index('ix_social_reply_chat_msg', 'telegram_chat_id', 'telegram_message_id'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    telegram_chat_id = Column(String(50), nullable=False)
    telegram_message_id = Column(Integer, nullable=False)
    platform = Column(String(20), nullable=False)   # "pleroma"
    target_id = Column(String(255), nullable=True)   # note/status id to reply to
    visibility = Column(String(20), nullable=True)   # inherit parent visibility on reply
    created_at = Column(DateTime, default=datetime.utcnow)




class BlossomBlob(Base):
    """A blob stored by the built-in Blossom server (BUD-01/02). Content-addressed:
    the row key is the blob's sha256, so the same bytes uploaded by several users are
    stored ONCE (the row's `pubkey` is the first/owning uploader). `storage` selects the
    backend — `local` keeps bytes on this node under `path` (a file in the blob dir),
    `proxy` keeps them on the configured storage server (`path` is the relative path the
    storage node returned). `expires_at` is the per-blob TTL (0/NULL = never); the cleanup
    sweep deletes rows + bytes past it."""
    __tablename__ = "blossom_blobs"
    __table_args__ = (
        Index('ix_blossom_pubkey', 'pubkey'),
        Index('ix_blossom_expires', 'expires_at'),
    )

    sha256 = Column(String(64), primary_key=True)
    pubkey = Column(String(64), nullable=False)        # owning uploader (hex x-only)
    size = Column(BigInteger, nullable=False)   # bytes — BigInteger: a >2.1 GB blob overflowed INT4
    mime = Column(String(120), nullable=True)
    created_at = Column(Integer, nullable=False)        # unix seconds
    expires_at = Column(Integer, nullable=True)         # unix seconds; 0/NULL = never
    storage = Column(String(10), nullable=False, default="local")  # "local" | "proxy"
    path = Column(String(512), nullable=False)          # local file path or proxy rel-path
    # PRIVATE blob: an AI-chat artifact, stored as ciphertext under the owner's derived storage key
    # and decrypted on demand by /client/file. These must NEVER appear in the public BUD-02 listing:
    # that listing published the sha256 of every one of them, and the sha256 is the only thing
    # /client/file requires to hand back the DECRYPTED bytes — so an unauthenticated listing was a
    # full read of every user's AI-chat files. Public uploads (normal Blossom media) stay listable.
    private = Column(Boolean, nullable=False, default=False, server_default="false")
    # KEEP: exempt from the age-based cleanup sweep, forever. Ordinary blobs are swept once they are
    # older than `blossom_blob_ttl_days` — which is fine for chat media (the message still renders the
    # loss as a broken image) but is silent, unrecoverable data loss for the client-side ENCRYPTED
    # drive: Notes attachments, Music tracks and the files-index blob are ciphertext whose only copy
    # is here, and whose owner has no way to notice until they open a note and the picture is gone.
    # The flag is set by the uploader (`X-Keep`), so the client that knows a blob is drive content
    # decides — the server can't tell, the bytes are opaque. Never cleared once set (see save_blob).
    keep = Column(Boolean, nullable=False, default=False, server_default="false")


class BlossomBlobOwner(Base):
    """Who references a blob. Blossom is content-addressed and dedups, so `blossom_blobs` holds ONE
    row per sha256 owned by whoever uploaded it FIRST — which silently broke two things for everyone
    after them: their upload created no row, so the file never appeared in their own BUD-02 listing,
    and if the first uploader deleted it, the bytes went out from under everybody else.

    This is the many-to-many that `blossom_blobs.pubkey` pretended to be. The blob row (and its bytes)
    survive until the LAST owner releases. `blossom_blobs.pubkey` stays as the original uploader for
    back-compat and attribution.

    A SQL table rather than a relay doc, deliberately: this is a hot-path index over an existing SQL
    table (every list and every delete joins it), not new feature state."""
    __tablename__ = "blossom_blob_owners"
    __table_args__ = (
        Index('ix_blobowner_pubkey', 'pubkey'),
    )

    sha256 = Column(String(64), ForeignKey("blossom_blobs.sha256", ondelete="CASCADE"), primary_key=True)
    pubkey = Column(String(64), primary_key=True)       # hex x-only pubkey of a referencing user
    created_at = Column(Integer, nullable=False)        # unix seconds — when THIS user added it
    # Original filename as uploaded (best-effort, from the X-Filename header). It lives HERE and not
    # on the blob because dedup means one set of bytes can be two different files to two people —
    # and because the blob's own identity is its hash, never a name. Only used for presentation:
    # the BUD-02 listing and the download filename.
    name = Column(String(255), nullable=True)


class StreamVOD(Base):
    """A finished live stream saved to the streamer's Blossom drive (see stream_vod_service).

    The stream is recorded (by MediaMTX) to a size-capped tmpfs, then on confirmed end
    concatenated to one fmp4 and streamed into Blossom — attributed to the streamer's own
    `pubkey` (their User.nostr_npub, hex). This row is the app-side index the web UI reads to
    list a user's past streams and play them back at the Blossom URL (blossom_public_url/<sha256>).
    The bytes live in Blossom (dedup/retention there); deleting this row just hides the VOD."""
    __tablename__ = "stream_vods"
    __table_args__ = (
        Index('ix_streamvod_user', 'user_id'),
        Index('ix_streamvod_token', 'token'),
        # One VOD per (stream session) — makes finalize idempotent across end-event retries.
        Index('ix_streamvod_session', 'token', 'started_at', unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    pubkey = Column(String(64), nullable=False)         # streamer's hex pubkey (owns the Blossom blob)
    token = Column(String(64), nullable=False)          # the stream publish token (MediaMTX path / 30311 d-tag)
    sha256 = Column(String(64), nullable=False)         # Blossom blob hash → playback URL
    mime = Column(String(60), nullable=False, default="video/mp4")
    size = Column(BigInteger, nullable=False)           # bytes
    duration_s = Column(Integer, nullable=True)         # length in seconds (best-effort from ffprobe)
    title = Column(String(300), nullable=True)          # stream title at go-live, if known
    started_at = Column(Integer, nullable=False)        # unix seconds — recording start
    created_at = Column(Integer, nullable=False)        # unix seconds — finalize/upload time



class FediPuppet(Base):
    """A fediverse account mirrored into the Nostr side by the Nostr↔Fediverse bridge.

    Each fedi author the bridge encounters (in the global timeline mirror, a DM, or a notification)
    gets a deterministic "puppet" Nostr keypair — `pubkey_hex = BIP340(HMAC(operator_bridge_secret,
    canonical_actor_uri))` — so the same fedi user always maps to the same npub, no secret is stored
    (the app re-derives it on demand; only the pubkey is recorded), and it survives a DB loss. The
    relay subprocess loads `pubkey_hex` (publish allowlist) + `nip05_name`→`pubkey_hex` (NIP-05
    resolution) from this table on start (thread._collect_bridge_pubkeys) and incrementally via the
    `bridge-add` control command. `profile_sig` is a hash of the last-published kind-0 fields so the
    bridge only re-publishes the profile when the display name/avatar actually change. `last_seen`
    bounds the registry: puppets unseen past retention (and with no surviving notes) are GC'd."""
    __tablename__ = "fedi_puppets"
    __table_args__ = (
        Index('ix_fedi_puppet_pubkey', 'pubkey_hex'),
        Index('ix_fedi_puppet_nip05', 'nip05_name'),
    )

    actor_uri = Column(String(512), primary_key=True)   # canonical AP actor URI (the derivation input)
    acct = Column(String(255), nullable=False)          # fedi handle, e.g. alice@mastodon.social
    instance_host = Column(String(255), nullable=True)  # host part (for the domain blocklist)
    pubkey_hex = Column(String(64), nullable=False)     # derived x-only pubkey
    nip05_name = Column(String(255), nullable=False)    # local-part served at <name>@<this instance>
    display_name = Column(String(255), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    profile_sig = Column(String(64), nullable=True)     # hash of last-published kind-0 (change detect)
    last_seen = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class FediBridgeDelivered(Base):
    """Maps a mirrored fediverse post ↔ the Nostr note the bridge published for it.

    The public-plane analogue of TimelinePost: drives dedup (skip a note already mirrored, keyed on
    note_uri cross-instance / note_id same-instance) and resolution (a local NIP-05 user's Nostr
    reply/like/repost on `nostr_event_id` routes back to the fedi `note_id` on `instance_url`; a
    reply note finds its parent's `nostr_event_id` for the e/p tags). A write-back action records a
    synthetic row so the global-timeline poller won't re-mirror the user's own post once it federates
    back. Prunable: rows whose puppet/note aged out of the relay can be GC'd alongside the notes."""
    __tablename__ = "fedi_bridge_delivered"
    __table_args__ = (
        Index('ix_fedi_deliv_uri', 'note_uri'),
        Index('ix_fedi_deliv_note', 'instance_url', 'note_id'),
        Index('ix_fedi_deliv_event', 'nostr_event_id'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)        # "pleroma"
    instance_url = Column(String(255), nullable=False)   # instance the note was read from
    note_id = Column(String(255), nullable=False)        # status/note id on instance_url
    note_uri = Column(String(512), nullable=True)        # canonical AP URI (cross-instance key)
    author_acct = Column(String(255), nullable=True)
    nostr_event_id = Column(String(64), nullable=False)  # the kind-1 we published (puppet-signed)
    nostr_pubkey = Column(String(64), nullable=True)     # the puppet that authored it
    created_at = Column(DateTime, default=datetime.utcnow)
    # Set once the fediverse status has actually been deleted. The row must SURVIVE the delete (it is
    # what stops the mirror re-importing the post as a puppet note), so without this marker the
    # reconnect replay of the kind-5 finds a live note_id and deletes the same status on every restart.
    deleted_at = Column(DateTime, nullable=True)


class FediBridgeSkipped(Base):
    """Why a fediverse post was NOT mirrored. The counterpart to FediBridgeDelivered.

    Until this existed a skip was indistinguishable from "never saw it": every early return in
    _process/_deliver dropped the post with no row, no log and no counter, so a coverage gap could
    only be found by hand-diffing the instance against the relay (which is how 5 of one account's
    40 recent posts turned out to be missing — all of them hashtag-heavy). Recording the REASON is
    what makes the next gap announce itself instead of needing a forensic session.

    Not a dedup key — _seen/_delivered_by_uri still own that. Purely diagnostic, and prunable."""
    __tablename__ = "fedi_bridge_skipped"
    __table_args__ = (
        Index('ix_fedi_skip_uri', 'note_uri'),
        Index('ix_fedi_skip_reason', 'reason', 'created_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=True)
    instance_url = Column(String(255), nullable=True)
    note_id = Column(String(255), nullable=True)
    note_uri = Column(String(512), nullable=True)
    author_acct = Column(String(255), nullable=True)
    reason = Column(String(40), nullable=False)          # short code, e.g. "blocked", "oversized"
    detail = Column(String(500), nullable=True)          # free text (relay message, exception, …)
    created_at = Column(DateTime, default=datetime.utcnow)


class FediReconcileState(Base):
    """Per-author cursor for the reconciliation pass.

    The drain reads TIMELINES, which are a filtered, ephemeral view: anything the instance keeps out
    of one is skipped by the forward-only cursor and lost permanently. Reconciliation re-reads each
    author's OWN outbox (/api/v1/accounts/:id/statuses) and re-delivers whatever never landed, which
    is cause-agnostic — it repairs timeline omissions, transient publish failures and restart gaps
    alike without us having to diagnose each one first.

    One row per (instance_url, acct). `account_id` caches the instance's id for that acct so the
    hourly pass costs one request per author instead of two."""
    __tablename__ = "fedi_reconcile_state"
    __table_args__ = (
        Index('ix_fedi_recon_acct', 'instance_url', 'acct', unique=True),
        Index('ix_fedi_recon_checked', 'last_checked_at'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    instance_url = Column(String(255), nullable=False)
    acct = Column(String(255), nullable=False)
    account_id = Column(String(64), nullable=True)       # id on the instance (cached lookup)
    last_checked_at = Column(DateTime, nullable=True)    # drives least-recently-checked rotation
    last_repaired = Column(Integer, default=0)           # posts re-delivered on the last pass
    total_repaired = Column(Integer, default=0)
    last_error = Column(String(300), nullable=True)


class FediBridgeAction(Base):
    """A write-back INTERACTION (favourite / emoji reaction / reblog) performed on the fediverse for one
    Nostr event — so a later NIP-09 delete of that event can UNDO it.

    Needed because at un-react time the facts are already gone: FediBridgeDelivered only maps notes we
    POSTED, and the relay hard-deletes the kind-7/6 the moment the kind-5 lands (store._insert_one), so
    the deleted event's target and emoji can't be read back. `emoji` is stored in the exact form the
    instance accepted, since removing a reaction means replaying it to the same URL with DELETE.

    A row is ALSO the durable "we already did this" marker the write-back checks before acting, which is
    why an undone row is TOMBSTONED (undone_at) rather than deleted. Deleting it would let the reconnect
    replay of the still-live kind-7 re-perform a reaction the user had explicitly removed."""
    __tablename__ = "fedi_bridge_action"
    __table_args__ = (Index('ix_fedi_action_event', 'nostr_event_id'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    nostr_event_id = Column(String(64), nullable=False)  # the kind-7/6 we acted on
    nostr_pubkey = Column(String(64), nullable=False)    # its author (scopes the undo to its own actor)
    platform = Column(String(20), nullable=False)        # "pleroma"
    instance_url = Column(String(255), nullable=False)   # instance the action was performed on
    target_id = Column(String(255), nullable=False)      # status id acted upon
    action = Column(String(12), nullable=False)          # "favourite" | "react" | "reblog"
    emoji = Column(String(120), nullable=True)           # "react" only, as sent
    created_at = Column(DateTime, default=datetime.utcnow)
    undone_at = Column(DateTime, nullable=True)          # set when un-done; the row STAYS as the marker


class FediBridgeMap(Base):
    """Personal-plane reply routing: maps a Nostr event the bridge delivered to a user (a NIP-17 DM
    or a notification mirror) → the fediverse target to act on when the user replies on Nostr.

    Maps a delivered notification back to its fedi target. `kind` distinguishes a DM (reply stays visibility=direct
    in the same conversation) from a notification (reply to the referenced status)."""
    __tablename__ = "fedi_bridge_map"
    __table_args__ = (Index('ix_fedi_map_event', 'nostr_event_id'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    nostr_event_id = Column(String(64), nullable=False)  # the DM/notif event we delivered to the user
    kind = Column(String(10), nullable=False)            # "dm" | "notif"
    platform = Column(String(20), nullable=False)
    instance_url = Column(String(255), nullable=False)
    peer_pubkey = Column(String(64), nullable=True)      # the sender/actor puppet pubkey (DM convo key)
    target_id = Column(String(255), nullable=True)       # status id to reply to (notif) / latest in convo (dm)
    visibility = Column(String(20), nullable=True)       # preserve the parent's visibility
    created_at = Column(DateTime, default=datetime.utcnow)


class Bot(Base):
    """A managed bot from the merged ~/posterchan framework (now botframework/).

    Replaces the hand-edited bots_config.py: bot_manager_service reads these rows, builds
    per-bot env, and spawns botframework/main.py <modes> as a child process (one row → one
    long-running listener, or a scheduled image poster). Editable from Admin → Bots.

    Identity/filter fields are first-class columns (for listing + per-host filtering); the
    remaining per-bot fields (server, username, access_token, prompt, tts_voice,
    welcome_*/block_*/report_* etc.) live in `config` as a JSON object, mirroring the original
    bots_config dict shape so new bot types don't need schema changes."""
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True)              # should the manager keep it running / scheduled
    bot_type = Column(String(20), default="text")        # "text" (long-running) | "image" (scheduled)
    platform = Column(String(20), default="pleroma")     # "pleroma"
    host = Column(String(100), nullable=True)            # node hostname that runs it; empty = any node
    modes = Column(Text, default="")                     # comma-separated main.py flags, e.g. "--pleroma"
    config = Column(Text, default="{}")                  # JSON: all other per-bot fields (creds, prompt, feature opts)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Reminder(Base):
    """A user reminder (`remind` command). The text + due time are parsed from natural language
    by the LLM (`reminder_service.parse_reminder`); a background scheduler polls for due rows and
    delivers them — ALWAYS to the web UI (a dedicated "⏰ Reminders" conversation + a live push to
    any connected websocket), and ALSO to Telegram when the user has it configured."""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    text = Column(Text, nullable=False)                  # what to remind the user about
    due_at = Column(DateTime, nullable=False, index=True)  # UTC; when to fire
    status = Column(String(20), default="pending", index=True)  # pending | done | cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)       # set when fired

    user = relationship("User", backref="reminders")


class SavedSearch(Base):
    """A user's pinned/saved search (`pin` command). Clicking one re-runs `search <query>`. No time
    component (unlike reminders) — just a saved query the user can run or delete from a list."""
    __tablename__ = "saved_searches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    query = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="saved_searches")



class ScheduledPost(Base):
    """A Nostr note the user scheduled to publish later. The note is PRE-SIGNED client-side (the server
    never holds the user's key) with its intended future `created_at`; a background scheduler
    (`scheduled_posts_service`) polls for due rows and broadcasts the already-signed event to the relay,
    so it works for every login type (nip07 / Amber / local nsec). This row (in the app's Postgres) is
    the store of record — it persists across restarts; a row left 'sending' by a crash is recovered to
    'pending' on startup. `content_preview` lets the Drafts UI list schedules without the key."""
    __tablename__ = "scheduled_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(String(64), nullable=False, index=True)   # the pre-signed event's id (dedup/reference)
    event_json = Column(Text, nullable=False)                   # the full pre-signed Nostr event
    scheduled_at = Column(DateTime, nullable=False, index=True)  # UTC; when to publish
    # pending → sending (claimed by a scheduler tick) → sent | failed; or cancelled by the user.
    status = Column(String(20), default="pending", index=True)
    content_preview = Column(String(280), default="")          # first line of the note, for the list UI
    attempts = Column(Integer, default=0)                      # publish attempts so far (give up after _MAX_ATTEMPTS)
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)                  # when it reached a terminal state (sent/failed/cancelled) — the prune clock

    user = relationship("User", backref="scheduled_posts")


class PushSubscription(Base):
    """One notification device tied to a Nostr pubkey.

    Browsers use ordinary VAPID Web Push.  The native Android app uses PosterChan Direct: it holds a
    random bearer token and an authenticated WebSocket to this node.  Only the token's SHA-256 digest
    is stored here, so a database read cannot be turned into a device connection.
    """
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pubkey = Column(String(64), index=True, nullable=False)   # Nostr pubkey hex this device belongs to
    endpoint = Column(Text, unique=True, nullable=False)       # Web Push URL; internal direct:<pk>:<device>
    transport = Column(String(32), nullable=False, default="webpush", index=True)
    device_id = Column(String(128), nullable=True, index=True) # app-generated, non-secret stable device id
    token_hash = Column(String(64), nullable=True, unique=True, index=True)  # Direct bearer SHA-256
    last_seen = Column(DateTime, nullable=True)
    p256dh = Column(String(255), nullable=True)                # client public key (base64url) — Web Push only
    auth = Column(String(255), nullable=True)                  # auth secret (base64url) — Web Push only
    created_at = Column(DateTime, default=datetime.utcnow)


class DirectPushMessage(Base):
    """Short, acknowledged queue for PosterChan Direct notifications.

    A foreground Android service normally receives these immediately.  Keeping the small payload in
    SQL until the device ACKs it closes the reconnect/reboot race without storing notification bearer
    tokens or private message contents (NIP-17 notifications contain no decrypted body).
    """
    __tablename__ = "direct_push_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subscription_id = Column(Integer, ForeignKey("push_subscriptions.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)

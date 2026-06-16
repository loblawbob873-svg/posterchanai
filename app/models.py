from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, ForeignKey, Index, Table
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

    # Scheduled news settings
    news_schedule_enabled = Column(Boolean, default=False)
    news_schedule_time = Column(String(5), default="12:00")  # HH:MM format, default noon
    news_sources = Column(Text, default="")  # Custom news sources, one per line: url|name


    # Storage quota (in bytes, 0 = unlimited)
    storage_quota = Column(Integer, default=0)  # 0 means unlimited

    # Telegram integration settings
    telegram_enabled = Column(Boolean, default=False)
    telegram_chat_id = Column(String(50), nullable=True)  # Uniqueness enforced by partial index (non-NULL only)
    telegram_notifications = Column(Text, default="")  # Comma-separated: "news,downloads,mentions"
    telegram_key = Column(String(64), nullable=True, index=True)  # One-time link key generated from User Settings
    telegram_key_expires_at = Column(DateTime, nullable=True)  # Expiry for the pending link key

    # Misskey integration settings
    misskey_enabled = Column(Boolean, default=False)
    misskey_instance_url = Column(String(500), nullable=True)
    misskey_api_token = Column(String(500), nullable=True)

    # Pleroma integration settings
    pleroma_enabled = Column(Boolean, default=False)
    pleroma_instance_url = Column(String(500), nullable=True)
    pleroma_access_token = Column(String(500), nullable=True)

    # Matrix integration settings
    matrix_enabled = Column(Boolean, default=False)
    matrix_homeserver = Column(String(500), nullable=True)
    matrix_user_id = Column(String(500), nullable=True)
    matrix_access_token = Column(String(2000), nullable=True)

    # Matrix bot notification settings (posterchan bot DMs)
    matrix_dm_bot_user_id = Column(String(500), nullable=True)  # Bot to DM via, e.g. @posterchan:server

    # Finance (Budget Manager) integration — per-user API key for that user's finance account
    finance_api_key = Column(String(200), nullable=True)

    # Social notification relay → Telegram (master per-user toggle + per-platform cursors)
    social_notif_enabled = Column(Boolean, default=False)
    misskey_notif_since = Column(Text, nullable=True)   # last-seen Misskey notification id
    pleroma_notif_since = Column(Text, nullable=True)   # last-seen Pleroma notification id
    matrix_notif_since = Column(Text, nullable=True)    # Matrix /sync next_batch cursor

    # Fediverse notifications → Matrix DM (independent per-user toggle, separate from Telegram above)
    matrix_notif_enabled = Column(Boolean, default=False)

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


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text)


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
    key = Column(String(64), unique=True, nullable=False, index=True)
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
    platform = Column(String(20), nullable=False)   # "misskey" | "pleroma" | "matrix"
    target_id = Column(String(255), nullable=True)   # note/status id to reply to (null for matrix)
    room_id = Column(String(255), nullable=True)     # matrix room id
    event_id = Column(String(255), nullable=True)    # matrix event id being replied to
    visibility = Column(String(20), nullable=True)   # inherit parent visibility on reply
    created_at = Column(DateTime, default=datetime.utcnow)


class TimelinePost(Base):
    """Maps a Matrix event in the timeline bridge room ↔ a fediverse post.

    The fedi-timeline bridge (app/services/fedi_timeline_service.py) mirrors one Misskey/
    Pleroma timeline into a single Matrix room. Each posted note records a row here, which
    drives two things:
      - dedup — skip a note we've already posted (matched on note_uri, the cross-instance
        canonical AP URI, falling back to note_id for same-instance lookups);
      - action routing — a member's ❤/🔁/reply on a Matrix event resolves back to the
        underlying post via (room_id, event_id) → note_uri.
    A reply made from Element also gets a row (with the federated note's canonical URI) so the
    descendants poller won't re-post it once it federates back to the source instance."""
    __tablename__ = "timeline_posts"
    __table_args__ = (
        Index('ix_timeline_event', 'room_id', 'event_id'),
        Index('ix_timeline_note', 'room_id', 'note_id'),
        Index('ix_timeline_uri', 'room_id', 'note_uri'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(String(255), nullable=False)
    event_id = Column(String(255), nullable=False)
    thread_root_event_id = Column(String(255), nullable=True)  # null = this row is a thread root
    platform = Column(String(20), nullable=False)              # "misskey" | "pleroma"
    instance_url = Column(String(255), nullable=False)         # source instance the note was read from
    note_id = Column(String(255), nullable=False)              # note/status id on instance_url
    note_uri = Column(String(512), nullable=True)              # canonical AP URI (cross-instance key)
    author_acct = Column(String(255), nullable=True)
    body = Column(Text, nullable=True)                         # plain text we posted (for share→boost matching)
    created_at = Column(DateTime, default=datetime.utcnow)


class MatrixAvatarCache(Base):
    """Generic source-URL → Matrix mxc cache. Originally for author avatars (so the fedi-timeline
    bridge doesn't re-upload the same avatar on every post); also reused for post media and custom
    emoji, so identical media shared across boosts/quotes is uploaded to Synapse exactly once
    (saves the re-download/re-upload and avoids duplicate blobs filling the media store).
    `width`/`height` are the cached display dimensions for inline images (NULL for avatars/video)."""
    __tablename__ = "matrix_avatar_cache"

    author_avatar_url = Column(String(512), primary_key=True)
    mxc = Column(String(255), nullable=False)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)


class MatrixNotifyMap(Base):
    """Maps a notification DMed into Matrix (matrix_notifications_service) → the fedi post it
    concerns, so a user replying to that DM message can post the reply back to their account.

    The Matrix-DM analogue of SocialReplyMap (which does this for Telegram)."""
    __tablename__ = "matrix_notify_map"
    __table_args__ = (Index('ix_matrix_notify_event', 'room_id', 'event_id'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    room_id = Column(String(255), nullable=False)
    event_id = Column(String(255), nullable=False)   # the notification's Matrix event
    platform = Column(String(20), nullable=False)    # "misskey" | "pleroma"
    instance_url = Column(String(255), nullable=False)
    target_id = Column(String(255), nullable=False)  # note/status id to reply to
    visibility = Column(String(20), nullable=True)   # inherit the parent's visibility
    created_at = Column(DateTime, default=datetime.utcnow)


class Bot(Base):
    """A managed bot from the merged ~/posterchan framework (now botframework/).

    Replaces the hand-edited bots_config.py: bot_manager_service reads these rows, builds
    per-bot env, and spawns botframework/main.py <modes> as a child process (one row → one
    long-running listener, or a scheduled image poster). Editable from Admin → Bots.

    Identity/filter fields are first-class columns (for listing + per-host filtering); the
    remaining per-bot fields (server, username, access_token, prompt, nitter_feeds, tts_voice,
    welcome_*/block_*/report_* etc.) live in `config` as a JSON object, mirroring the original
    bots_config dict shape so new bot types don't need schema changes."""
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    enabled = Column(Boolean, default=True)              # should the manager keep it running / scheduled
    bot_type = Column(String(20), default="text")        # "text" (long-running) | "image" (scheduled)
    platform = Column(String(20), default="misskey")     # "misskey" | "pleroma" | "matrix"
    host = Column(String(100), nullable=True)            # node hostname that runs it; empty = any node
    modes = Column(Text, default="")                     # comma-separated main.py flags, e.g. "--pleroma,--matrix"
    config = Column(Text, default="{}")                  # JSON: all other per-bot fields (creds, prompt, feature opts)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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

    # Custom LLM service settings
    custom_ai_enabled = Column(Boolean, default=False)
    custom_ai_type = Column(String(50), nullable=True)  # "ollama" or "openai"
    custom_ai_url = Column(String(500), nullable=True)
    custom_ai_model = Column(String(200), nullable=True)
    custom_ai_api_key = Column(String(500), nullable=True)
    custom_llm_prompt = Column(Text, nullable=True)  # Custom instructions for the AI

    # Custom Image Generation service settings
    custom_image_enabled = Column(Boolean, default=False)
    custom_image_url = Column(String(500), nullable=True)

    # Scheduled news settings
    news_schedule_enabled = Column(Boolean, default=False)
    news_schedule_time = Column(String(5), default="12:00")  # HH:MM format, default noon
    news_sources = Column(Text, default="")  # Custom news sources, one per line: url|name


    # Storage quota (in bytes, 0 = unlimited)
    storage_quota = Column(Integer, default=0)  # 0 means unlimited

    # Telegram integration settings
    telegram_enabled = Column(Boolean, default=False)
    telegram_chat_id = Column(String(50), nullable=True)
    telegram_notifications = Column(Text, default="")  # Comma-separated: "news,downloads,mentions"

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

    user = relationship("User", backref="api_keys")


class VerificationToken(Base):
    __tablename__ = "verification_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    user = relationship("User", backref="verification_tokens")


# RAG (Retrieval-Augmented Generation) Models

class RAGCollection(Base):
    """A RAG collection for storing indexed documents/code"""
    __tablename__ = "rag_collections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    collection_type = Column(String(20), nullable=False)  # "folder", "git", "watcher"
    source_path = Column(String(500), nullable=True)  # Local path or git URL
    git_branch = Column(String(100), default="main")
    file_patterns = Column(Text, default="*.py,*.js,*.ts,*.tsx,*.jsx,*.html,*.css,*.json,*.yaml,*.yml,*.md,*.txt,*.go,*.rs,*.java,*.sh")  # Comma-separated globs
    last_indexed_at = Column(DateTime, nullable=True)
    document_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="rag_collections")
    documents = relationship("RAGDocument", back_populates="collection", cascade="all, delete-orphan")
    watchers = relationship("RAGWatcher", back_populates="collection", cascade="all, delete-orphan")


class RAGDocument(Base):
    """Tracks indexed documents for incremental updates"""
    __tablename__ = "rag_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(Integer, ForeignKey("rag_collections.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(64), nullable=False)  # SHA256 for change detection
    chunk_count = Column(Integer, default=0)
    indexed_at = Column(DateTime, default=datetime.utcnow)

    collection = relationship("RAGCollection", back_populates="documents")


class RAGWatcher(Base):
    """File watcher configurations for VS Code integration"""
    __tablename__ = "rag_watchers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    collection_id = Column(Integer, ForeignKey("rag_collections.id", ondelete="CASCADE"), nullable=False)
    watch_path = Column(String(500), nullable=False)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    last_event_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="rag_watchers")
    collection = relationship("RAGCollection", back_populates="watchers")


# Junction table for ExternalStorage and User many-to-many relationship
external_storage_users = Table(
    'external_storage_users',
    Base.metadata,
    Column('external_storage_id', Integer, ForeignKey('external_storage.id', ondelete='CASCADE'), primary_key=True),
    Column('user_id', Integer, ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
    Index('ix_external_storage_users', 'external_storage_id', 'user_id')
)


class CalDAVSyncToken(Base):
    """Track sync-token state for CalDAV sync-collection to properly report deletions."""
    __tablename__ = "caldav_sync_tokens"
    __table_args__ = (Index('ix_caldav_sync_tokens_user_calendar_token', 'user_id', 'calendar_name', 'sync_token'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    calendar_name = Column(String(100), nullable=False)  # e.g., "main", "work"
    sync_token = Column(String(255), nullable=False, index=True)  # Full sync-token URL
    event_uids = Column(Text, nullable=False)  # JSON array of event UIDs that existed at this token
    created_at = Column(DateTime, default=datetime.utcnow)


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

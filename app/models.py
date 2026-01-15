from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, ForeignKey, Index
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

    # Custom Image Generation service settings
    custom_image_enabled = Column(Boolean, default=False)
    custom_image_url = Column(String(500), nullable=True)

    # Scheduled news settings
    news_schedule_enabled = Column(Boolean, default=False)
    news_schedule_time = Column(String(5), default="12:00")  # HH:MM format, default noon
    news_sources = Column(Text, default="")  # Custom news sources, one per line: url|name

    # Miniflux news plugin settings (per-user override)
    miniflux_enabled = Column(Boolean, default=True)  # Whether to enable for this user
    miniflux_url = Column(String(500), nullable=True)  # User-specific Miniflux URL (overrides default)
    miniflux_username = Column(String(200), nullable=True)  # User-specific username
    miniflux_password = Column(String(500), nullable=True)  # User-specific password

    # Native RSS settings (per-user)
    rss_enabled = Column(Boolean, default=False)  # Whether native RSS is enabled for this user

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
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


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


# AI Plugin System

class Plugin(Base):
    """User-defined AI plugins for external service integration"""
    __tablename__ = "plugins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)  # NULL = global plugin
    name = Column(String(50), nullable=False)  # e.g., "budget", "flood"
    description = Column(Text, nullable=False)  # Description for the AI to understand when to use it
    base_url = Column(String(500), nullable=False)  # e.g., "https://budget.poster.place/api/v1"
    auth_type = Column(String(20), default="none")  # "none", "bearer", "basic", "header"
    auth_header = Column(String(100), default="X-API-Key")  # Header name for auth
    auth_value = Column(String(500), nullable=True)  # API key or credentials
    actions = Column(Text, nullable=False)  # JSON array of action definitions
    enabled = Column(Boolean, default=True)
    allowed_users = Column(Text, nullable=True)  # Comma-separated user IDs, NULL = all users
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", backref="plugins")

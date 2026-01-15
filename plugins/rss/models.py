"""
RSS Plugin Database Models

These models are registered dynamically when the plugin is loaded.
"""
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class RssFeed(Base):
    """RSS feed subscription"""
    __tablename__ = "rss_feeds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    url = Column(String(500), nullable=False)
    title = Column(String(255), nullable=True)
    custom_name = Column(String(255), nullable=True)
    enabled = Column(Boolean, default=True)
    last_fetched_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    entries = relationship("RssEntry", back_populates="feed", cascade="all, delete-orphan")

    @property
    def display_name(self) -> str:
        return self.custom_name or self.title or self.url


class RssEntry(Base):
    """Individual RSS entry/article"""
    __tablename__ = "rss_entries"
    __table_args__ = (Index('ix_rss_entries_guid', 'feed_id', 'guid'),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    feed_id = Column(Integer, ForeignKey("rss_feeds.id", ondelete="CASCADE"), nullable=False)
    guid = Column(String(500), nullable=False)
    title = Column(String(500), nullable=False)
    url = Column(String(500), nullable=True)
    content = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True)
    is_read = Column(Boolean, default=False)
    is_summarized = Column(Boolean, default=False)
    is_posted = Column(Boolean, default=False)  # Posted to Fediverse/external
    created_at = Column(DateTime, default=datetime.utcnow)

    feed = relationship("RssFeed", back_populates="entries")

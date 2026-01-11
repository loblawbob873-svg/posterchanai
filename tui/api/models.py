"""
Pydantic models for API responses.
"""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel


class User(BaseModel):
    """User model."""
    id: int
    username: str
    email: Optional[str] = None
    is_admin: bool = False
    notification_email: Optional[str] = None
    avatar: Optional[str] = None


class Message(BaseModel):
    """Chat message model."""
    id: int
    role: str  # "user" or "assistant"
    content: str
    image_path: Optional[str] = None
    created_at: datetime


class Conversation(BaseModel):
    """Conversation model."""
    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationWithMessages(Conversation):
    """Conversation with message history."""
    messages: List[Message] = []


class LoginResponse(BaseModel):
    """Login response with JWT token."""
    access_token: str
    token_type: str = "bearer"


class CommandResponse(BaseModel):
    """Command execution response."""
    type: str  # text, search, images, generated_image, music_play, music_playlist, etc.
    content: str
    data: Optional[dict] = None
    results: Optional[List[dict]] = None
    images: Optional[List[dict]] = None
    tracks: Optional[List[dict]] = None


class UserSettings(BaseModel):
    """User settings model."""
    notification_email: Optional[str] = None
    custom_ai_enabled: bool = False
    custom_ai_type: Optional[str] = None
    custom_ai_url: Optional[str] = None
    custom_ai_model: Optional[str] = None
    custom_image_enabled: bool = False
    custom_image_url: Optional[str] = None
    # Mail, calendar, music configs are JSON strings
    mail_accounts: Optional[List[dict]] = None
    caldav_url: Optional[str] = None
    carddav_url: Optional[str] = None
    webdav_music_url: Optional[str] = None


class Track(BaseModel):
    """Music track model."""
    path: str
    title: str
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[int] = None
    streamUrl: Optional[str] = None

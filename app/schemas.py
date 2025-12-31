from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


# Auth schemas
class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserLogin(BaseModel):
    username: str
    password: str


class UserRegister(BaseModel):
    username: str
    email: Optional[str] = None
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    is_admin: bool
    created_at: datetime

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# Chat schemas
class MessageCreate(BaseModel):
    content: str


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationCreate(BaseModel):
    title: Optional[str] = "New Chat"


class ConversationResponse(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationWithMessages(ConversationResponse):
    messages: List[MessageResponse] = []


# Settings schemas
class SettingUpdate(BaseModel):
    key: str
    value: str


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


class SettingsResponse(BaseModel):
    comfyui_url: str = ""
    comfyui_default_model: str = ""
    comfyui_anime_model: str = ""
    comfyui_timeout: str = "300000"
    searxng_url: str = ""
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_rate: str = "+5%"
    tts_pitch: str = "+10Hz"
    upload_path: str = "/var/lib/posterchanai"
    # Ollama settings
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "richardyoung/qwen3-14b-abliterated:Q5_K_M"
    ollama_timeout: str = "120000"
    ollama_max_concurrent: str = "1"
    ollama_system_prompt: str = ""
    # Advanced model settings
    ollama_temperature: str = "0.2"
    ollama_top_p: str = "0.9"
    ollama_top_k: str = "40"
    ollama_repeat_penalty: str = "1.1"
    ollama_num_ctx: str = "32768"
    ollama_num_predict: str = "4096"
    ollama_keep_alive: str = "-1"
    ollama_stop: str = ""
    ollama_seed: str = ""
    ollama_mirostat: str = "0"
    ollama_mirostat_eta: str = "0.1"
    ollama_mirostat_tau: str = "5.0"
    ollama_tfs_z: str = "1.0"
    # API settings
    openai_api_key: str = ""
    # Registration settings
    allow_registration: str = "false"
    # Ollama health check
    ollama_ping_enabled: str = "false"
    ollama_restart_command: str = "sudo systemctl restart ollama"
    ollama_ping_interval: str = "90"
    ollama_restart_after_failures: str = "5"
    # Email settings (SMTP)
    smtp_enabled: str = "false"
    smtp_host: str = ""
    smtp_port: str = "587"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "Posterchanai"
    smtp_use_tls: str = "true"
    smtp_use_ssl: str = "false"
    # Email settings (IMAP)
    imap_enabled: str = "false"
    imap_host: str = ""
    imap_port: str = "993"
    imap_username: str = ""
    imap_password: str = ""
    imap_use_ssl: str = "true"
    imap_sent_folder: str = "Sent"


# TTS schema
class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = None


class TTSResponse(BaseModel):
    audio: str  # base64 encoded


# Image generation schema
class ImageGenRequest(BaseModel):
    prompt: str


class ImageGenResponse(BaseModel):
    image_url: Optional[str] = None
    error: Optional[str] = None


# OpenAI-compatible API schemas
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[List[str]] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Optional[ChatCompletionUsage] = None


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "ollama"


class ModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


# API Key schemas
class APIKeyCreate(BaseModel):
    name: Optional[str] = "Default"


class APIKeyResponse(BaseModel):
    id: int
    name: str
    key: str  # Only shown once on creation
    created_at: datetime
    last_used_at: Optional[datetime] = None
    is_active: bool

    class Config:
        from_attributes = True


class APIKeyListItem(BaseModel):
    id: int
    name: str
    key_preview: str  # Only show last 4 chars
    created_at: datetime
    last_used_at: Optional[datetime] = None
    is_active: bool

    class Config:
        from_attributes = True

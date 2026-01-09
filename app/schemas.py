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
    image_path: Optional[str] = None
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
    # Native image generation settings
    image_backend: str = "comfyui"  # "native" or "comfyui"
    image_model_path: str = ""
    image_anime_model_path: str = ""  # Optional anime model for style switching
    image_model_type: str = "sdxl"  # "sd15", "sdxl", "sd3", "flux"
    image_default_steps: str = "20"
    image_default_cfg: str = "7.0"
    image_default_width: str = "1024"
    image_default_height: str = "1024"
    image_gpu_device: str = "auto"  # "auto", "cuda", "xpu", "cpu"
    image_idle_timeout: str = "120"  # Seconds before unloading image model (0=disabled)
    # VRAM management
    vram_mode: str = "shared"  # "shared" (swap models) or "dedicated" (keep both)
    searxng_url: str = ""
    tts_voice: str = "en-GB-SoniaNeural"
    tts_rate: str = "+5%"
    tts_pitch: str = "+10Hz"
    upload_path: str = "/var/lib/posterchanai"
    # LLM Backend settings
    llm_backend: str = "native"  # "native", "ipex", or "ollama"
    llm_model_path: str = ""
    llm_gpu_layers: str = "-1"  # -1 = all layers on GPU
    llm_n_threads: str = "0"  # 0 = auto-detect (cpu_count - 2)
    llm_n_batch: str = "1024"  # Batch size for prompt processing (higher = faster)
    llm_max_concurrent: str = "1"  # Max concurrent inference requests
    # CPU optimization settings
    llm_cpu_mode: str = "false"  # Force CPU-only (n_gpu_layers=0)
    llm_use_mmap: str = "true"  # Memory-map model file
    llm_use_mlock: str = "false"  # Lock model in RAM
    llm_idle_timeout: str = "0"  # Seconds before unloading LLM model (0=disabled)
    # Ollama settings (also used for native backend sampling parameters)
    ollama_url: str = "http://localhost:11434"
    ollama_api_format: str = "ollama"  # "ollama" for /api/chat, "openai" for /v1/chat/completions
    ollama_model: str = "richardyoung/qwen3-14b-abliterated:Q5_K_M"
    ollama_timeout: str = "120000"
    ollama_system_prompt: str = ""
    # Advanced model settings
    ollama_temperature: str = "0.2"
    ollama_top_p: str = "0.9"
    ollama_top_k: str = "40"
    ollama_repeat_penalty: str = "1.1"
    ollama_num_ctx: str = "16384"
    ollama_num_predict: str = "2048"
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
    # Load balancing - proxy chat to external posterchanai servers
    chat_server_urls: str = ""  # Comma-separated list of posterchanai server URLs for load balancing
    # Load balancing - proxy image generation to external posterchanai servers
    image_server_urls: str = ""  # Comma-separated list of posterchanai server URLs for image load balancing
    # Ollama health check
    ollama_ping_enabled: str = "false"
    ollama_restart_command: str = "sudo systemctl restart ollama"
    ollama_ping_interval: str = "90"
    ollama_restart_after_failures: str = "5"
    # GPU memory monitoring
    gpu_memory_check_enabled: str = "false"
    gpu_memory_threshold: str = "99"
    gpu_type: str = "nvidia"
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
    # News sources
    news_sources: str = ""
    # RAG (Retrieval-Augmented Generation) settings
    rag_enabled: str = "true"
    rag_embedding_model: str = "all-MiniLM-L6-v2"
    rag_chunk_size: str = "1000"
    rag_chunk_overlap: str = "200"
    rag_top_k: str = "5"
    rag_min_similarity: str = "0.3"
    rag_max_file_size: str = "1"
    rag_max_log_size: str = "100"
    rag_embedding_batch_size: str = "64"
    rag_num_threads: str = "0"
    rag_chromadb_path: str = "./data/chromadb"
    rag_auto_context: str = "true"
    rag_max_context_chars: str = "32000"  # Max total context injected into prompt
    rag_max_chunk_display: str = "8000"   # Max chars per chunk when displaying
    rag_max_chunk_index: str = "10000"    # Max chunk size during indexing
    rag_api_url: str = ""  # Remote RAG API URL for distributed setups
    # RAG cache settings
    rag_embedding_cache_max: str = "250000"  # Max cached embeddings
    rag_query_cache_max: str = "100000"      # Max cached query results
    rag_query_cache_ttl: str = "600"         # Query cache TTL in seconds
    # ChromaDB HNSW tuning
    rag_hnsw_ef_search: str = "100"          # Query-time accuracy
    rag_hnsw_ef_construction: str = "200"    # Index build quality
    rag_hnsw_m: str = "16"                   # Max connections per node


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

    class Config:
        extra = "ignore"  # Ignore extra fields like image_path


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


# User Settings schemas (for custom AI service)
class UserSettingsUpdate(BaseModel):
    notification_email: Optional[str] = None
    # Custom LLM settings
    custom_ai_enabled: Optional[bool] = None
    custom_ai_type: Optional[str] = None  # "ollama" or "openai"
    custom_ai_url: Optional[str] = None
    custom_ai_model: Optional[str] = None
    custom_ai_api_key: Optional[str] = None
    # Custom Image Generation settings
    custom_image_enabled: Optional[bool] = None
    custom_image_url: Optional[str] = None
    # Scheduled news settings
    news_schedule_enabled: Optional[bool] = None
    news_schedule_time: Optional[str] = None  # HH:MM format
    news_sources: Optional[str] = None  # Custom sources, one per line: url|name


class UserSettingsResponse(BaseModel):
    notification_email: Optional[str] = None
    avatar: Optional[str] = None
    # Custom LLM settings
    custom_ai_enabled: bool = False
    custom_ai_type: Optional[str] = None
    custom_ai_url: Optional[str] = None
    custom_ai_model: Optional[str] = None
    custom_ai_has_api_key: bool = False  # Don't expose actual key
    # Custom Image Generation settings
    custom_image_enabled: bool = False
    custom_image_url: Optional[str] = None
    # Scheduled news settings
    news_schedule_enabled: bool = False
    news_schedule_time: str = "12:00"
    news_sources: str = ""  # Custom sources, one per line: url|name


class TestConnectionRequest(BaseModel):
    api_type: str  # "ollama" or "openai"
    url: str
    model: Optional[str] = None
    api_key: Optional[str] = None
    use_stored_key: bool = False  # If true, use the user's stored API key


class TestConnectionResponse(BaseModel):
    success: bool
    message: str
    models: Optional[List[str]] = None  # Available models if successful


# RAG (Retrieval-Augmented Generation) schemas

class RAGCollectionCreate(BaseModel):
    name: str
    description: Optional[str] = None
    collection_type: str  # "folder", "git", "watcher"
    source_path: Optional[str] = None
    git_branch: Optional[str] = "main"
    file_patterns: Optional[str] = "*.py,*.js,*.ts,*.tsx,*.jsx,*.html,*.css,*.json,*.yaml,*.yml,*.md,*.txt,*.go,*.rs,*.java,*.sh"


class RAGCollectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    source_path: Optional[str] = None
    git_branch: Optional[str] = None
    file_patterns: Optional[str] = None


class RAGCollectionResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    collection_type: str
    source_path: Optional[str] = None
    git_branch: Optional[str] = None
    file_patterns: Optional[str] = None
    document_count: int = 0
    last_indexed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RAGQueryRequest(BaseModel):
    query: str
    collection_ids: Optional[List[int]] = None  # None = search all user collections
    top_k: Optional[int] = None


class RAGQueryResult(BaseModel):
    content: str
    file_path: str
    similarity: float
    collection_name: str


class RAGWatcherCreate(BaseModel):
    collection_id: int
    watch_path: str


class RAGWatcherResponse(BaseModel):
    id: int
    collection_id: int
    watch_path: str
    api_key: str
    is_active: bool
    last_event_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class RAGFileEvent(BaseModel):
    """Event from VS Code file watcher"""
    event_type: str  # "created", "modified", "deleted"
    file_path: str
    content: Optional[str] = None  # File content for create/modify


class RAGGitCloneRequest(BaseModel):
    name: str
    git_url: str
    branch: Optional[str] = "main"
    file_patterns: Optional[str] = "*.py,*.js,*.ts,*.tsx,*.jsx,*.html,*.css,*.json,*.yaml,*.yml,*.md,*.txt,*.go,*.rs,*.java,*.sh"


class RAGStatusResponse(BaseModel):
    enabled: bool
    embedding_model: str
    collections_count: int
    total_documents: int

from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, List, Union, Any
import typing
from datetime import datetime


# Auth schemas
class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserLogin(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    is_admin: bool
    storage_quota: int = 0  # 0 = unlimited
    can_image: bool = True
    can_music: bool = True
    can_video: bool = True
    can_torrent: bool = True
    can_blossom: bool = False
    can_ai: bool = False
    telegram_enabled: bool = False
    telegram_chat_id: Optional[str] = None
    telegram_notifications: str = ""
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
    # Native image generation (diffusers / torch-XPU). image_timeout also bounds the
    # image load-balancer request when chat_server_urls is configured.
    image_timeout: str = "300000"  # image request timeout in ms
    image_model_path: str = ""
    image_anime_model_path: str = ""  # Optional anime model for style switching
    image_model_type: str = "sdxl"  # "sd15", "sdxl", "sd3", "flux"
    image_default_steps: str = "20"
    image_default_cfg: str = "7.0"
    image_default_width: str = "1024"
    image_default_height: str = "1024"
    image_gpu_device: str = "auto"  # "auto", "cuda", "xpu", "cpu"
    image_idle_timeout: str = "120"  # Seconds before unloading image model (0=disabled)
    image_attention_slicing: str = "off"  # "off" (fastest), "auto" (balanced), "max" (least VRAM, slowest)
    image_subprocess_mode: str = "false"  # Run each image in subprocess for VRAM release (Intel XPU)
    # Music generation (ACE-Step). ACE-Step needs its own Python 3.11-3.12 env and ships a REST
    # server, so it runs as a SEPARATE service and the app talks to it over HTTP at the local
    # default (localhost:8001); cross-node LB uses chat_server_urls. Web UI + Telegram only.
    music_enabled: str = "true"
    music_gpu_device: str = "auto"  # "auto"/"cuda"/"xpu"/"cpu" — picks the GPU vs CPU lock locally
    music_model: str = ""  # DiT model name/path, e.g. acestep-v15-turbo (blank = server default)
    music_default_duration: str = "180"  # seconds (ACE-Step range 10-600)
    music_default_steps: str = "8"  # diffusion steps (turbo ~8, base up to ~200)
    music_format: str = "mp3"  # mp3 | wav | flac | opus | aac
    music_timeout: str = "300000"  # music request timeout in ms (mirrors image_timeout)
    music_watermark_enabled: str = "true"  # append the branded end-card outro to the song video
    # Video generation (videogeni — NATIVE in-process diffusers, like image gen; mirrors music LB)
    video_enabled: str = "false"
    video_local_enabled: str = "true"  # generate on THIS node's GPU (the native diffusers path)
    video_gpu_device: str = "auto"  # "auto"/"cuda"/"xpu"/"cpu" — picks the GPU vs CPU lock locally
    video_model: str = "Wan-AI/Wan2.1-T2V-1.3B-Diffusers"  # HF id / local path of the diffusers model
    video_cpu_offload: str = "false"  # stream weights from RAM (fits big models like CogVideoX-5B on
    # a 16GB GPU; much slower). Needed for 10s/HD models that don't fit fully in VRAM.
    video_free_music: str = "false"  # on a node where music+video share ONE GPU: stop the acestep
    # music server during a video render (free its VRAM), restart it for music. Needs passwordless sudo.
    music_service_name: str = "acestep"  # the systemd unit name of the local ACE-Step music server
    video_width: str = "832"  # GENERATION width  (multiple of 16). Wan2.1-1.3B is a 480p model.
    video_height: str = "480"  # GENERATION height (multiple of 16)
    video_upscale_height: str = "720"  # upscale the finished clip to this height (0/native = none).
    # The 1.3B model only generates ~480p well; this lanczos-upscales the output to 720p/1080p
    # (cheap, no extra VRAM). For TRUE native 720p you'd need the Wan 14B model (more VRAM).
    video_num_frames: str = "49"  # frames (Wan: 4k+1). Arc 16GB is tight — cap lower there.
    video_max_frames: str = "81"  # hard ceiling (clamp) — model maxes ~81 (5s); >this OOMs 16GB
    video_fps: str = "16"  # playback fps for the assembled mp4
    video_default_steps: str = "32"  # diffusion steps
    video_guidance: str = "5.0"  # classifier-free guidance scale
    video_timeout: str = "600000"  # video request timeout in ms
    video_watermark_enabled: str = "true"  # append the branded end-card outro to the clip
    # VRAM management
    vram_mode: str = "shared"  # "shared" (swap models) or "dedicated" (keep both)
    searxng_url: str = ""
    torrent_site_url: str = ""  # TorrentGalaxy or compatible site URL
    tts_voice: str = "en-GB-SoniaNeural"
    tts_rate: str = "+5%"
    tts_pitch: str = "+10Hz"
    upload_path: str = "/var/lib/posterchanai"
    # LLM settings (backend is always native llama.cpp — SYCL/CUDA/HIP/CPU)
    llm_model_path: str = ""
    # GGUF (absolute path preferred; a bare basename is resolved against the models dir) used for
    # tool-calling / agentic jobs: opencode/aider via /v1, the `node agent` command (web UI +
    # Telegram), and the system-health report. BLANK (default) → use llm_model_path, so it always
    # points at a model that exists. The installer sets this to a downloaded coding model's full
    # path; Docker via POSTERCHANAI_LLM_TOOLS_MODEL. Admin-overridable in the UI.
    llm_tools_model: str = ""
    llm_gpu_layers: str = "-1"  # -1 = all layers on GPU
    llm_n_threads: str = "0"  # 0 = auto-detect (cpu_count - 2)
    llm_n_batch: str = "1024"  # Batch size for prompt processing (higher = faster)
    llm_max_concurrent: str = "1"  # Max concurrent inference requests
    # CPU optimization settings
    llm_cpu_mode: str = "false"  # Force CPU-only (n_gpu_layers=0)
    llm_use_mmap: str = "true"  # Memory-map model file
    llm_use_mlock: str = "true"  # Lock model in RAM
    llm_idle_timeout: str = "0"  # Seconds before unloading LLM model (0=disabled)
    llm_token_timeout: str = "600"  # Max seconds between tokens during streaming
    # LLM generation parameters (the `ollama_` prefix is a legacy key namespace — these are the
    # native llama.cpp backend's sampling/runtime settings, NOT an Ollama connection).
    ollama_model: str = "native"  # display/label for the loaded model
    ollama_timeout: str = "300000"  # LLM request timeout in ms (5 min; video summaries need longer)
    ollama_system_prompt: str = ""
    # Advanced model settings
    ollama_temperature: str = "0.2"
    ollama_top_p: str = "0.9"
    ollama_top_k: str = "40"
    ollama_repeat_penalty: str = "1.1"
    ollama_num_ctx: str = "auto"  # "auto" sizes the window to fit the detected GPU; or an explicit int
    ollama_num_predict: str = "8192"
    # Separate, higher output cap used ONLY when a request carries tools (agentic coding clients
    # like opencode emit whole-file writes as one tool call - the lower num_predict cap truncates
    # large files mid-function). Plain chat keeps ollama_num_predict so slow generations stay under
    # the client's request timeout.
    ollama_tool_num_predict: str = "16384"
    # Small-context coding guidance injected server-side into the system prompt of any request that
    # carries tools (opencode et al.), so large-file editing behaves without each client needing its
    # own AGENTS.md. Empty tool_guidance_text => the built-in default in openai_api._DEFAULT_TOOL_GUIDANCE.
    tool_guidance_enabled: str = "true"
    tool_guidance_text: str = ""
    # Append the TikTok-style "made with PosterChanAI" end-card to effect videos (per-user
    # avatar/@username in the web UI / Telegram / Matrix; static card for bot-posted effects).
    effect_outro_enabled: str = "true"
    ollama_stop: str = ""
    ollama_seed: str = ""
    ollama_mirostat: str = "0"
    ollama_mirostat_eta: str = "0.1"
    ollama_mirostat_tau: str = "5.0"
    ollama_tfs_z: str = "1.0"
    # Load balancing - ONE unified list of posterchanai node URLs that drives chat, image, music
    # and video LB (Site → Load Balancing → Server URLs).
    chat_server_urls: str = ""  # Comma-separated list of posterchanai server URLs for load balancing
    # Nostr-native distributed LB (NIP-90 DVM). The cluster = the relay's Web-of-Trust seed npubs
    # (configured in Admin → Relay); jobs are dispatched to those nodes over Nostr (encrypted job →
    # peer → encrypted result) instead of the IP/HTTP LB above.
    nostr_dvm_enabled: bool = False
    nostr_dvm_blossom_url: str = ""    # shared Blossom base URL for media transfer (blank = blossom_public_url)
    # Native LLM health check (ping the loaded model; reload it on repeated failure / high VRAM)
    llm_health_check_enabled: str = "false"
    llm_health_check_interval: str = "90"
    llm_reload_after_failures: str = "5"
    # GPU memory monitoring
    gpu_memory_check_enabled: str = "false"
    gpu_memory_threshold: str = "99"
    gpu_type: str = "nvidia"
    nvidia_reset_before_reload: str = "false"  # Run scripts/nvidia_reset.sh before native model reload (NVIDIA only)
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
    # Intelligent Intent Detection settings
    intent_detection_enabled: str = "true"  # Enable AI-powered intent detection for natural language actions (only triggers on action keywords)
    intent_confidence_threshold: str = "0.7"  # Minimum confidence to execute detected actions (0.0-1.0)
    # System health report (agentic): drives node_service.run_agent across the Remote Node
    # Management nodes; see app/services/logs_scheduler.py
    logs_scheduler_enabled: str = "false"
    logs_schedule: str = "1,12,18"
    logs_nodes: str = ""  # comma-separated node names to include (empty = all configured nodes + local)
    # Social notification relay (Misskey/Pleroma/Matrix → Telegram)
    social_notif_enabled: str = "true"       # global kill-switch (on by default; per-user toggle in User Settings is the real control)
    social_notif_poll_seconds: str = "60"    # poll interval in seconds
    # Fediverse timeline → Matrix room bridge (see app/services/fedi_timeline_service.py).
    # Shared feed: one source instance mirrored into one Matrix room; members act under their
    # own linked accounts. Cursor is kept in a separate Setting row (fedi_timeline_since).
    fedi_timeline_enabled: str = "false"
    fedi_timeline_platform: str = "misskey"          # misskey | pleroma
    fedi_timeline_instance_url: str = ""
    fedi_timeline_token: str = ""                     # service token used to READ the source feed
    fedi_timeline_type: str = "home"                  # home | global | local
    fedi_timeline_matrix_homeserver: str = ""
    fedi_timeline_matrix_bot_token: str = ""          # token used to POST into the room
    fedi_timeline_room_id: str = ""
    fedi_timeline_poll_seconds: str = "90"
    fedi_timeline_include_replies: str = "true"
    # When true, replies are mirrored as inline rich-replies (shown in the main timeline like the
    # fediverse) instead of Matrix threads (hidden behind a "N replies" click). Backend dedup/reply
    # bookkeeping is unchanged; only the wire relation (m.in_reply_to vs m.thread) differs.
    fedi_timeline_inline_replies: str = "false"
    # Personal fedi notifications → private Matrix DM (uses the fedi-timeline bot account).
    # Admin kill-switch (default off); per-user opt-in is the User.matrix_notif_enabled column.
    matrix_notif_enabled: str = "false"
    matrix_notif_poll_seconds: str = "60"
    # Remote node management (run OS commands on nodes over SSH, or 'local' on this host)
    node_exec_enabled: str = "false"
    node_exec_nodes: str = ""  # one per line: name|user@host  (host 'local' or empty = run on this host)
    node_exec_users: str = ""  # comma-separated usernames allowed (admins always allowed)
    node_exec_agent_max_steps: str = "8"  # max LLM iterations in agentic mode
    node_exec_agent_model: str = "Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"  # agentic-tuned model for `node agent` (falls back to default if absent)
    node_exec_agent_step_timeout: str = "600"  # max seconds per command in `node agent` (0 = use job timeout); bounds long/hung commands so the loop can't deadlock
    node_exec_job_timeout: str = "0"  # per-job timeout in seconds (0 = no timeout)
    # Finance (Budget Manager) integration — per-user API keys live on User; this is the shared base URL
    finance_api_base: str = "http://localhost:5001"
    # Screenshot: hosts allowed to bypass the SSRF private-IP guard (the operator's own
    # domains that resolve to a LAN IP via split-horizon DNS). Comma/space/newline-separated;
    # a parent domain also covers its subdomains (e.g. poster.place allows www.poster.place).
    screenshot_allowed_hosts: str = ""
    # Built-in torrent client settings
    bt_enabled: str = "false"
    bt_server_url: str = ""  # Remote torrent server URL (empty = local)
    bt_download_dir: str = "/var/lib/posterchanai/torrents"
    storage_server_url: str = ""  # Remote storage server URL (empty = local)
    ytdl_cookies_path: str = ""  # Optional Netscape cookies file for yt-dlp (YouTube 403 workaround)
    ytdl_no_ssl_verify: str = "false"  # Skip SSL cert verification for yt-dlp (proxy/firewall hostname mismatch)
    file_cache_enabled: str = "true"  # Enable file listing cache
    file_cache_ttl: str = "300"  # File cache TTL in seconds (default: 5 minutes)
    file_cache_max_size: str = "1000"  # Maximum number of cached directory listings
    bt_proxy_host: str = ""
    bt_proxy_port: str = "8118"
    bt_listen_port: str = "6881"
    # Built-in Tor settings
    tor_enabled: str = "false"
    tor_listen_host: str = "127.0.0.1"
    tor_socks_port: str = "9052"
    tor_control_port: str = "9053"
    tor_exit_nodes: str = "{us}"
    tor_data_dir: str = "/var/lib/posterchanai/tor"
    # Built-in HTTP proxy settings
    proxy_enabled: str = "false"
    proxy_listen_host: str = "127.0.0.1"
    proxy_listen_port: str = "8118"
    proxy_socks_host: str = ""
    proxy_socks_port: str = "9052"
    # Telegram Bot settings
    telegram_bot_token: str = ""  # Bot token from @BotFather
    telegram_webhook_url: str = ""  # Webhook URL for receiving updates
    telegram_enabled: str = "false"  # Enable Telegram bot
    # Bot framework (merged from ~/posterchan; managed in Admin → Bots). These are the
    # GLOBAL settings shared by every managed bot; per-bot config lives on the Bot model.
    # bot_manager_service maps these into the env vars botframework/config.py expects when it
    # spawns each listener (OPENAI_ENDPOINT/KEY, AI_MODEL, SQL_*, SEARXNG_URL, TIMEZONE, …).
    # Master kill-switch (default off): the manager runs NO bots until this is on. Lets a node
    # deploy the merged code safely while the legacy posterchan.service still owns the bots —
    # flip on only after retiring the old service to avoid double-posting.
    bots_manager_enabled: str = "false"
    # Unified codebase: ONE PosterChanAI server URL drives everything. The bots reach the shared
    # LLM at {server}/api/chat/completions and do image generation via the same server's API
    # (they're separate processes, so they use HTTP rather than the in-process GPU model). No
    # separate OpenAI endpoint, no ComfyUI/Stable-Diffusion.
    bots_server_url: str = ""                  # e.g. https://ai.poster.place  (chat + image)
    bots_ai_api_key: str = ""                  # key for the server's chat endpoint
    bots_ai_model: str = ""
    bots_posterchanai_username: str = ""       # app login the bots use for the image API
    bots_posterchanai_password: str = ""
    bots_posterchanai_api_key: str = ""        # per-server image API key (X-API-Key)
    # (web search reuses the app's own `searxng_url` setting — no separate bot copy)
    bots_timezone: str = "MST"
    bots_sql_user: str = ""                    # Pleroma Postgres creds (blockbot/welcome/report)
    bots_sql_pass: str = ""
    bots_sql_host: str = ""
    # Built-in Nostr WoT relay (own thread; serves NIP-01 at /relay on nostr_relay_port). These are
    # GLOBAL admin settings (the admin Settings page reads/writes them via this model) — keep them
    # here, NOT in the per-user UserSettingsUpdate, or GET /settings drops them and the relay/blossom
    # fields show blank + "never save" in the admin UI.
    nostr_relay_enabled: Optional[bool] = None
    nostr_relay_disable_proxy: Optional[bool] = None  # bypass Tor for relay upstream traffic
    nostr_relay_backup_datastore: Optional[bool] = False  # broadcast the encrypted pcai: config docs (settings/accounts/bots) to upstream relays for disaster recovery
    nostr_relay_firehose_enabled: Optional[bool] = None  # live firehose sync (real-time)
    nostr_relay_bind: Optional[str] = None
    nostr_relay_port: Optional[int] = None
    nostr_relay_wot_seeds: Optional[str] = None          # npub/hex seeds, newline/comma
    nostr_relay_upstream_relays: Optional[str] = None     # blank = bots' DEFAULT_RELAYS
    nostr_relay_retention_days: Optional[int] = None  # auto-clean feed notes older than N days (0=off)
    nostr_relay_max_events: Optional[int] = None      # hard count cap on feed events (0=unlimited)
    nostr_relay_wot_enabled: Optional[bool] = True
    nostr_relay_send_only: Optional[bool] = False
    nostr_relay_wot_refresh_sec: Optional[int] = None
    nostr_relay_wot_depth: Optional[int] = None
    nostr_relay_wot_min_followers: Optional[int] = None
    nostr_relay_wot_max: Optional[int] = None
    nostr_relay_max_connections: Optional[int] = None
    nostr_relay_fetch_ancestors: Optional[bool] = None
    nostr_relay_max_ancestors: Optional[int] = None
    nostr_relay_sync_window_sec: Optional[int] = None
    nostr_relay_sync_interval_sec: Optional[int] = None
    nostr_relay_sync_idle_interval_sec: Optional[int] = None
    nostr_relay_backfill_hours: Optional[int] = None
    nostr_relay_overlap_sec: Optional[int] = None
    nostr_relay_ingest_kinds: Optional[str] = None
    nostr_relay_author_batch: Optional[int] = None
    nostr_relay_request_pace_sec: Optional[float] = None
    nostr_relay_outbox_min_interval_sec: Optional[float] = None
    nostr_relay_outbox_max_queue: Optional[int] = None
    nostr_relay_name: Optional[str] = None
    nostr_relay_description: Optional[str] = None
    nostr_relay_pubkey: Optional[str] = None
    nostr_relay_contact: Optional[str] = None
    nostr_relay_icon: Optional[str] = None
    nostr_relay_advertise_restricted_writes: Optional[bool] = False
    nostr_relay_mirror_feeds: Optional[bool] = False
    nostr_relay_firehose_max_relays: Optional[int] = 0
    nostr_relay_blocked_langs: Optional[str] = None
    nostr_relay_blocked_words: Optional[str] = None
    nostr_relay_blocked_pubkeys: Optional[str] = None
    nostr_relay_blocked_relays: Optional[str] = None
    nostr_relay_block_bridged: Optional[bool] = False
    nostr_relay_nip05_enabled: Optional[bool] = True
    nostr_relay_nip05_names: Optional[str] = (
        "verita84 4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6\n"
        "posterchan c7de13bab5818ab7918b5b47a05de11735c4e519e49c8577fd7ce7267fe84d4b"
    )
    nostr_relay_nip05_relays: Optional[str] = "wss://relay.poster.place"
    nostr_relay_nip05_domain: Optional[str] = None
    nostr_relay_pg_dsn: Optional[str] = None
    nostr_relay_prune_interval_sec: Optional[str] = None
    # Built-in Blossom media server (BUD-01/02). Served by the app at /blossom (front with TLS).
    blossom_enabled: Optional[bool] = None
    blossom_public_url: Optional[str] = None
    blossom_blob_ttl_days: Optional[int] = None
    blossom_max_upload_mb: Optional[int] = None
    blossom_storage_backend: Optional[str] = None
    blossom_storage_path: Optional[str] = None
    blossom_cache_mb: Optional[int] = None
    blossom_whitelist: Optional[str] = None
    blossom_mirror_servers: Optional[str] = None   # DR: external Blossom servers to mirror blobs to
    tenor_api_key: Optional[str] = None
    giphy_api_key: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _empty_strings_to_none(cls, data):
        """Settings are stored as strings; a numeric/bool field stored as "" would 500 the whole
        settings GET on validation. Coerce "" → None for non-string declared fields so it falls back
        to the field default instead."""
        if isinstance(data, dict):
            for name, field in cls.model_fields.items():
                if data.get(name) == "":
                    ann = field.annotation
                    args = typing.get_args(ann)
                    base = next((a for a in args if a is not type(None)), ann) if args else ann
                    if base is not str:
                        data[name] = None
        return data

    class Config:
        extra = "allow"  # Allow arbitrary extra settings


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
def _normalize_message_content(value: Union[str, List[Any], None]) -> str:
    """Accept OpenAI-style content: string or list of parts (e.g. {"type": "text", "text": "..."})."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(str(item["text"]))
                elif "text" in item:
                    parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) if parts else ""
    return str(value)


class ChatMessage(BaseModel):
    role: str
    content: Union[str, List[Any]] = ""
    tool_calls: Optional[List[Any]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    class Config:
        extra = "ignore"

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, v: Union[str, List[Any], None]) -> str:
        return _normalize_message_content(v)


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
    tools: Optional[List[Any]] = None
    tool_choice: Optional[Any] = None

    class Config:
        extra = "ignore"


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
    # Context size for OpenAI-compatible clients (e.g. OpenClaw); minimum 16000 for agent use
    root_context_length: Optional[int] = None
    context_length: Optional[int] = None


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
    # Scheduled news settings
    news_schedule_enabled: Optional[bool] = None
    news_schedule_time: Optional[str] = None  # HH:MM format
    news_sources: Optional[str] = None  # Custom sources, one per line: url|name
    # Mail settings
    mail_accounts: Optional[List[dict]] = None  # List of {email, imap_server, imap_port, smtp_server, smtp_port, password}
    # Telegram settings — linking/unlinking managed via /api/telegram/*, not here
    telegram_notifications: Optional[str] = None
    # Misskey settings
    misskey_enabled: Optional[bool] = None
    misskey_instance_url: Optional[str] = None
    misskey_api_token: Optional[str] = None
    # Pleroma settings (read-only via /api/pleroma/connect; exposed here for display)
    pleroma_enabled: Optional[bool] = None
    pleroma_instance_url: Optional[str] = None
    # Nostr settings (key linked via /api/nostr/connect; relays/media editable here)
    nostr_enabled: Optional[bool] = None
    nostr_relays: Optional[str] = None
    nostr_media_service: Optional[str] = None
    nostr_media_endpoint: Optional[str] = None
    # Matrix settings
    matrix_enabled: Optional[bool] = None
    matrix_homeserver: Optional[str] = None
    matrix_dm_bot_user_id: Optional[str] = None
    # Finance (Budget Manager) — per-user API key
    finance_api_key: Optional[str] = None
    # Relay social notifications (Misskey/Pleroma/Matrix) to Telegram
    social_notif_enabled: Optional[bool] = None
    # Relay fediverse notifications to Matrix DM (independent of the Telegram toggle above)
    matrix_notif_enabled: Optional[bool] = None
    # Nitter RSS feeds (newline-separated URLs) posted as image cards to Telegram
    nitter_feeds: Optional[str] = None
    # NOTE: the global relay/Blossom/GIF settings that used to live here were MOVED to
    # SettingsResponse (they're admin-global, not per-user) — see that class.


class UserSettingsResponse(BaseModel):
    notification_email: Optional[str] = None
    avatar: Optional[str] = None
    # Scheduled news settings
    news_schedule_enabled: bool = False
    news_schedule_time: str = "12:00"
    news_sources: str = ""  # Custom sources, one per line: url|name
    # Mail settings
    mail_accounts: List[dict] = []  # List of mail accounts (passwords masked)
    # Telegram settings
    telegram_enabled: bool = False
    telegram_chat_id: Optional[str] = None
    telegram_notifications: str = ""
    telegram_pending_key: Optional[str] = None       # Pending link key (exposed to owner only)
    telegram_key_expires_at: Optional[datetime] = None
    # Misskey settings
    misskey_enabled: bool = False
    misskey_instance_url: Optional[str] = None
    misskey_has_api_token: bool = False
    # Pleroma settings
    pleroma_enabled: bool = False
    pleroma_instance_url: Optional[str] = None
    pleroma_has_access_token: bool = False
    # Nostr settings (the secret key is never returned, only whether one is set)
    nostr_enabled: bool = False
    nostr_npub: Optional[str] = None
    nostr_has_key: bool = False
    nostr_relays: Optional[str] = None
    nostr_media_service: Optional[str] = None
    nostr_media_endpoint: Optional[str] = None
    # Matrix settings
    matrix_enabled: bool = False
    matrix_homeserver: Optional[str] = None
    matrix_user_id: Optional[str] = None
    matrix_has_access_token: bool = False
    matrix_dm_bot_user_id: Optional[str] = None
    # Finance (Budget Manager) — per-user API key (key itself never exposed)
    finance_has_api_key: bool = False
    # Relay social notifications (Misskey/Pleroma/Matrix) to Telegram
    social_notif_enabled: bool = False
    # Relay fediverse notifications to Matrix DM (independent of the Telegram toggle above)
    matrix_notif_enabled: bool = False
    # Nitter RSS feeds (newline-separated URLs) posted as image cards to Telegram
    nitter_feeds: str = ""



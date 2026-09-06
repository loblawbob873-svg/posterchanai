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
    can_media: bool = False
    can_blossom: bool = False
    can_stream: bool = False
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
    # Local, low-balance Monero tip wallet. These node settings are stored in the operator-signed,
    # NIP-44-encrypted settings document. The password is masked by the admin API on reads.
    monero_wallet_enabled: str = ""
    monero_wallet_rpc_url: str = ""
    monero_wallet_rpc_user: str = ""
    monero_wallet_rpc_password: str = ""
    monero_wallet_network: str = ""
    monero_wallet_transfer_cap_xmr: str = ""
    monero_wallet_daily_cap_xmr: str = ""
    monero_wallet_rpc_timeout: str = ""
    # ---- multi-chain wallet (Discover → Exodus Wallet) ----------------------------------------
    # OFF by default: it is custodial (this node holds the seeds), so an operator turns it on
    # deliberately rather than discovering they are holding somebody's Bitcoin.
    exodus_wallet_enabled: str = ""
    # Where each chain is read from. Empty falls back to a public endpoint, which is a fallback and
    # not a plan — a node that matters points at its own. Declared one per chain rather than as a
    # JSON blob so Admin can hydrate and save them with no per-field JS, the way every other
    # setting here works.
    exodus_rpc_btc: str = ""
    exodus_rpc_eth: str = ""
    exodus_rpc_ltc: str = ""
    exodus_rpc_doge: str = ""
    exodus_rpc_bch: str = ""
    exodus_rpc_matic: str = ""
    exodus_rpc_bnb: str = ""
    exodus_rpc_avax: str = ""
    exodus_rpc_sol: str = ""
    monero_wallet_spend_ledger: str = ""
    #: The operator's cut of each CUSTODIAL zap, as a percentage. Only the path where this
    #: node executes the transfer can be charged — the URI/QR flow is non-custodial and the
    #: payment never touches this server. Blank or 0 disables it.
    monero_zap_fee_percent: str = "2"
    #: Where that cut goes. Blank means the node's own wallet address.
    monero_zap_fee_address: str = ""
    # Default UI theme for the Nostr web client (/client) — applied to any visitor/device that
    # hasn't picked their own theme. One of CLIENT_THEMES; the client falls back to "cyberpunk" (the
    # flagship bare-:root theme) if unknown. Stored in the relay like every other setting.
    client_default_theme: str = "cyberpunk"
    # Master admission switch. Existing accounts can always sign in; only creation of a new User is
    # refused. Stored in the operator's encrypted settings document like the rest of Admin → Site.
    registration_enabled: str = "true"
    # Custom branding: URL of an image to replace the built-in PosterChan logo in the web client's
    # favicon, login splash and header (Admin → Site). Blank → the default logo. Stored in
    # the relay like every other setting.
    site_logo_url: str = ""
    # Instance custom emoji (Pleroma/Akkoma-style packs): the directory holding them, managed from
    # Admin → Emoji and offered in the web client's emoji picker. Relative
    # paths resolve against the install root; blank switches the feature off. The FILES are per-node
    # operator content (gitignored) — only this path is a setting.
    custom_emoji_dir: str = "assets/emoji"
    # Native image generation (diffusers / torch-XPU). image_timeout also bounds the
    # image load-balancer request when chat_server_urls is configured.
    image_timeout: str = "300000"  # image request timeout in ms
    image_model_path: str = ""
    image_anime_model_path: str = ""  # Optional anime model for style switching
    image_model_type: str = "sdxl"  # "sd15", "sdxl", "sd3", "flux"
    # The negative prompt used when a request does not supply its own.
    #
    # THE MONOCHROME TERMS ARE LOAD-BEARING, not padding. The anime path routes to Danbooru-tagged
    # SDXL checkpoints (Illustrious / NoobAI / Pony), and a large slice of that training data is
    # monochrome manga art tagged `monochrome`, `greyscale`, `sketch`, `lineart`. Nothing in the old
    # default ("bad quality, blurry, distorted, ugly, deformed, low resolution") steers away from it,
    # so those models routinely landed there and returned COLOURLESS LINE SKETCHES for an ordinary
    # coloured-illustration prompt — reported as "geni always produces colorless sketches". It is a
    # conditioning problem, not a broken checkpoint.
    image_negative_prompt: str = ("bad quality, blurry, distorted, ugly, deformed, low resolution, "
                                  "monochrome, greyscale, grayscale, sketch, lineart, line art, "
                                  "unfinished, rough sketch, flat color")
    image_default_steps: str = "20"
    image_default_cfg: str = "7.0"
    image_default_width: str = "1024"
    image_default_height: str = "1024"
    image_gpu_device: str = "auto"  # "auto", "cuda", "xpu", "cpu"
    image_idle_timeout: str = "120"  # Seconds before unloading image model (0=disabled)
    image_attention_slicing: str = "off"  # "off" (fastest), "auto" (balanced), "max" (least VRAM, slowest)
    image_subprocess_mode: str = "false"  # Run each image in subprocess for VRAM release (Intel XPU)
    # Music generation (ACE-Step). ACE-Step needs its own Python 3.11-3.12 env and ships a REST
    # server. It now generates IN-PROCESS on the app's own torch stack + GPU lock (no sidecar, no
    # second venv, no HTTP hop); cross-node LB uses chat_server_urls. Web UI + Telegram only.
    music_enabled: str = "true"
    music_gpu_device: str = "auto"  # "auto"/"cuda"/"xpu"/"cpu" — picks the GPU vs CPU lock locally
    music_model: str = ""  # checkpoint DIR under <ACESTEP_ROOT>/checkpoints (blank = acestep-v15-turbo)
    # Generate music IN-PROCESS via upstream's AceStepHandler instead of an external ACE-Step server.
    # ON by default — the sidecar is retired. (diffusers' AceStepPipeline is NOT what loads: no
    # published ACE-Step repo ships a model_index.json, so its from_pretrained 404s.)
    music_native: str = "true"   # in-process ACE-Step (no sidecar); false forces the HTTP path
    # The native path's own knobs. These were read by music_local but defined in NO schema, so they
    # could never be written, never became `pcai:setting:` events, and silently stayed at the code
    # default forever — the same defect that pinned every song to the fallback duration.
    music_cpu_offload: str = "false"  # accelerate CPU offload; CUDA-only (meta-tensor bug on XPU)
    music_guidance: str = "7.5"       # classifier-free guidance scale
    music_idle_timeout: str = "600"   # seconds before the idle monitor frees the music model's VRAM
    music_default_duration: str = "180"  # seconds (ACE-Step range 10-600)
    music_default_steps: str = "8"  # diffusion steps (turbo ~8, base up to ~200)
    music_format: str = "mp3"  # mp3 | wav | flac | opus | aac
    music_timeout: str = "300000"  # music request timeout in ms (mirrors image_timeout)
    music_watermark_enabled: str = "true"  # append the branded end-card outro to the song video
    # Voice cloning (Chatterbox, zero-shot). The FIRST local speech model in the stack — `tts_voice`
    # above is edge-tts, i.e. Microsoft's CLOUD voices, which cost no GPU and stay the default for
    # ordinary narration. This competes for the same single GPU as chat/image/music/video and runs at
    # roughly 10x realtime, so it is only ever reached by an explicit `voice` request. Web UI +
    # Telegram only, never the fedi bots: cloning a voice is an impersonation surface, the same
    # reasoning that keeps music and video off them.
    voice_enabled: str = "false"        # master switch; OFF by default (6.1GB of weights to fetch first)
    voice_device: str = "auto"          # "auto"/"cuda"/"xpu"/"cpu" — "cuda" also covers ROCm
    voice_model: str = "ResembleAI/chatterbox"   # HF id of the zero-shot cloning model
    voice_exaggeration: str = "0.5"     # how much emotion carries over from the reference clip
    voice_cfg_weight: str = "0.5"       # similarity vs stability (higher = closer, more artefacts)
    voice_temperature: str = "0.8"      # sampling temperature
    voice_max_chars: str = "800"        # per-request cap: the model degrades on long single passes
    voice_max_ref_seconds: str = "30"   # longest reference clip accepted (a few seconds is plenty)
    voice_timeout: str = "600000"       # request timeout in ms (mirrors music_timeout)
    voice_watermark_enabled: str = "true"  # append the branded end-card outro to the spoken video
    # Voice/video calls (WebRTC, P2P-first, Nostr-signaled) + the built-in Pion TURN/STUN relay. The
    # relay is a bundled Go binary the app supervises (turn_service.py); FastAPI mints short-lived TURN
    # REST creds from turn_shared_secret. TURN is opt-in and needs one open public port + a grey-clouded
    # turn.<domain>. calls_* gate the client feature. See docs + app/routers/calls.py.
    calls_enabled: str = "true"        # show the Calls feature in the web client
    calls_default_video: str = "false"  # audio-first; video is opt-in per call (bandwidth/battery)
    turn_enabled: str = "false"        # run the built-in TURN relay + advertise it in ICE (needs a public port)
    turn_domain: str = ""              # e.g. turn.poster.place (grey-clouded A record → this server)
    turn_public_ip: str = ""           # public IP advertised in relay candidates (required when turn_enabled)
    turn_port: str = "3478"            # STUN+TURN UDP+TCP port
    turn_tls_port: str = ""            # TURN-over-TLS port (443 recommended for restrictive nets); blank = off
    turn_tls_cert: str = ""            # PEM cert path for turns:// (blank = no TLS listener)
    turn_tls_key: str = ""             # PEM key path for turns://
    turn_realm: str = "posterchan"
    turn_shared_secret: str = ""       # HMAC secret shared by the minted creds + the Pion server (auto-generated)
    turn_relay_min_port: str = "49160"  # relay UDP port range (small = few ports to forward; widen to scale)
    turn_relay_max_port: str = "49200"
    stun_fallback_urls: str = ""       # optional public STUN (comma list) used when turn_enabled is off
    # OBS streaming — bundled MediaMTX (stream_service.py) ingests RTMP from OBS + remuxes to HLS for the
    # NIP-53 Streams tab. Publish auth reuses the per-user API key; see app/routers/streams.py.
    stream_enabled: str = "false"      # run the built-in MediaMTX media server (needs a public RTMP port)
    stream_domain: str = ""            # host OBS pushes to (e.g. stream.poster.place → this server); blank = public IP
    stream_rtmp_port: str = "1935"     # RTMP ingest port (open/forward this for OBS)
    stream_hls_port: str = "8888"      # MediaMTX HLS output port (proxied by the app unless stream_hls_base is set)
    stream_hls_base: str = ""          # direct public HLS base (grey-clouded subdomain) for scale; blank = app proxy
    stream_srt_port: str = ""          # optional SRT ingest port; blank = off
    stream_webrtc_port: str = "8889"   # WebRTC/WHIP ingest (HTTP) — lets a phone go live from the browser
    stream_webrtc_udp_port: str = "8189"  # WebRTC media (UDP/ICE); forward this + set a public IP for remote phones
    stream_auth_secret: str = ""       # secret the app injects into MediaMTX's auth-hook URL (auto-generated); gates /api/streams/auth
    stream_api_port: str = "9997"      # MediaMTX control API, LOOPBACK-only; stream_end_service asks it if a path is still publishing
    # Save ended live streams to the streamer's Blossom drive (stream_vod_service). MediaMTX records each
    # stream as fmp4 into a temp dir; on confirmed end it's streamed into Blossom and indexed for the
    # web-UI "Past streams" view. The dir is just a path — mount it as tmpfs/point it at /dev/shm if you
    # want RAM-backed (no SSD writes); the app doesn't care which.
    stream_record_enabled: str = "false"   # global kill-switch; per-user opt-in is User.stream_record
    stream_record_dir: str = "/tmp/posterchanai-streams"   # recording scratch dir (temp; cleaned as it goes)
    # Bitrate clamp (stream_service._write_clamp_script). MediaMTX is a pure remux — whatever OBS sends is
    # what EVERY viewer pulls, so one 1080p60 streamer at 6 Mbps costs 6 Mbps of upload PER VIEWER. The clamp
    # transcodes each live stream down to a fixed ceiling and serves viewers ONLY that, so a streamer's
    # encoder settings can't dictate the instance's bandwidth bill. ON by default; admins can turn it off.
    # Runs on the GPU's media engine (fixed-function, separate from the compute cores) so it does not
    # contend with LLM/image/music/video generation.
    stream_clamp_enabled: str = "true"       # transcode live streams down to the ceiling below
    stream_clamp_height: str = "720"         # max height (width follows the aspect ratio); never upscales
    stream_clamp_fps: str = "30"             # max frame rate; a lower source is passed through untouched
    stream_clamp_bitrate: str = "1500k"      # video bitrate ceiling (this is the per-viewer cost)
    stream_clamp_audio_bitrate: str = "128k"  # audio bitrate (AAC; normalises OBS's AAC and WHIP's Opus)
    stream_clamp_encoder: str = ""           # blank = autodetect (NVENC → VAAPI → libx264); or force one
    stream_rtsp_port: str = "8554"           # MediaMTX RTSP, LOOPBACK-only: how the clamp reads/writes streams
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
    # The bundled CalDAV server (Radicale, mounted at /caldav). OFF by default: it opens a
    # password login surface, so an operator turns it on deliberately.
    caldav_enabled: bool = False
    # Background IMAP poll that pushes new mail to a phone whose screen is off. OFF by default: a
    # background poll across every account is the shape of the load that took the Email client down
    # once, so an operator turns it on deliberately. Floor of 2 minutes, enforced in the service.
    mail_poll_enabled: bool = False
    mail_poll_minutes: int = 10
    searxng_url: str = ""
    # The OFF switch for web search on this node. It exists because clearing searxng_url no longer
    # means "don't search": resolution falls through to a bundled instance and then to a public one,
    # so a blank field would send every query — the user's, the AI's, the bots' — to a third party.
    searxng_enabled: bool = True
    # Send the BUNDLED instance's ENGINE requests through this node's own HTTP proxy
    # (proxy_fallback_port — Tor1 → Tor2 → direct), so a search doesn't leave from this node's real
    # IP. Separate from `searxng_url`'s transport, which is the app→instance hop and already proxied
    # for a REMOTE instance (search_service.search_transport); this is the instance→Google/Bing hop,
    # which SearXNG makes itself and which was going out direct.
    # OFF by default, and that is a MEASUREMENT, not a preference. Routed through Tor the default
    # engines answer "too many requests" / "access denied" / a CAPTCHA, and SearXNG suspends an
    # engine that replies that way for an HOUR — which reaches the user as "no results", never as a
    # proxy problem. Measured on this deployment with the block on: engine round-trips of 6.5-12.0s
    # against the 12.0s ceiling the block itself sets, so 7 of 10 searches hit the wall and came back
    # HTTP 200 with an empty result list; direct, the same queries took 0.5-1.6s and 8 of 8 answered.
    # That is the "usually doesn't work the first time, works on the third" report.
    searxng_proxy_engines: bool = False
    # Spread searches over the nodes in Site → Load Balancing, the way chat/image/music/video are.
    searxng_load_balance: bool = True
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
    # Flash attention (llama_service reads it; seeded in database.py default_settings). It has an
    # Admin → LLM checkbox, but was missing here — so GET /settings dropped it and the box loaded
    # unchecked no matter what was stored, then wrote "false" back on the next Save.
    llm_flash_attn: str = "false"  # enable for Qwen3/3.5 on CUDA builds; OFF on Arc/SYCL
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
    # avatar/@username in the web UI / Telegram; static card for bot-posted effects).
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
    nostr_dvm_peers: str = ""          # shared cluster: peer cards, one per line "npub relay" — MUTUAL (they can use you + you can use them)
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
    # Uptime monitoring (Discover → Server Stats → Uptime). Checks run in the WORKER process
    # (app/worker.py) and the state lives in ONE operator-signed relay doc — see
    # app/services/uptime_service.py. The status page is PUBLIC, like Server Stats itself.
    uptime_enabled: str = "false"
    uptime_monitors: str = ""            # one per line: `Name | https://url | interval_secs | expected text`
    uptime_interval_seconds: str = "60"  # default per-monitor interval (a line's 3rd field overrides it)
    uptime_timeout_seconds: str = "15"
    uptime_retries: str = "2"            # consecutive failures before a monitor is called DOWN (and alerts)
    uptime_alert_telegram: str = "false"  # alert the admin's Telegram on up→down / down→up
    uptime_alert_nostr: str = "false"     # alert by NIP-17 DM from the operator key
    uptime_alert_npubs: str = ""          # npubs to DM (one per line/comma; empty = the admin's own npub)
    # Nostr Stats Bot is now a per-bot FEATURE (Bot.config.stats_enabled), not a global setting —
    # see app/services/stats_bot_service.py + the Bots tab. (No global stats_bot_* keys.)
    # Social notification relay (Pleroma → Telegram)
    social_notif_enabled: str = "true"       # global kill-switch (on by default; per-user toggle in User Settings is the real control)
    social_notif_poll_seconds: str = "60"    # poll interval in seconds
    fedi_bridge_enabled: str = "false"          # master switch for the whole bridge (default off)
    fedi_bridge_instance_url: str = ""          # the shared read account's instance
    fedi_bridge_access_token: str = ""          # the shared read account's token (READ-only use)
    fedi_bridge_type: str = "global"            # which timeline to mirror: global | local | home
    fedi_bridge_poll_seconds: str = "90"
    fedi_bridge_include_replies: str = "true"   # mirror replies (threaded via e/p tags) too
    # Broadcast the mirrored fediverse notes to OTHER Nostr relays (upstream). Default OFF — the
    # mirror stays local to this instance's relay; flip on to federate it to the wider network.
    fedi_bridge_broadcast: str = "false"
    # Admin token on the home instance (= the read instance above) for the 1-click "Bridge Access"
    # feature, which auto-creates a fediverse account for a native Nostr user via the Pleroma admin
    # API. Blank → fall back to the read access token (if that account has admin rights).
    fedi_bridge_admin_token: str = ""
    # Admin domain blocklist enforced AT INGEST: posts whose author host (or origin instance) matches
    # are never mirrored. One host per line/comma; a parent domain covers subdomains (mastodon.social
    # covers a.mastodon.social). Independent of the read account's own block/mute lists (also honored).
    fedi_bridge_blocked_domains: str = ""
    # ---- "Sign in with an account" on the client login page ----
    # Both are OFF by default: they are the only paths where an identity is created by the SERVER
    # rather than in the browser, so a node opts in deliberately.
    # Pleroma/Mastodon: the OAuth app is registered per instance at runtime (POST /api/v1/apps, the
    # same public endpoint the existing account-linking flow uses), so there is no client id/secret to
    # configure here — only which instance the login form offers by default. Blank = the bridge's read
    # instance (fedi_bridge_instance_url); the user can type another one.
    pleroma_login_enabled: str = "false"
    pleroma_login_instance: str = ""
    # Google: one OAuth 2.0 **Web application** client from the Google Cloud console. Its authorised
    # redirect URI must be exactly <public base>/api/auth/google/callback, and the consent screen needs
    # the openid/email/profile scopes. The secret is stored like every other setting — in the
    # operator-signed relay doc, NIP-44 encrypted — never in the repo or a config file.
    google_login_enabled: str = "false"
    google_client_id: str = ""
    google_client_secret: str = ""
    # SSH terminal (the client's Terminal app). A SECOND remote-execution path, and a wider one than
    # node management below: that reaches nodes you registered, over Nostr; this reaches any host you
    # can log into. Off by default, admin + allowlist gated, and `ssh_hosts` is an ALLOWLIST — the
    # client names a host, never an address. No credential is stored: a host either names a private
    # key file already on this server, or the user types a password for that session only.
    ssh_terminal_enabled: str = "false"
    ssh_terminal_users: str = ""   # npubs allowed, comma/newline-separated (admins always allowed)
    ssh_hosts: str = ""            # one per line:  name  user@host[:port]  [key=/path/to/private_key]
    # A SESSION LIVES UNTIL YOU KILL IT — tmux semantics, deliberately. These three bounds are the
    # operator's to re-impose and are OFF (0/blank) by default; a timer's only effect on someone who
    # wants their session back is to take it away, and what actually bounds this is the per-account
    # cap on how many shells one node will run at once.
    ssh_terminal_idle_min: str = ""     # minutes with nothing typed WHILE ATTACHED (0/blank = never)
    ssh_terminal_max_hours: str = ""    # a hard ceiling on a session's age (0/blank = none)
    ssh_terminal_detach_min: str = ""   # how long a detached session is held (0/blank = forever)
    # Run the shell inside tmux/screen ON THE REMOTE HOST when one is installed. That is the only
    # thing that survives a reboot of THIS node; it degrades to a plain login shell when neither is
    # there, which is what the keeper process is for.
    ssh_terminal_multiplex: str = "true"
    # The desktop's mempool widget asks THIS node, which asks upstream once and shares the answer —
    # so a reader's IP never reaches a block explorer. Point it at your own mempool instance if you
    # run one; blank uses the public site.
    mempool_api_base: str = ""
    # Node management (Nostr-only transport: remote nodes are npub workers; `local` runs on this host)
    node_exec_enabled: str = "false"
    node_exec_users: str = ""  # comma/newline-separated npubs allowed (first user/admin always allowed)
    # Per-user Debian Docker sandbox: lets NON-admin AI users (can_ai) run agentic tasks confined to
    # their own throwaway container (admins can opt in too). Needs Docker on the host + the service user
    # in the `docker` group. Off by default.
    node_exec_sandbox_enabled: str = "false"
    node_exec_agent_node: str = ""  # pin ALL sandbox/agentic runs to ONE node (a node name from Agentic Node Management); empty = run on this host. Funnels agentic GPU work through a single worker, serialized by its 1-at-a-time agent lock.
    # posterchanai-sandbox:3 is BUILT from Dockerfile.sandbox (python:3.12-slim + bech32/coincurve/
    # websockets/requests). python:3.12-slim already saved the apt-get-python steps; baking in bech32
    # then removed the recurring nsec-decode failure — the model diagnosed it needed bech32 but would not
    # `pip install` it. :2 adds PYTHONPATH (so the baked libs survive a hand-made venv) + an on-PATH
    # `nostr-post` tool (zero-dep decode+sign+publish+confirm). :3 adds the check suite's toolchain
    # (chromium, node, the app's non-AI deps) so an agent can run ./test.sh in its container without
    # four minutes of apt-get and pip on every run — see docs/TESTING.md. Built by install.sh
    # --sandbox and self-built on first use (sandbox_service).
    node_exec_sandbox_image: str = "posterchanai-sandbox:3"  # per-user container image (built locally)
    node_exec_sandbox_workspace: str = "true"  # persistent /workspace volume per user (survives the
    # throwaway container, so an agent can keep a checkout between runs)
    node_exec_sandbox_network: str = "bridge"  # "bridge" (internet for apt) or "none" (fully isolated)
    node_exec_sandbox_memory: str = "1g"       # per-container memory cap (docker --memory)
    node_exec_sandbox_cpus: str = "1"          # per-container CPU cap (docker --cpus)
    node_exec_sandbox_pids: str = "4096"       # per-container TASK cap (docker --pids-limit). Counts
    # THREADS, not processes: it was hardcoded at 256, and `./test.sh` in a sandbox runs six headless
    # Chromiums at once, which peak at 782 tasks. Every browser check died with `can't start new
    # thread` / `BlockingIOError` and read as a broken test suite.
    agent_artifact_ttl_days: str = "14"  # per-blob TTL for TRANSIENT agent artifacts (workspace backups
    # from a sandbox run): they auto-expire after this many days so a run-every-time auto-archive can't
    # fill storage. 0 = keep forever. Chat-generated images/files are NOT affected (they persist).
    node_exec_agent_max_steps: str = "30"  # max tool-call iterations in agentic mode (was 8 — a context
    # limit in disguise; the agent now digests/trims its own transcript, so a real budget is affordable)
    node_exec_agent_context_chars: str = "48000"  # transcript budget (~4 chars/token) before old tool
    # results are shrunk and the oldest exchanges dropped; sized for the smallest node in the fleet
    node_exec_agent_model: str = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"  # legacy per-feature override for
    # `node agent`; prefer the unified llm_tools_model. Falls back to Model Path when the gguf is absent.
    # Max seconds per command in `node agent` (0 = use job timeout); bounds long/hung commands so the
    # loop can't deadlock. 1800 and not 600: the check suite is the longest command this repo asks an
    # agent to run and it MEASURES 10m22s, so the old default killed it 22 seconds from the finish —
    # and since `./test.sh --brief` prints one block at the very end and nothing before it, what came
    # back was empty. Ten minutes of apparently nothing, then nothing. See docs/TESTING.md.
    node_exec_agent_step_timeout: str = "1800"
    node_exec_job_timeout: str = "0"  # per-job timeout in seconds (0 = no timeout)
    # Nostr transport for node/agent tasks — reuses the DVM (nostr_dvm.py): a command is an encrypted
    # NIP-90 event p-tagged to a worker node's npub; the worker runs it LOCALLY and returns an encrypted
    # result. Replaces SSH — nodes are addressed by npub (no keys/ports; NAT-friendly). Trust is a DEDICATED
    # allowlist (running commands is a bigger grant than GPU-offload, so it is NOT the DVM peer trust).
    node_exec_nostr_enabled: str = "false"  # dispatch node/agent tasks over Nostr instead of SSH
    node_exec_node_npubs: str = ""  # node addressing over Nostr: one per line `name npub1…` (name → worker npub)
    node_exec_trusted_npubs: str = ""  # controllers this WORKER will run commands from: one npub per line (signature-verified)
    # Built-in git-over-nostr host (GRASP + NIP-34). Self-contained smart-HTTP git server run as a
    # watchdogged subprocess (git_host_main.py) on the port-3051 instance; push is authorized by a
    # maintainer-signed kind-30618 (no HTTP passwords). OFF BY DEFAULT — nothing spawns and the API
    # 404s until an admin turns it on (shipping dormant makes a one-shot deploy safe). See docs/GIT_OVER_NOSTR.md.
    git_server_enabled: str = "false"          # master switch; supervisor spawns nothing until on
    git_server_port: str = "3053"              # localhost bind for the git-http subprocess
    git_server_bind: str = "127.0.0.1"         # bind host (container turnkey may set 0.0.0.0)
    git_server_public_base: str = ""           # e.g. https://poster.place/git — stamped into 30617 clone URLs
    git_server_allowlist: str = ""             # npubs allowed to provision (empty ⇒ admins only)
    git_server_repo_max_mb: str = "512"        # per-repo hard size cap (enforced in the pre-receive hook)
    git_server_total_gb: str = "20"            # global storage cap (daily reaper warns/repacks)
    git_server_allow_force: str = "true"       # allow maintainer-signed non-fast-forward (force) pushes
    git_server_nip98_push: str = "true"        # accept NIP-98-authenticated pushes (admin/sync.sh path)
    grasp_mirror_owner: str = ""               # npub/hex that OWNS the mirrored nostr repo (empty = this
    # node's operator key). Set it when the repo is owned by a human npub and the node is only a
    # maintainer: the owner pubkey is the clone-URL path, so scripts/grasp_mirror.py would otherwise
    # keep pushing to the node-owned copy and let the real repo drift.
    git_server_default_private: str = "false"  # default privacy for newly-provisioned/imported repos
    # Multi-node: when SET, this node runs NO local git subprocess — its git front reverse-proxies the
    # smart-HTTP requests to the hosting node here (like the Blossom storage proxy). Auth + repos + hooks
    # + the 30617/30618 lookups stay on the hosting node. Empty ⇒ run the local host (per git_server_enabled).
    git_server_proxy_url: str = ""             # e.g. http://nas.lan:3053 or https://nas.lan/git
    # Screenshot: hosts allowed to bypass the SSRF private-IP guard (the operator's own
    # domains that resolve to a LAN IP via split-horizon DNS). Comma/space/newline-separated;
    # a parent domain also covers its subdomains (e.g. poster.place allows www.poster.place).
    screenshot_allowed_hosts: str = ""
    # Built-in torrent client settings
    bt_enabled: str = "false"
    bt_server_url: str = ""  # Remote torrent server URL (empty = local)
    bt_download_dir: str = "/var/lib/posterchanai/torrents"
    storage_server_url: str = ""  # Remote storage server URL (empty = local)
    media_center_server_url: str = ""  # Per-node NAS proxy origin; empty serves local media
    # Shared secret proving a request really is another NODE, not a stranger who set the
    # X-Posterchanai-Load-Balanced header by hand (which was an auth bypass on every LB endpoint).
    # Set the SAME value on every node. Empty = the legacy header-only trust, kept so an existing
    # multi-node deployment doesn't lose peer calls the moment this ships. See app/utils/lb_auth.py.
    lb_shared_secret: str = ""
    ytdl_cookies_path: str = ""  # Optional Netscape cookies file for yt-dlp (YouTube 403 workaround)
    ytdl_no_ssl_verify: str = "false"  # Skip SSL cert verification for yt-dlp (proxy/firewall hostname mismatch)
    file_cache_enabled: str = "true"  # Enable file listing cache
    file_cache_ttl: str = "300"  # File cache TTL in seconds (default: 5 minutes)
    file_cache_max_size: str = "1000"  # Maximum number of cached directory listings
    bt_proxy_host: str = ""
    bt_proxy_port: str = "8118"
    bt_listen_port: str = "6881"
    # Built-in Tor settings (ON by default — matches database.py default_settings; the UI/GET-settings
    # fallback was "false", so on a fresh install Tor read as disabled even though the seed enabled it).
    tor_enabled: str = "true"
    tor_listen_host: str = "127.0.0.1"
    tor_socks_port: str = "9052"
    tor_control_port: str = "9053"
    tor_exit_nodes: str = "{us}"
    tor_data_dir: str = "/var/lib/posterchanai/tor"
    # Second Tor daemon (different exit region) — the HTTP proxy load-balances across both circuits.
    # Only runs when tor_enabled is also on. Own ports + data dir so the two daemons don't collide.
    tor2_enabled: str = "true"
    tor2_socks_port: str = "9062"
    tor2_control_port: str = "9063"
    tor2_exit_nodes: str = "{ca}"
    tor2_data_dir: str = "/var/lib/posterchanai/tor2"
    # Onion (v3 hidden service) — expose this deployment at a persistent .onion address (primary Tor
    # daemon hosts it; keys persist in the tor data dir → same address across restarts).
    onion_enabled: str = "false"
    # Built-in HTTP proxy settings (ON by default — matches database.py; the "false" fallback made a
    # fresh install read as disabled, so nothing listened on :8118 and every outbound relay connect hit
    # ECONNREFUSED before falling back to direct).
    proxy_enabled: str = "true"
    proxy_listen_host: str = "127.0.0.1"
    proxy_listen_port: str = "8118"
    # Second listener on the same proxy process: Tor1 → Tor2 → DIRECT. Used by this node's own
    # SearXNG (which has no fallback of its own, so a Tor outage would otherwise make every search —
    # AI web lookups, news digests, the bots, Web Search — time out and read as "no results").
    # NEVER point torrent traffic at it: the direct fallback is exactly what proxy_listen_port refuses.
    proxy_fallback_port: str = "8119"
    # SOCKS target the HTTP proxy forwards to (the built-in Tor). Must be non-empty or the proxy
    # subprocess won't start ("no SOCKS5 target host configured"), defeating proxy_enabled.
    proxy_socks_host: str = "127.0.0.1"
    proxy_socks_port: str = "9052"
    # Telegram Bot settings
    telegram_bot_token: str = ""  # Bot token from @BotFather
    telegram_webhook_url: str = ""  # Webhook URL for receiving updates
    telegram_enabled: str = "false"  # Enable Telegram bot
    # Local Bot API server (lifts the cloud API's 20 MB download cap to ~2 GB). These are READ at
    # runtime by telegram_service/_api_base, but were missing from this schema — so GET /settings
    # never returned them and the admin form loaded them BLANK on every visit. For the checkbox that
    # was destructive, not just cosmetic: an unhydrated box posts "false" on the next Save and turns
    # the local server back off. Covered by tests/test_admin_settings_coverage.py.
    telegram_api_base: str = ""      # e.g. http://localhost:8081 (blank = cloud Bot API)
    telegram_api_id: str = ""        # from my.telegram.org
    telegram_api_hash: str = ""      # from my.telegram.org
    telegram_local_api: str = "false"  # use the local Bot API server
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
    # separate OpenAI endpoint.
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
    nostr_relay_backup_datastore: Optional[bool] = True  # broadcast the encrypted pcai: config docs (settings/accounts/bots) to upstream relays for disaster recovery (ON by default — Nostr-based DR)
    nostr_relay_firehose_enabled: Optional[bool] = None  # live firehose sync (real-time)
    nostr_relay_posterchan_clients_only: Optional[bool] = False
    nostr_relay_posterchan_origins: Optional[str] = ""
    nostr_relay_bind: Optional[str] = None
    nostr_relay_port: Optional[int] = None
    nostr_relay_wot_seeds: Optional[str] = None          # npub/hex seeds, newline/comma
    nostr_relay_upstream_relays: Optional[str] = None     # blank = bots' DEFAULT_RELAYS
    # Mirror the PRIVATE encrypted libraries (notes/passwords/budget/files index) to relays you run.
    # Blank = off. Deliberately NOT the public upstreams: the bodies are ciphertext, but each event
    # is a permanent per-user metadata trail (pubkey + stable d-tag + timestamp) wherever it lands.
    nostr_relay_private_relays: Optional[str] = None
    nostr_relay_retention_days: Optional[int] = None  # auto-clean feed notes older than N days (0=off)
    nostr_relay_max_events: Optional[int] = None      # hard count cap on feed events (0=unlimited)
    # Pay-to-stay: an OPTIONAL paid retention tier for authors with no account here. All five are
    # inert until `_enabled` is on AND `_free_retention_days` is a non-zero number — see
    # app/services/paid_retention_service.py.
    nostr_relay_paid_retention_enabled: Optional[bool] = False
    nostr_relay_free_retention_days: Optional[int] = 0    # non-subscribers' own posts (0 = forever)
    nostr_relay_paid_retention_days: Optional[int] = 0    # subscribers' own posts (0 = forever)
    nostr_relay_paid_sats_per_month: Optional[int] = 0    # price; 0 = nothing can be bought
    nostr_relay_paid_lud16: Optional[str] = None          # lightning address zaps must be paid to
    nostr_relay_paid_pubkey: Optional[str] = None         # zap target npub (blank = relay/operator key)
    nostr_relay_wot_enabled: Optional[bool] = True
    nostr_relay_send_only: Optional[bool] = False
    nostr_relay_wot_refresh_sec: Optional[int] = None
    nostr_relay_wot_depth: Optional[int] = None
    nostr_relay_wot_min_followers: Optional[int] = None
    nostr_relay_wot_max: Optional[int] = None
    nostr_relay_wot_depth3_crawl_max: Optional[int] = None
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
    # Both are READ at runtime (nostr_relay/thread.py builds the Outbox from them) and were never
    # declared here — so GET dropped them from the typed response and a PUT could not set them, which
    # made the two knobs that decide how long a refusing relay is chased unreachable from Admin. They
    # matter: 2 retries x 15s against a relay that answers `pow:`/`blocked:` is pure waste, and it is
    # what pinned the retry pool at its cap. Declared, not yet given inputs — see docs/RELAY.md.
    nostr_relay_outbox_retries: Optional[int] = None
    nostr_relay_outbox_retry_delay_sec: Optional[float] = None
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
    blossom_user_quota_gb: Optional[int] = None   # per-user storage cap in GB (0/blank = unlimited).
    # Matters because blossom_blob_ttl_days=0 (keep forever) means nothing bounds growth by age.
    blossom_storage_backend: Optional[str] = None
    blossom_storage_path: Optional[str] = None
    blossom_cache_mb: Optional[int] = None
    blossom_whitelist: Optional[str] = None
    blossom_mirror_servers: Optional[str] = None   # DR: external Blossom servers to mirror blobs to
    # Hostnames this deployment serves ITSELF that resolve to a private IP from inside the LAN
    # (split-horizon DNS). Exempted from the SSRF guard when the server fetches media. One per line.
    media_own_hosts: Optional[str] = None
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
# Client UI themes the user can pick (Cyberpunk is the flagship default). Slugs match the
# `:root[data-theme="…"]` blocks in static/css/client.css and the <select> in the client Settings.
CLIENT_THEMES = ("cyberpunk", "cherryblossom", "professional", "win98", "winxp", "animegirl", "sovietgothic", "dark", "monero")


class UserSettingsUpdate(BaseModel):
    notification_email: Optional[str] = None
    theme: Optional[str] = None  # one of CLIENT_THEMES; ignored if unknown
    news_sources: Optional[str] = None  # Custom sources for the `news` command, one per line: url|name
    # Mail settings
    mail_accounts: Optional[List[dict]] = None  # List of {email, imap_server, imap_port, smtp_server, smtp_port, password}
    # Telegram settings — linking/unlinking managed via /api/telegram/*, not here
    telegram_notifications: Optional[str] = None
    # Pleroma settings (read-only via /api/pleroma/connect; exposed here for display)
    pleroma_enabled: Optional[bool] = None
    pleroma_instance_url: Optional[str] = None
    # Nostr settings (key linked via /api/nostr/connect; relays/media editable here)
    nostr_enabled: Optional[bool] = None
    nostr_relays: Optional[str] = None
    nostr_media_service: Optional[str] = None
    nostr_media_endpoint: Optional[str] = None
    # Relay social notifications (Pleroma) to Telegram
    social_notif_enabled: Optional[bool] = None
    # Nostr ↔ Fediverse bridge: opt in to personal fedi DMs + notifications on the Nostr side
    fedi_bridge_enabled: Optional[bool] = None
    # Cross-post my top-level Nostr notes to my linked Pleroma account
    fedi_crosspost_enabled: Optional[bool] = None
    fedi_only: Optional[bool] = None
    # NOTE: the global relay/Blossom/GIF settings that used to live here were MOVED to
    # SettingsResponse (they're admin-global, not per-user) — see that class.


class BridgeAccessRequest(BaseModel):
    enable: bool = True   # True = 1-click provision + enable; False = disable bridge access


class UserSettingsResponse(BaseModel):
    notification_email: Optional[str] = None
    avatar: Optional[str] = None
    theme: str = "cyberpunk"
    news_sources: str = ""  # Custom sources for the `news` command, one per line: url|name
    # Mail settings
    mail_accounts: List[dict] = []  # List of mail accounts (passwords masked)
    # Telegram settings
    telegram_enabled: bool = False
    telegram_chat_id: Optional[str] = None
    telegram_notifications: str = ""
    telegram_pending_key: Optional[str] = None       # Pending link key (exposed to owner only)
    telegram_key_expires_at: Optional[datetime] = None
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
    # Relay social notifications (Pleroma) to Telegram
    social_notif_enabled: bool = False
    # Nostr ↔ Fediverse bridge: personal fedi DMs + notifications mirrored to the Nostr side
    fedi_bridge_enabled: bool = False
    # Cross-post my top-level Nostr notes to my linked Pleroma account
    fedi_crosspost_enabled: bool = False
    fedi_only: bool = False

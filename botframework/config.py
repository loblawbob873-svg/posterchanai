import os

# These globals used to live in bots_config.py. After merging this framework into
# posterchanai they are injected as env vars by bot_manager_service (from the global
# "bots_*" admin settings). Precedence: env (manager-injected) → legacy bots_config.py
# if one is still present during migration → safe default. This removes the hard
# dependency on bots_config.py so the listeners run purely from manager-supplied env.
try:
    import bots_config as _bc  # legacy/optional
except Exception:
    _bc = None


def _g(env_key, attr, default=""):
    val = os.getenv(env_key)
    if val is not None and val != "":
        return val
    if _bc is not None and hasattr(_bc, attr):
        return getattr(_bc, attr)
    return default


def _gb(env_key, attr, default=False):
    val = os.getenv(env_key)
    if val is not None and val != "":
        return val.strip().lower() in ("true", "1", "yes")
    if _bc is not None and hasattr(_bc, attr):
        return bool(getattr(_bc, attr))
    return default


MODEL = _g("AI_MODEL", "AI_MODEL", "")
USE_POSTERCHANAI = _gb("USE_POSTERCHANAI", "USE_POSTERCHANAI", True)
POSTERCHANAI_API_ENDPOINT = _g("POSTERCHANAI_API_ENDPOINT", "POSTERCHANAI_API_ENDPOINT", "")
POSTERCHANAI_USERNAME = _g("POSTERCHANAI_USERNAME", "POSTERCHANAI_USERNAME", "")
POSTERCHANAI_PASSWORD = _g("POSTERCHANAI_PASSWORD", "POSTERCHANAI_PASSWORD", "")

# Server timezone
TIMEZONE = os.getenv("TIMEZONE", "MST")

FEDI_TIMELINE_ROOM_ID = os.getenv("FEDI_TIMELINE_ROOM_ID", "!MaaoDPUoNpHtiDMJAQ:chat.poster.place")
# SSL verification (set "false" for self-signed certs)


# Nostr Configuration. Identity is a secret key (nsec/hex); posts are signed events
# published to multiple relays; media goes to an external Blossom/NIP-96 host (NOT the
# "instance"). NOSTR_RELAYS is a comma/newline list; blank → app default relays.
NOSTR_NSEC = os.getenv("NOSTR_NSEC")
NOSTR_RELAYS = os.getenv("NOSTR_RELAYS", "")
NOSTR_MEDIA_SERVICE = os.getenv("NOSTR_MEDIA_SERVICE", "blossom")   # "blossom" | "nip96"
NOSTR_MEDIA_ENDPOINT = os.getenv("NOSTR_MEDIA_ENDPOINT", "")        # blank → service default

# Pleroma Configuration
PLEROMA_ENDPOINT = os.getenv("PLEROMA_ENDPOINT")
PLEROMA_USERNAME = os.getenv("PLEROMA_USERNAME")
PLEROMA_ACCESS_TOKEN = os.getenv("PLEROMA_ACCESS_TOKEN")
PLEROMA_ADMIN_TOKEN = os.getenv("PLEROMA_ADMIN_TOKEN")

# Extra hostnames trusted for media downloads in addition to the bot's own instance.
# A self-hosted instance on the LAN resolves to a private IP, which the SSRF guard
# (is_safe_url) blocks by default; list such instances here to allow their media
# (e.g. compress/clip/convert on files hosted there). Comma-separated.
TRUSTED_MEDIA_HOSTS = [h.strip().lower() for h in os.getenv("TRUSTED_MEDIA_HOSTS", "").split(",") if h.strip()]

# Blockbot Configuration
BLOCK_LIMIT = 1
SQL_USER = os.getenv("SQL_USER")
SQL_PASS = os.getenv("SQL_PASS")
SQL_HOST = os.getenv("SQL_HOST")
SQL_DATABASE = os.getenv("SQL_DATABASE")
BLOCK_IMAGE = os.getenv("BLOCK_IMAGE", "/home/verita84/posterchan/bot.png")
BLOCK_PROMPT = os.getenv("BLOCK_PROMPT", "Generate a post about this block event. Be dramatic and entertaining. Use a stern male tone. Include the blocker's profile link. CRITICAL: You MUST preserve the exact usernames and domains exactly as provided. Do NOT change any usernames or domains. Do NOT mix languages - respond ONLY in English. Use the format 'BLOCKER: @user@domain blocked @user2@domain2' (the first user performed the block, the second was blocked). Do NOT include the word BLOCKEE in the post. Do not reverse blocker and blockee. Use the exact same usernames and domains from the block details. Respond only with the post in English. Block details: {block_details}")

# Unfollowbot Configuration
UNFOLLOW_IMAGE = os.getenv("UNFOLLOW_IMAGE", "/home/verita84/posterchan/bot.png")
UNFOLLOW_SILENT_MODE = os.getenv("UNFOLLOW_SILENT_MODE", "").lower() in ("true", "1", "yes")

# AI Configuration
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ENDPOINT = os.getenv("OPENAI_ENDPOINT")
PROMPT = os.getenv("PROMPT", "")
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.7"))
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
RESPOND_TO_ALL = os.getenv("RESPOND_TO_ALL", "false").lower() == "true"

# Image Poster (--image mode)
IMAGE_POSTER_FREQ = 3600
IMAGE_POSTER_PROMPT = os.getenv("IMAGE_POSTER_PROMPT")
IMAGE_POSTER_TEXT = os.getenv("IMAGE_POSTER_TEXT", "") or ""  # empty (no caption) when unset
IMAGE_POSTER_RANDOM_SCENES = os.getenv("IMAGE_POSTER_RANDOM_SCENES", "").lower() in ("true", "1", "yes")

# Auto Poster (--autopost mode): scheduled in-character standalone posts. Scheduling
# (interval/cap/quiet-hours) lives in bot_manager_service; these drive content/targets only.
AUTO_POST_SEED = os.getenv("AUTO_POST_SEED", "")
AUTO_POST_TOPICS = os.getenv("AUTO_POST_TOPICS", "")
AUTO_POST_ROOMS = os.getenv("AUTO_POST_ROOMS", "")

# SearXNG Configuration
SEARXNG_URL = os.getenv("SEARXNG_URL", "https://search.poster.place")

# Image Generation (backend settings imported from bots_config above; key is per-bot)
POSTERCHANAI_API_KEY = os.getenv("POSTERCHANAI_API_KEY", "")
BASIC_IMAGE_MODEL = "cyberrealisticXL_v100.safetensors"
ANIME_IMAGE_MODEL = "nova3DCGXL_ilV80.safetensors"

# Prohibited words that block image generation (filter: core.utils.contains_bad_words)
BAD_WORDS = [
    "child",
    "preteen",
    "cunny",
    "csam",
    "childporn",
    "teen",
    "child sex",
    "loli",
    "little girl",
    "little boy",
    "baby",
    "toddler",
    "shota",
    "candydoll",
    "幼女",
]

# Bots to ignore to prevent bot-to-bot loops (local @name or remote name@domain)
BOT_BLACKLIST = [
    "posterchan_64",
    "yenta",
    "judgedread",
    "news",
    "tonesha",
    "jeet",
    "candy",
    "blockbot",
    "anime",
]

# Hex pubkeys of our OWN nostr bots (injected by the bot manager as BOT_NOSTR_PUBKEYS). A nostr
# listener skips notes from any of these so our bots never reply to each other — robust by pubkey,
# unlike the handle-based BOT_BLACKLIST above (a nostr sender is a pubkey, not a name).
BOT_NOSTR_PUBKEYS = {p.strip().lower() for p in os.getenv("BOT_NOSTR_PUBKEYS", "").split(",") if p.strip()}

# Phrase that must never be sent as part of a reply/post
BLOCK_PHRASE = "I am programmed to be a helpful and harmless AI assistant"

# Welcome Bot Configuration
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE", "/home/verita84/posterchan/welcome.gif")
WELCOME_MESSAGE = os.getenv("WELCOME_MESSAGE", "Welcome to {instance_name}")
WELCOME_LOOKBACK_MINUTES = int(os.getenv("WELCOME_LOOKBACK_MINUTES", "5"))
WELCOME_PROMPT = os.getenv("WELCOME_PROMPT", "Welcome the new user @{username} to {instance_name}. Use a stern male tone. Tell the user something like you are now truly free but don't act like a fucking nigger. Use hashtags. Don't add any characters to the username. Respond only with the post.")

# Report Bot Configuration
REPORT_IMAGE = os.getenv("REPORT_IMAGE", "/home/verita84/posterchan/bot.png")
REPORT_PROMPT = os.getenv("REPORT_PROMPT", "Summarize this user report in a dramatic and entertaining way. Start and end the post with 🚨 emojis. Include the reporter, the reported user, and the reason. Use hashtags. Do not introduce yourself or announce your name. Respond only with the post. Report details: {report_details}")

# TTS Configuration (for /narrate command and auto_narrate)
TTS_VOICE = os.getenv("TTS_VOICE", "en-GB-SoniaNeural")
TTS_RATE = os.getenv("TTS_RATE", "+10%")
TTS_PITCH = os.getenv("TTS_PITCH", "+5Hz")
AUTO_NARRATE = os.getenv("AUTO_NARRATE", "").lower() in ("true", "1", "yes")

# Video encoder for TTS video (auto, libx264, h264_nvenc, h264_amf, h264_vaapi)
VIDEO_ENCODER = os.getenv("VIDEO_ENCODER", "auto")

# Response delay in seconds before responding
RESPONSE_DELAY = int(os.getenv("RESPONSE_DELAY", "0"))


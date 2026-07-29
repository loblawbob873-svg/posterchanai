"""Bot manager — the in-app replacement for ~/posterchan's botctl.py.

Reads `Bot` rows from the DB (Admin → Bots), builds per-bot environment from the global
`bots_*` settings + the bot's JSON `config`, and spawns `botframework/main.py <modes>` as a
child process. A background reconcile loop keeps enabled bots running (restarting crashes,
rate-limited) and stops bots that have been disabled/deleted; image bots run on a daily
schedule instead.

Design mirrors botctl.build_env / run_all_bots, but the source of truth is the DB rather than
bots_config.py, and the loop *reconciles against the DB* every few seconds so the UI's On/Off
toggle (which just flips Bot.enabled) takes effect without a restart.

Single-worker assumption: the process registry is in-memory, correct only on the one port-3051
instance (same caveat as the node-job registry and the social poller).
"""

import os
import sys
import json
import time
import random
import hashlib
import socket
import logging
import threading
import subprocess
from pathlib import Path

from app.database import SessionLocal
from app.models import Bot
from app.services import settings_store

logger = logging.getLogger(__name__)

# botframework/ lives at the repo root, two levels up from app/services/.
BOTFRAMEWORK_DIR = Path(__file__).resolve().parents[2] / "botframework"
MAIN_PY = BOTFRAMEWORK_DIR / "main.py"

# Restart rate limiting (per botctl)
MAX_RESTARTS_PER_HOUR = 10
RESTART_COUNT_RESET_TIME = 3600

# Image bots run daily at these hours, staggered to avoid flooding the image queue.
IMAGE_SCHEDULE_HOURS = [0, 6, 12, 18]
IMAGE_STAGGER_DELAY = 300  # seconds between staggered image bots
RECONCILE_INTERVAL = 5     # seconds between reconcile passes

# Text auto-post (--autopost) default cadence when a bot enables it without setting an
# interval. Conservative on purpose; per-bot config overrides both ends.
AUTOPOST_DEFAULT_MIN_MIN = 180   # minutes
AUTOPOST_DEFAULT_MAX_MIN = 360   # minutes

# Unified codebase: a SINGLE PosterChanAI server URL (bots_server_url) drives everything —
# the bots reach the shared LLM and image generation through it over HTTP (they're separate
# processes, so they can't share the GPU-loaded model in-process). No separate OPENAI endpoint.
# These straightforward settings map 1:1; the server-derived ones
# (OPENAI_ENDPOINT / POSTERCHANAI_API_ENDPOINT / USE_POSTERCHANAI) are set in _load_global_env.
_GLOBAL_ENV_MAP = {
    "bots_ai_api_key": "OPENAI_API_KEY",
    "bots_ai_model": "AI_MODEL",
    "searxng_url": "SEARXNG_URL",   # reuse the app's own SearXNG setting (no separate bot copy)
    "bots_timezone": "TIMEZONE",
    # Image API auth: an app API key (preferred) OR the app login. posterchanai_api.py uses the
    # API key if set, else logs in with username/password.
    "bots_posterchanai_username": "POSTERCHANAI_USERNAME",
    "bots_posterchanai_password": "POSTERCHANAI_PASSWORD",
    "bots_posterchanai_api_key": "POSTERCHANAI_API_KEY",
}

# The one endpoint setting. (Old installs used bots_posterchanai_api_endpoint; we read that as a
# fallback so the merge keeps working without a manual re-entry.)
_SERVER_URL_KEYS = ("bots_server_url", "bots_posterchanai_api_endpoint")

# ---- module state (guarded by _lock) -----------------------------------------
_lock = threading.RLock()
_procs = {}            # bot name -> subprocess.Popen (text bots; running children)
_proc_sig = {}         # bot name -> spawn-spec signature (env+cmd); change ⇒ restart to apply edits
_last_env = {}         # bot name -> the env it was last spawned with, so a restart can name what moved
_restart_counts = {}   # bot name -> {"count": int, "first_restart": float}
# Scheduled one-shot posters (image bots + text bots with auto_post_enabled). Keyed by bot
# name -> {"next_run": float, "process": Popen|None, "offset": int, "day": int, "count": int}.
# Plus the sentinel "_last_start": float used for inter-spawn staggering.
_post_sched = {}
# Manual one-shot test posts fired from the admin UI (publish-now). Fire-and-forget; reaped
# lazily so finished children don't linger as zombies.
_oneshot_procs = []
_monitor_thread = None
_stop_event = threading.Event()


# ---- helpers -----------------------------------------------------------------

def get_hostname():
    return socket.gethostname().split(".")[0]


def _instance_domain() -> str:
    """Host clients should NIP-05-verify a bare bot nip05 against — derived from the `site_url`
    setting (else the public Blossom host's apex). e.g. lets 'TheQuartering' resolve to poster.place."""
    for key in ("site_url", "blossom_public_url"):
        s = (settings_store.get(key, "") or "").strip()
        if not s:
            continue
        host = s.split("://", 1)[-1].split("/", 1)[0].strip().rstrip(".")
        if host.startswith("media."):
            host = host[len("media."):]
        if host:
            return host
    return ""


def _nip05_full(v) -> str:
    """A bare nip05 local-part becomes '<name>@<instance domain>' so bot settings only need the name,
    not the domain ('TheQuartering' → 'TheQuartering@poster.place'). Full nip05s pass through."""
    v = str(v).strip()
    if not v or "@" in v:
        return v
    dom = _instance_domain()
    return f"{v}@{dom}" if dom else v


def _load_global_env():
    """Base env shared by every bot, derived from the global bots_* settings.

    Everything routes through the single PosterChanAI server URL: the bot's LLM calls hit
    {server}/api/chat/completions and its image generation uses the server's image API
    (USE_POSTERCHANAI is forced on — native diffusers)."""
    env = os.environ.copy()
    wanted = set(_GLOBAL_ENV_MAP) | set(_SERVER_URL_KEYS)
    rows = {k: settings_store.get(k) for k in wanted if settings_store.exists(k)}
    for key, env_key in _GLOBAL_ENV_MAP.items():
        val = rows.get(key)
        if val is not None and val != "":
            env[env_key] = val
    # Derive every endpoint from the one server URL.
    server = ""
    for k in _SERVER_URL_KEYS:
        if rows.get(k):
            server = rows[k].strip().rstrip("/")
            break
    if not server:
        # Unified architecture: the bot's only LLM/image endpoint is THIS PosterChanAI server.
        # When the operator never set bots_server_url, fall back to the local listener so bots
        # work out-of-the-box. Without this OPENAI_ENDPOINT is unset → every fresh bot one-shot
        # (autopost, image-post) fails "OpenAI not configured" → generates empty → silently never
        # posts (the long-running listeners only kept working off a stale env from an earlier spawn).
        server = f"http://127.0.0.1:{os.getenv('POSTERCHANAI_PORT', '3051')}"
    if server:
        # `localhost` can resolve to ::1 (IPv6) where the app binds IPv4-only (0.0.0.0:3051), so the
        # bot's Python `requests` STALLS ~20s per call on the dead IPv6 address before falling back to
        # IPv4 — which made every bot LLM call crawl and the 120s autopost cap time out. Pin loopback
        # to 127.0.0.1 so it connects straight to the IPv4 listener.
        server = server.replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")
        if server.endswith("://localhost"):
            server = server[:-len("://localhost")] + "://127.0.0.1"
        env["POSTERCHANAI_API_ENDPOINT"] = server
        env["OPENAI_ENDPOINT"] = server + "/api/chat/completions"
    env["USE_POSTERCHANAI"] = "true"   # always use the unified server (native diffusers)
    # SQL_* base creds (per-bot SQL_DATABASE is added in _build_env when sql_database is set)
    env["_SQL_USER"] = settings_store.get("bots_sql_user", "")
    env["_SQL_PASS"] = settings_store.get("bots_sql_pass", "")
    env["_SQL_HOST"] = settings_store.get("bots_sql_host", "")
    return env


def bot_to_dict(bot: Bot) -> dict:
    """Merge a Bot row's columns + JSON config into one flat dict (old bots_config shape)."""
    try:
        cfg = json.loads(bot.config) if bot.config else {}
    except (ValueError, TypeError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    merged = dict(cfg)
    merged["name"] = bot.name
    merged["platform"] = bot.platform
    merged["host"] = bot.host or ""
    merged["bot_type"] = bot.bot_type
    if bot.modes:
        merged["modes"] = [m.strip() for m in bot.modes.split(",") if m.strip()]
    return merged


def _set_internal_blossom(env: dict):
    """Nostr bots always upload media to THIS node's built-in Blossom server (no external host to
    configure — keeps setup simple and keeps the bytes on our infra). The bot's npub must be on the
    Blossom whitelist (the 'New identity' provisioner grants this automatically)."""
    api = (env.get("POSTERCHANAI_API_ENDPOINT") or "").rstrip("/")
    if not api:
        api = f"http://127.0.0.1:{os.getenv('POSTERCHANAI_PORT', '3051')}"
    env["NOSTR_MEDIA_SERVICE"] = "blossom"
    env["NOSTR_MEDIA_ENDPOINT"] = api + "/blossom"
    # Route ONLY through THIS node's built-in WoT relay: it stores the bot's app-data (kind-30078
    # game state the web client reads) AND its outbox re-broadcasts the bot's PUBLIC posts (boards,
    # replies) upstream. So external relays in the bot's list are redundant and just cause it to
    # connect/push to the default relays directly (noise + WoT rejections) — force local-only.
    rport = (settings_store.get("nostr_relay_port", "3052") or "3052").strip()
    env["NOSTR_RELAYS"] = f"ws://127.0.0.1:{rport}"
    # Public site URL for the chess bot's "play interactively" footer — admin `site_url`, else
    # derive from the public Blossom host (drop a leading "media." label → the apex site).
    site = (settings_store.get("site_url", "") or "").strip().rstrip("/")
    if not site:
        pub = (settings_store.get("blossom_public_url", "") or "").strip().rstrip("/")
        if pub:
            import re as _re
            site = _re.sub(r"//media\.", "//", pub)
    if site:
        env["CHESS_SITE_URL"] = site
    # Blossom server list for this bot to advertise as its own kind-10063 (BUD-03) on startup, so
    # clients fail over by hash for the bot's media too: our public Blossom URL + the DR mirrors.
    servers, seen = [], set()
    pub = (settings_store.get("blossom_public_url", "") or "").strip().rstrip("/")
    for u in ([pub] if pub else []) + (settings_store.get("blossom_mirror_servers", "") or "").split():
        u = (u or "").strip().rstrip("/")
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            servers.append(u)
    if servers:
        env["BLOSSOM_SERVERS"] = " ".join(servers)


_BOT_PK_DERIV: dict = {}                       # nsec -> pubkey hex; the EC derivation is expensive
_ALL_PK_CACHE: dict = {"val": "", "ts": 0.0}   # comma-joined result, cached briefly
_ALL_PK_TTL = 60.0


def _all_nostr_bot_pubkeys() -> str:
    """Hex pubkeys of EVERY nostr-capable bot (derived from each bot's nsec), comma-separated. Injected
    into each nostr bot's env (BOT_NOSTR_PUBKEYS) so its listener can skip notes from ANOTHER of our
    bots — a robust, pubkey-based anti-loop the handle-based BOT_BLACKLIST can't do. Best-effort: any
    failure yields an empty string (the listener then just falls back to the handle blacklist).

    MEMOIZED: the reconcile loop (every 5s) calls this once PER BOT, and the pubkey derivation is
    pure-Python elliptic-curve math (secp256k1 point-mul) — re-deriving every bot's key on every call
    pegged a core. Now each nsec is derived once (cached forever) and the whole result is cached for
    `_ALL_PK_TTL`s, so a reconcile pass costs a dict lookup, not O(bots^2) EC multiplications."""
    now = time.time()
    if _ALL_PK_CACHE["val"] and (now - _ALL_PK_CACHE["ts"]) < _ALL_PK_TTL:
        return _ALL_PK_CACHE["val"]
    out = set()
    try:
        from app.services.nostr import nostr_service
        db = SessionLocal()
        try:
            for b in db.query(Bot).all():
                try:
                    nsec = (json.loads(b.config or "{}") or {}).get("nostr_nsec")
                except Exception:
                    nsec = None
                if not nsec:
                    continue
                pk = _BOT_PK_DERIV.get(nsec)
                if pk is None:
                    try:
                        d = nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec))
                        pk = d if isinstance(d, str) else d.hex()
                        _BOT_PK_DERIV[nsec] = pk   # derive each key's pubkey ONCE, ever
                    except Exception:
                        continue
                out.add(pk)
        finally:
            db.close()
    except Exception:
        pass
    res = ",".join(sorted(out))
    if res:
        _ALL_PK_CACHE["val"], _ALL_PK_CACHE["ts"] = res, now
    return res


def _build_env(bot_dict: dict, base_env: dict) -> dict:
    """Port of botctl.build_env, reading from a merged bot dict instead of bots_config."""
    env = dict(base_env)
    sql_user, sql_pass, sql_host = env.pop("_SQL_USER", ""), env.pop("_SQL_PASS", ""), env.pop("_SQL_HOST", "")
    is_image = bot_dict.get("bot_type") == "image"

    # Put the repo root on PYTHONPATH so a bot can `import app.services.*` (the incremental
    # dedup shims reuse the app's services from inside the spawned subprocess). botframework/
    # stays sys.path[0] for the child, so its own bare imports (config, news, …) still win.
    repo_root = str(BOTFRAMEWORK_DIR.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = repo_root + (os.pathsep + existing_pp if existing_pp else "")

    # Route ALL of a bot's outbound internet traffic (fediverse/nostr relays, Nitter RSS +
    # media, web search, news, blossom uploads, image downloads) through the built-in HTTP
    # proxy (→ SOCKS5/Tor). requests, httpx and websockets all honour these standard env
    # vars, so this single injection covers every bot library without per-call wiring.
    # localhost is excluded so the bot's calls to the local app (chat/image/media at
    # bots_server_url=http://localhost:3051) stay direct and never go through Tor.
    try:
        from app.services.proxy_utils import get_proxy_config
        _proxy = get_proxy_config()
    except Exception:
        _proxy = None
    if _proxy:
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            env[k] = _proxy
        # Bypass Tor for the bot's INTERNAL backends. The local app, SearXNG, news, etc. live on
        # localhost or a private LAN IP (e.g. a SearXNG at 192.168.x.x) — Tor can't route RFC1918
        # and returns 502, so those calls MUST stay direct. localhost is always excluded; we also
        # add the hostnames of the configured internal endpoints so a LAN SearXNG/app server is
        # reached directly. (requests/httpx NO_PROXY matches by host, not CIDR — hence explicit
        # hosts rather than a subnet.)
        from urllib.parse import urlparse
        no_proxy = ["localhost", "127.0.0.1", "::1"]
        for url_key in ("SEARXNG_URL", "POSTERCHANAI_API_ENDPOINT", "OPENAI_ENDPOINT"):
            u = env.get(url_key)
            if not u:
                continue
            host = urlparse(u if "://" in u else "http://" + u).hostname
            if host and host not in no_proxy:
                no_proxy.append(host)
        env["NO_PROXY"] = env["no_proxy"] = ",".join(no_proxy)

    def setif(key, env_key, transform=str):
        v = bot_dict.get(key)
        if v not in (None, "", [], {}):
            env[env_key] = transform(v)

    if is_image:
        # Image bots post to their own platform — set creds for whichever it is (the
        # one-shot imageposter picks pleroma/nostr from these env vars).
        if bot_dict.get("platform") == "pleroma":
            setif("server", "PLEROMA_ENDPOINT")
            setif("username", "PLEROMA_USERNAME", lambda v: str(v).lstrip("@"))
            setif("access_token", "PLEROMA_ACCESS_TOKEN")
        elif bot_dict.get("platform") == "nostr":
            setif("nostr_nsec", "NOSTR_NSEC")
            setif("nostr_profile_name", "NOSTR_PROFILE_NAME")
            setif("nostr_profile_nip05", "NOSTR_PROFILE_NIP05", _nip05_full)
            setif("nostr_profile_picture", "NOSTR_PROFILE_PICTURE")
            _set_internal_blossom(env)
        setif("prompt", "IMAGE_POSTER_PROMPT")
        setif("text", "IMAGE_POSTER_TEXT")
        setif("image_negative", "IMAGE_POSTER_NEGATIVE")  # optional negative prompt
        if bot_dict.get("random_scenes"):
            env["IMAGE_POSTER_RANDOM_SCENES"] = "true"
    else:
        platform = bot_dict.get("platform", "pleroma")
        if platform == "pleroma":
            setif("server", "PLEROMA_ENDPOINT")
            setif("username", "PLEROMA_USERNAME", lambda v: str(v).lstrip("@"))
            setif("access_token", "PLEROMA_ACCESS_TOKEN")
            setif("pleroma_admin_token", "PLEROMA_ADMIN_TOKEN")
        elif platform == "nostr":
            # Nostr identity is a secret key (nsec/hex). Relays are NOT per-bot: every Nostr bot
            # publishes to the LOCAL relay ONLY (NOSTR_RELAYS was set to ws://127.0.0.1:3052 above),
            # whose outbox federates the posts upstream — so there's no per-bot relay override here.
            # The external media host comes from config (_set_internal_blossom).
            setif("nostr_nsec", "NOSTR_NSEC")
            # Anti-loop: the hex pubkeys of ALL our nostr bots, so this listener never replies to
            # another of our bots. Robust by pubkey (the handle-based BOT_BLACKLIST can't, since a
            # nostr sender is a pubkey, not a name).
            _peers = _all_nostr_bot_pubkeys()
            if _peers:
                env["BOT_NOSTR_PUBKEYS"] = _peers
            setif("nostr_profile_name", "NOSTR_PROFILE_NAME")
            setif("nostr_profile_nip05", "NOSTR_PROFILE_NIP05", _nip05_full)
            setif("nostr_profile_picture", "NOSTR_PROFILE_PICTURE")
            _set_internal_blossom(env)
            # Mention-poll cadence. With our self-hosted relay (point relays at ws://127.0.0.1:3052
            # — no CF round-trip, no public rate limit) the bot can poll fast for snappy replies.
            setif("nostr_poll_seconds", "NOSTR_POLL_SECONDS")
            # Abuse rate limits (per-pubkey + global per window) — Nostr is permissionless.
            setif("nostr_rate_per_user", "NOSTR_RATE_PER_USER")
            setif("nostr_rate_global", "NOSTR_RATE_GLOBAL")
            setif("nostr_rate_window", "NOSTR_RATE_WINDOW")
            setif("nostr_rate_exempt", "NOSTR_RATE_EXEMPT")
            # Random-reply: occasionally start a thread with a NIP-05-verified stranger on the
            # firehose (off by default; obeys quiet hours; ≤N new threads/hour; ≤2 replies/thread).
            setif("nostr_random_reply", "NOSTR_RANDOM_REPLY")
            setif("nostr_random_reply_quiet", "NOSTR_RANDOM_REPLY_QUIET")
            setif("nostr_random_reply_per_hour", "NOSTR_RANDOM_REPLY_PER_HOUR")
            # Reply listener is opt-in: Admin → Bots "reply" maps to `--nostr` in `modes` (admin-bots.js).
            # When it's UNCHECKED, modes is empty and _cmd_for still defaults to `--nostr` so a stats /
            # identity bot goes live + publishes kind-0 — but it must NOT reply to mentions. Flag that
            # defaulted process as presence-only. (Explicit `--nostr` in modes ⇒ user wants replies.)
            if not bot_dict.get("modes"):
                env["NOSTR_PRESENCE_ONLY"] = "1"
        # Nitter RSS feeds
        if bot_dict.get("nitter_feeds"):
            env["NITTER_FEEDS"] = json.dumps(bot_dict["nitter_feeds"])
        setif("nitter_poll_seconds", "NITTER_POLL_SECONDS")

        tmh = bot_dict.get("trusted_media_hosts")
        if tmh:
            env["TRUSTED_MEDIA_HOSTS"] = ",".join(tmh) if isinstance(tmh, (list, tuple)) else str(tmh)

        setif("prompt", "PROMPT")

        # Auto-poster content config (--autopost). Scheduling (interval/cap/quiet-hours) is
        # read by the manager from the bot's config, not the child; the child only needs the
        # content knobs. auto_post_topics is a free-form string (one per line / comma-sep).
        setif("auto_post_seed", "AUTO_POST_SEED")
        setif("auto_post_topics", "AUTO_POST_TOPICS")

        # Phase-4 dedup A/B switch: route this bot's Pleroma network ops through the
        # app's shared services (via botframework/*_shim). Set "use_app_service": true in the
        # bot's Advanced config to test one bot against the legacy clients.
        if bot_dict.get("use_app_service"):
            env["PLEROMA_USE_APP_SERVICE"] = "true"

        # Image-backend API key: per-bot override beats the global already in base_env.
        setif("posterchanai_api_key", "POSTERCHANAI_API_KEY")

        # DB creds (only when the bot needs Pleroma Postgres). Per-bot db_user/db_pass/db_host
        # override the global SQL settings; blank falls back to the global.
        if bot_dict.get("sql_database"):
            env["SQL_USER"] = str(bot_dict.get("db_user") or sql_user)
            env["SQL_PASS"] = str(bot_dict.get("db_pass") or sql_pass)
            env["SQL_HOST"] = str(bot_dict.get("db_host") or sql_host)
            env["SQL_DATABASE"] = str(bot_dict["sql_database"])

        setif("block_image", "BLOCK_IMAGE")
        setif("block_prompt", "BLOCK_PROMPT")
        setif("welcome_image", "WELCOME_IMAGE")
        setif("welcome_message", "WELCOME_MESSAGE")
        setif("welcome_lookback_minutes", "WELCOME_LOOKBACK_MINUTES")
        setif("welcome_prompt", "WELCOME_PROMPT")
        setif("report_image", "REPORT_IMAGE")
        setif("report_prompt", "REPORT_PROMPT")
        setif("unfollow_image", "UNFOLLOW_IMAGE")
        if bot_dict.get("unfollow_silent_mode") is not None:
            env["UNFOLLOW_SILENT_MODE"] = str(bot_dict["unfollow_silent_mode"]).lower()
        setif("tts_voice", "TTS_VOICE")
        setif("tts_rate", "TTS_RATE")
        setif("tts_pitch", "TTS_PITCH")
        if bot_dict.get("auto_narrate"):
            env["AUTO_NARRATE"] = "true"
        setif("video_encoder", "VIDEO_ENCODER")
        if bot_dict.get("temperature") is not None:
            env["AI_TEMPERATURE"] = str(bot_dict["temperature"])

    return env


def _cmd_for(bot_dict: dict) -> list:
    if bot_dict.get("bot_type") == "image":
        return [sys.executable, str(MAIN_PY), "--image"]
    # Default to the bot's OWN platform when no feature modes are set — a Nostr bot (e.g. a
    # stats-only NostrStats) must run as --nostr (so it goes live + publishes its kind-0
    # name/nip05/avatar), not as the platform listener. Falls back to pleroma if platform is unset.
    modes = bot_dict.get("modes") or ["--" + (bot_dict.get("platform") or "pleroma")]
    return [sys.executable, str(MAIN_PY)] + list(modes)


def _spec_sig(bot_dict: dict, base_env: dict) -> str:
    """A signature of everything that determines the spawned process — its env (nsec, relays,
    profile name/nip05/picture, …) and command (modes). When it changes, the running bot must be
    restarted so UI edits (e.g. a new display name → NOSTR_PROFILE_NAME → republished kind-0) take
    effect; otherwise the live process keeps the stale env and the change never lands."""
    env = _build_env(bot_dict, base_env)
    # Only the manager's OWN overlay counts — the keys it derives from the bot row + the bots_*
    # settings. Everything inherited from this process's os.environ is excluded, because that is
    # not config: importing torch-XPU/oneAPI or OpenCV mutates LD_LIBRARY_PATH and QT_QPA_* in the
    # live app, so hashing the whole env meant the FIRST image or effect render silently changed
    # every bot's signature and the next reconcile restarted all of them. In production that killed
    # a Nostr `geni` 3s in — the image finished, the listener that would have replied was gone.
    # Restarts are for real edits; ambient library churn must not be able to trigger one.
    overlay = {k: v for k, v in env.items() if base_env.get(k) != v}
    blob = json.dumps({"env": overlay, "cmd": _cmd_for(bot_dict)}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def _spawn(bot_dict: dict, base_env: dict) -> subprocess.Popen:
    """Spawn botframework/main.py for a bot. cwd=botframework so its root imports resolve."""
    env = _build_env(bot_dict, base_env)
    cmd = _cmd_for(bot_dict)
    _proc_sig[bot_dict["name"]] = _spec_sig(bot_dict, base_env)
    _last_env[bot_dict["name"]] = dict(env)   # baseline for the "what changed" diff (KEYS are logged, never values — env holds the nsec)
    return subprocess.Popen(cmd, env=env, cwd=str(BOTFRAMEWORK_DIR))


def _enabled_bots_for_host():
    """Return {'text': [dict,...], 'image': [dict,...]} of enabled bots for this node.

    A bot with an empty host runs on every node; otherwise host must match this hostname."""
    host = get_hostname()
    db = SessionLocal()
    try:
        bots = db.query(Bot).filter(Bot.enabled == True).all()  # noqa: E712
        out = {"text": [], "image": []}
        for b in bots:
            if b.host and b.host.strip() and b.host.strip() != host:
                continue
            d = bot_to_dict(b)
            out["image" if b.bot_type == "image" else "text"].append(d)
        return out
    finally:
        db.close()


# ---- text-bot reconcile ------------------------------------------------------

def _terminate(name, proc, timeout=3):
    # 3s (was 10): bots are stateless subprocesses that normally exit at once on SIGTERM; a short
    # graceful wait before SIGKILL keeps a slow/hung bot from pushing the service past its 10s stop
    # deadline.
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass
    logger.info("[BOTS] stopped %s", name)


def _terminate_all(named_procs, timeout=3):
    """SIGTERM every proc at once, then wait up to `timeout` TOTAL (shared deadline) for them to exit,
    SIGKILL stragglers. CONCURRENT — so N slow bots shut down in parallel and can't compound the
    service's 10s stop deadline the way N sequential per-bot waits would."""
    import time as _time
    live = [(n, p) for (n, p) in named_procs if p and p.poll() is None]
    for _n, p in live:
        try:
            p.terminate()
        except Exception:
            pass
    end = _time.monotonic() + timeout
    for n, p in live:
        try:
            p.wait(timeout=max(0.05, end - _time.monotonic()))
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        logger.info("[BOTS] stopped %s", n)


def _reconcile_text(text_bots, base_env):
    """Ensure exactly the enabled text bots are running; restart crashes (rate-limited).

    Only bots with listener modes get a persistent process — a pure auto-poster (auto-post
    enabled, no reply/feature modes) has nothing to listen for and is driven entirely by
    _reconcile_scheduled, so spawning a listener would just crash-loop on a default mode."""
    # Bots with listener modes get a persistent process. ALSO include Nostr Stats bots: the stats
    # feature is a config flag (not a mode), but the bot still needs a live process to publish its
    # kind-0 profile + go green — _cmd_for defaults it to --nostr. (The app posts its stats on a 6h
    # timer; the live --nostr process is just its identity/presence.)
    wanted = {d["name"]: d for d in text_bots if d.get("modes") or d.get("stats_enabled")}
    now = time.time()

    # stop bots no longer wanted
    for name in list(_procs.keys()):
        if name not in wanted:
            proc = _procs.pop(name)
            if proc and proc.poll() is None:
                _terminate(name, proc)
            _restart_counts.pop(name, None)
            _proc_sig.pop(name, None)

    # start/restart wanted bots
    for name, d in wanted.items():
        proc = _procs.get(name)
        if proc is None:
            _procs[name] = _spawn(d, base_env)
            logger.info("[BOTS] started %s (pid %s)", name, _procs[name].pid)
            continue
        if proc.poll() is None and _proc_sig.get(name) != _spec_sig(d, base_env):
            # config edited in the UI (e.g. display name / nip05 / avatar / modes) — restart so the
            # new env is applied and the bot republishes its kind-0 profile on startup.
            # Say WHICH keys moved. A restart kills whatever the bot is mid-way through (a Nostr
            # `geni` that takes 30s comes back to no listener and the user never gets a reply), and
            # in production every bot was restarting together every few minutes with no UI edit —
            # a signature this opaque can't be told apart from a real edit without naming the delta.
            try:
                _new_env = _build_env(d, base_env)
                _old_env = _last_env.get(name) or {}
                _diff = sorted(k for k in set(_new_env) | set(_old_env)
                               if _old_env.get(k) != _new_env.get(k))
                logger.info("[BOTS] %s config changed; restarting to apply (changed: %s)",
                            name, ", ".join(_diff[:12]) or "cmd/modes only")
                _last_env[name] = _new_env
            except Exception:
                logger.info("[BOTS] %s config changed; restarting to apply", name)
            _terminate(name, proc)
            _procs[name] = _spawn(d, base_env)
            continue
        if proc.poll() is not None:  # crashed
            info = _restart_counts.setdefault(name, {"count": 0, "first_restart": now, "gaveup": False})
            if now - info["first_restart"] > RESTART_COUNT_RESET_TIME:
                info["count"], info["first_restart"], info["gaveup"] = 0, now, False
            if info["count"] >= MAX_RESTARTS_PER_HOUR:
                # Leave the dead proc parked; log once (not every 5s pass) until the window resets.
                if not info["gaveup"]:
                    logger.error("[BOTS] %s exceeded %d restarts/hour; giving up until the hour resets",
                                 name, MAX_RESTARTS_PER_HOUR)
                    info["gaveup"] = True
                continue
            logger.warning("[BOTS] %s died (code %s); restarting", name, proc.returncode)
            _procs[name] = _spawn(d, base_env)
            info["count"] += 1


# ---- scheduled one-shot posters (image bots + text auto-post) ----------------
#
# Both kinds are "on a timer, spawn a one-shot subprocess that generates and posts, then
# exits." Image bots default to fixed hours (legacy behavior, preserved); text auto-post
# bots use a randomized interval. Either kind can override scheduling via per-bot config.

def _to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _next_scheduled_time(hours):
    now = time.localtime()
    for h in sorted(hours):
        if h > now.tm_hour or (h == now.tm_hour and now.tm_min < 5):
            return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, 0, 0,
                                now.tm_wday, now.tm_yday, now.tm_isdst))
    tom = time.localtime(time.time() + 86400)
    return time.mktime((tom.tm_year, tom.tm_mon, tom.tm_mday, min(hours), 0, 0,
                        tom.tm_wday, tom.tm_yday, tom.tm_isdst))


def _in_quiet_hours(spec):
    """spec is "HH-HH" (24h, local). True if the current hour is inside the window
    (handles windows that wrap past midnight, e.g. "23-06"). Blank/invalid = never quiet."""
    if not spec:
        return False
    try:
        a, b = (int(x) % 24 for x in str(spec).split("-", 1))
    except (ValueError, TypeError):
        return False
    if a == b:
        return False
    h = time.localtime().tm_hour
    return a <= h < b if a < b else (h >= a or h < b)


def _scheduled_jobs(image_bots, text_bots):
    """Build the list of scheduled posters: every image bot, plus every text bot that has
    auto_post_enabled. Each job carries the one-shot mode to spawn and its kind."""
    jobs = []
    for d in image_bots:
        jobs.append({"name": d["name"], "dict": d, "kind": "image", "mode": "--image"})
    for d in text_bots:
        if _truthy(d.get("auto_post_enabled")):
            jobs.append({"name": d["name"], "dict": d, "kind": "text", "mode": "--autopost"})
    return jobs


# Persisted scheduler state per bot, keyed by name, so a restart resumes instead of resetting.
#  - last-post times (epoch secs): an interval schedule survives restarts instead of re-rolling
#    its countdown on every deploy. Image bots on fixed hours are already deploy-proof, so this
#    only affects intervals.
#  - daily post counts {"day": yday, "count": n}: a per-day cap survives restarts (the in-memory
#    counter alone resets to 0 each process, letting a frequently-restarted node blow past the cap).
_LAST_RUN_KEY = "autopost_last_runs"
_DAILY_COUNT_KEY = "autopost_daily_counts"


def _load_json_dict(key: str) -> dict:
    """Read a name-keyed JSON dict Setting (returns {} if absent/corrupt)."""
    val = settings_store.get(key, "")
    if val:
        try:
            data = json.loads(val)
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _save_json_dict_entry(key: str, name: str, value):
    """Set data[name]=value in a name-keyed JSON dict Setting (read-modify-write)."""
    try:
        data = _load_json_dict(key)
        data[name] = value
        settings_store.put(key, json.dumps(data))
    except Exception as e:
        logger.error("[BOTS] could not persist %s for %s: %s", key, name, e)


def _load_last_runs() -> dict:
    return _load_json_dict(_LAST_RUN_KEY)


def _save_last_run(name: str, ts: float):
    _save_json_dict_entry(_LAST_RUN_KEY, name, ts)


def _load_daily_counts() -> dict:
    return _load_json_dict(_DAILY_COUNT_KEY)


def _save_daily_count(name: str, day: int, count: int):
    _save_json_dict_entry(_DAILY_COUNT_KEY, name, {"day": day, "count": count})


def _job_uses_interval(job) -> bool:
    """True if this job schedules off a randomized interval — text auto-post, or any bot
    (including an image bot) that has an explicit interval configured — rather than the fixed
    image-bot hours. Image bots are now interval-configurable: set auto_post_interval_min/max
    to drive them on a timer, or leave both blank to keep the legacy fixed-hours cadence."""
    d = job["dict"]
    return (_to_num(d.get("auto_post_interval_min")) is not None
            or _to_num(d.get("auto_post_interval_max")) is not None
            or job["kind"] == "text")


def _next_run_for(job, now, offset=0.0, last_run=None):
    """When should this job next fire? Interval jobs (text auto-post, or any bot with an
    interval configured) post on the configured cadence; image bots without an interval keep
    the fixed-hours schedule.

    Interval semantics (kept intuitive): a SINGLE value is an exact cadence — set 30 and it
    posts every 30 min. Set BOTH min and max to jitter each gap randomly between them. With
    neither set (auto-post enabled but unconfigured), fall back to a conservative random default.

    For interval jobs, if a persisted last_run is known the next fire is scheduled relative
    to it (deploy-proof: an overdue bot fires once on the next reconcile, then re-rolls)."""
    d = job["dict"]
    if _job_uses_interval(job):
        imin = _to_num(d.get("auto_post_interval_min"))
        imax = _to_num(d.get("auto_post_interval_max"))
        if imin is None and imax is None:
            lo, hi = AUTOPOST_DEFAULT_MIN_MIN, AUTOPOST_DEFAULT_MAX_MIN
        else:
            # a single value is an exact interval; both present = random window between them
            lo = imin if imin is not None else imax
            hi = imax if imax is not None else imin
        lo = max(lo, 1.0)  # floor: a misconfigured 0/negative must not spin (post every reconcile)
        if hi < lo:
            hi = lo
        gap = random.uniform(lo, hi) * 60.0  # lo == hi -> exactly that interval
        return (last_run + gap) if last_run else (now + gap)
    return _next_scheduled_time(IMAGE_SCHEDULE_HOURS) + offset


def _reconcile_scheduled(jobs, base_env):
    """Ensure each scheduled poster fires on its cadence (subject to daily cap + quiet
    hours), spawning a one-shot subprocess that posts and exits."""
    wanted = {j["name"]: j for j in jobs}
    now = time.time()
    today = time.localtime().tm_yday

    # reap finished manual test-post children (publish-now) so they don't linger as zombies
    global _oneshot_procs
    _oneshot_procs = [p for p in _oneshot_procs if p.poll() is None]

    # persisted last-post times + daily counts so interval schedules and per-day caps survive
    # restarts. Only hit the DB when a schedule actually needs (re)creating — steady-state
    # passes skip the read.
    need_last = any(n not in _post_sched for n in wanted)
    last_runs = _load_last_runs() if need_last else {}
    daily_counts = _load_daily_counts() if need_last else {}

    # drop schedules for bots no longer wanted (disabled/deleted/auto-post turned off)
    for name in list(_post_sched.keys()):
        if name == "_last_start":
            continue
        if name not in wanted:
            sched = _post_sched.pop(name)
            if sched.get("process") and sched["process"].poll() is None:
                _terminate(name, sched["process"])

    for i, (name, job) in enumerate(wanted.items()):
        d = job["dict"]
        sched = _post_sched.get(name)
        if sched is None:
            offset = i * IMAGE_STAGGER_DELAY if job["kind"] == "image" else 0.0
            anchor = last_runs.get(name)
            # Anchor interval schedules to a persisted time so a restart resumes the existing
            # deadline instead of re-rolling a fresh (up to AUTOPOST_DEFAULT_MAX_MIN) countdown
            # on every deploy. Without this seed the deploy-proofing never engaged — it only
            # kicked in after the first successful post, so a frequently-restarted node never
            # reached the deadline and the bot never posted at all. (Fixed-hour image bots
            # schedule off the absolute clock and ignore the anchor, so seeding is a no-op there.)
            if anchor is None and _job_uses_interval(job):
                anchor = now
                _save_last_run(name, now)
                last_runs[name] = now
            # restore today's persisted post count so a per-day cap holds across restarts;
            # a stale (previous-day) entry resets to 0.
            dc = daily_counts.get(name) or {}
            count = int(dc.get("count", 0)) if dc.get("day") == today else 0
            sched = {"next_run": _next_run_for(job, now, offset, anchor),
                     "process": None, "offset": offset, "day": today, "count": count}
            _post_sched[name] = sched
        # reap a finished one-shot
        if sched["process"] and sched["process"].poll() is not None:
            sched["process"] = None
        # reset the per-day counter at the day boundary (next fire re-persists for the new day)
        if sched["day"] != today:
            sched["day"], sched["count"] = today, 0
        if sched["process"] is not None or now < sched["next_run"]:
            continue
        # don't post during quiet hours; re-check shortly
        if _in_quiet_hours(d.get("auto_post_quiet_hours")):
            sched["next_run"] = now + 1800
            continue
        # respect a per-day cap if one is set
        cap = _to_num(d.get("auto_post_max_per_day"))
        if cap is not None and sched["count"] >= cap:
            sched["next_run"] = now + 3600
            continue
        # stagger image spawns so they don't flood the image queue (text posts are cheap)
        if job["kind"] == "image" and now - _post_sched.get("_last_start", 0) < IMAGE_STAGGER_DELAY:
            continue
        spawn_dict = dict(d)
        spawn_dict["modes"] = [job["mode"]]
        sched["process"] = _spawn(spawn_dict, base_env)
        _post_sched["_last_start"] = now
        sched["count"] += 1
        sched["next_run"] = _next_run_for(job, now, sched["offset"], now)
        _save_last_run(name, now)
        _save_daily_count(name, today, sched["count"])
        logger.info("[BOTS] ran %s post for %s (%d today)", job["kind"], name, sched["count"])


def _manager_enabled() -> bool:
    """Master kill-switch (bots_manager_enabled). Off = run nothing (safe-by-default)."""
    return settings_store.get_bool("bots_manager_enabled")


def _stop_all_children():
    for name in list(_procs.keys()):
        proc = _procs.pop(name)
        if proc and proc.poll() is None:
            _terminate(name, proc)
    _restart_counts.clear()
    _proc_sig.clear()
    for name in list(_post_sched.keys()):
        if name == "_last_start":
            continue
        sched = _post_sched.pop(name)
        if sched.get("process") and sched["process"].poll() is None:
            _terminate(name, sched["process"])


def _reconcile():
    with _lock:
        try:
            if not _manager_enabled():
                if _procs or any(k != "_last_start" for k in _post_sched):
                    logger.info("[BOTS] manager disabled; stopping all bots")
                    _stop_all_children()
                return
            bots = _enabled_bots_for_host()
            base_env = _load_global_env()
            _reconcile_text(bots["text"], base_env)
            _reconcile_scheduled(_scheduled_jobs(bots["image"], bots["text"]), base_env)
        except Exception as e:
            logger.error("[BOTS] reconcile error: %s", e, exc_info=True)


def _wait_for_relay(timeout: float = 60.0) -> None:
    """Block until the local Nostr relay accepts connections before spawning bots. The bots publish to
    ws://127.0.0.1:3052 the moment they start, so spawning them while the relay subprocess is still
    booting wastes ~a minute of 'connection refused' retries PER bot on every restart. Bounded; on
    timeout we spawn anyway and let the bots' own reconnect handle it."""
    import socket
    import time as _t
    try:
        port = int((settings_store.get("nostr_relay_port", "3052") or "3052").strip())
    except (ValueError, TypeError):
        port = 3052
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline and not _stop_event.is_set():
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                logger.info("[BOTS] local relay up on :%d — starting bots", port)
                return
        except OSError:
            _stop_event.wait(1.0)
    logger.warning("[BOTS] local relay not up after %ds — starting bots anyway", int(timeout))


def _monitor_loop():
    logger.info("[BOTS] manager monitor started (host=%s)", get_hostname())
    _wait_for_relay()   # don't spawn bots into a not-yet-listening relay (startup-race noise)
    while not _stop_event.is_set():
        _reconcile()
        _stop_event.wait(RECONCILE_INTERVAL)
    logger.info("[BOTS] manager monitor stopped")


# ---- one-time migration seed -------------------------------------------------

# Recovered ~/posterchan bots_config (gitignored, local-only). Used to seed the DB the first
# time the manager runs so existing bots + global settings carry over from the merge.
_EXPORT_PATH = BOTFRAMEWORK_DIR / "bots_config_export.json"

# Global export key -> bots_* setting key. The legacy POSTERCHANAI_API_ENDPOINT becomes the
# single bots_server_url (chat + image both derive from it); separate-AI-URL dropped.
_SEED_GLOBALS = {
    "POSTERCHANAI_API_ENDPOINT": "bots_server_url",
    "AI_API_KEY": "bots_ai_api_key", "AI_MODEL": "bots_ai_model",
    "SEARXNG_URL": "bots_searxng_url", "TIMEZONE": "bots_timezone",
    "SQL_USER": "bots_sql_user", "SQL_PASS": "bots_sql_pass", "SQL_HOST": "bots_sql_host",
    "POSTERCHANAI_USERNAME": "bots_posterchanai_username",
    "POSTERCHANAI_PASSWORD": "bots_posterchanai_password",
    "POSTERCHANAI_API_KEY": "bots_posterchanai_api_key",
}
# Bot dict keys that map to first-class columns (everything else → JSON config)
_COLUMN_KEYS = {"name", "platform", "host", "bot_type", "modes"}


def seed_from_export():
    """If the bots table is empty and a recovered export exists, import bots + globals once."""
    if not _EXPORT_PATH.exists():
        return
    db = SessionLocal()
    try:
        if db.query(Bot).count() > 0:
            return
        try:
            data = json.loads(_EXPORT_PATH.read_text())
        except Exception as e:
            logger.warning("[BOTS] could not read %s: %s", _EXPORT_PATH, e)
            return

        # global settings (only fill ones not already set)
        for src, dst in _SEED_GLOBALS.items():
            if src not in data:
                continue
            val = data[src]
            val = ("true" if val else "false") if isinstance(val, bool) else str(val)
            # Only fill if not already set (empty/absent counts as unset).
            if not settings_store.get(dst, ""):
                settings_store.put(dst, val)

        # bots
        count = 0
        for bot_type, key in (("image", "IMAGE_BOTS"), ("text", "TEXT_BOTS")):
            for name, cfg in (data.get(key) or {}).items():
                cfg = dict(cfg or {})
                modes = cfg.get("modes") or []
                if isinstance(modes, (list, tuple)):
                    modes = ",".join(modes)
                json_cfg = {k: v for k, v in cfg.items() if k not in _COLUMN_KEYS}
                db.add(Bot(
                    name=name, enabled=True, bot_type=cfg.get("bot_type", bot_type),
                    platform=cfg.get("platform", "pleroma"), host=cfg.get("host", "") or "",
                    modes=modes, config=json.dumps(json_cfg),
                ))
                count += 1
        db.commit()
        logger.info("[BOTS] seeded %d bots + globals from %s", count, _EXPORT_PATH.name)
    except Exception as e:
        db.rollback()
        logger.error("[BOTS] seed failed: %s", e, exc_info=True)
    finally:
        db.close()


# ---- public API (used by main.py lifecycle + the router) ---------------------

def start_bot_manager():
    """Idempotent: launch the monitor thread (port-3051 only, wired in app/main.py)."""
    global _monitor_thread
    with _lock:
        if _monitor_thread and _monitor_thread.is_alive():
            return
        if not MAIN_PY.exists():
            logger.warning("[BOTS] %s missing; bot manager not started", MAIN_PY)
            return
        seed_from_export()
        _stop_event.clear()
        _monitor_thread = threading.Thread(target=_monitor_loop, name="bot-manager", daemon=True)
        _monitor_thread.start()


def stop_bot_manager():
    """Stop the monitor and terminate all child processes."""
    global _monitor_thread
    _stop_event.set()
    with _lock:
        # Collect ALL child processes and terminate them concurrently (one shared ~3s deadline) rather
        # than sequentially, so a busy node's many bots can't sum past the service's 10s stop deadline.
        named = list(_procs.items())
        named += [(n, s["process"]) for n, s in _post_sched.items()
                  if n != "_last_start" and s.get("process")]
        named += [("test-post", p) for p in _oneshot_procs]
        _terminate_all(named)
        _procs.clear()
        _post_sched.clear()
        _oneshot_procs.clear()
    if _monitor_thread:
        _monitor_thread.join(timeout=3)   # was 15 — don't let the monitor join blow the stop deadline
        _monitor_thread = None


def reconcile_now():
    """Force an immediate reconcile (called after CRUD / On-Off so the UI feels instant)."""
    if _monitor_thread and _monitor_thread.is_alive():
        _reconcile()


def restart_bot(name: str):
    """Kill a running bot's child so the next reconcile respawns it (if still enabled)."""
    with _lock:
        proc = _procs.pop(name, None)
        if proc and proc.poll() is None:
            _terminate(name, proc)
        _restart_counts.pop(name, None)
    reconcile_now()


def random_scene() -> str:
    """Pick a random background/location from the bot framework's scene list, so the image
    preview path can mirror imageposter's random-scene behavior. Returns '' on any failure."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("random_scenes", BOTFRAMEWORK_DIR / "random_scenes.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return random.choice(mod.RANDOM_SCENE_ELEMENTS)
    except Exception as e:
        logger.warning("[BOTS] could not load random scene: %s", e)
        return ""


def _bot_dict_by_name(name: str):
    """Look up a single bot (enabled or not) and return its merged config dict, or None."""
    db = SessionLocal()
    try:
        b = db.query(Bot).filter(Bot.name == name).first()
        return bot_to_dict(b) if b else None
    finally:
        db.close()


def _one_shot_mode(bot_dict: dict) -> str:
    """The spawn mode that makes a bot post once: image bots render an image, everything
    else generates an in-character text post."""
    return "--image" if bot_dict.get("bot_type") == "image" else "--autopost"


def publish_post(name: str) -> dict:
    """Fire a single post now (bypassing the schedule), reusing the same one-shot the
    scheduler spawns. Fire-and-forget; the child posts and exits on its own."""
    with _lock:
        d = _bot_dict_by_name(name)
        if not d:
            return {"ok": False, "error": "bot not found"}
        spawn_dict = dict(d)
        spawn_dict["modes"] = [_one_shot_mode(d)]
        proc = _spawn(spawn_dict, _load_global_env())
        # reap any finished prior test posts so they don't linger as zombies
        global _oneshot_procs
        _oneshot_procs = [p for p in _oneshot_procs if p.poll() is None]
        _oneshot_procs.append(proc)
        return {"ok": True, "pid": proc.pid, "mode": spawn_dict["modes"][0]}


def preview_post(name: str, timeout: int = 120) -> dict:
    """Generate one post and return its text WITHOUT publishing. Text bots only — image
    previews would have to round-trip a rendered image, which this doesn't do."""
    d = _bot_dict_by_name(name)
    if not d:
        return {"ok": False, "error": "bot not found"}
    if d.get("bot_type") == "image":
        return {"ok": False, "error": "Preview is only available for text bots."}
    env = _build_env(d, _load_global_env())
    cmd = [sys.executable, str(MAIN_PY), "--autopost-print"]
    try:
        out = subprocess.run(cmd, env=env, cwd=str(BOTFRAMEWORK_DIR),
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Generation timed out."}
    # Extract the post text the child wrapped in PREVIEW markers (see autopost.py).
    begin, end = "=== AUTOPOST PREVIEW BEGIN ===", "=== AUTOPOST PREVIEW END ==="
    so = out.stdout or ""
    if begin in so and end in so:
        text = so.split(begin, 1)[1].split(end, 1)[0].strip()
        if text:
            return {"ok": True, "text": text}
    return {"ok": False, "error": "Generation failed (no output). Check the bot's prompt and server settings."}


def get_status():
    """Per-bot runtime status for the admin UI (merges DB rows with the live registry)."""
    host = get_hostname()
    db = SessionLocal()
    try:
        rows = db.query(Bot).all()
    finally:
        db.close()
    with _lock:
        out = []
        for b in rows:
            running = False
            pid = None
            scheduled = False           # image / auto-post bots: "online" = registered with a next run
            next_run = None
            if b.bot_type == "image":
                sched = _post_sched.get(b.name)
                proc = sched.get("process") if sched else None
                if sched and sched.get("next_run"):
                    scheduled, next_run = True, sched["next_run"]
            else:
                proc = _procs.get(b.name)
            if proc and proc.poll() is None:
                running, pid = True, proc.pid
            on_this_host = (not b.host) or (b.host.strip() in ("", host))
            out.append({
                "id": b.id, "name": b.name, "enabled": bool(b.enabled),
                "bot_type": b.bot_type, "platform": b.platform,
                "host": b.host or "", "modes": b.modes or "",
                "running": running, "pid": pid, "on_this_host": on_this_host,
                "scheduled": scheduled, "next_run": next_run,
                "restarts": _restart_counts.get(b.name, {}).get("count", 0),
            })
        return out

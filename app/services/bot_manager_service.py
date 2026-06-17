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
import socket
import logging
import threading
import subprocess
from pathlib import Path

from app.database import SessionLocal
from app.models import Bot, Setting

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
# processes, so they can't share the GPU-loaded model in-process). No separate OPENAI endpoint,
# no ComfyUI/Stable-Diffusion. These straightforward settings map 1:1; the server-derived ones
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


def _load_global_env():
    """Base env shared by every bot, derived from the global bots_* settings.

    Everything routes through the single PosterChanAI server URL: the bot's LLM calls hit
    {server}/api/chat/completions and its image generation uses the server's image API
    (USE_POSTERCHANAI is forced on — no ComfyUI/SD)."""
    env = os.environ.copy()
    wanted = set(_GLOBAL_ENV_MAP) | set(_SERVER_URL_KEYS)
    db = SessionLocal()
    try:
        rows = {s.key: s.value for s in db.query(Setting).filter(Setting.key.in_(wanted)).all()}
    finally:
        db.close()
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
    if server:
        env["POSTERCHANAI_API_ENDPOINT"] = server
        env["OPENAI_ENDPOINT"] = server + "/api/chat/completions"
    env["USE_POSTERCHANAI"] = "true"   # always use the unified server; never ComfyUI/SD
    # SQL_* base creds (per-bot SQL_DATABASE is added in _build_env when sql_database is set)
    db = SessionLocal()
    try:
        sql = {s.key: s.value for s in db.query(Setting).filter(
            Setting.key.in_(["bots_sql_user", "bots_sql_pass", "bots_sql_host"])).all()}
    finally:
        db.close()
    env["_SQL_USER"] = sql.get("bots_sql_user", "")
    env["_SQL_PASS"] = sql.get("bots_sql_pass", "")
    env["_SQL_HOST"] = sql.get("bots_sql_host", "")
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

    def setif(key, env_key, transform=str):
        v = bot_dict.get(key)
        if v not in (None, "", [], {}):
            env[env_key] = transform(v)

    if is_image:
        # Image bots post to their own platform — set creds for whichever it is (the
        # one-shot imageposter picks pleroma vs misskey from these env vars).
        if bot_dict.get("platform") == "pleroma":
            setif("server", "PLEROMA_ENDPOINT")
            setif("username", "PLEROMA_USERNAME", lambda v: str(v).lstrip("@"))
            setif("access_token", "PLEROMA_ACCESS_TOKEN")
        else:
            setif("server", "MISSKEY_SERVER")
            setif("username", "MISSKEY_USERNAME", lambda v: str(v).lstrip("@"))
            setif("access_token", "MISSKEY_ACCESS_TOKEN")
        setif("prompt", "IMAGE_POSTER_PROMPT")
        setif("text", "IMAGE_POSTER_TEXT")
        setif("image_negative", "IMAGE_POSTER_NEGATIVE")  # optional negative prompt
        if bot_dict.get("random_scenes"):
            env["IMAGE_POSTER_RANDOM_SCENES"] = "true"
    else:
        platform = bot_dict.get("platform", "misskey")
        if platform == "misskey":
            setif("server", "MISSKEY_SERVER")
            setif("username", "MISSKEY_USERNAME", lambda v: str(v).lstrip("@"))
            setif("access_token", "MISSKEY_ACCESS_TOKEN")
        elif platform == "pleroma":
            setif("server", "PLEROMA_ENDPOINT")
            setif("username", "PLEROMA_USERNAME", lambda v: str(v).lstrip("@"))
            setif("access_token", "PLEROMA_ACCESS_TOKEN")
            setif("pleroma_admin_token", "PLEROMA_ADMIN_TOKEN")
        elif platform == "nostr":
            # Nostr identity is a secret key (nsec/hex); relays + the external media host come
            # from the bot's config. Blank relays → app defaults.
            setif("nostr_nsec", "NOSTR_NSEC")
            setif("nostr_relays", "NOSTR_RELAYS")
            setif("nostr_media_service", "NOSTR_MEDIA_SERVICE")
            setif("nostr_media_endpoint", "NOSTR_MEDIA_ENDPOINT")
        elif platform == "matrix":
            setif("matrix_server", "MATRIX_SERVER")
            setif("matrix_user_id", "MATRIX_USER_ID")
            setif("matrix_access_token", "MATRIX_ACCESS_TOKEN")
            setif("matrix_room_id", "MATRIX_ROOM_ID")
            setif("matrix_admins", "MATRIX_ADMINS")

        # Matrix settings can accompany any platform
        setif("matrix_server", "MATRIX_SERVER")
        setif("matrix_user_id", "MATRIX_USER_ID")
        setif("matrix_access_token", "MATRIX_ACCESS_TOKEN")
        setif("matrix_room_id", "MATRIX_ROOM_ID")
        setif("matrix_admins", "MATRIX_ADMINS")
        if bot_dict.get("matrix_verify_ssl") is not None:
            env["MATRIX_VERIFY_SSL"] = str(bot_dict["matrix_verify_ssl"]).lower()
        if bot_dict.get("shamebot_rooms"):
            sr = bot_dict["shamebot_rooms"]
            env["SHAMEBOT_ROOMS"] = ",".join(sr) if isinstance(sr, (list, tuple)) else str(sr)

        # Nitter RSS → Matrix feeds
        if bot_dict.get("nitter_feeds"):
            env["NITTER_FEEDS"] = json.dumps(bot_dict["nitter_feeds"])
        setif("nitter_poll_seconds", "NITTER_POLL_SECONDS")

        if bot_dict.get("stickers_enabled"):
            env["STICKERS_ENABLED"] = "true"

        tmh = bot_dict.get("trusted_media_hosts")
        if tmh:
            env["TRUSTED_MEDIA_HOSTS"] = ",".join(tmh) if isinstance(tmh, (list, tuple)) else str(tmh)

        setif("prompt", "PROMPT")

        # Auto-poster content config (--autopost). Scheduling (interval/cap/quiet-hours) is
        # read by the manager from the bot's config, not the child; the child only needs the
        # content knobs. auto_post_topics is a free-form string (one per line / comma-sep).
        setif("auto_post_seed", "AUTO_POST_SEED")
        setif("auto_post_topics", "AUTO_POST_TOPICS")
        setif("auto_post_rooms", "AUTO_POST_ROOMS")  # Matrix-only: rooms to auto-post to

        # Phase-4 dedup A/B switch: route this bot's Pleroma/Misskey network ops through the
        # app's shared services (via botframework/*_shim). Set "use_app_service": true in the
        # bot's Advanced config to test one bot against the legacy clients.
        if bot_dict.get("use_app_service"):
            env["PLEROMA_USE_APP_SERVICE"] = "true"
            env["MISSKEY_USE_APP_SERVICE"] = "true"
            env["MATRIX_USE_APP_SERVICE"] = "true"

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


def _spawn(bot_dict: dict, base_env: dict) -> subprocess.Popen:
    """Spawn botframework/main.py for a bot. cwd=botframework so its root imports resolve."""
    env = _build_env(bot_dict, base_env)
    if bot_dict.get("bot_type") == "image":
        cmd = [sys.executable, str(MAIN_PY), "--image"]
    else:
        modes = bot_dict.get("modes") or ["--misskey"]
        cmd = [sys.executable, str(MAIN_PY)] + list(modes)
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

def _terminate(name, proc, timeout=10):
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass
    logger.info("[BOTS] stopped %s", name)


def _reconcile_text(text_bots, base_env):
    """Ensure exactly the enabled text bots are running; restart crashes (rate-limited).

    Only bots with listener modes get a persistent process — a pure auto-poster (auto-post
    enabled, no reply/feature modes) has nothing to listen for and is driven entirely by
    _reconcile_scheduled, so spawning a listener would just crash-loop on a default mode."""
    wanted = {d["name"]: d for d in text_bots if d.get("modes")}
    now = time.time()

    # stop bots no longer wanted
    for name in list(_procs.keys()):
        if name not in wanted:
            proc = _procs.pop(name)
            if proc and proc.poll() is None:
                _terminate(name, proc)
            _restart_counts.pop(name, None)

    # start/restart wanted bots
    for name, d in wanted.items():
        proc = _procs.get(name)
        if proc is None:
            _procs[name] = _spawn(d, base_env)
            logger.info("[BOTS] started %s (pid %s)", name, _procs[name].pid)
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
    db = SessionLocal()
    try:
        s = db.query(Setting).filter(Setting.key == key).first()
        if s and s.value:
            try:
                data = json.loads(s.value)
                return data if isinstance(data, dict) else {}
            except (ValueError, TypeError):
                return {}
        return {}
    finally:
        db.close()


def _save_json_dict_entry(key: str, name: str, value):
    """Set data[name]=value in a name-keyed JSON dict Setting (read-modify-write)."""
    db = SessionLocal()
    try:
        s = db.query(Setting).filter(Setting.key == key).first()
        data = {}
        if s and s.value:
            try:
                data = json.loads(s.value)
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, TypeError):
                data = {}
        data[name] = value
        if s:
            s.value = json.dumps(data)
        else:
            db.add(Setting(key=key, value=json.dumps(data)))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("[BOTS] could not persist %s for %s: %s", key, name, e)
    finally:
        db.close()


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
    db = SessionLocal()
    try:
        s = db.query(Setting).filter(Setting.key == "bots_manager_enabled").first()
        return bool(s and str(s.value).strip().lower() in ("true", "1", "yes"))
    finally:
        db.close()


def _stop_all_children():
    for name in list(_procs.keys()):
        proc = _procs.pop(name)
        if proc and proc.poll() is None:
            _terminate(name, proc)
    _restart_counts.clear()
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


def _monitor_loop():
    logger.info("[BOTS] manager monitor started (host=%s)", get_hostname())
    while not _stop_event.is_set():
        _reconcile()
        _stop_event.wait(RECONCILE_INTERVAL)
    logger.info("[BOTS] manager monitor stopped")


# ---- one-time migration seed -------------------------------------------------

# Recovered ~/posterchan bots_config (gitignored, local-only). Used to seed the DB the first
# time the manager runs so existing bots + global settings carry over from the merge.
_EXPORT_PATH = BOTFRAMEWORK_DIR / "bots_config_export.json"

# Global export key -> bots_* setting key. The legacy POSTERCHANAI_API_ENDPOINT becomes the
# single bots_server_url (chat + image both derive from it); ComfyUI/SD/separate-AI-URL dropped.
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
        existing = {s.key: s for s in db.query(Setting).filter(
            Setting.key.in_(set(_SEED_GLOBALS.values()))).all()}
        for src, dst in _SEED_GLOBALS.items():
            if src not in data:
                continue
            val = data[src]
            val = ("true" if val else "false") if isinstance(val, bool) else str(val)
            if dst in existing:
                if not existing[dst].value:
                    existing[dst].value = val
            else:
                db.add(Setting(key=dst, value=val))

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
                    platform=cfg.get("platform", "misskey"), host=cfg.get("host", "") or "",
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
        for name, proc in list(_procs.items()):
            if proc and proc.poll() is None:
                _terminate(name, proc)
        _procs.clear()
        for name, sched in list(_post_sched.items()):
            if name == "_last_start":
                continue
            if sched.get("process") and sched["process"].poll() is None:
                _terminate(name, sched["process"])
        _post_sched.clear()
        # terminate any in-flight manual test-post one-shots
        for proc in _oneshot_procs:
            if proc and proc.poll() is None:
                _terminate("test-post", proc)
        _oneshot_procs.clear()
    if _monitor_thread:
        _monitor_thread.join(timeout=15)
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
            if b.bot_type == "image":
                sched = _post_sched.get(b.name)
                proc = sched.get("process") if sched else None
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
                "restarts": _restart_counts.get(b.name, {}).get("count", 0),
            })
        return out

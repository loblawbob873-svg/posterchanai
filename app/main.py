from fastapi import FastAPI, Request, Depends, Response
from fastapi import Request as FastAPIRequest
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
from urllib.parse import quote
import os
import logging
import threading

# Configure logging for console output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)

from app.database import init_db, get_db
from app.auth import get_current_user_optional, get_current_user, create_access_token
from app.models import User, VerificationToken
from app.routers import auth, chat, admin, tts, stt, openai_api, image_api, media_api, news, mail, torrent, storage, files, music_api, video_api, voice_api, effects_api
from app.routers import fourchan, youtube_thumb, bots, push, calls, streams, rss, markets, websearch
from app.routers import admin_emoji
from app.routers import git as git_router
from app.routers.telegram import router as telegram_router
from app.routers.pleroma import router as pleroma_router
from app.routers.social_login import router as social_login_router
from app.routers.nostr import router as nostr_router
from app.routers.blossom import router as blossom_router
from app.routers.client import router as client_router
from app.services.load_balancer import NoHealthyServersError
# Which components THIS process supervises — read by BOTH startup and shutdown below, so a
# component that is started here is always stopped here. Module scope on purpose: a
# function-local import left the shutdown handler's _owns() undefined, which is a NameError
# on every stop and a leaked mediamtx/pion-turn.
from app.role import owns as _owns
from fastapi.responses import JSONResponse

# Custom JSON encoder to handle bytes and other non-serializable types
import json
from pathlib import Path

class BytesSafeJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles bytes and Path objects."""
    def default(self, obj):
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='ignore')
        elif isinstance(obj, Path):
            return str(obj)
        return super().default(obj)

# Initialize FastAPI app
app = FastAPI(
    title="Posterchanai",
    description="AI Chat Application",
    version="1.0.0",
    json_encoder=BytesSafeJSONEncoder
)

# CORS for the native app (Capacitor Android WebView): it bundles the web UI locally at the
# https://localhost origin and calls this server cross-origin for data (relay, API, Blossom). Allow those
# app origins (browsers on poster.place are same-origin and unaffected).
from fastapi.middleware.cors import CORSMiddleware


class _ScopedCORS(CORSMiddleware):
    """App-wide CORS for the Capacitor app — but NEVER for /blossom or /git. Both set their own
    wide-open (Access-Control-Allow-Origin: *) CORS per-route and must accept ANY origin: they're
    fetched cross-origin by arbitrary Nostr clients AND by our own web client (served on poster.place
    but hitting media.poster.place — a different origin). Letting this narrow, credentialed allowlist
    handle the preflight 400'd every browser upload/list ('blossom broken'), and did the same to every
    in-browser git client reading a hosted repo (gitworkshop.dev: "blocked by CORS"). It fails twice
    over: it answers the preflight 400 for an origin not on the allowlist, and it appends
    Allow-Credentials: true, which a browser REJECTS when combined with Origin: * — so the route's own
    correct header was neutralised by this one. Skipping both prefixes hands the request to their own
    OPTIONS handlers."""
    _OWN_CORS = ("/blossom", "/git/")   # trailing slash: must not swallow a future /gitea-style route

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").startswith(self._OWN_CORS):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


app.add_middleware(
    _ScopedCORS,
    # Native-app origins only. Capacitor (androidScheme=https → https://localhost) and the desktop
    # Electron build, which serves its bundled client from its own privileged app:// scheme so the page
    # gets a secure context (crypto.subtle) and a real tuple origin. NOT http://localhost: with
    # allow_credentials it would let any plaintext-http localhost page read the victim's authed responses.
    #
    # An instance that has not shipped `app://posterchan` here refuses the desktop app's credentialed
    # calls, so a self-hoster upgrading their node is what re-enables AI/media/streams for desktop users
    # pointed at it. The relays-only mode needs nothing from this list — it makes no cross-origin call.
    allow_origins=["https://localhost", "capacitor://localhost", "app://posterchan"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Exception handler for NoHealthyServersError - prevent it from being shown to client
@app.exception_handler(NoHealthyServersError)
async def no_healthy_servers_handler(request: FastAPIRequest, exc: NoHealthyServersError):
    """Silently handle NoHealthyServersError - this triggers fallback to local inference"""
    # This shouldn't normally be reached since we catch it in the router,
    # but if it is (e.g., during streaming), return a 500 that won't show the error message
    # The actual error message is empty, so we return a generic message
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Service temporarily unavailable"}}
    )

# Global exception handler to ensure all errors return JSON (not HTML)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

def _clean_error_detail(detail):
    """Clean error detail to ensure it's JSON serializable."""
    if detail is None:
        return None
    elif isinstance(detail, bytes):
        return detail.decode('utf-8', errors='ignore')
    elif isinstance(detail, (str, int, float, bool)):
        return detail
    elif isinstance(detail, (list, tuple)):
        return [_clean_error_detail(item) for item in detail]
    elif isinstance(detail, dict):
        return {str(k): _clean_error_detail(v) for k, v in detail.items()}
    else:
        return str(detail)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: FastAPIRequest, exc: StarletteHTTPException):
    """Ensure HTTP exceptions return JSON instead of HTML"""
    cleaned_detail = _clean_error_detail(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": cleaned_detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: FastAPIRequest, exc: RequestValidationError):
    """Ensure validation errors return JSON with a body (never empty)."""
    errors = exc.errors()
    cleaned_errors = _clean_error_detail(errors)
    body = {"detail": cleaned_errors, "message": "Request validation failed (422). Check 'detail' for field errors."}
    return JSONResponse(status_code=422, content=body)

@app.exception_handler(Exception)
async def general_exception_handler(request: FastAPIRequest, exc: Exception):
    """Catch-all exception handler to ensure JSON responses for API routes"""
    logger = logging.getLogger(__name__)
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    
    error_detail = str(exc)
    # Clean the error detail to ensure it's JSON serializable
    error_detail = _clean_error_detail(error_detail)
    
    # Check if this is an API request (starts with /api/)
    if request.url.path.startswith("/api/"):
        # Return JSON error response for API routes
        # Ensure the content dict is also clean
        content = {"detail": f"Internal server error: {error_detail}"}
        content = _clean_error_detail(content)
        return JSONResponse(
            status_code=500,
            content=content
        )
    # For non-API requests, re-raise to let FastAPI's default handler deal with it
    # This allows HTML error pages for web pages
    return HTMLResponse(
        status_code=500,
        content=f"<html><body><h1>Internal Server Error</h1><p>{error_detail}</p></body></html>"
    )

# Mount static files
static_path = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.middleware("http")
async def _static_revalidate(request: Request, call_next):
    """Force JS/CSS to revalidate on every load so a deploy's new asset is always picked up.
    The `?v=` query-string bump only busts caches that key on the query string — a CDN (e.g.
    Cloudflare in front of this deployment) can be configured to IGNORE it, serving stale JS
    indefinitely (the "I still see the old behavior" bug). `no-cache` lets the browser/CDN keep
    a copy but requires revalidation against the origin, which returns the new file after deploy."""
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/static/js/") or p.startswith("/static/css/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp

# Templates
templates_path = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=templates_path)

# Include routers
# IMPORTANT: files.router must come before chat.router to avoid route conflicts
# chat.router has /api/files/{username}/{conversation_id}/{filename} which could match
# files.router has /api/files/view/{file_path:path} which should take precedence for /view/ paths
app.include_router(auth.router)
app.include_router(push.router)   # PWA Web Push (VAPID subscribe + delivery)
app.include_router(files.router)  # Register files router first to avoid conflicts
app.include_router(chat.router)
app.include_router(chat.ws_only_router)  # /ws/chat/{id} for clients that omit /api prefix
app.include_router(admin.router)
app.include_router(admin_emoji.router)  # /api/admin/emoji/* (instance custom emoji packs)
app.include_router(tts.router)
app.include_router(stt.router)
app.include_router(image_api.router)
app.include_router(music_api.router)
app.include_router(video_api.router)
app.include_router(voice_api.router)
app.include_router(effects_api.router)
app.include_router(media_api.router)
app.include_router(news.router)
app.include_router(websearch.router)  # /api/websearch/* (Web Search screen: SearXNG proxy, reader, AI overview)
app.include_router(mail.router)
app.include_router(torrent.router)
app.include_router(fourchan.router)
app.include_router(rss.router)
app.include_router(markets.router)
app.include_router(youtube_thumb.router)
app.include_router(bots.router)
app.include_router(calls.router)  # /api/calls/turn-credentials (ICE config for voice/video calls)
app.include_router(streams.router)  # /api/streams/* (OBS streaming: MediaMTX auth hook, ingest info, HLS proxy)
app.include_router(git_router.router)  # /api/git/* (GRASP git host: provision/list/announce; 404 unless git_server_enabled)
app.include_router(git_router.smart_router)  # /git/* smart-HTTP reverse-proxy (active only when git_server_proxy_url set)
app.include_router(storage.router)
app.include_router(telegram_router)
app.include_router(pleroma_router)
app.include_router(social_login_router)   # /api/auth/{google,pleroma}/* — sign in with an account
app.include_router(nostr_router)
app.include_router(blossom_router)
app.include_router(client_router)
# OpenAI-compatible API: use OPENAI_API_PREFIX if app is behind a reverse proxy subpath
_openai_prefix = os.getenv("OPENAI_API_PREFIX", "").strip().rstrip("/")
app.include_router(openai_api.router, prefix=_openai_prefix)
# Also include files_router if it exists (for storage server compatibility)
if hasattr(storage, 'files_router'):
    app.include_router(storage.files_router)



@app.on_event("startup")
async def startup():
    try:
        init_db()
        from app.database import SessionLocal

        # Settings live in the Nostr relay datastore (no SQL Setting table). init_db() already loaded
        # the local-only keys + defaults into the in-process cache; now mint the operator key (signs
        # the setting docs) and hydrate the cache from the relay's event store (sync SQL) so the
        # env-seed blocks below + every reader see authoritative values, and settings_store.put()
        # writes are signed.
        try:
            from app.services import settings_store as _ss0
            _kdb0 = SessionLocal()
            try:
                _ss0.ensure_operator_key(_kdb0)
                _ss0.hydrate_from_db(_kdb0)
                # Data-safety: migrate any custom values still in a legacy SQL `settings` table into
                # the relay (for keys the relay doesn't already hold), so upgrades don't lose config.
                _ss0.migrate_legacy_table(_kdb0)
            finally:
                _kdb0.close()
        except Exception as _e0:
            logging.warning(f"Early settings hydrate failed: {_e0}")

        # Turnkey Docker music: when POSTERCHANAI_MUSIC=1 (the compose `music` profile sets this),
        # auto-enable music and point it at the acestep container — only seeding keys the admin
        # hasn't already set, so UI changes win. Lets `docker compose --profile music` generate a
        # song with no manual config.
        if os.environ.get("POSTERCHANAI_MUSIC", "0") == "1":
            try:
                from app.services import settings_store as _ss
                if not _ss.exists("music_enabled"):
                    _ss.put("music_enabled", "true")
                # Only point at a container when one was EXPLICITLY given. Music is in-process now,
                # so seeding the old acestep URL by default would send every Docker deployment at a
                # sidecar it no longer runs — and make prepare_for_music try to start it.
                _ace = os.environ.get("POSTERCHANAI_ACESTEP_URL", "").strip()
                if _ace and not _ss.exists("music_api_base"):
                    _ss.put("music_api_base", _ace)
                logging.info("Music auto-configured from POSTERCHANAI_MUSIC env (native in-process%s)",
                             "" if not _ace else f"; external server {_ace}")
            except Exception as e:
                logging.error(f"Error seeding music settings: {e}")

        # Turnkey Docker voice cloning: POSTERCHANAI_VOICE=1 (compose passes the SAME variable as the
        # INSTALL_VOICE build arg, so the engine can't be missing from an image that has the feature
        # on). Seeds only keys the admin hasn't set, so UI changes win.
        if os.environ.get("POSTERCHANAI_VOICE", "0") == "1":
            try:
                from app.services import settings_store as _ss
                if not _ss.exists("voice_enabled"):
                    _ss.put("voice_enabled", "true")
                _vm = os.environ.get("POSTERCHANAI_VOICE_MODEL", "").strip()
                if _vm and not _ss.exists("voice_model"):
                    _ss.put("voice_model", _vm)
                logging.info("Voice cloning auto-configured from POSTERCHANAI_VOICE env")
            except Exception as e:
                logging.error(f"Error seeding voice settings: {e}")

        # Turnkey Docker video: when POSTERCHANAI_VIDEO=1, auto-enable native text-to-video and
        # (optionally) point video_model at POSTERCHANAI_VIDEO_MODEL. The model auto-downloads to
        # HF_HOME on first use (persisted on the data volume). Only seeds keys the admin hasn't set.
        if os.environ.get("POSTERCHANAI_VIDEO", "0") == "1":
            try:
                from app.services import settings_store as _ss
                if not _ss.exists("video_enabled"):
                    _ss.put("video_enabled", "true")
                if not _ss.exists("video_local_enabled"):
                    _ss.put("video_local_enabled", "true")
                _vm = os.environ.get("POSTERCHANAI_VIDEO_MODEL")
                if _vm and not _ss.exists("video_model"):
                    _ss.put("video_model", _vm)
                logging.info("Video generation auto-configured from POSTERCHANAI_VIDEO env")
            except Exception as e:
                logging.error(f"Error seeding video settings: {e}")

        # Turnkey Docker Nostr relay: when POSTERCHANAI_NOSTR_RELAY=1, auto-enable the built-in
        # web-of-trust relay, bind 0.0.0.0 (so the published container port is reachable) and
        # point its snapshot at the data volume. Only seeds keys the admin hasn't set.
        if os.environ.get("POSTERCHANAI_NOSTR_RELAY", "0") == "1":
            try:
                from app.services import settings_store as _ss
                # Plumbing keys (local-only) → persist to the local JSON. First-run only (if absent).
                if not _ss.exists("nostr_relay_enabled"):
                    _ss.put("nostr_relay_enabled", "true")
                if not _ss.exists("nostr_relay_bind"):
                    _ss.put("nostr_relay_bind", "0.0.0.0")
                logging.info("Nostr relay seeded from POSTERCHANAI_NOSTR_RELAY env")
            except Exception as e:
                logging.error(f"Error seeding Nostr relay settings: {e}")

        # IN A CONTAINER, loopback-only is never the useful bind. The relay's default is 127.0.0.1 and
        # only POSTERCHANAI_NOSTR_RELAY=1 used to widen it — but the relay became the app's DATASTORE and
        # is now enabled by default (database.py), so a plain `docker compose up` (that env defaults to 0)
        # ran the relay bound to the container's own loopback while compose published 3052. The browser
        # client is handed ws://<host>:3052/relay and every connection was refused: "never connects to
        # relays on a new install". Publishing a port the process can't be reached on is never intended,
        # so widen it whenever we're containerised — still first-run only, so an explicit bind is kept.
        try:
            if os.path.exists("/.dockerenv"):
                from app.services import settings_store as _ss
                if not _ss.exists("nostr_relay_bind"):
                    _ss.put("nostr_relay_bind", "0.0.0.0")
                    logging.info("Container detected: relay bind seeded to 0.0.0.0 "
                                 "(127.0.0.1 would make the published port unreachable)")
        except Exception as e:
            logging.error(f"Error seeding container relay bind: {e}")

        # Turnkey Docker calls/TURN: when POSTERCHANAI_TURN=1, enable the built-in Pion TURN relay and
        # seed its public IP + optional domain from env. The app supervises the bundled binary; still needs
        # one open public port (3478 and/or a TLS port). Only seeds keys the admin hasn't set.
        if os.environ.get("POSTERCHANAI_TURN", "0") == "1":
            try:
                from app.services import settings_store as _ss
                if not _ss.exists("turn_enabled"):
                    _ss.put("turn_enabled", "true")
                _ip = os.environ.get("POSTERCHANAI_TURN_PUBLIC_IP")
                if _ip and not _ss.exists("turn_public_ip"):
                    _ss.put("turn_public_ip", _ip)
                _dom = os.environ.get("POSTERCHANAI_TURN_DOMAIN")
                if _dom and not _ss.exists("turn_domain"):
                    _ss.put("turn_domain", _dom)
                logging.info("TURN relay seeded from POSTERCHANAI_TURN env")
            except Exception as e:
                logging.error(f"Error seeding TURN settings: {e}")

        # Turnkey Docker OBS streaming: when POSTERCHANAI_STREAM=1, enable the built-in MediaMTX server and
        # seed its public host from env. The app supervises the bundled binary; needs an open RTMP port.
        if os.environ.get("POSTERCHANAI_STREAM", "0") in ("1", "true"):
            try:
                from app.services import settings_store as _ss
                if not _ss.exists("stream_enabled"):
                    _ss.put("stream_enabled", "true")
                _sdom = os.environ.get("POSTERCHANAI_STREAM_DOMAIN")
                if _sdom and not _ss.exists("stream_domain"):
                    _ss.put("stream_domain", _sdom)
                logging.info("MediaMTX streaming seeded from POSTERCHANAI_STREAM env")
            except Exception as e:
                logging.error(f"Error seeding streaming settings: {e}")

        # Turnkey Docker Blossom server: when POSTERCHANAI_BLOSSOM=1, ENABLE the built-in Blossom
        # media server. `blossom_enabled` is FORCE-set on every boot — the env flag is a declarative
        # deployment directive, so it must win even on an existing volume where the key is already
        # "false" (otherwise a rebuilt nostr image silently stays "Blossom server disabled"). The
        # other blossom_* config keys are seeded only if absent so admin tuning is preserved. Default
        # backend stays "proxy" (falls back to local on the data volume if no storage server set).
        if os.environ.get("POSTERCHANAI_BLOSSOM", "0") == "1":
            try:
                from app.services import settings_store as _ss
                # FORCE-set on every boot: the env flag is a declarative deployment directive, so it
                # must win even though apply_defaults() already wrote blossom_enabled="false" into the
                # store (which makes exists() true) — otherwise the nostr image stays "Blossom disabled".
                _ss.put("blossom_enabled", "true")
                if not _ss.exists("blossom_storage_path"):
                    _ss.put("blossom_storage_path", "/app/data/blossom")
                logging.info("Blossom server seeded from POSTERCHANAI_BLOSSOM env")
            except Exception as e:
                logging.error(f"Error seeding Blossom settings: {e}")

        # Turnkey Docker git host: when POSTERCHANAI_GIT=1, enable the built-in GRASP git server and
        # bind 0.0.0.0 so the published container port is reachable (127.0.0.1 inside a container is
        # not). Mirrors the relay's seeding: FORCE the enable flag (a declarative deployment directive
        # must win on an existing volume where apply_defaults already wrote "false"), seed the rest
        # only if absent so admin tuning survives.
        if os.environ.get("POSTERCHANAI_GIT", "0") in ("1", "true"):
            try:
                from app.services import settings_store as _ss
                _ss.put("git_server_enabled", "true")
                if not _ss.exists("git_server_bind"):
                    _ss.put("git_server_bind", "0.0.0.0")
                _gbase = os.environ.get("POSTERCHANAI_GIT_PUBLIC_BASE")
                if _gbase and not _ss.exists("git_server_public_base"):
                    # Clone URLs are built from this, so a wrong/missing value produces unusable ones.
                    _ss.put("git_server_public_base", _gbase.rstrip("/"))
                logging.info("Git host seeded from POSTERCHANAI_GIT env")
            except Exception as e:
                logging.error(f"Error seeding git host settings: {e}")

        # (Turnkey out-of-box config — proxy/tor/torrent on + wired, blossom backend local, upstream
        # relays populated, timeline backfill on — lives in app/database.py default_settings, seeded
        # on first run for EVERY install path: install.sh, Docker, manual. No install-type-specific
        # code here; the Admin UI is the source of truth after first run.)

        # Start health check if enabled (in background, don't block startup)
        try:
            from app.services.health_check import start_health_check
            # Start health check in background thread to avoid blocking startup
            def start_health_check_background():
                import time
                time.sleep(2)  # Wait a moment for server to be ready
                try:
                    start_health_check()
                except Exception as e:
                    logging.error(f"Error starting health check in background: {e}", exc_info=True)
            thread = threading.Thread(target=start_health_check_background, daemon=True)
            thread.start()
            logging.info("Health check scheduled to start in background")
        except Exception as e:
            logging.error(f"Error scheduling health check: {e}", exc_info=True)

        # Only start schedulers on main instance (port 3051) to avoid database locks
        app_port = int(os.environ.get("POSTERCHANAI_PORT", "3051"))

        # …and only supervise the components THIS ROLE owns. Under the default role ('all') every
        # `_owns()` below is True and this behaves exactly as it always has: one process supervising
        # the relay, worker, mediamtx, TURN and the bots. When those are split into their own systemd
        # units the app runs as role 'app' and stops spawning them, which is the entire point — a
        # deploy of a router change then restarts the web app WITHOUT dropping every Nostr client,
        # killing live streams mid-broadcast, dropping calls, or restarting nine bots.
        #
        # Defaulting to 'all' is what makes this safe to ship: a node whose unit file has not been
        # updated keeps the old behaviour on the next code deploy.
        _role = os.environ.get("POSTERCHANAI_ROLE", "all")
        if app_port == 3051 and _role != "all":
            logging.info("[role] running as '%s' — supervising only this role's components", _role)
        if app_port == 3051:
            try:
                # Background pollers (social/nitter/logs)
                # run in a SEPARATE worker process so their polling/bridging doesn't contend
                # with the web/API event loop (the bridge could otherwise stall the reactor).
                # They're DB-mediated, so the app's reply/action endpoints keep working.
                from app.worker import start_worker_process
                if _owns('worker'): start_worker_process()
            except Exception as e:
                logging.error(f"Error starting background worker: {e}", exc_info=True)

            try:
                # Start the bot manager (merged ~/posterchan framework; Admin → Bots)
                from app.services.bot_manager_service import start_bot_manager
                if _owns('bots'): start_bot_manager()
            except Exception as e:
                logging.error(f"Error starting bot manager: {e}", exc_info=True)

            try:
                # Start the reminders poller (`remind` command)
                from app.services.reminder_service import start_reminder_scheduler
                start_reminder_scheduler()
            except Exception as e:
                logging.error(f"Error starting reminder scheduler: {e}", exc_info=True)
            try:
                # Start the scheduled-posts poller (publishes pre-signed notes at their scheduled time)
                from app.services.scheduled_posts_service import start_scheduled_posts_scheduler
                start_scheduled_posts_scheduler()
            except Exception as e:
                logging.error(f"Error starting scheduled-posts scheduler: {e}", exc_info=True)
            try:
                # Distributed-LB (DVM): listen for GPU jobs addressed to this node over Nostr
                # (no-op unless nostr_dvm_enabled). Runs in this loop; jobs hop to the GPU lock.
                from app.services import nostr_dvm
                nostr_dvm.start_worker()
            except Exception as e:
                logging.error(f"Error starting Nostr DVM worker: {e}", exc_info=True)
            try:
                # 4chan catalog warm-refresh (15 min, only boards a user viewed → kind-30078 cache)
                from app.routers.fourchan import start_catalog_refresh
                start_catalog_refresh()
            except Exception as e:
                logging.error(f"Error starting 4chan refresh scheduler: {e}", exc_info=True)
            try:
                # Markets daily digest (08:00): crypto price+news → operator kind-30078, served via /api/markets
                from app.services.markets_service import start_markets_scheduler
                start_markets_scheduler()
            except Exception as e:
                logging.error(f"Error starting markets scheduler: {e}", exc_info=True)

            try:
                # Resolve/mint the datastore operator key into the keyfile BEFORE the relay starts,
                # so the relay's operator set includes this signer from its first boot. Otherwise a
                # fresh node (no linked users) starts the relay with an empty operator set and then
                # rejects its own settings docs as "not in web of trust".
                from app.services import settings_store as _ss
                _kdb = SessionLocal()
                try:
                    _ss.ensure_operator_key(_kdb)
                    # NOTE: admin is NOT auto-provisioned from the operator key anymore — that made an
                    # admin the owner doesn't hold. Instead the FIRST npub to sign in claims admin
                    # automatically (app/routers/auth.py nostr_login). Turnkey + it's the owner's key.
                finally:
                    _kdb.close()
            except Exception as e:
                logging.warning(f"Could not pre-mint operator key: {e}")

            try:
                # Start the built-in Nostr WoT relay (own thread; no-op unless enabled)
                from app.services.nostr_relay import start_nostr_relay
                if _owns('relay'): start_nostr_relay()
            except Exception as e:
                logging.error(f"Error starting Nostr relay: {e}", exc_info=True)

            try:
                # Supervise the built-in GRASP git-over-nostr host (subprocess; no-op unless
                # git_server_enabled). MUST start AFTER the relay: the push-auth hook reads the
                # relay's Postgres for maintainer-signed 30618 state.
                from app.services.git_http_service import start_git_http
                if _owns('git'): start_git_http()
            except Exception as e:
                logging.error(f"Error starting git host: {e}", exc_info=True)

            try:
                # Supervise the built-in Pion TURN relay for voice/video calls (subprocess; no-op unless
                # turn_enabled + the binary is built + a public IP is set)
                from app.services.turn_service import start_turn_server
                if _owns('media'): start_turn_server()
            except Exception as e:
                logging.error(f"Error starting TURN server: {e}", exc_info=True)

            try:
                # Supervise the built-in MediaMTX server for OBS streaming (subprocess; no-op unless
                # stream_enabled + the binary is installed)
                from app.services.stream_service import start_stream_server
                if _owns('media'): start_stream_server()
                # Clear any stream recordings orphaned in tmpfs by a mid-stream restart/crash (they'd
                # otherwise accumulate until /dev/shm fills). Safe at startup — nothing is served yet.
                from app.services.stream_vod_service import sweep_orphans
                sweep_orphans()
            except Exception as e:
                logging.error(f"Error starting stream server: {e}", exc_info=True)

            try:
                # The same idea for the REST of the app's temp files. `finally: rmtree` covers every
                # normal path but cannot survive SIGKILL, so an OOM kill or a restart landing
                # mid-render strands whatever it was holding. /tmp is a tmpfs here, so that is
                # pinned, unreclaimable RAM which makes the next OOM likelier. Startup is the only
                # hook needed: orphans come from kills, and a kill is always followed by a start.
                from app.services.temp_sweep_service import sweep_temp_orphans
                sweep_temp_orphans()
            except Exception as e:
                logging.error(f"Error sweeping orphaned temp files: {e}", exc_info=True)

            try:
                # Safety net that publishes a live stream's parked "ended" event when its feed is gone —
                # covers the ends MediaMTX's runOnUnpublish hook can't deliver (app restarted mid-stream,
                # mediamtx killed). Streams would otherwise stay announced as ● LIVE forever.
                from app.services.stream_end_service import start_stream_end_reaper
                start_stream_end_reaper()
            except Exception as e:
                logging.error(f"Error starting stream-end reaper: {e}", exc_info=True)

            try:
                # Settings read-path: hydrate the local Setting cache from the relay (the
                # authoritative datastore). Deferred so the relay's WS is up first. Runs in the
                # background so startup isn't blocked.
                import asyncio as _aio
                async def _relay_ready(timeout=45.0):
                    """Wait until the relay's WS listener actually accepts a TCP connection, so the
                    hydrate/seed below don't race it (a fixed sleep loses when the relay does a WoT
                    build before listening → 'Connection refused' on the first writes)."""
                    import socket
                    from app.services import settings_store as _ss
                    port = _ss.get_int("nostr_relay_port", 3052)
                    deadline = _aio.get_event_loop().time() + timeout
                    while _aio.get_event_loop().time() < deadline:
                        try:
                            _r, _w = await _aio.wait_for(_aio.open_connection("127.0.0.1", port), 2.0)
                            _w.close()
                            return True
                        except Exception:
                            await _aio.sleep(1.0)
                    return False
                async def _hydrate_settings():
                    await _aio.sleep(2)
                    await _relay_ready()
                    _db = SessionLocal()
                    try:
                        from app.services import settings_store
                        from app.database import DEFAULT_SETTINGS
                        # FRESH NODE: the relay may have read its operator set before the operator key
                        # existed (it has no linked users yet), so it would reject the operator's own
                        # settings docs as "not in web of trust". Refresh the relay's operator set
                        # (this reload also re-reads cfg["operator"], which includes the keyfile
                        # operator key) BEFORE seeding, then settings writes are accepted.
                        try:
                            from app.services.nostr_relay.thread import trigger_block_reload
                            trigger_block_reload()
                            await _aio.sleep(1.5)   # let the relay's control poller apply it
                        except Exception as e:
                            logging.warning(f"relay operator-set reload before seed failed: {e}")
                        # Re-hydrate now the relay is up (catch anything written since early startup),
                        # then push any default settings the relay doesn't yet hold UP to it (Nostr
                        # events) so the relay is the authoritative store of the out-of-box config.
                        settings_store.hydrate_from_db(_db)
                        await settings_store.seed_relay_defaults(_db, DEFAULT_SETTINGS)
                        # Re-assert declarative env directives AFTER hydrate+seed so they win even when
                        # the relay still holds the default "false" (hydrate/seed would clobber the
                        # early force-put at startup). The write-through persists "true" to the relay.
                        if os.environ.get("POSTERCHANAI_BLOSSOM", "0") == "1":
                            settings_store.put("blossom_enabled", "true")
                    except Exception as e:
                        logging.warning(f"Settings hydrate/seed from relay failed: {e}")
                    try:
                        from app.services import users_store
                        await users_store.hydrate(_db)
                        await users_store.hydrate_user_kv(_db)   # mail/nitter/caldav kv from relay
                        # SQL→relay catch-all: mirror every account's record + non-exempt kv up to the
                        # relay (closes gaps from auxiliary save paths — tz/caldav/webdav/music — that
                        # don't write-through), then keep doing it periodically.
                        await users_store.reconcile_all(_db)
                        users_store.start_users_reconcile()
                    except Exception as e:
                        logging.warning(f"Users hydrate from relay failed: {e}")
                    try:
                        from app.services import bots_store
                        await bots_store.hydrate(_db)
                    except Exception as e:
                        logging.warning(f"Bots hydrate from relay failed: {e}")
                    try:
                        from app.services import chat_store
                        await chat_store.hydrate_conversations(_db)
                    except Exception as e:
                        logging.warning(f"Conversations hydrate from relay failed: {e}")
                    try:
                        from app.services import record_store
                        await record_store.hydrate(_db)
                    except Exception as e:
                        logging.warning(f"Records hydrate from relay failed: {e}")
                    try:
                        # Advertise the operator's Blossom server list (kind-10063 / BUD-03) now the
                        # relay is up + settings hydrated, so clients can fail over to the mirrors by
                        # hash when this node's Blossom is down.
                        from app.services import blossom_service
                        await blossom_service.publish_operator_server_list(_db)
                    except Exception as e:
                        logging.warning(f"Blossom kind-10063 advertise failed: {e}")
                    finally:
                        _db.close()
                _aio.create_task(_hydrate_settings())
            except Exception as e:
                logging.error(f"Error scheduling settings hydrate: {e}", exc_info=True)

            try:
                # Start the Blossom expiry-cleanup thread (idle until blobs have a TTL)
                from app.services.blossom_service import start_blossom_cleanup
                start_blossom_cleanup()
            except Exception as e:
                logging.error(f"Error starting Blossom cleanup: {e}", exc_info=True)

            try:
                # Per-user sandbox container reaper. ONCE, as soon as Docker is reachable, sweep EVERY
                # pcai-sandbox container left by a PRIOR process (reap_all — safe: a fresh process has no
                # active runs). Then every 5 min reap only TRACKED containers idle >15 min and not in use
                # (reap_idle — NO orphan sweep, which would race an in-flight run). Retry availability
                # quickly until the first sweep so a slow-booting daemon doesn't disable the reaper.
                from app.services import sandbox_service as _sbx
                async def _sandbox_reaper():
                    swept = False
                    while True:
                        try:
                            if await _sbx.available():
                                if not swept:
                                    n = await _sbx.reap_all()
                                    swept = True
                                    if n:
                                        logging.info(f"[sandbox] startup swept {n} leftover container(s)")
                                else:
                                    await _sbx.reap_idle(ttl=900)
                        except Exception as _e:
                            logging.warning(f"[sandbox] reaper pass failed: {_e}")
                        await _aio.sleep(300 if swept else 20)
                _aio.create_task(_sandbox_reaper())
            except Exception as e:
                logging.error(f"Error starting sandbox reaper: {e}", exc_info=True)

        else:
            logging.info(f"Schedulers disabled on port {app_port} (only run on port 3051)")

        # Tor + the HTTP proxy that fronts it. Both are now `if _owns(...)` and their bodies live in
        # the services (start_from_settings) so the `tor` / `proxy` role processes run the identical
        # code — a second copy here would drift the moment either changed.
        try:
            if _owns('tor'):
                from app.services.tor_service import start_from_settings as _tor_start
                _tor_start()
        except Exception as e:
            logging.error(f"Failed to start built-in Tor: {e}", exc_info=True)

        try:
            if _owns('proxy'):
                from app.services.http_proxy_service import start_from_settings as _proxy_start
                _proxy_start()
        except Exception as e:
            logging.error(f"Failed to start built-in HTTP proxy: {e}", exc_info=True)

        # Auto-start built-in torrent client if enabled (skip if using remote server)
        try:
            from app.services import settings_store as _ss
            bt_server_url = _ss.get("bt_server_url", "")
            if bt_server_url:
                logging.info(f"Torrent requests will be forwarded to: {bt_server_url}")
            elif _ss.get_bool("bt_enabled"):
                bt_proxy_host = _ss.get("bt_proxy_host", "")
                if bt_proxy_host:
                    download_dir = _ss.get("bt_download_dir", "") or "/var/lib/posterchanai/torrents"
                    proxy_port = _ss.get_int("bt_proxy_port", 8118)
                    listen_port = _ss.get_int("bt_listen_port", 6881)
                    from app.services.libtorrent_service import LibtorrentService
                    service = LibtorrentService.get_instance(
                        download_dir=download_dir,
                        proxy_host=bt_proxy_host,
                        proxy_port=proxy_port,
                        listen_port=listen_port
                    )
                    logging.info("Built-in torrent client started")
                else:
                    logging.warning("Built-in torrent client enabled but no proxy host configured")
        except Exception as e:
            logging.error(f"Failed to start built-in torrent client: {e}", exc_info=True)

        logging.info("Application startup complete")
    except Exception as e:
        logging.error(f"CRITICAL: Startup failed with exception: {e}", exc_info=True)
        raise  # Re-raise to let FastAPI handle it properly


@app.on_event("shutdown")
async def shutdown():
    # Stop health check
    from app.services.health_check import stop_health_check
    stop_health_check()

    # Only stop schedulers on main instance (port 3051)
    app_port = int(os.environ.get("POSTERCHANAI_PORT", "3051"))
    if app_port == 3051:
        # Stop the background worker process (fedi-timeline bridge + social/nitter/
        # logs pollers all run there now).
        try:
            from app.worker import stop_worker_process
            if _owns('worker'): stop_worker_process()
        except Exception:
            pass

        # Stop the bot manager (terminates all managed bot child processes)
        try:
            from app.services.bot_manager_service import stop_bot_manager
            if _owns('bots'): stop_bot_manager()
        except Exception:
            pass

        # Stop the reminders poller
        try:
            from app.services.reminder_service import stop_reminder_scheduler
            stop_reminder_scheduler()
        except Exception:
            pass
        # Stop the scheduled-posts poller
        try:
            from app.services.scheduled_posts_service import stop_scheduled_posts_scheduler
            stop_scheduled_posts_scheduler()
        except Exception:
            pass
        try:
            from app.services import nostr_dvm
            await nostr_dvm.stop_worker()
        except Exception:
            pass
        try:
            from app.routers.fourchan import stop_catalog_refresh
            stop_catalog_refresh()
        except Exception:
            pass
        try:
            from app.services.markets_service import stop_markets_scheduler
            stop_markets_scheduler()
        except Exception:
            pass

        # Stop the built-in Nostr WoT relay (final snapshot + join its thread) — ONLY if this process
        # started it. Ungated, an app running as role 'app' would reach in and stop the relay owned by
        # posterchanai-relay.service: restarting the web app would take the relay down with it, which
        # is the precise outage the split exists to remove.
        try:
            if _owns('relay'):
                from app.services.nostr_relay import stop_nostr_relay
                stop_nostr_relay()
        except Exception:
            pass

        # Stop the built-in GRASP git host supervisor + terminate the git-http subprocess
        try:
            from app.services.git_http_service import stop_git_http
            if _owns('git'): stop_git_http()
        except Exception:
            pass

        # Stop the built-in TURN relay supervisor + terminate pion-turn
        try:
            from app.services.turn_service import stop_turn_server
            if _owns('media'): stop_turn_server()
        except Exception:
            pass
        try:
            # Stop the built-in MediaMTX streaming server supervisor + terminate mediamtx
            from app.services.stream_service import stop_stream_server
            if _owns('media'): stop_stream_server()
        except Exception:
            pass
        try:
            from app.services.stream_end_service import stop_stream_end_reaper
            stop_stream_end_reaper()
        except Exception:
            pass

        # Stop the Blossom expiry-cleanup thread + close the shared storage-proxy HTTP client
        try:
            from app.services.blossom_service import stop_blossom_cleanup, aclose_http
            stop_blossom_cleanup()
            await aclose_http()
        except Exception:
            pass

    # Stop built-in torrent client if running (only if libtorrent is available)
    try:
        from app.services.libtorrent_service import LibtorrentService
        if LibtorrentService._instance is not None:
            LibtorrentService._instance.stop()
            logging.info("Built-in torrent client stopped")
    except ImportError:
        pass  # libtorrent not installed (using remote forwarding)
    except Exception as e:
        logging.error(f"Error stopping torrent client: {e}")

    # Stop built-in HTTP proxy subprocess if running
    try:
        from app.services.http_proxy_service import stop_http_proxy_process
        stop_http_proxy_process()
    except Exception as e:
        logging.error(f"Error stopping HTTP proxy: {e}")

    # Stop built-in Tor if running
    try:
        from app.services.tor_service import stop_tor_service
        stop_tor_service()
    except Exception as e:
        logging.error(f"Error stopping Tor: {e}")


@app.get("/")
async def index(request: Request):
    # Unified UI: the Nostr client is the single face of the app (old index.html chat UI retired).
    # Everyone lands on /client and logs in with their Nostr key; the AI lives in its tab.
    return RedirectResponse(url="/client", status_code=302)


_APK_LOCAL_PATH = "/home/verita84/posterchan-apk/posterchan.apk"


@app.get("/apk")
async def latest_apk():
    """Download the newest PosterChan Android APK. GitHub Actions builds + signs it on each deploy and
    publishes it to the rolling 'apk-latest' Release; a small refresh job mirrors that asset to a local
    path so poster.place/apk serves the bytes **directly from this server, behind Cloudflare** — a CDN
    edge close to the user, with Range/resume support (Starlette FileResponse honours Range) — which
    downloads far more reliably on slow/throttled mobile links than bouncing to GitHub's distant CDN.
    Falls back to the GitHub redirect if the local mirror isn't present yet."""
    if os.path.exists(_APK_LOCAL_PATH):
        return FileResponse(
            _APK_LOCAL_PATH,
            media_type="application/vnd.android.package-archive",
            filename="posterchan.apk",
            headers={"Cache-Control": "public, max-age=300"},
        )
    return RedirectResponse(
        url="https://github.com/loblawbob873-svg/posterchanai/releases/download/apk-latest/posterchan.apk",
        status_code=302,
    )


_APK_VERSION_PATH = "/home/verita84/posterchan-apk/version.txt"


@app.get("/apk/version")
async def apk_version():
    """Latest published APK build number (the GitHub Actions run_number, == the APK's versionCode), read
    from a sidecar the refresh job writes. The bundled Android app compares it to its baked-in
    window.__PC_APP_BUILD__ and, when this is higher, surfaces an in-app 'Update available' that downloads
    /apk. build:0 means unknown (no sidecar yet) — the app then simply won't prompt."""
    build = 0
    try:
        with open(_APK_VERSION_PATH) as f:
            build = int((f.read() or "0").strip() or "0")
    except Exception:
        build = 0
    return {"build": build, "versionName": (f"1.0.{build}" if build else "")}


# ---- desktop app (Electron: Windows / Linux / macOS) -------------------------------------------
# GitHub Actions (.github/workflows/desktop.yml) builds every target and publishes them to the rolling
# 'desktop-latest' Release; these routes are the stable public face of that release. They also ARE the
# electron-updater feed: the app is built with a generic provider pointing at https://poster.place/desktop/,
# so it fetches /desktop/latest.yml and then the artifact named inside it. Going through this server
# instead of electron-updater's GitHub provider keeps the feed correct — the repo carries two rolling
# releases (apk-latest, desktop-latest) and that provider just takes whichever was published last.
_DESKTOP_DL = "https://github.com/loblawbob873-svg/posterchanai/releases/download/desktop-latest/"
# Allowlist, not a passthrough: this route redirects off-site, so an unchecked name is an open redirect.
_DESKTOP_ASSETS = {
    "PosterChan-Setup.exe", "PosterChan-Setup.exe.blockmap",
    "PosterChan.AppImage", "PosterChan-arm64.dmg", "PosterChan-x64.dmg",
    "latest.yml", "latest-linux.yml",
}
_DESKTOP_ALIASES = {
    "win": "PosterChan-Setup.exe", "windows": "PosterChan-Setup.exe",
    "linux": "PosterChan.AppImage", "appimage": "PosterChan.AppImage",
    "mac": "PosterChan-arm64.dmg", "mac-intel": "PosterChan-x64.dmg",
}


@app.get("/desktop", response_class=HTMLResponse)
async def desktop_page():
    """Download page for the desktop app (one build per platform, all from the same Electron shell)."""
    return HTMLResponse("""<!doctype html><meta charset=utf-8><title>PosterChan for desktop</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0a10;color:#e8e8f0;
font:16px/1.55 system-ui,Segoe UI,Roboto,sans-serif}.c{width:min(430px,92vw);text-align:center;padding:26px}
img{width:84px;height:84px;border-radius:20px}h1{font-size:22px;margin:14px 0 4px}p{color:#8b8ba3;font-size:14px;margin:0 0 20px}
a{display:block;margin:9px 0;padding:13px;border:1px solid #262636;border-radius:12px;color:#e8e8f0;text-decoration:none;background:#12121c}
a:hover{border-color:#00f0ff;color:#00f0ff}a.s{background:transparent;font-size:13.5px;color:#8b8ba3}
small{color:#5c5c73;font-size:12px;display:block;margin-top:18px}</style>
<div class=c><img src="/static/icon-512.png"><h1>PosterChan for desktop</h1>
<p>The same client, in its own window. Auto-updates on Windows and Linux.</p>
<a href="/desktop/win">⊞ &nbsp;Windows installer</a>
<a href="/desktop/linux">🐧 &nbsp;Linux AppImage</a>
<a href="/desktop/mac">🍎 &nbsp;macOS (Apple silicon)</a>
<a class=s href="/desktop/mac-intel">macOS (Intel)</a>
<a class=s href="/apk">📱 Android APK</a>
<a class=s href="/extension">🔑 Firefox add-on (passwords) — on addons.mozilla.org</a>
<small>Unsigned builds — Windows shows a SmartScreen prompt (More info → Run anyway);
on macOS right-click the app → Open.</small></div>""")


_EXT_DL = ("https://github.com/loblawbob873-svg/posterchanai/releases/download/extension-latest/")
# The Mozilla-signed listing. Locale-neutral on purpose — AMO redirects to the visitor's own locale,
# so hardcoding /en-US/ would hand a German user an English page for no reason.
_EXT_AMO = "https://addons.mozilla.org/firefox/addon/posterchan-passwords/"


@app.get("/extension")
async def firefox_extension():
    """The Firefox add-on (autofill + one-time codes for the password vault) — the SIGNED listing.

    This used to hand out the unpacked tarball, because there was no signed build and a temporary
    add-on was the only thing release Firefox would accept. It's on AMO now, so the ordinary answer
    to "I want this add-on" is one click that also auto-updates and works on Firefox for Android —
    neither of which a sideload gives you. The raw artifacts are still one path segment away for
    anyone who wants to install what they built rather than what Mozilla signed.
    """
    return RedirectResponse(url=_EXT_AMO, status_code=302)


@app.get("/extension/unpacked")
async def firefox_extension_unpacked():
    """The unpacked bundle, for `about:debugging` → Load Temporary Add-on (it wants a directory).

    A redirect rather than a local mirror, unlike /apk: an add-on is a one-off desktop download over
    a connection that is by definition working, not a 60 MB APK pulled onto a throttled phone, so the
    resume/Range argument that justifies mirroring the APK doesn't apply. The artifact is built by
    .github/workflows/extension.yml — it is deliberately not in the repo, because it is assembled
    from files that already live there (the shared vaultcore.js and the vendored nostr bundle) and a
    committed copy is a copy that goes stale.
    """
    return RedirectResponse(url=_EXT_DL + "posterchan-passwords-unpacked.tar.gz", status_code=302)


@app.get("/extension/zip")
async def firefox_extension_zip():
    """The packed .zip — what gets submitted to addons.mozilla.org for signing. The unpacked tarball
    above is the one to grab for `about:debugging`, which wants a directory."""
    return RedirectResponse(url=_EXT_DL + "posterchan-passwords.zip", status_code=302)


@app.get("/extension/chrome")
async def chrome_extension():
    """Chrome, Edge and Brave: the same extension, packed with a generated MV3 manifest whose
    background is a service worker (Chrome refuses one that lists `scripts`; Firefox requires them).

    Extract it and use chrome://extensions → Developer mode → Load unpacked. Chrome will not install
    a zip directly — but an unpacked extension needs no signing and no store account, and unlike a
    Firefox temporary add-on it survives a restart, so this is the whole installation story here.
    """
    return RedirectResponse(url=_EXT_DL + "posterchan-passwords-chrome.zip", status_code=302)


@app.get("/desktop/{asset}")
async def desktop_asset(asset: str):
    name = _DESKTOP_ALIASES.get(asset.lower(), asset)
    if name not in _DESKTOP_ASSETS:
        return RedirectResponse(url="/desktop", status_code=302)
    return RedirectResponse(url=_DESKTOP_DL + name, status_code=302)


def _safe_next(nxt: str) -> str:
    """A same-origin PATH to return to after signing in, or "".

    Must start with a single "/" and carry no scheme and no authority: "//evil.com" is a
    protocol-relative URL that browsers follow off-site, so it is refused along with "http://…".

    CONTROL CHARACTERS ARE REJECTED, and that is not tidiness. Browsers DELETE tab, newline and
    carriage return from a URL before resolving it, so "/<TAB>/evil.com" passes every check that only
    looks at the leading characters — it does not start with "//" — and then navigates to //evil.com,
    i.e. off-site. Found reviewing this function, not in the wild; it is the whole reason the check is
    a whitelist of what may appear rather than a blacklist of what may not.
    """
    nxt = (nxt or "").strip()
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or ":" in nxt.split("/")[0]:
        return ""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in nxt):
        return ""
    return nxt[:200]


@app.get("/login")
async def login_page(request: Request, next: str = None):
    # Old password login UI retired — log in with your Nostr key in the unified client. (The session
    # cookie nostr-login sets is what /admin needs, so admins reach the panel from Settings → Admin.)
    #
    # `next` used to be accepted and DISCARDED, which is half of why /admin was a dead end for anyone
    # without a cookie: it bounced here and here forgot where they had been going.
    nxt = _safe_next(next)
    return RedirectResponse(url="/client" + (f"?next={quote(nxt, safe='')}" if nxt else ""), status_code=302)


@app.get("/admin")
async def admin_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    """The admin panel SHELL. It holds no data — every value in it is fetched from /api/admin/* by
    admin.js — so it is served without a session, and the credentials are checked on those calls.

    That inversion is the point. The panel is framed by the Nostr client, an iframe's document load
    carries only COOKIES, and in a bundled app that cookie is cross-site: SameSite=None, which needs
    Secure, which needs HTTPS. Against a .onion (plain HTTP by design) no cookie can ever be sent, so
    as long as the PAGE was the thing being authorised, the panel could not work there at all — /admin
    saw no session, redirected to the client, and the app rendered the website in the admin pane.

    Now the page arrives unauthenticated and the CLIENT hands it a bearer token over postMessage
    (static/js/admin-auth.js), which is scheme-agnostic. A visitor with no token and no cookie gets the
    same skeleton and a "sign in" message from the page itself — the fields are empty, because they are
    filled by requests that 401.

    A browser visit with a real session still renders exactly as before; the non-admin redirect is kept
    so a signed-in non-admin is not left staring at a panel they cannot use.
    """
    if current_user and not current_user.is_admin:
        return RedirectResponse(url="/", status_code=302)
    # No `user` in the context on purpose: nothing renders it any more, and leaving it available is an
    # invitation to put an identity back into a page that is now served to anyone.
    resp = templates.TemplateResponse("admin.html", {
        "request": request,
        "cache_bust": int(datetime.now().timestamp())
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.get("/sw.js")
async def service_worker():
    """Serve service worker from root for proper PWA scope"""
    sw_path = os.path.join(os.path.dirname(__file__), "..", "static", "sw.js")
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"}
    )


@app.get("/search", response_class=HTMLResponse)
async def browser_search(request: Request, q: str = ""):
    """`https://poster.place/search?q=…` — this node as a browser search engine.

    Serves the client itself rather than redirecting to /client?…: a redirect costs a round trip on
    every search from the URL bar, and the client already reads `view`/`q` off the address bar on
    boot. Paired with /opensearch.xml, which is what lets a browser OFFER to add this as a search
    engine instead of the user typing the %s template in by hand.
    """
    from app.routers.client import client_app
    return await client_app(request)


@app.get("/opensearch.xml")
async def opensearch_descriptor(request: Request):
    """OpenSearch descriptor, linked from the client shell. Chrome/Firefox pick it up on first visit
    and then `poster.place` (or a keyword) in the URL bar searches through this node."""
    base = str(request.base_url).rstrip("/")
    # A public-facing node is behind a reverse proxy; base_url already reflects the forwarded host.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>PosterChan</ShortName>
  <Description>Search the web with PosterChan</Description>
  <InputEncoding>UTF-8</InputEncoding>
  <Image width="16" height="16" type="image/png">{base}/static/favicon.png</Image>
  <Url type="text/html" method="get" template="{base}/search?q={{searchTerms}}"/>
  <moz:SearchForm xmlns:moz="http://www.mozilla.org/2006/browser/search/">{base}/search</moz:SearchForm>
</OpenSearchDescription>
"""
    return Response(content=xml, media_type="application/opensearchdescription+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.json")
async def manifest():
    """Serve manifest from root for PWA"""
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "static", "manifest.json")
    return FileResponse(manifest_path, media_type="application/manifest+json")


@app.get("/status", response_class=HTMLResponse)
async def public_status_page(request: Request):
    """The PUBLIC status page: poster.place/status. No account, no client bundle, no JavaScript.

    Deliberately a top-level route rather than a tab-only view: the audience for a status page is
    people who can't use the app right now, and asking them to load the SPA to find out whether the
    SPA is up defeats the point. Registered before the /{entity} catch-all so /status isn't parsed as
    a Nostr entity. 404s while uptime monitoring is off — nothing to publish yet.
    """
    from app.services import uptime_service, settings_store
    data = await uptime_service.get_status()
    if not data.get("enabled"):
        raise StarletteHTTPException(status_code=404)
    # Both of these are commonly unset on a fresh instance, so fall back to something meaningful
    # rather than to nothing: the app's own icon, and the hostname the visitor actually typed.
    logo = (settings_store.get("site_logo_url", "") or "/static/icon-192.png").strip()
    site = (settings_store.get("site_name", "") or "").strip() or (request.url.hostname or "this server")
    try:
        view = uptime_service.status_view(data)
    except Exception as e:
        # A status page that 500s is the worst possible failure mode for a status page — it's the one
        # thing people load when they already suspect the server is broken. Degrade to the banner.
        logging.warning("[status] could not render monitors: %s", e)
        view = {"ok": False, "empty": True, "banner": "Status is temporarily unavailable",
                "total": 0, "up": 0, "down": 0, "monitors": [], "updated": "just now"}
    return templates.TemplateResponse("status.html", {
        "request": request, "site": site, "logo": logo, "v": view,
    })


@app.get("/status.json")
async def public_status_json():
    """The same status, machine-readable — for anyone else's monitoring, a badge, or a phone widget.
    Same cached payload as /client/uptime (which the in-app Uptime tab uses); this is just the public,
    guessable name for it."""
    from app.services import uptime_service
    data = await uptime_service.get_status()
    if not data.get("enabled"):
        raise StarletteHTTPException(status_code=404)
    return JSONResponse(data)


@app.get("/verify/{token}")
async def verify_email_page(
    token: str,
    response: Response,
    db: Session = Depends(get_db)
):
    """Handle email verification link clicks"""
    # Find the verification token
    verification = db.query(VerificationToken).filter(
        VerificationToken.token == token
    ).first()

    if not verification:
        return HTMLResponse(content="""
        <html>
        <head><title>Verification Failed</title>
        <style>body{font-family:Arial;background:#1a1a2e;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
        .container{text-align:center;background:#16213e;padding:40px;border-radius:12px}
        h1{color:#e74c3c}a{color:#4a9eff}</style></head>
        <body><div class="container">
        <h1>Invalid Token</h1>
        <p>This verification link is invalid or has already been used.</p>
        <p><a href="/login">Go to Login</a></p>
        </div></body></html>
        """, status_code=400)

    # Check if expired
    if verification.expires_at < datetime.utcnow():
        db.delete(verification)
        db.commit()
        return HTMLResponse(content="""
        <html>
        <head><title>Token Expired</title>
        <style>body{font-family:Arial;background:#1a1a2e;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
        .container{text-align:center;background:#16213e;padding:40px;border-radius:12px}
        h1{color:#e74c3c}a{color:#4a9eff}</style></head>
        <body><div class="container">
        <h1>Token Expired</h1>
        <p>This verification link has expired. Please register again.</p>
        <p><a href="/login">Go to Login</a></p>
        </div></body></html>
        """, status_code=400)

    # Get the user
    user = db.query(User).filter(User.id == verification.user_id).first()
    if not user:
        db.delete(verification)
        db.commit()
        return HTMLResponse(content="""
        <html>
        <head><title>User Not Found</title>
        <style>body{font-family:Arial;background:#1a1a2e;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
        .container{text-align:center;background:#16213e;padding:40px;border-radius:12px}
        h1{color:#e74c3c}a{color:#4a9eff}</style></head>
        <body><div class="container">
        <h1>User Not Found</h1>
        <p>The user associated with this verification link no longer exists.</p>
        <p><a href="/login">Go to Login</a></p>
        </div></body></html>
        """, status_code=400)

    # Mark email as verified
    user.email_verified = True
    db.delete(verification)  # Token is single-use
    db.commit()

    # Create access token and set cookie
    access_token = create_access_token({"sub": str(user.id)})

    # Redirect to home with cookie set
    redirect = RedirectResponse(url="/", status_code=302)
    redirect.set_cookie(
        key="access_token",
        value=access_token,
        httponly=False,
        max_age=30 * 24 * 60 * 60,
        samesite="lax",
        path="/"
    )
    return redirect


# ---- shareable Nostr-entity URLs (njump-style: poster.place/<npub|nevent|…>) -----------------
# Registered LAST so this single-segment catch-all only claims paths no explicit route / mounted
# router took first; anything that isn't a Nostr entity returns 404. The client JS reads
# location.pathname and opens the matching profile/thread (see routeFromPath in app.js).
import re as _re_entity  # noqa: E402
_NOSTR_ENTITY_RE = _re_entity.compile(
    r"^(?:npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+$", _re_entity.IGNORECASE)


@app.get("/{entity}", response_class=HTMLResponse)
async def nostr_entity_page(entity: str, request: Request):
    """Serve the Nostr client for /<npub|nprofile|note|nevent|naddr>."""
    if not _NOSTR_ENTITY_RE.match(entity):
        raise StarletteHTTPException(status_code=404)
    from app.routers.client import client_app
    return await client_app(request)


@app.get("/users/{name}", response_class=HTMLResponse)
async def nostr_user_page(name: str, request: Request):
    """Friendly profile URL: poster.place/users/<name> (a NIP-05 local name or npub)."""
    from app.routers.client import client_app
    return await client_app(request)

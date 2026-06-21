from fastapi import FastAPI, Request, Depends, Response
from fastapi import Request as FastAPIRequest
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
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
from app.models import User, VerificationToken, Setting
from app.routers import auth, chat, admin, tts, stt, openai_api, image_api, media_api, news, mail, torrent, storage, files, music_api, video_api
from app.routers import fourchan, youtube_thumb, bots
from app.routers.telegram import router as telegram_router
from app.routers.misskey import router as misskey_router
from app.routers.pleroma import router as pleroma_router
from app.routers.nostr import router as nostr_router
from app.routers.blossom import router as blossom_router
from app.routers.client import router as client_router
from app.routers.matrix import router as matrix_router
from app.services.load_balancer import NoHealthyServersError
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
app.include_router(files.router)  # Register files router first to avoid conflicts
app.include_router(chat.router)
app.include_router(chat.ws_only_router)  # /ws/chat/{id} for clients that omit /api prefix
app.include_router(admin.router)
app.include_router(tts.router)
app.include_router(stt.router)
app.include_router(image_api.router)
app.include_router(music_api.router)
app.include_router(video_api.router)
app.include_router(media_api.router)
app.include_router(news.router)
app.include_router(mail.router)
app.include_router(torrent.router)
app.include_router(fourchan.router)
app.include_router(youtube_thumb.router)
app.include_router(bots.router)
app.include_router(storage.router)
app.include_router(telegram_router)
app.include_router(misskey_router)
app.include_router(pleroma_router)
app.include_router(nostr_router)
app.include_router(blossom_router)
app.include_router(client_router)
app.include_router(matrix_router)
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

        # Turnkey Docker music: when POSTERCHANAI_MUSIC=1 (the compose `music` profile sets this),
        # auto-enable music and point it at the acestep container — only seeding keys the admin
        # hasn't already set, so UI changes win. Lets `docker compose --profile music` generate a
        # song with no manual config.
        if os.environ.get("POSTERCHANAI_MUSIC", "0") == "1":
            try:
                _db = SessionLocal()
                _defaults = {
                    "music_enabled": "true",
                    "music_api_base": os.environ.get("POSTERCHANAI_ACESTEP_URL", "http://acestep:8001"),
                }
                for _k, _v in _defaults.items():
                    if not _db.query(Setting).filter(Setting.key == _k).first():
                        _db.add(Setting(key=_k, value=_v))
                _db.commit()
                _db.close()
                logging.info("Music (ACE-Step) auto-configured from POSTERCHANAI_MUSIC env")
            except Exception as e:
                logging.error(f"Error seeding music settings: {e}")

        # Turnkey Docker video: when POSTERCHANAI_VIDEO=1, auto-enable native text-to-video and
        # (optionally) point video_model at POSTERCHANAI_VIDEO_MODEL. The model auto-downloads to
        # HF_HOME on first use (persisted on the data volume). Only seeds keys the admin hasn't set.
        if os.environ.get("POSTERCHANAI_VIDEO", "0") == "1":
            try:
                _db = SessionLocal()
                _vdefaults = {"video_enabled": "true", "video_local_enabled": "true"}
                _vm = os.environ.get("POSTERCHANAI_VIDEO_MODEL")
                if _vm:
                    _vdefaults["video_model"] = _vm
                for _k, _v in _vdefaults.items():
                    if not _db.query(Setting).filter(Setting.key == _k).first():
                        _db.add(Setting(key=_k, value=_v))
                _db.commit()
                _db.close()
                logging.info("Video generation auto-configured from POSTERCHANAI_VIDEO env")
            except Exception as e:
                logging.error(f"Error seeding video settings: {e}")

        # Turnkey Docker Nostr relay: when POSTERCHANAI_NOSTR_RELAY=1, auto-enable the built-in
        # web-of-trust relay, bind 0.0.0.0 (so the published container port is reachable) and
        # point its snapshot at the data volume. Only seeds keys the admin hasn't set.
        if os.environ.get("POSTERCHANAI_NOSTR_RELAY", "0") == "1":
            try:
                _db = SessionLocal()
                # First-run defaults only (seed if absent) so the Admin UI stays authoritative after.
                # The one-time container upgrade below handles reused volumes whose key is "false".
                _rdefaults = {"nostr_relay_enabled": "true", "nostr_relay_bind": "0.0.0.0"}
                for _k, _v in _rdefaults.items():
                    if not _db.query(Setting).filter(Setting.key == _k).first():
                        _db.add(Setting(key=_k, value=_v))
                _db.commit()
                _db.close()
                logging.info("Nostr relay seeded from POSTERCHANAI_NOSTR_RELAY env")
            except Exception as e:
                logging.error(f"Error seeding Nostr relay settings: {e}")

        # Turnkey Docker Blossom server: when POSTERCHANAI_BLOSSOM=1, ENABLE the built-in Blossom
        # media server. `blossom_enabled` is FORCE-set on every boot — the env flag is a declarative
        # deployment directive, so it must win even on an existing volume where the key is already
        # "false" (otherwise a rebuilt nostr image silently stays "Blossom server disabled"). The
        # other blossom_* config keys are seeded only if absent so admin tuning is preserved. Default
        # backend stays "proxy" (falls back to local on the data volume if no storage server set).
        if os.environ.get("POSTERCHANAI_BLOSSOM", "0") == "1":
            try:
                _db = SessionLocal()
                # First-run defaults only (seed if absent); Admin UI is the source of truth after.
                _bdefaults = {"blossom_enabled": "true", "blossom_storage_path": "/app/data/blossom"}
                for _k, _v in _bdefaults.items():
                    if not _db.query(Setting).filter(Setting.key == _k).first():
                        _db.add(Setting(key=_k, value=_v))
                _db.commit()
                _db.close()
                logging.info("Blossom server seeded from POSTERCHANAI_BLOSSOM env")
            except Exception as e:
                logging.error(f"Error seeding Blossom settings: {e}")

        # One-time container turnkey upgrade. PC_ACCEL is set ONLY in the Docker images (never on
        # bare metal), so this never touches server1/nas. Guarded by a marker so it runs EXACTLY
        # ONCE per volume: it brings a reused/older volume up to the single-node turnkey defaults,
        # then never runs again — so the Admin UI stays the source of truth in production. Opt out
        # of the outbound stack pieces with POSTERCHANAI_{TOR,PROXY,BT}_ENABLED=false.
        if os.environ.get("PC_ACCEL"):
            try:
                _db = SessionLocal()
                _marker = _db.query(Setting).filter(Setting.key == "_turnkey_upgrade_v1").first()
                if not _marker:
                    def _get(k, d=""):
                        r = _db.query(Setting).filter(Setting.key == k).first()
                        return (r.value if r and r.value is not None else d)
                    def _set(k, v):
                        r = _db.query(Setting).filter(Setting.key == k).first()
                        if r:
                            r.value = v
                        else:
                            _db.add(Setting(key=k, value=v))
                    def _set_if_blank(k, v):
                        if not (_get(k).strip()):
                            _set(k, v)
                    def _on(name):  # default ON in containers; opt out with =false/0/no/off
                        return os.environ.get(name, "true").strip().lower() not in ("0", "false", "no", "off")
                    # Outbound Tor -> HTTP proxy -> torrent stack, pre-wired.
                    if _on("POSTERCHANAI_TOR_ENABLED"):
                        _set("tor_enabled", "true")
                    if _on("POSTERCHANAI_PROXY_ENABLED"):
                        _set("proxy_enabled", "true")
                        _set_if_blank("proxy_socks_host", "127.0.0.1")
                    if _on("POSTERCHANAI_BT_ENABLED"):
                        _set("bt_enabled", "true")
                        _set_if_blank("bt_proxy_host", "127.0.0.1")
                    # Single-node Blossom: the storage proxy is multi-node only. With no storage
                    # server set, "proxy" already behaves as local — make it explicit for the UI.
                    if _get("blossom_storage_backend") == "proxy" and not _get("storage_server_url").strip():
                        _set("blossom_storage_backend", "local")
                    # Populate the upstream relay list so the UI shows it (blank still works via the
                    # code default, but a fresh node should display the relays).
                    if not _get("nostr_relay_upstream_relays").strip():
                        from app.services.nostr import DEFAULT_RELAYS
                        _set("nostr_relay_upstream_relays", "\n".join(DEFAULT_RELAYS))
                    # Honour the profile's declared intent ONCE (so a reused nostr volume whose keys
                    # are still "false" gets relay + Blossom on); admin owns them afterward.
                    if os.environ.get("POSTERCHANAI_NOSTR_RELAY", "0") == "1":
                        _set("nostr_relay_enabled", "true")
                    if os.environ.get("POSTERCHANAI_BLOSSOM", "0") == "1":
                        _set("blossom_enabled", "true")
                    _set("_turnkey_upgrade_v1", "done")
                    _db.commit()
                    logging.info("Applied one-time container turnkey upgrade (proxy/tor/torrent, single-node blossom, default relays)")
                _db.close()
            except Exception as e:
                logging.error(f"Error applying container turnkey upgrade: {e}")

        # One-time: enable the timeline backfill sweep (mirror_feeds) on turnkey nodes so a fresh
        # relay populates ~48h of history instead of only filling forward from the firehose. SEPARATE
        # marker from the v1 upgrade so it applies its delta ONCE without re-running (re-forcing) the
        # v1 settings — the Admin UI stays the source of truth (toggle it off in Admin → Relay to
        # stop the sweep). Container-only (PC_ACCEL); bare-metal server1/nas never touched.
        if os.environ.get("PC_ACCEL"):
            try:
                _db = SessionLocal()
                if not _db.query(Setting).filter(Setting.key == "_turnkey_backfill_v1").first():
                    _row = _db.query(Setting).filter(Setting.key == "nostr_relay_mirror_feeds").first()
                    if _row:
                        _row.value = "true"
                    else:
                        _db.add(Setting(key="nostr_relay_mirror_feeds", value="true"))
                    _db.add(Setting(key="_turnkey_backfill_v1", value="done"))
                    _db.commit()
                    logging.info("Enabled timeline backfill (mirror_feeds) for turnkey node")
                _db.close()
            except Exception as e:
                logging.error(f"Error enabling turnkey backfill: {e}")

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
        if app_port == 3051:
            try:
                # Background pollers (fedi-timeline bridge + social/nitter/matrix-notif/logs)
                # run in a SEPARATE worker process so their polling/bridging doesn't contend
                # with the web/API event loop (the bridge could otherwise stall the reactor).
                # They're DB-mediated, so the app's reply/action endpoints keep working.
                from app.worker import start_worker_process
                start_worker_process()
            except Exception as e:
                logging.error(f"Error starting background worker: {e}", exc_info=True)

            try:
                # Start the bot manager (merged ~/posterchan framework; Admin → Bots)
                from app.services.bot_manager_service import start_bot_manager
                start_bot_manager()
            except Exception as e:
                logging.error(f"Error starting bot manager: {e}", exc_info=True)

            try:
                # Start the reminders poller (`remind` command)
                from app.services.reminder_service import start_reminder_scheduler
                start_reminder_scheduler()
            except Exception as e:
                logging.error(f"Error starting reminder scheduler: {e}", exc_info=True)

            try:
                # Resolve/mint the datastore operator key into the keyfile BEFORE the relay starts,
                # so the relay's operator set includes this signer from its first boot. Otherwise a
                # fresh node (no linked users) starts the relay with an empty operator set and then
                # rejects its own settings docs as "not in web of trust".
                from app.services import settings_store as _ss
                _kdb = SessionLocal()
                try:
                    _ss.ensure_operator_key(_kdb)
                finally:
                    _kdb.close()
            except Exception as e:
                logging.warning(f"Could not pre-mint operator key: {e}")

            try:
                # Start the built-in Nostr WoT relay (own thread; no-op unless enabled)
                from app.services.nostr_relay import start_nostr_relay
                start_nostr_relay()
            except Exception as e:
                logging.error(f"Error starting Nostr relay: {e}", exc_info=True)

            try:
                # Settings read-path: hydrate the local Setting cache from the relay (authoritative
                # when settings_backend == relay). Deferred so the relay's WS is up first; no-op
                # otherwise. Runs in the background so startup isn't blocked.
                import asyncio as _aio
                async def _hydrate_settings():
                    await _aio.sleep(6)
                    _db = SessionLocal()
                    try:
                        from app.services import settings_store
                        await settings_store.hydrate(_db)
                    except Exception as e:
                        logging.warning(f"Settings hydrate from relay failed: {e}")
                    try:
                        from app.services import users_store
                        await users_store.hydrate(_db)
                        await users_store.hydrate_user_kv(_db)   # mail/nitter/caldav kv from relay
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

        else:
            logging.info(f"Schedulers disabled on port {app_port} (only run on port 3051)")

        # Auto-start built-in Tor if enabled
        db_tor = SessionLocal()
        try:
            tor_enabled = db_tor.query(Setting).filter(Setting.key == "tor_enabled").first()
            if tor_enabled and tor_enabled.value.lower() == "true":
                def get_tor_setting(key, default=""):
                    s = db_tor.query(Setting).filter(Setting.key == key).first()
                    return s.value if s and s.value else default

                from app.services.tor_service import start_tor_service
                listen_host = get_tor_setting("tor_listen_host", "127.0.0.1")
                socks_port = get_tor_setting("tor_socks_port", "9052")
                control_port = int(get_tor_setting("tor_control_port", "9053"))
                tor_service = start_tor_service(
                    listen_host=listen_host,
                    socks_port=int(socks_port),
                    control_port=control_port,
                    dns_port=int(get_tor_setting("tor_dns_port", str(control_port + 2))),
                    exit_nodes=get_tor_setting("tor_exit_nodes", "{us}"),
                    data_dir=get_tor_setting("tor_data_dir", "/var/lib/posterchanai/tor"),
                )
                if tor_service:
                    logging.info(f"Built-in Tor started (SOCKS5 on {listen_host}:{socks_port})")
                else:
                    logging.error("Failed to start built-in Tor")
        except Exception as e:
            logging.error(f"Failed to start built-in Tor: {e}", exc_info=True)
        finally:
            db_tor.close()

        # Auto-start built-in HTTP proxy if enabled
        db_proxy = SessionLocal()
        try:
            proxy_enabled = db_proxy.query(Setting).filter(Setting.key == "proxy_enabled").first()
            if proxy_enabled and proxy_enabled.value.lower() == "true":
                def get_proxy_setting(key, default=""):
                    s = db_proxy.query(Setting).filter(Setting.key == key).first()
                    return s.value if s and s.value else default

                socks_host = get_proxy_setting("proxy_socks_host")
                if socks_host:
                    # Run the proxy as its OWN process so its asyncio loop gets a dedicated
                    # core and doesn't contend with the app's event loop (all bot/social media
                    # uploads route through it — in-process this pegged a shared core).
                    from app.services.http_proxy_service import start_http_proxy_process
                    start_http_proxy_process(
                        listen_host=get_proxy_setting("proxy_listen_host", "127.0.0.1"),
                        listen_port=int(get_proxy_setting("proxy_listen_port", "8118")),
                        socks_host=socks_host,
                        socks_port=int(get_proxy_setting("proxy_socks_port", "9052")),
                    )
                    logging.info(f"Built-in HTTP proxy (subprocess) started on port {get_proxy_setting('proxy_listen_port', '8118')}")
                else:
                    logging.warning("HTTP proxy enabled but no SOCKS5 target host configured")
        except Exception as e:
            logging.error(f"Failed to start built-in HTTP proxy: {e}", exc_info=True)
        finally:
            db_proxy.close()

        # Auto-start built-in torrent client if enabled (skip if using remote server)
        db3 = SessionLocal()
        try:
            bt_enabled = db3.query(Setting).filter(Setting.key == "bt_enabled").first()
            bt_server_url = db3.query(Setting).filter(Setting.key == "bt_server_url").first()

            # Skip local torrent client if forwarding to remote server
            if bt_server_url and bt_server_url.value:
                logging.info(f"Torrent requests will be forwarded to: {bt_server_url.value}")
            elif bt_enabled and bt_enabled.value.lower() == "true":
                bt_proxy_host = db3.query(Setting).filter(Setting.key == "bt_proxy_host").first()
                if bt_proxy_host and bt_proxy_host.value:
                    def get_bt_setting(key):
                        s = db3.query(Setting).filter(Setting.key == key).first()
                        return s.value if s else None

                    download_dir = get_bt_setting("bt_download_dir") or "/var/lib/posterchanai/torrents"
                    proxy_port = int(get_bt_setting("bt_proxy_port") or "8118")
                    listen_port = int(get_bt_setting("bt_listen_port") or "6881")

                    from app.services.libtorrent_service import LibtorrentService
                    service = LibtorrentService.get_instance(
                        download_dir=download_dir,
                        proxy_host=bt_proxy_host.value,
                        proxy_port=proxy_port,
                        listen_port=listen_port
                    )
                    logging.info(f"Built-in torrent client started")
                else:
                    logging.warning("Built-in torrent client enabled but no proxy host configured")
        except Exception as e:
            logging.error(f"Failed to start built-in torrent client: {e}", exc_info=True)
        finally:
            db3.close()

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
        # matrix-notif/logs pollers all run there now).
        try:
            from app.worker import stop_worker_process
            stop_worker_process()
        except Exception:
            pass

        # Stop the bot manager (terminates all managed bot child processes)
        try:
            from app.services.bot_manager_service import stop_bot_manager
            stop_bot_manager()
        except Exception:
            pass

        # Stop the reminders poller
        try:
            from app.services.reminder_service import stop_reminder_scheduler
            stop_reminder_scheduler()
        except Exception:
            pass

        # Stop the built-in Nostr WoT relay (final snapshot + join its thread)
        try:
            from app.services.nostr_relay import stop_nostr_relay
            stop_nostr_relay()
        except Exception:
            pass

        # Stop the Blossom expiry-cleanup thread
        try:
            from app.services.blossom_service import stop_blossom_cleanup
            stop_blossom_cleanup()
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


@app.get("/login")
async def login_page(request: Request, next: str = None):
    # Old password login UI retired — log in with your Nostr key in the unified client. (The session
    # cookie nostr-login sets is what /admin needs, so admins reach the panel from Settings → Admin.)
    return RedirectResponse(url="/client", status_code=302)


@app.get("/admin")
async def admin_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    if not current_user.is_admin:
        return RedirectResponse(url="/", status_code=302)
    resp = templates.TemplateResponse("admin.html", {
        "request": request,
        "user": current_user,
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


@app.get("/manifest.json")
async def manifest():
    """Serve manifest from root for PWA"""
    manifest_path = os.path.join(os.path.dirname(__file__), "..", "static", "manifest.json")
    return FileResponse(manifest_path, media_type="application/manifest+json")


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

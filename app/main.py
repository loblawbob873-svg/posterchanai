from fastapi import FastAPI, Request, Depends, Response
from fastapi import Request as FastAPIRequest
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
import os
import logging

from app.middleware.csrf import CSRFMiddleware

# Configure logging for console output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)

from app.database import init_db, get_db
from app.auth import get_current_user_optional, create_access_token
from app.models import User, VerificationToken
from app.routers import auth, chat, admin, tts, stt, openai_api, image_api, news, rag, plugins, mail, music, torrent, contacts, notes, storage, files
from app.services.load_balancer import NoHealthyServersError
from fastapi.responses import JSONResponse

# Initialize FastAPI app
app = FastAPI(
    title="Posterchanai",
    description="AI Chat Application",
    version="1.0.0"
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

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: FastAPIRequest, exc: StarletteHTTPException):
    """Ensure HTTP exceptions return JSON instead of HTML"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: FastAPIRequest, exc: RequestValidationError):
    """Ensure validation errors return JSON"""
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: FastAPIRequest, exc: Exception):
    """Catch-all exception handler to ensure JSON responses for API routes"""
    logger = logging.getLogger(__name__)
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    
    error_detail = str(exc)
    
    # Check if this is an API request (starts with /api/)
    if request.url.path.startswith("/api/"):
        # Return JSON error response for API routes
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal server error: {error_detail}"}
        )
    # For non-API requests, re-raise to let FastAPI's default handler deal with it
    # This allows HTML error pages for web pages
    from fastapi.responses import HTMLResponse
    return HTMLResponse(
        status_code=500,
        content=f"<html><body><h1>Internal Server Error</h1><p>{error_detail}</p></body></html>"
    )

# Add CSRF protection middleware
app.add_middleware(CSRFMiddleware)

# Mount static files
static_path = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=static_path), name="static")

# Templates
templates_path = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=templates_path)

# Include routers
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(tts.router)
app.include_router(stt.router)
app.include_router(openai_api.router)
app.include_router(image_api.router)
app.include_router(news.router)
app.include_router(rag.router)
app.include_router(plugins.router)
app.include_router(mail.router)
app.include_router(contacts.router)
app.include_router(music.router)
app.include_router(torrent.router)
app.include_router(notes.router)
app.include_router(storage.router)
app.include_router(files.router)

# Load enabled plugins
from plugins import load_enabled_plugins
load_enabled_plugins(app)


@app.on_event("startup")
async def startup():
    try:
        init_db()

        # Check LLM backend configuration
        from app.database import SessionLocal
        from app.models import Setting
        db = SessionLocal()
        try:
            backend = db.query(Setting).filter(Setting.key == "llm_backend").first()
            backend_type = backend.value if backend else "ollama"

            if backend_type == "ipex":
                # Verify IPEX environment is properly configured
                from app.services.ipex_service import check_xpu_available
                xpu_ok, xpu_msg = check_xpu_available()
                if xpu_ok:
                    logging.info(f"IPEX backend: {xpu_msg}")
                else:
                    logging.warning("=" * 60)
                    logging.warning(f"IPEX BACKEND WARNING: {xpu_msg}")
                    logging.warning("GPU acceleration may not work. Start with ./run-ipex.sh")
                    logging.warning("=" * 60)
        except Exception as e:
            logging.error(f"Error checking LLM backend configuration: {e}", exc_info=True)
        finally:
            db.close()

        # Start health check if enabled (in background, don't block startup)
        try:
            from app.services.health_check import start_health_check
            # Start health check in background thread to avoid blocking startup
            import threading
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
                # Start news scheduler
                from app.services.news_scheduler import start_scheduler
                start_scheduler()
            except Exception as e:
                logging.error(f"Error starting news scheduler: {e}", exc_info=True)

            try:
                # Start plugin schedulers
                from plugins import start_plugin_schedulers
                start_plugin_schedulers()
            except Exception as e:
                logging.error(f"Error starting plugin schedulers: {e}", exc_info=True)

            try:
                # Start Logs scheduler
                from app.services.logs_scheduler import start_logs_scheduler
                start_logs_scheduler()
            except Exception as e:
                logging.error(f"Error starting logs scheduler: {e}", exc_info=True)

            try:
                # Start Schedule (daily calendar summary) scheduler
                from app.services.schedule_scheduler import start_schedule_scheduler
                start_schedule_scheduler()
            except Exception as e:
                logging.error(f"Error starting schedule scheduler: {e}", exc_info=True)
        else:
            logging.info(f"Schedulers disabled on port {app_port} (only run on port 3051)")

        # Auto-warmup RAG cache if enabled (only if MCP server is not handling it)
        db2 = SessionLocal()
        try:
            rag_enabled = db2.query(Setting).filter(Setting.key == "rag_enabled").first()
            rag_auto_warmup = db2.query(Setting).filter(Setting.key == "rag_auto_warmup").first()
            mcp_enabled = db2.query(Setting).filter(Setting.key == "mcp_enabled").first()

            # Only run standalone RAG warmup if MCP is disabled (MCP handles its own warmup)
            if (rag_enabled and rag_enabled.value == "true" and
                (not rag_auto_warmup or rag_auto_warmup.value == "true") and
                (not mcp_enabled or mcp_enabled.value != "true")):
                import threading
                from app.services.rag_warmup import warmup_rag_cache
                logging.info("Starting RAG cache warmup in background...")
                warmup_thread = threading.Thread(target=warmup_rag_cache, daemon=True)
                warmup_thread.start()
        except Exception as e:
            logging.error(f"Error starting RAG warmup: {e}", exc_info=True)
        finally:
            db2.close()

        # Start integrated MCP server if enabled (only on main instance to avoid port conflicts)
        app_port = int(os.environ.get("POSTERCHANAI_PORT", "3051"))
        if app_port == 3051:
            try:
                from app.services.mcp_service import start_mcp_server
                start_mcp_server()
            except Exception as e:
                logging.error(f"Error starting MCP server: {e}", exc_info=True)
        else:
            logging.info(f"MCP server disabled on port {app_port} (only run on port 3051)")

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
                tor_service = start_tor_service(
                    listen_host=listen_host,
                    socks_port=int(socks_port),
                    control_port=int(get_tor_setting("tor_control_port", "9053")),
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
                    from app.services.http_proxy_service import start_http_proxy
                    start_http_proxy(
                        listen_host=get_proxy_setting("proxy_listen_host", "127.0.0.1"),
                        listen_port=int(get_proxy_setting("proxy_listen_port", "8118")),
                        socks_host=socks_host,
                        socks_port=int(get_proxy_setting("proxy_socks_port", "9052")),
                    )
                    logging.info(f"Built-in HTTP proxy started on port {get_proxy_setting('proxy_listen_port', '8118')}")
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

        # Auto-start WebDAV/CalDAV/CardDAV servers if enabled
        db_dav = SessionLocal()
        try:
            def get_dav_setting(key, default=""):
                s = db_dav.query(Setting).filter(Setting.key == key).first()
                return s.value if s and s.value else default
            
            # Get all DAV settings once to avoid duplicate queries
            webdav_enabled = get_dav_setting("webdav_enabled", "false")
            caldav_enabled = get_dav_setting("caldav_enabled", "false")
            cardav_enabled = get_dav_setting("cardav_enabled", "false")
            
            try:
                from app.services.webdav_server import start_webdav_server
            except ImportError as e:
                # Only warn if WebDAV is actually enabled
                if webdav_enabled.lower() == "true":
                    logging.warning(f"WebDAV server is enabled but not available (missing wsgidav?): {e}")
                    logging.warning("Install wsgidav to enable WebDAV: pip install wsgidav>=4.3.0")
                start_webdav_server = None
            try:
                from app.services.caldav_server import start_caldav_server
                from app.services.cardav_server import start_cardav_server
            except ImportError as e:
                # Only warn if CalDAV/CardDAV are enabled
                if caldav_enabled.lower() == "true" or cardav_enabled.lower() == "true":
                    logging.warning(f"CalDAV/CardDAV servers are enabled but not available: {e}")
                start_caldav_server = None
                start_cardav_server = None
            
            # Start WebDAV server
            if start_webdav_server and webdav_enabled.lower() == "true":
                webdav_port = int(get_dav_setting("webdav_port", "8080"))
                if start_webdav_server(db_dav, webdav_port):
                    logging.info(f"Built-in WebDAV server started on port {webdav_port}")
                else:
                    logging.error("Failed to start WebDAV server")
            
            # Start CalDAV server
            if start_caldav_server and caldav_enabled.lower() == "true":
                caldav_port = int(get_dav_setting("caldav_port", "8081"))
                if start_caldav_server(db_dav, caldav_port):
                    logging.info(f"Built-in CalDAV server started on port {caldav_port}")
                else:
                    logging.error("Failed to start CalDAV server")
            
            # Start CardDAV server
            if start_cardav_server and cardav_enabled.lower() == "true":
                cardav_port = int(get_dav_setting("cardav_port", "8082"))
                if start_cardav_server(db_dav, cardav_port):
                    logging.info(f"Built-in CardDAV server started on port {cardav_port}")
                else:
                    logging.error("Failed to start CardDAV server")
        except Exception as e:
            logging.error(f"Failed to start DAV servers: {e}", exc_info=True)
        finally:
            db_dav.close()
        
        logging.info("Application startup complete")
    except Exception as e:
        logging.error(f"CRITICAL: Startup failed with exception: {e}", exc_info=True)
        raise  # Re-raise to let FastAPI handle it properly


@app.on_event("shutdown")
async def shutdown():
    # Stop WebDAV/CalDAV/CardDAV servers
    try:
        try:
            from app.services.webdav_server import stop_webdav_server
            stop_webdav_server()
        except ImportError:
            pass  # wsgidav not installed
        try:
            from app.services.caldav_server import stop_caldav_server
            from app.services.cardav_server import stop_cardav_server
            stop_caldav_server()
            stop_cardav_server()
        except ImportError:
            pass  # Servers not available
    except Exception as e:
        logging.error(f"Error stopping DAV servers: {e}", exc_info=True)
    # Stop health check
    from app.services.health_check import stop_health_check
    stop_health_check()

    # Only stop schedulers on main instance (port 3051)
    app_port = int(os.environ.get("POSTERCHANAI_PORT", "3051"))
    if app_port == 3051:
        # Stop news scheduler
        from app.services.news_scheduler import stop_scheduler
        stop_scheduler()

        # Stop plugin schedulers
        from plugins import stop_plugin_schedulers
        stop_plugin_schedulers()

        # Stop Logs scheduler
        from app.services.logs_scheduler import stop_logs_scheduler
        stop_logs_scheduler()

        # Stop Schedule scheduler
        from app.services.schedule_scheduler import stop_schedule_scheduler
        stop_schedule_scheduler()

    # Stop MCP server
    from app.services.mcp_service import stop_mcp_server
    stop_mcp_server()

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

    # Stop built-in HTTP proxy if running
    try:
        from app.services.http_proxy_service import stop_http_proxy
        stop_http_proxy()
    except Exception as e:
        logging.error(f"Error stopping HTTP proxy: {e}")

    # Stop built-in Tor if running
    try:
        from app.services.tor_service import stop_tor_service
        stop_tor_service()
    except Exception as e:
        logging.error(f"Error stopping Tor: {e}")


@app.get("/")
async def index(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    if not current_user:
        # Preserve ?q= search parameter through login
        query_string = str(request.query_params)
        if query_string:
            return RedirectResponse(url=f"/login?next=/?{query_string}", status_code=302)
        return RedirectResponse(url="/login", status_code=302)
    resp = templates.TemplateResponse("index.html", {
        "request": request,
        "user": current_user
    })
    # Prevent caching so back button after logout doesn't show cached page
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/login")
async def login_page(
    request: Request,
    next: str = None,
    current_user: User = Depends(get_current_user_optional)
):
    if current_user:
        # Redirect to next URL if provided, otherwise home
        return RedirectResponse(url=next or "/", status_code=302)
    resp = templates.TemplateResponse("login.html", {
        "request": request,
        "next": next or "/"
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


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

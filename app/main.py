from fastapi import FastAPI, Request, Depends, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
import os
import logging

# Configure logging for console output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)

from app.database import init_db, get_db
from app.auth import get_current_user_optional, create_access_token
from app.models import User, VerificationToken
from app.routers import auth, chat, admin, tts, openai_api, image_api, news, rag, plugins

# Initialize FastAPI app
app = FastAPI(
    title="Posterchanai",
    description="AI Chat Application",
    version="1.0.0"
)

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
app.include_router(openai_api.router)
app.include_router(image_api.router)
app.include_router(news.router)
app.include_router(rag.router)
app.include_router(plugins.router)


@app.on_event("startup")
async def startup():
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
    finally:
        db.close()

    # Start health check if enabled
    from app.services.health_check import start_health_check
    start_health_check()
    # Start news scheduler
    from app.services.news_scheduler import start_scheduler
    start_scheduler()

    # Auto-warmup RAG cache if enabled
    db2 = SessionLocal()
    try:
        rag_enabled = db2.query(Setting).filter(Setting.key == "rag_enabled").first()
        rag_auto_warmup = db2.query(Setting).filter(Setting.key == "rag_auto_warmup").first()

        if (rag_enabled and rag_enabled.value == "true" and
            (not rag_auto_warmup or rag_auto_warmup.value == "true")):
            import threading
            from app.services.rag_warmup import warmup_rag_cache
            logging.info("Starting RAG cache warmup in background...")
            warmup_thread = threading.Thread(target=warmup_rag_cache, daemon=True)
            warmup_thread.start()
    finally:
        db2.close()


@app.on_event("shutdown")
async def shutdown():
    # Stop health check
    from app.services.health_check import stop_health_check
    stop_health_check()
    # Stop news scheduler
    from app.services.news_scheduler import stop_scheduler
    stop_scheduler()


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
        "user": current_user
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

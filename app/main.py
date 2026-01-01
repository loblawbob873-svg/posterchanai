from fastapi import FastAPI, Request, Depends, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import os

from app.database import init_db, get_db
from app.auth import get_current_user_optional
from app.models import User
from app.routers import auth, chat, admin, tts, openai_api

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


@app.on_event("startup")
async def startup():
    init_db()
    # Start health check if enabled
    from app.services.health_check import start_health_check
    start_health_check()


@app.on_event("shutdown")
async def shutdown():
    # Stop health check
    from app.services.health_check import stop_health_check
    stop_health_check()


@app.get("/")
async def index(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    if not current_user:
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
    current_user: User = Depends(get_current_user_optional)
):
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    resp = templates.TemplateResponse("login.html", {"request": request})
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

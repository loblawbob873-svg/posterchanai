from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import os

from app.database import init_db, get_db
from app.auth import get_current_user_optional
from app.models import User
from app.routers import auth, chat, admin, tts

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


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
async def index(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
):
    if not current_user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": current_user
    })


@app.get("/login")
async def login_page(
    request: Request,
    current_user: User = Depends(get_current_user_optional)
):
    if current_user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


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
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "user": current_user
    })

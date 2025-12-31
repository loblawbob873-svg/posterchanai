from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./posterchanai.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app.models import User, Conversation, Message, Setting
    Base.metadata.create_all(bind=engine)

    # Create default settings if not exist
    db = SessionLocal()
    try:
        default_settings = {
            "openwebui_url": "https://ai.poster.place",
            "openwebui_api_key": "sk-cc101c9fbdbe4781b07fe2b290e95928",
            "openwebui_model": "richardyoung/qwen3-14b-abliterated:Q5_K_M",
            "openwebui_timeout": "60000",
            "comfyui_url": "http://192.168.0.85:8188",
            "comfyui_default_model": "halcyonSDXL_v19.safetensors",
            "comfyui_anime_model": "nova3DCGXL_ilV80.safetensors",
            "comfyui_timeout": "300000",
            "searxng_url": "https://search.poster.place",
            "tts_voice": "zh-CN-XiaoxiaoNeural",
            "tts_rate": "+5%",
            "tts_pitch": "+10Hz",
        }

        for key, value in default_settings.items():
            existing = db.query(Setting).filter(Setting.key == key).first()
            if not existing:
                db.add(Setting(key=key, value=value))

        # Create default admin user if no users exist
        from app.auth import get_password_hash
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                password_hash=get_password_hash("admin"),
                is_admin=True
            )
            db.add(admin)

        db.commit()
    finally:
        db.close()

import os
import shutil
import base64
from pathlib import Path
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import Setting


class StorageService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.upload_path = settings.get("upload_path", "/var/lib/posterchanai")

    def get_user_path(self, username: str) -> Path:
        """Get the upload directory for a user"""
        user_path = Path(self.upload_path) / username
        user_path.mkdir(parents=True, exist_ok=True)
        return user_path

    def get_conversation_path(self, username: str, conversation_id: int) -> Path:
        """Get the upload directory for a specific conversation"""
        conv_path = self.get_user_path(username) / str(conversation_id)
        conv_path.mkdir(parents=True, exist_ok=True)
        return conv_path

    def save_image(self, username: str, conversation_id: int, image_base64: str, prefix: str = "img") -> str:
        """Save a base64 image to disk and return the file path"""
        conv_path = self.get_conversation_path(username, conversation_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{prefix}_{timestamp}.png"
        filepath = conv_path / filename

        image_data = base64.b64decode(image_base64)
        with open(filepath, "wb") as f:
            f.write(image_data)

        return str(filepath)

    def save_avatar(self, username: str, image_data: bytes, ext: str = ".png") -> str:
        """Save user avatar image and return the filename"""
        user_path = self.get_user_path(username)
        filename = f"avatar{ext}"
        filepath = user_path / filename

        # Delete old avatar if exists (any extension)
        for old_file in user_path.glob("avatar.*"):
            old_file.unlink()

        with open(filepath, "wb") as f:
            f.write(image_data)

        return filename

    def get_avatar_path(self, username: str) -> Path | None:
        """Get path to user's avatar if it exists"""
        user_path = Path(self.upload_path) / username
        for avatar_file in user_path.glob("avatar.*"):
            return avatar_file
        return None

    def save_file(self, username: str, conversation_id: int, content: str, original_name: str = "file.txt") -> str:
        """Save a text file to disk and return the file path"""
        conv_path = self.get_conversation_path(username, conversation_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # Keep extension from original name
        ext = Path(original_name).suffix or ".txt"
        filename = f"file_{timestamp}{ext}"
        filepath = conv_path / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return str(filepath)

    def save_raw_file(self, username: str, conversation_id: int, data: bytes, original_name: str) -> str:
        """Save raw file bytes to disk and return the file path"""
        conv_path = self.get_conversation_path(username, conversation_id)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext = Path(original_name).suffix or ""
        safe_name = "".join(c for c in Path(original_name).stem if c.isalnum() or c in "-_")[:30]
        filename = f"{safe_name}_{timestamp}{ext}"
        filepath = conv_path / filename

        with open(filepath, "wb") as f:
            f.write(data)

        return str(filepath)

    def get_relative_path(self, full_path: str, username: str) -> str:
        """Get relative path for API response (from upload_path)"""
        return str(Path(full_path).relative_to(self.upload_path))

    def delete_conversation_files(self, username: str, conversation_id: int) -> bool:
        """Delete all files for a conversation"""
        conv_path = Path(self.upload_path) / username / str(conversation_id)
        if conv_path.exists():
            shutil.rmtree(conv_path)
            return True
        return False

    def delete_user_files(self, username: str) -> bool:
        """Delete all files for a user"""
        user_path = Path(self.upload_path) / username
        if user_path.exists():
            shutil.rmtree(user_path)
            return True
        return False

    def get_file_count(self, username: str, conversation_id: int = None) -> int:
        """Count files for a user or specific conversation"""
        if conversation_id:
            target_path = Path(self.upload_path) / username / str(conversation_id)
        else:
            target_path = Path(self.upload_path) / username

        if not target_path.exists():
            return 0

        count = 0
        for root, dirs, files in os.walk(target_path):
            count += len(files)
        return count


def get_storage_service(db: Session) -> StorageService:
    return StorageService(db)

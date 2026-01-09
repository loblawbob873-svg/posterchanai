"""
Folder Indexer for RAG.
Indexes uploaded folder contents or local directories.
"""
import os
import logging
from pathlib import Path
from typing import Optional, List, Callable
from fnmatch import fnmatch
from datetime import datetime
from sqlalchemy.orm import Session

from app.models import RAGCollection, Setting
from app.services.rag_service import get_rag_service

logger = logging.getLogger(__name__)


class FolderIndexer:
    """Indexes folders and uploaded files."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self.rag_service = get_rag_service(db, user_id)
        self._load_settings()

    def _load_settings(self):
        """Load settings from database."""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.upload_path = settings.get("upload_path", "/var/lib/posterchanai")
        # File size limits in bytes (stored as MB in settings)
        self.max_file_size = int(settings.get("rag_max_file_size", "1")) * 1_000_000
        self.max_log_size = int(settings.get("rag_max_log_size", "100")) * 1_000_000

    def get_upload_folder(self, collection_id: int) -> Path:
        """Get the upload folder path for a collection."""
        return Path(self.upload_path) / "rag" / str(self.user_id) / str(collection_id)

    def index_folder(
        self,
        collection: RAGCollection,
        progress_callback: Optional[Callable[[str, str], None]] = None
    ) -> int:
        """
        Index contents of a local folder.

        Args:
            collection: RAGCollection with folder path as source_path
            progress_callback: Optional callback(stage, message) for progress updates

        Returns:
            Total number of chunks indexed
        """
        folder_path = Path(collection.source_path)
        if not folder_path.exists():
            raise FileNotFoundError(f"Folder not found: {folder_path}")

        patterns = [p.strip() for p in collection.file_patterns.split(",")]

        # Find matching files
        files = self._find_matching_files(folder_path, patterns)
        logger.info(f"Found {len(files)} files matching patterns in {folder_path}")

        if progress_callback:
            progress_callback("indexing", f"Found {len(files)} files to index")

        total_chunks = 0
        for i, file_path in enumerate(files):
            if progress_callback:
                progress_callback("indexing", f"Indexing {i+1}/{len(files)}: {file_path.name}")

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                # Use configurable file size limits
                max_size = self.max_log_size if file_path.suffix == '.log' else self.max_file_size
                if len(content) > max_size:
                    logger.warning(f"Skipping large file: {file_path} ({len(content)} bytes, limit: {max_size})")
                    continue

                rel_path = str(file_path.relative_to(folder_path))
                chunks = self.rag_service.index_file(collection.id, rel_path, content)
                total_chunks += chunks
            except Exception as e:
                logger.warning(f"Failed to index {file_path}: {e}")

        # Update collection metadata
        collection.last_indexed_at = datetime.utcnow()
        collection.document_count = len(files)
        self.db.commit()

        logger.info(f"Indexed {total_chunks} total chunks from {len(files)} files")
        return total_chunks

    def index_uploaded_files(
        self,
        collection: RAGCollection,
        files: List[dict],  # [{"filename": str, "content": str}, ...]
        progress_callback: Optional[Callable[[str, str], None]] = None
    ) -> int:
        """
        Index uploaded files from a zip or direct upload.

        Args:
            collection: RAGCollection to index into
            files: List of dicts with filename and content
            progress_callback: Optional callback(stage, message) for progress updates

        Returns:
            Total number of chunks indexed
        """
        if progress_callback:
            progress_callback("indexing", f"Indexing {len(files)} uploaded files")

        total_chunks = 0

        for i, file_info in enumerate(files):
            if progress_callback:
                progress_callback("indexing", f"Indexing {i+1}/{len(files)}: {file_info['filename']}")

            try:
                content = file_info["content"]
                # Use configurable file size limits
                filename = file_info["filename"]
                is_log = filename.endswith('.log')
                max_size = self.max_log_size if is_log else self.max_file_size
                if len(content) > max_size:
                    logger.warning(f"Skipping large file: {filename} ({len(content)} bytes, limit: {max_size})")
                    continue

                chunks = self.rag_service.index_file(
                    collection.id,
                    file_info["filename"],
                    content
                )
                total_chunks += chunks
            except Exception as e:
                logger.warning(f"Failed to index {file_info['filename']}: {e}")

        # Update collection metadata
        collection.last_indexed_at = datetime.utcnow()
        collection.document_count = len(files)
        self.db.commit()

        logger.info(f"Indexed {total_chunks} total chunks from {len(files)} uploaded files")
        return total_chunks

    def _find_matching_files(self, root_dir: Path, patterns: List[str]) -> List[Path]:
        """Find all files matching the given patterns."""
        matches = []

        # Directories to skip
        skip_dirs = {
            '.git', 'node_modules', '__pycache__', 'venv', '.venv',
            'dist', 'build', '.next', '.nuxt', 'target', 'vendor',
            '.idea', '.vscode', 'coverage', '.cache', '.tox'
        }

        for file_path in root_dir.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(root_dir)
                parts = rel_path.parts

                # Skip hidden files and excluded directories
                if any(part.startswith('.') for part in parts):
                    continue
                if any(part in skip_dirs for part in parts):
                    continue
                # Skip venv-* directories (venv-ipex, venv-xpu, etc.)
                if any(part.startswith('venv') for part in parts):
                    continue

                for pattern in patterns:
                    if fnmatch(file_path.name, pattern):
                        matches.append(file_path)
                        break

        return matches


def get_folder_indexer(db: Session, user_id: int) -> FolderIndexer:
    """Get folder indexer instance."""
    return FolderIndexer(db, user_id)

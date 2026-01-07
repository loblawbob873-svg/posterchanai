"""
Git Repository Indexer for RAG.
Clones repositories and indexes their contents.
"""
import os
import shutil
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import Optional, List, Callable
from fnmatch import fnmatch
from datetime import datetime
from sqlalchemy.orm import Session

from app.models import RAGCollection
from app.services.rag_service import get_rag_service

logger = logging.getLogger(__name__)


class GitIndexer:
    """Clones and indexes git repositories."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self.rag_service = get_rag_service(db, user_id)

    def clone_and_index(
        self,
        collection: RAGCollection,
        progress_callback: Optional[Callable[[str, str], None]] = None
    ) -> int:
        """
        Clone a git repository and index its contents.

        Args:
            collection: RAGCollection with git_url as source_path
            progress_callback: Optional callback(stage, message) for progress updates

        Returns:
            Total number of chunks indexed
        """
        git_url = collection.source_path
        branch = collection.git_branch or "main"
        patterns = [p.strip() for p in collection.file_patterns.split(",")]

        # Create temp directory for clone
        temp_dir = tempfile.mkdtemp(prefix="rag_git_")

        try:
            # Clone repository
            logger.info(f"Cloning {git_url} (branch: {branch})")
            if progress_callback:
                progress_callback("cloning", f"Cloning repository...")

            result = subprocess.run(
                ["git", "clone", "--depth=1", "--branch", branch, git_url, temp_dir],
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )

            if result.returncode != 0:
                # Try without branch specification (some repos use 'master')
                result = subprocess.run(
                    ["git", "clone", "--depth=1", git_url, temp_dir],
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                if result.returncode != 0:
                    raise Exception(f"Git clone failed: {result.stderr}")

            # Find matching files
            files = self._find_matching_files(temp_dir, patterns)
            logger.info(f"Found {len(files)} files matching patterns")

            if progress_callback:
                progress_callback("indexing", f"Found {len(files)} files to index")

            total_chunks = 0
            for i, file_path in enumerate(files):
                if progress_callback:
                    progress_callback("indexing", f"Indexing {i+1}/{len(files)}: {file_path.name}")

                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    # Skip very large files (>1MB)
                    if len(content) > 1_000_000:
                        logger.warning(f"Skipping large file: {file_path}")
                        continue

                    # Use relative path from repo root
                    rel_path = str(file_path.relative_to(temp_dir))
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

        finally:
            # Cleanup temp directory
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _find_matching_files(self, root_dir: str, patterns: List[str]) -> List[Path]:
        """Find all files matching the given patterns."""
        root = Path(root_dir)
        matches = []

        # Directories to skip
        skip_dirs = {
            '.git', 'node_modules', '__pycache__', 'venv', '.venv',
            'dist', 'build', '.next', '.nuxt', 'target', 'vendor',
            '.idea', '.vscode', 'coverage', '.cache', '.tox'
        }

        # Walk directory tree
        for file_path in root.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(root)
                parts = rel_path.parts

                # Skip hidden files and excluded directories
                if any(part.startswith('.') for part in parts):
                    continue
                if any(part in skip_dirs for part in parts):
                    continue

                # Check if matches any pattern
                for pattern in patterns:
                    if fnmatch(file_path.name, pattern):
                        matches.append(file_path)
                        break

        return matches


def get_git_indexer(db: Session, user_id: int) -> GitIndexer:
    """Get git indexer instance."""
    return GitIndexer(db, user_id)

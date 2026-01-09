"""
Git Repository Indexer for RAG.
Clones repositories and indexes their contents.
"""
import shutil
import tempfile
import subprocess
import logging
from pathlib import Path
from typing import Optional, List, Callable
from fnmatch import fnmatch
from datetime import datetime
from sqlalchemy.orm import Session

from app.models import RAGCollection, Setting
from app.services.rag_service import get_rag_service

logger = logging.getLogger(__name__)

# Default persistent directory for git clones
DEFAULT_GIT_REPOS_PATH = "./data/git_repos"


class GitIndexer:
    """Clones and indexes git repositories."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self.rag_service = get_rag_service(db, user_id)

        # Get persistent repos path from settings or use default
        setting = db.query(Setting).filter(Setting.key == "rag_git_repos_path").first()
        self.repos_path = Path(setting.value if setting else DEFAULT_GIT_REPOS_PATH)
        self.repos_path.mkdir(parents=True, exist_ok=True)

    def _get_repo_path(self, collection_id: int) -> Path:
        """Get persistent path for a collection's git repo."""
        return self.repos_path / f"collection_{collection_id}"

    def pull_and_index(
        self,
        collection: RAGCollection,
        progress_callback: Optional[Callable[[str, str], None]] = None
    ) -> int:
        """
        Pull latest changes from git repository and re-index.
        If repo doesn't exist locally, does a fresh clone.

        Args:
            collection: RAGCollection with git_url as source_path
            progress_callback: Optional callback(stage, message) for progress updates

        Returns:
            Total number of chunks indexed
        """
        git_url = collection.source_path
        branch = collection.git_branch or "main"
        patterns = [p.strip() for p in collection.file_patterns.split(",")]
        repo_path = self._get_repo_path(collection.id)

        try:
            if repo_path.exists() and (repo_path / ".git").exists():
                # Existing repo - do git pull
                logger.info(f"Pulling latest changes for {collection.name}")
                if progress_callback:
                    progress_callback("pulling", "Pulling latest changes...")

                # Fetch and reset to handle force pushes
                result = subprocess.run(
                    ["git", "fetch", "origin", branch],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                if result.returncode != 0:
                    logger.warning(f"Git fetch failed: {result.stderr}")

                result = subprocess.run(
                    ["git", "reset", "--hard", f"origin/{branch}"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if result.returncode != 0:
                    raise Exception(f"Git reset failed: {result.stderr}")

                if progress_callback:
                    progress_callback("pulled", "Pull complete, indexing...")
            else:
                # No existing repo - do fresh clone
                logger.info(f"Cloning {git_url} to persistent location")
                if progress_callback:
                    progress_callback("cloning", "Cloning repository...")

                # Remove any partial clone
                if repo_path.exists():
                    shutil.rmtree(repo_path)

                result = subprocess.run(
                    ["git", "clone", "--branch", branch, git_url, str(repo_path)],
                    capture_output=True,
                    text=True,
                    timeout=600
                )

                if result.returncode != 0:
                    # Try without branch specification
                    result = subprocess.run(
                        ["git", "clone", git_url, str(repo_path)],
                        capture_output=True,
                        text=True,
                        timeout=600
                    )
                    if result.returncode != 0:
                        raise Exception(f"Git clone failed: {result.stderr}")

            # Index the files
            return self._index_directory(collection, repo_path, patterns, progress_callback)

        except Exception as e:
            logger.error(f"Pull and index failed: {e}")
            raise

    def clone_and_index(
        self,
        collection: RAGCollection,
        progress_callback: Optional[Callable[[str, str], None]] = None
    ) -> int:
        """
        Clone a git repository (shallow) and index its contents.
        Uses a temp directory that's cleaned up after indexing.

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
            # Clone repository (shallow)
            logger.info(f"Cloning {git_url} (branch: {branch})")
            if progress_callback:
                progress_callback("cloning", "Cloning repository...")

            result = subprocess.run(
                ["git", "clone", "--depth=1", "--branch", branch, git_url, temp_dir],
                capture_output=True,
                text=True,
                timeout=300
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

            # Index using shared helper
            return self._index_directory(collection, Path(temp_dir), patterns, progress_callback)

        finally:
            # Cleanup temp directory
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _index_directory(
        self,
        collection: RAGCollection,
        repo_path: Path,
        patterns: List[str],
        progress_callback: Optional[Callable[[str, str], None]] = None
    ) -> int:
        """Index all matching files in a directory."""
        files = self._find_matching_files(str(repo_path), patterns)
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
                rel_path = str(file_path.relative_to(repo_path))
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

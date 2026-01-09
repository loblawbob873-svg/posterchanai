"""
Embedding Service - Local sentence-transformers for RAG.
Provides text embeddings using HuggingFace sentence-transformers.
No external API dependencies - fully self-contained.
"""
import os
import time
import logging
import hashlib
from functools import lru_cache
from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from app.models import Setting

logger = logging.getLogger(__name__)

# Set threading for CPU parallelism before importing torch
# These environment variables must be set before torch is imported
_num_threads = os.environ.get("RAG_NUM_THREADS", "")
if _num_threads:
    os.environ.setdefault("OMP_NUM_THREADS", _num_threads)
    os.environ.setdefault("MKL_NUM_THREADS", _num_threads)

# Singleton model instance (shared across all service instances)
_model = None
_model_name = None

# Global settings cache to avoid repeated DB queries
_settings_cache = {}
_settings_cache_time = 0
_SETTINGS_TTL = 3600  # 1 hour (settings rarely change)

# LRU cache for embeddings - LARGE cache for aggressive RAM usage
# Each embedding is ~1.5KB (384 floats * 4 bytes), so 250k entries ≈ 375MB
_embedding_cache = {}
_EMBEDDING_CACHE_MAX = 250000  # 250k cached embeddings (default)


class EmbeddingService:
    """Local sentence-transformers embedding service."""

    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        """Load embedding settings from database with caching."""
        global _settings_cache, _settings_cache_time

        current_time = time.time()
        if _settings_cache and (current_time - _settings_cache_time) < _SETTINGS_TTL:
            settings = _settings_cache
        else:
            settings = {s.key: s.value for s in self.db.query(Setting).all()}
            _settings_cache = settings
            _settings_cache_time = current_time

        self.model_name = settings.get("rag_embedding_model", "all-MiniLM-L6-v2")
        self.batch_size = int(settings.get("rag_embedding_batch_size", "64"))
        self.num_threads = int(settings.get("rag_num_threads", "0"))  # 0 = auto
        self.embedding_cache_max = int(settings.get("rag_embedding_cache_max", "250000"))

    def _ensure_model_loaded(self):
        """Lazy load the model on first use."""
        global _model, _model_name

        # Check if we need to load/reload the model
        if _model is None or _model_name != self.model_name:
            try:
                import torch
                from sentence_transformers import SentenceTransformer

                # Set thread count for CPU parallelism
                if self.num_threads > 0:
                    torch.set_num_threads(self.num_threads)
                    logger.info(f"Set torch threads to {self.num_threads}")
                else:
                    # Auto-detect: use all available cores
                    import multiprocessing
                    cpu_count = multiprocessing.cpu_count()
                    torch.set_num_threads(cpu_count)
                    logger.info(f"Auto-set torch threads to {cpu_count}")

                logger.info(f"Loading sentence-transformers model: {self.model_name}")
                # Set clean_up_tokenization_spaces explicitly to avoid FutureWarning
                _model = SentenceTransformer(
                    self.model_name,
                    tokenizer_kwargs={'clean_up_tokenization_spaces': True}
                )
                _model_name = self.model_name
                logger.info(f"Model loaded, embedding dimension: {_model.get_sentence_embedding_dimension()}")
            except ImportError:
                logger.error("sentence-transformers not installed. Run: pip install sentence-transformers")
                raise RuntimeError("sentence-transformers not installed")
            except Exception as e:
                logger.error(f"Failed to load embedding model: {e}")
                raise

    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for a list of texts.

        Args:
            texts: List of strings to embed

        Returns:
            List of embedding vectors (each is a list of floats)
        """
        if not texts:
            return []

        self._ensure_model_loaded()

        try:
            total = len(texts)
            logger.info(f"[EMBED] Starting embedding generation for {total} texts (batch_size={self.batch_size})")

            # For very large batches, process in chunks and log progress
            if total > 1000:
                all_embeddings = []
                chunk_size = 1000  # Process 1000 at a time for progress logging
                for i in range(0, total, chunk_size):
                    chunk = texts[i:i + chunk_size]
                    logger.info(f"[EMBED] Processing texts {i+1}-{min(i+len(chunk), total)} of {total} ({100*i//total}%)")
                    chunk_embeddings = _model.encode(
                        chunk,
                        convert_to_numpy=True,
                        batch_size=self.batch_size,
                        show_progress_bar=False
                    )
                    all_embeddings.extend(chunk_embeddings.tolist())
                logger.info(f"[EMBED] Completed all {total} embeddings")
                return all_embeddings
            else:
                embeddings = _model.encode(
                    texts,
                    convert_to_numpy=True,
                    batch_size=self.batch_size,
                    show_progress_bar=False
                )
                logger.info(f"[EMBED] Completed {total} embeddings")
                return embeddings.tolist()
        except Exception as e:
            logger.error(f"[EMBED] Embedding generation failed: {e}")
            raise

    def embed_single(self, text: str, use_cache: bool = True) -> List[float]:
        """
        Generate embedding for a single text with caching.

        Args:
            text: String to embed
            use_cache: Whether to use the embedding cache (default True)

        Returns:
            Embedding vector as list of floats
        """
        global _embedding_cache

        if use_cache:
            # Create cache key from text hash + model name
            cache_key = hashlib.md5(f"{text}:{self.model_name}".encode()).hexdigest()

            if cache_key in _embedding_cache:
                logger.debug(f"[EMBED] Cache hit for query")
                return _embedding_cache[cache_key]

            # Generate embedding
            embedding = self.embed([text])[0]

            # Add to cache (with size limit from settings)
            cache_max = self.embedding_cache_max
            if len(_embedding_cache) >= cache_max:
                # Remove oldest entries (first 10%)
                keys_to_remove = list(_embedding_cache.keys())[:cache_max // 10]
                for k in keys_to_remove:
                    del _embedding_cache[k]

            _embedding_cache[cache_key] = embedding
            return embedding

        return self.embed([text])[0]

    @property
    def dimension(self) -> int:
        """Get embedding dimension."""
        self._ensure_model_loaded()
        return _model.get_sentence_embedding_dimension()

    def get_status(self) -> dict:
        """Get embedding service status."""
        global _model, _model_name
        return {
            "model": self.model_name,
            "loaded": _model is not None and _model_name == self.model_name,
            "dimension": _model.get_sentence_embedding_dimension() if _model else None
        }


def get_embedding_service(db: Session) -> EmbeddingService:
    """Get embedding service instance."""
    return EmbeddingService(db)


def unload_model():
    """Unload the embedding model to free memory."""
    global _model, _model_name
    if _model is not None:
        del _model
        _model = None
        _model_name = None
        logger.info("Embedding model unloaded")


def reload_embedding_model(db: Session):
    """Reload the embedding model with current settings (useful after settings change)."""
    global _model, _model_name, _settings_cache, _settings_cache_time, _embedding_cache

    # Unload existing model
    if _model is not None:
        logger.info("Reloading embedding model...")
        del _model
        _model = None
        _model_name = None

    # Clear caches
    _settings_cache = {}
    _settings_cache_time = 0
    _embedding_cache = {}

    # Load fresh settings and reinitialize
    service = EmbeddingService(db)
    service._ensure_model_loaded()
    logger.info("Embedding model reloaded with new settings")


def clear_embedding_cache():
    """Clear the embedding cache."""
    global _embedding_cache
    _embedding_cache = {}
    logger.info("Embedding cache cleared")


def get_cache_stats() -> dict:
    """Get embedding cache statistics."""
    # Get max from settings if available
    cache_max = _settings_cache.get("rag_embedding_cache_max", "50000") if _settings_cache else "50000"
    return {
        "embedding_cache_size": len(_embedding_cache),
        "embedding_cache_max": int(cache_max),
        "settings_cache_age_seconds": time.time() - _settings_cache_time if _settings_cache_time else None,
        "settings_ttl": _SETTINGS_TTL
    }

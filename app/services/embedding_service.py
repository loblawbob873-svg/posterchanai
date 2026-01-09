"""
Embedding Service - Local sentence-transformers for RAG.
Provides text embeddings using HuggingFace sentence-transformers.
No external API dependencies - fully self-contained.
"""
import os
import logging
from typing import List, Optional
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


class EmbeddingService:
    """Local sentence-transformers embedding service."""

    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        """Load embedding settings from database."""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.model_name = settings.get("rag_embedding_model", "all-MiniLM-L6-v2")
        self.batch_size = int(settings.get("rag_embedding_batch_size", "64"))
        self.num_threads = int(settings.get("rag_num_threads", "0"))  # 0 = auto

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

    def embed_single(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: String to embed

        Returns:
            Embedding vector as list of floats
        """
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
    global _model, _model_name

    # Unload existing model
    if _model is not None:
        logger.info("Reloading embedding model...")
        del _model
        _model = None
        _model_name = None

    # Load fresh settings and reinitialize
    service = EmbeddingService(db)
    service._ensure_model_loaded()
    logger.info("Embedding model reloaded with new settings")

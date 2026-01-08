"""
RAG Service - Core RAG functionality with ChromaDB.
Handles document indexing, code-aware chunking, and retrieval.
"""
import os
import re
import hashlib
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session

from app.models import Setting, RAGCollection, RAGDocument
from app.services.embedding_service import get_embedding_service

logger = logging.getLogger(__name__)

# Singleton ChromaDB client
_chroma_client = None


class RAGService:
    """Core RAG service with ChromaDB vector store."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self._load_settings()
        self._ensure_chroma_client()

    def _load_settings(self):
        """Load RAG settings from database."""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}
        self.chromadb_path = settings.get("rag_chromadb_path", "./data/chromadb")
        self.chunk_size = int(settings.get("rag_chunk_size", "1000"))
        self.chunk_overlap = int(settings.get("rag_chunk_overlap", "200"))
        self.top_k = int(settings.get("rag_top_k", "5"))
        self.min_similarity = float(settings.get("rag_min_similarity", "0.3"))

    def _ensure_chroma_client(self):
        """Get or create ChromaDB client."""
        global _chroma_client

        if _chroma_client is None:
            try:
                import chromadb
                from chromadb.config import Settings as ChromaSettings

                os.makedirs(self.chromadb_path, exist_ok=True)
                _chroma_client = chromadb.PersistentClient(
                    path=self.chromadb_path,
                    settings=ChromaSettings(anonymized_telemetry=False)
                )
                logger.info(f"ChromaDB initialized at {self.chromadb_path}")
            except ImportError:
                logger.error("chromadb not installed. Run: pip install chromadb")
                raise RuntimeError("chromadb not installed")

        self.client = _chroma_client

    def _get_collection_name(self, collection_id: int) -> str:
        """Generate unique ChromaDB collection name."""
        return f"user_{self.user_id}_collection_{collection_id}"

    def _get_or_create_chroma_collection(self, collection_id: int):
        """Get or create a ChromaDB collection."""
        name = self._get_collection_name(collection_id)
        return self.client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"}
        )

    # ----- Code-Aware Chunking -----

    def _chunk_content(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """
        Chunk content based on file type.
        Respects language boundaries for code files.
        """
        ext = Path(file_path).suffix.lower()

        # Language-specific chunking
        if ext == '.py':
            return self._chunk_python(content, file_path)
        elif ext in ['.js', '.jsx']:
            return self._chunk_javascript(content, file_path)
        elif ext in ['.ts', '.tsx']:
            return self._chunk_typescript(content, file_path)
        elif ext == '.go':
            return self._chunk_go(content, file_path)
        elif ext == '.rs':
            return self._chunk_rust(content, file_path)
        elif ext in ['.java', '.kt']:
            return self._chunk_java(content, file_path)
        elif ext in ['.md', '.markdown']:
            return self._chunk_markdown(content, file_path)
        else:
            return self._chunk_generic(content, file_path)

    def _chunk_python(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Python code by class/function definitions."""
        chunks = []

        # Pattern matches class and function definitions with their bodies
        # Uses indentation to determine block boundaries
        lines = content.split('\n')
        current_chunk = []
        current_type = None
        indent_level = 0

        for i, line in enumerate(lines):
            stripped = line.lstrip()

            # Check for new definition
            if stripped.startswith('class ') or stripped.startswith('def ') or stripped.startswith('async def '):
                # Save previous chunk if exists
                if current_chunk and len('\n'.join(current_chunk).strip()) > 50:
                    chunks.append({
                        "content": '\n'.join(current_chunk).strip(),
                        "metadata": {
                            "file_path": file_path,
                            "chunk_type": "code_block",
                            "language": "python"
                        }
                    })

                current_chunk = [line]
                current_type = 'class' if stripped.startswith('class ') else 'function'
                indent_level = len(line) - len(stripped)
            elif current_chunk:
                # Continue current block or end it
                if stripped and not line.startswith(' ' * (indent_level + 1)) and not stripped.startswith('#'):
                    # New top-level code, save chunk and start new
                    if len('\n'.join(current_chunk).strip()) > 50:
                        chunks.append({
                            "content": '\n'.join(current_chunk).strip(),
                            "metadata": {
                                "file_path": file_path,
                                "chunk_type": "code_block",
                                "language": "python"
                            }
                        })
                    current_chunk = [line]
                else:
                    current_chunk.append(line)
            else:
                current_chunk.append(line)

        # Don't forget last chunk
        if current_chunk and len('\n'.join(current_chunk).strip()) > 50:
            chunks.append({
                "content": '\n'.join(current_chunk).strip(),
                "metadata": {
                    "file_path": file_path,
                    "chunk_type": "code_block",
                    "language": "python"
                }
            })

        # If we got very few chunks, fall back to generic
        if len(chunks) < 2:
            return self._chunk_generic(content, file_path)

        return chunks

    def _chunk_javascript(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk JavaScript by function/class boundaries."""
        return self._chunk_c_style(content, file_path, "javascript")

    def _chunk_typescript(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk TypeScript by function/class/interface boundaries."""
        return self._chunk_c_style(content, file_path, "typescript")

    def _chunk_go(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Go by func/type boundaries."""
        return self._chunk_c_style(content, file_path, "go")

    def _chunk_rust(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Rust by fn/impl/struct boundaries."""
        return self._chunk_c_style(content, file_path, "rust")

    def _chunk_java(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk Java by class/method boundaries."""
        return self._chunk_c_style(content, file_path, "java")

    def _chunk_c_style(self, content: str, file_path: str, language: str) -> List[Dict[str, Any]]:
        """Generic chunking for C-style languages using brace matching."""
        chunks = []

        # Split by top-level braces (simplified approach)
        lines = content.split('\n')
        current_chunk = []
        brace_depth = 0
        in_block = False

        for line in lines:
            current_chunk.append(line)
            brace_depth += line.count('{') - line.count('}')

            if brace_depth == 0 and in_block:
                # End of a top-level block
                chunk_text = '\n'.join(current_chunk).strip()
                if len(chunk_text) > 50:
                    chunks.append({
                        "content": chunk_text,
                        "metadata": {
                            "file_path": file_path,
                            "chunk_type": "code_block",
                            "language": language
                        }
                    })
                current_chunk = []
                in_block = False
            elif brace_depth > 0:
                in_block = True

        # Handle remaining content
        if current_chunk:
            chunk_text = '\n'.join(current_chunk).strip()
            if len(chunk_text) > 50:
                chunks.append({
                    "content": chunk_text,
                    "metadata": {
                        "file_path": file_path,
                        "chunk_type": "code_block",
                        "language": language
                    }
                })

        if len(chunks) < 2:
            return self._chunk_generic(content, file_path)

        return chunks

    def _chunk_markdown(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Chunk markdown by headers."""
        chunks = []

        # Split by headers
        sections = re.split(r'\n(?=#{1,3}\s)', content)

        for section in sections:
            section = section.strip()
            if len(section) > 50:
                chunks.append({
                    "content": section,
                    "metadata": {
                        "file_path": file_path,
                        "chunk_type": "section",
                        "language": "markdown"
                    }
                })

        if len(chunks) < 2:
            return self._chunk_generic(content, file_path)

        return chunks

    def _chunk_generic(self, content: str, file_path: str) -> List[Dict[str, Any]]:
        """Generic chunking with overlap for any text."""
        chunks = []

        # Try splitting by double newlines first (paragraphs)
        paragraphs = content.split('\n\n')

        # If we only got one big paragraph (common in log files), split by single newlines
        if len(paragraphs) <= 1 or (len(paragraphs) > 0 and len(paragraphs[0]) > self.chunk_size * 2):
            # Split by single newlines for log-style files
            lines = content.split('\n')

            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line) + 1 < self.chunk_size:
                    current_chunk += line + "\n"
                else:
                    if current_chunk.strip():
                        chunks.append({
                            "content": current_chunk.strip(),
                            "metadata": {
                                "file_path": file_path,
                                "chunk_type": "text"
                            }
                        })
                    # Start new chunk with overlap from previous lines
                    if self.chunk_overlap > 0:
                        overlap_lines = current_chunk.split('\n')
                        overlap_text = ""
                        for ol in reversed(overlap_lines):
                            if len(overlap_text) + len(ol) < self.chunk_overlap:
                                overlap_text = ol + "\n" + overlap_text
                            else:
                                break
                        current_chunk = overlap_text + line + "\n"
                    else:
                        current_chunk = line + "\n"

            # Add remaining content
            if current_chunk.strip():
                chunks.append({
                    "content": current_chunk.strip(),
                    "metadata": {
                        "file_path": file_path,
                        "chunk_type": "text"
                    }
                })

            return chunks

        # Original paragraph-based chunking for files with double newlines
        current_chunk = ""
        for para in paragraphs:
            if len(current_chunk) + len(para) < self.chunk_size:
                current_chunk += para + "\n\n"
            else:
                if current_chunk.strip():
                    chunks.append({
                        "content": current_chunk.strip(),
                        "metadata": {
                            "file_path": file_path,
                            "chunk_type": "text"
                        }
                    })
                # Start new chunk with overlap
                overlap_start = max(0, len(current_chunk) - self.chunk_overlap)
                current_chunk = current_chunk[overlap_start:] + para + "\n\n"

        # Add remaining content
        if current_chunk.strip():
            chunks.append({
                "content": current_chunk.strip(),
                "metadata": {
                    "file_path": file_path,
                    "chunk_type": "text"
                }
            })

        return chunks

    # ----- Document Indexing -----

    def _compute_file_hash(self, content: str) -> str:
        """Compute SHA256 hash of file content."""
        return hashlib.sha256(content.encode()).hexdigest()

    def index_file(self, collection_id: int, file_path: str, content: str) -> int:
        """
        Index a single file into a collection.
        Returns number of chunks indexed.
        """
        file_hash = self._compute_file_hash(content)

        # Check if file already indexed with same hash
        existing = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id,
            RAGDocument.file_path == file_path
        ).first()

        if existing and existing.file_hash == file_hash:
            logger.debug(f"File unchanged, skipping: {file_path}")
            return 0

        # Get ChromaDB collection
        chroma_collection = self._get_or_create_chroma_collection(collection_id)

        # Remove old chunks if file was previously indexed
        if existing:
            try:
                chroma_collection.delete(where={"file_path": file_path})
            except Exception:
                pass  # Collection might be empty
            self.db.delete(existing)
            self.db.flush()

        # Chunk the content
        chunks = self._chunk_content(content, file_path)

        if not chunks:
            return 0

        # Enforce maximum chunk size to prevent massive chunks from being stored
        max_chunk_size = 10000  # 10k chars max per chunk
        filtered_chunks = []
        for chunk in chunks:
            if len(chunk["content"]) > max_chunk_size:
                # Split oversized chunk into smaller pieces
                content_text = chunk["content"]
                for i in range(0, len(content_text), max_chunk_size):
                    piece = content_text[i:i + max_chunk_size]
                    if piece.strip():
                        filtered_chunks.append({
                            "content": piece,
                            "metadata": chunk["metadata"].copy()
                        })
            else:
                filtered_chunks.append(chunk)
        chunks = filtered_chunks

        if not chunks:
            return 0

        # Generate embeddings
        embedding_service = get_embedding_service(self.db)
        texts = [c["content"] for c in chunks]
        embeddings = embedding_service.embed(texts)

        # Add to ChromaDB
        ids = [f"{file_path}_{i}" for i in range(len(chunks))]
        metadatas = [c["metadata"] for c in chunks]

        chroma_collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas
        )

        # Track in database
        doc = RAGDocument(
            collection_id=collection_id,
            file_path=file_path,
            file_hash=file_hash,
            chunk_count=len(chunks)
        )
        self.db.add(doc)
        self.db.commit()

        logger.info(f"Indexed {len(chunks)} chunks from {file_path}")
        return len(chunks)

    def delete_file(self, collection_id: int, file_path: str):
        """Remove a file from the index."""
        chroma_collection = self._get_or_create_chroma_collection(collection_id)

        try:
            chroma_collection.delete(where={"file_path": file_path})
        except Exception:
            pass

        doc = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id,
            RAGDocument.file_path == file_path
        ).first()

        if doc:
            self.db.delete(doc)
            self.db.commit()

    # ----- Query/Retrieval -----

    def query(
        self,
        query_text: str,
        collection_ids: Optional[List[int]] = None,
        top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Query the RAG index and return relevant chunks.
        """
        if top_k is None:
            top_k = self.top_k

        # Get user's collections if not specified
        if collection_ids is None:
            collections = self.db.query(RAGCollection).filter(
                RAGCollection.user_id == self.user_id
            ).all()
            collection_ids = [c.id for c in collections]

        if not collection_ids:
            return []

        # Generate query embedding
        embedding_service = get_embedding_service(self.db)
        query_embedding = embedding_service.embed_single(query_text)

        # Query each collection and merge results
        all_results = []

        for collection_id in collection_ids:
            try:
                chroma_collection = self._get_or_create_chroma_collection(collection_id)

                # Check if collection has any documents
                if chroma_collection.count() == 0:
                    continue

                # Get collection info for name
                db_collection = self.db.query(RAGCollection).filter(
                    RAGCollection.id == collection_id
                ).first()
                collection_name = db_collection.name if db_collection else "Unknown"

                results = chroma_collection.query(
                    query_embeddings=[query_embedding],
                    n_results=min(top_k, chroma_collection.count()),
                    include=["documents", "metadatas", "distances"]
                )

                if results["documents"] and results["documents"][0]:
                    for i, doc in enumerate(results["documents"][0]):
                        # ChromaDB returns distance, convert to similarity
                        distance = results["distances"][0][i] if results["distances"] else 0
                        similarity = 1 - distance  # For cosine distance

                        if similarity >= self.min_similarity:
                            all_results.append({
                                "content": doc,
                                "file_path": results["metadatas"][0][i].get("file_path", ""),
                                "similarity": similarity,
                                "collection_name": collection_name,
                                "metadata": results["metadatas"][0][i]
                            })
            except Exception as e:
                logger.error(f"Error querying collection {collection_id}: {e}")

        # Sort by similarity and return top_k
        all_results.sort(key=lambda x: x["similarity"], reverse=True)
        return all_results[:top_k]

    def format_context(self, results: List[Dict[str, Any]], max_context_chars: int = 32000) -> str:
        """Format query results as context for the LLM.

        Args:
            results: Query results from RAG
            max_context_chars: Maximum total characters for context (default 32k to fit in most context windows)
        """
        if not results:
            return ""

        context_parts = ["## Relevant Code/Documentation Context\n"]
        total_chars = len(context_parts[0])
        max_chunk_chars = 8000  # Max chars per individual chunk to prevent single massive chunks

        for i, result in enumerate(results, 1):
            file_path = result['file_path']
            lang = result.get('metadata', {}).get('language', '')
            content = result['content']

            # Truncate individual chunks that are too large
            if len(content) > max_chunk_chars:
                content = content[:max_chunk_chars] + "\n... [truncated - chunk too large]"

            header = f"### Source {i}: `{file_path}`\n"
            header += f"Collection: {result['collection_name']} | Relevance: {result['similarity']:.0%}\n"

            # Add code block with language hint
            if lang:
                chunk_text = f"{header}```{lang}\n{content}\n```\n\n"
            else:
                chunk_text = f"{header}```\n{content}\n```\n\n"

            # Check if adding this chunk would exceed max context
            if total_chars + len(chunk_text) > max_context_chars:
                if i == 1:
                    # At least include first result (truncated)
                    remaining = max_context_chars - total_chars - 100
                    if remaining > 500:
                        truncated_content = content[:remaining] + "\n... [truncated to fit context]"
                        if lang:
                            chunk_text = f"{header}```{lang}\n{truncated_content}\n```\n\n"
                        else:
                            chunk_text = f"{header}```\n{truncated_content}\n```\n\n"
                        context_parts.append(chunk_text)
                context_parts.append(f"\n[{len(results) - i + 1} more results omitted to fit context window]")
                break

            context_parts.append(chunk_text)
            total_chars += len(chunk_text)

        return "\n".join(context_parts)

    # ----- Collection Management -----

    def update_collection_document_count(self, collection_id: int):
        """Update the document_count field to match actual RAGDocument count."""
        doc_count = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).count()

        collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()

        if collection:
            collection.document_count = doc_count

    def delete_collection_documents(self, collection_id: int):
        """Delete all documents in a collection but keep the collection itself."""
        name = self._get_collection_name(collection_id)
        try:
            self.client.delete_collection(name)
        except Exception:
            pass  # Collection may not exist

        # Delete documents from database but keep the collection
        self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).delete()

        # Reset document count
        collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()
        if collection:
            collection.document_count = 0
        self.db.commit()

    def delete_collection(self, collection_id: int):
        """Delete a collection and all its data."""
        name = self._get_collection_name(collection_id)
        try:
            self.client.delete_collection(name)
        except Exception:
            pass  # Collection may not exist

        # Delete from database
        self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).delete()
        self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).delete()
        self.db.commit()

    def get_collection_stats(self, collection_id: int) -> Dict[str, Any]:
        """Get statistics for a collection."""
        chroma_collection = self._get_or_create_chroma_collection(collection_id)

        db_collection = self.db.query(RAGCollection).filter(
            RAGCollection.id == collection_id
        ).first()

        doc_count = self.db.query(RAGDocument).filter(
            RAGDocument.collection_id == collection_id
        ).count()

        return {
            "name": db_collection.name if db_collection else "Unknown",
            "document_count": doc_count,
            "chunk_count": chroma_collection.count(),
            "last_indexed": db_collection.last_indexed_at.isoformat() if db_collection and db_collection.last_indexed_at else None
        }


def get_rag_service(db: Session, user_id: int) -> RAGService:
    """Get RAG service instance for a user."""
    return RAGService(db, user_id)

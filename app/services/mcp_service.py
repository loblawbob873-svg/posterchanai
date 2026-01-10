"""
Integrated MCP Server Service for Posterchanai.

This service runs the MCP (Model Context Protocol) server within the main app,
exposing RAG functionality to MCP clients like Continue.dev, Claude Desktop, etc.

The server can be enabled/disabled via admin settings without needing a separate process.
"""
import sys
import os
import asyncio
import logging
import time
import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any

# Bounded thread pool for embedding operations (prevents CPU overload)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mcp_worker")

# Query result cache with TTL
_query_cache: OrderedDict = OrderedDict()
_QUERY_CACHE_MAX = 50000
_QUERY_CACHE_TTL = 600  # 10 minutes

# Document chunk cache
_document_cache: Dict[int, Dict] = {}
_document_cache_loaded = False

# Server state
_mcp_server_thread: Optional[threading.Thread] = None
_mcp_server_running = False
_warmup_complete = False

logger = logging.getLogger(__name__)

# User ID for RAG operations
DEFAULT_USER_ID = int(os.environ.get("RAG_USER_ID", "1"))


def _get_cached_result(cache_key: str):
    """Get cached result if still valid."""
    if cache_key in _query_cache:
        result, timestamp = _query_cache[cache_key]
        if time.time() - timestamp < _QUERY_CACHE_TTL:
            _query_cache.move_to_end(cache_key)
            return result
        else:
            del _query_cache[cache_key]
    return None


def _cache_result(cache_key: str, result):
    """Cache a result with timestamp."""
    while len(_query_cache) >= _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)
    _query_cache[cache_key] = (result, time.time())


def warmup_model(load_documents: bool = True) -> Dict[str, Any]:
    """Pre-load the embedding model and warm up all caches."""
    global _warmup_complete, _document_cache, _document_cache_loaded

    if _warmup_complete:
        logger.info("[MCP] Already warmed up, skipping")
        return {"status": "already_warm"}

    from app.database import SessionLocal
    from app.models import Setting, RAGCollection

    # Check if RAG is enabled
    db = SessionLocal()
    try:
        rag_enabled = db.query(Setting).filter(Setting.key == "rag_enabled").first()
        if not rag_enabled or rag_enabled.value != "true":
            logger.info("[MCP] RAG is disabled, skipping warmup")
            return {"status": "disabled", "reason": "rag_disabled"}
    finally:
        db.close()

    logger.info("[MCP] Starting cache warmup...")
    start = time.time()
    stats = {
        "collections_loaded": 0,
        "documents_cached": 0,
        "chunks_cached": 0,
    }

    db = SessionLocal()
    try:
        from app.services.embedding_service import get_embedding_service
        from app.services.rag_service import get_rag_service

        # Load embedding model
        logger.info("[MCP] Loading embedding model...")
        embed_svc = get_embedding_service(db)
        embed_svc._ensure_model_loaded()
        _ = embed_svc.embed_single("warmup test query", use_cache=False)

        # Initialize RAG service
        logger.info("[MCP] Initializing RAG service...")
        rag_svc = get_rag_service(db, DEFAULT_USER_ID)

        # Pre-load ChromaDB collections
        logger.info("[MCP] Loading ChromaDB collections...")
        collections = db.query(RAGCollection).filter(
            RAGCollection.user_id == DEFAULT_USER_ID
        ).all()

        ref_embedding = embed_svc.embed_single("test", use_cache=True)

        for col in collections:
            try:
                chroma_col = rag_svc._get_or_create_chroma_collection(col.id)
                chroma_col.query(query_embeddings=[ref_embedding], n_results=1)
                stats["collections_loaded"] += 1
                logger.info(f"[MCP] Loaded collection: {col.name} ({col.document_count} docs)")
            except Exception as e:
                logger.warning(f"[MCP] Failed to load collection {col.name}: {e}")

        # Cache document chunks in RAM
        if load_documents:
            logger.info("[MCP] Caching document chunks in RAM...")
            for col in collections:
                try:
                    chroma_col = rag_svc._get_or_create_chroma_collection(col.id)
                    all_data = chroma_col.get(include=["documents", "metadatas", "embeddings"])

                    if all_data and all_data.get("ids"):
                        _document_cache[col.id] = {}
                        for i, doc_id in enumerate(all_data["ids"]):
                            _document_cache[col.id][doc_id] = {
                                "content": all_data["documents"][i] if all_data["documents"] else None,
                                "metadata": all_data["metadatas"][i] if all_data["metadatas"] else {},
                                "embedding": all_data["embeddings"][i] if all_data.get("embeddings") else None,
                            }
                            stats["chunks_cached"] += 1
                        stats["documents_cached"] += len(all_data["ids"])
                        logger.info(f"[MCP] Cached {len(all_data['ids'])} chunks from {col.name}")
                except Exception as e:
                    logger.warning(f"[MCP] Failed to cache documents for {col.name}: {e}")

            _document_cache_loaded = True

        elapsed = time.time() - start
        _warmup_complete = True

        logger.info(f"[MCP] Warmup complete in {elapsed:.2f}s - {stats}")
        return {"status": "ok", "elapsed_seconds": round(elapsed, 2), **stats}

    except Exception as e:
        logger.error(f"[MCP] Warmup failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        db.close()


def get_mcp_status() -> Dict[str, Any]:
    """Get MCP server status and cache statistics."""
    from app.services.embedding_service import get_cache_stats as get_embed_cache_stats
    from app.services.rag_service import get_cache_stats as get_rag_cache_stats

    embed_stats = get_embed_cache_stats()
    rag_stats = get_rag_cache_stats()

    doc_cache_chunks = sum(len(docs) for docs in _document_cache.values())
    doc_cache_collections = len(_document_cache)

    return {
        "running": _mcp_server_running,
        "warmup_complete": _warmup_complete,
        "mcp_query_cache_size": len(_query_cache),
        "mcp_query_cache_max": _QUERY_CACHE_MAX,
        "document_cache": {
            "loaded": _document_cache_loaded,
            "collections": doc_cache_collections,
            "chunks": doc_cache_chunks,
        },
        "embedding_cache": embed_stats,
        "rag_cache": rag_stats
    }


def _sync_search(query: str, top_k: int):
    """Synchronous search helper with caching."""
    cache_key = hashlib.md5(f"{query}:{top_k}:{DEFAULT_USER_ID}".encode()).hexdigest()
    cached = _get_cached_result(cache_key)
    if cached is not None:
        logger.debug(f"[MCP] Cache hit for query: {query[:50]}...")
        return cached

    from app.database import SessionLocal
    from app.services.rag_service import get_rag_service

    db = SessionLocal()
    try:
        rag_service = get_rag_service(db, DEFAULT_USER_ID)
        results = rag_service.query(query, top_k=top_k)
        _cache_result(cache_key, results)
        return results
    except Exception as e:
        logger.error(f"[MCP] RAG search failed: {e}")
        return f"Error searching codebase: {str(e)}"
    finally:
        db.close()


def _sync_list_collections():
    """Synchronous list collections helper."""
    from app.database import SessionLocal
    from app.models import RAGCollection

    db = SessionLocal()
    try:
        collections = db.query(RAGCollection).filter(
            RAGCollection.user_id == DEFAULT_USER_ID
        ).all()

        if not collections:
            return "No RAG collections found."

        output = "## RAG Collections\n\n"
        for col in collections:
            output += f"- **{col.name}** (id: {col.id})\n"
            output += f"  - Type: {col.collection_type}\n"
            output += f"  - Source: {col.source_path}\n"
            output += f"  - Documents: {col.document_count}\n\n"
        return output

    except Exception as e:
        logger.error(f"[MCP] Failed to list collections: {e}")
        return f"Error: {str(e)}"
    finally:
        db.close()


def _sync_reindex_collection(collection_id: int):
    """Synchronous reindex helper."""
    from app.database import SessionLocal
    from app.models import RAGCollection
    from app.services.rag_folder_service import get_folder_indexer

    db = SessionLocal()
    try:
        collection = db.query(RAGCollection).filter(
            RAGCollection.id == collection_id,
            RAGCollection.user_id == DEFAULT_USER_ID
        ).first()

        if not collection:
            return f"Error: Collection {collection_id} not found"

        if collection.collection_type not in ("codebase", "folder"):
            return f"Error: Collection '{collection.name}' is type '{collection.collection_type}'. Only codebase/folder collections can be re-indexed."

        if not collection.source_path or not os.path.isdir(collection.source_path):
            return f"Error: Source path '{collection.source_path}' is not valid"

        indexer = get_folder_indexer(db, DEFAULT_USER_ID)
        indexer.index_folder(collection)
        db.refresh(collection)

        return f"Re-indexed '{collection.name}' - {collection.document_count} documents"

    except Exception as e:
        logger.error(f"[MCP] Failed to reindex collection {collection_id}: {e}")
        return f"Error re-indexing: {str(e)}"
    finally:
        db.close()


# Allowed paths for file operations (security)
ALLOWED_PATHS = ["/home/verita84/posterchanai"]


def _is_path_allowed(file_path: str) -> bool:
    """Check if file path is within allowed directories."""
    abs_path = os.path.abspath(file_path)
    return any(abs_path.startswith(allowed) for allowed in ALLOWED_PATHS)


def _run_mcp_sse_server(host: str, port: int):
    """Run the MCP SSE server in a separate thread."""
    global _mcp_server_running

    try:
        from mcp.server import Server
        from mcp.server.sse import SseServerTransport
        from mcp.types import Tool, TextContent
        import uvicorn
        import json

        app = Server("posterchanai-rag")

        @app.list_tools()
        async def list_tools():
            return [
                Tool(
                    name="search_codebase",
                    description="Search the indexed codebase for relevant code snippets.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Search query"},
                            "top_k": {"type": "integer", "description": "Results to return", "default": 5}
                        },
                        "required": ["query"]
                    }
                ),
                Tool(
                    name="list_collections",
                    description="List all available RAG collections",
                    inputSchema={"type": "object", "properties": {}}
                ),
                Tool(
                    name="reindex_collection",
                    description="Re-index a RAG collection to pick up file changes.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "collection_id": {"type": "integer", "description": "Collection ID", "default": 2}
                        }
                    }
                ),
                Tool(
                    name="read_file",
                    description="Read file contents with optional line range.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path to file"},
                            "start_line": {"type": "integer", "description": "Start line (1-indexed)"},
                            "end_line": {"type": "integer", "description": "End line (1-indexed)"}
                        },
                        "required": ["file_path"]
                    }
                ),
                Tool(
                    name="edit_file",
                    description="Edit a file by replacing text.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path to file"},
                            "old_string": {"type": "string", "description": "Text to find"},
                            "new_string": {"type": "string", "description": "Replacement text"}
                        },
                        "required": ["file_path", "old_string", "new_string"]
                    }
                ),
                Tool(
                    name="write_file",
                    description="Write content to a file.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "Absolute path to file"},
                            "content": {"type": "string", "description": "Content to write"}
                        },
                        "required": ["file_path", "content"]
                    }
                )
            ]

        @app.call_tool()
        async def call_tool(name: str, arguments: dict):
            logger.info(f"[MCP] Tool call: {name}")

            if name == "search_codebase":
                query = arguments.get("query", "")
                top_k = arguments.get("top_k", 5)
                if not query:
                    return [TextContent(type="text", text="Error: query is required")]

                results = await asyncio.to_thread(_sync_search, query, top_k)

                if isinstance(results, str):
                    return [TextContent(type="text", text=results)]

                if not results:
                    return [TextContent(type="text", text=f"No results found for: {query}")]

                output = f"## Found {len(results)} results\n\n"
                for i, result in enumerate(results, 1):
                    file_path = result.get("file_path", "unknown")
                    similarity = result.get("similarity", 0)
                    content = result.get("content", "")[:250]
                    ext = os.path.splitext(file_path)[1].lstrip('.')
                    lang = ext if ext in ['py', 'js', 'ts', 'tsx', 'go', 'rs', 'java', 'md'] else ''
                    output += f"### {i}. `{file_path}` ({similarity:.0%})\n```{lang}\n{content}\n```\n\n"

                return [TextContent(type="text", text=output)]

            elif name == "list_collections":
                result = await asyncio.to_thread(_sync_list_collections)
                return [TextContent(type="text", text=result)]

            elif name == "reindex_collection":
                collection_id = arguments.get("collection_id", 2)
                result = await asyncio.to_thread(_sync_reindex_collection, collection_id)
                return [TextContent(type="text", text=result)]

            elif name == "read_file":
                file_path = arguments.get("file_path", "")
                if not file_path:
                    return [TextContent(type="text", text="Error: file_path is required")]
                if not _is_path_allowed(file_path):
                    return [TextContent(type="text", text=f"Error: Access denied")]

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        lines = f.readlines()

                    start_line = arguments.get("start_line")
                    end_line = arguments.get("end_line")
                    if start_line or end_line:
                        start_idx = (start_line - 1) if start_line else 0
                        end_idx = end_line if end_line else len(lines)
                        lines = lines[start_idx:end_idx]

                    numbered = [f"{i}: {line.rstrip()}" for i, line in enumerate(lines, start=start_line or 1)]
                    content = "\n".join(numbered)
                    return [TextContent(type="text", text=f"## {file_path}\n```\n{content}\n```")]
                except FileNotFoundError:
                    return [TextContent(type="text", text=f"Error: File not found: {file_path}")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Error reading file: {str(e)}")]

            elif name == "edit_file":
                file_path = arguments.get("file_path", "")
                old_string = arguments.get("old_string", "")
                new_string = arguments.get("new_string", "")

                if not file_path or not old_string:
                    return [TextContent(type="text", text="Error: file_path and old_string are required")]
                if not _is_path_allowed(file_path):
                    return [TextContent(type="text", text="Error: Access denied")]

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    if old_string not in content:
                        return [TextContent(type="text", text="Error: old_string not found in file")]

                    count = content.count(old_string)
                    if count > 1:
                        return [TextContent(type="text", text=f"Error: old_string found {count} times. Must be unique.")]

                    new_content = content.replace(old_string, new_string, 1)
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)

                    return [TextContent(type="text", text=f"Successfully edited {file_path}")]
                except FileNotFoundError:
                    return [TextContent(type="text", text=f"Error: File not found: {file_path}")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Error editing file: {str(e)}")]

            elif name == "write_file":
                file_path = arguments.get("file_path", "")
                content = arguments.get("content", "")

                if not file_path:
                    return [TextContent(type="text", text="Error: file_path is required")]
                if not _is_path_allowed(file_path):
                    return [TextContent(type="text", text="Error: Access denied")]

                try:
                    parent_dir = os.path.dirname(file_path)
                    if parent_dir and not os.path.exists(parent_dir):
                        os.makedirs(parent_dir, exist_ok=True)

                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)

                    return [TextContent(type="text", text=f"Successfully wrote {len(content)} bytes to {file_path}")]
                except Exception as e:
                    return [TextContent(type="text", text=f"Error writing file: {str(e)}")]

            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

        sse = SseServerTransport("/messages/")

        async def asgi_app(scope, receive, send):
            if scope["type"] != "http":
                return

            path = scope["path"]
            method = scope["method"]

            if path == "/sse" and method == "GET":
                async with sse.connect_sse(scope, receive, send) as streams:
                    await app.run(streams[0], streams[1], app.create_initialization_options())

            elif path.startswith("/messages/") and method == "POST":
                await sse.handle_post_message(scope, receive, send)

            elif path == "/search" and method == "POST":
                body = b""
                while True:
                    message = await receive()
                    body += message.get("body", b"")
                    if not message.get("more_body", False):
                        break

                try:
                    data = json.loads(body) if body else {}
                    query = data.get("query", "")
                    top_k = data.get("top_k", 3)
                except Exception:
                    await send({"type": "http.response.start", "status": 400, "headers": [[b"content-type", b"application/json"]]})
                    await send({"type": "http.response.body", "body": json.dumps({"error": "Invalid JSON"}).encode()})
                    return

                if not query:
                    await send({"type": "http.response.start", "status": 400, "headers": [[b"content-type", b"application/json"]]})
                    await send({"type": "http.response.body", "body": json.dumps({"error": "query is required"}).encode()})
                    return

                results = await asyncio.to_thread(_sync_search, query, top_k)
                if isinstance(results, str):
                    response_body = json.dumps({"error": results, "results": []}).encode()
                else:
                    response_body = json.dumps({"results": results or []}).encode()

                await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", b"application/json"]]})
                await send({"type": "http.response.body", "body": response_body})

            elif path == "/status" and method == "GET":
                status = get_mcp_status()
                await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", b"application/json"]]})
                await send({"type": "http.response.body", "body": json.dumps(status).encode()})

            elif path == "/warmup" and method == "POST":
                result = await asyncio.to_thread(warmup_model)
                await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", b"application/json"]]})
                await send({"type": "http.response.body", "body": json.dumps(result).encode()})

            else:
                await send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"text/plain"]]})
                await send({"type": "http.response.body", "body": b"Not Found"})

        logger.info(f"[MCP] Starting SSE server on http://{host}:{port}")
        _mcp_server_running = True

        config = uvicorn.Config(asgi_app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        server.run()

    except Exception as e:
        logger.error(f"[MCP] Server error: {e}")
    finally:
        _mcp_server_running = False


def start_mcp_server(db_session=None) -> bool:
    """Start the MCP server if enabled in settings."""
    global _mcp_server_thread, _mcp_server_running

    if _mcp_server_running:
        logger.info("[MCP] Server already running")
        return True

    from app.database import SessionLocal
    from app.models import Setting

    db = db_session or SessionLocal()
    close_db = db_session is None

    try:
        mcp_enabled = db.query(Setting).filter(Setting.key == "mcp_enabled").first()
        if not mcp_enabled or mcp_enabled.value != "true":
            logger.info("[MCP] Server is disabled in settings")
            return False

        mcp_port = db.query(Setting).filter(Setting.key == "mcp_port").first()
        mcp_host = db.query(Setting).filter(Setting.key == "mcp_host").first()
        mcp_warmup = db.query(Setting).filter(Setting.key == "mcp_warmup").first()

        port = int(mcp_port.value) if mcp_port else 8808
        host = mcp_host.value if mcp_host else "0.0.0.0"
        do_warmup = mcp_warmup and mcp_warmup.value == "true"

        # Warmup in background if enabled
        if do_warmup:
            logger.info("[MCP] Running warmup in background...")
            warmup_thread = threading.Thread(target=warmup_model, daemon=True)
            warmup_thread.start()

        # Start MCP server in daemon thread
        _mcp_server_thread = threading.Thread(
            target=_run_mcp_sse_server,
            args=(host, port),
            daemon=True
        )
        _mcp_server_thread.start()

        logger.info(f"[MCP] Server started on http://{host}:{port}")
        return True

    except Exception as e:
        logger.error(f"[MCP] Failed to start server: {e}")
        return False
    finally:
        if close_db:
            db.close()


def stop_mcp_server():
    """Stop the MCP server (note: uvicorn doesn't support graceful shutdown from another thread easily)."""
    global _mcp_server_running
    # Since uvicorn runs in a daemon thread, it will stop when the main app stops
    # For immediate stop, we'd need to use a shutdown event, but this is fine for now
    logger.info("[MCP] Server will stop when main app stops")
    _mcp_server_running = False


def is_mcp_running() -> bool:
    """Check if MCP server is running."""
    return _mcp_server_running

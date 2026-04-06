from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import FileResponse, Response
from starlette.requests import Request
from pydantic import BaseModel
import asyncio
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
from pathlib import Path
from urllib.parse import unquote
import json
import logging
import httpx
from datetime import datetime

logger = logging.getLogger(__name__)

from app.database import get_db, SessionLocal
from app.models import User, Conversation, Message, Setting
from app.schemas import ConversationCreate, ConversationResponse, ConversationWithMessages, MessageResponse
from app.auth import get_current_user, get_user_from_websocket
from app.services.chat_service import ChatService
from app.services.command_service import CommandService
from app.services.storage_service import StorageService
from app.services.document_service import extract_pdf_text, extract_document_text, extract_image_text
from app.services.email_service import EmailService
from app.services.search_service import SearchService, is_safe_url
from app.services.proxy_image_cache import get as proxy_cache_get
from app.services.plugin_service import PluginService
from app.services.intent_service import IntentService

router = APIRouter(prefix="/api", tags=["chat"])


# REST Endpoints

@router.get("/conversations", response_model=List[ConversationResponse])
def list_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    return db.query(Conversation).filter(
        Conversation.user_id == current_user.id
    ).order_by(Conversation.updated_at.desc()).all()


@router.post("/conversations", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
def create_conversation(
    data: ConversationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = Conversation(
        user_id=current_user.id,
        title=data.title or "New Chat"
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/conversations/{conversation_id}", response_model=ConversationWithMessages)
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Delete associated files
    storage = StorageService(db)
    storage.delete_conversation_files(current_user.username, conversation_id)

    db.delete(conversation)
    db.commit()
    return {"message": "Conversation deleted"}


@router.delete("/conversations")
def delete_all_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Delete all user files
    storage = StorageService(db)
    storage.delete_user_files(current_user.username)

    db.query(Conversation).filter(
        Conversation.user_id == current_user.id
    ).delete()
    db.commit()
    return {"message": "All conversations deleted"}


class CommandRequest(BaseModel):
    command: str

@router.post("/save-generated-image")
async def save_generated_image(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Save a generated image to user storage on demand."""
    try:
        data = await request.json()
        image_base64 = data.get("image")
        prompt = data.get("prompt", "")
        
        if not image_base64:
            raise HTTPException(status_code=400, detail="Image data is required")
        
        storage = StorageService(db)
        saved_path = storage.save_generated_image(current_user.username, image_base64, prompt)
        logger.info(f"Saved generated image to user storage: {saved_path}")
        
        # Generate viewable URL for the saved image
        from urllib.parse import quote
        encoded_username = quote(current_user.username, safe='')
        encoded_path = quote(saved_path, safe='')
        view_url = f"/api/files/view/{encoded_username}/{encoded_path}"
        
        return {"success": True, "path": saved_path, "view_url": view_url}
    except Exception as e:
        logger.error(f"Failed to save generated image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _proxy_fetch(url_raw: str):
    """Fetch image from URL; returns (content, media_type) or raises HTTPException."""
    is_safe, err = is_safe_url(url_raw)
    if not is_safe:
        raise HTTPException(status_code=400, detail=err)
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        resp = await client.get(
            url_raw,
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36", "Accept": "image/*,*/*"},
        )
        resp.raise_for_status()
        content = resp.content
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and (ctype.startswith("text/") or "json" in ctype or "xml" in ctype):
            raise HTTPException(status_code=502, detail="Upstream did not return an image")
        media_type = ctype if (ctype and ctype.startswith("image/")) else "image/png"
        return content, media_type


@router.get("/proxy-image/{thumb_id}")
async def proxy_image_by_id(
    thumb_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy an image by short id (from image search). Keeps WebSocket payload small."""
    raw = proxy_cache_get(thumb_id, db)
    if not raw:
        raise HTTPException(status_code=404, detail="Unknown or expired image id")
    try:
        content, media_type = await _proxy_fetch(raw)
        return Response(content=content, media_type=media_type)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Proxy image failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@router.get("/proxy-image")
async def proxy_image_get(
    url: str = Query(..., description="Image URL to proxy"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy an image URL (GET; query length limit may truncate long URLs)."""
    raw = unquote(url)
    try:
        content, media_type = await _proxy_fetch(raw)
        return Response(content=content, media_type=media_type)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Proxy image failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


class ProxyImageBody(BaseModel):
    url: Optional[str] = None
    thumb_id: Optional[str] = None


@router.post("/proxy-image")
async def proxy_image_post(
    body: ProxyImageBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Proxy an image: send url (long URLs) or thumb_id (short id from image search). Auth via header/cookie."""
    raw = None
    if (body.thumb_id or "").strip():
        raw = proxy_cache_get((body.thumb_id or "").strip(), db)
        if not raw:
            raise HTTPException(status_code=404, detail="Unknown or expired image id")
    elif (body.url or "").strip():
        raw = (body.url or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="url or thumb_id required")
    try:
        content, media_type = await _proxy_fetch(raw)
        return Response(content=content, media_type=media_type)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="Upstream error")
    except HTTPException:
        raise
    except Exception as e:
        logger.debug(f"Proxy image failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to fetch image")


@router.post("/save-mail-attachment")
async def save_mail_attachment(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Save a mail attachment to user storage on demand."""
    try:
        data = await request.json()
        attachment_data = data.get("data")  # base64 encoded
        filename = data.get("filename")
        
        if not attachment_data or not filename:
            raise HTTPException(status_code=400, detail="Attachment data and filename are required")
        
        # Decode base64 data
        import base64
        attachment_bytes = base64.b64decode(attachment_data)
        
        storage = StorageService(db)
        saved_path = storage.save_mail_attachment(current_user.username, attachment_bytes, filename)
        logger.info(f"Saved mail attachment to user storage: {saved_path}")
        
        # Generate URL to view the file
        from urllib.parse import quote
        encoded_username = quote(current_user.username, safe='')
        encoded_path = quote(saved_path, safe='')
        view_url = f"/api/files/view/{encoded_username}/{encoded_path}"
        
        return {"success": True, "path": saved_path, "view_url": view_url}
    except Exception as e:
        logger.error(f"Failed to save mail attachment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/command")
async def execute_command_endpoint(
    request: CommandRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Execute a command and return the result."""
    command_service = CommandService(db, user=current_user)
    command, arg = command_service.parse_command(request.command)
    if not command:
        return {"type": "text", "content": "Invalid command"}
    result = await command_service.execute_command(command, arg)
    return result


@router.get("/conversations/{conversation_id}/messages")
def get_messages(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Build response with image URLs
    result = []
    for msg in conversation.messages:
        msg_dict = {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at,
            "image_path": None
        }
        # Convert file path to API URL if exists
        if msg.image_path:
            filename = Path(msg.image_path).name
            from urllib.parse import quote
            msg_dict["image_path"] = f"/api/files/{quote(current_user.username, safe='')}/{conversation_id}/{filename}"
        result.append(msg_dict)
    return result


@router.get("/files/{username}/{conversation_id}/{filename}")
async def serve_file(
    username: str,
    conversation_id: int,
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Serve a stored file (image, document, etc.). Proxies to storage server if configured."""
    # Verify user owns this file (username must match)
    if current_user.username != username:
        raise HTTPException(status_code=403, detail="Access denied")

    # Verify conversation belongs to user
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == current_user.id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Check if storage server is configured - proxy request if so
    storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
    if storage_server_url and storage_server_url.value:
        # Proxy to storage server
        from app.services.storage_proxy import proxy_storage_request
        return await proxy_storage_request(
            db=db,
            request=request,
            endpoint=f"/api/chat/files/{username}/{conversation_id}/{filename}",
            method="GET",
            stream=True
        )

    # On storage server: Use local filesystem
    from app.services.storage_service import StorageService, _sanitize_path_component, _validate_path_within_base, ascii_safe_header_filename
    storage = StorageService(db)
    user_path = storage.get_conversation_path(current_user.username, conversation_id)
    
    # Sanitize filename
    try:
        safe_filename = _sanitize_path_component(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {str(e)}")
    
    file_path = user_path / safe_filename
    
    # Verify path is within user directory
    if not _validate_path_within_base(file_path, user_path):
        raise HTTPException(status_code=403, detail="Access denied: path outside user directory")
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    if file_path.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory, not a file")
    
    # Determine media type
    from mimetypes import guess_type
    content_type, _ = guess_type(str(file_path))
    if not content_type:
        suffix = Path(filename).suffix.lower()
        media_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        content_type = media_types.get(suffix, "application/octet-stream")
    
    # Read file
    def _read_file_sync():
        with open(file_path, 'rb') as f:
            return f.read()
    
    file_data = await asyncio.to_thread(_read_file_sync)
    
    # Return file response (ASCII-safe filename for Content-Disposition header)
    from fastapi.responses import Response
    safe_name = ascii_safe_header_filename(filename)
    return Response(
        content=file_data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"'
        }
    )


@router.post("/chat/email-response")
def email_response(
    data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Email an AI response to the user's notification email"""
    content = data.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="No content to email")

    if not current_user.notification_email:
        raise HTTPException(status_code=400, detail="No notification email configured. Please set one in Settings.")

    email_service = EmailService(db)
    if not email_service.smtp_enabled:
        raise HTTPException(status_code=400, detail="Email is not configured on this server")

    success, message = email_service.send_chat_response(
        to_email=current_user.notification_email,
        username=current_user.username,
        content=content
    )

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {message}")

    return {"message": "Email sent successfully"}


@router.get("/news-sources")
def get_news_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get configured news sources for the News modal"""
    setting = db.query(Setting).filter(Setting.key == "news_sources").first()

    # Default news sources if not configured
    default_sources = """drudgereport.com|Drudge Report
usatoday.com|USA Today
msn.com|MSN
cnn.com|CNN
foxnews.com|Fox News"""

    raw = setting.value if setting and setting.value else default_sources

    sources = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if "|" in line:
            url, name = line.split("|", 1)
            sources.append({"url": url.strip(), "name": name.strip()})
        elif line:
            # Just a URL without a name
            sources.append({"url": line, "name": line})

    return {"sources": sources}


# WebSocket for real-time chat

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}
        self.connection_ids: dict[int, int] = {}  # Track connection ID per user to detect stale connections
        self.conversation_ids: dict[int, int] = {}  # Track which conversation each user is connected to
        self.last_image_prompts: dict[int, str] = {}  # Track last image prompt per user
        self.stop_flags: dict[int, bool] = {}  # Stop streaming flags per user
        self.pending_results: dict[tuple, list] = {}  # (user_id, conv_id) -> list of pending results
        self._next_conn_id = 0
        self._conn_lock = asyncio.Lock()  # Protect connection ID increment

    async def connect(self, user_id: int, conversation_id: int, websocket: WebSocket) -> int:
        if websocket.client_state.name != "CONNECTED":
            await websocket.accept()
        # If same user reconnects to same conversation, don't kill the stream - just switch socket
        prev_conv = self.conversation_ids.get(user_id)
        same_conversation = prev_conv == conversation_id
        if not same_conversation:
            self.stop_flags[user_id] = True
        self.active_connections[user_id] = websocket
        self.conversation_ids[user_id] = conversation_id
        async with self._conn_lock:
            if same_conversation and user_id in self.connection_ids:
                conn_id = self.connection_ids[user_id]
            else:
                self._next_conn_id += 1
                conn_id = self._next_conn_id
                self.connection_ids[user_id] = conn_id
        self.stop_flags[user_id] = False
        return conn_id

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)
        self.stop_flags.pop(user_id, None)
        self.conversation_ids.pop(user_id, None)

    def should_stop(self, user_id: int, conn_id: int = None) -> bool:
        # Stop if flag is set OR if connection ID doesn't match (user switched chats)
        if self.stop_flags.get(user_id, False):
            return True
        if conn_id is not None and self.connection_ids.get(user_id) != conn_id:
            return True
        return False

    def set_stop(self, user_id: int, value: bool):
        self.stop_flags[user_id] = value

    def queue_result(self, user_id: int, conversation_id: int, data: dict):
        """Queue a result for later delivery when user reconnects to this conversation"""
        key = (user_id, conversation_id)
        if key not in self.pending_results:
            self.pending_results[key] = []
        self.pending_results[key].append(data)
        logger.debug(f"Saved pending result for user {user_id}, conv {conversation_id}")

    def get_pending_results(self, user_id: int, conversation_id: int) -> list:
        """Get and clear pending results for a conversation"""
        key = (user_id, conversation_id)
        results = self.pending_results.pop(key, [])
        if results:
            logger.debug(f"Delivering {len(results)} pending result(s) to user {user_id}, conv {conversation_id}")
        return results

    async def send_json(self, user_id: int, data: dict, conn_id: int = None, conversation_id: int = None):
        # Check if connection ID matches (prevents sending to wrong chat)
        if conn_id is not None and self.connection_ids.get(user_id) != conn_id:
            # Connection is stale - queue the result for later
            if conversation_id is not None and data.get("type") == "response":
                self.queue_result(user_id, conversation_id, data)
            return
        ws = self.active_connections.get(user_id)
        if ws:
            try:
                await ws.send_json(data)
            except Exception:
                # Failed to send - queue for later if it's a response
                if conversation_id is not None and data.get("type") == "response":
                    self.queue_result(user_id, conversation_id, data)
                pass  # Connection may be closed


manager = ConnectionManager()

# Router with no prefix so /ws/chat/{id} works (some clients connect without /api)
ws_only_router = APIRouter(tags=["chat"])


@router.websocket("/ws/chat/{conversation_id}")
@ws_only_router.websocket("/ws/chat/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: int):
    """Chat WebSocket. Accept immediately so we never return HTTP 403 (avoids proxy/WAF issues)."""
    conn_id = None
    user = None
    db = None
    logger.info("WebSocket /ws/chat/%s connection attempt", conversation_id)
    await websocket.accept()
    try:
        db = SessionLocal()
        user = await get_user_from_websocket(websocket, db)
        if not user:
            await websocket.send_json({"type": "error", "message": "Please log in again"})
            await websocket.close(code=4001)
            return

        # Verify conversation belongs to user (eagerly load messages to avoid N+1 queries)
        conversation = db.query(Conversation).options(
            joinedload(Conversation.messages)
        ).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id
        ).first()
        if not conversation:
            await websocket.send_json({"type": "error", "message": "Conversation not found"})
            await websocket.close(code=4004)
            return

        # Use manager.connect() which handles stopping old streams and returns connection ID
        try:
            conn_id = await manager.connect(user.id, conversation_id, websocket)
        except Exception as connect_err:
            logger.error(f"Failed to connect websocket: {connect_err}", exc_info=True)
            try:
                await websocket.send_json({"type": "error", "message": "Failed to establish connection"})
                await websocket.close(code=4000)
            except Exception:
                pass
            return

        # Check for and deliver any pending results from previous sessions
        pending = manager.get_pending_results(user.id, conversation_id)
        for pending_data in pending:
            try:
                await websocket.send_json(pending_data)
            except Exception:
                pass

        chat_service = ChatService(db, user=user)
        command_service = CommandService(db, user=user)
        storage_service = StorageService(db)
        search_service = SearchService(db)
        plugin_service = PluginService(db)
        intent_service = IntentService(db, user=user)

        try:
            while True:
                try:
                    # Check if websocket is still connected before receiving
                    if websocket.client_state.name != "CONNECTED":
                        logger.debug("WebSocket is not connected, breaking loop")
                        break
                    # Use receive_text to get better error info, then parse JSON
                    raw_text = await websocket.receive_text()
                    logger.debug(f"Received raw text length: {len(raw_text)}")
                    data = json.loads(raw_text)
                except json.JSONDecodeError as json_err:
                    logger.debug(f"JSON parse failed: {json_err}")
                    continue
                except Exception as recv_err:
                    logger.debug(f"Failed to receive: {type(recv_err).__name__}: {recv_err}")
                    raise
                logger.debug(f"Received: type={data.get('type')}, content={data.get('content', '')[:50] if data.get('content') else ''}, has_image={data.get('image_data') is not None}")

                if data.get("type") == "stop":
                    manager.set_stop(user.id, True)
                    continue

                if data.get("type") == "message":
                    manager.set_stop(user.id, False)  # Reset for new message
                    content = data.get("content", "").strip()
                    image_data = data.get("image_data")  # base64 image (single, for backward compat)
                    images = data.get("images", [])  # Array of {base64, filename}
                    image_path = data.get("image_path")  # path to stored image (for editing)
                    file_content = data.get("file_content")  # text file content (single, for backward compat)
                    files = data.get("files", [])  # Array of {content, filename}
                    pdf_data = data.get("pdf_data")  # base64 PDF (single, for backward compat)
                    pdfs = data.get("pdfs", [])  # Array of {base64, filename}
                    document_data = data.get("document_data")  # base64 Office document (single, for backward compat)
                    documents = data.get("documents", [])  # Array of {base64, filename, type}
                    
                    # If arrays are provided, use them; otherwise fall back to single values for backward compat
                    if images and not image_data:
                        image_data = images[0].get("base64") if images else None
                    if pdfs and not pdf_data:
                        pdf_data = pdfs[0].get("base64") if pdfs else None
                    if documents and not document_data:
                        document_data = documents[0].get("base64") if documents else None
                    if files and not file_content:
                        file_content = files[0].get("content") if files else None

                    # If image_path provided but no image_data, load from disk
                    if image_path and not image_data:
                        try:
                            # Log the image_path to debug emoji issues
                            logger.info(f"[CHAT] Loading image from path: {repr(image_path)} (length={len(image_path) if image_path else 0})")
                            loaded_image = storage_service.load_image_as_base64(image_path)
                            if loaded_image:
                                image_data = loaded_image
                                logger.debug(f"Loaded image from path: {image_path}")
                        except Exception as e:
                            logger.warning(f"[CHAT] Failed to load image from path {repr(image_path)}: {e}", exc_info=True)

                    # Extract text from PDF if provided
                    if pdf_data:
                        extracted = extract_pdf_text(pdf_data)
                        if extracted:
                            file_content = f"[PDF Document]\n\n{extracted}"

                    # Extract text from Office document if provided
                    if document_data:
                        extracted = extract_document_text(document_data)
                        if extracted:
                            file_content = f"[Office Document]\n\n{extracted}"

                    if not content and not file_content and not image_data:
                        continue

                    # Save uploaded files to disk and get paths
                    user_image_path = None
                    if image_data:
                        user_image_path = storage_service.save_image(user.username, conversation_id, image_data, "upload")
                    if file_content:
                        # Save file content for persistence (non-blocking - chat will work even if this fails)
                        try:
                            storage_service.save_file(user.username, conversation_id, file_content)
                        except Exception as e:
                            logger.warning(f"[CHAT] Failed to save file content to disk (chat will continue): {e}")

                    # Save user message with image path if uploaded
                    user_msg = Message(
                        conversation_id=conversation_id,
                        role="user",
                        content=content,
                        image_path=user_image_path
                    )
                    db.add(user_msg)
                    db.commit()

                    # Update conversation title if it's the first message
                    if len(conversation.messages) <= 1:
                        conversation.title = content[:50] + ("..." if len(content) > 50 else "")

                    conversation.updated_at = datetime.utcnow()
                    db.commit()

                    # Check for commands
                    command, arg = command_service.parse_command(content)

                    # Check for YouTube URLs (auto-summarize)
                    if not command:
                        youtube_result = await command_service.check_youtube_url(content)
                        if youtube_result:
                            # Save and send YouTube summary
                            assistant_msg = Message(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=youtube_result.get("content", "")
                            )
                            db.add(assistant_msg)
                            db.commit()
                            await manager.send_json(user.id, {
                                "type": "response",
                                "data": youtube_result
                            }, conn_id, conversation_id)
                            await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                            continue

                    if command:
                        # Execute command with stop check
                        try:
                            # Check if already stopped before starting
                            if manager.should_stop(user.id, conn_id):
                                logger.debug("Command cancelled before start")
                                continue

                            logger.debug(f"Executing command: {command} with arg: {arg[:50] if arg else ''}, has_image: {image_data is not None}")
                            last_prompt = manager.last_image_prompts.get(user.id)

                            # Create stop check function for long-running commands
                            def should_stop_command():
                                return manager.should_stop(user.id, conn_id)

                            # Prepare attachments for mail command - support multiple attachments
                            mail_attachments = None
                            if command == "mail":
                                import base64
                                mail_attachments = []

                                # Handle multiple image attachments
                                if images:
                                    for img in images:
                                        try:
                                            img_base64 = img.get("base64") or img  # Support both object and string
                                            if isinstance(img, dict):
                                                img_base64 = img.get("base64")
                                            else:
                                                img_base64 = img
                                            img_bytes = base64.b64decode(img_base64)
                                            filename = img.get("filename", "image") if isinstance(img, dict) else "image"
                                            content_type = "image/png"
                                            if img_base64.startswith("/9j/"):
                                                content_type = "image/jpeg"
                                                if not filename.endswith(('.jpg', '.jpeg')):
                                                    filename = f"{filename}.jpg" if '.' not in filename else filename.rsplit('.', 1)[0] + '.jpg'
                                            elif img_base64.startswith("R0lGOD"):
                                                content_type = "image/gif"
                                                if not filename.endswith('.gif'):
                                                    filename = f"{filename}.gif" if '.' not in filename else filename.rsplit('.', 1)[0] + '.gif'
                                            else:
                                                if not filename.endswith('.png'):
                                                    filename = f"{filename}.png" if '.' not in filename else filename.rsplit('.', 1)[0] + '.png'
                                            mail_attachments.append((filename, img_bytes, content_type))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process image attachment: {att_err}")
                                elif image_data:  # Backward compat: single image
                                    try:
                                        img_bytes = base64.b64decode(image_data)
                                        content_type = "image/png"
                                        if image_data.startswith("/9j/"):
                                            content_type = "image/jpeg"
                                        elif image_data.startswith("R0lGOD"):
                                            content_type = "image/gif"
                                        ext = content_type.split("/")[1]
                                        mail_attachments.append((f"image.{ext}", img_bytes, content_type))
                                    except Exception as att_err:
                                        logger.warning(f"Failed to process image attachment: {att_err}")

                                # Handle multiple PDF attachments
                                if pdfs:
                                    for pdf in pdfs:
                                        try:
                                            pdf_base64 = pdf.get("base64") if isinstance(pdf, dict) else pdf
                                            pdf_bytes = base64.b64decode(pdf_base64)
                                            filename = pdf.get("filename", "document.pdf") if isinstance(pdf, dict) else "document.pdf"
                                            mail_attachments.append((filename, pdf_bytes, "application/pdf"))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process PDF attachment: {att_err}")
                                elif pdf_data:  # Backward compat: single PDF
                                    try:
                                        pdf_bytes = base64.b64decode(pdf_data)
                                        mail_attachments.append(("document.pdf", pdf_bytes, "application/pdf"))
                                    except Exception as att_err:
                                        logger.warning(f"Failed to process PDF attachment: {att_err}")

                                # Handle multiple Office document attachments
                                if documents:
                                    for doc in documents:
                                        try:
                                            doc_base64 = doc.get("base64") if isinstance(doc, dict) else doc
                                            doc_bytes = base64.b64decode(doc_base64)
                                            doc_type = doc.get("type", "docx") if isinstance(doc, dict) else "docx"
                                            filename = doc.get("filename", "document") if isinstance(doc, dict) else "document"
                                            # Try to guess type from content
                                            content_type = "application/octet-stream"
                                            if doc_bytes[:4] == b'PK\x03\x04':  # ZIP-based (docx, xlsx, pptx)
                                                if b'word/' in doc_bytes[:2000]:
                                                    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                                    if not filename.endswith('.docx'):
                                                        filename = f"{filename}.docx" if '.' not in filename else filename.rsplit('.', 1)[0] + '.docx'
                                                elif b'xl/' in doc_bytes[:2000]:
                                                    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                                    if not filename.endswith('.xlsx'):
                                                        filename = f"{filename}.xlsx" if '.' not in filename else filename.rsplit('.', 1)[0] + '.xlsx'
                                                else:
                                                    if not filename.endswith('.pptx'):
                                                        filename = f"{filename}.pptx" if '.' not in filename else filename.rsplit('.', 1)[0] + '.pptx'
                                            mail_attachments.append((filename, doc_bytes, content_type))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process document attachment: {att_err}")
                                elif document_data:  # Backward compat: single document
                                    try:
                                        doc_bytes = base64.b64decode(document_data)
                                        # Try to guess type from content
                                        content_type = "application/octet-stream"
                                        filename = "document"
                                        if doc_bytes[:4] == b'PK\x03\x04':  # ZIP-based (docx, xlsx, pptx)
                                            if b'word/' in doc_bytes[:2000]:
                                                content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                                filename = "document.docx"
                                            elif b'xl/' in doc_bytes[:2000]:
                                                content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                                                filename = "spreadsheet.xlsx"
                                            else:
                                                filename = "document.docx"
                                        mail_attachments.append((filename, doc_bytes, content_type))
                                    except Exception as att_err:
                                        logger.warning(f"Failed to process document attachment: {att_err}")

                                # Handle multiple text file attachments (only if no PDF/document was sent)
                                if files and not pdfs and not documents:
                                    for file_item in files:
                                        try:
                                            file_content_item = file_item.get("content") if isinstance(file_item, dict) else file_item
                                            filename = file_item.get("filename", "attachment.txt") if isinstance(file_item, dict) else "attachment.txt"
                                            # Only attach raw text files, not extracted content
                                            if not file_content_item.startswith("[PDF Document]") and not file_content_item.startswith("[Office Document]"):
                                                if len(file_content_item) < 50000:  # Reasonable file size
                                                    mail_attachments.append((filename, file_content_item.encode("utf-8"), "text/plain"))
                                        except Exception as att_err:
                                            logger.warning(f"Failed to process text file attachment: {att_err}")
                                elif file_content and not pdf_data and not document_data:  # Backward compat: single file
                                    # Only attach raw text files, not extracted content
                                    if not file_content.startswith("[PDF Document]") and not file_content.startswith("[Office Document]"):
                                        if len(file_content) < 50000:  # Reasonable file size
                                            mail_attachments.append(("attachment.txt", file_content.encode("utf-8"), "text/plain"))

                                if not mail_attachments:
                                    mail_attachments = None

                            result = await command_service.execute_command(
                                command, arg, last_prompt,
                                stop_check=should_stop_command,
                                attachments=mail_attachments
                            )

                            # Check if stopped during execution
                            if manager.should_stop(user.id, conn_id):
                                logger.debug("Command stopped during execution")
                                manager.set_stop(user.id, False)
                                continue

                            logger.debug(f"Command result type: {result.get('type')}")
                        except Exception as cmd_err:
                            logger.error(f"Command execution failed: {type(cmd_err).__name__}: {cmd_err}", exc_info=True)
                            # Rollback any uncommitted transaction to prevent session corruption
                            try:
                                db.rollback()
                            except Exception:
                                pass
                            result = {"type": "text", "content": f"Error: {cmd_err}"}

                        # Save generated image to disk (non-blocking - don't fail if storage save fails)
                        generated_image_path = None
                        if result.get("type") == "generated_image" and result.get("prompt"):
                            manager.last_image_prompts[user.id] = result["prompt"]
                            # Save generated image to disk
                            if result.get("image"):
                                try:
                                    generated_image_path = storage_service.save_image(user.username, conversation_id, result["image"], "generated")
                                except Exception as save_err:
                                    logger.warning(f"Failed to save generated image to storage (non-fatal): {save_err}")
                                    # Continue without saving - image will still be displayed to user
                                    generated_image_path = None

                        # Save assistant response with image path
                        assistant_msg = None
                        try:
                            assistant_msg = Message(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=result.get("content", ""),
                                image_path=generated_image_path
                            )
                            db.add(assistant_msg)
                            db.commit()
                        except Exception as save_err:
                            logger.error(f"Failed to save assistant message: {save_err}")
                            assistant_msg = None
                            try:
                                db.rollback()
                            except Exception:
                                pass

                        # Add LLM follow-up for certain commands
                        if command in ("flood", "budget", "firewall"):
                            try:
                                # Truncate result for LLM context if too long
                                result_summary = result.get('content', '')
                                if len(result_summary) > 500:
                                    result_summary = result_summary[:500] + "..."

                                # Build context for LLM
                                follow_up_messages = [
                                    {"role": "system", "content": "You are a helpful assistant. Respond conversationally and briefly. One sentence max."},
                                    {"role": "user", "content": f"Command: {command} {arg}\nResult: {result_summary}\n\nGive a brief, friendly one-line response about this."}
                                ]

                                # Get LLM follow-up (non-streaming for simplicity)
                                follow_up_text = await chat_service.chat(follow_up_messages)
                                if follow_up_text:
                                    result["content"] = result.get("content", "") + "\n\n" + follow_up_text
                                    # Update saved message if it exists
                                    if assistant_msg:
                                        assistant_msg.content = result["content"]
                                        db.commit()
                            except Exception as e:
                                logger.exception(f"LLM follow-up failed: {e}")
                                try:
                                    db.rollback()
                                except Exception:
                                    pass

                        # Send response (with conn_id to ensure it goes to correct chat, queue if stale)
                        # Log image generation responses for debugging
                        if result.get("type") == "generated_image":
                            image_len = len(result.get("image", "")) if result.get("image") else 0
                            logger.info(f"[WEBSOCKET] Sending generated_image response: image_length={image_len}, has_prompt={bool(result.get('prompt'))}")
                        
                        await manager.send_json(user.id, {
                            "type": "response",
                            "data": result
                        }, conn_id, conversation_id)
                        # Signal end of response so TUI stops waiting
                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                    else:
                        # Check if intent detection is enabled
                        intent_enabled = db.query(Setting).filter(Setting.key == "intent_detection_enabled").first()
                        intent_enabled = (intent_enabled.value if intent_enabled else "true").lower() == "true"

                        if intent_enabled:
                            # Try AI-powered intent detection first
                            # Build context from file content/OCR for intent analysis
                            intent_context = ""
                            if file_content:
                                intent_context = file_content
                            elif image_data:
                                # Extract OCR for intent detection
                                ocr_for_intent = extract_image_text(image_data)
                                if ocr_for_intent:
                                    intent_context = ocr_for_intent

                            try:
                                intent_result = await intent_service.detect_intent(content, intent_context)
                                if intent_result and intent_result.get("action") != "none":
                                    logger.info(f"Intent detected: {intent_result['action']} (confidence: {intent_result.get('confidence', 0):.2f})")

                                    # Execute the detected intent
                                    action_result = await intent_service.execute_intent(intent_result)

                                    if action_result:
                                        # Check if stopped during execution
                                        if manager.should_stop(user.id, conn_id):
                                            logger.debug("Intent action stopped during execution")
                                            manager.set_stop(user.id, False)
                                            continue

                                        # Save assistant response
                                        assistant_msg = Message(
                                            conversation_id=conversation_id,
                                            role="assistant",
                                            content=action_result.get("content", "")
                                        )
                                        db.add(assistant_msg)
                                        db.commit()

                                        # Send response
                                        await manager.send_json(user.id, {
                                            "type": "response",
                                            "data": action_result
                                        }, conn_id, conversation_id)
                                        # Signal end of response so TUI stops waiting
                                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)
                                        continue  # Skip regular chat since action was taken
                            except Exception as intent_err:
                                logger.debug(f"Intent detection skipped: {intent_err}")
                                # Fall through to regular chat on any error

                        # Regular chat - stream response
                        # Wrap in try-finally to ensure stream_end is always sent
                        try:
                            # Build message history (exclude the just-added user message)
                            # Replace date placeholder in system prompt
                            system_prompt = chat_service.system_prompt.replace(
                                "{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d")
                            )
                            # Add plugin information to system prompt
                            plugin_prompt = plugin_service.build_system_prompt_addition(user.id)
                            if plugin_prompt:
                                system_prompt += plugin_prompt
                            
                            # Add user's custom LLM prompt if set
                            if hasattr(user, 'custom_llm_prompt') and user.custom_llm_prompt:
                                system_prompt += f"\n\n## User's Custom Instructions\nThe user has provided these custom instructions that you should follow:\n\n{user.custom_llm_prompt}\n"
                            
                            messages = [
                                {"role": "system", "content": system_prompt}
                            ]
                            # Get last 19 messages (excluding the one we just added)
                            for msg in conversation.messages[-21:-1]:
                                messages.append({"role": msg.role, "content": msg.content})

                            # Detect and fetch URLs in user message (with timeout to avoid hanging)
                            url_context = ""
                            urls = SearchService.extract_urls(content)
                            if urls:
                                logger.info(f"Detected URLs in message: {urls}")
                                try:
                                    # Add 15 second timeout for URL fetching to avoid long delays
                                    fetched = await asyncio.wait_for(
                                        search_service.fetch_urls(urls, max_urls=3),
                                        timeout=15
                                    )
                                    for result in fetched:
                                        if result.get("content") and not result.get("error"):
                                            logger.info(f"Fetched {len(result['content'])} chars from {result['url']}")
                                            url_context += f"\n\n---\nContent from {result['url']}:\nTitle: {result['title']}\n\n{result['content']}\n---"
                                        elif result.get("error"):
                                            logger.warning(f"Failed to fetch {result['url']}: {result['error']}")
                                            url_context += f"\n\n[Failed to fetch {result['url']}: {result['error']}]"
                                except asyncio.TimeoutError:
                                    logger.warning(f"URL fetching timed out after 15s for URLs: {urls}")
                                    url_context = "\n\n[Note: Could not fetch URL content due to timeout]"
                            
                            # Get ACTUAL context size for intelligent truncation
                            actual_ctx = 4096
                            try:
                                from app.services.inference_factory import get_inference_service
                                service = get_inference_service(db)
                                actual_ctx = getattr(service, 'num_ctx', 4096)
                            except Exception:
                                pass
                            
                            # Reserve ~2500 tokens for system/history/user/response, use rest for URL content
                            max_url_chars = max(500, int(actual_ctx * 4) - 10000)
                            if len(url_context) > max_url_chars:
                                logger.info(f"Truncating URL context from {len(url_context):,} to {max_url_chars:,} chars")
                                url_context = url_context[:max_url_chars] + "\n\n[URL content truncated to fit context window]"

                            # Add current message with file/image content if provided
                            if image_data:
                                # Use OCR to extract text from image
                                ocr_text = extract_image_text(image_data)
                                if ocr_text:
                                    user_request = content if content else "Please provide a detailed, objective summary and analysis of this document."
                                    messages.append({
                                        "role": "user",
                                        "content": f"""The user uploaded an image containing the following text (extracted via OCR):

---BEGIN EXTRACTED TEXT---
{ocr_text}
---END EXTRACTED TEXT---

User's request: {user_request}

Please analyze the above text objectively and thoroughly. Provide a comprehensive summary covering the main points, key details, and any important information found in the document."""
                                    })
                                else:
                                    messages.append({
                                        "role": "user",
                                        "content": f"{content or 'The user uploaded an image.'} [Note: An image was uploaded but no text could be extracted from it. Please ask the user to describe what they see.]"
                                    })
                            elif file_content:
                                # Get ACTUAL context size (may be reduced from configured value due to memory)
                                context_size = 4096  # Safe default
                                try:
                                    from app.services.inference_factory import get_inference_service
                                    service = get_inference_service(db)
                                    context_size = getattr(service, 'num_ctx', 4096)
                                except Exception as e:
                                    logger.debug(f"Could not get service context size: {e}")
                                
                                # Use ~50% of context tokens as chars for file content
                                # (accounting for system/history/response overhead)
                                max_file_chars = int(context_size * 2.0)
                                
                                if len(file_content) > max_file_chars:
                                    logger.info(f"Truncating file content from {len(file_content):,} to {max_file_chars:,} chars (actual context: {context_size})")
                                    file_content = file_content[:max_file_chars] + "\n\n[File content truncated - document is too large for context window]"
                                
                                # If user message is just "summarize" or similar, make the instruction explicit
                                user_message_lower = (content or "").lower().strip()
                                summarize_keywords = ["summarize", "summarise", "summary", "summarie"]
                                is_summarize_request = any(keyword in user_message_lower for keyword in summarize_keywords) and len(user_message_lower.split()) <= 3
                                
                                if is_summarize_request or not content or len(content.strip()) < 5:
                                    # User wants a summary or gave minimal instruction - be explicit
                                    messages.append({
                                        "role": "user",
                                        "content": f"The user uploaded a file and asked you to summarize it. Please provide a comprehensive summary of the following file content:\n\n```\n{file_content}\n```\n\nProvide a detailed summary covering the main points, key information, and important details from the document."
                                    })
                                else:
                                    # User provided specific instructions - include both file and their message
                                    messages.append({
                                        "role": "user",
                                        "content": f"Here is a file the user uploaded:\n\n```\n{file_content}\n```\n\nUser's message: {content}"
                                    })
                            elif url_context:
                                logger.info(f"Adding {len(url_context)} chars of URL context to message")
                                messages.append({
                                    "role": "user",
                                    "content": f"{content}\n\n[The following web content was fetched from URLs mentioned in the user's message:]{url_context}"
                                })
                            else:
                                messages.append({"role": "user", "content": content})

                            # Stream response with thinking tag filtering
                            # Import from central location for consistency
                            from app.services.text_utils import find_thinking_open
                            BUFFER_MARGIN = 20  # Enough for longest closing tag

                            full_response = ""
                            buffer = ""
                            in_thinking = False
                            current_close_tag = None  # Track which closing tag we're looking for

                            async for chunk in chat_service.chat_stream(messages):
                                # Check if user requested stop OR switched to another chat
                                if manager.should_stop(user.id, conn_id):
                                    break
                                full_response += chunk
                                buffer += chunk
                                logger.info(f"[STREAM] Chunk received, len={len(chunk)}, buffer_len={len(buffer)}")

                                # Filter out thinking content in real-time
                                while True:
                                    if not in_thinking:
                                        # Look for start of any thinking tag
                                        think_start, tag_pair = find_thinking_open(buffer)
                                        if think_start == -1:
                                            # No thinking tag, send buffered content (keep margin in case tag is split)
                                            # For very short responses, send immediately to avoid empty bubbles
                                            if len(buffer) > BUFFER_MARGIN:
                                                to_send = buffer[:-BUFFER_MARGIN]
                                                buffer = buffer[-BUFFER_MARGIN:]
                                                if to_send:
                                                    logger.info(f"[STREAM] Sending chunk, len={len(to_send)}")
                                                    await manager.send_json(user.id, {
                                                        "type": "stream",
                                                        "data": {"content": to_send}
                                                    }, conn_id)
                                            # If buffer is small but we have content, send it immediately
                                            elif len(buffer) > 0:
                                                to_send = buffer
                                                buffer = ""
                                                logger.info(f"[STREAM] Sending small chunk immediately, len={len(to_send)}")
                                                await manager.send_json(user.id, {
                                                    "type": "stream",
                                                    "data": {"content": to_send}
                                                }, conn_id)
                                            break
                                        else:
                                            # Found opening tag, send content before it and enter thinking mode
                                            if think_start > 0:
                                                await manager.send_json(user.id, {
                                                    "type": "stream",
                                                    "data": {"content": buffer[:think_start]}
                                                }, conn_id)
                                            open_tag, close_tag = tag_pair
                                            buffer = buffer[think_start + len(open_tag):]
                                            current_close_tag = close_tag
                                            in_thinking = True
                                    else:
                                        # In thinking mode, look for matching end tag
                                        think_end = buffer.lower().find(current_close_tag)
                                        if think_end == -1:
                                            # Still in thinking, discard buffered thinking content but keep margin
                                            if len(buffer) > BUFFER_MARGIN:
                                                buffer = buffer[-BUFFER_MARGIN:]
                                            break
                                        else:
                                            # Found closing tag, exit thinking mode
                                            buffer = buffer[think_end + len(current_close_tag):]
                                            current_close_tag = None
                                            in_thinking = False

                            # Send any remaining buffered content
                            if buffer and not in_thinking:
                                logger.info(f"[STREAM] Sending final buffer, len={len(buffer)}")
                                await manager.send_json(user.id, {
                                    "type": "stream",
                                    "data": {"content": buffer}
                                }, conn_id)

                            # Ensure we always send stream_end, even if there was an error or stop request
                            logger.info(f"[STREAM] Complete, total_len={len(full_response)}, stopped={manager.should_stop(user.id, conn_id)}")
                            
                            # Always send stream_end, even if full_response is empty
                            await manager.send_json(user.id, {"type": "stream_end"}, conn_id)

                            # Save assistant response
                            if full_response:
                                clean_response = chat_service.strip_thinking_tags(full_response)

                                # Check for plugin tool calls in the response
                                tool_calls = plugin_service.parse_tool_calls(clean_response)
                                if tool_calls:
                                    # Execute tool calls
                                    stripped_response, results = await plugin_service.execute_all_tool_calls(
                                        clean_response, user.id
                                    )

                                    # Send tool results to client (formatted for display)
                                    for r in results:
                                        formatted_result = plugin_service.format_result_for_display(
                                            r['plugin'], r['action'], r['result']
                                        )
                                        await manager.send_json(user.id, {
                                            "type": "plugin_result",
                                            "plugin": r['plugin'],
                                            "action": r['action'],
                                            "result": formatted_result
                                        }, conn_id)

                                    # Get AI follow-up response with tool results
                                    result_context = plugin_service.format_results_for_ai(results)
                                    follow_up_messages = messages + [
                                        {"role": "assistant", "content": stripped_response},
                                        {"role": "user", "content": f"Plugin results:{result_context}\n\nRespond helpfully to the user based on these results. Be conversational but informative."}
                                    ]

                                    # Signal frontend to clear current content for follow-up
                                    await manager.send_json(user.id, {
                                        "type": "stream_clear"
                                    }, conn_id)

                                    # Stream follow-up response
                                    follow_up_response = ""
                                    async for chunk in chat_service.chat_stream(follow_up_messages):
                                        if manager.should_stop(user.id, conn_id):
                                            break
                                        follow_up_response += chunk
                                        await manager.send_json(user.id, {
                                            "type": "stream",
                                            "data": {"content": chunk}
                                        }, conn_id)

                                    # Save combined response
                                    final_response = chat_service.strip_thinking_tags(follow_up_response) if follow_up_response else stripped_response
                                    assistant_msg = Message(
                                        conversation_id=conversation_id,
                                        role="assistant",
                                        content=final_response
                                    )
                                else:
                                    assistant_msg = Message(
                                        conversation_id=conversation_id,
                                        role="assistant",
                                        content=clean_response
                                    )
                                    db.add(assistant_msg)
                                    db.commit()

                        except Exception as stream_err:
                            logger.error(f"Error during streaming: {stream_err}", exc_info=True)
                            # Try to send error message to client
                            try:
                                await manager.send_json(user.id, {
                                    "type": "stream",
                                    "data": {"content": f"\n\n[Error: {str(stream_err)}]"}
                                }, conn_id)
                            except Exception:
                                pass
                        finally:
                            # Always send stream_end to prevent UI hanging
                            await manager.send_json(user.id, {"type": "stream_end"}, conn_id)

        except WebSocketDisconnect:
            if user:
                manager.disconnect(user.id)
    finally:
        if db:
            db.close()

from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from typing import List, Optional
import json
from datetime import datetime

from app.database import get_db, SessionLocal
from app.models import User, Conversation, Message
from app.schemas import ConversationCreate, ConversationResponse, ConversationWithMessages, MessageResponse
from app.auth import get_current_user, get_user_from_websocket
from app.services.chat_service import ChatService
from app.services.command_service import CommandService
from app.services.storage_service import StorageService
from app.services.document_service import extract_pdf_text, extract_document_text, extract_image_text
from app.services.email_service import EmailService

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


@router.get("/conversations/{conversation_id}/messages", response_model=List[MessageResponse])
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

    return conversation.messages


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

    async def connect(self, user_id: int, conversation_id: int, websocket: WebSocket) -> int:
        await websocket.accept()
        # Stop any previous streaming for this user (prevents messages going to wrong chat)
        self.stop_flags[user_id] = True
        self.active_connections[user_id] = websocket
        self.conversation_ids[user_id] = conversation_id
        # Increment connection ID so old streams know they're stale
        self._next_conn_id += 1
        self.connection_ids[user_id] = self._next_conn_id
        # Reset stop flag for new connection
        self.stop_flags[user_id] = False
        return self._next_conn_id

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
        print(f"[QUEUE] Saved pending result for user {user_id}, conv {conversation_id}")

    def get_pending_results(self, user_id: int, conversation_id: int) -> list:
        """Get and clear pending results for a conversation"""
        key = (user_id, conversation_id)
        results = self.pending_results.pop(key, [])
        if results:
            print(f"[QUEUE] Delivering {len(results)} pending result(s) to user {user_id}, conv {conversation_id}")
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


@router.websocket("/ws/chat/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: int):
    db = SessionLocal()
    conn_id = None
    user = None
    try:
        user = await get_user_from_websocket(websocket, db)
        if not user:
            await websocket.accept()
            await websocket.send_json({"type": "error", "message": "Please log in again"})
            await websocket.close(code=4001)
            return

        # Verify conversation belongs to user
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id
        ).first()
        if not conversation:
            await websocket.accept()
            await websocket.send_json({"type": "error", "message": "Conversation not found"})
            await websocket.close(code=4004)
            return

        # Use manager.connect() which handles stopping old streams and returns connection ID
        conn_id = await manager.connect(user.id, conversation_id, websocket)

        # Check for and deliver any pending results from previous sessions
        pending = manager.get_pending_results(user.id, conversation_id)
        for pending_data in pending:
            try:
                await websocket.send_json(pending_data)
            except Exception:
                pass

        chat_service = ChatService(db)
        command_service = CommandService(db)
        storage_service = StorageService(db)

        try:
            while True:
                try:
                    # Use receive_text to get better error info, then parse JSON
                    raw_text = await websocket.receive_text()
                    print(f"[DEBUG] Received raw text length: {len(raw_text)}")
                    data = json.loads(raw_text)
                except json.JSONDecodeError as json_err:
                    print(f"[DEBUG] JSON parse failed: {json_err}")
                    continue
                except Exception as recv_err:
                    print(f"[DEBUG] Failed to receive: {type(recv_err).__name__}: {recv_err}")
                    raise
                print(f"[DEBUG] Received: type={data.get('type')}, content={data.get('content', '')[:50] if data.get('content') else ''}, has_image={data.get('image_data') is not None}")

                if data.get("type") == "stop":
                    manager.set_stop(user.id, True)
                    continue

                if data.get("type") == "message":
                    manager.set_stop(user.id, False)  # Reset for new message
                    content = data.get("content", "").strip()
                    image_data = data.get("image_data")  # base64 image
                    file_content = data.get("file_content")  # text file content
                    pdf_data = data.get("pdf_data")  # base64 PDF
                    document_data = data.get("document_data")  # base64 Office document

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

                    # Save uploaded files to disk
                    if image_data:
                        storage_service.save_image(user.username, conversation_id, image_data, "upload")
                    if file_content:
                        storage_service.save_file(user.username, conversation_id, file_content)

                    # Save user message
                    user_msg = Message(
                        conversation_id=conversation_id,
                        role="user",
                        content=content
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

                    if command:
                        # Execute command
                        try:
                            print(f"[DEBUG] Executing command: {command} with arg: {arg[:50] if arg else ''}, has_image: {image_data is not None}")
                            last_prompt = manager.last_image_prompts.get(user.id)
                            result = await command_service.execute_command(
                                command, arg, last_prompt,
                                image_data=image_data,
                                file_content=file_content
                            )
                            print(f"[DEBUG] Command result type: {result.get('type')}")
                        except Exception as cmd_err:
                            print(f"[DEBUG] Command execution failed: {type(cmd_err).__name__}: {cmd_err}")
                            import traceback
                            traceback.print_exc()
                            result = {"type": "text", "content": f"Error: {cmd_err}"}

                        # Track image prompts for regen
                        if result.get("type") == "generated_image" and result.get("prompt"):
                            manager.last_image_prompts[user.id] = result["prompt"]
                            # Save generated image to disk
                            if result.get("image"):
                                storage_service.save_image(user.username, conversation_id, result["image"], "generated")

                        # Save assistant response
                        assistant_msg = Message(
                            conversation_id=conversation_id,
                            role="assistant",
                            content=result.get("content", "")
                        )
                        db.add(assistant_msg)
                        db.commit()

                        # Send response (with conn_id to ensure it goes to correct chat, queue if stale)
                        await manager.send_json(user.id, {
                            "type": "response",
                            "data": result
                        }, conn_id, conversation_id)
                    else:
                        # Regular chat - stream response
                        # Build message history (exclude the just-added user message)
                        # Replace date placeholder in system prompt
                        system_prompt = chat_service.system_prompt.replace(
                            "{{CURRENT_DATE}}", datetime.utcnow().strftime("%Y-%m-%d")
                        )
                        messages = [
                            {"role": "system", "content": system_prompt}
                        ]
                        # Get last 19 messages (excluding the one we just added)
                        for msg in conversation.messages[-21:-1]:
                            messages.append({"role": msg.role, "content": msg.content})

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
                            messages.append({
                                "role": "user",
                                "content": f"Here is a file the user uploaded:\n\n```\n{file_content}\n```\n\nUser's message: {content}"
                            })
                        else:
                            messages.append({"role": "user", "content": content})

                        # Stream response
                        full_response = ""
                        async for chunk in chat_service.chat_stream(messages):
                            # Check if user requested stop OR switched to another chat
                            if manager.should_stop(user.id, conn_id):
                                break
                            full_response += chunk
                            await manager.send_json(user.id, {
                                "type": "stream",
                                "content": chunk
                            }, conn_id)

                        # Save assistant response
                        if full_response:
                            clean_response = chat_service.strip_thinking_tags(full_response)
                            assistant_msg = Message(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=clean_response
                            )
                            db.add(assistant_msg)
                            db.commit()

                        await manager.send_json(user.id, {"type": "stream_end"}, conn_id)

        except WebSocketDisconnect:
            manager.disconnect(user.id)
    finally:
        db.close()

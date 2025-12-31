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
from app.services.document_service import extract_pdf_text, extract_document_text

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


# WebSocket for real-time chat

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[int, WebSocket] = {}
        self.last_image_prompts: dict[int, str] = {}  # Track last image prompt per user

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)

    async def send_json(self, user_id: int, data: dict):
        ws = self.active_connections.get(user_id)
        if ws:
            await ws.send_json(data)


manager = ConnectionManager()


@router.websocket("/ws/chat/{conversation_id}")
async def websocket_chat(websocket: WebSocket, conversation_id: int):
    await websocket.accept()

    db = SessionLocal()
    try:
        user = await get_user_from_websocket(websocket, db)
        if not user:
            await websocket.send_json({"type": "error", "message": "Please log in again"})
            await websocket.close(code=4001)
            return

        # Verify conversation belongs to user
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id
        ).first()
        if not conversation:
            await websocket.send_json({"type": "error", "message": "Conversation not found"})
            await websocket.close(code=4004)
            return

        manager.active_connections[user.id] = websocket

        chat_service = ChatService(db)
        command_service = CommandService(db)
        storage_service = StorageService(db)

        try:
            while True:
                data = await websocket.receive_json()

                if data.get("type") == "message":
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
                        last_prompt = manager.last_image_prompts.get(user.id)
                        result = await command_service.execute_command(
                            command, arg, last_prompt,
                            image_data=image_data,
                            file_content=file_content
                        )

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

                        # Send response
                        await manager.send_json(user.id, {
                            "type": "response",
                            "data": result
                        })
                    else:
                        # Regular chat - stream response
                        # Build message history (exclude the just-added user message)
                        messages = [
                            {"role": "system", "content": "You are a helpful, friendly AI assistant. Be concise but thorough."}
                        ]
                        # Get last 19 messages (excluding the one we just added)
                        for msg in conversation.messages[-21:-1]:
                            messages.append({"role": msg.role, "content": msg.content})

                        # Add current message with file/image content if provided
                        has_vision_message = False
                        if image_data:
                            # Try vision API format for image
                            messages.append({
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": content or "What is this image?"},
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}}
                                ]
                            })
                            has_vision_message = True
                        elif file_content:
                            messages.append({
                                "role": "user",
                                "content": f"Here is a file the user uploaded:\n\n```\n{file_content}\n```\n\nUser's message: {content}"
                            })
                        else:
                            messages.append({"role": "user", "content": content})

                        # Stream response
                        full_response = ""
                        vision_failed = False
                        async for chunk in chat_service.chat_stream(messages):
                            # Check for vision API errors
                            if has_vision_message and "Error:" in chunk and full_response == "":
                                vision_failed = True
                                break
                            full_response += chunk
                            await manager.send_json(user.id, {
                                "type": "stream",
                                "content": chunk
                            })

                        # Fallback if vision failed - retry without image
                        if vision_failed and has_vision_message:
                            messages[-1] = {
                                "role": "user",
                                "content": f"{content or 'The user uploaded an image.'} [Note: Image was uploaded but vision is not available]"
                            }
                            async for chunk in chat_service.chat_stream(messages):
                                full_response += chunk
                                await manager.send_json(user.id, {
                                    "type": "stream",
                                    "content": chunk
                                })

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

                        await manager.send_json(user.id, {"type": "stream_end"})

        except WebSocketDisconnect:
            manager.disconnect(user.id)
    finally:
        db.close()

"""Matrix integration router — login, logout, room listing."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/matrix", tags=["matrix"])


class MatrixConnectRequest(BaseModel):
    homeserver: str
    username: str
    password: str


@router.post("/connect")
async def connect_matrix(
    data: MatrixConnectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Login to Matrix with username/password and store the access token."""
    from app.services.matrix_service import login as matrix_login

    homeserver = data.homeserver.strip().rstrip("/")
    if not homeserver.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Homeserver URL must start with http:// or https://")

    username = data.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    try:
        result = await matrix_login(homeserver, username, data.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Matrix login error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not connect to homeserver: {e}")

    current_user.matrix_enabled = True
    current_user.matrix_homeserver = homeserver
    current_user.matrix_user_id = result["user_id"]
    current_user.matrix_access_token = result["access_token"]
    db.commit()

    return {"ok": True, "user_id": result["user_id"]}


@router.post("/disconnect")
async def disconnect_matrix(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Logout from Matrix and remove stored credentials."""
    if current_user.matrix_access_token and current_user.matrix_homeserver:
        from app.services.matrix_service import logout as matrix_logout
        try:
            await matrix_logout(current_user.matrix_homeserver, current_user.matrix_access_token)
        except Exception as e:
            logger.warning(f"Matrix remote logout failed (continuing): {e}")

    current_user.matrix_enabled = False
    current_user.matrix_homeserver = None
    current_user.matrix_user_id = None
    current_user.matrix_access_token = None
    db.commit()

    return {"ok": True}


@router.get("/rooms")
async def list_rooms(
    current_user: User = Depends(get_current_user),
):
    """Return the list of Matrix rooms the user has joined."""
    if not current_user.matrix_enabled or not current_user.matrix_access_token or not current_user.matrix_homeserver:
        raise HTTPException(status_code=400, detail="Matrix is not connected")

    from app.services.matrix_service import get_joined_rooms

    try:
        rooms = await get_joined_rooms(current_user.matrix_homeserver, current_user.matrix_access_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Matrix list rooms error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not fetch rooms: {e}")

    return {"rooms": rooms}


class MatrixTestDmRequest(BaseModel):
    bot_user_id: str


@router.post("/test-dm")
async def send_test_dm(
    data: MatrixTestDmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a DM room with the bot user and send a test message."""
    if not current_user.matrix_enabled or not current_user.matrix_access_token or not current_user.matrix_homeserver:
        raise HTTPException(status_code=400, detail="Matrix is not connected")

    bot_user_id = data.bot_user_id.strip()
    if not bot_user_id.startswith("@") or ":" not in bot_user_id:
        raise HTTPException(status_code=400, detail="Invalid Matrix user ID format (expected @user:server)")

    from app.services.matrix_service import create_or_get_dm_room, send_message as matrix_send

    try:
        room_id = await create_or_get_dm_room(
            current_user.matrix_homeserver,
            current_user.matrix_access_token,
            bot_user_id,
        )
        await matrix_send(
            current_user.matrix_homeserver,
            current_user.matrix_access_token,
            room_id,
            "Hello from Posterchanai! Your Matrix bot integration is working.",
        )
        return {"ok": True, "room_id": room_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Matrix test DM error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=str(e))


class MatrixCommandRequest(BaseModel):
    matrix_user_id: str
    command: str


@router.post("/command")
async def execute_matrix_command(
    data: MatrixCommandRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Execute a posterchanai command on behalf of a Matrix user.

    Called by the posterchan Matrix bot. Looks up the posterchanai account
    linked to the sender's Matrix user ID. Optionally authenticated via a
    Bearer API key for additional security when the key is configured.
    """
    from app.models import APIKey

    sender_matrix_id = data.matrix_user_id.strip()
    if not sender_matrix_id:
        raise HTTPException(status_code=400, detail="matrix_user_id is required")

    # Optional Bearer API key — if provided, the key's owner must match the linked Matrix user
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        api_key_obj = db.query(APIKey).filter(
            APIKey.key == token,
            APIKey.is_active == True,
        ).first()
        if not api_key_obj:
            raise HTTPException(status_code=401, detail="Invalid API key")
        user = db.query(User).filter(
            User.id == api_key_obj.user_id,
            User.matrix_enabled == True,
            User.matrix_user_id == sender_matrix_id,
        ).first()
        if not user:
            raise HTTPException(status_code=403, detail="Matrix user is not linked to this account")
    else:
        # No API key — look up directly by linked Matrix user ID
        user = db.query(User).filter(
            User.matrix_enabled == True,
            User.matrix_user_id == sender_matrix_id,
        ).first()
        if not user:
            raise HTTPException(status_code=403, detail="Matrix user is not linked to any account")

    command_str = data.command.strip()
    if not command_str:
        raise HTTPException(status_code=400, detail="Command is required")

    # Parse command
    from app.services.command_service import CommandService
    cmd_service = CommandService(db, user=user)
    command, arg = cmd_service.parse_command(command_str)

    if not command:
        # Not a recognized command — let the AI respond
        from app.services.chat_service import ChatService
        chat_svc = ChatService(db, user=user)
        try:
            reply = await chat_svc.chat([
                {"role": "system", "content": chat_svc.system_prompt},
                {"role": "user", "content": command_str},
            ])
        except Exception as e:
            logger.error(f"Matrix command chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        return {"result": reply}

    try:
        result = await cmd_service.execute_command(command, arg)
        content = result.get("content", "")
        return {"result": content}
    except Exception as e:
        logger.error(f"Matrix command execution error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

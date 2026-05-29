"""Matrix integration router — login, logout, room listing."""

import logging
from fastapi import APIRouter, Depends, HTTPException
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

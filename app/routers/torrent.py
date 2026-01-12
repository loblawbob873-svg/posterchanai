"""
Torrent API router for remote access to the built-in torrent client.
Supports both local libtorrent and remote server forwarding via bt_server_url.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
import logging
import httpx

from app.database import get_db
from app.auth import get_current_user
from app.models import User, Setting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/torrent", tags=["torrent"])
security = HTTPBearer(auto_error=False)


def get_torrent_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """
    Get current user or validate server-to-server API token.
    Accepts:
    - Normal user JWT auth (header or cookie)
    - Server API token (bt_server_token) for server-to-server requests
    """
    # Check for server-to-server API token first
    if credentials and credentials.credentials:
        token = credentials.credentials
        # Check if this is the server API token
        server_token = db.query(Setting).filter(Setting.key == "bt_server_token").first()
        if server_token and server_token.value and token == server_token.value:
            # Valid server token - return None to indicate system access (no specific user)
            # This allows server-to-server requests without requiring a user account
            return None

    # Fall back to normal user authentication
    try:
        return get_current_user(request, credentials, db)
    except HTTPException:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated"
        )


async def forward_to_remote(
    db: Session,
    request: Request,
    endpoint: str,
    method: str = "GET",
    json_body: dict = None
) -> dict:
    """Forward request to remote torrent server."""
    server_url = db.query(Setting).filter(Setting.key == "bt_server_url").first()
    if not server_url or not server_url.value:
        return None

    # Get server-to-server API token
    server_token = db.query(Setting).filter(Setting.key == "bt_server_token").first()

    # Also try to forward the user's cookie (fallback)
    access_token = request.cookies.get("access_token", "")

    url = f"{server_url.value.rstrip('/')}/api/torrent{endpoint}"
    headers = {}

    # Prefer server token for server-to-server auth
    if server_token and server_token.value:
        headers["Authorization"] = f"Bearer {server_token.value}"
    elif access_token:
        headers["Cookie"] = f"access_token={access_token}"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=json_body)

            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=response.json().get("detail", "Remote server error")
                )
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to remote torrent server: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot reach remote torrent server: {e}")


def get_remote_server_url(db: Session) -> Optional[str]:
    """Check if remote server URL is configured."""
    server_url = db.query(Setting).filter(Setting.key == "bt_server_url").first()
    return server_url.value if server_url and server_url.value else None


class AddTorrentRequest(BaseModel):
    magnet: str


class TorrentActionRequest(BaseModel):
    num: int
    delete_files: Optional[bool] = False


def get_bt_service(db: Session):
    """Get the local libtorrent service."""
    bt_enabled = db.query(Setting).filter(Setting.key == "bt_enabled").first()
    if not bt_enabled or bt_enabled.value.lower() != "true":
        return None

    def get_setting(key: str, default: str = "") -> str:
        s = db.query(Setting).filter(Setting.key == key).first()
        return s.value if s and s.value else default

    proxy_host = get_setting("bt_proxy_host")
    if not proxy_host:
        return None

    try:
        from app.services.libtorrent_service import LibtorrentService
        return LibtorrentService.get_instance(
            download_dir=get_setting("bt_download_dir", "/var/lib/posterchanai/torrents"),
            proxy_host=proxy_host,
            proxy_port=int(get_setting("bt_proxy_port", "8118")),
            listen_port=int(get_setting("bt_listen_port", "6881")),
        )
    except ImportError:
        return None


@router.get("/list")
async def list_torrents(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """List all active torrents."""
    # Check for remote server first
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/list")

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    torrents = service.list_torrents()
    return {
        "torrents": [
            {
                "num": i + 1,
                "info_hash": t.info_hash,
                "name": t.name,
                "size": t.size,
                "downloaded": t.downloaded,
                "uploaded": t.uploaded,
                "progress": t.progress,
                "download_rate": t.download_rate,
                "upload_rate": t.upload_rate,
                "state": t.state,
                "seeders": t.seeders,
                "peers": t.peers,
                "is_paused": t.is_paused,
                "is_finished": t.is_finished,
                "save_path": t.save_path,
            }
            for i, t in enumerate(torrents)
        ]
    }


@router.post("/add")
async def add_torrent(
    body: AddTorrentRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Add a torrent by magnet link."""
    # Check for remote server first
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/add", method="POST", json_body={"magnet": body.magnet})

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    if not body.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Invalid magnet link")

    info_hash = service.add_magnet(body.magnet)
    return {"info_hash": info_hash, "message": "Torrent added"}


@router.post("/pause")
async def pause_torrent(
    body: TorrentActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Pause a torrent by number."""
    # Check for remote server first
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/pause", method="POST", json_body={"num": body.num})

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(body.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if service.pause(info_hash):
        return {"message": f"Paused torrent #{body.num}"}
    raise HTTPException(status_code=500, detail="Failed to pause torrent")


@router.post("/resume")
async def resume_torrent(
    body: TorrentActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Resume a torrent by number."""
    # Check for remote server first
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/resume", method="POST", json_body={"num": body.num})

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(body.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if service.resume(info_hash):
        return {"message": f"Resumed torrent #{body.num}"}
    raise HTTPException(status_code=500, detail="Failed to resume torrent")


@router.post("/remove")
async def remove_torrent(
    body: TorrentActionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Remove a torrent by number."""
    # Check for remote server first
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/remove", method="POST", json_body={"num": body.num, "delete_files": body.delete_files})

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(body.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if service.remove(info_hash, delete_files=body.delete_files):
        msg = "and deleted files" if body.delete_files else "(files kept)"
        return {"message": f"Removed torrent #{body.num} {msg}"}
    raise HTTPException(status_code=500, detail="Failed to remove torrent")


@router.get("/info/{num}")
async def get_torrent_info(
    num: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Get detailed info for a torrent."""
    # Check for remote server first
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, f"/info/{num}")

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    t = service.get_torrent(info_hash)
    if not t:
        raise HTTPException(status_code=404, detail="Torrent not found")

    files = service.get_files(info_hash)

    return {
        "info_hash": t.info_hash,
        "name": t.name,
        "size": t.size,
        "downloaded": t.downloaded,
        "uploaded": t.uploaded,
        "progress": t.progress,
        "download_rate": t.download_rate,
        "upload_rate": t.upload_rate,
        "state": t.state,
        "seeders": t.seeders,
        "peers": t.peers,
        "is_paused": t.is_paused,
        "is_finished": t.is_finished,
        "save_path": t.save_path,
        "files": files,
    }

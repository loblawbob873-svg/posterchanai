"""
Torrent API router for remote access to the built-in torrent client.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
import logging

from app.database import get_db
from app.auth import get_current_user
from app.models import User, Setting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/torrent", tags=["torrent"])


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
            scgi_host=get_setting("bt_scgi_host", "0.0.0.0"),
            scgi_port=int(get_setting("bt_scgi_port", "5001")),
            listen_port=int(get_setting("bt_listen_port", "6881")),
        )
    except ImportError:
        return None


@router.get("/list")
async def list_torrents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all active torrents."""
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
    request: AddTorrentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add a torrent by magnet link."""
    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    if not request.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Invalid magnet link")

    info_hash = service.add_magnet(request.magnet)
    return {"info_hash": info_hash, "message": "Torrent added"}


@router.post("/pause")
async def pause_torrent(
    request: TorrentActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Pause a torrent by number."""
    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(request.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if service.pause(info_hash):
        return {"message": f"Paused torrent #{request.num}"}
    raise HTTPException(status_code=500, detail="Failed to pause torrent")


@router.post("/resume")
async def resume_torrent(
    request: TorrentActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Resume a torrent by number."""
    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(request.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if service.resume(info_hash):
        return {"message": f"Resumed torrent #{request.num}"}
    raise HTTPException(status_code=500, detail="Failed to resume torrent")


@router.post("/remove")
async def remove_torrent(
    request: TorrentActionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove a torrent by number."""
    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(request.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if service.remove(info_hash, delete_files=request.delete_files):
        msg = "and deleted files" if request.delete_files else "(files kept)"
        return {"message": f"Removed torrent #{request.num} {msg}"}
    raise HTTPException(status_code=500, detail="Failed to remove torrent")


@router.get("/info/{num}")
async def get_torrent_info(
    num: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get detailed info for a torrent."""
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

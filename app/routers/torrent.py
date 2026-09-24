"""
Torrent API router for remote access to the built-in torrent client.
Supports both local libtorrent and remote server forwarding via bt_server_url.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
import logging
import httpx

from app.database import get_db
from app.utils import lb_auth
from app.auth import get_current_user
from app.services import instance_membership
from app.models import User
from app.services import settings_store
from app.services.torrent_service import scrape_torrents, search_torrents
from app.services.nyaa_service import search_nyaa

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/torrent", tags=["torrent"])
security = HTTPBearer(auto_error=False)


async def get_torrent_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """
    Get current user for torrent API.
    Load-balanced requests from other posterchanai nodes are allowed without authentication.
    Accepts:
    - Normal user JWT auth (header or cookie)
    - Load-balanced requests (X-Posterchanai-Load-Balanced header)
    """
    try:
        # A peer node, proven by the shared secret once `lb_shared_secret` is set. The bare header
        # alone is settable by any caller — see app/utils/lb_auth.py.
        if lb_auth.is_internal(request):
            logger.debug("[TORRENT] ✓ Load-balanced request from another posterchanai node - allowing without auth")
            return None  # System access, no specific user

        # Fall back to normal user authentication
        user = get_current_user(request, credentials, db)
        await instance_membership.require_user(user)
        # Per-user torrent access (Admin → Users). Admins/user-1 always allowed; the flag
        # defaults True so existing users are unaffected. Load-balanced traffic returns above
        # with user=None and is never gated, so remote bt-server forwarding still works.
        if user is not None and not (
            getattr(user, "is_admin", False) or user.id == 1 or getattr(user, "can_torrent", True)
        ):
            raise HTTPException(status_code=403,
                                detail="You don't have access to torrents on this server. Ask an admin to enable it.")
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[TORRENT] Auth error: {e}")
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
    server_url = settings_store.get("bt_server_url")
    if not server_url:
        return None

    # Server-to-server requests don't need authentication
    url = f"{server_url.rstrip('/')}/api/torrent{endpoint}"
    headers = lb_auth.headers()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            logger.info(f"[TORRENT] Forwarding to {url} (load-balanced)")
            if method == "GET":
                response = await client.get(url, headers=headers)
            else:
                response = await client.post(url, headers=headers, json=json_body)

            logger.info(f"[TORRENT] Remote response: {response.status_code}")

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as e:
                    logger.error(f"[TORRENT] Failed to parse JSON response: {e}, body: {response.text[:500]}")
                    raise HTTPException(status_code=502, detail="Remote server returned invalid JSON")
            else:
                # Try to get error detail from JSON, fall back to text
                try:
                    error_detail = response.json().get("detail", "Remote server error")
                except Exception:
                    error_detail = response.text[:200] if response.text else f"HTTP {response.status_code}"
                logger.error(f"[TORRENT] Remote error: {response.status_code} - {error_detail}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=error_detail
                )
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to remote torrent server: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot reach remote torrent server: {e}")


def get_remote_server_url(db: Session) -> Optional[str]:
    """Check if remote server URL is configured."""
    server_url = settings_store.get("bt_server_url")
    return server_url if server_url else None


class AddTorrentRequest(BaseModel):
    magnet: str = ""
    torrent_url: str = ""  # a .torrent URL — the server downloads + adds it (add_torrent_file)
    # Download ONLY these file indices (everything else gets libtorrent priority 0). None = the
    # whole torrent, which is what every existing caller sends and therefore what it keeps doing.
    # A magnet has no file list at add time, so a selection sent with one is recorded and applied
    # the moment the metadata lands — see LibtorrentService.select_files.
    files: Optional[list[int]] = None


class TorrentActionRequest(BaseModel):
    num: int
    delete_files: Optional[bool] = False


class TorrentFilesRequest(BaseModel):
    num: int
    # Either: `files` = the complete set of indices to download (everything else switched off), or
    # `priorities` = a PARTIAL map of index → libtorrent priority (0-7) leaving the rest alone.
    files: Optional[list[int]] = None
    priorities: Optional[dict[str, int]] = None


class FeedRequest(BaseModel):
    url: str = ""
    title: str = ""
    include: str = ""
    exclude: str = ""
    enabled: Optional[bool] = True


def get_bt_service(db: Session):
    """Get the local libtorrent service."""
    if not settings_store.get_bool("bt_enabled"):
        return None

    def get_setting(key: str, default: str = "") -> str:
        s = settings_store.get(key)
        return s if s else default

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
    except ConnectionError as e:
        logger.error(f"[TORRENT] Proxy connection failed: {e}")
        return None
    except Exception as e:
        logger.error(f"[TORRENT] Failed to start service: {e}")
        return None


def _items(results) -> list[dict]:
    """The one row shape every browse endpoint answers with (catalog, search, nyaa)."""
    return [
        {
            "num": i + 1,
            "title": t.title,
            "magnet": t.magnet,
            "size": t.size,
            "seeders": t.seeders,
            "leechers": t.leechers,
            "url": t.url or "",
        }
        for i, t in enumerate(results)
    ]


@router.get("/catalog")
async def catalog(
    category: str = Query("movies", description="One of: all, movies, tv, music, anime"),
    limit: int = Query(15, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Browse torrents by category (for native app). Returns list with title, magnet, size, seeders, leechers. Runs on this server (scraping), not forwarded to bt_server_url.

    `all` is what the chat's bare `torrents` command shows -- every category at once, each row naming
    its own (`limit` is then PER category, as the command's overview is)."""
    category = category.lower()
    if category == "all":
        from app.services.torrent_service import scrape_all_categories
        try:
            grouped = await scrape_all_categories(db, limit_per_category=limit)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        items = []
        for cat, results in grouped.items():
            for row in _items(results):
                items.append({**row, "num": len(items) + 1, "category": cat})
        return {"category": "all", "items": items}
    if category not in ("movies", "tv", "music", "anime"):
        raise HTTPException(status_code=400, detail="category must be one of: all, movies, tv, music, anime")
    try:
        results = await scrape_torrents(db, category, limit)
        return {
            "category": category,
            "items": _items(results),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(15, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Search torrents (for native app). Returns list with title, magnet, size, seeders, leechers. Runs on this server, not forwarded."""
    try:
        results = await search_torrents(db, q.strip(), limit)
        return {
            "query": q,
            "items": _items(results),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/nyaa")
async def nyaa_search(
    q: str = Query("", description="Search terms; empty = nyaa's newest uploads"),
    limit: int = Query(20, ge=1, le=75),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Search nyaa.si (or, with no query, list its newest uploads). Returns title, magnet, size, seeders, leechers."""
    try:
        results = await search_nyaa(q.strip(), limit=limit, latest=True)
        return {
            "query": q,
            "items": _items(results),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    """Add a torrent by magnet link or .torrent URL."""
    # Check for remote server first — forward both fields so a chained remote can do the work.
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/add", method="POST",
                                       json_body={"magnet": body.magnet, "torrent_url": body.torrent_url})

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    # A .torrent URL: download the file here (this node owns the torrent client + proxy) and add it.
    if body.torrent_url and not body.magnet:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as _c:
                _resp = await _c.get(body.torrent_url, headers={"User-Agent": "Mozilla/5.0"})
                _resp.raise_for_status()
                _data = _resp.content
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Couldn't download .torrent: {e}")
        try:
            info_hash = service.add_torrent_file(_data, user_id=current_user.id if current_user else None)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Couldn't add .torrent: {e}")
        return {"info_hash": info_hash, "message": "Torrent added",
                "selection": _apply_selection(service, info_hash, body.files)}

    if not body.magnet.startswith("magnet:"):
        raise HTTPException(status_code=400, detail="Invalid magnet link")

    info_hash = service.add_magnet(body.magnet, user_id=current_user.id if current_user else None)
    return {"info_hash": info_hash, "message": "Torrent added",
            "selection": _apply_selection(service, info_hash, body.files)}


def _apply_selection(service, info_hash: str, files):
    """Put an add-time file selection onto a freshly added torrent.

    Returns "" when no selection was asked for — the caller must be able to tell "you chose
    everything" from "your choice was recorded", because with a magnet the choice is almost always
    PENDING (no metadata yet) and a client that reported it as applied would show a file list it
    had not actually filtered.
    """
    if files is None:
        return ""
    try:
        return service.select_files(info_hash, list(files))
    except Exception as e:
        logger.error(f"[TORRENT] could not apply file selection to {info_hash}: {e}")
        return "failed"


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


# ---------------------------------------------------------------- per-file selection
#
# A torrent is often a season pack, a discography or a game with six language packs — and the whole
# of it is a download nobody asked for. libtorrent's per-file priority is the mechanism (0 = do not
# download) and nothing here deletes anything: a file switched off simply stops being fetched, and
# switching it back on resumes rather than restarts.

@router.get("/files/{num}")
async def list_torrent_files(
    num: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """The files inside a torrent, each with its index, size, priority and own progress."""
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, f"/files/{num}")

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    files = service.get_files(info_hash)
    pending = None
    try:
        pending = service.pending_selection(info_hash)
    except AttributeError:
        pending = None
    # An EMPTY list is "the metadata has not arrived yet", not "this torrent has no files", and the
    # two need different words on screen — a magnet shows nothing for its first few seconds and a
    # client that said "no files" would be reporting a fault.
    return {"info_hash": info_hash, "num": num, "files": files,
            "metadata": bool(files), "pending_selection": pending}


@router.post("/files")
async def set_torrent_files(
    body: TorrentFilesRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Choose which files inside a torrent to download."""
    if get_remote_server_url(db):
        return await forward_to_remote(db, request, "/files", method="POST",
                                       json_body={"num": body.num, "files": body.files,
                                                  "priorities": body.priorities})

    service = get_bt_service(db)
    if not service:
        raise HTTPException(status_code=503, detail="Torrent client not configured")

    info_hash = service.get_hash_by_number(body.num)
    if not info_hash:
        raise HTTPException(status_code=404, detail="Torrent not found")

    if body.priorities is not None:
        if not service.set_file_priorities(info_hash, body.priorities):
            raise HTTPException(status_code=409,
                                detail="That torrent has no file list yet (still fetching metadata)")
        return {"message": "Priorities updated", "selection": "applied",
                "files": service.get_files(info_hash)}

    if body.files is None:
        raise HTTPException(status_code=400, detail="Send `files` (indices to download) or `priorities`")
    # An EMPTY selection would pause the torrent by the back door — every file at priority 0 leaves
    # a download that can never finish and says nothing about why. Refuse it and name the button
    # that does what they meant.
    if not body.files:
        raise HTTPException(status_code=400,
                            detail="Select at least one file, or use Pause to stop the whole torrent")

    state = service.select_files(info_hash, list(body.files))
    if state == "unknown":
        raise HTTPException(status_code=404, detail="Torrent not found")
    return {"message": "Selection saved", "selection": state,
            "files": service.get_files(info_hash)}


# ---------------------------------------------------------------- RSS feeds
#
# These are NOT forwarded to bt_server_url: the subscription list is a decision made on the node
# somebody administers, and the poller resolves bt_server_url itself when it adds. Forwarding the
# feed CRUD as well would put the list on the remote node, where this node's UI cannot show it.

@router.get("/feeds")
async def list_feeds(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """The torrent RSS subscriptions on this node."""
    from app.services import torrent_rss_service as trss
    try:
        feeds = trss.list_feeds()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "feeds": feeds,
        "enabled": settings_store.get_bool("torrent_rss_enabled"),
        "interval_minutes": settings_store.get("torrent_rss_interval_minutes") or "30",
        "max_per_poll": settings_store.get("torrent_rss_max_per_poll") or "5",
    }


@router.post("/feeds")
async def add_feed(
    body: FeedRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Subscribe to a torrent RSS feed. The URL goes through the news reader's SSRF guard."""
    from app.services import torrent_rss_service as trss
    try:
        feed = trss.add_feed(body.url, body.title, body.include, body.exclude,
                             True if body.enabled is None else bool(body.enabled))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"feed": feed, "message": "Feed added"}


@router.post("/feeds/{feed_id}")
async def edit_feed(
    feed_id: str,
    body: FeedRequest,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Edit a subscription's title, filters or enabled state."""
    from app.services import torrent_rss_service as trss
    try:
        feed = trss.update_feed(feed_id, url=body.url or None, title=body.title,
                                include=body.include, exclude=body.exclude, enabled=body.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400 if "URL" in str(e) else 404, detail=str(e))
    return {"feed": feed, "message": "Feed updated"}


@router.delete("/feeds/{feed_id}")
async def delete_feed(
    feed_id: str,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Unsubscribe. The torrents it already added are left alone — removing a subscription is not
    an instruction to delete what it downloaded."""
    from app.services import torrent_rss_service as trss
    if not trss.remove_feed(feed_id):
        raise HTTPException(status_code=404, detail="No such feed")
    return {"message": "Feed removed"}


@router.post("/feeds/{feed_id}/check")
async def check_feed(
    feed_id: str,
    add: bool = Query(False, description="Actually add what matches, instead of previewing it"),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_torrent_user)
):
    """Poll one feed now.

    Defaults to a PREVIEW (`add=false`): it fetches, filters and reports what WOULD be taken without
    adding anything or marking anything seen. That is what makes a filter writable — a filter you
    cannot try is one you find out about after it has downloaded the wrong season.
    """
    from app.services import torrent_rss_service as trss
    feed = trss.get_feed(feed_id)
    if not feed:
        raise HTTPException(status_code=404, detail="No such feed")
    result = await trss.poll_feed(feed, dry_run=not add)
    return result

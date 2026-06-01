"""Misskey API client — posting notes and MiAuth token exchange."""

import uuid
import logging
import httpx

logger = logging.getLogger(__name__)


async def check_miauth_session(instance_url: str, session_id: str) -> dict:
    """Call Misskey's MiAuth check endpoint and return the access token payload."""
    url = instance_url.rstrip("/") + f"/api/miauth/{session_id}/check"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url)
        resp.raise_for_status()
        return resp.json()


def _detect_mime(image_bytes: bytes) -> tuple[str, str]:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image.jpg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image.png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image.gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp", "image.webp"
    return "image/jpeg", "image.jpg"


async def upload_file(instance_url: str, token: str, image_bytes: bytes, mime: str = "") -> str:
    """Upload a file to Misskey Drive and return the file ID."""
    detected_mime, filename = _detect_mime(image_bytes)
    mime = mime or detected_mime
    url = instance_url.rstrip("/") + "/api/drive/files/create"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            data={"i": token},
            files={"file": (filename, image_bytes, mime)},
        )
    logger.info(f"Misskey file upload: HTTP {resp.status_code} — {resp.text[:200]}")
    resp.raise_for_status()
    file_id = resp.json().get("id")
    if not file_id:
        raise ValueError(f"Misskey file upload returned no id: {resp.text[:200]}")
    return file_id


async def post_note(
    instance_url: str,
    token: str,
    text: str,
    visibility: str = "public",
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
    reply_id: str | None = None,
) -> dict:
    """Create a note on the configured Misskey instance. Uploads image_bytes if provided.
    Pass reply_id to post the note as a reply to an existing note."""
    file_ids = []
    if image_bytes:
        file_id = await upload_file(instance_url, token, image_bytes, image_mime)
        file_ids.append(file_id)

    url = instance_url.rstrip("/") + "/api/notes/create"
    payload: dict = {"i": token, "text": text, "visibility": visibility}
    if file_ids:
        payload["fileIds"] = file_ids
    if reply_id:
        payload["replyId"] = reply_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def fetch_notifications(instance_url: str, token: str, since_id: str | None = None, limit: int = 20) -> list[dict]:
    """Fetch recent notifications (raw Misskey objects). Returns newest-first; when since_id
    is given, only notifications newer than it are returned."""
    url = instance_url.rstrip("/") + "/api/i/notifications"
    payload: dict = {"i": token, "limit": limit}
    if since_id:
        payload["sinceId"] = since_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


def build_miauth_url(instance_url: str, session_id: str, callback_url: str, app_name: str = "PosterChanAI") -> str:
    """Build the MiAuth authorization URL to redirect the user to."""
    base = instance_url.rstrip("/")
    callback_encoded = httpx.URL(callback_url).__str__()
    return (
        f"{base}/miauth/{session_id}"
        f"?name={app_name}"
        f"&callback={callback_encoded}"
        f"&permission=read:notifications,write:notes"
    )

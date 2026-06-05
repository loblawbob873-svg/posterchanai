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
    if image_bytes[4:8] == b"ftyp":
        return "video/mp4", "video.mp4"
    if image_bytes[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", "video.webm"
    return "image/jpeg", "image.jpg"


async def upload_file(instance_url: str, token: str, image_bytes: bytes, mime: str = "") -> str:
    """Upload a file to Misskey Drive and return the file ID."""
    detected_mime, filename = _detect_mime(image_bytes)
    # Trust the byte sniff for video — callers default image_mime to an image/*
    # type, which would otherwise upload an MP4 mislabeled as image.jpg.
    mime = detected_mime if detected_mime.startswith("video/") else (mime or detected_mime)
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


async def call(instance_url: str, token: str, method: str, params: dict | None = None):
    """Generic Misskey API call: POST /api/<method> with {"i": token, **params}.

    Returns the parsed JSON (or {} for an empty 200 body); raises on non-2xx. This mirrors the
    standalone bot client's misskey_post so the botframework Misskey shim can route every call
    through this shared service. Used by botframework/misskey_shim.py."""
    url = instance_url.rstrip("/") + f"/api/{method}"
    body = {"i": token}
    if params:
        body.update(params)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json() if resp.text else {}


async def post_note(
    instance_url: str,
    token: str,
    text: str,
    visibility: str = "public",
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
    reply_id: str | None = None,
    media: list[tuple[bytes, str]] | None = None,
    renote_id: str | None = None,
) -> dict:
    """Create a note on the configured Misskey instance. Uploads image_bytes if provided, or
    every (bytes, mime) in `media` when given. Pass reply_id to reply to an existing note, or
    renote_id (with text) to quote-renote one."""
    file_ids = []
    if media:
        for (m_bytes, m_mime) in media:
            file_ids.append(await upload_file(instance_url, token, m_bytes, m_mime))
    elif image_bytes:
        file_id = await upload_file(instance_url, token, image_bytes, image_mime)
        file_ids.append(file_id)

    url = instance_url.rstrip("/") + "/api/notes/create"
    payload: dict = {"i": token, "text": text, "visibility": visibility}
    if file_ids:
        payload["fileIds"] = file_ids
    if reply_id:
        payload["replyId"] = reply_id
    if renote_id:
        payload["renoteId"] = renote_id   # with text → quote-renote; without → plain renote
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


_TIMELINE_ENDPOINTS = {
    "home": "/api/notes/timeline",
    "global": "/api/notes/global-timeline",
    "local": "/api/notes/local-timeline",
}


async def fetch_timeline(instance_url: str, token: str, timeline_type: str = "home",
                         since_id: str | None = None, limit: int = 20) -> list[dict]:
    """Fetch recent notes from the home/global/local timeline (raw Misskey notes,
    newest-first). When since_id is given, only newer notes are returned."""
    endpoint = _TIMELINE_ENDPOINTS.get(timeline_type, _TIMELINE_ENDPOINTS["home"])
    url = instance_url.rstrip("/") + endpoint
    payload: dict = {"i": token, "limit": limit}
    if since_id:
        payload["sinceId"] = since_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def fetch_children(instance_url: str, token: str, note_id: str, limit: int = 30) -> list[dict]:
    """Fetch replies (descendants) of a note, raw Misskey notes."""
    url = instance_url.rstrip("/") + "/api/notes/children"
    payload = {"i": token, "noteId": note_id, "limit": limit, "depth": 5}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def fetch_conversation(instance_url: str, token: str, note_id: str, limit: int = 20) -> list[dict]:
    """Fetch a note's ancestors (the reply chain up to the root), raw Misskey notes.
    /api/notes/conversation returns them, typically nearest-first."""
    url = instance_url.rstrip("/") + "/api/notes/conversation"
    payload = {"i": token, "noteId": note_id, "limit": limit}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def create_reaction(instance_url: str, token: str, note_id: str, reaction: str = "❤️") -> None:
    """React to a note (the Misskey equivalent of a favourite). Returns 204 on success."""
    url = instance_url.rstrip("/") + "/api/notes/reactions/create"
    payload = {"i": token, "noteId": note_id, "reaction": reaction}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()


async def renote(instance_url: str, token: str, note_id: str) -> dict:
    """Renote (boost) an existing note."""
    url = instance_url.rstrip("/") + "/api/notes/create"
    payload = {"i": token, "renoteId": note_id}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def resolve_note(instance_url: str, token: str, uri: str) -> dict | None:
    """Resolve a remote post by its canonical AP URI to the local note object on this
    instance (so a member can act on it from their own instance). Returns the note dict
    or None if it can't be resolved."""
    url = instance_url.rstrip("/") + "/api/ap/show"
    payload = {"i": token, "uri": uri}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            return None
        data = resp.json()
    # /api/ap/show returns {"type": "Note"|"User", "object": {...}}
    if isinstance(data, dict) and data.get("type") == "Note":
        return data.get("object")
    return None


def build_miauth_url(instance_url: str, session_id: str, callback_url: str, app_name: str = "PosterChanAI") -> str:
    """Build the MiAuth authorization URL to redirect the user to.

    Note: `callback` is intentionally NOT percent-encoded — Misskey's MiAuth expects the
    raw callback URL here, and ours is a fixed internal path (no special chars). `name`
    encoded for safety; `permission` must stay literal (Misskey parses it unencoded).
    """
    from urllib.parse import quote
    base = instance_url.rstrip("/")
    return (
        f"{base}/miauth/{session_id}"
        f"?name={quote(app_name)}"
        f"&callback={callback_url}"
        # write:reactions is needed for the timeline bridge's ❤ favourite (a Misskey reaction);
        # write:notes covers posting notes, renotes (boost) and replies.
        f"&permission=read:notifications,write:notes,write:reactions"
    )

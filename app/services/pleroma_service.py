"""Pleroma/Mastodon API client — OAuth2 app registration, token exchange, and posting."""

import logging
import httpx

logger = logging.getLogger(__name__)


async def register_app(instance_url: str, redirect_uri: str, app_name: str = "PosterChanAI") -> dict:
    """Register this app with the Pleroma/Mastodon instance.

    Returns a dict containing at least ``client_id`` and ``client_secret``.
    """
    url = instance_url.rstrip("/") + "/api/v1/apps"
    payload = {
        "client_name": app_name,
        "redirect_uris": redirect_uri,
        "scopes": "read write",
        "website": instance_url,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, data=payload)
        resp.raise_for_status()
        return resp.json()


async def exchange_code(
    instance_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> str:
    """Exchange an authorization code for an access token. Returns the token string."""
    url = instance_url.rstrip("/") + "/oauth/token"
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, data=payload)
        resp.raise_for_status()
        data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {data}")
    return token


def build_auth_url(instance_url: str, client_id: str, redirect_uri: str) -> str:
    """Build the OAuth2 authorization URL to redirect the user to."""
    base = instance_url.rstrip("/")
    from urllib.parse import urlencode
    params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "read write",
    })
    return f"{base}/oauth/authorize?{params}"


def _detect_mime(image_bytes: bytes) -> tuple[str, str]:
    """Return (mime_type, extension) by inspecting the image header bytes."""
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
    return "image/jpeg", "image.jpg"  # sensible default


async def upload_media(instance_url: str, access_token: str, image_bytes: bytes, mime: str = "") -> str:
    """Upload media to Pleroma/Mastodon and return the media ID."""
    base = instance_url.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    detected_mime, filename = _detect_mime(image_bytes)
    # Trust the byte sniff for video — callers default image_mime to an image/*
    # type, which would otherwise upload an MP4 mislabeled as image.jpg.
    mime = detected_mime if detected_mime.startswith("video/") else (mime or detected_mime)
    # Try v1 first (works on Pleroma and all Mastodon), v2 is Mastodon 3.1.4+
    for endpoint in (f"{base}/api/v1/media", f"{base}/api/v2/media"):
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                endpoint,
                headers=headers,
                files={"file": (filename, image_bytes, mime)},
            )
        logger.info(f"Pleroma media upload {endpoint}: HTTP {resp.status_code} — {resp.text[:200]}")
        if resp.status_code in (200, 202):
            media_id = resp.json().get("id")
            if media_id:
                return media_id
        elif resp.status_code == 404:
            continue
        elif resp.status_code in (401, 403) and endpoint.endswith("/api/v2/media"):
            raise ValueError(
                "Pleroma returned 403 Insufficient permissions. "
                "Your token was issued without write:media scope. "
                "Please go to User Settings → Pleroma, disconnect, and reconnect to get a new token."
            )
        elif resp.status_code in (401, 403):
            continue  # Try the next endpoint before giving up
        elif resp.status_code == 422:
            raise ValueError(f"Pleroma rejected media (422): {resp.text[:300]}")
        else:
            raise ValueError(f"Media upload failed HTTP {resp.status_code}: {resp.text[:300]}")
    raise ValueError("Media upload failed: no endpoint succeeded")


async def post_status(
    instance_url: str,
    access_token: str,
    text: str,
    visibility: str = "public",
    image_bytes: bytes | None = None,
    image_mime: str = "image/png",
    in_reply_to_id: str | None = None,
    media: list[tuple[bytes, str]] | None = None,
) -> dict:
    """Post a status to a Pleroma or Mastodon instance. Uploads image_bytes if provided, or
    every (bytes, mime) in `media` when given. Pass in_reply_to_id to reply to a status."""
    media_ids = []
    if media:
        for (m_bytes, m_mime) in media:
            media_ids.append(await upload_media(instance_url, access_token, m_bytes, m_mime))
    elif image_bytes:
        media_id = await upload_media(instance_url, access_token, image_bytes, image_mime)
        media_ids.append(media_id)

    url = instance_url.rstrip("/") + "/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload: dict = {"status": text, "visibility": visibility}
    if media_ids:
        payload["media_ids"] = media_ids
    if in_reply_to_id:
        payload["in_reply_to_id"] = in_reply_to_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def fetch_notifications(instance_url: str, access_token: str, since_id: str | None = None, limit: int = 20) -> list[dict]:
    """Fetch recent notifications (raw Mastodon/Pleroma objects). When since_id is given,
    only notifications newer than it are returned (newest-first)."""
    url = instance_url.rstrip("/") + "/api/v1/notifications"
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict = {"limit": limit}
    if since_id:
        params["since_id"] = since_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def fetch_timeline(instance_url: str, access_token: str, timeline_type: str = "home",
                         since_id: str | None = None, limit: int = 20) -> list[dict]:
    """Fetch recent statuses from the home/public timeline (raw Mastodon/Pleroma statuses,
    newest-first). 'global' → public; 'local' → public?local=true. When since_id is given,
    only newer statuses are returned."""
    base = instance_url.rstrip("/")
    if timeline_type == "home":
        url = f"{base}/api/v1/timelines/home"
    else:
        url = f"{base}/api/v1/timelines/public"
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict = {"limit": limit}
    if timeline_type == "local":
        params["local"] = "true"
    if since_id:
        params["since_id"] = since_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def fetch_context(instance_url: str, access_token: str, status_id: str) -> dict:
    """Fetch a status's thread context ({"ancestors": [...], "descendants": [...]})."""
    url = instance_url.rstrip("/") + f"/api/v1/statuses/{status_id}/context"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}


async def favourite_status(instance_url: str, access_token: str, status_id: str) -> dict:
    """Favourite (like) a status."""
    url = instance_url.rstrip("/") + f"/api/v1/statuses/{status_id}/favourite"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def reblog_status(instance_url: str, access_token: str, status_id: str) -> dict:
    """Reblog (boost) a status."""
    url = instance_url.rstrip("/") + f"/api/v1/statuses/{status_id}/reblog"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def emoji_react(instance_url: str, access_token: str, status_id: str, emoji: str) -> dict:
    """Add an emoji reaction to a status (Pleroma extension; not on vanilla Mastodon).
    `emoji` is a unicode emoji or a `:shortcode:`. Raises on non-2xx (caller may fall back
    to a plain favourite)."""
    from urllib.parse import quote
    url = instance_url.rstrip("/") + f"/api/v1/pleroma/statuses/{status_id}/reactions/{quote(emoji, safe='')}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def resolve_status(instance_url: str, access_token: str, uri: str) -> dict | None:
    """Resolve a remote post by its canonical AP URI to the local status on this instance
    (so a member can act on it from their own instance). Returns the status dict or None."""
    url = instance_url.rstrip("/") + "/api/v2/search"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"q": uri, "resolve": "true", "type": "statuses", "limit": 1}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    statuses = (data or {}).get("statuses") or []
    return statuses[0] if statuses else None


async def verify_credentials(instance_url: str, access_token: str) -> dict:
    """Verify that the access token is valid by calling the credentials endpoint."""
    url = instance_url.rstrip("/") + "/api/v1/accounts/verify_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

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
    return "image/jpeg", "image.jpg"  # sensible default


async def upload_media(instance_url: str, access_token: str, image_bytes: bytes, mime: str = "") -> str:
    """Upload media to Pleroma/Mastodon and return the media ID."""
    base = instance_url.rstrip("/")
    headers = {"Authorization": f"Bearer {access_token}"}
    detected_mime, filename = _detect_mime(image_bytes)
    mime = mime or detected_mime
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
) -> dict:
    """Post a status to a Pleroma or Mastodon instance. Uploads image_bytes if provided."""
    media_ids = []
    if image_bytes:
        media_id = await upload_media(instance_url, access_token, image_bytes, image_mime)
        media_ids.append(media_id)

    url = instance_url.rstrip("/") + "/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload: dict = {"status": text, "visibility": visibility}
    if media_ids:
        payload["media_ids"] = media_ids
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def verify_credentials(instance_url: str, access_token: str) -> dict:
    """Verify that the access token is valid by calling the credentials endpoint."""
    url = instance_url.rstrip("/") + "/api/v1/accounts/verify_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

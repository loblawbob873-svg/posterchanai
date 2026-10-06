"""Pleroma/Mastodon API client — OAuth2 app registration, token exchange, and posting."""

import logging
import re

import httpx

from app.services.proxy_utils import afallback_transport

logger = logging.getLogger(__name__)


async def password_grant(
    instance_url: str,
    username: str,
    password: str,
    scopes: str = "read write follow push",
    app_name: str = "PosterChanAI",
) -> str:
    """Mint an access token from a username/password via the OAuth2 password grant.

    Registers a throwaway OAuth app (out-of-band redirect) on the instance, then exchanges
    the credentials for a token. Used by Admin → Bots so an admin can connect a Pleroma bot
    account by typing its password instead of running the browser authorization-code flow.
    Returns the token string. Pleroma/Mastodon only.
    """
    base = instance_url.rstrip("/")
    oob = "urn:ietf:wg:oauth:2.0:oob"
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=20) as client:
        app_resp = await client.post(base + "/api/v1/apps", data={
            "client_name": app_name,
            "redirect_uris": oob,
            "scopes": scopes,
            "website": base,
        })
        app_resp.raise_for_status()
        app = app_resp.json()
        client_id, client_secret = app.get("client_id"), app.get("client_secret")
        if not client_id or not client_secret:
            raise ValueError(f"App registration returned no client credentials: {app}")

        tok_resp = await client.post(base + "/oauth/token", data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scopes,
        })
        tok_resp.raise_for_status()
        data = tok_resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError(f"No access_token in response: {data}")
    return token


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
        async with httpx.AsyncClient(transport=afallback_transport(), timeout=60) as client:
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
    content_type: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Post a status to a Pleroma or Mastodon instance. Uploads image_bytes if provided, or
    every (bytes, mime) in `media` when given. Pass in_reply_to_id to reply to a status.
    `content_type` (e.g. "text/markdown") is a Pleroma extension; omit for the instance default.
    `idempotency_key` sets the Mastodon `Idempotency-Key` header so the SAME key never creates a
    duplicate status (server-side dedup) — used by the bridge so a crash/replay can't double-post."""
    media_ids = []
    if media:
        for (m_bytes, m_mime) in media:
            media_ids.append(await upload_media(instance_url, access_token, m_bytes, m_mime))
    elif image_bytes:
        media_id = await upload_media(instance_url, access_token, image_bytes, image_mime)
        media_ids.append(media_id)

    url = instance_url.rstrip("/") + "/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    payload: dict = {"status": text, "visibility": visibility}
    if media_ids:
        payload["media_ids"] = media_ids
    if in_reply_to_id:
        payload["in_reply_to_id"] = in_reply_to_id
    if content_type:
        payload["content_type"] = content_type
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=15) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def update_credentials(instance_url: str, access_token: str, display_name: str | None = None,
                             note: str | None = None, avatar_bytes: bytes | None = None,
                             avatar_mime: str = "image/png") -> dict | None:
    """Update the authenticated account's profile (display name / bio / avatar) — used to copy a
    Nostr profile onto a freshly-created bridge account."""
    url = instance_url.rstrip("/") + "/api/v1/accounts/update_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}
    data: dict = {}
    if display_name is not None:
        data["display_name"] = display_name
    if note is not None:
        data["note"] = note
    files = None
    if avatar_bytes:
        files = {"avatar": ("avatar", avatar_bytes, avatar_mime)}
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=30) as client:
        resp = await client.patch(url, headers=headers, data=data, files=files)
    if resp.status_code != 200:
        return None
    return resp.json()


async def fetch_status(instance_url: str, access_token: str, status_id: str) -> dict | None:
    """Fetch a single status by id (raw Mastodon/Pleroma object), or None if not found."""
    url = instance_url.rstrip("/") + f"/api/v1/statuses/{status_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=15) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None


async def fetch_notifications(instance_url: str, access_token: str, since_id: str | None = None,
                              limit: int = 20, min_id: str | None = None) -> list[dict]:
    """Fetch recent notifications (raw Mastodon/Pleroma objects). `since_id` returns the NEWEST after
    the id (gap-prone). `min_id` returns those immediately after the id and paginates forward without
    gaps — advance min_id to the newest returned id to drain a backlog (no dropped items)."""
    url = instance_url.rstrip("/") + "/api/v1/notifications"
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict = {"limit": limit}
    if min_id:
        params["min_id"] = min_id
    elif since_id:
        params["since_id"] = since_id
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=15) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []


async def emoji_react(instance_url: str, access_token: str, status_id: str, emoji: str) -> dict:
    """Add an emoji reaction to a status (Pleroma extension; not on vanilla Mastodon).
    `emoji` is a unicode emoji or a `:shortcode:`. Raises on non-2xx (caller may fall back
    to a plain favourite)."""
    url = _reaction_url(instance_url, status_id, emoji)
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=15) as client:
        resp = await client.put(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _reaction_url(instance_url: str, status_id: str, emoji: str) -> str:
    """PUT adds / DELETE removes — the emoji is a PATH segment, so it must be fully escaped
    (a `:shortcode:`'s colons and any unicode alike)."""
    from urllib.parse import quote
    return (instance_url.rstrip("/")
            + f"/api/v1/pleroma/statuses/{status_id}/reactions/{quote(emoji, safe='')}")


async def resolve_status(instance_url: str, access_token: str, uri: str) -> dict | None:
    """Resolve a remote post by its canonical AP URI to the local status on this instance
    (so a member can act on it from their own instance). Returns the status dict or None."""
    url = instance_url.rstrip("/") + "/api/v2/search"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"q": uri, "resolve": "true", "type": "statuses", "limit": 1}
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=20) as client:
        resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return None
        data = resp.json()
    statuses = (data or {}).get("statuses") or []
    return statuses[0] if statuses else None


def _link_next(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from a Mastodon/Pleroma Link header, or None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


async def _fetch_account_list(instance_url: str, access_token: str, path: str, limit: int = 80) -> list[dict]:
    """Page through an account-list endpoint (/api/v1/blocks, /api/v1/mutes, /followers), returning all
    raw account objects (bounded). Followers/following paginate on an internal RELATIONSHIP id carried
    in the `Link: rel="next"` header — NOT the account id — so follow that header; fall back to max_id
    (account id) only when no Link header is present (older servers)."""
    out: list[dict] = []
    seen: set = set()   # dedup by account id + detect a stuck/looping cursor (a page that adds nothing new)
    headers = {"Authorization": f"Bearer {access_token}"}
    url = instance_url.rstrip("/") + path
    params: dict = {"limit": limit}
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=15) as client:
        for _ in range(30):   # hard page cap so a huge list can't run unbounded (~2400 @ limit 80)
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            added = 0
            for a in batch:
                aid = a.get("id") if isinstance(a, dict) else None
                if aid is not None and aid in seen:
                    continue
                if aid is not None:
                    seen.add(aid)
                out.append(a)
                added += 1
            # No NEW accounts on this page → a stuck/looping cursor (some instances hand back a rel="next"
            # that never advances, repeating the same window forever). Stop rather than spin to the page
            # cap. This also naturally ends a healthy list (its final page repeats nothing).
            if added == 0:
                break
            # Follow the rel="next" cursor whenever present — followers/following endpoints page on a
            # relationship id and legitimately return SHORT pages (fewer than `limit`) that still
            # continue, so a `len(batch) < limit` break BEFORE this would truncate a large list. Fall
            # back to short-page + max_id only when the server sent no usable Link header (older servers)
            # or a cursor that didn't move.
            nxt = _link_next(resp.headers.get("link") or resp.headers.get("Link"))
            if nxt and nxt != url:
                url, params = nxt, {}   # the next URL already carries the correct cursor
                continue
            if nxt and nxt == url:
                break                   # cursor didn't advance → stop
            if len(batch) < limit:
                break
            last = batch[-1].get("id") if isinstance(batch[-1], dict) else None
            if not last:
                break
            params = {"limit": limit, "max_id": last}
    return out


async def verify_credentials(instance_url: str, access_token: str) -> dict:
    """Verify that the access token is valid by calling the credentials endpoint."""
    url = instance_url.rstrip("/") + "/api/v1/accounts/verify_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(transport=afallback_transport(), timeout=10) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()

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
        "scopes": "read:accounts write:statuses",
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
        "scope": "read:accounts write:statuses",
    })
    return f"{base}/oauth/authorize?{params}"


async def post_status(instance_url: str, access_token: str, text: str, visibility: str = "public") -> dict:
    """Post a status to a Pleroma or Mastodon instance using the Mastodon-compatible API."""
    url = instance_url.rstrip("/") + "/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"status": text, "visibility": visibility}
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

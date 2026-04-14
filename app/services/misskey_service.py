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


async def post_note(instance_url: str, token: str, text: str, visibility: str = "public") -> dict:
    """Create a note on the configured Misskey instance."""
    url = instance_url.rstrip("/") + "/api/notes/create"
    payload = {
        "i": token,
        "text": text,
        "visibility": visibility,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


def build_miauth_url(instance_url: str, session_id: str, callback_url: str, app_name: str = "PosterChanAI") -> str:
    """Build the MiAuth authorization URL to redirect the user to."""
    base = instance_url.rstrip("/")
    callback_encoded = httpx.URL(callback_url).__str__()
    return (
        f"{base}/miauth/{session_id}"
        f"?name={app_name}"
        f"&callback={callback_encoded}"
        f"&permission=write:notes"
    )

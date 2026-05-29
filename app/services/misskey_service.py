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


async def upload_file(instance_url: str, token: str, image_bytes: bytes, mime: str = "image/png") -> str:
    """Upload a file to Misskey Drive and return the file ID."""
    url = instance_url.rstrip("/") + "/api/drive/files/create"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            data={"i": token},
            files={"file": ("image.png", image_bytes, mime)},
        )
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
) -> dict:
    """Create a note on the configured Misskey instance. Uploads image_bytes if provided."""
    file_ids = []
    if image_bytes:
        try:
            file_id = await upload_file(instance_url, token, image_bytes, image_mime)
            file_ids.append(file_id)
        except Exception as e:
            logger.warning(f"Misskey file upload failed, posting text only: {e}")

    url = instance_url.rstrip("/") + "/api/notes/create"
    payload: dict = {"i": token, "text": text, "visibility": visibility}
    if file_ids:
        payload["fileIds"] = file_ids
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

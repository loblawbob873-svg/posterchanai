"""Nostr media upload — Blossom (BUD-02) and NIP-96 (e.g. nostr.build).

Unlike Pleroma there is no per-instance Drive: media goes to an external
host which returns a URL that we embed in the note content (+ a NIP-92 imeta tag).
Both flows authenticate with a signed Nostr event (Blossom kind 24242 / NIP-98 kind 27235)
sent as an `Authorization: Nostr <base64-json-event>` header.

upload(media_cfg, seckey, data, mime) dispatches on media_cfg["service"]
("blossom" | "nip96") and returns a dict {url, mime, sha256, dim}.
"""

import json
import time
import base64
import hashlib
import logging

import httpx

from . import event as _event

logger = logging.getLogger(__name__)

DEFAULT_BLOSSOM_SERVER = "https://blossom.primal.net"
DEFAULT_NIP96_SERVER = "https://nostr.build"


def _is_local(server: str) -> bool:
    """True for localhost / private-LAN / *.lan hosts — these must NOT go through the Tor proxy."""
    import re as _re
    s = (server or "").lower()
    return bool(_re.search(r"(?:^|//)(?:localhost|127\.0\.0\.1|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|\[::1\])", s)
                or _re.search(r"\.lan(?::|/|$)", s))


def _proxy(server: str = ""):
    """Built-in HTTP proxy (→ Tor) for media uploads, or None for direct. The internal/LAN Blossom
    server is reached directly (proxying localhost through Tor would just fail)."""
    if server and _is_local(server):
        return None
    try:
        from app.services.proxy_utils import get_outbound_proxy
        return get_outbound_proxy()
    except Exception:
        return None


def _auth_header(ev: dict) -> str:
    return "Nostr " + base64.b64encode(
        json.dumps(ev, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode()


def _image_dim(data: bytes) -> str:
    """Best-effort WxH for an imeta dim tag (PNG/JPEG/GIF), else ''."""
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return f"{im.width}x{im.height}"
    except Exception:
        return ""


async def upload_blossom(server: str, seckey: bytes, data: bytes, mime: str = "") -> dict:
    """BUD-02 upload: PUT {server}/upload with a kind-24242 auth event."""
    server = (server or DEFAULT_BLOSSOM_SERVER).rstrip("/")
    digest = hashlib.sha256(data).hexdigest()
    now = int(time.time())
    auth = _event.build_event(
        seckey, 24242, "Upload blob",
        tags=[["t", "upload"], ["x", digest], ["expiration", str(now + 600)]],
        created_at=now,
    )
    headers = {"Authorization": _auth_header(auth)}
    if mime:
        headers["Content-Type"] = mime
    async with httpx.AsyncClient(proxy=_proxy(server), timeout=120) as client:
        resp = await client.put(f"{server}/upload", content=data, headers=headers)
    logger.info(f"[nostr] blossom upload: HTTP {resp.status_code} — {resp.text[:200]}")
    resp.raise_for_status()
    body = resp.json()
    url = body.get("url") or f"{server}/{digest}"
    return {"url": url, "mime": body.get("type") or mime, "sha256": digest, "dim": _image_dim(data)}


async def _nip96_endpoint(server: str) -> str:
    server = server.rstrip("/")
    try:
        async with httpx.AsyncClient(proxy=_proxy(), timeout=20) as client:
            resp = await client.get(f"{server}/.well-known/nostr/nip96.json")
            resp.raise_for_status()
            api = resp.json().get("api_url")
            if api:
                return api if api.startswith("http") else server + api
    except Exception as e:
        logger.warning(f"[nostr] nip96 discovery failed for {server}: {e}")
    return f"{server}/api/v2/nip96/upload"


async def upload_nip96(server: str, seckey: bytes, data: bytes, mime: str = "") -> dict:
    """NIP-96 upload: multipart POST with a NIP-98 (kind 27235) auth event."""
    server = (server or DEFAULT_NIP96_SERVER)
    endpoint = await _nip96_endpoint(server)
    digest = hashlib.sha256(data).hexdigest()
    # NIP-98 auth. The optional `payload` tag must be sha256 of the *request body*, but for a
    # multipart upload that's the whole encoded body (boundaries included), not the file digest
    # — sending the file digest there makes strict servers reject the upload, so we omit it
    # (payload is optional per NIP-98).
    auth = _event.build_event(
        seckey, 27235, "",
        tags=[["u", endpoint], ["method", "POST"]],
    )
    headers = {"Authorization": _auth_header(auth)}
    files = {"file": ("upload", data, mime or "application/octet-stream")}
    async with httpx.AsyncClient(proxy=_proxy(server), timeout=120) as client:
        resp = await client.post(endpoint, files=files, headers=headers)
    logger.info(f"[nostr] nip96 upload: HTTP {resp.status_code} — {resp.text[:200]}")
    resp.raise_for_status()
    body = resp.json()
    url = ""
    for tag in body.get("nip94_event", {}).get("tags", []):
        if len(tag) >= 2 and tag[0] == "url":
            url = tag[1]
            break
    url = url or body.get("url") or ""
    return {"url": url, "mime": mime, "sha256": digest, "dim": _image_dim(data)}


async def upload(media_cfg: dict, seckey: bytes, data: bytes, mime: str = "") -> dict:
    """Dispatch to the configured uploader. media_cfg = {service, endpoint}."""
    service = (media_cfg or {}).get("service", "blossom").lower()
    endpoint = (media_cfg or {}).get("endpoint", "")
    if service == "nip96":
        return await upload_nip96(endpoint or DEFAULT_NIP96_SERVER, seckey, data, mime)
    return await upload_blossom(endpoint or DEFAULT_BLOSSOM_SERVER, seckey, data, mime)

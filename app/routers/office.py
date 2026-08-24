"""Built-in Collabora CODE integration and a deliberately small WOPI host.

The browser uploads the *plain* working copy to this router.  This is important for
PosterChan's encrypted drive: CODE never receives the Blossom ciphertext or a drive
key.  After editing, the browser downloads the result and runs the normal
encrypt/upload path again.  Sessions are capability-token protected and expire.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(tags=["office"])

_ROOT = Path(os.getenv("POSTERCHANAI_OFFICE_WORK_DIR", "/tmp/posterchanai-office"))
_CODE = os.getenv("POSTERCHANAI_CODE_URL", "http://127.0.0.1:9983").rstrip("/")
_MAX = int(os.getenv("POSTERCHANAI_OFFICE_MAX_BYTES", str(128 * 1024 * 1024)))
_TTL = int(os.getenv("POSTERCHANAI_OFFICE_SESSION_TTL", "21600"))
_SECRET = os.getenv("POSTERCHANAI_OFFICE_SECRET") or secrets.token_hex(32)
_LOCKS: dict[str, tuple[str, float]] = {}
_MU = threading.Lock()
_EXTS = {"doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "xlsm", "ods", "csv",
         "ppt", "pptx", "odp"}


def enabled() -> bool:
    return os.getenv("POSTERCHANAI_OFFICE", "0").lower() in {"1", "true", "yes", "on"}


def _safe_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HTTPException(404, "office session not found")
    return value


def _dir(file_id: str) -> Path:
    return _ROOT / _safe_id(file_id)


def _token(file_id: str, expires: int) -> str:
    body = f"{file_id}.{expires}"
    sig = hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _authorize(file_id: str, token: str) -> dict:
    try:
        expires_s, sig = token.split(".", 1)
        expires = int(expires_s)
    except Exception:
        raise HTTPException(401, "invalid office token")
    if expires < int(time.time()):
        raise HTTPException(401, "office token expired")
    expected = _token(file_id, expires).split(".", 1)[1]
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, "invalid office token")
    p = _dir(file_id)
    try:
        return json.loads((p / "meta.json").read_text())
    except (OSError, ValueError):
        raise HTTPException(404, "office session not found")


def _cleanup() -> None:
    now = time.time()
    try:
        _ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        for p in _ROOT.iterdir():
            if p.is_dir() and now - p.stat().st_mtime > _TTL:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


async def _action_url(ext: str, mode: str) -> str:
    """Resolve CODE's versioned browser URL from WOPI discovery; never hard-code it."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{_CODE}/hosting/discovery")
            response.raise_for_status()
        root = ET.fromstring(response.content)
        wanted = "edit" if mode == "edit" else "view"
        fallback = None
        for action in root.iter("action"):
            if action.attrib.get("ext", "").lower() != ext:
                continue
            if action.attrib.get("name") == wanted:
                return action.attrib["urlsrc"]
            if action.attrib.get("name") == "view":
                fallback = action.attrib.get("urlsrc")
        if fallback:
            return fallback
    except Exception as exc:
        raise HTTPException(503, f"built-in office server unavailable: {exc}")
    raise HTTPException(415, f"CODE does not advertise support for .{ext}")


@router.post("/client/office/session")
async def create_session(request: Request, file: UploadFile = File(...), mode: str = Form("edit")):
    if not enabled():
        raise HTTPException(404, "built-in office support is disabled")
    name = Path(file.filename or "document").name[:240]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _EXTS:
        raise HTTPException(415, "unsupported office document")
    data = await file.read(_MAX + 1)
    if len(data) > _MAX:
        raise HTTPException(413, "office document is too large")
    _cleanup()
    file_id = secrets.token_hex(16)
    p = _ROOT / file_id
    p.mkdir(mode=0o700, parents=True)
    (p / "document").write_bytes(data)
    meta = {"name": name, "size": len(data), "version": 1, "created": int(time.time()),
            "readonly": mode != "edit"}
    (p / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    expires = int(time.time()) + _TTL
    token = _token(file_id, expires)
    wopi_src = f"{_public_base(request)}/wopi/files/{file_id}"
    urlsrc = await _action_url(ext, mode)
    # Discovery is fetched over the private container/loopback address.  The browser must use the
    # same-origin proxy, never that unreachable address.  Preserve CODE's versioned path/query.
    parsed = urllib.parse.urlsplit(urlsrc)
    urlsrc = _public_base(request) + "/office-code" + parsed.path
    if parsed.query:
        urlsrc += "?" + parsed.query
    sep = "&" if "?" in urlsrc else "?"
    editor = f"{urlsrc}{sep}WOPISrc={urllib.parse.quote(wopi_src, safe='')}"
    return {"id": file_id, "token": token, "editor_url": editor, "expires": expires,
            "readonly": meta["readonly"]}


@router.get("/client/office/session/{file_id}/contents")
def session_contents(file_id: str, access_token: str = Query(...)):
    meta = _authorize(file_id, access_token)
    return FileResponse(_dir(file_id) / "document", media_type="application/octet-stream",
                        filename=meta["name"])


@router.delete("/client/office/session/{file_id}")
def delete_session(file_id: str, access_token: str = Query(...)):
    _authorize(file_id, access_token)
    shutil.rmtree(_dir(file_id), ignore_errors=True)
    with _MU:
        _LOCKS.pop(file_id, None)
    return {"ok": True}


@router.get("/wopi/files/{file_id}")
def check_file_info(file_id: str, request: Request, access_token: str = Query(...)):
    meta = _authorize(file_id, access_token)
    base = _public_base(request)
    return {"BaseFileName": meta["name"], "Size": meta["size"],
            "Version": str(meta["version"]), "OwnerId": "posterchan",
            "UserId": "posterchan", "UserFriendlyName": "PosterChan user",
            "UserCanWrite": not meta["readonly"], "SupportsLocks": True,
            "SupportsUpdate": not meta["readonly"], "PostMessageOrigin": base}


@router.get("/wopi/files/{file_id}/contents")
def get_file(file_id: str, access_token: str = Query(...)):
    meta = _authorize(file_id, access_token)
    return FileResponse(_dir(file_id) / "document", media_type="application/octet-stream",
                        headers={"X-WOPI-ItemVersion": str(meta["version"])})


@router.post("/wopi/files/{file_id}/contents")
async def put_file(file_id: str, request: Request, access_token: str = Query(...),
                   x_wopi_lock: str | None = Header(None)):
    meta = _authorize(file_id, access_token)
    if meta["readonly"]:
        raise HTTPException(403, "document is read-only")
    with _MU:
        current = _LOCKS.get(file_id)
    if current and current[0] != (x_wopi_lock or ""):
        return JSONResponse({}, status_code=409, headers={"X-WOPI-Lock": current[0]})
    data = await request.body()
    if len(data) > _MAX:
        raise HTTPException(413, "office document is too large")
    p = _dir(file_id)
    fd, tmp = tempfile.mkstemp(prefix="save-", dir=p)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, p / "document")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    meta.update(size=len(data), version=int(meta["version"]) + 1)
    (p / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return JSONResponse({}, headers={"X-WOPI-ItemVersion": str(meta["version"])})


@router.post("/wopi/files/{file_id}")
def file_operation(file_id: str, access_token: str = Query(...),
                   x_wopi_override: str = Header(""), x_wopi_lock: str = Header(""),
                   x_wopi_oldlock: str = Header("")):
    _authorize(file_id, access_token)
    op = x_wopi_override.upper()
    with _MU:
        current = _LOCKS.get(file_id)
        current_lock = current[0] if current and current[1] > time.time() else ""
        if op == "LOCK":
            if current_lock and current_lock not in {x_wopi_lock, x_wopi_oldlock}:
                return JSONResponse({}, status_code=409, headers={"X-WOPI-Lock": current_lock})
            _LOCKS[file_id] = (x_wopi_lock, time.time() + 1800)
        elif op == "REFRESH_LOCK":
            if current_lock != x_wopi_lock:
                return JSONResponse({}, status_code=409, headers={"X-WOPI-Lock": current_lock})
            _LOCKS[file_id] = (x_wopi_lock, time.time() + 1800)
        elif op == "UNLOCK":
            if current_lock != x_wopi_lock:
                return JSONResponse({}, status_code=409, headers={"X-WOPI-Lock": current_lock})
            _LOCKS.pop(file_id, None)
        elif op == "GET_LOCK":
            return JSONResponse({}, headers={"X-WOPI-Lock": current_lock})
        else:
            raise HTTPException(501, "unsupported WOPI operation")
    return JSONResponse({})

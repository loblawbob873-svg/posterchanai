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
# THE SUB-PATH CODE LIVES UNDER, and all three halves of the deployment must agree on it:
# coolwsd is started with `--o:net.service_root=<this>` (posterchanai-office.service and
# scripts/install/office.sh), nginx publishes `location ^~ <this>/` WITHOUT rewriting the prefix
# away (nginx/posterchanai.conf.example, docker/proxy/posterchanai.conf), and this module joins it.
#
# WHY IT CANNOT JUST BE nginx's JOB. coolwsd writes its OWN absolute URLs — the `<script src>` tags
# inside cool.html (via %SERVICE_ROOT%), the editing WebSocket, and the discovery `urlsrc`. Told
# nothing, it writes them at the site ROOT, so nginx stripped the prefix on the way in and the page
# then asked for `/browser/<hash>/global.js` at the top level, which is not a route this site has.
# The document frame loaded and stayed WHITE, with four 404s in the console and nothing server-side
# to say so — cool.html itself had been served perfectly. `service_root` is the setting that exists
# for exactly this, and its own comment in coolwsd.xml describes this deployment.
_SERVICE_ROOT = "/office-code"
_MAX = int(os.getenv("POSTERCHANAI_OFFICE_MAX_BYTES", str(128 * 1024 * 1024)))
_TTL = int(os.getenv("POSTERCHANAI_OFFICE_SESSION_TTL", "21600"))
_SECRET = os.getenv("POSTERCHANAI_OFFICE_SECRET") or secrets.token_hex(32)
_LOCKS: dict[str, tuple[str, float]] = {}
_MU = threading.Lock()
# WHAT CAN BE OPENED IS CODE'S ANSWER, NOT A LIST KEPT HERE.
#
# This was a hand-written set of thirteen, and CODE advertises NINETY-TWO in its own discovery. So
# "pdf: unsuported office document" — a format LibreOffice opens in Draw, refused by a gate in front
# of a server that supports it, with a message that blamed the document. Same for odg, otp, ots, ott
# and seventy more. `_action_url` ALREADY refuses anything CODE does not advertise, with a message
# that names the extension, so this second list could only ever be wrong in the strict direction.
#
# It is still a real guard — an upload is rejected before any bytes are written — it is just asked
# of the server that will actually do the work. The static set below is the FALLBACK for a CODE that
# cannot be reached; erring narrow there is right, because the alternative is writing a file to disk
# for an editor that is not answering.
_EXTS_FALLBACK = {"doc", "docx", "odt", "rtf", "txt", "xls", "xlsx", "xlsm", "ods", "csv",
                  "ppt", "pptx", "odp", "pdf", "odg", "otp", "ots", "ott", "otg", "fodt",
                  "fods", "fodp", "fodg", "odm", "oth", "otm", "sxw", "sxc", "sxi", "sxd"}
_EXTS_TTL = 300
_exts_cache: tuple[float, frozenset[str]] | None = None


async def _accepted_exts() -> frozenset[str]:
    """The extensions CODE advertises, cached briefly; the static set if it cannot be asked.

    Cached because this is on the path of every document that is opened, and discovery is a whole
    XML document listing ninety-odd formats — but only briefly, so a CODE that gains a filter after
    an upgrade is picked up without anybody restarting anything.
    """
    global _exts_cache
    now = time.time()
    if _exts_cache and now - _exts_cache[0] < _EXTS_TTL:
        return _exts_cache[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await _discover(client)
        root = ET.fromstring(response.content)
        found = {a.attrib["ext"].lower() for a in root.iter("action") if a.attrib.get("ext")}
        if found:                       # an empty answer is not an answer; keep what we had
            _exts_cache = (now, frozenset(found))
            return _exts_cache[1]
    except Exception:
        pass
    return frozenset(_EXTS_FALLBACK)


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


async def _discover(client: httpx.AsyncClient) -> httpx.Response:
    """Fetch discovery, under the service root or at the origin — whichever this CODE answers.

    A configured `service_root` moves EVERY path, including this one and including on loopback, so
    the prefixed URL is asked first. The bare one is not a fallback for a misconfigured server: it
    is what makes the two restarts ORDER-INDEPENDENT. `sync.sh` restarts the app and deliberately
    never restarts the office unit (a document somebody has open must not be closed by a deploy), so
    for as long as it takes a person to run `systemctl restart posterchanai-office` there is a new
    app talking to an old CODE — and it is a 404 that would otherwise read as "office unavailable".
    """
    last: Exception | None = None
    for root in (_SERVICE_ROOT, ""):
        try:
            response = await client.get(f"{_CODE}{root}/hosting/discovery")
            response.raise_for_status()
            return response
        except Exception as exc:                        # 404 here means "not under that root"
            last = exc
    raise last if last else RuntimeError("no discovery endpoint answered")


async def _action_url(ext: str, mode: str) -> str:
    """Resolve CODE's versioned browser URL from WOPI discovery; never hard-code it."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await _discover(client)
        root = ET.fromstring(response.content)
        # THE ACTION NAME IS NOT ALWAYS "edit" OR "view", and assuming so is what refused PDFs.
        #
        # CODE advertises pdf under exactly one action: `view_comment` — its annotation mode, which
        # is the only way it offers a PDF at all. Asked for "edit" and then falling back to "view",
        # this found neither and reported "CODE does not advertise support for .pdf" about a format
        # sitting right there in the discovery document.
        #
        # So the preference is a CHAIN, most capable first, and whatever the file actually has is
        # taken rather than demanded. A PDF opens in the annotator; a .docx still opens in Writer,
        # because `edit` is still preferred wherever it exists.
        order = (["edit", "view_comment", "view"] if mode == "edit"
                 else ["view", "view_comment", "edit"])
        found: dict[str, str] = {}
        for action in root.iter("action"):
            if action.attrib.get("ext", "").lower() != ext:
                continue
            name, url = action.attrib.get("name"), action.attrib.get("urlsrc")
            if name and url and name not in found:
                found[name] = url
        for name in order:
            if name in found:
                return found[name]
        if found:                       # something else entirely — better than refusing the file
            return next(iter(found.values()))
    except Exception as exc:
        raise HTTPException(503, f"built-in office server unavailable: {exc}")
    raise HTTPException(415, f"CODE does not advertise support for .{ext}")


@router.post("/client/office/session")
async def create_session(request: Request, file: UploadFile = File(...), mode: str = Form("edit")):
    if not enabled():
        raise HTTPException(404, "built-in office support is disabled")
    name = Path(file.filename or "document").name[:240]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in await _accepted_exts():
        raise HTTPException(415, f"the office editor does not open .{ext} files")
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
    # The service root goes on EXACTLY ONCE. With `service_root` configured, CODE already wrote it
    # into the path it advertises, and adding it again produced /office-code/office-code/browser/…
    # — a 404 that looks nothing like its cause. Adding it when CODE did not is the other half, and
    # is what keeps this working against a CODE that has not been restarted yet (see _discover).
    path = parsed.path
    if not (path == _SERVICE_ROOT or path.startswith(_SERVICE_ROOT + "/")):
        path = _SERVICE_ROOT + path
    urlsrc = _public_base(request) + path
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

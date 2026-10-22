"""The CORD-07 call broker: `/.well-known/concord/av` (static/js/client/cord-voice.js `token()` is the client).

BLIND BY DESIGN. A Concord voice channel has a room keypair whose secret only its members hold (cord-reader.js
voiceMaterial); a caller proves it holds that key by signing a kind-27235 grant for this exact URL. So the broker
learns that SOMEONE with the room key wants in -- never who, never which community -- and answers with a short
LiveKit token for that room alone. The media itself is end-to-end encrypted per sender; the SFU
(livekit_service) only forwards it.

  GET /.well-known/concord/av           -> 204 when this node runs a call SFU (anything else: "not a broker")
  GET /.well-known/concord/av/<room>    -> {token, identity, url}, with
      Authorization: Concord <base64(JSON grant)>
      grant = kind 27235, pubkey == <room>, tags u=<this URL>, method=GET; created_at within 60s; an id used once.

Every rule below is one a forged or replayed grant would otherwise pass: the signer must BE the room (else any key
joins any room), `u` must be THIS host's URL for THIS room (else a grant made for another broker is replayed here),
an event id works once (for 300s, CORD-07 asks 240) and a grant is only fresh for a minute. Callers come from other origins (Armada, the APK), with
credentials omitted, so CORS is open and nothing here reads a cookie.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict, deque

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from app.services import livekit_service
from app.services.nostr.event import verify_event

logger = logging.getLogger(__name__)
router = APIRouter()

GRANT_KIND = 27235
FRESH_SECONDS = 60
# CORD-07 §2: "event ids MUST NOT repeat for at least 240 seconds". Replay is keyed on the EVENT ID -- the spec
# requires no nonce tag, and Armada's client need not send one; demanding it refused every other client.
REPLAY_SECONDS = 300
TOKEN_TTL = 600
MAX_GRANT_BYTES = 4096
_ROOM = re.compile(r"^[0-9a-f]{64}$")
_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS",
         "Access-Control-Allow-Headers": "Authorization", "Access-Control-Max-Age": "600",
         "Cache-Control": "no-store"}

_lock = threading.Lock()
_nonces: "OrderedDict[str, float]" = OrderedDict()   # spent grant event id -> expiry; bounded
_MAX_NONCES = 50_000
_hits: dict[str, deque] = {}
RATE_PER_MINUTE = 30


def _seen(nonce: str, now: float) -> bool:
    """True if this nonce was already spent (and spends it otherwise)."""
    with _lock:
        while _nonces and next(iter(_nonces.values())) < now:
            _nonces.popitem(last=False)
        if nonce in _nonces:
            return True
        _nonces[nonce] = now + REPLAY_SECONDS
        while len(_nonces) > _MAX_NONCES:
            _nonces.popitem(last=False)
        return False


def _limited(ip: str, now: float) -> bool:
    with _lock:
        q = _hits.setdefault(ip, deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= RATE_PER_MINUTE:
            return True
        q.append(now)
        if len(_hits) > 10_000:
            for k in [k for k, v in _hits.items() if not v][:5000]:
                _hits.pop(k, None)
        return False


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint_token(api_key: str, api_secret: str, room: str, identity: str, now: int | None = None) -> str:
    """A LiveKit access token (HS256 JWT) for one room: join, publish, subscribe, data. Nothing else."""
    now = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"iss": api_key, "sub": identity, "nbf": now - 10, "exp": now + TOKEN_TTL,
              "video": {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True,
                        "canPublishData": True}}
    signing = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        _b64url(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(api_secret.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + _b64url(sig)


def check_grant(auth: str, room: str, expected_url: str, now: float) -> str:
    """'' when the Authorization header proves the room key for this URL, else why not."""
    if not auth.startswith("Concord "):
        return "missing grant"
    raw = auth[len("Concord "):].strip()
    if len(raw) > MAX_GRANT_BYTES:
        return "grant too large"
    try:
        ev = json.loads(base64.b64decode(raw, validate=True))
    except Exception:
        return "unreadable grant"
    if not isinstance(ev, dict) or ev.get("kind") != GRANT_KIND or ev.get("content") not in ("", None):
        return "not a call grant"
    if ev.get("pubkey") != room:
        return "grant is not signed by the room key"
    try:
        at = int(ev.get("created_at"))
    except (TypeError, ValueError):
        return "grant has no time"
    if abs(now - at) > FRESH_SECONDS:
        return "grant is not fresh"
    tags = ev.get("tags") if isinstance(ev.get("tags"), list) else []
    def one(name):
        vals = [t[1] for t in tags if isinstance(t, list) and len(t) >= 2 and t[0] == name and isinstance(t[1], str)]
        return vals[0] if len(vals) == 1 else None
    if one("u") != expected_url:
        return "grant is for another address"
    if (one("method") or "").upper() != "GET":
        return "grant is for another method"
    if not verify_event(ev):
        return "grant signature is invalid"
    if _seen(str(ev.get("id")), now):
        return "grant was already used"
    return ""


def _host(request: Request) -> str:
    h = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip().lower()
    return h if re.fullmatch(r"[a-z0-9.-]+(:\d{1,5})?", h) else ""


def public_url(request: Request) -> str:
    """Where callers reach the SFU's signalling: livekit_public_url, else wss://<this host>/livekit (nginx)."""
    cfg = livekit_service._cfg()
    configured = (cfg.get("livekit_public_url", "") or "").strip()
    if re.fullmatch(r"wss://[A-Za-z0-9.:-]+(/[A-Za-z0-9._/-]*)?", configured):
        return configured.rstrip("/")
    return f"wss://{_host(request)}/livekit"


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    fwd = (request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd if fwd and (peer.startswith("127.") or peer.startswith("192.168.") or peer.startswith("10.")) else peer


@router.options("/.well-known/concord/av")
@router.options("/.well-known/concord/av/{room}")
async def preflight(room: str = ""):
    return Response(status_code=204, headers=_CORS)


@router.get("/.well-known/concord/av")
async def probe():
    # 204 only while the SFU actually runs: a client that sees it will call through us.
    if livekit_service.available():
        return Response(status_code=204, headers=_CORS)
    return Response(status_code=404, headers=_CORS)


@router.get("/.well-known/concord/av/{room}")
async def grant(room: str, request: Request):
    now = time.time()
    if not _ROOM.fullmatch(room or ""):
        return JSONResponse({"error": "invalid call room"}, status_code=400, headers=_CORS)
    if not livekit_service.available():
        return JSONResponse({"error": "this server does not provide Concord calls"}, status_code=404, headers=_CORS)
    if _limited(_client_ip(request), now):
        return JSONResponse({"error": "too many call requests"}, status_code=429, headers={**_CORS, "Retry-After": "30"})
    host = _host(request)
    why = check_grant(request.headers.get("authorization", ""), room,
                      f"https://{host}/.well-known/concord/av/{room}", now) if host else "unknown host"
    if why:
        logger.info("[concord-av] refused a call grant: %s", why)
        return JSONResponse({"error": why}, status_code=401, headers=_CORS)
    key, secret = livekit_service.credentials()
    identity = secrets.token_hex(16)
    return JSONResponse({"token": mint_token(key, secret, room, identity, int(now)), "identity": identity,
                         "url": public_url(request)}, headers=_CORS)


# ---- signalling: wss://<host>/livekit/... -> the SFU on loopback ----------------------------------------------
# LiveKit's signalling port stays on 127.0.0.1 and the app carries it, so a node needs no nginx change: its
# catch-all location already forwards WebSocket upgrades to the app. Only signalling crosses here (small protobuf
# frames); media goes straight to the SFU's UDP/TCP ports.
_SIG_PATH = re.compile(r"^[A-Za-z0-9/_.-]{0,64}$")


def _upstream(path: str) -> str:
    return f"127.0.0.1:{livekit_service._port(livekit_service._cfg(), 'livekit_http_port')}/{path}"


@router.websocket("/livekit/{path:path}")
async def signalling(ws: WebSocket, path: str):
    if not livekit_service.available() or not _SIG_PATH.fullmatch(path or ""):
        await ws.close(code=1008)
        return
    q = ws.url.query
    try:
        up = await websockets.connect("ws://" + _upstream(path) + (f"?{q}" if q else ""), max_size=None,
                                      open_timeout=10, ping_interval=None)
    except Exception as e:
        logger.info("[concord-av] the SFU refused signalling: %s", type(e).__name__)
        await ws.close(code=1011)
        return
    await ws.accept()

    async def down():   # browser -> SFU
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                await up.send(m["bytes"])
            elif m.get("text") is not None:
                await up.send(m["text"])

    async def back():   # SFU -> browser
        async for m in up:
            if isinstance(m, bytes):
                await ws.send_bytes(m)
            else:
                await ws.send_text(m)

    tasks = [asyncio.create_task(down()), asyncio.create_task(back())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await up.close()
        with contextlib.suppress(Exception):
            await ws.close()


@router.get("/livekit/{path:path}")
async def signalling_http(path: str, request: Request):
    """The SDK's /rtc/validate, which it calls after a failed connect to say WHY (an expired token, a full room)."""
    if not livekit_service.available() or not _SIG_PATH.fullmatch(path or ""):
        return Response(status_code=404, headers=_CORS)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("http://" + _upstream(path), params=dict(request.query_params))
    except httpx.HTTPError:
        return Response(status_code=502, headers=_CORS)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "text/plain"),
                    headers=_CORS)

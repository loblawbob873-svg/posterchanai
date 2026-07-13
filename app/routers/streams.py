"""OBS streaming endpoints — the server side of the NIP-53 live-stream feature.

Flow:
- The streamer opens "Go Live" in the web client, which calls GET /api/streams/ingest to get their OBS
  setup: an RTMP server URL + a stream key of the form `<token>?key=<api_key>`. They paste those into OBS.
- OBS publishes to the built-in MediaMTX server (app/services/stream_service.py). On publish, MediaMTX
  calls POST /api/streams/auth here; we validate the API key from the query and allow/deny. Playback
  (HLS reads) is public.
- MediaMTX remuxes the RTMP feed to HLS. The client publishes a kind-30311 (NIP-53) event with the HLS
  URL, which surfaces the stream under Discover → Streams (the viewer/player already exists in the client).

The API key is never in the public HLS URL — only the random <token> is (and that token rides the public
kind-30311 anyway). Reusing the existing per-user API key (Settings → API keys) means no new credential
system, per the design decision.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.auth import get_current_user
from app.database import get_db
from app.models import APIKey, User, UserSetting
from app.services import settings_store, stream_end_service
from app.services.nostr.event import verify_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/streams", tags=["streams"])

_OBS_KEY_NAME = "OBS Stream"
_TOKEN_SETTING = "stream_token"


def _stream_enabled() -> bool:
    return (settings_store.get("stream_enabled", "false") or "").strip().lower() == "true"


def _user_token(db, user: User) -> str:
    """Stable per-user publish token (unguessable, in the public HLS path). Generated once."""
    row = db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == _TOKEN_SETTING).first()
    if row and row.value:
        return row.value
    tok = secrets.token_hex(8)
    if row:
        row.value = tok
    else:
        db.add(UserSetting(user_id=user.id, key=_TOKEN_SETTING, value=tok))
    db.commit()
    return tok


def _obs_key(db, user: User) -> str:
    """Return the user's OBS stream API key, creating a dedicated one on first use."""
    row = db.query(APIKey).filter(APIKey.user_id == user.id, APIKey.name == _OBS_KEY_NAME,
                                  APIKey.is_active == True).first()  # noqa: E712
    if row:
        return row.key
    key = f"sk-{secrets.token_hex(32)}"
    db.add(APIKey(user_id=user.id, key=key, name=_OBS_KEY_NAME))
    db.commit()
    return key


@router.post("/auth")
async def stream_auth(request: Request, db=Depends(get_db)):
    """MediaMTX external-auth hook. Allow playback (reads); gate publishing on a valid API key.

    MediaMTX POSTs {action, path, query, protocol, ip, ...}. A 200 authorizes; anything else denies.
    Gated by a secret the app injects into MediaMTX's configured auth-hook URL (?hook=...): only our own
    MediaMTX knows it, so the public app can't be used as an API-key validity oracle / DoS surface. This is
    robust regardless of proxy-header / loopback-IP quirks (request.client.host is unreliable behind a proxy).
    """
    secret = (settings_store.get("stream_auth_secret", "") or "").strip()
    hook = request.query_params.get("hook") or ""
    if not secret or not hmac.compare_digest(hook, secret):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body.get("action") or "").lower()
    # Default-DENY: only playback/read is public (watch a live stream); publish needs a key; anything
    # else (incl. a future/renamed MediaMTX write action) is rejected rather than silently authorized.
    if action in ("read", "playback"):
        return Response(status_code=200)
    if action != "publish":
        return JSONResponse({"error": "unsupported action"}, status_code=401)
    # Extract the API key from the RTMP/SRT query (OBS stream key = "<token>?key=<api_key>").
    key = ""
    q = body.get("query") or ""
    if q:
        try:
            key = (parse_qs(q).get("key") or [""])[0]
        except Exception:
            key = ""
    key = (key or body.get("password") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=401)
    row = db.query(APIKey).filter(APIKey.key == key, APIKey.is_active == True).first()  # noqa: E712
    if not row:
        logger.info("[stream] publish denied (bad/inactive key) path=%s", body.get("path"))
        return JSONResponse({"error": "invalid key"}, status_code=401)
    # A user may publish ONLY to their OWN token (not an arbitrary/made-up path, and not a sub-path of a
    # victim's token). Require the path to equal the key-owner's stream_token exactly — this closes both
    # the open-publish resource-abuse vector and the sub-path ownership bypass.
    path = (body.get("path") or "").strip()
    own = db.query(UserSetting).filter(UserSetting.user_id == row.user_id,
                                       UserSetting.key == _TOKEN_SETTING).first()
    if not own or not own.value or path != own.value:
        logger.info("[stream] publish denied (path %r is not the key owner's token)", path)
        return JSONResponse({"error": "not your stream"}, status_code=403)
    # The feed is really flowing now — let the reaper end this stream if it later disappears.
    try:
        stream_end_service.mark_publishing(db, row.user_id)
    except Exception as e:
        logger.debug("[stream] could not mark %s publishing: %s", path, e)
    return Response(status_code=200)


@router.post("/unpublish")
async def stream_unpublish(request: Request, db=Depends(get_db)):
    """MediaMTX `runOnUnpublish` hook — OBS/the phone dropped, so the stream is over.

    The kind-30311 can only be signed in the streamer's browser, so we can't author an "ended" event here;
    we publish the one they parked at go-live (see stream_end_service). Gated by the same ?hook=<secret> as
    /auth. Returns immediately — MediaMTX is waiting on this call — and the actual end is graced + re-probed
    in the background so an OBS reconnect blip doesn't end a stream that's still running.
    """
    secret = (settings_store.get("stream_auth_secret", "") or "").strip()
    hook = request.query_params.get("hook") or ""
    if not secret or not hmac.compare_digest(hook, secret):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    token = (request.query_params.get("path") or "").strip()
    if not token:
        return JSONResponse({"error": "missing path"}, status_code=400)
    user_id = stream_end_service.user_by_token(db, token)
    if user_id is None:
        return Response(status_code=200)   # unknown token — nothing of ours to end
    stream_end_service.schedule_end(token, user_id)
    return Response(status_code=200)


@router.post("/sentinel")
async def stream_sentinel(request: Request, current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """Park the client's pre-signed "ended" kind-30311 so the server can end the stream if the browser dies.

    We never sign this — the streamer's key never leaves their browser. We only store what they already
    signed and publish it verbatim when MediaMTX says the feed is gone (see stream_end_service).
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    event = (body or {}).get("event")
    if not isinstance(event, dict):
        return JSONResponse({"error": "bad event"}, status_code=400)
    if len(json.dumps(event)) > 8192:      # a 30311 is a few hundred bytes; refuse anything absurd
        return JSONResponse({"error": "event too large"}, status_code=413)
    if event.get("kind") != 30311 or not verify_event(event):
        return JSONResponse({"error": "bad event"}, status_code=400)

    def _tag(name: str) -> str:
        for t in (event.get("tags") or []):
            if isinstance(t, list) and len(t) >= 2 and t[0] == name:
                return str(t[1])
        return ""

    if _tag("status") != "ended":
        return JSONResponse({"error": "not an ended event"}, status_code=400)
    # It may only end the caller's OWN stream: the `d` tag has to be their publish token.
    if _tag("d") != _user_token(db, current_user):
        return JSONResponse({"error": "not your stream"}, status_code=403)
    stream_end_service.save_sentinel(db, current_user.id, event)
    return {"ok": True}


@router.delete("/sentinel")
def stream_sentinel_clear(current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """The client ended its own stream (stamping an accurate `ends`) — drop the parked fallback."""
    stream_end_service.clear_sentinel(db, current_user.id)
    return {"ok": True}


@router.get("/ingest")
def stream_ingest(request: Request, current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """Return the OBS setup for this user: RTMP server + stream key, plus the HLS playback URL + token.

    The client shows the RTMP server + stream key (copy-paste into OBS) and, on "Go Live", publishes a
    kind-30311 whose `streaming` tag is the returned hls_url.
    """
    cfg = settings_store.all_settings()
    enabled = (cfg.get("stream_enabled", "false") or "").strip().lower() == "true"
    rtmp_port = (cfg.get("stream_rtmp_port", "") or "1935").strip()
    host = (cfg.get("stream_domain", "") or "").strip() or (cfg.get("turn_public_ip", "") or "").strip() \
        or (request.url.hostname or "")
    token = _user_token(db, current_user)
    api_key = _obs_key(db, current_user)

    # HLS playback URL: a configured direct base (grey-clouded stream subdomain, scales best) or the
    # app's reverse-proxy path (zero-config — rides the existing tunnel).
    hls_base = (cfg.get("stream_hls_base", "") or "").strip().rstrip("/")
    if hls_base:
        hls_url = f"{hls_base}/{token}/index.m3u8"
    else:
        origin = str(request.base_url).rstrip("/")
        hls_url = f"{origin}/api/streams/hls/{token}/index.m3u8"

    origin = str(request.base_url).rstrip("/")
    return {
        "enabled": enabled,
        "rtmp_url": f"rtmp://{host}:{rtmp_port}",
        "stream_key": f"{token}?key={api_key}",
        "token": token,
        "hls_url": hls_url,
        # Phone/browser go-live: the client publishes WebRTC via WHIP through this same-origin (HTTPS)
        # proxy — avoids mixed-content and keeps the key server-side (session-authed).
        "whip_url": f"{origin}/api/streams/whip/{token}",
    }


def _prefer_h264(sdp: str) -> str:
    """Force the video m-section to negotiate H264 by dropping every other video codec from the OFFER.

    A browser WHIP offer usually lists VP8 first, so MediaMTX negotiates VP8 — but HLS can only carry
    H264/H265/AV1, so a VP8 phone stream produces NO playlist and viewers get 404 (the whole "can't watch a
    phone stream" bug). Every real phone (Android WebView / iOS Safari) hardware-encodes H264, so we keep
    ONLY the H264 payload types (plus their RTX retransmission companions) in the video m-line. If the offer
    somehow has no H264, we leave it untouched rather than break ingest. Audio (Opus) is never touched.
    """
    import re
    lines = sdp.replace("\r\n", "\n").split("\n")
    # Locate the video m-section span.
    v_start = next((i for i, l in enumerate(lines) if l.startswith("m=video ")), -1)
    if v_start < 0:
        return sdp
    v_end = next((i for i in range(v_start + 1, len(lines)) if lines[i].startswith("m=")), len(lines))
    section = lines[v_start:v_end]
    # payload-type → codec name, and rtx apt→ its referenced pt.
    codec_of, rtx_apt = {}, {}
    for l in section:
        m = re.match(r"a=rtpmap:(\d+)\s+([A-Za-z0-9\-]+)/", l)
        if m:
            codec_of[m.group(1)] = m.group(2).upper()
        m = re.match(r"a=fmtp:(\d+)\s+apt=(\d+)", l)
        if m:
            rtx_apt[m.group(1)] = m.group(2)
    h264 = {pt for pt, c in codec_of.items() if c == "H264"}
    if not h264:
        return sdp
    keep = set(h264) | {pt for pt, apt in rtx_apt.items() if apt in h264}
    # Rewrite the m= line's payload list, preserving its original order.
    parts = section[0].split()
    header, pts = parts[:3], [p for p in parts[3:] if p in keep]
    if not pts:
        return sdp
    new_section = [" ".join(header + pts)]
    for l in section[1:]:
        m = re.match(r"a=(?:rtpmap|fmtp|rtcp-fb):(\d+)", l)
        if m and m.group(1) not in keep:
            continue        # drop attributes for the codecs we removed
        new_section.append(l)
    return "\r\n".join(lines[:v_start] + new_section + lines[v_end:])


@router.post("/whip/{token}")
async def stream_whip(token: str, request: Request, current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """WHIP ingest proxy — lets the web client (PWA/app) publish a live WebRTC stream from the phone camera.

    The browser POSTs its SDP offer here (same-origin HTTPS, session-authed); we verify the token is the
    caller's own, then forward the offer to the local MediaMTX WHIP endpoint (appending the user's stream
    key so MediaMTX's auth hook passes) and relay the SDP answer back. Media then flows phone↔MediaMTX
    over WebRTC/ICE directly.
    """
    if not _stream_enabled():
        return Response(status_code=404)
    own = db.query(UserSetting).filter(UserSetting.user_id == current_user.id,
                                       UserSetting.key == _TOKEN_SETTING).first()
    if not own or own.value != token:
        return JSONResponse({"error": "not your stream"}, status_code=403)
    # Cap the offer body — an SDP is a few KB; refuse anything absurd (authed, but no memory-exhaustion foot-gun).
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > 262144:
        return JSONResponse({"error": "offer too large"}, status_code=413)
    offer = await request.body()
    if len(offer) > 262144:
        return JSONResponse({"error": "offer too large"}, status_code=413)
    # Force H264 so the stream is HLS-viewable (a VP8 phone stream produces no playlist). Best-effort —
    # if munging fails for any reason, forward the original offer rather than drop the go-live.
    try:
        offer = _prefer_h264(offer.decode("utf-8", "ignore")).encode("utf-8")
    except Exception:
        pass
    key = _obs_key(db, current_user)
    webrtc_port = (settings_store.get("stream_webrtc_port", "8889") or "8889").strip()
    upstream = f"http://127.0.0.1:{webrtc_port}/{token}/whip?key={key}"
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=4.0)) as client:
            r = await client.post(upstream, content=offer,
                                  headers={"Content-Type": "application/sdp"})
    except Exception:
        return Response(status_code=502)
    # Relay only the SDP answer — NOT MediaMTX's Location header (it points at the internal 127.0.0.1
    # WebRTC port; leaking it is pointless and confusing). The client tears the stream down by closing its
    # RTCPeerConnection, which MediaMTX detects as a WebRTC disconnect and cleans up on its own.
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/sdp"))


@router.get("/hls/{token}/{path:path}")
async def stream_hls_proxy(token: str, path: str, request: Request):
    """Reverse-proxy HLS playlists/segments from the local MediaMTX HLS server.

    Lets viewers watch over the existing tunnel with no extra port/subdomain (turnkey). Operators who
    expect many viewers should set stream_hls_base to a direct grey-clouded subdomain instead.

    Cookie handling: MediaMTX gates HLS with a `?cookieCheck=1` redirect AND an `hlsSession` session
    cookie set by the master playlist that the variant/segment requests MUST carry (else 401). Both cookies
    are *Secure*, so they can't survive our internal plain-HTTP hop untouched — so we forward the browser's
    cookies upstream (+ assert cookieCheck), and relay MediaMTX's Set-Cookie back to the browser so it
    carries the hlsSession on follow-up requests. The whole HLS session then rides our HTTPS origin.
    """
    if not _stream_enabled():
        return Response(status_code=404)
    # Only allow the two HLS asset types (playlists + segments); never proxy arbitrary paths.
    safe = "".join(c for c in f"{token}/{path}" if c.isalnum() or c in "._-/")
    if ".." in safe or safe != f"{token}/{path}":
        return Response(status_code=400)
    hls_port = (settings_store.get("stream_hls_port", "8888") or "8888").strip()
    upstream = f"http://127.0.0.1:{hls_port}/{token}/{path}?cookieCheck=1"
    # Forward ONLY the HLS session cookie upstream (never the app's auth/session cookies), always asserting
    # cookieCheck. The browser sends back the hlsSession we relayed from the master playlist response.
    fwd = ["cookieCheck=1"]
    for c in (request.headers.get("cookie", "") or "").split(";"):
        c = c.strip()
        if c.startswith("hlsSession="):
            fwd.append(c)
    up_cookie = "; ".join(fwd)
    import httpx
    client = None
    try:
        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=4.0))
        req = client.build_request("GET", upstream, headers={"Cookie": up_cookie})
        resp = await client.send(req, stream=True)
    except Exception:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        return Response(status_code=502)
    if resp.status_code != 200:
        code = resp.status_code
        await resp.aclose(); await client.aclose()
        return Response(status_code=code)
    media_type = resp.headers.get("content-type", "application/octet-stream")
    # Relay MediaMTX's Set-Cookie(s) (the hlsSession) so the browser carries it on the variant + segments.
    set_cookies = []
    try:
        set_cookies = resp.headers.get_list("set-cookie")
    except Exception:
        sc = resp.headers.get("set-cookie")
        if sc:
            set_cookies = [sc]

    async def _body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    # CORS: the native app plays HLS cross-origin WITH credentials (to carry the session cookie), and a
    # credentialed request may NOT use `Access-Control-Allow-Origin: *` — echo the caller's Origin instead.
    origin = request.headers.get("origin")
    cors = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"} if origin \
        else {"Access-Control-Allow-Origin": "*"}
    response = StreamingResponse(_body(), media_type=media_type,
                                 headers={"Cache-Control": "no-cache", **cors})
    # Re-emit MediaMTX's session cookie scoped to this stream's HLS path. SameSite=None; Secure so the
    # NATIVE app (cross-site https://localhost → our HTTPS origin) also returns it on variant/segment
    # requests; same-origin PWA works too. (Requires HTTPS, which prod is.)
    for sc in set_cookies:
        nv = (sc.split(";", 1)[0] or "").strip()   # "hlsSession=<uuid>"
        if not nv or "=" not in nv:
            continue
        cookie = f"{nv}; Path=/api/streams/hls/{token}/; HttpOnly; Secure; SameSite=None"
        try:
            response.raw_headers.append((b"set-cookie", cookie.encode("latin-1")))
        except Exception:
            pass
    return response

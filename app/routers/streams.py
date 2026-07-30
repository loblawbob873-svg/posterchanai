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

import hashlib
import hmac
import json
import logging
import secrets
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.auth import get_current_user
from app.database import get_db
from app.models import APIKey, User, UserSetting, StreamVOD
from app.services import settings_store, stream_end_service, stream_service, users_store
from app.services.nostr.event import verify_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/streams", tags=["streams"])

_OBS_KEY_NAME = "OBS Stream"
_TOKEN_SETTING = "stream_token"

# ---------------------------------------------------------------- Live viewer counting
# MediaMTX's /v3/paths/get lists RTSP/RTMP/WebRTC readers, but HLS viewers are NOT path readers — they're
# clients of one fan-out HLS muxer, and here they're further hidden behind our own HLS proxy, so MediaMTX
# reports ~0 no matter how many people watch (the "viewer count stuck at 0" bug). We DO see every viewer: each
# browser re-fetches a playlist/segment every few seconds through stream_hls_proxy. Viewers no longer carry a
# per-session cookie (the proxy holds MediaMTX's session server-side — see _hls_session_cookie), so we key the
# headcount on a client fingerprint (IP + UA) instead. Count distinct fingerprints seen recently, per token.
# Per-process is fine: streaming is single-node, single-worker (same constraint as the in-memory node-job registry).
_VIEWER_WINDOW = 30.0   # a live viewer re-fetches well within this; drop them this long after their last request
_hls_viewers: dict[str, dict[str, float]] = {}


def _mark_viewer(token: str, session_id: str) -> None:
    if not token or not session_id:
        return
    _hls_viewers.setdefault(token, {})[session_id] = time.monotonic()


def _count_viewers(token: str) -> int:
    seen = _hls_viewers.get(token)
    if not seen:
        return 0
    cutoff = time.monotonic() - _VIEWER_WINDOW
    for sid in [s for s, t in seen.items() if t < cutoff]:
        del seen[sid]
    if not seen:
        _hls_viewers.pop(token, None)
        return 0
    return len(seen)


# ---------------------------------------------------------------- HLS session (held server-side)
# MediaMTX v1.19 gates every HLS variant/segment behind a session it opens on the master playlist: a
# `?cookieCheck=1` redirect sets a `cookieCheck` cookie, then the master hands back an `hlsSession=<uuid>`
# cookie that EVERY follow-up request must carry (else 400/401). This fires whenever `authMethod` is set —
# it is NOT the publish-auth hook, so excluding `read` from auth does not remove it. A third-party player
# (zap.stream, Amethyst, …) fetches our proxied playlist cross-origin WITHOUT credentials and so never
# returns that cookie → the variant 401s → black video for everyone but VLC/WebRTC. Fix: prime and hold the
# session HERE and inject it on the upstream hop, so the browser needs no cookie and we serve plain
# `ACAO: *`. All upstream requests come from 127.0.0.1 (this proxy), so one session per token serves every
# viewer. NB: httpx's cookie jar drops the *Secure* hlsSession over our plain-http hop, so we parse it out
# of Set-Cookie by hand rather than relying on the jar.
_HLS_SESSION_TTL = 20.0
_hls_sessions: dict[str, tuple[str, float]] = {}   # token -> ("cookieCheck=1; hlsSession=..", monotonic_ts)


async def _hls_session_cookie(hls_port: str, token: str, force: bool = False) -> "str | None":
    """Upstream Cookie header that authorizes MediaMTX HLS for `token`; primes + caches it, refreshes on demand."""
    now = time.monotonic()
    if not force:
        ent = _hls_sessions.get(token)
        if ent and (now - ent[1]) < _HLS_SESSION_TTL:
            return ent[0]
    import httpx
    url = f"http://127.0.0.1:{hls_port}/{token}/index.m3u8?cookieCheck=1"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as c:
            r = await c.get(url, headers={"Cookie": "cookieCheck=1"})
            if r.status_code != 200:
                return None
            hs = None
            for sc in r.headers.get_list("set-cookie"):
                sc = sc.strip()
                if sc.startswith("hlsSession="):
                    hs = sc.split(";", 1)[0].split("=", 1)[1].strip()
            cookie = "cookieCheck=1" + (f"; hlsSession={hs}" if hs else "")
            _hls_sessions[token] = (cookie, now)
            return cookie
    except Exception:
        return None


def _client_fingerprint(request: Request) -> str:
    """Per-viewer headcount key now that viewers carry no per-session cookie. Behind the tunnel the real
    client IP is the first X-Forwarded-For hop; pair it with the User-Agent."""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = xff or (request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{ip}|{ua}".encode("utf-8", "replace")).hexdigest()[:16]


def _public_origin(request: Request) -> str:
    """The origin as the OUTSIDE world sees it, not as uvicorn sees it.

    We sit behind a reverse proxy that terminates TLS, so request.base_url is `http://…`. That url ends up in
    the kind-30311's `streaming` tag — a PUBLIC, cross-client link — and every serious client (zap.stream, the
    PWA, anything on https) refuses to load an http playlist from an https page as mixed content. The stream
    then looks broken everywhere but here. Trust the proxy's X-Forwarded-* (only this app is exposed through
    it), falling back to what the request itself claims.
    """
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    proto = fwd_proto or request.url.scheme or "https"
    host = fwd_host or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


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


def _cache_control(path: str) -> str:
    """Cache policy for one HLS asset. This is the difference between O(N) and O(1) bandwidth.

    Segments are IMMUTABLE and uniquely named — MediaMTX never reuses a segment name within a stream — so
    they can be cached forever and every viewer after the first is served by the edge (Cloudflare here)
    instead of this server's uplink. Sending `no-cache` on them, as this used to, forbids every cache in
    the chain and makes N viewers cost N x bitrate off a home connection.

    The PLAYLIST is the opposite: it is rewritten every segment duration and IS the liveness signal, so it
    gets ~1s. That split is the whole trick — playlists are a few hundred bytes and segments are hundreds of
    KB, so caching only the big immutable half captures essentially all of the saving while keeping latency.
    `no-transform` stops a proxy re-encoding the media (Cloudflare Polish et al. mangling fMP4).
    """
    if path.endswith(".m3u8"):
        # max-age=1 rather than no-cache: it still collapses the thundering herd of viewers polling the
        # same playlist within the same second, which is exactly what a popular stream produces.
        return "public, max-age=1, no-transform"
    return "public, max-age=31536000, immutable, no-transform"


# Which MediaMTX path a viewer is served: the clamped transcode when it's up, else the raw source. Probed
# against MediaMTX's control API and memoised briefly — a live viewer re-fetches every couple of seconds, so
# without the memo every viewer would add a control-API round-trip to every segment.
_CLAMP_TTL = 5.0
_clamp_ready: dict[str, tuple[bool, float]] = {}


async def _upstream_path(token: str) -> str:
    """The MediaMTX path to proxy for `token` — `<token>_clamped` while the clamp is publishing it.

    Falls back to the source path whenever the clamp isn't up: during the second or two after go-live before
    ffmpeg has produced anything, when the admin has clamping off, and when MediaMTX can't be reached (in
    which case the source won't work either, but failing to the unclamped path can only be less broken).
    """
    if not stream_service.clamp_enabled():
        return token
    name = f"{token}{stream_service.CLAMP_SUFFIX}"
    now = time.monotonic()
    ent = _clamp_ready.get(name)
    if ent is not None and now - ent[1] < _CLAMP_TTL:
        return name if ent[0] else token
    ready = bool(await stream_end_service.is_publishing(name))
    _clamp_ready[name] = (ready, now)
    if len(_clamp_ready) > 512:                 # bound the memo; entries are re-probed on demand anyway
        for k in [k for k, v in _clamp_ready.items() if now - v[1] > _CLAMP_TTL]:
            _clamp_ready.pop(k, None)
    return name if ready else token


def _may_stream(user) -> bool:
    """Admins always may; everyone else needs the granted capability. Watching is NOT gated — only
    publishing, which is what costs bandwidth and carries the instance's name."""
    return bool(getattr(user, "is_admin", False) or getattr(user, "can_stream", False))

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
    path = (body.get("path") or "").strip()
    q = (body.get("query") or "").strip().strip("?/&")
    # The bitrate clamp publishes its transcoded copy back into MediaMTX over loopback RTSP (clamp.sh). It
    # holds no API key — there is no user behind it — so it proves itself with a secret derived from the
    # auth-hook secret, carried in the RTSP URL query. Two things must hold: that secret, and a base name
    # that is a real streamer's token (so this can't be used to publish arbitrary paths).
    # NB: authorizing on MediaMTX's reported publisher `ip` instead does NOT work — it reports a LAN address
    # for connections made to a 127.0.0.1-bound listener, so a loopback check denies every clamp and viewers
    # silently get the unclamped source. Measured against MediaMTX v1.19.2; don't "simplify" it back.
    if path.endswith(stream_service.CLAMP_SUFFIX):
        base = path[: -len(stream_service.CLAMP_SUFFIX)]
        want = stream_service.clamp_secret()
        got = ""
        try:
            got = (parse_qs(q).get("clamp") or [""])[0]
        except Exception:
            got = ""
        if want and hmac.compare_digest(got, want) and stream_end_service.user_by_token(db, base) is not None:
            return Response(status_code=200)
        logger.info("[stream] clamp publish denied for %r", path)
        return JSONResponse({"error": "not a clamp publish"}, status_code=403)
    # Extract the API key from the RTMP/SRT query. Two shapes reach us, both legitimate:
    #   OBS         → "key=<api_key>"   (stream key "<token>?key=<api_key>")
    #   the app     → "<api_key>"       (bare, no "key=" — see _native_rtmp_url for why)
    # and either can arrive with a stray separator glued on by the encoder's URL joining, so the value is
    # parsed leniently rather than assumed well-formed. A bare query is only ever treated as the key when it
    # carries no "=" at all, so a real query string can never be mistaken for one.
    key = ""
    if q:
        if "=" in q:
            try:
                key = (parse_qs(q).get("key") or [""])[0]
            except Exception:
                key = ""
        else:
            key = q
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
    own = db.query(UserSetting).filter(UserSetting.user_id == row.user_id,
                                       UserSetting.key == _TOKEN_SETTING).first()
    if not own or not own.value or path != own.value:
        logger.info("[stream] publish denied (path %r is not the key owner's token)", path)
        return JSONResponse({"error": "not your stream"}, status_code=403)
    # The OBS path is the one that survives a revoke: a stream key already pasted into someone's
    # encoder keeps working unless permission is checked HERE, at the moment MediaMTX asks.
    _owner = db.query(User).filter(User.id == row.user_id).first()
    if not _may_stream(_owner):
        logger.info("[stream] publish denied (user %s has no stream permission)", row.user_id)
        return JSONResponse({"error": "no permission"}, status_code=403)
    # The feed is really flowing now — let the reaper end this stream if it later disappears.
    try:
        stream_end_service.mark_publishing(db, row.user_id)
        # Record the authoritative go-live time for this session so the VOD finalizer knows the real
        # session start (and which segments belong to it), independent of file mtimes. No-op if recording
        # is off. Preserved across a reconnect (only written when absent), cleared when the VOD is claimed.
        if (settings_store.get("stream_record_enabled", "") or "").strip().lower() == "true":
            from app.services import stream_vod_service
            stream_vod_service.mark_golive(path)
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
    # The clamp's output path is not a stream — it's our own transcode of one. The generated config gives
    # clamped paths no runOnNotReady, so this shouldn't fire; belt-and-braces for a hand-edited config,
    # because ending on this name would schedule an end that the source path can never re-confirm as live.
    if token.endswith(stream_service.CLAMP_SUFFIX):
        return Response(status_code=200)
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
    if not _may_stream(current_user):
        return {"enabled": False, "error": "no_permission",
                "message": "Live streaming isn't enabled for your account yet — ask an admin for access."}
    cfg = settings_store.all_settings()
    enabled = (cfg.get("stream_enabled", "false") or "").strip().lower() == "true"
    rtmp_port = (cfg.get("stream_rtmp_port", "") or "1935").strip()
    host = (cfg.get("stream_domain", "") or "").strip() or (cfg.get("turn_public_ip", "") or "").strip() \
        or (request.url.hostname or "")
    token = _user_token(db, current_user)
    api_key = _obs_key(db, current_user)

    # HLS playback URL: a configured direct base (grey-clouded stream subdomain, scales best) or the
    # app's reverse-proxy path (zero-config — rides the existing tunnel).
    origin = _public_origin(request)
    hls_base = (cfg.get("stream_hls_base", "") or "").strip().rstrip("/")
    if hls_base:
        # A direct base bypasses our proxy, so the clamped path has to be baked into the public URL here.
        # Unlike the proxy path this is decided ONCE, at go-live: an admin who toggles clamping mid-stream
        # breaks the URL already published in that streamer's kind-30311. That's the accepted trade-off for
        # the direct-base deployment (an advanced, set-and-forget option) — the proxy path resolves live.
        suffix = stream_service.CLAMP_SUFFIX if stream_service.clamp_enabled(cfg) else ""
        hls_url = f"{hls_base}/{token}{suffix}/index.m3u8"
    else:
        hls_url = f"{origin}/api/streams/hls/{token}/index.m3u8"

    record_on = (cfg.get("stream_record_enabled", "") or "").strip().lower() == "true"
    return {
        "enabled": enabled,
        "rtmp_url": f"rtmp://{host}:{rtmp_port}",
        "stream_key": f"{token}?key={api_key}",
        "token": token,
        "hls_url": hls_url,
        # Save-to-Blossom recording: whether the node offers it, and this user's opt-in.
        "record_available": record_on,
        "record_enabled": bool(getattr(current_user, "stream_record", False)),
        # Per-streamer quality tier. Offered only when the clamp is actually on — with no clamp there is no
        # transcode to lower, so a picker would be a control that does nothing.
        "quality_available": stream_service.clamp_enabled(cfg),
        "quality": stream_service.get_quality(token),
        "quality_tiers": sorted(stream_service.QUALITY_TIERS.keys(), reverse=True),
        # The Android app's native screen share pushes RTMP directly (its WebView has no getDisplayMedia, so
        # the screen can't go through WHIP). It needs the WHOLE url, not OBS's server+key split — and it must
        # be built HERE, because the encoder (RootEncoder) parses an RTMP url as `app/stream` and mangles the
        # usual "<token>?key=<api_key>" form into app="<token>?" + stream="key=…", which MediaMTX would never
        # resolve to the path <token>. A BARE query survives its parser intact (it only strips `k=v` pairs it
        # recognises), keeping the path exactly <token> — so the key rides as a bare query and /auth above
        # accepts it. Don't "tidy" this into `?key=` without re-reading that parser.
        "rtmp_native_url": f"rtmp://{host}:{rtmp_port}/{token}?{api_key}",
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
    # Gate the PUBLISH itself, not just the credentials screen: /ingest hands out the RTMP key, but a
    # client that already had one (or a stale build) must not be able to go live without permission.
    if not _may_stream(current_user):
        return Response(status_code=403)
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


@router.get("/viewers/{token}")
async def stream_viewers(token: str):
    """How many people are actually watching, straight from MediaMTX.

    Each HLS reader on the path is one viewer. Nothing here is secret — the token already rides the public
    kind-30311 and the HLS url — so this needs no auth; it just reports what the media server already knows.
    (The NIP-53 `current_participants` tag can only be written by the streamer's own client, which is what
    consumes this.) Any failure reports "not live" rather than an error: a viewer count is decoration, and it
    must never be able to break the page that shows it.
    """
    if not _stream_enabled():
        return {"live": False, "viewers": 0}
    if not token.isalnum():
        return JSONResponse({"error": "bad token"}, status_code=400)
    # HLS viewers counted from the proxy (the real headcount); MediaMTX readers only catch RTSP/RTMP/WebRTC.
    pc = _count_viewers(token)
    api_port = (settings_store.get("stream_api_port", "9997") or "9997").strip()
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0)) as client:
            r = await client.get(f"http://127.0.0.1:{api_port}/v3/paths/get/{token}")
        if r.status_code != 200:
            return {"live": pc > 0, "viewers": pc}
        data = r.json()
    except Exception:
        return {"live": pc > 0, "viewers": pc}
    # Drop RTSP readers before counting. RTSP is bound to loopback and exists ONLY so the bitrate clamp can
    # read the source and publish the transcode back, so an rtspSession here is our own ffmpeg, never a
    # person — counting it reported "1 viewer" on every live stream that nobody was watching.
    readers = [x for x in (data.get("readers") or [])
               if isinstance(x, dict) and x.get("type") != "rtspSession"]
    return {"live": bool(data.get("ready")), "viewers": max(len(readers), pc)}


@router.api_route("/hls/{token}/{path:path}", methods=["GET", "HEAD"])
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
    if request.method == "HEAD":
        # Players (and native <video>) probe the playlist with a HEAD preflight; answer it directly with the
        # right content-type instead of 405ing. The follow-up GET does the real cookie-gated MediaMTX proxy.
        ct = ("application/vnd.apple.mpegurl" if path.endswith(".m3u8")
              else "video/mp2t" if path.endswith(".ts")
              else "video/mp4" if path.endswith((".mp4", ".m4s"))
              else "application/octet-stream")
        return Response(status_code=200, media_type=ct)
    hls_port = (settings_store.get("stream_hls_port", "8888") or "8888").strip()
    # Viewers always address the PUBLIC token (it's what rides the kind-30311); the clamped transcode is an
    # internal path they never see, so the swap happens here. Segment URLs inside a MediaMTX playlist are
    # relative, so the follow-up segment requests come back to this same route and resolve identically.
    src = await _upstream_path(token)
    upstream = f"http://127.0.0.1:{hls_port}/{src}/{path}?cookieCheck=1"
    _mark_viewer(token, _client_fingerprint(request))   # headcount by client fingerprint (no per-viewer cookie now)
    import httpx

    async def _open(cook: str):
        cl = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=4.0))
        rq = cl.build_request("GET", upstream, headers={"Cookie": cook})
        rp = await cl.send(rq, stream=True)
        return cl, rp

    cookie = await _hls_session_cookie(hls_port, src)
    if cookie is None:
        return Response(status_code=502)
    client = None
    resp = None
    try:
        client, resp = await _open(cookie)
        # A rotated/expired MediaMTX session answers the variant/segment with 400/401/403. The browser can't
        # fix this (it holds no cookie), so re-prime the session HERE and retry once before giving up.
        if resp.status_code in (400, 401, 403):
            await resp.aclose(); await client.aclose(); client = resp = None
            cookie = await _hls_session_cookie(hls_port, src, force=True)
            if cookie is None:
                return Response(status_code=502)
            client, resp = await _open(cookie)
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

    async def _body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    # The browser carries NO cookie now (we hold MediaMTX's session server-side), so an uncredentialed
    # cross-origin fetch works and we can serve the maximally-compatible `Access-Control-Allow-Origin: *`
    # to every third-party player (zap.stream, Amethyst, …). No Set-Cookie is relayed to the browser.
    return StreamingResponse(_body(), media_type=media_type,
                             headers={"Cache-Control": _cache_control(path),
                                      "Access-Control-Allow-Origin": "*"})


# ---------------------------------------------------------------- Past streams (VODs)

def _vod_url(sha256: str) -> str:
    """Public playback URL for a VOD blob (BUD-01 GET on the Blossom server)."""
    base = (settings_store.get("blossom_public_url", "") or "").strip().rstrip("/")
    # Fallback to the built-in Blossom server's BUD-01 GET route (mounted at /blossom, no /api prefix).
    return f"{base}/{sha256}" if base else f"/blossom/{sha256}"


def _vod_json(v: StreamVOD) -> dict:
    return {
        "id": v.id,
        "token": v.token,
        "url": _vod_url(v.sha256),
        "sha256": v.sha256,
        "size": v.size,
        "duration_s": v.duration_s,
        "title": v.title,
        "started_at": v.started_at,
        "created_at": v.created_at,
    }


@router.get("/vods")
def stream_vods(current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """The signed-in user's saved past streams, newest first."""
    rows = (db.query(StreamVOD).filter(StreamVOD.user_id == current_user.id)
            .order_by(StreamVOD.started_at.desc()).limit(200).all())
    return {"vods": [_vod_json(v) for v in rows]}


@router.get("/vods/by-token/{token}")
def stream_vods_by_token(token: str, db=Depends(get_db)):
    """A streamer's past streams by publish token — PUBLIC, like HLS playback (the bytes are already
    public on the Blossom server). Lets a viewer watch streams that ended without needing an account."""
    rows = (db.query(StreamVOD).filter(StreamVOD.token == token)
            .order_by(StreamVOD.started_at.desc()).limit(200).all())
    return {"vods": [_vod_json(v) for v in rows]}


@router.post("/quality")
def stream_quality_set(body: dict, current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """Set THIS user's stream quality tier ("720"/"480"/"360", anything else = auto//node default).

    Keyed by the caller's own publish token, so a user can only ever change their own stream — and the
    tier is capped against the admin clamp settings when clamp.sh is generated, so this can only lower
    what the node already does, never ask it for more bandwidth.
    """
    # JSONResponse, not HTTPException — this router imports neither HTTPException nor anything that
    # re-exports it, and every other handler here reports errors the same way.
    token = _user_token(db, current_user)
    if not token:
        return JSONResponse({"error": "no stream token for this user"}, status_code=400)
    try:
        tier = stream_service.set_quality(token, str(body.get("quality") or ""))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    logger.info("[stream] %s set quality tier %s", token[:8], tier)
    return {"quality": tier}


@router.post("/record")
def stream_record_toggle(body: dict, current_user: User = Depends(get_current_user), db=Depends(get_db)):
    """Set the per-user 'save my ended streams to Blossom' opt-in. Mirrored to Nostr (users_store) so it
    hydrates on a fresh node. The global stream_record_enabled kill-switch still gates recording."""
    raw = body.get("enabled")
    # Coerce robustly: a JSON string like "false"/"0" is truthy to bool(), which would make the
    # toggle impossible to turn off.
    enabled = raw.strip().lower() in ("1", "true", "yes", "on") if isinstance(raw, str) else bool(raw)
    current_user.stream_record = enabled
    db.commit()
    try:
        users_store.sync_user_blocking(db, current_user)
    except Exception as e:
        logger.warning("[stream] record-opt-in Nostr sync failed for %s: %s", current_user.id, e)
    return {"stream_record": enabled}

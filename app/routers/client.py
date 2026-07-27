"""Cyberpunk Nostr web client (PWA) served at /client.

The actual Nostr logic lives in the browser (static/js/client/*): it talks ONLY to this node's
built-in relay over WebSocket and to the built-in Blossom server over HTTP, signs with a NIP-07
extension or an in-page nsec (crypto in a Web Worker), and never sends secret keys to the server.

This router only:
  * serves the SPA shell, PWA manifest and service worker,
  * exposes a small `/client/config` (relay URL, operator npub, Blossom base) so the page isn't
    hardcoded per-deployment,
  * `/client/signup-follow`: because the relay is web-of-trust-gated, a brand-new account can't
    publish. On signup the node's operator account auto-follows the new pubkey (kind-3) and a WoT
    refresh is triggered, pulling the newcomer into the trust graph (depth-1) so they can post.
"""
import asyncio
import base64
import contextlib
import glob
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response, Query, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import emoji_service, settings_store, tor_service
from app.services.nostr import nostr_service, event as nostr_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/client", tags=["client"])

_TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "..", "templates"))
_STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "static")


def _setting(db: Session, key: str, default: str = "") -> str:
    v = settings_store.get(key)
    return (v if v else default)


def _default_theme(db: Session) -> str:
    """Admin-chosen default UI theme for /client, validated against the known theme slugs so a stale
    value can never paint a broken theme. Visitors/devices without their own pick get this one."""
    from app.schemas import CLIENT_THEMES
    t = _setting(db, "client_default_theme", "professional")
    return t if t in CLIENT_THEMES else "professional"


def _relay_url(request: Request, db: Session) -> str:
    # Reached over our .onion? Then the admin's clearnet relay URL is exactly the wrong answer — it
    # would drag every socket back out an exit node (or just fail, for an onion-only client). Tor
    # forwards TCP and not paths, so the relay rides the onion on its OWN port (see tor_service
    # .onion_relay_port), which is the same shape as the direct-access fallback below.
    onion = tor_service.request_onion_host(request)
    if onion:
        return f"ws://{onion}:{_setting(db, 'nostr_relay_port', '3052')}/relay"
    explicit = _setting(db, "client_relay_url")
    if explicit:
        return explicit
    # Behind a reverse proxy (nginx/prod): it routes /relay → the relay, so use the SAME public
    # host + the /relay path with the forwarded scheme.
    fwd_host = request.headers.get("x-forwarded-host")
    if fwd_host:
        proto = request.headers.get("x-forwarded-proto") or "https"
        ws = "wss" if proto == "https" else "ws"
        return f"{ws}://{fwd_host}/relay"
    # Direct access (no reverse proxy — e.g. a turnkey node hit at http://host:3051/client): the
    # relay is its OWN server on nostr_relay_port (default 3052, published by the container), NOT
    # reachable at /relay on the app's port — so the app would 403 a ws://host:3051/relay. Point the
    # client straight at the relay's published port instead. (Custom port mappings: set
    # client_relay_url explicitly.)
    host = (request.headers.get("host") or request.url.netloc).split(":")[0]
    ws = "wss" if request.url.scheme == "https" else "ws"
    port = _setting(db, "nostr_relay_port", "3052")
    return f"{ws}://{host}:{port}/relay"


def _blossom_url(request: Request, db: Session) -> str:
    """Public base URL the client uploads blobs to. Mirrors the relay URL logic and the Blossom
    router's own `_base_url`: use the admin-set `blossom_public_url` if present, otherwise DERIVE it
    from the (proxied) request so a fresh node works out of the box — the admin sets a real domain
    for production. Without this the client would get an empty URL and uploads would silently fail.

    Onion visitors get the onion base instead of the configured clearnet one: an upload's response URL
    is what ends up EMBEDDED IN THE NOTE the user publishes, so a clearnet media host would (a) exit Tor
    for every image and (b) stamp the instance's real domain into their posts."""
    onion = tor_service.request_onion_host(request)
    if onion:
        return f"http://{onion}/blossom"
    explicit = _setting(db, "blossom_public_url").rstrip("/")
    if explicit:
        return explicit
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/blossom"


def _operator(db: Session) -> User | None:
    """The node's signing operator: prefer an admin with a linked Nostr secret, else any user
    with one. Used to auto-follow new signups into the web of trust."""
    q = db.query(User).filter(User.nostr_nsec.isnot(None))
    return q.filter(User.is_admin == True).first() or q.first()  # noqa: E712


def _static_version() -> str:
    """A cache-busting token derived from the newest mtime of the client's CSS/JS. Cloudflare
    rewrites these assets' Cache-Control to a 31-day max-age, so without a versioned URL a browser
    keeps serving a stale client.css/app.js for weeks after a deploy. Appending `?v=<this>` makes
    each change a brand-new URL no cache can answer with old bytes. Computed per request from
    mtimes, so it updates on every file change — even UI-only deploys that don't restart Python.
    Globs the WHOLE client JS dir (not a fixed list) so a game-file-only edit (e.g. holdem.js) also
    bumps the token — otherwise that one file would stay stale behind the long edge cache."""
    paths = glob.glob(os.path.join(_STATIC, "js", "client", "*.js"))
    paths.append(os.path.join(_STATIC, "css", "client.css"))
    mt = 0.0
    for p in paths:
        try:
            mt = max(mt, os.path.getmtime(p))
        except OSError:
            pass
    return str(int(mt))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def client_app(request: Request, db: Session = Depends(get_db)):
    # `secure` gates the upgrade-insecure-requests CSP: harmless over HTTPS (server1 via Cloudflare),
    # but over plain HTTP (e.g. http://nas.lan:3051 on the LAN) it would force every script/CSS to
    # https://<host> — which a node serving bare HTTP doesn't have — breaking the whole page.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    # Nostr-only deployments hide the AI tab + AI compose actions (POSTERCHANAI_NOSTR_ONLY=1).
    nostr_only = os.getenv("POSTERCHANAI_NOSTR_ONLY", "0").lower() in ("1", "true", "yes", "on")
    return _TEMPLATES.TemplateResponse("client.html",
        {"request": request, "ver": _static_version(), "secure": proto == "https",
         "nostr_only": nostr_only, "default_theme": _default_theme(db)},
        # This page sent NO Cache-Control, so Chromium (and the Electron desktop app, which loads the client
        # over HTTP) fell back to HEURISTIC caching and served a stale copy. The page is what carries the
        # `?v=<mtime>` tokens for the JS/CSS, so a stale page pins the whole client to OLD assets — deploys
        # looked like they "weren't reaching the desktop app". The assets already revalidate; the shell that
        # references them must never be cached, or the cache-busting it exists to provide cannot work.
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"})


@router.get("/meme-font.ttf")
async def meme_font():
    """Serve the EXACT font the renderer draws captions with, so the Meme Builder preview can use it too.
    Without this the browser fell back to whatever it had (Windows has no Liberation Sans -> Arial, and at
    weight 800 it SYNTHESISES a bolder, wider face), so the caption on screen was not the caption that
    rendered — different glyph shapes and different widths, which is what made positioning unwinnable."""
    from app.services.effects_service._common import _meme_font_path
    path = _meme_font_path()
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="no meme font installed")
    return FileResponse(path, media_type="font/ttf",
                        headers={"Cache-Control": "public, max-age=604800", "Access-Control-Allow-Origin": "*"})


@router.get("/config")
async def client_config(request: Request, db: Session = Depends(get_db)):
    op = _operator(db)
    op_npub = None
    if op and op.nostr_npub:
        op_npub = op.nostr_npub
    # npubs of admin accounts — the client shows admin-only controls (block) for these. The
    # block endpoint still verifies a signed request server-side, so this list isn't a trust gate.
    admin_npubs = [u.nostr_npub for u in db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).all()]  # noqa: E712
    return JSONResponse({
        "relay_url": _relay_url(request, db),
        "blossom_url": _blossom_url(request, db),
        "blossom_enabled": _setting(db, "blossom_enabled", "false").lower() == "true",
        # Whether this node runs the built-in media server. The client uses it only to decide whether
        # to SHOW the "Go Live" entry points — /api/streams/* still gates the real thing.
        "stream_enabled": _setting(db, "stream_enabled", "false").lower() == "true",
        # GRASP git host for the client's "Create repo" flow. A node can HOST git (git_server_enabled)
        # OR PROXY it to the hosting node (git_server_proxy_url) — the node serving /client is usually the
        # PROXY (e.g. server1 → nas), which has no local public_base. So expose the base when known AND a
        # "can create here" flag; the client falls back to its own origin + /git when the base is blank.
        # /client/git/create re-verifies the NIP-98 + the allowlist on the hosting node regardless.
        "git_host_base": _setting(db, "git_server_public_base", ""),
        "git_create_available": (_setting(db, "git_server_enabled", "false").lower() == "true"
                                 or bool(_setting(db, "git_server_proxy_url", ""))),
        "operator_npub": op_npub,
        "admin_npubs": admin_npubs,
        # Fresh install with no admin yet → the client offers first-run "become admin" setup
        # (solves the chicken/egg: nobody can grant AI access until an admin exists).
        "admin_unclaimed": len(admin_npubs) == 0,
        "gif_enabled": bool(_setting(db, "tenor_api_key") or _setting(db, "giphy_api_key")),
        # Public source link shown on the logged-out guest card. Overridable so a fork points at
        # its own repo instead of ours.
        "source_url": _setting(db, "source_url", "https://github.com/loblawbob873-svg/posterchanai"),
        "name": _setting(db, "site_name", "PosterChan"),
        # Custom logo URL (Admin → Site Settings); blank → the client keeps its built-in logo.
        "logo_url": _setting(db, "site_logo_url", ""),
        # Default UI theme for visitors/devices without their own saved pick (Admin → Site Settings).
        "default_theme": _default_theme(db),
        # Community size — the relay's web-of-trust member count (cached in its status file; cheap).
        "users": _relay_user_count(),
        # npubs of the enabled game-referee bots (chess/ttt/hangman), so each Games tab can tag the
        # right bot when inviting an opponent.
        "chess_bot_npub": _game_bot_npub(db, "--chess"),
        "ttt_bot_npub": _game_bot_npub(db, "--ttt"),
        "hangman_bot_npub": _game_bot_npub(db, "--hangman"),
        "connect4_bot_npub": _game_bot_npub(db, "--connect4"),
        "blackjack_bot_npub": _game_bot_npub(db, "--blackjack"),
        "holdem_bot_npub": _game_bot_npub(db, "--holdem"),
    })


def _emoji_base(request: Request) -> str:
    """Public base for custom-emoji URLs. These are written INTO published notes as NIP-30 tags, so
    they must be absolute and reachable from other clients — derived from the (proxied) request the
    same way `_blossom_url` derives the media base, and pinned to the onion for onion visitors."""
    onion = tor_service.request_onion_host(request)
    if onion:
        return f"http://{onion}/client/emoji"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/client/emoji"


@router.get("/emojis")
async def client_emojis(request: Request):
    """The instance's custom emoji, for the client's picker.

    Deliberately COMPACT — shortcode, pack and filename, with the absolute base sent once — because
    an instance can have thousands (the pack this was built against has 3336, and repeating the full
    URL per entry tripled the payload the picker downloads on a phone). The client rebuilds
    `<base>/<pack>/<file>` for the NIP-30 tag and appends `?t=1` for the grid thumbnail."""
    out = [{"s": e["shortcode"], "p": e["pack"], "f": e["shortcode"] + e["ext"]}
           for e in emoji_service.index()]
    return JSONResponse({"base": _emoji_base(request), "emojis": out},
                        headers={"Cache-Control": "public, max-age=60",
                                 "Access-Control-Allow-Origin": "*"})


@router.get("/emoji/{pack}/{name}")
async def client_emoji_file(pack: str, name: str, t: int = 0):
    """One emoji image. `?t=1` serves the cached 72px still the picker grid uses. The path is only
    ever resolved THROUGH the index (never joined onto the request), so a crafted pack/name can't
    escape the emoji directory. Immutable + CORS: other Nostr clients fetch these cross-origin."""
    shortcode = os.path.splitext(name)[0]
    entry = emoji_service.lookup(pack, shortcode)
    if not entry:
        raise HTTPException(status_code=404, detail="no such emoji")
    path, media = entry["path"], None
    if t:
        thumb = emoji_service.thumbnail(pack, shortcode)
        if thumb:
            path, media = thumb, "image/webp"
    if not media:
        import mimetypes
        media = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, headers={
        # A thumbnail is a DERIVED file at a stable URL, so it gets a day — long enough to make the
        # grid free on re-open, short enough that replacing an emoji in Admin doesn't leave the old
        # art in pickers for a week. The full image is what notes point at: cache it hard.
        "Cache-Control": "public, max-age=86400" if t else "public, max-age=604800",
        "Access-Control-Allow-Origin": "*",
        # These are operator-uploaded files served from a public, unauthenticated path — never let a
        # browser sniff one into something it can execute.
        "X-Content-Type-Options": "nosniff",
    })


def _game_bot_npub(db, flag: str) -> str | None:
    """npub of an enabled game-referee bot whose modes include `flag` (e.g. --chess/--ttt/--hangman),
    derived from the bot's nsec in its JSON config (Bot has no npub column)."""
    try:
        import json as _json
        from app.models import Bot
        for bot in db.query(Bot).filter(Bot.enabled == True, Bot.modes.like(f"%{flag}%")).all():  # noqa: E712
            try:
                nsec = (_json.loads(bot.config or "{}")).get("nostr_nsec")
                if nsec:
                    return nostr_service.npub_from_seckey(nsec)
            except Exception:
                continue
        return None
    except Exception:
        return None


def _relay_user_count() -> int:
    """WoT member count from the relay status (community size shown in the client). Cheap + cached;
    returns 0 if the relay isn't running."""
    try:
        from app.services.nostr_relay.thread import relay_status
        return int(relay_status().get("members", 0) or 0)
    except Exception:
        return 0


# Active web-client viewers — a far more accurate "people using the SITE right now" than raw relay
# connections (which also count external clients, scrapers and the federation). The /client page
# polls /client/stats ~every 60s; we stamp each poller and count the DISTINCT ones seen in the last
# window. Deduped by a client-supplied stable id (the user's npub when logged in, else a per-browser
# anon id) so multiple tabs / a logged-in user on two devices collapse to one; falls back to the
# forwarded client IP for old clients. Per-process (single worker); monotonic clock so the NTP
# clock-step on startup can't skew the window.
import time as _time
_VIEWERS: "dict[str, float]" = {}
_VIEWER_WINDOW = 150.0   # seconds — tolerates one missed 60s poll
_LOCAL_VIDS = {"127.0.0.1", "::1", "localhost", "?"}   # internal/loopback callers are never "people online"
_BOT_VID_CACHE: "dict" = {"set": set(), "ts": 0.0}


def _bot_viewer_ids() -> set:
    """Viewer-ids that correspond to OUR OWN nostr bots, so they never count as online people. The
    client viewer-id is 'k'+pubkey[:16] (see _viewerId in app.js). Cached ~5 min — bot keys rarely change."""
    now = _time.monotonic()
    if _BOT_VID_CACHE["set"] and (now - _BOT_VID_CACHE["ts"]) < 300:
        return _BOT_VID_CACHE["set"]
    s = set()
    try:
        from app.services.bot_manager_service import _all_nostr_bot_pubkeys
        s = {"k" + p[:16] for p in (_all_nostr_bot_pubkeys() or "").split(",") if p}
    except Exception:
        pass
    _BOT_VID_CACHE["set"], _BOT_VID_CACHE["ts"] = s, now
    return s


def _record_viewer(request: Request, vid: str) -> int:
    now = _time.monotonic()
    key = (vid or "").strip()[:80]
    if not key:
        xff = request.headers.get("x-forwarded-for", "") or request.headers.get("x-real-ip", "")
        key = (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "?")
    cutoff = now - _VIEWER_WINDOW
    # Count REAL people only: skip our own bots (by pubkey-id) and loopback/internal callers.
    if key and key not in _LOCAL_VIDS and key not in _bot_viewer_ids():
        _VIEWERS[key] = now
    for _k in [k for k, t in _VIEWERS.items() if t < cutoff]:
        _VIEWERS.pop(_k, None)
    return len(_VIEWERS)


@router.get("/stats")
async def client_stats(request: Request, v: str = ""):
    """Sidebar stats: `users` = WoT network size (cached), `online` = distinct people with the site
    open right now (active /client viewers in the last ~2.5 min, deduped per-user). Polled ~1/min."""
    online = _record_viewer(request, v)
    members = 0
    relay_conns = 0
    calls = 0
    try:
        from app.services.nostr_relay.thread import relay_status
        st = relay_status()
        members = int(st.get("members", 0) or 0)
        # Deduped-by-IP count = distinct PEOPLE connected to the relay right now (not raw sockets, which
        # also count multi-tab/federation/scrapers). Falls back to raw conns if the relay didn't dedup.
        relay_conns = int(st.get("online", st.get("conns", 0)) or 0)
        calls = int(st.get("calls", 0) or 0)
    except Exception:
        pass
    return JSONResponse({"users": members, "online": online, "relay": relay_conns,
                         "calls": calls, "streams": await _live_stream_count()})


_stream_count_cache = {"n": 0, "at": 0.0}


async def _live_stream_count() -> int:
    """How many streams are live on this instance right now = MediaMTX paths that are ready. Cached for 10s
    so the ~per-viewer /client/stats poll doesn't hit MediaMTX every request. Best-effort: any failure
    (streaming disabled, MediaMTX down) yields 0 and the ticker hides itself."""
    import time as _t
    now = _t.monotonic()
    if now - _stream_count_cache["at"] < 10:
        return _stream_count_cache["n"]
    n = 0
    try:
        from app.services import settings_store
        if (settings_store.get("stream_enabled", "false") or "").strip().lower() == "true":
            api_port = (settings_store.get("stream_api_port", "9997") or "9997").strip()
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=1.5)) as client:
                r = await client.get(f"http://127.0.0.1:{api_port}/v3/paths/list")
            if r.status_code == 200:
                items = (r.json() or {}).get("items") or []
                n = sum(1 for it in items if isinstance(it, dict) and it.get("ready"))
    except Exception:
        n = 0
    _stream_count_cache["n"], _stream_count_cache["at"] = n, now
    return n


@router.post("/qr")
async def client_qr(request: Request):
    """Render arbitrary text as a QR-code SVG. Used for Primal-style mobile sign-in: the web shows
    a QR of the ephemeral `nostrconnect://` URI, and a phone signer (Amber / nsec.app / Primal)
    scans it to establish the NIP-46 remote-signer session. POST (not GET) so the connect secret in
    the URI never lands in an access log. Same-origin, no auth — it just encodes whatever is posted."""
    data = (await request.body()).decode("utf-8", "ignore").strip()
    if not data or len(data) > 4096:
        return Response(status_code=400)
    try:
        import io
        import segno
        buf = io.BytesIO()
        # dark-on-white for reliable scanning; quiet zone (border) per spec. SVG = crisp at any size.
        segno.make(data, error="m").save(buf, kind="svg", scale=6, border=2, dark="#000", light="#fff")
        return Response(content=buf.getvalue(), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        logger.warning(f"[client] QR render failed: {e}")
        return Response(status_code=500)


@router.post("/translate")
async def client_translate(request: Request, db: Session = Depends(get_db)):
    """Translate a post's text via the node's own LLM — powers the timeline 'Translate' action.
    Same-origin helper (like /gif, /lnurl); returns 503 where there's no LLM (e.g. a Nostr-only
    node), which the client treats as 'translation unavailable'."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    text = (body.get("text") or "").strip()
    to = (str(body.get("to") or "English")).strip()[:40] or "English"
    # Callers pass an ISO code (navigator.language / Live Translate) OR a name. Keep `to` for the
    # already-in-target short-circuit (compared as to[:2]), but translate INTO the full language NAME
    # in the prompt — an LLM renders "into Filipino"/"into Indonesian" far more reliably than "into tl"/"id".
    _LANG_NAMES = {"en": "English", "th": "Thai", "es": "Spanish", "fr": "French", "de": "German",
                   "it": "Italian", "pt": "Portuguese", "ru": "Russian", "zh": "Chinese", "ja": "Japanese",
                   "ko": "Korean", "ar": "Arabic", "hi": "Hindi", "vi": "Vietnamese", "id": "Indonesian",
                   "tl": "Filipino", "uk": "Ukrainian", "tr": "Turkish", "pl": "Polish", "nl": "Dutch",
                   "my": "Burmese"}
    # Map an ISO code ("en"/"th") or locale ("en-US") → language NAME for the prompt (LLMs render "into
    # Filipino" better than "into tl"). Leave a FREE-TEXT name untouched — a composer "Other…" entry like
    # "Traditional Chinese" must NOT be [:2]-mapped ("tr" → Turkish).
    _to_l = to.strip().lower()
    if _to_l in _LANG_NAMES:
        to_name = _LANG_NAMES[_to_l]
    elif "-" in _to_l and _to_l[:2] in _LANG_NAMES:   # locale code e.g. en-us / pt-br
        to_name = _LANG_NAMES[_to_l[:2]]
    else:
        to_name = to
    # Live Translate sets fast=true: skip the separate source-detection round-trip and the few-shot
    # examples, and cap output low — conversational turns are short, so this ~halves the LLM latency.
    fast = bool(body.get("fast"))
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    text = text[:4000]   # cap: posts are short; bounds the LLM work
    # Strip nostr: refs / bare bech32 entities (npub/nprofile/note/nevent/naddr). A leading mention like
    # "nostr:npub1…" makes the language detector mis-fire (returns the target lang → "unchanged") and the
    # translator echo the whole post back — they're not prose, so drop them and translate the real text.
    import re as _re
    _stripped = _re.sub(r"nostr:[a-z0-9]+|\b(?:npub1|nprofile1|note1|nevent1|naddr1)[0-9a-z]{20,}", " ",
                        text, flags=_re.IGNORECASE)
    _stripped = _re.sub(r"\s+", " ", _stripped).strip()
    if _stripped:
        text = _stripped
    try:
        from app.services.inference_factory import get_inference_service
        svc = get_inference_service(db)
        # Detect the source language FIRST (skipped in fast mode). This avoids two opposite failures:
        # (a) a foreign post echoed + shown as "already in your language", and (b) fabricating a bogus
        # translation for text that genuinely already is the target language. (Live Translate skips this
        # and relies on its own normalized echo-retry to catch a mis-route.)
        if not fast:
            try:
                det = await svc.chat_completion(
                    [{"role": "system", "content": "Identify the language of the user's message. Reply with "
                      "ONLY its ISO 639-1 code (e.g. en, fr, es, ja, de). No other text."},
                     {"role": "user", "content": text[:600]}], max_tokens=4, temperature=0.0)
                src_lang = (det.get("choices") or [{}])[0].get("message", {}).get("content", "").strip().lower()[:2]
            except Exception:
                src_lang = ""
            if src_lang and src_lang == to.strip().lower()[:2]:
                # Genuinely already the target language → return unchanged; the client shows the honest
                # "already in your language" notice (it compares the result to the source).
                return JSONResponse({"text": text})
        msgs = [{"role": "system", "content": f"You are a translation engine. Translate the user's "
              f"message into {to_name}. The message is often colloquial, run-on, code-switched and "
              f"unpunctuated. Translate the ENTIRE message into natural {to_name} — EVERY word or phrase "
              f"that is not already {to_name}. This applies to SHORT, casual, and slang messages too "
              f"(e.g. 'lol that's funny') — they MUST be rendered in {to_name}, never echoed in the "
              f"source language. Do NOT leave any source-language words in the output, and do not "
              f"just re-punctuate the original. Keep @mentions, #hashtags, URLs and emoji exactly "
              f"as-is. Output ONLY the translated text — no preamble, notes, or quotes."}]
        # The echo-fixing examples translate INTO ENGLISH (they fix the 'English-dominant mixed text
        # gets echoed' case). They bias the model toward ENGLISH output, so ONLY include them when the
        # target IS English — otherwise translating to (say) Japanese would wrongly echo the English.
        if not fast and to.strip().lower() in ("english", "en", "en-us", "en-gb"):
            msgs += [
                {"role": "user", "content": "My babies! ang cute nila kahit pagod na pagod ako kakaalaga. Hook Needle"},
                {"role": "assistant", "content": "My babies! They're so cute even though I'm exhausted from taking care of them. Hook Needle"},
                {"role": "user", "content": "Good evening guys ayon bago palang kami nag karoon ng kuryente magmula kasi kanina alas 6 ng umaga nawala tapus ngayon lang nag karoon kumusta ang lahat kumain na ba kayo"},
                {"role": "assistant", "content": "Good evening guys, we only just got electricity back — it had been out since 6 this morning and only returned just now. How is everyone, have you eaten yet?"},
            ]
        msgs.append({"role": "user", "content": text})
        # Translation should be faithful, not creative — near-greedy decoding cuts hallucination.
        res = await svc.chat_completion(msgs, max_tokens=(400 if fast else 1200), temperature=0.0)
        out = (res.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not out:
            return JSONResponse({"error": "translation unavailable"}, status_code=503)
        return JSONResponse({"text": out})
    except Exception as e:
        logger.warning(f"[client] translate failed: {e}")
        return JSONResponse({"error": "translation unavailable"}, status_code=503)


@router.post("/narrate")
async def client_narrate(request: Request, db: Session = Depends(get_db)):
    """Read a post aloud via the node's built-in TTS — powers the timeline 'Read Aloud' action.
    Same-origin helper (like /translate); returns 503 where TTS is unavailable. The client builds the
    text (author name + content, with URLs/hashtags/attachments already stripped) and plays the
    returned base64 MP3."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    text = (body.get("text") or "").strip()[:2000]   # posts are short; bound the TTS work
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    try:
        from app.services.tts_service import TTSService
        audio = await TTSService(db).generate_speech(text, (body.get("voice") or None), (body.get("lang") or None))
        if not audio:
            return JSONResponse({"error": "narration unavailable"}, status_code=503)
        return JSONResponse({"audio": audio})
    except Exception as e:
        logger.warning(f"[client] narrate failed: {e}")
        return JSONResponse({"error": "narration unavailable"}, status_code=503)


async def _img_data_uri(url: str, base_domain: str) -> str:
    """Fetch an image URL → data: URI for the post card. Done SERVER-side because the client can't read
    most avatars/images as bytes (cross-origin CORS). SSRF guard: allow public hosts + the instance's
    own domain/subdomains (so LAN-resolved media.<host> works), refuse other private/internal targets."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return ""
    from urllib.parse import urlparse
    from app.services.command_service._common import _url_is_safe_to_fetch
    host = (urlparse(url).hostname or "").lower()
    ok = bool(base_domain) and (host == base_domain or host.endswith("." + base_domain))
    if not ok:
        ok = await asyncio.to_thread(_url_is_safe_to_fetch, url, [])
    if not ok:
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 (posterchanai-card)"})
        if r.status_code != 200:
            return ""
        ct = (r.headers.get("content-type") or "").split(";")[0].strip()
        if not ct.startswith("image/") or len(r.content) > 8_000_000:
            return ""
        import base64 as _b64
        return f"data:{ct};base64,{_b64.b64encode(r.content).decode()}"
    except Exception:
        return ""


@router.post("/screenshot")
async def client_screenshot(request: Request, db: Session = Depends(get_db)):
    """Render a Nostr note as a clean tweet-style post card PNG (the timeline ☰ → Screenshot action) —
    JUST the post, like the Nitter cards. Reliable + instance-branded: built server-side from the
    note's fields via _render_post_card_png (no live-SPA capture). Avatar/image are fetched server-side
    (the client can't, due to cross-origin CORS) and embedded as data URIs. Returns a base64 PNG."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    import asyncio
    import base64
    from urllib.parse import urlparse
    from app.services import settings_store
    from app.services.command_service import _render_post_card_png
    name = (body.get("name") or "").strip()
    handle = (body.get("handle") or "").strip()
    text = (body.get("text") or "").strip()
    timestamp = (body.get("timestamp") or "").strip()
    if not (handle or text):
        return JSONResponse({"error": "nothing to render"}, status_code=400)

    # registrable domain of THIS instance → also trust its subdomains (e.g. a LAN media.<host>)
    site_host = (urlparse(settings_store.get("site_url") or "").hostname or "")
    req_host = (request.headers.get("host") or "").split(":")[0]
    base = (site_host or req_host or "")
    base_domain = ".".join(base.split(".")[-2:]).lower() if base else ""
    avatar_uri = await _img_data_uri(body.get("avatar_url") or "", base_domain)
    media_uri = await _img_data_uri(body.get("image_url") or "", base_domain)
    try:
        png = await asyncio.wait_for(asyncio.to_thread(
            _render_post_card_png, name or handle, handle, text, timestamp, media_uri, avatar_uri,
        ), timeout=60)
    except Exception as e:
        logger.warning(f"[client] post-card render failed: {e}")
        return JSONResponse({"error": "render failed"}, status_code=503)
    if not png:
        return JSONResponse({"error": "no image"}, status_code=503)
    return JSONResponse({"image": base64.b64encode(png).decode()})


@router.post("/stt")
async def client_stt(audio: UploadFile = File(...), language: str = Form("auto")):
    """Voice input for the web client's AI chat + Live Translate — Whisper speech-to-text. Same-origin
    helper (no app-user auth, like /narrate); returns 503 if STT (faster-whisper) isn't installed.
    `language` defaults to "auto" (Whisper detects it); returns the detected language as `lang` so the
    Live Translate screen can route the turn to the other language."""
    from app.services import stt_service
    if not stt_service.is_available():
        return JSONResponse({"error": "voice input unavailable"}, status_code=503)
    try:
        data = await audio.read()
    except Exception:
        return JSONResponse({"error": "bad audio"}, status_code=400)
    if len(data) < 100:
        return JSONResponse({"error": "audio too small"}, status_code=400)
    try:
        text, lang = await stt_service.transcribe_audio(data, language or "auto")
    except Exception as e:
        logger.warning(f"[client] stt failed: {e}")
        return JSONResponse({"error": "transcription failed"}, status_code=503)
    return JSONResponse({"text": text or "", "lang": lang or ""})


@router.post("/summarize")
async def client_summarize(request: Request, db: Session = Depends(get_db)):
    """Summarize a post/thread via the node's own LLM — powers the timeline 'Summary' action. The
    client sends the post (and its surrounding thread) as plain 'name: text' lines. Same-origin
    helper; 503 where there's no LLM (e.g. a Nostr-only node), shown as 'summary unavailable'."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    text = text[:8000]   # cap the thread blob
    try:
        from app.services.inference_factory import get_inference_service
        svc = get_inference_service(db)
        res = await svc.chat_completion(
            [{"role": "system", "content": "You are a summarizer. The user gives a Nostr post or "
              "conversation thread as 'name: text' lines. Write a NEW, detailed summary IN YOUR OWN "
              "WORDS — never repeat the messages verbatim. Cover what it is about, the key points and "
              "specifics, who said what, any questions raised or conclusions reached, and the overall "
              "tone. Use short paragraphs or bullet points. If it's a single post, summarize just "
              "that post. Translate any non-English parts into English. Output ONLY the summary — no "
              "preamble like 'Here is a summary'."},
             {"role": "user", "content": "alice: heading to the beach today, the weather looks "
              "perfect\n\nbob: lucky! it's pouring rain here\n\nalice: come visit then 😄"},
             {"role": "assistant", "content": "Alice is excited about the perfect weather and is "
              "heading to the beach. Bob replies that it's pouring rain where he is. Alice playfully "
              "invites him to come visit. The exchange is short, light, and friendly."},
             {"role": "user", "content": text}],
            max_tokens=900, temperature=0.3)
        out = (res.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not out:
            return JSONResponse({"error": "summary unavailable"}, status_code=503)
        return JSONResponse({"text": out})
    except Exception as e:
        logger.warning(f"[client] summarize failed: {e}")
        return JSONResponse({"error": "summary unavailable"}, status_code=503)


@router.post("/compose-from-url")
async def compose_from_url(request: Request, db: Session = Depends(get_db)):
    """Draft a social-media post from a pasted link via the node's own LLM — powers the composer's
    🤖 AI button. Fetches the page text (or, for a YouTube link, the video's transcript) and writes
    one engaging summary post, with the original link appended at the end. Same-origin helper; 503
    where there's no LLM (e.g. a Nostr-only node), shown as 'summary unavailable'."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    url = (body.get("url") or "").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return JSONResponse({"error": "no link"}, status_code=400)
    # fetch_url_content centralises page extraction + YouTube transcript handling, so a video link
    # is summarized from its captions (not the contentless watch page).
    try:
        from app.services.search_service import SearchService
        fetched = await SearchService(db).fetch_url_content(url)
    except Exception as e:
        logger.warning(f"[client] compose-from-url fetch failed: {e}")
        return JSONResponse({"error": "could not read that link"}, status_code=502)
    if not fetched or fetched.get("error") or not (fetched.get("content") or "").strip():
        err = (fetched or {}).get("error") or "no readable content (a video may lack captions)"
        return JSONResponse({"error": err}, status_code=422)
    title = (fetched.get("title") or "").strip()
    src = (("Title: " + title + "\n\n") if title else "") + (fetched.get("content") or "").strip()[:6000]
    try:
        from app.services.inference_factory import get_inference_service
        svc = get_inference_service(db)
        res = await svc.chat_completion(
            [{"role": "system", "content": (
                "You write engaging social-media posts. The user gives you the text (or video "
                "transcript) of a web page. Write ONE detailed, natural-sounding post that summarizes "
                "it for a general audience: open with a hook, cover the key points and specifics in a "
                "few short paragraphs, and keep an authentic voice (informative, not clickbait). Do "
                "NOT invent facts, do NOT include the link or any URL (it is added separately), and "
                "do NOT add a preamble like 'Here is a post'. Output ONLY the post text.")},
             {"role": "user", "content": src}],
            max_tokens=700, temperature=0.5)
        out = (res.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not out:
            return JSONResponse({"error": "summary unavailable"}, status_code=503)
        return JSONResponse({"text": out.rstrip() + "\n\n" + url})
    except Exception as e:
        logger.warning(f"[client] compose-from-url failed: {e}")
        return JSONResponse({"error": "summary unavailable"}, status_code=503)


@router.post("/hashtags")
async def suggest_hashtags(request: Request, db: Session = Depends(get_db)):
    """Suggest commonly-used Nostr hashtags for a draft post via the node's LLM — powers the composer's
    AI → Hashtags menu item. Returns a single space-separated string of #tags for the client to append
    after a blank line. Follows Nostr convention: #asknostr for questions, #memestr for image posts.
    Same-origin helper; degrades to convention-only tags where there's no LLM (e.g. a Nostr-only node)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    text = (body.get("text") or "").strip()
    has_image = bool(body.get("has_image"))
    if not text and not has_image:
        return JSONResponse({"error": "nothing to tag"}, status_code=400)
    low = text.lower()
    is_question = text.endswith("?") or bool(re.match(
        r"^(who|what|when|where|why|how|is|are|can|should|does|do|could|would|will|any|anyone)\b", low))
    tags: list[str] = []

    def _add(t: str):
        t = t.strip().lower()
        if not t.startswith("#"):
            t = "#" + t
        if re.fullmatch(r"#\w{2,30}", t) and t not in tags:
            tags.append(t)

    if text:
        try:
            from app.services.inference_factory import get_inference_service
            svc = get_inference_service(db)
            res = await svc.chat_completion(
                [{"role": "system", "content": (
                    "You suggest hashtags for a Nostr social post. Output 3 to 6 SHORT, commonly-used "
                    "hashtags that genuinely fit the content, space-separated, each starting with '#', "
                    "lowercase, no explanation and no punctuation other than '#'. Prefer popular Nostr "
                    "tags when relevant (e.g. #nostr #bitcoin #plebchain #grownostr #art #photography "
                    "#foodstr #coffeechain #zap). Output ONLY the hashtags on one line.")},
                 {"role": "user", "content": text[:2000]}],
                max_tokens=60, temperature=0.4)
            out = (res.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            for t in re.findall(r"#\w+", out):
                _add(t)
        except Exception as e:
            logger.warning(f"[client] hashtags LLM failed (convention-only): {e}")

    # Nostr convention additions (deduped): questions → #asknostr (first), images → #memestr.
    if is_question:
        if "#asknostr" in tags:
            tags.remove("#asknostr")
        tags.insert(0, "#asknostr")
    if has_image:
        _add("#memestr")
    if not tags:
        _add("#nostr")
    tags = tags[:8]
    return JSONResponse({"hashtags": " ".join(tags)})


@router.get("/server-stats")
async def client_server_stats():
    """Public server statistics for the Server Stats page. No auth: these are aggregate counts only.

    NOT /client/stats — that name was already taken by the sidebar ticker above (users/online/relay).
    FastAPI keeps the FIRST route registered for a path, so reusing it meant this handler never ran
    and the page silently received the ticker payload instead.

    The payload is computed at most once per minute for the whole instance (see stats_service), so
    hitting this endpoint in a loop costs a dictionary lookup, not a query.
    """
    from app.services import stats_service
    try:
        return JSONResponse(await stats_service.get_stats())
    except Exception as e:
        logger.warning("[client] stats failed: %s", e)
        return JSONResponse({"error": "unavailable"}, status_code=503)


@router.get("/uptime")
async def client_uptime():
    """Public uptime-monitor status for the Server Stats page's Uptime tab.

    Public on purpose — it is a status page, and the endpoints on it are ones an admin chose to
    publish. The checks themselves run in the worker process; this only reads the state doc the
    worker writes to the relay, cached server-side, so polling it is a dictionary lookup.
    """
    from app.services import uptime_service
    try:
        return JSONResponse(await uptime_service.get_status())
    except Exception as e:
        logger.warning("[client] uptime failed: %s", e)
        return JSONResponse({"error": "unavailable"}, status_code=503)


@router.get("/commands")
async def client_commands():
    """The command catalogue for the client's help sheet — grouped, with descriptions.

    `help` used to answer with all 109 commands as one 8,900-character wall of markdown in the chat,
    which reads as intimidating rather than helpful. The client renders this as a searchable sheet
    instead (same shape as the Effects studio). Grouping lives HERE so the sheet can't drift from what
    CommandService actually dispatches: anything not named below still shows up under "More", so a new
    command is never invisible just because nobody updated a list.
    """
    from app.services.command_service import CommandService as CS
    cmds = getattr(CS, "COMMANDS", {}) or {}
    effects = set(getattr(CS, "MOTION_EFFECTS", ()) or ())

    groups = [
        ("✨ Create", "geni musicgeni videogeni narrate poll"),
        ("🔍 Find", "search images yt news dailynews files torrents nyaa"),
        ("🖼 Files & media", "compress clip convert extractaudio circlecrop removebackground ocr collage ytdl screenshot"),
        ("📚 Learn", "flashcards translate"),
        ("💰 Money", "bill"),   # the budget itself is client-side + encrypted (Discover → Budget)
        ("⏰ Keep track", "remind reminders pin pins mail"),
        ("📣 Share", "post"),
        ("⚙️ System", "logs node help"),
    ]

    def entry(name):
        d = cmds.get(name, "")
        if isinstance(d, dict):
            d = d.get("description") or d.get("help") or ""
        return {"name": name, "desc": str(d or "").strip()}

    out, seen = [], set()
    for title, names in groups:
        items = [entry(n) for n in names.split() if n in cmds]
        for n in names.split():
            seen.add(n)
        if items:
            out.append({"title": title, "items": items})
    # Anything not categorised (and not an effect — those have their own studio) still appears.
    rest = [entry(n) for n in sorted(cmds) if n not in seen and n not in effects]
    if rest:
        out.append({"title": "🧩 More", "items": rest})
    return JSONResponse({"groups": out, "effects_count": len(effects)})


@router.get("/effects")
async def client_effects():
    """The image/video effects available to the Nostr client's Effects studio (names + descriptions,
    straight from CommandService so the studio never drifts from what the bot/Telegram support)."""
    from app.services.command_service import CommandService as CS
    motion = list(getattr(CS, "MOTION_EFFECTS", ()) or ())
    cmds = getattr(CS, "COMMANDS", {}) or {}

    def desc(n):
        v = cmds.get(n, "")
        if isinstance(v, dict):
            v = v.get("description") or v.get("help") or ""
        return str(v or "").strip()[:90]

    enhance = [n for n in ("glow", "alive") if n in motion]
    effects = [n for n in motion if n not in enhance]
    if "removebackground" in cmds:
        effects.append("removebackground")
    # stickers = the `char <name>` overlay characters (assets/characters/*), so the studio's
    # optional sticker step stays in sync with what the effect engine actually has.
    chars = []
    try:
        cdir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "assets", "characters")
        for fn in sorted(os.listdir(cdir)):
            n = os.path.splitext(fn)[0].strip()
            if n and n not in chars:
                chars.append(n)
    except Exception:
        chars = ["animegirl", "boobs", "cow", "panties", "pepe", "trump"]
    return JSONResponse({
        "enhance": [{"name": n, "desc": desc(n)} for n in enhance],
        "effects": [{"name": n, "desc": desc(n)} for n in effects],
        "motions": ["zoom", "shake", "medshake", "beginshake", "pulse", "glow", "alive", "trippy"],
        "chars": chars,
        # Combination rules, straight from CommandService.check_motion_combo, so the studio
        # can only build commands the renderer will actually accept (one movement; looks
        # stack; no movement on an already-animated effect).
        "rules": {
            "movement": list(CS.MOVEMENT_MOTIONS),
            "looks": list(CS.LOOK_MOTIONS),
            "animated": sorted(CS.ANIMATED_EFFECTS),
            "stillOnly": list(CS.STILL_ONLY_MOTIONS),
        },
    })


@router.get("/meme/sounds")
async def meme_sounds():
    """Sound effects selectable per LAYER in the Meme Builder — the same catalogue the AI chat uses
    (curb, fahh, sopranos…), filtered to the ones whose audio file actually exists on this node."""
    from app.services import meme_builder_service as mb
    names = [n for n in mb.sound_names() if mb._sound_path(n)]
    return JSONResponse({"sounds": names})


@router.get("/meme/effects")
async def meme_effects():
    """Full effects that can be added to a build as a TRANSPARENT overlay LAYER (nakedman, shrug, and
    the character overlays) — the picker's list, filtered to the ones whose assets actually resolve on
    this node. Mirrors /meme/sounds. Each entry: {name, label, audio}."""
    from app.services import meme_builder_service as mb
    return JSONResponse({"effects": mb.alpha_effect_catalog()})


class MemeEffectReq(BaseModel):
    pubkey: str
    auth: str                    # base64 signed kind-27235 by this pubkey — same self-proof as /meme/render
    name: str                    # one of alpha_effect_catalog() names
    dur: float | None = None     # optional length hint (seconds)


_effect_cooldown: dict = {}      # pubkey -> monotonic ts of last effect render (in-memory; single port-3051 worker)
_EFFECT_COOLDOWN_S = 4.0         # per-user minimum gap between effect renders (each is a heavy ProRes encode)


# Render a full effect onto a TRANSPARENT canvas and store it to Blossom, so the client can add it as an
# ordinary video layer (its URL is then fetched by /meme/render exactly like any other layer source). We
# STORE rather than stream-back (unlike /meme/render) because the client needs a stable URL to hang on
# the timeline. The bytes are ProRes 4444 .mov (alpha preserved — see media_service) and public, which is
# fine: this is a generated cartoon, not private user content, and it must be fetchable by the later
# render pass (whose SSRF guard already exempts our own blossom_public_url host).
@router.post("/meme/effect")
async def meme_effect(data: MemeEffectReq, request: Request, db: Session = Depends(get_db)):
    from app.services import meme_builder_service as mb
    from app.services import blossom_service

    pk = nostr_service.to_pubkey_hex(data.pubkey or "")
    if not pk or not _verify_self_auth(data.auth, pk):
        raise HTTPException(status_code=401, detail="bad auth")
    # A forwarded job renders here and hands the BYTES back — the node that took the user's request
    # stores them. So a render peer needs ffmpeg, not a media store: `blossom_enabled` is per-node and
    # is off on a node that only holds bytes for someone else's Blossom (nas), which used to 503 every
    # forwarded effect straight back to local. See _meme_lb_forward.
    _fwded = bool(request is not None and request.headers.get("x-pcai-meme-fwd"))
    if not _fwded and not blossom_service.is_enabled(db):
        raise HTTPException(status_code=503, detail="media storage (Blossom) is disabled on this node")

    name = (data.name or "").strip().lower()
    catalog = {e["name"] for e in mb.alpha_effect_catalog()}
    if name not in catalog:
        raise HTTPException(status_code=400, detail="unknown effect")

    # Per-user cooldown: each render is a heavy ProRes encode and the shared render queue is a single
    # worker, so one impatient user shouldn't be able to fill it (the semaphore below still bounds total
    # concurrency; this just adds per-user fairness). Cheap in-memory gate, like the node-job registry.
    _now = time.monotonic()
    if _now - _effect_cooldown.get(pk, 0.0) < _EFFECT_COOLDOWN_S:
        raise HTTPException(status_code=429, detail="one effect at a time — give the last render a moment")
    _effect_cooldown[pk] = _now

    # Busy-overflow LB: if this node's render queue is full, run it on a peer instead.
    _fwd = await _meme_lb_forward(request, "effect",
                                  {"pubkey": data.pubkey, "auth": data.auth, "name": data.name, "dur": data.dur},
                                  db=db)
    if _fwd is not None:
        return _fwd

    # Cap the length the same spirit as the Meme Builder's own bounds — an effect layer is a short clip on
    # the shared GPU/CPU box, not a film.
    dur = None
    if data.dur is not None:
        try:
            dur = max(0.5, min(float(data.dur), 30.0))
        except (TypeError, ValueError):
            dur = None

    # ffmpeg is blocking → a thread, and share the Meme Builder's render queue so effect renders can't
    # stack N ffmpegs alongside timeline renders and starve the single worker.
    async with _meme_slot():
        try:
            clip, has_audio = await asyncio.to_thread(mb.render_alpha_effect, name, dur)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            logger.warning("[meme] effect render failed (%s) for %s: %s", name, pk[:12], e)
            raise HTTPException(status_code=500, detail=str(e))

    # A sensible default LENGTH for the timeline layer so the whole clip plays (nakedman is ~8s, the
    # shrug clip ~2.7s, a static character 6s). The user trims from there like any layer.
    nominal = dur or (8.0 if name == "nakedman" else 2.7 if name == "shrug" else 6.0)
    if _fwded:
        # Rendered on behalf of another node: hand back the raw clip + the metadata it can't recompute.
        # That node stores it and builds the URL, so this one needs no blob store of its own.
        return Response(content=clip, media_type="video/webm", headers={
            "x-pcai-effect-audio": "1" if has_audio else "0",
            "x-pcai-effect-name": name,
            "x-pcai-effect-dur": str(round(nominal, 2)),
        })

    # Store under the acting user's pubkey (their generated content). save_blob is content-addressed and
    # dedups, so re-rolling the same effect twice costs nothing. VP9-alpha .webm: plays transparently in
    # the browser preview AND composites in the render (decoded with libvpx-vp9 — see media_service).
    desc = await blossom_service.save_blob(db, pk, clip, "video/webm")
    url = f"{_blossom_url(request, db)}/{desc['sha256']}.webm"
    # The clip is SILENT (audio would corrupt VP9 alpha). If the effect has a sound, hand back its name so
    # the client sets the layer's `sound` field — the render mixes it via the existing per-layer sound path.
    return JSONResponse({"ok": True, "url": url, "audio": bool(has_audio),
                         "sound": name if has_audio else None,
                         "name": name, "dur": round(nominal, 2)})


class MemeApplyEffectReq(BaseModel):
    pubkey: str
    auth: str                    # base64 signed kind-27235 by this pubkey — same self-proof as /meme/effect
    url: str                     # the layer's source IMAGE url to transform
    effect: str                  # a MOTION_EFFECTS / ANIMATED_EFFECTS name (glow, alive, nakedman, meme, …)
    arg: str | None = None       # optional effect argument (caption text, motion modifier, …)


@router.post("/meme/apply-effect")
async def meme_apply_effect(data: MemeApplyEffectReq, request: Request, db: Session = Depends(get_db)):
    """Apply a FULL effect to a Meme Builder layer's IMAGE — the SAME engine as the Effects studio and
    Telegram (glow, alive, nakedman, meme, sopranos, diarrhea, …), so the Meme Builder never drifts from
    what the rest of the app supports. Returns the resulting video URL; the client swaps the layer's
    source to it. RAW effect output — no PosterChan branding end-card (that belongs on the finished meme,
    not a sub-layer)."""
    from app.services import blossom_service
    from app.services.command_service import CommandService

    pk = nostr_service.to_pubkey_hex(data.pubkey or "")
    if not pk or not _verify_self_auth(data.auth, pk):
        raise HTTPException(status_code=401, detail="bad auth")
    # Forwarded job → return bytes, let the requesting node store them (see meme_effect).
    _fwded = bool(request is not None and request.headers.get("x-pcai-meme-fwd"))
    if not _fwded and not blossom_service.is_enabled(db):
        raise HTTPException(status_code=503, detail="media storage (Blossom) is disabled on this node")

    effect = (data.effect or "").strip().lower()
    allowed = set(CommandService.MOTION_EFFECTS) | set(CommandService.ANIMATED_EFFECTS)
    if effect not in allowed:
        raise HTTPException(status_code=400, detail="unknown effect")

    _now = time.monotonic()
    if _now - _effect_cooldown.get(pk, 0.0) < _EFFECT_COOLDOWN_S:
        raise HTTPException(status_code=429, detail="one effect at a time — give the last render a moment")
    _effect_cooldown[pk] = _now

    # Busy-overflow LB: full local queue → run this effect on a peer node instead.
    _fwd = await _meme_lb_forward(request, "apply-effect",
                                  {"pubkey": data.pubkey, "auth": data.auth, "url": data.url,
                                   "effect": data.effect, "arg": data.arg},
                                  db=db)
    if _fwd is not None:
        return _fwd

    # Fetch the layer image. OUR OWN media hosts (blossom_public_url) resolve to a PRIVATE LAN IP under
    # split-horizon DNS (media.poster.place -> 192.168.0.1 inside the LAN), so the SSRF guard rejects them
    # as private — which would refuse every Blossom blob the user just uploaded. Exempt them exactly like
    # /meme/render does (these are URLs this node itself mints + serves, not an SSRF primitive); everything
    # else still goes through the guard.
    import httpx
    from urllib.parse import urlparse
    from app.services.rss_service import looks_fetchable, is_safe_host
    u = (data.url or "").strip()
    if urlparse(u).scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="bad image url")
    own = set()
    for key in ("blossom_public_url", "nostr_dvm_blossom_url"):
        h = urlparse(_setting(db, key) or "").hostname
        if h:
            own.add(h.lower())
    host = (urlparse(u).hostname or "").lower()
    if host not in own:
        if not looks_fetchable(u) or not await asyncio.to_thread(is_safe_host, u):
            raise HTTPException(status_code=400, detail="refused image source")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0), follow_redirects=False) as c:
            resp = await c.get(u)
            resp.raise_for_status()
            img = resp.content
            ct = resp.headers.get("content-type", "") or "image/jpeg"
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="could not fetch the layer image")
    if not img:
        raise HTTPException(status_code=400, detail="empty image")
    if len(img) > 80 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image too large (80 MB limit)")
    ext = "png" if "png" in (ct or "") else ("gif" if "gif" in (ct or "") else "jpg")
    attachments = [(f"layer.{ext}", img, ct or "image/jpeg")]

    async with _meme_slot():
        try:
            cs = CommandService(db)
            # _execute_command_inner is the RAW effect (execute_command would append the branding outro, which
            # we don't want on a compositing sub-layer). Allowlisted to effects above → only runs effects.
            result = await cs._execute_command_inner(effect, (data.arg or "").strip(), None, None, attachments, None)
        except Exception as e:
            logger.warning("[meme] apply-effect %s failed for %s: %s", effect, pk[:12], e)
            raise HTTPException(status_code=500, detail="effect render failed")

    files = (result or {}).get("files") if isinstance(result, dict) else None
    out = None
    for f in (files or []):
        if isinstance(f, dict) and f.get("data") and str(f.get("content_type") or "").startswith("video/"):
            out = f
            break
    if not out:   # some effects (e.g. removebackground) hand back an image — fall back to the first file
        out = next((f for f in (files or []) if isinstance(f, dict) and f.get("data")), None)
    if not out:
        raise HTTPException(status_code=500, detail="effect produced no output")

    ct_out = str(out.get("content_type") or "video/mp4")
    ext_out = "mp4" if "mp4" in ct_out else ((ct_out.split("/")[-1] or "mp4").split(";")[0])

    dur = 0.0
    try:
        import subprocess as _sp
        import tempfile as _tf
        with _tf.NamedTemporaryFile(suffix="." + ext_out, delete=False) as tfh:
            tfh.write(out["data"])
            tp = tfh.name
        p = _sp.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", tp], capture_output=True, timeout=20)
        dur = float((p.stdout or b"0").strip() or 0)
        os.unlink(tp)
    except Exception:
        dur = 0.0

    if _fwded:
        return Response(content=out["data"], media_type=ct_out, headers={
            "x-pcai-effect-name": effect,
            "x-pcai-effect-dur": str(round(dur, 2)),
        })

    desc = await blossom_service.save_blob(db, pk, out["data"], ct_out)
    url = f"{_blossom_url(request, db)}/{desc['sha256']}.{ext_out}"
    return JSONResponse({"ok": True, "url": url, "dur": round(dur, 2), "effect": effect,
                         "is_video": ct_out.startswith("video/")})


@router.get("/proxy-image")
async def client_proxy_image(url: str = Query(...)):
    """Same-origin image proxy for the Nostr web client (e.g. the Effects studio grabbing a post's
    image to apply an effect — the browser can't fetch a cross-origin Blossom blob as bytes). Reuses
    the SSRF-guarded fetch; returns image bytes only."""
    from app.routers.chat import _proxy_fetch
    try:
        content, media_type = await _proxy_fetch(url)
        return Response(content=content, media_type=media_type)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="failed to fetch image")


@router.get("/gif")
async def gif_search(q: str = "", db: Session = Depends(get_db)):
    """GIF picker — proxies Giphy or Tenor (key server-side, never exposed). Giphy wins if both set."""
    giphy = _setting(db, "giphy_api_key")
    tenor = _setting(db, "tenor_api_key")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            if giphy:
                base = "https://api.giphy.com/v1/gifs/" + ("search" if q else "trending")
                params = {"api_key": giphy, "limit": "24", "rating": "pg-13"}
                if q:
                    params["q"] = q
                j = (await client.get(base, params=params)).json()
                out = []
                for g in j.get("data", []):
                    im = g.get("images", {})
                    full = (im.get("fixed_height") or {}).get("url")
                    prev = (im.get("fixed_height_small") or im.get("fixed_height") or {}).get("url")
                    if full:
                        out.append({"url": full, "preview": prev or full})
                return JSONResponse({"results": out})
            if tenor:
                base = "https://tenor.googleapis.com/v2/" + ("search" if q else "featured")
                params = {"key": tenor, "limit": "24", "media_filter": "tinygif,gif", "client_key": "posterchan"}
                if q:
                    params["q"] = q
                j = (await client.get(base, params=params)).json()
                out = []
                for r in j.get("results", []):
                    mf = r.get("media_formats", {})
                    full = mf.get("gif") or mf.get("tinygif") or {}
                    tiny = mf.get("tinygif") or full
                    if full.get("url"):
                        out.append({"url": full["url"], "preview": tiny.get("url", full["url"])})
                return JSONResponse({"results": out})
    except Exception as e:
        logger.warning("[client] gif search failed: %s", e)
        return JSONResponse({"results": [], "error": "fetch_failed"})
    return JSONResponse({"results": [], "error": "no_key"})


@router.get("/manifest.json")
async def client_manifest(request: Request, db: Session = Depends(get_db)):
    name = _setting(db, "site_name", "PosterChan")
    return JSONResponse({
        "name": f"{name} Nostr",
        "short_name": name,
        # Stable install identity (defaults to start_url otherwise). Helps Samsung Internet / Chrome
        # match an existing install instead of failing the WebAPK mint.
        "id": "/client",
        "start_url": "/client",
        "scope": "/client",
        "display": "standalone",
        "background_color": "#08060f",
        "theme_color": "#0b0118",
        "description": "Cyberpunk Nostr client",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            # Maskable variants (logo padded into the safe zone). Samsung Internet's WebAPK mint
            # commonly fails ("failed to download") without a maskable icon present.
            {"src": "/static/icon-192-maskable.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icon-512-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        # Long-press the home-screen icon → jump straight to a view / the composer. The SPA reads
        # these query params on boot (see _consumeLaunchParams in app.js) and cleans the URL.
        "shortcuts": [
            {"name": "New post", "short_name": "Post", "url": "/client?compose=1",
             "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
            {"name": "Notifications", "short_name": "Notifs", "url": "/client?view=notifications",
             "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
            {"name": "Messages", "short_name": "Messages", "url": "/client?view=messages",
             "icons": [{"src": "/static/icon-192.png", "sizes": "192x192"}]},
        ],
        # Web Share Target (POST, multipart): share a file/image/video AND/OR text/link from any app →
        # opens the composer with the text pre-filled and the file(s) uploaded + attached. The POST is
        # intercepted by the service worker (handleShare → stashes the file in a cache, redirects to
        # /client?shared=1), which the SPA drains on boot (_consumeSharedFiles). POST is required to carry
        # files; text-only shares still work through the same path.
        "share_target": {
            "action": "/client/share",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [{"name": "media", "accept": ["image/*", "video/*", "audio/*", "application/pdf", "text/plain"]}],
            },
        },
    }, media_type="application/manifest+json")   # not application/json — Samsung Internet's WebAPK install rejects the wrong MIME ("failed to download")


@router.post("/share")
async def client_share(request: Request):
    """Fallback for the Web Share Target POST when the service worker isn't yet active to intercept it
    (very first load before the SW installs, or a stale SW). Normally handleShare in the SW handles the
    whole thing client-side, files included. Here we can only forward the TEXT to the SPA via the query
    string (a file needs the SW's cache stash), then redirect to the composer so a text/link share still
    works and never errors."""
    from urllib.parse import urlencode
    try:
        form = await request.form()
        params = {k: (form.get(k) or "") for k in ("title", "text", "url") if form.get(k)}
    except Exception:
        params = {}
    qs = ("?" + urlencode(params)) if params else "?shared=1"
    return RedirectResponse(url="/client" + qs, status_code=303)


@router.get("/sw.js")
async def client_sw():
    # Served from the app (not /static) so the service-worker scope can be /client.
    return FileResponse(os.path.join(_STATIC, "js", "client", "sw.js"), media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/client", "Cache-Control": "no-cache"})


_preview_cache: dict[str, tuple[float, dict]] = {}   # url -> (expires, data)
_PREVIEW_TTL = 3600.0


def _is_public_host(host: str) -> bool:
    """SSRF guard: reject hosts that resolve to private/loopback/link-local addresses."""
    import socket
    import ipaddress
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


@router.get("/preview")
async def link_preview(url: str):
    """Fetch OpenGraph/Twitter-card metadata for a URL so the client can show a link card.
    Public endpoint, so it guards against SSRF (no private IPs), caps size, and caches briefly."""
    import re
    from urllib.parse import urlparse, urljoin
    if not url.startswith(("http://", "https://")):
        return JSONResponse({}, status_code=400)
    now = time.time()
    hit = _preview_cache.get(url)
    if hit and hit[0] > now:
        return JSONResponse(hit[1])
    host = urlparse(url).hostname or ""
    if not host or not _is_public_host(host):
        return JSONResponse({})
    data = {}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=4.0), follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; PosterChanBot/1.0)"}) as client:
            async with client.stream("GET", url) as resp:
                ctype = resp.headers.get("content-type", "")
                if resp.status_code == 200 and "text/html" in ctype:
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > 524288:   # 512 KB cap — OG tags live in <head>
                            break
                    html = body.decode("utf-8", "ignore")

                    def meta(*names):
                        for n in names:
                            m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(n)
                                          + r'["\'][^>]+content=["\']([^"\']+)["\']', html, re.I) \
                                or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']'
                                             + re.escape(n) + r'["\']', html, re.I)
                            if m:
                                return m.group(1).strip()
                        return None

                    title = meta("og:title", "twitter:title")
                    if not title:
                        tm = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
                        title = tm.group(1).strip() if tm else None
                    img = meta("og:image", "twitter:image", "twitter:image:src")
                    if img:
                        img = urljoin(url, img)
                    data = {"url": url, "title": title, "description": meta("og:description", "twitter:description", "description"),
                            "image": img, "site": meta("og:site_name") or host}
    except Exception as e:
        logger.debug("[client] preview fetch failed for %s: %s", url, e)
    # cache (even negatives, briefly) to avoid refetch storms
    _preview_cache[url] = (now + _PREVIEW_TTL, data)
    if len(_preview_cache) > 2000:
        _preview_cache.clear()
    return JSONResponse(data)


# ---------- NIP-34 git repo README bridge ----------
# Discover → Git Repos lists kind-30617 repo announcements. Until a native Nostr git host exists,
# the README is fetched from the repo's existing forge (Gitea/Forgejo/GitHub/GitLab) given its
# clone/web URL. Best-effort over many forges; SSRF-guarded; size-capped.
_readme_cache: dict[str, tuple[float, dict]] = {}   # url -> (expires, {ok, markdown, source})
_README_TTL = 600.0
_README_MAX = 524288   # 512 KB cap on the fetched body
_git_creds_cache: dict[str, object] = {"t": 0.0, "map": {}}


def _git_host_creds() -> dict:
    """HTTP basic-auth creds for the node's OWN git host, parsed from this checkout's git remotes
    (e.g. an `origin` like https://user:token@git.example.com/...). Used ONLY to read a README from
    that exact host when it is sign-in-gated — the creds are never attached to any other host, so
    this can't leak them via SSRF. Cached for 5 min."""
    now = time.time()
    cached = _git_creds_cache.get("map") or {}
    if cached and now - float(_git_creds_cache.get("t") or 0) < 300:
        return cached   # type: ignore[return-value]
    import subprocess
    from urllib.parse import urlparse, unquote
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    out: dict = {}
    try:
        res = subprocess.run(["git", "-C", root, "remote", "-v"],
                             capture_output=True, text=True, timeout=5)
        for line in (res.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            p = urlparse(parts[1])
            if p.scheme in ("http", "https") and p.username and p.password and p.hostname:
                out[p.hostname.lower()] = (unquote(p.username), unquote(p.password))
    except Exception as e:
        logger.debug("[client] git remote creds parse failed: %s", e)
    _git_creds_cache["t"] = now
    if out:
        _git_creds_cache["map"] = out
    return out


def _readme_candidates(url: str) -> list:
    """Derive candidate raw-README URLs from a clone/web URL across common forges. Returns a list of
    (url, kind) where kind is 'raw' (plain markdown body) or 'api' (JSON with base64 `content`)."""
    from urllib.parse import urlparse
    url = (url or "").strip()
    if not url:
        return []
    web = url[:-4] if url.endswith(".git") else url    # strip trailing .git → web base
    web = web.rstrip("/")
    p = urlparse(web)
    host = (p.hostname or "").lower()
    scheme = p.scheme or "https"
    origin = f"{scheme}://{p.netloc}"
    segs = [s for s in p.path.split("/") if s]
    owner = segs[0] if len(segs) >= 1 else ""
    repo = segs[1] if len(segs) >= 2 else ""
    branches = ["main", "master"]
    names = ["README.md", "readme.md", "README", "readme"]
    cands: list = []
    if owner and repo:
        if host == "github.com":
            cands.append((f"https://api.github.com/repos/{owner}/{repo}/readme", "api"))
        else:
            # Gitea/Forgejo readme API (base64) — best-effort; not on every version.
            cands.append((f"{origin}/api/v1/repos/{owner}/{repo}/readme", "api"))
    if host == "github.com" and owner and repo:
        for b in branches:
            for n in names:
                cands.append((f"https://raw.githubusercontent.com/{owner}/{repo}/{b}/{n}", "raw"))
    if "gitlab" in host and owner and repo:
        for b in branches:
            for n in names:
                cands.append((f"{web}/-/raw/{b}/{n}", "raw"))
    # Gitea/Forgejo raw (both path shapes) + a generic <web>/raw/<b>/<name>.
    for b in branches:
        for n in names:
            cands.append((f"{web}/raw/branch/{b}/{n}", "raw"))
    for b in branches:
        for n in names:
            cands.append((f"{web}/raw/{b}/{n}", "raw"))
    seen: set = set()
    out: list = []
    for c in cands:
        if c[0] in seen:
            continue
        seen.add(c[0])
        out.append(c)
    return out


def _looks_like_html_page(text: str) -> bool:
    """A forge that requires sign-in serves an HTML login page (200) instead of the raw file — reject
    it so a login page never masquerades as a README. Real markdown may contain inline HTML, but a
    full document starts with a doctype / <html>."""
    head = (text or "").lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


async def _grasp_readme(clone_url: str) -> str | None:
    """If clone_url is a Nostr/GRASP repo (…/<npub|hex>/<id>.git), read the README straight from THIS
    node's git host raw endpoint (<npub>/<id>.git/raw/HEAD/README.md) — our /git/ is smart-HTTP (pack)
    only, so the forge-URL candidates never match it. We reach the git host we PROXY to (git_server_proxy_url,
    e.g. nas.lan:3053) or, on a hosting node, localhost:git_server_port. Scoped to npub/hex owners so it
    never hijacks a normal forge (GitHub/Gitea owners are usernames); a repo we don't host just 404s and
    the caller falls through to the forge candidates. Our own LAN/localhost service — no SSRF surface."""
    from urllib.parse import urlparse
    import re as _re
    try:
        pu = urlparse(clone_url)
        segs = [s for s in pu.path.split("/") if s]
        gi = next((i for i, s in enumerate(segs) if s.endswith(".git")), None)
        if gi is None or gi < 1:
            return None
        owner_seg, rid = segs[gi - 1], segs[gi][:-4]
        if not (owner_seg.startswith("npub1") or _re.fullmatch(r"[0-9a-fA-F]{64}", owner_seg)):
            return None   # not a Nostr git owner → leave it to the forge candidates
        proxy = (settings_store.get("git_server_proxy_url", "") or "").strip().rstrip("/")
        host_base = proxy or ("http://127.0.0.1:%s" % (settings_store.get("git_server_port", "3053") or "3053"))
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as c:
            for name in ("README.md", "readme.md", "README", "readme", "README.markdown", "Readme.md"):
                try:
                    r = await c.get("%s/%s/%s.git/raw/HEAD/%s" % (host_base, owner_seg, rid, name))
                    if r.status_code == 200 and r.content:
                        return r.content[:_README_MAX].decode("utf-8", "ignore")
                except Exception:
                    continue
    except Exception:
        return None
    return None


@router.get("/git/readme")
async def git_readme(url: str):
    """Fetch a repo's README markdown from its forge given a clone/web URL — powers Discover → Git
    Repos' repo-detail view. Public helper (mirrors /preview): SSRF-guarded, size-capped, best-effort
    across Gitea/Forgejo/GitHub/GitLab, AND our own self-hosted GRASP host. Returns {ok, markdown, source}."""
    from urllib.parse import urlparse
    from app.services import rss_service
    if not url or not url.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "bad url"}, status_code=400)
    now = time.time()
    hit = _readme_cache.get(url)
    if hit and hit[0] > now:
        return JSONResponse(hit[1])
    # Our own GRASP host first (smart-HTTP only → forge candidates below can't read it).
    _g = await _grasp_readme(url)
    if _g is not None:
        out = {"ok": True, "markdown": _g, "source": "grasp"}
        _readme_cache[url] = (now + 300, out)
        return JSONResponse(out)
    creds = _git_host_creds()
    result = {"ok": False, "error": "no README found"}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=False,
                                     headers={"User-Agent": "PosterChanBot/1.0",
                                              "Accept": "application/vnd.github.raw, text/plain, */*"}) as client:
            for cand_url, kind in _readme_candidates(url):
                if not rss_service.looks_fetchable(cand_url):
                    continue
                host = (urlparse(cand_url).hostname or "").lower()
                # The node's OWN git forge is deliberately self-hosted (often on the LAN), so it fails
                # the resolve-based private-IP guard. Trust ONLY the exact host(s) we hold git creds for
                # (mirrors search_service's trusted_domains); every other host keeps the strict SSRF check.
                trusted = host in creds
                if not trusted and not await asyncio.to_thread(rss_service.is_safe_host, cand_url):
                    continue
                auth = httpx.BasicAuth(creds[host][0], creds[host][1]) if trusted else None
                try:
                    async with client.stream("GET", cand_url, auth=auth) as resp:
                        if resp.status_code != 200:
                            continue
                        body = b""
                        async for chunk in resp.aiter_bytes():
                            body += chunk
                            if len(body) > _README_MAX:
                                break
                    text = body.decode("utf-8", "ignore")
                except Exception:
                    continue
                if kind == "api":
                    try:
                        obj = json.loads(text)
                        enc = (obj.get("encoding") or "").lower()
                        content = obj.get("content") or ""
                        md = base64.b64decode(content).decode("utf-8", "ignore") if enc == "base64" else content
                    except Exception:
                        continue
                else:
                    md = text
                if not md or not md.strip() or _looks_like_html_page(md):
                    continue
                result = {"ok": True, "markdown": md[:_README_MAX], "source": cand_url}
                break
    except Exception as e:
        logger.debug("[client] git readme fetch failed for %s: %s", url, e)
        result = {"ok": False, "error": "fetch failed"}
    _readme_cache[url] = (now + _README_TTL, result)
    if len(_readme_cache) > 500:
        _readme_cache.clear()
    return JSONResponse(result)


def _grasp_host_target(clone_url: str):
    """(host_base, owner_seg, repo_id) for a Nostr/GRASP clone url, else None. host_base is the git host
    this node reaches — the peer we proxy to (git_server_proxy_url) or localhost on a hosting node."""
    from urllib.parse import urlparse
    import re as _re
    pu = urlparse(clone_url or "")
    segs = [s for s in pu.path.split("/") if s]
    gi = next((i for i, s in enumerate(segs) if s.endswith(".git")), None)
    if gi is None or gi < 1:
        return None
    owner_seg, rid = segs[gi - 1], segs[gi][:-4]
    if not (owner_seg.startswith("npub1") or _re.fullmatch(r"[0-9a-fA-F]{64}", owner_seg)):
        return None
    proxy = (settings_store.get("git_server_proxy_url", "") or "").strip().rstrip("/")
    host_base = proxy or ("http://127.0.0.1:%s" % (settings_store.get("git_server_port", "3053") or "3053"))
    return host_base, owner_seg, rid


def _grasp_url(clone_url: str, route: str, path: str = "", *, ref: str = "HEAD", extra: str = ""):
    """Build the git host URL for one browse route, or None if this isn't a self-hosted repo / the path
    is unsafe. `ref` always rides as `?ref=` (a branch name may contain slashes, which the path segment
    after the route can't express) while the path segment stays HEAD."""
    from urllib.parse import quote
    tgt = _grasp_host_target(clone_url)
    if not tgt:
        return None
    host_base, owner_seg, rid = tgt
    path = (path or "").strip("/")
    if ".." in path.split("/"):
        return None
    q = "ref=%s" % quote(ref or "HEAD", safe="")
    if extra:
        q += "&" + extra
    return "%s/%s/%s.git/%s/HEAD%s?%s" % (host_base, owner_seg, rid, route,
                                          ("/" + quote(path)) if path else "", q)


async def _grasp_json(u: str, timeout: float = 10.0):
    """GET one of the git host's JSON browse routes -> (payload, error_response). The three read
    endpoints below differ only in URL and timeout, so the fetch/erroring lives here once."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=4.0)) as c:
            r = await c.get(u)
            if r.status_code != 200:
                return None, JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            return r.json(), None
    except Exception:
        return None, JSONResponse({"ok": False, "error": "read failed"}, status_code=502)


@router.get("/git/tree")
async def git_tree(url: str, path: str = "", ref: str = "HEAD"):
    """List a directory in a self-hosted GRASP repo (the Files browser). Proxies the git host's tree route
    (git ls-tree). Only Nostr-owned (npub/hex) repos we host/proxy — everything else 400s."""
    u = _grasp_url(url, "tree", path, ref=ref)
    if not u:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    data, err = await _grasp_json(u, 10.0)
    return err or JSONResponse({"ok": True, **data})


@router.get("/git/log")
async def git_log(url: str, path: str = "", ref: str = "HEAD", limit: int = 50):
    """Commit history for a self-hosted GRASP repo (the Commits view + a file's history). Proxies the
    git host's log route. Only Nostr-owned (npub/hex) repos we host/proxy — everything else 400s."""
    u = _grasp_url(url, "log", path, ref=ref,
                   extra="limit=%d" % max(1, min(int(limit or 50), 200)))
    if not u:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    data, err = await _grasp_json(u, 20.0)
    return err or JSONResponse({"ok": True, **data})


@router.get("/git/refs")
async def git_refs(url: str):
    """Branches + tags of a self-hosted GRASP repo — what the repo view's ref switcher is built from."""
    tgt = _grasp_host_target(url)
    if not tgt:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    data, err = await _grasp_json("%s/%s/%s.git/refs" % tgt, 10.0)
    return err or JSONResponse({"ok": True, **data})


@router.get("/git/commit")
async def git_commit(url: str, sha: str):
    """One commit with its per-file stats and patch — "what changed in this commit". The host bounds
    the patch size and flags `truncated`, so a giant commit can't be used to pull an unbounded body."""
    import re as _re
    tgt = _grasp_host_target(url)
    if not tgt:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    if not _re.fullmatch(r"[0-9a-fA-F]{7,40}", (sha or "").strip()):
        return JSONResponse({"ok": False, "error": "bad sha"}, status_code=400)
    data, err = await _grasp_json("%s/%s/%s.git/commit/%s" % (*tgt, sha.strip()), 25.0)
    return err or JSONResponse({"ok": True, **data})


@router.get("/git/blob")
async def git_blob(url: str, path: str, ref: str = "HEAD"):
    """One file's content from a self-hosted GRASP repo (Files browser). Text -> {ok, text}; binary or
    >1 MB -> {ok, binary:true, size} (the client shows a note/download instead of rendering)."""
    if not (path or "").strip("/"):
        return JSONResponse({"ok": False, "error": "bad path"}, status_code=400)
    u = _grasp_url(url, "raw", path, ref=ref)
    if not u:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=4.0)) as c:
            r = await c.get(u)
            if r.status_code != 200:
                return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
            content = r.content
    except Exception:
        return JSONResponse({"ok": False, "error": "read failed"}, status_code=502)
    if len(content) > 1024 * 1024:
        return JSONResponse({"ok": True, "binary": True, "size": len(content)})
    try:
        return JSONResponse({"ok": True, "text": content.decode("utf-8")})
    except UnicodeDecodeError:
        return JSONResponse({"ok": True, "binary": True, "size": len(content)})


@router.get("/git/download")
async def git_download(url: str, path: str, ref: str = "HEAD"):
    """Download one file from a self-hosted GRASP repo as an attachment — streamed straight through
    from the git host so a large file never lands in this process's memory. Binary-safe (unlike /blob,
    which exists to RENDER text), which is what "save this file" needs."""
    from fastapi.responses import StreamingResponse
    if not (path or "").strip("/"):
        return JSONResponse({"ok": False, "error": "bad path"}, status_code=400)
    u = _grasp_url(url, "download", path, ref=ref)
    if not u:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    name = (path or "").strip("/").split("/")[-1] or "file"
    safe = re.sub(r'[^A-Za-z0-9._-]', "_", name)[:100] or "file"
    import httpx
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=4.0))
    try:
        req = client.build_request("GET", u)
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        return JSONResponse({"ok": False, "error": "read failed"}, status_code=502)
    if resp.status_code != 200:
        await resp.aclose()
        await client.aclose()
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    async def _body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {"Content-Disposition": 'attachment; filename="%s"' % safe}
    if resp.headers.get("Content-Length"):
        headers["Content-Length"] = resp.headers["Content-Length"]
    return StreamingResponse(_body(), media_type="application/octet-stream", headers=headers)


class GitEditReq(BaseModel):
    url: str                       # the repo's clone URL (identifies owner + repo id)
    path: str                      # file to write, repo-relative
    ref: str = "HEAD"              # branch to commit on
    content: str = ""              # new file content (ignored when delete=true)
    message: str = ""              # commit message
    base: str = ""                 # commit sha the editor started from (compare-and-swap)
    delete: bool = False
    auth: str                      # NIP-98 `Nostr <base64>` header value, signed by a maintainer


class GitCreateReq(BaseModel):
    url: str                       # INTENDED clone URL: <git_host_base>/<owner-npub>/<repo_id>.git
    name: str = ""
    description: str = ""
    private: bool = False
    auth: str                      # NIP-98 header signed by the repo owner, bound to <url>/create


class GitDeleteReq(BaseModel):
    url: str                       # the repo's clone URL (identifies owner + repo id)
    auth: str                      # NIP-98 header signed by the repo OWNER, bound to <url>/delete


@router.post("/git/edit")
async def git_edit(data: GitEditReq):
    """Commit a single file change to a self-hosted GRASP repo from the web editor.

    This endpoint holds NO authority: it forwards the caller's NIP-98 header to the git host, which
    verifies the signature against the repo's maintainer ACL (owner ∪ 30617 `maintainers`) exactly as
    the push hook does. So a web edit is authorized by the same Nostr key that authorizes a push, and
    this proxy can't grant anything a `git push` couldn't."""
    tgt = _grasp_host_target(data.url)
    if not tgt:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    if not (data.auth or "").strip().lower().startswith("nostr "):
        return JSONResponse({"ok": False, "error": "a signed NIP-98 header is required"}, status_code=400)
    body = {"ref": data.ref, "path": data.path, "content": data.content,
            "message": data.message, "base": data.base, "delete": bool(data.delete)}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=4.0)) as c:
            r = await c.post("%s/%s/%s.git/edit" % tgt, json=body,
                             headers={"Authorization": data.auth})
    except Exception as e:
        logger.warning("[client] git edit proxy failed: %s", e)
        return JSONResponse({"ok": False, "error": "the git host did not answer"}, status_code=502)
    try:
        payload = r.json()
    except Exception:
        payload = {"ok": r.status_code == 200,
                   "error": (r.text or "").strip()[:200] or "edit failed"}
    return JSONResponse(payload, status_code=r.status_code)


@router.post("/git/create")
async def git_create(data: GitCreateReq):
    """Provision a new self-hosted GRASP repo from the web "New repo" button.

    Like /git/edit this endpoint holds NO authority: it forwards the caller's NIP-98 header to the git
    host (the hosting node — the peer this node proxies to, or localhost when we ARE the host), which
    re-verifies the signature is the repo owner's AND that the owner is on git_server_allowlist before
    creating anything. So a web "create" is authorized by the same Nostr key that would `git push`, and
    the response hands back the 30617 tags for the CLIENT to sign+publish (we never sign for the user)."""
    tgt = _grasp_host_target(data.url)
    if not tgt:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo URL"}, status_code=400)
    if not (data.auth or "").strip().lower().startswith("nostr "):
        return JSONResponse({"ok": False, "error": "a signed NIP-98 header is required"}, status_code=400)
    body = {"name": data.name, "description": data.description, "private": bool(data.private)}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=4.0)) as c:
            r = await c.post("%s/%s/%s.git/create" % tgt, json=body,
                             headers={"Authorization": data.auth})
    except Exception as e:
        logger.warning("[client] git create proxy failed: %s", e)
        return JSONResponse({"ok": False, "error": "the git host did not answer"}, status_code=502)
    try:
        payload = r.json()
    except Exception:
        payload = {"ok": r.status_code == 200,
                   "error": (r.text or "").strip()[:200] or "create failed"}
    return JSONResponse(payload, status_code=r.status_code)


@router.post("/git/delete")
async def git_delete(data: GitDeleteReq):
    """Delete a self-hosted GRASP repo. Like /git/edit + /git/create it holds NO authority: it forwards
    the caller's NIP-98 header to the git host, which re-verifies the signer IS the repo owner before
    removing anything. The 30617 announcement + 30618 state are deleted client-side (NIP-09)."""
    tgt = _grasp_host_target(data.url)
    if not tgt:
        return JSONResponse({"ok": False, "error": "not a self-hosted repo"}, status_code=400)
    if not (data.auth or "").strip().lower().startswith("nostr "):
        return JSONResponse({"ok": False, "error": "a signed NIP-98 header is required"}, status_code=400)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=4.0)) as c:
            r = await c.post("%s/%s/%s.git/delete" % tgt, headers={"Authorization": data.auth})
    except Exception as e:
        logger.warning("[client] git delete proxy failed: %s", e)
        return JSONResponse({"ok": False, "error": "the git host did not answer"}, status_code=502)
    try:
        payload = r.json()
    except Exception:
        payload = {"ok": r.status_code == 200,
                   "error": (r.text or "").strip()[:200] or "delete failed"}
    return JSONResponse(payload, status_code=r.status_code)


_nip05_cache: dict[str, tuple[float, dict]] = {}   # "domain|name" -> (expires, data)
_NIP05_TTL = 600.0


@router.get("/reports")
async def client_reports(pubkey: str):
    """NIP-56 reports a user has RECEIVED (kind-1984 events that p-tag them). The built-in relay only
    stores WoT-authored events, so reports about an arbitrary user live UPSTREAM — fetch them on demand
    from the configured upstream relays (the client is relay-only and can't reach them itself). Returns
    each report's reporter, type, reason, the reported event id (if any), and time."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    from app.services.nostr import relay as _relay
    ups = (nostr_service.relay.normalize_relays(settings_store.get("nostr_relay_upstream_relays", ""))
           or list(nostr_service.DEFAULT_RELAYS))[:8]
    try:
        evs = await _relay.query(ups, [{"kinds": [1984], "#p": [h], "limit": 300}])
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    seen, out = set(), []
    for e in (evs or []):
        eid = e.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        tags = e.get("tags", [])
        ptag = next((t for t in tags if t and t[0] == "p" and len(t) > 1 and t[1] == h), None)
        etag = next((t for t in tags if t and t[0] == "e" and len(t) > 1), None)
        rtype = ((ptag[2] if ptag and len(ptag) > 2 else None)
                 or (etag[2] if etag and len(etag) > 2 else None)
                 or next((t[1] for t in tags if t and t[0] == "report" and len(t) > 1), None) or "other")
        out.append({"id": eid, "reporter": e.get("pubkey"), "type": rtype,
                    "reason": (e.get("content") or "")[:1000], "created_at": e.get("created_at", 0),
                    "event": (etag[1] if etag else None)})
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return {"ok": True, "reports": out}


@router.get("/nip05")
async def nip05_proxy(domain: str, name: str = "_"):
    """CORS proxy for NIP-05 verification. The blue check verifies a profile's claimed name@domain
    by fetching `https://domain/.well-known/nostr.json?name=NAME` and checking the returned pubkey.
    Most domains DON'T send `Access-Control-Allow-Origin`, so a direct browser fetch fails and the
    check never shows — so the page asks the node to fetch it (server-side, SSRF-guarded) instead.
    Returns just the queried name's mapping: {"names": {name: pubkey}, "pubkey": pubkey}."""
    import re
    domain = (domain or "").strip().lower()
    name = (name or "_").strip()
    if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$", domain) \
            or len(name) > 64 or not re.match(r"^[a-z0-9_.\-]+$", name, re.I):
        return JSONResponse({"names": {}}, status_code=400)
    key = f"{domain}|{name}"
    now = time.time()
    hit = _nip05_cache.get(key)
    if hit and hit[0] > now:
        return JSONResponse(hit[1])
    if not _is_public_host(domain):
        # cache the negative too, so a bad/unresolvable domain shared by many profiles isn't
        # re-resolved on every blue-check attempt
        _nip05_cache[key] = (now + _NIP05_TTL, {"names": {}})
        return JSONResponse({"names": {}}, status_code=400)
    from urllib.parse import quote
    url = f"https://{domain}/.well-known/nostr.json?name={quote(name)}"
    out = {"names": {}}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=4.0), follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; PosterChanBot/1.0)"}) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code == 200:
                    body = b""
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > 262144:   # 256 KB cap (some servers return the full registry)
                            break
                    j = json.loads(body.decode("utf-8", "ignore"))
                    names = j.get("names", {}) if isinstance(j, dict) else {}
                    pk = names.get(name)
                    if isinstance(pk, str):
                        out = {"names": {name: pk}, "pubkey": pk}
    except Exception as e:
        logger.debug("[client] nip05 proxy failed for %s: %s", url, e)
    _nip05_cache[key] = (now + _NIP05_TTL, out)
    if len(_nip05_cache) > 5000:
        _nip05_cache.clear()
    return JSONResponse(out)


@router.get("/lnurl")
async def lnurl_proxy(url: str):
    """CORS fallback for NIP-57 zaps: fetch an LNURL-pay endpoint (the lnurlp well-known params or
    the invoice callback) server-side when the wallet service doesn't send CORS headers. HTTPS-only
    and SSRF-guarded, returns the parsed JSON. The client tries a direct fetch first and only falls
    back here on failure."""
    from urllib.parse import urlparse
    if not url.startswith("https://"):
        return JSONResponse({"status": "ERROR", "reason": "https required"}, status_code=400)
    host = urlparse(url).hostname or ""
    if not host or not _is_public_host(host):
        return JSONResponse({"status": "ERROR", "reason": "host not allowed"}, status_code=400)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (compatible; PosterChanBot/1.0)"}) as client:
            async with client.stream("GET", url) as resp:
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > 131072:   # 128 KB cap — LNURL responses are tiny JSON
                        break
                return JSONResponse(json.loads(body.decode("utf-8", "ignore")))
    except Exception as e:
        logger.debug("[client] lnurl proxy failed for %s: %s", url, e)
        return JSONResponse({"status": "ERROR", "reason": "fetch_failed"}, status_code=502)


# ----- captcha: distorted-text challenge gating new-account WoT admission (anti-spam/DDoS) -----
_CAPTCHAS: dict = {}                       # token -> (CODE, expiry). Per-process (single worker on 3051).
_CAPTCHA_TTL = 300.0
_CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no ambiguous 0/O/1/I/L


def _captcha_new() -> tuple[str, str]:
    import secrets
    now = time.time()
    for t in [k for k, (_, e) in list(_CAPTCHAS.items()) if e < now]:
        _CAPTCHAS.pop(t, None)
    if len(_CAPTCHAS) > 5000:              # hard memory cap
        _CAPTCHAS.clear()
    code = "".join(secrets.choice(_CAPTCHA_ALPHABET) for _ in range(5))
    token = secrets.token_urlsafe(18)
    _CAPTCHAS[token] = (code, now + _CAPTCHA_TTL)
    return token, code


def _captcha_image(code: str) -> bytes:
    import io
    import random
    from PIL import Image, ImageDraw, ImageFont
    W, H = 214, 74
    img = Image.new("RGB", (W, H), (10, 6, 24))
    d = ImageDraw.Draw(img)
    font = None
    for fp in ("/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
               "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(fp, 42)
            break
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()
    for _ in range(6):                     # neon noise lines
        d.line([(random.randint(0, W), random.randint(0, H)), (random.randint(0, W), random.randint(0, H))],
               fill=(random.randint(0, 90), random.randint(90, 220), random.randint(140, 255)), width=1)
    x = 16
    for ch in code:                        # jittered, multi-coloured glyphs
        d.text((x, random.randint(6, 22)), ch, font=font,
               fill=(random.randint(140, 255), random.randint(120, 255), random.randint(180, 255)))
        x += random.randint(33, 40)
    for _ in range(220):                   # speckle
        img.putpixel((random.randint(0, W - 1), random.randint(0, H - 1)),
                     (random.randint(0, 120), random.randint(120, 255), random.randint(160, 255)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _captcha_verify(token, answer) -> bool:
    rec = _CAPTCHAS.pop(token, None) if token else None    # one-shot (consumed on check)
    if not rec or not answer:
        return False
    code, exp = rec
    return exp >= time.time() and str(answer).strip().upper() == code


@router.get("/captcha")
async def captcha():
    """Issue a fresh distorted-text captcha (token + inline PNG). Must be solved at signup to admit a
    new account to the WoT — anti-spam/DDoS on account creation."""
    import base64 as _b64
    token, code = _captcha_new()
    try:
        img = _captcha_image(code)
    except Exception as e:
        return JSONResponse({"error": f"captcha render failed: {e}"}, status_code=500)
    return JSONResponse({"token": token, "image": "data:image/png;base64," + _b64.b64encode(img).decode()})


class SignupFollow(BaseModel):
    pubkey: str   # new account's npub or 64-hex
    captcha_token: str | None = None
    captcha_answer: str | None = None


async def _publish_to_relay(port: int, event: dict, timeout: float = 8.0) -> tuple[bool, str]:
    """Publish a signed event to the local relay over WebSocket (the normal publish path, so the
    WoT gate applies — the operator IS a member). Returns (accepted, message)."""
    import websockets
    uri = f"ws://127.0.0.1:{port}/relay"
    try:
        async with websockets.connect(uri, open_timeout=timeout, close_timeout=2) as ws:
            await ws.send(json.dumps(["EVENT", event]))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(raw)
                if msg[0] == "OK" and msg[1] == event["id"]:
                    return bool(msg[2]), (msg[3] if len(msg) > 3 else "")
    except Exception as e:
        return False, str(e)


async def _fetch_latest_kind3(port: int, pubkey: str, timeout: float = 6.0) -> dict | None:
    """Grab the operator's newest contact list (kind 3) from the relay so we append, not clobber."""
    import websockets
    uri = f"ws://127.0.0.1:{port}/relay"
    sub = "op-k3"
    latest = None
    try:
        async with websockets.connect(uri, open_timeout=timeout, close_timeout=2) as ws:
            await ws.send(json.dumps(["REQ", sub, {"authors": [pubkey], "kinds": [3], "limit": 1}]))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
                if msg[0] == "EVENT" and msg[1] == sub:
                    latest = msg[2]
                elif msg[0] == "EOSE" and msg[1] == sub:
                    break
    except Exception as e:
        logger.debug("[client] kind3 fetch failed: %s", e)
    return latest


async def follow_and_admit(db: Session, new_pk: str) -> tuple[bool, str]:
    """Operator follows the new account AND admits it to the relay WoT IMMEDIATELY — so a fresh user
    can post + receive DMs right away, not after the daily upstream-driven rebuild. Reused by signup
    AND nostr-login (so login-with-existing-key users are admitted too). Returns (ok, message)."""
    if not new_pk:
        return False, "invalid pubkey"
    # Admit to the WoT now (this is what actually unblocks their posts + DMs-to-them on the relay),
    # regardless of how fast the kind-3 follow propagates upstream.
    try:
        from app.services.nostr_relay.thread import trigger_wot_add
        trigger_wot_add([new_pk])
    except Exception as e:
        logger.warning("[client] wot-add failed: %s", e)
    op = _operator(db)
    if not op or not op.nostr_nsec:
        return True, "admitted (no operator to follow you)"
    try:
        seckey = nostr_service.decode_seckey(op.nostr_nsec)
    except ValueError:
        return True, "admitted (operator key invalid)"
    op_pk = nostr_service.derive_pubkey(seckey)
    port = int(_setting(db, "nostr_relay_port", "3052"))
    existing = await _fetch_latest_kind3(port, op_pk)
    tags = [list(t) for t in (existing.get("tags", []) if existing else [])]
    if any(len(t) >= 2 and t[0] == "p" and t[1] == new_pk for t in tags):
        return True, "already followed"
    tags.append(["p", new_pk])
    ev = nostr_event.build_event(seckey, 3, (existing.get("content", "") if existing else ""), tags=tags)
    accepted, msg = await _publish_to_relay(port, ev)
    if accepted:
        # The new member is ALREADY admitted instantly via trigger_wot_add above — do NOT trigger a
        # full WoT graph crawl here. Per-follow full rebuilds (one per signup) were pegging a core;
        # the expensive crawl is left to the daily cadence (and the throttled admin/bot refresh path).
        return True, "operator followed you + admitted"
    return True, f"admitted (follow not stored: {msg})"   # WoT-add already done → still usable


@router.post("/signup-follow")
async def signup_follow(data: SignupFollow, db: Session = Depends(get_db)):
    """Operator auto-follows + admits a freshly-created account so the WoT relay accepts its posts."""
    new_pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not new_pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _captcha_verify(data.captcha_token, data.captcha_answer):
        return JSONResponse({"ok": False, "error": "captcha", "message": "captcha incorrect or expired"}, status_code=403)
    ok, msg = await follow_and_admit(db, new_pk)
    return JSONResponse({"ok": ok, "message": msg})


class ClaimAdmin(BaseModel):
    pubkey: str          # npub/hex claiming admin
    auth: str            # base64 signed event proving they hold the key


@router.post("/claim-admin")
async def claim_admin(data: ClaimAdmin, db: Session = Depends(get_db)):
    """First-run setup: on a fresh install with NO admin yet, the first key to sign in here claims
    admin (solves the chicken/egg — nobody can grant AI access until an admin exists). Locked down
    once any admin has an npub, so it can't be used to take over a configured instance."""
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "signature required (or stale request)"}, status_code=403)
    npub = nostr_service.npub_of(pk)
    # Re-check server-side: refuse only if a DIFFERENT key already claimed admin (so a configured
    # instance can't be taken over). IDEMPOTENT for the SAME key: on a turnkey node POSTERCHANAI_AUTO_ADMIN
    # already promoted this npub at login, so the client's (stale-config) first-run claim by that same key
    # must succeed — otherwise it 409s with the confusing "an admin already exists" right after first login.
    admins = db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).all()  # noqa: E712
    if admins and all(a.nostr_npub != npub for a in admins):
        return JSONResponse({"ok": False, "error": "an admin already exists"}, status_code=409)
    u = db.query(User).filter(User.nostr_npub == npub).first()
    if not u:
        from app.auth import get_password_hash
        base = "npub_" + npub[4:16]
        username = base
        for i in range(2, 100):
            if not db.query(User).filter(User.username == username).first():
                break
            username = f"{base}{i}"
        u = User(username=username, email=None,
                 password_hash=get_password_hash(secrets.token_urlsafe(32)),
                 email_verified=True, nostr_npub=npub)
        db.add(u)
    u.is_admin = True
    u.can_ai = True
    u.can_image = True
    u.can_blossom = True
    # Seed the WoT with the new admin's own npub so a fresh install self-bootstraps its trust set
    # (operator + everyone they follow) instead of relying on any baked-in seed list. Idempotent.
    seeds_val = settings_store.get("nostr_relay_wot_seeds", "") or ""
    if npub not in seeds_val:
        new_seeds = (seeds_val.rstrip() + "\n" + npub).strip() if seeds_val.strip() else npub
        settings_store.put("nostr_relay_wot_seeds", new_seeds)
    db.commit()
    logger.info("[client] first-run admin claimed by %s (%s)", u.username, npub[:16])
    try:
        await follow_and_admit(db, pk)
        from app.services.nostr_relay.thread import trigger_wot_refresh
        trigger_wot_refresh()
    except Exception as e:
        logger.warning("[client] follow/admit on claim-admin failed: %s", e)
    try:
        from app.services import users_store
        await users_store.sync_user(db, u)
    except Exception as e:
        logger.warning("[client] account sync after claim-admin failed: %s", e)
    return JSONResponse({"ok": True, "npub": npub})


class BlockReq(BaseModel):
    target: str          # npub/hex to (un)block
    remove: bool = False
    auth: str            # base64 of a signed Nostr event proving the requester holds an admin key


def _verify_admin_auth(db: Session, auth_b64: str, target_hex: str) -> str | None:
    """Verify a base64 signed Nostr auth event and return the signer's hex pubkey IFF: the
    signature is valid, created_at is within a 300s anti-replay window, the event p-tags exactly
    `target_hex` (so the signature authorizes THIS block, not any replayed admin event), and the
    signer is an admin user."""
    try:
        ev = json.loads(base64.b64decode(auth_b64))
    except Exception:
        return None
    if not nostr_event.verify_event(ev):
        return None
    if abs(int(ev.get("created_at", 0)) - int(time.time())) > 300:
        return None
    if not any(len(t) >= 2 and t[0] == "p" and t[1] == target_hex for t in ev.get("tags", [])):
        return None
    try:
        npub = nostr_service.npub_of(ev["pubkey"])
    except Exception:
        return None
    u = db.query(User).filter(User.nostr_npub == npub, User.is_admin == True).first()  # noqa: E712
    return ev["pubkey"] if u else None


def _verify_admin_signer(db: Session, auth_b64: str, expect_content: str) -> str | None:
    """Like `_verify_admin_auth` but for admin LIST endpoints that have no single target. With no target
    p-tag to bind to, the proof is bound instead to a FIXED action string in the event content
    (`expect_content`) — so a generic admin signature (a kind-1 note, a Blossom upload-auth, etc.) can't
    be replayed here; only an event the admin signed specifically for THIS action counts. Valid sig +
    300s anti-replay window + signer is an admin."""
    try:
        ev = json.loads(base64.b64decode(auth_b64))
    except Exception:
        return None
    if not nostr_event.verify_event(ev):
        return None
    if abs(int(ev.get("created_at", 0)) - int(time.time())) > 300:
        return None
    if (ev.get("content") or "") != expect_content:   # bind the proof to this specific action
        return None
    try:
        npub = nostr_service.npub_of(ev["pubkey"])
    except Exception:
        return None
    u = db.query(User).filter(User.nostr_npub == npub, User.is_admin == True).first()  # noqa: E712
    return ev["pubkey"] if u else None


@router.post("/block")
async def block_pubkey(data: BlockReq, db: Session = Depends(get_db)):
    """Admin-only: add/remove a pubkey on the relay's denylist and re-apply it live (gate + purge)."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    # Never block the node's own operator/bot keys — that would reject the operator's signup-follow
    # events and break new-account admission, with no in-app way to recover.
    if not data.remove:
        from app.services.blossom_service import _operator_pubkeys
        if target in _operator_pubkeys(db):
            return JSONResponse({"ok": False, "error": "refusing to block an operator key"}, status_code=400)

    blocked_val = settings_store.get("nostr_relay_blocked_pubkeys")
    current = []
    if blocked_val:
        for tok in blocked_val.replace(",", "\n").split():
            h = nostr_service.to_pubkey_hex(tok.strip())
            if h:
                current.append(h)
    cur = set(current)   # canonical hex set
    if data.remove:
        cur.discard(target)
    else:
        cur.add(target)
    # store as npubs (readable in the Admin → Relay "Blocked accounts" box; relay converts back)
    out = []
    for h in sorted(cur):
        try:
            out.append(nostr_service.npub_of(h))
        except Exception:
            out.append(h)
    value = "\n".join(out)
    settings_store.put("nostr_relay_blocked_pubkeys", value)

    try:
        from app.services.nostr_relay.thread import trigger_block_reload
        trigger_block_reload()
    except Exception as e:
        logger.warning("[client] block reload failed: %s", e)

    return JSONResponse({"ok": True, "blocked": not data.remove, "count": len(cur)})


# ----- Blossom upload access (admin grants/revokes via the client's profile menu) -----
class BlossomAccessReq(BaseModel):
    target: str          # npub/hex to grant/revoke
    grant: bool = True
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


def _whitelist_hex(db: Session) -> set:
    """Current `blossom_whitelist` setting as a hex pubkey set."""
    wl = settings_store.get("blossom_whitelist")
    out = set()
    if wl:
        for tok in wl.replace(",", "\n").split():
            h = nostr_service.to_pubkey_hex(tok.strip())
            if h:
                out.add(h)
    return out


@router.get("/blossom-access")
async def blossom_access_status(pubkey: str, db: Session = Depends(get_db)):
    """Blossom upload status for a pubkey.
      * `allowed`     — can it ACTUALLY upload to the built-in server? Mirrors the real gate
                        (blossom_service.is_pubkey_allowed: whitelist OR admin/can_blossom OR
                        operator/bot/DVM keys). The client uses this to decide built-in vs the
                        nostr.build fallback — so an admin who was never explicitly whitelisted is
                        no longer wrongly diverted to nostr.build.
      * `whitelisted` — raw membership of the `blossom_whitelist` setting, for the admin Grant/Revoke
                        toggle (which manages that list specifically)."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    from app.services import blossom_service
    return JSONResponse({
        "ok": True,
        "whitelisted": h in _whitelist_hex(db),
        "allowed": blossom_service.is_pubkey_allowed(db, h),
    })


@router.post("/blossom-access")
async def blossom_access(data: BlossomAccessReq, db: Session = Depends(get_db)):
    """Admin-only: add/remove a pubkey on the Blossom upload whitelist (Admin → Blossom tab)."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    # This is a read-modify-write of the SHARED replaceable whitelist. Refresh the cache from the relay's
    # own event store FIRST so `cur` reflects reality — otherwise a stale/empty cache (e.g. a grant on a
    # node whose cache is behind, or right after a restart) would rewrite a list missing everyone else's
    # grants (project_blossom_whitelist_wipe). If we've never synced with the relay, refuse rather than
    # risk wiping the list.
    settings_store.hydrate_from_db(db)
    if not settings_store.is_hydrated():
        return JSONResponse({"ok": False, "error": "settings still loading — try again in a moment"}, status_code=503)
    cur = _whitelist_hex(db)
    _was = target in cur          # 0->1 only: re-saving the list shouldn't re-DM everyone on it
    if data.grant:
        cur.add(target)
    else:
        cur.discard(target)
    # store as npubs (readable in Admin → Blossom; blossom_service accepts npub or hex)
    out = []
    for h in sorted(cur):
        try:
            out.append(nostr_service.npub_of(h))
        except Exception:
            out.append(h)
    value = "\n".join(out)
    # blossom_service re-reads the setting on its next (short-TTL) cache miss — no reload.
    # settings_store.put persists locally + writes through to the relay (authoritative store).
    settings_store.put("blossom_whitelist", value)
    if data.grant and not _was:
        # DM the PUBKEY. Whitelisting shouldn't conjure a User row for someone who has never signed
        # in — an account appearing as a side effect of a notification is a side effect too far.
        from app.services.access_notify_service import notify_access_granted
        await notify_access_granted(db, target, "blossom")
    return JSONResponse({"ok": True, "whitelisted": data.grant, "count": len(cur)})


# ----- Blossom purge (admin deletes ALL of a user's blobs from the client profile menu) -----
class BlossomPurgeReq(BaseModel):
    target: str          # npub/hex whose blobs to purge
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


@router.post("/blossom-purge")
async def blossom_purge(data: BlossomPurgeReq, db: Session = Depends(get_db)):
    """Admin-only: delete EVERY Blossom blob owned by a pubkey — underlying bytes (local/proxy) and
    the index rows. Irreversible; gated by the same signed-admin proof as /block."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    from app.services import blossom_service
    blobs = blossom_service.list_for_pubkey(db, target)
    deleted = 0
    for blob in blobs:
        await blossom_service.delete_blob_bytes(db, blob)
        db.delete(blob)
        deleted += 1
    db.commit()
    logger.info("[client] admin purged %d blossom blob(s) for %s", deleted, target)
    return JSONResponse({"ok": True, "deleted": deleted})


# ----- Blossom admin overview (storage-per-user + per-user file moderation, client Files → Admin tab) -----
class AdminAuthReq(BaseModel):
    auth: str            # base64 signed admin event (no target) — proves the caller is an admin


@router.post("/admin-blossom-usage")
async def admin_blossom_usage(data: AdminAuthReq, db: Session = Depends(get_db)):
    """Admin-only: Blossom storage used per uploader (bytes + blob count), biggest first. Powers the
    Files → Admin tab so admins can see who's using storage and drill into a user's files."""
    if not _verify_admin_signer(db, data.auth, "blossom-usage"):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    from app.models import BlossomBlob
    from sqlalchemy import func
    rows = (db.query(BlossomBlob.pubkey, func.coalesce(func.sum(BlossomBlob.size), 0), func.count())
            .group_by(BlossomBlob.pubkey).all())
    users = []
    for pk, total, cnt in rows:
        try:
            npub = nostr_service.npub_of(pk)
        except Exception:
            npub = pk
        users.append({"pubkey": pk, "npub": npub, "size": int(total or 0), "count": int(cnt or 0)})
    users.sort(key=lambda x: x["size"], reverse=True)
    return JSONResponse({"ok": True, "users": users, "total": sum(u["size"] for u in users)})


# Per-user file LISTING reuses the Blossom server's public /list/<pubkey> (BUD-02) on the client side —
# no dedicated endpoint needed; deletion reuses the existing /blossom-purge.


# ----- Relay sync (admin backfills a user's Nostr history from the client profile menu) -----
class RelaySyncReq(BaseModel):
    target: str          # npub/hex whose history to backfill into the relay
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


@router.post("/relay-sync")
async def relay_sync(data: RelaySyncReq, db: Session = Depends(get_db)):
    """Admin-only: backfill a user's Nostr post history into the built-in relay (the same
    "Sync a user's data" action as Admin → Relay). Gated by the signed-admin proof as /block."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    try:
        from app.services.nostr_relay.thread import trigger_backfill
        trigger_backfill(target)
    except Exception as e:
        logger.warning("[client] relay backfill failed for %s: %s", target, e)
        return JSONResponse({"ok": False, "error": "sync failed"}, status_code=500)
    logger.info("[client] admin queued relay backfill for %s", target)
    return JSONResponse({"ok": True})


# ----- AI access (admin approves a user's AI request from the client profile menu) -----
class AiAccessReq(BaseModel):
    target: str          # npub/hex to grant/revoke
    grant: bool = True
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


class StreamRequestReq(BaseModel):
    pubkey: str
    auth: str            # self-signed proof, same shape as claim-nip05


@router.post("/stream-request")
async def stream_request(data: StreamRequestReq, db: Session = Depends(get_db)):
    """A user asks for live-streaming access. Records it so the admin can see the queue even if the
    DM is missed, and notifies the admins — the same two halves the AI request has (Blossom only
    DMs, which loses the request if the admin never reads it)."""
    from app.models import UserSetting
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not u:
        return JSONResponse({"ok": False, "error": "sign in first"}, status_code=404)
    if u.is_admin or getattr(u, "can_stream", False):
        return JSONResponse({"ok": True, "already": True})
    row = db.query(UserSetting).filter(UserSetting.user_id == u.id,
                                       UserSetting.key == "stream_requested").first()
    if row:
        row.value = str(int(time.time()))
    else:
        db.add(UserSetting(user_id=u.id, key="stream_requested", value=str(int(time.time()))))
    db.commit()
    logger.info("[client] streaming access requested by %s", u.username)
    return JSONResponse({"ok": True})


@router.get("/stream-requests")
async def stream_requests(db: Session = Depends(get_db)):
    """Pending streaming-access requests, mirroring /ai-requests so the admin panel can list both."""
    from app.models import UserSetting
    out = []
    for r in db.query(UserSetting).filter(UserSetting.key == "stream_requested").all():
        u = db.query(User).filter(User.id == r.user_id).first()
        if u and u.nostr_npub and not (u.is_admin or getattr(u, "can_stream", False)):
            out.append({"npub": u.nostr_npub, "name": u.username, "ts": r.value})
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return JSONResponse({"ok": True, "requests": out})


@router.get("/ai-requests")
async def ai_requests(db: Session = Depends(get_db)):
    """Pending AI-access requests (users who asked, not yet granted), for admins to see + approve in
    the client. Sensitive only insofar as it lists requesters; returns just npub + name + when."""
    from app.models import UserSetting
    out = []
    for r in db.query(UserSetting).filter(UserSetting.key == "ai_requested").all():
        u = db.query(User).filter(User.id == r.user_id).first()
        if u and u.nostr_npub and not (u.is_admin or u.can_ai):
            out.append({"npub": u.nostr_npub, "name": u.username, "ts": r.value})
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return JSONResponse({"ok": True, "requests": out})


class BridgeAccessGrantReq(BaseModel):
    target: str          # npub/hex to grant/revoke bridge access for
    grant: bool = True
    auth: str            # admin-signed event (p-tags target), same proof as /ai-access


@router.get("/bridge-access")
async def bridge_access_status(pubkey: str, db: Session = Depends(get_db)):
    """Whether a user currently has Bridge Access (fedi bridge / cross-post enabled), so the admin
    Permissions panel shows the right toggle state."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(h)).first()
    on = bool(u and (getattr(u, "fedi_bridge_enabled", False) or getattr(u, "fedi_crosspost_enabled", False)))
    return JSONResponse({"ok": True, "enabled": on})


@router.post("/bridge-access")
async def bridge_access_grant(data: BridgeAccessGrantReq, db: Session = Depends(get_db)):
    """Admin-only: grant Bridge Access to a user (auto-create their fediverse account, copy profile,
    set NIP-05, enable cross-post + DMs/notifications) or revoke it. The admin whitelists anyone —
    no requirement that the user hold a NIP-05 on this domain."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin authorization required"}, status_code=403)
    npub = nostr_service.npub_of(target)
    u = db.query(User).filter(User.nostr_npub == npub).first()
    if not u:
        if not data.grant:
            return JSONResponse({"ok": True})   # nothing to revoke for a non-existent account
        # Onboard a native Nostr user who has never signed in: create their User row (mirrors the
        # nostr_login signup) so the admin can grant Bridge Access to ANY npub, then provision below.
        from app.auth import get_password_hash
        import secrets as _secrets
        base = "npub_" + npub[4:16]
        username = base
        for i in range(2, 100):
            if not db.query(User).filter(User.username == username).first():
                break
            username = f"{base}{i}"
        u = User(username=username, email=None,
                 password_hash=get_password_hash(_secrets.token_urlsafe(32)),
                 is_admin=False, email_verified=True, nostr_npub=npub,
                 can_image=True, can_music=True, can_video=False, can_torrent=False,
                 can_blossom=False, can_ai=False)
        db.add(u)
        db.commit()
        db.refresh(u)
        try:
            await follow_and_admit(db, target)   # admit to the relay WoT + operator follow
        except Exception as e:
            logger.warning("[bridge-access] follow/admit for new user failed: %s", e)
    from app.services import fedi_bridge_access
    r = await (fedi_bridge_access.enable(db, u, by_admin=True) if data.grant else fedi_bridge_access.disable(db, u))
    if not r.get("ok"):
        return JSONResponse({"ok": False, "error": r.get("error") or "failed"}, status_code=400)
    return JSONResponse({"ok": True})


_USER_CAPS = ("can_image", "can_music", "can_video", "can_torrent")


class UserCapsReq(BaseModel):
    target: str
    caps: dict           # {can_torrent: true, ...}
    auth: str            # admin-signed event (p-tags target)


@router.get("/user-caps")
async def user_caps_status(pubkey: str, db: Session = Depends(get_db)):
    """A user's feature capabilities (image/music/video/torrent), so an admin can toggle them from
    the client profile menu instead of Admin → Users."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(h)).first()
    if not u:
        return JSONResponse({"ok": True, "exists": False})
    return JSONResponse({"ok": True, "exists": True, "is_admin": bool(u.is_admin),
                         "caps": {c: bool(getattr(u, c, False)) for c in _USER_CAPS}})


async def _find_or_create_user(db, hex_pk: str):
    """Find a user by npub, or CREATE a gated account so an admin can PRE-GRANT access by npub
    (before the person has ever signed in). New accounts are admitted to the WoT + get a storage key,
    same as a fresh nostr-login. Returns the User."""
    npub = nostr_service.npub_of(hex_pk)
    u = db.query(User).filter(User.nostr_npub == npub).first()
    if u:
        return u
    from app.auth import get_password_hash
    base = "npub_" + npub[4:16]
    username = base
    for i in range(2, 1000):
        if not db.query(User).filter(User.username == username).first():
            break
        username = f"{base[:46]}{i}"
    u = User(username=username, email=None,
             password_hash=get_password_hash(secrets.token_urlsafe(32)),
             email_verified=True, nostr_npub=npub,
             can_image=True, can_music=True, can_video=False, can_torrent=False,
             can_blossom=False, can_ai=False)
    db.add(u)
    db.commit()
    db.refresh(u)
    try:
        from app.services import nostr_store
        nostr_store.user_storage_seckey(db, u)
        await follow_and_admit(db, hex_pk)
    except Exception as e:
        logger.warning("[client] provisioning pre-granted account failed: %s", e)
    return u


@router.post("/user-caps")
async def user_caps_set(data: UserCapsReq, db: Session = Depends(get_db)):
    """Admin-only: set a user's feature capabilities by npub (replaces the Admin → Users toggles).
    Creates the account if it doesn't exist yet (pre-grant)."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    u = await _find_or_create_user(db, target)
    for c in _USER_CAPS:
        if c in data.caps:
            setattr(u, c, bool(data.caps[c]))
    db.commit()
    logger.info("[client] caps for %s set: %s", u.username, {c: getattr(u, c) for c in _USER_CAPS})
    try:
        from app.services import users_store
        await users_store.sync_user(db, u)
    except Exception as e:
        logger.warning("[client] account sync after caps failed: %s", e)
    return JSONResponse({"ok": True, "caps": {c: bool(getattr(u, c, False)) for c in _USER_CAPS}})


class StreamAccessReq(BaseModel):
    target: str
    grant: bool = True
    auth: str


@router.get("/stream-access")
async def stream_access_status(pubkey: str, db: Session = Depends(get_db)):
    """Is live streaming enabled for this account? Drives the toggle in Additional permissions."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(h)).first()
    return JSONResponse({"ok": True, "exists": bool(u),
                         "enabled": bool(u and (u.is_admin or getattr(u, "can_stream", False)))})


@router.post("/stream-access")
async def stream_access(data: StreamAccessReq, db: Session = Depends(get_db)):
    """Admin-only: grant/revoke live-streaming access (can_stream), mirroring /ai-access."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    u = await _find_or_create_user(db, target)
    _was = bool(getattr(u, "can_stream", False))
    u.can_stream = bool(data.grant)
    db.commit()
    logger.info("[client] streaming access %s for %s", "granted" if data.grant else "revoked", u.username)
    if bool(data.grant) and not _was:
        from app.services.access_notify_service import notify_access_granted
        await notify_access_granted(db, u, "stream")
    try:
        from app.services import users_store
        await users_store.sync_user(db, u)
    except Exception as e:
        logger.warning("[client] account sync after stream-access failed: %s", e)
    return JSONResponse({"ok": True, "enabled": bool(data.grant)})


@router.get("/ai-access")
async def ai_access_status(pubkey: str, db: Session = Depends(get_db)):
    """Is AI enabled for this account? Drives Grant vs Revoke in the client profile menu."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(h)).first()
    return JSONResponse({"ok": True, "exists": bool(u),
                         "enabled": bool(u and (u.is_admin or u.can_ai))})


@router.post("/ai-access")
async def ai_access(data: AiAccessReq, db: Session = Depends(get_db)):
    """Admin-only: grant/revoke a user's AI access (the can_ai flag). The user must have signed in
    to the AI app at least once (so a User row exists for their npub)."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    u = await _find_or_create_user(db, target)   # pre-grant: create the account if it doesn't exist
    _was = bool(u.can_ai)
    u.can_ai = bool(data.grant)
    db.commit()
    logger.info("[client] AI access %s for %s", "granted" if data.grant else "revoked", u.username)
    # Tell them straight away. Only on the 0->1 transition: re-saving the same permission shouldn't
    # re-DM, and a revoke certainly shouldn't.
    if bool(data.grant) and not _was:
        from app.services.access_notify_service import notify_access_granted
        await notify_access_granted(db, u, "ai")
    try:
        from app.services import users_store
        await users_store.sync_user(db, u)
    except Exception as e:
        logger.warning("[client] account sync after ai-access failed: %s", e)
    return JSONResponse({"ok": True, "enabled": bool(data.grant)})


# ----- AI files: list/delete the user's encrypted chat uploads + generated artifacts -----
class AiFileReq(BaseModel):
    pubkey: str
    auth: str
    sha: str = ""        # for delete


@router.post("/ai-files")
async def ai_files(data: AiFileReq, db: Session = Depends(get_db)):
    """List the user's AI chat files (uploads + generated images) — decrypted refs, served via the
    decrypting /api/files route. Lets the client show + manage them (they live under the storage key,
    so they don't appear in the normal Blossom Files list)."""
    import re
    from urllib.parse import quote
    from app.services import nostr_store as store
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": True, "files": []})
    sk = store.user_storage_seckey(db, user)
    port = int(_setting(db, "nostr_relay_port", "3052"))
    np = nostr_service.npub_of(pk)
    out = []
    for d, ref in (await store.list_docs(port, store.NS_UPLOAD, seckey=sk)).items():
        if isinstance(ref, dict) and ref.get("sha256"):
            conv = d[len(store.NS_UPLOAD):].split(":")[0]
            name = ref.get("name") or "file"
            ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
            out.append({"url": f"/client/file/{np}/{conv}/enc_{ref['sha256']}.{ext}",
                        "name": name, "mime": ref.get("mime") or "", "sha": ref["sha256"], "kind": "upload"})
    for d, rec in (await store.list_docs(port, store.NS_MSG, seckey=sk)).items():
        if not isinstance(rec, dict):
            continue
        conv = d[len(store.NS_MSG):].split(":")[0]
        # An artifact is referenced in ONE of two places, and listing only the first was a bug: a
        # generated IMAGE lands in `image_path`, but generated/derived MEDIA (an extracted MP3, a
        # rendered song or video, a converted file) is appended into the message CONTENT as a
        # markdown link. So an "extract the audio" result existed in Blossom but never appeared in
        # AI chat's Files list. Scan both, de-duplicated by sha.
        _refs = [rec.get("image_path") or ""]
        _refs += re.findall(r'enc_[0-9a-f]{64}\.\w+', rec.get("content") or "")
        for _r in _refs:
            m = re.search(r'(enc_([0-9a-f]{64})\.(\w+))$', _r or "")
            if not m or any(f["sha"] == m.group(2) for f in out):
                continue
            ext = (m.group(3) or "").lower()
            mime = ("audio/mpeg" if ext in ("mp3", "m4a", "aac") else
                    "audio/wav" if ext == "wav" else
                    "audio/flac" if ext == "flac" else
                    "video/mp4" if ext in ("mp4", "webm", "mov") else
                    "application/pdf" if ext == "pdf" else "image/png")
            label = ("generated audio" if mime.startswith("audio/") else
                     "generated video" if mime.startswith("video/") else
                     "generated file" if mime == "application/pdf" else "generated image")
            out.append({"url": f"/client/file/{np}/{conv}/{m.group(1)}",
                        "name": label, "mime": mime, "sha": m.group(2), "kind": "generated"})
    # ORPHANS. Everything above is found by walking the user's relay DOCS, so an artifact whose
    # referencing message is gone (a deleted chat, before the cleanup covered content links) became
    # invisible AND undeletable — the blob just sat there. Blossom's own listing excluded private blobs
    # entirely (`include_private` had no callers), so nothing in the product could see them. Ownership
    # is already proved above, so list them here with their sizes and let the owner clean them up.
    orphans, orphan_bytes = [], 0
    try:
        from app.services import blossom_service
        from app.services.nostr import bip340
        storage_pub = bip340.pubkey_from_seckey(sk).hex()
        known = {f["sha"] for f in out}
        for blob in blossom_service.list_for_pubkey(db, storage_pub, include_private=True):
            if not blob.private or blob.sha256 in known:
                continue
            orphan_bytes += int(blob.size or 0)
            orphans.append({"sha": blob.sha256, "size": int(blob.size or 0),
                            "uploaded": int(blob.created_at or 0), "kind": "orphan",
                            "name": "unreferenced artifact", "mime": blob.mime or "",
                            # No URL: /client/file needs a conversation to decrypt against, and this
                            # blob's chat is gone. It can be sized and deleted, not viewed.
                            "url": ""})
        orphans.sort(key=lambda o: o["size"], reverse=True)
    except Exception as e:
        logger.warning("[client] orphan artifact scan failed: %s", e)
    return JSONResponse({"ok": True, "files": out, "orphans": orphans,
                         "orphan_bytes": orphan_bytes})


@router.post("/ai-files-prune")
async def ai_files_prune(data: AiFileReq, db: Session = Depends(get_db)):
    """Delete EVERY unreferenced private artifact blob of this user (the `orphans` that /ai-files
    reports). Signed self-auth, same proof as the listing.

    Safety: a blob is only removed when NO live doc of this user references its sha — the two places a
    reference can live (an NS_UPLOAD ref, or an NS_MSG record's `image_path`/content) are both scanned
    first, and only blobs under the caller's OWN storage pubkey are considered."""
    import re
    from app.services import nostr_store as store
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": True, "deleted": 0, "bytes": 0})
    sk = store.user_storage_seckey(db, user)
    port = int(_setting(db, "nostr_relay_port", "3052"))
    keep: set = set()
    for ref in (await store.list_docs(port, store.NS_UPLOAD, seckey=sk)).values():
        if isinstance(ref, dict) and ref.get("sha256"):
            keep.add(ref["sha256"])
    for rec in (await store.list_docs(port, store.NS_MSG, seckey=sk)).values():
        if not isinstance(rec, dict):
            continue
        keep |= set(re.findall(r'enc_([0-9a-f]{64})', rec.get("image_path") or ""))
        keep |= set(re.findall(r'enc_([0-9a-f]{64})', rec.get("content") or ""))
    from app.services import blossom_service, artifact_store
    from app.services.nostr import bip340
    storage_pub = bip340.pubkey_from_seckey(sk).hex()
    deleted, freed = 0, 0
    for blob in blossom_service.list_for_pubkey(db, storage_pub, include_private=True):
        if not blob.private or blob.sha256 in keep:
            continue
        size = int(blob.size or 0)
        try:
            if await artifact_store.delete_blob(db, blob.sha256):
                deleted += 1
                freed += size
        except Exception as e:
            logger.warning("[client] orphan prune failed for %s: %s", blob.sha256[:12], e)
    logger.info("[client] pruned %d orphaned artifact(s) (%d bytes) for %s", deleted, freed, pk[:12])
    return JSONResponse({"ok": True, "deleted": deleted, "bytes": freed})



# ---- AI-chat file access: a signed, expiring, HttpOnly cookie -------------------------------------
# /client/file used to serve DECRYPTED artifacts to anyone who knew the blob's sha256, on the theory
# that the sha was a secret capability. Two public sources leaked it (the BUD-02 listing and the
# upload doc's d-tag, both since fixed) — but "you have to know the hash" was never privacy: the URL
# is bearer, permanent, and survives being pasted anywhere. These files are private, so the endpoint
# now demands proof of ownership.
#
# It has to be a COOKIE, not a header: these URLs go in <img src>/<video src>, which cannot carry an
# Authorization header. The client proves ownership ONCE (a signed kind-27235, same scheme as
# /client/ai-files) and gets a short-lived cookie the browser then attaches automatically.
_FILE_COOKIE = "pc_file"
_FILE_TTL = 12 * 3600


def _file_auth_secret() -> bytes:
    """HMAC key derived from this node's operator key: stable across restarts, never leaves the box,
    and needs no new stored secret."""
    from app.services import keystore
    seed = (keystore.get_operator_nsec() or "pcai-no-operator-key")
    return hashlib.sha256(b"pcai-file-auth|" + seed.encode()).digest()


def _mint_file_cookie(pubkey_hex: str) -> str:
    exp = int(time.time()) + _FILE_TTL
    msg = f"{pubkey_hex}.{exp}".encode()
    sig = hmac.new(_file_auth_secret(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{pubkey_hex}.{exp}.{sig}"


def _file_cookie_pubkey(raw: str) -> str:
    """The pubkey a cookie proves ownership of, or "" when absent/forged/expired."""
    try:
        pk, exp, sig = (raw or "").split(".")
        if int(exp) < int(time.time()):
            return ""
        good = hmac.new(_file_auth_secret(), f"{pk}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return pk if hmac.compare_digest(sig, good) else ""
    except Exception:
        return ""


class FileAuthReq(BaseModel):
    pubkey: str
    auth: str


@router.post("/file-auth")
async def client_file_auth(data: FileAuthReq, request: Request):
    """Exchange a signed ownership proof for the short-lived cookie /client/file requires."""
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    tok = _mint_file_cookie(pk)
    # Is this request on a connection where a cookie CAN'T work? An .onion is plain http, and a `Secure`
    # cookie (which SameSite=None requires) is refused over a non-HTTPS connection — so the APK, whose
    # page origin is https://localhost, has no cookie path to an onion host at all.
    proto = (request.headers.get("x-forwarded-proto", "") or "").split(",")[0].strip().lower() \
        or request.url.scheme
    cleartext = proto != "https"
    # Only THEN hand the token to script, as a ?t= fallback. The cookie is HttpOnly on purpose ("script
    # never needs to read it"), so widening that everywhere to serve the onion case would be a straight
    # downgrade for the HTTPS majority that never needs it.
    body = {"ok": True, "expires_in": _FILE_TTL}
    if cleartext:
        body["token"] = tok
    resp = JSONResponse(body)
    # SameSite=None so the APK's WebView (a different origin from the API host) still sends it; Secure
    # keeps that safe. Over cleartext neither applies (see above) — drop both so a direct Tor Browser
    # visit, which IS same-site, still gets a usable cookie instead of one the browser rejects outright.
    resp.set_cookie(_FILE_COOKIE, tok, max_age=_FILE_TTL, httponly=True,
                    secure=not cleartext, samesite="lax" if cleartext else "none", path="/client/file")
    return resp


@router.get("/file/{npub}/{conv}/{name}")
async def client_file(npub: str, conv: str, name: str, request: Request, db: Session = Depends(get_db)):
    """Serve a decrypted AI-chat artifact for the Nostr client. The client has NO server session, so
    unlike /api/files (session-gated) this is addressed by the encrypted blob's **sha256** — which is
    the secret stored inside the owner's NIP-44-encrypted NS_UPLOAD doc (knowing it == owning it, the
    same capability model as Blossom GET). `?thumb=1` returns a small JPEG to save bandwidth."""
    from app.services import artifact_store
    pk = nostr_service.to_pubkey_hex(npub)
    if not pk:
        return JSONResponse({"error": "invalid npub"}, status_code=400)
    # Ownership required: the sha256 alone is NOT authorisation (see _FILE_COOKIE above). The cookie
    # must prove the SAME key the file belongs to, so one user's proof can't read another's files.
    # Cookie first; ?t= is the fallback for contexts where a cross-origin cookie can't survive (the
    # APK against an .onion — plain http, so the Secure cookie is refused). Same token, same proof.
    _tok = request.cookies.get(_FILE_COOKIE, "") or request.query_params.get("t", "")
    if _file_cookie_pubkey(_tok) != pk:
        return JSONResponse({"error": "not authorized"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    m = re.match(r'^enc_([0-9a-fA-F]{64})\.(\w+)$', name or "")
    if not user or not m:
        return JSONResponse({"error": "not found"}, status_code=404)
    data = await artifact_store.read_bytes(db, user, m.group(1).lower())
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    from mimetypes import guess_type as _gt
    ct = _gt("x." + m.group(2))[0] or "application/octet-stream"
    if request.query_params.get("thumb") and ct.startswith("image/"):
        try:
            from app.services.media_service import compress_image
            data = compress_image(data, max_dimension=320, quality=70)
            ct = "image/jpeg"
        except Exception:
            pass
    disp = "inline" if ct.startswith(("image/", "video/", "audio/")) else f'attachment; filename="{name}"'
    return Response(content=data, media_type=ct,
                    headers={"Content-Disposition": disp, "Cache-Control": "private, max-age=86400"})


@router.post("/ai-file-delete")
async def ai_file_delete(data: AiFileReq, db: Session = Depends(get_db)):
    """Delete one AI file by sha (the user's own, signed). Removes BOTH the encrypted blob bytes AND
    the doc reference that `ai_files` lists from — otherwise the card keeps showing (now a 404), which
    is the "I deleted it and it still shows" bug. Uploads live as their own NS_UPLOAD doc (delete it);
    generated images are referenced inside an NS_MSG chat record (clear that record's image_path)."""
    from app.services import nostr_store as store
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk or not re.fullmatch(r'[0-9a-f]{64}', data.sha or ''):
        return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    from app.services import artifact_store
    await artifact_store.delete_blob(db, data.sha)
    # Drop the listing reference so the file actually disappears from the Files view.
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if user:
        sk = store.user_storage_seckey(db, user)
        port = int(_setting(db, "nostr_relay_port", "3052"))
        for d, ref in (await store.list_docs(port, store.NS_UPLOAD, seckey=sk)).items():
            if isinstance(ref, dict) and ref.get("sha256") == data.sha:
                await store.delete_doc(port, sk, d)
        for d, rec in (await store.list_docs(port, store.NS_MSG, seckey=sk)).items():
            if isinstance(rec, dict) and data.sha in (rec.get("image_path") or ""):
                rec = {k: v for k, v in rec.items() if k != "image_path"}
                await store.put_doc(port, sk, d, rec)
    return JSONResponse({"ok": True})


# ----- drafts: synced across devices as ONE encrypted doc under the user's storage key -----
class DraftsReq(BaseModel):
    pubkey: str
    auth: str
    drafts: list | None = None   # present → save the list; absent → load


def _cap_drafts(entries) -> list:
    """DATA SAFETY: keep EVERY live draft, only bound tombstones. The old `sorted(...)[:300]` dropped the
    oldest entries by timestamp — so a burst of freshly-stamped tombstones (a delete storm / autosave
    churn) evicted real drafts out of the cap and lost them, with no older relay copy to recover from
    (kind-30078 is replaceable). A tombstone only needs to survive long enough to propagate its delete, so
    a bounded recent set is plenty; a live draft is NEVER dropped in favour of one. Mirrors Drafts._cap on
    the client."""
    live = [d for d in entries if isinstance(d, dict) and d.get("id") and not d.get("del")]
    tomb = [d for d in entries if isinstance(d, dict) and d.get("id") and d.get("del")]
    live.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    tomb.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return live[:2000] + tomb[:120]


@router.post("/drafts")
async def drafts_sync(data: DraftsReq, db: Session = Depends(get_db)):
    """Save (when `drafts` provided) or load the user's drafts — stored as one encrypted doc under
    their server-held storage key, so drafts written on one device show up on another."""
    from app.services import nostr_store as store
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": True, "drafts": []})
    sk = store.user_storage_seckey(db, user)
    port = int(_setting(db, "nostr_relay_port", "3052"))
    if data.drafts is not None:
        # MERGE with the stored doc — never blind-overwrite. The doc is ONE replaceable kind-30078
        # event (last-write-wins), so a second device pushing its own list would otherwise clobber
        # drafts only the first device had ("draft shows on phone, not desktop"). Union by id, newest
        # ts wins; a del:true tombstone with a fresh ts makes a deletion win. (Mirrors the client merge
        # in Drafts.pull, and the replaceable-list-wipe fix pattern.)
        existing = await store.get_doc(port, "pcai:drafts", seckey=sk)
        prev = existing.get("drafts", []) if isinstance(existing, dict) else []
        merged: dict = {}
        for d in [*(prev if isinstance(prev, list) else []), *data.drafts]:
            if isinstance(d, dict) and d.get("id"):
                cur = merged.get(d["id"])
                if cur is None or (d.get("ts") or 0) >= (cur.get("ts") or 0):
                    merged[d["id"]] = d
        out = _cap_drafts(merged.values())
        await store.put_doc(port, sk, "pcai:drafts", {"drafts": out})
        return JSONResponse({"ok": True, "drafts": out})
    doc = await store.get_doc(port, "pcai:drafts", seckey=sk)
    drafts = doc.get("drafts", []) if isinstance(doc, dict) else []
    return JSONResponse({"ok": True, "drafts": drafts if isinstance(drafts, list) else []})


# ----- Scheduled posts: publish a pre-signed note at a future time -----
class ScheduledCreateReq(BaseModel):
    pubkey: str
    auth: str
    event: dict          # a full, client-SIGNED Nostr event (created_at = the scheduled time)
    scheduled_at: int    # unix seconds; when to publish (must match the event's created_at)


class ScheduledAuthReq(BaseModel):
    pubkey: str
    auth: str


class ScheduledCancelReq(BaseModel):
    pubkey: str
    auth: str
    id: int


_MAX_PENDING_SCHEDULES = 100   # per-user cap (abuse guard)


@router.post("/scheduled")
async def scheduled_create(data: ScheduledCreateReq, db: Session = Depends(get_db)):
    """Store a pre-signed note to publish later. The server never signs it — the client already did,
    with created_at = the scheduled time — so this only validates ownership + the event, then queues it."""
    from app.services import scheduled_posts_service as sched
    from app.models import ScheduledPost
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    # Validate the payload BEFORE the account lookup (fail fast on a bad event).
    ev = data.event or {}
    # Must be a real, signed post authored by THIS user (we broadcast it verbatim later). Allow the
    # top-level post kinds the composer can schedule: kind 1 (text/image notes) and kind 1068 (polls).
    if ev.get("kind") not in (1, 1068):
        return JSONResponse({"ok": False, "error": "only text/image notes and polls can be scheduled"}, status_code=400)
    if ev.get("pubkey") != pk or not nostr_event.verify_event(ev):
        return JSONResponse({"ok": False, "error": "event must be validly signed by you"}, status_code=400)
    now = int(time.time())
    when = int(data.scheduled_at)
    if when < now - 60:
        return JSONResponse({"ok": False, "error": "scheduled time is in the past"}, status_code=400)
    if when > now + sched._MAX_FUTURE_DAYS * 86400:
        return JSONResponse({"ok": False, "error": "scheduled time is too far in the future"}, status_code=400)
    # created_at IS what the published note will show as its time — require it to MATCH the schedule
    # (the client signs with created_at = scheduled time). A tight bound also keeps the broadcast event
    # from being future-dated (which strict upstream relays reject) once it's published at its due time.
    if abs(int(ev.get("created_at", 0)) - when) > 120:
        return JSONResponse({"ok": False, "error": "event time must match the scheduled time"}, status_code=400)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=403)
    pending = (db.query(ScheduledPost)
               .filter(ScheduledPost.user_id == user.id,
                       ScheduledPost.status.in_(("pending", "sending"))).count())
    if pending >= _MAX_PENDING_SCHEDULES:
        return JSONResponse({"ok": False, "error": f"too many scheduled posts (max {_MAX_PENDING_SCHEDULES})"}, status_code=429)
    row = sched.create(db, user, ev, datetime.utcfromtimestamp(when))
    return JSONResponse({"ok": True, "id": row.id, "scheduled_at": when})


@router.post("/scheduled/list")
async def scheduled_list(data: ScheduledAuthReq, db: Session = Depends(get_db)):
    from app.services import scheduled_posts_service as sched
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": True, "posts": []})
    return JSONResponse({"ok": True, "posts": sched.list_for_user(db, user)})


@router.post("/scheduled/cancel")
async def scheduled_cancel(data: ScheduledCancelReq, db: Session = Depends(get_db)):
    from app.services import scheduled_posts_service as sched
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=403)
    ok = sched.cancel(db, user, int(data.id))
    return JSONResponse({"ok": ok, "error": None if ok else "already sending or sent"})


# ----- Files folder index: folder tree + per-file metadata (name/folder), one encrypted doc -----
_FILES_INDEX_BAKS = 5      # how many replaced index versions to keep (see _files_index_backup)
_FILES_INDEX_BAK_DAYS = 30  # how long a superseded index BLOB stays recoverable before the sweep takes it


class FilesIndexReq(BaseModel):
    pubkey: str
    auth: str
    index: dict | None = None   # present → save; absent → load
    force: bool = False         # override the collapse guard — a deliberate mass-delete


@router.post("/files-index")
async def files_index(data: FilesIndexReq, db: Session = Depends(get_db)):
    """Save/load the Files folder index — the folder tree + each file's {name, folder, store, key…},
    stored as ONE encrypted doc under the user's storage key (cross-device, survives reinstalls). The
    blobs themselves live in Blossom; this is just the foldering/metadata overlay (+ wrapped keys for
    encrypted Music tracks)."""
    from app.services import nostr_store as store
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": True, "index": {}})
    sk = store.user_storage_seckey(db, user)
    port = int(_setting(db, "nostr_relay_port", "3052"))
    if data.index is not None:
        # Read the CURRENT index first — strict, so an unreachable relay raises instead of looking
        # like "there was nothing there", which is the read that makes a destructive write look safe.
        try:
            prev = await store.get_doc(port, "pcai:files-index", seckey=sk, strict=True)
        except Exception as e:
            logger.warning("[client] files-index: cannot read current index, refusing to write: %s", e)
            return JSONResponse({"ok": False, "error": "index unavailable, not saved"}, status_code=503)

        drop = _files_index_collapse(prev, data.index)
        if drop and not data.force:
            # THE invariant. Everything else protecting this document lives in client code, and the
            # server cannot choose which build a device runs — a stale bundle, an old APK or a
            # third-party client can all still send an empty index over a full one. That is exactly
            # how a drive lost 417 filenames and folders. Refusing here covers every client that will
            # ever exist. A genuine mass-delete re-sends with force=true.
            logger.warning("[client] files-index: REFUSED a collapsing write for %s (%s)", pk[:12], drop)
            # The client uploads the index BLOB before sending this pointer, so a refusal leaves that
            # blob referenced by nothing. Give it a short TTL: it is reclaimed if the save is never
            # retried, and un-expired below if the same bytes are later accepted.
            in_use = await _index_shas_in_use(store, port, sk, prev)
            cand = data.index.get("indexSha")
            if in_use is not None and cand not in in_use:
                _expire_unreferenced_index(db, cand, 7)
            return JSONResponse({"ok": False, "error": "refused: " + drop, "collapse": True},
                                status_code=409)

        # Keep the outgoing version before replacing it. kind 30078 is replaceable, so without this
        # every overwrite is final and NOTHING — not this bug, not a bad bulk-move, not a fat-fingered
        # folder delete — is undoable.
        evicted_sha = None
        if isinstance(prev, dict) and prev:
            old_n, new_n = _files_index_count(prev), _files_index_count(data.index)
            shrinking = old_n is not None and new_n is not None and new_n < old_n
            evicted_sha = await _files_index_backup(store, port, sk, prev, shrinking)

        # CHECK THE WRITE. put_doc returns False when the relay rejects or never acks it; answering
        # {"ok": true} regardless told the client to clear its dirty flag and drop the edit — reporting
        # success for a write that did not happen, which is the same failure this endpoint now exists
        # to prevent on the client's side.
        if not await store.put_doc(port, sk, "pcai:files-index", data.index):
            logger.warning("[client] files-index: relay REJECTED the write for %s", pk[:12])
            in_use = await _index_shas_in_use(store, port, sk, prev)
            cand = data.index.get("indexSha")
            if in_use is not None and cand not in in_use:
                _expire_unreferenced_index(db, cand, 7)
            return JSONResponse({"ok": False, "error": "relay rejected the write, not saved"},
                                status_code=503)

        # This index is now LIVE, so make sure it carries no expiry from an earlier refused attempt
        # at the same bytes — otherwise a retry that finally succeeds still gets swept later.
        _expire_unreferenced_index(db, data.index.get("indexSha"), 0)

        # Age out only the index blob that just fell OUT of backup retention. A merely superseded
        # blob is still referenced by the backup written above, and expiring that one left the version
        # history pointing at bytes due for deletion — a backup that silently stops being restorable.
        try:
            live = {data.index.get("indexSha")}
            if isinstance(prev, dict):
                live.add(prev.get("indexSha"))          # still referenced by the backup just written
            if evicted_sha and evicted_sha not in live:
                from app.services import blossom_service
                blossom_service.expire_blob_in(db, evicted_sha, _FILES_INDEX_BAK_DAYS)
        except Exception as e:
            logger.debug("[client] files-index: could not age out the old index blob: %s", e)
        return JSONResponse({"ok": True})
    doc = await store.get_doc(port, "pcai:files-index", seckey=sk)
    return JSONResponse({"ok": True, "index": doc if isinstance(doc, dict) else {}})


async def _index_shas_in_use(store, port: int, sk: bytes, prev) -> set:
    """Every index blob sha currently referenced by the LIVE doc or by a retained backup slot.

    A refused save's blob is normally unreferenced garbage — but if the user reverted to an earlier
    state, the identical bytes may be exactly what a backup slot points at. Expiring that would
    schedule deletion of a version the history still promises, which is the same defect as expiring a
    blob the moment it was superseded. Cheap: the slots are five known d-tags."""
    used = set()
    if isinstance(prev, dict) and prev.get("indexSha"):
        used.add(prev["indexSha"])
    try:
        for i in range(1, _FILES_INDEX_BAKS + 1):
            # strict: a relay failure must RAISE, not read as "this slot references nothing" —
            # that empty answer would let the caller expire a blob a backup still needs.
            doc = await store.get_doc(port, f"pcai:files-index-bak:{i}", seckey=sk, strict=True)
            if isinstance(doc, dict) and doc.get("indexSha"):
                used.add(doc["indexSha"])
    except Exception as e:
        logger.debug("[client] files-index: could not read backup slots: %s", e)
        return None          # unknown → the caller must not expire anything
    return used


def _expire_unreferenced_index(db, sha: str | None, days: int) -> None:
    """Give an index blob a short TTL (days>0) or clear one (days=0). Best-effort, never raises.

    The client uploads the encrypted index blob BEFORE the server has agreed to store the pointer, so
    a refused or rejected write leaves that blob referenced by nothing at all — and since the client
    no longer deletes blobs and the global age sweep is off, it would sit there forever. The server
    knows the sha from the very pointer it rejected, which makes this precise rather than a guess."""
    if not sha:
        return
    try:
        from app.services import blossom_service
        if days > 0:
            blossom_service.expire_blob_in(db, sha, days)
        else:
            blossom_service.clear_blob_expiry(db, sha)
    except Exception as e:
        logger.debug("[client] files-index: TTL touch on %s failed: %s", str(sha)[:12], e)


def _files_index_count(doc) -> int | None:
    """How many entries an index doc holds, or None when that can't be known.

    Two shapes: an INLINE index (`files` dict, small drives) and a POINTER to an encrypted Blossom
    blob (`indexSha`, which the server cannot read). Clients stamp a plaintext `n` on both so the
    pointer form is still comparable; a pointer from an older client without `n` returns None."""
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("n"), int):
        return doc["n"]
    if isinstance(doc.get("files"), dict):
        return len(doc["files"])
    return None


def _files_index_collapse(prev, new) -> str | None:
    """Describe why `new` looks like it would destroy `prev`, or None if the write is safe."""
    if not isinstance(prev, dict) or not prev:
        return None                                  # nothing to lose yet
    old_n, new_n = _files_index_count(prev), _files_index_count(new)
    if old_n is not None and new_n is not None:
        if old_n >= 10 and new_n < old_n // 2:
            return f"{old_n} entries -> {new_n}"
        return None
    # An unmeasurable POINTER being replaced by a tiny INLINE index is the wipe signature itself: the
    # index only moves to a blob once it exceeds ~45 KB, i.e. hundreds of files. A handful of inline
    # entries cannot legitimately be that same drive.
    if prev.get("indexSha") and isinstance(new, dict) and not new.get("indexSha"):
        n = len(new.get("files") or {})
        if n < 20:
            return f"large stored index -> {n} entries"
    return None


async def _files_index_backup(store, port: int, sk: bytes, prev: dict, shrinking: bool):
    """Keep the replaced index in one of _FILES_INDEX_BAKS fixed slots, overwriting the oldest.

    FIXED SLOTS, not timestamped d-tags. A timestamped scheme has to enumerate what exists to prune
    it, and the only listing available walks EVERY kind-30078 doc the storage key owns — chats, mail
    and budget share that namespace, so the cost grows with the account and lands on the save path.
    Five known d-tags are one bounded query, one write, and no deletes (30078 is replaceable).

    `shrinking` writes a backup unconditionally — losing entries is exactly when an undo is wanted.
    Growing saves (the overwhelming majority: every upload) back up at most hourly, so a 400-file
    import doesn't burn all five slots on near-identical copies and push the pre-import state out.

    Best-effort: a backup that fails must not block the user's save."""
    import time as _t
    from app.services.nostr import bip340
    try:
        pk = bip340.pubkey_from_seckey(sk).hex()
        slots = [f"pcai:files-index-bak:{i}" for i in range(1, _FILES_INDEX_BAKS + 1)]
        evs = await store._ws_query(port, [{"authors": [pk], "kinds": [store.APP_KIND],
                                            "#d": slots, "limit": _FILES_INDEX_BAKS}])
        seen = {}
        for ev in evs:
            d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), None)
            if d in slots:
                seen[d] = max(seen.get(d, 0), int(ev.get("created_at") or 0))
        free = [s for s in slots if s not in seen]
        if not free and not shrinking and seen and (int(_t.time()) - min(seen.values())) < 3600:
            return                                   # a recent backup already covers this state
        target = free[0] if free else min(seen, key=seen.get)
        # Whatever this slot held is about to fall out of retention. Its index BLOB is what becomes
        # reclaimable — and only now. Expiring a blob the moment it was superseded (what this did at
        # first) leaves the backup that references it pointing at bytes that get deleted underneath
        # it: a version history that silently stops being restorable, which is the exact failure this
        # whole series exists to stop. Return it so the caller can age it out.
        evicted = None
        if target not in free:
            try:
                old_doc = await store.get_doc(port, target, seckey=sk)
                if isinstance(old_doc, dict):
                    evicted = old_doc.get("indexSha")
            except Exception:
                evicted = None
        await store.put_doc(port, sk, target, prev)
        return evicted
    except Exception as e:
        logger.warning("[client] files-index: backup failed (save continues): %s", e)
    return None


# ----- music: transcode an upload to Opus (compression) — client then encrypts + uploads to Blossom -----
@router.post("/music-compress")
async def music_compress(request: Request, db: Session = Depends(get_db)):
    """Transcode an uploaded audio file to Opus (~96 kbps) to save bandwidth. Stateless — returns the
    compressed bytes; the client encrypts them and uploads the ciphertext to its own Blossom. Gated by
    a self-signed Nostr auth (any signed-in user) so the transcode CPU isn't an anonymous abuse surface."""
    pk = nostr_service.to_pubkey_hex(request.headers.get("x-pubkey", ""))
    if not pk or not _verify_self_auth(request.headers.get("x-auth", ""), pk):
        return JSONResponse({"error": "auth required"}, status_code=403)
    data = await request.body()
    if not data:
        return JSONResponse({"error": "empty"}, status_code=400)
    if len(data) > 300 * 1024 * 1024:
        return JSONResponse({"error": "too large (max 300 MB)"}, status_code=413)
    from app.services.media_service import compress_audio_opus
    try:
        out = await asyncio.to_thread(compress_audio_opus, data)
    except Exception as e:
        return JSONResponse({"error": f"transcode failed: {e}"}, status_code=500)
    return Response(content=out, media_type="audio/ogg",
                    headers={"Cache-Control": "no-store"})


# ----- delete my account (the AI app account + all its data) -----
class DeleteAccountReq(BaseModel):
    pubkey: str
    auth: str            # base64 signed event BY this pubkey (proves ownership)


@router.post("/delete-account")
async def delete_account(data: DeleteAccountReq, db: Session = Depends(get_db)):
    """Delete the user's PosterChan account: their conversations + messages (app.db), their encrypted
    relay chat events + uploads + Blossom blobs, their storage key, and the account row. (Does not,
    and cannot, delete their Nostr identity globally — only their data on this node.)"""
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    user = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pk)).first()
    if not user:
        return JSONResponse({"ok": True, "already": True})
    if user.is_admin:
        return JSONResponse({"ok": False, "error": "admin accounts can't self-delete (use Admin → Users)"}, status_code=400)
    from app.models import Conversation, Message, UserSetting
    from app.services import chat_store, upload_store
    convs = db.query(Conversation).filter(Conversation.user_id == user.id).all()
    for c in convs:
        try:
            await chat_store.delete_conversation(db, user, c.id)   # relay msg events + artifact blobs
            await upload_store.delete_uploads(db, user, c.id)      # upload blobs + refs
        except Exception as e:
            logger.warning("[client] delete-account relay purge (conv %s) failed: %s", c.id, e)
        db.query(Message).filter(Message.conversation_id == c.id).delete()
        db.delete(c)
    db.query(UserSetting).filter(UserSetting.user_id == user.id).delete()
    npub = user.nostr_npub   # capture before delete — needed to remove the relay account docs
    db.delete(user)
    db.commit()
    try:
        from app.services import users_store
        await users_store.delete_user(db, npub)   # remove account docs so a rebuild won't resurrect it
    except Exception as e:
        logger.warning("[client] account-delete relay doc removal failed: %s", e)
    logger.info("[client] account deleted: %s", pk[:16])
    return JSONResponse({"ok": True})


# ----- sync my posts to this relay (backfill the user's own Nostr history) -----
class SyncPostsReq(BaseModel):
    pubkey: str
    auth: str            # base64 signed event BY this pubkey (proves ownership)


@router.post("/sync-posts")
async def sync_posts(data: SyncPostsReq, db: Session = Depends(get_db)):
    """Pull the user's OWN Nostr history (from the upstream relays) into this relay's store, so their
    posts from other clients show up here. (Merged from the old 'sync my posts to the relay' setting.)"""
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    try:
        from app.services.nostr_relay.thread import trigger_backfill
        trigger_backfill(pk)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})


# ----- auto NIP-05 name on signup -----
class ClaimNip05(BaseModel):
    pubkey: str
    name: str = ""
    auth: str            # base64 signed event BY this pubkey (proves key ownership → no squatting)


class AdminNip05Req(BaseModel):
    target: str          # npub/hex to grant/remove a NIP-05 name for
    name: str = ""       # the local-part to grant (ignored when remove=True)
    remove: bool = False
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


class MemeRenderReq(BaseModel):
    pubkey: str
    auth: str            # base64 signed kind-27235 by this pubkey — same self-proof as drafts/ai-files
    edit: dict           # the timeline: {w,h,fps,duration,bg,layers:[…]} (see meme_builder_service)


# ----- Meme Builder: render a layered timeline into one MP4 -----
# The client edits on a canvas and posts the edit list here; ffmpeg composites it (meme_builder_service).
# Pubkeys with a render in flight — one at a time per user, so repeat Render clicks can't stack N ffmpegs
# (each slower than the last) and leave the UI stuck on "rendering…". Per-process is correct here: the
# renders run on this single port-3051 worker.
_meme_rendering: set = set()
# ...and a QUEUE across all users: ffmpeg render is CPU-heavy, so let a couple run and make the rest WAIT
# their turn rather than starting N at once and starving the box (and every other request on this single
# worker). Waiters are bounded by _MEME_QUEUE_WAIT_S so a request can't hang forever behind a stuck render.
_MEME_MAX_CONCURRENT = 2
_MEME_QUEUE_WAIT_S = 180
_meme_slots: asyncio.Semaphore | None = None


def _meme_semaphore() -> asyncio.Semaphore:
    """Created lazily so it binds to the running event loop, not import time."""
    global _meme_slots
    if _meme_slots is None:
        _meme_slots = asyncio.Semaphore(_MEME_MAX_CONCURRENT)
    return _meme_slots


_meme_rr = [0]   # round-robin cursor over peer nodes for render overflow
_meme_busy = [0]  # local render jobs holding a slot right now — what the LB reads to decide "am I busy?"


@contextlib.asynccontextmanager
async def _meme_slot():
    """Hold one of this node's _MEME_MAX_CONCURRENT ffmpeg render slots for the duration of the block
    (503 if the queue doesn't drain within _MEME_QUEUE_WAIT_S). Also maintains `_meme_busy`, the count
    the overflow LB reads: the semaphore's own free-slot count is private, and `locked()` only goes True
    when EVERY slot is taken, which is a far rarer condition than "this node is already rendering"."""
    try:
        await asyncio.wait_for(_meme_semaphore().acquire(), timeout=_MEME_QUEUE_WAIT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="the render queue is busy — try again in a moment")
    _meme_busy[0] += 1
    try:
        yield
    finally:
        _meme_busy[0] -= 1
        _meme_semaphore().release()


_MEME_BLOB_RE = re.compile(r"/([0-9a-f]{64})(\.[A-Za-z0-9]{1,8})?(?:[?#]|$)")


async def _meme_adopt_peer_blob(db: Session, peer: str, payload: dict) -> bool:
    """Copy a peer-rendered blob into THIS node's Blossom store, so the URL the peer handed back
    actually resolves. Nodes share the public Blossom base URL (`blossom_public_url`) but each has
    its OWN Postgres: a blob saved on the peer has no BlossomBlob row here, and /blossom/<sha> is a
    row lookup — so without this copy the URL 404s and the layer silently breaks. Content-addressed,
    so re-saving the same bytes locally yields the SAME sha and the peer's URL stays correct."""
    from app.services import blossom_service
    url = (payload or {}).get("url") or ""
    m = _MEME_BLOB_RE.search(url)
    if not m:
        return False
    sha, ext = m.group(1), (m.group(2) or "")
    if blossom_service.get_blob_meta(db, sha) is not None:
        return True   # already here (dedup / shared DB) — nothing to copy
    import httpx
    # Fetch from the PEER directly, not from the shared public base: that base points at whichever
    # node serves media, which is exactly the node that doesn't have the blob yet.
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=8.0)) as c:
        r = await c.get("%s/blossom/%s%s" % (peer.rstrip("/"), sha, ext))
        r.raise_for_status()
        data = r.content
    if not data:
        return False
    mime = (r.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
    pk = nostr_service.to_pubkey_hex((payload or {}).get("pubkey") or "") or ""
    desc = await blossom_service.save_blob(db, pk or sha, data, mime)
    return desc.get("sha256") == sha


async def _meme_store_peer_media(request: "Request", db: Session, body: dict, subpath: str, r):
    """Store an effect rendered BY A PEER into this node's Blossom and return the JSON the client
    expects. The peer sends raw media plus x-pcai-effect-* headers precisely so that it never needs a
    blob store of its own — the node holding the user's request owns the storage. Returns None if the
    bytes are unusable, so the caller can fall back to rendering locally."""
    from app.services import blossom_service
    data = r.content
    if not data:
        return None
    pk = nostr_service.to_pubkey_hex((body or {}).get("pubkey") or "")
    if not pk:
        return None
    ct = (r.headers.get("content-type") or "video/mp4").split(";")[0].strip()
    ext = "webm" if "webm" in ct else ("mp4" if "mp4" in ct else (ct.split("/")[-1] or "mp4"))
    desc = await blossom_service.save_blob(db, pk, data, ct)
    sha = desc.get("sha256")
    if not sha:
        return None
    url = "%s/%s.%s" % (_blossom_url(request, db), sha, ext)
    try:
        dur = round(float(r.headers.get("x-pcai-effect-dur") or 0), 2)
    except (TypeError, ValueError):
        dur = 0.0
    name = r.headers.get("x-pcai-effect-name") or ""
    if subpath == "effect":
        audio = (r.headers.get("x-pcai-effect-audio") or "0") == "1"
        return JSONResponse({"ok": True, "url": url, "audio": audio,
                             "sound": name if audio else None, "name": name, "dur": dur})
    return JSONResponse({"ok": True, "url": url, "dur": dur, "effect": name,
                         "is_video": ct.startswith("video/")})


async def _meme_lb_forward(request: "Request", subpath: str, body: dict, db: Session | None = None):
    """Node load balancer for meme/effect RENDER jobs: hand the job to a peer (round-robin) and return
    its Response, so renders spread across the fleet instead of piling onto one box (this is the
    ffmpeg-render analogue of the chat/image node LB). Returns None to run LOCALLY: a request already
    forwarded to us (loop guard header), or no peers at all. Any peer error falls through to local.

    Scheduling is ROUND-ROBIN over [this node] + peers, the same policy image generation uses — not
    "overflow only". Two earlier gates each looked correct and each left every job on one box:
    `locked()` (all _MEME_MAX_CONCURRENT slots full) is nearly unreachable, since a pubkey may hold
    only one timeline render (`_meme_rendering`) and may start an effect only every
    _EFFECT_COOLDOWN_S; and "am I rendering right now" only fires when jobs happen to overlap, so
    ordinary click-wait-click editing never leaves the local node. Rotating unconditionally is what
    actually spreads the fleet. Local keeps its turn in the rotation (no hop, no blob copy), and
    gives that turn up when it is already rendering and a peer could take the job instead."""
    try:
        if request is not None and request.headers.get("x-pcai-meme-fwd"):
            return None   # already a forwarded job — run it here, never re-forward (loop guard)
        from app.services import settings_store, video_factory
        peers = video_factory.parse_video_server_urls(settings_store.get("chat_server_urls", "") or "")
        if not peers:
            return None
        import httpx
        # One slot per node in the rotation: 0 = local, 1..N = peers.
        turn = _meme_rr[0] % (len(peers) + 1); _meme_rr[0] += 1
        if turn == 0 and _meme_busy[0] <= 0:
            return None   # local's turn and local is free → run here (cheapest path)
        start = (turn - 1) if turn > 0 else 0
        for k in range(len(peers)):
            peer = peers[(start + k) % len(peers)].rstrip("/")
            url = "%s/client/meme/%s" % (peer, subpath)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=8.0)) as c:
                    r = await c.post(url, json=body, headers={"x-pcai-meme-fwd": "1"})
                if r.status_code >= 500:
                    continue   # peer busy/broke → try the next, else local
                logger.info("[meme] render forwarded to %s (%s) -> %d", peer, subpath, r.status_code)
                ct = r.headers.get("content-type", "") or ""
                # An effect peer answers with RAW MEDIA (it has ffmpeg, not necessarily a blob store):
                # store it here, under this node's Blossom, and build the URL the client expects.
                if subpath in ("effect", "apply-effect") and r.status_code < 400 \
                        and not ct.startswith("application/json") and db is not None:
                    stored = await _meme_store_peer_media(request, db, body, subpath, r)
                    if stored is None:
                        logger.warning("[meme] could not store %s media from %s — rendering locally",
                                       subpath, peer)
                        return None
                    return stored
                if ct.startswith("application/json"):
                    payload = r.json()
                    # The endpoints that answer with a Blossom URL (effect / apply-effect) stored their
                    # output in the PEER's blob store. Adopt it here or the URL is dead on arrival; if the
                    # copy fails, fall through to local rather than hand back a 404 link.
                    if r.status_code < 400 and isinstance(payload, dict) and payload.get("url") and db is not None:
                        try:
                            if not await _meme_adopt_peer_blob(db, peer, {**payload, "pubkey": body.get("pubkey")}):
                                logger.warning("[meme] peer %s blob adopt failed (%s) — rendering locally", peer, subpath)
                                return None
                        except Exception as e:
                            logger.warning("[meme] peer %s blob adopt error (%s) — rendering locally: %s",
                                           peer, subpath, e)
                            return None
                    return JSONResponse(payload, status_code=r.status_code)
                return Response(content=r.content, status_code=r.status_code,
                                media_type=ct or "application/octet-stream")
            except Exception as e:
                logger.info("[meme] peer %s failed (%s) — trying next/local: %s", peer, subpath, e)
                continue
    except Exception as e:
        logger.debug("[meme] lb forward skipped: %s", e)
    return None
# Layer sources are URLs the client already has (Blossom blobs it uploaded, or media already on the
# timeline), which is why this only ever FETCHES — it never accepts uploaded bytes, so there is no new
# upload surface. Every fetch goes through the rss_service SSRF guard, the same one the fediverse bridge
# uses: without it an edit list would be a fully general "make the server request this URL" primitive
# against the LAN.
@router.post("/meme/render")
async def meme_render(data: MemeRenderReq, request: Request, db: Session = Depends(get_db)):
    import tempfile
    import httpx
    from urllib.parse import urlparse
    from app.services import meme_builder_service
    from app.services.rss_service import looks_fetchable, is_safe_host

    pk = nostr_service.to_pubkey_hex(data.pubkey or "")
    if not pk or not _verify_self_auth(data.auth, pk):
        raise HTTPException(status_code=401, detail="bad auth")

    # Busy-overflow LB: if this node's render queue is full, run the whole project on a peer node and
    # stream its MP4 back — so several memes render across the fleet at once (the ffmpeg analogue of the
    # chat/image node LB). Loop-guarded by the x-pcai-meme-fwd header.
    _fwd = await _meme_lb_forward(request, "render",
                                  {"pubkey": data.pubkey, "auth": data.auth, "edit": data.edit})
    if _fwd is not None:
        return _fwd

    # ONE render at a time per user. Without this every extra Render click spawned ANOTHER full ffmpeg of the
    # same project — they pile up, each one slower than the last (software x264 when VAAPI is unavailable),
    # and the UI just sits on "rendering…" forever. Reject the duplicate instead of stacking it.
    if pk in _meme_rendering:
        raise HTTPException(status_code=429, detail="a render is already running — wait for it to finish")

    layers = (data.edit or {}).get("layers") or []
    urls = {str(l.get("src")) for l in layers if isinstance(l, dict) and l.get("src")
            and (l.get("type") or "") != "text"}
    if len(urls) > meme_builder_service.MAX_LAYERS:
        raise HTTPException(status_code=400, detail="too many distinct sources")

    # OUR OWN media hosts are exempt from the SSRF guard, and have to be: on this deployment
    # poster.place and media.poster.place resolve to 192.168.0.1 from inside the LAN (split-horizon
    # DNS), so is_safe_host rejects them as private — which would refuse every Blossom blob the user
    # just uploaded and make the feature fail 100% of the time. These are URLs this node itself mints
    # and serves, so fetching them is not an SSRF primitive. Everything else still goes through the guard.
    own = set()
    for key in ("blossom_public_url", "nostr_dvm_blossom_url"):
        h = urlparse(_setting(db, key) or "").hostname
        if h:
            own.add(h.lower())

    tmpdir = tempfile.mkdtemp(prefix="pcmemesrc-")
    sources: dict = {}
    _meme_rendering.add(pk)   # released in the finally below — paired so a failure can never wedge the user out
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0), follow_redirects=False) as c:
            for u in urls:
                host = (urlparse(u).hostname or "").lower()
                if urlparse(u).scheme not in ("http", "https"):
                    raise HTTPException(status_code=400, detail=f"refused source: {u[:80]}")
                if host not in own:
                    # is_safe_host resolves DNS (blocking) — off the event loop, per its own docstring.
                    if not looks_fetchable(u) or not await asyncio.to_thread(is_safe_host, u):
                        raise HTTPException(status_code=400, detail=f"refused source: {u[:80]}")
                try:
                    r = await c.get(u)
                    r.raise_for_status()
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"could not fetch a layer source: {e}")
                # 80 MB per source: a phone-shot clip fits, a film does not. Renders share the box with
                # chat and image gen, so this is a real resource bound, not a formality.
                if len(r.content) > 80 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail="a layer source is too large (80 MB limit)")
                # KEEP the source's extension on the temp file. The renderer decides how to decode a layer
                # partly by extension — a VP9-alpha .webm effect layer MUST be decoded with libvpx-vp9 or its
                # alpha is dropped and the overlay renders INVISIBLE (the "effect layer is audio-only" bug).
                # A hash-only filename hid that from the renderer.
                _base = u.split("?", 1)[0].split("#", 1)[0]
                _ext = os.path.splitext(_base)[1].lower()
                if len(_ext) > 6 or not _ext[1:].isalnum():
                    _ext = ""
                p = os.path.join(tmpdir, hashlib.sha256(u.encode()).hexdigest()[:24] + _ext)
                with open(p, "wb") as fh:
                    fh.write(r.content)
                sources[u] = p
        try:
            # Blocking ffmpeg → a thread, or it stalls the whole event loop (every other request on this
            # single-worker process) for the length of the render. Take a QUEUE slot first so only
            # _MEME_MAX_CONCURRENT renders run at once — the rest wait here instead of all piling onto the
            # CPU together. Bounded, so a wedged render can't leave this request hanging indefinitely.
            async with _meme_slot():
                out = await asyncio.to_thread(meme_builder_service.render, data.edit, sources)
                # Same auto-compress the effects get: a Meme Builder timeline over a phone-camera
                # layer renders tens of MB, and this MP4 goes straight into a post/upload.
                from app.services import media_service as _ms
                out = (await asyncio.to_thread(_ms.compress_effect_outputs,
                                               [{"filename": "meme.mp4", "data": out,
                                                 "content_type": "video/mp4"}]))[0]["data"]
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            logger.warning("[meme] render failed for %s: %s", pk[:12], e)
            raise HTTPException(status_code=500, detail=str(e))
        # Public stats counter (Server Stats page): count a PRODUCED meme, not an attempt — this sits
        # after the render so a 400/500 never inflates it. The MP4 is streamed straight back and no row
        # is kept, so like image/music/video there is nothing to aggregate after the fact.
        try:
            from app.services import stats_service
            stats_service.bump("meme")
        except Exception:
            pass
        return Response(content=out, media_type="video/mp4",
                        headers={"Content-Disposition": 'attachment; filename="meme.mp4"'})
    finally:
        _meme_rendering.discard(pk)
        try:
            for f in os.listdir(tmpdir):
                os.unlink(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except Exception:
            pass


def _verify_self_auth(auth_b64: str, pubkey_hex: str) -> bool:
    """Verify a base64 signed Nostr event authored by `pubkey_hex` within the replay window."""
    try:
        ev = json.loads(base64.b64decode(auth_b64))
    except Exception:
        return False
    return (nostr_event.verify_event(ev) and ev.get("pubkey") == pubkey_hex
            and abs(int(ev.get("created_at", 0)) - int(time.time())) <= 300)


def _nip05_domain(request: Request, db: Session) -> str:
    """Domain for the assigned NIP-05 address (`name@<domain>`). The admin can pin it; otherwise
    use the host the client was served from (where /.well-known/nostr.json is proxied)."""
    d = _setting(db, "nostr_relay_nip05_domain").strip().lstrip("@")
    if d:
        return d.lower()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return (host or "").split(",")[0].split(":")[0].strip().lower()


def _sanitize_nip05_name(s: str) -> str:
    s = re.sub(r"[^a-z0-9_.\-]", "", (s or "").strip().lower())
    return s.strip("._-")[:30]


@router.post("/claim-nip05")
async def claim_nip05(data: ClaimNip05, request: Request, db: Session = Depends(get_db)):
    """Assign a fresh account a NIP-05 name on this node's identity server (Admin → Relay → NIP-05),
    so new web-client signups get a verified `name@domain` automatically. Idempotent per pubkey."""
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    from app.services.nostr_relay.thread import _parse_nip05
    raw = settings_store.get("nostr_relay_nip05_names", "") or ""
    names, _ = _parse_nip05(raw, "")
    domain = _nip05_domain(request, db)
    # Already named (re-signup / retry) → return the existing one, don't duplicate.
    existing = next((n for n, h in names.items() if h == pk), None)
    if existing:
        return JSONResponse({"ok": True, "name": existing, "nip05": f"{existing}@{domain}", "existing": True})
    base = _sanitize_nip05_name(data.name) or ("user" + pk[:8])
    name, taken = base, set(names.keys())
    i = 1
    while name in taken:
        i += 1
        name = f"{base}{i}"
        if i > 9999:
            name = "user" + pk[:12]
            break
    try:
        npub = nostr_service.npub_of(pk)
    except Exception:
        npub = pk
    new_line = f"{name} {npub}"
    value = (raw.rstrip() + "\n" + new_line) if raw.strip() else new_line
    settings_store.put("nostr_relay_nip05_names", value)
    try:
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception as e:
        logger.warning("[client] nip05 reload after claim failed: %s", e)
    return JSONResponse({"ok": True, "name": name, "nip05": f"{name}@{domain}"})


@router.get("/admin-nip05")
async def admin_nip05_status(pubkey: str, request: Request, db: Session = Depends(get_db)):
    """Current NIP-05 name granted to this pubkey on THIS node's identity server (if any). Lets the
    admin Permissions panel show the checkbox state + prefill the name. Public read (it's in nostr.json)."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(settings_store.get("nostr_relay_nip05_names", "") or "", "")
    cur = next((n for n, hx in names.items() if hx == h), None)
    domain = _nip05_domain(request, db)
    return JSONResponse({"ok": True, "name": cur, "nip05": (f"{cur}@{domain}" if cur else None)})


@router.post("/admin-nip05")
async def admin_nip05(data: AdminNip05Req, request: Request, db: Session = Depends(get_db)):
    """Admin-only: grant or remove a NIP-05 name for a target pubkey in this node's Relay Settings
    (`nostr_relay_nip05_names`). Gated by the same signed-admin proof as /block. Grant replaces any
    existing name for the target; collisions with a DIFFERENT account are rejected."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    from app.services.nostr_relay.thread import trigger_nip05_reload
    raw = settings_store.get("nostr_relay_nip05_names", "") or ""
    if not raw.strip():
        # An empty read may be the partial-hydrate window (relay subprocess not loaded yet), NOT a
        # genuinely empty list — re-read authoritatively before a write that would otherwise WIPE every
        # other user's grant (the documented replaceable-list / partial-hydrate data-loss class).
        try:
            settings_store.hydrate_from_db(db)
            raw = settings_store.get("nostr_relay_nip05_names", "") or ""
        except Exception:
            pass
    # MINIMAL edit on the raw text: drop only the target's existing line(s), keep every other line
    # VERBATIM (comments, blanks, hand-curated formatting), then append the new grant — so one admin
    # action can't reformat or lose the rest of the list, and unchanged npubs aren't re-encoded.
    kept, taken = [], set()
    for line in raw.split("\n"):
        s = line.strip()
        if not s or s.startswith("#"):
            kept.append(line); continue
        toks = s.replace("=", " ").replace(",", " ").split()
        owner = nostr_service.to_pubkey_hex(toks[1].strip()) if len(toks) >= 2 else None
        if owner == target:
            continue   # drop the target's existing grant (a grant replaces it; a remove clears it)
        kept.append(line)
        if len(toks) >= 2:
            taken.add(toks[0].strip().lower())
    granted_name = None
    if not data.remove:
        base = _sanitize_nip05_name(data.name)
        if not base:
            return JSONResponse({"ok": False, "error": "invalid name"}, status_code=400)
        if base.lower() in taken:   # case-insensitive collision with a DIFFERENT account
            return JSONResponse({"ok": False, "error": f"'{base}' is already taken"}, status_code=409)
        try:
            npub = nostr_service.npub_of(target)
        except Exception:
            npub = data.target
        kept.append(f"{base} {npub}")
        granted_name = base
    settings_store.put("nostr_relay_nip05_names", "\n".join(kept).strip("\n"))
    try:
        trigger_nip05_reload()
    except Exception as e:
        logger.warning("[client] nip05 reload after admin grant/remove failed: %s", e)
    domain = _nip05_domain(request, db)
    return JSONResponse({"ok": True, "granted": not data.remove, "name": granted_name,
                         "nip05": (f"{granted_name}@{domain}" if granted_name else None)})


_DEEPLINK_ENTITY = re.compile(
    r"^(?:nostr:)?(?:npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]+$", re.I)


@router.get("/{path:path}", response_class=HTMLResponse)
async def client_deeplink(path: str, request: Request, db: Session = Depends(get_db)):
    """Serve the SPA shell for client-side deep-link routes (/client/<npub>, /client/<nevent>,
    /client/users/<name>, …) so a REFRESH or bookmark of a profile / note / thread loads the app
    instead of 404ing ({"detail":"Not Found"}). The client reads location.pathname and opens the
    right view. Registered LAST so it never shadows the real /client/* API routes (FastAPI matches
    in registration order, so every specific route above wins first). ONLY entity-like paths get the
    shell — an unknown /client/* API path still returns a clean JSON 404 (not an HTML 200), so JSON
    consumers aren't handed a shell document."""
    seg = (path or "").split("/")[0]
    if not (_DEEPLINK_ENTITY.match(seg) or seg.lower() == "users"):
        raise HTTPException(status_code=404, detail="Not Found")
    return await client_app(request, db)

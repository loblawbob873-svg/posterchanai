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
import json
import logging
import os
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Setting, User
from app.services.nostr import nostr_service, event as nostr_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/client", tags=["client"])

_TEMPLATES = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "..", "templates"))
_STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "static")


def _setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(Setting).filter(Setting.key == key).first()
    return (row.value if row and row.value else default)


def _relay_url(request: Request, db: Session) -> str:
    explicit = _setting(db, "client_relay_url")
    if explicit:
        return explicit
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    ws = "wss" if proto == "https" else "ws"
    return f"{ws}://{host}/relay"


def _operator(db: Session) -> User | None:
    """The node's signing operator: prefer an admin with a linked Nostr secret, else any user
    with one. Used to auto-follow new signups into the web of trust."""
    q = db.query(User).filter(User.nostr_nsec.isnot(None))
    return q.filter(User.is_admin == True).first() or q.first()  # noqa: E712


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def client_app(request: Request):
    return _TEMPLATES.TemplateResponse("client.html", {"request": request})


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
        "blossom_url": _setting(db, "blossom_public_url").rstrip("/"),
        "blossom_enabled": _setting(db, "blossom_enabled", "false").lower() == "true",
        "operator_npub": op_npub,
        "admin_npubs": admin_npubs,
        "gif_enabled": bool(_setting(db, "tenor_api_key") or _setting(db, "giphy_api_key")),
        "name": _setting(db, "site_name", "PosterChan"),
    })


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
        "start_url": "/client",
        "scope": "/client",
        "display": "standalone",
        "background_color": "#08060f",
        "theme_color": "#0b0118",
        "description": "Cyberpunk Nostr client",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


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


class SignupFollow(BaseModel):
    pubkey: str   # new account's npub or 64-hex


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


@router.post("/signup-follow")
async def signup_follow(data: SignupFollow, db: Session = Depends(get_db)):
    """Operator auto-follows a freshly-created account so the WoT-gated relay accepts its posts."""
    new_pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not new_pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)

    op = _operator(db)
    if not op or not op.nostr_nsec:
        return JSONResponse({"ok": False, "error": "no operator account on this node to follow you"},
                            status_code=503)
    try:
        seckey = nostr_service.decode_seckey(op.nostr_nsec)
    except ValueError:
        return JSONResponse({"ok": False, "error": "operator key invalid"}, status_code=500)
    op_pk = nostr_service.derive_pubkey(seckey)

    port = int(_setting(db, "nostr_relay_port", "3052"))
    existing = await _fetch_latest_kind3(port, op_pk)
    # Preserve the operator's ENTIRE contact list (all tags + content — relay hints etc.); only
    # append the new follow. Filtering to 'p' tags would wipe NIP-65/relay metadata each signup.
    tags = [list(t) for t in (existing.get("tags", []) if existing else [])]
    if any(len(t) >= 2 and t[0] == "p" and t[1] == new_pk for t in tags):
        return JSONResponse({"ok": True, "message": "already followed"})
    tags.append(["p", new_pk])

    ev = nostr_event.build_event(seckey, 3, (existing.get("content", "") if existing else ""), tags=tags)
    accepted, msg = await _publish_to_relay(port, ev)
    if not accepted:
        return JSONResponse({"ok": False, "error": f"relay rejected follow: {msg}"}, status_code=502)

    # Pull the newcomer into the trust graph now (otherwise it waits for the daily rebuild).
    try:
        from app.services.nostr_relay.thread import trigger_wot_refresh
        trigger_wot_refresh()
    except Exception as e:
        logger.warning("[client] WoT refresh after signup failed: %s", e)

    return JSONResponse({"ok": True, "message": "operator followed you; you can post shortly"})


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

    row = db.query(Setting).filter(Setting.key == "nostr_relay_blocked_pubkeys").first()
    current = []
    if row and row.value:
        for tok in row.value.replace(",", "\n").split():
            h = nostr_service.to_pubkey_hex(tok.strip())
            if h:
                current.append(h)
    cur = set(current)
    if data.remove:
        cur.discard(target)
    else:
        cur.add(target)
    value = "\n".join(sorted(cur))
    if row:
        row.value = value
    else:
        db.add(Setting(key="nostr_relay_blocked_pubkeys", value=value))
    db.commit()

    try:
        from app.services.nostr_relay.thread import trigger_block_reload
        trigger_block_reload()
    except Exception as e:
        logger.warning("[client] block reload failed: %s", e)

    return JSONResponse({"ok": True, "blocked": not data.remove, "count": len(cur)})

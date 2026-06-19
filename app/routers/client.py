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
import re
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


def _static_version() -> str:
    """A cache-busting token derived from the newest mtime of the client's CSS/JS. Cloudflare
    rewrites these assets' Cache-Control to a 31-day max-age, so without a versioned URL a browser
    keeps serving a stale client.css/app.js for weeks after a deploy. Appending `?v=<this>` makes
    each change a brand-new URL no cache can answer with old bytes. Computed per request from
    mtimes, so it updates on every file change — even UI-only deploys that don't restart Python."""
    rels = ["css/client.css", "js/client/store.js", "js/client/relay.js", "js/client/app.js",
            "js/client/signer-worker.js"]
    mt = 0.0
    for r in rels:
        try:
            mt = max(mt, os.path.getmtime(os.path.join(_STATIC, *r.split("/"))))
        except OSError:
            pass
    return str(int(mt))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def client_app(request: Request):
    return _TEMPLATES.TemplateResponse("client.html", {"request": request, "ver": _static_version()})


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


_nip05_cache: dict[str, tuple[float, dict]] = {}   # "domain|name" -> (expires, data)
_NIP05_TTL = 600.0


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


# ----- Blossom upload access (admin grants/revokes via the client's profile menu) -----
class BlossomAccessReq(BaseModel):
    target: str          # npub/hex to grant/revoke
    grant: bool = True
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


def _whitelist_hex(db: Session) -> set:
    """Current `blossom_whitelist` setting as a hex pubkey set."""
    row = db.query(Setting).filter(Setting.key == "blossom_whitelist").first()
    out = set()
    if row and row.value:
        for tok in row.value.replace(",", "\n").split():
            h = nostr_service.to_pubkey_hex(tok.strip())
            if h:
                out.add(h)
    return out


@router.get("/blossom-access")
async def blossom_access_status(pubkey: str, db: Session = Depends(get_db)):
    """Is this pubkey on the Blossom upload whitelist? Lets the client show Grant vs Revoke."""
    h = nostr_service.to_pubkey_hex(pubkey)
    if not h:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
    return JSONResponse({"ok": True, "whitelisted": h in _whitelist_hex(db)})


@router.post("/blossom-access")
async def blossom_access(data: BlossomAccessReq, db: Session = Depends(get_db)):
    """Admin-only: add/remove a pubkey on the Blossom upload whitelist (Admin → Blossom tab)."""
    target = nostr_service.to_pubkey_hex(data.target)
    if not target:
        return JSONResponse({"ok": False, "error": "invalid target"}, status_code=400)
    if not _verify_admin_auth(db, data.auth, target):
        return JSONResponse({"ok": False, "error": "admin signature required (or stale request)"}, status_code=403)
    cur = _whitelist_hex(db)
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
    row = db.query(Setting).filter(Setting.key == "blossom_whitelist").first()
    if row:
        row.value = value
    else:
        db.add(Setting(key="blossom_whitelist", value=value))
    db.commit()   # blossom_service re-reads the setting on its next (short-TTL) cache miss — no reload
    return JSONResponse({"ok": True, "whitelisted": data.grant, "count": len(cur)})


# ----- AI access (admin approves a user's AI request from the client profile menu) -----
class AiAccessReq(BaseModel):
    target: str          # npub/hex to grant/revoke
    grant: bool = True
    auth: str            # base64 signed admin event (p-tags target), same proof as /block


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
    u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(target)).first()
    if not u:
        return JSONResponse({"ok": False, "error": "that user hasn't opened the AI app yet"}, status_code=404)
    u.can_ai = bool(data.grant)
    db.commit()
    logger.info("[client] AI access %s for %s", "granted" if data.grant else "revoked", u.username)
    return JSONResponse({"ok": True, "enabled": bool(data.grant)})


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
    row = db.query(Setting).filter(Setting.key == "nostr_relay_nip05_names").first()
    raw = row.value if row and row.value else ""
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
    if row:
        row.value = value
    else:
        db.add(Setting(key="nostr_relay_nip05_names", value=value))
    db.commit()
    try:
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception as e:
        logger.warning("[client] nip05 reload after claim failed: %s", e)
    return JSONResponse({"ok": True, "name": name, "nip05": f"{name}@{domain}"})

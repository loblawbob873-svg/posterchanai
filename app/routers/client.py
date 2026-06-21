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
import secrets
import time

from fastapi import APIRouter, Depends, Request, Response, Query, HTTPException
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
    for production. Without this the client would get an empty URL and uploads would silently fail."""
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
    # `secure` gates the upgrade-insecure-requests CSP: harmless over HTTPS (server1 via Cloudflare),
    # but over plain HTTP (e.g. http://nas.lan:3051 on the LAN) it would force every script/CSS to
    # https://<host> — which a node serving bare HTTP doesn't have — breaking the whole page.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    # Nostr-only deployments hide the AI tab + AI compose actions (POSTERCHANAI_NOSTR_ONLY=1).
    nostr_only = os.getenv("POSTERCHANAI_NOSTR_ONLY", "0").lower() in ("1", "true", "yes", "on")
    return _TEMPLATES.TemplateResponse("client.html",
        {"request": request, "ver": _static_version(), "secure": proto == "https",
         "nostr_only": nostr_only})


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
        "operator_npub": op_npub,
        "admin_npubs": admin_npubs,
        # Fresh install with no admin yet → the client offers first-run "become admin" setup
        # (solves the chicken/egg: nobody can grant AI access until an admin exists).
        "admin_unclaimed": len(admin_npubs) == 0,
        "gif_enabled": bool(_setting(db, "tenor_api_key") or _setting(db, "giphy_api_key")),
        "name": _setting(db, "site_name", "PosterChan"),
        # Community size — the relay's web-of-trust member count (cached in its status file; cheap).
        "users": _relay_user_count(),
    })


def _relay_user_count() -> int:
    """WoT member count from the relay status (community size shown in the client). Cheap + cached;
    returns 0 if the relay isn't running."""
    try:
        from app.services.nostr_relay.thread import relay_status
        return int(relay_status().get("members", 0) or 0)
    except Exception:
        return 0


@router.get("/stats")
async def client_stats():
    """Live community stats for the sidebar: `users` = WoT network size (cached), `online` = current
    relay client connections (people using the site right now). Cheap status-file read; polled."""
    try:
        from app.services.nostr_relay.thread import relay_status
        st = relay_status()
        # `online` is deduped by client IP (people now), falling back to raw conns if unavailable.
        online = st.get("online", st.get("conns", 0))
        return JSONResponse({"users": int(st.get("members", 0) or 0), "online": int(online or 0)})
    except Exception:
        return JSONResponse({"users": 0, "online": 0})


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
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    text = text[:4000]   # cap: posts are short; bounds the LLM work
    try:
        from app.services.inference_factory import get_inference_service
        svc = get_inference_service(db)
        msgs = [{"role": "system", "content": f"You are a translation engine. Translate the user's "
              f"message into {to}. The message is often colloquial, run-on, code-switched and "
              f"unpunctuated. Translate the ENTIRE message into natural {to} — EVERY word or phrase "
              f"that is not already {to}. This applies to SHORT, casual, and slang messages too "
              f"(e.g. 'lol that's funny') — they MUST be rendered in {to}, never echoed in the "
              f"source language. Do NOT leave any source-language words in the output, and do not "
              f"just re-punctuate the original. Keep @mentions, #hashtags, URLs and emoji exactly "
              f"as-is. Output ONLY the translated text — no preamble, notes, or quotes."}]
        # The echo-fixing examples translate INTO ENGLISH (they fix the 'English-dominant mixed text
        # gets echoed' case). They bias the model toward ENGLISH output, so ONLY include them when the
        # target IS English — otherwise translating to (say) Japanese would wrongly echo the English.
        if to.strip().lower() in ("english", "en", "en-us", "en-gb"):
            msgs += [
                {"role": "user", "content": "My babies! ang cute nila kahit pagod na pagod ako kakaalaga. Hook Needle"},
                {"role": "assistant", "content": "My babies! They're so cute even though I'm exhausted from taking care of them. Hook Needle"},
                {"role": "user", "content": "Good evening guys ayon bago palang kami nag karoon ng kuryente magmula kasi kanina alas 6 ng umaga nawala tapus ngayon lang nag karoon kumusta ang lahat kumain na ba kayo"},
                {"role": "assistant", "content": "Good evening guys, we only just got electricity back — it had been out since 6 this morning and only returned just now. How is everyone, have you eaten yet?"},
            ]
        msgs.append({"role": "user", "content": text})
        # Translation should be faithful, not creative — near-greedy decoding cuts hallucination.
        res = await svc.chat_completion(msgs, max_tokens=1200, temperature=0.0)
        out = (res.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not out:
            return JSONResponse({"error": "translation unavailable"}, status_code=503)
        return JSONResponse({"text": out})
    except Exception as e:
        logger.warning(f"[client] translate failed: {e}")
        return JSONResponse({"error": "translation unavailable"}, status_code=503)


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
    })


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
        try:
            from app.services.nostr_relay.thread import trigger_wot_refresh
            trigger_wot_refresh()
        except Exception:
            pass
        return True, "operator followed you + admitted"
    return True, f"admitted (follow not stored: {msg})"   # WoT-add already done → still usable


@router.post("/signup-follow")
async def signup_follow(data: SignupFollow, db: Session = Depends(get_db)):
    """Operator auto-follows + admits a freshly-created account so the WoT relay accepts its posts."""
    new_pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not new_pk:
        return JSONResponse({"ok": False, "error": "invalid pubkey"}, status_code=400)
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
    # Re-check server-side: refuse if any admin already has an npub (instance already set up).
    if db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).first():  # noqa: E712
        return JSONResponse({"ok": False, "error": "an admin already exists"}, status_code=409)
    npub = nostr_service.npub_of(pk)
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
    seeds_row = db.query(Setting).filter(Setting.key == "nostr_relay_wot_seeds").first()
    seeds_val = (seeds_row.value if seeds_row else "") or ""
    if npub not in seeds_val:
        new_seeds = (seeds_val.rstrip() + "\n" + npub).strip() if seeds_val.strip() else npub
        if seeds_row:
            seeds_row.value = new_seeds
        else:
            db.add(Setting(key="nostr_relay_wot_seeds", value=new_seeds))
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
    # Persist past restart: with settings_backend=relay, the startup hydrate would otherwise revert
    # this Setting to the relay's copy. Write the change through to the relay (no-op if backend off).
    try:
        from app.services import settings_store
        if settings_store.enabled(db):
            await settings_store.write_through(db, {"blossom_whitelist": value})
    except Exception as e:
        logger.warning("[client] blossom_whitelist write-through failed: %s", e)
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
    u.can_ai = bool(data.grant)
    db.commit()
    logger.info("[client] AI access %s for %s", "granted" if data.grant else "revoked", u.username)
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
        m = isinstance(rec, dict) and re.search(r'(enc_([0-9a-f]{64})\.\w+)$', rec.get("image_path") or "")
        if m:
            conv = d[len(store.NS_MSG):].split(":")[0]
            out.append({"url": f"/client/file/{np}/{conv}/{m.group(1)}",
                        "name": "generated image", "mime": "image/png", "sha": m.group(2), "kind": "generated"})
    return JSONResponse({"ok": True, "files": out})


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
    """Delete one AI file blob by sha (the user's own, signed)."""
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk or not re.fullmatch(r'[0-9a-f]{64}', data.sha or ''):
        return JSONResponse({"ok": False, "error": "invalid request"}, status_code=400)
    if not _verify_self_auth(data.auth, pk):
        return JSONResponse({"ok": False, "error": "ownership proof required"}, status_code=403)
    from app.services import artifact_store
    await artifact_store.delete_blob(db, data.sha)
    return JSONResponse({"ok": True})


# ----- drafts: synced across devices as ONE encrypted doc under the user's storage key -----
class DraftsReq(BaseModel):
    pubkey: str
    auth: str
    drafts: list | None = None   # present → save the list; absent → load


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
        await store.put_doc(port, sk, "pcai:drafts", {"drafts": data.drafts[:300]})
        return JSONResponse({"ok": True})
    doc = await store.get_doc(port, "pcai:drafts", seckey=sk)
    drafts = doc.get("drafts", []) if isinstance(doc, dict) else []
    return JSONResponse({"ok": True, "drafts": drafts if isinstance(drafts, list) else []})


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
    db.delete(user)
    db.commit()
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

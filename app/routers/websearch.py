"""Web Search — the client's own front end to this node's configured SearXNG instance.

Four things happen here, and only the first is free:

  * `/search`   proxies one page of SearXNG results (no LLM, no page fetching).
  * `/read`     extracts a page's text so a result can be read IN the client and backed out of,
                instead of throwing the reader out to a browser tab and losing the results.
  * `/summarize` summarizes ONE link.
  * `/overview` summarizes the RESULTS — the Google/Bing "AI overview" — with numbered citations
                back to the results it used, because an overview whose claims can't be traced is
                just a confident paragraph.

The three LLM paths are gated on the same `can_ai` flag as chat (one shared GPU: a search box that
could fan a page of results into inference per keystroke is exactly the thing that flag exists for),
serialized behind one semaphore, and answered from a short TTL cache — clicking ✨ twice on the same
query is the normal case, not an exotic one.

Fetching happens through `search_service`, so the SSRF guard (`is_safe_url`, applied inside
`fetch_url_content`) is the same one every other URL-reading path in the app uses. A result URL comes
from a third-party search engine, i.e. it is attacker-influencable by definition — that guard is what
stops "summarize this link" from being a request to 169.254.169.254.
"""
import asyncio
import logging
import re
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.search_service import get_search_service
from app.services.text_utils import strip_preamble, strip_thinking_tags

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/websearch", tags=["websearch"])

# One LLM job at a time from this screen. The inference service serializes internally anyway; this
# keeps a burst of clicks from queueing behind each other with the VRAM swap re-run per job.
_LLM_SLOT = asyncio.Semaphore(1)

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 900          # 15 minutes — long enough to cover re-reading a page of results
_CACHE_MAX = 200

# How much of a fetched page the model is shown. Two numbers because an overview reads SEVERAL pages
# and a single-link summary reads one: the same budget spent either way.
_OVERVIEW_PAGE_CHARS = 2500
_OVERVIEW_PAGES = 3
_SUMMARY_CHARS = 12000


def _cache_get(key: str):
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: dict):
    if len(_CACHE) >= _CACHE_MAX:
        for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 4]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time(), val)


def _require_ai(user: User):
    """Same gate as chat. Admins always; everyone else needs the admin-granted flag."""
    if not (getattr(user, "is_admin", False) or getattr(user, "can_ai", False)):
        raise HTTPException(status_code=403, detail="AI access not enabled — request access and an admin will approve.")


async def _complete(db: Session, messages: list, max_tokens: int = 900) -> str:
    """One LLM round trip. Returns the text, or raises HTTPException with something a user can act on."""
    async with _LLM_SLOT:
        prepare_vram_for_llm(db)
        service = get_inference_service(db)
        if service is None:
            raise HTTPException(status_code=503, detail="No inference service available. Enable an LLM in Admin settings.")
        try:
            async with asyncio.timeout(120):
                result = await service.chat_completion(messages=messages, temperature=0.3, max_tokens=max_tokens)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="The model took too long — try again.")
        if "error" in result:
            msg = result["error"].get("message") if isinstance(result.get("error"), dict) else str(result["error"])
            raise HTTPException(status_code=502, detail=msg or "Summarization failed.")
        content = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        # Both callers' prompts say "no preamble"; strip_preamble is what enforces it. One place,
        # because the overview and the page summary are the same promise to the reader.
        text = strip_preamble(strip_thinking_tags(content).strip())
        if not text:
            raise HTTPException(status_code=502, detail="The model returned nothing.")
        return text


@router.get("/search")
async def web_search(
    q: str = Query(..., min_length=1, max_length=300),
    category: str = Query("general"),
    time_range: str = Query(""),
    page: int = Query(1, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One page of results. No LLM — every logged-in user can search."""
    svc = get_search_service(db)
    return await svc.search_page(q.strip(), category=category, time_range=time_range, page=page)


@router.get("/read")
async def read_page(
    url: str = Query(..., max_length=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Extracted text of a result, so it can be read in place and backed out of.

    Not a proxy for the page itself: this returns TEXT, which the client renders as paragraphs. A
    real proxy would have to rewrite scripts, frames and subresources, and would put this node's IP
    behind every asset on an arbitrary page.
    """
    out = await get_search_service(db).fetch_url_content(url.strip(), max_length=40000)
    if not out:
        raise HTTPException(status_code=502, detail="Could not read that page.")
    if out.get("error"):
        # A blocked or unreadable URL is a 200 with an `error` the UI shows next to an "open the
        # original" link — the page is still reachable in a browser tab, so this is not a dead end.
        return {"url": out.get("url", url), "title": out.get("title") or url,
                "content": "", "error": out["error"]}
    return {"url": out.get("url", url), "title": out.get("title") or url,
            "content": out.get("content") or "", "error": None}


_PAGE_CSP = (
    # No scripts AT ALL (default-src 'none' and no script-src), which is what makes serving someone
    # else's markup from our origin safe enough to do. Pictures, stylesheets, fonts and media still
    # load — that is the whole point of showing the page instead of a wall of extracted text — and
    # they load from the site itself, so this is not an anonymising proxy and does not pretend to be.
    "default-src 'none'; "
    "img-src * data: blob:; style-src * 'unsafe-inline'; font-src * data:; media-src *; "
    # frame-ancestors lists the BUNDLED shells as well as 'self'. In the Electron app the embedding
    # document is app://posterchan and in the APK it is the WebView's own origin — neither is 'self'
    # relative to the instance, so with 'self' alone the browser refuses to render the frame at all,
    # no matter how right the URL is. (That was the second half of "Windows won't load sites".)
    "form-action 'none'; frame-ancestors 'self' app://posterchan https://localhost capacitor://localhost"
    # NO `base-uri 'none'` — the ONE <base> in this document is the one we inject (every other is
    # stripped), and with the directive on, the browser ignores it and every relative URL on the page
    # resolves against /api/websearch/page instead of the site. Stylesheets and images 404 and the
    # "real page" renders as unstyled text, which is the thing this endpoint exists to avoid.
)
# Elements that either execute, phone home invisibly, or would frame something else inside the frame.
_SCHEME_JUNK = re.compile(r"[\x00-\x20\x7f]")   # what a browser ignores inside a URL scheme
_PAGE_STRIP = ("script", "noscript", "iframe", "frame", "frameset", "object", "embed", "applet",
               "form", "input", "button", "select", "textarea", "template")


def _self_link(self_base: str, absolute: str, token: str) -> str:
    """A link back through this endpoint, ABSOLUTE.

    It cannot be root-relative: the document carries `<base href="<the site>">`, so `/api/websearch/
    page?url=…` inside the frame resolves against the SITE — the browser then asks github.com for our
    path, gets a 404 page, and Firefox shows "github.com will not allow … to display the page if
    another site has embedded it". Which reads as our frame being blocked when nothing of ours was
    ever requested.

    The token rides along for the same reason the frame's own src carries one: a navigation cannot
    send an Authorization header, and in the bundled app there is no cookie for this origin either.
    """
    from urllib.parse import quote
    out = f"{self_base}/api/websearch/page?url=" + quote(absolute, safe="")
    if token:
        out += "&t=" + quote(token, safe="")
    return out


def _render_page(html: str, final_url: str, self_base: str = "", token: str = "") -> str:
    """Someone else's page, made safe to show inside ours.

    Not a "reader mode" — the CSS, the images and the layout are the page's own, because that is what
    "open the result" means. What is removed is everything that runs or submits: scripts, event
    handlers, `javascript:` urls, forms and nested frames. Links are rewritten back through this
    endpoint so following one stays in the app (and cannot escape the frame), with the original
    always one tap away in the bar above.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(_PAGE_STRIP)):
        tag.decompose()

    # `<base>` first: everything below resolves against the URL we ENDED on, not the one asked for.
    for old in soup.find_all("base"):
        old.decompose()
    head = soup.head or soup.new_tag("head")
    if not soup.head:
        (soup.html or soup).insert(0, head)
    base = soup.new_tag("base", href=final_url)
    base["target"] = "_self"
    head.insert(0, base)

    for el in soup.find_all(True):
        # Inline handlers survive a script strip, and they are scripts.
        for attr in [a for a in el.attrs if a.lower().startswith("on")]:
            del el[attr]
        for attr in ("href", "src", "action", "poster", "srcset", "data-src"):
            v = el.get(attr)
            # Entity decoding happens BEFORE we see the value, so `java&#9;script:` arrives as
            # "java\tscript:" and a bare startswith misses it. Strip every ASCII whitespace/control
            # character out of the comparison — the browser ignores them in a scheme, so we must too.
            if isinstance(v, str) and _SCHEME_JUNK.sub("", v).lower().startswith(
                    ("javascript:", "vbscript:", "data:text/html")):
                del el[attr]
        # Lazy-loaded images: the real URL sits in data-src and the src is a placeholder, so without
        # this half a page renders blank inside the frame (the loader that would swap them is gone).
        if el.name == "img":
            real = el.get("data-src") or el.get("data-lazy-src") or el.get("data-original")
            if real and not (el.get("src") or "").startswith("http"):
                el["src"] = real
            el["loading"] = "lazy"
            el["referrerpolicy"] = "no-referrer"
        # ABSOLUTISE, rather than trusting the injected <base> alone. The base handles what this
        # misses (CSS `url()`, anything exotic), but a stylesheet or image that silently resolves
        # against /api/websearch/page renders the page naked, and that is the exact failure this
        # endpoint exists to avoid — so the common attributes are pinned here.
        for attr in ("src", "poster"):
            v = el.get(attr)
            if not isinstance(v, str):
                continue
            v = v.strip()
            if not v:
                del el[attr]          # src="" asks the browser for the PAGE again (and logs ERR_INVALID_URL)
                continue
            if v.startswith(("data:", "blob:", "#")):
                continue
            try:
                el[attr] = _asset_link(self_base, urljoin(final_url, v), token)
            except Exception:
                del el[attr]
        # Stylesheets, images and fonts go through the ASSET proxy: framed from our origin, a page
        # cannot load its own webfonts (fonts are always a CORS fetch) or any CORS-mode image, so
        # without this it renders half-dressed — which is what "open the page" was supposed to fix.
        if el.name == "link" and isinstance(el.get("href"), str):
            rels = " ".join(el.get("rel") or []).lower()
            h = el["href"].strip()
            if h and not h.startswith(("data:", "#")):
                absolute = urljoin(final_url, h)
                el["href"] = (_asset_link(self_base, absolute, token)
                              if ("stylesheet" in rels or "icon" in rels) else absolute)
        # A lazy <source> keeps the real value in data-srcset, exactly as a lazy <img> keeps it in
        # data-src — and with no JS to promote it, the <picture> renders as a broken box.
        if not el.get("srcset"):
            lazy_set = el.get("data-srcset") or el.get("data-lazy-srcset")
            if lazy_set:
                el["srcset"] = lazy_set
        srcset = el.get("srcset")
        if isinstance(srcset, str) and srcset:
            parts = []
            for cand in srcset.split(","):
                bits = cand.strip().split(None, 1)
                if not bits:
                    continue
                u = bits[0].strip()
                # Empty or whitespace-bearing candidates are what the browser reports as
                # ERR_INVALID_URL; a srcset entry is a URL and cannot contain a space.
                if not u or any(c.isspace() for c in u):
                    continue
                if not u.startswith("data:"):
                    u = _asset_link(self_base, urljoin(final_url, u), token)
                parts.append(" ".join([u] + bits[1:]))
            el["srcset"] = ", ".join(parts)

    # INLINE css: a <style> block and a style="" attribute reference images and fonts exactly like a
    # stylesheet does, and a page that keeps its hero image in one renders blank without this.
    for st in soup.find_all("style"):
        if st.string:
            st.string.replace_with(_rewrite_css(st.string, final_url, self_base, token))
    for el in soup.find_all(style=True):
        v = el.get("style")
        if isinstance(v, str) and "url(" in v.lower():
            el["style"] = _rewrite_css(v, final_url, self_base, token)

    # A meta refresh would navigate the FRAME to the raw site (which then refuses to be framed) —
    # a link out of the proxy that nobody clicked.
    for m in soup.find_all("meta"):
        if (m.get("http-equiv") or "").lower() == "refresh":
            m.decompose()

    # SVG's own image reference, which is not `src`.
    for im in soup.find_all(["image", "use"]):
        for attr in ("href", "xlink:href"):
            v = im.get(attr)
            if isinstance(v, str) and v.strip() and not v.strip().startswith(("data:", "#")):
                im[attr] = _asset_link(self_base, urljoin(final_url, v.strip()), token)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("#") or not href:
            continue
        absolute = urljoin(final_url, href)
        if absolute.lower().startswith(("http://", "https://")):
            a["href"] = _self_link(self_base, absolute, token)
            a["target"] = "_self"
        else:
            del a["href"]      # mailto:, tel:, anything else — not ours to open from a frame

    return str(soup)


# ---- framing tickets ---------------------------------------------------------------------------
# An <iframe src> is a NAVIGATION: no Authorization header, and in the bundled desktop app / APK no
# cookie either, because the page's origin is not the API host. The first version therefore put the
# session JWT in the query string — where nginx and Cloudflare log the full request line and the
# browser keeps it in history. app.js already rejected exactly that for the admin iframe (it hands
# its token over by postMessage, "so no secret lands in history, a Referer or a log").
#
# So: a ticket. Random, 15 minutes, one user, and it opens NOTHING but this read-only endpoint — a
# capability rather than a credential. A web client never needs one (its cookie rides along on the
# same origin); only the bundled shells mint one.
_TICKET_TTL = 900
_TICKETS: dict[str, tuple[float, int]] = {}


def _mint_ticket(user_id: int) -> str:
    now = time.time()
    for k, (exp, _) in list(_TICKETS.items()):     # opportunistic sweep; this dict stays small
        if exp < now:
            _TICKETS.pop(k, None)
    tok = secrets.token_urlsafe(24)
    _TICKETS[tok] = (now + _TICKET_TTL, user_id)
    return tok


def _ticket_user(tok: str):
    hit = _TICKETS.get(tok or "")
    if not hit:
        return None
    exp, uid = hit
    if exp < time.time():
        _TICKETS.pop(tok, None)
        return None
    return uid


def _self_base(request: Request) -> str:
    """This node's absolute base — what a URL inside the frame has to point back at.

    The SCHEME is the awkward part. Behind the reverse proxy the upstream connection is plain HTTP,
    so `request.url.scheme` says http even though the browser is on https; nginx here does not send
    `x-forwarded-proto` either. Emitting http:// then makes every asset a mixed-content request that
    the page's own `upgrade-insecure-requests` has to rescue — 38 CSP warnings per page, and a plain
    failure anywhere that policy is absent.

    So: the forwarded proto if there is one, else http ONLY for a loopback/private host (a LAN node
    genuinely served over http), else https. A public hostname reached over a proxy is https in every
    deployment this ships to.
    """
    fwd_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if not proto:
        host_only = fwd_host.split(":")[0].lower()
        local = (host_only in ("localhost", "127.0.0.1", "::1", "[::1]")
                 or host_only.endswith(".lan") or host_only.endswith(".local")
                 or host_only.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.")))
        proto = request.url.scheme if local else "https"
    return f"{proto}://{fwd_host}".rstrip("/") if fwd_host else str(request.base_url).rstrip("/")


def _page_viewer(request: Request, t: str = Query(""), db: Session = Depends(get_db)) -> User:
    """Who is asking for this frame: a ticket, else an ordinary session (cookie / header / ?token=)."""
    uid = _ticket_user(t)
    if uid:
        user = db.query(User).filter(User.id == uid).first()
        if user:
            return user
    # No ticket → an ordinary session. The Authorization header has to be handed over explicitly:
    # get_current_user only reads `credentials`, the cookie and `?token=`, so passing None silently
    # ignored every Bearer-authenticated caller (the APK, the checks, curl) and answered 401.
    creds = None
    authz = request.headers.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        from fastapi.security import HTTPAuthorizationCredentials
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=authz[7:].strip())
    return get_current_user(request=request, credentials=creds, db=db)


@router.post("/ticket")
async def page_ticket(current_user: User = Depends(get_current_user)):
    """A short-lived key for the page frame — see _mint_ticket."""
    return {"ticket": _mint_ticket(current_user.id), "expires_in": _TICKET_TTL}


# Subresource types worth re-serving. Everything else the page asks for is simply not fetched — the
# frame is showing a document, not proxying an application.
_ASSET_TYPES = ("text/css", "image/", "font/", "application/font", "application/x-font",
                "application/vnd.ms-fontobject", "image/svg+xml")
_ASSET_MAX = 8_000_000


def _asset_link(self_base: str, absolute: str, token: str) -> str:
    from urllib.parse import quote
    out = f"{self_base}/api/websearch/asset?url=" + quote(absolute, safe="")
    if token:
        out += "&t=" + quote(token, safe="")
    return out


def _rewrite_css(css: str, css_url: str, self_base: str, token: str) -> str:
    """Point a stylesheet's own `url(...)` and `@import` at the proxy.

    Without this, proxying the CSS makes things WORSE: its relative urls would resolve against
    /api/websearch/asset instead of the site, so every background image and @font-face in it 404s.
    """
    from urllib.parse import urljoin

    def _u(m):
        raw = (m.group(2) or "").strip()
        if not raw or raw.startswith(("data:", "blob:", "#")):
            return m.group(0)
        return f"{m.group(1)}{_asset_link(self_base, urljoin(css_url, raw), token)}{m.group(3)}"

    css = re.sub(r"(url\(\s*[\"\']?)([^)\"\']+)([\"\']?\s*\))", _u, css)
    css = re.sub(r"(@import\s+[\"\'])([^\"\']+)([\"\'])", _u, css)
    return css


@router.get("/asset")
async def render_asset(
    request: Request,
    url: str = Query(..., max_length=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(_page_viewer),
):
    """A stylesheet, image or font belonging to a framed page, re-served from this node.

    Not gold-plating: a page framed from OUR origin cannot load its own WEBFONTS (fonts are always
    fetched in CORS mode, and a site's font server has no reason to allow poster.place), and any
    image the page requests in CORS mode fails the same way — "blocked by CORS policy … from origin
    'null'", then from our origin. Proxying is what makes a framed page look like the page.

    It also means the reader's browser never contacts the site directly, which is a happy side
    effect rather than a promise: the NODE still does.
    """
    svc = get_search_service(db)
    out = await svc.fetch_asset(url.strip(), max_bytes=_ASSET_MAX, allow=_ASSET_TYPES)
    if out.get("error"):
        # A type this endpoint does not serve (a script, a video, an XHR payload a stylesheet points
        # at) is not an ERROR — the page simply doesn't get it, and the CSP was going to refuse it
        # anyway. Answering 502 only wrote red lines into the console for a document that rendered
        # perfectly. Anything else (an upstream 404, a refused host) keeps its own meaning.
        if "type not served here" in out["error"]:
            return Response(content=b"", status_code=204,
                            headers={"Cache-Control": "private, max-age=600"})
        raise HTTPException(status_code=502, detail=out["error"][:200])
    body, ctype = out["body"], out["content_type"]
    if ctype.startswith("text/css"):
        self_base = _self_base(request)
        token = request.query_params.get("t") or ""
        from starlette.concurrency import run_in_threadpool
        css = await run_in_threadpool(_rewrite_css, body.decode("utf-8", errors="replace"),
                                      out["url"], self_base, token)
        body = css.encode("utf-8")
    return Response(content=body, media_type=ctype or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=600", "Referrer-Policy": "no-referrer"})


def _looks_empty(html: str) -> bool:
    """Did the sanitised document end up with nothing to read?"""
    from bs4 import BeautifulSoup
    try:
        body = BeautifulSoup(html, "lxml").body
        return len((body.get_text(" ", strip=True) if body else "")) < 200
    except Exception:
        return False


# The signatures of an interstitial that is asking a BROWSER to prove itself. Matched against the
# raw upstream body, not the sanitised one — the sanitiser strips exactly the scripts that carry the
# giveaway. Cloudflare, DDoS-Guard and the "enable JS and cookies" wall are what actually turn up.
_CHALLENGE_RE = re.compile(
    r"just a moment\.\.\.|checking your browser|cf-browser-verification|cf_chl_|__cf_chl|"
    r"attention required!\s*\|\s*cloudflare|ddos-guard|enable javascript and cookies to continue|"
    r"please turn javascript on and reload", re.I)


def _refused_page(url: str, status, raw: str) -> str:
    """The site REFUSED us, rather than the page being script-built.

    A bot challenge is a real HTML document that happens to be empty once its scripts are stripped,
    so it landed on "This page is built by JavaScript — nothing is wrong with the link". Something is
    wrong with the link: the site turned us away, and no amount of Reader will change that. Says which
    of the two happened, because the remedies differ.
    """
    from html import escape as _esc
    challenged = bool(raw and _CHALLENGE_RE.search(raw[:20000]))
    blocked = isinstance(status, int) and status >= 400
    if not (challenged or blocked):
        return ""
    href = _esc(url, quote=True) if url.lower().startswith(("http://", "https://")) else ""
    if challenged:
        head = "The site is asking for a browser check"
        why = ("It answered with a bot-protection page (Cloudflare or similar) instead of the "
               "article. Those checks need scripts and cookies, which this view does not run, so it "
               "cannot be passed here.")
    else:
        head = f"The site refused this request ({status})"
        why = ("It answered with an error rather than the page. That is the site's decision, not a "
               "problem with the link you clicked.")
    return ("<!doctype html><meta charset=utf-8><style>body{font:16px/1.7 system-ui;padding:28px;"
            "color:#e8e8f0;background:#111}a{color:#3ce8ff}.h{font-size:19px;font-weight:700;"
            "margin-bottom:10px}.m{color:#9fa1c6}</style>"
            f"<div class=h>{_esc(head)}</div><p class=m>{_esc(why)}</p>"
            "<p class=m>Opening it in a real tab will usually work:</p>"
            + (f'<p><a href="{href}" target="_blank" rel="noopener noreferrer">{href}</a></p>' if href else ""))


def _needs_js_page(url: str) -> str:
    from html import escape as _esc
    href = _esc(url, quote=True) if url.lower().startswith(("http://", "https://")) else ""
    return ("<!doctype html><meta charset=utf-8><style>body{font:16px/1.7 system-ui;padding:28px;"
            "color:#e8e8f0;background:#111}a{color:#3ce8ff}.h{font-size:19px;font-weight:700;"
            "margin-bottom:10px}.m{color:#9fa1c6}</style>"
            "<div class=h>This page is built by JavaScript</div>"
            "<p class=m>Pages are shown here with scripts turned off, so a site that draws itself "
            "with JavaScript (YouTube's player page, most shops) arrives empty. Nothing is wrong with "
            "the link.</p>"
            "<p class=m>Try <b>Reader</b> above for the text, or open it in a tab:</p>"
            + (f'<p><a href="{href}" target="_blank" rel="noopener noreferrer">{href}</a></p>' if href else ""))


@router.get("/page")
async def render_page(
    request: Request,
    url: str = Query(..., max_length=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(_page_viewer),
):
    """The result's ACTUAL page, rendered in the app.

    `/read` gives the text, which is the right answer for summarizing and for a page the layout of
    which is noise. This is the other half: the page as it looks, in an iframe, with a Back that
    returns to the results — the thing a browser tab does, without the browser tab (which on a phone
    is a one-way door, and in the PWA/APK often a cold restart that loses the results).

    Served from our own origin because most sites refuse to be framed (X-Frame-Options /
    frame-ancestors); that is the only reason this proxies rather than pointing an iframe at the site.
    The response carries a no-script CSP and the markup is stripped of anything that executes, so what
    comes back can lay itself out and nothing more.
    """
    out = await get_search_service(db).fetch_url_raw(url.strip())
    if out.get("error"):
        # ESCAPED, both of them. `url` is caller-supplied and the error text can echo it back, and
        # this page is served from OUR origin inside the app's own frame — unescaped, a crafted link
        # renders attacker markup on poster.place. (Scripts are dead either way under the CSP; text
        # and styling are quite enough to make a page say anything.)
        from html import escape as _esc
        safe = _esc((out["error"] or "")[:300])
        href = _esc(url, quote=True) if url.lower().startswith(("http://", "https://")) else ""
        body = (f"<!doctype html><meta charset=utf-8><style>body{{font:16px/1.6 system-ui;padding:24px;"
                f"color:#ddd;background:#111}}a{{color:#3ce8ff}}</style>"
                f"<p>This page can't be shown here: {safe}</p>"
                + (f"<p><a href=\"{href}\" target=\"_blank\" rel=\"noopener noreferrer\">Open the original</a></p>"
                   if href else ""))
        return Response(content=body, media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": _PAGE_CSP, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "Cache-Control": "private, max-age=60"})
    # ONE place decides this node's public base (scheme included — see _self_base). Computing it
    # here as `request.base_url` is what emitted http:// links on an https page: every asset became a
    # mixed-content request that only `upgrade-insecure-requests` rescued, 38 CSP warnings deep.
    self_base = _self_base(request)
    # Every URL this page will ask for — its stylesheets, images, fonts, and the links in it — is a
    # request the FRAME makes, and a frame carries no Authorization header (and, from a bundled app,
    # no cookie for this origin either). So they all travel with a ticket: the one this request came
    # with, or a fresh one when the frame itself was opened with an ordinary session.
    #
    # Without this every subresource 401s and the page renders naked with no images — which is what
    # scripts/check_websearch_pages.py found across all five test sites, and exactly the failure the
    # page view exists to avoid.
    token = request.query_params.get("t") or _mint_ticket(current_user.id)
    try:
        # OFF the event loop: this is an lxml parse plus a full-tree attribute walk over up to 3 MB of
        # someone else's markup, and the deployment runs a SINGLE uvicorn worker — inline, one heavy
        # page stalls every in-flight LLM stream, relay proxy hop and Telegram webhook for seconds.
        from starlette.concurrency import run_in_threadpool
        html = await run_in_threadpool(_render_page, out["html"], out["url"], self_base, token)
        # A page that is ALL JavaScript renders as a blank white rectangle here — YouTube's watch page
        # and Amazon's product pages are both shells an app fills in, and this endpoint runs no
        # scripts by design. A blank frame reads as "your app is broken"; say what happened instead,
        # and offer the two things that do work.
        if _looks_empty(html):
            # Which kind of empty? A refusal and a script-built page look identical after stripping,
            # and telling someone "nothing is wrong with the link" when the site just 403'd them is
            # the wrong advice as well as the wrong diagnosis.
            html = _refused_page(url, out.get("status"), out.get("html") or "") or _needs_js_page(url)
    except Exception as e:
        logger.warning("page render failed for %s: %s", url, e)
        raise HTTPException(status_code=502, detail="Could not render that page.")
    # NO `X-Content-Type-Options: nosniff` here, deliberately. It applies to this document's
    # SUBRESOURCES too: with it, Chrome refuses any stylesheet whose own server sends a blank or
    # wrong Content-Type — measured on apple.com ("Refused to apply style … MIME type ('')"), which
    # renders the page unstyled, i.e. the thing this endpoint exists to avoid. The response's own
    # type is stated exactly and the CSP forbids scripts outright, so there is nothing here for
    # sniffing to escalate.
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": _PAGE_CSP,
                             "Referrer-Policy": "no-referrer", "Cache-Control": "private, max-age=60"})


class SummarizeReq(BaseModel):
    url: str


@router.post("/summarize")
async def summarize_link(
    req: SummarizeReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Summarize one result's page."""
    _require_ai(current_user)
    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="No URL given.")

    key = "sum:" + url
    cached = _cache_get(key)
    if cached:
        return cached

    page = await get_search_service(db).fetch_url_content(url, max_length=_SUMMARY_CHARS)
    if not page or page.get("error"):
        raise HTTPException(status_code=502, detail=(page or {}).get("error") or "Could not read that page.")
    text = (page.get("content") or "").strip()
    if len(text) < 200:
        raise HTTPException(status_code=422, detail="There wasn't enough readable text on that page to summarize.")

    summary = await _complete(db, [
        {"role": "system", "content":
            "Summarize the page the user provides in 4-6 sentences of plain prose. Lead with what it "
            "actually says. Do not add a preamble, a title, or commentary about the summary itself. If "
            "the text is mostly navigation or boilerplate, say so instead of inventing content."},
        {"role": "user", "content": f"Title: {page.get('title') or url}\nURL: {url}\n\n{text}"},
    ], max_tokens=700)

    out = {"url": url, "title": page.get("title") or url, "summary": summary}
    _cache_put(key, out)
    return out


class OverviewReq(BaseModel):
    q: str
    category: str = "general"
    time_range: str = ""


@router.post("/overview")
async def overview(
    req: OverviewReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The "AI overview": answer the query from the top results, citing them by number.

    The results are re-fetched HERE rather than accepted from the client. Sending them up would let
    the page choose what the model reads, and the search is cached upstream anyway — this costs a
    request and removes a whole class of "the overview said something no engine returned".
    """
    _require_ai(current_user)
    q = (req.q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="No query given.")

    key = f"ov:{q}|{req.category}|{req.time_range}"
    cached = _cache_get(key)
    if cached:
        return cached

    svc = get_search_service(db)
    found = await svc.search_page(q, category=req.category, time_range=req.time_range, page=1, limit=8)
    results = found.get("results") or []
    if not results:
        raise HTTPException(status_code=404, detail=found.get("error") or "No results to summarize.")

    # Read the top few pages for substance; the rest contribute their snippet. A page that won't
    # load simply falls back to its snippet — one slow site must not cost the whole overview.
    async def _body(r):
        try:
            async with asyncio.timeout(20):
                page = await svc.fetch_url_content(r["url"], max_length=_OVERVIEW_PAGE_CHARS)
            if page and not page.get("error"):
                return (page.get("content") or "").strip()
        except Exception as e:
            logger.debug("overview fetch failed for %s: %s", r.get("url"), e)
        return ""

    bodies = await asyncio.gather(*[_body(r) for r in results[:_OVERVIEW_PAGES]], return_exceptions=True)

    sources, blocks = [], []
    for i, r in enumerate(results, 1):
        sources.append({"n": i, "title": r["title"], "url": r["url"]})
        body = ""
        if i <= len(bodies) and not isinstance(bodies[i - 1], Exception):
            body = bodies[i - 1] or ""
        snippet = body[:_OVERVIEW_PAGE_CHARS] or r.get("content") or ""
        blocks.append(f"[{i}] {r['title']}\n{r['url']}\n{snippet}")

    text = await _complete(db, [
        {"role": "system", "content":
            "You are summarizing web search results for the user's query, the way a search engine's "
            "overview does. Write 3-6 sentences of plain prose that answer the query using ONLY the "
            "numbered sources given. Cite each claim with its source number in square brackets, like "
            "[1] or [2][3]. If the sources disagree, say so. If they do not actually answer the query, "
            "say that plainly instead of guessing. No preamble, no headings, no bullet list of the "
            "sources themselves."},
        {"role": "user", "content": f"Query: {q}\n\n" + "\n\n".join(blocks)},
    ], max_tokens=800)

    out = {"query": q, "overview": text, "sources": sources}
    _cache_put(key, out)
    return out

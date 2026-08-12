import asyncio
import httpx
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Optional
from urllib.parse import urlparse
from sqlalchemy.orm import Session
from bs4 import BeautifulSoup
from app.services import settings_store
from app.services import page_render

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------------
# Which SearXNG a node searches. ONE resolution order, used by EVERY consumer — the AI's web-search
# tool, the news digests, the Web Search screen, and the bots (bot_manager_service hands them the
# resolved value as SEARXNG_URL, so a bot can never end up searching somewhere else):
#
#   1. `searxng_url` in Admin → Tools, if set.
#   2. A SearXNG BUNDLED WITH THIS NODE, if one answers. `./install.sh --searxng` runs one, and the
#      docker-compose `searxng` profile is the same thing for container installs. Two candidates
#      because the app is either on the host (loopback) or in the compose network (service name).
#   3. A public instance, as a last resort.
#
# Step 3 is a fallback, not a plan: measured against this default from a server, it answers 429 Too
# Many Requests to both its JSON and its HTML endpoint — public instances rate-limit clients that
# don't look like a browser. That is exactly why a bundled instance sits in front of it.
#
# NOTE what is NOT here: this used to default to `https://search.poster.place`, hardcoded, so every
# node that never filled the field in silently searched through one particular deployment's box.
DEFAULT_SEARXNG_URL = "https://searx.tiekoetter.com"
# 8899 and not SearXNG's own 8080 or the obvious 8888: 8888 is MediaMTX's HLS port on any node that
# streams, i.e. every default install.
DEFAULT_LOCAL_SEARXNG_PORT = "8899"
# The installer writes the port it actually used here (repo-relative), because an env var set at
# INSTALL time never reaches the app SERVICE: `POSTERCHANAI_SEARXNG_PORT=9000 ./install.sh --searxng`
# published a container the app then looked for on 8899, found nothing, and quietly fell through to
# the public instance while a healthy local one sat unused.
_PORT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "searxng", "port")
_LOCAL_PROBE_TTL = 300           # re-probe every 5 minutes: a bundled instance can be started later
_local_probe: dict = {"ts": 0.0, "url": ""}
# host -> (ts, is_private). See _is_local_base: the lookup is blocking and sits on the search path.
_DNS_TTL = 3600
_dns_cache: dict = {}

# The default this replaced. A node that still has it stored never CHOSE it — it was seeded by an
# older install — and the box behind it is retired, so honouring it means every search on that node
# fails with nothing to say why. Treated as "not configured" (the bundled instance then wins), rather
# than deleted: mutating an operator's settings across every node on upgrade is a bigger hammer than
# this needs, and the field still shows what is stored.
LEGACY_SEARXNG_URLS = ("https://search.poster.place", "http://search.poster.place")


def _local_port() -> str:
    """The port the bundled instance was actually installed on: the env var if the app was given one,
    else what the installer recorded, else the default."""
    env = (os.environ.get("POSTERCHANAI_SEARXNG_PORT") or "").strip()
    if env.isdigit():
        return env
    try:
        with open(_PORT_FILE) as fh:
            p = fh.read().strip()
            if p.isdigit():
                return p
    except Exception:
        pass
    return DEFAULT_LOCAL_SEARXNG_PORT


def local_searxng_urls() -> tuple:
    """Where a bundled instance could be: this host (systemd/docker), or a sibling compose service."""
    return (f"http://127.0.0.1:{_local_port()}", "http://searxng:8080")


def _is_searxng(base: str) -> bool:
    """Is a SearXNG — one that can answer THIS app — actually listening there?

    Two requests, and both are load-bearing:

      /healthz must answer **200**. `status < 500` was not enough: an unrelated listener (a reverse
      proxy, a stale container) 404s, which passed, and the node then adopted it as its search
      backend for the next five minutes — with the public fallback never tried, because the probe had
      "succeeded".

      /config must answer JSON. That is what distinguishes SearXNG from anything else that happens to
      have a health endpoint, and it is nearly the same question as "will format=json work", which is
      the one thing this app needs from an instance and the one SearXNG ships turned OFF.
    """
    try:
        if httpx.get(f"{base}/healthz", timeout=1.5).status_code != 200:
            return False
        r = httpx.get(f"{base}/config", timeout=2.5)
        if r.status_code != 200:
            return False
        return "json" in (r.headers.get("content-type") or "").lower()
    except Exception:
        return False


def local_searxng_url() -> str:
    """The bundled instance's URL if one is answering, else "". Cached, so this costs a request
    every few minutes at most.

    Deliberately SYNC (and called off-thread from async paths): the one caller that cannot await is
    the bot manager, which builds each bot's environment at spawn time — and a bot searching
    somewhere other than its own node is precisely the drift this resolution order exists to stop.
    """
    now = time.time()
    if now - _local_probe["ts"] < _LOCAL_PROBE_TTL:
        return _local_probe["url"]
    found = ""
    for base in local_searxng_urls():
        if _is_searxng(base):
            found = base
            break
    _local_probe.update({"ts": now, "url": found})
    return found


def search_enabled() -> bool:
    """Is this node allowed to search at all?

    An explicit switch, because clearing the URL no longer means "off": resolution now ends at a
    public instance, so an operator who blanked the field to stop this node making external search
    requests would instead have had every query — theirs, the AI's, the bots' — sent to an
    unaffiliated third party. Off = `web_search` returns nothing, exactly as an empty URL used to.

    An EMPTY stored value counts as "not set", i.e. ON. `settings_store.get_bool` takes the default
    only for None and reads "" as FALSE — and a blank row is exactly what a legacy-table migration or
    a half-written setting leaves behind, which would switch web search off across a node with
    nothing said anywhere. Off has to be something an admin chose.
    """
    v = settings_store.get("searxng_enabled", None)
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _configured_url() -> str:
    """Admin -> Tools, normalised — and EMPTY when it still holds the retired hardcoded default."""
    v = (settings_store.get("searxng_url") or "").strip().rstrip("/")
    return "" if v in LEGACY_SEARXNG_URLS else v


def resolve_searxng_url() -> str:
    """The base URL to search, by the order documented above. "" when search is turned off."""
    if not search_enabled():
        return ""
    configured = _configured_url()
    if configured:
        return configured
    return local_searxng_url() or DEFAULT_SEARXNG_URL


def _is_local_base(base: str) -> bool:
    """Is this SearXNG somewhere the Tor proxy cannot reach — this machine, this LAN, or a sibling
    container?

    NOT just loopback. Tor cannot route RFC1918, and the built-in proxy answers an unroutable target
    with a 502 **response**, which `afallback_transport` does not retry direct (it only falls back on
    connect-level failures, deliberately, so a delivered request is never re-sent). So a perfectly
    ordinary `http://192.168.0.85:8888` — the shape of every self-hosted instance, including this
    deployment's own — would have gone through Tor and failed every single time, reported to the user
    as "no results".
    """
    h = (urlparse(base).hostname or "").lower()
    if not h:
        return False
    if h in ("localhost", "searxng") or h.endswith(".local") or h.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # A NAME. Resolve it: split-horizon DNS is normal here (our own public names answer with a
        # LAN address from inside), and a name that lands on a private IP is a private target.
        #
        # MEMOIZED, because this runs on the search path and `gethostbyname` BLOCKS: on the single
        # uvicorn worker an unreachable resolver would stall every other request in flight, per
        # search. Cached both ways for an hour — a SearXNG host moving between the LAN and the
        # internet is not a thing that happens mid-session.
        now = time.time()
        hit = _dns_cache.get(h)
        if hit and now - hit[0] < _DNS_TTL:
            return hit[1]
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(h))
        except Exception:
            _dns_cache[h] = (now, False)
            return False        # unresolvable → treat as remote; the proxy is the safer default
        verdict = ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        _dns_cache[h] = (now, verdict)
        return verdict
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def search_transport(base: str):
    """The httpx transport a SearXNG request should use.

    A REMOTE instance is reached the way the rest of this app reaches the internet: through the
    built-in HTTP proxy — which round-robins Tor1 and Tor2 — falling back to a direct connection when
    the proxy can't be reached (proxy_utils.afallback_transport). Search queries are the last thing
    that should carry this node's IP, and it is also what keeps a public instance from rate-limiting
    one address into a 429.

    A LOCAL or LAN instance is reached DIRECTLY — see _is_local_base for why that is not merely an
    optimisation. For a bundled instance the hop that needs anonymising is SearXNG → the engines,
    which is configured on the SearXNG side (see scripts/install/searxng.sh).
    """
    from app.services.proxy_utils import afallback_transport
    if _is_local_base(base):
        return httpx.AsyncHTTPTransport(retries=0)
    return afallback_transport()


def own_media_hosts() -> set:
    """Hostnames THIS deployment serves itself, exempt from the private-IP check below.

    Our own public names resolve to a PRIVATE LAN IP from inside the LAN (split-horizon DNS:
    media.poster.place -> 192.168.0.1), so the guard rejects them as internal and every fetch of our
    own media is refused — reported from AI chat as "URL blocked: Private IP not allowed: 192.168.0.1"
    on a perfectly ordinary media.poster.place image.

    It lives HERE, next to the guard, and is the single list: the meme/effect fetch path
    (client._own_media_hosts) delegates to it rather than keeping its own. Two lists is how one path
    ends up trusting a host the other blocks — which is exactly the state this fixes, since only the
    render path was ever exempt.

    Sources are the configured Blossom bases plus the admin's "Own media hosts" setting
    (Admin -> Blossom), one hostname per line. Nothing is hardcoded: a deployment's own names are
    deployment config, and a name listed here says "this box serves that" — the same trust already
    placed in blossom_public_url.
    """
    own = set()
    for key in ("blossom_public_url", "nostr_dvm_blossom_url"):
        h = urlparse(settings_store.get(key) or "").hostname
        if h:
            own.add(h.lower())
    for line in (settings_store.get("media_own_hosts") or "").replace(",", "\n").split("\n"):
        t = line.strip().lower().strip(".")
        if not t:
            continue
        # Accept a bare hostname OR a pasted URL. EXACT hostname match, no wildcards, so a typo can
        # never widen the exemption to a whole zone.
        own.add(urlparse(t).hostname or t.split("/", 1)[0].split(":", 1)[0])
    own.discard("")
    return own


def is_safe_url(url: str) -> tuple[bool, str]:
    """
    SSRF protection: Validate that URL doesn't point to internal networks.
    Returns (is_safe, error_message).
    """
    try:
        parsed = urlparse(url)

        # Only allow http/https
        if parsed.scheme not in ('http', 'https'):
            return False, f"Invalid scheme: {parsed.scheme}"

        hostname = parsed.hostname
        if not hostname:
            return False, "No hostname in URL"

        # Trusted domains that are allowed even if they resolve to private IPs
        # (e.g., internal git servers, local services with proper DNS entries)
        trusted_domains = {'git.poster.place', 'poster.place'}
        if hostname.lower() in trusted_domains:
            return True, ""
        # …and the hosts this deployment serves itself, which are CONFIG rather than hardcoded names.
        # Without this, only the meme/effect fetch path was exempt and everything else — AI chat
        # reading a page, translate <url>, link previews — refused our own media.
        try:
            if hostname.lower() in own_media_hosts():
                return True, ""
        except Exception:
            pass   # never let a settings read turn the guard into a hard failure

        # Block localhost variations
        localhost_names = {'localhost', 'localhost.localdomain', '127.0.0.1', '::1'}
        if hostname.lower() in localhost_names:
            return False, "Localhost URLs not allowed"

        # Resolve hostname to IP
        try:
            ip = socket.gethostbyname(hostname)
            ip_obj = ipaddress.ip_address(ip)
        except (socket.gaierror, ValueError) as e:
            return False, f"Cannot resolve hostname: {e}"

        # Block private/internal IP ranges
        if ip_obj.is_private:
            return False, f"Private IP not allowed: {ip}"
        if ip_obj.is_loopback:
            return False, f"Loopback IP not allowed: {ip}"
        if ip_obj.is_link_local:
            return False, f"Link-local IP not allowed: {ip}"
        if ip_obj.is_reserved:
            return False, f"Reserved IP not allowed: {ip}"
        if ip_obj.is_multicast:
            return False, f"Multicast IP not allowed: {ip}"

        # Block common internal hostnames
        internal_patterns = ['internal', 'intranet', 'corp', 'local', 'private']
        for pattern in internal_patterns:
            if pattern in hostname.lower():
                return False, f"Internal hostname pattern detected: {hostname}"

        # Block AWS/cloud metadata endpoints
        metadata_ips = ['169.254.169.254', '100.100.100.200', 'fd00:ec2::254']
        if ip in metadata_ips:
            return False, "Cloud metadata endpoint not allowed"

        return True, ""
    except Exception as e:
        return False, f"URL validation error: {e}"

# Regex patterns for URL detection
# Match full URLs with protocol
# The trailing character class drops the punctuation that ends a SENTENCE rather than a URL. `:`
# and `;` are there because "Content from https://www.cnn.com/:" — the header this app itself puts
# above fetched content, and the way people write "see: <url>:" — captured the colon and then
# fetched `https://www.cnn.com/:`, which answers 404. Measured in the journal as
# "Failed to fetch https://www.cnn.com/:: HTTP 404".
URL_WITH_PROTOCOL = re.compile(
    r'https?://[^\s<>"\')\]},]+[^\s<>"\')\]},.:;]',
    re.IGNORECASE
)
# Match domain-style URLs without protocol (e.g., example.com/path)
URL_WITHOUT_PROTOCOL = re.compile(
    r'(?<![/@])\b([a-zA-Z0-9][-a-zA-Z0-9]*\.)+(?:com|org|net|edu|gov|io|co|info|biz|me|tv|us|uk|de|fr|jp|cn|ru|br|au|in|nl|se|no|fi|dk|pl|cz|ch|at|be|es|it|pt|ca|mx|ar|nz|za|kr|tw|hk|sg|my|th|vn|id|ph|ae|il|tr)(?:/[^\s<>"\')\]},]*)?',
    re.IGNORECASE
)

class SearchService:
    def __init__(self, db: Session):
        self.db = db
        self._load_settings()

    def _load_settings(self):
        # The CONFIGURED value only. The bundled-instance probe happens per search (in `base()`), off
        # the event loop — resolving it here would put a network probe in every constructor, including
        # the many that never search at all (fetch_url_content, extract_urls).
        #
        # Trailing slash stripped in _configured_url, once: every call site builds f"{base}/search",
        # so a URL pasted from a browser (which always carries the slash) would otherwise request
        # `//search` — served by some instances and 404'd by others, i.e. a config that works on one
        # node and not the next. It also drops the retired hardcoded default (see LEGACY_SEARXNG_URLS).
        self.searxng_url = _configured_url()

    async def base(self) -> str:
        """Where THIS search goes: nowhere when search is switched off, else the configured instance,
        else the bundled one, else the public fallback. The probe is sync, so it runs off-thread —
        1.5s on the single uvicorn worker is not a thing to spend on a cold cache."""
        if not search_enabled():
            return ""
        if self.searxng_url:
            return self.searxng_url
        import asyncio as _asyncio
        try:
            local = await _asyncio.to_thread(local_searxng_url)
        except Exception:
            local = ""
        return local or DEFAULT_SEARXNG_URL

    async def web_search(
        self,
        query: str,
        limit: int = 5,
        categories: Optional[str] = None,
        time_range: Optional[str] = None,
        sort_recent: bool = False,
    ) -> list[dict]:
        """Search the web using SearXNG.

        categories: optional SearXNG category (e.g. "news", "videos", "science").
        time_range: optional SearXNG time filter ("day", "week", "month", "year").
        """
        base = await self.base()
        if not base:
            return []

        params = {
            "q": query,
            "format": "json",
            "language": "en",
        }
        if categories:
            params["categories"] = categories
        if time_range:
            params["time_range"] = time_range

        # Caller decides recency sort (e.g. news). When on, sort the most-recent first (SearXNG
        # returns `publishedDate` for some engines but doesn't globally sort by it).

        async with httpx.AsyncClient(timeout=30, transport=search_transport(base)) as client:
            try:
                response = await client.get(
                    f"{base}/search",
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])

                if sort_recent:
                    from datetime import datetime, timezone as _tz

                    def _pub(r):
                        d = r.get("publishedDate")
                        if not d:
                            return None
                        try:
                            dt = datetime.fromisoformat(str(d).replace("Z", "+00:00"))
                            return dt if dt.tzinfo else dt.replace(tzinfo=_tz.utc)
                        except Exception:
                            return None
                    # Sort the FULL list (before trimming) so the newest survive: dated newest-first,
                    # then undated results in their original (relevance) order.
                    dated = [r for r in results if _pub(r) is not None]
                    undated = [r for r in results if _pub(r) is None]
                    dated.sort(key=_pub, reverse=True)
                    results = dated + undated

                results = results[:limit]
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", "")[:300] if r.get("content") else "",
                        "published": r.get("publishedDate", "") or "",
                    }
                    for r in results
                ]
            except Exception as e:
                logger.error(f"Search error: {e}")
                return []

    # Categories the interactive Web Search UI is allowed to ask SearXNG for. An allowlist rather
    # than a passthrough: `categories` reaches a third-party instance verbatim, and the client is
    # not the place to decide what this node queries.
    BROWSE_CATEGORIES = ("general", "news", "images", "videos", "music",
                         "science", "it", "files", "social media", "map")
    BROWSE_TIME_RANGES = ("day", "week", "month", "year")

    async def search_page(
        self,
        query: str,
        category: str = "general",
        time_range: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict:
        """One PAGE of SearXNG results, for the browsable Web Search screen.

        Deliberately separate from `web_search`, which is the LLM's tool: that one trims content to
        300 chars, has no pagination and no thumbnails, and every AI path in the app depends on its
        exact shape. Widening it to serve a UI is how a "search results look odd" change turns into
        the model reading different snippets than it used to.

        Returns a dict (never raises): `error` is a string when the search itself failed, so the UI
        can say "search is not configured" instead of silently showing an empty page — which reads
        as "no results for your query" and is the wrong answer.
        """
        base = await self.base()
        if not base:
            return {"results": [], "answers": [], "suggestions": [],
                    "error": ("web search is turned off for this instance (Admin → Tools)"
                              if not search_enabled() else "no SearXNG instance configured")}

        category = (category or "general").strip().lower()
        if category not in self.BROWSE_CATEGORIES:
            category = "general"
        time_range = (time_range or "").strip().lower() or None
        if time_range and time_range not in self.BROWSE_TIME_RANGES:
            time_range = None
        page = max(1, min(int(page or 1), 20))

        params = {
            "q": query,
            "format": "json",
            "language": "en",
            "categories": category,
            "pageno": str(page),
            # Thumbnails come back as URLs on the SearXNG host, so the browser never asks the
            # image's own server for it — the same reason image_search sets it.
            "image_proxy": "1",
        }
        if time_range:
            params["time_range"] = time_range

        async with httpx.AsyncClient(timeout=30, transport=search_transport(base)) as client:
            try:
                response = await client.get(f"{base}/search", params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                logger.warning("Web search failed (%s): %s", query, e)
                return {"results": [], "answers": [], "suggestions": [], "error": "search request failed"}

        out = []
        for r in (data.get("results") or [])[:limit]:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            thumb = ((r.get("thumbnail_src") or "").strip()
                     or (r.get("thumbnail") or "").strip()
                     or (r.get("img_src") or "").strip())
            if thumb and not (thumb.startswith("http") or thumb.startswith("data:")):
                thumb = ""      # a relative/odd src is not something the page should try to load
            out.append({
                "title": (r.get("title") or url)[:300],
                "url": url,
                "content": (r.get("content") or "")[:600],
                "engine": (r.get("engine") or "")[:60],
                "published": str(r.get("publishedDate") or "")[:40],
                "thumbnail": thumb,
                # Images-category results need the FULL image, not just a thumb, to open at size.
                "img_src": ((r.get("img_src") or "").strip() if category == "images" else ""),
                "length": str(r.get("length") or "")[:20],
            })

        answers = []
        for a in (data.get("answers") or [])[:3]:
            # SearXNG 1.x answers are dicts ({answer, url, …}); older builds returned plain strings.
            txt = (a.get("answer") if isinstance(a, dict) else a) or ""
            if txt:
                answers.append(str(txt)[:800])
        suggestions = [str(s)[:100] for s in (data.get("suggestions") or [])[:8]]

        return {"results": out, "answers": answers, "suggestions": suggestions, "error": None}

    async def image_search(self, query: str, limit: int = 10) -> list[dict]:
        """Search for images using SearXNG. Only return results with a non-empty thumbnail URL."""
        base = await self.base()
        if not base:
            return []

        async with httpx.AsyncClient(timeout=30, transport=search_transport(base)) as client:
            try:
                response = await client.get(
                    f"{base}/search",
                    params={
                        "q": query,
                        "format": "json",
                        "categories": "images",
                        "image_proxy": "1",
                    }
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])[:limit * 2]
                out = []
                for r in results:
                    thumb = (
                        (r.get("thumbnail_src") or "").strip()
                        or (r.get("img_src") or "").strip()
                        or (r.get("thumbnail") or "").strip()
                    )
                    if not thumb or (not thumb.startswith("http") and not thumb.startswith("data:")):
                        continue
                    url = (r.get("url") or "").strip() or thumb
                    out.append({
                        "title": (r.get("title") or "Image")[:200],
                        "url": url,
                        "img_src": thumb,
                    })
                    if len(out) >= limit:
                        break
                return out[:limit]
            except Exception as e:
                logger.error(f"Image search error: {e}")
                return []

    # Maps a SearXNG category to the trigger words that imply it.
    # Order matters: earlier categories win when multiple match.
    _CATEGORY_KEYWORDS = {
        "news": ("news", "headline", "headlines", "breaking", "latest on",
                 "press release", "current events"),
        "files": ("torrent", "torrents", "magnet", "iso", "download",
                  "downloads", "files"),
        "videos": ("video", "videos", "clip", "clips", "footage", "trailer"),
        "music": ("song", "songs", "lyrics", "album", "music"),
        "science": ("paper", "papers", "study", "studies", "research",
                    "journal", "publication", "preprint"),
        "it": ("github", "stackoverflow", "stack overflow", "source code",
               "repository", "pip package", "npm package", "man page"),
        "map": ("map", "directions", "near me", "nearby", "route to"),
        "social media": ("reddit", "subreddit", "tweet", "tweets", "mastodon",
                         "lemmy", "hacker news"),
    }

    # Time-range trigger words -> SearXNG time_range value.
    _TIME_KEYWORDS = {
        "day": ("today", "past 24 hours", "last 24 hours", "past day"),
        "week": ("this week", "past week", "last week"),
        "month": ("this month", "past month", "last month"),
        "year": ("this year", "past year", "last year"),
    }

    @classmethod
    def detect_search_intent(cls, query: str) -> tuple[str, Optional[str], Optional[str]]:
        """Heuristically infer the SearXNG category and time range from a natural
        query. Returns (clean_query, categories, time_range).

        The category trigger word is kept in the query (SearXNG ranks fine with
        it, and stripping risks dropping meaningful terms like "music" in a band
        name) — only the category/time filters are derived from it.
        """
        lowered = f" {query.lower()} "

        categories = None
        for category, keywords in cls._CATEGORY_KEYWORDS.items():
            if any(f" {kw} " in lowered for kw in keywords):
                categories = category
                break

        time_range = None
        for tr, keywords in cls._TIME_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                time_range = tr
                break

        # "latest"/"recent"/"now" imply recency without a specific window → bias to the past week.
        if time_range is None and any(kw in lowered for kw in (" latest ", " recent ", " right now ", " just now ")):
            time_range = "week"

        # News with no explicit window: default to the past month so stale articles drop off while
        # the newest-first sort (in web_search) surfaces the freshest. Keeps news "by latest".
        if categories == "news" and time_range is None:
            time_range = "month"

        return query.strip(), categories, time_range

    # WHAT WE READ HAS TO OUTRANK WHAT THE MODEL ALREADY "KNOWS", and where it sits in the message
    # decides that. Measured: with the question first and 4709 chars of a fetched profile appended
    # after it — a crypto-anarchist meme poster called Jordan S — the answer's first token was
    # "Jordan Peterson is definitely an asshole", a real public figure the page never mentions. The
    # page was fetched correctly; it was read as an appendix to a question the model had already
    # answered from its priors. So the content goes FIRST and the question LAST, which is the order
    # the bare-URL summarize path here has always used, and the reason it never had this bug.
    # "Sorry, I can't access external links. But from his activity…" — reported with the whole
    # profile in the prompt, and then it invented the activity. The link WAS opened, by this app,
    # before the model ever saw the message; a model that says otherwise is refusing a question it
    # has the answer to and then filling the gap from priors, which is the worst of both. So the
    # note says the reading already happened, in the first sentence, and forbids the refusal.
    GROUNDING_NOTE = (
        "The text above was fetched just now, for you, from the link(s) in the message below — it "
        "is that page's own content, already retrieved. You are NOT being asked to browse, so "
        "never reply that you cannot access links or open URLs: the page is right there. Answer "
        "ONLY from it. If a name in it also belongs to someone famous, that is a coincidence: the "
        "text above is about the person or subject at the link, never the famous one, and you must "
        "not bring in anything you know about that name. If the text above does not answer the "
        "question, say exactly that — do not fill the gap with what you already believe.")

    @classmethod
    def build_grounded_message(cls, user_text: str, url_context: str,
                               instruction: Optional[str] = None) -> str:
        """The user turn for a message whose links were fetched: content, grounding rule, question.

        One helper for all three chat surfaces (web UI, the OpenAI-compatible API and Telegram),
        which had three different orderings of the same three pieces and so three different
        behaviours from the same fetch.
        """
        parts = [(url_context or "").strip(), "", cls.GROUNDING_NOTE]
        if instruction:
            parts.append(instruction)
        if (user_text or "").strip():
            parts.append(f"\nThe user's message: {user_text.strip()}")
        return "\n".join(parts)

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """Extract URLs from text, including those without http:// prefix"""
        urls = []

        # Find URLs with protocol
        for url in URL_WITH_PROTOCOL.findall(text):
            urls.append(url)

        # Find URLs without protocol and add https://
        for match in URL_WITHOUT_PROTOCOL.finditer(text):
            url = match.group(0)
            full_url = f"https://{url}"
            # Don't add if we already have this URL with protocol
            if full_url not in urls and f"http://{url}" not in urls:
                urls.append(full_url)

        # Deduplicate while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            normalized = url.lower().rstrip('/')
            if normalized not in seen:
                seen.add(normalized)
                unique_urls.append(url)
        return unique_urls

    async def _fetch_youtube_content(self, url: str, max_length: int = 15000) -> dict:
        """Return a fetch_url_content-shaped dict for a YouTube link using its TRANSCRIPT.

        The watch-page HTML has no spoken content, so summarizing/posting from it makes the LLM
        hallucinate. When there is no transcript we return empty content + an error so callers
        don't invent one. Title is best-effort from the page <title>."""
        import asyncio as _asyncio
        from app.services import youtube_service as _yt

        vid = _yt.extract_video_id(url)
        transcript = await _asyncio.to_thread(_yt.get_transcript, vid) if vid else None

        title = "YouTube video"
        try:  # best-effort page title (the watch-page <title> is the video title)
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and r.text:
                    t = BeautifulSoup(r.text, "lxml").title
                    if t:
                        cand = t.get_text(strip=True)
                        if cand.endswith(" - YouTube"):
                            cand = cand[: -len(" - YouTube")].strip()
                        title = cand or title
        except Exception:
            pass

        if transcript:
            return {"url": url, "title": title, "content": transcript[:max_length], "error": None}
        return {"url": url, "title": title, "content": "",
                "error": "no transcript/captions available for this video"}

    async def fetch_url_raw(self, url: str, max_bytes: int = 3_000_000) -> dict:
        """The page's OWN html, unextracted — for rendering it rather than summarizing it.

        Separate from `fetch_url_content` on purpose: that one returns text for a model to read, and
        every AI path in the app depends on its shape. This one is the input to the Web Search
        reader's page view, so it keeps the markup (and reports the URL it ENDED on, which is what
        relative links have to resolve against).

        Same SSRF guard, re-checked on every redirect hop — a search result is a URL this node did not
        choose, and rendering it is not a reason to trust it any further than summarizing it.
        Returns {url, html, content_type, error}; `error` is set instead of raising.
        """
        ok, why = is_safe_url(url)
        if not ok:
            return {"url": url, "html": "", "content_type": "", "error": f"URL blocked: {why}"}

        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
                current, hops = url, 0
                while True:
                    # STREAMED, so `max_bytes` is a real limit rather than a slice taken after the
                    # fact. `client.get()` reads the whole body into memory first: a result URL this
                    # node did not choose could point at a multi-hundred-MB file, and the single
                    # uvicorn worker would buffer all of it — and then reject it as "not a web page".
                    # Now the content-type is checked from the HEADERS, and the read stops at the cap.
                    async with client.stream("GET", current, headers=headers) as r:
                        if r.status_code in (301, 302, 303, 307, 308):
                            loc = r.headers.get("location")
                            if loc:
                                hops += 1
                                if hops > 5:
                                    return {"url": current, "html": "", "content_type": "",
                                            "error": "too many redirects"}
                                current = str(httpx.URL(current).join(loc))
                                ok, why = is_safe_url(current)
                                if not ok:
                                    logger.warning("SSRF blocked (redirect): %s -> %s - %s", url, current, why)
                                    return {"url": url, "html": "", "content_type": "",
                                            "error": f"URL blocked: {why}"}
                                continue
                        ctype = (r.headers.get("content-type") or "").lower()
                        if "html" not in ctype:
                            # A PDF or an image is not something to re-serve through here; the client
                            # offers the original link for those.
                            return {"url": current, "html": "", "content_type": ctype,
                                    "error": f"not a web page ({ctype.split(';')[0] or 'unknown type'})"}
                        chunks, total = [], 0
                        async for chunk in r.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= max_bytes:
                                break
                        enc = r.encoding or "utf-8"
                        # Carry the STATUS. A 403 bot-challenge page is perfectly valid HTML, so
                        # without it the caller cannot tell "the site refused us" from "the site is
                        # built by JavaScript" — and it told users the latter for both.
                        return {"url": current, "html": b"".join(chunks)[:max_bytes].decode(enc, errors="replace"),
                                "content_type": ctype, "status": r.status_code, "error": None}
        except Exception as e:
            logger.info("page fetch failed for %s: %s", url, e)
            return {"url": url, "html": "", "content_type": "", "error": str(e)[:200]}

    async def fetch_asset(self, url: str, max_bytes: int = 8_000_000, allow: tuple = ()) -> dict:
        """One SUBRESOURCE of a framed page — a stylesheet, an image, a font.

        Same SSRF guard per redirect hop as everything else here, streamed against a hard cap, and
        limited to types a document lays itself out with. Returns {url, body, content_type, error}.
        """
        ok, why = is_safe_url(url)
        if not ok:
            return {"url": url, "body": b"", "content_type": "", "error": f"URL blocked: {why}"}
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "*/*",
        }
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                current, hops = url, 0
                while True:
                    async with client.stream("GET", current, headers=headers) as r:
                        if r.status_code in (301, 302, 303, 307, 308):
                            loc = r.headers.get("location")
                            if loc:
                                hops += 1
                                if hops > 4:
                                    return {"url": current, "body": b"", "content_type": "",
                                            "error": "too many redirects"}
                                current = str(httpx.URL(current).join(loc))
                                ok, why = is_safe_url(current)
                                if not ok:
                                    return {"url": url, "body": b"", "content_type": "",
                                            "error": f"URL blocked: {why}"}
                                continue
                        if r.status_code >= 400:
                            return {"url": current, "body": b"", "content_type": "",
                                    "error": f"upstream {r.status_code}"}
                        ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                        if allow and not any(ctype.startswith(a) for a in allow):
                            return {"url": current, "body": b"", "content_type": ctype,
                                    "error": f"type not served here ({ctype or 'unknown'})"}
                        chunks, total = [], 0
                        async for chunk in r.aiter_bytes():
                            chunks.append(chunk)
                            total += len(chunk)
                            if total >= max_bytes:
                                break
                        return {"url": current, "body": b"".join(chunks)[:max_bytes],
                                "content_type": r.headers.get("content-type") or ctype, "error": None}
        except Exception as e:
            logger.info("asset fetch failed for %s: %s", url, e)
            return {"url": url, "body": b"", "content_type": "", "error": str(e)[:200]}

    async def fetch_url_content(self, url: str, max_length: int = 15000,
                                allow_render: bool = True) -> Optional[dict]:
        """Fetch and extract text content from a URL.

        `allow_render=False` forbids the headless-browser fallback below — `fetch_urls` spends it on
        at most ONE url per message, because every caller wraps the whole batch in a 15s timeout and
        three renders would not fit.
        """
        did_render = False
        # YouTube *videos* need the transcript, not the watch-page HTML (which is contentless and
        # makes the LLM hallucinate). Centralised here so EVERY caller - telegram/web/
        # pleroma and the summarize & post commands - gets it via fetch_urls()
        # automatically. Only intercept actual video URLs (watch/shorts/embed/youtu.be); channel,
        # search and playlist pages have no video id and fall through to normal HTML fetch.
        try:
            from app.services import youtube_service as _yt
            if _yt.extract_video_id(url):
                return await self._fetch_youtube_content(url, max_length)
        except Exception as _yt_err:
            logger.warning(f"YouTube transcript path failed for {url}: {_yt_err}")

        # SSRF protection: validate URL before fetching
        is_safe, error_msg = is_safe_url(url)
        if not is_safe:
            logger.warning(f"SSRF blocked: {url} - {error_msg}")
            return {
                "url": url,
                "title": url,
                "content": "",
                "error": f"URL blocked: {error_msg}"
            }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        # follow_redirects=False + a hand-rolled hop loop, NOT because redirects are unwanted (most
        # article URLs take one) but because `is_safe_url` above only ever saw the FIRST url. Letting
        # httpx follow made the guard advisory: `https://attacker.example/r` → 302 →
        # `http://169.254.169.254/latest/meta-data/` was fetched and its body handed back to whoever
        # asked. That matters more now that a search RESULT — a URL this node did not choose — can be
        # the input, via Web Search's reader and its two summarize endpoints.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            try:
                response, hops = None, 0
                next_url = url
                while True:
                    response = await client.get(next_url, headers=headers)
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    loc = response.headers.get("location")
                    if not loc:
                        break
                    hops += 1
                    if hops > 5:
                        return {"url": url, "title": url, "content": "", "error": "too many redirects"}
                    # Relative Location is legal and common; resolve it against the hop we are on.
                    next_url = str(httpx.URL(next_url).join(loc))
                    ok, why = is_safe_url(next_url)
                    if not ok:
                        logger.warning("SSRF blocked (redirect): %s -> %s - %s", url, next_url, why)
                        return {"url": url, "title": url, "content": "",
                                "error": f"URL blocked: {why}"}
                response.raise_for_status()

                content_type = response.headers.get("content-type", "").lower()

                # Only process HTML content
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    return {
                        "url": url,
                        "title": url,
                        "content": f"[Non-HTML content: {content_type}]",
                        "error": None
                    }

                html = response.text
                soup = BeautifulSoup(html, "lxml")

                # Remove script, style, nav, footer, and other non-content elements
                for element in soup(["script", "style", "nav", "footer", "header",
                                     "aside", "noscript", "iframe", "form", "button"]):
                    element.decompose()

                # Get title
                title = ""
                if soup.title:
                    title = soup.title.get_text(strip=True)

                # Try to find main content area
                main_content = None
                for selector in ["main", "article", "[role='main']", "#content", ".content", "#main", ".main"]:
                    main_content = soup.select_one(selector)
                    if main_content:
                        break

                # Fall back to body if no main content area found
                if not main_content:
                    main_content = soup.body or soup

                # Extract links with their text (for news sites)
                from urllib.parse import urlparse
                parsed_url = urlparse(url)
                base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
                links_text = []
                seen_texts = set()  # Avoid duplicate headlines

                # Words that indicate navigation rather than article headlines
                nav_patterns = ['trending', 'live updates', 'breaking', 'watch', 'listen',
                               'subscribe', 'sign in', 'log in', 'menu', 'search', 'more']

                for a in main_content.find_all('a', href=True):
                    # Use separator to prevent text mashing
                    link_text = a.get_text(separator=' ', strip=True)
                    # Clean up multiple spaces
                    link_text = ' '.join(link_text.split())
                    href = a['href']

                    # Skip empty, anchor-only, or very short links
                    if not link_text or len(link_text) < 15 or href.startswith('#'):
                        continue

                    # Skip navigation-like links
                    text_lower = link_text.lower()
                    if any(nav in text_lower for nav in nav_patterns):
                        continue

                    # Skip if just numbers, ranking-prefixed (e.g. "7 Avalanche AVAX"), or very generic
                    if link_text.isdigit() or text_lower in ['read more', 'click here', 'learn more']:
                        continue
                    if re.match(r'^\d+[\s.]', link_text):
                        continue

                    # Skip duplicate headlines
                    if text_lower in seen_texts:
                        continue
                    seen_texts.add(text_lower)

                    # Make relative URLs absolute
                    if href.startswith('/'):
                        href = base_url + href
                    elif not href.startswith('http'):
                        continue

                    # Format as markdown link
                    links_text.append(f"- [{link_text}]({href})")

                # Extract plain text as fallback
                text = main_content.get_text(separator="\n", strip=True)

                # Clean up whitespace and filter JS/data garbage
                raw_lines = [line.strip() for line in text.split("\n") if line.strip()]
                filtered = []
                seen = set()
                for line in raw_lines:
                    # Deduplicate identical lines (repeated table rows, nav labels)
                    key = line.lower()
                    if key in seen:
                        continue
                    seen.add(key)

                    # Skip very short tokens (nav labels, button text, currency symbols)
                    if len(line) < 8:
                        continue

                    # Skip lines that are purely numeric/symbolic (price rows, rank numbers, percentages)
                    if re.match(r'^[\d\s$%,.\-+/\\*:]+$', line):
                        continue

                    # Skip lines starting with a rank/index number or percentage rating
                    # e.g. "1 Bitcoin", "42 Solana", "100% accurate", "75% True"
                    if re.match(r'^\d+[\s.%]', line):
                        continue

                    # Skip question lines — FAQ questions cause the model to enter a Q&A loop,
                    # hallucinating follow-up questions and answers after the real response.
                    if line.endswith('?'):
                        continue

                    # Skip lines that look like JS code
                    # — long unbroken strings (minified JS, base64, data URIs)
                    if len(line) > 120 and " " not in line:
                        continue
                    # — high density of JS/JSON special characters
                    js_chars = sum(1 for c in line if c in "{}[]();=>|\\")
                    if len(line) > 0 and js_chars / len(line) > 0.12:
                        continue
                    # — starts with JS keywords
                    js_kw = ("var ", "let ", "const ", "function ", "return ",
                             "import ", "export ", "window.", "document.",
                             "module.", "require(", "__NEXT", "undefined", "null,")
                    if any(line.lstrip().startswith(kw) for kw in js_kw):
                        continue

                    # Skip lines that are predominantly non-alphabetic
                    # (price rows, percentage tables, raw numbers)
                    alpha = sum(1 for c in line if c.isalpha())
                    if len(line) > 0 and alpha / len(line) < 0.30:
                        continue

                    filtered.append(line)

                text = "\n".join(filtered)

                # Prepend extracted links if we found any
                if links_text:
                    links_section = "\n".join(links_text[:8])
                    text = f"ARTICLE LINKS:\n{links_section}\n\nPAGE TEXT:\n{text}"

                # NOTHING CAME OUT — so read the page the way a browser reads it. What the server
                # sent is only the whole page on a server-rendered site; anywhere else it is a shell
                # whose text arrives when the page's own JavaScript runs. Measured here, an SPA
                # route extracts ZERO characters while the thinnest real page extracts ~2800, and
                # zero characters is the shape that makes a model answer from its priors instead.
                # Generic on purpose: the fallback knows nothing about the site, only that this one
                # said nothing.
                if allow_render and page_render.looks_unrendered(text):
                    rendered = await asyncio.to_thread(
                        page_render.render_page_text, str(response.url),
                        page_render.RENDER_TIMEOUT,
                        lambda u: is_safe_url(u)[0])
                    if rendered:
                        r_title, r_text = rendered
                        logger.info("Rendered %s in a browser: %d chars (server sent %d)",
                                    url, len(r_text), len(text))
                        text, title, did_render = r_text, (title or r_title), True

                # Truncate if too long
                if len(text) > max_length:
                    text = text[:max_length] + "\n\n[Content truncated...]"

                return {
                    "url": url,
                    "title": title or url,
                    "content": text,
                    "error": None,
                    "rendered": did_render,
                }

            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP error fetching {url}: {e.response.status_code}")
                return {
                    "url": url,
                    "title": url,
                    "content": "",
                    "error": f"HTTP {e.response.status_code}"
                }
            except Exception as e:
                logger.warning(f"Error fetching {url}: {e}")
                return {
                    "url": url,
                    "title": url,
                    "content": "",
                    "error": str(e)
                }

    async def fetch_urls(self, urls: list[str], max_urls: int = 3) -> list[dict]:
        """Fetch content from multiple URLs.

        The browser fallback is spent at most ONCE per message: it costs seconds, and the callers
        give the whole batch 15s. A message with three JS-rendered links reads the first one
        properly rather than timing out and reading none of them.
        """
        results = []
        rendered_one = False
        for url in urls[:max_urls]:
            result = await self.fetch_url_content(url, allow_render=not rendered_one)
            if result:
                rendered_one = rendered_one or bool(result.get("rendered"))
                results.append(result)
        return results


def get_search_service(db: Session) -> SearchService:
    return SearchService(db)

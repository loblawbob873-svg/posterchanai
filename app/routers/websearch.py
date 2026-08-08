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
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.search_service import get_search_service
from app.services.text_utils import strip_thinking_tags

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
        text = strip_thinking_tags(content).strip()
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

"""News RSS reader endpoints (client-facing). Thin wrapper over app.services.rss_service — fetch via the
built-in proxy (→ Tor) with a direct fallback, structured JSON, shared cache. Public;
the SSRF guard restricts targets to real external hosts. Per-user feed list + read state live as Nostr
events on the client, so nothing user-specific is stored here."""
import asyncio

from fastapi import APIRouter, Query, HTTPException

from app.services import rss_service

router = APIRouter(prefix="/api/rss", tags=["rss"])


async def _safe(url: str) -> str:
    # cheap syntactic gate only (NO DNS) — the authoritative resolve-based SSRF check runs at fetch time
    # inside rss_service (per redirect hop), so a cache HIT costs no getaddrinfo → scales to many users.
    url = rss_service.normalize_url(url)
    return url if url and rss_service.looks_fetchable(url) else ""


@router.get("/feed")
async def feed(url: str = Query(..., max_length=2048), force: bool = False):
    """One RSS/Atom feed → {url, title, items:[{id,title,link,ts,snippet,image}], error, cached}."""
    safe = await _safe(url)
    if not safe:
        raise HTTPException(status_code=400, detail="invalid or disallowed feed URL")
    return await rss_service.get_feed(safe, force=force)


@router.get("/feeds")
async def feeds(urls: str = Query(..., description="comma-separated feed URLs", max_length=8192)):
    """Several feeds at once (the 'All' view), fetched CONCURRENTLY and each served from the shared cache."""
    raw = [u.strip() for u in (urls or "").split(",") if u.strip()][:30]
    safe = await asyncio.gather(*[_safe(u) for u in raw])
    out, todo = [], []
    for orig, s in zip(raw, safe):
        if s:
            todo.append((orig, s))
        else:
            out.append({"url": orig, "title": orig, "items": [], "error": "invalid or disallowed feed URL"})
    results = await asyncio.gather(*[rss_service.get_feed(s) for (_o, s) in todo], return_exceptions=True)
    for (orig, s), r in zip(todo, results):
        out.append(r if isinstance(r, dict) else {"url": orig, "title": orig, "items": [], "error": str(r)})
    return {"feeds": out}

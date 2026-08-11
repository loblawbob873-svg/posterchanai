"""Markets digest endpoint (client-facing). Thin wrapper over markets_service — returns the shared daily
crypto price+news report (server-generated at 08:00, cached as an operator-signed Nostr doc). Public read,
like /api/rss/*: it exposes no user data and can trigger at most one background generation (single-flight)."""
from fastapi import APIRouter

from app.services import markets_service

router = APIRouter(prefix="/api/markets", tags=["markets"])


@router.get("")
async def markets():
    """The shared daily crypto digest: {generated_at, coins:[{sym,name,summary,articles}], generating?}."""
    return await markets_service.get_report()


@router.get("/prices")
async def prices():
    """Live-ish prices for the desktop ticker: {at, prices:{SYM:{usd,chg24h}}, stale?}.

    Separate from the digest above because they refresh on completely different terms — the digest is
    generated twice a day with an LLM, this is one cached upstream call every 90s, shared by every
    viewer. Public read like the digest: it is a public price feed and carries no user data."""
    return await markets_service.get_prices()

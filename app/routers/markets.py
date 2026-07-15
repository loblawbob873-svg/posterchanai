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

"""Bitcoin block data for the desktop widget. Thin wrapper over mempool_service, which owns the
caching and the reason this is proxied at all (the client's IP never reaches the upstream).

Public read, like /api/markets and /api/weather: it is public chain data and carries nothing of the
caller's. There is no parameter, so there is nothing to validate and nothing to key a cache on — the
node holds exactly one answer at a time."""
from fastapi import APIRouter

from app.services import mempool_service

router = APIRouter(prefix="/api/mempool", tags=["mempool"])


@router.get("/blocks")
async def blocks():
    """Projected (pending) blocks and the most recent confirmed ones, reduced to what a tile draws."""
    return await mempool_service.blocks()

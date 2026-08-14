"""`POST /api/search` — the node-to-node half of the search load balancer.

A peer calls this to have THIS node run a search on its own SearXNG, with its own outgoing proxy and
its own engine-suspension state. It is the search equivalent of `/api/generate-image` and
`/api/generate-music`, and it is authorized the same way (`lb_auth`), for the same reason: there is
no user on the other side, just another node doing work.

**It calls `web_search_local`, never `web_search`.** That single choice is the loop guard for the
whole feature — `web_search` is the balanced entry point, so calling it here would let A→B become
B→A→B and back. A header hop-count would not do: the caller sets the headers.

Why this exists at all rather than peers talking to each other's SearXNG directly: the bundled
instance is deliberately unreachable from off-box. `/searxng` is gated on loopback AND the absence of
a forwarded-for header (behind nginx the peer IS 127.0.0.1, so the address alone proves nothing), and
`posterchanai-searxng.service` binds loopback. Opening either of those to peers would publish an
unauthenticated, limiter-disabled metasearch instance that makes outbound requests on demand carrying
this node's IP. So the search is asked for through the app, which already knows how to authenticate a
peer.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
# The SAME peer/API-key/JWT check the image and music endpoints use, imported rather than
# reimplemented. Authentication is the last place to grow a second, subtly different copy — and this
# endpoint has exactly the shape that one is written for: a peer with no user behind it.
from app.routers.image_api import get_image_auth as peer_or_user_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])


class SearchRequest(BaseModel):
    query: str
    limit: int = 5
    categories: Optional[str] = None
    time_range: Optional[str] = None
    sort_recent: bool = False


@router.post("/search")
async def api_search(
    body: SearchRequest,
    _ok: bool = Depends(peer_or_user_auth),
    db: Session = Depends(get_db),
):
    """Run the search HERE and return the raw result rows.

    Errors are deliberately NOT swallowed into an empty list: the caller's whole reason for asking a
    peer is to tell "nothing matched" from "that node could not search", and `[]` cannot say both.
    `web_search_local` raises, this becomes a 502, and the calling node moves on to the next node.
    """
    from app.services.search_service import get_search_service
    service = get_search_service(db)
    try:
        results = await service.web_search_local(
            body.query,
            limit=body.limit,
            categories=body.categories,
            time_range=body.time_range,
            sort_recent=body.sort_recent,
        )
    except Exception as exc:
        logger.warning("[search-api] local search failed: %s", exc)
        raise HTTPException(status_code=502, detail="search failed on this node")
    return {"results": results}

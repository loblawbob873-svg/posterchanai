"""/api/music/cover -- the Music library's opt-in online album-cover lookup (app/services/music_cover_service.py).

Thin by design: the member gate, a per-user rate limit, and a status code per answer --
  POST /api/music/cover {artist, album}  200 {"id": <MusicBrainz id>} | 404 (there is no cover) | 503 (could not ask)
  GET  /api/music/cover/<id>             200 image bytes             | 404                        | 503
The client stores the id in the track's encrypted record, so an album is looked up once; 503 is never "no cover".

A POST with a BODY, not a GET with a query: the access log (uvicorn's, and the reverse proxy's in front of it)
writes every request URL, and the artist and album somebody listens to must not end up in a journal.
"""
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services import music_cover_service as svc
from app.services.user_table import UserRow as User

router = APIRouter(prefix="/api/music", tags=["music-library"])

RATE_N, RATE_WINDOW = 60, 60.0          # per user: a library scan asks one album at a time, ~1/s at most
_recent: dict = defaultdict(deque)


async def get_music_member(current_user: User = Depends(get_current_user)):
    # The same gate as the rest of the members' apps (Web Search, Mail, Files): a name this node granted.
    from app.services.instance_membership import require_user
    return await require_user(current_user)


def _rate(user: User) -> None:
    q, now = _recent[getattr(user, "id", None) or id(user)], time.monotonic()
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_N:
        raise HTTPException(status_code=429, detail="Too many cover lookups — try again in a minute.",
                            headers={"Retry-After": str(int(RATE_WINDOW - (now - q[0])) + 1)})
    q.append(now)


def _unavailable():
    return JSONResponse({"detail": "Could not reach the cover service — nothing was saved; try again later."},
                        status_code=503, headers={"Retry-After": "120"})


class CoverAsk(BaseModel):
    artist: str = Field(..., min_length=1, max_length=300)
    album: str = Field(..., min_length=1, max_length=300)


@router.post("/cover")
async def cover_lookup(ask: CoverAsk, user: User = Depends(get_music_member)):
    _rate(user)
    got = await svc.lookup(ask.artist, ask.album)
    if got.kind == "ok":
        return {"id": got.id}
    if got.kind == "none":
        return JSONResponse({"detail": "No cover found for that album."}, status_code=404)
    return _unavailable()


@router.get("/cover/{cover_id}")
async def cover_image(cover_id: str, user: User = Depends(get_music_member)):
    if not svc.valid_id(cover_id):
        raise HTTPException(status_code=404, detail="No such cover.")
    _rate(user)
    kind, data, mime = await svc.cover(cover_id)
    if kind == "ok":
        return Response(data, media_type=mime,
                        headers={"Cache-Control": "private, max-age=604800", "X-Content-Type-Options": "nosniff"})
    if kind == "none":
        return JSONResponse({"detail": "No such cover."}, status_code=404)
    return _unavailable()

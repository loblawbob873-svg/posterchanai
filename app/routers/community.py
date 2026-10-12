"""Community facts for the bots that post about this node (block bot, daily stats) -- see
app/services/community_stats.py.

NOT the ordinary bot-API auth: who blocked whom is private on the fediverse, so any member's API key
reading it (or a script with one) would be a leak. Only the key the bots are configured with, an
admin's key, or a peer node may ask."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import community_stats
from app.utils import lb_auth


async def get_bot_auth(request: Request, x_api_key: Optional[str] = Header(None),
                       authorization: Optional[str] = Header(None),
                       x_pc_community_token: Optional[str] = Header(None),
                       db: Session = Depends(get_db)) -> bool:
    if lb_auth.is_internal(request):
        return True
    # The manager-issued bot credential (community_stats.bot_token) -- scoped to THESE endpoints.
    tok = (x_pc_community_token or "").strip()
    if tok:
        import hmac
        from app.services import community_stats
        if hmac.compare_digest(tok, community_stats.bot_token()):
            return True
    key = (x_api_key or "").strip() or ((authorization or "")[7:].strip()
                                         if (authorization or "").startswith("Bearer ") else "")
    if not key:
        raise HTTPException(401, "An API key is required")
    from app.services import bot_table, settings_store
    bot_keys = {str(settings_store.get("bots_posterchanai_api_key", "") or "").strip()}
    for cfg in [b.config for b in await bot_table.aall_bots(db)]:
        try:
            bot_keys.add(str(json.loads(cfg or "{}").get("posterchanai_api_key") or "").strip())
        except (ValueError, TypeError, AttributeError):
            continue
    bot_keys.discard("")
    if key in bot_keys:
        return True
    from app.utils.auth_utils import get_user_from_api_key, aquery_api_key
    from app.services.relay_reader import Unavailable
    try:
        api_key, user_id = await aquery_api_key(key)
        user = get_user_from_api_key(db, user_id) if api_key and user_id else None
    except Unavailable:
        # "Could not check the key" is not "not an admin's key": the bot retries a 503, and reads a
        # 403 as a configuration error.
        raise HTTPException(503, "API keys cannot be checked right now; try again shortly")
    except Exception:
        user = None
    if user is not None and user.is_admin:
        return True
    raise HTTPException(403, "Only the bots' key or an admin's key can read this")


router = APIRouter(prefix="/api/community", tags=["community"])


async def _read(coro):
    try:
        return await coro
    except Exception as e:
        # "Could not ask the relay" is never an empty answer: a bot that read [] would announce
        # nothing today and, on the next good read, every block of the last day at once.
        raise HTTPException(503, f"could not read the relay: {type(e).__name__}")


@router.get("/blocks")
async def blocks(since: int = 0, _auth: bool = Depends(get_bot_auth)):
    rows = await _read(community_stats.blocks())
    return {"blocks": [r for r in rows if r["at"] > since] if since else rows}


@router.get("/block-leaderboard")
async def block_leaderboard(min: int = 1, _auth: bool = Depends(get_bot_auth)):
    return {"leaderboard": await _read(community_stats.leaderboard(min))}


@router.get("/activity")
async def activity(top: int = 5, _auth: bool = Depends(get_bot_auth)):
    return await _read(community_stats.activity(top))


@router.get("/reports")
async def reports(since: int = 0, _auth: bool = Depends(get_bot_auth)):
    return {"reports": await _read(community_stats.reports(since))}


@router.get("/badpost-votes")
async def badpost_votes(since: int = 0, until: int = 0, _auth: bool = Depends(get_bot_auth)):
    """Bad Post of the Day votes (NIP-32 labels from the client's post menu) cast in [since, until)."""
    return {"votes": await _read(community_stats.badpost_votes(since, until))}


@router.get("/badpost-card")
async def badpost_card(id: str = "", _auth: bool = Depends(get_bot_auth)):
    """The voted-on post, plus a screenshot of it rendered by the SAME card renderer as the client's
    ⋯ → Screenshot (`_render_post_card_png`, avatar and image fetched behind its SSRF guard). `post`
    is null when the relay no longer holds it; `card` is null when nothing could render it -- the bot
    still posts then, it just has no picture of the post."""
    post = await _read(community_stats.badpost_post(id))
    if not post:
        return {"post": None, "card": None}
    import asyncio
    import base64
    import logging
    from datetime import datetime, timezone
    from urllib.parse import urlparse
    from app.routers.client import _img_data_uri
    from app.services import settings_store
    from app.services.command_service import _render_post_card_png
    site_host = (urlparse(settings_store.get("site_url") or "").hostname or "").lower()
    base_domain = ".".join(site_host.split(".")[-2:]) if site_host else ""
    card = None
    try:
        avatar = await _img_data_uri(post.get("picture") or "", base_domain)
        media = await _img_data_uri(post.get("image") or "", base_domain)
        when = datetime.fromtimestamp(post["created_at"], tz=timezone.utc).strftime("%b %d, %Y") \
            if post.get("created_at") else ""
        png = await asyncio.wait_for(asyncio.to_thread(
            _render_post_card_png, post.get("name") or post.get("handle") or "", post.get("handle") or "",
            post.get("text") or "", when, media, avatar), timeout=60)
        if png:
            card = base64.b64encode(png).decode()
    except Exception as e:
        logging.getLogger(__name__).warning(f"[community] badpost card for {post['id'][:12]} failed: "
                                            f"{type(e).__name__}")
    return {"post": post, "card": card, "content_type": "image/png"}


@router.get("/members")
async def members(_auth: bool = Depends(get_bot_auth)):
    async def roll():
        return community_stats.member_list()
    return {"members": await _read(roll())}


@router.get("/follows")
async def follows(_auth: bool = Depends(get_bot_auth)):
    """The unfollow bot's picture of who follows this instance's members (community_stats.follows)."""
    return await _read(community_stats.follows())


@router.get("/contact-lists")
async def contact_lists(authors: str = "", _auth: bool = Depends(get_bot_auth)):
    """The current contact list of up to 50 keys -- what a remembered follower who vanished from /follows
    now follows."""
    return {"lists": await _read(community_stats.contact_lists([a for a in authors.split(",") if a]))}

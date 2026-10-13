"""Concord's relay tunnel: a member ticket over HTTP, then `/ws/concord-relay` carries one relay connection out
through this node (app/services/concord_tunnel.py). The client asks for a ticket first; with none (no instance,
not a member, no proxy on this node) it dials relays itself exactly as it did before."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, WebSocket

from app.auth import get_current_user
from app.services.user_table import UserRow as User
from app.services import concord_tunnel, instance_membership

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/client/concord", tags=["concord"])
ws_router = APIRouter()


async def _member(user: User = Depends(get_current_user)):
    return await instance_membership.require_user(user)


@router.get("/tunnel-ticket")
async def tunnel_ticket(user: User = Depends(_member)):
    """{ticket, ttl} for a member while this node can carry the connections; {ticket: ''} otherwise."""
    from app.services.nostr.nostr_service import to_pubkey_hex
    pk = ""
    try:
        pk = to_pubkey_hex(getattr(user, "nostr_npub", "") or "") or ""
    except Exception:
        pk = ""
    if len(pk) != 64 or not concord_tunnel.proxy_url():
        return {"ticket": "", "ttl": 0}
    return {"ticket": concord_tunnel.issue(pk.lower()), "ttl": concord_tunnel.TICKET_TTL}


@ws_router.websocket("/ws/concord-relay")
async def concord_relay(websocket: WebSocket):
    target = websocket.query_params.get("u", "")
    pk = concord_tunnel.verify(websocket.query_params.get("t", ""))
    if not pk:
        await websocket.close(code=4401, reason="ticket")
        return
    ok, why = concord_tunnel.check_target(target)
    proxy = concord_tunnel.proxy_url() if ok else ""
    if not ok or not proxy:
        await websocket.close(code=4403, reason=(why or "no proxy")[:100])
        return
    if not concord_tunnel.acquire(pk):
        await websocket.close(code=4429, reason="too many relay connections")
        return
    try:
        try:
            upstream_cm = concord_tunnel.connect(target, proxy)
            upstream = await asyncio.wait_for(upstream_cm.__aenter__(), timeout=concord_tunnel.OPEN_TIMEOUT + 5)
        except Exception as e:
            logger.info("[concord-tunnel] could not reach a relay: %s", type(e).__name__)
            await websocket.close(code=4502, reason="relay unreachable")
            return
        await websocket.accept()
        try:
            await concord_tunnel.pipe(websocket, upstream)
        except Exception:
            pass
        finally:
            try:
                await upstream_cm.__aexit__(None, None, None)
            except Exception:
                pass
            try:
                await websocket.close()
            except Exception:
                pass
    finally:
        concord_tunnel.release(pk)

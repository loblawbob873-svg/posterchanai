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


@router.get("/default-invite")
async def default_invite(user: User = Depends(_member)):
    """The community this instance joins every member to (Admin -> Nostr Relay), or {invite: ''}.

    MEMBERS ONLY: the invite carries the community's # secret, so it is never in the public client
    config. The client joins it once per account and remembers it did, so leaving is respected."""
    from app.services import settings_store
    raw = str(settings_store.get("concord_default_invite", "") or "").strip()
    return {"invite": raw if raw.startswith(("https://", "http://")) and "/invite/" in raw and "#" in raw else ""}


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
        upstream_cm = upstream = None
        # ACCEPT FIRST, THEN DIAL. A browser keeps one websocket per host in CONNECTING and queues the rest (RFC 6455
        # 4.1), so a handshake held open while the far relay is dialled (~25s over Tor, plus the direct retry) queued
        # the app's own relay socket behind every Concord dial -- the laptop's wss://poster.place/relay sat in
        # CONNECTING for minutes on 2026-10-13. Frames the client sends meanwhile wait in the socket.
        await websocket.accept()
        # Through the proxy first; then DIRECT -- the last hop of the proxy's own Tor -> Tor -> direct chain, which
        # it takes only on a CONNECT error, never on a relay that refuses Tor after it (damus: 503) or times out
        # over it (asia.vectorapp.io, a CORD relay). Measured 2026-10-13: both answer direct.
        # Tor gets concord_tunnel.TOR_OPEN_TIMEOUT, and a host it failed for goes direct first for a while.
        for hop in concord_tunnel.hops(target, proxy):
            try:
                upstream_cm = concord_tunnel.connect(target, hop)
                upstream = await asyncio.wait_for(upstream_cm.__aenter__(), timeout=concord_tunnel.open_timeout(hop) + 1)
                if hop:
                    concord_tunnel.tor_result(target, True)
                break
            except Exception as e:
                if hop:
                    concord_tunnel.tor_result(target, False)
                logger.info("[concord-tunnel] could not reach a relay %s: %s",
                            "through the proxy" if hop else "directly", type(e).__name__)
                upstream_cm = upstream = None
        if upstream is None:
            await websocket.close(code=4502, reason="relay unreachable")
            return
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

"""Concord's relay tunnel: a member ticket over HTTP, then `/ws/concord-relay` carries one relay connection out
through this node (app/services/concord_tunnel.py). The client asks for a ticket first; with none (no instance,
not a member, no proxy on this node) it dials relays itself exactly as it did before."""
from __future__ import annotations

import asyncio
import time
import logging

from fastapi import APIRouter, Depends, WebSocket
from pydantic import BaseModel

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


@router.get("/mirror-state")
async def mirror_state(user: User = Depends(_member)):
    """{groups: {group: {authors, direct, at}}}: the Concord communities this node mirrors for THIS account
    (concord_mirror.member_state). A device with no record of its own (new, cleared, Tor Browser) reads them
    from this node's copy at once instead of tunnelling to every relay. Only the caller's own registrations."""
    from app.services.nostr.nostr_service import to_pubkey_hex
    from app.services import relay_reader
    from app.services.nostr_relay.aggregates import ask as ask_relay
    pk = ""
    try:
        pk = (to_pubkey_hex(getattr(user, "nostr_npub", "") or "") or "").lower()
    except Exception:
        pk = ""
    if len(pk) != 64:
        return {"groups": {}}
    try:
        got = await asyncio.to_thread(ask_relay, "concord-mirror-state", {"member": pk}, timeout=5.0)
    except relay_reader.Unavailable:
        from fastapi.responses import JSONResponse
        return JSONResponse({"groups": {}, "error": "the relay could not be asked"}, status_code=503)
    # `strict`: this node answers a `pcms…` REQ ("do you mirror this stream?") -- a client asks it before tunnelling.
    return {"groups": (got or {}).get("groups") or {}, "strict": True}


@router.get("/default-invite")
async def default_invite(user: User = Depends(_member)):
    """The community this instance joins every member to (Admin -> Nostr Relay), or {invite: ''}.

    MEMBERS ONLY: the invite carries the community's # secret, so it is never in the public client
    config. The client joins it once per account and remembers it did, so leaving is respected."""
    from app.services import settings_store
    raw = str(settings_store.get("concord_default_invite", "") or "").strip()
    return {"invite": raw if raw.startswith(("https://", "http://")) and "/invite/" in raw and "#" in raw else ""}


class _AmongReq(BaseModel):
    pubkeys: list[str] = []


@router.post("/blocked-among")
async def blocked_among(data: _AmongReq, user: User = Depends(_member)):
    """Which of THESE authors are blocked on this node's relay -- MEMBERS ONLY, and never the whole list.

    A relay block could not reach Concord (2026-10-21: "if we block a user at the relay level, we need to make sure
    that they can't write to our concord rooms"): rooms live on outside relays and every message is a gift wrap
    signed by a throwaway key, so no relay can tell who wrote it. The members' own clients can, after decrypting --
    so a client asks about the authors it is already showing, hides the blocked ones, and offers a community's
    owner/moderators the cryptographic ban. Who is blocked stays moderation state: a member learns it only about
    people who are in front of them anyway, 500 at a time."""
    from app.services import relay_blocklist
    want = []
    for pk in (data.pubkeys or [])[:500]:
        pk = str(pk or "").strip().lower()
        if len(pk) == 64 and all(c in "0123456789abcdef" for c in pk):
            want.append(pk)
    return {"blocked": [pk for pk in dict.fromkeys(want) if relay_blocklist.is_blocked(pk)]}


@ws_router.websocket("/ws/concord-relay")
async def concord_relay(websocket: WebSocket):
    target = websocket.query_params.get("u", "")
    pk = concord_tunnel.verify(websocket.query_params.get("t", ""))
    if not pk:
        await websocket.close(code=4401, reason="ticket")
        return
    ok, why = concord_tunnel.check_target(target)
    proxy = concord_tunnel.proxy_url() if ok else ""
    # A REFUSAL ABOUT THE RELAY IS ANSWERED ON AN OPEN SOCKET. Closed before accept(), it is a failed handshake, and every
    # browser prints that in red: a community that lists a relay whose name no longer resolves (relay.0xchat.com,
    # 2026-10-29) put "WebSocket connection to wss://poster.place/ws/concord-relay?... failed" in every member's console
    # and failed the post-deploy sign-in check twice. Accepted and then closed with the same code, the client sees the
    # same closed tunnel (it reads no code) and the console stays clean. A forged ticket is still refused at the door.
    if not ok or not proxy:
        await websocket.accept()
        await websocket.close(code=4403, reason=(why or "no proxy")[:100])
        return
    if not concord_tunnel.acquire(pk):
        await websocket.accept()
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
        # Every Tor daemon in the proxy's order, then DIRECT last (concord_tunnel.egress_hops) -- unless no hop reached
        # this host a moment ago, when the answer is the same and nothing is dialled (concord_tunnel.unreachable).
        # concord_tunnel.open_upstream: one dial at a time to a host nothing reached lately (a burst of tunnels to a
        # dead relay walked the whole chain once EACH), and nothing at all inside a host's pause.
        upstream_cm, upstream = await concord_tunnel.open_upstream(target)
        if upstream is None:
            await websocket.close(code=4502, reason="relay unreachable")
            return
        info, began = {}, time.monotonic()
        try:
            await concord_tunnel.pipe(websocket, upstream, info)
        except Exception:
            pass
        finally:
            concord_tunnel.closed(target, info.get("first", ""), time.monotonic() - began, info.get("ended_by", ""),
                                  concord_tunnel.client_kind(websocket.headers.get("user-agent", ""),
                                                             websocket.headers.get("origin", "")))
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

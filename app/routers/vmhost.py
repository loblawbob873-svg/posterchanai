"""VM hosting HTTP surface: the console WebSocket and the admin status panel.

Management itself does NOT come through here — it is Nostr (app/services/vmhost/transport.py). This
router only carries what Nostr cannot: the VNC byte stream, behind a ticket obtained over Nostr.

/ws/vmconsole speaks, in order:
    → {"t":"open","ticket":"…"}          FIRST frame, text. Never a query string (proxy logs).
    ← {"t":"ok"}                         the ticket was valid and the VNC display answered
    → {"t":"go"}                         the client has handed the socket to noVNC; start pumping
    ⇄ binary                             raw RFB, both ways
    ← {"t":"err","m":"…"}                any refusal, as a MESSAGE, then close

Refusals are messages, not HTTP statuses, for the same reason as /ws/ssh: a 403 on an upgrade is what
proxies turn into an unexplained failure. Any Origin is accepted — the credential is a single-use
bearer in the first frame, not a cookie a third-party page could ride.

The `go` step is not ceremony. noVNC replaces the socket's onmessage when it attaches; RFB bytes that
arrived before that (the server speaks first in RFB) would be delivered to the handler that read the
`ok` and lost, and the session hangs at "connecting" with nothing in any log.
"""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect

from app.auth import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vmhost"])
ws_router = APIRouter()

FIRST_FRAME_TIMEOUT = 20


async def _refuse(ws: WebSocket, msg: str) -> None:
    try:
        await ws.send_json({"t": "err", "m": msg})
    except Exception:
        pass


async def _recv_text_json(ws: WebSocket, timeout: float):
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if msg.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect()
    raw = msg.get("text")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@ws_router.websocket("/ws/vmconsole")
async def websocket_vmconsole(websocket: WebSocket):
    await websocket.accept()
    from app.services.vmhost import service as vmsvc
    from app.services.vmhost.service import VmHostError
    writer = None
    cid = None
    reg = None
    ticket = None
    try:
        if "ticket" in websocket.query_params:
            await _refuse(websocket, "send the ticket in the first frame, never in the URL")
            return
        svc = vmsvc.current()
        if svc is None:
            await _refuse(websocket, "VM hosting is not running on this node")
            return
        reg = svc.consoles
        try:
            first = await _recv_text_json(websocket, FIRST_FRAME_TIMEOUT)
        except asyncio.TimeoutError:
            await _refuse(websocket, "expected an open frame")
            return
        if not isinstance(first, dict) or first.get("t") != "open":
            await _refuse(websocket, "expected an open frame")
            return
        ticket = reg.consume(first.get("ticket"))
        if ticket is None:
            await _refuse(websocket, "this console ticket is invalid, expired or already used")
            return
        try:
            host, port = await svc.console_target(ticket.pubkey, ticket.vm)
        except VmHostError as e:
            await _refuse(websocket, e.message)
            return
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
        except (OSError, asyncio.TimeoutError):
            await _refuse(websocket, "the VM's display is not answering")
            return
        await websocket.send_json({"t": "ok"})
        try:
            go = await _recv_text_json(websocket, FIRST_FRAME_TIMEOUT)
        except asyncio.TimeoutError:
            go = None
        if not isinstance(go, dict) or go.get("t") != "go":
            await _refuse(websocket, "expected a go frame")
            return

        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Thread-safe: a revoke can come from whichever task/loop handled the unassign or stop.
        cid = reg.attach(ticket.vm, ticket.pubkey, lambda: loop.call_soon_threadsafe(done.set))
        deadline = time.monotonic() + svc.cfg.console_max_minutes * 60

        async def up():
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                if data:
                    writer.write(data)
                    await writer.drain()

        async def down():
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                await websocket.send_bytes(chunk)

        tasks = [asyncio.create_task(up()), asyncio.create_task(down()), asyncio.create_task(done.wait())]
        try:
            finished, _ = await asyncio.wait(tasks, timeout=max(1.0, deadline - time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
            if not finished:
                await _refuse(websocket, "console session time limit reached")
            elif tasks[2] in finished:
                await _refuse(websocket, "console closed: access to this VM changed")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("[vmhost] console error: %s", e)
        await _refuse(websocket, "console error")
    finally:
        if reg is not None and cid is not None and ticket is not None:
            reg.detach(ticket.vm, cid)
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/api/admin/vmhost/status")
async def vmhost_status(admin=Depends(get_admin_user)):
    """What the admin tab shows: is it running, as whom, can it reach libvirt, where are the files."""
    import os
    from app.services import nostr_dvm
    from app.services.vmhost import config, service as vmsvc, transport
    cfg = config.current()
    st = transport.status()
    out = {"enabled": cfg.enabled, "running": st["running"], "error": st["error"],
           "node_npub": nostr_dvm.node_npub() or "", "libvirt_uri": cfg.libvirt_uri,
           "storage_dir": cfg.storage_dir, "storage_exists": os.path.isdir(cfg.storage_dir),
           "storage_writable": os.access(cfg.storage_dir, os.W_OK) if os.path.isdir(cfg.storage_dir) else False,
           "kvm": os.path.exists("/dev/kvm"), "public_url": cfg.public_url,
           "admins": len(cfg.admin_pubkeys), "allowed": len(cfg.allowed_pubkeys)}
    svc = vmsvc.current()
    if svc is not None:
        try:
            avail = await asyncio.wait_for(svc.backend.available(), 15)
            out["libvirt"] = avail
            domains = await asyncio.wait_for(svc.backend.list_domains(), 30)
            out["vms"] = {"total": len(domains), "running": sum(1 for d in domains if d.state == "running")}
            out["consoles_open"] = svc.consoles.live_count()
        except Exception as e:
            out["libvirt"] = {"ok": False, "error": str(e)[:300]}
    return out


# ======================================================================================================
# PHASE 3 — the cold-migration transfer. The TARGET host pulls each exported file from here with Range
# requests, authenticated by a NIP-98 header signed by the target's node key and bound to this exact
# URL. Served ONLY for a migration this host is the source of, ONLY to its target, ONLY while it is
# exporting/transferring (app/services/vmhost/migrate.py:serve_transfer). No DB session: a multi-GB
# transfer must not hold a pool connection, and nothing here needs one. File reads go through
# asyncio.to_thread in 1 MiB chunks so the single uvicorn worker keeps serving everything else.
# ======================================================================================================
@router.get("/api/vmhost/transfer/{mig}/{index}")
async def vmhost_transfer(mig: str, index: str, request: Request):
    from starlette.responses import PlainTextResponse
    from app.services.vmhost import service as vmsvc
    svc = vmsvc.current()
    migrator = getattr(svc, "migrator", None) if svc is not None else None
    if migrator is None:
        return PlainTextResponse("VM hosting is not running on this node", status_code=404)
    return await migrator.serve_transfer(mig, index, request.headers.get("authorization"),
                                         request.headers.get("range"), request.url.path)

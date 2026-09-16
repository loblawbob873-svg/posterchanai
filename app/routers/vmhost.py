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
from fastapi.responses import JSONResponse, Response

from app.auth import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vmhost"])
ws_router = APIRouter()

FIRST_FRAME_TIMEOUT = 20
# A live console re-checks that its user may still reach the VM (role, assignment, running, loopback
# display, same host service) this often — access removed behind the service's back (virsh by hand, a
# guest powered off from inside) has no revoke to close the socket.
RECHECK_EVERY = 30


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

        async def still_allowed() -> str:
            """'' while this console may stay open, otherwise why it must close. Fails CLOSED: a
            check that cannot be made is not a check that passed."""
            if vmsvc.current() is not svc:
                return "console closed: the VM host restarted"
            try:
                await svc.console_target(ticket.pubkey, ticket.vm)
            except VmHostError as e:
                return "console closed: " + e.message
            except Exception as e:
                logger.warning("[vmhost] console access re-check failed: %s", e)
                return "console closed: could not re-check access to this VM"
            return ""

        # Re-check once right after registering: a revoke that ran between the open-time check and
        # attach() found nothing to close.
        why = await still_allowed()
        if why:
            await _refuse(websocket, why)
            return
        reason = {"m": ""}

        async def watch():
            while True:
                await asyncio.sleep(RECHECK_EVERY)
                why = await still_allowed()
                if why:
                    reason["m"] = why
                    return

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

        tasks = [asyncio.create_task(up()), asyncio.create_task(down()), asyncio.create_task(done.wait()),
                 asyncio.create_task(watch())]
        try:
            finished, _ = await asyncio.wait(tasks, timeout=max(1.0, deadline - time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
            if not finished:
                await _refuse(websocket, "console session time limit reached")
            elif tasks[2] in finished:
                await _refuse(websocket, "console closed: access to this VM changed, or the VM host restarted")
            elif tasks[3] in finished:
                await _refuse(websocket, reason["m"])
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


_UPLOAD_CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"}


@router.options("/api/vmhost/iso/{ticket}")
async def vmhost_iso_upload_preflight(ticket: str):
    return Response(status_code=204, headers=_UPLOAD_CORS)


@router.put("/api/vmhost/iso/{ticket}")
async def vmhost_iso_upload(ticket: str, request: Request):
    """Receive an ISO for the library. The credential is the single-use ticket issued over Nostr
    (`iso.upload_ticket`, admins only); it is consumed BEFORE the body is read, so a copy of this URL in
    a proxy log is already spent. The body is streamed to disk, never buffered — see isolib.py."""
    from app.services.vmhost import service as vmsvc
    from app.services.vmhost.service import VmHostError
    svc = vmsvc.current()
    if svc is None:
        return JSONResponse({"ok": False, "error": {"code": "unsupported",
                                                    "message": "VM hosting is not running on this node"}},
                            status_code=503, headers=_UPLOAD_CORS)
    try:
        res = await svc.receive_upload(ticket, request.stream())
    except VmHostError as e:
        status = {"forbidden": 403, "conflict": 409, "insufficient_capacity": 413}.get(e.code, 400)
        return JSONResponse({"ok": False, "error": {"code": e.code, "message": e.message}}, status_code=status,
                            headers=_UPLOAD_CORS)
    except Exception as e:  # a dropped connection mid-body, a full disk
        logger.warning("[vmhost] ISO upload failed: %s", e)
        return JSONResponse({"ok": False, "error": {"code": "backend_error", "message": "the upload failed"}},
                            status_code=500, headers=_UPLOAD_CORS)
    return JSONResponse({"ok": True, "result": res}, headers=_UPLOAD_CORS)


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

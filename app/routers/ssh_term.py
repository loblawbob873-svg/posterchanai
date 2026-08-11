"""The SSH terminal's endpoints: the host list, and the PTY WebSocket.

Both gates are checked HERE, on every entry, and neither is the UI's job: the client hides the
Terminal when the feature is off, and hiding a thing is not the same as refusing it.

The socket speaks JSON frames both ways:
    → {"t":"open","host":"build","password":"…","cols":120,"rows":32}
    → {"t":"in","d":"ls -la\\n"}          keystrokes
    → {"t":"size","cols":120,"rows":32}
    ← {"t":"out","d":"…"}                 terminal bytes, as text
    ← {"t":"ready"} / {"t":"err","m":"…"} / {"t":"end"}
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_user_from_websocket
from app.database import SessionLocal, get_db
from app.services import ssh_service

logger = logging.getLogger(__name__)

def _user_from_token(token: str, db: Session):
    """Resolve a bearer exactly as get_user_from_websocket does — same decoder, same lookup — so the
    two cannot drift into disagreeing about who someone is."""
    if not token:
        return None
    try:
        from app.auth import decode_token
        from app.models import User
        payload = decode_token(token)
        if not payload:
            return None
        uid = payload.get("sub")
        return db.query(User).filter(User.id == int(uid)).first() if uid else None
    except Exception:
        return None


def _why(e: Exception) -> str:
    """The KIND of failure, in words, without the exception's text.

    paramiko's messages carry the server-side private-key path, which /api/ssh/hosts deliberately
    withholds; other libraries' carry internal addresses. What a person can act on is which of these
    it was, and that is derivable from the type."""
    n = type(e).__name__
    if "Authentication" in n:
        return "the host refused those credentials"
    if "BadHostKey" in n:
        return "the host key changed since this server last saw it"
    if n in ("NoValidConnectionsError", "ConnectionRefusedError", "OSError", "socket.error", "gaierror"):
        return "the host could not be reached"
    if "Timeout" in n or n == "TimeoutError":
        return "the host did not answer in time"
    # A KEY FILE THAT IS NOT A PRIVATE KEY IS THE COMMON MISTAKE, and "the SSH handshake failed"
    # sends you to look at the network for it. paramiko raises a plain SSHException here, so the kind
    # has to come from the message -- which is READ but never echoed, so nothing leaks. The .pub
    # confusion is worth naming outright: it is the file people have to hand, and pointing at a
    # public key is the single most likely way to configure this wrong.
    msg = str(e).lower()
    # Order matters: paramiko says "Private key file is encrypted", which matches BOTH of the first
    # two tests, and the passphrase answer is the useful one.
    if "encrypted" in msg or "passphrase" in msg:
        return "that private key is passphrase-protected, which this terminal cannot unlock"
    if "not found" in msg or "no such file" in msg:
        return "the key file configured for this host is not on the server"
    if "private key" in msg:
        return ("that key file is not a usable private key — if the path ends in .pub it is the "
                "PUBLIC half; the server needs the private one (no .pub)")
    if "SSH" in n:
        return "the SSH handshake failed"
    return "the connection failed"


# A PTY is a login on someone else's machine and a thread of the shared executor while it connects.
# Unbounded, a script (or a stuck reconnect loop) opens as many as it likes on a single-worker node.
MAX_LIVE = 8
_live: set = set()


def _origin_ok(websocket: WebSocket) -> bool:
    """A WebSocket upgrade is NOT covered by the same-origin policy, and the session cookie is issued
    SameSite=none so the native apps can use it — so a page on any site could open this socket in a
    victim's browser and be handed a shell. Same-origin, an app:// or capacitor:// bundle origin, or
    no Origin at all (a non-browser client, which cannot be a CSRF victim) are allowed."""
    o = (websocket.headers.get("origin") or "").strip()
    if not o:
        return True
    if o.startswith("app://") or o.startswith("capacitor://") or o.startswith("http://localhost"):
        return True
    host = (websocket.headers.get("host") or "").strip().lower()
    try:
        from urllib.parse import urlparse
        return bool(host) and urlparse(o).netloc.lower() == host
    except Exception:
        return False


router = APIRouter(prefix="/api/ssh", tags=["ssh"])
ws_router = APIRouter()


@router.get("/hosts")
async def list_hosts(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """What this user may connect to. Names and destinations only — never a key path, which is a fact
    about the server's filesystem and no business of the browser's."""
    if not ssh_service.is_enabled():
        raise HTTPException(status_code=403, detail="the SSH terminal is switched off")
    if not ssh_service.user_allowed(db, user):
        raise HTTPException(status_code=403, detail="you are not allowed to use the SSH terminal")
    return {
        "ok": True,
        "available": ssh_service.available(),
        "hosts": [
            {"name": h.name, "label": f"{h.user}@{h.host}" + (f":{h.port}" if h.port != 22 else ""),
             "keyed": bool(h.key)}
            for h in ssh_service.hosts().values()
        ],
    }


@ws_router.websocket("/ws/ssh")
async def websocket_ssh(websocket: WebSocket):
    """A PTY, pumped both ways.

    Accepted immediately and refused with a MESSAGE rather than an HTTP status, the same as the chat
    socket — a 403 on the upgrade is what proxies and WAFs turn into an unexplained failure."""
    await websocket.accept()
    db = None
    sess = None
    pump = None
    slot = None
    try:
        if not _origin_ok(websocket):
            logger.warning("[ssh] refused a socket from origin %r", websocket.headers.get("origin"))
            await websocket.send_json({"t": "err", "m": "that origin may not open a terminal"})
            return
        if len(_live) >= MAX_LIVE:
            await websocket.send_json({"t": "err", "m": "too many terminals are open on this server"})
            return
        db = SessionLocal()
        # The OPEN frame comes first, before anything is authorised, because it is what carries the
        # credential. `get_user_from_websocket` reads a cookie or a `?token=` query string, and
        # neither is available to every client: the bundled apps are cross-origin to the instance, so
        # their cookie is unusable, and a token in the URL is written into every proxy log between
        # here and the server. A frame is the only place it is both usable and not logged.
        first = await asyncio.wait_for(websocket.receive_json(), timeout=60)
        if (first or {}).get("t") != "open":
            await websocket.send_json({"t": "err", "m": "expected an open frame"})
            return
        user = await get_user_from_websocket(websocket, db)
        if not user:
            user = _user_from_token(str((first or {}).get("token") or ""), db)
        if not user:
            await websocket.send_json({"t": "err", "m": "please log in again"})
            return
        if not ssh_service.is_enabled():
            await websocket.send_json({"t": "err", "m": "the SSH terminal is switched off"})
            return
        if not ssh_service.user_allowed(db, user):
            await websocket.send_json({"t": "err", "m": "you are not allowed to use the SSH terminal"})
            return
        if not ssh_service.available():
            await websocket.send_json({"t": "err", "m": "this node has no SSH library installed "
                                                        "(paramiko) — run install.sh to add it"})
            return
        # AUTH IS DONE, so give the pooled connection back. A terminal can be open for hours and the
        # query above leaves its connection idle-in-transaction; holding one per session exhausts the
        # pool and takes the whole app down with it, for sessions that are only sitting at a prompt.
        # Everything below needs the settings (which come from the relay) and nothing from SQL.
        who = getattr(user, "username", None) or getattr(user, "id", "?")
        db.close()
        db = None

        # The client names a HOST, never an address — see the allowlist note in ssh_service.
        h = ssh_service.hosts().get(str(first.get("host") or ""))
        if not h:
            await websocket.send_json({"t": "err", "m": "no such host is configured"})
            return
        cols, rows = int(first.get("cols") or 80), int(first.get("rows") or 24)
        sess = ssh_service.SshSession()
        try:
            await sess.connect(h, password=str(first.get("password") or ""), cols=cols, rows=rows)
        except Exception as e:
            # The KIND of failure matters — "auth failed" and "no route to host" send you to
            # completely different places, and this is the one screen where a person can act on
            # either. The exception TEXT does not: paramiko's includes the server-side key path,
            # which /api/ssh/hosts deliberately withholds sixty lines above. Classify, then log the
            # detail where the operator can read it and the browser cannot.
            logger.warning("[ssh] connect to %s failed: %s", h.name, e)
            await websocket.send_json({"t": "err", "m": "could not connect: " + _why(e)})
            return
        logger.info("[ssh] %s opened a terminal on %s (%s@%s)", who, h.name, h.user, h.host)
        await websocket.send_json({"t": "ready", "host": h.name})

        started = asyncio.get_event_loop().time()
        # A one-cell list: the pump task below reads it while this loop writes it.
        last_in = [started]

        async def to_client():
            """Poll the channel and forward. paramiko has no awaitable read, so this is a poll — 25ms
            is under the threshold where typing feels laggy and far above a busy loop."""
            while True:
                if sess.closed():
                    break
                # BOTH BOUNDS, EVERY PASS. They used to sit on branches a real session never reaches:
                # MAX_SESSION only where there was nothing to read — so `tail -f`, `top` or `yes` made
                # the channel readable on essentially every poll and the 12-hour cap was never
                # evaluated at all — and IDLE_TIMEOUT only inside receive_json's timeout, so any
                # client frame more often than every 30s (a phone's keyboard toggling sends `size`)
                # deferred it for ever. A bound that a busy session escapes is not a bound.
                now = asyncio.get_event_loop().time()
                if now - started > ssh_service.MAX_SESSION:
                    await websocket.send_json({"t": "err", "m": "closed after 12 hours"})
                    break
                if now - last_in[0] > ssh_service.IDLE_TIMEOUT:
                    await websocket.send_json({"t": "err", "m": "closed after 30 minutes with nothing typed"})
                    break
                if sess.read_ready():
                    data = await sess.read()
                    if not data:
                        break
                    await websocket.send_json({"t": "out", "d": data.decode("utf-8", "replace")})
                    continue
                await asyncio.sleep(0.025)

        slot = object(); _live.add(slot)
        pump = asyncio.create_task(to_client())
        while True:
            if pump.done():
                break
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                # An idle SOCKET is normal — you are reading, not typing. The session's own clocks are
                # in the pump above, which runs whether or not anything arrives here.
                continue
            t = (msg or {}).get("t")
            if t == "in":
                last_in[0] = asyncio.get_event_loop().time()
                await sess.send(str(msg.get("d") or ""))
            elif t == "size":
                await sess.resize(msg.get("cols"), msg.get("rows"))
            elif t == "close":
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("[ssh] session ended: %s", e)
        try:
            await websocket.send_json({"t": "err", "m": _why(e)})
        except Exception:
            pass
    finally:
        if slot is not None:
            _live.discard(slot)
        if pump:
            pump.cancel()
        if sess:
            sess.close()
        if db:
            db.close()
        try:
            await websocket.send_json({"t": "end"})
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass

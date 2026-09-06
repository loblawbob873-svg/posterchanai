"""The SSH terminal's endpoints: the host list, and the PTY WebSocket.

Both gates are checked HERE, on every entry, and neither is the UI's job: the client hides the
Terminal when the feature is off, and hiding a thing is not the same as refusing it.

The socket speaks JSON frames both ways:
    → {"t":"open","host":"build","password":"…","cols":120,"rows":32}
    → {"t":"in","d":"ls -la\\n"}          keystrokes
    → {"t":"size","cols":120,"rows":32}
    → {"t":"detach"} / {"t":"close"}      leave it running / end it
    ← {"t":"out","d":"…","seq":N}         terminal bytes, as text, with a resumable cursor
    ← {"t":"ready","sid":"…"} / {"t":"gone"} / {"t":"err","m":"…"} / {"t":"end"}

A SESSION OUTLIVES ITS SOCKET. `{"t":"open","resume":"<sid>","cursor":N}` reattaches to a shell that
is still running and is sent whatever it produced past N — which is what makes a dropped Tor circuit
a two-second gap rather than a lost afternoon. Nothing here expires; `close` is what ends one.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth import get_current_user, get_user_from_websocket
from app.database import SessionLocal, get_db
from app.services import ssh_keeper, ssh_service

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
    return _why_kind(type(e).__name__, str(e))


def _why_kind(n: str, raw: str) -> str:
    """The KIND of failure, in words, without the exception's text.

    paramiko's messages carry the server-side private-key path, which /api/ssh/hosts deliberately
    withholds; other libraries' carry internal addresses. What a person can act on is which of these
    it was, and that is derivable from the type."""
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
    msg = (raw or "").lower()
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
#
# Counted from the SESSION registry rather than from live sockets, because a detached session is still
# a shell running on a remote host — the thing the cap is about. Counting sockets would let a client
# that reconnects in a loop hold eight shells while appearing to hold none.
MAX_LIVE = 8


def _origin_ok(websocket: WebSocket) -> bool:
    """A WebSocket upgrade is NOT covered by the same-origin policy, and the session cookie is issued
    SameSite=none so the native apps can use it — so a page on any site could open this socket in a
    victim's browser and be handed a shell. Same-origin, an app:// or capacitor:// bundle origin, or
    no Origin at all (a non-browser client, which cannot be a CSRF victim) are allowed."""
    o = (websocket.headers.get("origin") or "").strip()
    if not o:
        return True
    # THE SAME LIST THE API TRUSTS, not one written from memory beside it.
    #
    # The first version here allowed `http://localhost` and not `https://localhost` -- backwards on
    # both counts. Capacitor on Android serves the bundle from `https://localhost`, so the APK was
    # refused with "that origin may not open a terminal"; and `http://localhost` is the one the CORS
    # middleware deliberately EXCLUDES, because with credentials any plaintext page on localhost could
    # read the victim's authed responses. Two hand-maintained copies of a trust list is how one of
    # them ends up wrong, and this was the one.
    from app.auth import NATIVE_APP_ORIGINS
    if o in NATIVE_APP_ORIGINS:
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


@router.get("/sessions")
async def list_sessions(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The shells this account still has running, attached or not.

    This is what makes the feature tmux-shaped rather than merely reconnect-shaped: a client that was
    closed, reloaded, or replaced by a different device has no session id to resume from, and without
    a list its shell exists but is unreachable. Scoped to the caller — a session id is an authorisation
    to type on someone's servers."""
    if not ssh_service.is_enabled() or not ssh_service.user_allowed(db, user):
        raise HTTPException(status_code=403, detail="the SSH terminal is switched off")
    uid = getattr(user, "id", None)
    if ssh_keeper.is_up():
        return {"ok": True, "keeper": True, "sessions": await ssh_keeper.sessions_for(uid)}
    # No keeper: sessions live in THIS process and end with it. Said out loud, because "my shell
    # vanished" after a deploy is otherwise indistinguishable from a bug.
    return {"ok": True, "keeper": False, "sessions": ssh_service.sessions_for(uid)}


@router.post("/sessions/kill")
async def kill_session(body: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """End one outright. Sessions do not expire on their own, so this is the way they end."""
    if not ssh_service.is_enabled() or not ssh_service.user_allowed(db, user):
        raise HTTPException(status_code=403, detail="the SSH terminal is switched off")
    sid = str((body or {}).get("sid") or "")
    uid = getattr(user, "id", None)
    if ssh_keeper.is_up():
        return {"ok": await ssh_keeper.kill(sid, uid)}
    return {"ok": await ssh_service.kill(sid, uid)}


async def _via_keeper(websocket: WebSocket, uid, who: str, first: dict, cols: int, rows: int) -> None:
    """Relay this socket to the keeper process, which is where the PTY actually lives.

    Almost a byte pipe on purpose: the keeper speaks the same frames the browser does, so there is no
    second copy of the session rules here to drift from the in-process path below. What this end does
    own is the two things the keeper deliberately does not — turning a failure KIND into a sentence,
    and resolving a host NAME to a destination (the allowlist; a client may never name an address)."""
    resume = str(first.get("resume") or "")
    label = str(first.get("label") or "main")
    h = ssh_service.hosts().get(str(first.get("host") or ""))

    async def _op(req):
        r, w = await ssh_keeper.open_conn()
        w.write((json.dumps(req) + "\n").encode("utf-8"))
        await w.drain()
        line = await asyncio.wait_for(r.readline(), timeout=45)
        return r, w, (json.loads(line.decode("utf-8")) if line else {})

    r = w = None
    try:
        if resume:
            r, w, msg = await _op({"op": "attach", "user_id": uid, "sid": resume,
                                   "cursor": first.get("cursor"), "cols": cols, "rows": rows})
            if msg.get("t") == "gone":
                # Fall through to opening a NEW one, which is what a person wants after a keeper
                # restart — but say so first, because a silent fresh shell in the same window reads
                # as "my work vanished".
                await websocket.send_json({"t": "gone",
                                           "m": "that session is no longer running — starting a new one"})
                try:
                    w.close()
                except Exception:
                    pass
                r = w = None
            else:
                await websocket.send_json(msg)
                logger.info("[ssh] %s reattached to %s via the keeper", who, resume)

        if r is None:
            if not h:
                await websocket.send_json({"t": "err", "m": "no such host is configured"})
                return
            # Per-ACCOUNT cap. The keeper's sessions outlive this process, so a global count taken
            # here would be of the wrong thing entirely.
            if len(await ssh_keeper.sessions_for(uid)) >= MAX_LIVE:
                await websocket.send_json({"t": "err", "m": "you already have the maximum number of "
                                                            "sessions open — kill one first"})
                return
            r, w, msg = await _op({
                "op": "open", "user_id": uid, "cols": cols, "rows": rows, "label": label,
                "password": str(first.get("password") or ""),
                "host": {"name": h.name, "user": h.user, "host": h.host, "port": h.port, "key": h.key},
            })
            if msg.get("t") != "ready":
                why = _why_kind(str(msg.get("kind") or ""), str(msg.get("m") or ""))
                logger.warning("[ssh] connect to %s failed: %s", h.name, msg.get("m"))
                await websocket.send_json({"t": "err", "m": "could not connect: " + why})
                return
            await websocket.send_json(msg)
            logger.info("[ssh] %s opened a terminal on %s via the keeper", who, h.name)

        async def _down():
            """Keeper -> browser.

            THE EXCEPT IS THE POINT. This runs as a task, so anything raised here ends the relay
            with no traceback and no log line: the attach had already succeeded and said so, and the
            browser just saw its socket close. That is exactly how a 64 KiB reader limit against a
            256 KiB replay buffer stayed invisible — "reattached … via the keeper" in the log, a
            blank screen on the phone, and no console error at either end, because the throw was
            here. Whatever fails next, it says so."""
            try:
                while True:
                    line = await r.readline()
                    if not line:
                        break
                    try:
                        await websocket.send_json(json.loads(line.decode("utf-8")))
                    except Exception:
                        break                     # the browser hung up; ordinary
            except asyncio.CancelledError:
                raise                             # a normal teardown, not a failure
            except Exception as e:
                logger.warning("[ssh] relay from the keeper died for %s (%s): %s",
                               who, type(e).__name__, e)

        down = asyncio.create_task(_down())
        try:
            while not down.done():
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
                except asyncio.TimeoutError:
                    continue                      # reading, not typing — normal
                t = (msg or {}).get("t")
                if t not in ("in", "size", "detach", "close"):
                    continue                      # never relay a browser-supplied `op`
                w.write((json.dumps(msg) + "\n").encode("utf-8"))
                await w.drain()
                if t in ("detach", "close"):
                    break
        finally:
            down.cancel()
    finally:
        # Hanging up IS detaching, as far as the keeper is concerned — the shell keeps running.
        if w:
            try:
                w.close()
            except Exception:
                pass


@ws_router.websocket("/ws/ssh")
async def websocket_ssh(websocket: WebSocket):
    """A PTY, pumped both ways.

    Accepted immediately and refused with a MESSAGE rather than an HTTP status, the same as the chat
    socket — a 403 on the upgrade is what proxies and WAFs turn into an unexplained failure."""
    await websocket.accept()
    db = None
    sess = None
    pump = None
    try:
        if not _origin_ok(websocket):
            logger.warning("[ssh] refused a socket from origin %r", websocket.headers.get("origin"))
            await websocket.send_json({"t": "err", "m": "that origin may not open a terminal"})
            return
        if ssh_service.live_count() >= MAX_LIVE:
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

        cols, rows = int(first.get("cols") or 80), int(first.get("rows") or 24)

        # THE KEEPER OWNS THE SESSION WHEN IT IS RUNNING — see app/services/ssh_keeper.py. That is
        # what makes a shell survive `./sync.sh`, which restarts this process several times a day.
        # Falling back in-process when it is not there is deliberate: the terminal still works on a
        # node that never installed the unit, it just does not outlive a deploy.
        if ssh_keeper.is_up():
            await _via_keeper(websocket, getattr(user, "id", None), str(who), first, cols, rows)
            return

        # RESUME. A Tor circuit dropping is routine, and a shell that dies with its socket is one you
        # cannot use over Orbot -- you lose the working directory, the running command and the
        # scrollback every few minutes. So the PTY outlives the connection: the client keeps its
        # session id, and coming back re-attaches to the shell that is still running.
        resume = str(first.get("resume") or "")
        sess = ssh_service.get_session(resume, getattr(user, "id", None)) if resume else None
        if resume and not sess:
            # Say which it was. "It didn't resume" covers a shell that timed out, one that belongs to
            # another account, and a server that restarted — and they need different reactions.
            await websocket.send_json({"t": "gone", "m": "that session is no longer running — starting a new one"})
        if sess:
            sess.attach()
            await sess.resize(cols, rows)
            logger.info("[ssh] %s reattached to %s (%s)", who, sess.sid, sess.host_name)
            await websocket.send_json({"t": "ready", "host": sess.host_name, "sid": sess.sid,
                                       "label": sess.label, "resumed": True})
            # What they missed while they were away, from their own cursor.
            cur0 = first.get("cursor")
            have = sess.seq - len(sess.buf)
            cur0 = sess.seq if cur0 is None else max(have, min(int(cur0), sess.seq))
            miss = sess.since(cur0)
            if miss:
                await websocket.send_json({"t": "out", "d": miss.decode("utf-8", "replace"),
                                           "seq": sess.seq})
        else:
            # The client names a HOST, never an address — see the allowlist note in ssh_service.
            h = ssh_service.hosts().get(str(first.get("host") or ""))
            if not h:
                await websocket.send_json({"t": "err", "m": "no such host is configured"})
                return
            sess = ssh_service.SshSession(user_id=getattr(user, "id", None), host_name=h.name)
            try:
                # THE LABEL IS WHICH TAB THIS IS, and dropping it here opened every new tab into the
                # same tmux session — see _mux_name. The keeper path already forwarded it; this one
                # took the "main" default, so the in-process fallback had the bug on its own.
                await sess.connect(h, password=str(first.get("password") or ""), cols=cols, rows=rows,
                                   label=str(first.get("label") or "main"))
            except Exception as e:
                # The KIND of failure matters -- "auth failed" and "no route to host" send you to
                # completely different places, and this is the one screen where a person can act on
                # either. The exception TEXT does not: paramiko's includes the server-side key path,
                # which /api/ssh/hosts deliberately withholds. Classify, then log the detail where the
                # operator can read it and the browser cannot.
                logger.warning("[ssh] connect to %s failed: %s", h.name, e)
                await websocket.send_json({"t": "err", "m": "could not connect: " + _why(e)})
                return
            sess.attach()
            logger.info("[ssh] %s opened a terminal on %s (%s@%s)", who, h.name, h.user, h.host)
            await websocket.send_json({"t": "ready", "host": h.name, "sid": sess.sid,
                                       "label": sess.label})

        # FORWARD FROM THE SESSION'S BUFFER, never from the channel directly.
        #
        # The session drains the PTY on its own (see SshSession._drain) so a detached shell keeps
        # running -- if nobody read it, paramiko's window would fill and the REMOTE command would
        # block, which turns "my connection dropped" into "my build froze". This loop only moves bytes
        # the session has already collected, from wherever this client is up to.
        cursor = sess.seq
        stop = False

        async def to_client():
            nonlocal cursor
            while not stop:
                if cursor < sess.seq:
                    cursor = max(cursor, sess.seq - len(sess.buf))
                    data = sess.since(cursor)
                    # Hold back a character split across the buffer boundary — see utf8_take. A TUI
                    # is mostly multi-byte glyphs, and a replacement character written once is there
                    # for good.
                    take = ssh_service.utf8_take(data)
                    if take:
                        cursor += take
                        await websocket.send_json({"t": "out", "d": data[:take].decode("utf-8", "replace"),
                                                   "seq": cursor})
                        continue
                if sess.closed() or sess.sid not in ssh_service._sessions:
                    break
                sess.wake.clear()
                try:
                    await asyncio.wait_for(sess.wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

        pump = asyncio.create_task(to_client())
        while True:
            if pump.done():
                break
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                # An idle SOCKET is normal — you are reading, not typing. The session's own clocks run
                # in its reader, whether or not anything arrives here.
                continue
            t = (msg or {}).get("t")
            if t == "in":
                await sess.send(str(msg.get("d") or ""))
            elif t == "size":
                await sess.resize(msg.get("cols"), msg.get("rows"))
            elif t == "detach":
                # Leave, keep the shell. tmux's Ctrl-B d.
                break
            elif t == "close":
                # KILL. The only thing that ends a session, since nothing expires — which is why the
                # UI keeps this and "detach" as two visibly different buttons rather than one X whose
                # meaning you have to guess. `terminate`, not `close`: with tmux/screen on the far
                # end, closing our connection is what DETACHING does.
                await sess.terminate()
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
        if pump:
            pump.cancel()
        # DETACH, DO NOT CLOSE. This is the whole of resume: the socket going away is the normal case
        # over Tor, and the shell has to still be there when the client comes back. The session reaps
        # itself after DETACH_GRACE with nobody connected, so a walked-away-from login is not held for
        # ever — and an explicit "close" above already ended it.
        if sess and not sess.closed():
            sess.detach()
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

"""The terminal keeper: SSH sessions that outlive the web app's process.

WHY THIS EXISTS. A PTY held in the web app dies when the web app restarts, and this app is restarted
by every `./sync.sh` — so a terminal session's realistic lifetime was "until the next deploy", which
is minutes. The obvious answer is tmux on the far end, and that is still supported (see
`ssh_service._mux_command`), but it needs tmux INSTALLED on every host you want to reach, which is
not a thing this app gets to decide about someone else's server.

So the session lives in a SEPARATE PROCESS ON THIS NODE — `posterchanai-shell.service`, its own
systemd unit, its own cgroup — and the web app talks to it over a unix socket. Restarting the app
(or the relay, or the worker, or all of them) leaves the shells running; a client reconnects, the app
reattaches to the keeper, and the session is exactly where it was, running command and all. Nothing
is required on the remote host beyond sshd.

WHAT IS AND IS NOT PROMISED. The keeper is a normal process: rebooting this box, or restarting the
keeper's own unit, ends its sessions. `scripts/deploy_targets.py` therefore maps almost nothing to
this unit, so an ordinary deploy does not restart it. If you want a shell that survives even that,
run tmux on the far end — the two compose, and with `ssh_terminal_multiplex` on you get both.

THE PROTOCOL is the SAME JSON frames the browser socket speaks (see app/routers/ssh_term.py), one
per line, so the router is close to a relay rather than a translation layer. The first line from the
app is a request:

    {"op":"open",   "user_id":N, "host":{…}, "password":"…", "cols":C, "rows":R, "label":"main"}
    {"op":"attach", "user_id":N, "sid":"…", "cursor":N, "cols":C, "rows":R}
    {"op":"list",   "user_id":N}                     one-shot
    {"op":"kill",   "user_id":N, "sid":"…"}          one-shot

after which an open/attach connection streams `out` frames and accepts `in`/`size`/`detach`/`close`.

AUTHORISATION IS THE APP'S JOB, NOT THIS PROCESS'S. The keeper trusts `user_id` because the socket is
a filesystem object with 0600 permissions in the repo's own directory — anyone who can open it is
already running as this service. What the keeper DOES enforce is that a session belongs to the
user_id that opened it, so a bug in the app cannot hand one person another's shell.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading

from app.services import ssh_service

logger = logging.getLogger(__name__)

_loop = None
_thread = None
_server = None


def socket_path() -> str:
    """Where the socket lives. In the REPO rather than /tmp: on at least one node here /tmp is a
    tmpfs that the OOM story revolves around, and distro tmp-cleaners delete sockets by age."""
    env = (os.environ.get("POSTERCHANAI_SSH_SOCK") or "").strip()
    if env:
        return env
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, ".run", "ssh-keeper.sock")


def is_up() -> bool:
    """Whether a keeper is actually listening — checked by CONNECTING, never by the file existing.

    A unix socket left behind by a killed process is still a file, and `os.path.exists` on it is the
    difference between "sessions survive a deploy" and every terminal failing to open with a
    connection-refused nobody can see. Cheap: a local connect is microseconds."""
    path = socket_path()
    try:
        import socket as _s
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(path)
        s.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------------------------
# server side


async def _send(w: asyncio.StreamWriter, obj) -> None:
    w.write((json.dumps(obj) + "\n").encode("utf-8"))
    await w.drain()


async def _stream(sess, w: asyncio.StreamWriter, cursor: int, stop: asyncio.Event) -> None:
    """Push everything the session produces past `cursor` until it ends or the caller detaches."""
    while not stop.is_set():
        if cursor < sess.seq:
            data = sess.since(cursor)
            take = ssh_service.utf8_take(data)
            if take:
                cursor += take
                await _send(w, {"t": "out", "d": data[:take].decode("utf-8", "replace"), "seq": cursor})
                continue
        if sess.closed():
            break
        sess.wake.clear()
        try:
            await asyncio.wait_for(sess.wake.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    try:
        await _send(w, {"t": "end", "m": getattr(sess, "closed_reason", "")})
    except Exception:
        pass                              # the app hung up first; that is the ordinary case


async def _client(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    sess = None
    stop = asyncio.Event()
    pump = None
    attached = False
    try:
        line = await asyncio.wait_for(r.readline(), timeout=30)
        if not line:
            return
        req = json.loads(line.decode("utf-8"))
        op = req.get("op")
        uid = req.get("user_id")

        if op == "list":
            return await _send(w, {"ok": True, "sessions": ssh_service.sessions_for(uid)})
        if op == "kill":
            return await _send(w, {"ok": await ssh_service.kill(str(req.get("sid") or ""), uid)})

        cols, rows = int(req.get("cols") or 80), int(req.get("rows") or 24)

        if op == "attach":
            sess = ssh_service.get_session(str(req.get("sid") or ""), uid)
            if not sess:
                return await _send(w, {"t": "gone", "m": "that session is no longer running"})
            sess.attach()
            await sess.resize(cols, rows)
            cursor = req.get("cursor")
            # A cursor older than what is still buffered gets everything held — `since` decides, and
            # says so there. Start the stream from what the client claims to have, not from `seq`.
            have = sess.seq - len(sess.buf)
            cursor = sess.seq if cursor is None else max(0, min(int(cursor), sess.seq))
            if cursor < have:
                cursor = have
            await _send(w, {"t": "ready", "sid": sess.sid, "host": sess.host_name, "resumed": True})
            attached = True
        elif op == "open":
            h = req.get("host") or {}
            host = ssh_service.SshHost(name=str(h.get("name") or ""), user=str(h.get("user") or ""),
                                       host=str(h.get("host") or ""), port=int(h.get("port") or 22),
                                       key=str(h.get("key") or ""))
            sess = ssh_service.SshSession(user_id=uid, host_name=host.name)
            try:
                await sess.connect(host, password=str(req.get("password") or ""), cols=cols, rows=rows,
                                   label=str(req.get("label") or "main"))
            except Exception as e:
                logger.warning("[keeper] connect to %s failed: %s", host.name, e)
                # The KIND of failure, classified by the app — the keeper hands back the exception's
                # type name and lets the router turn it into words, so there is one such table.
                return await _send(w, {"t": "err", "kind": type(e).__name__, "m": str(e)})
            await _send(w, {"t": "ready", "sid": sess.sid, "host": sess.host_name})
            sess.attach()
            attached = True
            cursor = 0
        else:
            return await _send(w, {"t": "err", "m": "unknown op"})

        pump = asyncio.create_task(_stream(sess, w, cursor, stop))
        while True:
            if pump.done():
                break
            line = await r.readline()
            if not line:
                break                                  # the app went away: DETACH (see finally)
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            t = msg.get("t")
            if t == "in":
                await sess.send(str(msg.get("d") or ""))
            elif t == "size":
                await sess.resize(msg.get("cols"), msg.get("rows"))
            elif t == "detach":
                break
            elif t == "close":
                await sess.terminate()
                break
    except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
        pass
    except Exception as e:
        logger.warning("[keeper] client error: %s", e)
    finally:
        stop.set()
        if sess:
            sess.wake.set()
        if pump:
            pump.cancel()
        # DETACH, never close: the app restarting is the case this whole process exists for, and it
        # looks from here exactly like a client hanging up.
        if attached and sess and not sess.closed():
            sess.detach()
        try:
            w.close()
        except Exception:
            pass


async def _serve() -> None:
    global _server
    path = socket_path()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    try:
        # The socket IS the authority to open a shell on every host in the allowlist, so the
        # directory is locked down as well as the socket: chmod on the socket happens after bind, and
        # a directory nobody else can traverse closes that window.
        os.chmod(d, 0o700)
    except Exception:
        pass
    # A stale socket file from a killed keeper would make bind() fail with "address already in use"
    # for ever. Removing it is safe because `is_up()` connects rather than stats — if something is
    # really listening, this process would not have been started to replace it.
    try:
        if stat.S_ISSOCK(os.stat(path).st_mode):
            os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    _server = await asyncio.start_unix_server(_client, path=path)
    try:
        os.chmod(path, 0o600)          # this user only: the socket is authority to open a shell
    except Exception:
        pass
    logger.info("[keeper] listening on %s", path)
    async with _server:
        await _server.serve_forever()


def start_ssh_keeper() -> None:
    """Start the keeper in this process (its own thread + loop). Idempotent."""
    global _thread
    if _thread and _thread.is_alive():
        return

    def _run():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        try:
            _loop.run_until_complete(_serve())
        except asyncio.CancelledError:
            # `serve_forever` raises this when the server is closed — i.e. every clean shutdown. A
            # traceback here is how an operator concludes something is broken when nothing is.
            logger.info("[keeper] stopped")
        except Exception as e:
            logger.error("[keeper] server stopped: %s", e, exc_info=True)

    _thread = threading.Thread(target=_run, name="ssh-keeper", daemon=True)
    _thread.start()


def stop_ssh_keeper() -> None:
    """Stop listening and END EVERY SESSION.

    Deliberately not a graceful detach: this process going away means nothing will ever read those
    channels again, and a paramiko connection with no reader is a login left open on someone else's
    machine with its receive window filling. If the sessions are meant to survive THIS, the answer is
    tmux on the far end, which is a different mechanism and says so."""
    global _server
    try:
        for sid in list(ssh_service._sessions):
            s = ssh_service._sessions.get(sid)
            if s:
                s.close()
    except Exception:
        pass
    if _server and _loop:
        try:
            _loop.call_soon_threadsafe(_server.close)
        except Exception:
            pass
    try:
        os.unlink(socket_path())
    except Exception:
        pass
    _server = None


# ---------------------------------------------------------------------------------------------
# client side — what the web app calls. Kept here so the frame names have exactly one home.


async def open_conn():
    """Connect to the keeper. Raises if it is not there; callers fall back to in-process."""
    return await asyncio.open_unix_connection(path=socket_path())


async def request(obj, timeout: float = 5.0):
    """A one-shot op (`list`, `kill`) — send, read one line, hang up."""
    r, w = await open_conn()
    try:
        await _send(w, obj)
        line = await asyncio.wait_for(r.readline(), timeout=timeout)
        return json.loads(line.decode("utf-8")) if line else None
    finally:
        try:
            w.close()
        except Exception:
            pass


async def sessions_for(user_id):
    try:
        out = await request({"op": "list", "user_id": user_id})
        return (out or {}).get("sessions") or []
    except Exception:
        return []


async def kill(sid: str, user_id) -> bool:
    try:
        out = await request({"op": "kill", "sid": sid, "user_id": user_id})
        return bool((out or {}).get("ok"))
    except Exception:
        return False

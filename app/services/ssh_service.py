"""SSH terminal — a real PTY on a real host, for the client's Terminal app.

THIS IS DELIBERATE REMOTE CODE EXECUTION, and it is the second such path in this codebase. The first
is `node_service`, whose transport is Nostr-only (SSH was removed from it on purpose): a command
rides an encrypted NIP-90 event to a worker that runs it locally. That path reaches nodes you own and
have registered. This one reaches ANY host you can log into, which is a different and larger thing,
so it is gated separately and defaults to OFF:

  ssh_terminal_enabled   "true"/"false" — master switch, default FALSE.
  ssh_terminal_users     npubs allowed, one per line or comma-separated. Admins always allowed.
  ssh_hosts              the hosts on offer, one per line:
                             name  user@host[:port]  [key=/path/to/private_key]
                         e.g.  build  deploy@10.0.0.9:22  key=/home/pc/.ssh/id_ed25519

NO CREDENTIAL IS STORED BY THIS FEATURE. A host either names a private key FILE that already exists
on this server (the operator put it there; we never write one), or it authenticates with a password
the user types into the terminal for that session, which is held only for the length of the connect
call. That is the whole reason the host list is a list of DESTINATIONS rather than of logins: a
password box that remembers is a credential store, and a credential store for arbitrary SSH is a much
worse thing to own than the terminal itself.

The host list is also an ALLOWLIST, not a convenience. The client sends a host NAME and this module
resolves it; a client cannot name an address. Without that, "a terminal that can ssh" is an
unauthenticated proxy into every machine this server can route to — including the ones behind it.
"""
import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services import settings_store

logger = logging.getLogger(__name__)

# A SESSION IS A TMUX SESSION: it lives until the remote shell exits or you kill it. That is the
# feature, not a side effect of resume — "I got disconnected and it breaks the entire experience" is
# the whole reason this exists, and a shell that quietly reaps itself after some interval is one you
# cannot leave a build running in either.
#
# So all three bounds default to OFF and are the OPERATOR's to set (Admin → Nodes), in minutes/hours;
# 0 or blank means no bound. What is left holding the line is MAX_LIVE in the router, which caps how
# many shells one node will run at once — a count is the honest bound here, because a timer's only
# effect on someone who wants their session back is to take it away.
#
# SURVIVING A RESTART OF THIS SERVICE, which nothing above can do on its own: the session objects here
# live in the app's own process, and `sync.sh` restarts it. So the shell is not kept HERE at all — it
# is opened inside a `tmux` (or `screen`) session ON THE REMOTE HOST, named deterministically per user
# and per terminal. Reconnecting runs `tmux new-session -A`, which attaches to that session if it is
# there and creates it if it is not.
#
# That is what makes this the real thing rather than an imitation of it: the running command, the
# working directory and the scrollback belong to the far end, so they outlive a dropped circuit, a
# deploy, a reboot of THIS box, and closing the app on a phone. The in-process buffer below is then
# only what makes a reattach instant — it replays the last screenful while tmux redraws.
#
# It needs tmux (or screen) on the host, so it degrades in order and never fails: tmux → screen → a
# plain login shell, which is exactly what this did before and is still perfectly usable, just
# mortal. `ssh_terminal_multiplex` turns it off.
READ_CHUNK = 32 * 1024


def utf8_take(b: bytes) -> int:
    """How many of these bytes can be decoded without splitting a character.

    The buffer is BYTES and the wire carries text, so a chunk boundary can land mid-character — and
    decoding with 'replace' at that boundary turns it into two U+FFFDs permanently, because the
    cursor has already moved past it. Anything that draws a box (htop, mc, a TUI installer) is full of
    multi-byte characters, so this is not theoretical. Hold the tail back instead; it arrives with the
    next frame, microseconds later."""
    n = len(b)
    if not n:
        return 0
    # A UTF-8 sequence is at most 4 bytes, so a partial one can only be in the last 3.
    for back in range(1, min(4, n) + 1):
        c = b[n - back]
        if c < 0x80:
            return n                        # ASCII: everything up to here is whole
        if c >= 0xC0:                       # a lead byte — is its sequence complete?
            need = 2 if c < 0xE0 else 3 if c < 0xF0 else 4
            return n if back >= need else n - back
        # a continuation byte: keep looking back for its lead
    return n


def _mux_name(user_id, label: str) -> str:
    """The tmux session name. DETERMINISTIC, because it is the only thing that reconnects a person to
    their shell once this process has forgotten every id it ever issued."""
    lab = re.sub(r"[^A-Za-z0-9_-]", "", str(label or "")[:24]) or "main"
    return f"pcai-{user_id or 0}-{lab}"


def _mux_command(name: str) -> str:
    """tmux, else screen, else a plain login shell — decided ON THE HOST, at connect time, because
    what is installed there is not something this node can know.

    `new-session -A` is attach-or-create in one atomic step; `-s` names it. screen's `-xRR` is the
    same idea. Both are `exec`d so the wrapper shell does not sit between the PTY and the session."""
    q = shlex.quote(name)
    return (
        f"if command -v tmux >/dev/null 2>&1; then exec tmux -u new-session -A -s {q}; "
        f"elif command -v screen >/dev/null 2>&1; then exec screen -xRR {q}; "
        f'else exec "${{SHELL:-/bin/sh}}" -l; fi'
    )


def multiplex_enabled() -> bool:
    return (_get("ssh_terminal_multiplex", "true") or "true").strip().lower() != "false"


def _minutes(key: str) -> float:
    try:
        return max(0.0, float(str(_get(key, "")).strip() or 0)) * 60
    except Exception:
        return 0.0


def limits() -> tuple[float, float, float]:
    """(idle, max age, detach grace) in seconds; 0 = no bound. Read once per session, at connect —
    every setting here is a relay round trip, and the reader polls twenty times a second."""
    return (_minutes("ssh_terminal_idle_min"),
            _minutes("ssh_terminal_max_hours") * 60,
            _minutes("ssh_terminal_detach_min"))


@dataclass
class SshHost:
    name: str
    user: str
    host: str
    port: int = 22
    key: str = ""


def _get(key: str, default: str = "") -> str:
    return settings_store.get(key) or default


def is_enabled() -> bool:
    return _get("ssh_terminal_enabled", "false").strip().lower() == "true"


def available() -> bool:
    """paramiko is an OPTIONAL dependency: `sync.sh` deploys code, not packages, so a node can be
    running this file without the library. Saying so plainly beats a 500 from an import error."""
    try:
        import paramiko
        return bool(paramiko)
    except Exception:
        return False


_HOST_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]{1,40})\s+(?P<user>[^@\s]{1,64})@(?P<host>[^\s:]{1,255})"
                      r"(?::(?P<port>\d{1,5}))?(?P<rest>.*)$")


def hosts() -> dict[str, SshHost]:
    """The configured destinations, by name. Malformed lines are skipped rather than guessed at."""
    out: dict[str, SshHost] = {}
    for raw in (_get("ssh_hosts") or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _HOST_RE.match(line)
        if not m:
            logger.warning("[ssh] ignoring unparseable host line: %r", line[:80])
            continue
        port = int(m.group("port") or 22)
        if not (0 < port < 65536):
            continue
        key = ""
        # shlex.split raises ValueError on an unbalanced quote — and an apostrophe in a trailing
        # comment ("# don't touch") is one. That escaped the per-line guard above, so ONE malformed
        # line took down the host list for every host: a 500 from /api/ssh/hosts and a dead socket.
        # A line we cannot tokenise still yields its destination; only its options are dropped.
        try:
            toks = shlex.split(m.group("rest") or "")
        except ValueError:
            logger.warning("[ssh] host %r has an unbalanced quote; ignoring its options", m.group("name"))
            toks = []
        for tok in toks:
            if tok == "#" or tok.startswith("#"):
                break                      # a trailing comment, not an option
            if tok.startswith("key="):
                key = tok[4:]
        name = m.group("name")
        if name in out:
            continue
        out[name] = SshHost(name=name, user=m.group("user"), host=m.group("host"), port=port, key=key)
    return out


def user_allowed(db: Session, user) -> bool:
    """Admins and the first signup always; everyone else must be an allowlisted npub.

    IDENTITIES ARE COMPARED AS CANONICAL PUBKEY HEX, through the same `nostr_service.to_pubkey_hex`
    that auth, blossom, the client and node_service use. The first version of this read
    `user.nostr_pubkey` and `user.npub` — neither of which exists. The column is `nostr_npub` and it
    stores HEX, while the setting is filled in with an `npub1…`, so even reading the right attribute
    would compare two different encodings of the same key and never match. The allowlist was
    therefore dead: an admin could paste a colleague's npub in and that colleague would be refused
    for ever, with the UI insisting the setting was applied.

    (My own test for this passed, because its fake user carried the attribute I had imagined. That is
    what a stub is for and also how a stub lies — the test now builds a user the way the model does.)

    Mirrors node_service.user_allowed deliberately: two answers to "may this person run commands" is
    how one of them ends up wrong."""
    if user is None:
        return False
    if getattr(user, "is_admin", False) or getattr(user, "id", None) == 1:
        return True
    raw = _get("ssh_terminal_users")
    if not raw:
        return False
    from app.services.nostr import nostr_service
    me = nostr_service.to_pubkey_hex(getattr(user, "nostr_npub", None) or "")
    if not me:
        return False
    me = me.lower()
    allowed = {h.lower() for h in
               (nostr_service.to_pubkey_hex(x) for x in re.split(r"[,\s]+", raw) if x.strip()) if h}
    return me in allowed


# What a reattaching client is shown of what it missed. A shell, not a transcript: enough that a build
# that finished while you were on the train is still on screen when you come back.
REPLAY_MAX = 256 * 1024

_sessions: dict = {}             # sid -> SshSession, live AND detached
_sid_seq = 0


def _new_sid() -> str:
    global _sid_seq
    _sid_seq += 1
    return f"{int(time.time()):x}-{_sid_seq:x}-{os.urandom(4).hex()}"


class SshSession:
    """One PTY, which OUTLIVES the socket that opened it.

    Every blocking paramiko call runs in a worker thread (`asyncio.to_thread`): the library is
    synchronous, and doing this on the event loop would stall every other request on the node for the
    length of a TCP connect to an unreachable host.

    THE SESSION READS THE CHANNEL, not the WebSocket handler. That is what makes resume work, and it
    is not merely tidier: if nobody drained the channel while the client was away, paramiko's receive
    window would fill and the REMOTE process would block mid-command — so a build kicked off before a
    circuit dropped would sit frozen instead of finishing. The reader keeps draining into a bounded
    buffer, and a client that comes back is shown what it missed."""

    def __init__(self, user_id=None, host_name=""):
        self.client = None
        self.chan = None
        self.sid = _new_sid()
        self.user_id = user_id
        self.host_name = host_name
        self.buf = bytearray()          # what has arrived; trimmed to REPLAY_MAX
        self.seq = 0                    # total bytes ever produced — a cursor a client can resume from
        self.wake = asyncio.Event()     # set whenever new bytes land
        self.detached_at = None         # when the LAST socket went away; None while any is attached
        self.attached = 0               # how many clients are watching (see attach/detach)
        self.started = time.time()
        self.last_in = time.time()
        self._reader = None
        self.closed_reason = ""
        self.killed = False
        self.mux = False
        self.mux_name = ""
        self._idle, self._max, self._grace = limits()

    # ---- lifetime ------------------------------------------------------------------------------
    #
    # COUNTED, not a boolean. Two devices may hold the same session at once — that is what "resume on
    # another device" means in practice, since you rarely close the laptop before picking up the
    # phone. With a flag, the laptop's socket dropping would mark the session detached while the
    # phone was actively typing in it, and an operator-configured grace would then reap a shell
    # somebody was using.
    def attach(self):
        self.attached += 1
        self.detached_at = None

    def detach(self):
        self.attached = max(0, self.attached - 1)
        if not self.attached:
            self.detached_at = time.time()

    @property
    def detached(self) -> bool:
        return self.detached_at is not None

    def _push(self, data: bytes):
        self.buf.extend(data)
        self.seq += len(data)
        if len(self.buf) > REPLAY_MAX:
            del self.buf[:len(self.buf) - REPLAY_MAX]
        self.wake.set()

    async def _drain(self):
        """Always running while the PTY is open — see the class note."""
        try:
            while True:
                if self.closed():
                    break
                if self.read_ready():
                    data = await self.read()
                    if not data:
                        break
                    self._push(data)
                    continue
                # Every bound is off unless the operator set one — see the note at the top of this
                # module. `_idle` counts from the last KEYSTROKE while somebody is attached, so it can
                # reap a forgotten prompt; it deliberately does not run while detached, where the
                # grace is the bound that applies, or "left it running overnight" and "walked away
                # from a prompt" would be the same thing.
                now = time.time()
                if self._max and now - self.started > self._max:
                    self.closed_reason = "closed: this server caps a session's age"
                    break
                if self._idle and not self.detached and now - self.last_in > self._idle:
                    self.closed_reason = "closed: nothing typed for a while"
                    break
                if self._grace and self.detached and now - self.detached_at > self._grace:
                    self.closed_reason = "closed: this server does not hold a detached session open"
                    break
                await asyncio.sleep(0.025)
        except Exception as e:                     # a reader that dies must not leave a zombie PTY
            logger.warning("[ssh] reader for %s ended: %s", self.sid, e)
        finally:
            self.wake.set()                        # release anyone waiting on the next byte
            self.close()
            _sessions.pop(self.sid, None)

    async def connect(self, h: SshHost, password: str = "", cols: int = 80, rows: int = 24,
                      label: str = "main") -> None:
        import paramiko

        self.mux_name = _mux_name(self.user_id, label) if multiplex_enabled() else ""
        mux = _mux_command(self.mux_name) if self.mux_name else ""
        self.mux = bool(mux)

        def _open():
            cli = paramiko.SSHClient()
            # Accept an unknown host key. This is a terminal the operator pointed at their own
            # machines, and refusing to connect until someone hand-populates known_hosts ON THE
            # SERVER would make the feature unusable — but say so, because it is a real trade and it
            # belongs in the log rather than in nobody's head.
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = dict(hostname=h.host, port=h.port, username=h.user,
                          timeout=15, auth_timeout=20, banner_timeout=20,
                          allow_agent=False, look_for_keys=False)
            if h.key:
                kwargs["key_filename"] = h.key
            if password:
                kwargs["password"] = password
            cli.connect(**kwargs)
            if mux:
                # A PTY running the multiplexer, rather than invoke_shell's login shell. Same channel
                # shape either way — `get_pty` + `exec_command` IS what invoke_shell does, with a
                # command instead of the default shell — so nothing downstream cares which it got.
                chan = cli.get_transport().open_session()
                chan.get_pty(term="xterm-256color", width=cols, height=rows)
                chan.exec_command(mux)
            else:
                chan = cli.invoke_shell(term="xterm-256color", width=cols, height=rows)
            chan.settimeout(0.0)          # non-blocking; the reader polls
            return cli, chan

        self.client, self.chan = await asyncio.to_thread(_open)
        logger.info("[ssh] opened %s@%s:%s (session %s)", h.user, h.host, h.port, self.sid)
        _sessions[self.sid] = self
        self._reader = asyncio.create_task(self._drain())

    async def send(self, data: str) -> None:
        """`sendall`, never `send`.

        `Channel.send` writes at most one packet (~32KB) and RETURNS THE COUNT — the remainder is
        simply dropped. xterm delivers a paste as a single onData string, so pasting a here-doc, a
        base64 key or a long pipeline would run a silently truncated command on the remote host. The
        channel is also non-blocking (`settimeout(0.0)`), where a full transmit window makes `send`
        raise instead of waiting; `sendall` handles both."""
        if not self.chan:
            return
        self.last_in = time.time()
        await asyncio.to_thread(self.chan.sendall, data)

    async def resize(self, cols: int, rows: int) -> None:
        if not self.chan:
            return
        cols = max(20, min(500, int(cols or 80)))
        rows = max(5, min(200, int(rows or 24)))
        await asyncio.to_thread(self.chan.resize_pty, cols, rows)

    def read_ready(self) -> bool:
        return bool(self.chan and self.chan.recv_ready())

    def closed(self) -> bool:
        return not self.chan or self.chan.exit_status_ready() or self.chan.closed

    async def read(self) -> bytes:
        if not self.chan:
            return b""
        return await asyncio.to_thread(self.chan.recv, READ_CHUNK)

    def since(self, cursor: int) -> bytes:
        """What a returning client missed, from its cursor. A cursor older than the retained buffer
        gets the whole buffer — it is a shell, not a transcript, and saying so beats a silent gap."""
        have = self.seq - len(self.buf)
        if cursor is None or cursor < have:
            return bytes(self.buf)
        return bytes(self.buf[cursor - have:])

    async def terminate(self) -> None:
        """END IT FOR REAL, including the multiplexer session on the far end.

        `close()` only drops OUR connection — which, when the shell is running inside tmux/screen, is
        exactly what DETACHING from it does. So without this, "Kill" left the remote session running
        with everything in it, the UI said "anything running in it is stopped", and the next Connect
        silently reattached to the shell you thought you had ended. That is the same class of bug as
        a delete that does not delete.

        Both spellings are sent because the host decided which multiplexer to use, not us, and
        neither failing matters: this is best-effort cleanup on the way out, and the connection is
        closed either way."""
        cli, name = self.client, self.mux_name
        if cli and name:
            def _quit():
                for cmd in (f"tmux kill-session -t {shlex.quote(name)} 2>/dev/null",
                            f"screen -S {shlex.quote(name)} -X quit 2>/dev/null"):
                    try:
                        cli.exec_command(cmd, timeout=5)
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(asyncio.to_thread(_quit), timeout=10)
            except Exception as e:
                logger.warning("[ssh] could not end %s's multiplexer session: %s", self.sid, e)
        self.close()

    def close(self) -> None:
        for obj in (self.chan, self.client):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.chan = self.client = None
        _sessions.pop(self.sid, None)


async def kill(sid: str, user_id) -> bool:
    """End a session outright — the counterpart to detaching, and the only thing that ends one now
    that they do not expire. Ownership-checked for the same reason `get_session` is."""
    s = get_session(sid, user_id)
    if not s:
        return False
    s.killed = True
    s.closed_reason = "killed"
    await s.terminate()
    s.wake.set()                 # let the reader (and any attached socket) notice immediately
    return True


def get_session(sid: str, user_id):
    """A session by id, and ONLY for the account that opened it.

    The id is the whole authorisation for reattaching to a live shell, so it is checked against the
    user rather than trusted for being unguessable. Without that, a leaked id in a log or a shared
    device would be a shell on somebody else's servers."""
    s = _sessions.get(str(sid or ""))
    if not s or s.user_id != user_id:
        return None
    return s


def live_count() -> int:
    return len(_sessions)


def sessions_for(user_id):
    """What this account has open — including detached ones, which is what a client needs in order to
    offer 'you have a shell still running' rather than silently starting a second one."""
    now = time.time()
    return [{"sid": s.sid, "host": s.host_name, "detached": s.detached,
             "age": int(now - s.started),
             "idle": int(now - s.detached_at) if s.detached_at else 0,
             "bytes": s.seq}
            for s in _sessions.values() if s.user_id == user_id]

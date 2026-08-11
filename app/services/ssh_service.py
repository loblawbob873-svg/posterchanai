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
import re
import shlex
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services import settings_store

logger = logging.getLogger(__name__)

# A PTY that nobody types in is a login left open on a remote host. Both bounds are deliberate.
IDLE_TIMEOUT = 30 * 60          # no input for this long → close
MAX_SESSION = 12 * 60 * 60      # a session may not outlive this, however busy
READ_CHUNK = 32 * 1024


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


class SshSession:
    """One PTY. Owns the paramiko client and channel and nothing else.

    Every blocking paramiko call runs in a worker thread (`asyncio.to_thread`): the library is
    synchronous, and doing this on the event loop would stall every other request on the node for the
    length of a TCP connect to an unreachable host."""

    def __init__(self):
        self.client = None
        self.chan = None

    async def connect(self, h: SshHost, password: str = "", cols: int = 80, rows: int = 24) -> None:
        import paramiko

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
            chan = cli.invoke_shell(term="xterm-256color", width=cols, height=rows)
            chan.settimeout(0.0)          # non-blocking; the reader polls
            return cli, chan

        self.client, self.chan = await asyncio.to_thread(_open)
        logger.info("[ssh] opened %s@%s:%s", h.user, h.host, h.port)

    async def send(self, data: str) -> None:
        """`sendall`, never `send`.

        `Channel.send` writes at most one packet (~32KB) and RETURNS THE COUNT — the remainder is
        simply dropped. xterm delivers a paste as a single onData string, so pasting a here-doc, a
        base64 key or a long pipeline would run a silently truncated command on the remote host. The
        channel is also non-blocking (`settimeout(0.0)`), where a full transmit window makes `send`
        raise instead of waiting; `sendall` handles both."""
        if not self.chan:
            return
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

    def close(self) -> None:
        for obj in (self.chan, self.client):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.chan = self.client = None

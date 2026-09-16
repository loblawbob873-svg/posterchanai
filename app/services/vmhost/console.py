"""Console tickets: the one-use bearer that turns a Nostr-authorised request into a VNC socket.

The console runs over the node's HTTPS (`/ws/vmconsole`) because noVNC needs a WebSocket, and Nostr
is the wrong transport for a framebuffer. So authorisation happens over Nostr — `console.ticket` is
signed by the requester and checked against the same role/assignment rules as every other op — and
what comes back, inside the NIP-44 result only that requester can read, is:

  * a TICKET: 32 random bytes, valid for `vmhost_console_ticket_ttl_sec` (60s), bound to ONE VM and
    ONE pubkey, consumed atomically on first use. It is sent in the socket's FIRST FRAME, never the
    URL — a query string is written into every proxy access log between the client and here, and a
    logged bearer is a published one;
  * a VNC password, set on the guest's display for the same 60 seconds (QMP `set_password` +
    `expire_password`), so even a process on this host that reaches the loopback VNC port cannot
    attach with a stale one.

A console is also a LIVE grant: unassigning the user, stopping the VM or deleting it closes every
console open on it (`revoke`). Otherwise removing somebody's access would leave them typing into a
guest for the rest of a four-hour session.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

TICKETS_PER_MINUTE = 6


@dataclass
class Ticket:
    vm: str
    pubkey: str
    exp: float


class ConsoleRegistry:
    def __init__(self, now=time.time):
        self.now = now
        self._tickets: dict = {}
        self._issued: dict = {}              # pubkey -> [timestamps]
        self._live: dict = {}                # vm uuid -> {id: (pubkey, closer)}
        self._pw: dict = {}                  # vm uuid -> (VNC password, latest ticket expiry)
        self._lock = threading.Lock()        # consume() is reached from the ws route's task

    # ---- tickets ------------------------------------------------------------------------------
    def rate_ok(self, pubkey: str) -> bool:
        t = self.now()
        with self._lock:
            recent = [x for x in self._issued.get(pubkey, []) if t - x < 60]
            self._issued[pubkey] = recent
            return len(recent) < TICKETS_PER_MINUTE

    def issue(self, vm: str, pubkey: str, ttl: int, password: str | None = None) -> tuple:
        tok = secrets.token_urlsafe(32)
        exp = self.now() + max(1, int(ttl))
        with self._lock:
            self._sweep()
            self._tickets[tok] = Ticket(vm=vm, pubkey=pubkey, exp=exp)
            self._issued.setdefault(pubkey, []).append(self.now())
            if password:
                prev = self._pw.get(vm)
                self._pw[vm] = (password, max(exp, prev[1]) if prev and prev[0] == password else exp)
        return tok, int(exp)

    def current_password(self, vm: str) -> str | None:
        """The VM's display password while a ticket issued with it is still unexpired, else None.
        Two people sharing a VM must not rotate each other's password: A is issued a ticket and is
        still logging in when B asks for one — a new password for B fails A's authentication."""
        with self._lock:
            rec = self._pw.get(vm)
            if rec is None:
                return None
            if rec[1] < self.now():
                self._pw.pop(vm, None)
                return None
            return rec[0]

    def consume(self, token) -> Ticket | None:
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        with self._lock:
            t = self._tickets.pop(token, None)
        if t is None or t.exp < self.now():
            return None
        return t

    def _sweep(self) -> None:
        t = self.now()
        for k in [k for k, v in self._tickets.items() if v.exp < t]:
            self._tickets.pop(k, None)

    # ---- live consoles ------------------------------------------------------------------------
    def attach(self, vm: str, pubkey: str, closer) -> str:
        cid = secrets.token_hex(8)
        with self._lock:
            self._live.setdefault(vm, {})[cid] = (pubkey, closer)
        return cid

    def detach(self, vm: str, cid: str) -> None:
        with self._lock:
            self._live.get(vm, {}).pop(cid, None)
            if vm in self._live and not self._live[vm]:
                self._live.pop(vm, None)

    def live_count(self, vm: str | None = None) -> int:
        with self._lock:
            if vm:
                return len(self._live.get(vm, {}))
            return sum(len(v) for v in self._live.values())

    def revoke(self, vm: str, pubkey: str | None = None) -> int:
        """Drop unused tickets and close open consoles for `vm` (only `pubkey`'s when given)."""
        with self._lock:
            for k in [k for k, v in self._tickets.items()
                      if v.vm == vm and (pubkey is None or v.pubkey == pubkey)]:
                self._tickets.pop(k, None)
            # Whoever was revoked may hold the current password: the next ticket rotates it.
            self._pw.pop(vm, None)
            victims = [(cid, c) for cid, (pk, c) in self._live.get(vm, {}).items()
                       if pubkey is None or pk == pubkey]
        n = 0
        for _cid, closer in victims:
            try:
                closer()
                n += 1
            except Exception:
                pass
        return n

    def close_all(self) -> int:
        """Close EVERY open console and drop every unused ticket — the host service is stopping (a
        settings Save restarts it with a NEW registry, and this one is the only thing that can reach
        these sockets)."""
        with self._lock:
            self._tickets.clear()
            self._pw.clear()
            victims = [c for per_vm in self._live.values() for (_pk, c) in per_vm.values()]
            self._live.clear()
        n = 0
        for closer in victims:
            try:
                closer()
                n += 1
            except Exception:
                pass
        return n

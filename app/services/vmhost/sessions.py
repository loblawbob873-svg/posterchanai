"""Session keys: poll a host without asking a remote signer to sign every ten seconds, and without
letting that convenience manage anything.

WHY. The VM screen polls (host.whoami/info, vm.list every 10 s, power, console tickets). With a local
nsec that is free. With a REMOTE signer — NIP-46 bunker, Amber, a browser extension — every request is a
round trip to a phone or a popup, so polling either drains the signer or does not happen. So a client may
open a SESSION: a throwaway key the host accepts, for a bounded time, on behalf of the real key that
opened it.

THE RULES, each enforced here on the host:
  * `session.open` must be signed by the REAL key (a session cannot open a session), names the session
    public key, an expiry no later than `vmhost_session_max_hours` from now, and `scope: "use"`, and
    carries a PROOF — an event signed by the session key over (owner, expiry) — so nobody can register a
    public key they do not hold (e.g. somebody else's real key, to capture its requests).
  * A session key that is itself somebody's real identity on this host (admin, allowed, assigned), or the
    node, is refused — its own requests must never be read as somebody else's session.
  * A request signed by a session key acts AS its owner, with the owner's role — but ONLY for the use
    ops (`SESSION_OPS`: read, power, console, closing itself). Anything else, admin or not, is refused
    `step_up_required`: sign it with the real key. That is what keeps "a key sitting in localStorage for
    twelve hours" from being "a key that can delete VMs".
  * An EXPIRED (or closed) session is answered `session_expired`, encrypted to the session key, for a
    grace day — not dropped. Dropping would look exactly like an offline host to the client, which would
    then say "no answer" about a host that is perfectly well.
  * Live sessions persist in `.state/sessions.json` (0600) so an app restart does not strand every client. An
    ended session leaves the file at once; live sessions are capped per owner (MAX_PER_OWNER, the oldest is
    closed) and on the host (MAX_SESSIONS, then `busy`); the file is written off the event loop.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

PROOF_KIND = 27310
SESSION_OPS = frozenset({"host.whoami", "host.info", "vm.list", "vm.get", "vm.power", "console.ticket",
                         "session.close"})
MAX_PER_OWNER = 8
MAX_SESSIONS = 4096             # live sessions on one host, every owner together
MAX_ENDED = 4096                # remembered ended sessions (answered session_expired), every owner together
MAX_ENDED_PER_OWNER = 16
GRACE = 24 * 3600
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# The session key a request arrived under (None = the real key). Set by VmHostService.handle; a context
# variable, not an attribute, because requests run concurrently on one service object.
CURRENT_SESSION: contextvars.ContextVar = contextvars.ContextVar("vmhost_session", default=None)


def _err(code, msg):
    from .service import VmHostError
    return VmHostError(code, msg)


def proof_content(owner: str, exp: int, host: str) -> str:
    return f"posterchan-vmhost-session:{owner}:{int(exp)}:{host}"


class SessionRegistry:
    """Live sessions only — an ended one (closed, expired, displaced by the per-owner cap) leaves the map and the
    file AT ONCE, and is remembered in a small bounded in-memory tombstone set so it is still ANSWERED
    `session_expired` (silence would look like an offline host). The file is rewritten off the event loop
    (`flush`: tmp + fsync + rename, coalesced), never inside a request."""

    def __init__(self, path: Path | None = None, now=time.time):
        self.path = Path(path) if path else None
        self.now = now
        self._s: dict = {}                     # session pk -> {"owner", "exp", "scope"}   (LIVE only)
        self._ended: "OrderedDict[str, tuple]" = OrderedDict()   # session pk -> (owner, ended at)
        self._lock = threading.Lock()          # the maps
        self._write_lock = threading.Lock()    # the file
        self._flushing = False
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for pk, rec in (data.get("sessions") or {}).items():
                if _HEX64.match(pk) and _HEX64.match(str(rec.get("owner", ""))) and int(rec.get("exp", 0)) > 0:
                    if rec.get("closed"):
                        self._end(pk, rec["owner"])
                    else:
                        self._s[pk] = {"owner": rec["owner"], "exp": int(rec["exp"]), "scope": "use"}
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning("[vmhost] sessions.json unreadable — every session will have to reopen")
        with self._lock:
            self._prune()

    # ---- bookkeeping (caller holds _lock, except from _load)
    def _end(self, spk: str, owner: str) -> None:
        self._s.pop(spk, None)
        self._ended.pop(spk, None)
        self._ended[spk] = (owner, self.now())
        mine = [k for k, (o, _) in self._ended.items() if o == owner]
        for k in mine[:max(0, len(mine) - MAX_ENDED_PER_OWNER)]:
            self._ended.pop(k, None)
        while len(self._ended) > MAX_ENDED:
            self._ended.popitem(last=False)

    def _prune(self) -> bool:
        t = self.now()
        gone = [k for k, v in self._s.items() if v["exp"] <= t]
        for k in gone:
            self._end(k, self._s[k]["owner"])
        while self._ended:
            k, (_, at) = next(iter(self._ended.items()))
            if at + GRACE >= t:
                break
            self._ended.popitem(last=False)
        return bool(gone)

    def _snapshot(self) -> str:
        return json.dumps({"v": 2, "sessions": self._s})

    def _write(self, data: str) -> None:
        if not self.path:
            return
        with self._write_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except OSError as e:
                logger.warning("[vmhost] could not save sessions: %s", e)

    async def flush(self) -> None:
        """Write the current state, off the loop. Concurrent callers coalesce: one write runs, and one more
        follows if anything changed while it ran."""
        import asyncio
        if not self.path:
            return
        if self._flushing:
            self._dirty = True
            return
        self._flushing = True
        try:
            while True:
                self._dirty = False
                with self._lock:
                    data = self._snapshot()
                await asyncio.to_thread(self._write, data)
                if not self._dirty:
                    break
        finally:
            self._flushing = False

    # ---- API
    def lookup(self, session_pk: str):
        """(owner, state) where state is 'live' or 'expired'; (None, None) for an unknown key."""
        k = str(session_pk or "").lower()
        with self._lock:
            rec = self._s.get(k)
            if rec is not None:
                if rec["exp"] > self.now():
                    return rec["owner"], "live"
                self._end(k, rec["owner"])
                self._dirty = True
                return rec["owner"], "expired"
            ended = self._ended.get(k)
            if ended is not None and ended[1] + GRACE >= self.now():
                return ended[0], "expired"
            return None, None

    def open(self, owner: str, session_pk: str, exp: int) -> None:
        with self._lock:
            self._prune()
            cur = self._s.get(session_pk)
            prev = self._ended.get(session_pk)
            if (cur is not None and cur["owner"] != owner) or (prev is not None and prev[0] != owner):
                raise _err("conflict", "that session key belongs to somebody else")
            if cur is None and len(self._s) >= MAX_SESSIONS:
                raise _err("busy", "this host has too many open sessions — try again later")
            live = sorted((k for k, v in self._s.items() if v["owner"] == owner and k != session_pk),
                          key=lambda k: self._s[k]["exp"])
            for k in live[:max(0, len(live) - MAX_PER_OWNER + 1)]:
                self._end(k, owner)
            self._ended.pop(session_pk, None)
            self._s[session_pk] = {"owner": owner, "exp": int(exp), "scope": "use"}

    def close(self, owner: str, session_pk: str | None = None) -> int:
        with self._lock:
            mine = [k for k, v in self._s.items() if v["owner"] == owner and (session_pk is None or k == session_pk)]
            for k in mine:
                self._end(k, owner)
            return len(mine)


class SessionOps:
    def _sessions(self) -> SessionRegistry:
        if getattr(self, "_sessions_o", None) is None:
            self._sessions_o = SessionRegistry(self.storage.state_dir / "sessions.json", now=self.now)
        return self._sessions_o

    async def _op_session_open(self, pk, role, args, progress):
        from app.services.nostr import event as nostr_event
        spk = str(args.get("pk") or "").lower()
        if not _HEX64.match(spk):
            raise _err("bad_request", "pk must be the session's 64-hex public key")
        if args.get("scope", "use") != "use":
            raise _err("bad_request", 'the only session scope is "use"')
        try:
            exp = int(args.get("exp"))
        except (TypeError, ValueError):
            raise _err("bad_request", "exp (unix seconds) is required")
        now = int(self.now())
        max_exp = now + self.cfg.session_max_hours * 3600
        if exp <= now + 60:
            raise _err("bad_request", "exp is in the past")
        if exp > max_exp:
            raise _err("bad_request", f"a session may last at most {self.cfg.session_max_hours} hours on this host")
        if spk == pk or spk == self.node_pubkey or await self.role_of(spk) is not None:
            raise _err("bad_request", "a session key must be a fresh key, not an identity on this host")
        proof = args.get("proof")
        if (not isinstance(proof, dict) or proof.get("pubkey") != spk or proof.get("kind") != PROOF_KIND
                or proof.get("content") != proof_content(pk, exp, self.node_pubkey)
                or not nostr_event.verify_event(proof)):
            raise _err("bad_request", "the session proof is missing or not signed by the session key")
        self._sessions().open(pk, spk, exp)
        await self._sessions().flush()
        logger.info("[vmhost] session opened for %s until %d", pk[:12], exp)
        return {"pk": spk, "exp": exp, "scope": "use", "ops": sorted(SESSION_OPS)}

    async def _op_session_close(self, pk, role, args, progress):
        via = CURRENT_SESSION.get()
        target = via or (str(args.get("pk")).lower() if args.get("pk") else None)
        n = self._sessions().close(pk, target)
        if n:
            await self._sessions().flush()
        return {"closed": n}

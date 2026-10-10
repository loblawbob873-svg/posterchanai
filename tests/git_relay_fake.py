"""A stand-in for THIS node's relay, for the git tests (#161: git_auth reads events from the relay, not
from its Postgres tables).

Two shapes, both answering NIP-01 filters the way the real store does (ids / authors / kinds / exact
single-letter tag filters / limit, newest first by created_at then id):

  * `FakeRelay(events)`  -- what git_auth's loaders take in place of the old psycopg2 connection
    (anything with `.query(filters)`). `down=True` raises relay_reader.Unavailable, the "could not
    ask" answer every caller must fail closed on. `asked` records the authors each read named.
  * `serve(events)`      -- a real websocket relay on 127.0.0.1 (for the pre-receive hook, which runs
    in its own process). It sends a NIP-42 challenge on connect, as the real relay does, and accepts
    AUTH. `private_readers` withholds GRASP-08 private repo events from a socket not authenticated as
    one of them and answers `auth-required`, as nostr_relay/server.py does.
"""
from __future__ import annotations

import json
import secrets
import threading


def _match(ev: dict, flt: dict) -> bool:
    if flt.get("ids") and ev.get("id") not in flt["ids"]:
        return False
    if flt.get("authors") and ev.get("pubkey") not in flt["authors"]:
        return False
    if flt.get("kinds") and int(ev.get("kind", -1)) not in [int(k) for k in flt["kinds"]]:
        return False
    for key, want in flt.items():
        if key.startswith("#") and len(key) == 2:
            have = {str(t[1]) for t in ev.get("tags") or [] if len(t) >= 2 and t[0] == key[1]}
            if not have & {str(w) for w in want}:
                return False
    return True


def _order(events):
    return sorted(events, key=lambda e: (int(e.get("created_at", 0)), str(e.get("id", ""))), reverse=True)


def answer(events, filters) -> list:
    out = {}
    for flt in filters:
        hits = _order([e for e in events if _match(e, flt)])
        for e in hits[: int(flt.get("limit") or 500)]:
            out[e["id"]] = e
    return _order(out.values())


class FakeRelay:
    def __init__(self, events=(), *, down: bool = False):
        self.events, self.down = list(events), down
        self.asked: list = []
        self.filters: list = []

    def query(self, filters: list) -> list:
        from app.services import relay_reader
        if self.down:
            raise relay_reader.Unavailable("ConnectionRefusedError: the test relay is down")
        self.filters.extend(filters)
        for f in filters:
            self.asked.extend(f.get("authors") or [])
        return answer(self.events, filters)


def _is_private(ev) -> bool:
    from app.services import git_auth
    return int(ev.get("kind", 0)) in (30617, 30618) and git_auth.event_says_private(ev)


def serve(events, *, private_readers=()):
    """(server, port). The server holds `events` (a list it keeps reading, so a test may append)."""
    from websockets.sync.server import serve as _serve
    from app.services.nostr.event import verify_event

    readers = set(private_readers)

    def handler(ws):
        challenge = secrets.token_hex(16)
        authed = set()
        ws.send(json.dumps(["AUTH", challenge]))
        for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                ev = msg[1]
                ok = (verify_event(ev) and ev.get("kind") == 22242
                      and any(t[:2] == ["challenge", challenge] for t in ev.get("tags", [])))
                if ok:
                    authed.add(ev["pubkey"])
                ws.send(json.dumps(["OK", ev.get("id"), bool(ok), "" if ok else "invalid"]))
            elif msg[0] == "REQ":
                sub, filters = msg[1], msg[2:]
                withheld = False
                for ev in reversed(answer(events, filters)):
                    if _is_private(ev) and not (readers & authed):
                        withheld = True
                        continue
                    ws.send(json.dumps(["EVENT", sub, ev]))
                if withheld:
                    ws.send(json.dumps(["CLOSED", sub, "auth-required: private repositories need AUTH"]))
                else:
                    ws.send(json.dumps(["EOSE", sub]))
            elif msg[0] == "CLOSE":
                pass

    srv = _serve(handler, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.socket.getsockname()[1]

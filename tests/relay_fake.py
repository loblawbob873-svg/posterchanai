"""A small in-memory relay that speaks the websocket protocol THIS node's relay speaks to a loopback reader
(relay_reader, #161): REQ with NIP-01 filters plus the local `#d~` prefix and `_cursor` extensions, NIP-45
COUNT, and the NIP-78 rule (kinds 78/30078 are served only to their AUTHed author and never counted for anyone
else). AUTH is challenged on connect, as the real relay does.

For tests that used to fake the relay's Postgres tables: the code under test now asks a relay, so the fake is
a relay.
"""
from __future__ import annotations

import json
import secrets
import threading

from websockets.sync.server import serve

NIP78 = (78, 30078)


def _matches(ev: dict, flt: dict) -> bool:
    if flt.get("ids") and ev["id"] not in flt["ids"]:
        return False
    if flt.get("authors") and ev["pubkey"] not in flt["authors"]:
        return False
    if flt.get("kinds") and int(ev["kind"]) not in [int(k) for k in flt["kinds"]]:
        return False
    if flt.get("since") is not None and ev["created_at"] < int(flt["since"]):
        return False
    if flt.get("until") is not None and ev["created_at"] > int(flt["until"]):
        return False
    cur = flt.get("_cursor")
    if isinstance(cur, list) and len(cur) == 2:
        if not (ev["created_at"] < int(cur[0]) or (ev["created_at"] == int(cur[0]) and ev["id"] < str(cur[1]))):
            return False
    for key, vals in flt.items():
        if not (isinstance(key, str) and key.startswith("#") and vals):
            continue
        name = key[1]
        tagvals = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == name]
        if len(key) == 2:
            if not set(map(str, vals)) & set(tagvals):
                return False
        elif len(key) == 3 and key.endswith("~"):
            if not any(tv.startswith(str(v)) for v in vals for tv in tagvals):
                return False
    return True


def _order(evs):
    return sorted(evs, key=lambda e: (e["created_at"], e["id"]), reverse=True)


class FakeRelay:
    def __init__(self, events=(), *, max_filters_per_req: int = 10):
        self.events = list(events)
        self.max_filters = max_filters_per_req
        self.log: list = []          # every message type received, in order
        self.auths: list = []        # pubkeys that authenticated
        self._srv = serve(self._handle, "127.0.0.1", 0)
        self.port = self._srv.socket.getsockname()[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self):
        self._srv.shutdown()

    # -- the protocol ------------------------------------------------------------------------------------
    def _handle(self, ws):
        challenge = secrets.token_hex(8)
        authed: set = set()
        ws.send(json.dumps(["AUTH", challenge]))
        for raw in ws:
            msg = json.loads(raw)
            self.log.append(msg[0])
            if msg[0] == "AUTH":
                ev = msg[1]
                ok = (ev.get("kind") == 22242
                      and ["challenge", challenge] in [list(t) for t in ev.get("tags", [])])
                if ok:
                    authed.add(ev["pubkey"])
                    self.auths.append(ev["pubkey"])
                ws.send(json.dumps(["OK", ev.get("id"), ok, "" if ok else "invalid"]))
            elif msg[0] == "REQ":
                sub, filters = msg[1], [f for f in msg[2:] if isinstance(f, dict)][: self.max_filters]
                explicit = any(int(k) in NIP78 for f in filters for k in (f.get("kinds") or []))
                owners = {pk for f in filters for pk in (f.get("authors") or [])}
                if explicit and not (owners and owners <= authed):
                    ws.send(json.dumps(["CLOSED", sub, "auth-required: NIP-78 reads require AUTH"]))
                    continue
                seen = {}
                for f in filters:
                    lim = max(1, min(int(f.get("limit") or 500), 5000))
                    for ev in _order([e for e in self.events if _matches(e, f)])[:lim]:
                        seen[ev["id"]] = ev
                for ev in reversed(_order(seen.values())):
                    if int(ev["kind"]) in NIP78 and ev["pubkey"] not in authed:
                        continue        # served only to the author
                    ws.send(json.dumps(["EVENT", sub, ev]))
                ws.send(json.dumps(["EOSE", sub]))
            elif msg[0] == "COUNT":
                sub, f = msg[1], msg[2]
                explicit = any(int(k) in NIP78 for k in (f.get("kinds") or []))
                n = sum(1 for e in self.events if _matches(e, f) and (explicit or int(e["kind"]) not in NIP78))
                ws.send(json.dumps(["COUNT", sub, {"count": n}]))


_seq = [0]


def ev(pubkey: str, kind: int, created_at: int, *, tags=None, content: str = "", eid: str | None = None) -> dict:
    """An event as the relay stores it (unsigned: the code under test reads, it does not verify)."""
    _seq[0] += 1
    return {"id": eid or ("%064x" % _seq[0]), "pubkey": pubkey, "kind": kind, "created_at": created_at,
            "tags": tags or [], "content": content, "sig": "0" * 128}

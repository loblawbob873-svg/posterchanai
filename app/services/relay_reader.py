"""Read events from THIS node's relay, synchronously -- the one door for code that used to SELECT from the relay's
Postgres tables directly (#161, retiring Postgres as the relay's store).

Two rules every former SQL reader keeps by going through here:

  * "could not ask" is an EXCEPTION (`Unavailable`), never an empty list. A dead socket, a timeout or a relay that
    closed the subscription with an error must not read as "there are no events" -- an auth decision made on an
    empty answer would refuse a maintainer, and a writer acting on it would replace real data with nothing;
  * the relay answers a REQ exactly as it answers any client, so these readers see what clients see (PosterChanDB
    when the mirror serves, Postgres otherwise) and need no database credentials at all.

Import-light on purpose (stdlib + `websockets`): the git pre-receive hook runs it in its own process.
"""
import json
import os
import uuid

DEFAULT_PORT = 3052


class Unavailable(RuntimeError):
    """The relay could not be asked. Never "no events"."""


def relay_port() -> int:
    """POSTERCHANAI_RELAY_PORT for a process that has no settings store (the git hook); else the setting."""
    env = os.environ.get("POSTERCHANAI_RELAY_PORT")
    if env and env.isdigit():
        return int(env)
    try:
        from app.services import settings_store
        return settings_store.get_int("nostr_relay_port", DEFAULT_PORT)
    except Exception:       # noqa: BLE001 -- a process without the app's settings
        return DEFAULT_PORT


def _authenticate(ws, target: str, seckey: bytes, timeout: float) -> None:
    """NIP-42: answer the challenge this relay sends on connect, and wait for it to be accepted -- kind 30078
    (the app's datastore) is served only to its author, so a reader of the operator's documents signs in first.
    A refused or missing challenge is Unavailable: reading on without it would answer "no documents"."""
    from app.services.nostr.event import build_event      # lazy: the git hook never authenticates
    auth_id = None
    while True:
        msg = json.loads(ws.recv(timeout=timeout))
        if not isinstance(msg, list) or not msg:
            continue
        if msg[0] == "AUTH" and len(msg) >= 2 and auth_id is None:
            ev = build_event(seckey, 22242, "", tags=[["relay", target], ["challenge", str(msg[1])]])
            auth_id = ev["id"]
            ws.send(json.dumps(["AUTH", ev]))
        elif msg[0] == "OK" and len(msg) >= 3 and auth_id and msg[1] == auth_id:
            if not msg[2]:
                raise Unavailable("the relay refused NIP-42 AUTH: %s" % (msg[3] if len(msg) > 3 else ""))
            return


def _target(url, port) -> str:
    return url or "ws://127.0.0.1:%d/relay" % (port or relay_port())


def query(filters: list, *, port: int | None = None, timeout: float = 10.0, url: str | None = None,
          auth_seckey: bytes | None = None) -> list:
    """Every event matching `filters` (a list of NIP-01 filter dicts), up to each filter's own limit.

    `auth_seckey` signs in (NIP-42) as that key before asking -- needed to read NIP-78 documents, which this
    relay serves only to their author.

    Raises Unavailable when the relay cannot be asked or does not finish answering in `timeout` seconds."""
    if not filters:
        return []
    try:
        from websockets.sync.client import connect
    except Exception as e:      # noqa: BLE001
        raise Unavailable("websockets is not installed: %s" % e) from e
    target = _target(url, port)
    sub = "rr" + uuid.uuid4().hex[:10]
    out: list = []
    try:
        with connect(target, open_timeout=timeout, close_timeout=2, max_size=64 * 1024 * 1024) as ws:
            if auth_seckey:
                _authenticate(ws, target, auth_seckey, timeout)
            ws.send(json.dumps(["REQ", sub, *filters]))
            while True:
                msg = json.loads(ws.recv(timeout=timeout))
                if not isinstance(msg, list) or not msg:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3 and msg[1] == sub:
                    out.append(msg[2])
                elif msg[0] == "EOSE" and msg[1] == sub:
                    break
                elif msg[0] == "CLOSED" and msg[1] == sub:
                    raise Unavailable("the relay closed the query: %s" % (msg[2] if len(msg) > 2 else ""))
            try:
                ws.send(json.dumps(["CLOSE", sub]))
            except Exception:       # noqa: BLE001
                pass
    except Unavailable:
        raise
    except Exception as e:      # noqa: BLE001
        raise Unavailable("%s: %s" % (type(e).__name__, e)) from e
    return out


def counts(filters: list, *, port: int | None = None, timeout: float = 10.0, url: str | None = None) -> list:
    """NIP-45: one COUNT per filter in `filters`, all on one socket; the answers in the same order.

    The relay counts what it would serve a client that has not signed in, so NIP-78 documents (kinds 78/30078)
    are never in a count. Raises Unavailable when any count cannot be had -- a missing count is never 0."""
    if not filters:
        return []
    try:
        from websockets.sync.client import connect
    except Exception as e:      # noqa: BLE001
        raise Unavailable("websockets is not installed: %s" % e) from e
    base = "rc" + uuid.uuid4().hex[:8]
    subs = ["%s-%d" % (base, i) for i in range(len(filters))]
    got: dict = {}
    try:
        with connect(_target(url, port), open_timeout=timeout, close_timeout=2) as ws:
            for sub, flt in zip(subs, filters):
                ws.send(json.dumps(["COUNT", sub, flt]))
            want = set(subs)
            while want - set(got):
                msg = json.loads(ws.recv(timeout=timeout))
                if not isinstance(msg, list) or len(msg) < 2 or msg[1] not in want:
                    continue
                if msg[0] == "COUNT" and len(msg) >= 3 and isinstance(msg[2], dict):
                    n = msg[2].get("count")
                    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                        raise Unavailable("the relay answered a COUNT with %r" % (msg[2],))
                    got[msg[1]] = n
                elif msg[0] == "CLOSED":
                    raise Unavailable("the relay refused a COUNT: %s" % (msg[2] if len(msg) > 2 else ""))
    except Unavailable:
        raise
    except Exception as e:      # noqa: BLE001
        raise Unavailable("%s: %s" % (type(e).__name__, e)) from e
    return [got[s] for s in subs]


def tag_values(event: dict, name: str) -> list:
    return [t[1] for t in (event.get("tags") or []) if isinstance(t, list) and len(t) >= 2 and t[0] == name]


def d_tag(event: dict) -> str:
    v = tag_values(event, "d")
    return v[0] if v else ""

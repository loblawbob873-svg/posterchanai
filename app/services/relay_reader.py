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
import time
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
    """NIP-42: answer the challenge the relay sends on connect, and wait for it to ACCEPT.

    Done before the REQ, never in reply to an `auth-required` CLOSED: the relay withholds what this key
    may read (a private repository's announcement) from an unauthenticated socket, and an answer
    that silently left those out would read as "this repository has no announcement". A refused or
    missing AUTH is Unavailable, because the answer that would follow it is not the whole answer."""
    from app.services.nostr.event import build_event
    deadline = time.monotonic() + timeout

    def _next():
        left = deadline - time.monotonic()
        if left <= 0:
            raise Unavailable("the relay did not complete AUTH in %ss" % timeout)
        return json.loads(ws.recv(timeout=left))

    challenge = None
    while challenge is None:
        msg = _next()
        if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "AUTH" and isinstance(msg[1], str):
            challenge = msg[1]
    auth = build_event(seckey, 22242, "", tags=[["relay", target], ["challenge", challenge]])
    ws.send(json.dumps(["AUTH", auth]))
    while True:
        msg = _next()
        if isinstance(msg, list) and len(msg) >= 3 and msg[0] == "OK" and msg[1] == auth["id"]:
            if not msg[2]:
                raise Unavailable("the relay refused AUTH: %s" % (msg[3] if len(msg) > 3 else ""))
            return


def query(filters: list, *, port: int | None = None, timeout: float = 10.0, url: str | None = None,
          auth_seckey: bytes | None = None) -> list:
    """Every event matching `filters` (a list of NIP-01 filter dicts), up to each filter's own limit.

    With `auth_seckey`, the socket authenticates (NIP-42) as that key BEFORE asking, so the answer
    includes what the relay serves only to that key.

    Raises Unavailable when the relay cannot be asked or does not finish answering in `timeout` seconds."""
    if not filters:
        return []
    try:
        from websockets.sync.client import connect
    except Exception as e:      # noqa: BLE001
        raise Unavailable("websockets is not installed: %s" % e) from e
    target = url or "ws://127.0.0.1:%d/relay" % (port or relay_port())
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


def tag_values(event: dict, name: str) -> list:
    return [t[1] for t in (event.get("tags") or []) if isinstance(t, list) and len(t) >= 2 and t[0] == name]


def d_tag(event: dict) -> str:
    v = tag_values(event, "d")
    return v[0] if v else ""

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


def query(filters: list, *, port: int | None = None, timeout: float = 10.0, url: str | None = None) -> list:
    """Every event matching `filters` (a list of NIP-01 filter dicts), up to each filter's own limit.

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

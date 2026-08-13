"""SearXNG, running INSIDE this app — no container, no second port, no unit of its own.

WHY THIS EXISTS AT ALL is unchanged and still the point: every search this node makes — the AI's
web-search tool, the news digests, the bots, the Web Search screen — goes through one SearXNG, and a
node with none of its own either searches through somebody else's box or falls back to a public
instance that answers a server 429. So a node runs its own.

WHAT CHANGED IS HOW. It used to be the upstream Docker image under `posterchanai-searxng.service`,
because "SearXNG upstream ships a container, and its bare-metal path is a uwsgi/nginx build against
system Python that would fight the app's own venv". That is true of upstream's *deployment* docs and
false of the package: `searx.webapp.app` is an ordinary WSGI (Flask) application, and this repo
already runs one of those inside the app — Radicale, at /caldav, through a2wsgi. So SearXNG is
mounted the same way, at /searxng, and the container, the unit, the second port and the docker
dependency all go away.

MEASURED BEFORE IT WAS WRITTEN, because "it imports" is not the question:

  * `/searxng/healthz` 200, `/searxng/config` 200 application/json (the two things the app's probe
    demands), and `/searxng/search?format=json` returning 25 real results;
  * the HTML page renders with `/searxng/static/...` links and no bare `/static` — a2wsgi passes
    SCRIPT_NAME through, so Flask's url_for is correct at a sub-path with nothing to configure;
  * `pip install` of its runtime deps against this venv resolves with **no downgrades** — every one
    is a new package (httpx 0.28.1, lxml 6.1.1, jinja2 3.1.6, pygments, pyyaml, markdown-it-py and
    python-dateutil were already at the exact versions SearXNG pins).

THREE THINGS THAT BITE, all of them silent:

 1. **SearXNG is not on PyPI** (`pip index versions searxng` → "No matching distribution found"), so
    its SOURCE is cloned and installed `--no-deps`, the ACE-Step pattern. Its real runtime deps live
    in requirements.txt, at ranges rather than its exact pins — its `typing-extensions==4.16.0` and
    `certifi==2026.7.22` would otherwise be free licence to move packages that torch and pydantic
    also depend on.

 2. **`--no-build-isolation` is required.** Its setup.py does `from searx.version import ...`, which
    imports `searx/__init__.py`, which imports msgspec — absent from pip's isolated build env, so the
    build dies with ModuleNotFoundError before a single dependency of ours is even consulted.

 3. **Importing `searx` reconfigures the ROOT logger** — `logging.basicConfig(level=WARNING)` plus
    `logging.root.setLevel(WARNING)`, at import time, in `searx/__init__.py`. Left alone that
    silences every INFO line this app emits, node-wide, from the moment somebody first searches: the
    app keeps working and its logs simply go quiet, which is the worst way to find out. `_import()`
    saves and restores the root level and handler list around the import.

WHY IT STILL SPEAKS HTTP rather than calling `searx.search.Search` in-process. Two of the four
consumers are not in this process: the bots are subprocesses (they take a `SEARXNG_URL` in their
environment) and the news digests run in the worker. One interface for all four is what keeps a node
from searching two different places, so the mount is addressed as a URL like any other instance —
`http://127.0.0.1:<app port>/searxng` — and `resolve_searxng_url()`'s order is untouched.

The mount is LAZY: importing SearXNG loads its whole engine catalogue, which is seconds of startup
and tens of MB on a node that may never search. Nothing is imported until the first request reaches
/searxng.

TWO WAYS TO RUN IT, one implementation. `posterchanai-searxng.service` still exists and is still
what `systemctl status posterchanai-searxng` reports on — it just runs `python -m
app.services.searxng_native` out of the app's own venv (uvicorn + a2wsgi, loopback, the same
settings.yml) instead of a container. The in-app mount is the FALLBACK: `resolve_searxng_url()`
probes the service first, so on a node where the unit is running the app never imports SearXNG at
all, and on a node where it is stopped, masked or crashed, search keeps working instead of falling
through to a public instance. One node, one settings file, one behaviour, two places it can be
served from.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import tempfile
import threading
from importlib.util import find_spec
from pathlib import Path

logger = logging.getLogger(__name__)

MOUNT_PATH = "/searxng"

# The repo root, i.e. .../app/services/searxng_native.py -> ../../
_REPO_ROOT = Path(__file__).resolve().parents[2]

_lock = threading.Lock()
_app = None            # the built WSGI app, once
_build_failed = False  # a failed build is remembered; retrying it per request is a 200-engine import

# Set by app/main.py when the mount is registered — i.e. THIS process is the one that serves
# /searxng. Only the app process imports main, so nothing else can set it by accident.
MOUNTED = False


def mark_mounted() -> None:
    """Called by app/main.py once the /searxng mount is registered."""
    global MOUNTED
    MOUNTED = True


def settings_path() -> Path:
    """This node's settings.yml — generated once by the installer, then the operator's file."""
    env = (os.environ.get("SEARXNG_SETTINGS_PATH") or "").strip()
    if env:
        return Path(env)
    return _REPO_ROOT / "searxng" / "settings.yml"


def available() -> bool:
    """Can this process serve SearXNG itself?

    BOTH halves are required, and the second is not a formality. `use_default_settings` ships the
    JSON API **off**, and with it off every search here is a 403 with an HTML body that each caller
    reads as "no results" rather than as a misconfiguration — so a node with the package installed
    and no settings file is not a working instance, and must not be advertised as one. The file is
    what carries `search.formats: [html, json]`, the disabled limiter and the outgoing-proxy block.
    """
    if find_spec("searx") is None:
        return False
    return settings_path().is_file()


def mount_url() -> str:
    """Where this app serves it — the URL every consumer uses, including out-of-process ones."""
    port = (os.environ.get("POSTERCHANAI_PORT") or "3051").strip() or "3051"
    return f"http://127.0.0.1:{port}{MOUNT_PATH}"


def request_is_local(scope) -> bool:
    """May this ASGI request reach the mount?

    The container this replaces was bound to 127.0.0.1 and reachable only by this node. A mount on
    the app's own port inherits the app's PUBLIC TLS instead, and this instance has its limiter
    disabled on purpose (the limiter is what makes public instances 429 a server, which is why a node
    runs its own) — so unguarded it would be an open metasearch proxy making outbound requests on
    demand with this node's IP on them.

    THE HEADER IS THE REAL GATE; THE ADDRESS IS ONLY A FLOOR. Behind nginx the peer is a local
    address for internet traffic too, so no address test can separate the two — a forwarded-for
    header is what does, and every reverse proxy in front of this app sets one.

    AND "LOOPBACK" IS NOT OBSERVABLE ON EVERY NODE. Measured on server1: `curl http://127.0.0.1:3051`
    reaches the app with a peer address of **192.168.0.2**, the box's own LAN IP. That is the same
    trap the live-stream clamp already hit and documented ("never authorize the clamp's publish by IP
    — MediaMTX reports a LAN address for a connection made to a 127.0.0.1-bound listener"), and
    written as a loopback-only test this refused every request on that node — the fallback silently
    unreachable on the node most likely to need it. So the floor is loopback OR private, which is
    what "this machine or this LAN" actually looks like from inside the app.

    That floor is not doing nothing: it still refuses a public peer, which is the shape of a request
    that reached :3051 directly from the internet (the app binds 0.0.0.0). What it no longer claims
    is the ability to tell a loopback client from a LAN one, because on this deployment it cannot.
    """
    peer = (scope.get("client") or ("", 0))[0]
    if not _is_private_peer(peer):
        return False
    for name, _value in scope.get("headers") or []:
        if name.lower() in (b"x-forwarded-for", b"x-real-ip", b"x-forwarded-host", b"forwarded"):
            return False
    return True


def _is_private_peer(peer: str) -> bool:
    """Is this address this machine or this LAN? An unparseable or empty address is never private."""
    peer = (peer or "").strip()
    if not peer:
        return False
    if peer == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _import():
    """Import searx.webapp with this node's settings, without letting it take over our logging.

    `searx/__init__.py` calls logging.basicConfig + logging.root.setLevel(WARNING) at import time.
    basicConfig is a no-op once handlers exist, but setLevel is not, and neither is the handler
    basicConfig installs on a process that has none yet — so both the level and the handler list are
    captured and put back. Nothing else about searx's own loggers is touched; they keep the level it
    chose for them.
    """
    os.environ.setdefault("SEARXNG_SETTINGS_PATH", str(settings_path()))
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    tmp = tempfile.tempdir
    try:
        tempfile.tempdir = _cache_dir() or tmp
        import searx.webapp as webapp  # noqa: PLC0415 — deliberately lazy; see the module docstring
        return webapp.app
    finally:
        tempfile.tempdir = tmp
        root.setLevel(level)
        root.handlers[:] = handlers


def _cache_dir():
    """Where SearXNG's SQLite caches go — beside its settings, never in /tmp. None if unusable.

    SearXNG has no cache_dir setting: `ExpireCacheCfg` and the favicon cache take
    `tempfile.gettempdir()` as their default db path, and they do it AT IMPORT TIME, which is why
    this is set around the import and put straight back (the rest of the app keeps the real /tmp).

    TWO measured reasons, either one sufficient:

      * /tmp CARRIES THE RETIRED CONTAINER'S FILES. It ran --network host with no private tmp, so it
        wrote /tmp/sxng_cache_*.db as uid 977 (`searxng`). The app runs as the service user, so after
        the cutover the very first search in-process died on `attempt to write a readonly database` —
        a SearXNG that imports, reports itself available, and cannot answer. Found on nas within
        minutes of the deploy, and it would have outlived the container by however long /tmp does.
      * /tmp IS A tmpfs ON THIS DEPLOYMENT (server1, and no swap), so these files are RAM that
        free/MemAvailable do not report as reclaimable. The unit already avoids that with TMPDIR;
        this is the same fix for the in-process copy, and it also means the 7-day engine cache
        survives a restart instead of being rebuilt by the next search.
    """
    try:
        d = settings_path().parent / "cache"
        d.mkdir(parents=True, exist_ok=True)
        if not os.access(d, os.W_OK):
            return None
        return str(d)
    except Exception:                       # unwritable config dir: /tmp beats not starting
        return None


def wsgi_app():
    """The SearXNG WSGI app, built once on first use. None when it can't be built."""
    global _app, _build_failed
    if _app is not None or _build_failed:
        return _app
    with _lock:
        if _app is not None or _build_failed:
            return _app
        try:
            _app = _import()
            logger.info("[searxng] mounted natively at %s (settings: %s)", MOUNT_PATH, settings_path())
        except Exception as exc:                       # a broken SearXNG must not break the app
            _build_failed = True
            logger.warning("[searxng] not available in-process: %s", exc)
    return _app


def serve(host: str = "127.0.0.1", port: int | None = None) -> int:
    """Run SearXNG on its own, out of this venv — what `posterchanai-searxng.service` executes.

    uvicorn + a2wsgi rather than granian or SearXNG's own `searxng-run`: uvicorn is already a
    dependency of this app, `searxng-run` is werkzeug's development server, and granian is a third
    server to install and a fourth place the bind address can be set. That last one is not
    hypothetical — the container this replaces read its bind address from GRANIAN_HOST while
    ignoring both SEARXNG_BIND_ADDRESS and `server.bind_address`, and the first version of it
    listened on every interface of the box with the limiter off.

    LOOPBACK by default and by design: the only clients are this node's app, its worker and its
    bots. There is no gate here of the kind the in-app mount needs, because there is no reverse
    proxy in front of this — binding it anywhere else is what would need one.
    """
    import uvicorn
    from a2wsgi import WSGIMiddleware

    wsgi = wsgi_app()
    if wsgi is None:
        print(f"SearXNG is not installed in this venv, or {settings_path()} is missing.")
        print("Run: ./install.sh --searxng")
        return 1
    port = port or int((os.environ.get("POSTERCHANAI_SEARXNG_PORT") or "8899").strip() or "8899")
    logging.getLogger().setLevel(logging.INFO)
    logger.info("[searxng] serving on http://%s:%s (settings: %s)", host, port, settings_path())
    uvicorn.run(WSGIMiddleware(wsgi), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":                                   # python -m app.services.searxng_native
    import sys
    sys.exit(serve(os.environ.get("POSTERCHANAI_SEARXNG_HOST", "127.0.0.1")))


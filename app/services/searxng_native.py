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

import logging
import os
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

    BOTH conditions, and the second is the one that is easy to miss: behind nginx the peer IS
    127.0.0.1, so a loopback check alone admits the entire internet on every node with a reverse
    proxy in front of it — which is every node that serves the client over TLS. A forwarded-for
    header is what separates this app's own subprocesses from traffic that came in off the wire.
    """
    peer = (scope.get("client") or ("", 0))[0]
    if peer not in ("127.0.0.1", "::1", "localhost"):
        return False
    for name, _value in scope.get("headers") or []:
        if name.lower() in (b"x-forwarded-for", b"x-real-ip", b"x-forwarded-host", b"forwarded"):
            return False
    return True


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
    try:
        import searx.webapp as webapp  # noqa: PLC0415 — deliberately lazy; see the module docstring
        return webapp.app
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers


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


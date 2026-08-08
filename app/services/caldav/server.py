"""Build the Radicale WSGI application this app mounts at /caldav."""
import logging
import os

logger = logging.getLogger(__name__)

_app = None


def working_dir() -> str:
    """Radicale's own storage directory — a CACHE of what the relay holds (see storage.py)."""
    base = os.environ.get("POSTERCHANAI_CALDAV_DIR")
    if not base:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        base = os.path.join(root, "caldav-data")
    os.makedirs(base, exist_ok=True)
    return base


def build_app():
    """The WSGI app, or None when the dependency is missing.

    Built ONCE per process and cached: Radicale reads its configuration at construction, and the
    hydrate-once bookkeeping in the storage plugin is per process too.
    """
    global _app
    if _app is not None:
        return _app
    try:
        from radicale import Application, config as radicale_config
    except Exception as e:            # not installed (a lean/older deployment) — the mount is skipped
        logger.info("[caldav] radicale not available (%s); calendar server disabled", e)
        return None
    cfg = radicale_config.load()
    cfg.update({
        # NOTHING about [server] is set: those options configure Radicale's OWN listener, which never
        # runs here — we are mounted as WSGI. Setting hosts:"" made Radicale refuse to build at all
        # ("malformed IP address"), which took the whole app down with it.
        "auth": {"type": "app.services.caldav.auth"},
        "storage": {"type": "app.services.caldav.storage",
                    "filesystem_folder": working_dir()},
        # Every account may reach only its own collections. Radicale's own rule, named here so the
        # answer to "who can read whose calendar" is in this file rather than a default.
        "rights": {"type": "owner_only"},
        "web": {"type": "none"},                       # no Radicale web UI: the app has its own
        "logging": {"level": "warning"},
    }, "posterchanai", privileged=True)
    # Radicale logs ~60 INFO lines about its own configuration on every construction — in this app
    # that is journalctl noise on every restart, and it drowns the app's own startup. Its `logging`
    # section is read before ours applies, so the level is set on the logger itself.
    logging.getLogger("radicale").setLevel(logging.WARNING)
    _app = Application(cfg)
    logger.info("[caldav] Radicale mounted (storage: encrypted Nostr events; cache: %s)", working_dir())
    return _app

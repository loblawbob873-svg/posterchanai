"""A FRESH PROCESS that starts the way the app does and reports what Admin and the consumers see.

Used by tests/test_admin_settings_persist_today.py (and scripts/check_admin_settings_persist.py): nothing in this
process's memory was ever written by a Save -- the settings cache starts from init_db()'s defaults and is filled by
settings_store.hydrate_from_db() reading the operator's `pcai:setting:` documents off a relay over its socket, i.e.
exactly what a restarted node does. Then it answers, as JSON on stdout:

  get     -- GET /api/admin/settings through the real admin router (the admin page hydrates from this)
  pool    -- len(tor_service.pool_plan()) on a 2-CPU box, reading the hydrated settings (tor_extra_daemons)
  relay   -- the relay's config from thread._read_config(), which does its OWN hydrate from the relay
  status  -- the GET's HTTP status (a stored value the schema cannot parse used to make it a 500)

argv: <relay port> <operator secret key hex>
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# Never read this machine's real per-node settings file, and never dial its real relay.
os.environ["POSTERCHANAI_LOCAL_SETTINGS"] = os.path.join(tempfile.mkdtemp(prefix="pc-fresh-"), "local.json")
os.environ.pop("POSTERCHANDB_MODE", None)


def main():
    port, sk_hex = int(sys.argv[1]), sys.argv[2]
    op_sk = bytes.fromhex(sk_hex)
    from unittest import mock
    from app.services import settings_store as S

    S._operator_seckey = lambda db: op_sk
    S._port = lambda db=None: port
    S._schedule_relay_write = lambda changes: (_ for _ in ()).throw(AssertionError("a fresh read wrote " + str(changes)))

    from app.database import init_db
    init_db()                                   # defaults into the cache, as app startup does
    hydrated = S.hydrate_from_db(None)          # then the relay, as app startup does
    out = {"hydrated": hydrated, "is_hydrated": S.is_hydrated()}

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import get_current_user
    from app.database import get_db
    from app.routers import admin as admin_router

    app = FastAPI()
    app.include_router(admin_router.router)
    app.dependency_overrides[get_current_user] = lambda: mock.Mock(is_admin=True)
    app.dependency_overrides[get_db] = lambda: iter([object()])
    r = TestClient(app, raise_server_exceptions=False).get("/api/admin/settings")
    out["status"] = r.status_code
    out["get"] = r.json() if r.status_code == 200 else r.text[-500:]

    from app.services import tor_service
    out["pool"] = len(tor_service.pool_plan(cpus=2))

    from app.services.nostr_relay import thread
    import app.database as database
    with mock.patch.object(database, "SessionLocal", lambda: mock.MagicMock()), \
            mock.patch.object(thread, "_collect_operator_pubkeys", lambda db: []), \
            mock.patch.object(thread, "_collect_preserve_pubkeys", lambda db: []), \
            mock.patch.object(thread, "_node_pubkey", lambda: ""):
        cfg = thread._read_config()
    keep = ("pcdb_mode", "pcdb_flush_s", "pcdb_cache_mb", "pcdb_tuning", "posts_per_min", "same_per_hour",
            "gift_per_recipient", "max_connections", "max_subs_per_conn", "max_filters_per_req", "max_message_size")
    out["relay"] = {k: cfg.get(k) for k in keep}
    print("RESULT " + json.dumps(out))


if __name__ == "__main__":
    main()

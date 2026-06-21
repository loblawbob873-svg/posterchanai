#!/usr/bin/env python3
"""Installer / entrypoint step — provision this node's Nostr relay OPERATOR (instance) key.

The operator key is the instance's signing identity for the Nostr-as-datastore. It auto-mints on
first app start; doing it here too means the admin sees the instance npub up front in the install /
container logs. The relay trusts this key via its **operator set** (built from the keyfile — see
`_collect_operator_pubkeys`), so it can sign the datastore docs out of the box, and the default Web
of Trust seeds are seeded by the app on startup (`app/database.py` default_settings). Idempotent —
safe to re-run.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        from app.database import init_db, SessionLocal
        from app.services.settings_store import ensure_operator_key
        from app.services import keystore
        from app.services.nostr import nostr_service
    except Exception as e:  # pragma: no cover - install-time diagnostics
        print(f"[init-instance-key] import failed: {e}", file=sys.stderr)
        return 1

    # Run the app's full DB init first (idempotent: create_all on Postgres + seed defaults, incl. the
    # 10 default WoT seeds), so the operator key is minted into a ready database.
    try:
        init_db()
    except Exception as e:
        print(f"[init-instance-key] WARNING: init_db failed ({e}); the key still mints lazily at app start",
              file=sys.stderr)

    db = SessionLocal()
    try:
        ensure_operator_key(db)                 # mint into the keyfile if missing
        nsec = keystore.get_operator_nsec()
        if not nsec:
            print("[init-instance-key] ERROR: no operator key after ensure_operator_key", file=sys.stderr)
            return 1
        npub = nostr_service.npub_from_seckey(nsec)
        print(f"[init-instance-key] instance npub: {npub}")
        # The relay trusts this key via its operator set (keyfile-derived), and the app seeds the
        # default WoT on startup — nothing else to provision here. Admin is claimed by the FIRST npub
        # to sign in (app/routers/auth.py nostr_login).
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Installer step — provision this node's Nostr relay OPERATOR (instance) key.

The operator key is the instance's signing identity for the Nostr-as-datastore. It auto-mints on
first run, but doing it here at install time means:
  * the admin sees the instance npub up front,
  * the relay's Web of Trust is SEEDED with it, so the relay trusts its own operator (and signups)
    out of the box, and a daily WoT rebuild can never exclude it (seeds are always kept in the
    member set — see WotGate.build), and
  * the operator pubkey is also always trusted via the gate's separate operator set.
Idempotent — safe to re-run.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    try:
        from app.database import SessionLocal, Base, engine
        from app.models import Setting
        from app.services.settings_store import ensure_operator_key
        from app.services import keystore
        from app.services.nostr import nostr_service
    except Exception as e:  # pragma: no cover - install-time diagnostics
        print(f"[init-instance-key] import failed: {e}", file=sys.stderr)
        return 1

    # Run the app's FULL DB init first, not just create_all. create_all only makes empty tables —
    # if we then write nostr_relay_wot_seeds with only the instance npub, the later init_db() at app
    # startup sees the key already exists and SKIPS the 10-npub default seed list, leaving a fresh
    # install with just 2 seeds (instance + claimed admin). init_db() seeds all defaults (including
    # the WoT seeds) first and is idempotent, so we then append the instance npub on TOP of the 10.
    try:
        from app.database import init_db
        init_db()
    except Exception as e:
        print(f"[init-instance-key] WARNING: init_db failed ({e}); falling back to create_all", file=sys.stderr)
        try:
            Base.metadata.create_all(bind=engine)
        except Exception as e2:
            print(f"[init-instance-key] WARNING: could not ensure tables ({e2}); the key still mints", file=sys.stderr)

    db = SessionLocal()
    try:
        ensure_operator_key(db)                 # mint into the keyfile if missing
        nsec = keystore.get_operator_nsec()
        if not nsec:
            print("[init-instance-key] ERROR: no operator key after ensure_operator_key", file=sys.stderr)
            return 1
        npub = nostr_service.npub_from_seckey(nsec)
        op_hex = nostr_service.to_pubkey_hex(npub)

        # Seed the relay WoT with the instance npub so it's a permanent build root.
        try:
            row = db.query(Setting).filter(Setting.key == "nostr_relay_wot_seeds").first()
            cur = (row.value if row and row.value else "")
            seeds = [s.strip() for s in cur.replace(",", "\n").split("\n") if s.strip()]
            if not any(nostr_service.to_pubkey_hex(s) == op_hex for s in seeds):
                seeds.append(npub)
                value = "\n".join(seeds)
                if row:
                    row.value = value
                else:
                    db.add(Setting(key="nostr_relay_wot_seeds", value=value))
                db.commit()
                print("[init-instance-key] seeded the relay Web of Trust with the instance key")
        except Exception as e:
            db.rollback()
            print(f"[init-instance-key] WARNING: could not seed WoT ({e}); operator is still trusted via its key",
                  file=sys.stderr)

        print(f"[init-instance-key] instance npub: {npub}")
        # Admin is claimed by the FIRST npub to sign in (app/routers/auth.py nostr_login) — turnkey,
        # and it's the owner's own key. Nothing to provision here.
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

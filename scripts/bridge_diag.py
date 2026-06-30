"""Read-only Nostr↔Fediverse bridge diagnostic.

For each Nostr event (hex id, note1…, or nevent1…) print what the LOCAL relay holds and how the
bridge mapped it: kind/author, the threading/quote/emoji tags, whether the author is a bridge puppet
or a local user (with their crosspost/bridge toggles), and any FediBridgeDelivered / FediBridgeMap
row. For e/q references it also reports whether the target was bridge-mirrored — so you can see why a
quote didn't embed, a reply didn't thread, or a note never cross-posted to the fediverse.

Run on a node from the repo root with the app venv, e.g.:
    ./venv-unified/bin/python scripts/bridge_diag.py nevent1… note1… <hex>
With no args it checks a small built-in set of example ids.

NOTHING is written. Safe to run on production.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.services import settings_store
from app.services.fedi_bridge_identity import query_one
from app.models import FediBridgeDelivered, FediBridgeMap, FediPuppet, User
from app.services.nostr import nostr_service, bech32

# Built-in examples (used when no ids are passed on the command line).
EXAMPLES = {
    "missed_notif":    "24c9e3e1b98ebd630fb14a68d003eaf2d9f6ad6f8637c5089b8887a8c9f9a1bf",
    "no_reply_notif":  "515e253b7dc724a0d04c8ff58f155e2a44cf765c5480054c78f283d2c9ec8343",
    "not_on_fedi":     "5d0635b921005cfe688ad5664b70885d02da71d58040d56d26203919fa25c5d1",
}


def _to_hex(s: str) -> str | None:
    """Accept a 64-char hex id, or a note1/nevent1 bech32 (decode to the event id)."""
    s = s.strip()
    if len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s):
        return s.lower()
    try:
        raw = bech32.decode_any(s)
        return raw.hex() if raw and len(raw) == 32 else None
    except Exception:
        return None


def _port() -> int:
    try:
        return int(settings_store.get("nostr_relay_port", 3052) or 3052)
    except Exception:
        return 3052


def _tagsof(ev, *names):
    return [t for t in ev.get("tags", []) if t and t[0] in names]


async def _report(db, label: str, hid: str, port: int) -> None:
    print("=" * 78)
    print(f"{label}  {hid}")
    ok, ev = await query_one(port, {"ids": [hid]})
    if not ok:
        print("  relay query FAILED (relay unreachable?)")
        return
    if not ev:
        print("  NOT on relay (never stored / pruned)")
    else:
        print(f"  kind={ev.get('kind')} pubkey={ev.get('pubkey', '')[:16]}… created_at={ev.get('created_at')}")
        print(f"  content: {(ev.get('content') or '').replace(chr(10), ' ')[:180]}")
        for nm in ("e", "p", "q", "emoji", "proxy", "fedibridge", "nofederate"):
            ts = _tagsof(ev, nm)
            if ts:
                print(f"  {nm}-tags: {json.dumps(ts)[:240]}")
        pk = ev.get("pubkey")
        pup = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == pk).first()
        usr = next((u for u in db.query(User).filter(User.nostr_npub.isnot(None)).all()
                    if nostr_service.to_pubkey_hex(u.nostr_npub) == pk), None)
        if pup:
            who = f"PUPPET {pup.acct or '?'}"
        elif usr:
            who = (f"LOCAL-USER {usr.username} (pleroma={bool(usr.pleroma_enabled)}, "
                   f"crosspost={bool(getattr(usr, 'fedi_crosspost_enabled', False))}, "
                   f"bridge={bool(getattr(usr, 'fedi_bridge_enabled', False))})")
        else:
            who = "(neither puppet nor local user)"
        print(f"  author: {who}")
    d = db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == hid).first()
    print(f"  FediBridgeDelivered: {('note_id=%s uri=%s acct=%s' % (d.note_id, d.note_uri, d.author_acct)) if d else 'NONE (not bridge-mirrored or cross-posted)'}")
    m = db.query(FediBridgeMap).filter(FediBridgeMap.nostr_event_id == hid).first()
    print(f"  FediBridgeMap: {('kind=%s target=%s vis=%s' % (m.kind, m.target_id, m.visibility)) if m else 'NONE'}")
    if ev:
        for t in _tagsof(ev, "e", "q"):
            tgt = t[1] if len(t) >= 2 else None
            if not tgt:
                continue
            td = db.query(FediBridgeDelivered).filter(FediBridgeDelivered.nostr_event_id == tgt).first()
            print(f"    ref {t[0]} {tgt[:16]}… -> {'mirrored ' + str(td.note_uri) if td else 'NOT in delivered-map'}")


async def main() -> None:
    args = sys.argv[1:]
    if args:
        targets = {}
        for a in args:
            h = _to_hex(a)
            if h:
                targets[a[:18]] = h
            else:
                print(f"!! could not parse event id: {a}")
    else:
        targets = EXAMPLES
    port = _port()
    db = SessionLocal()
    try:
        for label, hid in targets.items():
            await _report(db, label, hid, port)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

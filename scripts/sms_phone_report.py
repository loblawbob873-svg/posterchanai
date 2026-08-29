#!/usr/bin/env python3
"""What the handsets on this node say about their own SMS/MMS backup.

    venv-unified/bin/python scripts/sms_phone_report.py [user-id ...]

WHY THIS EXISTS. The handset is the only device that knows why a picture is not in the archive, and
until now it had no way to say so: its provider counts, refusals, ceiling, last upload error and
migration latches lived in memory, in localStorage, and in one transient sentence under a search
box. Diagnosing "no media on the old messages" cost a week of asking somebody to read that sentence
out loud. `publishStatus` in sms.js files a COUNTS-ONLY report per sweep — no address, no body, no
filename, no hash — and this reads it.

The one number that matters is `mmsRows` against `mmsRowsWithParts`:

    equal          the provider IS handing over attachments; look at partsUploaded/partError
    withParts ~ 0  the phone never sees an attachment to upload, and no upload fix can help

Requires the account's key to be server-held (custodial). Prints counts only.
"""
import asyncio, json, sys, time
sys.path.insert(0, "/home/verita84/posterchanai")

from app.database import SessionLocal
from app.models import User
from app.services.nostr import nip44
from app.services.nostr.bip340 import pubkey_from_seckey
from app.services.nostr.nostr_service import decode_seckey
from sqlalchemy import text as sqltext

D = "pcai:sms-status:"


def ago(ts):
    d = max(0, int(time.time()) - int(ts or 0))
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    return f"{d // 3600}h ago"


def verdict(r):
    rows, with_parts = int(r.get("mmsRows") or 0), int(r.get("mmsRowsWithParts") or 0)
    if not rows:
        return "no picture messages in this sweep's window"
    if with_parts == 0:
        return ("THE PROVIDER HANDED OVER NO ATTACHMENTS — the phone has nothing to upload, so no "
                "upload fix can help. Look at the provider read, not at Blossom.")
    if int(r.get("partsFailed") or 0):
        return "attachments WERE offered and the upload failed: " + (r.get("partError") or "?")
    if int(r.get("partsUploaded") or 0):
        return "attachments offered and uploaded — this sweep was healthy"
    return "attachments offered, none uploaded and none failed — nothing was attempted"


def main():
    db = SessionLocal()
    ids = [int(a) for a in sys.argv[1:]] or None
    users = db.query(User).filter(User.nostr_nsec.isnot(None)).all()
    if ids:
        users = [u for u in users if u.id in ids]
    found = 0
    for u in users:
        try:
            sk = decode_seckey(u.nostr_nsec)
        except Exception:
            continue
        pk = pubkey_from_seckey(sk).hex()
        rows = db.execute(sqltext(
            "SELECT t.value, e.content, e.created_at FROM events e "
            "JOIN event_tags t ON t.event_id=e.id AND t.tag='d' "
            "WHERE e.kind=30078 AND e.pubkey=:pk AND t.value LIKE :d "
            "ORDER BY e.created_at DESC"), {"pk": pk, "d": D + "%"}).fetchall()
        for d, content, at in rows:
            try:
                r = json.loads(nip44.decrypt_self(sk, content))
            except Exception as e:
                print(f"user {u.id} {d[-8:]}: unreadable ({type(e).__name__})")
                continue
            found += 1
            print(f"\n=== user {u.id} {u.username} · device {d[-8:]} · {ago(at)} ===")
            print(f"  provider gave : {r.get('rowsRead')} rows, {r.get('mmsRows')} picture "
                  f"messages, {r.get('mmsRowsWithParts')} of them WITH attachments "
                  f"({r.get('partsSeen')} parts)")
            print(f"  this sweep    : published {r.get('published')}, uploaded "
                  f"{r.get('partsUploaded')}, failed {r.get('partsFailed')}")
            if r.get("partError"):
                print(f"  upload error  : {r['partError']}")
            if r.get("archiveError"):
                print(f"  archive error : {r['archiveError']}")
            flags = [k for k in ("refused", "mmsRefused", "mmsCapped") if r.get(k)]
            print(f"  provider flags: {', '.join(flags) if flags else 'none'}"
                  f" · audited={bool(r.get('mmsAudited'))}")
            m = r.get("markers") or {}
            print(f"  latches       : blossom-done={m.get('blossom')} rewound={m.get('rewound')} "
                  f"oldest-first={m.get('oldestFirst')} hwm={m.get('hwm')}")
            print(f"  builds        : apk={r.get('app') or '?'} client={r.get('client') or '?'}"
                  f"  (a stale client reports the OLD behaviour from a phone you just opened)")
            print(f"  holding       : {r.get('held')} messages"
                  + (f" · app {r['app']}" if r.get("app") else ""))
            print(f"  VERDICT       : {verdict(r)}")
    if not found:
        print("No handset has filed a report yet. A phone files one at the end of every mirror "
              "sweep, so open Texts on the handset once after this build reaches it.")
    db.close()


if __name__ == "__main__":
    main()

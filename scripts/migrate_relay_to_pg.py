"""One-time migration: relay event store SQLite (data/nostr_relay.db) → PostgreSQL (posterchan_relay).

Batched + idempotent (ON CONFLICT). Drops the secondary tag index during the bulk load and recreates
it after. Strips NUL bytes (\\x00) which SQLite TEXT permits but PostgreSQL TEXT rejects.
"""
import sqlite3, sys, time
import psycopg2
from psycopg2.extras import execute_values

import os
SQLITE = sys.argv[1] if len(sys.argv) > 1 else "data/nostr_relay.db"
DSN = os.environ.get("NOSTR_RELAY_PG_DSN", "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan")

def clean(v):
    return v.replace("\x00", "") if isinstance(v, str) else v

s = sqlite3.connect(f"file:{SQLITE}?mode=ro", uri=True)
s.row_factory = sqlite3.Row
p = psycopg2.connect(DSN); p.autocommit = False
pc = p.cursor()

t0 = time.time()
# Speed: drop the secondary tag index during load (PK stays for ON CONFLICT), recreate after.
pc.execute("DROP INDEX IF EXISTS idx_event_tags_tv"); p.commit()

def copy(table, cols, conflict, batch=10000, clean_idx=()):
    sel = s.execute(f"SELECT {','.join(cols)} FROM {table}")
    ph = "(" + ",".join(["%s"] * len(cols)) + ")"
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES %s {conflict}"
    n = 0
    while True:
        rows = sel.fetchmany(batch)
        if not rows:
            break
        data = []
        for r in rows:
            t = list(r)
            for i in clean_idx:
                t[i] = clean(t[i])
            data.append(tuple(t))
        execute_values(pc, sql, data, template=ph, page_size=batch)
        p.commit()
        n += len(rows)
        print(f"  {table}: {n}", flush=True)
    return n

print("events..."); copy("events",
    ["id","pubkey","created_at","kind","content","tags","sig","raw","origin","expiration"],
    "ON CONFLICT (id) DO NOTHING", batch=5000, clean_idx=(4,5,7))  # content, tags, raw
print("event_tags..."); copy("event_tags",
    ["event_id","tag","value"], "ON CONFLICT DO NOTHING", batch=20000, clean_idx=(2,))
print("wot..."); copy("wot", ["pubkey","depth","added_at"], "ON CONFLICT (pubkey) DO NOTHING")
print("relay_kv..."); copy("relay_kv", ["key","value"],
    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", clean_idx=(1,))

print("recreating idx_event_tags_tv...", flush=True)
pc.execute("CREATE INDEX IF NOT EXISTS idx_event_tags_tv ON event_tags(tag, value)"); p.commit()
pc.execute("ANALYZE"); p.commit()

for t in ("events","event_tags","wot","relay_kv"):
    pc.execute(f"SELECT count(*) FROM {t}"); print("PG", t, pc.fetchone()[0])
print(f"done in {time.time()-t0:.0f}s")
s.close(); p.close()

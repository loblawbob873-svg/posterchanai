"""One-time migration: app DB SQLite (posterchanai.db) → PostgreSQL (posterchan_app).

Builds the schema with create_all on PG, copies every table in FK-safe order (parents first),
then resets the SERIAL sequences to MAX(id). Idempotent-ish: run against a FRESH posterchan_app.
"""
import sys
from sqlalchemy import create_engine, text

SRC = "sqlite:///./posterchanai.db"
# Same Postgres DB as the relay — ONE database holds both the relay's event tables and the app's
# operational/cache tables (no SQLite, no separate app database).
DST = "postgresql+psycopg2://posterchan:posterchan_local@127.0.0.1:5432/posterchan_relay"

import app.models  # noqa: F401 — register all tables on Base.metadata
from app.database import Base

src = create_engine(SRC)
dst = create_engine(DST)

# 1) schema — clean slate first (drops ONLY the app's Base tables; the relay's events/event_tags/
# wot/relay_kv are raw-SQL tables not in Base.metadata, so they are untouched).
Base.metadata.drop_all(dst)
Base.metadata.create_all(dst)
print("schema created on PG")

# 2) copy data, parents before children (FK order)
with src.connect() as s:
    for table in Base.metadata.sorted_tables:
        try:
            rows = [dict(r) for r in s.execute(table.select()).mappings()]
        except Exception as e:
            print(f"  {table.name}: SKIP read ({e})"); continue
        if not rows:
            print(f"  {table.name}: 0"); continue
        with dst.begin() as d:
            for i in range(0, len(rows), 1000):
                d.execute(table.insert(), rows[i:i + 1000])
        print(f"  {table.name}: {len(rows)}")

# 3) reset SERIAL sequences to MAX(id) so future inserts don't collide with migrated ids
with dst.begin() as d:
    for table in Base.metadata.sorted_tables:
        for col in table.primary_key.columns:
            if str(col.type).upper().startswith(("INTEGER", "BIGINT")):
                d.execute(text(
                    "SELECT setval(pg_get_serial_sequence(:t, :c), "
                    "COALESCE((SELECT MAX(\"" + col.name + "\") FROM \"" + table.name + "\"), 1))"
                ), {"t": table.name, "c": col.name})
print("sequences reset")

# 4) verify counts
with dst.connect() as d:
    for table in Base.metadata.sorted_tables:
        n = d.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar()
        print(f"PG {table.name}: {n}")

"""The ONE way a bot connects to the Pleroma/Akkoma database.

Every bot that reads it (welcomebot, blockbot, reportbot, engagement) only READS, and each used to
copy the same `psycopg2.connect(...)` without autocommit. psycopg2 opens a transaction implicitly on
the first execute() and keeps it open until commit(), which none of them ever called — so each bot
process sat "idle in transaction" for its whole life. Measured on nas.lan (detroitriotcity,
2026-09-26): welcomebot's connection held an AccessShareLock on `users` for as long as it ran, so a
DROP TRIGGER / pg_repack on that table waited behind it indefinitely, and the open snapshot pinned
the xmin horizon, so VACUUM could not reclaim dead rows anywhere in the database. A failed query also
left the connection in an aborted transaction, failing every later poll until the process restarted.

Autocommit ends every statement's transaction with the statement. Nothing here writes, so there is
no multi-statement unit of work to protect.
"""
import psycopg2


def connect(dbname, user, password, host=None, **extra):
    kw = dict(dbname=dbname, user=user, password=password, connect_timeout=10)
    if host:
        kw["host"] = host
    kw.update(extra)
    conn = psycopg2.connect(**kw)
    conn.autocommit = True
    return conn

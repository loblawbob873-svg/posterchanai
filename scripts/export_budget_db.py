#!/usr/bin/env python3
"""Export one user's data out of the old Budget Manager SQLite DB as JSON, for import into the
client's encrypted Budget doc (Discover → Budget → Import).

This is a MIGRATION AID, not a server-side importer, and it cannot be anything else: the budget doc
is NIP-44-encrypted to the user's own Nostr key, which lives in their browser/signer and never on the
server. So the only place the ciphertext can be produced is the client — this script just hands it
clean JSON to paste in.

    python3 scripts/export_budget_db.py ~/finance/budget.db --user verita84@poster.place > budget.json

Read-only: it opens the DB in immutable mode and never writes.
"""
import argparse
import json
import sqlite3
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="path to budget.db")
    ap.add_argument("--user", help="username (email) to export")
    ap.add_argument("--user-id", type=int, help="numeric user id, if you know it")
    ap.add_argument("--list", action="store_true", help="list users and exit")
    args = ap.parse_args()

    # immutable=1: never take a write lock or create a -wal beside the live app's database.
    con = sqlite3.connect(f"file:{args.db}?immutable=1", uri=True)
    con.row_factory = sqlite3.Row

    if args.list:
        for r in con.execute("SELECT id, username, is_admin FROM users ORDER BY id"):
            n = con.execute("SELECT COUNT(*) FROM bills WHERE user_id=?", (r["id"],)).fetchone()[0]
            print(f'{r["id"]}\t{r["username"]}\t{n} bills')
        return 0

    uid = args.user_id
    if uid is None:
        if not args.user:
            print("give --user <username> or --user-id <n> (or --list)", file=sys.stderr)
            return 2
        row = con.execute("SELECT id FROM users WHERE username=?", (args.user,)).fetchone()
        if not row:
            print(f"no such user: {args.user}", file=sys.stderr)
            return 1
        uid = row["id"]

    bills = [{
        "name": b["name"],
        "cost": b["cost"],
        "paid": b["paid"],
        "payment_method": b["payment_method"] or "",
        # sqlite stores these as 0/1 ints; the client's schema uses real booleans
        "is_income": bool(b["is_income"]),
        "is_recurring": bool(b["is_recurring"]),
        "hidden_month": b["hidden_month"] or "",
    } for b in con.execute(
        "SELECT * FROM bills WHERE user_id=? ORDER BY is_income DESC, sort_order", (uid,))]

    # Category ids are kept ONLY so items can point at their category; the client re-keys both on
    # import (SQLite AUTOINCREMENT ids would collide with ids already in the doc).
    cats = [{
        "id": c["id"],
        "name": c["name"],
        "paid": c["paid"],
        "hidden_month": c["hidden_month"] or "",
    } for c in con.execute(
        "SELECT * FROM plan_categories WHERE user_id=? ORDER BY sort_order", (uid,))]

    items = [{
        "cat": i["category_id"],
        "name": i["name"],
        "amount": i["amount"],
    } for i in con.execute(
        "SELECT * FROM plan_items WHERE user_id=? ORDER BY sort_order", (uid,))]

    json.dump({"bills": bills, "cats": cats, "items": items}, sys.stdout, indent=1)
    print()
    print(f"# {len(bills)} bills, {len(cats)} plans, {len(items)} plan items", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

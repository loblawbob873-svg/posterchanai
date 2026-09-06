# Repeated-zap output maintenance

A reported account had one locked unspent output and nineteen spent outputs.
Maintenance queried `incoming_transfers(transfer_type="unavailable")` and treated
those spent outputs as an earlier split still unlocking. Once an account had spent
an output, that guard could prevent replenishment indefinitely.

The wallet RPC contract defines available as unspent and unavailable as spent:
https://docs.getmonero.org/rpc-library/wallet-rpc/#incoming_transfers

Maintenance now inspects unspent outputs' explicit unlocked flag. Frozen or spent
outputs do not count as capacity. Locked unspent outputs prevent repeated splits;
pending outgoing transactions provide the same protection before newly created
outputs appear in the confirmed wallet history. Unknown unlock state cannot
authorize a sweep. A split still targets only one key image, preserving reserves.

The deployment map also omitted the maintenance worker for Monero module changes.
Both the API and worker now restart; relays and streaming services are unaffected.

Verification: 384 Monero and deployment tests passed, including five new/updated
cases that failed before the fix. The deployment mapping test also failed before
its correction. An RPC-state simulation models pending splits, locked outputs,
worker restarts and eight consecutive zaps after replenishment, without additional
maintenance consuming the remaining reserves. No test spends live funds.

An existing locked output must still satisfy Monero's chain unlock rule. Splitting
it creates further outputs that also require confirmation before use. This fix
restores independent output capacity; it does not bypass consensus locks or promise
unlimited immediate zaps.


## Spending-cap outage: test ledger contamination

The live operator ledger held exactly five entries for test ADMIN user ID 3,
0.1 XMR each, at 2026-09-06 17:05:09.861241–17:05:09.978997 UTC. These match
the HTTP daily-cap test's five mocked confirmations. They predate the settings
isolation fixture added at 17:49 UTC. Live RPC history contained no outgoing,
pending, or failed payments in the corresponding day window. The cap was
exhausted by test records, not user payments.

Backed up the SQLite database and removed only these five entries, with exact
user/amount/count and microsecond timestamp checks inside a transaction. The
rolling total is now zero; production configuration remains 0.1 XMR per transfer
and 0.5 XMR daily. A live-config 0.0001 XMR preparation now succeeds. No payment
was confirmed or sent by this verification. The first strict timestamp check
refused the repair due to floating-point precision; it changed no records.

Added an autouse test boundary that refuses any TransferGate ledger outside
that test's temporary directory, even if settings hydration selects a host
path. A regression test reproduced the unsafe file creation before the guard;
it passes afterward. The existing settings isolation remains in place.
160 focused wallet/API tests and all 386 Monero/deployment tests passed. This is separate from pooled-user output
maintenance, which does not use the operator spending cap.

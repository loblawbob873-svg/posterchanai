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

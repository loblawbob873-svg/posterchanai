# Native SOL and XRP send follow-up

This isolated follow-up adds native SOL and XRP sends to the wallet release. BTC,
LTC, DOGE and BCH remain receive/balance-discovery assets pending separate reviewed
UTXO transaction support. It does not change the built-in Monero wallet or wallet CSS.

SOL uses solders 0.29.0 to build and sign a versioned system transfer. Before
broadcast it verifies mainnet genesis, recipient, balance and fee, then simulates
the signed transaction. A random request-token memo makes separate intentional
payments distinct when the RPC supplies the same recent blockhash.

XRP uses xrpl-py 5.1.0 and the selected Exodus derivation key. It verifies mainnet,
a recent validated ledger, source account, account/owner reserves, fee ceiling,
recipient account requirements, and destination tag. Classic and mainnet X-addresses
are supported; conflicting embedded tags and testnet addresses are rejected.
Every payment signs a LastLedgerSequence four ledgers beyond the validated head.

Both locally signed identities are durably encrypted before the first broadcast.
An acknowledged submission remains pending. Same-request retries reuse its result;
new requests cannot spend while confirmation is unresolved. SOL/XRP addresses keep
their case in lock scopes and request fingerprints. XRP tags are part of the retry
identity. Existing EVM lock identities and persisted fingerprints remain compatible.
Locks cover duplicate imports within the same authenticated user on one node;
cross-node and cross-user ownership coordination is not provided.

Status accepts SOL confirmed/finalized results and XRP validated results, including
explicit network failures. The UI distinguishes Submitted, Sent, and failed; a
failed on-chain transaction may still have charged a fee. XRP expiration releases
the lock only when the validated head is past the signed expiry and the RPC proves
it searched every ledger in that exact window. Missing/pruned history never proves
failure. SOL missing status currently remains locked for manual investigation even
after blockhash expiry; automatic safe expiry recovery is still a limitation.

SDK dependencies must be installed from requirements.txt (or requirements-nostr.txt)
before enabling this release on every serving node. Validation used production
Python 3.11 with the additional SDKs isolated under /tmp, without modifying the live
environment. No live wallet-expansion funds were sent.

Validation: real signed transactions are decoded and signatures verified; tests
assert exact recipient, amount, fee, sequence, validity window and destination tag.
Negative cases cover wrong networks, stale ledger, invalid fee/balance responses,
insufficient reserves, required tags, failed simulations, lost acknowledgements,
duplicate request concurrency, incomplete history and expired XRP recovery. The
full scoped wallet suite passed 312 cases before the final extra expiry-store case;
the 32 browser cases passed, including submission wording and destination-tag input.
See /tmp/pc-wallet-sol-xrp-core.log and /tmp/pc-wallet-sol-xrp-browser.log.

Primary contracts:
- https://kevinheavey.github.io/solders/tutorials/transactions.html
- https://solana.com/docs/rpc/http/getsignaturestatuses
- https://solana.com/docs/rpc/http/simulatetransaction
- https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission
- https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/tx
- https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/server-info-methods/server_info

# Native SOL and XRP send follow-up

This isolated follow-up adds native SOL and XRP sends to the wallet release. The
subsequent BTC/LTC/DOGE/BCH implementation is reviewed in
EXODUS-UTXO-SEND-20260906.md. Neither changes the built-in Monero wallet or wallet CSS.

SOL uses solders 0.29.0 to build and sign a versioned system transfer. Before
broadcast it verifies mainnet genesis, recipient, balance and fee, then simulates
the signed transaction. A random request-token memo makes separate intentional
payments distinct when the RPC supplies the same recent blockhash.

XRP uses xrpl-py 5.1.0 in a separate offline SDK environment and the selected Exodus derivation key. It verifies mainnet,
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

Install solders from requirements.txt (or requirements-nostr.txt). XRP must be
installed separately with `bash scripts/install/exodus_xrp_sdk.sh`; its default
venv is `/usr/local/libexec/pc-exodus/xrp-venv`. Set `EXODUS_XRP_PYTHON` if using a
different interpreter path. Provision writable `EXODUS_XRP_SLOT_DIR` (default
`data/exodus-xrp-slots`) on each node. The installer restricts its destination to a
dedicated XRP directory and runs pip check inside that environment. It never
installs XRPL into the application venv: XRPL requires websockets<16, whereas the
signer/relay environment uses websockets16. The subprocess helper accepts only
recipient validation and local signing, retains RPC in the backend, limits input
and output to 4096 bytes and runtime to 10 seconds, and shares two file-locked slots
across workers. Private keys travel only over stdin. Python isolated mode and a
minimal environment exclude inherited Python settings and proxy credentials;
stderr is discarded and validation errors never include request values.

Validation uses production Python 3.11 with its existing dependency graph and an
actual dedicated XRPL venv with the SDK's resolved dependencies. Test reference
decoding runs in that same dedicated venv, so no XRPL imports reach the application
interpreter. Set `EXODUS_XRP_PYTHON` when running SDK integration tests; missing SDK
is reported as an explicit test skip. No live wallet-expansion funds were sent.

Validation: real signed transactions are decoded and signatures verified; tests
assert exact recipient, amount, fee, sequence, validity window and destination tag.
Negative cases cover wrong networks, stale ledger, invalid fee/balance responses,
insufficient reserves, required tags, failed simulations, lost acknowledgements,
duplicate request concurrency, incomplete history and expired XRP recovery. The
full scoped wallet suite passed 322 cases after SDK isolation and expiry recovery;
the 32 browser cases passed, including submission wording and destination-tag input.
See /tmp/pc-wallet-sol-xrp-core.log and /tmp/pc-wallet-sol-xrp-browser.log.

Primary contracts:
- https://kevinheavey.github.io/solders/tutorials/transactions.html
- https://solana.com/docs/rpc/http/getsignaturestatuses
- https://solana.com/docs/rpc/http/simulatetransaction
- https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission
- https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/transaction-methods/tx
- https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/server-info-methods/server_info

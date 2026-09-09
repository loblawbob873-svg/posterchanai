# How Monero works in PosterChan

Engineering guide · 8 September 2026 · source baseline `c43178cf4`

Wallet ownership, authentication, sending, zaps, output replenishment, recovery, and the boundaries of the current implementation.

This describes the checked-in implementation, not an audit of a particular host's balances, secrets, backup jobs, or live configuration. It does not promise unlimited immediate payments or that every external wallet derives identical addresses.

## 1. Three distinct wallet paths

| Path | Funds and keys | Implementation |
| --- | --- | --- |
| Operator / node wallet | An operator-provisioned wallet served by authenticated wallet RPC. The service uses that wallet's spending authority; it does not expose seed creation or seed export through this API. | `monero_wallet_service.py`, `monero_wallet.py`; canonical API `/api/wallet/xmr`. |
| Built-in personal Monero wallet | One pooled wallet with a separate Monero account for each authenticated user's key. The node holds the pooled wallet's keys. Accounts partition the wallet's outputs; they are not separately exported user seed wallets. | `monero_user_wallets.py`, `monero_user_wallet.py`; API `/api/wallet/xmr/me`. |
| Exodus-style multi-asset Wallet, XMR asset | Seed-derived Monero wallet state, separate from both built-in RPC wallets. Encrypted wallet files and temporary private wallet-RPC processes. The blockchain daemon may be shared. | `exodus_wallet.py`, `exodus_monero.py`, wallet derivation/collection/transfer modules. |

Sharing a blockchain daemon does not mean sharing a wallet, account index, seed, or balance. The Exodus XMR implementation does not use the pooled account selector or the operator's RPC wallet. Conversely, the built-in pooled wallet is not made portable merely because the separate Wallet app offers seed export.

The pooled service locates accounts by a label built from `pc:` plus the stored user key. Account creation is serialized within the service instance and rechecks for an existing account before creating one. Caller input cannot select an arbitrary source account index. Do not casually rename account labels or change key normalization: those labels identify ownership.

## 2. Authentication and access

The browser or installed client establishes an application session using the configured Nostr sign-in path. Browser extensions and phone signers participate in Nostr authentication. The server then authorizes the wallet HTTP request. A Nostr signer is not the Monero transaction signer for the built-in RPC wallets; the Monero wallet service signs the chain transaction.

- Operator endpoints require the admin dependency and the instance-membership check.
- Personal-wallet endpoints require a signed-in user and instance membership. The source identity comes from the session's user record, not a request's account index or public-key parameter.
- Configured instance membership policy can deny wallet access before wallet RPC is reached. Profile/NIP-05 eligibility and chain synchronization are separate checks.

A message such as “could not establish your app session” identifies the authentication stage. “The wallet did not answer” can instead refer to RPC after authentication. Diagnose the failing stage before changing timeouts or restarting services. The Settings loading improvement adds accurate progress and stale-response guards; it is not proof that the earlier cold phone-signer stall is resolved.

## 3. Amounts, confirmation, and sending

The backend uses integer atomic units with `10^12` units per XMR. Payment input is decimal text parsed with `Decimal`, positive and finite, with at most twelve decimal places. Normalized monetary response fields use exact XMR text. Clients must respect each endpoint's schema: some fields, including the prepare response's `amount_atomic`, are explicitly atomic integers. Avoid converting money through floating-point arithmetic.

Address prevalidation checks configured-network prefix, Base58 character set, and allowed length. This is not a full address-checksum implementation; wallet RPC remains authoritative for acceptance. Network mismatch, insufficient unlocked funds, miner fees, and chain state remain real constraints even with application caps disabled.

### Operator two-step transfer

1. `POST /api/wallet/xmr/transfer/prepare` receives destination and decimal amount, validates them, and evaluates optional operator caps.
2. It returns a random confirmation token bound to the user, destination, amount, and a 90-second expiry. Pending tokens are process-local.
3. `POST /api/wallet/xmr/transfer/confirm` consumes the token before the RPC attempt. A SQLite spend-attempt reservation is recorded durably before the network call.
4. The wallet executes the transfer. The token cannot be replayed, including after an RPC failure. A process restart invalidates unconfirmed in-memory tokens, while the durable attempt ledger survives.

The durable daily total is wallet-wide across users and uses a rolling 24-hour window. It tracks attempted reserved principal, not a definitive chain ledger or complete accounting of miner fees. Unknown attempts remain charged to any configured budget. SQLite transactions protect reservations across connections; the pending-confirmation map itself is local to one process.

### Personal-wallet sends

`POST /api/wallet/xmr/me/pay` accepts 1–60 destination/amount entries. All destinations are validated before sending. The implementation uses a batch bound of fifteen destinations, reserving one destination for an aggregated service fee when applicable. This is an implementation policy based on its tested RPC behavior, not a statement of a universal Monero consensus maximum.

Batches are submitted sequentially through `transfer_split`. A later failure cannot roll back an earlier successful batch. The pooled API does not offer the operator's prepare/confirm token or a general durable request-ID replay contract. Consumers must account for partial batches and uncertain results rather than automatically resubmitting the whole request.

`POST /api/wallet/xmr/me/withdraw` sweeps the selected account's spendable funds to the requested address using `sweep_all`. It is separate from the percentage-fee payment path. Locked funds cannot be spent merely by requesting a withdrawal.

### Timeouts and duplicate-payment protection

Built-in RPC reads default to eight seconds. Spending methods receive at least 120 seconds, with a bounded connection timeout. A read/write/pool timeout during a spending call becomes `WalletUnsure`: the payment may already have been sent. A read timeout instead becomes `WalletBusy`. A connection failure is a separate availability error.

The personal Send UI has ready, sending, unknown, and sent states. Changing the acknowledgement checkbox cannot unlock an in-flight or uncertain payment. After dispatch, transport failures, gateway errors, or malformed successful receipts are treated as uncertain. Success requires a nonempty list of valid transaction hashes. Known validation refusals can be retried after correction.

An unknown result is not permission to send again. Check transaction history and pending wallet state first. The current implementation is not a universal exactly-once payment system across all endpoints, processes, and transport failures.

## 4. Repeated zaps and output replenishment

A wallet can show a sufficient total balance but lack independent unlocked outputs for another payment. Spending consumes selected inputs and produces outputs including change; those new outputs must satisfy chain unlock rules. More independent spendable outputs improve successive-payment capacity, but one payment can consume multiple inputs.

The pooled-wallet worker runs output maintenance every **60 seconds**, with one scheduler instance job at a time and coalescing. This is separate from the instance's fifteen-minute membership/access cleanup.

1. Inspect existing `pc:` accounts, ignoring unrelated labels.
2. Query `incoming_transfers` with `transfer_type=available`; this means unspent, not necessarily unlocked. Exclude spent and frozen outputs, and require an explicit true unlock flag to count spendable capacity.
3. At four or more spendable outputs, leave the account alone. The replenishment target is eight outputs.
4. If locked/unknown-unlock unspent outputs or a pending outgoing transaction exist, wait. These wallet-backed guards survive worker restart and prevent repeated maintenance consuming all remaining reserves.
5. Select one individually spendable output with a key image, preferring the largest. Use `sweep_single` back to the account address to replenish while preserving other inputs. The planning minimum is 0.001 XMR per resulting output; insufficient value causes waiting.

The regression fix stopped treating historical spent outputs as evidence of a split still unlocking. Another guard prevents repeated splits while the first transaction is pending or its outputs are locked. Maintenance itself is a real transaction with miner fees and can temporarily reduce spendable capacity; eight outputs do not guarantee eight instant zaps.

The operator wallet has an explicit `POST /split` action accepting 2–16 outputs. It uses a sweep back to its own address and is not the pooled single-input reserve-preserving strategy. Treat it as a spending operation, not a cosmetic setting. Neither split mechanism bypasses Monero consensus locks.

## 5. Service fees and public zap announcements

The pooled pay path reads `monero_zap_fee_percent`, defaulting to 2%. Nonpositive or invalid values disable it; the implementation clamps positive values to at most 50%. The cut is deducted from the entered amount, rounded down to atomic units, and aggregated into a fee destination per batch. Miner fees are additional and distinct from the service cut.

If a usable fee address cannot be resolved, or rounding yields no representable cut, the payment proceeds without that cut. A payment to the fee address itself is exempt. Operator-wallet payments and external-wallet URI/QR payments do not pass through this pooled percentage deduction. A configured percentage is therefore not proof a particular transfer incurred it.

The final “Do not post this zap” checkbox suppresses PosterChan's public announcement for that zap. It does not cancel payment, remove wallet history, hide a transaction from the wallet operator, or create an anonymity guarantee. The quiet choice is carried separately from payment success. Ordinary zaps retain the announcement path; withdrawals retain their own behavior. An external URI/QR handoff alone does not prove chain settlement.

## 6. Independent Exodus-style XMR implementation

The multi-asset Wallet service derives Monero keys from the selected wallet/portfolio recovery material and supports a separate Monero recovery phrase where provided. It uses encrypted wallet state, a private temporary wallet-RPC process, and a configured blockchain daemon. Balance synchronization is background work with cached state, rather than opening the built-in pool's account.

The XMR send path prepares and verifies a transaction, records its request fingerprint, transaction hash and relay metadata durably before relay, and records an uncertain outcome if relay acknowledgement is lost. It invalidates stale balance cache after a relay attempt. Reusing the tracked request must not silently construct a second payment. These records are distinct from the built-in operator spend-attempt ledger.

Seed/recovery reveal is an explicit action, including `/reveal-monero`, not an ordinary balance/status response. Exportability does not mean the hosted service lacks access to spending material during operation. Do not describe this as browser-only self-custody.

Compatibility must be demonstrated using matching recovery material, derivation, portfolio selection, restore height and resulting addresses. The interface is Exodus-style; this document does not assert affiliation with Exodus or universal official-app import/export compatibility. In particular, do not assume a general multi-asset phrase automatically reproduces every externally created Monero wallet. Preserve and verify the separate Monero backup when applicable.

## 7. Configuration and API reference

Built-in configuration generally reads stored node settings first, with environment fallbacks. Defaults are not evidence of a live instance's current settings. The operator default network is stagenet; real mainnet operation must be explicitly configured.

| Purpose | Environment fallback / behavior |
| --- | --- |
| Operator enable / RPC | `MONERO_WALLET_ENABLED`, `MONERO_WALLET_RPC_URL`, `MONERO_WALLET_RPC_USER`, `MONERO_WALLET_RPC_PASSWORD` |
| Network / reads | `MONERO_WALLET_NETWORK` (stagenet default); `MONERO_WALLET_RPC_TIMEOUT` (8 seconds default; operator validation allows 0.5–30). |
| Optional operator caps | `MONERO_WALLET_TRANSFER_CAP_XMR`, `MONERO_WALLET_DAILY_CAP_XMR`. Both default to `0`, meaning disabled. Existing nonzero stored settings still impose caps. |
| Attempt ledger | `MONERO_WALLET_SPEND_LEDGER`, default `data/monero_wallet_spend.sqlite3`. Durable storage required. |
| Pooled RPC | `MONERO_POOL_RPC_URL`, `MONERO_POOL_RPC_USER`, `MONERO_POOL_RPC_PASSWORD`; all required for enabled status. |
| Pooled fee | `MONERO_ZAP_FEE_PERCENT`, stored setting `monero_zap_fee_percent`. |
| Independent Wallet XMR | `EXODUS_MONERO_DIR` (default `data/exodus-monero`), `EXODUS_MONERO_RPC_BINARY` (or executable lookup), `EXODUS_MONERO_DAEMON`. |

The operator RPC validator requires authenticated plain HTTP to a numeric loopback or RFC1918 address, an explicit port, and a path ending in `/json_rpc`; public IPs, hostnames, embedded credentials and redirects are not accepted by that path. The pool has its own configuration/client; do not infer identical URL validation from the operator validator. Both built-in clients use HTTP Digest authentication and disable environment-proxy inheritance.

Operator routes: `GET status, balance, address, history, sync, node-status`; `POST make-uri, split, transfer/prepare, transfer/confirm`. Prefix `/api/wallet/xmr`. The legacy `/api/wallet/monero` alias remains for compatibility. Personal routes under `/api/wallet/xmr/me`: `GET status, address, balance`; `POST pay, withdraw`. Use the canonical XMR path for new clients.

## 8. Operations and troubleshooting

- **Wallet does not load:** distinguish missing app session, membership denial, unconfigured RPC, failed connection and a busy wallet. Check endpoint response and role logs before assuming signer or chain failure.
- **Balance exists but send fails:** inspect unlocked balance, selected account, network, fees, available inputs and pending transactions. Total balance alone is insufficient.
- **Spending-cap error:** inspect stored operator settings as well as environment defaults. Zero disables optional caps; pooled sends do not use that operator gate. Do not erase the attempt ledger to hide an uncertain payment.
- **Repeat-zap waiting:** inspect maintenance execution and unspent/unlocked/pending state. Historical spent outputs must not count as an in-progress split. Do not repeatedly sweep reserves.
- **Uncertain send:** reconcile wallet history and pending transaction hashes before another payment. Repeated UI actions are not a safe recovery strategy.
- **Independent XMR unavailable:** inspect its executable, daemon configuration, wallet files and background synchronization. Enabling the built-in pool is not its repair.

Back up the actual operator wallet, pooled wallet, independent encrypted wallet/recovery material, required encryption keys/configuration and durable transfer records according to the paths in use. Verify restoration; this guide does not certify an installation's backup schedule. Never publish seeds, RPC credentials, bearer tokens or transaction relay metadata in support logs.

Deploy role-aware changes: built-in Monero service changes affect app and maintenance worker; client-only changes do not justify restarting relay, live media, or terminal keeper. A docs-only deployment requires no wallet or application restart.

## 9. Regression tests and remaining limitations

Relevant repository tests include `test_monero_wallet.py`, `test_monero_wallet_api.py`, `test_monero_wallet_test_isolation.py`, `test_monero_a_timeout_is_not_a_failure.py`, `test_monero_busy_is_not_absent.py`, `test_monero_user_output_maintenance.py`, `test_monero_keep_outputs_counts_spendable.py`, `test_monero_zap_service_fee.py`, `test_exodus_monero.py`, and client Send, scan and quiet-zap runtime tests.

Coverage includes exact decimals, authorization, account selection, caps disabled with zero, durable reservations, replay/expiry, ambiguous outcomes, checkbox double-send protection, receipts, fee behavior, spent-versus-locked outputs, maintenance across restarts and independent-wallet boundaries. Wallet test isolation must prevent fixtures writing the production ledger; an earlier test contamination incident made that boundary necessary.

The c43178cf4 release's combined focused gate passed 72 cases, including quiet-zap and other release features. This is not a claim that 72 tests were all Monero tests or that a fresh entire project suite passed at that SHA. Prior broader results and explicit skips are recorded separately. No live funds were spent to write or publish this guide.

Open concerns include the cold phone-signer session stall, intermittent pooled maintenance `get_accounts` read timeouts, partial multi-batch outcomes, physical-device verification, and recovery compatibility validation for external wallets. Preserve uncertainty in both UI and operations; neither this documentation nor existing tests establishes an absolute guarantee against future regressions.

Source map: `app/services/monero_wallet_service.py`, `app/services/monero_user_wallets.py`, corresponding routers, `static/js/client/monero-wallet.js` and `app.js`, `app/services/exodus_monero.py`, `app/routers/exodus_wallet.py`. Historical incident notes in `docs/MONERO-*.md` describe their dated state and may contain older cap settings.

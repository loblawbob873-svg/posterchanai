# Exodus wallets and portfolios — implementation and review

Adds a named-wallet selector and up to 16 named portfolios per wallet, asset logos,
USD portfolio value, allocation, and recorded value history. Each additional wallet
has its own recovery phrase; portfolios use the existing phrase's BIP-44 account
component. Existing account-zero receive addresses and keys are preserved.

Private storage uses the existing kind-30078 encrypted namespace and retention rules.
A new table holds encrypted backups; there is no migration or replacement of original
seed rows. Wallet IDs are server-generated and resolved only inside the authenticated
account. Strict failed reads never mean an absent wallet. Seed/backup mismatches fail
closed. Discovery uses namespace filtering and stable pagination, including equal
second timestamps. Monero is shown once, in the original wallet's first portfolio,
because its existing pooled account is separate from BIP-39 derived wallets.

Prices are fetched together from CoinGecko with a 90-second shared cache. Missing
balances and prices older than 15 minutes produce explicitly incomplete totals.
Arithmetic uses Decimal. History records actual complete observations at most once
per 15 minutes; no retrospective graph is invented. Failed history reads cannot
replace existing history. Logos are bundled SVGs with their MIT license.

Review addressed private-document retention/broadcast policy, stale responses after
wallet/account switches, receive/recovery target isolation, duplicate send clicks,
HTML escaping, and preserving open forms during asynchronous balance updates.
The Monero review found a deployed singleton/factory mismatch hidden by old test mocks:
the route called UserWallets as a function. The separate Monero fix uses the actual
singleton and tests its real account lookup/conversion with only RPC transport mocked.
A read-only live check confirmed a known balance and matching existing receive address.

Validation before integration: 130 wallet tests passed, including 24 browser cases
across nine themes at phone/desktop sizes, delayed responses, and duplicate send
confirmation. The later Monero fix passed 43 wallet/Monero tests. Final integration
and deployment results will be recorded after those checks finish.

Scope: existing coin/network coverage is preserved; sending remains supported by the
existing EVM implementation. This does not claim complete compatibility with every
Exodus derivation path, token, hardware wallet, or imported portfolio discovery rule.
The node continues to hold these wallet keys, as stated in the existing UI.

References:
- https://www.exodus.com/support/en/articles/8598661-how-do-i-manage-multiple-portfolios-in-the-same-wallet
- https://www.exodus.com/support/en/articles/8598933-derivation-paths-in-exodus
- https://docs.coingecko.com/reference/simple-price

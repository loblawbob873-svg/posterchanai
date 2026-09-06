# Exodus wallets and portfolios

The dashboard adds named wallet switching, up to sixteen named portfolios per wallet,
asset logos, USD portfolio value, allocation, and recorded value history. Each added
wallet has its own recovery phrase; portfolios use the wallet's account derivation.

Private documents use the existing encrypted kind-30078 namespace. A separate table
keeps encrypted backups without replacing original seed rows. Wallet identifiers are
server-generated and scoped to the authenticated account. Failed reads never mean
an absent wallet; seed or recovery-profile mismatches fail closed. Discovery uses
stable pagination, including documents with equal timestamps.

Prices are fetched together with a ninety-second cache. Missing balances or stale
prices produce an explicitly incomplete total. Decimal arithmetic preserves value
precision. History records complete observations at most once per fifteen minutes;
the graph does not invent observations before the wallet was opened. Asset logos
are bundled SVGs with their license.

The final implementation uses independent Monero wallet files and recovery keys.
It does not read or spend from the built-in Monero zap wallet. See
[the wallet review](EXODUS-WALLET-REVIEW-20260906.md) for the final recovery formats,
separate Monero import, supported send assets, deployment prerequisites and tests.

The review covers stale responses after wallet/account switches, receive/recovery
isolation, duplicate send clicks, HTML escaping, and preserving open forms during
asynchronous balance updates. Actual browser tests cover nine themes at phone and
desktop widths. The server manages the keys; recovery exports allow restoring the
supported assets independently, subject to the documented derivation format.

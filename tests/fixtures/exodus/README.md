# Independent Exodus derivation vectors

`keychain-v12.json` was generated using the official MIT-licensed `@exodus/keychain`
12.0.0 and its locked dependencies, rather than this application's derivation code.
It contains only the public BIP-39 `abandon … about` fixture. Never fund its addresses.

To reproduce in a disposable directory, copy `reference/`, run `npm ci --ignore-scripts`,
then run `node vectors.mjs` and compare its JSON output with `keychain-v12.json`.
The lockfile records package URLs and integrity hashes. These development dependencies
are not bundled into the application.

The fixture covers every currently listed BIP-39 asset at portfolio accounts 0, 1 and 15,
including Solana's Exodus-specific BIP-32 derivation and XRP's hardened child paths.
Bitcoin includes BIP-44, BIP-84 and BIP-86, receive/change branches and subsequent addresses.
Litecoin, Dogecoin and Bitcoin Cash also include both branches and subsequent addresses;
the fixture now contains ninety independently generated key pairs.

Reference: https://www.exodus.com/support/en/articles/8598933-derivation-paths-in-exodus
This verifies key compatibility. It does not verify address discovery, network balances,
transaction construction, or historical Exodus Monero restoration.

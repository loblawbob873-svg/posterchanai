# Wallet expansion review

This change is isolated from the built-in Monero wallet and its zap output pool.
Wallets and portfolios retain separate addresses, recovery information and display state.
The dashboard includes asset logos, available portfolio value and recorded value history;
an unavailable chain does not silently become a zero balance.

## Recovery compatibility

New BIP-39 wallets use the versioned `exodus-v1` derivation profile. Checked-in public
vectors from `@exodus/keychain` version 12 cover Bitcoin receive/change paths and the
Solana and XRP derivations. The fixture README records their origin and reproduction.
Existing documents without a profile retain `cloudos-v1`; their previously issued
addresses must not change. Such legacy Bitcoin, Solana and XRP addresses do not match
an ordinary Exodus phrase restore. The recovery UI and downloadable backup retain
that distinction. The restore form's Recovery format option accepts a legacy CloudOS
backup explicitly, preserving those original addresses; unknown formats are refused.

Historical Exodus twelve-word Monero recovery has not been verified. Importing an
existing Monero wallet therefore takes its separate twenty-five-word recovery phrase.
It applies to the main portfolio only and remains separately exportable. Other
portfolios derive their own independent Monero keys. Exported JSON includes the profile
and both phrases when applicable; restoration currently uses the phrase fields rather
than a JSON file upload.

Native sends are implemented for ETH, MATIC, BNB, AVAX and independent XMR. Sending
BTC, LTC, DOGE, BCH, SOL and XRP remains unavailable; those buttons are not shown.
This is not full Exodus asset or token support. No real funds were sent during testing.
Bitcoin, LTC, DOGE and BCH discovery scans receive and change branches. Spent addresses
still extend the scan; one missing history response prevents a complete balance. The
gap limit is twenty unused addresses with a maximum of one thousand addresses per
branch. Bitcoin includes BIP-44/84/86; the other three use their documented Exodus
BIP-44 families. Native Dogecoin and Bitcoin Cash providers and custom Esplora
endpoints retain their distinct history contracts. Expired or incomplete scans remain
unknown, never a confident zero. The dashboard polls while visible and pauses around
open send/import/recovery forms so it does not interrupt typing.

## Transfer review

EVM sends verify the selected network and sender, calculate fees before broadcast,
and persist the locally signed transaction identity before sending. Request identities
and wallet locks are shared by this node's workers. Duplicate imports of the same key
within the same authenticated user share the spend lock. Lost acknowledgements remain uncertain until the recorded hash
can be found; neither a new request ID nor a worker restart silently repeats a payment.

Independent Monero sends prepare without relaying and persist encrypted transaction
metadata before relay. A review regression covered process cleanup failing after a
successful relay or a lost relay acknowledgement: both previously reported that no
transaction had been relayed. Both now retain the uncertain outcome and transaction
hash, with retries still deduplicated.

The durable journals are local to one application node. Independently writable nodes
must not concurrently serve the same wallet without a shared spend coordinator.

## Monero deployment prerequisites

Provision these only with the wallet release, on every application node serving its
wallet endpoints:

* Install a verified `monero-wallet-rpc` executable and set
  `EXODUS_MONERO_RPC_BINARY` to its absolute path. The test executable was version
  0.18.5.1 with SHA-256
  `c1e3aff7c72837e6f29045c439b772a82b5cd7324c8b831fa825a6ce2019a656`.
* Set `EXODUS_MONERO_DAEMON` to a synchronized mainnet blockchain daemon. The reviewed
  internal read endpoint was `http://nas.lan:18089`. This is a blockchain endpoint,
  never either built-in wallet-RPC endpoint.
* Keep `EXODUS_MONERO_DIR`, `EXODUS_TRANSFER_DIR` and `EXODUS_DISCOVERY_DIR` on durable
  storage writable only by the application user. Defaults live below `data/`.
  Include encrypted wallet files and transfer journals in private backups.

The wallet process uses private temporary configuration, localhost authenticated RPC,
a bounded lifetime and per-wallet locks. Imported wallets may require a full blockchain
scan before their balances are known. No provisioning or production deployment is
claimed by this review document.

## Validation evidence

* Complete final Exodus core and browser run on production Python 3.11: 295 passed
  (266 core, 29 browser), including actual offline
  Monero wallet creation, reopening and matching recovery words.
* Actual Chrome wallet dashboard and recovery actions: 29 passed, including all
  configured themes at phone/desktop widths, stale responses, duplicate send clicks,
  downloading both phrases and clearing the revealed words.
* The complete client suite caught one stale assertion requiring the old custody
  wording that the user requested changing. The corrected eight-test group requires
  a visible server-managed summary plus accurate operator-key and recovery-export
  disclosure. Its focused rerun passed.
* Cleanup-after-relay regressions: two failed before the production fix and two
  passed after it. The final core run includes these new cases.
* Successful and uncertain relay attempts both invalidate the prior Monero balance
  snapshot; two regression cases failed before the fix and pass afterward.
* The initial browser run failed to start Chrome because sandbox socket access was
  denied. The unchanged tests passed outside that sandbox; this was not an application
  failure. A sandboxed core run was interrupted before test output and is not a pass.

Desktop and phone screenshots were visually reviewed at `/tmp/pc-wallet-desktop.png`
and `/tmp/pc-wallet-phone.png`, using deterministic public fixture balances.

Logs are preserved under `/tmp/pc-wallet-*-review.log` and
`/tmp/pc-wallet-backup-browser-rerun.log`. Integration and the complete repository
suite are separate release gates.

Discovery contracts were checked against the [Exodus path table](https://www.exodus.com/support/en/articles/8598933-derivation-paths-in-exodus),
the [BlockCypher address API](https://www.blockcypher.com/dev/bitcoin/#address-balance-endpoint),
and the [PSF BCH consumer](https://github.com/Permissionless-Software-Foundation/bch-consumer).
A public-address read verified the current BCH `/bch/txHistory` response; no transfer
was made. Thirty-six additional official keychain vectors cover LTC/DOGE/BCH receive
and change indices across three portfolios.

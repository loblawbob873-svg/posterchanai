# BTC, LTC, DOGE and BCH send review

This follow-up completes native sends for the wallet's listed assets: BTC, LTC,
DOGE, BCH, SOL, XRP, ETH, MATIC, BNB, AVAX and independent XMR. It changes only the
Exodus wallet path; the built-in Monero wallet and signer transport are unchanged.
Tokens, NFTs, automatic Exodus portfolio-label restoration and hardware-wallet
integration are outside this native-asset implementation.

Funding is discovered across the selected portfolio's receive/change branches.
Bitcoin supports BIP44 legacy, BIP84 SegWit and BIP86 Taproot inputs together. Other
UTXO assets use their documented BIP44 keys. New Bitcoin wallets return change to
BIP84; legacy CloudOS wallets retain BIP44 change so old recovery formats continue
to find it. Change uses the next unused index reported by fresh discovery. Wallet
locks use a stable account identity across legacy/current BTC imports within the
same authenticated user on this node.

Every selected output's amount and locking script are verified against the raw
funding transaction's hash and selected private key. The provider is queried again
to confirm the output is unspent before broadcast. Recipient network/checksum,
minimum output, explicit change and final serialized fee are checked. embit0.8.0
constructs/signs BTC/LTC/DOGE; BitCash1.2.0 supplies BCH fork-ID signatures. BCH
multi-key transactions retain only the correct input script from each SDK-signed
variant. Tests independently verify the BCH BIP143/FORKID preimage and signature.
No transaction-signing cryptographic primitive is implemented in application code.

The local transaction identity is encrypted and persisted before broadcast.
Matching acknowledgements remain pending until transaction lookup verifies the
same raw transaction hash. Repeated request IDs return their recorded result;
different requests cannot spend during an unresolved submission. A lost reply
never triggers an automatic resend or a false payment-failed response. Cache
cleanup errors cannot reverse the reported submission outcome.

Two file-locked slots bound concurrent native send work across server processes.
Address/key derivation, transaction signing and funding validation run off the
application event loop. A heartbeat regression proves slow derivation does not
stall other tasks. Dogecoin sends, status lookups and discovery share a file-locked
0.4-second request schedule across workers. Its queue is capped at20 seconds;
reboot/invalid persisted clocks are reset and canceled reservations leave bounded
gaps. This pacing does not remove the public provider's hourly/daily quotas.

Resource/operational limits:
- Discovery must be less than300 seconds old. At most256 discovered addresses,
  2000 candidate outputs and50 selected inputs are supported per send.
- Preparation is limited to55 seconds; provider responses are capped at4 MB.
- Dust floors: BTC/BCH546 satoshis, LTC1000 litoshis, DOGE0.01 DOGE.
- Fee ceilings: BTC0.01 BTC, LTC0.1 LTC, DOGE100 DOGE, BCH0.01 BCH. Fees must also
  cover the actual signed virtual size. BCH defaults to2 sat/byte, configurable
  with `exodus_fee_bch_sat_vbyte`; other providers supply estimates.
- The native BCH provider must classify outputs as plain BCH. SLP, CashToken and
  unclassified outputs are excluded. Bitcoin inscriptions/other overlay assets
  are not detected: this is a native-coin wallet, not an NFT/overlay wallet.
- Missing/pruned transaction history does not prove a failed broadcast. Such
  payments remain locked; automatic cancellation, replacement and fee bumping are
  not implemented. The same local-node/cross-user lock limits as the earlier
  EVM/SOL/XRP release still apply.
- Live read-only checks using the actual application transport verified BTC,
  LTC and DOGE mainnet/fee responses and the BCH mainnet anchor. A subsequent BCH
  public-example output lookup timed out; public-provider availability remains
  an operational dependency. Provider errors fail closed. No live monetary send
  was performed by this wallet-expansion thread.

Dependencies are pinned in both app requirements files. Installed production
coincurve21.0.0 and requests2.34.2 already satisfy BitCash; embit has no runtime
dependency graph. XRP remains in its separate SDK environment, preserving the
application's websockets16 signer/relay transport.

Validation:
- Full wallet scope:396 passed with one malformed test fixture failure. The fixture
  accidentally inserted the required address; corrected missing-address,
  malformed-list and valid-empty cases are retained explicitly.
- Subsequent affected scope:133 passed (provider, SDK signing, transfer journal,
  route, pacing and discovery tests).
-32 browser tests passed with all native send buttons enabled.
- Final legacy-change review added a regression that failed before the correction;
  legacy BIP44 and current BIP84 change tests both pass after it.
- Pyflakes, JavaScript syntax and git diff whitespace checks pass.

Primary provider/library contracts:
- https://github.com/diybitcoinhardware/embit
- https://github.com/pybitcash/bitcash
- https://github.com/Blockstream/esplora/blob/master/API.md
- https://www.blockcypher.com/dev/bitcoin/
- https://github.com/Permissionless-Software-Foundation/ipfs-bch-wallet-consumer
- https://github.com/Permissionless-Software-Foundation/ipfs-bch-wallet-service
- https://github.com/litecoin-project/litecoin/blob/master/src/chainparams.cpp
- https://github.com/dogecoin/dogecoin/blob/master/src/chainparams.cpp

The checked-in public BCH reference transaction comes from the service's own
documentation and is verified by hash. It anchors BCH mainnet without relying on
the genesis coinbase, which getrawtransaction cannot retrieve.

Final integration review caught two provider-contract defects before release:
DOGE's real fee quote exceeded the generic BTC-oriented rate bound, and LTC's
provider returned 404 for the Esplora fee endpoint. DOGE now uses a chain-specific
rate limit while retaining the total transaction fee ceiling. LTC falls back to
its recommended-fees endpoint only on 404 and strictly validates hourFee. The
93 UTXO regression tests pass, including captured public payload shapes, invalid
fees and fee ceilings. Corrected live quotes passed through the app transport.

The final browser suite has 51 wallet cases, including actual Send controls and
submission forms for BTC, LTC, DOGE, BCH, SOL and XRP, wallet selection, receiving
and pending status. Those 51 cases also passed against both generated Android
and desktop web bundles. Full release results are recorded separately in
WALLET-SMS-FINAL-RELEASE-20260907.md.

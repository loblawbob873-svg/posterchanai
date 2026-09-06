# Active shared-checkout handoff — 2026-09-06

Security/splash continuation session: `01a0777d-0781-7900-a3ab-9c088a324d8e`.
Recovered source: `01a073f2-e0ac-7f03-b309-9b5a019df1fe`.

The user explicitly requests that agents avoid interfering with each other's work.

- The agent already merging `static/js/client/app.js` owns that merge. The
  security/splash continuation will not edit or stage that file during the merge.
- The continuation owns new `instance_welcome` backend/service/tests and
  `instance-welcome.js` / `instance-welcome.css` assets, plus audit regressions
  and `docs/CONTINUATION-20260906.md`. Please leave those files alone.
- Existing security changes span authentication, middleware, native OS session
  proofs, dependency manifests, and packaging. Do not reset or stage the whole
  checkout. Review individual hunks when touching those shared files.
- Wallet/Exodus changes belong to the other agent; the continuation leaves them alone.
- Template/router registration changes require a fresh read and minimal patches.
  Any unexpected changes to a file under active work should be reconciled before writing.

This is a coordination notice, not an exclusive filesystem lock. Separate
sessions cannot currently communicate through the continuation's agent tool.

Additional continuation-owned files: `app/routers/jellyfin.py`,
`tests/test_jellyfin.py`, `static/js/client/media-center-ui.js`,
`static/css/media-center-ui.css`, and the small XML-parser security lock update
in `mobile/package-lock.json`. User requests final combined integration checks
before merging/deploying both agents' work.

Combined-suite failure to reconcile: `tests/client/concord_runtime.mjs:625`
expects `pickerAt.left/top`, but the current emoji-picker bridge leaves
`pickerAt` null. This appeared before the continuation's Jellyfin/UI changes.
Please review alongside the recent Concord picker commits.

Continuation integration update: the Concord failure was a stale DOMRect fixture,
missing width/height after hidden-anchor validation landed. Updated only the
fixture and added a zero-size regression; `node tests/client/concord_runtime.mjs`
passes. No change to Concord production JS or app.js.

Production runtime reconciliation in progress: `run-intel.sh` uses venv-unified,
which still contains FastAPI 0.136.3/Starlette 0.50.0 and pre-fix image/crypto/HTTP
packages; .venv tests use patched versions. Continuation is preparing a scoped
runtime dependency upgrade and final verification. Please avoid restarting or
blanket-deploying the dirty checkout during this reconciliation.
Additional owned files: XML parser safety in app/routers/news.py and office.py;
welcome retry scheduler registration in app/worker.py.

Final integration: wallet commits through 2a22648cc are preserved. User explicitly
requested testing and deployment. A three-line linkify normalization was added to
app.js for already-delivered application DMs containing nostr:<hex pubkey>;
new application DMs use nostr:npub. Both formats pass the actual DM renderer test.
The existing navigation + signed OS-account-switch hunks remain intact.
Runtime web/crypto/image dependencies are now patched on server1 and nas.
One historical exposed API key was confirmed active and revoked in server1's DB.

Windows DM follow-up: continuation now owns a minimal app.js DM startup change:
install subscriptions before history, bound historical decryption batches, retry
cached failed decryptions every minute, and release the history latch on query
failure. Actual shipped-function tests cover liveness, retry, and operator self-DMs.
Wallet stylesheet commit e6d184789 is preserved and will be included in deployment.
User explicitly requests committing all completed work and deploying both agents.

Completed integration: 4a7e7a7d3 committed all reviewed continuation files, preserving
e6d184789 and earlier wallet work. Full suite 10,736 passed + 630 subtests, 25 skips;
later DM/UI/wallet selection 132 passed. Deployed server1/NAS/router, verified
matching public assets and healthy services. Desktop CI published 1.0.1491 for all
platforms; continuation updating the Gentoo pin and final deployment records.

SIGNER PRIORITY FOLLOW-UP: user prioritizes correcting regression in 4a7e7a7d3.
Continuation owns app.js DM queue/cache barrier, dm_delivery/dm_retry harnesses,
release workflow gates, and check_nip46_bulk_lane helper correction. Full suite
will run before signer deployment. Do not blanket-commit or publish this checkout.
Other requested fixes (agent.txt OS first-run/window controls and AI/Blossom/Live
Streaming policy preview + 15-minute cleanup) follow that deployment. Unfinished
welcome/first-run test patches are saved in /tmp/pc-followup-after-signer.patch.

Signer correction febb0fe00 deployed all nodes; full suite 10745 +630 subtests passed,25 skips. Desktop/Android CI running. Continuation now owns welcome/first-run, registration visibility, Concord reaction anchor (shared picker bridge), media device layout and rescan, policy service/admin/worker scheduling, and tests. User additionally requests Exodus presentation, totals/charts/logos and wallet switching; preserve prior wallet implementation and all seeds. Avoid blanket commits/deployment while this stage is dirty.

Coordination: new independent edits detected in os/gentoo.sh (installer memory/mirror/scripted options). Preserved; continuation will not blanket-stage them. Desktop1492 pin verified but its gentoo.sh line shares your dirty file. Please commit/test your installer changes explicitly when ready; sync.sh publishes working-tree overlay so we will deploy only a reviewed snapshot.

Full follow-up suite stalled at55% in tests/test_exodus_wallet_routes.py teardown (py-spy confirmed anyio portal cancellation waiting forever). That route-only fixture unnecessarily started every service. Continuation changed it to a FastAPI app with the real wallet router and mocked chain transport; wallet/storage/chain tests remain intact. Rerunning full suite. This also explains the other session's long-running wallet route test process; did not stop that other process.

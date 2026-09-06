# Security and media continuation — 2026-09-06

Recovered session: `01a073f2-e0ac-7f03-b309-9b5a019df1fe`.
Integration base includes the independent wallet agent's commits through
`e6d184789`. No checkout reset, whole-tree restore, or history rewrite was used.

## Findings fixed

- Nostr authentication accepted replayable public events. Ownership proofs now
  require kind 27235 and a recent timestamp; login and privileged administration
  additionally require the correct action. Tests reject public-event kinds,
  stale signatures, wrong identities, and cross-action proofs.
- Peer trust previously failed open when its shared secret was absent. It now
  fails closed, including settings-read errors. Both deployed nodes were checked
  for configured peer credentials without printing them.
- Cookie mutations lacked an Origin guard. Cross-origin ambient-cookie requests
  are rejected; a Basic Authorization header cannot bypass it. Explicit Bearer
  requests still authenticate through their own route guard.
- Uploaded HTML/SVG now receives an opaque-origin CSP sandbox, nosniff and no-referrer
  headers on the files/storage/Blossom routes.
- Anonymous helper requests now have body-size, per-IP request and concurrent-work
  limits. A disconnected caller cannot free a slot while its work is still running.
- Screenshot image redirects are checked at every hop and streamed under a size
  limit. The incoming Host header no longer adds a trusted private fetch domain.
- Password and both email-verification login paths no longer create administrator
  sessions. Administrators use their signed Nostr identity.
- Native OS provisioning/switching now requires a root-issued, single-use,
  caller/identity/action/payload-bound signature. User-controlled home descendants
  are written after dropping root privileges. Installer and published overlay
  include the verifier and its canonical crypto modules.
- SSH terminal connections verify the operator account's known_hosts and reject
  unknown or changed host keys.
- Remote RSS and office discovery XML use defusedxml. Entity-expansion regression
  tests reject malicious XML while preserving normal RSS and safe office fallback.
- Compose drops NET_RAW, enables no-new-privileges, removes SYS_TIME and removes
  the example default office administrator password.
- One API key found in historical load-balancing test scripts was still active in
  the current database. It was revoked; a follow-up query confirmed zero matching
  active keys. No key values are included in this report. A separate historical
  OpenWebUI key was not present among current API keys; this does not establish its
  status on any external service.

## Dependency scans and deployment environment

The original production Python inventory reported 61 advisory records in 15
packages. The post-update inventory reports 16 records in five packages; duplicates
in the advisory feed are included in these counts. Server1's actual `venv-unified`
was updated, not just the test `.venv`. NAS web dependencies were updated too.

Patched server1 packages: FastAPI 0.141.1, Starlette 1.6.0, cryptography 50.0.1,
aiohttp 3.14.3, Pillow 12.3.0, icalendar 7.3.0, MCP 1.29.1, ONNX 1.22.0,
pyasn1 0.6.4, pydantic-settings 2.15.0 and pip 26.2.1. The templates use the
request-first Starlette API. Existing GPU torch/transformers pins were preserved.

Remaining findings are **not a clean bill of health**:

| Package | Remaining issue / disposition |
| --- | --- |
| chromadb 1.5.9 | Four server authorization/model-code advisories; the feed supplied no fixed version. Installed-package finding, not a verified exposed Chroma endpoint. |
| diskcache 5.6.3 | Pickle loading after attacker write access to a cache directory; no fixed version supplied. |
| ecdsa 0.19.2 | Signing/key-generation timing exposure; no fixed version supplied. Verification is not the affected operation. |
| transformers 4.57.6 | Six advisory records concerning model/checkpoint loading and serialization. Fixes require 5.x; this project's music stack explicitly requires <5. No unsupported major upgrade was forced. |
| setuptools 70.2.0 | Four advisory records (two distinct issues): package-index path traversal and source-distribution exclusion behavior. Version 84 was resolved but reverted because the installed torch/ACE-Step stack constrains setuptools below the complete fix. |

Mobile npm's vulnerable XML parser lock entry was updated; npm audit then reported
zero findings. Desktop npm audit reported zero. Go dependencies were upgraded to
Pion TURN 4.1.4 and current compatible crypto/transport versions; Docker's builder
now uses Go 1.26. Server1's installed TURN executable was rebuilt atomically
with Go 1.26.4; NAS/router had no existing TURN executable to replace. `govulncheck` exits zero: the remaining module-only record,
GO-2026-5932, concerns unimported legacy OpenPGP packages and has no reachable call
trace. `go test ./...` compiles the module; it has no Go test files.

The Rust OSV inventory checked 385 registry packages and found paste 1.0.15's
unmaintained-package notice, RUSTSEC-2024-0436.

Bandit after fixes reports 837 findings: 759 low, 68 medium, 10 high. The ten high
records are SHA1/MD5 use for persistent bot dedup/config identifiers and thumbnail
cache names, rather than authorization signatures; changing their identity would
lose dedup continuity. SQL warnings were reviewed for parameterized values and
allowlisted/static query fragments. Shell warnings include explicit operator SSH
terminal functionality. These heuristic findings are retained, not suppressed to
manufacture a zero count. History scanning reported 42 matches: most are example
credentials, test vectors, storage-key names, and comments; the confirmed active
historical credential is addressed above.

Limitations: dependency inventories do not prove exploit reachability; this is a
source/configuration review and automated regression scan, not an exhaustive
penetration test. Fetch validation still does not pin DNS resolution between
validation and connection. Native hardware/GPU stacks and all optional integrations
cannot be certified by browser fixtures.

## Welcome, directory UI, and Roku fixes

The instance welcome is a separate theme-aware module with the instance logo,
File Storage / Live Streaming / AI benefits, signed one-click application, durable
application state, administrator notification, and approval DM with profile setup
instructions. Notification delivery retries in the worker. Delivered recipients
are remembered; a process crash between relay acceptance and recording it can still
cause a retry duplicate. Existing members and pending applicants are not prompted.
Both new npub links and previously delivered hex-key profile links are clickable.

Media Center's three administration panels live in a keyboard-accessible hamburger
menu. Folder navigation uses artwork tiles and plain breadcrumbs. The existing
forms and handlers are preserved. The isolated browser check loads the real module
and uses separate dynamically allocated HTTP ports for its edge/NAS fixtures.

Official Roku AudioMiniPlayer directly indexes `Artists[0]`; our missing Artists
array reproduces a BrightScript runtime crash. Audio metadata now includes required
arrays. Roku's ItemId-only playback reports resolve only one fresh play owned by
that exact device token; ambiguous or explicit-invalid sessions are rejected.
Audio artwork uses an opaque session-bound ID because Roku omits auth/Tag on that
path. API/video requests remain authenticated, and artwork rechecks device/session
and library access. Canonical bare artwork IDs remain denied. In-memory artwork
grants are bounded, expire, and are recreated by authenticated metadata reads;
clients must refresh metadata after a server restart. Files with no source cover
can still have no thumbnail.

The reported Roku is a 55S451 running Roku 15.1.4 with developer mode disabled.
No device crash stack or post-fix physical-Roku playback result was obtained; the
BrightScript reproduction and official client tests are the available evidence.

## Windows DM delivery and final review

Live subscriptions now start before shared-cache downloads and historical decryption.
Historical gift wraps enter six at a time, so a new arrival does not wait behind
hundreds of signer requests. Failed history reads release their retry latch without
creating duplicate subscriptions. A minute timer retries cached, not-yet-decrypted
wraps: relay-level event dedup otherwise suppresses redelivery after a transient
signer error. Gift-wrap subscription dates remain unfiltered because their outer
timestamps are randomized. Historical operator-to-self NIP-05 application notices
remain visible and unread; replay does not duplicate them or their unread count.
These are reproduced client liveness defects, not a remotely captured diagnosis of
the user's Windows process. Physical Windows delivery is not yet observed.

Final source review covered the combined auth/middleware/native changes, notification
retry persistence, artwork authorization and expiry, folder navigation, DM startup
and retry behavior, dependency/runtime alignment, and both agents' shared template,
service-worker and app.js changes. No history rewrite or blanket restoration was
used. The independent wallet stylesheet and wallet commits remain in the ancestry.

## Verification

- 77 Jellyfin tests passed against official JS SDK and Roku/BrightScript tools in
  the production Python environment: local and NAS proxy, actual HLS decode,
  playback progress, private artwork, revocation, subtitles/audio switching.
- 123 production-runtime security/welcome/Jellyfin tests passed (six optional
  client-tool cases skipped in that run; covered separately above).
- 192 integration regressions passed; 222 wallet/settings/upstream-check tests
  passed with isolated settings. The original full run had 10,614 passes and
  82 failures. Missing wallet test dependencies, stale DOM/bridge fixtures, a
  phantom theme variable and leaking settings state were corrected. Full rerun
  passed: **10,736 tests and 630 subtests, 25 skipped**, in 26m57s. The later
  Windows DM/UI selection passed separately (132 tests).
- 132 combined DM, system-notification, welcome, menu and wallet-style checks passed;
  browser cases cover all nine themes and phone/
  desktop sizes. A separate real DM-renderer test verifies new and historical
  applicant profile links using the bundled NIP-19 implementation.
- 62 packaging/OS/deployment checks passed, including actual Compose configuration
  rendering. Browser checks exercise two viewers, access revocation, real playback,
  menu forms, folder artwork, mobile/TV sizing and stream-slot cleanup.
- Fresh official upstream source comparison passed. Run
  `.venv/bin/python scripts/check_jellyfin_upstream.py` for client tests plus drift
  detection; `--drift-only` is the weekly CI source-drift check. `--record` explicitly
  accepts reviewed changes. Baseline drift is tested and never silently accepted.

Full suite passed: 10,736 tests, 630 subtests, 25 skipped.

## Deployment

Code commit `4a7e7a7d3114c8b70acfe1314e6a068bfb41b5ed` deployed successfully to
server1, NAS and router; origin/master and github/main matched. Both application
nodes' app/worker/media services were active after restart. Server1 reported no
startup traceback or import error. Public and local `/client` and Jellyfin server
info returned 200; malformed welcome proofs returned 422. Public app.js, welcome,
wallet stylesheet and service-worker bytes matched the checkout.

[Desktop 1.0.1491](https://github.com/loblawbob873-svg/posterchanai/releases/tag/desktop-v1.0.1491)
was built from that commit and published successfully for Windows, macOS and Linux.
The Windows update feed advertises 1.0.1491. The Gentoo desktop pin was updated to
1.0.1491 after verifying the Linux payload's published SHA512 checksum. Hardware
Windows/Roku behavior remains unobserved as described above.

Task-owned scanner environment, upstream scratch checkouts and recovery snapshot
were removed after preserving this report and committing the recovered files.
Other agents' working files and live application caches were left alone.

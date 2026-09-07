# Large-library scan completion and stale paths

The live Movies scans failed twice at the former 10,000-item cap. The interrupted
catalog retained 9,982 entries, including eight paths whose files had moved; the
source folder contained 11,450 candidate media files. Incremental scan checkpoints
retain previous entries until successful completion, so those scans never reached
the final replacement that removes obsolete paths.

The scan now supports a bounded 25,000 playable items per library. It retains the
existing encrypted page size and manifest commit behavior. Exceeding the limit
produces an actionable message instead of a generic FFmpeg/relay error. The scan
status endpoint now reports a persisted incomplete scan as interrupted after a
restart, consistent with the library listing.

Three added regressions cover a real temporary tree of 10,005 files, a whole-folder
move and deletion followed by successful catalog replacement, bounded encrypted
pages, preservation of the existing catalog on item-limit failure, and interrupted
status after process state is cleared. The large-library case fails with the old
10,000 limit and passes with the new bound. Focused result: 3 passed in 2.46 seconds.
Read-only peer review found no material blockers; root diff review also passed.
Evidence: /tmp/pc-media-scan-old-limit-red.log and
/tmp/pc-media-scan-capacity-tests.log.

Full backend suite: **7785 passed, 18 skipped, 519 subtests passed** in739.25s,
with the real Kotlin SDK1.7.1/1.8.12 and JavaScript Jellyfin SDK enabled.
Log: /tmp/pc-media-scan-full-backend.log. Live rescan verification follows rollout.

## Deployment incident and correction

The first rollout incorrectly restarted all service roles because
app/services/media_center.py lacked an explicit deploy_targets mapping. A playing
TV received HTTP500/connection-refused errors during the relay restart at02:37UTC.
The mapping now targets only the HTTP app; the mixed router/scanner release also
includes the existing worker target. Relay and dedicated live-streaming services
are excluded. A regression reproduces the mixed scanner/Android release paths and
checks the exact restart set; the mapping-only correction itself restarts nothing.
Deployment-target and role-split checks:60 passed (/tmp/pc-media-deploy-targets-tests.log).
Post-restart public login, both WebSocket messages and library/item decoding pass
both official SDKs (/tmp/pc-post-scan-deploy-tv-recovery.log). This verifies recovery,
not uninterrupted playback during a service restart.

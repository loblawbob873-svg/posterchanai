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
